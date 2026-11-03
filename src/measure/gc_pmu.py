"""2T gain cell alternating write/read transient on the 4225-PMUs (KXCI :PMU:SARB), after the Clarius test
gc_pmu_rw (KULT gc_library_v0 / gc_transient_rw.c, read in .ignore/kult_src/KULT/Koustav).

Lines -> PMU channels: each line is driven by the 4225-RPM of its SMU (smu_map wwl/wbl/rbl/rwl), so the PMU
channel follows the RPM wiring (k4200_pmu.pmu_channel: SMU1 = PMU1-1 = ch 1, SMU2 = ch 2, SMU3 = PMU2-1 = ch 3,
SMU4 = ch 4). With the gain-cell smu_map (WWL smu4, WBL smu3, RBL smu2, RWL smu1): WWL ch 4, WBL ch 3, RBL ch 2,
RWL ch 1. (Clarius drove WWL/WBL/RWL/RBL from PMU1-1/1-2/2-1/2-2 through its own cabling.)

Waveform (one EXECUTE), as gc_transient_rw, RBL held at rbl_V throughout:
- lead-in: all lines 0 V -> baseline (WWL low, WBL low, RWL low) in trf, then tdelay at the baseline
- cycle, looped n_cycles times (22 segments; trf on every edge):
  write 1: WWL low -> high with WBL low -> high, WWL high for twrite, WWL back, trf later WBL back to low;
  thold; read 1: RWL low -> high for tread (WBL low) and back;
  WBL -> high (prepare 0); thold; write 0: WWL -> high with WBL high -> low, twrite, WWL back, trf later WBL back
  to high; thold; read 0: RWL pulse (WBL high); WBL -> low (reset); thold
- lead-out (not measured): baseline -> 0 V
Measured: waveform capture (MEAS:TYPE 2) of every lead-in and cycle segment on all 4 lines (V and I), like
Clarius' PULSE_MEAS_WFM_PER. Over KXCI the samples per channel stay under ~4096 (seen live for spot means and for
a 1 ms capture, 2026-10-16), so the sample rate is the highest 200 MS/s / n that keeps the captured time x rate
<= max_samples (198 us at the Clarius timing -> 20 MS/s, ~200 samples per 10 us read).
Read current: signed mean of -I_RBL over read_window (fraction of the RWL-high plateau, default 50-90 %) of each
read; the run log ends with read 1 / read 0 averaged over the cycles.
Current range: irange is one range (A) or a list, e.g. [1e-6, 1e-5]: then the same waveform runs once per range,
in that order, in the same landing, each range with its own files (tag _1uA, _10uA after GC_PMU_rw) and its own
summary row (Irange_A).
Time axis: PMU timestamps relative to the first sample (the lead-in starts at t = 0); a read window whose RWL
voltage is not at rwl_high flags a time misalignment.
Files: GC_PMU_rw[_<range>]_<stamp>_<descriptor>.csv (samples), GC_PMU_rw_reads[_<range>]_*.csv (one row per
read), GC_PMU_rw_meta[_<range>]_*.json, GC_PMU_rw_raw[_<range>]_*.txt (raw :PMU:DATA:GET replies), .png.
"""
import json
import os
import time

import numpy as np
import pandas as pd

from measure.instruments import k4200_pmu as P

LINES = ('WWL', 'WBL', 'RWL', 'RBL')
FILE_PREFIX = 'GC_PMU_rw_'
DEFAULTS = dict(wwl_high=2.5, wwl_low=-0.5, wbl_high=1.5, wbl_low=0.0, rwl_high=1.5, rwl_low=0.0, rbl_V=0.0,
                tdelay_s=10e-6, trf_s=1e-6, twrite_s=10e-6, thold_s=10e-6, tread_s=10e-6, n_cycles=2, skip_cycles=0,
                irange=1e-5, vrange=10.0, load_ohm=1e6, read_window=[0.5, 0.9], max_samples=4000, v_max=3.0)
SUMMARY = ('I_read1_A', 'I_read0_A', 'read_ratio', 'margin_A', 'I_read1_std_A', 'I_read0_std_A', 'I_read1_max_A',
           'I_read0_absmax_A')
MIN_READ_SAMPLES = 10            # samples inside each read average


def params(block):
    unknown = set(block) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"gc_pmu_rw: unknown settings {sorted(unknown)}")
    return {**DEFAULTS, **block}


def ranges(p):
    """irange as a list of current ranges (A), in measurement order."""
    rs = p['irange'] if isinstance(p['irange'], (list, tuple)) else [p['irange']]
    if not rs or len(set(rs)) != len(rs):
        raise ValueError(f"gc_pmu_rw: irange {p['irange']} must be one range or a list of distinct ranges")
    return [float(r) for r in rs]


def range_tag(irange):
    """1e-6 -> '1uA', 1e-5 -> '10uA', 1e-7 -> '100nA'."""
    return f"{irange * 1e6:g}uA" if irange >= 1e-6 else f"{irange * 1e9:g}nA"


def pmu_chans(smus):
    """{'wwl': smu, ...} -> {'WWL': KXCI pulse channel, ...}."""
    return {t: P.pmu_channel(smus[t.lower()]) for t in LINES}


def build_waveform(p, chans):
    """chans = {'WWL': ch, 'WBL': ch, 'RWL': ch, 'RBL': ch}; voltages in LINES order."""
    tk = {k: P.ticks(p[k], k) for k in ('tdelay_s', 'trf_s', 'twrite_s', 'thold_s', 'tread_s')}
    trf = tk['trf_s']
    rbl = P._v(p['rbl_V'])
    v = lambda wwl, wbl, rwl: (P._v(wwl), P._v(wbl), P._v(rwl), rbl)
    L, H, d0, d1, r0, r1 = p['wwl_low'], p['wwl_high'], p['wbl_low'], p['wbl_high'], p['rwl_low'], p['rwl_high']
    base, zero = v(L, d0, r0), (0.0,) * 4
    cap = lambda n, a, b, name: P.Seg(n, a, b, meas=True, read=name, win=(0, n))
    half = tk['tdelay_s'] // 2
    lead = [cap(trf, zero, base, 'lead_ramp'), cap(half, base, base, 'tdelay'),
            cap(tk['tdelay_s'] - half, base, base, 'tdelay')]
    steps = [('w1_rise', trf, v(H, d1, r0)), ('write1', tk['twrite_s'], v(H, d1, r0)),
             ('w1_fall', trf, v(L, d1, r0)), ('w1_gap', trf, v(L, d1, r0)), ('w1_wbl_back', trf, v(L, d0, r0)),
             ('hold1', tk['thold_s'], v(L, d0, r0)), ('r1_rise', trf, v(L, d0, r1)),
             ('read1', tk['tread_s'], v(L, d0, r1)), ('r1_fall', trf, v(L, d0, r0)),
             ('prep0', trf, v(L, d1, r0)), ('hold2', tk['thold_s'], v(L, d1, r0)),
             ('w0_rise', trf, v(H, d0, r0)), ('write0', tk['twrite_s'], v(H, d0, r0)),
             ('w0_fall', trf, v(L, d0, r0)), ('w0_gap', trf, v(L, d0, r0)), ('w0_wbl_back', trf, v(L, d1, r0)),
             ('hold3', tk['thold_s'], v(L, d1, r0)), ('r0_rise', trf, v(L, d1, r1)),
             ('read0', tk['tread_s'], v(L, d1, r1)), ('r0_fall', trf, v(L, d1, r0)),
             ('reset', trf, v(L, d0, r0)), ('hold4', tk['thold_s'], v(L, d0, r0))]
    cycle, prev = [], base
    for name, n, nxt in steps:
        cycle.append(cap(n, prev, nxt, name))
        prev = nxt
    assert prev == base
    out = [P.Seg(trf, base, base), P.Seg(trf, base, zero), P.Seg(trf, zero, zero)]
    seqs = {1: P.Seq('lead_in', 'unit', lead), 2: P.Seq('cycle', 'unit', cycle), 3: P.Seq('lead_out', 'unit', out)}
    entries = [(1, 1, {'phase': 'lead_in'}), (2, int(p['n_cycles']), {'phase': 'cycle'}), (3, 1, {'phase': 'lead_out'})]
    return P.Waveform('gc_pmu_rw', seqs, entries, tuple(chans[t] for t in LINES), 0, trf, meas_type=2,
                      info=dict(lines=LINES))


def sample_rate(wf, max_samples):
    """Highest 200 MS/s / n with captured time x rate <= max_samples, n a divisor of 200e6 so the rate is a
    whole number of S/s (:PMU:SAMPLE:RATE takes an integer; a rounded-down 200e6/12 would give the next slower
    rate, 200e6/13)."""
    measured = P.budgets(wf)['measured_time_s']
    n = 1
    while P.BASE_RATE % n or P.BASE_RATE / n * measured > max_samples:
        n += 1
    return P.BASE_RATE // n


def check_config(cfg, chans=None):
    """Raise (ValueError) on a bad 'gc_pmu_rw' block before anything connects or moves; returns
    (params, waveform, sample rate, budgets). chans: PMU channels per line (default 1..4)."""
    p = params(cfg.get('gc_pmu_rw', {}))
    for key in ('wwl_high', 'wwl_low', 'wbl_high', 'wbl_low', 'rwl_high', 'rwl_low', 'rbl_V'):
        if abs(p[key]) > p['v_max']:
            raise ValueError(f"gc_pmu_rw: |{key}| = {abs(p[key])} V exceeds v_max = {p['v_max']} V")
    if int(p['n_cycles']) != p['n_cycles'] or not 1 <= p['n_cycles'] <= 1000:
        raise ValueError(f"gc_pmu_rw: n_cycles {p['n_cycles']} outside 1..1000")
    if int(p['skip_cycles']) != p['skip_cycles'] or not 0 <= p['skip_cycles'] < p['n_cycles']:
        raise ValueError(f"gc_pmu_rw: skip_cycles {p['skip_cycles']} must be a whole number 0..n_cycles-1")
    if p['tdelay_s'] < 40e-9:
        raise ValueError("gc_pmu_rw: tdelay_s must be >= 40 ns (two 20 ns segments)")
    a, b = p['read_window']
    if not 0 <= a < b <= 1:
        raise ValueError(f"gc_pmu_rw: read_window {p['read_window']} must be 0 <= start < stop <= 1")
    wf = build_waveform(p, chans or dict(zip(LINES, (1, 2, 3, 4))))
    rate = sample_rate(wf, p['max_samples'])
    for r in ranges(p):                                   # RPM timing limits differ per range
        bud = P.validate(wf, r, p['vrange'], rate)
    if bud['samples'] > P.KXCI_WINDOW_SAMPLES:
        raise ValueError(f"gc_pmu_rw: {bud['samples']:.0f} samples per channel > {P.KXCI_WINDOW_SAMPLES} (KXCI)")
    per_read = (b - a) * p['tread_s'] * rate
    if per_read < MIN_READ_SAMPLES:
        raise ValueError(f"gc_pmu_rw: only {per_read:.1f} samples per read average at {rate / 1e6:.3g} MS/s (the "
                         f"~4096-sample KXCI cap spread over {bud['measured_time_s'] * 1e6:.0f} us); need >= "
                         f"{MIN_READ_SAMPLES}: fewer cycles or shorter holds")
    return p, wf, rate, bud


def schedule(wf):
    """Absolute (start, stop) in s of every measured segment, in order, with its name and cycle index."""
    rows, t = [], 0
    for sid, loops, info in wf.entries:
        q = wf.seqs[sid]
        for k in range(loops):
            for s in q.segs:
                if s.meas:
                    rows.append(dict(name=s.read, cycle=k if info['phase'] == 'cycle' else -1,
                                     t0=t * P.TICK, t1=(t + s.n) * P.TICK))
                t += s.n
    return pd.DataFrame(rows)


def programmed(wf, t):
    """Programmed voltage of every line at times t (s), piecewise linear."""
    T, V, tt = [0.0], [wf.seqs[wf.entries[0][0]].segs[0].v0], 0
    for sid, loops, _ in wf.entries:
        for _ in range(loops):
            for s in wf.seqs[sid].segs:
                tt += s.n
                T.append(tt * P.TICK); V.append(s.v1)
    V = np.array(V, dtype=float)
    return {line: np.interp(t, T, V[:, i]) for i, line in enumerate(LINES)}


def to_frame(data, wf):
    """{channel: [raw points]} -> samples DataFrame (Time_s from the first sample of WWL)."""
    cols, n = {}, None
    for line, ch in zip(LINES, wf.chans):
        pts = [P.parse_point(x) for x in data[ch]]
        if n is None:
            n = len(pts)
        elif len(pts) != n:
            raise ValueError(f"{line} (ch {ch}): {len(pts)} samples, WWL has {n}")
        cols[f'{line}_Voltage'] = [x[0] for x in pts]
        cols[f'{line}_Current'] = [x[1] for x in pts]
        cols[f'{line}_T'] = [x[2] for x in pts]
        cols[f'{line}_Status'] = [x[3] for x in pts]
    d = pd.DataFrame(cols)
    d.insert(0, 'Time_s', d['WWL_T'] - d['WWL_T'].iloc[0] if n else [])
    for line, vals in programmed(wf, d['Time_s'].to_numpy()).items():
        d[f'{line}_V_set'] = np.round(vals, 6)
    return d


def read_frame(path):
    """A saved samples CSV, with the 8-digit PMU status codes kept as text (a plain read drops the leading 0)."""
    d = pd.read_csv(path, dtype={f'{t}_Status': str for t in LINES})
    for t in LINES:
        d[f'{t}_Status'] = d[f'{t}_Status'].str.zfill(8)
    return d


def analyse(d, wf, p):
    """One row per read, over read_window of the RWL-high plateau: the read current as the SIGNED mean,
    I_RBL_A = -mean(RBL current) (the read current flows out of RBL), and I_RWL_A = mean(RWL current); the mean
    of |I| would return the noise level (~2e-8 A on 10 uA) for a read 0 far below it. I_RBL_sem_A = standard error
    of that mean. The first skip_cycles cycles are kept in the table (used = False) but left out of every average,
    spread, maximum and flag (the first write after a rest reads far higher, e.g. 11 vs 1.7 uA on R16C15)."""
    sch = schedule(wf)
    a, b = p['read_window']
    rows = []
    for _, r in sch[sch['name'].isin(['read1', 'read0'])].iterrows():
        lo, hi = r.t0 + a * (r.t1 - r.t0), r.t0 + b * (r.t1 - r.t0)
        m = (d['Time_s'] >= lo - 1e-12) & (d['Time_s'] <= hi + 1e-12)
        w = d[m]
        over = bool(((w['RBL_Current'].abs() >= p['irange']) | w['RBL_Status'].map(P.status_overflow)).any()) \
            if len(w) else False
        rows.append(dict(cycle=int(r.cycle), read=r['name'], data=1 if r['name'] == 'read1' else 0,
                         t_start_s=lo, t_stop_s=hi, n_samples=int(m.sum()),
                         I_RBL_A=float(-w['RBL_Current'].mean()) if len(w) else np.nan,
                         I_RBL_sem_A=float(w['RBL_Current'].std() / np.sqrt(len(w))) if len(w) > 1 else np.nan,
                         I_RWL_A=float(w['RWL_Current'].mean()) if len(w) else np.nan,
                         V_RWL_V=float(w['RWL_Voltage'].mean()) if len(w) else np.nan,
                         V_WBL_V=float(w['WBL_Voltage'].mean()) if len(w) else np.nan, overflow=over,
                         used=int(r.cycle) >= int(p.get('skip_cycles', 0))))
    reads = pd.DataFrame(rows)
    flags = []
    if (reads['n_samples'] == 0).any() or (abs(reads['V_RWL_V'] - p['rwl_high']) > 0.2).any():
        flags.append("time alignment: a read window is not on the RWL-high plateau")
    u = reads[reads['used']]
    if u['overflow'].any():
        flags.append(f"RBL overflow in {int(u['overflow'].sum())} reads ({p['irange']:g} A range)")
    r1, r0 = u[u['data'] == 1], u[u['data'] == 0]
    i1, i0 = r1['I_RBL_A'].mean(), r0['I_RBL_A'].mean()
    sem0 = np.sqrt((r0['I_RBL_sem_A'] ** 2).sum()) / max(len(r0), 1)      # of the read-0 mean over cycles
    resolved = i0 > 3 * sem0
    if not resolved:
        flags.append(f"read 0 not resolved ({i0:.2g} A, noise {sem0:.2g} A): read1/read0 left empty")
    res = dict(I_read1_A=float(i1), I_read0_A=float(i0), read_ratio=float(i1 / i0) if resolved else np.nan,
               margin_A=float(i1 - i0),
               I_read1_std_A=float(r1['I_RBL_A'].std()) if len(r1) > 1 else np.nan,    # cycle-to-cycle spread
               I_read0_std_A=float(r0['I_RBL_A'].std()) if len(r0) > 1 else np.nan,
               I_read0_noise_A=float(sem0),
               I_read1_max_A=float(r1['I_RBL_A'].max()) if len(r1) else np.nan,         # for range choices
               I_read0_absmax_A=float(r0['I_RBL_A'].abs().max()) if len(r0) else np.nan)
    return reads, res, flags


def average_line(reads, res):
    """End-of-run line: read 1 / read 0 averaged over the used cycles, with the cycle-to-cycle spread and range."""
    u = reads[reads['used']] if 'used' in reads else reads
    skipped = sorted(set(reads['cycle']) - set(u['cycle']))

    def one(data):
        r = u[u['data'] == data]['I_RBL_A']
        key = f'I_read{data}'
        return (f"read {data} {res[key + '_A']:.3g} A (std {res[key + '_std_A']:.2g} over cycles, "
                f"range {r.min():.3g}..{r.max():.3g})")
    skip = f" (cycle {', '.join(map(str, skipped))} skipped)" if skipped else ''
    return (f"PMU average over {u['cycle'].nunique()} cycles{skip}: {one(1)}; {one(0)}, noise "
            f"{res['I_read0_noise_A']:.2g} A | read1/read0 {res['read_ratio']:.3g}, margin {res['margin_A']:.3g} A")


def plot(d, reads, wf, p, title):
    import matplotlib.pyplot as plt
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    us = d['Time_s'] * 1e6
    colors = dict(WWL='tab:red', WBL='tab:green', RWL='tab:orange', RBL='tab:blue')
    for line in ('WWL', 'WBL', 'RWL'):
        ax.plot(us, d[f'{line}_Voltage'], color=colors[line], label=f'V {line}')
        ax.plot(us, d[f'{line}_V_set'], ':', color=colors[line], lw=0.8)
    ax.set_ylabel('V (V)'); ax.grid(True, alpha=0.3); ax.legend(loc='upper right', ncol=3)
    ax2.semilogy(us, d['RBL_Current'].abs().clip(lower=1e-13), color=colors['RBL'], lw=0.8, label='|I RBL|')
    for _, r in reads.iterrows():
        used = bool(r.get('used', True))
        ax2.axvspan(r.t_start_s * 1e6, r.t_stop_s * 1e6, color=('0.85' if r.data else '0.93') if used else '0.97',
                    zorder=0)
        ax2.annotate(f"{r['read']}{'' if used else ' (skipped)'}\n{r.I_RBL_A:.2e}",
                     (r.t_start_s * 1e6, max(abs(r.I_RBL_A), 1e-12)), fontsize=7, xytext=(0, 6),
                     textcoords='offset points', color='k' if used else '0.5')
    ax2.axhline(p['irange'], color='0.5', lw=0.8, ls='--')
    ax2.set_xlabel('time (us)'); ax2.set_ylabel('|I RBL| (A)'); ax2.grid(True, which='both', alpha=0.3)
    ax2.legend(loc='upper right')
    fig.suptitle(f"{title}\nWWL {p['wwl_low']:g}/{p['wwl_high']:g} V, WBL {p['wbl_low']:g}/{p['wbl_high']:g} V, "
                 f"RWL {p['rwl_low']:g}/{p['rwl_high']:g} V, RBL {p['rbl_V']:g} V; trf {p['trf_s'] * 1e6:g} us, "
                 f"write/hold/read {p['twrite_s'] * 1e6:g}/{p['thold_s'] * 1e6:g}/{p['tread_s'] * 1e6:g} us, "
                 f"{p['n_cycles']} cycles" + (f" (first {p['skip_cycles']} skipped)" if p.get('skip_cycles') else '')
                 + f"; {p['irange']:g} A range")
    fig.tight_layout()
    return fig


def measure_cell(k, smus, cfg, save_dir, descriptor, save=True, show=False, log=print, runner_kw=None, tag=''):
    """The write/read transient on the cell under the probes, once per current range (irange one value or a list);
    smus = {'wwl': n, 'wbl': n, 'rbl': n, 'rwl': n}. Returns the summary dict, or one per range (with Irange_A)
    when irange is a list. tag goes into the file names after 'GC_PMU_rw' (e.g. '_pre1')."""
    chans = pmu_chans(smus)
    p, wf, rate, _ = check_config(cfg, chans)
    rs = ranges(p)
    if not isinstance(p['irange'], (list, tuple)):
        return _measure_range(k, smus, chans, p, wf, rate, save_dir, descriptor, tag, save, show, log, runner_kw)
    out = []
    for r in rs:
        log(f"--- {range_tag(r)} range ({rs.index(r) + 1}/{len(rs)})")
        out.append({'Irange_A': r, **_measure_range(k, smus, chans, {**p, 'irange': r}, wf, rate, save_dir,
                                                     descriptor, f"{tag}_{range_tag(r)}", save, show, log, runner_kw)})
    return out


def _measure_range(k, smus, chans, p, wf, rate, save_dir, descriptor, tag, save, show, log, runner_kw):
    bud = P.validate(wf, p['irange'], p['vrange'], rate)
    os.makedirs(save_dir, exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    f = lambda name, ext='csv': os.path.join(save_dir, f"{name}{tag}_{stamp}_{descriptor}.{ext}")
    paths = dict(data=f('GC_PMU_rw'), reads=f('GC_PMU_rw_reads'), meta=f('GC_PMU_rw_meta', 'json'),
                 raw=f('GC_PMU_rw_raw', 'txt'), plot=f('GC_PMU_rw', 'png'))
    meta = dict(descriptor=descriptor, params=p, pmu_channels=chans, smus=smus, sample_rate=rate, budgets=bud,
                idn=k.idn, opt=k.opt, started=time.strftime('%Y-%m-%d %H:%M:%S'),
                commands=P.commands(wf, p['irange'], p['vrange'], p['load_ohm'], rate))
    with open(paths['meta'], 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, indent=1, default=lambda x: x.item() if hasattr(x, 'item') else str(x))
    log(f"PMU write/read ({p['irange']:g} A range): {wf.duration_s * 1e6:.1f} us, {p['n_cycles']} cycles, capture {bud['measured_time_s'] * 1e6:.1f} us "
        f"at {rate / 1e6:g} MS/s ({bud['samples']:.0f} samples per line); PMU ch WWL {chans['WWL']}, WBL {chans['WBL']}, "
        f"RWL {chans['RWL']}, RBL {chans['RBL']}")
    runner = P.PmuRunner(k, tuple(smus[t.lower()] for t in LINES), log=log, **(runner_kw or {}))
    data = runner.run(wf, p['irange'], p['vrange'], p['load_ohm'], rate, raw_log=paths['raw'])
    d = to_frame(data, wf)
    reads, res, flags = analyse(d, wf, p)
    dt = np.median(np.diff(d['Time_s'])) if len(d) > 1 else np.nan
    if np.isfinite(dt) and abs(dt * rate - 1) > 0.05:
        flags.append(f"sample spacing {dt * 1e9:.0f} ns, programmed {1e9 / rate:.0f} ns (KXCI changed the rate)")
    d.to_csv(paths['data'], index=False)
    reads.to_csv(paths['reads'], index=False)
    meta.update(result=res, flags=flags, samples=len(d), sample_spacing_s=dt, exec_s=runner.exec_s)
    with open(paths['meta'], 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, indent=1, default=lambda x: x.item() if hasattr(x, 'item') else str(x))
    if save:
        import matplotlib.pyplot as plt
        fig = plot(d, reads, wf, p, descriptor)
        fig.savefig(paths['plot'])
        if show:
            plt.show(block=False); plt.pause(1)
        plt.close(fig)
    log(f"Data saved to: {paths['data']}")
    log(f"PMU reads (signed mean of -I RBL, {int(p['read_window'][0] * 100)}-{int(p['read_window'][1] * 100)} % of "
        f"each read): " + ', '.join(f"c{r.cycle} {r['read']} {r.I_RBL_A:.2e}{'' if r.used else ' (skipped)'}"
                                    for _, r in reads.iterrows()))
    log(f"[{p['irange']:g} A] " + average_line(reads, res) + (f" | flags: {'; '.join(flags)}" if flags else ''))
    return {'status': 'measured', 'reason': '', 'flags': '; '.join(flags), 'main_file': paths['data'],
            **{key: res[key] for key in SUMMARY}}


def main(argv=None):
    """Offline only: recompute the reads and summary of saved GC_PMU_rw_*.csv files (no instrument), using the
    settings in the matching GC_PMU_rw_meta_*.json. Prints; --out writes the per-read table (the data files
    themselves are never changed)."""
    import argparse
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument('--reanalyse', nargs='+', required=True, metavar='CSV')
    ap.add_argument('--out', help='write the per-read table of all files to this CSV')
    ap.add_argument('--skip-cycles', type=int, help='leave out the first N cycles (default: as in the meta file)')
    a = ap.parse_args(argv)
    tables = []
    for f in a.reanalyse:
        meta = json.load(open(f.replace(FILE_PREFIX, FILE_PREFIX + 'meta_').replace('.csv', '.json'), encoding='utf-8'))
        p = params(meta['params'])
        if a.skip_cycles is not None:
            p['skip_cycles'] = a.skip_cycles
        wf = build_waveform(p, meta['pmu_channels'])
        reads, res, flags = analyse(read_frame(f), wf, p)
        reads.insert(0, 'file', os.path.basename(f))
        tables.append(reads)
        with pd.option_context('display.width', 250, 'display.max_columns', 20):
            print(os.path.basename(f))
            print(reads.drop(columns='file').to_string(index=False, float_format=lambda x: f'{x:.3g}'))
        print('  ' + ', '.join(f'{k} {v:.3g}' for k, v in res.items()) + (f" | flags: {'; '.join(flags)}" if flags else ''))
        print('  ' + average_line(reads, res))
    if a.out:
        pd.concat(tables).to_csv(a.out, index=False)
        print(f"written: {a.out}")


if __name__ == '__main__':
    main()
