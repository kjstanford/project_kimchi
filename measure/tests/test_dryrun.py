"""Dry-run tests with fake instruments. No GPIB/VISA is ever opened.

Run from the repo root:  python -m measure.tests.test_dryrun
"""
import copy
import json
import os
import shutil
import sys

# --- hard guard: any attempt to open a real VISA / pymeasure session fails the tests ---
import pyvisa


class _NoHardware(Exception):
    pass


def _blocked(*a, **k):
    raise _NoHardware("Attempted to open a real instrument session during tests")


pyvisa.ResourceManager = _blocked
import pymeasure.instruments.agilent as _pm_agilent  # noqa: E402
_pm_agilent.AgilentB1500 = _blocked

from measure import device_map as dm  # noqa: E402
from measure import run_map  # noqa: E402
from measure.instruments.fakes import SafetyViolation  # noqa: E402

DATA = os.path.join('.ignore', '_dryrun_test')     # never inside data/ (AGENTS.md)
CFG = json.load(open('configs/lc500_idvg.json'))


def fresh():
    shutil.rmtree(DATA, ignore_errors=True)
    return copy.deepcopy(CFG)


def cmds(bench, prefix):
    return [c.split(': ', 1)[1] for c in bench.log if c.startswith(prefix)]


def test_full_map():
    cfg = fresh()
    rows, bench = run_map.run(cfg, data_dir=DATA)
    assert len(rows) == 144, len(rows)
    assert all(r['status'] == 'measured' for r in rows), {r['status'] for r in rows}

    # serpentine: bottom row A->L, next row L->A, ...
    keys = [r['key'] for r in rows]
    assert keys[:12] == [f"{c}:2" for c in "ABCDEFGHIJKL"], keys[:12]
    assert keys[12:24] == [f"{c}:1" for c in "LKJIHGFEDCBA"], keys[12:24]

    # every XY move is a 200 um grid step, a row change, or a TLM gap jump; never > max_step
    moves = [c for c in cmds(bench, 'PROBER') if c.startswith('MoveChuck ')]
    assert len(moves) == 143, len(moves)
    for mv in moves:
        dx, dy = (-float(v) for v in mv.split()[1:3])
        assert (dx, dy) in {(200, 0), (-200, 0), (0, 200), (800, 0), (-800, 0)}, mv

    # Z/XY ordering: separation before every XY move, contact before every trigger,
    # DZ before every separation
    seq = bench.log
    for i, c in enumerate(seq):
        if c.startswith('PROBER: MoveChuck '):
            prev_z = [s for s in seq[:i] if 'MoveChuckSeparation' in s or 'MoveChuckContact' in s][-1]
            assert 'Separation' in prev_z, (i, c, prev_z)
        if c == 'PROBER: MoveChuckSeparation 10.':
            last_b = [s for s in seq[:i] if s.startswith('B1500:')]
            assert not last_b or last_b[-1] == 'B1500: DZ', (i, last_b[-3:])

    # SMU mapping gate=smu3(slot5), drain=smu1(slot3), source=smu2(slot4)
    b = cmds(bench, 'B1500')
    assert any(c.startswith('WV 5,') for c in b) and any(c.startswith('DV 3,0.05') for c in b)
    assert any(c.startswith('DV 4,0') for c in b)
    # only Vd = 0.05 V is ever applied to the drain (slot 3)
    assert {c.split(',')[1] for c in b if c.startswith('DV 3,')} == {'0.05'}, \
        {c for c in b if c.startswith('DV 3,')}
    assert not bench.bias_live and not bench.in_contact

    # files
    for r in rows:
        assert os.path.exists(r['scan_file']) and os.path.exists(r['main_file'])
        assert os.path.exists(r['main_file'].replace('.csv', '.png'))
    print("test_full_map OK")
    return cfg


def test_resume(cfg):
    plan, _ = run_map.build_plan(cfg, data_dir=DATA, log=lambda m: None)
    assert plan == [], len(plan)
    plan, _ = run_map.build_plan(dict(cfg, repeat_measurement=True), data_dir=DATA, log=lambda m: None)
    assert len(plan) == 144
    print("test_resume OK")


def test_fault_cleanup():
    cfg = fresh()
    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'A:2', fail_on_trigger=7)
    try:
        run_map.run(cfg, data_dir=DATA, fakes=fakes)
        raise AssertionError("expected OSError")
    except OSError:
        pass
    bench = fakes[0]
    tail = bench.log[-4:]
    assert 'B1500: DZ' in tail and 'PROBER: MoveChuckSeparation 10.' in tail, tail
    assert not bench.bias_live and not bench.in_contact
    print("test_fault_cleanup OK")


def test_keyboard_interrupt():
    cfg = fresh()
    full = dm.load_devices(cfg['device_csv'])
    bench, res, b = run_map.make_fake_instruments(cfg, full, 'A:2')
    orig, n = b.send_trigger, [0]

    def trig():
        n[0] += 1
        if n[0] == 6:  # 5 triggers per device -> first trigger of device 2
            raise KeyboardInterrupt
        orig()
    b.send_trigger = trig
    rows, _ = run_map.run(cfg, data_dir=DATA, fakes=(bench, res, b))
    assert len(rows) == 1, len(rows)
    assert not bench.bias_live and not bench.in_contact
    print("test_keyboard_interrupt OK")


def test_dead_device_screened():
    cfg = fresh()
    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'A:2', dead={'C:2'})
    rows, _ = run_map.run(cfg, data_dir=DATA, fakes=fakes, only='A:2,B:2,C:2,D:2')
    st = {r['key']: r['status'] for r in rows}
    assert st == {'A:2': 'measured', 'B:2': 'measured', 'C:2': 'screened_out', 'D:2': 'measured'}, st
    print("test_dead_device_screened OK")


def test_limits_and_origin():
    cfg = fresh()
    cfg['motion']['max_step_um'] = 150
    try:
        run_map.build_plan(cfg, data_dir=DATA, log=lambda m: None)
        raise AssertionError("expected max_step violation")
    except ValueError as e:
        assert 'max_step_um' in str(e)
    cfg = fresh()
    plan, _ = run_map.build_plan(cfg, only='K:0.3', origin='K:0.04', data_dir=DATA, log=lambda m: None)
    assert (plan[0]['dx'], plan[0]['dy']) == (0.0, -1400.0), plan[0]
    cfg['motion']['transform'] = {'rotate_deg': 180, 'mirror_x': False, 'mirror_y': False}
    plan, _ = run_map.build_plan(cfg, only='B:2', data_dir=DATA, log=lambda m: None)
    assert (round(plan[0]['dx'], 6), round(plan[0]['dy'], 6)) == (-200.0, 0.0), plan[0]
    # first row right->left from L:2: K..A on row 0, then A->L on row 1
    cfg = fresh()
    cfg['first_row'] = 'right'
    plan, _ = run_map.build_plan(cfg, origin='L:2', columns=list("ABCDEFGHIJK") + ['L'], L_values=['2', '1'],
                                 data_dir=DATA, log=lambda m: None)
    keys = [p['key'] for p in plan]
    assert keys[:12] == [f"{c}:2" for c in "LKJIHGFEDCBA"], keys[:12]
    assert keys[12:24] == [f"{c}:1" for c in "ABCDEFGHIJKL"], keys[12:24]
    assert (plan[0]['dx'], plan[0]['dy']) == (0.0, 0.0) and (plan[1]['dx'], plan[1]['dy']) == (-200.0, 0.0)
    print("test_limits_and_origin OK")


def test_chuck_drift_guard():
    """The S200 error seen live: (-200, 0) commanded -> (-203.557, +3.198) read back.
    One move like that is within tolerance; the accumulated drift must stop the run."""
    import math
    from measure.instruments.fakes import FakeVisaProber
    from measure.instruments.prober import ProberError
    scale = math.hypot(-203.557, 3.198) / 200.0
    rot = math.degrees(math.atan2(3.198, -203.557) - math.pi)
    cfg = fresh()
    full = dm.load_devices(cfg['device_csv'])
    bench, _, b = run_map.make_fake_instruments(cfg, full, 'A:2')
    res = FakeVisaProber(bench, move_scale=scale, move_rot_deg=rot)
    b.device_lookup = lambda: (10.0, 2.0)
    try:
        run_map.run(cfg, data_dir=DATA, fakes=(bench, res, b), only=','.join(f"{c}:2" for c in "ABCDEFGHIJKL"))
        raise AssertionError("expected drift error")
    except ProberError as e:
        assert 'drift' in str(e), e
    n_moves = sum(1 for c in bench.log if c.startswith('PROBER: MoveChuck '))
    assert n_moves == 5, n_moves   # 4.8 um/move -> stops on the 5th move (> 20 um)
    assert not bench.bias_live and not bench.in_contact
    print(f"test_chuck_drift_guard OK (stopped after {n_moves} moves)")


def test_fmt21_parse():
    import math
    from measure.fet_idvg import parse_fmt21, status_flags
    raw = ("000EI+1.000000E-12,000CI+2.000000E-06,000DI-2.000000E-06,  WEV-1.000000E+00,"
           "008EI+5.000000E-03,004CI+3.000000E-06,004DI-5.003000E-03,  WEV+0.000000E+00,"
           "000EI+1.100000E-12,001CI+199.9990E+99,000DI-4.000000E-06,  WEV+1.000000E+00,"
           "000EI+1.200000E-12,064CI+0.000000E+00,128DI-5.000000E-06,  EEV+2.000000E+00")
    d = parse_fmt21(raw, {'E': 'Gate', 'C': 'Drain', 'D': 'Source'})
    assert len(d) == 4, d
    assert list(d['Gate_Voltage']) == [-1.0, 0.0, 1.0, 2.0]
    assert list(d['Gate_Status']) == [0, 8, 0, 0] and list(d['Drain_Status']) == [0, 4, 1, 64]
    assert list(d['Source_Status']) == [0, 4, 0, 0]          # EOD bit (128) masked out
    assert math.isnan(d['Drain_Current'][2]) and math.isnan(d['Drain_Current'][3])
    assert d['Drain_Current'][1] == 3e-6 and d['Source_Current'][3] == -5e-6
    assert status_flags(d) == 'gate compliance x1; drain overflow/invalid x2', status_flags(d)
    print("test_fmt21_parse OK")


def test_fault_devices_and_compliance():
    cfg = fresh()
    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'A:2',
                                          faults={'B:2': 'short', 'C:2': 'gate_break', 'D:2': 'overflow'})
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=fakes, only='A:2,B:2,C:2,D:2')
    r = {x['key']: x for x in rows}
    assert r['A:2']['status'] == 'measured' and r['A:2']['flags'] == '', r['A:2']
    assert r['B:2']['status'] == 'screened_out' and r['B:2']['reason'].startswith('S/D short: Id flat ~1.25 mA'), r['B:2']
    assert r['B:2']['Vt_V'] != r['B:2']['Vt_V']                       # NaN, not a bogus fit
    assert r['C:2']['status'] == 'screened_out' and r['C:2']['reason'].startswith('gate compliance'), r['C:2']
    assert 'gate compliance' in r['C:2']['flags'], r['C:2']
    assert r['D:2']['status'] == 'measured' and 'drain overflow/invalid' in r['D:2']['flags'], r['D:2']
    import pandas as pd
    d = pd.read_csv(r['D:2']['main_file'])
    assert {'Gate_Status', 'Drain_Status', 'Source_Status'} <= set(d.columns) and d['Drain_Current'].isna().any()
    # 5 mA compliance reaches every source command
    b = cmds(bench, 'B1500')
    comps = {c.split(',')[-1] for c in b if c.startswith(('DV 3,', 'DV 4,', 'WV 5,'))}
    assert comps == {'0.005'}, comps
    summ = [f for f in os.listdir(os.path.join(DATA, 'fluorine', 'top_right')) if f.startswith('summary_')]
    assert 'flags' in open(os.path.join(DATA, 'fluorine', 'top_right', sorted(summ)[-1])).readline()
    print("test_fault_devices_and_compliance OK")


def test_igzo_v6_device_id_map():
    """IGZO v6 mask: device_id keys, per-device W, fixed -1..3 V gate sweep, Vd 0.05 + 1.5 V, smu1/2/3 = G/D/S."""
    import pandas as pd
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_idvg.json'))
    full = dm.load_devices(cfg['device_csv'])
    assert len(full) == 984 and full.set_index('key').loc['R42C29R', ['W', 'L']].tolist() == [10, 4]
    plan, _ = run_map.build_plan(cfg, only='r42c29r,R41C29R', data_dir=DATA)
    assert [p['key'] for p in plan] == ['R42C29R', 'R41C29R'], [p['key'] for p in plan]
    assert (plan[1]['dx'], plan[1]['dy']) == (0, 290), plan[1]

    rows, bench = run_map.run(cfg, data_dir=DATA, limit=26)
    keys = [r['key'] for r in rows]
    # bottom row right -> left starting on the origin, then the next row up left -> right
    assert keys[0] == 'R42C29R' and keys[1] == 'R42C29L' and keys[23] == 'R42C18L', keys[:24]
    assert keys[24:26] == ['R41C18L', 'R41C18R'], keys[24:]
    assert all(r['status'] == 'measured' for r in rows), {r['key']: r['reason'] for r in rows if r['reason']}
    moves = [tuple(-float(v) for v in c.split()[1:3]) for c in cmds(bench, 'PROBER') if c.startswith('MoveChuck ')]
    assert len(moves) == 25, len(moves)            # no move for the origin itself
    assert all((dy == 0 and 328 < abs(dx) < 437) or (dx, dy) == (0, 290) for dx, dy in moves), moves
    assert all(dx < 0 for dx, _ in moves[:23]) and moves[23] == (0, 290) and moves[24][0] > 0, moves

    b = cmds(bench, 'B1500')
    wv = {tuple(c.split(',')[2:4]) for c in b if c.startswith('WV ')}
    assert {c.split(',')[0] for c in b if c.startswith('WV ')} == {'WV 3'}, 'gate must be smu1 (slot 3)'
    assert wv == {('-1.0', '3.0')}, wv            # scan and main sweep both -1 -> 3 V
    assert {float(c.split(',')[1]) for c in b if c.startswith('DV 4,')} == {0.05, 1.5}   # drain = smu2
    assert {float(c.split(',')[1]) for c in b if c.startswith('DV 5,')} == {0.0}         # source = smu3

    r0 = rows[0]
    assert r0['W_um'] == 10 and r0['L_um'] == 4 and r0['gate_start_V'] == -1.0, r0
    folder = os.path.join(DATA, 'wd_bobafet_6916', '0_0', 'R42C29R')
    assert os.path.dirname(r0['main_file']) == folder and r0['main_file'].endswith('_wd_bobafet_6916_0_0_R42C29R_W10_L4.csv')
    d = pd.read_csv(r0['main_file'])
    assert set(d['Drain_Voltage']) == {0.05, 1.5} and set(d['Cycle']) == {1, 2}
    assert d['Gate_Voltage'].min() == -1.0 and d['Gate_Voltage'].max() == 3.0
    # per-device W/L reaches the (fake) device: W10/L2 next door carries ~2x the current of W10/L4
    d1 = pd.read_csv(rows[1]['main_file'])
    on = lambda x: x[(x['Drain_Voltage'] == 0.05) & (x['Cycle'] == 1)]['Drain_Current'].abs().max()
    assert 1.8 < on(d1) / on(d) < 2.2, on(d1) / on(d)
    summ = sorted(f for f in os.listdir(os.path.dirname(folder)) if f.startswith('summary_'))[-1]
    assert 'W_um' in open(os.path.join(os.path.dirname(folder), summ)).readline()

    # resume (probes left on the last device -> --origin) skips what was measured
    plan, _ = run_map.build_plan(cfg, limit=3, origin='R41C18R', data_dir=DATA)
    assert plan[0]['key'] == 'R41C19L' and (plan[0]['dx'], plan[0]['dy']) == (328.092, 0), plan[0]
    print("test_igzo_v6_device_id_map OK")


def test_drift_correction():
    """Systematic chuck error (0.3 % scale + 0.2 deg: ~1.3 um x / ~1.5 um y per 436 um step).
    Without correction the accumulated drift trips max_drift_um; with drift_correct_um = 5 every
    axis is cancelled once it reaches +/-5 um, so the run completes and lands on every device."""
    import re
    from measure.instruments.prober import ProberError
    err = dict(move_scale=1.003, move_rot_deg=0.2)
    base = json.load(open('configs/bobafet_6916_idvg.json'))
    assert base['motion']['drift_correct_um'] == 5.0

    cfg = copy.deepcopy(base); cfg['motion']['drift_correct_um'] = None
    shutil.rmtree(DATA, ignore_errors=True)
    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'R42C29R', prober_kw=err)
    try:
        run_map.run(cfg, data_dir=DATA, limit=26, fakes=fakes)
        raise AssertionError("expected drift error without correction")
    except ProberError as e:
        assert 'drift' in str(e), e

    cfg = copy.deepcopy(base)
    shutil.rmtree(DATA, ignore_errors=True)
    fakes = run_map.make_fake_instruments(cfg, full, 'R42C29R', prober_kw=err)
    rows, bench = run_map.run(cfg, data_dir=DATA, limit=26, fakes=fakes)
    assert len(rows) == 26 and all(r['status'] == 'measured' for r in rows), [r['reason'] for r in rows]
    die = os.path.join(DATA, 'wd_bobafet_6916', '0_0')
    log = open(os.path.join(die, sorted(f for f in os.listdir(die) if f.startswith('run_'))[-1])).read()
    drifts = [(float(a), float(b)) for a, b in re.findall(r'accumulated drift \(([-+.\d]+), ([-+.\d]+)\)', log)]
    n_corr = log.count('Drift correction:')
    assert len(drifts) == 25 and n_corr >= 4, (len(drifts), n_corr)
    assert max(abs(v) for d in drifts for v in d) < 5 + 2, drifts   # threshold + one move's error
    # the move after a correction carries it: a commanded step that is not a pure layout step
    moves = [tuple(float(v) for v in c.split()[1:3]) for c in cmds(bench, 'PROBER') if c.startswith('MoveChuck ')]
    odd = [m for m in moves if not any(abs(abs(m[0]) - s) < 1e-3 for s in (436, 329, 436.908, 328.092, 0))
           or abs(m[1]) not in (0, 290)]
    assert len(odd) == n_corr, (len(odd), n_corr)
    print(f"test_drift_correction OK ({n_corr} corrections, max |drift| "
          f"{max(abs(v) for d in drifts for v in d):.2f} um)")


def test_long_move_hops_keep_order():
    """--keep-order visits devices as given; a step > max_step_um is split into equal verified hops."""
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_idvg.json'))
    only = 'R42C29L,R22C18L,R30C25R'
    plan, _ = run_map.build_plan(cfg, only=only, origin='R42C29L', data_dir=DATA, keep_order=True)
    assert [p['key'] for p in plan] == only.split(','), [p['key'] for p in plan]
    assert [p['hops'] for p in plan] == [1, 4, 3], [(p['dx'], p['dy'], p['hops']) for p in plan]
    cfg2 = copy.deepcopy(cfg); cfg2['motion']['split_long_moves'] = False
    try:
        run_map.build_plan(cfg2, only=only, origin='R42C29L', data_dir=DATA, keep_order=True)
        raise AssertionError("expected max_step_um error without split_long_moves")
    except ValueError as e:
        assert 'max_step_um' in str(e), e

    full = dm.load_devices(cfg['device_csv'])
    rows, bench = run_map.run(cfg, data_dir=DATA, only=only, origin='R42C29L', keep_order=True,
                              fakes=run_map.make_fake_instruments(cfg, full, 'R42C29L'))
    assert [r['key'] for r in rows] == only.split(',') and all(r['status'] == 'measured' for r in rows), rows
    assert [(r['W_um'], r['L_um']) for r in rows] == [(10, 2), (1, 1), (2, 4)], rows
    moves = [tuple(-float(v) for v in c.split()[1:3]) for c in cmds(bench, 'PROBER') if c.startswith('MoveChuck ')]
    assert len(moves) == 7 and all((dx * dx + dy * dy) ** .5 <= cfg['motion']['max_step_um'] for dx, dy in moves)
    for p, seg in ((plan[1], moves[:4]), (plan[2], moves[4:])):
        assert abs(sum(m[0] for m in seg) - p['dx']) < 0.01 and abs(sum(m[1] for m in seg) - p['dy']) < 0.01
    # no XY motion with the probes down or bias on is enforced by the fakes (SafetyViolation)
    print("test_long_move_hops_keep_order OK")


def test_max_id_scales_with_WL():
    """screen.Id_max_min_per_WL replaces the fixed Id_max_min with value * W/L (low-W/L devices are not skipped).
    Fake devices carry ~4.3e-7 A * W/L at Vg = 3 V: R42C18R is W1/L10, R42C18L is W1/L1."""
    base = json.load(open('configs/bobafet_6916_idvg.json'))
    assert base['measurement']['screen']['Id_max_min_per_WL'] == 3e-7
    only = 'R42C18R,R42C18L'
    full = dm.load_devices(base['device_csv'])

    def screen(**s):
        cfg = copy.deepcopy(base)
        cfg['measurement']['screen'].pop('Id_max_min_per_WL')
        cfg['measurement']['screen'].update(s)
        shutil.rmtree(DATA, ignore_errors=True)
        rows, _ = run_map.run(cfg, data_dir=DATA, only=only, origin='R42C18R', keep_order=True,
                              fakes=run_map.make_fake_instruments(cfg, full, 'R42C18R'))
        return {r['key']: (r['status'], r['reason']) for r in rows}

    fixed = screen(Id_max_min=1e-6)                        # fixed limit: only the small W/L device fails
    assert fixed['R42C18R'] == ('screened_out', 'max Id < 1e-06 A') and fixed['R42C18L'][0] == 'measured', fixed
    ok = screen(Id_max_min=1e-6, Id_max_min_per_WL=3e-6)   # per-W/L wins over the fixed value
    assert all(v[0] == 'measured' for v in ok.values()), ok
    tight = screen(Id_max_min_per_WL=1e-5)                 # same relative bar for both sizes
    assert tight == {'R42C18R': ('screened_out', 'max Id < 1e-06 A (1e-05 A x W/L)'),
                     'R42C18L': ('screened_out', 'max Id < 1e-05 A (1e-05 A x W/L)')}, tight
    print("test_max_id_scales_with_WL OK")


K4200_CFG = 'configs/bobafet_6916_idvg_k4200.json'


def test_k4200_map():
    """4200A over KXCI (real driver, fake resource): smu1/2/3 = G/D/S, -1 -> 2 V list double sweep in 25 mV
    steps (Clarius osfet-id-vgs settings: 0.1 A compliance, 1 uA lowest range, 10 ms delay), outputs off after
    every sweep and before every move, faults screened as on the B1500, same CSV columns."""
    import pandas as pd
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open(K4200_CFG))
    m = cfg['measurement']
    assert run_map.smu_tool(cfg)[0] == 'k4200' and cfg['origin_device'] == 'R33C26L'
    assert (m['default_start'], m['default_stop'], m['screen']['Id_max_min_per_WL']) == (-1.0, 2.0, 1e-7)
    full = dm.load_devices(cfg['device_csv'])
    only = 'R36C18R,R36C18L,R35C18L,R35C18R,R34C18R'
    fakes = run_map.make_fake_instruments(cfg, full, 'R36C18R',
                                          faults={'R36C18L': 'gate_break', 'R35C18L': 'overflow', 'R35C18R': 'short'})
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=fakes, only=only, keep_order=True, origin='R36C18R')
    r = {x['key']: x for x in rows}
    assert list(r) == only.split(','), list(r)
    assert r['R36C18R']['status'] == 'measured' and r['R36C18R']['flags'] == '', r['R36C18R']
    assert r['R36C18L']['status'] == 'screened_out' and r['R36C18L']['reason'].startswith('gate compliance'), r['R36C18L']
    assert r['R35C18L']['status'] == 'measured' and 'drain overflow/invalid' in r['R35C18L']['flags'], r['R35C18L']
    assert r['R35C18R']['status'] == 'screened_out' and r['R35C18R']['reason'].startswith('S/D short'), r['R35C18R']
    assert r['R34C18R']['status'] == 'measured', r['R34C18R']

    k = cmds(bench, 'K4200')
    vl = [c for c in k if c.startswith('VL')]
    assert all(c.startswith('VL1, 1, 0.1, ') for c in vl), 'gate must be smu1, master, 0.1 A compliance'
    for c in vl:
        pts = [float(v) for v in c.split(',')[3:]]
        n = len(pts) // 2
        assert len(pts) == 242 and pts[:n] == pts[n:][::-1] and (pts[0], pts[n - 1]) == (-1.0, 2.0), c[:60]
    assert len(vl) == 3 * 3 + 2, len(vl)                  # 3 measured x (scan + 2 main), 2 screened x scan
    assert {c for c in k if c.startswith(('RG ', 'DT '))} == {'RG 1, 1e-06', 'RG 2, 1e-06', 'RG 3, 1e-06', 'DT 0.01'}
    assert {float(c.split(',')[1]) for c in k if c.startswith('VC2,')} == {0.05, 1.5}    # drain = smu2
    assert {float(c.split(',')[1]) for c in k if c.startswith('VC3,')} == {0.0}          # source = smu3
    assert all(k.count(f'ST {ch}, 1') == len(vl) for ch in (1, 2, 3))
    assert k.count('CH4') == len(vl) and k.count('ME1') == len(vl)
    # after every test: user mode, drain/source/gate outputs off, before the chuck moves again
    seq = bench.log
    for i, c in enumerate(seq):
        if c == 'K4200: ME1':
            nxt = [s for s in seq[i + 1:] if s.startswith('PROBER') or s in ('K4200: US', 'K4200: DV1')]
            assert nxt[:2] == ['K4200: US', 'K4200: DV1'], nxt[:3]
    assert not bench.bias_live and not bench.in_contact

    d = pd.read_csv(r['R36C18R']['main_file'])
    assert set(d.columns) == {'Drain_Current', 'Gate_Current', 'Gate_Voltage', 'Source_Current', 'Drain_Status',
                              'Gate_Status', 'Source_Status', 'Gate_Voltage_meas', 'Drain_Voltage', 'Cycle'}
    assert set(d['Drain_Voltage']) == {0.05, 1.5} and set(d['Cycle']) == {1} and len(d) == 2 * 242
    assert r['R36C18R']['main_file'].endswith(os.path.join('0_0', 'R36C18R', os.path.basename(r['R36C18R']['main_file'])))
    assert pd.read_csv(r['R35C18L']['main_file'])['Drain_Current'].isna().any()

    # an I/O fault mid-run: outputs off and chuck separated in cleanup
    fakes = run_map.make_fake_instruments(cfg, full, 'R36C18R', fail_on_trigger=3)
    try:
        run_map.run({**cfg, 'repeat_measurement': True}, data_dir=DATA, fakes=fakes, only='R36C18R', origin='R36C18R')
        raise AssertionError("expected OSError")
    except OSError:
        pass
    tail = fakes[0].log[-6:]
    assert 'K4200: DV1' in tail and 'PROBER: MoveChuckSeparation 10.' in tail, tail
    assert not fakes[0].bias_live and not fakes[0].in_contact
    print("test_k4200_map OK")


def test_k4200_driver_kxci():
    """KXCI details: DO parsing, data-ready latch, syntax errors, optional commands, standby, timeout, RPMs."""
    from measure.instruments import k4200 as K
    from measure.instruments.fakes import Bench, FakeK4200Visa
    from measure import fet_idvg_k4200 as F
    quiet = lambda *a: None
    assert K.parse_do('N 4.3555E-15,C+1.0000E-03,V+1.0000E+37,') == [('N', 4.3555e-15), ('C', 1e-3), ('V', 1e37)]
    assert K._num(-0.30000000000000004, 4) == '-0.3000' and K._num(-1e-7, 4) == '0.0000' and K._num(1e-9) == '1e-09'

    def make(**kw):
        bench = Bench(); bench.in_contact = True
        fake = FakeK4200Visa(bench, **kw)
        k = K.K4200(fake, log=quiet, poll_s=0, sweep_timeout_s=5)
        return bench, fake, k, K.get_smus(k, {'gate': 'smu1', 'drain': 'smu2', 'source': 'smu3'})

    # test already finished when the syntax check right after ME1 polls: B0 must not be lost
    bench, fake, k, smus = make(busy_polls=0)
    d = F.IdVg_single_Vd(k, *smus, -1, 2, 0.1, 0.05, 1e-9, None, 0.005)
    assert len(d) == 62 and not bench.bias_live

    # rejected command -> K4200Error with the KXCI message; optional -> logged once, then not resent
    try:
        k.write('VC2, 0.05, 0.005')                     # page is US now
        raise AssertionError('expected K4200Error')
    except K.K4200Error as e:
        assert '-989' in str(e), e
    k.write('ZZ1', optional=True); k.write('ZZ1', optional=True)
    assert cmds(bench, 'K4200').count('ZZ1') == 1

    # without ST n,1 the fake keeps the outputs on after the test; outputs_off clears them
    bench, fake, k, smus = make()
    w = k.write
    k.write = lambda c, optional=False: None if c.startswith('ST ') else w(c, optional)
    k.list_sweep(*smus, F.gate_list(-1, 2, 0.5), 1.5, 0.005, 1e-9)
    assert bench.bias_live
    K.zero_outputs(k)
    assert not bench.bias_live

    # no data ready -> abort with ME4 and raise
    bench, fake, k, smus = make(busy_polls=10 ** 9)
    k.sweep_timeout_s = 0
    try:
        k.list_sweep(*smus, [0.0, 0.0], 0.05, 0.005, 1e-9)
        raise AssertionError('expected K4200Error')
    except K.K4200Error:
        assert cmds(bench, 'K4200')[-1] == 'ME4'

    # RPMs: switched to SMU mode once, back to pulse on close; duplicate SMUs rejected
    bench, fake, k, smus = make(opt='SMUPA1,SMUPA2,SMUPA3,SMU4,PMU1RPM1-2,PMU2RPM1-2,,')
    for _ in range(2):
        F.IdVg_single_Vd(k, *smus, -1, 2, 0.5, 0.05, 1e-9, None, 0.005)
    k.close()
    rp = [c for c in cmds(bench, 'K4200') if c.startswith('RP ')]
    assert rp == ['RP PMU1-1, 2', 'RP PMU1-2, 2', 'RP PMU2-1, 2', 'RP PMU1-1, 0', 'RP PMU1-2, 0', 'RP PMU2-1, 0'], rp
    try:
        K.get_smus(k, {'gate': 'smu1', 'drain': 'smu1', 'source': 'smu3'})
        raise AssertionError('expected ValueError')
    except ValueError:
        pass

    # the real tool (2026-10-15): no preamps, and 'RG n, 1e-09' is rejected with -993 and no status bit
    real_opt = 'SMU1,SMU2,SMU3,SMU4,SMU5,PMU1RPM1-2,PMU2RPM1-2,PMU3RPM1-2,PMU4RPM1-2'
    bench, fake, k, smus = make(opt=real_opt)
    assert k.preamp == {1: False, 2: False, 3: False, 4: False, 5: False} and k.no_error == ''
    try:
        K.check_settings(k, smus, {'Irange': 1e-9})
        raise AssertionError('expected K4200Error')
    except K.K4200Error as e:
        assert 'preamp' in str(e), e
    K.check_settings(k, smus, json.load(open(K4200_CFG))['measurement'])     # config Irange 1e-6 passes
    try:
        k.list_sweep(*smus, [0.0, 1.0], 0.05, 0.005, 1e-9)                    # check_settings bypassed
        raise AssertionError('expected K4200Error')
    except K.K4200Error as e:
        assert '-993' in str(e) and 'sweep setup' in str(e), e
    assert 'ME1' not in cmds(bench, 'K4200'), 'must stop before the test runs'
    assert len(F.IdVg_single_Vd(k, *smus, -1, 2, 0.5, 0.05, 1e-7, None, 0.005)) == 14

    # no reply to :ERROR:LAST:GET -> logged warning, check off, sweeps still work
    msgs = []
    bench = Bench(); bench.in_contact = True
    fake = FakeK4200Visa(bench, opt=real_opt)
    fake.query = (lambda q, orig=fake.query: (_ for _ in ()).throw(TimeoutError('VI_ERROR_TMO'))
                  if q == ':ERROR:LAST:GET' else orig(q))
    k = K.K4200(fake, log=msgs.append, poll_s=0)
    assert k.no_error is None and any('WARNING' in m for m in msgs), msgs
    smus = K.get_smus(k, {'gate': 'smu1', 'drain': 'smu2', 'source': 'smu3'})
    assert len(F.IdVg_single_Vd(k, *smus, -1, 2, 0.5, 0.05, 1e-7, None, 0.005)) == 14
    print("test_k4200_driver_kxci OK")


def _windows_after(n_units, n_windows):
    """The live L0b/L0c test structure: n_units x 1 ms at 0 V, then n_windows back-to-back 20 us plateaus with
    10-19 us windows, all at 0 V (was pmu_bringup._windows_after, archived)."""
    from measure.instruments import k4200_pmu as P
    z = (0.0, 0.0, 0.0)
    reads = P.Seq('reads', 'spot', [P.Seg(100, z, z)] + [P.Seg(2000, z, z, meas=True, level=i)
                                                        for i in range(n_windows)] + [P.Seg(100, z, z)])
    seqs, entries = {2: reads}, [(2, 1, dict(phase='L0', read='spot'))]
    if n_units:
        seqs[1] = P.Seq('unit_1ms', 'unit', P.hold3(0.0, 100_000))
        entries.insert(0, (1, n_units, dict(phase='L0')))
    return P.renumber(P.Waveform('L0', seqs, entries, (1, 2, 3), 1000, 1900))


def _refused(fn, text):
    from measure.instruments import k4200_pmu as P
    try:
        fn()
    except P.SarbError as e:
        assert text in str(e), (text, str(e))
        return
    raise AssertionError(f"expected SarbError with {text!r}")


def test_sarb_rules_and_rate_model():
    """k4200_pmu limits: the KXCI rate model gives the rates seen live (L0b/L0c 2026-10-15), and every broken
    rule is refused before anything could be sent: the KULT bias_stress.c timing on the 1 uA range, the 10 ns
    grid, > 1 s segments, a non-seamless step, sequence-id gaps, < 3 segments, > 2048 segments, the samples per
    A/D, the 1-9 s rate trap and KXCI's -951 zone. Commands stay <= 128 values."""
    import copy
    import dataclasses
    from measure import stress_stair as S
    from measure.instruments import k4200_pmu as P
    for units, n_win, seen in ((0, 50, 9090909), (5000, 50, 199800), (20000, 50, 9090909),
                               (1000, 1, 995025), (20000, 1, 200e6)):
        assert abs(P.kxci_rate(_windows_after(units, n_win)) - seen) < 1, (units, n_win)
    p = S.params({})
    wf = S.build_waveform(p, (1, 2, 3))
    rate = S._settings(p, wf)['rate']
    assert rate == 100_000_000 and P.validate(wf, 1e-5, rate=rate, min_kxci_spw=4)['kxci_samples_per_window'] > 6
    _refused(lambda: P.validate(wf, 1e-5, rate=200_000_000), 'samples per A/D')
    _refused(lambda: P.validate(wf, 1e-6), 'plateau 2e-05 s < 2.04e-05 s')          # 20 us is too short for 1 uA
    kult = S.build_waveform(dict(p, plateau_s=2e-6, meas_start_s=1e-6, meas_stop_s=1.8e-6, edge_s=100e-9), (1, 2, 3))
    _refused(lambda: P.validate(kult, 1e-6), 'plateau 2e-06 s < 2.04e-05 s')        # osfet-bias-stress#1 timing
    _refused(lambda: P.validate(kult, 1e-6), 'transition 1e-07 s < 3.6e-07 s')
    _refused(lambda: P.ticks(1.234e-8), 'not on the 10 ns grid')
    stair = next(sid for sid, q in wf.seqs.items() if q.name == 'stair')
    longseg = copy.deepcopy(wf)
    longseg.seqs[stair].segs[2] = dataclasses.replace(longseg.seqs[stair].segs[2], n=2 * P.T_MAX_TICKS)
    _refused(lambda: P.validate(longseg, 1e-5), 'outside 20 ns..1 s')
    broken = copy.deepcopy(wf)
    seg = broken.seqs[stair].segs[5]
    broken.seqs[stair].segs[5] = dataclasses.replace(seg, v1=(seg.v1[0] + 0.01,) + seg.v1[1:])
    _refused(lambda: P.validate(broken, 1e-5), '!= previous stop')
    gap = copy.deepcopy(wf)
    top = max(gap.seqs)
    gap.seqs[top + 1] = gap.seqs.pop(top)
    gap.entries = [(top + 1 if sid == top else sid, loops, info) for sid, loops, info in gap.entries]
    _refused(lambda: P.validate(gap, 1e-5), 'without gaps')
    assert sorted(P.renumber(gap).seqs) == list(range(1, top + 1)) and P.validate(gap, 1e-5)
    short = copy.deepcopy(wf)
    sid = next(sid for sid, q in short.seqs.items() if q.name == 'stdby')
    short.seqs[sid].segs = short.seqs[sid].segs[:2]
    _refused(lambda: P.validate(short, 1e-5), 'a sequence needs >= 3')
    _refused(lambda: S.build_waveform(dict(p, stair_step_v=0.001), (1, 2, 3)), 'earlier than the end of the previous')
    fine = S.build_waveform(dict(p, stair_step_v=0.001, recovery_times_s=[]), (1, 2, 3))     # 63 ms staircases
    _refused(lambda: P.validate(fine, 1e-5), 'segments per channel > 2048')
    _refused(lambda: P.validate(_windows_after(5000, 300), 1e-5, rate=1_000_000, min_kxci_spw=4),
             'KXCI will lower the rate')                                            # 5 s: ~1.8 samples per window
    stress_only = S.build_waveform(dict(p, recovery_times_s=[], min_duration_s=0), (1, 2, 3))     # 10.0 s
    _refused(lambda: P.validate(stress_only, 1e-5, rate=200_000_000), '-951')
    for c in P.commands(wf, 1e-5, rate=rate):
        n = len(c.split(',')) - (2 if ':SARB:SEQ:' in c else 1)
        assert n <= (64 if ':WFM:' in c else 128), c[:60]
    for bad in ({'vstres': 3.0}, {'stress_times_s': [1.0, 0.5]}, {'recovery_times_s': [0.0]}, {'id_from': 'gate'},
                {'vstress': 1.5}):
        try:
            S.params(bad)
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass
    print("test_sarb_rules_and_rate_model OK")


def test_stress_stair_map():
    """KULT bias_stress.c scheme on :PMU:SARB: stdby | stair 0 | (stdby | stress | stdby | stair)... | stdby |
    (0 V hold | recovery stair | stdby)... Every stdby is 10 us with all channels at 0 V, every stress is flat at
    3 V for exactly the increment of the cumulative time (gate only), every staircase is -1 -> 2 V in 0.1 V at
    Vd 0.1 V (drain first) with 1 us edges and 20 us plateaus, each recovery staircase starts exactly its
    recovery time after the end of the stress. One fake device through run_map: dVt follows the fake's hidden
    PBTI model in stress and recovery; resume skips the device afterwards."""
    import math
    import numpy as np
    import pandas as pd
    from measure import stress_stair as S
    from measure.instruments import k4200_pmu as P
    from measure.instruments.fakes import FakeK4200Visa
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_stress_stair_k4200.json'))
    assert (cfg['stress_stair']['vt_id_per_wl'], cfg['stress_stair']['vt_id_min_a']) == (1e-8, 2e-8)   # 60 nA at W6/L1
    # the fake FET's subthreshold is far steeper than the real IGZO devices: 60 nA falls within one 0.1 V step of
    # its noise floor, so the model comparison runs at 1e-7 x W/L (the real 19:57 data crosses 60 nA cleanly)
    cfg['stress_stair'].update(poll_s=0, vt_id_per_wl=1e-7, vt_id_min_a=1e-7)
    p, wf, b = S.check_config(cfg)
    st, rt = p['stress_times_s'], p['recovery_times_s']
    assert st == [1e-3, 5e-3, 1e-2, 5e-2, 0.1, 0.5, 1.0, 5.0, 10.0] and p['vstress'] == 3.0 and p['vd'] == 0.1
    assert rt == st and b['points'] == 31 * 19 and b['kxci_samples_per_window'] > 6, b
    phases = [info['phase'] for _, _, info in wf.entries]
    merged = [ph for i, ph in enumerate(phases) if not i or ph != phases[i - 1] or ph == 'stdby']
    assert merged == (['stdby', 'read'] + ['stdby', 'stress', 'stdby', 'read'] * len(st) + ['stdby']
                      + ['rec_hold', 'recovery', 'stdby'] * len(rt)), merged
    assert 20.0 < wf.duration_s < 20.01 and P.MAX_SAMPLES / wf.duration_s * b['window_s'] < 0.5   # like the 20 s L0
    zero, flat_3v, edges = (0.0, 0.0, 0.0), {}, {}
    for sid, loops, info in wf.entries:
        q = wf.seqs[sid]
        if info['phase'] in ('stdby', 'rec_hold', 'end_rest'):
            assert all(s.v0 == s.v1 == zero for s in q.segs), q
            if info['phase'] == 'stdby':
                assert q.n == 1000 and loops == 1, q
        elif info['phase'] == 'stress':
            k = info['point']
            assert all(s.v0 == (3.0, 0.0, 0.0) for s in q.segs if s.flat) and all(s.v0[1:] == (0.0, 0.0) for s in q.segs)
            flat_3v[k] = flat_3v.get(k, 0) + loops * sum(s.n for s in q.segs if s.flat)
            edges[k] = edges.get(k, []) + [s.n for s in q.segs if not s.flat] * loops
        else:
            assert loops == 1 and q.name == 'stair'
            m = [s for s in q.segs if s.meas]
            assert [s.v0[0] for s in m] == P.levels(-1.0, 2.0, 0.1) and {s.v0[1] for s in m} == {0.1}
            assert {s.n for s in m} == {2000} and {s.n for s in q.segs if not s.flat} == {100}
            on = (0.0, 0.1, 0.0)        # drain first, then the gate; at the end the gate first, then the drain
            assert [(s.v0, s.v1) for s in q.segs[:2]] == [(zero, on), (on, (-1.0, 0.1, 0.0))], q.segs[:2]
            assert [(s.v0, s.v1) for s in q.segs[-2:]] == [((2.0, 0.1, 0.0), on), (on, zero)], q.segs[-2:]
    incr = np.diff([0.0] + st)
    assert np.allclose([flat_3v[k] * P.TICK for k in sorted(flat_3v)], incr, atol=1e-12), flat_3v   # exact increments
    assert all(e == [100, 100] for e in edges.values()), edges                                      # 1 us ramps
    # recovery staircase j starts exactly rt[j] after the end of the last flat 3 V segment
    et = P.entry_times(wf)
    last = max(i for i, (_, _, info) in enumerate(wf.entries) if info['phase'] == 'stress')
    stress_end = et[last][1] - 1e-6
    starts = [et[i][0] - stress_end for i, (_, _, info) in enumerate(wf.entries) if info['phase'] == 'recovery']
    assert np.allclose(starts, rt, atol=1e-11), starts
    tl = P.timeline(wf)
    first = tl.groupby(['phase', 'point'], sort=False).first()
    assert np.allclose(first.loc['read', 't_stress_s'].to_numpy(), [0.0] + st, atol=1e-9), first
    assert np.allclose(first.loc['read', 't_off_s'].iloc[1:], 1e-6 + 10e-6 + 2e-6 + 14.5e-6, atol=1e-9)
    assert (first.loc['recovery', 't_stress_s'] == st[-1]).all()
    assert np.allclose(first.loc['recovery', 't_off_s'], np.array(rt) + 2e-6 + 14.5e-6, atol=1e-9)
    stress_only = S.build_waveform(dict(p, recovery_times_s=[]), (1, 2, 3))         # padded to 11.5 s at the end
    assert abs(stress_only.duration_s - 11.5) < 1e-9 and phases[:len(stress_only.entries) - 2] == \
        [i['phase'] for _, _, i in stress_only.entries][:-2]
    full = dm.load_devices(cfg['device_csv'])
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=run_map.make_fake_instruments(cfg, full, 'R23C21L'),
                              only='R23C21L', origin='R23C21L')
    x = rows[0]
    assert x['status'] == 'measured' and x['dvt_file'], x
    d = pd.read_csv(x['main_file'])
    assert len(d) == b['points'] and d['Source_Current'].notna().all()
    vt = pd.read_csv(x['dvt_file'])
    assert list(vt['phase']) == ['read'] * 10 + ['recovery'] * 9 and np.allclose(vt['t_rec_s'].iloc[10:], rt)
    A, n, r, tau, _ = FakeK4200Visa(None).stress_model
    model = [A * t ** n * max(0.0, 1 - r * math.log10(1 + to / tau))
             for t, to in zip(vt['t_stress_s'][1:], vt['t_off_at_vt_source_s'][1:])]
    assert max(abs(vt['dVt_source_V'][1:] - model)) < 0.005, (vt['dVt_source_V'].tolist(), model)
    ref = vt.iloc[0]                   # reference = staircase 0 of this waveform, right before the 1 ms stress
    assert (ref['phase'], ref['point'], ref['t_stress_s']) == ('read', 0, 0.0)
    assert ref['dVt_source_V'] == 0 and ref['dVt_drain_V'] == 0 and abs(x['Vt_V'] - ref['Vt_source_V']) < 1e-12
    assert abs(x['dVt_recovery_end_V'] - vt['dVt_source_V'].iloc[-1]) < 1e-12
    assert x['dVt_recovery_end_V'] < 0.7 * x['dVt_stress_end_V']          # the fake recovers by about half
    k = cmds(bench, 'K4200')
    assert k.count(':PMU:EXECUTE') == 1 and not any(c.startswith('RP ') for c in k)
    vt2, out = S.reanalyse(x['main_file'], log=lambda m: None)       # saved data, default (smaller) Vt current
    assert os.path.isfile(out['vt']) and os.path.isfile(out['plot']) and 'reanalysis_Iref6e-8' in out['vt'], out
    assert list(vt2['phase']) == list(vt['phase']) and vt2['dVt_source_V'].iloc[0] == 0
    assert (vt2['Vt_source_V'] < vt['Vt_source_V']).all()            # a lower current crosses at a lower Vg
    plan, _ = run_map.build_plan(cfg, only='R23C21L', origin='R23C21L', data_dir=DATA, log=lambda m: None)
    assert not plan, plan                                    # resume: never stressed twice
    print(f"test_stress_stair_map OK ({b['points']} spot means, {b['duration_s']:.4g} s; dVt after 10 s stress "
          f"{x['dVt_stress_end_V'] * 1e3:.1f} mV, after 10 s recovery {x['dVt_recovery_end_V'] * 1e3:.1f} mV "
          f"vs model {model[-1] * 1e3:.1f} mV)")


def test_stress_stair_ac():
    """AC stress mode (configs/bobafet_6916_stress_stair_ac_1000s_k4200.json): every stress increment becomes
    increment / 1 ms pulses of 2 ms period (1 ms flat at 3 V, 1 us edges, the rest at 0 V on every channel), as
    a looped pulse sequence plus a last pulse without its low, so the cumulative time at 3 V is exactly each
    stress time and every read follows the last falling edge as in DC. Recovery starts are exact; bad AC
    settings are refused; one fake device through run_map: dVt follows the fake's hidden model."""
    import math
    import numpy as np
    import pandas as pd
    from measure import stress_stair as S
    from measure.instruments import k4200_pmu as P
    from measure.instruments.fakes import FakeK4200Visa
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_stress_stair_ac_1000s_k4200.json'))
    p, wf, b = S.check_config(cfg)
    st, rt = p['stress_times_s'], p['recovery_times_s']
    assert (p['stress_mode'], p['ac_period_s'], p['ac_duty'], p['vstress']) == ('ac', 2e-3, 0.5, 3.0)
    assert (p['stair_start_v'], p['stair_stop_v'], p['vt_id_per_wl'], p['vt_id_min_a']) == (0.0, 2.5, 1e-8, 2e-8)
    assert st[-1] == 1000.0 and rt == st and b['kxci_samples_per_window'] > 4, b
    assert S.stress_label(p) == 'AC 3 V, 2 ms period, 50% duty'
    incr = np.diff([0.0] + st)
    pulses = S.ac_pulses(p)
    assert pulses == [round(i / 1e-3) for i in incr] and pulses[:4] == [1, 4, 5, 40], pulses
    zero, hi3 = (0.0, 0.0, 0.0), (3.0, 0.0, 0.0)
    shape = [(100, zero, hi3), (100_000, hi3, hi3), (100, hi3, zero)]
    flat_3v, n_pulses = {}, {}
    for sid, loops, info in wf.entries:
        if info['phase'] != 'stress':
            continue
        q, k = wf.seqs[sid], info['point']
        segs = [(s.n, s.v0, s.v1) for s in q.segs]
        if q.name == 'ac_pulse':
            assert segs == shape + [(99_800, zero, zero)] and q.n == 200_000, segs     # 2 ms period, 50 %
        else:
            assert q.name == 'ac_last' and segs == shape and loops == 1, (q.name, segs)
        flat_3v[k] = flat_3v.get(k, 0) + loops * sum(s.n for s in q.segs if s.flat and s.v0 == hi3)
        n_pulses[k] = n_pulses.get(k, 0) + loops
    assert [n_pulses[k] for k in sorted(n_pulses)] == pulses, n_pulses
    assert np.allclose([flat_3v[k] * P.TICK for k in sorted(flat_3v)], incr, atol=1e-12), flat_3v   # exact
    assert [info['phase'] for _, _, info in wf.entries][-3:] == ['rec_hold', 'recovery', 'stdby']
    wall = sum((n - 1) * 2e-3 + 1e-3 + 2e-6 for n in pulses)
    assert abs(S.stress_wall_s(wf) - wall) < 1e-9 and 2999 < wf.duration_s < 3001, (S.stress_wall_s(wf), wall)
    # the reads follow the stress exactly as in the DC waveform; recovery starts exactly rt[j] after the last high
    dc = S.build_waveform(dict(p, stress_mode='dc'), (1, 2, 3))
    first = P.timeline(wf).groupby(['phase', 'point'], sort=False).first()
    first_dc = P.timeline(dc).groupby(['phase', 'point'], sort=False).first()
    assert np.allclose(first.loc['read', 't_stress_s'].to_numpy(), [0.0] + st, atol=1e-9)
    assert np.allclose(first['t_off_s'].iloc[1:], first_dc['t_off_s'].iloc[1:], atol=1e-12)
    et = P.entry_times(wf)
    last = max(i for i, (_, _, info) in enumerate(wf.entries) if info['phase'] == 'stress')
    starts = [et[i][0] - (et[last][1] - 1e-6) for i, (_, _, info) in enumerate(wf.entries)
              if info['phase'] == 'recovery']
    assert np.allclose(starts, rt, atol=1e-9), starts
    for bad, text in (({'ac_duty': 0.3}, 'whole number'), ({'ac_duty': 0.0}, 'between 0 and 1'),
                      ({'ac_duty': 1.0}, 'between 0 and 1'), ({'ac_period_s': 2e-6}, 'no time at 0 V'),
                      ({'ac_period_s': 4.0}, '<= 1 s'), ({'stress_mode': 'pulsed'}, "'dc' or 'ac'")):
        try:
            S.params({'stress_mode': 'ac', **bad})
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError as e:
            assert text in str(e), (bad, str(e))
    # 10 us / 20 us variant: 100 pulses for the first 1 ms, same cumulative times, still 3000 s
    p20, wf20, b20 = S.check_config(json.load(open('configs/bobafet_6916_stress_stair_ac20us_1000s_k4200.json')))
    assert S.stress_label(p20) == 'AC 3 V, 20 us period, 50% duty' and p20['stress_times_s'] == st
    assert S.ac_pulses(p20) == [100 * n for n in pulses] and S.ac_ticks(p20) == (1000, 800)
    pulse20 = next(q for q in wf20.seqs.values() if q.name == 'ac_pulse')
    assert [(s.n, s.v0, s.v1) for s in pulse20.segs] == [(100, zero, hi3), (1000, hi3, hi3), (100, hi3, zero),
                                                          (800, zero, zero)]
    wall20 = sum((n - 1) * 20e-6 + 10e-6 + 2e-6 for n in S.ac_pulses(p20))
    assert abs(S.stress_wall_s(wf20) - wall20) < 1e-9 and b20['kxci_samples_per_window'] > 4
    os.makedirs(DATA, exist_ok=True)
    S.plot_waveform(wf, os.path.join(DATA, 'ac_waveform.png'))            # pulse trains drawn as a band
    S.plot_waveform(wf20, os.path.join(DATA, 'ac20_waveform.png'))
    cfg['stress_stair'].update(poll_s=0, vt_id_per_wl=1e-7, vt_id_min_a=1e-7)      # fake: see test_stress_stair_map
    full = dm.load_devices(cfg['device_csv'])
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=run_map.make_fake_instruments(cfg, full, 'R23C21L'),
                              only='R23C21L', origin='R23C21L')
    x = rows[0]
    assert x['status'] == 'measured' and x['dvt_file'], x
    vt = pd.read_csv(x['dvt_file'])
    assert list(vt['phase']) == ['read'] * 14 + ['recovery'] * 13 and vt['dVt_source_V'].iloc[0] == 0
    A, n, r, tau, _ = FakeK4200Visa(None).stress_model
    model = [A * t ** n * max(0.0, 1 - r * math.log10(1 + to / tau))
             for t, to in zip(vt['t_stress_s'][1:], vt['t_off_at_vt_source_s'][1:])]
    assert max(abs(vt['dVt_source_V'][1:] - model)) < 0.005, (vt['dVt_source_V'].tolist(), model)
    plan, _ = run_map.build_plan(cfg, only='R23C21L', origin='R23C21L', data_dir=DATA, log=lambda m: None)
    assert not plan, plan
    print(f"test_stress_stair_ac OK ({sum(pulses)} pulses, {S.stress_wall_s(wf):.4g} s of pulsing, "
          f"{wf.duration_s:.4g} s; dVt after 1000 s at 3 V {x['dVt_stress_end_V'] * 1e3:.1f} mV, model "
          f"{model[12] * 1e3:.1f} mV)")


def test_k4200_rpm_cmd_pmu():
    """rpm_cmd='pmu' switches the RPMs with :PMU:RPM:CONFIGURE; the fake refuses RP after :PMU:; the PMU runner
    refuses to start with the legacy RP setting."""
    from measure.instruments import k4200 as K
    from measure.instruments import k4200_pmu as P
    from measure.instruments.fakes import Bench, FakeK4200Visa
    from measure import fet_idvg_k4200 as F
    opt = 'SMU1,SMU2,SMU3,SMU4,SMU5,PMU1RPM1-2,PMU2RPM1-2'
    bench = Bench(); bench.in_contact = True
    k = K.K4200(FakeK4200Visa(bench, opt=opt), log=lambda *a: None, poll_s=0, rpm_cmd='pmu')
    smus = K.get_smus(k, {'gate': 'smu1', 'drain': 'smu2', 'source': 'smu3'})
    F.IdVg_single_Vd(k, *smus, -1, 2, 0.5, 0.05, 1e-7, None, 0.005)
    k.close()
    rpm = [c for c in cmds(bench, 'K4200') if 'RPM' in c or c.startswith('RP ')]
    assert rpm == [f':PMU:RPM:CONFIGURE {n}, {m}' for m in (2, 0) for n in ('PMU1-1', 'PMU1-2', 'PMU2-1')], rpm
    bench = Bench(); bench.in_contact = True
    k = K.K4200(FakeK4200Visa(bench, opt=opt), log=lambda *a: None, poll_s=0)
    k.write(':PMU:INIT 1')
    try:
        k.set_rpm(smus, 2)
        raise AssertionError('expected K4200Error')
    except K.K4200Error as e:
        assert 'RP cannot be used' in str(e), e
    try:
        P.PmuRunner(k, smus).run(None, 1e-5)
        raise AssertionError('expected K4200Error')
    except K.K4200Error as e:
        assert "rpm_cmd = 'pmu'" in str(e), e
    print("test_k4200_rpm_cmd_pmu OK")


def test_k4200_stale_reply_and_status():
    """A reply left in the output buffer by an earlier session is drained before *IDN? (live 2026-10-15:
    *IDN? came back '' and *OPT? 'KSMU1,...'); PMU status digit F flags an overflow."""
    from measure.instruments import k4200 as K
    from measure.instruments import k4200_pmu as P
    from measure.instruments.fakes import Bench, FakeK4200Visa
    msgs = []
    bench = Bench(); bench.in_contact = True
    fake = FakeK4200Visa(bench, opt='SMU1,SMU2,SMU3,SMU4,SMU5,PMU1RPM1-2,PMU2RPM1-2')
    fake.pending = ['']
    k = K.K4200(fake, log=msgs.append, poll_s=0, rpm_cmd='pmu')
    assert k.idn.startswith('KEITHLEY') and k.opt.startswith('SMU1') and any('stale' in m for m in msgs), msgs
    assert not P.status_overflow('01010021') and P.status_overflow('01010421') and not P.status_overflow('')
    assert not bench.bias_live
    print("test_k4200_stale_reply_and_status OK")


def test_live_needs_confirmation(monkey_input='no'):
    cfg = fresh()
    import builtins
    orig = builtins.input
    builtins.input = lambda *a: monkey_input
    try:
        rows, bench = run_map.run(cfg, live=True, data_dir=DATA, limit=1)
    finally:
        builtins.input = orig
    assert rows == [] and bench is None
    print("test_live_needs_confirmation OK (declined before any connection)")


if __name__ == '__main__':
    import io, contextlib
    quiet = io.StringIO()
    tests = [test_full_map, test_resume, test_fault_cleanup, test_keyboard_interrupt,
             test_dead_device_screened, test_limits_and_origin, test_chuck_drift_guard,
             test_fmt21_parse, test_fault_devices_and_compliance, test_igzo_v6_device_id_map,
             test_drift_correction, test_long_move_hops_keep_order, test_max_id_scales_with_WL,
             test_k4200_map, test_k4200_driver_kxci, test_k4200_rpm_cmd_pmu, test_sarb_rules_and_rate_model,
             test_stress_stair_map, test_stress_stair_ac, test_k4200_stale_reply_and_status,
             test_live_needs_confirmation]
    cfg = None
    for t in tests:
        with contextlib.redirect_stdout(quiet):
            out = t(cfg) if t is test_resume else t()
        if t is test_full_map:
            cfg = out
        print(quiet.getvalue().strip().splitlines()[-1]); quiet.seek(0); quiet.truncate()
    shutil.rmtree(DATA, ignore_errors=True)
    print("ALL DRY-RUN TESTS PASSED")
