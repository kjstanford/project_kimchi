# Architecture

```
browser (webapp/static: index.html, app.js, style.css, Plotly)
   │  HTTP + Server-Sent Events, JSON only
webapp/app.py        FastAPI routes; no logic beyond request checks
   ├─ webapp/configs.py   list/load/validate configs, plan preview    → measure.run_map, measure.device_map
   ├─ webapp/runs.py      RunManager: one run, a thread, events, gates → measure.run_map.run
   ├─ webapp/data.py      read-only data roots, CSV → columns          → pandas
   └─ webapp/jsonsafe.py  NaN/numpy → strict JSON, used by all of the above
measure/             the measurement package (unchanged except the optional hooks of run_map.run)
   ├─ run_map.py          plan, move, measure, summarise; CLI entry point
   ├─ device_map.py       layout CSV → devices → serpentine → stage moves
   ├─ idvg_pipeline.py, fet_idvg*.py, gc_*.py, stress_stair.py   experiments
   └─ instruments/        prober, b1500, k4200, k4200_pmu drivers; fakes.py for dry-runs
```

## Rules
- **One pipeline.** Every run, from the CLI or the web, goes through `measure.run_map.run`. The web passes hooks
  (`log_sink`, `confirm_fn`, `stop_event`, `on_plan`, `on_row`); with none it is the CLI, unchanged.
- **One owner of the bench.** `RunManager` refuses a second run while one is active (one GPIB bus, one chuck).
- **Safety stays in Python.** Live runs need `--allow-live` (localhost only). Both gates are run_map's own
  `interactive` prompts, answered through `confirm_fn`. The server checks the typed text.
- **Data is read-only for the web.** `webapp/data.py` only lists and reads inside fixed roots. Runs write as
  run_map always has: dry-runs to `.ignore/web_dryrun/`, live runs to the config's `data_dir`.
- **Events are the interface of a run.** `{seq, run, type: state | log | plan | row | confirm, ...}`, kept in
  memory, replayed from any `seq` (page reloads, several tabs).

## Run states
`idle → starting → running ⇄ awaiting_confirm → stopping → done | stopped | failed`

## Where a change goes
| Change | Place |
|---|---|
| New experiment | `measure/` + `run_map.EXPERIMENTS`; the web picks it up through `validate` and `run` |
| New default plot axes for a file type | `webapp/data.py` `DEFAULT_AXES` |
| New run option | `webapp/configs.py` `RUN_OPTIONS` (must be a `run_map.run` argument) + one input in `index.html` |
| New page element | `webapp/static/` only |
