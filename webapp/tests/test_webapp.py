"""Web app tests, all on fake instruments. No GPIB/VISA is opened: the live-gate tests replace every connect().

Run from the repo root:  python -m webapp.tests.test_webapp
Levels: smoke (server answers), unit (RunManager, validation, data paths), integration (API dry-runs through
measure.run_map.run, a fault, Stop), differential (web run == direct run_map.run, value for value).
"""
import contextlib
import json
import os
import shutil
import threading
import time

from fastapi.testclient import TestClient

from measure import run_map
from webapp import configs, data
from webapp.app import create_app
from webapp.runs import RunError, RunManager

TMP = os.path.join('.ignore', '_webapp_test')        # never inside data/ (AGENTS.md)
CFG_NAME = 'lc500_idvg.json'


def cfg():
    return configs.load_config(CFG_NAME)


@contextlib.contextmanager
def no_connections():
    """Any attempt to reach a real instrument fails the test instead of opening VISA."""
    def refuse(*a, **k):
        raise AssertionError("a test tried to connect to a real instrument")
    saved = [(m, m.connect) for m in (run_map.prober_mod, run_map.b1500_mod, run_map.k4200_mod)]
    for m, _ in saved:
        m.connect = refuse
    try:
        yield
    finally:
        for m, f in saved:
            m.connect = f


def wait_state(mgr, states, timeout=120):
    t0 = time.time()
    while mgr.state not in states:
        assert time.time() - t0 < timeout, f"timeout waiting for {states}, state {mgr.state}"
        time.sleep(0.02)
    return mgr.state


def events(mgr, kind):
    return [e for e in mgr.events_since(0) if e['type'] == kind]


# ---------- smoke ----------
def test_smoke():
    c = TestClient(create_app(RunManager(dryrun_data_dir=TMP)))
    h = c.get('/api/health').json()
    assert h['ok'] and h['allow_live'] is False
    assert c.get('/').status_code == 200 and 'Wafer-probe runs' in c.get('/').text
    assert CFG_NAME in c.get('/api/configs').json()
    print("test_smoke OK")


# ---------- unit ----------
def test_validate():
    assert configs.validate(cfg())['ok']
    bad = cfg(); bad['experiment'] = 'nope'
    assert any('experiment must be one of' in e for e in configs.validate(bad)['errors'])
    bad = cfg(); bad['device_csv'] = 'layouts/missing.csv'
    assert any('device_csv not found' in e for e in configs.validate(bad)['errors'])
    bad = cfg(); del bad['motion']
    assert "missing block 'motion'" in configs.validate(bad)['errors']
    bad = cfg(); bad['experiment'] = 'stress_stair'      # needs the 4200A
    assert not configs.validate(bad)['ok']
    for name in configs.list_configs():                  # every shipped config passes its own checks
        v = configs.validate(configs.load_config(name))
        assert v['ok'], (name, v['errors'])
    for name in ('../AGENTS.md', 'sub/x.json', 'x.txt'):
        try:
            configs.load_config(name)
            raise AssertionError(f"accepted {name}")
        except ValueError:
            pass
    print(f"test_validate OK ({len(configs.list_configs())} configs valid, bad configs rejected)")


def test_preview_matches_cli_plan():
    """Differential + tripwire: the web plan is run_map.build_plan's plan; device count and summed step agree."""
    c = cfg()
    for opts in ({}, {'columns': 'A,B', 'L_values': '2,1'}, {'limit': 5, 'origin': 'B:2'}):
        p = configs.preview(c, opts, TMP)
        o = configs.check_options(opts)
        plan, _ = run_map.build_plan(c, o.get('only'), o.get('columns'), o.get('L_values'), o.get('limit'),
                                     o.get('origin') or c['origin_device'], TMP, lambda m: None)
        assert [d['key'] for d in p['plan']] == [d['key'] for d in plan]
        assert len(p['plan']) == len(plan) > 0
        sx, sy = sum(d['dx'] for d in plan), sum(d['dy'] for d in plan)
        assert abs(sum(d['dx'] for d in p['plan']) - sx) < 1e-9 and abs(sum(d['dy'] for d in p['plan']) - sy) < 1e-9
        origin = [s for s in p['sites'] if s['origin']]
        assert len(origin) == 1 and abs(origin[0]['x']) < 1e-9 and abs(origin[0]['y']) < 1e-9
        last = p['plan'][-1]                              # the path ends where the summed steps say
        assert abs(last['tx'] - sx) < 1e-6 and abs(last['ty'] - sy) < 1e-6
    try:
        configs.preview(c, {'limit': 0}, TMP)
        raise AssertionError("limit 0 accepted")
    except ValueError:
        pass
    try:
        configs.preview(c, {'fakes': 1}, TMP)
        raise AssertionError("unknown option accepted")
    except ValueError:
        pass
    print("test_preview_matches_cli_plan OK")


def test_run_manager_gates():
    """Fake runner: states, one run at a time, live refused unless allowed, typed confirmation, stop at a gate."""
    started, release = threading.Event(), threading.Event()
    answers = []

    def runner(cfg, live, data_dir, interactive, log_sink, confirm_fn, stop_event, on_plan, on_row, **opts):
        assert interactive is True and cfg['show_plots'] is False
        log_sink('hello')
        on_plan([{'key': 'A:2'}])
        started.set()
        if live:
            answers.append(confirm_fn("LIVE RUN on b1500: idvg, 1 devices. Probes must be landed on A:2."))
            if answers[-1]:
                answers.append(confirm_fn("Check alignment at separation height."))
        release.wait(10)
        on_row({'key': 'A:2', 'status': 'measured', 'Vt_V': float('nan')})
        return [{'key': 'A:2'}], None

    mgr = RunManager(allow_live=False, dryrun_data_dir=TMP, runner=runner)
    try:
        mgr.start(cfg(), live=True)
        raise AssertionError("live run started without --allow-live")
    except RunError as e:
        assert 'disabled' in str(e)
    assert mgr.state == 'idle'

    mgr.start({**cfg(), 'show_plots': True})
    started.wait(5)
    try:
        mgr.start(cfg())
        raise AssertionError("second run accepted")
    except RunError:
        pass
    release.set()
    assert mgr.wait(10) == 'done'
    row = events(mgr, 'row')[0]
    assert row['row']['Vt_V'] is None and row['site'] == 'A:2'       # NaN -> null, strict JSON
    json.dumps(mgr.events_since(0), allow_nan=False)
    assert events(mgr, 'log')[0]['line'] == 'hello'

    # live: wrong text refused, origin accepted, then the alignment gate wants 'yes'
    mgr = RunManager(allow_live=True, dryrun_data_dir=TMP, runner=runner)
    started.clear(); release.clear()
    mgr.start(cfg(), live=True)
    wait_state(mgr, ('awaiting_confirm',))
    assert mgr.status()['expect'] == 'A:2'
    for wrong in ('yes', 'B:2', ''):
        try:
            mgr.confirm(wrong)
            raise AssertionError(f"accepted {wrong!r}")
        except RunError:
            pass
    mgr.confirm(' A:2 ')
    t0 = time.time()
    while mgr.status()['expect'] != 'yes':
        assert time.time() - t0 < 5
        time.sleep(0.01)
    mgr.confirm('yes')
    release.set()
    assert mgr.wait(10) == 'done' and answers == [True, True]

    # live: Stop while a gate waits answers 'no'
    answers.clear(); started.clear(); release.clear()
    mgr.start(cfg(), live=True)
    wait_state(mgr, ('awaiting_confirm',))
    mgr.stop()
    release.set()
    assert mgr.wait(10) == 'stopped' and answers == [False]
    print("test_run_manager_gates OK")


def test_data_paths():
    os.makedirs(os.path.join(TMP, 'x'), exist_ok=True)
    base = '.'
    for root, rel in (('data', '../AGENTS.md'), ('etc', ''), ('.ignore/web_dryrun', '../../AGENTS.md')):
        try:
            data.resolve(root, rel, base)
            raise AssertionError(f"resolved {root}/{rel}")
        except data.DataError:
            pass
    print("test_data_paths OK")


# ---------- integration ----------
def _api_dry_run(client, mgr, body):
    r = client.post('/api/runs', json=body)
    assert r.status_code == 200, r.text
    mgr.wait(300)
    return mgr.status()


def test_api_dry_run_equals_direct_run():
    """Differential: the same config and options through the web and through run_map.run give the same rows."""
    shutil.rmtree(TMP, ignore_errors=True)
    mgr = RunManager(dryrun_data_dir=os.path.join(TMP, 'web'))
    client = TestClient(create_app(mgr))
    with no_connections():
        st = _api_dry_run(client, mgr, {'cfg': cfg(), 'options': {'limit': 4}, 'config_name': CFG_NAME})
        assert st['state'] == 'done' and st['n_rows'] == 4, st
        web = [e['row'] for e in events(mgr, 'row')]
        direct, bench = run_map.run(cfg(), data_dir=os.path.join(TMP, 'direct'), limit=4)
    assert [r['key'] for r in web] == [r['key'] for r in direct]
    assert [r['status'] for r in web] == [r['status'] for r in direct] == ['measured'] * 4
    for a, b in zip(web, direct):
        assert a['Vt_V'] == b['Vt_V'] and a['on_off'] == b['on_off'], (a['key'], a['Vt_V'], b['Vt_V'])
    plan_ev = events(mgr, 'plan')[0]['sites']
    assert plan_ev == [r['key'] for r in web]
    # the files a row names are readable through the data API
    f = web[0]['main_file'].replace('\\', '/')
    root = os.path.join(TMP, 'web').replace('\\', '/')
    assert f.startswith(root + '/')
    data.DATA_ROOTS = data.DATA_ROOTS + (root,)
    t = client.get('/api/data/table', params={'root': root, 'path': f[len(root) + 1:]}).json()
    assert t['axes']['x'] == 'Gate_Voltage' and 'Drain_Current' in t['axes']['y'] and t['n_rows'] > 10
    print(f"test_api_dry_run_equals_direct_run OK (4 devices, Vt {web[0]['Vt_V']:.4f} V both ways)")


def test_api_stop_and_fault():
    """Stop mid-run ends through the safe cleanup; an open device is reported, not fatal; interlocks never trip."""
    from measure import device_map as dm
    c = cfg()
    stop_after = {'n': 2}

    def runner(cfg, **kw):
        full = dm.load_devices(cfg['device_csv'])
        fakes = run_map.make_fake_instruments(cfg, full, cfg['origin_device'], faults={'B:2': 'open'})
        on_row = kw['on_row']

        def row_then_stop(r):
            on_row(r)
            if mgr.info['n_rows'] >= stop_after['n']:
                mgr.stop()
        kw['on_row'] = row_then_stop
        out = run_map.run(cfg, fakes=fakes, **kw)
        runner.bench = fakes[0]
        return out

    mgr = RunManager(dryrun_data_dir=os.path.join(TMP, 'stop'), runner=runner)
    client = TestClient(create_app(mgr))
    with no_connections():
        st = _api_dry_run(client, mgr, {'cfg': c, 'options': {'limit': 6}})
    assert st['state'] == 'stopped' and st['n_rows'] == 2, st
    rows = [e['row'] for e in events(mgr, 'row')]
    assert rows[0]['status'] == 'measured' and rows[1]['key'] == 'B:2' and rows[1]['status'] != 'measured'
    lines = [e['line'] for e in events(mgr, 'log')]
    i = max(k for k, l in enumerate(lines) if 'Stop requested' in l)
    assert any('separation height' in l for l in lines[i:]), "probes not separated after Stop"
    bench_log = runner.bench.log
    assert bench_log and not runner.bench.bias_live and not runner.bench.in_contact
    print(f"test_api_stop_and_fault OK (stopped after 2 of 6; B:2 {rows[1]['status']}: {rows[1]['reason']})")


def test_live_gate_decline_never_connects():
    """A real run_map.run with live=True, declined at the browser gate: nothing connects, nothing is written."""
    mgr = RunManager(allow_live=True, dryrun_data_dir=os.path.join(TMP, 'live'))
    c = cfg()
    c['data_dir'] = os.path.join(TMP, 'live_data')
    with no_connections():
        mgr.start(c, live=True, options={'limit': 1})
        wait_state(mgr, ('awaiting_confirm', 'failed'))
        assert mgr.state == 'awaiting_confirm'
        prompt = events(mgr, 'confirm')[0]
        assert prompt['prompt'].startswith('LIVE RUN') and prompt['expect'] == 'A:2'
        mgr.decline()
        assert mgr.wait(30) == 'done'
    lines = [e['line'] for e in events(mgr, 'log')]
    assert any('Aborted by user before connecting' in l for l in lines)
    print("test_live_gate_decline_never_connects OK")


def test_api_rejects():
    c = TestClient(create_app(RunManager(dryrun_data_dir=TMP)))
    bad = cfg(); bad['experiment'] = 'nope'
    assert c.post('/api/runs', json={'cfg': bad}).status_code == 400
    assert c.post('/api/runs', json={'cfg': cfg(), 'live': True}).status_code == 409
    assert c.post('/api/runs', json={'cfg': cfg(), 'options': {'limit': -1}}).status_code == 400
    assert c.post('/api/runs/stop').status_code == 409
    assert c.post('/api/runs/confirm', json={'text': 'yes'}).status_code == 409
    assert c.get('/api/data/list', params={'root': 'data', 'path': '../..'}).status_code == 404
    assert c.post('/api/preview', json={'cfg': cfg(), 'options': {'origin': 'Z:99'}}).status_code == 400
    print("test_api_rejects OK")


if __name__ == '__main__':
    import io
    tests = [test_smoke, test_validate, test_preview_matches_cli_plan, test_run_manager_gates, test_data_paths,
             test_api_rejects, test_live_gate_decline_never_connects, test_api_dry_run_equals_direct_run,
             test_api_stop_and_fault]
    quiet = io.StringIO()
    try:
        for t in tests:
            with contextlib.redirect_stdout(quiet):
                t()
            print(quiet.getvalue().strip().splitlines()[-1]); quiet.seek(0); quiet.truncate()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("ALL WEB APP TESTS PASSED")
