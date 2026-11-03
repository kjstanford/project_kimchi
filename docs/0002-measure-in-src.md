# 0002: The measurement package lives in src/measure

**Decision.** `measure/` moved to `src/measure/` (git mv, history kept), next to `src/app/`.

**Why.** The course layout keeps all code under `src/` with `PYTHONPATH=/work/src`. The app imports `measure`
directly; a second copy would drift.

**Cost.** The CLI is now `make run-cli ARGS="--config ..."`, or `PYTHONPATH=src python -m measure.run_map ...`.
Configs keep their paths (`layouts/...`, `data`), because every command runs from the repository root.
