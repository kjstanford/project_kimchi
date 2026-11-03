"""FastAPI app: the HTTP interface between the browser and measure/ (via configs, runs, data).

    python -m webapp                     dry-run only, http://127.0.0.1:8300
    python -m webapp --allow-live        lab PC only: live runs allowed, bound to 127.0.0.1
"""
import argparse
import asyncio
import json
import os

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from webapp import configs, data
from webapp.jsonsafe import clean
from webapp.runs import RunError, RunManager

STATIC = os.path.join(os.path.dirname(__file__), 'static')
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_HOSTS = ('127.0.0.1', 'localhost', '::1')
PLOTLY_CDN = 'https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js'


def _plotly_js():
    try:
        import plotly
    except ImportError:
        return None
    path = os.path.join(os.path.dirname(plotly.__file__), 'package_data', 'plotly.min.js')
    return path if os.path.isfile(path) else None


def _bad(e, code=400):
    raise HTTPException(status_code=code, detail=str(e))


def create_app(manager=None, allow_live=False):
    mgr = manager or RunManager(allow_live=allow_live)
    app = FastAPI(title='Wafer-probe measurements')
    app.state.runs = mgr

    @app.get('/api/health')
    def health():
        return {'ok': True, 'allow_live': mgr.allow_live, 'dryrun_data_dir': mgr.dryrun_data_dir}

    # --- configs and plan -----------------------------------------------------------------------------
    @app.get('/api/configs')
    def list_configs():
        return configs.list_configs()

    @app.get('/api/configs/{name}')
    def get_config(name: str):
        try:
            return configs.load_config(name)
        except FileNotFoundError as e:
            _bad(f"no such config: {e}", 404)
        except ValueError as e:
            _bad(e)

    @app.post('/api/validate')
    def validate(cfg: dict = Body(...)):
        return configs.validate(cfg)

    @app.post('/api/preview')
    def preview(body: dict = Body(...)):
        cfg = body.get('cfg')
        v = configs.validate(cfg)
        if not v['ok']:
            _bad('; '.join(v['errors']))
        data_dir = cfg['data_dir'] if body.get('live') else mgr.dryrun_data_dir
        try:
            return clean(configs.preview(cfg, body.get('options'), data_dir))
        except (ValueError, KeyError) as e:
            _bad(f"{type(e).__name__}: {e}")

    # --- runs -----------------------------------------------------------------------------------------
    @app.post('/api/runs')
    def start_run(body: dict = Body(...)):
        cfg = body.get('cfg')
        v = configs.validate(cfg)
        if not v['ok']:
            _bad('; '.join(v['errors']))
        try:
            run_id = mgr.start(cfg, live=bool(body.get('live')), options=body.get('options'),
                               config_name=body.get('config_name'))
        except RunError as e:
            _bad(e, 409)
        except ValueError as e:
            _bad(e)
        return {'run': run_id}

    @app.get('/api/runs/status')
    def run_status():
        return mgr.status()

    @app.post('/api/runs/stop')
    def stop_run():
        try:
            mgr.stop()
        except RunError as e:
            _bad(e, 409)
        return mgr.status()

    @app.post('/api/runs/confirm')
    def confirm_run(body: dict = Body(...)):
        try:
            mgr.confirm(body.get('text', ''))
        except RunError as e:
            _bad(e, 409)
        return {'ok': True}

    @app.post('/api/runs/decline')
    def decline_run():
        try:
            mgr.decline()
        except RunError as e:
            _bad(e, 409)
        return {'ok': True}

    @app.get('/api/runs/events')
    def run_events(since: int = 0, timeout: float = Query(0.0, ge=0, le=30)):
        return mgr.events_since(since, timeout)

    @app.get('/api/runs/stream')
    async def run_stream(since: int = 0):
        async def gen():
            seq = since
            while True:
                evs = await asyncio.to_thread(mgr.events_since, seq, 15.0)
                if not evs:
                    yield ': keep-alive\n\n'
                for e in evs:
                    seq = e['seq']
                    yield f"id: {seq}\ndata: {json.dumps(e)}\n\n"
        return StreamingResponse(gen(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})

    # --- data (read-only) -----------------------------------------------------------------------------
    @app.get('/api/data/roots')
    def data_roots():
        return data.roots()

    @app.get('/api/data/list')
    def data_list(root: str, path: str = ''):
        try:
            return data.listdir(root, path)
        except data.DataError as e:
            _bad(e, 404)

    @app.get('/api/data/table')
    def data_table(root: str, path: str):
        try:
            return data.read_table(root, path)
        except data.DataError as e:
            _bad(e, 404)

    @app.get('/api/data/log', response_class=PlainTextResponse)
    def data_log(root: str, path: str):
        try:
            return data.read_text(root, path)
        except data.DataError as e:
            _bad(e, 404)

    @app.get('/api/data/image')
    def data_image(root: str, path: str):
        try:
            p = data.resolve(root, path)
        except data.DataError as e:
            _bad(e, 404)
        if not p.lower().endswith('.png'):
            _bad("not a PNG")
        return FileResponse(p, media_type='image/png')

    # --- frontend -------------------------------------------------------------------------------------
    @app.get('/')
    def index():
        return FileResponse(os.path.join(STATIC, 'index.html'))

    @app.get('/vendor/plotly.min.js')
    def plotly_js():
        """Plotly from the installed plotly package (works offline on the lab PC), else the pinned CDN copy."""
        path = _plotly_js()
        if path:
            return FileResponse(path, media_type='application/javascript')
        return RedirectResponse(PLOTLY_CDN)

    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8300)
    ap.add_argument('--allow-live', action='store_true',
                    help='allow live runs on the real instruments (lab PC only; binds to 127.0.0.1)')
    a = ap.parse_args(argv)
    if a.allow_live and a.host not in LOCAL_HOSTS:
        ap.error("--allow-live serves only on this machine: use --host 127.0.0.1")
    os.chdir(REPO)                 # configs and layouts use paths relative to the repository root
    import uvicorn
    print(f"{'LIVE RUNS ALLOWED' if a.allow_live else 'Dry-run only'}: http://{a.host}:{a.port}/", flush=True)
    uvicorn.run(create_app(allow_live=a.allow_live), host=a.host, port=a.port, log_level='warning')
