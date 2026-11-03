"""RunManager: one run at a time, live only when allowed, typed confirmation, Stop at a gate, strict JSON."""

import json
import threading
import time

import pytest
from conftest import events, wait_state

from app.runs import RunError, RunManager


class FakeRunner:
    """Stands in for run_map.run: logs, plans one device, asks the two live gates, writes one row."""

    def __init__(self):
        self.started, self.release, self.answers, self.cfgs = threading.Event(), threading.Event(), [], []

    def __call__(self, cfg, live, data_dir, interactive, log_sink, confirm_fn, stop_event, on_plan, on_row, **opts):
        assert interactive is True
        self.cfgs.append(cfg)
        log_sink("hello")
        on_plan([{"key": "A:2"}])
        self.started.set()
        if live:
            self.answers.append(confirm_fn("LIVE RUN on b1500: idvg, 1 devices. Probes must be landed on A:2."))
            if self.answers[-1]:
                self.answers.append(confirm_fn("Check alignment at separation height."))
        self.release.wait(10)
        on_row({"key": "A:2", "status": "measured", "Vt_V": float("nan")})
        return [{"key": "A:2"}], None


def test_live_refused_unless_allowed(cfg, tmp_data):
    mgr = RunManager(allow_live=False, dryrun_data_dir=str(tmp_data), runner=FakeRunner())
    with pytest.raises(RunError, match="disabled"):
        mgr.start(cfg, live=True)
    assert mgr.state == "idle"


def test_one_run_at_a_time_and_strict_json(cfg, tmp_data):
    r = FakeRunner()
    mgr = RunManager(dryrun_data_dir=str(tmp_data), runner=r)
    mgr.start({**cfg, "show_plots": True})
    r.started.wait(5)
    with pytest.raises(RunError, match="already"):
        mgr.start(cfg)
    r.release.set()
    assert mgr.wait(10) == "done"
    assert r.cfgs[0]["show_plots"] is False  # figures go to the browser, never to Tk in the worker thread
    row = events(mgr, "row")[0]
    assert row["row"]["Vt_V"] is None and row["site"] == "A:2"
    json.dumps(mgr.events_since(0), allow_nan=False)
    assert events(mgr, "log")[0]["line"] == "hello"


def test_live_gates_need_the_typed_answer(cfg, tmp_data):
    r = FakeRunner()
    mgr = RunManager(allow_live=True, dryrun_data_dir=str(tmp_data), runner=r)
    mgr.start(cfg, live=True)
    wait_state(mgr, ("awaiting_confirm",))
    assert mgr.status()["expect"] == "A:2"
    for wrong in ("yes", "B:2", ""):
        with pytest.raises(RunError):
            mgr.confirm(wrong)
    mgr.confirm(" A:2 ")
    t0 = time.time()
    while mgr.status()["expect"] != "yes":
        assert time.time() - t0 < 5
        time.sleep(0.01)
    mgr.confirm("yes")
    r.release.set()
    assert mgr.wait(10) == "done" and r.answers == [True, True]


def test_stop_at_a_gate_answers_no(cfg, tmp_data):
    r = FakeRunner()
    mgr = RunManager(allow_live=True, dryrun_data_dir=str(tmp_data), runner=r)
    mgr.start(cfg, live=True)
    wait_state(mgr, ("awaiting_confirm",))
    mgr.stop()
    r.release.set()
    assert mgr.wait(10) == "stopped" and r.answers == [False]


def test_real_live_gate_declined_never_connects(cfg, tmp_data):
    """measure.run_map.run with live=True, declined at the browser gate: no connect() is called (any call
    fails the test, see conftest) and the run ends before connecting."""
    mgr = RunManager(allow_live=True, dryrun_data_dir=str(tmp_data / "dry"))
    cfg["data_dir"] = str(tmp_data / "live")
    mgr.start(cfg, live=True, options={"limit": 1})
    wait_state(mgr, ("awaiting_confirm", "failed"))
    assert mgr.state == "awaiting_confirm"
    prompt = events(mgr, "confirm")[0]
    assert prompt["prompt"].startswith("LIVE RUN") and prompt["expect"] == "A:2"
    mgr.decline()
    assert mgr.wait(30) == "done"
    assert any("Aborted by user before connecting" in e["line"] for e in events(mgr, "log"))
