# Wafer-probe runs

Automated electrical measurements of IGZO transistors and 2T gain cells on a Cascade S200 prober with a Keysight
B1500A or Keithley 4200A-SCS, and a web app to plan, dry-run, run and browse them. It is for the researcher at
the probe station, and for anyone reading that researcher's data afterwards.

A run used to be a command line and a scrolling terminal. Now you see the probe plan on the wafer map before
anything moves, follow the run device by device, and open any saved curve as an interactive plot. The
measurement code is the same: every run, from the browser or the CLI, goes through `measure.run_map.run`.

## Run it

You need git, Docker (Docker Desktop on Windows, run `make` from Git Bash) and `make`. Nothing else is
installed on your machine.

```bash
cp .env.example .env
```

```bash
make build
```

```bash
make up
```

Open http://127.0.0.1:8300/ (the API contract is at /docs). `make down` stops it; `make help` lists every target.

In the container the app is **dry-run only**: fake instruments, data written to `.ignore/web_dryrun/`.

## What the page does

| Tab | What you see |
|---|---|
| Run | Pick a config from `configs/` (optionally edit it for this run) and the devices (only, columns, L, limit, origin). "Check & preview plan" runs the checks a run makes and draws the plan on the stage map, in µm from the origin device, with the total travel and the devices already measured. Start runs it; devices turn green (measured), red (failed) or amber (screened out) as their summary rows arrive, with the log and the latest curve. Stop ends the run before the next device, through the CLI's own cleanup: outputs zeroed, probes separated. |
| Data | A read-only browser for `data/`, `data_k4200/`, `data_k4200_stress/` and the dry-run folder. A CSV opens as a plot (Id-Vg files default to \|Id\|, \|Ig\| vs Vg on a log axis), a PNG as saved, a run log as text. |

## Live runs (lab PC only)

Docker cannot reach the GPIB adapter, so live runs use the lab PC's `qcodes` conda env directly (docs/0001).
Once, install the web packages into that env yourself:

```bash
E="$HOME/anaconda3/envs/qcodes"; export PATH="$E:$E/Library/mingw-w64/bin:$E/Library/usr/bin:$E/Library/bin:$E/Scripts:$PATH"
```

```bash
"$E/python.exe" -m pip install fastapi uvicorn plotly
```

Then, with the probes landed on the origin device:

```bash
make up-live PY="$E/python.exe"
```

The Live switch is enabled only in this mode, on 127.0.0.1. A live run asks the CLI's two questions in the
browser: type the origin device the probes are on, then `yes` after checking the alignment at separation height.
Stop or Decline at either ends the run before any contact. A live run writes to the config's `data_dir`.

The CLI still works: `make run-cli PY="$E/python.exe" ARGS="--config configs/lc500_idvg.json --live --limit 2"`.

### Email notifications

A live run, from the CLI or the browser, can email you:

- **When it ends**, whether it finished, was stopped, was interrupted with Ctrl+C, hit an error, or stopped itself.
  The subject gives the outcome and the device count. The body has the counts by status, the device the probes
  are above (the next `--origin`) and the last log lines.
- **Mid-run, when devices keep failing.** With `"alert_after_fails": N` in the config, you get one alert per run of
  N devices in a row that were not measured. The run keeps going.

Dry runs never send email. To set it up once, on the lab PC:

1. Create a Google app password for the sending Gmail account: Google Account > Security > 2-Step Verification >
   App passwords.
2. Fill in `NOTIFY_SMTP_USER`, `NOTIFY_SMTP_PASSWORD` (the app password, no spaces) and `NOTIFY_EMAIL_TO` in `.env`.
   See `.env.example`.
3. Send a test email:

```bash
make notify-test PY="$E/python.exe"
```

A run logs whether email is on, and every email it sends or fails to send. A failed send never stops a run. No email
is sent if the process dies: a closed terminal window, a power cut, or a killed process.

## Plots from saved data

`make plot-idvg` draws every device of one die on one figure: the newest main sweep of each device (initial scans
are not used, so screened-out devices are absent), at one drain voltage, as |Id|·L/W against Vg, with translucent
lines and one colour per channel length whatever the width. It reads `data/` and writes only to `--out`
(`.pptx_figs/` is gitignored).

```bash
make plot-idvg ARGS="--sample wd_bobafet_6802 --out .pptx_figs/wd_bobafet"   # --die 0_0 --vd 0.05 --alpha 0.15
```

## What its results are fit for

The app computes nothing new about the devices: Vt, on/off and the screening flags are those of
`measure.idvg_pipeline` (and the gain-cell and stress modules), exactly as the CLI reports them. The plan is
`measure.run_map.build_plan`'s. Dry-run numbers come from the fake instruments and describe the code, not a
device.

## Checks

`make verify` is the gate (lint, 70 tests, smoke, about 6 min). None of the tests can reach an instrument: every
`connect()` is replaced by a failure.

- Differential, with the CLI as answer key: the web plan equals `run_map.build_plan` (order, count, summed
  steps, total travel); a web dry-run gives the same rows, value for value, as a direct `run_map.run`.
- Known values, by hand: `lc500_idvg.json` has 144 devices; A:2, B:2, C:2 are 200 µm apart, so 400 µm of travel.
- Safety: live refused without `--allow-live` or off localhost; a gate accepts only the right typed text; Stop at
  a gate answers no; a declined live gate never connects; Stop mid-run separates the probes; an open device is
  reported, not fatal.
- The measurement package's own 37 dry-run tests, in Python 3.14. These include the notification emails: their
  content at each kind of run end, one alert per failure streak, a failing sender that leaves the run alone, and
  the password never appearing in a log.
- `measure.plot_idvg_all` on a synthetic die: only the newest main sweep per device, only the chosen Vd, |Id|·L/W
  by hand, one colour per L.
- `make e2e`: a user in Chromium previews and dry-runs 3 devices and sees 3 rows, 3 green devices and the curve.

## Where the backend/frontend boundary is

At the HTTP API (`src/app/api.py`). The page sends a config and device options and gets JSON back: plan, events,
rows, tables. Every decision about plans, safety and physics, and every number shown (the total travel, |I| for a
log axis), is computed in Python. Drawn one function earlier, with the page building the plan or judging the
gates, the plan you approve could differ from the one `run_map` executes, and any HTTP client could skip the
typed confirmation.

## Milestones
1. ✅ Dry-run console, plan preview, data browser, gated live runs in code; the course container and Makefile.
2. ⏳ First live run on the lab PC, with the owner's explicit approval.
3. ⏳ Analysis page: Vt roll-off and TLM Rc·W across the anneal series (`measure.anneal_series`).
4. ⏳ Default axes for gain-cell PMU waveforms and stress-staircase dVt(t).

## Top risks
1. **A live run nobody meant.** Live needs `--allow-live` on localhost and two typed answers checked by the
   server; Stop uses the CLI's cleanup.
2. **The app and the CLI drift apart.** The app calls `run_map.run` itself, and the differential tests compare the
   two on every change.
3. **A long run outlives the tab.** The run lives in the server; a reloaded page replays its events. Closing the
   tab never stops it; Stop does.

## Layout and rules
ARCHITECTURE.md (design, departures from the course layout), AGENTS.md (rules for anyone working here), docs/.

## How AI was used
The web app, the container, the tests and these documents were written with Claude Code from plans the owner
approved; the owner reviews and is accountable for them.
