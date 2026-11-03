# 0001: Live runs run natively on the lab PC

**Decision.** The container serves dry-runs only. Live runs use `make up-live` (or `make run-cli`) on the lab PC,
with the `qcodes` conda env's python, bound to 127.0.0.1.

**Why.** The S200 and the B1500A / 4200A sit on a GPIB-USB adapter driven by NI-VISA on Windows. Docker
Desktop's Linux VM cannot see that adapter, so no container can reach the instruments.

**How it stays safe.** Only `python -m app --allow-live` creates an app that accepts live runs; it refuses any
host but localhost, and compose never passes the flag. Both of run_map's own gates are answered in the browser:
the origin device typed back, then `yes` at the alignment check. Stop or Decline ends the run before contact.

**Cost.** One host install, by the owner, into the qcodes env: fastapi, uvicorn and plotly (for Plotly offline).
