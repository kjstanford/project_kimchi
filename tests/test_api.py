"""Through HTTP: smoke, rejections, a real dry-run equal to a direct run_map.run (differential), Stop and a fault."""

import os

import pytest
from conftest import CFG_NAME, events
from fastapi.testclient import TestClient

from app import pipeline
from app.api import create_app
from app.runs import RunManager
from measure import device_map as dm
from measure import run_map


@pytest.fixture
def client_mgr(tmp_data):
    mgr = RunManager(dryrun_data_dir=str(tmp_data / "web"))
    return TestClient(create_app(mgr)), mgr


def test_smoke(client_mgr):
    c, _ = client_mgr
    h = c.get("/api/health").json()
    assert h["ok"] and h["allow_live"] is False
    page = c.get("/")
    assert page.status_code == 200 and "Wafer-probe runs" in page.text
    assert CFG_NAME in c.get("/api/configs").json()
    assert "/api/preview" in c.get("/openapi.json").json()["paths"]


def test_rejections_are_one_message(client_mgr, cfg):
    c, _ = client_mgr
    bad = {**cfg, "experiment": "nope"}
    for r, code in [
        (c.post("/api/runs", json={"cfg": bad}), 400),
        (c.post("/api/runs", json={"cfg": cfg, "live": True}), 409),
        (c.post("/api/runs", json={"cfg": cfg, "options": {"limit": -1}}), 422),
        (c.post("/api/runs/stop"), 409),
        (c.post("/api/runs/confirm", json={"text": "yes"}), 409),
        (c.get("/api/data/list", params={"root": "data", "path": "../.."}), 404),
        (c.post("/api/preview", json={"cfg": cfg, "options": {"origin": "Z:99"}}), 400),
        (c.get("/api/configs/..%2FAGENTS.md"), 404),
    ]:
        assert r.status_code == code, (r.status_code, r.text)
        assert "detail" in r.json()


def test_dry_run_equals_direct_run(client_mgr, cfg, tmp_data, monkeypatch):
    """Differential: the same config through the API and through run_map.run gives the same rows, value for
    value (the fake instruments are seeded)."""
    c, mgr = client_mgr
    r = c.post("/api/runs", json={"cfg": cfg, "options": {"limit": 4}, "config_name": CFG_NAME})
    assert r.status_code == 200, r.text
    assert mgr.wait(300) == "done"
    web = [e["row"] for e in events(mgr, "row")]
    direct, _ = run_map.run(pipeline.get_config(CFG_NAME), data_dir=str(tmp_data / "direct"), limit=4)
    assert [x["key"] for x in web] == [x["key"] for x in direct]
    assert [x["status"] for x in web] == [x["status"] for x in direct] == ["measured"] * 4
    for a, b in zip(web, direct, strict=True):
        assert a["Vt_V"] == b["Vt_V"] and a["on_off"] == b["on_off"], a["key"]
    assert events(mgr, "plan")[0]["sites"] == [x["key"] for x in web]
    # the file a row names opens through the data API, with |I| from the backend
    root = str(tmp_data / "web").replace(os.sep, "/")
    f = web[0]["main_file"].replace(os.sep, "/")
    assert f.startswith(root + "/")
    monkeypatch.setattr(pipeline, "DATA_ROOTS", (*pipeline.DATA_ROOTS, root))
    t = c.get("/api/data/table", params={"root": root, "path": f[len(root) + 1 :], "abs": True}).json()
    assert t["axes"]["x"] == "Gate_Voltage" and "Drain_Current" in t["axes"]["y"] and t["n_rows"] > 10
    assert t["abs_values"] and min(v for v in t["data"]["Drain_Current"] if v is not None) >= 0


def test_stop_mid_run_with_an_open_device(tmp_data, cfg):
    """Stop after 2 of 6 devices ends through run_map's cleanup (probes separated, outputs off); an open device
    (B:2) is reported in its row, not fatal; the fake bench's interlocks never trip."""
    mgr = None
    benches = []

    def runner(cfg, **kw):
        full = dm.load_devices(cfg["device_csv"])
        fakes = run_map.make_fake_instruments(cfg, full, cfg["origin_device"], faults={"B:2": "open"})
        benches.append(fakes[0])
        on_row = kw["on_row"]

        def row_then_stop(r):
            on_row(r)
            if mgr.info["n_rows"] >= 2:
                mgr.stop()

        kw["on_row"] = row_then_stop
        return run_map.run(cfg, fakes=fakes, **kw)

    mgr = RunManager(dryrun_data_dir=str(tmp_data / "stop"), runner=runner)
    c = TestClient(create_app(mgr))
    assert c.post("/api/runs", json={"cfg": cfg, "options": {"limit": 6}}).status_code == 200
    assert mgr.wait(300) == "stopped" and mgr.info["n_rows"] == 2
    rows = [e["row"] for e in events(mgr, "row")]
    assert rows[0]["status"] == "measured" and rows[1]["key"] == "B:2" and rows[1]["status"] != "measured"
    lines = [e["line"] for e in events(mgr, "log")]
    i = max(k for k, line in enumerate(lines) if "Stop requested" in line)
    assert any("separation height" in line for line in lines[i:]), "probes not separated after Stop"
    assert benches[0].log and not benches[0].bias_live and not benches[0].in_contact


def test_native_launcher_is_local_only():
    """make up-live: --allow-live can only be served on this machine."""
    from app.__main__ import main

    with pytest.raises(SystemExit):
        main(["--allow-live", "--host", "0.0.0.0"])  # noqa: S104 (the address being refused)
