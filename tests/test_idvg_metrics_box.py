"""measure.idvg_metrics_box and measure.tr_extraction against an analytic transistor with known VT, muFE, SS and VTR.

The device is the textbook linear-regime charge-sheet curve Id = beta*Vd*m*ln(1 + exp((Vg - VT)/m)), m = eta*phiT:
exponential below VT (SS = ln10 * m), linear above (gm -> beta*Vd). Every expected value below is derived by hand
from that formula, not by running the code under test.
"""

import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from measure import idvg_metrics_box as box
from measure import tr_extraction as tr

TMP = Path(".ignore/_box_test")  # never inside data/ (AGENTS.md)
PHI_T = tr.thermal_voltage(300.0)
VD = 0.05
W, L = 10.0, 1.0
COX = box.EPS0 * 17 / 10e-9  # 10 nm HfO2, k = 17
MU = 5e-4  # m^2/Vs = 5 cm^2/Vs
BETA = MU * COX * W / L
SS_TRUE = 0.150  # V/dec
M = SS_TRUE / np.log(10)  # eta * phiT
ETA = M / PHI_T
VG = np.round(np.arange(-1.0, 3.0 + 1e-9, 0.02), 4)


def device(vg, vt=1.0, beta=BETA, vd=VD, r_series=0.0):
    """|Id| of the analytic device, a 0.1 pA-ish noise floor, and optionally a series resistance."""
    i = beta * vd * M * np.logaddexp(0.0, (vg - vt) / M)
    if r_series:
        i = i / (1 + i * r_series / vd)  # linear regime: Id(Vd - Id*R) = Id(Vd) * (1 - Id*R/Vd)
    return i + 1e-13 * (2 + vg)  # a floor that still rises slightly, as real data never sits on one value


# hand-derived answers for vt = 1 V
VT_CC = 1.0 + M * np.log(np.expm1(box.VT_ID_PER_WL * W / L / (BETA * VD * M)))  # Id = 1e-7 * W/L
VTON = 1.0 - ETA * VD / 2  # linear extrapolation is exactly VT; Eq. 9 subtracts eta*Vd/2
VTOFF = 1.0 + M * np.log(PHI_T * (1 - np.exp(-VD / PHI_T)) / VD)  # log line meets I_D0 (Eq. 8)


def test_hand_derived_answers_are_distinct():
    assert VT_CC == pytest.approx(1.264, abs=2e-3)
    assert VTON == pytest.approx(0.937, abs=2e-3) and VTOFF == pytest.approx(0.947, abs=2e-3)


def test_vt_mufe_ss_of_the_analytic_device():
    m = box.device_metrics(VG, -device(VG), W, L, VD, COX)  # sign of Id must not matter
    assert m["VT_V"] == pytest.approx(VT_CC, abs=5e-3)
    assert m["muFE_cm2Vs"] == pytest.approx(MU * 1e4, rel=0.01)
    assert m["SS_mVdec"] == pytest.approx(SS_TRUE * 1e3, rel=0.01)


def test_metrics_nan_when_not_extractable():
    off = np.full_like(VG, 1e-13)  # never turns on: no VT current, no subthreshold slope
    m = box.device_metrics(VG, off, W, L, VD, COX)
    assert np.isnan(m["VT_V"]) and np.isnan(m["SS_mVdec"])


def test_vtr_raw_id_method():
    on, off, vtr = tr.extract_VTR(VG, device(VG), VD, ID_limit=1e-11, correct_series_R=False)
    assert on == pytest.approx(VTON, abs=0.01)
    assert off == pytest.approx(VTOFF, abs=0.01)
    assert vtr == pytest.approx(on - off)
    assert box.vtr_metrics(VG, -device(VG), VD, 1e-11) == pytest.approx({"VTon_V": on, "VToff_V": off, "VTR_V": vtr})


def test_series_resistance_biases_raw_vton_not_vtoff():
    """The caveat on 6802: R_S + R_D pulls the raw-Id VT,on down a lot; VT,off moves only through beta in I_D0."""
    on_r, off_r, _ = tr.extract_VTR(VG, device(VG, r_series=5e3), VD, ID_limit=1e-11)
    assert on_r < VTON - 0.1
    assert abs(off_r - VTOFF) < 0.05 < VTON - on_r


@pytest.mark.parametrize("level", [1e-13, 1e-9])
def test_vtr_nan_when_no_usable_region(level):
    flat = np.full_like(VG, level) * (1 + 1e-3 * VG)  # never turns on: at the floor, or a flat leak
    assert all(np.isnan(v) for v in box.vtr_metrics(VG, flat, VD, 1e-11).values())


def _write_main(path: Path, fwd_vt=1.0):
    """A main-sweep file: cycles 1 and 2 at Vd 0.05 and 1.5 V, each a forward then a backward sweep.

    Only cycle 1, forward, Vd 0.05 V has VT = fwd_vt; every other branch is shifted, so using one shows."""
    rows = []
    for cycle, shift in ((1, 0.0), (2, 0.4)):
        for vd in (VD, 1.5):
            for vg, back in [(v, False) for v in VG] + [(v, True) for v in VG[::-1]]:
                vt = fwd_vt + shift + (0.3 if back else 0.0) + (0.2 if vd != VD else 0.0)
                rows.append(
                    {"Drain_Current": -device(vg, vt, vd=vd), "Gate_Voltage": vg, "Drain_Voltage": vd, "Cycle": cycle}
                )
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


@pytest.fixture
def samples():
    shutil.rmtree(TMP, ignore_errors=True)
    d = TMP / "data"
    _write_main(d / "sA/0_0/R01C01L/IdVg_main_20260101_000000_sA_0_0_R01C01L_W10_L1.csv", fwd_vt=0.2)  # older
    _write_main(d / "sA/0_0/R01C01L/IdVg_main_20260102_000000_sA_0_0_R01C01L_W10_L1.csv", fwd_vt=1.0)
    _write_main(d / "sA/0_0/R01C02L/IdVg_main_20260102_000000_sA_0_0_R01C02L_W10_L10.csv")  # L = 10: excluded
    _write_main(d / "sB/0_0/R05C05L/IdVg_main_20260102_000000_sB_0_0_R05C05L_W10_L1.csv", fwd_vt=1.5)
    yield d
    shutil.rmtree(TMP, ignore_errors=True)


def test_collect_uses_newest_file_cycle1_forward_and_drops_L10(samples):
    df = box.collect(samples, ["sA", "sB"], "0_0", VD, COX, exclude_L=[10.0], id_limit=1e-11)
    assert df[["sample", "device", "L"]].values.tolist() == [["sA", "R01C01L", 1.0], ["sB", "R05C05L", 1.0]]
    a, b = df.iloc[0], df.iloc[1]
    assert a["file"].startswith("IdVg_main_20260102")
    assert a["VT_V"] == pytest.approx(VT_CC, abs=5e-3)
    assert b["VT_V"] == pytest.approx(VT_CC + 0.5, abs=5e-3)
    assert a["VTon_V"] == pytest.approx(VTON, abs=0.01) and a["VToff_V"] == pytest.approx(VTOFF, abs=0.01)
    assert a["SS_mVdec"] == pytest.approx(150, rel=0.01) and a["muFE_cm2Vs"] == pytest.approx(5, rel=0.01)


def test_main_writes_csv_and_one_png_per_metric(samples):
    out = TMP / "out"
    box.main(["--samples", "sA", "sB", "--data-dir", str(samples), "--out", str(out)])
    assert (out / "metrics_sA_sB_0_0_Vd0.05.csv").is_file()
    for m in box.METRICS:
        png = out / f"box_{m}_sA_sB_0_0_Vd0.05.png"
        assert png.is_file() and png.stat().st_size > 0


def test_main_refuses_out_in_data_and_no_data(samples):
    with pytest.raises(SystemExit):
        box.main(["--samples", "sA", "--data-dir", str(samples), "--out", "data/x"])
    with pytest.raises(SystemExit):
        box.main(["--samples", "sA", "--data-dir", str(samples), "--vd", "0.7", "--out", str(TMP / "out")])
