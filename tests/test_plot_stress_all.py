"""measure.plot_stress_all on a synthetic stress die: which Vt table each run uses, which runs count as pre-stressed,
and what is drawn (dVt in mV against stress time and recovery time)."""

import json
import shutil
from pathlib import Path

import matplotlib.axes
import pandas as pd
import pytest

from measure import plot_stress_all as ps

TMP = Path(".ignore/_stress_test")  # never inside data/ (AGENTS.md)
DC = {"vstress": 3.0, "stress_mode": "dc", "ac_period_s": 0.002, "vt_id_per_wl": 1e-8, "vt_id_min_a": 2e-8}
AC = {**DC, "stress_mode": "ac", "ac_period_s": 2e-5}


def _vt_table(dvt_mv: float) -> pd.DataFrame:
    """Reference read at t = 0, two stress reads, two recovery reads; dVt = dvt_mv * (1, 2) then (0.5, 0.25)."""
    d = dvt_mv * 1e-3
    return pd.DataFrame(
        {
            "phase": ["read", "read", "read", "recovery", "recovery"],
            "t_stress_s": [0.0, 0.001, 1.0, None, None],
            "t_rec_s": [None, None, None, 0.001, 1.0],
            "Vt_source_V": [1.5, 1.5 + d, 1.5 + 2 * d, 1.5 + d / 2, 1.5 + d / 4],
            "dVt_source_V": [0.0, d, 2 * d, d / 2, d / 4],
        }
    )


def _run(dev: str, stamp: str, w: float, l_um: float, params: dict, vt: str | None, dvt_mv: float = 10.0):
    """Writes a meta JSON and, by vt = 'plain' / 'Iref<x>' / None, the run's Vt table."""
    folder = TMP / dev
    folder.mkdir(parents=True, exist_ok=True)
    desc = f"{stamp}_s_0_0_{dev}_W{w:g}_L{l_um:g}"
    meta = {"W_um": w, "L_um": l_um, "params": params, "started": stamp}
    (folder / f"PMU_stair_meta_{desc}.json").write_text(json.dumps(meta))
    if vt == "plain":
        _vt_table(dvt_mv).to_csv(folder / f"PMU_stair_vt_{desc}.csv", index=False)
    elif vt:
        _vt_table(dvt_mv).to_csv(folder / f"PMU_stair_vt_{desc}_reanalysis_{vt}.csv", index=False)


@pytest.fixture
def die():
    shutil.rmtree(TMP, ignore_errors=True)
    _run("D1", "20260101_000000", 4, 1, DC, "plain", dvt_mv=10)  # fresh
    _run("D2", "20260101_000000", 4, 2, AC, "plain", dvt_mv=20)  # fresh, AC
    _run("D3", "20260101_000000", 4, 1, DC, "plain")  # two runs on one device: both repeats
    _run("D3", "20260102_000000", 4, 1, DC, "Iref4e-8")  # only a re-analysis, at this run's 1e-8 * W/L
    _run("D4", "20260101_000000", 4, 1, DC, "plain")  # one run, but flagged already stressed
    (TMP / "D4" / "WARNING_D4_already_stressed.txt").write_text("x")
    _run("D5", "20260101_000000", 4, 1, DC, "Iref6e-8")  # re-analysis at another current only: skipped
    _run("D6", "20260101_000000", 4, 1, DC, None)  # no Vt table at all: skipped
    yield TMP
    shutil.rmtree(TMP, ignore_errors=True)


def test_mode_labels():
    assert ps.mode_label(DC) == "DC 3 V"
    assert ps.mode_label(AC) == "AC 3 V, 20 µs period"
    assert ps.mode_label({**AC, "ac_period_s": 0.002}) == "AC 3 V, 2 ms period"


def test_collect_tables_and_repeat_flags(die):
    runs = ps.collect(die)
    got = [(r["device"], r["started"], r["repeat"]) for r in runs]
    assert got == [
        ("D1", "20260101_000000", False),
        ("D2", "20260101_000000", False),
        ("D3", "20260101_000000", True),
        ("D3", "20260102_000000", True),
        ("D4", "20260101_000000", True),
    ]
    assert runs[3]["file"].endswith("_reanalysis_Iref4e-8.csv")
    assert runs[1]["mode"] == "AC 3 V, 20 µs period"


def _record_lines(monkeypatch) -> list:
    """Captures every Axes.plot call's (x, y) while still drawing it."""
    calls, original = [], matplotlib.axes.Axes.plot

    def plot(self, x, y, *args, **kwargs):
        calls.append((list(x), list(y)))
        return original(self, x, y, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "plot", plot)
    return calls


def test_main_draws_fresh_runs_in_mv(die, monkeypatch):
    calls = _record_lines(monkeypatch)
    out = ps.main(["--data-dir", str(die.parent), "--sample", die.name, "--die", ".", "--out", str(TMP / "out")])
    assert out.is_file() and out.stat().st_size > 0
    # D1 and D2 only, each a stress line (t > 0 reads) and a recovery line
    assert [c for c in calls if c[0]] == [
        ([0.001, 1.0], pytest.approx([10.0, 20.0])),
        ([0.001, 1.0], pytest.approx([5.0, 2.5])),
        ([0.001, 1.0], pytest.approx([20.0, 40.0])),
        ([0.001, 1.0], pytest.approx([10.0, 5.0])),
    ]


def test_include_repeats_and_no_runs(die, monkeypatch):
    calls = _record_lines(monkeypatch)
    ps.main(["--data-dir", str(die.parent), "--sample", die.name, "--die", ".", "--include-repeats",
             "--out", str(TMP / "out")])  # fmt: skip
    assert len([c for c in calls if c[0]]) == 2 * 5
    with pytest.raises(ValueError, match="no stress runs"):
        ps.plot([], TMP / "out" / "empty.png", "t")
