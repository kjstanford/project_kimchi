"""measure.sample_devices on the localBG v5 blocks with a synthetic data folder: every site in 3 blocks, measured
devices counted and never picked again, the blocks balanced, the --include device picked, the CSV a valid map."""

import json
import shutil
from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

from measure import device_map as dm
from measure import sample_devices as sd

TMP = Path(".ignore/_sample_test")  # never inside data/ (AGENTS.md)
CFG = "configs/yanjie_in203_110cycleHfO2_R1_all_idvg.json"


@pytest.fixture
def data_dir():
    """TL rows 1-2 and TR row 1 already scanned (42 devices)."""
    shutil.rmtree(TMP, ignore_errors=True)
    raw = sd.load_raw(json.loads(Path(CFG).read_text())["device_csv"])
    done = raw[((raw["region"] == "TL") & (raw["Row"] <= 2)) | ((raw["region"] == "TR") & (raw["Row"] == 1))]
    for _, r in done.iterrows():
        k = r["device_id"]
        folder = TMP / "yanjie_in203_110cycleHfO2" / "0_0" / k
        folder.mkdir(parents=True)
        name = f"IdVg_initial_scan_x_yanjie_in203_110cycleHfO2_0_0_{k}_W{r['W in um']:g}_L{r['L in um']:g}.csv"
        (folder / name).write_text("x")
    yield TMP
    shutil.rmtree(TMP, ignore_errors=True)


def test_every_site_three_blocks_balanced(data_dir):
    cfg = json.loads(Path(CFG).read_text())
    raw = sd.load_raw(cfg["device_csv"])
    done = sd.measured_ids(cfg, str(data_dir))
    assert len(done) == 42
    picked = sd.pick(raw, done, per_site=3, seed=1, include=["tr-r02c05"])
    assert "TR-R02C05" in picked and not set(picked) & done and len(picked) == len(set(picked))
    have = raw[raw["device_id"].isin(set(picked) | done)]
    assert (have.groupby(["Row", "Column"]).size() == 3).all()  # every site, three blocks
    load = Counter(have["region"])
    assert max(load.values()) - min(load.values()) <= 1, load  # 630 / 6 = 105 each
    assert sd.pick(raw, done, 3, 1) == sd.pick(raw, done, 3, 1) != sd.pick(raw, done, 3, 2)


def test_yanjie_sample3_plan(capsys):
    """The committed 3-blocks-per-site run: 535 devices, starting on the device the probes were left on."""
    from measure import run_map

    cfg = json.loads(Path("configs/yanjie_in203_110cycleHfO2_R1_sample3_idvg.json").read_text())
    plan, full = run_map.build_plan({**cfg, "repeat_measurement": True}, data_dir=str(TMP))
    keys = [p["key"] for p in plan]
    assert len(keys) == len(set(keys)) == 535 and keys[0] == "TR-R01C14" and (plan[0]["dx"], plan[0]["dy"]) == (0, 0)
    assert Counter(full["region"]) == {"BC": 105, "BR": 105, "MC": 105, "ML": 105, "TL": 23, "TR": 92}
    print(f"moves: {sum(p['hops'] for p in plan)} hops for {len(plan)} devices")


def test_yanjie_sample4_plan(capsys):
    """The follow-up run: a 4th copy of every site (35 per block), from BR-R15C14, where sample3 left the probes.
    BR-R15C14 is measured already; it is in the CSV only as the origin, and resume skips it on the lab PC."""
    from measure import run_map

    cfg = json.loads(Path("configs/yanjie_in203_110cycleHfO2_R1_sample4_idvg.json").read_text())
    plan, full = run_map.build_plan({**cfg, "repeat_measurement": True}, data_dir=str(TMP))
    keys = [p["key"] for p in plan]
    assert len(keys) == len(set(keys)) == 211 and keys[0] == "BR-R15C14" and (plan[0]["dx"], plan[0]["dy"]) == (0, 0)
    assert Counter(full["region"]) == {"BC": 35, "BR": 36, "MC": 35, "ML": 35, "TL": 35, "TR": 35}
    sample3 = set(pd.read_csv("configs/yanjie_in203_110cycleHfO2_R1_sample3_devices.csv")["device_id"])
    assert set(keys) & sample3 == {"BR-R15C14"}
    print("block order:", [k[:2] for i, k in enumerate(keys) if i == 0 or keys[i - 1][:2] != k[:2]])


def test_main_writes_a_device_map(data_dir):
    out = TMP / "out" / "devices.csv"
    inc = "TR-R02C05,TL-R01C01"  # unmeasured, and measured (the probes' device: in the CSV, not a new pick)
    sd.main(["--config", CFG, "--per-site", "3", "--include", inc, "--data-dir", str(data_dir), "--out", str(out)])
    full = dm.load_devices(str(out))
    assert len(full) == 630 - 42 + 1 and {"TR-R02C05", "TL-R01C01"} <= set(full["key"]) and full["W"].notna().all()
    assert list(pd.read_csv(out).columns) == list(pd.read_csv("layouts/localbg_v5_R1_TL.csv").columns)
    with pytest.raises(SystemExit):
        sd.main(["--config", CFG, "--per-site", "3", "--out", "data/x.csv"])
