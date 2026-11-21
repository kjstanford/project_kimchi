"""measure.live_stats counts live runs, active hours, devices and emails correctly from synthetic logs."""

from pathlib import Path

import pytest

from measure import live_stats

HEADER = "time,sample,die_idx,key,status,reason\n"

# A live run from 23:50:00 to 00:20:00 (30 min). It waits 5 min at the LIVE RUN gate (23:50:10 -> 23:55:10)
# and 2 min at the alignment check (23:55:30 -> 23:57:30), so 23 min are active.
LIVE_LOG = """\
[23:50:00] Mode: LIVE | SMU tool b1500 | experiment idvg | origin A:1
[23:50:00] Resume: skipping 3 already-measured devices
[23:50:10] Planned devices: 2
[23:50:10]   A:1  target (     0.0,      0.0)  step (    0.0,     0.0) um
[23:55:10] B1500 modules: {3: 'HRSMU'}
[23:55:10] === [1/2] A:1  (layout x=0, y=0)
[23:55:30] Chuck -> separation height
[23:57:30] Chuck -> contact height
[23:58:00] A:1: measured
[23:58:00] Email sent: [probe] s 0_0: 5 failures in a row, at A:1 (1/2)
[00:19:00] B:1: measured
[00:20:00] Email sent: [probe] s 0_0: INTERRUPTED, 2/2 devices (2 measured)
"""

# The log of a run that never reached a device: everything after the plan is the wait at the gate.
ABORTED_LOG = """\
[10:00:00] Mode: LIVE | origin A:1
[10:00:00] Planned devices: 1
[10:00:00]   A:1  target (     0.0,      0.0)  step (    0.0,     0.0) um
[10:10:00] Aborted by user before connecting.
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    die = root / "s" / "0_0"
    _write(die / "run_20261101_235000.log", LIVE_LOG)
    _write(die / "run_20261102_100000.log", ABORTED_LOG)
    # a dry run and a log that is not LIVE: neither counts
    _write(die / "run_20261101_120000_dryrun.log", LIVE_LOG)
    _write(die / "run_20261101_130000.log", LIVE_LOG.replace("Mode: LIVE", "Mode: DRY-RUN"))
    # A:1 twice (a remeasure) and a quoted reason with a comma: 2 devices, 3 visits
    _write(
        die / "summary_20261101_235000.csv",
        HEADER + "2026-11-01 23:58:00,s,0_0,A:1,measured,\n"
        '2026-11-02 00:19:00,s,0_0,B:1,screened_out,"Vt out of range [-3.5, 2.5] V"\n',
    )
    _write(die / "summary_20261102_110000.csv", HEADER + "2026-11-02 11:00:00,s,0_0,A:1,measured,\n")
    _write(die / "summary_20261101_120000_dryrun.csv", HEADER + "2026-11-01 12:00:00,s,0_0,Z:9,measured,\n")
    return root


def test_active_time_excludes_both_confirmation_waits_and_crosses_midnight():
    t = live_stats.run_times(live_stats._timed_lines(LIVE_LOG))
    assert t.span_s == 30 * 60
    assert t.idle_s == (5 + 2) * 60
    assert t.active_s == 23 * 60


def test_a_run_aborted_at_the_gate_has_no_active_time():
    t = live_stats.run_times(live_stats._timed_lines(ABORTED_LOG))
    assert (t.span_s, t.idle_s, t.active_s) == (600, 600, 0)


def test_folder_counts_only_live_runs_and_distinct_devices(tree: Path):
    s = live_stats.folder_stats(tree)
    assert len(s.runs) == 2
    assert sum(r.active_s for r in s.runs) == 23 * 60
    assert len(s.devices) == 2
    assert s.visits == 3
    assert s.emails == {"INTERRUPTED": 1, "ALERT": 1}


def test_main_prints_a_total_and_no_status(tree: Path, tmp_path: Path, capsys):
    other = tmp_path / "data_k4200"
    _write(other / "d" / "run_20261103_090000.log", ABORTED_LOG)
    assert live_stats.main([str(tree), str(other)]) == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert lines[0].split()[:2] == ["folder", "runs"]
    assert lines[-1].split()[:2] == ["total", "3"]
    assert "measured" not in out and "screened" not in out
    assert "interrupted 1 alert 1" in out


def test_main_refuses_a_missing_folder(tmp_path: Path, capsys):
    assert live_stats.main([str(tmp_path / "nope")]) == 2
    assert "not a folder" in capsys.readouterr().err
