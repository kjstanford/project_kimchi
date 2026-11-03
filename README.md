# Wafer-probe measurements, with a web app

Automated electrical measurements of IGZO transistors and 2T gain cells on a Cascade S200 prober with a Keysight
B1500A or a Keithley 4200A-SCS. You plan, dry-run, run and browse them from a browser.
It is for the researcher at the probe station, and for anyone reading that researcher's data afterwards.

```bash
make serve PY=.venv/bin/python      # dry-run only: fake instruments, http://127.0.0.1:8300/
```

On the lab PC, put the qcodes env on PATH first (see AGENTS.md), then use `PY="$HOME/anaconda3/envs/qcodes/python.exe"`.

## Why
A run used to be a command line and a scrolling terminal, and checking the data meant opening CSVs by hand. The
app shows the plan on the wafer map before anything moves: every device, the serpentine path, the origin and
the devices already measured. It streams the run (log, summary rows, the latest curve) and opens any saved
file as an interactive plot. The measurement code is unchanged: the app is a layer over `measure/`, and the CLI
(`python -m measure.run_map`) still works exactly as before.

## What it does
| Tab | What you see |
|---|---|
| Run | Pick a `configs/*.json` (optionally edit it for this run), choose devices (`only`, columns, L, limit, origin). The config is checked with the same checks a run makes. The plan appears on the stage map, in µm from the origin device. Start runs a dry-run on the fake instruments; devices turn green or red on the map as their summary rows arrive. Stop ends the run before the next device, through the same cleanup as Ctrl-C: outputs zeroed, probes separated. |
| Data | Read-only browser for `data/`, `data_k4200/`, `data_k4200_stress/` and the dry-run folder. A CSV opens as a plot with sensible default axes (e.g. \|Id\|, \|Ig\| vs Vg on a log scale), a PNG as saved, a run log as text. |

### Live runs (lab PC only)
`make serve-live` (`python -m webapp --allow-live`) is the only way to enable them. It binds to 127.0.0.1 only and
is never used in Docker. A live run still asks both of the CLI's questions, now in the browser:
1. The landing: you must type the origin device the probes are on.
2. The alignment at separation height: type `yes`.

Stop or Decline at either gate ends the run before any contact. Dry-run data goes to `.ignore/web_dryrun/`;
only a live run writes to the config's `data_dir`.

## Tests
```bash
make test-web PY=.venv/bin/python
```

```bash
make test-measure PY=.venv/bin/python
```

```bash
make e2e PY=.venv/bin/python
```

`make test-web` takes about 1 min, `make test-measure` about 5 min, and `make e2e` (Chromium) about 20 s.

None of them can reach an instrument: the live-gate tests replace every `connect()` with a failure.

- **Smoke:** the server answers and serves the page.
- **Unit:** every shipped config validates; bad configs, options and paths are rejected; RunManager runs one at a time; live is refused without `--allow-live`; a gate accepts only the right typed text; Stop at a gate answers no.
- **Integration:** a real `run_map.run` dry-run through the API; Stop mid-run separates the probes; an open device is reported, not fatal; a declined live gate never connects.
- **Differential (answer key = the CLI):** the web plan equals `run_map.build_plan` (device order, count and summed step, the tripwire). A web dry-run gives the same rows, value for value (Vt, on/off), as a direct `run_map.run`.
- **End-to-end:** a user picks `lc500_idvg.json`, previews 3 devices, runs them and sees 3 rows, 3 green devices and the curve.
- **Checked by failing:** with Stop ignored in `run_map`, or with any text accepted at a gate, the tests fail.

## Where the backend/frontend boundary is
At the HTTP API in `webapp/app.py`. The browser sends a config and device options and receives JSON: plan, events,
rows, tables. Every decision about safety, plans and physics is made in Python, in `measure/` and `webapp/`.

If it were drawn one function earlier (the browser building the plan or deciding the gates), two things would break:
- The plan you approve could differ from the plan `run_map` executes.
- The typed confirmation could be bypassed by any HTTP client.

## Milestones
1. ✅ Run hooks in `run_map.run` (log sink, confirm, stop, plan/row callbacks), RunManager, dry-run console with Stop.
2. ✅ Config validation and the wafer-map plan preview.
3. ✅ Read-only data explorer.
4. ✅ Live gating in code. ⏳ First live run on the lab PC, with the owner's explicit approval.
5. ⏳ Analysis page: Vt roll-off and TLM Rc·W from `measure/anneal_series.py`.
6. ⏳ Gain-cell and stress-staircase views: default axes for PMU waveforms and dVt(t).

## Top risks
1. **A live run the user did not mean.** Live is off unless the server is started with `--allow-live` on localhost; both gates need typed answers checked on the server; the stop path is the CLI's cleanup.
2. **The app and the CLI drift apart.** The app calls `run_map.run` itself, never a copy, and the differential tests compare the two on every change.
3. **A long run outlives the browser tab.** The run is in the server, not the page. Reopening the page replays its events, and closing the tab doesn't stop the run, so Stop is explicit.

## Layout
See [ARCHITECTURE.md](ARCHITECTURE.md).

## How AI was used
The web layer (`webapp/`, the hooks in `measure/run_map.py`, tests, Makefile, Dockerfile, this README) was written
with Claude Code, from a plan the owner approved. The owner reviews and is accountable for it.
