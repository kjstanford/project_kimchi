"""Screened PMU retention on a 2T gain cell, all in one landing: the WBL sweep (measure.gc_sweep, 'gc_sweep'
block) and the WWL sweep ('gc_wwl' block) as a health check, then the PMU retention (measure.gc_pmu_ret,
'gc_pmu_ret' block) only if both pass the 'gc_screen' criteria.

Pass rule:
- WBL: Vt_V found and inside wbl_vt_V; on_off >= wbl_on_off_min; Ileak_max_A (|I WWL|, |I WBL|) <= leak_max_A;
  no compliance flag
- WWL: Vt_V (write turn-on, state 1) found and inside wwl_vt_V; Iend_s1_A >= wwl_iend_s1_min_A;
  Iend_s1_A / Iend_s0_A >= wwl_storage_ratio_min; Ileak_max_A <= leak_max_A; no compliance flag
A NaN value fails its criterion.
auto_range: after a passing screen the PMU write/read (measure.gc_pmu, 'gc_pmu_rw' block, one range or a list,
e.g. [1e-6, 1e-5]; skip_cycles 1 leaves out its first cycle) runs first and sets the retention ranges
(choose_ranges): state 1 = the smallest r of ranges_s1 that the write/read run on r (or, if r was not measured,
on the smallest measured range above r) holds: no RBL overflow in a used cycle and average read 1 <=
range_fill x r (0.9: up to 0.9 uA -> 1 uA, up to 9 uA -> 10 uA); else the largest of ranges_s1. State 0 (RBL
only) = the smallest of ranges_s0 with |average read 0| (of that run) <= range_fill x range, never above the
state-1 range.
pre_rw N (default 0): after a passing screen, N throw-away write/reads (gc_pmu_rw block, one run each on
pre_rw_irange, files GC_PMU_rw_pre<i>_<range>_*) before the measured ones: the first PMU test after the SMU sweeps
writes 5-10x higher than later ones (2026-10-20, R23C15), so they keep every logged write/read and retention in
the same later state. They are saved and logged ('test' gc_pmu_rw_pre) but not used for the ranges.
The SMU sweeps and the PMU share the RPMs: k4200.rpm_cmd must be 'pmu' (K4200.set_rpm switches them to SMU mode
for the sweeps, PmuRunner back to pulse mode).
Files: those of the three tests, then GC_screen_<stamp>_<descriptor>.csv (one row: screen values, pass, reasons,
files), written last, so it marks the cell as done for resume and an interrupted cell is measured again.
"""
import os
import time

import numpy as np
import pandas as pd

from measure import gc_pmu, gc_pmu_ret, gc_sweep
from measure.instruments import k4200_pmu as P

FILE_PREFIX = 'GC_screen_'
DEFAULTS = dict(wbl_vt_V=[0.5, 1.6], wbl_on_off_min=1e4, leak_max_A=1e-9, wwl_vt_V=[0.8, 2.2],
                wwl_iend_s1_min_A=5e-9, wwl_storage_ratio_min=100.0,
                auto_range=False, ranges_s1=[1e-6, 1e-5, 1e-4], ranges_s0=[1e-7, 1e-6, 1e-5], range_fill=0.9,
                pre_rw=0, pre_rw_irange=1e-5)


def params(block):
    unknown = set(block) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"gc_screen: unknown settings {sorted(unknown)}")
    c = {**DEFAULTS, **block}
    for key in ('wbl_vt_V', 'wwl_vt_V'):
        lo, hi = c[key]
        if not lo < hi:
            raise ValueError(f"gc_screen: {key} {c[key]} must be [low, high] with low < high")
    for key in ('ranges_s1', 'ranges_s0'):
        rs = [float(r) for r in c[key]]
        if not rs or rs != sorted(set(rs)) or any(r not in P.RPM_TIMING for r in rs):
            raise ValueError(f"gc_screen: {key} {c[key]} must be increasing fixed RPM ranges {sorted(P.RPM_TIMING)}")
        c[key] = rs
    if not 0 < c['range_fill'] <= 1:
        raise ValueError(f"gc_screen: range_fill {c['range_fill']} must be in (0, 1]")
    if int(c['pre_rw']) != c['pre_rw'] or not 0 <= c['pre_rw'] <= 10:
        raise ValueError(f"gc_screen: pre_rw {c['pre_rw']} must be a whole number 0..10")
    if float(c['pre_rw_irange']) not in P.RPM_TIMING:
        raise ValueError(f"gc_screen: pre_rw_irange {c['pre_rw_irange']} must be a fixed RPM range {sorted(P.RPM_TIMING)}")
    return c


def pre_cfg(cfg, c):
    return {**cfg, 'gc_pmu_rw': {**cfg.get('gc_pmu_rw', {}), 'irange': float(c['pre_rw_irange'])}}


def range_pairs(c):
    """Every (state-1 range, state-0 RBL range) choose_ranges can return."""
    return sorted({(r1, r0) for r1 in c['ranges_s1'] for r0 in c['ranges_s0'] + [r1] if r0 <= r1})


def _num(x):
    return float(x) if x is not None and np.isfinite(float(x)) else np.nan


def choose_ranges(rw, c):
    """(state-1 range, state-0 RBL range, explanation) from the gc_pmu_rw summary, or the list of summaries (one
    per range, with Irange_A); (None, None, why) if read 1 was not measured. A summary without Irange_A judges
    every range."""
    runs = sorted((rw if isinstance(rw, list) else [rw]), key=lambda r: r.get('Irange_A') or np.inf)
    runs = [r for r in runs if np.isfinite(_num(r.get('I_read1_A')))]
    if not runs:
        return None, None, 'write/read gave no read 1'
    f = c['range_fill']
    tag = lambda run: f" on {run['Irange_A']:g} A" if run.get('Irange_A') else ''
    over = lambda run: 'RBL overflow' in (run.get('flags') or '')
    r1 = run = None
    for r in c['ranges_s1']:
        run = next((x for x in runs if (x.get('Irange_A') or np.inf) >= r), None)
        if run is not None and not over(run) and _num(run['I_read1_A']) <= f * r:
            r1 = r
            break
    if r1 is None:
        r1 = c['ranges_s1'][-1]
        run = next((x for x in reversed(runs) if not over(x)), runs[-1])
        why1 = (f"average read 1 {_num(run['I_read1_A']):.3g} A{tag(run)}" if not over(run)
                else f"read 1 over the write/read range{tag(run)}")
    else:
        why1 = f"average read 1 {_num(run['I_read1_A']):.3g} A{tag(run)}"
    i0 = abs(_num(run.get('I_read0_A')))
    i0 = i0 if np.isfinite(i0) else 0.0
    r0 = min(next((r for r in c['ranges_s0'] if i0 <= f * r), c['ranges_s0'][-1]), r1)
    return r1, r0, f"state 1 {r1:g} A ({why1}), state 0 RBL {r0:g} A (|average read 0| {i0:.2g} A)"


def check_config(cfg):
    """Raise on a bad setting in any of the four blocks before anything connects or moves."""
    if cfg.get('k4200', {}).get('rpm_cmd') != 'pmu':
        raise ValueError("gc_screen_ret: k4200.rpm_cmd must be 'pmu' (SMU sweeps and PMU tests share the RPMs)")
    c = params(cfg.get('gc_screen', {}))
    gc_sweep.check_config(cfg)
    gc_sweep.check_wwl_config(cfg)
    gc_pmu_ret.check_config(cfg)
    if c['pre_rw']:
        if 'gc_pmu_rw' not in cfg:
            raise ValueError("gc_screen.pre_rw needs a 'gc_pmu_rw' block (the throw-away write/read)")
        gc_pmu.check_config(pre_cfg(cfg, c))
    if c['auto_range']:
        if 'gc_pmu_rw' not in cfg:
            raise ValueError("gc_screen.auto_range needs a 'gc_pmu_rw' block (the write/read that sets the ranges)")
        gc_pmu.check_config(cfg)
        for r1, r0 in range_pairs(c):           # every range pair the rule can pick must be a valid waveform
            try:
                gc_pmu_ret.check_config(ret_cfg(cfg, r1, r0))
            except ValueError as e:
                raise ValueError(f"gc_screen.auto_range: retention with state 1 {r1:g} A / state 0 RBL {r0:g} A "
                                 f"is not possible: {e}") from e
    return c


def ret_cfg(cfg, r1, r0):
    return {**cfg, 'gc_pmu_ret': {**cfg.get('gc_pmu_ret', {}), 'irange': r1, 'irange_s0': r0}}


def evaluate(wbl, wwl, c):
    """(passed, [reasons]) from the WBL and WWL summary dicts."""
    bad = []
    f = lambda x: float(x) if x is not None else np.nan

    def inside(name, x, lim):
        x = f(x)
        if not np.isfinite(x):
            bad.append(f"{name} not found")
        elif not lim[0] <= x <= lim[1]:
            bad.append(f"{name} {x:.3f} V outside {lim[0]:g}..{lim[1]:g} V")

    def at_least(name, x, lim, unit=''):
        x = f(x)
        if not (np.isfinite(x) and x >= lim):
            bad.append(f"{name} {x:.2g}{unit} < {lim:g}{unit}")

    def at_most(name, x, lim, unit=''):
        x = f(x)
        if not (np.isfinite(x) and x <= lim):
            bad.append(f"{name} {x:.2g}{unit} > {lim:g}{unit}")

    inside('WBL Vt', wbl.get('Vt_V'), c['wbl_vt_V'])
    at_least('WBL on/off', wbl.get('on_off'), c['wbl_on_off_min'])
    at_most('WBL leakage', wbl.get('Ileak_max_A'), c['leak_max_A'], ' A')
    if 'compliance' in (wbl.get('flags') or ''):
        bad.append(f"WBL sweep compliance ({wbl['flags']})")
    inside('WWL Vt', wwl.get('Vt_V'), c['wwl_vt_V'])
    at_least('WWL stored 1', wwl.get('Iend_s1_A'), c['wwl_iend_s1_min_A'], ' A')
    s1, s0 = f(wwl.get('Iend_s1_A')), f(wwl.get('Iend_s0_A'))
    at_least('WWL stored 1/0', s1 / s0 if s0 > 0 else (np.inf if s1 > 0 else np.nan), c['wwl_storage_ratio_min'])
    at_most('WWL leakage', wwl.get('Ileak_max_A'), c['leak_max_A'], ' A')
    if 'compliance' in (wwl.get('flags') or ''):
        bad.append(f"WWL sweep compliance ({wwl['flags']})")
    return not bad, bad


def measure_cell(k, smus, cfg, save_dir, descriptor, save=True, show=False, log=print):
    """WBL sweep, WWL sweep, screen, then the PMU retention if the screen passed. Returns one summary part per
    test ('test' = gc_wbl / gc_wwl / gc_pmu_ret)."""
    c = check_config(cfg)
    kw = dict(save=save, show=show, log=log)
    wbl = gc_sweep.measure_cell(k, smus, cfg, save_dir, descriptor, **kw)
    wwl = gc_sweep.measure_cell_wwl(k, smus, cfg, save_dir, descriptor, **kw)
    passed, reasons = evaluate(wbl, wwl, c)
    log("screen: PASS" if passed else f"screen: FAIL ({'; '.join(reasons)})")
    skip = lambda why: {'status': 'skipped', 'reason': why, 'flags': '', 'main_file': ''}
    r1, r0, why = None, None, ''
    rws, pres = [], []
    for i in range(int(c['pre_rw']) if passed else 0):
        log(f"--- throw-away write/read {i + 1}/{int(c['pre_rw'])} ({c['pre_rw_irange']:g} A, not used for the ranges)")
        tag = f"_pre{i + 1}_{gc_pmu.range_tag(float(c['pre_rw_irange']))}"
        pres.append({'Irange_A': float(c['pre_rw_irange']),
                     **gc_pmu.measure_cell(k, smus, pre_cfg(cfg, c), save_dir, descriptor, tag=tag, **kw)})
    if passed and c['auto_range']:
        out = gc_pmu.measure_cell(k, smus, cfg, save_dir, descriptor, **kw)
        rws = out if isinstance(out, list) else [{'Irange_A': float(gc_pmu.params(cfg['gc_pmu_rw'])['irange']), **out}]
        r1, r0, why = choose_ranges(rws, c)
        log(f"ranges: {why}")
    elif c['auto_range']:
        rws = [skip(f"screen failed: {'; '.join(reasons)}")]
    if not passed:
        ret = skip(f"screen failed: {'; '.join(reasons)}")
    elif c['auto_range'] and r1 is None:
        ret = skip(f"no retention range: {why}")
    else:
        ret = gc_pmu_ret.measure_cell(k, smus, ret_cfg(cfg, r1, r0) if c['auto_range'] else cfg, save_dir,
                                      descriptor, **kw)
    if save:
        os.makedirs(save_dir, exist_ok=True)
        s1, s0 = wwl.get('Iend_s1_A'), wwl.get('Iend_s0_A')
        rw_cols = {}
        for x in rws:                       # rw_<range>_<field>, e.g. rw_1uA_I_read1_A ('rw_' alone if skipped)
            pre = f"rw_{gc_pmu.range_tag(x['Irange_A'])}_" if x.get('Irange_A') else 'rw_'
            for key in ('I_read1_A', 'I_read0_A', 'I_read1_max_A', 'I_read0_absmax_A', 'flags', 'main_file'):
                rw_cols[pre + ('file' if key == 'main_file' else key)] = x.get(key)
        for i, x in enumerate(pres):
            for key in ('I_read1_A', 'I_read0_A', 'flags', 'main_file'):
                rw_cols[f"pre{i + 1}_" + ('file' if key == 'main_file' else key)] = x.get(key)
        row = dict(passed=passed, reasons='; '.join(reasons), wbl_Vt_V=wbl.get('Vt_V'), wbl_on_off=wbl.get('on_off'),
                   wbl_Ileak_max_A=wbl.get('Ileak_max_A'), wbl_flags=wbl.get('flags'), wwl_Vt_V=wwl.get('Vt_V'),
                   wwl_Iend_s1_A=s1, wwl_Iend_s0_A=s0, wwl_Ileak_max_A=wwl.get('Ileak_max_A'),
                   wwl_flags=wwl.get('flags'), wbl_file=wbl.get('main_file'), wwl_file=wwl.get('main_file'),
                   **rw_cols, Irange_s1_A=r1, Irange_s0_A=r0, ranges_why=why,
                   ret_status=ret['status'], ret_file=ret.get('main_file'), **{f'crit_{k_}': str(v) for k_, v in c.items()})
        path = os.path.join(save_dir, f"{FILE_PREFIX}{time.strftime('%Y%m%d_%H%M%S')}_{descriptor}.csv")
        pd.DataFrame([row]).to_csv(path, index=False)
        log(f"Screen saved to: {path}")
    parts = [{'test': 'gc_wbl', **wbl}, {'test': 'gc_wwl', **wwl}]
    parts += [{'test': 'gc_pmu_rw_pre', **x} for x in pres] + [{'test': 'gc_pmu_rw', **x} for x in rws]
    return parts + [{'test': 'gc_pmu_ret', **ret}]
