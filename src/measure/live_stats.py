"""How much unattended probing the live runs did: runs, hours, devices and emails, from the run logs and summaries.

    python -m measure.live_stats data data_k4200 data_k4200_stress

Each folder is walked for `run_YYYYMMDD_HHMMSS.log` and `summary_YYYYMMDD_HHMMSS.csv`. Dry runs (`*_dryrun.*`) and
logs whose first line does not say `Mode: LIVE` are not counted.
- Active hours: from a log's first timestamp to its last, minus the time spent waiting at the two typed
  confirmations (measure.run_map.run): the LIVE RUN gate after the plan, and the alignment check before the first
  contact. Neither gate is logged, so each wait is read as a gap. The first gap runs from the last plan line to the
  next line. The second runs from the line before the first `Chuck -> contact height` to that line, which also
  counts a few seconds of chuck motion. A gap is taken only where the log really shows it.
- Devices: distinct (summary folder, device key) pairs. Device visits: summary rows (resumes and remeasures count
  again).
- Emails: the `Email sent:` lines, by outcome, and the alerts after N failures in a row.
- With --drift, one line per run that moved the chuck (measure.instruments.prober), showing the S200 chuck's
  placement, not the devices:
  - moves, and the drift corrections and extra fix moves the run made;
  - the largest single move error;
  - "uncorrected": the largest |running sum of the move errors|, which is the drift the run would have had
    without correction;
  - "held": the largest logged accumulated drift, which is what the probes actually saw.
Only counts, times and chuck positions are printed, never a device's measured value or status. Nothing is written.
"""
import argparse
import csv
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

LOG_RE = re.compile(r'^run_\d{8}_\d{6}\.log$')
SUMMARY_RE = re.compile(r'^summary_\d{8}_\d{6}\.csv$')
LINE_RE = re.compile(r'^\[(\d\d):(\d\d):(\d\d)\] (.*)$')
OUTCOMES = ('DONE', 'INTERRUPTED', 'STOPPED', 'ERROR', 'AUTO-STOPPED')
EMAIL_END_RE = re.compile(r'^Email sent: \[probe\] .*: (' + '|'.join(OUTCOMES) + r'), \d+/\d+ devices')
EMAIL_ALERT_RE = re.compile(r'^Email sent: \[probe\] .*: \d+ failures in a row')
HEADER_PREFIXES = ('Mode:', 'Planned devices:', 'Resume:')
_NUM = r'([-+]?\d+(?:\.\d+)?)'
MOVE_ERROR_RE = re.compile(rf'move error \({_NUM}, {_NUM}\) um')
DRIFT_RE = re.compile(rf'accumulated drift \({_NUM}, {_NUM}\) um')


@dataclass
class RunDrift:
    """The chuck placement of one live run, in um (module docstring, --drift)."""
    log: str
    moves: int = 0
    corrections: int = 0
    fix_moves: int = 0
    max_move_error: float = 0.0
    uncorrected: float = 0.0
    held: float = 0.0


@dataclass
class RunTimes:
    """One live run log: its span and the time spent waiting at the confirmations, in seconds."""
    span_s: float
    idle_s: float

    @property
    def active_s(self) -> float:
        return self.span_s - self.idle_s


@dataclass
class FolderStats:
    """The totals for one folder (or for all of them)."""
    name: str
    runs: list[RunTimes] = field(default_factory=list)
    devices: set[tuple[str, str]] = field(default_factory=set)
    visits: int = 0
    emails: Counter = field(default_factory=Counter)
    drifts: list[RunDrift] = field(default_factory=list)

    def add(self, other: 'FolderStats') -> None:
        """Fold another folder's totals into this one."""
        self.runs += other.runs
        self.drifts += other.drifts
        self.devices |= other.devices
        self.visits += other.visits
        self.emails += other.emails


def _timed_lines(text: str) -> list[tuple[float, str]]:
    """(seconds since the log's first midnight, message) per timestamped line; a time that goes back is a new day."""
    out, day, prev = [], 0, None
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        t = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
        if prev is not None and t < prev:
            day += 1
        prev = t
        out.append((day * 86400 + t, m[4]))
    return out


def _is_header(msg: str) -> bool:
    """A line of the plan block that precedes the LIVE RUN gate (the plan rows are indented)."""
    return msg.startswith(HEADER_PREFIXES) or msg.startswith('  ')


def run_times(lines: list[tuple[float, str]]) -> RunTimes:
    """The span of one log and the waits at its two confirmations (module docstring)."""
    if not lines:
        return RunTimes(0.0, 0.0)
    span = lines[-1][0] - lines[0][0]
    idle = 0.0
    n_header = next((i for i, (_, msg) in enumerate(lines) if not _is_header(msg)), len(lines))
    if 0 < n_header < len(lines):
        idle += lines[n_header][0] - lines[n_header - 1][0]
    contact = next((i for i, (_, msg) in enumerate(lines) if msg.startswith('Chuck -> contact height')), None)
    if contact is not None and contact > n_header:
        idle += lines[contact][0] - lines[contact - 1][0]
    return RunTimes(float(span), float(idle))


def run_drift(log: str, lines: list[tuple[float, str]]) -> RunDrift:
    """The chuck placement of one log: moves, corrections, and the uncorrected against the held drift."""
    d = RunDrift(log)
    sx = sy = 0.0
    for _, msg in lines:
        if msg.startswith('Drift correction:'):
            d.corrections += 1
        elif msg.startswith('Drift beyond'):
            d.fix_moves += 1
        if m := MOVE_ERROR_RE.search(msg):
            ex, ey = float(m[1]), float(m[2])
            d.moves += 1
            sx, sy = sx + ex, sy + ey
            d.max_move_error = max(d.max_move_error, math.hypot(ex, ey))
            d.uncorrected = max(d.uncorrected, math.hypot(sx, sy))
        if m := DRIFT_RE.search(msg):
            d.held = max(d.held, math.hypot(float(m[1]), float(m[2])))
    return d


def folder_stats(root: Path) -> FolderStats:
    """Walk one folder; raises FileNotFoundError when it does not exist."""
    if not root.is_dir():
        raise FileNotFoundError(f'not a folder: {root}')
    stats = FolderStats(root.as_posix())
    for log in sorted(p for p in root.rglob('run_*.log') if LOG_RE.match(p.name)):
        text = log.read_text(encoding='utf-8', errors='replace')
        lines = _timed_lines(text)
        if not lines or not lines[0][1].startswith('Mode: LIVE'):
            continue
        stats.runs.append(run_times(lines))
        drift = run_drift(log.as_posix(), lines)
        if drift.moves:
            stats.drifts.append(drift)
        for _, msg in lines:
            if m := EMAIL_END_RE.match(msg):
                stats.emails[m[1]] += 1
            elif EMAIL_ALERT_RE.match(msg):
                stats.emails['ALERT'] += 1
    for summary in sorted(p for p in root.rglob('summary_*.csv') if SUMMARY_RE.match(p.name)):
        with summary.open(newline='', encoding='utf-8', errors='replace') as f:
            for row in csv.DictReader(f):
                if row.get('key'):
                    stats.visits += 1
                    stats.devices.add((summary.parent.as_posix(), row['key']))
    return stats


def _h(seconds: float) -> str:
    return f'{seconds / 3600:.1f}'


def _format(head: tuple[str, ...], body: list[tuple[str, ...]], left: tuple[int, ...]) -> str:
    """Columns padded to their widest cell; the columns in `left` are left-aligned, the rest right-aligned."""
    widths = [max(len(r[i]) for r in (head, *body)) for i in range(len(head))]
    fmt = lambda r: '  '.join(c.ljust(w) if i in left else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths)))
    return '\n'.join(fmt(r).rstrip() for r in (head, *body))


def table(rows: list[FolderStats]) -> str:
    """The report: one line per folder and a total line."""
    head = ('folder', 'runs', 'active h', 'idle h', 'longest h', 'devices', 'visits', 'emails')
    body = []
    for s in rows:
        emails = ' '.join(f'{k.lower()} {s.emails[k]}' for k in (*OUTCOMES, 'ALERT') if s.emails[k]) or '-'
        body.append((s.name, str(len(s.runs)), _h(sum(r.active_s for r in s.runs)),
                     _h(sum(r.idle_s for r in s.runs)), _h(max((r.active_s for r in s.runs), default=0)),
                     str(len(s.devices)), str(s.visits), emails))
    return _format(head, body, left=(0, 7))


def drift_table(drifts: list[RunDrift]) -> str:
    """One line per run that moved, the largest uncorrected drift first, and a total line."""
    head = ('run', 'moves', 'corrections', 'fix moves', 'max move error um', 'uncorrected um', 'held um')
    rows = sorted(drifts, key=lambda d: d.uncorrected, reverse=True)
    body = [(d.log, str(d.moves), str(d.corrections), str(d.fix_moves), f'{d.max_move_error:.2f}',
             f'{d.uncorrected:.2f}', f'{d.held:.2f}') for d in rows]
    body.append((f'total ({len(rows)} runs)', str(sum(d.moves for d in rows)),
                 str(sum(d.corrections for d in rows)), str(sum(d.fix_moves for d in rows)),
                 f'{max((d.max_move_error for d in rows), default=0):.2f}',
                 f'{max((d.uncorrected for d in rows), default=0):.2f}', f'{max((d.held for d in rows), default=0):.2f}'))
    return _format(head, body, left=(0,))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; prints the report and returns the exit code."""
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('folders', nargs='+', type=Path, help='data folders to walk (read only)')
    ap.add_argument('--drift', action='store_true', help='also one line per run on the chuck placement')
    args = ap.parse_args(argv)
    try:
        per = [folder_stats(f) for f in args.folders]
    except FileNotFoundError as e:
        print(f'live_stats: {e}', file=sys.stderr)
        return 2
    total = FolderStats('total')
    for s in per:
        total.add(s)
    print(table(per + [total] if len(per) > 1 else per))
    if args.drift:
        print()
        print(drift_table(total.drifts))
    return 0


if __name__ == '__main__':
    sys.exit(main())
