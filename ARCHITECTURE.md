# Architecture

How this project is put together, and why. It started as the EE292M `starting_point` specification and is kept
true as the project changes. The rules that apply to every task are in AGENTS.md. Decisions worth remembering
are in `docs/`, one short file each; the departures from the course specification are listed at the end.

## Current state

- `src/measure/`: the measurement package (S200 chuck control, B1500A and 4200A drivers, the experiments, fake
  instruments for dry-runs) and its CLI, `measure.run_map`. Its dry-run suite runs under pytest.
  `measure.notify` emails the end of each live run, and failure streaks, over SMTP, with its settings in `.env`
  (docs/0004-email-notifications.md).
- `src/app/` and `src/web/`: the web app. Check a config, preview its probe plan on the stage map, dry-run it on
  the fake instruments (or, on the lab PC only, run it live behind two typed confirmations), follow the run
  live, and browse saved data as interactive plots.
- `docker/`, `Makefile`: the development container and every command, as specified below. `make verify` is green
  (lint, 67 tests, smoke); `make e2e` drives the page in Chromium.
- Not built yet: an analysis page (Vt roll-off, TLM Rc·W from `measure.anneal_series`) and default plot axes for
  the gain-cell PMU and stress-staircase files.

## Stack

- Backend: Python 3.14 (base image `python:3.14-slim`, pinned to the minor version) and FastAPI, with typed
  request and response models; `/docs` is the contract.
- Frontend: static HTML and JavaScript, no build step. Plotly (`plotly.js-dist-min`) draws every figure: the
  stage map and the measured curves need zoom, hover and log axes, and nothing custom enough for D3.
- Everything runs in one development container, as the host user. The single exception is a live run, which
  needs the lab PC's GPIB adapter (docs/0001-live-runs-native.md).

## Layout

```text
docker/              everything that builds the development container, and nothing else; the build context
  Dockerfile         stages uv, node, vendor, e2e, final
  .dockerignore
  compose.yaml       app; lock-py, lock-js (profile tools); e2e (profile e2e)
  pyproject.toml     Python dependencies, requires-python = ">=3.14,<3.15"
  uv.lock
  package.json       frontend libraries (plotly.js-dist-min; playwright for the e2e stage only)
  package-lock.json
  certs/             optional extra CA certificates (*.crt, gitignored) for a TLS-inspecting proxy
src/app/             the backend: api.py (routes only), pipeline.py, models.py, runs.py, __main__.py
src/web/             the page: index.html, app.js, styles.css
src/measure/         the measurement package and its CLI (docs/0002-measure-in-src.md)
tests/               pytest (test_pipeline, test_runs, test_api, test_measure_dryrun), smoke.py, e2e.mjs
docs/                decisions worth remembering, one short file each
configs/             run configs (JSON), one per sample / die / experiment
layouts/             device CSVs from the mask layouts, and the scripts that make them (gds env)
data/ data_k4200/ data_k4200_stress/   measurement output, gitignored, never modified by any task
Makefile  README.md  ARCHITECTURE.md  AGENTS.md  CLAUDE.md
.gitignore  .env.example  ruff.toml  pytest.ini
```

## Backend

- One entry-point function per API endpoint in `pipeline.py` (`preview_plan`, `start_run`, `read_table`, ...).
  The API calls it; tests may call any function.
- `api.py` holds routes only. It also serves the page from `src/web/` and Plotly under `/vendor/`.
- `models.py`: request and response models, and `clean()`, which turns NaN and numpy values into strict JSON.
- `runs.py`: `RunManager`, the one active run (one bench, one GPIB bus) in a background thread, and its events
  `{seq, run, type: state | log | plan | row | confirm}`, replayable from any `seq` (docs/0003-run-manager.md).
- Every run goes through `measure.run_map.run`. The web passes its optional hooks (`log_sink`, `confirm_fn`,
  `stop_event`, `on_plan`, `on_row`); without them it is the CLI, unchanged.
- Run states: `idle → starting → running ⇄ awaiting_confirm → stopping → done | stopped | failed`.
- Live runs: only `python -m app --allow-live` (make up-live, 127.0.0.1) allows them. Both gates are run_map's
  own prompts; the server accepts only the origin device typed back at the first and `yes` at the second.
- Saved data is read-only: fixed roots, every path resolved inside its root. Dry-runs write to
  `.ignore/web_dryrun/`; only a live run writes to the config's `data_dir`.
- Results the page shows are computed here: the plan and its total travel, |I| for log axes (`abs=true`).
- Errors reach the page as one plain message (`{"detail": ...}`), and the page clears any earlier result when
  it shows one: a stale plot never sits beside an error.

## The container

- The Dockerfile's tool stages, `uv` (Python 3.14 + uv from PyPI) and `node`, are where `make lock` runs, so
  the first lock needs no lock. `vendor` installs the frontend libraries from `package-lock.json`; `final`
  builds on `uv` with the Python dependencies from `uv.lock` (into /opt/venv) and the files from `vendor`.
  `e2e` (Playwright's image with Chromium) exists only for `make e2e`.
- The image never copies `src/`. The repository is mounted at `/work`, with `PYTHONPATH=/work/src`.
- Frontend libraries are installed at build time from `package-lock.json` into `/opt/vendor` (the mount would
  hide them inside /work). Never installed on the host, never committed, never loaded from a CDN.
- `compose.yaml` publishes on `127.0.0.1` and runs as the host user (`HOST_UID`/`HOST_GID`, passed in by the
  Makefile; Git Bash reserves `UID`), with `HOME=/tmp` for the tools' caches.
- The Makefile names the compose project after the repository directory (`COMPOSE_PROJECT_NAME`).
- `.env` holds plain `KEY=value` lines; the Makefile includes and exports it.

## Makefile

`make help` (the default) lists every target in groups.

- Everyday: `build`, `up`, `down`, `verify` (lint, test, smoke; must pass from a fresh clone with only Docker,
  Make and git), `clean` (this project's containers and images, test and dry-run output; `data*/` and `.env`
  are kept, and so are shared base images and build cache).
- While developing: `test` (`ARGS=` for a subset), `lint`, `fmt`, `smoke` (starts the app, checks a real
  preview: 144 sites, A:2 B:2 C:2, 400 µm of travel, Plotly served), `e2e`, `logs`, `shell`.
- Only when dependencies change: `lock` records exact versions in `docker/uv.lock` and
  `docker/package-lock.json`. `build` uses only the locks, and says so, with this instruction, when a manifest
  and its lock disagree.
- Lab PC only: `up-live`, `run-cli` and `notify-test` (one test email from the `.env` settings), native, with `PY`
  the `qcodes` env's python (quoted in the recipes, so a path with spaces works).

Every container target runs through compose (`docker compose -f docker/compose.yaml`).

## Departures from the course specification

| Departure | Why |
|---|---|
| `up-live` and `run-cli` run on the host (the qcodes env) | Docker Desktop cannot reach the GPIB adapter; docs/0001 |
| `src/measure/` next to `src/app/` | the existing measurement package, moved with its history; docs/0002 |
| `runs.py` beside api/pipeline/models | a run is long-lived state, not a request pipeline; docs/0003 |
| `data*/` are instrument output, not `data/raw` + `processed` | the lab's folders, which no task may modify |
| `e2e` Dockerfile stage | the browser test needs Chromium, which the app image does not |
| `docker/certs/` | builds behind a TLS-inspecting proxy; empty and inert elsewhere |
| uv installed from PyPI, not ghcr.io | ghcr.io is not reachable from every network this is built on |
