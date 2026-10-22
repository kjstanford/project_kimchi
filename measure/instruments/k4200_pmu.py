"""4225-PMU / 4225-RPM Segment Arb (SARB) over KXCI: waveform builder, checks, command list and runner.

Pages: KXCI manual 4200A-KXCI-907-01E [7-xx]; datasheet 1KW-60780-7 [DS p.xx]; PMU manual
4200A-PMU-900-01C [PMU p.x-xx]. Background: .claude/skills/k4200a-pmu (references/kxci-pmu.md §4-8).

- A waveform is a set of sequences (each >= 3 segments) and a sequence list [(seq, loops)]. All its
  channels (gate, drain, source for the FET experiments; any number, e.g. the 4 gain-cell lines) share the
  same segment times; each segment carries the start and stop voltage of every channel, in wf.chans order.
  Times are kept as integer 10 ns ticks [7-52]. A measured segment uses the waveform's window
  (meas_start..meas_stop) unless it carries its own (Seg.win, ticks), e.g. a full-segment capture.
- validate() checks every documented limit before anything is sent: KXCI segment time 20 ns..1 s [7-52],
  2048 segments / 512 sequences per channel [DS p.28; PMU p.4-11], loops 1..1e12 [7-56], seamless
  start = previous stop also across sequences [7-48], fixed-range RPM timing minimums [DS p.24],
  65,536 points per channel [7-2] and < 1e6 samples per A/D [PMU p.4-10].
- The current range is one value for every channel or a dict {channel: range} (:PMU:MEASURE:RANGE is per
  channel); the RPM timing minimums then come from the smallest range used.
- timeline() is the source of truth for timing: one row per measured window with its stress time and
  delay since the stress was removed (PMU timestamps are only a cross-check).
- PmuRunner sends the commands through the K4200 driver (status-byte check per command, :ERROR:LAST:GET
  after setup), polls :PMU:TEST:STATUS?, fetches data in <= 2048-point chunks [7-7] and always ends with
  the outputs off. While the test runs only :PMU:TEST:STATUS?, :PMU:DATA:COUNT? and :PMU:DATA:GET are
  sent, with a short VISA timeout: KXCI refuses anything else with -980 and never answers a refused query
  (seen live 2026-10-15). Errors are read after the test has finished.
Experiment waveforms are built in measure/stress_stair.py. The earlier bias_stress / pmu_diag / pmu_bringup
builders (pre-characterization, spot-read stress waveform, captures, zero-stress control) are archived with
their data in .ignore/k4200_stress_archive_20261016/.
Nothing here touches hardware at import time.
"""
import math
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from measure.instruments.k4200 import K4200Error, rpm_of_smu

TICK = 10e-9                     # SARB time resolution [7-52]
T_MIN_TICKS, T_MAX_TICKS = 2, 100_000_000     # 20 ns .. 1 s through KXCI [7-52]
MAX_SEGMENTS, MAX_SEQUENCES, MAX_ENTRIES = 2048, 512, 512
MAX_LOOPS = 10 ** 12
MAX_VALUES = 128                 # values per SARB command; more go in :ADD commands [7-40]
LIST_VALUES = 64                 # sequence-list values per command (no documented cap; kept small)
MAX_POINTS = 65536               # per channel [7-2]
MAX_SAMPLES = 1_000_000          # per A/D per test [PMU p.4-10]
KXCI_WINDOW_SAMPLES = 4096       # live 2026-10-15: KXCI keeps <= ~4096 samples over all spot-mean windows
# 1e6 / duration x window (samples per window KXCI's duration rate cut would leave): >= 1 is applied, 0.45 (20 s)
# and 0.80 (11.3 s, pmu_diag) were skipped and ran, 0.90 (10.0 s, stress_stair, 2026-10-16) was refused at
# EXECUTE with -951 'Cannot adjust PMU measure sample rate for SARB configuration'. Refuse [0.8, 1) untested.
KXCI_UNTESTED_SPW = 0.8
BASE_RATE = 200_000_000          # 200 MS/s; rates are 200e6/n [LPT p.6-44]
MIN_SLEW = 362.0                 # V/s on the 10 V range [LPT p.6-20]
# 4225-RPM fixed range, 10 V source range: (min pulse width, settling, min window, min transition) [DS p.24]
RPM_TIMING = {1e-7: (134e-6, 100e-6, 10e-6, 1e-6), 1e-6: (20.4e-6, 15e-6, 1.64e-6, 360e-9),
              1e-5: (8.36e-6, 6e-6, 1e-6, 360e-9), 1e-4: (1.04e-6, 750e-9, 130e-9, 40e-9),
              1e-3: (370e-9, 250e-9, 40e-9, 30e-9), 1e-2: (160e-9, 100e-9, 20e-9, 20e-9)}
ROLES = ('gate', 'drain', 'source')


class SarbError(ValueError):
    pass


def ticks(t, what='time'):
    """Seconds -> 10 ns ticks; refuses values off the 10 ns grid."""
    n = round(t / TICK)
    if abs(n * TICK - t) > 1e-6 * TICK + 1e-15:
        raise SarbError(f"{what} {t!r} s is not on the 10 ns grid")
    return int(n)


def pmu_channel(smu):
    """KXCI pulse channel of the RPM that carries this SMU: PMU1-1 = 1, PMU1-2 = 2, PMU2-1 = 3 [7-5]."""
    card, c = rpm_of_smu(smu)[3:].split('-')
    return 2 * (int(card) - 1) + int(c)


@dataclass
class Seg:
    n: int                       # duration in ticks
    v0: tuple                    # (gate, drain, source) at the start
    v1: tuple                    # ... at the end
    meas: bool = False
    level: int = -1              # index of the level within a read sequence
    read: str = ''               # read label of a measured segment (else the entry's)
    win: tuple = None            # own measure window (start, stop) in ticks, else the waveform's

    @property
    def t(self):
        return self.n * TICK

    @property
    def flat(self):
        return self.v0 == self.v1


@dataclass
class Seq:
    name: str
    kind: str                    # hold | ramp | unit | spot | sweep
    segs: list

    @property
    def n(self):
        return sum(s.n for s in self.segs)

    @property
    def n_meas(self):
        return sum(s.meas for s in self.segs)


@dataclass
class Waveform:
    name: str
    seqs: dict                   # seq id (1..512) -> Seq
    entries: list                # [(seq id, loops, info dict)]
    chans: tuple                 # KXCI channels of (gate, drain, source)
    meas_start: int              # window inside a measured segment, ticks from its start
    meas_stop: int
    vstress: float = None        # gate level counted as stress (None: no stress accounting)
    info: dict = field(default_factory=dict)
    meas_type: int = 1           # of every measured segment: 1 spot mean, 2 waveform capture [7-46]

    @property
    def duration_s(self):
        return sum(self.seqs[s].n * loops for s, loops, _ in self.entries) * TICK


def _v(x):
    return round(float(x) + 0.0, 6)


def staircase(levels, vd, anchor, edge, plateau, reads=None):
    """Read sequence that starts and ends at (anchor gate, 0 V drain, 0 V source): step to each gate level
    with the drain at vd, one measured plateau per level, then back. 3 + 2 (n - 1) segments.
    reads: optional read label per level."""
    a = (_v(anchor), 0.0, 0.0)
    pts = [(_v(g), _v(vd), 0.0) for g in levels]
    segs = [Seg(edge, a, pts[0])]
    for i, p in enumerate(pts):
        if i:
            segs.append(Seg(edge, pts[i - 1], p))
        segs.append(Seg(plateau, p, p, meas=True, level=i, read=reads[i] if reads else ''))
    segs.append(Seg(edge, pts[-1], a))
    return segs


def hold3(v, n_total):
    """Three flat segments (a sequence needs >= 3 [7-39]) at gate v, drain/source 0, n_total ticks."""
    p = (_v(v), 0.0, 0.0)
    a = n_total // 4
    return [Seg(a, p, p), Seg(a, p, p), Seg(n_total - 2 * a, p, p)]


def ramp3(v_from, v_to, edge, hold):
    a, b = (_v(v_from), 0.0, 0.0), (_v(v_to), 0.0, 0.0)
    return [Seg(edge, a, b), Seg(hold, b, b), Seg(hold, b, b)]


def levels(start, stop, step):
    n = int(round(abs(stop - start) / step))
    sgn = 1 if stop >= start else -1
    return [_v(start + sgn * i * step) for i in range(n + 1)]


def log_times(t_min, t_max, per_decade=(1, 2, 5)):
    """1-2-5 (or any mantissas) per decade from t_min to t_max inclusive."""
    out = set()
    for e in range(math.floor(math.log10(t_min)) - 1, math.ceil(math.log10(t_max)) + 1):
        for m in per_decade:
            t = float(f"{m * 10.0 ** e:.12g}")
            if t_min * (1 - 1e-9) <= t <= t_max * (1 + 1e-9):
                out.add(t)
    return sorted(out)


class _Acc:
    """Walks a sequence list: time, cumulative stress (time with the gate flat at vstress) and the end of
    the last stress segment. Loops of sequences without measurements are handled analytically."""

    def __init__(self, vstress):
        self.vs = None if vstress is None else _v(vstress)
        self.t = 0                    # ticks
        self.stress = 0               # ticks
        self.last_stress_end = None   # ticks

    def _is_stress(self, s):
        return self.vs is not None and s.flat and s.v0[0] == self.vs

    def seq_stress(self, seq):
        """(stress ticks per pass, end of the last stress segment within the pass or None)."""
        tot, end, t = 0, None, 0
        for s in seq.segs:
            t += s.n
            if self._is_stress(s):
                tot, end = tot + s.n, t
        return tot, end

    def advance(self, seq, loops):
        tot, end = self.seq_stress(seq)
        if tot:
            self.stress += tot * loops
            self.last_stress_end = self.t + (loops - 1) * seq.n + end
        self.t += seq.n * loops

    def segment(self, s):
        self.t += s.n
        if self._is_stress(s):
            self.stress += s.n
            self.last_stress_end = self.t


def timeline(wf, max_meas_loops=10000):
    """One row per measured window, in the order the PMU returns them."""
    acc, rows = _Acc(wf.vstress), []
    mid = (wf.meas_start + wf.meas_stop) / 2
    for i, (sid, loops, info) in enumerate(wf.entries):
        seq = wf.seqs[sid]
        if not seq.n_meas:
            acc.advance(seq, loops)
            continue
        if loops > max_meas_loops:
            raise SarbError(f"{seq.name}: {loops} loops of a measured sequence")
        for rep in range(loops):
            for s in seq.segs:
                if s.meas:
                    tc = acc.t + mid
                    rows.append(dict(entry=i, seq=seq.name, phase=info.get('phase', ''), point=info.get('point', -1),
                                     read=s.read or info.get('read', seq.kind),
                                     target_s=info.get('target_s', np.nan),
                                     rep=rep, level=s.level, Vg_set=s.v0[0], Vd_set=s.v0[1], Vs_set=s.v0[2],
                                     t_s=tc * TICK, t_stress_s=acc.stress * TICK,
                                     t_off_s=(tc - acc.last_stress_end) * TICK if acc.last_stress_end is not None
                                     else np.nan))
                acc.segment(s)
    return pd.DataFrame(rows)


def entry_times(wf):
    """[(start_s, end_s)] of every sequence-list entry."""
    out, t = [], 0
    for sid, loops, _ in wf.entries:
        n = wf.seqs[sid].n * loops
        out.append((t * TICK, (t + n) * TICK))
        t += n
    return out


def trace(wf, t0=0.0, t1=math.inf, collapse_loops=False, max_loops=5000):
    """Programmed waveform as piecewise-linear vertices (t, gate, drain, source) between t0 and t1 [s].
    collapse_loops draws a looped flat sequence as one segment (for whole-waveform overviews)."""
    T, V = [], []
    t = 0
    for sid, loops, _ in wf.entries:
        seq = wf.seqs[sid]
        start, end = t * TICK, (t + seq.n * loops) * TICK
        if end >= t0 and start <= t1:
            flat = all(s.flat for s in seq.segs) and seq.segs[0].v0 == seq.segs[-1].v1
            if collapse_loops and loops > 1 and flat:
                T += [start, end]; V += [seq.segs[0].v0, seq.segs[-1].v1]
            else:
                D = seq.n * TICK
                i0 = max(0, math.floor((t0 - start) / D))
                i1 = loops - 1 if t1 >= end else min(loops - 1, math.floor((t1 - start) / D))
                if i1 - i0 > max_loops:
                    raise SarbError(f"trace window covers {i1 - i0} loops of {seq.name}; narrow it or collapse")
                for k in range(i0, i1 + 1):
                    ts = t + k * seq.n
                    for s in seq.segs:
                        T += [ts * TICK, (ts + s.n) * TICK]; V += [s.v0, s.v1]
                        ts += s.n
        t += seq.n * loops
    V = np.array(V, dtype=float).reshape(-1, 3)
    return np.array(T), V[:, 0], V[:, 1], V[:, 2]


def renumber(wf):
    """Sequence ids 1..N in order, no gaps: KXCI refuses a gap at EXECUTE with -961 'Unknown sequence' on
    every channel even though the sequence list was accepted (seen live 2026-10-15 with only id 2 defined)."""
    ids = {old: new for new, old in enumerate(sorted(wf.seqs), 1)}
    wf.seqs = {ids[s]: q for s, q in wf.seqs.items()}
    wf.entries = [(ids[s], loops, info) for s, loops, info in wf.entries]
    return wf


# --- limits ---
def seg_window(wf, s):
    """(start, stop) ticks of a measured segment's window."""
    return s.win if s.win is not None else (wf.meas_start, wf.meas_stop)


def _windows(wf):
    """(shortest window in s, total measured time in s) over the sequence list."""
    used = [(wf.seqs[s], loops) for s, loops, _ in wf.entries]
    span = lambda g: (seg_window(wf, g)[1] - seg_window(wf, g)[0]) * TICK
    lens = [span(g) for q, _ in used for g in q.segs if g.meas]
    total = sum(loops * sum(span(g) for g in q.segs if g.meas) for q, loops in used)
    return (min(lens) if lens else (wf.meas_stop - wf.meas_start) * TICK), total


def budgets(wf, rate=None):
    used = {s for s, _, _ in wf.entries}
    win, measured = _windows(wf)
    points = sum(wf.seqs[s].n_meas * loops for s, loops, _ in wf.entries)
    b = dict(segments=sum(len(q.segs) for q in wf.seqs.values()), sequences=len(wf.seqs), entries=len(wf.entries),
             unused_sequences=sorted(set(wf.seqs) - used), points=points, window_s=win,
             measured_time_s=measured, duration_s=wf.duration_s)
    if rate:
        k = kxci_rate(wf, rate)
        b.update(sample_rate=rate, samples=measured * rate, samples_per_window=win * rate,
                 kxci_rate=k, kxci_samples_per_window=win * k)
    return b


def kxci_rate(wf, rate=BASE_RATE):
    """Sample rate KXCI V1.13 actually uses for a SARB test (it prints 'PMU measure sample rate was reduced
    to ...' on its console; there is no query). Empirical, live L0-L0c 2026-10-15, six tests:
    - samples over all windows <= ~4096: 50 windows -> 9.09 MS/s, 302 windows -> 1.504 MS/s;
    - samples over the whole duration < 1e6, applied only if that still gives >= 1 sample per window:
      1 s -> 995 kS/s, 5 s -> 199.8 kS/s, 20 s -> not applied.
    The lowest of these, rounded down to 200 MS/s / n."""
    win, measured = _windows(wf)
    points = sum(wf.seqs[s].n_meas * loops for s, loops, _ in wf.entries)
    target = rate
    if points:
        target = min(target, KXCI_WINDOW_SAMPLES / measured)
        by_duration = MAX_SAMPLES / wf.duration_s
        if by_duration * win >= 1:
            target = min(target, by_duration)
    n = max(1, -(-BASE_RATE // int(target)))
    while BASE_RATE / n > target:
        n += 1
    return BASE_RATE / n


def choose_rate(wf, max_rate=BASE_RATE, margin=0.95):
    """Highest 200 MS/s / n (integer, <= max_rate) that keeps the samples per A/D under the cap."""
    b = budgets(wf)
    for n in range(1, 200001):
        if BASE_RATE % n:
            continue
        r = BASE_RATE // n
        if r > max_rate:
            continue
        if b['window_s'] * r < 1:
            break
        if b['measured_time_s'] * r < margin * MAX_SAMPLES:
            return int(r)
    raise SarbError(f"no sample rate fits {b['points']} windows of {b['window_s']:g} s under "
                    f"{MAX_SAMPLES} samples with >= 1 sample per window")


def channel_ranges(wf, irange):
    """irange (one value, or {channel: range} covering every waveform channel) -> {channel: range}."""
    if isinstance(irange, dict):
        missing = [ch for ch in wf.chans if ch not in irange]
        if missing:
            raise SarbError(f"{wf.name}: no current range for channel(s) {missing}")
        return {ch: float(irange[ch]) for ch in wf.chans}
    return {ch: float(irange) for ch in wf.chans}


def validate(wf, irange, vrange=10.0, rate=None, min_kxci_spw=1.0):
    """Raise SarbError listing every broken rule; return budgets() on success. With per-channel ranges the
    timing minimums are those of the smallest range used."""
    errs = []
    rngs = channel_ranges(wf, irange)
    irange = min(rngs.values())
    bad = sorted({r for r in rngs.values() if r not in RPM_TIMING})
    if bad:
        errs.append(f"current range(s) {', '.join(f'{r:g}' for r in bad)} A not fixed 4225-RPM ranges "
                    f"{sorted(RPM_TIMING)}")
        min_pw = settle = min_win = min_edge = 0.0
    else:
        min_pw, settle, min_win, min_edge = RPM_TIMING[irange]
    if len(wf.seqs) > MAX_SEQUENCES or any(not 1 <= s <= MAX_SEQUENCES for s in wf.seqs):
        errs.append(f"{len(wf.seqs)} sequences or an id outside 1..{MAX_SEQUENCES}")
    elif sorted(wf.seqs) != list(range(1, len(wf.seqs) + 1)):
        errs.append(f"sequence ids {sorted(wf.seqs)} are not 1..{len(wf.seqs)} without gaps (KXCI -961; renumber())")
    nseg = sum(len(q.segs) for q in wf.seqs.values())
    if nseg > MAX_SEGMENTS:
        errs.append(f"{nseg} segments per channel > {MAX_SEGMENTS}")
    if len(wf.entries) > MAX_ENTRIES:
        errs.append(f"{len(wf.entries)} sequence-list entries > {MAX_ENTRIES}")
    zero = (0.0,) * len(wf.chans)
    for ms, me in sorted({seg_window(wf, g) for q in wf.seqs.values() for g in q.segs if g.meas}):
        if not 0 <= ms < me:
            errs.append(f"measure window {ms * TICK:g}..{me * TICK:g} s is empty")
        if me - ms < min_win / TICK - 1e-9:
            errs.append(f"measure window {(me - ms) * TICK:g} s < {min_win:g} s minimum for {irange:g} A")
        if wf.meas_type == 1 and ms < settle / TICK - 1e-9:      # a capture may start at the edge on purpose
            errs.append(f"measure start {ms * TICK:g} s < settling time {settle:g} s for {irange:g} A")
    for sid, q in wf.seqs.items():
        if len(q.segs) < 3:
            errs.append(f"{q.name}: {len(q.segs)} segments (a sequence needs >= 3)")
        for j, s in enumerate(q.segs):
            where = f"{q.name} segment {j}"
            if not isinstance(s.n, int) or not T_MIN_TICKS <= s.n <= T_MAX_TICKS:
                errs.append(f"{where}: {s.n * TICK:g} s outside 20 ns..1 s")
            if len(s.v0) != len(wf.chans) or len(s.v1) != len(wf.chans):
                errs.append(f"{where}: {len(s.v0)}/{len(s.v1)} voltages for {len(wf.chans)} channels")
            if any(abs(v) > vrange for v in s.v0 + s.v1):
                errs.append(f"{where}: |V| > {vrange:g} V")
            if j and q.segs[j - 1].v1 != s.v0:
                errs.append(f"{where}: start {s.v0} != previous stop {q.segs[j - 1].v1}")
            if s.meas and wf.meas_type == 1:            # spot mean: a settled plateau
                if not s.flat:
                    errs.append(f"{where}: measured segment is not flat")
                if s.n < min_pw / TICK - 1e-9:
                    errs.append(f"{where}: plateau {s.t:g} s < {min_pw:g} s minimum for {irange:g} A")
            if s.meas and seg_window(wf, s)[1] > s.n:
                errs.append(f"{where}: measure stop {seg_window(wf, s)[1] * TICK:g} s beyond the segment "
                            f"({s.t:g} s)")
            if not s.flat and not (s.meas and wf.meas_type == 1):
                dv = max(abs(a - b) for a, b in zip(s.v0, s.v1))
                if s.n < min_edge / TICK - 1e-9:
                    errs.append(f"{where}: transition {s.t:g} s < {min_edge:g} s minimum for {irange:g} A")
                if dv / s.t < MIN_SLEW:
                    errs.append(f"{where}: slew {dv / s.t:.3g} V/s below {MIN_SLEW:g} V/s")
    prev = zero
    for i, (sid, loops, _) in enumerate(wf.entries):
        if sid not in wf.seqs:
            errs.append(f"entry {i}: unknown sequence {sid}")
            continue
        q = wf.seqs[sid]
        if not isinstance(loops, int) or not 1 <= loops <= MAX_LOOPS:
            errs.append(f"entry {i} ({q.name}): loops {loops!r} outside 1..1e12")
        if q.segs[0].v0 != prev:
            errs.append(f"entry {i} ({q.name}): starts at {q.segs[0].v0} but the previous entry ends at {prev}")
        if loops > 1 and q.segs[-1].v1 != q.segs[0].v0:
            errs.append(f"entry {i} ({q.name}): looped but ends {q.segs[-1].v1} != start {q.segs[0].v0}")
        prev = q.segs[-1].v1
    if wf.entries and prev != zero:
        errs.append(f"waveform ends at {prev}, not 0 V")
    b = budgets(wf, rate)
    if b['points'] > MAX_POINTS:
        errs.append(f"{b['points']} points per channel > {MAX_POINTS}")
    if rate:
        if b['samples'] >= MAX_SAMPLES:
            errs.append(f"{b['samples']:.3g} samples per A/D >= {MAX_SAMPLES} at {rate:g} S/s")
        if b['samples_per_window'] < 1:
            errs.append(f"{b['samples_per_window']:.3g} samples per window at {rate:g} S/s")
        by_dur = MAX_SAMPLES / wf.duration_s * b['window_s'] if b['points'] else 0.0
        if KXCI_UNTESTED_SPW <= by_dur < 1:
            errs.append(f"1e6 samples over {wf.duration_s:.4g} s would leave {by_dur:.2f} samples per window: KXCI "
                        f"refused 0.90 with -951 (2026-10-16); keep this below {KXCI_UNTESTED_SPW:g} (longer "
                        f"waveform) or >= 1 (shorter)")
        if b['kxci_samples_per_window'] < min_kxci_spw:
            errs.append(f"KXCI will lower the rate to {b['kxci_rate']:.4g} S/s ({b['kxci_samples_per_window']:.2g} "
                        f"samples per window < {min_kxci_spw:g}; see kxci_rate(): use fewer windows, or avoid "
                        f"1-9 s waveforms)")
    if errs:
        raise SarbError(f"{wf.name}: " + '; '.join(errs))
    return b


# --- KXCI commands ---
def _fmt(x):
    s = f"{x:.9g}"
    if len(s) > 12:
        raise SarbError(f"value {x!r} needs more than 12 characters")
    return s


def _chunked(head, ch, seq, vals):
    out = []
    for i in range(0, len(vals), MAX_VALUES):
        h = head if i == 0 else head + ':ADD'
        out.append(f"{h} {ch}, {seq}, " + ', '.join(vals[i:i + MAX_VALUES]))
    return out


def commands(wf, irange, vrange=10, load_ohm=1e6, rate=BASE_RATE, offset_comp=False):
    """Everything after :PMU:INIT 1 and the RPM switch, up to (not including) OUTPUT:STATE and EXECUTE."""
    cmds, rngs = [], channel_ranges(wf, irange)
    for ch in wf.chans:
        cmds += [f":PMU:SOURCE:RANGE {ch}, {vrange:g}", f":PMU:MEASURE:RANGE {ch}, 2, {rngs[ch]:g}",
                 f":PMU:LOAD {ch}, {load_ohm:g}"]
    cmds.append(f":PMU:SAMPLE:RATE {int(rate)}")
    if offset_comp:      # type 3 = offset current, addressed by the lowest channel of each card [7-5]
        for c in sorted({2 * ((ch - 1) // 2) + 1 for ch in wf.chans}):
            cmds.append(f":PMU:CONNECTION:COMP {c}, 3, 1")
    for sid in sorted(wf.seqs):
        segs = wf.seqs[sid].segs
        ms = [_fmt(seg_window(wf, s)[0] * TICK) if s.meas else '0' for s in segs]
        me = [_fmt(seg_window(wf, s)[1] * TICK) if s.meas else '0' for s in segs]
        for r, ch in enumerate(wf.chans):
            arrays = (('STARTV', [_fmt(s.v0[r]) for s in segs]), ('STOPV', [_fmt(s.v1[r]) for s in segs]),
                      ('TIME', [_fmt(s.t) for s in segs]), ('MEAS:TYPE', [str(wf.meas_type) if s.meas else '0' for s in segs]),
                      ('MEAS:START', ms), ('MEAS:STOP', me), ('TRIG', ['1'] * len(segs)))
            for key, vals in arrays:
                cmds += _chunked(f":PMU:SARB:SEQ:{key}", ch, sid, vals)
    flat = [str(int(x)) for s, loops, _ in wf.entries for x in (s, loops)]
    for ch in wf.chans:
        for i in range(0, len(flat), LIST_VALUES):
            h = ":PMU:SARB:WFM:SEQ:LIST" if i == 0 else ":PMU:SARB:WFM:SEQ:LIST:ADD"
            cmds.append(f"{h} {ch}, " + ', '.join(flat[i:i + LIST_VALUES]))
    return cmds


# --- data ---
def parse_point(s):
    """One :PMU:DATA:GET point -> (V, I, T, status). The SARB spot-mean layout is not documented; the
    manual's pulse-train example returns 'V,I,T,status' [7-38], so the first four fields are read."""
    f = [x.strip() for x in s.split(',')]
    try:
        return float(f[0]), float(f[1]), float(f[2]) if len(f) > 2 else np.nan, f[3] if len(f) > 3 else ''
    except (ValueError, IndexError):
        raise K4200Error(f"Unparseable :PMU:DATA:GET point {s!r}")


def status_overflow(code):
    """8-digit PMU status ABCDEFGH [PMU p.3-13]: F is the overflow digit (1 -V, 2 +V, 4 -I, 8 +I, sums).
    Live 2026-10-15: '01010021' = RPM, spot mean, no overflow, 10 uA range, channel 1 at 10 V."""
    s = str(code).strip()
    return len(s) == 8 and s[5] != '0'


def merge(tl, data, chans, irange):
    """Timeline rows + the raw points of each channel (gate, drain, source order) -> one DataFrame.
    Keeps only the rows every channel has data for (an interrupted run returns fewer)."""
    n = min([len(tl)] + [len(data.get(ch, [])) for ch in chans])
    out = tl.iloc[:n].copy()
    for role, ch in zip(ROLES, chans):
        p = [parse_point(s) for s in data[ch][:n]]
        cap = role.capitalize()
        out[f'{cap}_Voltage_meas'] = [x[0] for x in p]
        out[f'{cap}_Current'] = [x[1] for x in p]
        out[f'{cap}_Time'] = [x[2] for x in p]
        out[f'{cap}_Status'] = [x[3] for x in p]
    rng = dict(zip(ROLES, chans))
    for role in ('Drain', 'Source'):
        r = irange[rng[role.lower()]] if isinstance(irange, dict) else irange
        out[f'{role}_Overflow'] = (np.abs(out[f'{role}_Current']) >= r) | \
            out[f'{role}_Status'].map(status_overflow)
    return out


class PmuRunner:
    """Run one SARB waveform on a K4200 driver (rpm_cmd='pmu'). Returns {channel: [raw point strings]}."""

    def __init__(self, k, smus, log=print, poll_s=1.0, fetch_every_s=10.0, timeout_margin_s=600.0, get_chunk=2048,
                 run_timeout_ms=5000, sleep=time.sleep):
        self.k, self.smus, self.log = k, tuple(smus), log
        self.poll_s, self.fetch_every_s, self.margin = poll_s, fetch_every_s, timeout_margin_s
        self.get_chunk, self.run_timeout_ms, self.sleep = min(int(get_chunk), 2048), run_timeout_ms, sleep
        self.data, self.raw_log = {}, None
        self.exec_s = self.mid_run_fetch = None       # EXECUTE -> idle time; did a fetch during the run work
        self.polls = []                               # (s after EXECUTE when sent, s the query took, reply)

    def run(self, wf, irange, vrange=10, load_ohm=1e6, rate=BASE_RATE, offset_comp=False, on_progress=None,
            raw_log=None):
        k = self.k
        if not k.rpms:
            raise K4200Error(f"*OPT? lists no RPMs ({k.opt}); this setup pulses through the 4225-RPMs")
        if getattr(k, 'rpm_cmd', 'RP') != 'pmu':
            raise K4200Error("PMU tests need k4200.rpm_cmd = 'pmu': the legacy RP command cannot be mixed with "
                             ":PMU: commands [KXCI p.7-1]")
        if tuple(pmu_channel(s) for s in self.smus) != tuple(wf.chans):
            raise K4200Error(f"waveform channels {wf.chans} do not match the SMUs {self.smus}")
        validate(wf, irange, vrange, rate)
        cmds = commands(wf, irange, vrange, load_ohm, rate, offset_comp)
        self.raw_log = raw_log
        self.data = {ch: [] for ch in wf.chans}
        expected = len(timeline(wf))
        k.clear_error()
        k.write(':PMU:INIT 1')
        k.set_rpm(self.smus, 0, force=True)
        for c in cmds:
            k.write(c)
        k.check_errors(f'PMU setup ({wf.name})')
        self.log(f"PMU {wf.name}: {len(cmds)} commands sent, {expected} {'capture windows' if wf.meas_type == 2 else 'points'} per channel, "
                 f"{wf.duration_s:.4g} s")
        ok, old_timeout = False, k.res.timeout
        self.exec_s = self.mid_run_fetch = None
        self.polls = []
        try:
            for ch in wf.chans:
                k.pmu_on.add(ch)
                k.write(f':PMU:OUTPUT:STATE {ch}, 1')
            k.pmu_running = True
            k.res.timeout = self.run_timeout_ms
            k.write(':PMU:EXECUTE')
            t0 = last = time.time()
            deadline = wf.duration_s + self.margin
            while True:
                self.sleep(self.poll_s)
                tq = time.time()
                reply = k.query(':PMU:TEST:STATUS?')
                now = time.time()
                self.polls.append((round(tq - t0, 4), round(now - tq, 4), reply))
                if not int(float(reply)):
                    break
                if self.mid_run_fetch is not False and now - last >= self.fetch_every_s:
                    try:
                        self.fetch()
                        self.mid_run_fetch = True
                    except Exception as e:      # keep the stress running; fetch after the end instead
                        self.mid_run_fetch = False
                        self.log(f"PMU {wf.name}: data read during the test failed ({type(e).__name__}: {e}); "
                                 "no more reads until the test ends")
                    last = now
                    if on_progress and self.mid_run_fetch:
                        on_progress(self.data)
                if now - t0 > deadline:
                    raise K4200Error(f"PMU test still running after {now - t0:.0f} s "
                                     f"(waveform {wf.duration_s:.4g} s); aborted")
            self.exec_s = time.time() - t0
            k.pmu_running = False
            k.res.timeout = old_timeout
            k.check_errors(f'PMU test ({wf.name})')
            self.fetch()
            ok = True
        finally:
            k.res.timeout = old_timeout
            problems = k.pmu_off()
            if ok and problems:
                raise K4200Error(f"PMU {wf.name}: turning the outputs off had problems: {problems}")
            if on_progress:
                try:
                    on_progress(self.data)
                except Exception as e:
                    self.log(f"saving partial PMU data failed: {type(e).__name__}: {e}")
        k.check_errors(f'PMU run / data read ({wf.name})')
        got = {ch: len(v) for ch, v in self.data.items()}
        if wf.meas_type == 2:          # waveform capture: samples, the same on every channel
            uniform = len({seg_window(wf, g) for q in wf.seqs.values() for g in q.segs if g.meas}) == 1
            if len(set(got.values())) != 1 or not got[wf.chans[0]] or (uniform and got[wf.chans[0]] % expected):
                raise K4200Error(f"PMU {wf.name}: {got} samples per channel for {expected} capture windows")
        elif any(n != expected for n in got.values()):
            raise K4200Error(f"PMU {wf.name}: expected {expected} points per channel, got {got}")
        return self.data

    def fetch(self):
        k = self.k
        for ch, pts in self.data.items():
            n = int(float(k.query(f':PMU:DATA:COUNT? {ch}')))
            while len(pts) < n:
                start = len(pts)
                reply = k.query(f':PMU:DATA:GET {ch}, {start}, {min(self.get_chunk, n - start)}')
                new = [p for p in reply.strip().split(';') if p.strip()]
                if self.raw_log:
                    with open(self.raw_log, 'a', encoding='utf-8') as f:
                        f.write(f"{ch}\t{start}\t{reply}\n")
                if not new:
                    break
                pts += new
