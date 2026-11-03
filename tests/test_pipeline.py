"""Unit and differential tests of app.pipeline: validation, the plan preview (answer key: run_map.build_plan),
saved-data access."""

import math

import pandas as pd
import pytest

from app import pipeline
from app.models import PreviewRequest, RunOptions
from app.pipeline import PipelineError
from app.runs import RunManager
from measure import run_map


def test_every_shipped_config_validates():
    names = pipeline.list_configs()
    assert len(names) >= 30
    for name in names:
        v = pipeline.validate_config(pipeline.get_config(name))
        assert v.ok, (name, v.errors)


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda c: c.update(experiment="nope"), "experiment must be one of"),
        (lambda c: c.update(device_csv="layouts/missing.csv"), "device_csv not found"),
        (lambda c: c.pop("motion"), "missing block 'motion'"),
        (lambda c: c.update(experiment="stress_stair"), "needs instruments.smu_tool = 'k4200'"),
    ],
)
def test_bad_configs_are_rejected(cfg, change, message):
    change(cfg)
    v = pipeline.validate_config(cfg)
    assert not v.ok and any(message in e for e in v.errors), v.errors


@pytest.mark.parametrize("name", ["../AGENTS.md", "sub/x.json", "x.txt"])
def test_config_names_cannot_leave_configs(name):
    with pytest.raises(PipelineError):
        pipeline.get_config(name)


@pytest.mark.parametrize(
    "opts",
    [{}, {"columns": "A,B", "L_values": "2,1"}, {"limit": 5, "origin": "B:2"}, {"only": "D:1,A:2", "keep_order": True}],
)
def test_preview_is_the_cli_plan(cfg, tmp_data, opts):
    """Differential + tripwire: same devices in the same order as run_map.build_plan, and the summed steps,
    the end of the path and the reported total travel agree with it."""
    mgr = RunManager(dryrun_data_dir=str(tmp_data))
    p = pipeline.preview_plan(PreviewRequest(cfg=cfg, options=RunOptions(**opts)), mgr)
    kw = RunOptions(**opts).run_kwargs()
    plan, _ = run_map.build_plan(
        cfg,
        kw.get("only"),
        kw.get("columns"),
        kw.get("L_values"),
        kw.get("limit"),
        kw.get("origin") or cfg["origin_device"],
        str(tmp_data),
        lambda m: None,
        kw.get("keep_order", False),
    )
    assert [e.key for e in p.plan] == [d["key"] for d in plan] and len(plan) > 0
    sx, sy = sum(d["dx"] for d in plan), sum(d["dy"] for d in plan)
    assert math.isclose(p.plan[-1].tx, sx, abs_tol=1e-6) and math.isclose(p.plan[-1].ty, sy, abs_tol=1e-6)
    assert math.isclose(p.total_travel_um, sum(math.hypot(d["dx"], d["dy"]) for d in plan), rel_tol=1e-12)
    origin = [s for s in p.sites if s.origin]
    assert len(origin) == 1 and origin[0].x == 0 and origin[0].y == 0


def test_preview_known_travel(cfg, tmp_data):
    """Known value, by hand: A:2 -> B:2 -> C:2 are 200 um apart in x in the layout; from the origin A:2 the
    plan steps 0, 200, 200 um, 400 um in all."""
    p = pipeline.preview_plan(
        PreviewRequest(cfg=cfg, options=RunOptions(limit=3)), RunManager(dryrun_data_dir=str(tmp_data))
    )
    assert [e.key for e in p.plan] == ["A:2", "B:2", "C:2"]
    assert p.total_travel_um == pytest.approx(400.0, abs=1e-9)
    assert len(p.sites) == 144


@pytest.mark.parametrize("opts", [{"limit": 0}, {"origin_offset": "8000"}, {"fakes": 1}])
def test_bad_options_are_rejected(opts):
    with pytest.raises(ValueError):
        RunOptions(**opts)


def test_preview_unknown_origin(cfg, tmp_data):
    with pytest.raises(PipelineError, match="Origin device Z:99"):
        pipeline.preview_plan(
            PreviewRequest(cfg=cfg, options=RunOptions(origin="Z:99")), RunManager(dryrun_data_dir=str(tmp_data))
        )


@pytest.mark.parametrize(
    "root, rel", [("data", "../AGENTS.md"), ("etc", ""), (".ignore/web_dryrun", "../../AGENTS.md")]
)
def test_data_paths_stay_inside_their_root(root, rel):
    with pytest.raises(PipelineError):
        pipeline.resolve(root, rel)


def test_read_table_abs_values(tmp_path, monkeypatch):
    """|I| for a log axis is computed by the backend: negative currents come back positive, NaN as null."""
    root = tmp_path / "data"
    root.mkdir()
    pd.DataFrame(
        {
            "Gate_Voltage": [-1.0, 0.0, 1.0],
            "Drain_Current": [-2e-12, float("nan"), 3e-6],
            "Gate_Status": ["N", "N", "C"],
        }
    ).to_csv(root / "IdVg_main_x.csv", index=False)
    plain = pipeline.read_table("data", "IdVg_main_x.csv", False, base=str(tmp_path))
    absd = pipeline.read_table("data", "IdVg_main_x.csv", True, base=str(tmp_path))
    assert plain.data["Drain_Current"] == [-2e-12, None, 3e-6]
    assert absd.data["Drain_Current"] == [2e-12, None, 3e-6] and absd.data["Gate_Voltage"] == [1.0, 0.0, 1.0]
    assert absd.data["Gate_Status"] == ["N", "N", "C"] and absd.abs_values and not plain.abs_values
    assert plain.axes.x == "Gate_Voltage" and plain.axes.y == ["Drain_Current"] and plain.axes.logy
