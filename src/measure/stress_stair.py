"""Measure-stress-measure with staircase reads on the 4200A PMU over KXCI: the scheme of the lab's KULT module
bias_stress.c (study: .ignore/study_0.md), rebuilt with :PMU:SARB (waveform rules: k4200_pmu.py).

Run through measure.run_map with "experiment": "stress_stair" (settings in the 'stress_stair' block). One
Segment Arb waveform per device, all three channels on the same segment times (gate PMU1-1, drain PMU1-2,
source PMU2-1):

    stdby | stair 0 (reference) | for each cumulative stress time t_k: stdby | stress to t_k | stdby | stair k
          | stdby | recovery: for each recovery time r_j: 0 V hold | stair (recovery j) | stdby | end rest

- stair: unidirectional gate staircase stair_start_v -> stair_stop_v in stair_step_v (default -1 -> 2 V in
  0.1 V, 31 levels). As in bias_stress.c the drain ramps first: drain 0 -> vd in edge_s with the gate at
  0 V, then the gate 0 -> stair_start_v in edge_s. Each level is a plateau_s plateau read as a spot mean
  from meas_start_s to meas_stop_s into it, the steps between levels take edge_s. At the end the gate
  returns to 0 V in edge_s, then the drain vd -> 0 in edge_s.
- stress (stress_mode 'dc'): the gate ramps 0 -> vstress in edge_s, stays flat at vstress for t_k - t_(k-1),
  and ramps back in edge_s. Drain and source stay at 0 V. The flat time is the counted stress time, so the
  cumulative stress before stair k is exactly t_k. The flat part is a ramp-up sequence (0.5 ms flat), looped
  1 ms units and a ramp-down sequence (0.5 ms flat), the structure of every long waveform that ran live; the
  first version, with plain segments of up to 1 s and 10.0 s in total, was refused at EXECUTE with -951
  "Cannot adjust PMU measure sample rate for SARB configuration" (2026-10-16 19:46).
- stress (stress_mode 'ac'): the same increments as gate pulses of ac_period_s (default 2 ms), flat at
  vstress for ac_duty of it (high, counted as stress) and with every channel at 0 V for the rest (low =
  period - high - 2 edges, the recovery condition). Phase k has (t_k - t_(k-1)) / high pulses, so the
  cumulative time at vstress before stair k is exactly t_k, as in DC (1, 4, 5, 40, 50 ... pulses for 1, 5,
  10, 50, 100 ms ... at 2 ms / 50 %). The phase ends on the falling edge of its last pulse (no trailing
  low), so the stdby and the read follow the stress exactly as in DC. One looped pulse sequence plus a last
  pulse per phase.
- stdby: every channel at 0 V for stdby_s, between every read and stress and at both ends.
- recovery: after the last stress read every channel stays at 0 V (the stdby condition) except during the
  recovery staircases. Recovery staircase j starts (drain ramp) exactly recovery_times_s[j] after the end of
  the last flat vstress segment, the reference that t_off_s in the timeline uses; the 0 V hold before it
  fills the gap (looped 1 ms units plus a remainder), after the stdby that follows every staircase.
  In AC mode the reference is the end of the last high of the last pulse. recovery_times_s = [] gives the
  stress-only waveform.
- end rest: 0 V after the last read, up to min_duration_s in total (11.5 s), so KXCI's duration-based rate
  cut lands where it was seen to be skipped (k4200_pmu.KXCI_UNTESTED_SPW). With recovery to 10 s the
  waveform is already about 21.5 s long and no end rest is added.
Unlike bias_stress.c, this module has a single-direction sweep, 0 V standby phases, an AC mode with 0 V lows
(bias_stress.c pulses between its own levels), and returns the data (every spot mean, all three channels)
instead of posting it to Clarius.

Per device: Vt of every staircase at a constant current (vt_id_per_wl * W/L, at least vt_id_min_a; default
1e-8 A x W/L, at least 20 nA, about 2x the largest off-state source reading), from
-I(source) and from I(drain); dVt = Vt(stair k) - Vt(stair 0) for the stress reads and the recovery reads;
and the time since the stress ended at which each staircase crossed Vt.
Files: PMU_stair_*.csv (one row per spot mean; first written as the plan before the stress starts, so
resume never stresses a device twice), PMU_stair_vt_*.csv, PMU_stair_meta_*.json (settings, budgets,
every command), PMU_stair_raw_*.txt (raw KXCI replies), PMU_stair_*.png.

Without hardware:  python -m measure.stress_stair --config configs/bobafet_6916_stress_stair_k4200.json
                   (prints the budgets; --plot-waveform PATH draws the programmed waveform)
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd

from measure.instruments import k4200_pmu as P

DEFAULTS = dict(
    # hardware: fixed ranges (Segment Arb has no autorange); 10 uA on every channel
    v_range=10, i_range=1e-5, load_ohm=1e6, offset_comp=False, min_samples_per_window=4,
    # read staircase (10 uA RPM range: >= 8.36 us plateau, >= 6 us settling, >= 360 ns edges [DS p.24])
    stair_start_v=-1.0, stair_stop_v=2.0, stair_step_v=0.1, vd=0.1,
    edge_s=1e-6, plateau_s=20e-6, meas_start_s=10e-6, meas_stop_s=19e-6,
    # stress on the gate, drain = source = 0 V; stress_times_s are cumulative times at vstress
    vstress=3.0, stress_times_s=[1e-3, 5e-3, 1e-2, 5e-2, 1e-1, 5e-1, 1.0, 5.0, 10.0],
    # 'dc': flat at vstress; 'ac': pulses of ac_period_s, at vstress for ac_duty of it, everything at 0 V between
    stress_mode='dc', ac_period_s=2e-3, ac_duty=0.5,
    stdby_s=10e-6,
    # recovery after the last stress read, everything at 0 V: start of each recovery staircase, measured from the
    # end of the last flat vstress segment (s)
    recovery_times_s=[1e-3, 5e-3, 1e-2, 5e-2, 1e-1, 5e-1, 1.0, 5.0, 10.0],
    # 0 V rest after the last read up to this total: KXCI refused the 10.0 s version (-951; its duration rate cut
    # would leave 0.90 samples per 9 us window), 11.3 s (0.80) ran live (k4200_pmu.KXCI_UNTESTED_SPW)
    min_duration_s=11.5,
    # analysis
    # Vt current: vt_id_per_wl * W/L, at least vt_id_min_a. Off-state noise in the 19:57 run (Vg <= 0, 10 uA
    # range): -I(source) offset 4.9 nA, std 2.7 nA, max 11 nA; I(drain) 1.6 nA, std 1.4 nA, max 4.8 nA
    id_from='source', vt_id_per_wl=1e-8, vt_id_min_a=2e-8,
    poll_s=0.5)
VT_FIELDS = ['phase', 'point', 't_stress_s', 't_rec_s', 'Vt_source_V', 'dVt_source_V', 't_off_at_vt_source_s',
             'Vt_drain_V', 'dVt_drain_V', 't_off_at_vt_drain_s', 'Id_max_A', 'overflow_points']


def params(block):
    unknown = set(block) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown stress_stair settings: {sorted(unknown)}")
    p = {**DEFAULTS, **block}
    st = [float(t) for t in p['stress_times_s']]
    if not st or st[0] <= 0 or any(b <= a for a, b in zip(st, st[1:])):
        raise ValueError("stress_times_s must be cumulative times > 0, strictly increasing")
    if p['id_from'] not in ('source', 'drain'):
        raise ValueError("id_from must be 'source' or 'drain'")
    if not p['vstress'] > p['stair_stop_v']:
        raise ValueError("vstress must be above stair_stop_v (the reads must not reach the stress level)")
    if not 0 <= p['meas_start_s'] < p['meas_stop_s'] <= p['plateau_s']:
        raise ValueError("need 0 <= meas_start_s < meas_stop_s <= plateau_s")
    rt = [float(t) for t in p['recovery_times_s']]
    if rt and (rt[0] <= 0 or any(b <= a for a, b in zip(rt, rt[1:]))):
        raise ValueError("recovery_times_s must be times > 0 after the last stress, strictly increasing")
    p['stress_times_s'], p['recovery_times_s'] = st, rt
    if p['stress_mode'] not in ('dc', 'ac'):
        raise ValueError("stress_mode must be 'dc' or 'ac'")
    if p['stress_mode'] == 'ac':
        if not 0 < p['ac_duty'] < 1:
            raise ValueError("ac_duty must be between 0 and 1 (exclusive)")
        hi, lo = ac_ticks(p)
        if lo < 2:
            raise ValueError(f"ac_period_s {p['ac_period_s']:g} s leaves no time at 0 V: the low part "
                             f"(period - high - 2 edges) must be >= 20 ns")
        if max(hi, lo) > P.ticks(1.0):
            raise ValueError("AC high and low parts must each be <= 1 s (KXCI segment limit)")
        prev = 0
        for t in st:
            n = P.ticks(t, f'stress time {t:g}') - prev
            prev += n
            if n % hi:
                raise ValueError(f"stress increment {n * P.TICK:g} s (to {t:g} s) is not a whole number of "
                                 f"{hi * P.TICK:g} s AC high times")
    return p


def ac_ticks(p):
    """(high, low) of one AC period in ticks: high = flat at vstress (counted as stress), low = flat at 0 V;
    high + low + 2 edges = ac_period_s."""
    hi = P.ticks(p['ac_duty'] * p['ac_period_s'], 'AC high time (ac_duty x ac_period_s)')
    return hi, P.ticks(p['ac_period_s'], 'ac_period_s') - hi - 2 * P.ticks(p['edge_s'], 'edge_s')


def stress_label(p):
    """'DC 3 V' or 'AC 3 V, 2 ms period, 50% duty'."""
    if p['stress_mode'] == 'dc':
        return f"DC {p['vstress']:g} V"
    t = p['ac_period_s']
    period = f"{t * 1e3:g} ms" if t >= 1e-3 else f"{t * 1e6:g} us"
    return f"AC {p['vstress']:g} V, {period} period, {p['ac_duty'] * 100:g}% duty"


# --- helpers (were in the archived measure/bias_stress.py) ---
def config_channels(cfg):
    """(gate, drain, source) SMU numbers from smu_map, and the KXCI pulse channels of their RPMs."""
    from measure.instruments.k4200 import get_smus

    class _Map:      # get_smus only stores the output order on its argument
        channels = None
    smus = get_smus(_Map(), cfg['smu_map'])
    return smus, tuple(P.pmu_channel(s) for s in smus)


def vt_const_current(vg, i, target):
    """Vg where |I| first reaches target going up in Vg and stays there for the next two points
    (log-linear interpolation between the bracketing points); NaN if it never crosses."""
    o = np.argsort(np.asarray(vg, float))
    vg, i = np.asarray(vg, float)[o], np.abs(np.asarray(i, float))[o]
    above = np.isfinite(i) & (i >= target)
    for k in range(1, len(vg)):
        if above[k] and not above[k - 1] and above[k:k + 3].all():
            if not i[k - 1] > 0:
                return float(vg[k])
            f = (math.log(target) - math.log(i[k - 1])) / (math.log(i[k]) - math.log(i[k - 1]))
            return float(vg[k - 1] + f * (vg[k] - vg[k - 1]))
    return float('nan')


def vt_target(p, W, L):
    """Constant-current Vt criterion: vt_id_per_wl * W/L, but not below vt_id_min_a (the 10 uA PMU range
    has ~5 nA RMS noise and up to +/-30 nA offset [DS p.24]; measured off-state max 11 nA on the source)."""
    return max(p['vt_id_per_wl'] * W / L, p['vt_id_min_a'])


# --- waveform ---
def _add(seqs, entries, seq, loops=1, **info):
    """Append a list entry, reusing an already defined sequence of the same name."""
    sid = next((s for s, q in seqs.items() if q.name == seq.name), None)
    if sid is None:
        sid = len(seqs) + 1
        seqs[sid] = seq
    entries.append((sid, int(loops), info))
    return sid


UNIT = 100_000              # 1 ms in ticks: long flat stretches are looped 1 ms units, as in every long
                            # waveform that ran live (a 10 s waveform of <= 1 s segments got -951, 2026-10-16)


def _add_stress(seqs, entries, p, n, **info):
    """0 V -> vstress (drain, source 0 V) in edge_s, flat for exactly n ticks, -> 0 V in edge_s.
    Flat time: 0.5 ms in the ramp-up sequence + (n / 1 ms - 1) looped 1 ms units + 0.5 ms in the ramp-down."""
    if n % UNIT or n < UNIT:
        raise P.SarbError(f"stress increment {n * P.TICK:g} s is not a whole number of ms (>= 1 ms)")
    edge, z, s = P.ticks(p['edge_s'], 'edge_s'), (0.0, 0.0, 0.0), (P._v(p['vstress']), 0.0, 0.0)
    q = UNIT // 4
    _add(seqs, entries, P.Seq('stress_up', 'ramp', [P.Seg(edge, z, s), P.Seg(q, s, s), P.Seg(q, s, s)]),
         phase='stress', **info)
    if n > UNIT:
        _add(seqs, entries, P.Seq('stress_unit', 'unit', P.hold3(p['vstress'], UNIT)), n // UNIT - 1,
             phase='stress', **info)
    _add(seqs, entries, P.Seq('stress_down', 'ramp', [P.Seg(q, s, s), P.Seg(q, s, s), P.Seg(edge, s, z)]),
         phase='stress', **info)


def _add_ac_stress(seqs, entries, p, n, **info):
    """n ticks of stress as n / high AC pulses (drain, source 0 V): (pulses - 1) looped periods of
    [0 -> vstress in edge_s, high flat, -> 0 V in edge_s, low flat at 0 V], then a last pulse without its low,
    so the stdby and the read follow the last falling edge exactly as after a DC stress."""
    hi, lo = ac_ticks(p)
    if n % hi or n < hi:
        raise P.SarbError(f"stress increment {n * P.TICK:g} s is not a whole number of AC high times")
    edge, z, s = P.ticks(p['edge_s'], 'edge_s'), (0.0, 0.0, 0.0), (P._v(p['vstress']), 0.0, 0.0)
    pulse = [P.Seg(edge, z, s), P.Seg(hi, s, s), P.Seg(edge, s, z)]
    if n > hi:
        _add(seqs, entries, P.Seq('ac_pulse', 'unit', pulse + [P.Seg(lo, z, z)]), n // hi - 1,
             phase='stress', **info)
    _add(seqs, entries, P.Seq('ac_last', 'ramp', pulse), phase='stress', **info)


def _add_zero(seqs, entries, n, **info):
    """n ticks at 0 V on every channel as looped 1 ms units plus one remainder sequence (a sequence of three
    segments needs >= 8 ticks: a remainder below that is merged with one unit). Returns the ticks added."""
    k, r = divmod(n, UNIT)
    if 0 < r < 8:
        if k:
            k, r = k - 1, r + UNIT
        else:
            r = 8                      # only for a hold under 80 ns
    if k:
        _add(seqs, entries, P.Seq('zero_unit', 'unit', P.hold3(0.0, UNIT)), k, **info)
    if r:
        _add(seqs, entries, P.Seq(f'zero_{r}t', 'hold', P.hold3(0.0, r)), **info)
    return k * UNIT + r


def stair_levels(p):
    return P.levels(p['stair_start_v'], p['stair_stop_v'], p['stair_step_v'])


def stair_segs(p, lv):
    """Read sequence from and back to 0 V on every channel: drain 0 -> vd (gate at 0 V), gate 0 -> first level,
    one measured plateau per level with edge_s steps between, gate last level -> 0 V, drain vd -> 0."""
    edge, plat = P.ticks(p['edge_s'], 'edge_s'), P.ticks(p['plateau_s'], 'plateau_s')
    vd, z = P._v(p['vd']), (0.0, 0.0, 0.0)
    on = (0.0, vd, 0.0)
    pts = [(P._v(g), vd, 0.0) for g in lv]
    segs, prev = [P.Seg(edge, z, on)], on
    for i, a in enumerate(pts):
        segs.append(P.Seg(edge, prev, a))
        segs.append(P.Seg(plat, a, a, meas=True, level=i))
        prev = a
    return segs + [P.Seg(edge, prev, on), P.Seg(edge, on, z)]


def build_waveform(p, chans):
    lv = stair_levels(p)
    stdby = P.Seq('stdby', 'hold', P.hold3(0.0, P.ticks(p['stdby_s'], 'stdby_s')))
    stair = P.Seq('stair', 'sweep', stair_segs(p, lv))
    seqs, entries = {}, []
    _add(seqs, entries, stdby, phase='stdby')
    _add(seqs, entries, stair, phase='read', point=0, read='sweep', target_s=0.0)
    prev = 0
    for k, t in enumerate(p['stress_times_s'], 1):
        n = P.ticks(t, f'stress time {t:g}') - prev
        prev += n
        _add(seqs, entries, stdby, phase='stdby')
        (_add_ac_stress if p['stress_mode'] == 'ac' else _add_stress)(seqs, entries, p, n, point=k, target_s=t)
        _add(seqs, entries, stdby, phase='stdby')
        _add(seqs, entries, stair, phase='read', point=k, read='sweep', target_s=t)
    _add(seqs, entries, stdby, phase='stdby')
    w = P.Waveform('stress_stair', seqs, entries, tuple(chans), P.ticks(p['meas_start_s']),
                   P.ticks(p['meas_stop_s']), p['vstress'], dict(levels=lv, stress_mode=p['stress_mode']))
    now = lambda: sum(seqs[sid].n * loops for sid, loops, _ in entries)
    # end of the last flat vstress segment = end of the last stress entry (stress_down / ac_last) minus its edge
    last = max(i for i, (_, _, info) in enumerate(entries) if info['phase'] == 'stress')
    stress_end = sum(seqs[sid].n * loops for sid, loops, _ in entries[:last + 1]) - P.ticks(p['edge_s'])
    for j, tr in enumerate(p['recovery_times_s'], 1):
        hold = P.ticks(tr, f'recovery time {tr:g}') - (now() - stress_end)
        if hold < 0:
            raise P.SarbError(f"recovery time {tr:g} s is earlier than the end of the previous read "
                              f"({(now() - stress_end) * P.TICK:g} s after the stress)")
        if hold:
            _add_zero(seqs, entries, hold, phase='rec_hold', point=j)
        _add(seqs, entries, stair, phase='recovery', point=j, read='sweep', target_s=tr)
        _add(seqs, entries, stdby, phase='stdby')
    short = P.ticks(p['min_duration_s'], 'min_duration_s') - now()
    if short > 0:                 # 0 V rest after the last read, out of KXCI's untested rate zone
        _add_zero(seqs, entries, short, phase='end_rest')
    return P.renumber(w)


def _settings(p, wf):
    """Commanded rate: the highest 200 MS/s / n under 1e6 samples per A/D over all windows [PMU p.4-10];
    KXCI then lowers it further by its own rules (P.kxci_rate)."""
    return dict(irange=p['i_range'], vrange=p['v_range'], load_ohm=p['load_ohm'], rate=P.choose_rate(wf),
                offset_comp=p['offset_comp'])


def check_config(cfg):
    """Before any connection or move: settings known, rpm_cmd 'pmu', the waveform within every limit."""
    p = params(cfg.get('stress_stair', {}))
    if cfg.get('k4200', {}).get('rpm_cmd') != 'pmu':
        raise ValueError("stress_stair needs \"k4200\": {\"rpm_cmd\": \"pmu\"} (RP cannot be mixed with :PMU:)")
    _, chans = config_channels(cfg)
    wf = build_waveform(p, chans)
    s = _settings(p, wf)
    return p, wf, P.validate(wf, s['irange'], s['vrange'], s['rate'], p['min_samples_per_window'])


# --- analysis ---
READS = ('read', 'recovery')


def vt_table(d, p, target):
    """One row per staircase in run order (stress reads, then recovery reads). dVt is against the reference:
    staircase 0, the pulsed sweep of this same waveform right before the first stress (no SMU Vt is used), so
    its own dVt is exactly 0 at t_stress = 0."""
    rows = []
    for (ph, k), g in d[d.phase.isin(READS)].groupby(['phase', 'point'], sort=False):
        g = g.sort_values('Vg_set')
        r = dict(phase=ph, point=int(k), t_stress_s=float(g['t_stress_s'].iloc[0]),
                 t_rec_s=float(g['target_s'].iloc[0]) if ph == 'recovery' else np.nan,
                 Id_max_A=float(np.nanmax(np.abs(g['Id']))),
                 overflow_points=int((g['Drain_Overflow'] | g['Source_Overflow']).sum()))
        for name, i in (('source', -g['Source_Current']), ('drain', g['Drain_Current'])):
            vt = vt_const_current(g['Vg_set'], i, target)
            r[f'Vt_{name}_V'] = vt
            r[f't_off_at_vt_{name}_s'] = float(np.interp(vt, g['Vg_set'], g['t_off_s'])) \
                if np.isfinite(vt) and g['t_off_s'].notna().all() else np.nan
        rows.append(r)
    t = pd.DataFrame(rows)
    ref = t[(t.phase == 'read') & (t.point == 0)]
    assert len(ref) == 1, "no reference staircase (read 0) in the data"
    for name in ('source', 'drain'):
        t[f'dVt_{name}_V'] = t[f'Vt_{name}_V'] - float(ref[f'Vt_{name}_V'].iloc[0])
    return t[VT_FIELDS]


def ac_pulses(p):
    """Number of AC pulses in each stress phase."""
    hi, prev, out = ac_ticks(p)[0], 0, []
    for t in p['stress_times_s']:
        n = P.ticks(t) - prev
        prev += n
        out.append(n // hi)
    return out


def stress_wall_s(wf):
    """Total wall time of the stress entries (s)."""
    return sum(t1 - t0 for (t0, t1), (_, _, info) in zip(P.entry_times(wf), wf.entries)
               if info.get('phase') == 'stress')


def entry_span(wf, phase, point):
    """(start_s, end_s) of the entries with this phase and point."""
    sel = [t for t, (_, _, info) in zip(P.entry_times(wf), wf.entries)
           if info.get('phase') == phase and info.get('point') == point]
    return sel[0][0], sel[-1][1]


def plot(d, vt, p, path, title, target):
    """Id-Vg of the stress and the recovery staircases, and dVt vs time for stress and recovery in one plot:
    a shared dVt axis, cumulative stress time on the left (0 = the reference staircase), time after the end of
    the stress on the right (at the Vt crossing). The two axes meet at the last stress read."""
    import matplotlib.pyplot as plt
    from matplotlib.ticker import LogFormatterSciNotation
    fig = plt.figure(figsize=(20, 5))
    gs = fig.add_gridspec(1, 3, width_ratios=[1, 1, 1.6])
    labels = {'read': lambda g, k: 'before stress' if k == 0 else f"after {g['t_stress_s'].iloc[0]:g} s stress",
              'recovery': lambda g, k: f"{g['target_s'].iloc[0]:g} s recovery"}
    for i, (ph, cmap) in enumerate((('read', 'viridis'), ('recovery', 'plasma'))):
        ax = fig.add_subplot(gs[i])
        sub = d[d.phase == ph]
        npt = max(1, sub['point'].max()) if len(sub) else 1
        for k, g in sub.groupby('point'):
            ax.semilogy(g['Vg_set'], np.abs(g['Id']), '.-', ms=3, lw=0.9, color=plt.get_cmap(cmap)(0.9 * k / npt),
                        label=labels[ph](g, k))
        if ph == 'recovery':
            g0 = d[(d.phase == 'read') & (d.point == 0)]
            ax.semilogy(g0['Vg_set'], np.abs(g0['Id']), 'k--', lw=0.9, label='before stress')
        ax.axhline(target, color='gray', ls=':', lw=1, label=f'Vt current {target:.3g} A')
        ax.set_xlabel('Vg (V)'); ax.set_ylabel(f"|Id| ({p['id_from']} channel) (A)"); ax.set_ylim(1e-10, 2e-5)
        ax.set_title(f"{'Stress' if ph == 'read' else 'Recovery (0 V)'} staircases, Vd {p['vd']:g} V, "
                     f"{p['plateau_s'] * 1e6:g} us per step", fontsize=9)
        ax.grid(True, alpha=0.3, which='both'); ax.legend(fontsize=6, ncol=2)
    sg = gs[2].subgridspec(1, 2, wspace=0)
    axs, axr = fig.add_subplot(sg[0]), None
    axr = fig.add_subplot(sg[1], sharey=axs)
    chans = (('source', 'C0', 'o', '-I(source)'), ('drain', 'C1', 's', 'I(drain)'))
    st = vt[vt.phase == 'read']
    rc = vt[vt.phase == 'recovery']
    last = st.iloc[-1]
    # the junction is the last stress read: the stress axis ends at its stress time and the recovery axis starts
    # at its t_off, so the stress curve runs straight on into recovery
    t_join = np.nanmin([last[f't_off_at_vt_{name}_s'] for name, *_ in chans])
    t_join = t_join if np.isfinite(t_join) else 1.5e-4
    for name, c, mk, lab in chans:
        axs.plot(st['t_stress_s'], st[f'dVt_{name}_V'] * 1e3, mk + '-', color=c, ms=4, label=lab, clip_on=False)
        axr.plot([last[f't_off_at_vt_{name}_s']] + list(rc[f't_off_at_vt_{name}_s']),
                 [last[f'dVt_{name}_V'] * 1e3] + list(rc[f'dVt_{name}_V'] * 1e3), '-', color=c, lw=1.2)
        axr.plot(rc[f't_off_at_vt_{name}_s'], rc[f'dVt_{name}_V'] * 1e3, mk, color=c, ms=4)
    axs.set_xscale('symlog', linthresh=min(p['stress_times_s']))
    axr.set_xscale('log')
    axs.set_xlim(right=max(p['stress_times_s']))
    axr.set_xlim(t_join, 3 * max(list(p['recovery_times_s']) + list(rc['t_off_at_vt_source_s'].dropna()) + [10.0]))

    class _JoinFmt(LogFormatterSciNotation):                     # no recovery label right at the junction
        def __call__(self, x, pos=None):
            return '' if x < t_join * 10 ** 0.7 else super().__call__(x, pos)
    axr.xaxis.set_major_formatter(_JoinFmt())
    for ax in (axs, axr):
        ax.axhline(0, color='gray', lw=0.8)
        ax.grid(True, alpha=0.3, which='both')
    axr.tick_params(labelleft=False)
    axr.spines['left'].set_color('k'); axr.spines['left'].set_linewidth(1.5)
    if not len(rc):
        axr.text(0.5, 0.5, 'no recovery reads\nin this run', transform=axr.transAxes, ha='center', va='center',
                 fontsize=9, color='gray')
    vt0 = float(st[f"Vt_{p['id_from']}_V"].iloc[0])
    axs.set_xlabel(f"{p['stress_mode'].upper()} stress: cumulative time at {p['vstress']:g} V (s; 0 = ref.)")
    axr.set_xlabel('recovery at 0 V: time after the stress at the Vt crossing (s)')
    axs.set_ylabel('dVt (mV)')
    axs.set_title(f"dVt vs time: stress | recovery   (ref: pre-stress staircase, Vt0 {vt0:.3f} V at "
                  f"{target:.2g} A, {p['id_from']})", fontsize=9, loc='left')
    axs.legend(fontsize=7)
    fig.suptitle(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def _overview(ax, wf):
    """Whole waveform: looped flat stretches as one line, looped pulse trains as a band between their lowest
    and highest gate level (too many edges to draw)."""
    groups, cur, band = [], None, False
    for (t0, t1), (sid, loops, _) in zip(P.entry_times(wf), wf.entries):
        seq = wf.seqs[sid]
        if loops > 1 and not all(s.flat for s in seq.segs):
            g = [v for s in seq.segs for v in (s.v0[0], s.v1[0])]
            ax.fill_between([t0, t1], min(g), max(g), color='C0', alpha=0.35, lw=0,
                            label=None if band else 'gate pulse train')
            cur, band = None, True
        elif cur is None:
            cur = [t0, t1]
            groups.append(cur)
        else:
            cur[1] = t1
    for i, (t0, t1) in enumerate(groups):
        T, G, D, _ = P.trace(wf, t0, t1, collapse_loops=True)
        ax.plot(T, G, color='C0', lw=1, label=None if i else 'gate')
        ax.plot(T, D, color='C1', lw=1, label=None if i else 'drain')


def plot_waveform(wf, path, title=''):
    """Programmed gate and drain: the whole waveform, one stress -> read transition, one staircase."""
    import matplotlib.pyplot as plt
    r0 = entry_span(wf, 'read', 0)[0]
    windows = [(0, wf.duration_s, 'whole waveform'),
               (entry_span(wf, 'stress', 1)[1] - 30e-6, entry_span(wf, 'read', 1)[1] + 15e-6,
                'end of stress 1 -> stdby -> staircase 1 -> stdby'),
               (r0 - 5e-6, r0 + 120e-6, 'start of staircase 0')]
    if wf.info.get('stress_mode') == 'ac':
        s0, s1 = entry_span(wf, 'stress', 1)
        period = max(wf.seqs[sid].n for sid, _, info in wf.entries if info.get('phase') == 'stress') * P.TICK
        windows.insert(1, (max(s0, s1 - 3.5 * period) - 5e-6, s1 + 20e-6, 'last AC pulses of stress phase 1'))
    if any(info.get('phase') == 'recovery' for _, _, info in wf.entries):
        k = max(info['point'] for _, _, info in wf.entries if info.get('phase') == 'read')
        windows.append((entry_span(wf, 'read', k)[0] - 30e-6, entry_span(wf, 'recovery', 1)[1] + 15e-6,
                        'last stress read -> 0 V -> recovery staircase 1'))
    fig, axs = plt.subplots(1, len(windows), figsize=(5.7 * len(windows), 4.5))
    for ax, (t0, t1, name) in zip(axs, windows):
        if name == 'whole waveform':
            _overview(ax, wf)
        else:
            T, G, D, _ = P.trace(wf, t0, t1)
            x = (T - t0) * 1e6
            ax.plot(x, G, lw=1, label='gate'); ax.plot(x, D, lw=1, label='drain')
            ax.set_xlim(0, (t1 - t0) * 1e6)
        ax.set_xlabel('time (s)' if name == 'whole waveform' else f'time (us) from {t0:.6f} s')
        ax.set_ylabel('V (V)'); ax.set_title(name, fontsize=9); ax.grid(True, alpha=0.3); ax.legend(fontsize=7)
    fig.suptitle(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


# --- per device ---
def measure_device(k, smus, cfg, W, L, save_dir, descriptor, save=True, show=False, log=print):
    p = params(cfg.get('stress_stair', {}))
    chans = tuple(P.pmu_channel(s) for s in smus)
    os.makedirs(save_dir, exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    f = lambda name, ext='csv': os.path.join(save_dir, f"{name}_{stamp}_{descriptor}.{ext}")
    paths = dict(data=f('PMU_stair'), vt=f('PMU_stair_vt'), meta=f('PMU_stair_meta', 'json'),
                 raw=f('PMU_stair_raw', 'txt'), plot=f('PMU_stair', 'png'))
    target = vt_target(p, W, L)
    wf = build_waveform(p, chans)
    s = _settings(p, wf)
    b = P.validate(wf, s['irange'], s['vrange'], s['rate'], p['min_samples_per_window'])
    tl = P.timeline(wf)
    meta = dict(descriptor=descriptor, W_um=W, L_um=L, smus=smus, pmu_channels=chans, params=p, idn=k.idn,
                opt=k.opt, started=time.strftime('%Y-%m-%d %H:%M:%S'), settings=s, budgets=b,
                vt_current_A=target, commands=P.commands(wf, **s))
    res = dict(status='failed', reason='', Vt_V=np.nan, flags='', main_file=paths['data'], dvt_file=None,
               t_stress_s=p['stress_times_s'][-1])

    def save_meta():
        with open(paths['meta'], 'w', encoding='utf-8') as fh:
            json.dump(meta, fh, indent=1, default=lambda x: x.item() if hasattr(x, 'item') else str(x))

    def merged(data):
        out = P.merge(tl, data, chans, p['i_range'])
        out['Id'] = -out['Source_Current'] if p['id_from'] == 'source' else out['Drain_Current']
        return out

    def save_partial(data):
        if any(data.get(ch) for ch in chans):
            merged(data).to_csv(paths['data'], index=False)

    save_meta()
    tl.to_csv(paths['data'], index=False)       # the plan, before any stress: resume skips this device
    st = p['stress_times_s']
    rt = p['recovery_times_s']
    n_st = len(st) + 1 + len(rt)
    log(f"Stress-staircase waveform: {len(tl)} spot means ({len(tl) // n_st} per staircase x {n_st}), "
        f"{b['segments']} segments, {b['entries']} list entries, {b['duration_s']:.4g} s | "
        f"{stress_label(p)} stress to {', '.join(f'{t:g}' for t in st)} s cumulative at {p['vstress']:g} V"
        + (f" ({', '.join(str(n) for n in ac_pulses(p))} pulses; {stress_wall_s(wf):.4g} s of pulsing)"
           if p['stress_mode'] == 'ac' else '')
        + " | recovery reads at 0 V after "
        f"{', '.join(f'{t:g}' for t in rt) or 'none'} s | KXCI will use about "
        f"{b['kxci_rate']:.4g} S/s, {b['kxci_samples_per_window']:.3g} samples per window")
    runner = P.PmuRunner(k, smus, log=log, poll_s=p['poll_s'], fetch_every_s=1e9, timeout_margin_s=120.0,
                         run_timeout_ms=30000)
    d = merged(runner.run(wf, raw_log=paths['raw'], on_progress=save_partial, **s))
    d.to_csv(paths['data'], index=False)
    vt = vt_table(d, p, target)
    vt.to_csv(paths['vt'], index=False)
    col = f"{p['id_from']}"
    log(f"dVt reference: staircase 0, the pulsed sweep right before the first ({st[0]:g} s) stress, Vt0 "
        f"{vt[f'Vt_{col}_V'].iloc[0]:.4f} V at {target:.3g} A ({col}); no SMU Vt is used")
    for r in vt.itertuples():
        t_vt = getattr(r, f't_off_at_vt_{col}_s')
        what = ("Staircase 0 (reference, before any stress)" if r.phase == 'read' and r.point == 0 else
                f"Staircase {r.point} after {r.t_stress_s:g} s stress" if r.phase == 'read'
                else f"Recovery {r.point} at {r.t_rec_s:g} s")
        log(f"{what}: Vt {getattr(r, f'Vt_{col}_V'):.4f} V, "
            f"dVt {getattr(r, f'dVt_{col}_V') * 1e3:+.1f} mV ({col}; drain {r.dVt_drain_V * 1e3:+.1f} mV), "
            + (f"Vt crossed {t_vt * 1e6:.0f} us after the stress, " if np.isfinite(t_vt) else '')
            + f"max |Id| {r.Id_max_A:.3g} A" + (f", {r.overflow_points} overflow points" if r.overflow_points else ''))
    flags = []
    if not np.isfinite(vt[f'Vt_{col}_V'].iloc[0]):
        flags.append('no Vt in the reference staircase')
    if vt['overflow_points'].sum():
        flags.append(f"{int(vt['overflow_points'].sum())} points at or over the {p['i_range']:g} A range")
    try:
        plot(d, vt, p, paths['plot'], f"{stress_label(p)} stress / staircase reads / 0 V recovery  "
             f"[{descriptor}]", target)
    except Exception as e:
        log(f"plot failed: {type(e).__name__}: {e}")
    rec = vt[vt.phase == 'recovery']
    res.update(status='measured', Vt_V=float(vt[f'Vt_{col}_V'].iloc[0]), dvt_file=paths['vt'],
               dVt_stress_end_V=float(vt.loc[vt.phase == 'read', f'dVt_{col}_V'].iloc[-1]),
               dVt_recovery_end_V=float(rec[f'dVt_{col}_V'].iloc[-1]) if len(rec) else np.nan,
               flags='; '.join(flags))
    meta['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
    meta['result'] = res
    meta['vt'] = vt.to_dict('records')
    save_meta()
    return res


def reanalyse(data_csv, vt_id_per_wl=None, vt_id_min_a=None, log=print):
    """Vt / dVt and the plot again from a saved PMU_stair_*.csv (no instrument), with the Vt current of the
    current DEFAULTS unless given. Settings, W and L come from the run's PMU_stair_meta_*.json. Writes
    PMU_stair_vt_*_reanalysis_Iref<A>.csv and PMU_stair_*_reanalysis_Iref<A>.png next to the data.
    Returns (vt table, {'vt': path, 'plot': path})."""
    folder, name = os.path.split(data_csv)
    if not (name.startswith('PMU_stair_') and name.endswith('.csv')) or name.startswith(('PMU_stair_vt_',
                                                                                         'PMU_stair_meta_')):
        raise ValueError(f"{data_csv} is not a PMU_stair_<stamp>_<descriptor>.csv data file")
    with open(os.path.join(folder, 'PMU_stair_meta_' + name[len('PMU_stair_'):-4] + '.json'), encoding='utf-8') as fh:
        meta = json.load(fh)
    p = params({k: v for k, v in meta['params'].items() if k in DEFAULTS and k != 'recovery_times_s'})
    p['recovery_times_s'] = meta['params'].get('recovery_times_s', [])
    p.update(vt_id_per_wl=DEFAULTS['vt_id_per_wl'] if vt_id_per_wl is None else vt_id_per_wl,
             vt_id_min_a=DEFAULTS['vt_id_min_a'] if vt_id_min_a is None else vt_id_min_a)
    target = vt_target(p, meta['W_um'], meta['L_um'])
    d = pd.read_csv(data_csv)
    d['Id'] = -d['Source_Current'] if p['id_from'] == 'source' else d['Drain_Current']
    vt = vt_table(d, p, target)
    tag = f"_reanalysis_Iref{target:.0e}".replace('+', '').replace('-0', '-')
    out = dict(vt=os.path.join(folder, 'PMU_stair_vt_' + name[len('PMU_stair_'):-4] + tag + '.csv'),
               plot=os.path.join(folder, name[:-4] + tag + '.png'))
    vt.to_csv(out['vt'], index=False)
    plot(d, vt, p, out['plot'], f"{stress_label(p)} stress / staircase reads / 0 V recovery  "
         f"[{meta['descriptor']}, re-analysed at {target:.2g} A; the run used {meta.get('vt_current_A', np.nan):.2g} A]",
         target)
    col = p['id_from']
    log(f"{name}: Vt at {target:.3g} A ({col}); reference Vt0 {vt[f'Vt_{col}_V'].iloc[0]:.4f} V")
    for r in vt.itertuples():
        log(f"  {r.phase:8s} {r.point:2d}  t_stress {r.t_stress_s:g} s"
            + (f", t_rec {r.t_rec_s:g} s" if r.phase == 'recovery' else '')
            + f": Vt {getattr(r, f'Vt_{col}_V'):.4f} V, dVt {getattr(r, f'dVt_{col}_V') * 1e3:+.1f} mV "
              f"(drain {r.dVt_drain_V * 1e3:+.1f} mV)")
    return vt, out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config')
    ap.add_argument('--plot-waveform', metavar='PATH', help='draw the programmed waveform to this PNG')
    ap.add_argument('--reanalyse', metavar='CSV', help='recompute Vt/dVt and the plot from a saved PMU_stair_*.csv')
    ap.add_argument('--vt-id-per-wl', type=float, help='with --reanalyse: Vt current per W/L (A)')
    ap.add_argument('--vt-id-min-a', type=float, help='with --reanalyse: lowest Vt current (A)')
    a = ap.parse_args(argv)
    if a.reanalyse:
        import matplotlib
        matplotlib.use('Agg')
        _, out = reanalyse(a.reanalyse, a.vt_id_per_wl, a.vt_id_min_a)
        print('\n'.join(out.values()))
        return
    if not a.config:
        ap.error('--config is required unless --reanalyse is given')
    with open(a.config) as fh:
        cfg = json.load(fh)
    p, wf, b = check_config(cfg)
    tl = P.timeline(wf)
    print(json.dumps({k: v for k, v in b.items() if k != 'unused_sequences'}, indent=1))
    first = tl.groupby(['phase', 'point'], sort=False).first()
    print(first[['target_s', 't_stress_s', 't_off_s', 'Vg_set']].rename(columns={'t_off_s': 't_off_first_s'})
          .to_string())
    if a.plot_waveform:
        import matplotlib
        matplotlib.use('Agg')
        os.makedirs(os.path.dirname(os.path.abspath(a.plot_waveform)), exist_ok=True)
        plot_waveform(wf, a.plot_waveform, f"stress_stair programmed waveform ({a.config})")
        print(a.plot_waveform)


if __name__ == '__main__':
    sys.exit(main())
