"""Typed request and response models: the API contract shown at /docs, and strict-JSON conversion."""

import math
import re
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator

_OFFSET = re.compile(r"^\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?)\s*,\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?)\s*$")


def clean(obj: Any) -> Any:
    """Recursively convert to strict-JSON types: NaN/inf -> None, numpy scalars and arrays -> Python."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return clean(obj.tolist())
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    return str(obj)


def _csv(value: str | None) -> list[str] | None:
    items = [v.strip() for v in (value or "").split(",") if v.strip()]
    return items or None


# --- requests ---------------------------------------------------------------------------------------------
class RunOptions(BaseModel):
    """Which devices of the config to visit; the same choices as the run_map CLI flags."""

    model_config = ConfigDict(extra="forbid")

    only: str | None = Field(None, description="comma list: Column:L (A:2,B:2) or device_id (R42C29R)")
    columns: str | None = Field(None, description="comma list, e.g. 'A,B,C' or '28,29'")
    L_values: str | None = Field(None, description="comma list of channel lengths in um, e.g. '2,1,0.5'")
    limit: int | None = Field(None, ge=1, description="measure only the first N planned devices")
    origin: str | None = Field(None, description="device the probes are landed on now (default: the config's)")
    origin_offset: str | None = Field(None, description="'X,Y': layout um of the die the probes are on now")
    keep_order: bool = Field(False, description="visit the 'only' devices in the order given")

    @field_validator("only", "columns", "L_values", "origin", "origin_offset", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("origin_offset")
    @classmethod
    def _offset(cls, v: str | None) -> str | None:
        if v is not None and not _OFFSET.match(v):
            raise ValueError("origin_offset must be 'X,Y' in layout um, e.g. '0,8000'")
        return v

    def run_kwargs(self) -> dict[str, Any]:
        """The keyword arguments of measure.run_map.run (and build_plan) these options stand for."""
        out: dict[str, Any] = {}
        if self.only:
            out["only"] = self.only
        if cols := _csv(self.columns):
            out["columns"] = cols
        if ls := _csv(self.L_values):
            out["L_values"] = ls
        if self.limit:
            out["limit"] = self.limit
        if self.origin:
            out["origin"] = self.origin.strip()
        if self.origin_offset:
            m = _OFFSET.match(self.origin_offset)
            out["origin_offset"] = (float(m.group(1)), float(m.group(2)))
        if self.keep_order:
            out["keep_order"] = True
        return out


class PreviewRequest(BaseModel):
    cfg: dict[str, Any] = Field(description="a run config, as in configs/*.json")
    options: RunOptions = RunOptions()
    live: bool = Field(False, description="preview against the config's data_dir instead of the dry-run folder")


class RunRequest(PreviewRequest):
    config_name: str | None = Field(None, max_length=200)


class ConfirmRequest(BaseModel):
    text: str = Field(max_length=200, description="the origin device at the live gate, 'yes' at the alignment check")


# --- responses --------------------------------------------------------------------------------------------
class Health(BaseModel):
    ok: bool
    allow_live: bool
    dryrun_data_dir: str


class Validation(BaseModel):
    ok: bool
    errors: list[str]
    experiment: str | None
    smu_tool: str | None


class Site(BaseModel):
    site: str
    x: float = Field(description="stage x from the origin device, um")
    y: float = Field(description="stage y from the origin device, um")
    L: float | None = Field(None, description="channel length, um")
    origin: bool


class PlanEntry(BaseModel):
    site: str
    key: str
    L: float | None
    W: float | None
    tx: float
    ty: float
    dx: float
    dy: float
    hops: int
    segment: str | None


class Preview(BaseModel):
    origin: str
    multi: bool
    sites: list[Site]
    measured: list[str] = Field(description="sites with data already, skipped by the run")
    plan: list[PlanEntry]
    total_travel_um: float = Field(description="sum of the planned step lengths, um")
    log: list[str]


class RunStarted(BaseModel):
    run: str


class RunStatus(BaseModel):
    model_config = ConfigDict(extra="allow")
    state: Literal["idle", "starting", "running", "awaiting_confirm", "stopping", "done", "stopped", "failed"]
    run: str | None
    allow_live: bool
    seq: int
    expect: str | None = None


class Ok(BaseModel):
    ok: bool = True


class FileEntry(BaseModel):
    name: str
    size: int
    mtime: float


class Listing(BaseModel):
    root: str
    path: str
    dirs: list[str]
    files: list[FileEntry]


class Axes(BaseModel):
    x: str | None
    y: list[str]
    logy: bool


class Table(BaseModel):
    name: str
    columns: list[str]
    numeric: list[str]
    n_rows: int
    truncated: bool
    abs_values: bool = Field(description="numeric columns are |value| (for a log axis)")
    data: dict[str, list[Any]]
    axes: Axes
