"""measure.plot_idvg_all on a synthetic die folder: which files and rows it uses, the W/L scaling, the figure."""

import shutil
from pathlib import Path

import pandas as pd
import pytest

from measure import plot_idvg_all as p

TMP = Path(".ignore/_plot_idvg_test")  # never inside data/ (AGENTS.md)
VG = [-1.0, 0.0, 1.0, 0.0, -1.0]  # one double sweep


def _main(vd_currents: dict, cycles=(1,)) -> pd.DataFrame:
    """A main-sweep table: for each Vd, Drain_Current = -current at every point (sign must not matter)."""
    rows = [
        {
            "Drain_Current": -i,
            "Gate_Current": 1e-13,
            "Gate_Voltage": vg,
            "Source_Current": i,
            "Drain_Status": 0,
            "Gate_Status": 0,
            "Source_Status": 0,
            "Drain_Voltage": vd,
            "Cycle": c,
        }
        for c in cycles
        for vd, i in vd_currents.items()
        for vg in VG
    ]
    return pd.DataFrame(rows)


@pytest.fixture
def die():
    shutil.rmtree(TMP, ignore_errors=True)
    a, b = TMP / "R01C01L", TMP / "R01C01R"
    a.mkdir(parents=True)
    b.mkdir()
    # A (W2/L1): an older try, then the rerun that must win; plus an initial scan that must be ignored
    _main({0.05: 9e-6}).to_csv(a / "IdVg_main_20260101_000000_s_0_0_R01C01L_W2_L1.csv", index=False)
    _main({0.05: 2e-6, 1.5: 7e-5}, cycles=(1, 2)).to_csv(
        a / "IdVg_main_20260102_000000_s_0_0_R01C01L_W2_L1.csv", index=False
    )
    _main({0.05: 5e-3}).to_csv(a / "IdVg_initial_scan_20260103_000000_s_0_0_R01C01L_W2_L1.csv", index=False)
    # B (W4/L10): one main sweep; C: no main sweep at all (screened out)
    _main({0.05: 8e-8, 1.5: 3e-6}).to_csv(b / "IdVg_main_20260102_000000_s_0_0_R01C01R_W4_L10.csv", index=False)
    (TMP / "R02C01L").mkdir()
    yield TMP
    shutil.rmtree(TMP, ignore_errors=True)


def test_newest_main_per_device_at_one_vd_scaled_by_l_over_w(die):
    c = p.load_curves(die, vd=0.05)
    assert sorted(c["device"].unique()) == ["R01C01L", "R01C01R"]
    assert set(c["Drain_Voltage"]) == {0.05}
    a, b = c[c["device"] == "R01C01L"], c[c["device"] == "R01C01R"]
    assert sorted(a["Cycle"].unique()) == [1, 2] and len(a) == 2 * len(VG)
    assert a["Id_LW"].tolist() == pytest.approx([2e-6 * 1 / 2] * len(a))  # newest file, |Id| * L/W
    assert b["Id_LW"].tolist() == pytest.approx([8e-8 * 10 / 4] * len(b))
    assert (a["W"].iloc[0], a["L"].iloc[0], b["L"].iloc[0]) == (2.0, 1.0, 10.0)


def test_other_vd_and_missing_vd(die):
    c = p.load_curves(die, vd=1.5)
    assert c.loc[c["device"] == "R01C01R", "Id_LW"].tolist() == pytest.approx([3e-6 * 10 / 4] * len(VG))
    assert p.load_curves(die, vd=0.7).empty


def test_one_colour_per_l_and_a_figure(die):
    c = p.load_curves(die, vd=0.05)
    out = p.plot(c, TMP / "out" / "fig.png", title="t", alpha=0.2)
    assert out.is_file() and out.stat().st_size > 0
    colours = p.colours_by_L(c["L"])
    assert list(colours) == [1.0, 10.0] and len(set(colours.values())) == 2
    with pytest.raises(ValueError, match="no IdVg_main"):
        p.plot(c.iloc[0:0], TMP / "out" / "empty.png", title="t")
