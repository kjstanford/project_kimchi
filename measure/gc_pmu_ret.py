"""2T gain cell retention on the 4225-PMUs (KXCI :PMU:SARB), after the Clarius test gc_pmu_retention (KULT
gc_library_v0 / gc_retention.c, read in .ignore/kult_src/KULT/Koustav/gc_retention.c).

Lines -> PMU channels as in measure.gc_pmu (each line on the 4225-RPM of its SMU, gc_pmu.pmu_chans).

Waveform, one SARB test (EXECUTE) per state, RBL held at rbl_V throughout; base = (WWL low, WBL at the level
opposite to the written state, RWL low):
- lead-in: all lines 0 V -> base in trf, then tdelay at base
- write: WWL low -> high with WBL base -> data in trf, WWL high for twrite, WWL back in trf, trf later WBL back
  to base in trf. Retention time 0 is the end of this WBL return (as gc_retention.c).
- read i at retention time t_i (cumulative, from that origin, to the start of the RWL edge): hold at base for
  t_i - t_(i-1) - (2 trf + tread), then RWL low -> high in trf, tread flat, back in trf. One spot mean
  (MEAS:TYPE 1) of every line over read_window (fraction of tread) of the RWL-high plateau. tread_s0_s, if set,
  is the read length of the state-0 test (e.g. 150 us for RBL on 100 nA while state 1 reads 30 us).
- lead-out (not measured): base -> 0 V.
Long holds are looped 1 ms units plus one remainder sequence (KXCI segments are <= 1 s; every long waveform that
ran live used looped 1 ms units, measure.stress_stair.UNIT), so they add no samples.
Current ranges: every line on irange; with irange_s0 set, RBL alone moves to that range for the state-0 test
(e.g. 100 nA for the sub-nA stored 0; the state-0 read must then satisfy that range's RPM timing: >= 134 us
plateau, average starting >= 100 us in, so tread_s0_s 150 us with read_window from 0.7). The drive lines keep irange, so their edges and the read current are not limited.
Read current: I_read_A = -I_RBL (the read current flows out of RBL). Time axis: the programmed schedule
(P.timeline); the PMU timestamps are only a cross-check.
Files: GC_PMU_ret_<stamp>_<descriptor>.csv (one row per read, all states), GC_PMU_ret_meta_*.json,
GC_PMU_ret_raw_*.txt (raw :PMU:DATA:GET replies, a '# state n' line before each test), .png.
"""
import json
import os
import time

import numpy as np
import pandas as pd

from measure import gc_pmu
from measure.instruments import k4200_pmu as P

LINES = gc_pmu.LINES
FILE_PREFIX = 'GC_PMU_ret_'
DEFAULTS = dict(wwl_high=2.5, wwl_low=-0.5, wbl_high=1.5, wbl_low=0.0, rwl_high=1.5, rwl_low=0.0, rbl_V=0.0,
                states=[1, 0], tdelay_s=10e-6, trf_s=2e-6, twrite_s=10e-6, tread_s=30e-6, tread_s0_s=None,
                read_window=[0.6, 0.95],
                retention_times_s=[0, 1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100],
                irange=1e-6, irange_s0=None, vrange=10.0, load_ohm=1e6, max_samples=4000, v_max=3.0)
SUMMARY = ('Iret_s1_start_A', 'Iret_s1_end_A', 'Iret_s0_start_A', 'Iret_s0_end_A', 'ret_ratio_end')
UNIT = 100_000                   # 1 ms in ticks
MIN_HOLD = 8                     # ticks: a 3-segment hold sequence needs >= 2 ticks (20 ns) per segment


def params(block):
    unknown = set(block) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"gc_pmu_ret: unknown settings {sorted(unknown)}")
    return {**DEFAULTS, **block}


def _add(seqs, entries, seq, loops=1, **info):
    """Append a list entry, reusing an already defined sequence of the same name."""
    sid = next((s for s, q in seqs.items() if q.name == seq.name), None)
    if sid is None:
        sid = len(seqs) + 1
        seqs[sid] = seq
    entries.append((sid, int(loops), info))


def _flat3(v, n):
    a = n // 4
    return [P.Seg(a, v, v), P.Seg(a, v, v), P.Seg(n - 2 * a, v, v)]


def _add_hold(seqs, entries, v, n, **info):
    """n ticks flat at v: looped 1 ms units plus one remainder sequence (merged with a unit if under 80 ns)."""
    k, r = divmod(n, UNIT)
    if 0 < r < MIN_HOLD:
        if not k:
            raise P.SarbError(f"hold of {n * P.TICK:g} s is shorter than {MIN_HOLD * P.TICK:g} s")
        k, r = k - 1, r + UNIT
    if k:
        _add(seqs, entries, P.Seq('hold_unit', 'unit', _flat3(v, UNIT)), k, **info)
    if r:
        _add(seqs, entries, P.Seq(f'hold_{r}t', 'hold', _flat3(v, r)), **info)


def read_s(p, state):
    """Read plateau length (s) of the test for this state."""
    return float(p['tread_s0_s']) if state == 0 and p['tread_s0_s'] else float(p['tread_s'])


def build_waveform(p, chans, state):
    """chans = {'WWL': ch, ...}; voltages in LINES order. wf.info['t0_ticks'] = retention origin."""
    tk = {k: P.ticks(p[k], k) for k in ('tdelay_s', 'trf_s', 'twrite_s')}
    trf, tread = tk['trf_s'], P.ticks(read_s(p, state), 'tread_s0_s' if state == 0 and p['tread_s0_s'] else 'tread_s')
    rbl = P._v(p['rbl_V'])
    v = lambda wwl, wbl, rwl: (P._v(wwl), P._v(wbl), P._v(rwl), rbl)
    L, H, r0, r1 = p['wwl_low'], p['wwl_high'], p['rwl_low'], p['rwl_high']
    data, opp = (p['wbl_high'], p['wbl_low']) if state == 1 else (p['wbl_low'], p['wbl_high'])
    base, zero = v(L, opp, r0), (0.0,) * 4
    half = tk['tdelay_s'] // 2
    seqs, entries = {}, []
    _add(seqs, entries, P.Seq('lead_in', 'ramp', [P.Seg(trf, zero, base), P.Seg(half, base, base),
                                                   P.Seg(tk['tdelay_s'] - half, base, base)]), phase='lead_in')
    w = [('w_rise', trf, v(H, data, r0)), ('write', tk['twrite_s'], v(H, data, r0)), ('w_fall', trf, v(L, data, r0)),
         ('w_gap', trf, v(L, data, r0)), ('wbl_back', trf, base)]
    segs, prev = [], base
    for _, n, nxt in w:
        segs.append(P.Seg(n, prev, nxt))
        prev = nxt
    _add(seqs, entries, P.Seq('write', 'unit', segs), phase='write')
    t0 = sum(seqs[s].n * loops for s, loops, _ in entries)
    hi = v(L, opp, r1)
    read = P.Seq('read', 'spot', [P.Seg(trf, base, hi), P.Seg(tread, hi, hi, meas=True, read='read'),
                                  P.Seg(trf, hi, base)])
    pulse, prev_end = read.n, None
    for i, t in enumerate(p['retention_times_s']):
        start = P.ticks(t, f'retention time {t:g}')
        gap = start if prev_end is None else start - prev_end
        if gap < 0:
            raise P.SarbError(f"retention time {t:g} s starts before the previous read ends "
                              f"({(prev_end or 0) * P.TICK:g} s; a read takes {pulse * P.TICK:g} s)")
        if gap:
            _add_hold(seqs, entries, base, gap, phase='hold', point=i, target_s=float(t))
        _add(seqs, entries, read, phase='read', point=i, target_s=float(t), state=state)
        prev_end = start + pulse
    _add(seqs, entries, P.Seq('lead_out', 'ramp', [P.Seg(trf, base, base), P.Seg(trf, base, zero),
                                                    P.Seg(trf, zero, zero)]), phase='lead_out')
    a, b = p['read_window']
    wf = P.Waveform(f'gc_pmu_ret_s{state}', seqs, entries, tuple(chans[t] for t in LINES),
                    int(round(a * tread)), int(round(b * tread)), meas_type=1,
                    info=dict(lines=LINES, state=state, t0_ticks=t0))
    return P.renumber(wf)


def state_ranges(p, wf, state):
    """{channel: current range} of the test for this state: all lines on irange, RBL on irange_s0 in state 0."""
    out = {ch: float(p['irange']) for ch in wf.chans}
    if state == 0 and p['irange_s0']:
        out[wf.chans[LINES.index('RBL')]] = float(p['irange_s0'])
    return out


def rbl_range(p, state):
    return float(p['irange_s0']) if state == 0 and p['irange_s0'] else float(p['irange'])


def check_config(cfg, chans=None):
    """Raise (ValueError) on a bad 'gc_pmu_ret' block before anything connects or moves; returns
    (params, {state: waveform}, sample rate, budgets of the first state; every state's budget is checked).
    chans: PMU channels per line."""
    p = params(cfg.get('gc_pmu_ret', {}))
    for key in ('wwl_high', 'wwl_low', 'wbl_high', 'wbl_low', 'rwl_high', 'rwl_low', 'rbl_V'):
        if abs(p[key]) > p['v_max']:
            raise ValueError(f"gc_pmu_ret: |{key}| = {abs(p[key])} V exceeds v_max = {p['v_max']} V")
    if not p['states'] or any(s not in (0, 1) for s in p['states']) or len(set(p['states'])) != len(p['states']):
        raise ValueError(f"gc_pmu_ret: states {p['states']} must be distinct values of 0 and 1")
    ts = [float(t) for t in p['retention_times_s']]
    if not ts or ts[0] < 0 or any(b <= a for a, b in zip(ts, ts[1:])):
        raise ValueError("gc_pmu_ret: retention_times_s must be >= 0 and strictly increasing")
    p['retention_times_s'] = ts
    if p['tdelay_s'] < 40e-9:
        raise ValueError("gc_pmu_ret: tdelay_s must be >= 40 ns (two 20 ns segments)")
    a, b = p['read_window']
    if not 0 <= a < b <= 1:
        raise ValueError(f"gc_pmu_ret: read_window {p['read_window']} must be 0 <= start < stop <= 1")
    if p['tread_s0_s'] is not None and not float(p['tread_s0_s']) > 0:
        raise ValueError(f"gc_pmu_ret: tread_s0_s {p['tread_s0_s']} must be > 0 s, or null (= tread_s)")
    chans = chans or dict(zip(LINES, (1, 2, 3, 4)))
    wfs = {s: build_waveform(p, chans, s) for s in p['states']}
    wf = wfs[p['states'][0]]
    rate = min(gc_pmu.sample_rate(w, p['max_samples']) for w in wfs.values())
    for s, w in wfs.items():
        P.validate(w, state_ranges(p, w, s), p['vrange'], rate, min_kxci_spw=gc_pmu.MIN_READ_SAMPLES)
    for s, w in wfs.items():
        b_ = P.budgets(w, rate)
        if b_['samples'] > P.KXCI_WINDOW_SAMPLES:
            raise ValueError(f"gc_pmu_ret: state {s}: {b_['samples']:.0f} samples per channel > "
                             f"{P.KXCI_WINDOW_SAMPLES} (KXCI)")
        if b_['samples_per_window'] < gc_pmu.MIN_READ_SAMPLES:
            raise ValueError(f"gc_pmu_ret: state {s}: only {b_['samples_per_window']:.1f} samples per read at "
                             f"{rate / 1e6:.3g} MS/s; need >= {gc_pmu.MIN_READ_SAMPLES}: fewer reads or a longer "
                             f"read_window")
    return p, wfs, rate, P.budgets(wf, rate)


def to_frame(data, wf, p):
    """{channel: [raw spot-mean points]} -> one row per read."""
    tl = P.timeline(wf)
    tl = tl[tl['phase'] == 'read'].reset_index(drop=True)
    n = min([len(tl)] + [len(data.get(ch, [])) for ch in wf.chans])
    t0 = wf.info['t0_ticks'] * P.TICK
    d = pd.DataFrame(dict(State=wf.info['state'], point=tl['point'][:n].astype(int), t_ret_s=tl['target_s'][:n],
                          t_mid_s=tl['t_s'][:n] - t0,
                          t_win_start_s=tl['t_s'][:n] - t0 - (wf.meas_stop - wf.meas_start) / 2 * P.TICK))
    for line, ch in zip(LINES, wf.chans):
        pts = [P.parse_point(x) for x in data[ch][:n]]
        d[f'{line}_Voltage_meas'] = [x[0] for x in pts]
        d[f'{line}_Current'] = [x[1] for x in pts]
        d[f'{line}_T'] = [x[2] for x in pts]
        d[f'{line}_Status'] = [str(x[3]).zfill(8) for x in pts]
    d['I_read_A'] = -d['RBL_Current']
    d['Irange_RBL_A'] = rbl_range(p, wf.info['state'])
    d['overflow'] = (d['RBL_Current'].abs() >= d['Irange_RBL_A']) | d['RBL_Status'].map(P.status_overflow)
    return d


def read_frame(path):
    """A saved CSV, with the 8-digit PMU status codes kept as text."""
    d = pd.read_csv(path, dtype={f'{t}_Status': str for t in LINES})
    for t in LINES:
        d[f'{t}_Status'] = d[f'{t}_Status'].str.zfill(8)
    return d


def analyse(d, p):
    """Summary (gc_retention field names) + flags. I_read0_noise_A = scatter (std) of the state-0 reads."""
    res, flags = {k: np.nan for k in SUMMARY}, []
    for s in (1, 0):
        r = d[d['State'] == s].sort_values('point')
        if len(r):
            res[f'Iret_s{s}_start_A'] = float(r['I_read_A'].iloc[0])
            res[f'Iret_s{s}_end_A'] = float(r['I_read_A'].iloc[-1])
    r0 = d[d['State'] == 0]['I_read_A']
    noise = float(r0.std()) if len(r0) > 1 else np.nan
    res['I_read0_noise_A'] = noise
    if np.isfinite(res['Iret_s1_end_A']) and np.isfinite(res['Iret_s0_end_A']):
        if res['Iret_s0_end_A'] > 3 * noise:
            res['ret_ratio_end'] = res['Iret_s1_end_A'] / res['Iret_s0_end_A']
        else:
            flags.append(f"read 0 not resolved ({res['Iret_s0_end_A']:.2g} A, scatter {noise:.2g} A): "
                         f"s1/s0 left empty")
    if d['overflow'].any():
        rs = sorted(set(d.loc[d['overflow'], 'Irange_RBL_A'])) if 'Irange_RBL_A' in d else [p['irange']]
        flags.append(f"RBL overflow in {int(d['overflow'].sum())} reads ({', '.join(f'{r:g}' for r in rs)} A range)")
    if (abs(d['RWL_Voltage_meas'] - p['rwl_high']) > 0.2).any():
        flags.append("time alignment: a read window is not on the RWL-high plateau")
    for s, r in d.groupby('State'):
        T = r['RBL_T'] - r['RBL_T'].iloc[0]
        prog = r['t_win_start_s'] - r['t_win_start_s'].iloc[0]
        bad = (abs(T - prog) > 1e-3 + 0.01 * prog).sum() if T.notna().all() else 0
        if bad:
            flags.append(f"PMU timestamps differ from the schedule in {int(bad)} reads of state {s} (time axis: schedule)")
    return res, flags


def plot(d, p, title, noise=np.nan):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 5.5))
    for s, color in ((1, 'tab:blue'), (0, 'tab:red')):
        r = d[d['State'] == s]
        if not len(r):
            continue
        pos = r['I_read_A'] > 0
        ax.loglog(r['t_mid_s'], r['I_read_A'].abs().clip(lower=1e-13), '-', color=color, lw=0.8, alpha=0.6)
        ax.loglog(r['t_mid_s'][pos], r['I_read_A'][pos], 'o', color=color, label=f'state {s}')
        ax.loglog(r['t_mid_s'][~pos], r['I_read_A'][~pos].abs().clip(lower=1e-13), 'o', mfc='none', color=color,
                  label=f'state {s}, negative' if (~pos).any() else None)
    if np.isfinite(noise) and noise > 0:
        ax.axhline(3 * noise, color='0.4', ls='--', lw=0.8, label='3 x state-0 scatter')
    ax.set_xlabel('time after write (s)'); ax.set_ylabel('read current -I RBL (A)')
    ax.grid(True, which='both', alpha=0.3); ax.legend(loc='best', fontsize=8)
    rd = f"read {read_s(p, 1) * 1e6:g} us" + (f" (state 0 {read_s(p, 0) * 1e6:g} us)" if read_s(p, 0) != read_s(p, 1) else '')
    ax.set_title(f"{title}\nWWL {p['wwl_low']:g}/{p['wwl_high']:g} V, WBL {p['wbl_low']:g}/{p['wbl_high']:g} V "
                 f"(held opposite), RWL {p['rwl_low']:g}/{p['rwl_high']:g} V; write {p['twrite_s'] * 1e6:g} us, "
                 f"{rd} ({p['read_window'][0]:g}-{p['read_window'][1]:g}); "
                 f"{p['irange']:g} A range" + (f", state 0 RBL {p['irange_s0']:g} A" if p['irange_s0'] else ''),
                 fontsize=9)
    fig.tight_layout()
    return fig


def measure_cell(k, smus, cfg, save_dir, descriptor, save=True, show=False, log=print, runner_kw=None):
    """Write + retention reads, one SARB test per state, on the cell under the probes;
    smus = {'wwl': n, 'wbl': n, 'rbl': n, 'rwl': n}."""
    chans = gc_pmu.pmu_chans(smus)
    p, wfs, rate, bud = check_config(cfg, chans)
    os.makedirs(save_dir, exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    f = lambda name, ext='csv': os.path.join(save_dir, f"{name}_{stamp}_{descriptor}.{ext}")
    paths = dict(data=f('GC_PMU_ret'), meta=f('GC_PMU_ret_meta', 'json'), raw=f('GC_PMU_ret_raw', 'txt'),
                 plot=f('GC_PMU_ret', 'png'))
    meta = dict(descriptor=descriptor, params=p, pmu_channels=chans, smus=smus, sample_rate=rate, budgets=bud,
                idn=k.idn, opt=k.opt, started=time.strftime('%Y-%m-%d %H:%M:%S'), states={})
    dump = lambda: json.dump(meta, open(paths['meta'], 'w', encoding='utf-8'), indent=1,
                             default=lambda x: x.item() if hasattr(x, 'item') else str(x))
    frames = []
    for s in p['states']:
        wf = wfs[s]
        rng, b_ = state_ranges(p, wf, s), P.budgets(wf, rate)
        meta['states'][s] = dict(commands=P.commands(wf, rng, p['vrange'], p['load_ohm'], rate), ranges=rng,
                                 budgets=b_, duration_s=wf.duration_s, started=time.strftime('%Y-%m-%d %H:%M:%S'))
        dump()
        log(f"PMU retention state {s}: {len(p['retention_times_s'])} reads at 0..{p['retention_times_s'][-1]:g} s "
            f"after the write, {wf.duration_s:.1f} s, write {p['twrite_s'] * 1e6:g} us, read {read_s(p, s) * 1e6:g} us, "
            f"{rate / 1e6:g} MS/s ({b_['samples_per_window']:.0f} samples per read), WBL held at "
            f"{p['wbl_low'] if s else p['wbl_high']:g} V, RBL range {rbl_range(p, s):g} A")
        with open(paths['raw'], 'a', encoding='utf-8') as fh:
            fh.write(f"# state {s}\n")
        runner = P.PmuRunner(k, tuple(smus[t.lower()] for t in LINES), log=log, **(runner_kw or {}))
        data = runner.run(wf, rng, p['vrange'], p['load_ohm'], rate, raw_log=paths['raw'])
        d = to_frame(data, wf, p)
        frames.append(d)
        meta['states'][s].update(exec_s=runner.exec_s, reads=len(d))
        log(f"state {s}: " + ', '.join(f"{t:g} s {i:.2e}" for t, i in zip(d['t_ret_s'], d['I_read_A'])))
    d = pd.concat(frames, ignore_index=True)
    res, flags = analyse(d, p)
    d.to_csv(paths['data'], index=False)
    meta.update(result=res, flags=flags)
    dump()
    if save:
        import matplotlib.pyplot as plt
        fig = plot(d, p, descriptor, res['I_read0_noise_A'])
        fig.savefig(paths['plot'])
        if show:
            plt.show(block=False); plt.pause(1)
        plt.close(fig)
    log(f"Data saved to: {paths['data']}")
    log(summary_line(res, p) + (f" | flags: {'; '.join(flags)}" if flags else ''))
    return {'status': 'measured', 'reason': '', 'flags': '; '.join(flags), 'main_file': paths['data'],
            'Irange_A': float(p['irange']), 'Irange_s0_A': rbl_range(p, 0), **{key: res[key] for key in SUMMARY}}


def summary_line(res, p):
    t = p['retention_times_s']
    return (f"PMU retention {t[0]:g} -> {t[-1]:g} s: state 1 {res['Iret_s1_start_A']:.3g} -> {res['Iret_s1_end_A']:.3g} A, "
            f"state 0 {res['Iret_s0_start_A']:.3g} -> {res['Iret_s0_end_A']:.3g} A (scatter "
            f"{res['I_read0_noise_A']:.2g} A) | s1/s0 at {t[-1]:g} s {res['ret_ratio_end']:.3g}")


def main(argv=None):
    """Offline only: recompute the summary of saved GC_PMU_ret_*.csv files (no instrument), with the settings
    in the matching GC_PMU_ret_meta_*.json. Prints; --out writes the per-read table of all files."""
    import argparse
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument('--reanalyse', nargs='+', required=True, metavar='CSV')
    ap.add_argument('--out', help='write the per-read table of all files to this CSV')
    a = ap.parse_args(argv)
    tables = []
    for f in a.reanalyse:
        meta = json.load(open(f.replace(FILE_PREFIX, FILE_PREFIX + 'meta_').replace('.csv', '.json'), encoding='utf-8'))
        p = params(meta['params'])
        d = read_frame(f)
        res, flags = analyse(d, p)
        d.insert(0, 'file', os.path.basename(f))
        tables.append(d)
        print(os.path.basename(f))
        cols = ['State', 'point', 't_ret_s', 'I_read_A', 'RWL_Voltage_meas', 'WBL_Voltage_meas', 'overflow']
        print(d[cols].to_string(index=False, float_format=lambda x: f'{x:.3g}'))
        print('  ' + ', '.join(f'{k} {v:.3g}' for k, v in res.items()) + (f" | flags: {'; '.join(flags)}" if flags else ''))
        print('  ' + summary_line(res, p))
    if a.out:
        pd.concat(tables).to_csv(a.out, index=False)
        print(f"written: {a.out}")


if __name__ == '__main__':
    main()
