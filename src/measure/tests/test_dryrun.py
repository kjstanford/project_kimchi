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
    # start on the top row from A:0.04: A->L on L=0.04, then L->A on L=0.06 (one row down)
    cfg = fresh()
    cfg['start_row'] = 'top'
    plan, _ = run_map.build_plan(cfg, origin='A:0.04', L_values=['0.04', '0.06'], data_dir=DATA, log=lambda m: None)
    keys = [p['key'] for p in plan]
    cols = "ABCGHIJKL"   # the wTLM layout has no D-F
    assert keys == [f"{c}:0.04" for c in cols] + [f"{c}:0.06" for c in cols[::-1]], keys
    assert (plan[0]['dx'], plan[0]['dy']) == (0.0, 0.0) and (plan[9]['dx'], plan[9]['dy']) == (0.0, -200.0)

    # probes on L:2 of the die 8000 um above: the first step crosses dies in hops of <= max_step_um
    plan, _ = run_map.build_plan(cfg, origin='L:2', L_values=['0.04', '0.06'], data_dir=DATA, log=lambda m: None,
                                 origin_offset=(0.0, 8000.0))
    assert (plan[0]['dx'], plan[0]['dy']) == (-2200.0, -5600.0) and plan[0]['hops'] == 3, plan[0]
    assert all(p['hops'] == 1 for p in plan[1:]) and (plan[9]['dx'], plan[9]['dy']) == (0.0, -200.0)
    rows, bench = run_map.run(cfg, only='A:0.04,B:0.04', origin='L:2', origin_offset=(0.0, 8000.0),
                              data_dir=DATA, interactive=False)
    assert [r['status'] for r in rows] == ['measured', 'measured'], rows   # fake devices found on the new die
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


def test_gc_map_scan_only():
    """Gain-cell FETs (layouts/igzo_v6_GC_FETs.csv) combined with the 3T map, so the origin can be a 3T FET;
    measurement.scan_only runs the initial scan only (no main sweep) and resume still works."""
    import pandas as pd
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_gc_idvg_k4200.json'))
    m = cfg['measurement']
    assert m['scan_only'] and (m['default_start'], m['default_stop'], m['Vd_scan']) == (-1.0, 2.0, 0.05)
    full = dm.load_devices(cfg['device_csv'])
    gc = pd.read_csv('layouts/igzo_v6_GC_FETs.csv')
    assert len(full) == 984 + 574 and {'R18C25L', 'R12C04L', 'R12C04R'} <= set(full['key'])
    # read (L) / write (R) pairs: same row, 435 um apart, standard 3T pads, storage node = read gate = write sd1
    rd, wr = gc[gc.role == 'read'].set_index('box_id'), gc[gc.role == 'write'].set_index('box_id')
    assert len(rd) == len(wr) == 287 and (rd['partner_id'] == wr['device_id']).all()
    assert ((wr['x in um'] - rd['x in um']).round(3) == 435).all() and (wr['y in um'] == rd['y in um']).all()
    assert set(rd['storage_pad']) == {'gate'} and set(wr['storage_pad']) == {'sd1'}
    assert set(gc['Column']) == set(range(4, 17, 2)) and set(wr[wr.Column < 16]['cell']) == {'W1um_L1um'}
    pads = [c for c in gc.columns if c.endswith('_pad_dx in um') or c.endswith('_pad_dy in um')]
    t3 = pd.read_csv('layouts/igzo_v6_3T_FETs.csv')
    assert all(set(gc[c]) == set(t3[c]) and len(set(gc[c])) == 1 for c in pads)
    for bad in (['layouts/igzo_v6_3T_FETs.csv', 'layouts/jimin_sdc_wTLM_Lc500.csv'],
                ['layouts/igzo_v6_3T_FETs.csv', 'layouts/igzo_v6_3T_FETs.csv']):
        try:
            dm.load_devices(bad)
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass

    only = 'R12C04L,R12C04R,R15C04L'
    fakes = run_map.make_fake_instruments(cfg, full, 'R18C25L')
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=fakes, only=only, keep_order=True, origin='R18C25L')
    assert [x['key'] for x in rows] == only.split(',')
    for x in rows:
        assert x['status'] == 'measured' and x['main_file'] is None and os.path.isfile(x['scan_file']), x
        assert os.listdir(os.path.dirname(x['scan_file'])) and not any(
            f.startswith('IdVg_main_') for f in os.listdir(os.path.dirname(x['scan_file'])))
    vl = [c for c in cmds(bench, 'K4200') if c.startswith('VL')]
    assert len(vl) == 3, len(vl)                                   # one scan per device, no main sweep
    assert {float(c.split(',')[1]) for c in cmds(bench, 'K4200') if c.startswith('VC2,')} == {0.05}
    assert any(f.startswith('summary_') for f in os.listdir(os.path.join(DATA, 'wd_bobafet_6916', '0_0')))
    assert not bench.bias_live and not bench.in_contact

    plan, _ = run_map.build_plan(cfg, only=only, origin='R18C25L', data_dir=DATA)
    assert plan == [], [p['key'] for p in plan]                    # resume: all three already scanned
    plan, _ = run_map.build_plan({**cfg, 'repeat_measurement': True}, only=only, origin='R18C25L', data_dir=DATA,
                                 keep_order=True)
    assert plan[0]['hops'] > 1 and [p['key'] for p in plan] == only.split(',')   # C25 -> C04 is split
    print("test_gc_map_scan_only OK")


def test_gc_wbl_sweep():
    """2T gain-cell WBL sweep (experiment gc_wbl): cells CSV (6-pad + 4-pad _No_Pad twins), the generalised
    K4200.smu_sweep (1 VL + 3 VC, all 4 SMUs measured and turned off), list_sweep's command sequence unchanged,
    run_map at the current position (no XY move), resume, bad settings refused before connecting."""
    import numpy as np
    import pandas as pd
    from measure import gc_sweep
    from measure.instruments import k4200 as K
    from measure.instruments.fakes import Bench, FakeK4200Visa
    shutil.rmtree(DATA, ignore_errors=True)
    quiet = lambda *a, **k: None

    cells = pd.read_csv('layouts/igzo_v6_GC_cells.csv').set_index('device_id')
    assert len(cells) == 574 and (cells['pads'].value_counts() == 287).all()
    assert set(cells[cells.pads == 4]['Column']) == set(range(5, 18, 2))
    a, b = cells.loc['R33C04'], cells.loc['R33C05']
    assert (a['pads'], b['pads'], a['printed_id'], a['twin_id']) == (6, 4, '018 04', 'R33C05')
    assert (b['x in um'] - a['x in um'], b['y in um'] - a['y in um']) == (765.0, 0.0)
    pad_cols = [c for c in cells.columns if c.split('_')[0] in ('rbl', 'rwl', 'wbl', 'wwl')]
    assert len(pad_cols) == 8 and all(cells[c].nunique() == 1 for c in pad_cols)   # same probe setup everywhere
    assert cells[cells.pads == 4]['sn_read_pad_dx in um'].isna().all()
    fets = pd.read_csv('layouts/igzo_v6_GC_FETs.csv').set_index('device_id')
    assert tuple(fets.loc['R33C04L', ['x in um', 'y in um']]) == tuple(a[['x in um', 'y in um']])

    # list_sweep (3T Id-Vg) still sends exactly the old sequence
    bench = Bench(); bench.in_contact = True
    k = K.K4200(FakeK4200Visa(bench, opt='SMU1,SMU2,SMU3,SMU4,SMU5'), log=quiet, poll_s=0, sweep_timeout_s=5)
    n0 = len(bench.log)
    k.list_sweep(1, 2, 3, [0.0, 0.5], 0.05, 0.1, 1e-6)
    got = [c.split(': ', 1)[1] for c in bench.log[n0:] if not c.startswith('K4200: :ERROR')]
    assert got == ['BC', 'DE', "CH1, 'VG', 'IG', 1, 1", "CH2, 'VD', 'ID', 1, 3", "CH3, 'VSRC', 'ISRC', 1, 3",
                   'CH4', 'CH5', 'VS1', 'VS2', 'VM1', 'VM2', 'SS', 'VL1, 1, 0.1, 0.0000, 0.5000', 'VC2, 0.05, 0.1',
                   'VC3, 0, 0.1', 'HT 0', 'DT 0', 'ST 1, 1', 'ST 2, 1', 'ST 3, 1', 'IT2', 'RG 1, 1e-06', 'RG 2, 1e-06',
                   'RG 3, 1e-06', 'SM', 'DM2', "LI 'VG', 'IG', 'ID', 'ISRC'", 'DR1', 'MD', 'ME1',
                   "DO 'VG'", "DO 'IG'", "DO 'ID'", "DO 'ISRC'"], got
    k.outputs_off()

    # bad maps / settings
    for bad in ({'wwl': 'smu4', 'wbl': 'smu3', 'rbl': 'smu2'}, {'wwl': 'smu4', 'wbl': 'smu3', 'rbl': 'smu2', 'rwl': 'smu2'},
                {'gate': 'smu1', 'drain': 'smu2', 'source': 'smu3', 'rwl': 'smu4'}):
        try:
            K.get_gc_smus(k, bad)
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass
    cfg = json.load(open('configs/bobafet_6916_gc_wbl_k4200.json'))
    assert cfg['experiment'] == 'gc_wbl' and cfg['smu_map'] == {'wwl': 'smu4', 'wbl': 'smu3', 'rbl': 'smu2', 'rwl': 'smu1'}
    for bad in ({'wwl_V': 3.5}, {'Irange': 1e-9}, {'wbl_step': 0}, {'compliance': 0.5}, {'wbl_stop': 0.0}, {'typo': 1}):
        try:
            run_map.run({**cfg, 'gc_sweep': {**cfg['gc_sweep'], **bad}}, data_dir=DATA, only='R33C04', origin='R33C04')
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass
    assert not os.path.isdir(DATA) or not any(f.startswith('run_') for f in os.listdir(os.path.join(DATA, 'wd_bobafet_6916', '0_0')))

    # run at the current position: the probes are on R33C04
    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'R33C04')
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=fakes, only='R33C04', origin='R33C04')
    assert len(rows) == 1 and rows[0]['status'] == 'measured' and rows[0]['flags'] == '', rows
    r = rows[0]
    assert np.isfinite(r['Vt_V']) and abs(r['hyst_V']) < 0.05 and r['Ileak_max_A'] < 1e-11 and r['on_off'] > 1e3, r
    assert not [c for c in cmds(bench, 'PROBER') if 'MoveChuck ' in c], 'no XY move expected'
    kx = cmds(bench, 'K4200')
    vl = [c for c in kx if c.startswith('VL')]
    pts = [float(v) for v in vl[0].split(',')[3:]]
    assert len(vl) == 1 and vl[0].startswith('VL3, 1, 0.1, ') and len(pts) == 62
    assert pts[:31] == pts[31:][::-1] and (pts[0], pts[30]) == (0.0, 1.5)
    assert {'VC4, 2.5, 0.1', 'VC1, 1.5, 0.1', 'VC2, 0, 0.1', 'DT 0.01', 'HT 0', 'IT2'} <= set(kx)
    assert all(f'RG {ch}, 1e-06' in kx and f'ST {ch}, 1' in kx for ch in (1, 2, 3, 4))
    assert "LI 'VWBL', 'IWBL', 'VWWL', 'IWWL', 'VRWL', 'IRWL', 'VRBL', 'IRBL'" in kx
    seq = bench.log
    i = seq.index('K4200: ME1')
    nxt = [s for s in seq[i + 1:] if s.startswith('PROBER') or s.startswith('K4200: DV') or s == 'K4200: US']
    assert nxt[:5] == ['K4200: US', 'K4200: DV2', 'K4200: DV1', 'K4200: DV3', 'K4200: DV4'], nxt[:6]
    assert not bench.bias_live and not bench.in_contact
    d = pd.read_csv(r['main_file'])
    assert os.path.basename(r['main_file']).startswith('GC_WBL_sweep_') and os.path.isfile(r['main_file'].replace('.csv', '.png'))
    assert os.path.basename(os.path.dirname(r['main_file'])) == 'R33C04'
    assert list(d.columns[:2]) == ['WBL_Voltage', 'Direction'] and len(d) == 62
    assert {f'{t}_{q}' for t in gc_sweep.TERMS for q in ('Voltage_meas', 'Current', 'Status')} <= set(d.columns)
    assert (d['RWL_Current'] > 0).all() and (d['RBL_Current'] < 0).all()
    assert abs(d['WWL_Voltage_meas'].mean() - 2.5) < 1e-3 and abs(d['RWL_Voltage_meas'].mean() - 1.5) < 1e-3
    summ = [f for f in os.listdir(os.path.join(DATA, 'wd_bobafet_6916', '0_0')) if f.startswith('summary_')]
    s = pd.read_csv(os.path.join(DATA, 'wd_bobafet_6916', '0_0', summ[0]))
    assert {'Vt_bwd_V', 'hyst_V', 'Ileak_max_A'} <= set(s.columns) and s['key'][0] == 'R33C04'

    plan, _ = run_map.build_plan(cfg, only='R33C04', origin='R33C04', data_dir=DATA)
    assert plan == []                                             # resume: already swept
    plan, _ = run_map.build_plan(cfg, only='R33C04,R33C05', origin='R33C04', data_dir=DATA, keep_order=True,
                                 limit=None)
    assert [(p['key'], round(p['dx'], 3), round(p['dy'], 3)) for p in plan] == [('R33C05', 765.0, 0.0)]
    print("test_gc_wbl_sweep OK")


def test_gc_wwl_sweep():
    """2T gain-cell WWL sweep (experiment gc_wwl, as gc_helpers.wwl_sweep): per state two conditioning biases
    with the outputs left on (WWL high, then low; WBL at the opposite data level; RWL, RBL low), then a WWL
    double sweep with WBL at the data level, RWL high; states 1 then 0; everything off before the chuck moves."""
    import numpy as np
    import pandas as pd
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_gc_wwl_k4200.json'))
    g = cfg['gc_wwl']
    assert cfg['experiment'] == 'gc_wwl' and cfg['smu_map'] == {'wwl': 'smu4', 'wbl': 'smu3', 'rbl': 'smu2', 'rwl': 'smu1'}
    assert (g['wwl_high'], g['wwl_low'], g['wwl_step'], g['wbl_high'], g['wbl_low'], g['rwl_high'], g['rwl_low'],
            g['rbl_V'], g['states']) == (2.5, -0.5, 0.1, 1.5, 0.0, 1.5, 0.0, 0.0, [1, 0])
    for bad in ({'wwl_high': 3.5}, {'states': [2]}, {'states': []}, {'wwl_step': 0}, {'conditioning_s': 700},
                {'Irange': 1e-9}, {'typo': 1}):
        try:
            run_map.run({**cfg, 'gc_wwl': {**g, **bad}}, data_dir=DATA, only='R33C04', origin='R36C15')
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass

    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'R36C15')
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=fakes, only='R33C04', origin='R36C15')
    r = rows[0]
    assert len(rows) == 1 and r['status'] == 'measured' and r['flags'] == '', rows
    assert r['I0_s0_A'] > 1e3 * r['I0_s1_A'] and r['Iend_s1_A'] > 1e3 * r['Iend_s0_A'], r   # state read + written
    assert np.isfinite(r['Vt_V']) and np.isfinite(r['Vt_s0_V']) and r['Ileak_max_A'] < 1e-11, r
    assert len([c for c in cmds(bench, 'PROBER') if c.startswith('MoveChuck ')]) == 4   # R36C15 -> R33C04, 4 hops

    # KXCI: tests in order bias, bias, sweep per state; split the log at each ME1
    kx = cmds(bench, 'K4200')
    tests, cur = [], []
    for c in kx:
        cur.append(c)
        if c == 'ME1':
            tests.append(cur); cur = []
    assert len(tests) == 6, len(tests)
    for i, (state, kind) in enumerate([(s, k) for s in (1, 0) for k in ('boost', 'hold', 'sweep')]):
        t = set(tests[i])
        data_v, init_v = ('1.5', '0') if state else ('0', '1.5')
        if kind == 'sweep':
            vl = [c for c in tests[i] if c.startswith('VL')]
            pts = [float(v) for v in vl[0].split(',')[3:]]
            assert vl[0].startswith('VL4, 1, 0.1, ') and len(pts) == 62 and pts[:31] == pts[31:][::-1]
            assert (pts[0], pts[30]) == (-0.5, 2.5)
            assert {f'VC3, {data_v}, 0.1', 'VC1, 1.5, 0.1', 'VC2, 0, 0.1', 'DT 0.01'} <= t
            assert all(f'ST {ch}, 1' in t for ch in (1, 2, 3, 4)) and not any(c.startswith('NR') for c in t)
        else:
            assert not any(c.startswith('VL') for c in t)
            assert {f"VC4, {'2.5' if kind == 'boost' else '-0.5'}, 0.1", f'VC3, {init_v}, 0.1', 'VC1, 0, 0.1',
                    'VC2, 0, 0.1', 'HT 2', 'DT 0', 'NR 1', 'IN 0.01', 'WT 0'} <= t, (i, sorted(t))
            assert all(f'ST {ch}, 0' in t for ch in (1, 2, 3, 4))
        assert all(f'RG {ch}, 1e-06' in t for ch in (1, 2, 3, 4))
        li = [c for c in tests[i] if c.startswith('LI ')]
        assert len(li) == 1 and {x.strip(" '") for x in li[0][3:].split(',')} == \
            {f'{q}{t}' for q in 'VI' for t in ('WWL', 'WBL', 'RWL', 'RBL')}, li
    seq = bench.log
    last = max(i for i, c in enumerate(seq) if c == 'K4200: ME1')
    nxt = [s for s in seq[last + 1:] if s.startswith('PROBER') or s.startswith('K4200: DV') or s == 'K4200: US']
    assert nxt[:5] == ['K4200: US', 'K4200: DV2', 'K4200: DV1', 'K4200: DV3', 'K4200: DV4'], nxt[:6]
    assert not bench.bias_live and not bench.in_contact

    d = pd.read_csv(r['main_file'])
    assert os.path.basename(r['main_file']).startswith('GC_WWL_sweep_') and os.path.isfile(r['main_file'].replace('.csv', '.png'))
    assert list(d['Phase'].value_counts().sort_index().items()) == [('cond_boost', 2), ('cond_hold', 2), ('sweep', 124)]
    assert list(d.loc[d.Phase == 'sweep', 'State'].unique()) == [1, 0]
    cond = d[d.Phase != 'sweep']
    assert (cond['RWL_Voltage_meas'].abs() < 1e-3).all() and (cond['RBL_Voltage_meas'].abs() < 1e-3).all()

    # state 0 whose stored high starts below the 1e-7 A reference, couples up through it, then is discharged
    # (seen live on R14C05): the falling crossing after the peak counts, not the first (rising) one
    from measure import gc_sweep
    v = [round(-0.5 + 0.1 * j, 1) for j in range(31)]
    i0 = [6e-8 * 10 ** (0.15 * j) if x <= 0.2 else 1e-12 for j, x in enumerate(v)]
    syn = pd.DataFrame({'State': 0, 'Phase': 'sweep', 'Direction': ['f'] * 31 + ['b'] * 31, 'WWL_Voltage': v + v[::-1],
                        'RBL_Current': [-x for x in i0] + [-1e-12] * 31, 'WWL_Current': 0.0, 'WBL_Current': 0.0})
    vt0 = gc_sweep.analyse_wwl(syn, {'vt_current_A': 1e-7})['Vt_s0_V']
    assert 0.2 < vt0 < 0.3, vt0

    plan, _ = run_map.build_plan(cfg, only='R33C04', origin='R36C15', data_dir=DATA)
    assert plan == []                                             # resume: already swept (WWL file)
    wbl = json.load(open('configs/bobafet_6916_gc_wbl_k4200.json'))
    plan, _ = run_map.build_plan(wbl, only='R33C04', origin='R36C15', data_dir=DATA)
    assert [p['key'] for p in plan] == ['R33C04']                 # a WWL file does not count as a WBL sweep
    print("test_gc_wwl_sweep OK")


def test_gc_retention():
    """2T gain-cell SMU retention (experiment gc_retention, as gc_helpers.gc_retention_test): per state two
    conditioning biases (outputs left on) writing WBL = data, then a sampling test (NR 301, IN 1) retaining with
    WBL = opposite, RWL high; states 1 then 0; everything off before the chuck moves; schedule time axis."""
    import numpy as np
    import pandas as pd
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_gc_retention_k4200.json'))
    g = cfg['gc_retention']
    assert cfg['experiment'] == 'gc_retention' and (g['retention_s'], g['interval_s'], g['states']) == (300.0, 1.0, [1, 0])
    for bad in ({'interval_s': 0.05}, {'retention_s': 1e5}, {'states': [2]}, {'wwl_high': 3.5}, {'typo': 1}):
        try:
            run_map.run({**cfg, 'gc_retention': {**g, **bad}}, data_dir=DATA, only='R33C04', origin='R36C15')
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass

    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'R36C15')
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=fakes, only='R33C04', origin='R36C15')
    r = rows[0]
    assert len(rows) == 1 and r['status'] == 'measured' and r['flags'] == '', rows
    assert r['Iret_s1_start_A'] > 1e3 * r['Iret_s0_start_A'] and r['Iret_s1_end_A'] < r['Iret_s1_start_A'], r
    assert r['ret_ratio_end'] > 1 and r['Ileak_max_A'] < 1e-11, r

    kx = cmds(bench, 'K4200')
    tests, cur = [], []
    for c in kx:
        cur.append(c)
        if c == 'ME1':
            tests.append(cur); cur = []
    assert len(tests) == 6, len(tests)
    for i, (state, kind) in enumerate([(s, k) for s in (1, 0) for k in ('boost', 'hold', 'retain')]):
        t = set(tests[i])
        prog, ret = ('1.5', '0') if state else ('0', '1.5')
        assert not any(c.startswith('VL') for c in t) and all(f'RG {ch}, 1e-06' in t for ch in (1, 2, 3, 4))
        if kind == 'retain':
            assert {'VC4, -0.5, 0.1', f'VC3, {ret}, 0.1', 'VC1, 1.5, 0.1', 'VC2, 0, 0.1', 'NR 301', 'IN 1', 'WT 0',
                    'HT 0'} <= t, (i, sorted(t))
            assert all(f'ST {ch}, 1' in t for ch in (1, 2, 3, 4))
        else:
            assert {f"VC4, {'2.5' if kind == 'boost' else '-0.5'}, 0.1", f'VC3, {prog}, 0.1', 'VC1, 0, 0.1',
                    'VC2, 0, 0.1', 'HT 2', 'NR 1', 'IN 0.01'} <= t, (i, sorted(t))
            assert all(f'ST {ch}, 0' in t for ch in (1, 2, 3, 4))
    seq = bench.log
    last = max(i for i, c in enumerate(seq) if c == 'K4200: ME1')
    nxt = [s for s in seq[last + 1:] if s.startswith('PROBER') or s.startswith('K4200: DV') or s == 'K4200: US']
    assert nxt[:5] == ['K4200: US', 'K4200: DV2', 'K4200: DV1', 'K4200: DV3', 'K4200: DV4'], nxt[:6]
    assert not bench.bias_live and not bench.in_contact

    d = pd.read_csv(r['main_file'])
    assert os.path.basename(r['main_file']).startswith('GC_retention_')
    assert os.path.isfile(r['main_file'].replace('.csv', '.png'))
    ret = d[d.Phase == 'retention']
    assert list(d['Phase'].value_counts().sort_index().items()) == [('cond_boost', 2), ('cond_hold', 2), ('retention', 602)]
    assert list(ret['State'].unique()) == [1, 0] and np.allclose(ret[ret.State == 1]['Time_s'], np.arange(301))
    assert (d.loc[d.Phase != 'retention', 'RWL_Voltage_meas'].abs() < 1e-3).all()
    assert (ret['RWL_Voltage_meas'] - 1.5).abs().max() < 1e-3

    # the driver: sampling-test limits and the stretched wait
    from measure.instruments import k4200 as K
    from measure.instruments.fakes import Bench, FakeK4200Visa
    b2 = Bench(); b2.in_contact = True
    k = K.K4200(FakeK4200Visa(b2, opt='SMU1,SMU2,SMU3,SMU4'), log=lambda *a: None, poll_s=0, sweep_timeout_s=5)
    names = {1: ('VRWL', 'IRWL'), 2: ('VRBL', 'IRBL'), 3: ('VWBL', 'IWBL'), 4: ('VWWL', 'IWWL')}
    for nr, iv in ((0, 1), (4097, 1), (10, 0.001), (10, 11)):
        try:
            k.smu_sample({1: 0, 2: 0, 3: 0, 4: 0}, names, nr, iv, 0.1, 1e-6)
            raise AssertionError(f"expected ValueError for NR {nr} IN {iv}")
        except ValueError:
            pass
    out = k.smu_sample({1: 1.5, 2: 0, 3: 0, 4: -0.5}, names, 5, 0.5, 0.1, 1e-6)
    assert all(len(v) == 5 for v in out.values()) and k.last_test_s >= 0
    k.outputs_off()

    plan, _ = run_map.build_plan(cfg, only='R33C04', origin='R36C15', data_dir=DATA)
    assert plan == []                                             # resume: retention file present
    for other in ('configs/bobafet_6916_gc_wwl_k4200.json', 'configs/bobafet_6916_gc_wbl_k4200.json'):
        plan, _ = run_map.build_plan(json.load(open(other)), only='R33C04', origin='R36C15', data_dir=DATA)
        assert [p['key'] for p in plan] == ['R33C04']             # a retention file is not a sweep
    print("test_gc_retention OK")


def test_gc_pmu_rw():
    """2T gain-cell alternating write/read on the PMUs (experiment gc_pmu_rw, Clarius gc_pmu_rw /
    gc_transient_rw.c over :PMU:SARB): 22-segment cycle, lines on the RPM channels of their SMUs, full waveform
    capture under the KXCI ~4096-sample cap, read means, outputs off before the chuck moves."""
    import numpy as np
    import pandas as pd
    from measure import gc_pmu
    from measure.instruments import k4200_pmu as P
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_gc_pmu_rw_k4200.json'))
    assert cfg['experiment'] == 'gc_pmu_rw' and cfg['k4200']['rpm_cmd'] == 'pmu'
    p, wf, rate, bud = gc_pmu.check_config(cfg, {'WWL': 4, 'WBL': 3, 'RWL': 1, 'RBL': 2})
    cyc = wf.seqs[2]
    assert p['irange'] == 1e-6 and p['trf_s'] == 2e-6            # 1 uA range: capture windows >= 1.64 us
    assert [len(wf.seqs[i].segs) for i in (1, 2, 3)] == [3, 22, 3] and cyc.n == 10800    # 108 us in 10 ns ticks
    assert wf.chans == (4, 3, 1, 2) and wf.entries[1][:2] == (2, 5)
    assert abs(wf.duration_s - 558e-6) < 1e-12 and abs(bud['measured_time_s'] - 552e-6) < 1e-12
    assert rate == 6_250_000 and bud['samples'] == 3450         # 200e6/32: whole-number rates only
    seg = {s.read: s for s in cyc.segs}
    assert seg['write1'].v0 == (2.5, 1.5, 0.0, 0.0) and seg['write0'].v0 == (2.5, 0.0, 0.0, 0.0)
    assert seg['read1'].v0 == (-0.5, 0.0, 1.5, 0.0) and seg['read0'].v0 == (-0.5, 1.5, 1.5, 0.0)
    assert cyc.segs[0].v0 == cyc.segs[-1].v1 == (-0.5, 0.0, 0.0, 0.0)
    assert all(s.meas and s.win == (0, s.n) for i in (1, 2) for s in wf.seqs[i].segs)
    assert not any(s.meas for s in wf.seqs[3].segs)
    assert p['skip_cycles'] == 1                                 # the first write after a rest reads far higher
    for bad in ({'n_cycles': 0}, {'trf_s': 5e-9}, {'irange': 3e-5}, {'n_cycles': 30}, {'wwl_high': 3.5},
                {'skip_cycles': 5}, {'skip_cycles': -1},
                {'tread_s': 10.005e-6}, {'typo': 1}):
        try:
            run_map.run({**cfg, 'gc_pmu_rw': {**cfg['gc_pmu_rw'], **bad}}, data_dir=DATA, only='R33C04',
                        origin='R36C15')
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass

    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'R36C15')
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=fakes, only='R33C04', origin='R36C15')
    r = rows[0]
    # the fake W1/L2 read FET gives ~1.7 uA, over the 1 uA range: the 4 used read-1 windows must be flagged
    # (cycle 0 is skipped); the fake read 0 (~1e-11 A) is far below the 1 uA-range noise (2 nA): no ratio
    fl = r['flags'].split('; ')
    assert len(rows) == 1 and r['status'] == 'measured' and fl[0] == 'RBL overflow in 4 reads (1e-06 A range)', rows
    assert len(fl) == 2 and fl[1].startswith('read 0 not resolved') and np.isnan(r['read_ratio']), r
    assert r['I_read1_A'] > 1e-7 and abs(r['I_read0_A']) < 5e-9, r

    # offline reanalysis = the live analysis, also with the status codes' leading 0 restored
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()) as out:
        gc_pmu.main(['--reanalyse', r['main_file']])
    assert f"I_read1_A {r['I_read1_A']:.3g}" in out.getvalue() and 'RBL overflow in 4 reads' in out.getvalue()
    assert f"PMU average over 4 cycles (cycle 0 skipped): read 1 {r['I_read1_A']:.3g} A" in out.getvalue()
    # cycle 0 left out of averages and flags: an overflow there alone is not flagged, one in cycle 2 is
    d = gc_pmu.read_frame(r['main_file'])
    reads0, _, _ = gc_pmu.analyse(d, wf, p)
    assert list(reads0['used']) == [False, False] + [True] * 8
    win = lambda c_, rd: (d['Time_s'] >= reads0[(reads0.cycle == c_) & (reads0.read == rd)].t_start_s.iloc[0] - 1e-12) & \
        (d['Time_s'] <= reads0[(reads0.cycle == c_) & (reads0.read == rd)].t_stop_s.iloc[0] + 1e-12)
    low = d.assign(RBL_Current=-2e-7)
    _, res0, fl0 = gc_pmu.analyse(low.assign(RBL_Current=low['RBL_Current'].mask(win(0, 'read1'), -5e-6)), wf, p)
    assert not any('overflow' in f for f in fl0) and abs(res0['I_read1_A'] - 2e-7) < 1e-12, (fl0, res0)
    _, res2, fl2 = gc_pmu.analyse(low.assign(RBL_Current=low['RBL_Current'].mask(win(2, 'read1'), -5e-6)), wf, p)
    assert 'RBL overflow in 1 reads (1e-06 A range)' in fl2 and abs(res2['I_read1_max_A'] - 5e-6) < 1e-12, fl2
    with contextlib.redirect_stdout(io.StringIO()) as out:
        gc_pmu.main(['--reanalyse', r['main_file'], '--skip-cycles', '0'])
    assert 'PMU average over 5 cycles: read 1' in out.getvalue() and 'RBL overflow in 5 reads' in out.getvalue()
    assert r['I_read1_std_A'] >= 0 and np.isfinite(r['I_read0_std_A'])
    assert gc_pmu.read_frame(r['main_file'])['RBL_Status'].str.len().eq(8).all()
    kx = cmds(bench, 'K4200')
    assert all(f':PMU:RPM:CONFIGURE PMU{c}, 0' in kx for c in ('1-1', '1-2', '2-1', '2-2'))
    assert all(f':PMU:MEASURE:RANGE {ch}, 2, 1e-06' in kx for ch in (1, 2, 3, 4))
    assert ':PMU:SAMPLE:RATE 6250000' in kx and kx.count(':PMU:EXECUTE') == 1
    assert all(f':PMU:SARB:WFM:SEQ:LIST {ch}, 1, 1, 2, 5, 3, 1' in kx for ch in (1, 2, 3, 4))
    mt = [c for c in kx if c.startswith(':PMU:SARB:SEQ:MEAS:TYPE 4,')]
    assert mt == [':PMU:SARB:SEQ:MEAS:TYPE 4, 1, 2, 2, 2', ':PMU:SARB:SEQ:MEAS:TYPE 4, 2, ' + ', '.join(['2'] * 22),
                  ':PMU:SARB:SEQ:MEAS:TYPE 4, 3, 0, 0, 0'], mt
    wwl = [c for c in kx if c.startswith(':PMU:SARB:SEQ:STARTV 4, 2,')][0]
    assert wwl.split(', ')[2:4] == ['-0.5', '2.5']                  # WWL on channel 4 (SMU4's RPM)
    seq = bench.log
    ex = seq.index('K4200: :PMU:EXECUTE')
    nxt = [s for s in seq[ex + 1:] if s.startswith('PROBER') or s.startswith('K4200: :PMU:OUTPUT:STATE')]
    assert nxt[:4] == [f'K4200: :PMU:OUTPUT:STATE {ch}, 0' for ch in sorted((1, 2, 3, 4))], nxt[:5]
    assert not bench.bias_live and not bench.in_contact

    d = pd.read_csv(r['main_file'])
    reads = pd.read_csv(r['main_file'].replace('GC_PMU_rw_', 'GC_PMU_rw_reads_'))
    assert abs(len(d) / 3450 - 1) < 0.01 and abs(d['Time_s'].diff().median() / 160e-9 - 1) < 1e-3
    assert {f'{t}_{q}' for t in gc_pmu.LINES for q in ('Voltage', 'Current', 'Status', 'V_set')} <= set(d.columns)
    assert list(reads['read']) == ['read1', 'read0'] * 5 and reads['n_samples'].between(24, 26).all()   # 4 us at 160 ns
    assert (abs(reads['V_RWL_V'] - 1.5) < 0.01).all() and list(reads['V_WBL_V'].round(2)) == [0.0, 1.5] * 5
    for ext in ('png', 'json', 'txt'):
        tag = {'png': 'GC_PMU_rw_', 'json': 'GC_PMU_rw_meta_', 'txt': 'GC_PMU_rw_raw_'}[ext]
        assert os.path.isfile(r['main_file'].replace('GC_PMU_rw_', tag).replace('.csv', f'.{ext}')), ext

    plan, _ = run_map.build_plan(cfg, only='R33C04', origin='R36C15', data_dir=DATA)
    assert plan == []                                             # resume: PMU file present
    print("test_gc_pmu_rw OK")


def test_gc_pmu_rw_ranges():
    """irange list [1e-6, 1e-5]: the same waveform once per range in one landing (1 uA first), separate tagged
    files and one summary row per range; the 7-cell route in the given order."""
    import contextlib, io
    import numpy as np
    from measure import gc_pmu
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_gc_pmu_rw_2ranges_k4200.json'))
    assert cfg['gc_pmu_rw']['irange'] == [1e-6, 1e-5] and cfg['origin_device'] == 'R33C04'
    for bad in ([], [1e-6, 1e-6], [1e-6, 3e-5]):
        try:
            gc_pmu.check_config({**cfg, 'gc_pmu_rw': {**cfg['gc_pmu_rw'], 'irange': bad}})
            raise AssertionError(f"expected ValueError for irange {bad}")
        except ValueError:
            pass
    route = 'R23C07,R14C05,R06C09,R09C17,R31C11,R34C13,R36C15'
    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'R33C04')
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=fakes, only=route, origin='R33C04', keep_order=True)
    assert [r['key'] for r in rows] == [k for k in route.split(',') for _ in (0, 1)], [r['key'] for r in rows]
    assert [r['Irange_A'] for r in rows] == [1e-6, 1e-5] * 7 and all(r['status'] == 'measured' for r in rows)
    # the fake read 1 (~1.7 uA at W1/L2, scaling with read W/L) overflows 1 uA everywhere, 10 uA only for read W >= 6 um
    assert all('RBL overflow in 5 reads (1e-06 A range)' in r['flags'] for r in rows[0::2]), rows[0]['flags']
    assert not any('RBL overflow' in r['flags'] for r in rows[1:8:2]), rows[1]['flags']
    for r in rows:
        tag = gc_pmu.range_tag(r['Irange_A'])
        assert os.path.basename(r['main_file']).startswith(f'GC_PMU_rw_{tag}_'), r['main_file']
        for kind, ext in (('GC_PMU_rw_reads_', 'csv'), ('GC_PMU_rw_meta_', 'json'), ('GC_PMU_rw_raw_', 'txt'),
                          ('GC_PMU_rw_', 'png')):
            assert os.path.isfile(r['main_file'].replace('GC_PMU_rw_', kind).replace('.csv', f'.{ext}')), (kind, ext)
        meta = json.load(open(r['main_file'].replace('GC_PMU_rw_', 'GC_PMU_rw_meta_').replace('.csv', '.json')))
        assert meta['params']['irange'] == r['Irange_A']
    with contextlib.redirect_stdout(io.StringIO()) as out:
        gc_pmu.main(['--reanalyse', rows[1]['main_file']])
    assert f"I_read1_A {rows[1]['I_read1_A']:.3g}" in out.getvalue()
    kx = cmds(bench, 'K4200')
    assert kx.count(':PMU:EXECUTE') == 14
    rng = [c.split(', ')[-1] for c in kx if c.startswith(':PMU:MEASURE:RANGE 2, 2,')]
    assert rng == ['1e-06', '1e-05'] * 7, rng
    seq = bench.log                    # both ranges in one landing: no prober command between the two EXECUTEs
    ex = [i for i, s in enumerate(seq) if s == 'K4200: :PMU:EXECUTE']
    assert all(not any(s.startswith('PROBER') for s in seq[a:b]) for a, b in zip(ex[0::2], ex[1::2]))
    assert not bench.bias_live and not bench.in_contact
    summ = [f for f in os.listdir(os.path.join(DATA, 'wd_bobafet_6916', '0_0')) if f.startswith('summary_')]
    import pandas as pd
    s = pd.read_csv(os.path.join(DATA, 'wd_bobafet_6916', '0_0', summ[0]))
    assert len(s) == 14 and list(s['Irange_A']) == [1e-6, 1e-5] * 7 and np.isfinite(s['I_read1_A']).all()
    plan, _ = run_map.build_plan(cfg, only=route, origin='R33C04', data_dir=DATA, keep_order=True)
    assert plan == []                                             # resume: tagged PMU files count
    print("test_gc_pmu_rw_ranges OK")


def test_gc_pmu_ret():
    """2T gain-cell retention on the PMUs (experiment gc_pmu_ret, Clarius gc_pmu_retention / gc_retention.c over
    :PMU:SARB): per state one write, WBL held opposite, spot-mean reads at the retention times (looped 1 ms holds
    between them), both states in one landing, outputs off before the chuck moves."""
    import contextlib, io
    import numpy as np
    import pandas as pd
    from measure import gc_pmu_ret as R
    from measure.instruments import k4200_pmu as P
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_gc_pmu_ret_k4200.json'))
    assert cfg['experiment'] == 'gc_pmu_ret' and cfg['k4200']['rpm_cmd'] == 'pmu'
    p, wfs, rate, bud = R.check_config(cfg, {'WWL': 4, 'WBL': 3, 'RWL': 1, 'RBL': 2})
    times = p['retention_times_s']
    assert len(times) == 17 and times[0] == 0 and times[-1] == 100 and p['irange'] == 1e-6
    assert rate == 20_000_000 and bud['samples'] == 3570 and bud['samples_per_window'] == 210
    for s, wbl in ((1, 0.0), (0, 1.5)):
        wf = wfs[s]
        assert wf.chans == (4, 3, 1, 2) and wf.meas_type == 1 and (wf.meas_start, wf.meas_stop) == (1800, 2850)
        tl = P.timeline(wf)
        t0 = wf.info['t0_ticks'] * P.TICK
        assert len(tl) == 17 and np.allclose(tl['t_s'] - t0, np.array(times) + 2e-6 + 23.25e-6, atol=1e-12, rtol=0)
        assert abs(wf.duration_s - 100.00007) < 1e-9
        unit = [q for q in wf.seqs.values() if q.name == 'hold_unit'][0]
        assert unit.n == 100_000 and all(g.v0 == (-0.5, wbl, 0.0, 0.0) for g in unit.segs)
        rd = [q for q in wf.seqs.values() if q.name == 'read'][0]
        assert [g.meas for g in rd.segs] == [False, True, False] and rd.segs[1].v0 == (-0.5, wbl, 1.5, 0.0)
        wr = [q for q in wf.seqs.values() if q.name == 'write'][0]
        assert wr.segs[1].v0 == (2.5, 1.5 - wbl, 0.0, 0.0) and wr.segs[-1].v1 == (-0.5, wbl, 0.0, 0.0)
        first = wf.seqs[wf.entries[0][0]].segs[0]
        assert first.v0 == (0.0,) * 4 and wf.seqs[wf.entries[-1][0]].segs[-1].v1 == (0.0,) * 4
    for bad in ({'tread_s': 10e-6}, {'retention_times_s': [0, 1e-5]}, {'retention_times_s': [0, 1, 2, 5]},
                {'states': [2]}, {'retention_times_s': [1, 0.5]}, {'wwl_high': 3.5}, {'typo': 1}):
        try:
            run_map.run({**cfg, 'gc_pmu_ret': {**cfg['gc_pmu_ret'], **bad}}, data_dir=DATA, only='R33C04',
                        origin='R36C15')
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass

    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'R36C15')
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=fakes, only='R33C04', origin='R36C15')
    r = rows[0]
    assert len(rows) == 1 and r['status'] == 'measured', rows
    # the fake W1/L2 read FET gives ~1.7 uA on the 1 uA range: state-1 reads overflow and are flagged
    assert 'RBL overflow' in r['flags'] and r['Iret_s1_start_A'] > 5e-7 and abs(r['Iret_s0_end_A']) < 2e-9, r
    kx = cmds(bench, 'K4200')
    assert kx.count(':PMU:EXECUTE') == 2
    assert all(f':PMU:MEASURE:RANGE {ch}, 2, 1e-06' in kx for ch in (1, 2, 3, 4)) and ':PMU:SAMPLE:RATE 20000000' in kx
    mt = [c for c in kx if c.startswith(':PMU:SARB:SEQ:MEAS:TYPE 2,')]
    spot = [c for c in mt if c.split(', ')[2:] != ['0'] * (len(c.split(', ')) - 2)]
    assert spot == [':PMU:SARB:SEQ:MEAS:TYPE 2, 3, 0, 1, 0'] * 2, mt    # only the read plateau, in both states
    assert any(c.startswith(':PMU:SARB:SEQ:MEAS:START 2, 3, 0, 1.8e-05, 0') for c in kx)
    assert any(c.startswith(':PMU:SARB:SEQ:MEAS:STOP 2, 3, 0, 2.85e-05, 0') for c in kx)
    seq = bench.log
    ex = [i for i, s in enumerate(seq) if s == 'K4200: :PMU:EXECUTE']
    assert not any(s.startswith('PROBER') for s in seq[ex[0]:ex[1]])          # both states in one landing
    nxt = [s for s in seq[ex[1] + 1:] if s.startswith('PROBER') or s.startswith('K4200: :PMU:OUTPUT:STATE')]
    assert nxt[:4] == [f'K4200: :PMU:OUTPUT:STATE {ch}, 0' for ch in (1, 2, 3, 4)], nxt[:5]
    assert not bench.bias_live and not bench.in_contact

    d = R.read_frame(r['main_file'])
    assert len(d) == 34 and list(d['State']) == [1] * 17 + [0] * 17 and d['RBL_Status'].str.len().eq(8).all()
    assert np.allclose(d['t_ret_s'], times * 2)
    s1 = d[d['State'] == 1]['I_read_A'].values
    assert (abs(d[d['State'] == 0]['WBL_Voltage_meas'] - 1.5) < 0.01).all()
    # fake: the node leaks toward 0 V with tau 100 s, so state 1 decays by the 100 s read
    assert s1[-1] < 0.5 * s1[0], s1
    for ext, tag in (('json', 'GC_PMU_ret_meta_'), ('txt', 'GC_PMU_ret_raw_'), ('png', 'GC_PMU_ret_')):
        assert os.path.isfile(r['main_file'].replace('GC_PMU_ret_', tag).replace('.csv', f'.{ext}')), ext
    raw = open(r['main_file'].replace('GC_PMU_ret_', 'GC_PMU_ret_raw_').replace('.csv', '.txt')).read()
    assert raw.startswith('# state 1') and '# state 0' in raw
    with contextlib.redirect_stdout(io.StringIO()) as out:
        R.main(['--reanalyse', r['main_file']])
    assert f"Iret_s1_start_A {r['Iret_s1_start_A']:.3g}" in out.getvalue() and 'PMU retention 0 -> 100 s' in out.getvalue()

    plan, _ = run_map.build_plan(cfg, only='R33C04', origin='R36C15', data_dir=DATA)
    assert plan == []                                             # resume: retention file present
    rw = json.load(open('configs/bobafet_6916_gc_pmu_rw_k4200.json'))
    plan, _ = run_map.build_plan(rw, only='R33C04', origin='R36C15', data_dir=DATA)
    assert [q['key'] for q in plan] == ['R33C04']                 # a retention file is not a write/read run
    print("test_gc_pmu_ret OK")


def test_gc_screen_ret():
    """gc_screen_ret: per cell in one landing, WBL + WWL sweeps, then the PMU retention only if both pass the
    gc_screen criteria; one summary row per test; GC_screen_ file written last (resume marker)."""
    import pandas as pd
    from measure import gc_screen
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_gc_screen_ret1000_k4200.json'))
    assert cfg['experiment'] == 'gc_screen_ret' and cfg['k4200']['rpm_cmd'] == 'pmu'
    assert cfg['gc_sweep'] == json.load(open('configs/bobafet_6916_gc_wbl_k4200.json'))['gc_sweep']
    assert cfg['gc_wwl'] == json.load(open('configs/bobafet_6916_gc_wwl_k4200.json'))['gc_wwl']
    assert cfg['gc_pmu_ret'] == json.load(open('configs/bobafet_6916_gc_pmu_ret1000_k4200.json'))['gc_pmu_ret']
    for blk, bad in (('gc_screen', {'typo': 1}), ('gc_screen', {'wbl_vt_V': [1.6, 0.5]}), ('gc_sweep', {'wbl_step': 0}),
                     ('gc_pmu_ret', {'tread_s': 10e-6}), ('k4200', {'rpm_cmd': 'RP'})):
        try:
            run_map.run({**cfg, blk: {**cfg[blk], **bad}}, data_dir=DATA, only='R35C15', origin='R36C15')
            raise AssertionError(f"expected ValueError for {blk} {bad}")
        except ValueError:
            pass
    good = dict(Vt_V=1.0, on_off=1e6, Ileak_max_A=3e-11, flags='')
    wwl = dict(Vt_V=1.3, Iend_s1_A=1e-7, Iend_s0_A=2e-12, Ileak_max_A=3e-11, flags='')
    c = gc_screen.params({})
    assert gc_screen.evaluate(good, wwl, c) == (True, [])
    ok, why = gc_screen.evaluate({**good, 'Vt_V': float('nan'), 'on_off': 50}, {**wwl, 'Iend_s0_A': 1e-8}, c)
    assert not ok and why == ['WBL Vt not found', 'WBL on/off 50 < 10000', 'WWL stored 1/0 10 < 100'], why
    ok, why = gc_screen.evaluate({**good, 'flags': 'rbl compliance x2'}, {**wwl, 'Ileak_max_A': 5e-9}, c)
    assert not ok and len(why) == 2 and why[0].startswith('WBL sweep compliance') and why[1].startswith('WWL leakage')

    route = 'R35C15,R29C17'
    # the fake read FET (W10/L2) reaches 100 nA at ~0.25 V, below the real cells' ~1 V: widen that window only
    cfg = {**cfg, 'gc_screen': {**cfg['gc_screen'], 'wbl_vt_V': [0.1, 1.6]}}
    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'R36C15', dead=('R29C17',))
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=fakes, only=route, origin='R36C15', keep_order=True)
    assert [(r['key'], r['test'], r['status']) for r in rows] == [
        ('R35C15', 'gc_wbl', 'measured'), ('R35C15', 'gc_wwl', 'measured'), ('R35C15', 'gc_pmu_ret', 'measured'),
        ('R29C17', 'gc_wbl', 'measured'), ('R29C17', 'gc_wwl', 'measured'), ('R29C17', 'gc_pmu_ret', 'skipped')], rows
    assert rows[5]['reason'].startswith('screen failed: WBL Vt not found'), rows[5]['reason']
    kx = cmds(bench, 'K4200')
    assert kx.count(':PMU:EXECUTE') == 2                         # only the passing cell's two states
    ex = [i for i, s in enumerate(kx) if s == ':PMU:EXECUTE']
    vl = [i for i, s in enumerate(kx) if s.startswith('VL')]
    smu_mode = [i for i, s in enumerate(kx) if s.startswith(':PMU:RPM:CONFIGURE') and s.endswith(', 2')]
    first, second = [i for i in vl if i < ex[0]], [i for i in vl if i > ex[1]]
    assert first and second and len(vl) == len(first) + len(second)  # sweeps before the PMU, then the next cell's
    assert any(i < first[0] for i in smu_mode) and any(ex[1] < i < second[0] for i in smu_mode)
    assert not bench.bias_live and not bench.in_contact
    die = os.path.join(DATA, 'wd_bobafet_6916', '0_0')
    for key, passed in (('R35C15', True), ('R29C17', False)):
        files = os.listdir(os.path.join(die, key))
        scr = [f for f in files if f.startswith('GC_screen_')]
        assert len(scr) == 1 and any(f.startswith('GC_WBL_sweep_') for f in files) and \
            any(f.startswith('GC_WWL_sweep_') for f in files), files
        assert any(f.startswith('GC_PMU_ret_') for f in files) == passed, files
        s = pd.read_csv(os.path.join(die, key, scr[0]))
        assert bool(s['passed'].iloc[0]) == passed and s['ret_status'].iloc[0] == ('measured' if passed else 'skipped')
    summ = [f for f in os.listdir(die) if f.startswith('summary_')]
    assert list(pd.read_csv(os.path.join(die, summ[0]))['test']) == ['gc_wbl', 'gc_wwl', 'gc_pmu_ret'] * 2
    plan, _ = run_map.build_plan(cfg, only=route, origin='R36C15', data_dir=DATA, keep_order=True)
    assert plan == []                                             # resume: both cells screened
    print("test_gc_screen_ret OK")


def test_gc_screen_auto_range():
    """gc_screen auto_range: screen -> gc_pmu_rw on 10 uA -> retention ranges from it (state 1: smallest range
    >= 2 x max read 1, 100 uA on overflow; state 0: RBL alone on 100 nA) -> 2000 s retention; per-channel PMU
    current ranges."""
    import pandas as pd
    from measure import gc_pmu_ret, gc_screen
    from measure.instruments import k4200_pmu as P
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_gc_screen_rw_ret2000_k4200.json'))
    c = gc_screen.check_config(cfg)
    assert c['auto_range'] and cfg['gc_pmu_rw']['irange'] == 1e-5 and cfg['gc_pmu_rw']['n_cycles'] == 5
    p, wfs, rate, bud = gc_pmu_ret.check_config(cfg, {'WWL': 4, 'WBL': 3, 'RWL': 1, 'RBL': 2})
    assert p['retention_times_s'][-1] == 2000 and len(p['retention_times_s']) == 21
    assert rate == 5_000_000 and abs(bud['samples_per_window'] - 187.5) < 1e-9 and bud['samples'] < 4000
    assert (wfs[0].meas_start, wfs[0].meas_stop) == (10500, 14250)          # 105-142.5 us of a 150 us read
    # per-channel ranges: state 0 puts RBL (ch 2) alone on 100 nA
    rng = gc_pmu_ret.state_ranges(p, wfs[0], 0)
    assert rng == {4: 1e-5, 3: 1e-5, 1: 1e-5, 2: 1e-7} and gc_pmu_ret.state_ranges(p, wfs[1], 1)[2] == 1e-5
    kx = P.commands(wfs[0], rng)
    assert ':PMU:MEASURE:RANGE 2, 2, 1e-07' in kx and ':PMU:MEASURE:RANGE 1, 2, 1e-05' in kx
    short = {**cfg['gc_pmu_ret'], 'tread_s': 30e-6, 'read_window': [0.6, 0.95]}
    try:                                                 # 100 nA needs 100 us settling: a 30 us read is refused
        gc_pmu_ret.check_config({**cfg, 'gc_pmu_ret': short})
        raise AssertionError("expected a settling error for RBL on 100 nA")
    except ValueError as e:
        assert 'settling time 0.0001' in str(e), e
    try:
        gc_screen.check_config({**cfg, 'gc_pmu_ret': {**short, 'irange_s0': None}})
        raise AssertionError("expected auto_range to refuse the 100 nA pair")
    except ValueError as e:
        assert 'state 0 RBL 1e-07 A is not possible' in str(e), e
    nan = float('nan')
    assert c['range_fill'] == 0.9 and cfg['gc_pmu_rw']['skip_cycles'] == 1
    # range from the last-4-cycle averages: up to 0.9 uA -> 1 uA, up to 9 uA -> 10 uA; state 0 up to 90 nA -> 100 nA
    for rw, want in (({'I_read1_A': 8.5e-7, 'I_read0_A': -1e-9}, (1e-6, 1e-7)),
                     ({'I_read1_A': 9.5e-7, 'I_read0_A': 1e-9}, (1e-5, 1e-7)),
                     ({'I_read1_A': 8.5e-6, 'I_read0_A': 8e-8}, (1e-5, 1e-7)),
                     ({'I_read1_A': 9.5e-6, 'I_read0_A': -9.5e-8}, (1e-4, 1e-6)),
                     ({'I_read1_A': 2e-6, 'flags': 'RBL overflow in 1 reads (1e-05 A range)'}, (1e-4, 1e-7)),
                     ({'I_read1_A': 4e-7, 'I_read0_A': 3e-6}, (1e-6, 1e-6)),
                     ({'I_read1_A': nan}, (None, None))):
        assert gc_screen.choose_ranges(rw, c)[:2] == want, (rw, gc_screen.choose_ranges(rw, c))

    # fake run: R13C07 (read W2/L2, fake ~3.4 uA -> 10 uA) and R16C15 (W10/L2, ~17 uA: over 10 uA -> 100 uA)
    route = 'R13C07,R16C15'
    cfg = {**cfg, 'gc_screen': {**cfg['gc_screen'], 'wbl_vt_V': [0.1, 1.6]}}     # fake read FETs: Vt ~0.25 V
    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'R04C17')
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=fakes, only=route, origin='R04C17', keep_order=True)
    tests = ['gc_wbl', 'gc_wwl', 'gc_pmu_rw', 'gc_pmu_ret']
    assert [(r['key'], r['test'], r['status']) for r in rows] == \
        [(k, t, 'measured') for k in ('R13C07', 'R16C15') for t in tests], rows
    ret = [r for r in rows if r['test'] == 'gc_pmu_ret']
    assert [(r['Irange_A'], r['Irange_s0_A']) for r in ret] == [(1e-5, 1e-7), (1e-4, 1e-7)], ret
    assert [r['Irange_A'] for r in rows if r['test'] == 'gc_pmu_rw'] == [1e-5, 1e-5]
    assert 'RBL overflow' not in ret[0]['flags'] and abs(ret[0]['Iret_s0_end_A']) < 1e-9, ret[0]
    kx = cmds(bench, 'K4200')
    assert kx.count(':PMU:EXECUTE') == 6                         # per cell: write/read, retention state 1 and 0
    rbl = [c_.split(', ')[-1] for c_ in kx if c_.startswith(':PMU:MEASURE:RANGE 2, 2,')]
    rwl = [c_.split(', ')[-1] for c_ in kx if c_.startswith(':PMU:MEASURE:RANGE 1, 2,')]
    assert rbl == ['1e-05', '1e-05', '1e-07', '1e-05', '0.0001', '1e-07'], rbl
    assert rwl == ['1e-05', '1e-05', '1e-05', '1e-05', '0.0001', '0.0001'], rwl
    vl = [i for i, s in enumerate(kx) if s.startswith('VL')]
    ex = [i for i, s in enumerate(kx) if s == ':PMU:EXECUTE']
    assert max(vl[:len(vl) // 2]) < ex[0] and ex[2] < min(vl[len(vl) // 2:]) < ex[3]  # sweeps before each cell's PMU
    assert not bench.bias_live and not bench.in_contact
    d = gc_pmu_ret.read_frame(ret[0]['main_file'])
    assert len(d) == 42 and set(d[d.State == 0]['Irange_RBL_A']) == {1e-7} and set(d[d.State == 1]['Irange_RBL_A']) == {1e-5}
    die = os.path.join(DATA, 'wd_bobafet_6916', '0_0')
    scr = [f for f in os.listdir(os.path.join(die, 'R16C15')) if f.startswith('GC_screen_')]
    s = pd.read_csv(os.path.join(die, 'R16C15', scr[0]))
    assert s['Irange_s1_A'].iloc[0] == 1e-4 and s['Irange_s0_A'].iloc[0] == 1e-7 and s['rw_10uA_I_read1_A'].iloc[0] > 5e-6
    meta = json.load(open(ret[1]['main_file'].replace('GC_PMU_ret_', 'GC_PMU_ret_meta_').replace('.csv', '.json')))
    assert meta['params']['irange'] == 1e-4 and meta['params']['irange_s0'] == 1e-7
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()) as out:
        gc_pmu_ret.main(['--reanalyse', ret[0]['main_file']])
    assert f"Iret_s1_start_A {ret[0]['Iret_s1_start_A']:.3g}" in out.getvalue()
    plan, _ = run_map.build_plan(cfg, only=route, origin='R04C17', data_dir=DATA, keep_order=True)
    assert plan == []
    print("test_gc_screen_auto_range OK")


def test_gc_screen_30us():
    """gc_screen_ret with 30 us writes and reads: gc_pmu_rw on 1 uA and on 10 uA, the retention state-1 range is
    the smallest one whose own run holds read 1 (avg <= 0.9 x range, no overflow); state 1 reads 30 us, state 0
    150 us with RBL alone on 100 nA (tread_s0_s)."""
    import pandas as pd
    from measure import gc_pmu, gc_pmu_ret, gc_screen
    from measure.instruments import k4200_pmu as P
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_gc_screen_rw2_ret2000_30us_k4200.json'))
    c = gc_screen.check_config(cfg)
    rw, ret_ = cfg['gc_pmu_rw'], cfg['gc_pmu_ret']
    assert rw['irange'] == [1e-6, 1e-5] and rw['twrite_s'] == rw['tread_s'] == 30e-6
    assert ret_['twrite_s'] == ret_['tread_s'] == 30e-6 and ret_['tread_s0_s'] == 150e-6
    assert c['ranges_s1'] == [1e-6, 1e-5] and c['ranges_s0'] == [1e-7]
    chans = {'WWL': 4, 'WBL': 3, 'RWL': 1, 'RBL': 2}
    p, wfs, rate, _ = gc_pmu_ret.check_config(cfg, chans)
    assert (wfs[1].meas_start, wfs[1].meas_stop) == (2100, 2850)            # 21-28.5 us of a 30 us read
    assert (wfs[0].meas_start, wfs[0].meas_stop) == (10500, 14250)          # 105-142.5 us of a 150 us read
    kx1, kx0 = (P.commands(wfs[s], gc_pmu_ret.state_ranges(p, wfs[s], s)) for s in (1, 0))
    assert ':PMU:SARB:SEQ:TIME 2, 3, 2e-06, 3e-05, 2e-06' in kx1 and ':PMU:SARB:SEQ:TIME 2, 3, 2e-06, 0.00015, 2e-06' in kx0
    for kx in (kx1, kx0):                                                   # write plateau 30 us in both states
        assert any(', 3e-05, ' in x for x in kx if x.startswith(':PMU:SARB:SEQ:TIME') and x.split(', ')[1] == '2'), kx
    assert gc_pmu_ret.read_s(p, 1) == 30e-6 and gc_pmu_ret.read_s(p, 0) == 150e-6
    assert gc_pmu_ret.read_s({**p, 'tread_s0_s': None}, 0) == 30e-6
    for bad in (-1e-6, 0):
        try:
            gc_pmu_ret.check_config({**cfg, 'gc_pmu_ret': {**ret_, 'tread_s0_s': bad}})
            raise AssertionError(f"expected ValueError for tread_s0_s {bad}")
        except ValueError:
            pass
    try:                                                 # a 30 us state-0 read on 100 nA is refused
        gc_screen.check_config({**cfg, 'gc_pmu_ret': {**ret_, 'tread_s0_s': None}})
        raise AssertionError("expected a settling error for state 0 on 100 nA")
    except ValueError as e:
        assert 'settling time 0.0001' in str(e), e
    _, wf_rw, rate_rw, _ = gc_pmu.check_config(cfg, chans)
    assert rate_rw * 0.25 * 30e-6 >= gc_pmu.MIN_READ_SAMPLES

    # choose from the two write/read runs
    ov = 'RBL overflow in 4 reads (1e-06 A range)'
    for runs, want in (([{'Irange_A': 1e-6, 'I_read1_A': 5e-7}, {'Irange_A': 1e-5, 'I_read1_A': 5.2e-7}], 1e-6),
                       ([{'Irange_A': 1e-6, 'I_read1_A': 9.3e-7}, {'Irange_A': 1e-5, 'I_read1_A': 8.8e-7}], 1e-5),
                       ([{'Irange_A': 1e-6, 'I_read1_A': 1.1e-6, 'flags': ov}, {'Irange_A': 1e-5, 'I_read1_A': 2e-6}], 1e-5),
                       ([{'Irange_A': 1e-6, 'I_read1_A': 1.1e-6, 'flags': ov},
                         {'Irange_A': 1e-5, 'I_read1_A': 1.1e-5, 'flags': 'RBL overflow in 4 reads (1e-05 A range)'}], 1e-5),
                       ([{'Irange_A': 1e-5, 'I_read1_A': 3e-7}], 1e-6)):     # one 10 uA run judges 1 uA too
        r1, r0, why = gc_screen.choose_ranges(runs, c)
        assert (r1, r0) == (want, 1e-7), (runs, r1, r0, why)
    assert '1e-06 A' in gc_screen.choose_ranges([{'Irange_A': 1e-6, 'I_read1_A': 5e-7}], c)[2]

    # fake run on one cell (fake W1/L2 read FET ~1.7 uA: over 1 uA, so 10 uA)
    cfg = {**cfg, 'gc_screen': {**cfg['gc_screen'], 'wbl_vt_V': [0.1, 1.6]}}     # fake read FETs: Vt ~0.25 V
    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'R29C17')
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=fakes, only='R22C05', origin='R29C17', keep_order=True)
    assert [(r['test'], r['status']) for r in rows] == [(t, 'measured') for t in
                                                         ('gc_wbl', 'gc_wwl', 'gc_pmu_rw', 'gc_pmu_rw', 'gc_pmu_ret')], rows
    assert [r['Irange_A'] for r in rows if r['test'] == 'gc_pmu_rw'] == [1e-6, 1e-5]
    ret = [r for r in rows if r['test'] == 'gc_pmu_ret'][0]
    assert (ret['Irange_A'], ret['Irange_s0_A']) == (1e-5, 1e-7), ret
    kx = cmds(bench, 'K4200')
    assert kx.count(':PMU:EXECUTE') == 4                         # write/read on 1 uA and 10 uA, retention 1 and 0
    rbl = [c_.split(', ')[-1] for c_ in kx if c_.startswith(':PMU:MEASURE:RANGE 2, 2,')]
    assert rbl == ['1e-06', '1e-05', '1e-05', '1e-07'], rbl
    d = gc_pmu_ret.read_frame(ret['main_file'])
    assert len(d) == 42 and set(d[d.State == 0]['Irange_RBL_A']) == {1e-7}
    die = os.path.join(DATA, 'wd_bobafet_6916', '0_0', 'R22C05')
    s = pd.read_csv(os.path.join(die, [f for f in os.listdir(die) if f.startswith('GC_screen_')][0])).iloc[0]
    assert s['rw_1uA_I_read1_A'] > 0 and s['rw_10uA_I_read1_A'] > 9e-7 and 'RBL overflow' in s['rw_1uA_flags']
    assert s['Irange_s1_A'] == 1e-5 and 'on 1e-05 A' in s['ranges_why'], s['ranges_why']
    meta = json.load(open(ret['main_file'].replace('GC_PMU_ret_', 'GC_PMU_ret_meta_').replace('.csv', '.json')))
    assert meta['params']['tread_s0_s'] == 150e-6 and meta['states']['0']['budgets']['samples_per_window'] > 180
    print("test_gc_screen_30us OK")


def test_gc_screen_pre_rw():
    """gc_screen.pre_rw: one throw-away write/read on 10 uA after a passing screen, saved as GC_PMU_rw_pre1_10uA_*
    and logged as test gc_pmu_rw_pre, then the measured write/reads (1 uA, 10 uA), ranges and retention as before."""
    import pandas as pd
    from measure import gc_screen
    shutil.rmtree(DATA, ignore_errors=True)
    cfg = json.load(open('configs/bobafet_6916_gc_screen_pre_rw2_ret2000_30us_k4200.json'))
    c = gc_screen.check_config(cfg)
    assert c['pre_rw'] == 1 and c['pre_rw_irange'] == 1e-5 and cfg['origin_device'] == 'R23C15'
    for bad in ({'pre_rw': -1}, {'pre_rw': 1.5}, {'pre_rw_irange': 2e-6}):
        try:
            gc_screen.check_config({**cfg, 'gc_screen': {**cfg['gc_screen'], **bad}})
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass
    cfg = {**cfg, 'gc_screen': {**cfg['gc_screen'], 'wbl_vt_V': [0.1, 1.6]}}     # fake read FETs: Vt ~0.25 V
    full = dm.load_devices(cfg['device_csv'])
    fakes = run_map.make_fake_instruments(cfg, full, 'R23C15')
    rows, bench = run_map.run(cfg, data_dir=DATA, fakes=fakes, only='R13C17', origin='R23C15', keep_order=True)
    assert [(r['test'], r['status']) for r in rows] == [(t, 'measured') for t in (
        'gc_wbl', 'gc_wwl', 'gc_pmu_rw_pre', 'gc_pmu_rw', 'gc_pmu_rw', 'gc_pmu_ret')], rows
    assert [r['Irange_A'] for r in rows if r['test'].startswith('gc_pmu_rw')] == [1e-5, 1e-6, 1e-5]
    kx = cmds(bench, 'K4200')
    assert kx.count(':PMU:EXECUTE') == 5
    rbl = [c_.split(', ')[-1] for c_ in kx if c_.startswith(':PMU:MEASURE:RANGE 2, 2,')]
    assert rbl[:3] == ['1e-05', '1e-06', '1e-05'] and rbl[-1] == '1e-07', rbl
    die = os.path.join(DATA, 'wd_bobafet_6916', '0_0', 'R13C17')
    files = os.listdir(die)
    assert any(f.startswith('GC_PMU_rw_pre1_10uA_') and f.endswith('.csv') for f in files), files
    assert any(f.startswith('GC_PMU_rw_reads_pre1_10uA_') for f in files), files
    s = pd.read_csv(os.path.join(die, [f for f in files if f.startswith('GC_screen_')][0])).iloc[0]
    assert s['pre1_I_read1_A'] > 0 and 'GC_PMU_rw_pre1_10uA_' in s['pre1_file'] and s['rw_1uA_I_read1_A'] > 0
    assert 'pre1' not in s['ranges_why']
    print("test_gc_screen_pre_rw OK")


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


def multi_cfg(drift_correct_um=2.0, drift_limit_um=3.0):
    """The unknown_2 six-array config cut to two short arrays (L = 2 and 1 only): bottom-left Lc100 and
    top-left Lc80 (the die 8000 um above), probes starting on L:0.04 of bottom-left Lc500."""
    import pandas as pd
    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(DATA, exist_ok=True)
    cfg = json.load(open('configs/unknown_2_BL_TL_lc100_80_60_idvg.json'))
    segs = []
    for s in (cfg['segments'][0], cfg['segments'][3]):
        full = pd.read_csv(s["device_csv"])
        path = os.path.join(DATA, os.path.basename(s['device_csv']))
        full[full['L in um'] >= 1].to_csv(path, index=False)
        segs.append({**s, 'device_csv': path})
    cfg['segments'] = segs
    cfg['motion'].update(drift_correct_um=drift_correct_um, drift_limit_um=drift_limit_um)
    return cfg


def test_multi_segment_run():
    """'segments': two arrays on two dies in one run; data, summaries per die / Lc; resume; the
    segment-start stop when the first devices of an array all fail."""
    import csv as _csv
    cfg = multi_cfg()
    plan, sites = run_map.build_multi_plan(cfg, data_dir=DATA)
    assert len(plan) == 48 and [p['key'] for p in plan[:2]] == ['L:2', 'K:2'], [p['key'] for p in plan[:3]]
    assert (plan[0]['dx'], plan[0]['dy'], plan[0]['hops']) == (-2600.0, -2400.0, 2), plan[0]
    p24 = plan[24]   # top-left Lc80 starts at L:2 (bottom row, right side), 8000 um up from bottom-left Lc100
    assert p24['key'] == 'L:2' and p24['cfg']['sample']['die_idx'] == 'top_left', p24['key']
    assert abs(p24['tx'] - 2.41) < 1e-6 and abs(p24['ty'] - (8000 + 2806.7 - 2400)) < 1e-6, (p24['tx'], p24['ty'])
    rows, bench = run_map.run(cfg, data_dir=DATA, interactive=False)
    assert len(rows) == 48 and all(r['status'] == 'measured' for r in rows), [(r['key'], r['reason']) for r in rows]
    assert [(r['die_idx'], r['Lc_nm']) for r in rows[::24]] == [('bottom_left', 100), ('top_left', 80)], rows[::24]
    for die, lc in (('bottom_left', 100), ('top_left', 80)):
        folder = os.path.join(DATA, 'unknown_2', die)
        summ = [f for f in os.listdir(folder) if f.startswith('summary_')]
        assert len(summ) == 1 and os.path.isdir(os.path.join(folder, f'Lc{lc}', 'A_L2')), os.listdir(folder)
        with open(os.path.join(folder, summ[0])) as f:
            assert {(r['die_idx'], r['Lc_nm']) for r in _csv.DictReader(f)} == {(die, str(lc))}
    assert [f for f in os.listdir(os.path.join(DATA, 'unknown_2')) if f.startswith('run_')], "run log in sample dir"
    rows, _ = run_map.run(cfg, data_dir=DATA, interactive=False)
    assert rows == [], "resume: all done"

    cfg = multi_cfg()
    plan, sites = run_map.build_multi_plan(cfg, data_dir=DATA)
    # second array: 2 gate breakdowns, then opens; only the opens count toward the stop
    keys = [f"top_left Lc80|{p['key']}" for p in plan[24:33]]
    fakes = run_map.make_fake_instruments(cfg, sites, 'origin|L:0.04', dead=keys[2:],
                                          faults={k: 'gate_break' for k in keys[:2]})
    rows, _ = run_map.run(cfg, data_dir=DATA, fakes=fakes)
    assert len(rows) == 24 + 2 + cfg['segment_start_max_fails'], len(rows)
    assert all(r['status'] != 'measured' for r in rows[24:]), rows[24:]
    assert all(r['reason'].startswith('gate compliance') for r in rows[24:26]), [r['reason'] for r in rows[24:26]]
    assert all(r['Id_max_A'] < 1e-9 for r in rows[26:]), [r['Id_max_A'] for r in rows[26:]]
    print("test_multi_segment_run OK")


def test_drift_limit():
    """Systematic chuck error (0.2 % scale + 0.1 deg) over the two-die run, long hops included:
    drift_correct_um = 2 folds corrections into normal moves, drift_limit_um = 3 adds a correction move
    after any landing beyond +/-3 um, so every contact is within +/-3 um of the layout path."""
    import re
    err = dict(move_scale=1.002, move_rot_deg=0.1)
    cfg = multi_cfg()
    plan, sites = run_map.build_multi_plan(cfg, data_dir=DATA)
    rows, bench = run_map.run(cfg, data_dir=DATA,
                              fakes=run_map.make_fake_instruments(cfg, sites, 'origin|L:0.04', prober_kw=err))
    assert len(rows) == 48 and all(r['status'] == 'measured' for r in rows), [r['reason'] for r in rows]
    folder = os.path.join(DATA, 'unknown_2')
    log = open(os.path.join(folder, [f for f in os.listdir(folder) if f.startswith('run_')][-1])).read()
    landings = [re.findall(r'accumulated drift \(([-+.\d]+), ([-+.\d]+)\)', chunk)
                for chunk in log.split('Chuck -> contact height')[1:-1]]
    worst = max(abs(float(v)) for chunk in landings if chunk for v in chunk[-1])
    n_fix = log.count('correction move')
    assert worst <= 3.0 and n_fix >= 1, (worst, n_fix)

    cfg = multi_cfg(drift_limit_um=None)     # without the hard limit the long hops overshoot 3 um
    plan, sites = run_map.build_multi_plan(cfg, data_dir=DATA)
    run_map.run(cfg, data_dir=DATA, fakes=run_map.make_fake_instruments(cfg, sites, 'origin|L:0.04', prober_kw=err))
    log = open(os.path.join(folder, [f for f in os.listdir(folder) if f.startswith('run_')][-1])).read()
    drifts = [abs(float(v)) for d in re.findall(r'accumulated drift \(([-+.\d]+), ([-+.\d]+)\)', log) for v in d]
    assert max(drifts) > 3.0 and 'correction move' not in log, max(drifts)
    print(f"test_drift_limit OK ({n_fix} correction moves, worst landing {worst:.2f} um)")


# Every test, in run order: test_full_map returns the config test_resume continues from (used by the pytest wrapper,
# tests/test_measure_dryrun.py, as well as by the runner below).
TESTS = [test_full_map, test_resume, test_fault_cleanup, test_keyboard_interrupt,
             test_dead_device_screened, test_limits_and_origin, test_chuck_drift_guard,
             test_fmt21_parse, test_fault_devices_and_compliance, test_igzo_v6_device_id_map,
             test_drift_correction, test_long_move_hops_keep_order, test_multi_segment_run, test_drift_limit,
             test_max_id_scales_with_WL,
             test_k4200_map, test_gc_map_scan_only, test_gc_wbl_sweep, test_gc_wwl_sweep, test_gc_retention,
             test_gc_pmu_rw, test_gc_pmu_rw_ranges, test_gc_pmu_ret, test_gc_screen_ret, test_gc_screen_auto_range, test_gc_screen_30us, test_gc_screen_pre_rw,
             test_k4200_driver_kxci,
             test_k4200_rpm_cmd_pmu,
             test_sarb_rules_and_rate_model,
             test_stress_stair_map, test_stress_stair_ac, test_k4200_stale_reply_and_status,
             test_live_needs_confirmation]


if __name__ == '__main__':
    import io, contextlib
    quiet = io.StringIO()
    cfg = None
    for t in TESTS:
        with contextlib.redirect_stdout(quiet):
            out = t(cfg) if t is test_resume else t()
        if t is test_full_map:
            cfg = out
        print(quiet.getvalue().strip().splitlines()[-1]); quiet.seek(0); quiet.truncate()
    shutil.rmtree(DATA, ignore_errors=True)
    print("ALL DRY-RUN TESTS PASSED")
