# Standards for this repository

These rules apply to every task. How the project is built is described in
ARCHITECTURE.md: read it before creating or moving files, adding a dependency,
or changing anything in `docker/`, and keep it true when you change the design.

## Boundaries (these override everything else)

- NOTHING is installed on the host. No packages, tools, libraries, language
  runtimes or global configuration, by any means. If you believe a host
  install is truly unavoidable, stop, explain why no container-based
  alternative works, and wait for my explicit approval of that specific
  install. Expect the answer to be no. The one standing exception is the lab
  PC's `qcodes` conda env, for live runs (below); I install into it myself.
- Every change on the host happens inside this repository. Nothing is written
  outside this directory tree. An agent's own memory or global instruction
  file is the only possible exception, and only when I have agreed.
- Powerful commands (docker, gh, curl, wget, and their like) run only inside
  Makefile targets. Never run them as one-off commands, and never tuck them
  inside a longer command or script. Anything run more than once (testing,
  validation, a data step) belongs in the Makefile; if a task needs a new
  command, add a target and say so.
- Secrets live in `.env`, which is gitignored; commit a `.env.example`.
- Never delete / modify anything inside `data`, `data_k4200`, and
  `data_k4200_stress` unless exclusively prompted by me. Even then, give me
  the commands to run myself, with the modifications / deletions they make.

## Instruments

- Never send GPIB/VISA commands to the real tools (S200, B1500A, 4200A, ...)
  without my explicit approval for that specific run. Test with the fakes in
  `src/measure/instruments/fakes.py` (`make test`).
- Live runs happen only on the lab PC, natively, because Docker cannot reach
  the GPIB adapter: `make up-live` (the web app with live runs allowed) or
  `make run-cli`, both with `PY` set to the `qcodes` env's python.

## Lab PC (live runs only)

- Git Bash is the shell; write any commands given to me for Git Bash.
- Anaconda isn't on PATH. Call it by full path, quoted because of the space:
  "$HOME/anaconda3/python.exe", "$HOME/anaconda3/Scripts/conda.exe".
- Calling a conda env's python.exe without activating it makes numpy/pandas
  imports die silently (exit 127). Put the env's DLL dirs on PATH first:
  E="$HOME/anaconda3/envs/qcodes"; export PATH="$E:$E/Library/mingw-w64/bin:$E/Library/usr/bin:$E/Library/bin:$E/Scripts:$PATH"
- Envs: `gds` (KLayout, for reading layouts with `layouts/make_*.py`),
  `qcodes` (pymeasure/pyvisa, for measurements, plus fastapi, uvicorn and
  plotly for `make up-live`).

## Reference manuals (read-only, outside this repo)

- S200 / Velox remote commands, one HTML page per command:
  "$HOME/OneDrive - Stanford/Cascade/S200_py_setup/FormFactor S200 Documentation/Manuals/Documentation/VeloxHTMLHelp/Content/VeloxRemoteInterface/COMMAND_NAME.htm"
  (station user guides are in ../Station User Guides/).
- B1500A FLEX programming guide and EasyEXPERT guides:
  "$HOME/OneDrive - Stanford/S200_B1500A_4200A_py_setup/References/b1500A_programming.pdf",
  b1500A_easy_expert_{1,2}.pdf.
- These are OneDrive files; the first read can be slow while they download.

## Git

- A new repository starts with one commit on `main`, made by me: at least a
  README. Branch from it.
- Work on your feature branch. Commit to it often, in small steps, each with a
  clear message. You may push it.
- `main` changes only through a pull request that I review and merge. Never
  commit to, merge into, or push to `main`.
- Merging into any branch other than your feature branch needs my explicit
  approval, every time. Approval for one merge is not approval for the next.
- Never force-push, rewrite published history, or change git configuration.

## Before you start

For any task that creates or changes more than one file, reply first with your
plan and the files you will touch, in under 40 lines, followed by your
assumptions and your questions. Wait for approval.

## Invariants (true after every change)

- Results are computed by the backend and delivered through the API. The
  frontend draws and handles interaction; it computes nothing a scientist
  would call a result (a fit, a unit conversion, a filter, a statistic).
- Every run, from the CLI or the web, goes through `measure.run_map.run`.
- Everything the app needs is installed when the image is built, never while
  it runs.
- The app is published on `127.0.0.1` only.

## Running anything

Through the Makefile, and nothing else. `make help` lists the targets;
`make verify` is the gate.

## Quality

Follow current best practice for code quality, structure and security, and say
in the plan where you depart from it and why. Code a stranger can read, with
type hints and docstrings on anything public. Every input validated at the API
boundary, errors handled and never swallowed. No secrets in code, logs or
error messages, no `eval`, no shell built from user input. Add a dependency
only when it earns its place, and say why.

## Done means

- `make verify` is green from a fresh clone.
- Each pipeline has a test that fails when the result is wrong; show it
  failing once before you make it pass.
- The README is enough for a stranger, and ARCHITECTURE.md still describes the
  project.
- Say what you did not verify.
