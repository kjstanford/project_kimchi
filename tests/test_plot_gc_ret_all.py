"""measure.plot_gc_ret_all on a synthetic die: one PMU and one SMU retention run, read current and what is drawn."""

import shutil
from pathlib import Path

import matplotlib.axes
import pandas as pd
import pytest

from measure import plot_gc_ret_all as pg

TMP = Path(".ignore/_gcret_test")  # never inside data/ (AGENTS.md)


@pytest.fixture
def die():
    shutil.rmtree(TMP, ignore_errors=True)
    a, b = TMP / "R01C01", TMP / "R02C02"
    a.mkdir(parents=True)
    b.mkdir()
    pd.DataFrame(
        {"State": [1, 1, 1, 0, 0, 0], "t_ret_s": [0.0, 1.0, 10.0] * 2, "I_read_A": [5e-7, 4e-7, 1e-7, 1e-9, 1e-9, 2e-9]}
    ).to_csv(a / "GC_PMU_ret_20260101_000000_s_0_0_R01C01_W4_L1.csv", index=False)
    pd.DataFrame(  # an SMU run: write phases first, then retention; RBL current flows out (negative)
        {
            "State": [1, 1, 1, 1, 0, 0, 0],
            "Phase": ["cond_boost", "retention", "retention", "retention", "cond_hold", "retention", "retention"],
            "Time_s": [None, 0.0, 2.0, 20.0, None, 2.0, 20.0],
            "RBL_Current": [9e-3, -3e-7, -2e-7, -5e-8, 9e-3, -4e-12, -3e-12],
        }
    ).to_csv(b / "GC_retention_20260101_000000_s_0_0_R02C02_W4_L4.csv", index=False)
    yield TMP
    shutil.rmtree(TMP, ignore_errors=True)


def test_collect_maps_both_kinds(die):
    df = pg.collect(die)
    pmu, smu = df[df["kind"] == "PMU"], df[df["kind"] == "SMU"]
    assert (pmu["L"].unique().tolist(), smu["L"].unique().tolist()) == ([1.0], [4.0])
    assert pmu["I_A"].tolist() == pytest.approx([5e-7, 4e-7, 1e-7, 1e-9, 1e-9, 2e-9])
    # retention rows only, read current = -RBL_Current
    assert smu["t_s"].tolist() == [0.0, 2.0, 20.0, 2.0, 20.0]
    assert smu["I_A"].tolist() == pytest.approx([3e-7, 2e-7, 5e-8, 4e-12, 3e-12])
    assert smu["State"].tolist() == [1, 1, 1, 0, 0]


def test_plot_leaves_out_t0_and_writes(die, monkeypatch):
    calls, original = [], matplotlib.axes.Axes.plot

    def plot(self, x, y, *args, **kwargs):
        calls.append((list(x), list(y), kwargs.get("linestyle")))
        return original(self, x, y, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "plot", plot)
    out = pg.main(["--data-dir", str(die.parent), "--sample", die.name, "--die", ".", "--out", str(TMP / "out")])
    assert out.is_file() and out.stat().st_size > 0
    drawn = [c for c in calls if c[0]]
    assert drawn == [
        ([1.0, 10.0], pytest.approx([4e-7, 1e-7]), "-"),
        ([1.0, 10.0], pytest.approx([1e-9, 2e-9]), "--"),
        ([2.0, 20.0], pytest.approx([2e-7, 5e-8]), "-"),
        ([2.0, 20.0], pytest.approx([4e-12, 3e-12]), "--"),
    ]
