# Capstone check-in

**Plan:** [PROMPT.md](../PROMPT.md), the project specification, as of commit 12e0a1f (branch
`k4200-pmu-stress-stair`; `main` is at 804aa92).

TODO(KJ):

- Decide whether to also link the plan you submitted on 12 October. PROMPT.md was written on 18 November to
  describe the whole project, which was planned step by step against the instruments.
- Add a line to your work log that points here.

## What works

Everything below ran on 18 November 2026 on the lab PC (Windows, Docker Desktop, Git Bash), with no instrument
connected to any command. The logs are in `.ignore/evidence_*.log`, which is gitignored.

- **The gate:** `make verify` at 3f2d185 took 6 min 27 s.
  - lint: `All checks passed!`, `17 files already formatted`
  - tests: `91 passed, 1 warning in 364.51s`
  - smoke: `smoke OK: 144 sites, plan A:2 B:2 C:2, 400 um, Plotly served locally`
- **A reviewer's path, from a fresh clone, with no lab, no licence and no mail settings:** `make reviewer-check`
  at 12e0a1f, exit 0, 7 min 42 s. It clones the pushed branch into `.ignore/reviewer_clone/` and copies
  `.env.example` to `.env`, then runs `make build`, `make verify`, `make up`, `make down`, a CLI dry run and
  `make clean`, and deletes the clone.
  - verify in the clone: `91 passed, 1 warning in 367.88s`, then the same smoke line as above
  - `make up` prints `App: http://127.0.0.1:8300/  (API contract: /docs)`
  - The clone used its own compose project, `reviewer_clone`, and `make clean` removed its image.
- **The CLI on the fake instruments:** `make dry-run ARGS="--config configs/lc500_idvg.json --limit 3"` at
  12e0a1f, run inside the reviewer clone.
  - Output: `A:2: measured`, `B:2: measured`, `C:2: measured`, each move error `(+0.000, +0.000) um`, and the
    summary written to `.ignore/cli_dryrun/…/summary_20261118_224605_dryrun.csv`.
  - `make dry-run ARGS="--live"` and `ARGS="--data-dir data"` are both refused (exit 2), so the CLI cannot reach
    an instrument or a data folder from the container.
- **The web app in a browser:** `make e2e` at 12e0a1f took 1 min 59 s and printed `E2E PASSED`, with 13 checks
  passing. Among them:
  - `the page says it is dry-run only`
  - `the Live switch is disabled without --allow-live`
  - `the preview plans 3 devices`
  - `all 3 devices measured`
  - `3 devices coloured measured on the map`
  - `the curve is |I| on a log axis, from the backend`
  - `no page errors`
- **Each pipeline's test:** the README's "Checks" section lists them, and all 91 are in the runs above.
  - `tests/test_measure_dryrun.py` holds the measurement package's 38 dry-run tests: Id-Vg, stress staircase,
    gain cells, motion and drift, notifications.
  - `tests/test_pipeline.py`, `test_runs.py` and `test_api.py` cover the web app, including the differential
    test against `run_map.run`.
  - `test_plot_idvg.py`, `test_idvg_metrics_box.py`, `test_plot_stress_all.py`, `test_plot_gc_ret_all.py` and
    `test_sample_devices.py` cover the analysis and run-preparation tools.
- **Live runs on the bench:** done with the CLI on the lab PC (a 535-device run on 18 November, for example). The
  data is unpublished and gitignored. TODO(KJ): what you can show from a live run without the data, such as a
  screenshot of the plan, or a run log with the values removed.
- **Not run here:** `make up-live`, `make run-cli`, `make notify-test` and `make layout-localbg-v5`. These are
  lab-PC-only targets that need the `qcodes` or `gds` conda env, and Docker cannot reach GPIB. They were left out
  on purpose, because a stray live command must never reach the instruments.

## What failed, and what I learned

TODO(KJ): pick one and write it. Candidate facts from `git log --since=2026-10-19`, with the evidence:

- 18 Nov, 12e0a1f: `make reviewer-check` failed on its first run with `sh: line 3: C:/Users/Koustav: No such file
  or directory` (exit 127), because `$(MAKE)` expands to a path with a space and wasn't quoted. Quoting it fixed
  the failure. Only a fresh-clone run showed it.
- 18 Nov, af9e5b7: the sample4 plan test failed with `Origin device BR-R15C14 is not in the device CSV`.
  `sample_devices --include` dropped devices that were already measured, though `run_map` needs the probes'
  device as the origin. The CSV now keeps every `--include` device, and resume skips the measured ones.
- 5–6 Nov, 54d3706: a live run lost several devices in a row to probing before anyone noticed. That led to
  email at run end and an alert after N failures in a row (`alert_after_fails`).
- 9 Nov, 598d893 / c1e635d / 15b16b2: three figure tools shipped "(no tests yet)" to meet a deadline. Their
  tests and the VTR extraction came the next day (eb7241e, 8cc6ab8).
- 15–16 Oct, before this window (cc0c8a5): the lab's KULT `bias_stress` module gave no valid data, and the first
  PMU read scheme was ruled out by a diagnostic. The stress test was rebuilt as staircase reads over `:PMU:SARB`.

## What I am cutting, and why

TODO(KJ): decide, then write the one sentence the 30 November draft will still claim. Candidates from the
README milestones and PROMPT.md "Not now":

- Milestone 3: an analysis page in the app (Vt roll-off and TLM Rc·W from `measure.anneal_series`). The code
  exists offline (ede8e85), but there is no page.
- Milestone 4: default plot axes for gain-cell PMU and stress-staircase files.
- LAN (TCP/IP) as well as GPIB; more device structures; more analyzers or stations.
- Milestone 2, "first live run from the web app": the live runs so far went through the CLI.
  TODO(KJ): confirm.

## Built to throw away, and built to last

TODO(KJ): choose one of each. Candidate facts:

- **Shortcuts, each with a reason it is fine for now:**
  - per-run resume and remeasure configs in `configs/` (e.g. `*_resume_idvg.json`), one per interrupted run;
  - `uv` installed from PyPI rather than ghcr.io (ARCHITECTURE.md departures);
  - the hand-run evidence logs in `.ignore/`.
- **Parts to keep:**
  - The single engine, `measure.run_map.run`, with the strict fakes. It is tested by the differential tests
    (`test_dry_run_equals_direct_run`, `test_preview_is_the_cli_plan`) and by the 38 dry-run tests, and
    documented in ARCHITECTURE.md ("Backend") and docs/0003.
  - The device maps from GDS (`layouts/make_*.py`). They check themselves against the mask labels, and
    `test_localbg_v5_region1_map` checks them against a matrix typed in by hand.
