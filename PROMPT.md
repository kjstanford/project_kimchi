Follow AGENTS.md.

<!--
Written on 2026-11-18 to describe this codebase as a whole: the plan that asks for what it does. The project was
built step by step against real instruments; this states the result as one specification. Drafted by Claude Code;
Koustav reviews and owns it. TODO(KJ) marks what is his to decide or confirm.
-->

# Probe-station automation for semiconductor device research

## 1. Context

**Who it is for.** Device researchers in academia with a FormFactor probe station (the Cascade S200, or another
FormFactor station driven by the same Velox remote commands) and one of the two most common parameter analyzers,
a Keysight B1500A or a Keithley 4200A-SCS. They measure many devices per die and compare process splits. Some
script their work; many do not, and use the instruments only through the vendor GUIs. The project serves both,
through two modes over the same code: a command line for those who script, and a web app for those who don't.

**The problem.** Measuring hundreds of devices on a die by hand takes days, and every lab writes its own scripts.
Unattended runs fail silently: the chuck drifts until the probes leave the pads, a run dies overnight and nobody
knows until morning, or hours go into full sweeps of devices that were dead from the first point.

**The decision it serves.** Which devices on a die work, and their figures of merit (Vt, on/off, leakage, mobility,
subthreshold swing, Vt shift under stress, gain-cell retention), from enough devices to compare process splits
with statistics rather than a few devices picked by hand.

**What researchers do today.** Land the probes on each device by hand and run one test at a time in the vendor
software (Clarius on the 4200A, EasyEXPERT on the B1500A), or keep a private script that drives one station with
one analyzer.

**Terms.**
- *Device map (device CSV)*: every device of a die, extracted from the mask layout (GDS): id, layout x and y in µm,
  W and L in µm.
- *Origin device*: the device the probes are landed on by hand before a run. Every move is relative to it.
- *Serpentine*: row by row, alternating direction, so that consecutive devices are neighbours.
- *Initial scan / main sweep*: a short single-Vd Id-Vg to screen a device, then, if it passes, the full multi-Vd,
  multi-cycle double sweep.
- *Vt*: the gate voltage at a constant drain current, by default |Id| = 1e-7 A × W/L.
- *Screened out*: the device answered but failed a screen. *Failed*: no usable answer (open, compliance).
- *Drift*: the chuck's reported position wandering from the planned one. *Separation*: probes lifted off the pads.
- *2T gain cell*: a write FET whose source/drain charges the gate of a read FET (the storage node). Its lines are
  the write and read word and bit lines (WWL, WBL, RWL, RBL).
- *SARB*: the 4225-PMU's segment-arbitrary waveform mode, programmed over KXCI.

## 2. Scope

**First slice, end to end.** From a browser on 127.0.0.1: pick a run config, preview the probe plan on a stage
map (in µm from the origin device, with the total travel and the devices already measured), dry-run it on fake
instruments, and watch each device turn green (measured), amber (screened out) or red (failed) as its summary row
arrives. Then open any saved curve as an interactive plot. The same run from the command line gives the same rows.
Reason: the command line came first and is what runs on the bench, so every run, from the browser or the CLI, goes
through one function, `measure.run_map.run`.

**Riskiest piece first: a live run nobody meant.** A run moves a chuck and puts voltages on real devices.
- Live runs are possible only on the lab PC, which holds the GPIB adapter, with `--allow-live`, on 127.0.0.1.
- A live run needs two typed answers, checked by the server: the origin device, then `yes` after checking the
  alignment at separation height.
- Every way a run ends (done, Stop, Ctrl+C, an error) zeroes the outputs and separates the probes.
- Fallback, if the browser gates cannot be made safe: the web app stays dry-run only, and live runs stay on the CLI.

**Instruments**, all over GPIB today:
- FormFactor S200 (Velox remote commands): chuck moves, heights, position readback.
- Keysight B1500A: SMU sweeps through FLEX.
- Keithley 4200A-SCS: SMU sweeps over KXCI, and 4225-PMU/RPM pulsed waveforms over KXCI SARB.
- A fake for each, as strict as its manual, so that any run can be checked without hardware.

**Device structures and tests.**
- *Three-terminal FET*:
  - an adaptive Id-Vg (initial scan, in-situ screening and Vt, then the main sweep only on devices that pass), on
    either analyzer;
  - a PMU bias-stress test: measure-stress-measure with staircase reads, DC or AC (pulsed) stress, then recovery
    at 0 V.
- *2T gain cell*, on the 4200A:
  - WBL and WWL sweeps;
  - SMU retention;
  - PMU write/read;
  - PMU retention;
  - a screened retention: the two sweeps first, and the retention only if both pass.

**Two modes, one engine.** Both modes run the same experiments, and both go through `measure.run_map.run`, so
they always agree; tests compare the two on every change.

- *CLI* (`python -m measure.run_map`, or `make run-cli` on the lab PC), for researchers who script. It can be
  scripted and batched.
- *Web app* (`make up`, or `make up-live` on the lab PC), for researchers who do not code. In the browser they:
  - pick a config and its devices from forms;
  - see the plan on the stage map before anything moves;
  - follow the run device by device;
  - answer the live gates;
  - browse saved data as interactive plots.

  The only command they need is the one that starts the app.

**Key features.**

1. *In-situ analysis.* Each device is screened from its first sweep: Vt range, gate leakage, too little current
   for its W/L, low on/off, a source-drain short. A failing device is recorded with its reason and skipped, so no
   time goes into full sweeps of faulty devices.
2. *Dry run.* Every config runs on the fake instruments first, with the same plan, moves, checks and output files
   as a live run.
3. *Email notification.* An email when a live run ends, whatever the outcome: done, stopped, interrupted with
   Ctrl+C, an error, or an automatic stop.
   - The email gives the counts by status, the device the probes are above (the next origin), and the last log
     lines.
   - An alert after N devices in a row fail.
   - The SMTP settings live only in `.env`.
4. *Automatic movement-error correction.* Every move's readback is checked, and accumulated drift is cancelled in
   the next move. A landing that drifted too far gets extra correction moves, and the run stops if it still
   cannot land.

**Also.**
- Device maps from GDS layouts, with checks against the labels printed on the mask.
- Resume: devices already measured are skipped.
- `--limit`, `--only` and `--origin`.
- Several arrays or dies in one run.
- A balanced random subset of a large die for a shorter run.
- Figures from saved data: all Id-Vg curves of a die on one plot, box plots of figures of merit per channel
  length, Vt shift against stress time, retention curves.

**Not now (README milestones).**
- LAN (TCP/IP) as well as GPIB.
- More device structures.
- An analysis page in the app.
- Default plot axes for PMU and stress files.
- TODO(KJ): other analyzers or stations, and the order of these.

**Departures from ARCHITECTURE.md**: listed in its last section.

## 3. Data

**Inputs.**
- *Device map* (CSV), one row per device:

  | Column | Unit | Notes |
  |---|---|---|
  | device id | | e.g. `R42C19R` or `TL-R01C01` |
  | row, column | | |
  | x, y | µm | layout frame |
  | W, L | µm | |

  The maps are made from the GDS by the scripts in `layouts/` (KLayout), which stop if a check fails.
- *Run config* (JSON):
  - the sample and die names, the device map and the origin device;
  - the serpentine start;
  - which SMU is the gate, drain and source;
  - motion limits (µm, %);
  - sweep settings (V, V/step, A compliance);
  - screen thresholds (V, A, ratio).

  A config is validated before any run, and an invalid one is refused with one plain message.

**Outputs**, to the config's data folder, which is gitignored and never modified by any task.
- *Per device*: an Id-Vg CSV with `Drain_Current, Gate_Current, Gate_Voltage, Source_Current` (A, V) and
  per-terminal status codes, plus a PNG.
  - Precision is as returned by the analyzer, 3–7 significant digits.
  - Currents below about 1 pA are noise.
- *Per run*: `summary_<stamp>.csv`, one row per device, with:
  - the ids;
  - `W_um, L_um, x_um, y_um`;
  - `status` (measured / screened_out / failed) and `reason`;
  - `Vt_V`, `on_off`, `Id_max_A` and `Ig_max_A`;
  - the experiment's own columns.

  Plus `run_<stamp>.log`.

**Five rows** of a summary from a dry run on the fake instruments: a model n-FET with Vt 0.3 V, W 10 µm, five
channel lengths. These are not device data.

| key | W_um | L_um | x_um | y_um | status | Vt_V | on_off |
|---|---|---|---|---|---|---|---|
| D:2 | 10 | 2 | 600 | 0 | measured | 0.336 | 7.4e9 |
| L:1 | 10 | 1 | 2200 | 200 | measured | 0.336 | 1.4e10 |
| A:0.7 | 10 | 0.7 | 0 | 400 | measured | 0.336 | 7.6e9 |
| L:0.5 | 10 | 0.5 | 2200 | 600 | measured | 0.336 | 9.1e10 |
| A:0.4 | 10 | 0.4 | 0 | 800 | measured | 0.336 | 3.9e9 |

**Quirks.**
- The Vt of a device that barely turns on is extrapolated, and can land far outside the sweep. It is
  range-checked, not trusted.
- `on_off` is `inf` when the off current reads exactly 0.

## 4. Method

- **Vt:** |Id| = 1e-7 A × W/L on the forward branch, interpolated. The current and the screen thresholds are
  config values.
- **Screens:**
  - Vt within [Vt_min, Vt_max];
  - |Ig| ≤ Ig_max;
  - max |Id| above an absolute minimum and a minimum per W/L;
  - on/off ≥ on_off_min;
  - a source-drain short: Id ≥ short_Id_min with on/off ≤ short_on_off_max.
- **Motion:**
  - layout → stage transform (rotation, mirroring), with moves relative to the origin device;
  - moves longer than `max_step_um` split into hops;
  - a bounds margin;
  - a readback check after every move (5 µm or 3 %);
  - a drift guard that stops the run above 20 µm accumulated;
  - correction when an axis reaches ±`drift_correct_um`;
  - extra moves when a landing is beyond ±`drift_limit_um`.
- **Figures of merit:**
  - µFE from max gm and the gate stack's Cox;
  - SS from the steepest 3-point fit above 10 pA;
  - VTR = VT,on − VT,off by the raw-Id method (K. Jana et al., Eqs. 6–9).
- **Bias stress:** ΔVt relative to each run's own pre-stress staircase, at a stated read delay.
- **PMU waveforms:**
  - segments, sequences and timing limits follow the KXCI manual (4200A-KXCI-907-01E) and the PMU manual
    (4200A-PMU-900-01C);
  - the sample rate KXCI actually uses follows a model fitted to the rates the instrument reports
    (`k4200_pmu.kxci_rate`).
- **Device subset:** the sites (row, column) are visited in random order. A site's missing copies come from the
  blocks with the fewest devices so far, with ties broken by a seeded random number.
- **Conventions:**
  - µm in the layout frame, +x right and +y up;
  - currents in A with the analyzer's sign, plotted as |I|;
  - voltages in V.

## 5. Truths

Each truth is checked by a test in `make verify`, unless it is marked live. TODO(KJ): confirm that you checked
each source yourself, and the tolerances.

| Kind | Truth | Tolerance and why | Source (independent of the AI) |
|---|---|---|---|
| Known value | a 144-device test map: the device count, and 400 µm of travel over three neighbouring devices 200 µm apart | exact: counts and the layout pitch | the layout, counted by hand |
| Known value | a mask's printed L × W matrix matches the generated device map, cell by cell | exact | the labels on the mask, typed in by hand |
| Known value | an analytic transistor Id = βVd·m·ln(1 + e^((Vg−VT)/m)): VT, µFE, SS, VT,on, VT,off | VT 5 mV, µFE and SS 1 %, VT,on and VT,off 10 mV, because of the 50 mV voltage grid | derived by hand |
| Independent method | the web plan equals `run_map.build_plan` (order, count, steps, travel); a web dry-run gives the same rows as `run_map.run` | exact | the CLI, used on the bench before the app existed |
| Independent method (live) | `kxci_rate()` reproduces the sample rates the 4200A reported in six live tests | < 1 S/s | the instrument's own console messages |
| Invariance | the same seed gives the same subset; every site is in exactly N blocks; the blocks are within 1 device of each other | exact | counting |
| Limiting case | the hops of a split move sum to the planned step | 0.01 µm | arithmetic |
| Rejection | live without `--allow-live` or off 127.0.0.1; a wrong typed answer at a gate; an invalid config; a path outside its root; output into a data folder; a PMU waveform that breaks a manual limit (e.g. a 2 µs plateau on the 1 µA range, which needs 20.4 µs) | refused with one plain message, and nothing sent | AGENTS.md, the manuals |
| A plot would hide | accumulated drift stops a run although every single move is within tolerance; the SMTP password never reaches a log | exact | the test logs |
