"""Step the S200 chuck over a device CSV and run the adaptive Id-Vg on each FET.

Dry run (default) uses fake instruments and sends nothing over GPIB:
    python -m measure.run_map --config configs/lc500_idvg.json
Live run (real GPIB; asks for typed confirmation first):
    python -m measure.run_map --config configs/lc500_idvg.json --live --limit 2

Before a live run, land the probes on the origin device (config 'origin_device',
default A:2, overridable with --origin). All moves are relative to that device.

The SMU tool is config instruments.smu_tool: 'b1500' (default, Keysight B1500A) or
'k4200' (Keithley 4200A-SCS over KXCI; timing options in the config's 'k4200' block).

Config 'experiment': 'idvg' (default, the adaptive Id-Vg) or 'stress_stair' (4200A PMU measure-stress-
measure with staircase reads and 0 V recovery, the scheme of the KULT bias_stress.c, measure.stress_stair,
settings in the 'stress_stair' block; resume treats a device with a PMU_stair file as done, so a device is
never stressed twice). The earlier 'bias_stress' and 'pmu_diag' experiments are archived with their data in
.ignore/k4200_stress_archive_20261016/.
'gc_wbl' (4200A): 2T gain cell WBL sweep, the read FET transfer curve (measure.gc_sweep, settings in the
'gc_sweep' block, smu_map with wwl/wbl/rbl/rwl); devices are cells from layouts/igzo_v6_GC_cells.csv.
'gc_wwl' (4200A): 2T gain cell WWL sweep after conditioning, states 1 and 0 (measure.gc_sweep, 'gc_wwl' block).
'gc_retention' (4200A): 2T gain cell SMU retention, states 1 and 0 (measure.gc_sweep, 'gc_retention' block).
'gc_pmu_rw' (4200A PMUs): 2T gain cell alternating write/read transient, Clarius gc_pmu_rw over :PMU:SARB
(measure.gc_pmu, 'gc_pmu_rw' block; k4200.rpm_cmd must be 'pmu').
'gc_pmu_ret' (4200A PMUs): 2T gain cell retention, spot-mean reads at a list of times after one write per state,
Clarius gc_pmu_retention over :PMU:SARB (measure.gc_pmu_ret, 'gc_pmu_ret' block; k4200.rpm_cmd 'pmu').
'gc_screen_ret' (4200A SMUs + PMUs): per cell in one landing, WBL + WWL sweeps, then the PMU retention only if
both pass the 'gc_screen' criteria (measure.gc_screen); one summary row per test ('test' column).

Config 'segments' (Column:L arrays, idvg only): measure several arrays, on one or several dies, in one run with
one chuck-drift reference. The probes start on 'origin_device' of the array in 'device_csv' / 'sample'; each
segment {die_idx, Lc_nm, device_csv, array_offset_um, die_offset_um, start_row, first_row} is one array, visited
in list order, serpentine within it. array_offset_um = layout (x, y) of the segment array's (0, 0) device
relative to the origin array's (0, 0) on the same die; die_offset_um = layout offset of the segment's die from
the origin die. Data go to each segment's die / Lc folders, one summary per die, the run log in the sample
folder. 'segment_start_max_fails' (optional): stop the run when that many devices at the start of a segment are
open (max |Id| of the initial scan < 'segment_open_Id_max_A', default 1 nA, and no gate compliance) before any is
measured (probes probably not on the array); other failures (breakdown, leakage, short) do not count.

Email (live runs only, measure.notify, settings in .env): one email when the run ends (done, stopped, Ctrl+C,
error, auto-stop) with the counts and the device the probes are above; with config 'alert_after_fails' = N, also
one alert per streak of N devices in a row that are not measured (the run goes on).
"""
import argparse
import collections
import contextlib
import csv
import json
import math
import os
import sys
import time

import matplotlib
import numpy as np
import pandas as pd

from measure import device_map as dm
from measure import fet_idvg, fet_idvg_k4200, gc_pmu, gc_pmu_ret, gc_screen, gc_sweep, notify
from measure.instruments import b1500 as b1500_mod
from measure.instruments import k4200 as k4200_mod
from measure.instruments import prober as prober_mod

SMU_TOOLS = {'b1500': (b1500_mod, fet_idvg), 'k4200': (k4200_mod, fet_idvg_k4200)}

SUMMARY_FIELDS = ['time', 'sample', 'die_idx', 'key', 'Lc_nm', 'col', 'W_um', 'L_um', 'x_um', 'y_um', 'status', 'reason',
                  'Vt_V',
                  'on_off', 'gate_start_V', 'flags', 'scan_file', 'main_file',
                  'dVt_stress_end_V', 'dVt_recovery_end_V', 't_stress_s', 'dvt_file',
                  'Vt_bwd_V', 'hyst_V', 'Ileak_max_A', 'Vt_s0_V', 'I0_s1_A', 'I0_s0_A', 'Iend_s1_A', 'Iend_s0_A',
                  'Iret_s1_start_A', 'Iret_s1_end_A', 'Iret_s0_start_A', 'Iret_s0_end_A', 'ret_ratio_end',
                  'I_read1_A', 'I_read0_A', 'read_ratio', 'margin_A', 'I_read1_std_A', 'I_read0_std_A', 'Irange_A', 'test',
                  'I_read1_max_A', 'I_read0_absmax_A', 'Irange_s0_A', 'Id_max_A', 'Ig_max_A']
EXPERIMENTS = ('idvg', 'stress_stair', 'gc_wbl', 'gc_wwl', 'gc_retention', 'gc_pmu_rw', 'gc_pmu_ret', 'gc_screen_ret')
PMU_EXPERIMENTS = ('stress_stair',)
GC_EXPERIMENTS = ('gc_wbl', 'gc_wwl', 'gc_retention', 'gc_pmu_rw', 'gc_pmu_ret', 'gc_screen_ret')
GC_PMU_EXPERIMENTS = ('gc_pmu_rw', 'gc_pmu_ret', 'gc_screen_ret')
GC_FUNCS = {'gc_wbl': (gc_sweep.check_config, gc_sweep.measure_cell),       # (config check, per-cell measurement)
            'gc_wwl': (gc_sweep.check_wwl_config, gc_sweep.measure_cell_wwl),
            'gc_retention': (gc_sweep.check_retention_config, gc_sweep.measure_cell_retention),
            'gc_pmu_rw': (gc_pmu.check_config, gc_pmu.measure_cell),
            'gc_pmu_ret': (gc_pmu_ret.check_config, gc_pmu_ret.measure_cell),
            'gc_screen_ret': (gc_screen.check_config, gc_screen.measure_cell)}
K4200_ONLY = PMU_EXPERIMENTS + GC_EXPERIMENTS
FILE_PREFIX = {'stress_stair': 'PMU_stair_', 'gc_wbl': gc_sweep.FILE_PREFIX, 'gc_wwl': gc_sweep.WWL_PREFIX,
               'gc_retention': gc_sweep.RET_PREFIX, 'gc_pmu_rw': gc_pmu.FILE_PREFIX,
               'gc_pmu_ret': gc_pmu_ret.FILE_PREFIX, 'gc_screen_ret': gc_screen.FILE_PREFIX}


def _has(d, k):
    v = d.get(k) if hasattr(d, 'get') else None
    return v is not None and v == v   # present and not NaN


def smu_tool(cfg):
    """-> (name, instrument module, sweep backend) for cfg instruments.smu_tool (default 'b1500')."""
    name = cfg['instruments'].get('smu_tool', 'b1500')
    if name not in SMU_TOOLS:
        raise ValueError(f"instruments.smu_tool must be one of {sorted(SMU_TOOLS)}, not {name!r}")
    return (name,) + SMU_TOOLS[name]


def experiment(cfg):
    name = cfg.get('experiment', 'idvg')
    if name not in EXPERIMENTS:
        raise ValueError(f"experiment must be one of {EXPERIMENTS}, not {name!r}")
    if name in K4200_ONLY and smu_tool(cfg)[0] != 'k4200':
        raise ValueError(f"experiment {name!r} needs instruments.smu_tool = 'k4200'")
    return name


def device_W(cfg, d):
    """W from the device CSV when it has one (IGZO v6 style), else the sample-wide W_um."""
    return float(d['W']) if _has(d, 'W') else float(cfg['sample']['W_um'])


class Logger:
    def __init__(self, path, sink=None):
        """sink: optional callable(line), also given every line (e.g. the web app's event stream)."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.f = open(path, 'a', encoding='utf-8')
        self.sink = sink
        self.tail = collections.deque(maxlen=40)   # the latest lines, for the notification emails

    def __call__(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        self.f.write(line + '\n'); self.f.flush()
        self.tail.append(line)
        if self.sink is not None:
            self.sink(line)

    def close(self):
        self.f.close()


def descriptor(cfg, d):
    s = cfg['sample']
    if _has(d, 'W'):
        return f"{s['name']}_{s['die_idx']}_{d['key']}_W{float(d['W']):g}_L{float(d['L']):g}"
    return f"{s['name']}_{s['die_idx']}_Lc{s['Lc_nm']}_{d['col']}_L{d['L']:g}"


def die_dir(cfg, data_dir):
    return os.path.join(data_dir, cfg['sample']['name'], cfg['sample']['die_idx'])


def device_dir(cfg, d, data_dir):
    if _has(d, 'W'):
        return os.path.join(die_dir(cfg, data_dir), d['key'])
    return os.path.join(die_dir(cfg, data_dir), f"Lc{cfg['sample']['Lc_nm']}", f"{d['col']}_L{d['L']:g}")


def already_measured(cfg, d, data_dir):
    folder = device_dir(cfg, d, data_dir)
    if not os.path.isdir(folder):
        return False
    tail = f"_{descriptor(cfg, d)}.csv"
    first = FILE_PREFIX.get(experiment(cfg), 'IdVg_initial_scan_')
    return any(f.startswith(first) and f.endswith(tail) for f in os.listdir(folder))


def build_plan(cfg, only=None, columns=None, L_values=None, limit=None, origin=None, data_dir=None, log=print,
               keep_order=False, origin_offset=None):
    """keep_order=True visits the --only devices in the order given instead of serpentine.
    origin_offset = layout (x, y) um of the die the probes are on relative to this die; its long first step
    is split into hops of at most max_step_um."""
    full = dm.load_devices(cfg['device_csv'])
    sel = dm.filter_devices(full, only, columns, L_values)
    if keep_order:
        if not only:
            raise ValueError("keep_order needs an explicit device list (--only)")
        sel = sel.set_index('key').loc[list(dict.fromkeys(dm.parse_keys(only)))].reset_index()
        sel = sel[[c for c in full.columns if c in sel.columns]]
    else:
        sel = dm.serpentine(sel, first_row=cfg.get('first_row', 'left'), start_row=cfg.get('start_row', 'bottom'))
    if not cfg.get('repeat_measurement', False):
        done = [k for k, (_, r) in zip(sel['key'], sel.iterrows()) if already_measured(cfg, r, data_dir)]
        if done:
            log(f"Resume: skipping {len(done)} already-measured devices")
        sel = sel[~sel['key'].isin(done)]
    if limit:
        sel = sel.head(limit)
    m = cfg['motion']
    plan = dm.plan_moves(sel, dm.parse_keys(origin or cfg['origin_device'])[0], m['transform'],
                         m['max_step_um'], m['bounds_margin_um'], full,
                         split_long=m.get('split_long_moves', False) or bool(origin_offset),
                         origin_offset=origin_offset or (0.0, 0.0))
    return plan, full


def segment_cfg(cfg, seg):
    """Config of one segment of a multi-array run: its die, Lc, device CSV and serpentine start."""
    c = {k: v for k, v in cfg.items() if k != 'segments'}
    c['sample'] = {**cfg['sample'], 'die_idx': seg['die_idx'], 'Lc_nm': seg['Lc_nm']}
    c['device_csv'] = seg['device_csv']
    for k in ('start_row', 'first_row'):
        if k in seg:
            c[k] = seg[k]
    return c


def segment_label(c):
    return f"{c['sample']['die_idx']} Lc{c['sample']['Lc_nm']}"


def build_multi_plan(cfg, origin=None, limit=None, data_dir=None, log=print):
    """Plan over cfg['segments'] (see the module docstring). Returns (plan, sites): plan entries carry the
    segment config under 'cfg'; sites = every device of the origin array and the segments in one layout frame,
    keys '<segment>|<key>' (the origin array: 'origin|<key>'), for the bounds and the fake bench."""
    m = cfg['motion']
    t = m['transform']
    base = dm.load_devices(cfg['device_csv'])
    origin_key = dm.parse_keys(origin or cfg['origin_device'])[0]
    if origin_key not in set(base['key']):
        raise ValueError(f"Origin device {origin_key} is not in {cfg['device_csv']}")
    o = base.set_index('key').loc[origin_key]
    ox, oy = dm.to_stage(o['x'], o['y'], **t)
    sites = [base.assign(key='origin|' + base['key'])]
    todo = []
    for seg in cfg['segments']:
        c = segment_cfg(cfg, seg)
        full = dm.load_devices(c['device_csv'])
        if full['lc'].isna().any() or set(full['lc']) != {c['sample']['Lc_nm']}:
            raise ValueError(f"{c['device_csv']}: 'Lc in nm' must all be the segment's Lc_nm {c['sample']['Lc_nm']}")
        off = [a + b for a, b in zip(seg.get('array_offset_um', (0, 0)), seg.get('die_offset_um', (0, 0)))]
        sites.append(full.assign(key=segment_label(c) + '|' + full['key'], x=full['x'] + off[0],
                                 y=full['y'] + off[1]))
        sel = dm.serpentine(full, first_row=c.get('first_row', 'left'), start_row=c.get('start_row', 'bottom'))
        if not cfg.get('repeat_measurement', False):
            done = [k for k, (_, r) in zip(sel['key'], sel.iterrows()) if already_measured(c, r, data_dir)]
            if done:
                log(f"Resume: {segment_label(c)}: skipping {len(done)} already-measured devices")
            sel = sel[~sel['key'].isin(done)]
        todo += [(c, off, r) for _, r in sel.iterrows()]
    sites = pd.concat(sites, ignore_index=True)
    if sites['key'].duplicated().any():
        raise ValueError("Two segments with the same die and Lc")
    if limit:
        todo = todo[:limit]
    xs, ys = zip(*(dm.to_stage(x, y, **t) for x, y in zip(sites['x'], sites['y'])))
    lo_x, hi_x = min(xs) - ox - m['bounds_margin_um'], max(xs) - ox + m['bounds_margin_um']
    lo_y, hi_y = min(ys) - oy - m['bounds_margin_um'], max(ys) - oy + m['bounds_margin_um']
    plan, cx, cy = [], 0.0, 0.0
    for c, off, r in todo:
        sx, sy = dm.to_stage(r['x'] + off[0], r['y'] + off[1], **t)
        tx, ty = sx - ox, sy - oy
        if not (lo_x <= tx <= hi_x and lo_y <= ty <= hi_y):
            raise ValueError(f"Target for {segment_label(c)} {r['key']} ({tx:.1f}, {ty:.1f}) is outside the map bounds")
        dx, dy = tx - cx, ty - cy
        hops = max(1, math.ceil(math.hypot(dx, dy) / m['max_step_um']))
        plan.append({**r.to_dict(), 'cfg': c, 'tx': tx, 'ty': ty, 'dx': dx, 'dy': dy, 'hops': hops})
        cx, cy = tx, ty
    return plan, sites


def make_fake_instruments(cfg, full, origin_key, dead=(), fail_on_trigger=None, faults=None, land_tol_um=10.0,
                          prober_kw=None, origin_offset=None):
    """Fake prober + SMU tool (FakeB1500, or the real K4200 driver on a FakeK4200Visa) whose simulated
    device follows the fake chuck position.
    `dead` = open sites; `faults` = {key: 'open' | 'short' | 'gate_break' | 'overflow'};
    `prober_kw` goes to FakeVisaProber (e.g. move_scale / move_rot_deg for a systematic chuck error)."""
    from measure.instruments.fakes import Bench, FakeVisaProber, FakeB1500, FakeK4200Visa
    bench = Bench()
    res = FakeVisaProber(bench, **(prober_kw or {}))
    x0, y0 = res.x, res.y
    t = cfg['motion']['transform']
    o = full.set_index('key').loc[origin_key]
    off = origin_offset or (0.0, 0.0)
    ox, oy = dm.to_stage(o['x'] + off[0], o['y'] + off[1], **t)
    pos, sites = [], []
    for _, r in full.iterrows():
        sx, sy = dm.to_stage(r['x'], r['y'], **t)
        pos.append((sx - ox, sy - oy))
        sites.append((r['key'], device_W(cfg, r), float(r['L'])))
    pos = np.array(pos)
    faults = {**{k: 'open' for k in dead}, **(faults or {})}

    def lookup():
        """Device under the probes: nearest site within land_tol_um (pads are tens of um wide), else open."""
        dist = np.hypot(pos[:, 0] + (res.x - x0), pos[:, 1] + (res.y - y0))
        i = int(np.argmin(dist))
        hit = sites[i] if dist[i] <= land_tol_um else None
        fault = faults.get(hit[0]) if hit else 'open'
        if fault == 'open':
            return None
        return hit[1], hit[2], fault

    if smu_tool(cfg)[0] == 'k4200':
        kw = {}
        if experiment(cfg) in K4200_ONLY:   # the lab's *OPT? (no preamps, RPMs on PMU1/PMU2)
            kw['opt'] = 'SMU1,SMU2,SMU3,SMU4,SMU5,PMU1RPM1-2,PMU2RPM1-2'
        if experiment(cfg) in GC_PMU_EXPERIMENTS:   # gain-cell PMU model on the RPM channels of the smu_map
            from measure.instruments.k4200_pmu import pmu_channel
            kw['pmu_gc'] = tuple(pmu_channel(int(str(cfg['smu_map'][t]).lower().replace('smu', '')))
                                 for t in ('wwl', 'wbl', 'rwl', 'rbl'))
        fake = FakeK4200Visa(bench, device_lookup=lookup, fail_on_trigger=fail_on_trigger, **kw)
        return bench, res, k4200_mod.K4200(fake, **cfg.get('k4200', {}))
    return bench, res, FakeB1500(bench, device_lookup=lookup, fail_on_trigger=fail_on_trigger)


def confirm(prompt):
    print(prompt)
    return input("Type 'yes' to continue: ").strip().lower() == 'yes'


OUTCOMES = {'DONE': 'all planned devices done', 'STOPPED': 'stopped on request (web Stop)',
            'INTERRUPTED': 'interrupted (Ctrl+C)', 'ERROR': 'stopped on an error',
            'AUTO-STOPPED': 'stopped: too many open devices at the start of a segment'}


def duration(seconds):
    """'H:MM:SS', hours not wrapped at 24 (a full map takes a day)."""
    m, s = divmod(int(seconds), 60)
    return f"{m // 60}:{m % 60:02d}:{s:02d}"


def _run_name(cfg, multi):
    return cfg['sample']['name'] + ('' if multi else f" {cfg['sample']['die_idx']}")


def end_email(cfg, multi, outcome, error, rows, n_plan, at, t0, live, summaries, log):
    """(subject, body) of the email sent when a run ends."""
    devices = list(dict.fromkeys((r.get('die_idx'), r.get('Lc_nm'), r['key']) for r in rows))
    ok = {(r.get('die_idx'), r.get('Lc_nm'), r['key']) for r in rows if r.get('status') == 'measured'}
    counts = collections.Counter(r.get('status') for r in rows)
    subject = (f"[probe] {_run_name(cfg, multi)}: {outcome}, {len(devices)}/{n_plan} devices "
               f"({len(ok)} measured)")
    lines = [f"Outcome: {OUTCOMES.get(outcome, outcome)}"]
    if error:
        lines.append(f"Error: {error}")
    lines += [f"Run: {'LIVE' if live else 'DRY RUN'} {experiment(cfg)} on {smu_tool(cfg)[0]}, sample "
              f"{_run_name(cfg, multi)}",
              f"Started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t0))}, ended "
              f"{time.strftime('%Y-%m-%d %H:%M:%S')}, took {duration(time.time() - t0)}",
              f"Devices: {len(devices)} of {n_plan} planned"]
    lines += [f"  {s}: {n}" for s, n in counts.items()]
    if rows:
        r = rows[-1]
        lines.append(f"Last device: {r['key']} ({r.get('status')}{': ' + str(r['reason']) if r.get('reason') else ''})")
    lines.append(f"Probes at separation above {at}" + ('' if multi else f": next --origin {at}"))
    lines += [f"Summary: {p}" for p in summaries] + [f"Log: {log.path}", '', 'Last log lines:'] + list(log.tail)[-20:]
    return subject, '\n'.join(lines)


def streak_email(cfg, multi, streak, i, n_plan, log):
    """(subject, body) of the alert after N devices in a row that are not measured."""
    subject = (f"[probe] {_run_name(cfg, multi)}: {len(streak)} failures in a row, at {streak[-1]['key']} "
               f"({i + 1}/{n_plan})")
    lines = [f"The last {len(streak)} devices were not measured:"]
    lines += [f"  {r['key']}  {r.get('status')}  max Id {r.get('Id_max_A', '')} A  {r.get('reason', '')}"
              for r in streak]
    drift = [s for s in log.tail if 'accumulated drift' in s]
    if drift:
        lines += ['', 'Latest chuck position:', drift[-1]]
    lines += ['', "The run goes on. To stop it: Ctrl+C in its terminal (or Stop in the web app).",
              f"Log: {log.path}"]
    return subject, '\n'.join(lines)


def run(cfg, live=False, only=None, columns=None, L_values=None, limit=None, origin=None, data_dir=None,
        fakes=None, interactive=True, keep_order=False, origin_offset=None,
        log_sink=None, confirm_fn=None, stop_event=None, on_plan=None, on_row=None, notify_fn=None):
    """Returns (summary_rows, bench_or_None). `fakes` = (bench, prober_resource, smu_inst) for tests.

    Hooks for a caller other than the CLI (the web app); all optional, the defaults are the CLI's behaviour:
    - log_sink(line): also receives every log line
    - confirm_fn(prompt) -> bool: replaces the typed 'yes' at the live gate and the alignment check
      (asked only when interactive=True, as before)
    - stop_event (threading.Event or anything with is_set()): checked before each device; when set the run
      stops there, through the same cleanup as KeyboardInterrupt (outputs zeroed, probes separated)
    - on_plan(plan): the planned devices, before any connection
    - on_row(row): each summary row as it is written
    - notify_fn(subject, body): sends the run-end email and the failure-streak alerts. Default: a live run uses
      measure.notify.from_env() (off when .env has no mail settings), a dry run sends nothing. A failed send is
      logged and never stops the run.
    """
    confirm_fn = confirm_fn or confirm
    data_dir = data_dir or cfg['data_dir']
    show = bool(cfg.get('show_plots')) and live
    matplotlib.use('TkAgg' if show else 'Agg', force=True)
    from measure.idvg_pipeline import measure_device
    exp = experiment(cfg)
    if exp == 'stress_stair':
        from measure import stress_stair
        stress_stair.check_config(cfg)          # raises on a bad setting before any connection or move
        if not only:
            raise ValueError("stress_stair stresses every device it visits: give the devices explicitly "
                             "(--only R04C18R,... / only=...), not the whole map")
    if exp in GC_EXPERIMENTS:
        GC_FUNCS[exp][0](cfg)

    multi = bool(cfg.get('segments'))
    if multi and (only or columns or L_values or keep_order or origin_offset or exp != 'idvg'):
        raise ValueError("a 'segments' run takes no --only/--columns/--L/--keep-order/--origin-die-offset "
                         "and runs the idvg experiment only")
    run_dir = os.path.join(data_dir, cfg['sample']['name']) if multi else die_dir(cfg, data_dir)
    t0 = time.time()
    stamp = time.strftime('%Y%m%d_%H%M%S', time.localtime(t0))
    log = Logger(os.path.join(run_dir, f"run_{stamp}{'' if live else '_dryrun'}.log"), sink=log_sink)
    origin_key = dm.parse_keys(origin or cfg['origin_device'])[0]
    tool_name, tool, idvg = smu_tool(cfg)
    log(f"Mode: {'LIVE' if live else 'DRY RUN (fake instruments, no GPIB)'} | SMU tool {tool_name} | "
        f"experiment {exp} | origin {origin_key}"
        + (f" of {segment_label(cfg)}" if multi else '')
        + (f" on the die at layout offset ({origin_offset[0]:g}, {origin_offset[1]:g}) um" if origin_offset else ''))

    if multi:
        plan, full = build_multi_plan(cfg, origin_key, limit, data_dir, log)
        fake_origin = f'origin|{origin_key}'
    else:
        plan, full = build_plan(cfg, only, columns, L_values, limit, origin_key, data_dir, log, keep_order,
                                origin_offset)
        fake_origin = origin_key
    log(f"Planned devices: {len(plan)}")
    if on_plan is not None:
        on_plan(plan)
    for i, p in enumerate(plan):
        first = multi and (i == 0 or p['cfg'] is not plan[i - 1]['cfg'])
        if first:
            log(f"  segment {segment_label(p['cfg'])}: {sum(q['cfg'] is p['cfg'] for q in plan)} devices")
        if i < 12 or first:
            log(f"  {p['key']:>8s}  target ({p['tx']:8.1f}, {p['ty']:8.1f})  step ({p['dx']:7.1f}, {p['dy']:7.1f}) um"
                + (f"  in {p['hops']} hops" if p.get('hops', 1) > 1 else ''))
    if not plan:
        log("Nothing to do."); log.close()
        return [], None

    m = cfg['motion']
    bench = rm = None
    if live:
        if interactive and not confirm_fn(
                f"LIVE RUN on {tool_name}: {exp}"
                + (f" ({stress_stair.stress_label(stress_stair.params(cfg.get('stress_stair', {})))} stress)"
                   if exp == 'stress_stair' else '')
                + f", {len(plan)} devices"
                + (f" in {len({id(p['cfg']) for p in plan})} arrays" if multi else '')
                + f". Probes must be landed on {origin_key}"
                + (f" of {segment_label(cfg)}" if multi else '')
                + (f" of the die at layout offset ({origin_offset[0]:g}, {origin_offset[1]:g}) um" if origin_offset
                   else '') + ". "
                f"SMUs {' '.join(f'{t}={s}' for t, s in cfg['smu_map'].items())}. First step "
                f"({plan[0]['dx']:.1f}, {plan[0]['dy']:.1f}) um."):
            log("Aborted by user before connecting."); log.close()
            return [], None
        if notify_fn is None:
            mailer = notify.from_env()
            notify_fn = mailer.send if mailer else None
            log(f"Email notifications on: {mailer.describe()}" if mailer else
                f"Email notifications off ({', '.join(notify.missing())} not set in .env)")
        rm, res = prober_mod.connect(cfg['instruments']['prober_resource'])
        try:
            smu_inst = tool.connect(cfg['instruments'][f'{tool_name}_resource'], log=log, **cfg.get(tool_name, {}))
        except Exception as e:
            log(f"Could not connect to the {tool_name}: {type(e).__name__}: {e}. Nothing was moved or biased.")
            for obj in (res, rm):
                try:
                    obj.close()
                except Exception:
                    pass
            log.close()
            raise
        waits = dict(xy_wait_s=m['xy_wait_s'], z_wait_s=m['z_wait_s'])
    else:
        bench, res, smu_inst = fakes or make_fake_instruments(cfg, full, fake_origin, origin_offset=origin_offset)
        waits = dict(xy_wait_s=0, z_wait_s=0)
    prober = prober_mod.VeloxProber(res, wait_mode=m['wait_mode'], velocity_pct=m['velocity_pct'],
                                    pos_tolerance_um=m['pos_tolerance_um'],
                                    pos_tolerance_rel=m.get('pos_tolerance_rel', 0.0),
                                    max_drift_um=m.get('max_drift_um'), drift_correct_um=m.get('drift_correct_um'),
                                    drift_limit_um=m.get('drift_limit_um'), log=log, **waits)
    if exp in GC_EXPERIMENTS:
        smus = tool.get_gc_smus(smu_inst, cfg['smu_map'])
        p = GC_FUNCS[exp][0](cfg) if exp not in GC_PMU_EXPERIMENTS else {'Irange': 1e-6}   # SMU range check only
        chk = (list(smus.values()), {'Irange': p['Irange']})
    else:
        smus = tool.get_smus(smu_inst, cfg['smu_map'])
        chk = (smus, cfg.get('measurement'))
    if hasattr(tool, 'check_settings'):
        try:
            tool.check_settings(smu_inst, *chk)
        except Exception as e:
            log(f"Settings rejected before any move: {e}")
            log.close()
            raise

    summary_name = f"summary_{stamp}{'' if live else '_dryrun'}.csv"
    summary_dirs = list(dict.fromkeys(die_dir(p['cfg'], data_dir) for p in plan)) if multi else [run_dir]
    max_fails = cfg.get('segment_start_max_fails') if multi else None
    open_Id_max = cfg.get('segment_open_Id_max_A', 1e-9)
    alert_n = cfg.get('alert_after_fails')

    def send(subject, body):
        if notify_fn is None:
            return
        try:
            notify_fn(subject, body)
            log(f"Email sent: {subject}")
        except Exception as e:
            log(f"Email notification failed: {type(e).__name__}: {e}")

    outcome = error = None
    at = origin_key           # device the probes are above (the next --origin)
    streak = []               # rows of the devices in a row that were not measured
    rows = []
    with contextlib.ExitStack() as stack:
        writers = {}              # die folder -> (file, csv writer)
        for folder in summary_dirs:
            os.makedirs(folder, exist_ok=True)
            sf = stack.enter_context(open(os.path.join(folder, summary_name), 'w', newline=''))
            writers[folder] = (sf, csv.DictWriter(sf, fieldnames=SUMMARY_FIELDS))
            writers[folder][1].writeheader()

        def write_row(c, r):
            sf, writer = writers[die_dir(c, data_dir) if multi else run_dir]
            writer.writerow(r); sf.flush()
            if on_row is not None:
                on_row(r)
        seg_fails = 0             # open devices in the current segment while none measured; None once one is
        try:
            for i, d in enumerate(plan):
                if stop_event is not None and stop_event.is_set():
                    log(f"Stop requested: stopping run before device {i + 1}/{len(plan)}")
                    outcome = 'STOPPED'
                    break
                c = d.get('cfg', cfg)
                if multi and (i == 0 or c is not plan[i - 1]['cfg']):
                    log(f"--- segment {segment_label(c)}")
                    seg_fails = 0
                log(f"=== [{i + 1}/{len(plan)}] {(segment_label(c) + ' ') if multi else ''}{d['key']}  "
                    f"(layout x={d['x']:g}, y={d['y']:g})")
                tool.zero_outputs(smu_inst)
                prober.separate()
                if math.hypot(d['dx'], d['dy']) > 1e-6:
                    n = d.get('hops', 1)
                    if n > 1:
                        log(f"Long step ({d['dx']:.1f}, {d['dy']:.1f}) um split into {n} hops")
                    for _ in range(n):
                        prober.move_relative(d['dx'] / n, d['dy'] / n)
                at = f"{segment_label(c)} {d['key']}" if multi else d['key']
                if live and i == 0 and interactive and not confirm_fn("Check alignment at separation height."):
                    log("Aborted by user before first contact.")
                    outcome = 'ABORTED'
                    break
                prober.contact()
                W = device_W(cfg, d)
                row = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'sample': c['sample']['name'],
                       'die_idx': c['sample']['die_idx'], 'key': d['key'],
                       'Lc_nm': d['lc'] if _has(d, 'lc') else '', 'col': d['col'], 'W_um': W, 'L_um': d['L'],
                       'x_um': d['x'], 'y_um': d['y']}
                parts = [{}]              # one summary row per part (gc_pmu_rw with several ranges: one per range)
                try:
                    if exp in GC_EXPERIMENTS:
                        fn = GC_FUNCS[exp][1]
                        out = fn(smu_inst, smus, cfg, device_dir(c, d, data_dir), descriptor(c, d),
                                 save=True, show=show, log=log)
                        parts = out if isinstance(out, list) else [out]
                    elif exp == 'stress_stair':
                        row.update(stress_stair.measure_device(smu_inst, smus, cfg, W, float(d['L']),
                                                               device_dir(c, d, data_dir), descriptor(c, d),
                                                               save=True, show=show, log=log))
                    else:
                        row.update(measure_device(smu_inst, smus, cfg['measurement'], W, float(d['L']),
                                                  device_dir(c, d, data_dir), descriptor(c, d),
                                                  save=True, show=show, log=log, idvg=idvg))
                except Exception as e:
                    row.update(status='failed', reason=f"{type(e).__name__}: {e}")
                    rows.append(row); write_row(c, row)
                    raise
                finally:
                    tool.zero_outputs(smu_inst)
                for part in parts:
                    r = {**row, **part}
                    rows.append(r); write_row(c, r)
                    log(f"{d['key']}: {r['status']} {r.get('reason', '')}"
                        + (f"({r['Irange_A']:g} A range)" if r.get('Irange_A') else '')
                        + (f" | flags: {r['flags']}" if r.get('flags') else ''))
                if any(part.get('status', row.get('status')) == 'measured' for part in parts):
                    streak = []
                else:
                    streak.append({**row, **parts[-1]})
                    if alert_n and len(streak) == alert_n:
                        send(*streak_email(cfg, multi, streak, i, len(plan), log))
                if seg_fails is not None:
                    if row.get('status') == 'measured':
                        seg_fails = None
                    elif (row.get('Id_max_A', np.inf) < open_Id_max
                          and not str(row.get('reason', '')).startswith('gate compliance')):
                        seg_fails += 1
                    if max_fails and seg_fails is not None and seg_fails >= max_fails:
                        log(f"STOPPING: {seg_fails} devices at the start of {segment_label(c)} were open (max Id < "
                            f"{open_Id_max:g} A) and none measured; the probes are probably not on the array. "
                            f"Check the landing before resuming.")
                        outcome = 'AUTO-STOPPED'
                        break
            outcome = outcome or 'DONE'
        except KeyboardInterrupt:
            outcome = 'INTERRUPTED'
            log("KeyboardInterrupt: stopping run")
        except Exception as e:
            outcome, error = 'ERROR', f"{type(e).__name__}: {e}"
            log(f"ERROR, stopping run: {error}")
            raise
        finally:
            for name, fn in (('zero outputs', lambda: tool.zero_outputs(smu_inst)),
                             ('separate', prober.separate)):
                try:
                    fn()
                except Exception as e:
                    log(f"Cleanup step '{name}' failed: {e}")
            if live:
                for obj in (res, rm):
                    try:
                        obj.close()
                    except Exception:
                        pass
                try:
                    tool.close(smu_inst)
                except Exception:
                    pass
            summaries = [os.path.join(folder, summary_name) for folder in summary_dirs]
            for p in summaries:
                log(f"Summary: {p}")
            if outcome not in (None, 'ABORTED'):
                send(*end_email(cfg, multi, outcome, error, rows, len(plan), at, t0, live, summaries, log))
            log.close()
    return rows, bench


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', required=True)
    ap.add_argument('--live', action='store_true', help='talk to the real instruments over GPIB')
    ap.add_argument('--limit', type=int, help='measure only the first N planned devices')
    ap.add_argument('--only', help="comma list of devices: Column:L (e.g. 'A:2,B:2') or device_id (e.g. 'R42C29R,R41C29R')")
    ap.add_argument('--columns', help="e.g. 'A,B,C' or '28,29'")
    ap.add_argument('--L', dest='L_values', help="e.g. '2,1,0.5'")
    ap.add_argument('--origin', help="device the probes are landed on now (default from config)")
    ap.add_argument('--origin-die-offset', help="X,Y: layout um of the die the probes are on now relative to the "
                    "die being measured (e.g. '0,8000' = --origin device of the die above); not saved in the config")
    ap.add_argument('--data-dir', help='override data_dir')
    ap.add_argument('--repeat', action='store_true', help='remeasure devices that already have data')
    ap.add_argument('--keep-order', action='store_true', help='visit the --only devices in the order given')
    ap.add_argument('--first-row', choices=('left', 'right'),
                    help="side the first row starts from: left = A->L (default), right = L->A")
    ap.add_argument('--start-row', choices=('bottom', 'top'),
                    help="row the serpentine starts on: bottom = lowest layout y (default), top = highest")
    a = ap.parse_args(argv)
    with open(a.config) as f:
        cfg = json.load(f)
    if a.first_row:
        cfg['first_row'] = a.first_row
    if a.start_row:
        cfg['start_row'] = a.start_row
    if a.repeat:
        cfg['repeat_measurement'] = True
    run(cfg, live=a.live, only=a.only, columns=a.columns.split(',') if a.columns else None,
        L_values=a.L_values.split(',') if a.L_values else None, limit=a.limit, origin=a.origin,
        data_dir=a.data_dir, keep_order=a.keep_order,
        origin_offset=tuple(float(v) for v in a.origin_die_offset.split(',')) if a.origin_die_offset else None)


if __name__ == '__main__':
    sys.exit(main())
