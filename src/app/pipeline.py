"""One entry function per API endpoint; everything a result depends on is computed here, not in the page.

Configs and plans reuse measure.run_map / measure.device_map unchanged; runs go through app.runs.RunManager to
measure.run_map.run; saved data is read-only, inside fixed roots.
"""

import json
import math
import os
from pathlib import Path
from typing import Any

import pandas as pd

from app.models import (
    Axes,
    ConfirmRequest,
    FileEntry,
    Health,
    Listing,
    Ok,
    PlanEntry,
    Preview,
    PreviewRequest,
    RunRequest,
    RunStarted,
    RunStatus,
    Site,
    Table,
    Validation,
    clean,
)
from app.runs import RunError, RunManager, site_id
from measure import device_map as dm
from measure import run_map

CONFIG_DIR = "configs"
DATA_ROOTS = ("data", "data_k4200", "data_k4200_stress", ".ignore/web_dryrun")
SHOWN = (".csv", ".png", ".log")
MAX_POINTS = 200_000
REQUIRED = {
    "sample": ("name", "die_idx"),
    "motion": ("transform", "max_step_um", "bounds_margin_um"),
    "instruments": ("prober_resource",),
}
# default plot axes per file prefix: (x column, y columns, log |y|)
DEFAULT_AXES = [
    ("IdVg_", "Gate_Voltage", ["Drain_Current", "Gate_Current"], True),
    ("GC_WBL_sweep_", "WBL_Voltage", ["RBL_Current", "WBL_Current", "WWL_Current"], True),
    ("summary_", None, [], False),
]


class PipelineError(Exception):
    """A request the pipeline rejects; status is the HTTP code the API answers with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# --- health and configs -----------------------------------------------------------------------------------
def health(mgr: RunManager) -> Health:
    """GET /api/health."""
    return Health(ok=True, allow_live=mgr.allow_live, dryrun_data_dir=mgr.dryrun_data_dir)


def list_configs(config_dir: str = CONFIG_DIR) -> list[str]:
    """GET /api/configs: the config file names in configs/."""
    return sorted(f for f in os.listdir(config_dir) if f.endswith(".json"))


def get_config(name: str, config_dir: str = CONFIG_DIR) -> dict[str, Any]:
    """GET /api/configs/{name}: one config; name must be a bare *.json file name in configs/."""
    if os.path.basename(name) != name or not name.endswith(".json"):
        raise PipelineError(f"not a config name: {name!r}")
    path = os.path.join(config_dir, name)
    if not os.path.isfile(path):
        raise PipelineError(f"no such config: {name}", 404)
    with open(path) as f:
        return json.load(f)


def validate_config(cfg: dict[str, Any]) -> Validation:
    """POST /api/validate: the checks a run makes before it connects, as a list of messages."""
    errors: list[str] = []
    for k in ("device_csv", "data_dir", "origin_device", "smu_map"):
        if k not in cfg:
            errors.append(f"missing '{k}'")
    for block, keys in REQUIRED.items():
        if not isinstance(cfg.get(block), dict):
            errors.append(f"missing block '{block}'")
            continue
        errors += [f"missing '{block}.{k}'" for k in keys if k not in cfg[block]]
    exp = tool = None
    try:
        exp = run_map.experiment(cfg)
    except (ValueError, KeyError) as e:
        errors.append(str(e))
    try:
        tool = run_map.smu_tool(cfg)[0]
    except (ValueError, KeyError) as e:
        errors.append(str(e))
    csvs = cfg.get("device_csv")
    for p in csvs if isinstance(csvs, list) else [csvs] if csvs else []:
        if not os.path.isfile(p):
            errors.append(f"device_csv not found: {p}")
    for seg in cfg.get("segments") or []:
        if not os.path.isfile(seg.get("device_csv", "")):
            errors.append(f"segment device_csv not found: {seg.get('device_csv')}")
    if not errors and exp:
        try:
            if exp == "stress_stair":
                from measure import stress_stair

                stress_stair.check_config(cfg)
            elif exp in run_map.GC_EXPERIMENTS:
                run_map.GC_FUNCS[exp][0](cfg)
        except Exception as e:  # the experiment's own check; any rejection becomes a message
            errors.append(f"{exp} settings: {e}")
    return Validation(ok=not errors, errors=errors, experiment=exp, smu_tool=tool)


def _require_valid(cfg: dict[str, Any]) -> None:
    v = validate_config(cfg)
    if not v.ok:
        raise PipelineError("; ".join(v.errors))


# --- plan preview -----------------------------------------------------------------------------------------
def _plan_entry(p: dict, cfg: dict, multi: bool) -> PlanEntry:
    c = p.get("cfg", cfg)
    return PlanEntry(
        site=site_id(c, p["key"], multi),
        key=p["key"],
        L=clean(p.get("L")),
        W=clean(run_map.device_W(c, p)),
        tx=p["tx"],
        ty=p["ty"],
        dx=p["dx"],
        dy=p["dy"],
        hops=int(p.get("hops", 1)),
        segment=run_map.segment_label(c) if multi else None,
    )


def preview_plan(req: PreviewRequest, mgr: RunManager) -> Preview:
    """POST /api/preview: the run's plan without connecting: every site on the stage (um from the origin
    device), the devices in visiting order, and those already measured (which the run skips)."""
    cfg = req.cfg
    _require_valid(cfg)
    opts = req.options.run_kwargs()
    data_dir = cfg["data_dir"] if req.live else mgr.dryrun_data_dir
    lines: list[str] = []
    multi = bool(cfg.get("segments"))
    origin_key = dm.parse_keys(opts.get("origin") or cfg["origin_device"])[0]
    t = cfg["motion"]["transform"]
    try:
        if multi:
            if set(opts) - {"limit", "origin"}:
                raise ValueError("a 'segments' run takes only limit and origin")
            plan, sites = run_map.build_multi_plan(cfg, origin_key, opts.get("limit"), data_dir, lines.append)
            o = sites.set_index("key").loc[f"origin|{origin_key}"]
            off = (0.0, 0.0)
        else:
            plan, sites = run_map.build_plan(
                cfg,
                opts.get("only"),
                opts.get("columns"),
                opts.get("L_values"),
                opts.get("limit"),
                origin_key,
                data_dir,
                lines.append,
                opts.get("keep_order", False),
                opts.get("origin_offset"),
            )
            o = sites.set_index("key").loc[origin_key]
            off = opts.get("origin_offset") or (0.0, 0.0)
    except (ValueError, KeyError) as e:
        raise PipelineError(f"{type(e).__name__}: {e}") from e
    ox, oy = dm.to_stage(o["x"] + off[0], o["y"] + off[1], **t)
    out_sites = []
    for _, r in sites.iterrows():
        sx, sy = dm.to_stage(r["x"], r["y"], **t)
        out_sites.append(
            Site(
                site=r["key"],
                x=sx - ox,
                y=sy - oy,
                L=clean(r.get("L")),
                origin=r["key"] in (origin_key, f"origin|{origin_key}"),
            )
        )
    entries = [_plan_entry(p, cfg, multi) for p in plan]
    planned = {e.site for e in entries}
    measured = []
    if not multi:
        measured = [
            r["key"]
            for _, r in sites.iterrows()
            if r["key"] not in planned and run_map.already_measured(cfg, r, data_dir)
        ]
    return Preview(
        origin=origin_key,
        multi=multi,
        sites=out_sites,
        measured=measured,
        plan=entries,
        total_travel_um=sum(math.hypot(e.dx, e.dy) for e in entries),
        log=lines,
    )


# --- runs -------------------------------------------------------------------------------------------------
def _run_call(fn, *args):
    try:
        return fn(*args)
    except RunError as e:
        raise PipelineError(str(e), 409) from e


def start_run(req: RunRequest, mgr: RunManager) -> RunStarted:
    """POST /api/runs: start a dry-run (or, with --allow-live, a gated live run) of a valid config."""
    _require_valid(req.cfg)
    run = _run_call(mgr.start, req.cfg, req.live, req.options.run_kwargs(), req.config_name)
    return RunStarted(run=run)


def run_status(mgr: RunManager) -> RunStatus:
    """GET /api/runs/status."""
    return RunStatus(**mgr.status())


def stop_run(mgr: RunManager) -> RunStatus:
    """POST /api/runs/stop: stop before the next device (or answer 'no' at a pending gate)."""
    _run_call(mgr.stop)
    return run_status(mgr)


def confirm_run(req: ConfirmRequest, mgr: RunManager) -> Ok:
    """POST /api/runs/confirm: answer the pending gate with the text it expects."""
    _run_call(mgr.confirm, req.text)
    return Ok()


def decline_run(mgr: RunManager) -> Ok:
    """POST /api/runs/decline."""
    _run_call(mgr.decline)
    return Ok()


def run_events(mgr: RunManager, since: int, timeout: float) -> list[dict[str, Any]]:
    """GET /api/runs/events (and the SSE stream): events after `since`, waiting up to `timeout` s."""
    return mgr.events_since(since, timeout)


# --- saved data (read-only) -------------------------------------------------------------------------------
def data_roots(base: str = ".") -> list[str]:
    """GET /api/data/roots: the data folders that exist."""
    return [r for r in DATA_ROOTS if os.path.isdir(os.path.join(base, r))]


def resolve(root: str, rel: str = "", base: str = ".") -> Path:
    """The real path of root/rel; it must exist and stay inside the root."""
    if root not in DATA_ROOTS:
        raise PipelineError(f"unknown data root {root!r}", 404)
    top = Path(base, root).resolve()
    path = (top / rel).resolve()
    if path != top and top not in path.parents:
        raise PipelineError("path outside the data root", 404)
    if not path.exists():
        raise PipelineError(f"not found: {root}/{rel}", 404)
    return path


def list_data(root: str, rel: str = "", base: str = ".") -> Listing:
    """GET /api/data/list: folders first, then CSV/PNG/log files, newest first."""
    path = resolve(root, rel, base)
    if not path.is_dir():
        raise PipelineError("not a folder", 404)
    dirs, files = [], []
    for e in os.scandir(path):
        if e.is_dir():
            dirs.append(e.name)
        elif e.name.lower().endswith(SHOWN):
            st = e.stat()
            files.append(FileEntry(name=e.name, size=st.st_size, mtime=st.st_mtime))
    return Listing(root=root, path=rel, dirs=sorted(dirs), files=sorted(files, key=lambda f: -f.mtime))


def default_axes(name: str, numeric: list[str]) -> Axes:
    """Suggested plot axes for a measurement file, by its prefix."""
    for prefix, x, ys, logy in DEFAULT_AXES:
        if name.startswith(prefix):
            return Axes(x=x if x in numeric else None, y=[y for y in ys if y in numeric], logy=logy)
    return Axes(x=numeric[0] if numeric else None, y=numeric[1:2], logy=False)


def read_table(root: str, rel: str, abs_values: bool = False, base: str = ".") -> Table:
    """GET /api/data/table: a CSV as columns -> values (NaN -> null); abs_values gives |value| of every
    numeric column, for a log axis."""
    path = resolve(root, rel, base)
    if path.suffix.lower() != ".csv":
        raise PipelineError("not a CSV file")
    df = pd.read_csv(path)
    truncated = len(df) > MAX_POINTS
    df = df.head(MAX_POINTS)
    numeric = [str(c) for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    if abs_values:
        df[numeric] = df[numeric].abs()
    return Table(
        name=path.name,
        columns=[str(c) for c in df.columns],
        numeric=numeric,
        n_rows=len(df),
        truncated=truncated,
        abs_values=abs_values,
        data=clean({str(c): df[c].tolist() for c in df.columns}),
        axes=default_axes(path.name, numeric),
    )


def read_log(root: str, rel: str, base: str = ".", max_bytes: int = 2_000_000) -> str:
    """GET /api/data/log: a run log as text (first 2 MB)."""
    path = resolve(root, rel, base)
    if path.suffix.lower() != ".log":
        raise PipelineError("not a log file")
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read(max_bytes)


def image_path(root: str, rel: str, base: str = ".") -> Path:
    """GET /api/data/image: the path of a saved PNG figure."""
    path = resolve(root, rel, base)
    if path.suffix.lower() != ".png":
        raise PipelineError("not a PNG")
    return path
