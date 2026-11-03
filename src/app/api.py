"""HTTP routes only: each calls one entry function of app.pipeline. Also serves the page and /vendor/.

In the container: uvicorn app.api:app (dry-run only). On the lab PC: python -m app --allow-live (app/__main__.py).
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import pipeline
from app.models import (
    ConfirmRequest,
    Health,
    Listing,
    Ok,
    Preview,
    PreviewRequest,
    RunRequest,
    RunStarted,
    RunStatus,
    Table,
    Validation,
)
from app.pipeline import PipelineError
from app.runs import RunManager

WEB = Path(__file__).resolve().parents[1] / "web"
VENDOR_DIR = Path(os.environ.get("VENDOR_DIR", "/opt/vendor/node_modules"))


def _plotly_js() -> Path | None:
    """plotly.min.js from the image's frontend libraries, else (native lab-PC mode) the plotly Python package."""
    p = VENDOR_DIR / "plotly.js-dist-min" / "plotly.min.js"
    if p.is_file():
        return p
    try:
        import plotly
    except ImportError:
        return None
    p = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
    return p if p.is_file() else None


def create_app(manager: RunManager | None = None, allow_live: bool = False) -> FastAPI:
    """The app around one RunManager (one bench). allow_live only from the native lab-PC launcher."""
    mgr = manager or RunManager(allow_live=allow_live)
    app = FastAPI(
        title="Wafer-probe runs",
        version="0.2.0",
        description="Plan, dry-run and run S200 + B1500A / 4200A device maps; browse saved data.",
    )
    app.state.runs = mgr

    @app.exception_handler(PipelineError)
    async def _pipeline_error(_: Request, e: PipelineError) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=e.status)

    @app.get("/api/health")
    def health() -> Health:
        return pipeline.health(mgr)

    @app.get("/api/configs")
    def list_configs() -> list[str]:
        return pipeline.list_configs()

    @app.get("/api/configs/{name}")
    def get_config(name: str) -> dict[str, Any]:
        return pipeline.get_config(name)

    @app.post("/api/validate")
    def validate(cfg: dict[str, Any]) -> Validation:
        return pipeline.validate_config(cfg)

    @app.post("/api/preview")
    def preview(req: PreviewRequest) -> Preview:
        return pipeline.preview_plan(req, mgr)

    @app.post("/api/runs")
    def start_run(req: RunRequest) -> RunStarted:
        return pipeline.start_run(req, mgr)

    @app.get("/api/runs/status")
    def run_status() -> RunStatus:
        return pipeline.run_status(mgr)

    @app.post("/api/runs/stop")
    def stop_run() -> RunStatus:
        return pipeline.stop_run(mgr)

    @app.post("/api/runs/confirm")
    def confirm_run(req: ConfirmRequest) -> Ok:
        return pipeline.confirm_run(req, mgr)

    @app.post("/api/runs/decline")
    def decline_run() -> Ok:
        return pipeline.decline_run(mgr)

    @app.get("/api/runs/events")
    def run_events(since: int = Query(0, ge=0), timeout: float = Query(0.0, ge=0, le=30)) -> list[dict[str, Any]]:
        return pipeline.run_events(mgr, since, timeout)

    @app.get("/api/runs/stream")
    async def run_stream(since: int = Query(0, ge=0)) -> StreamingResponse:
        async def gen():
            seq = since
            while True:
                evs = await asyncio.to_thread(pipeline.run_events, mgr, seq, 15.0)
                if not evs:
                    yield ": keep-alive\n\n"
                for e in evs:
                    seq = e["seq"]
                    yield f"id: {seq}\ndata: {json.dumps(e)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/data/roots")
    def data_roots() -> list[str]:
        return pipeline.data_roots()

    @app.get("/api/data/list")
    def data_list(root: str, path: str = "") -> Listing:
        return pipeline.list_data(root, path)

    @app.get("/api/data/table")
    def data_table(root: str, path: str, abs: bool = False) -> Table:  # noqa: A002 (query name)
        return pipeline.read_table(root, path, abs)

    @app.get("/api/data/log", response_class=PlainTextResponse)
    def data_log(root: str, path: str) -> str:
        return pipeline.read_log(root, path)

    @app.get("/api/data/image")
    def data_image(root: str, path: str) -> FileResponse:
        return FileResponse(pipeline.image_path(root, path), media_type="image/png")

    @app.get("/vendor/plotly.min.js", include_in_schema=False)
    def plotly_js() -> FileResponse:
        p = _plotly_js()
        if p is None:
            raise PipelineError("plotly.min.js is not installed", 404)
        return FileResponse(p, media_type="application/javascript")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app


app = create_app()
