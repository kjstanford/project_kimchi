"""One run at a time, in a background thread, with its events (log lines, plan, rows, state) kept for the browser.

The run itself is measure.run_map.run, unchanged: the web gives it a log sink, a confirmation callable, a stop
event and row/plan callbacks. One bench owns the GPIB bus, so a second run is refused while one is active.

Live runs:
- refused unless the manager was created with allow_live=True (python -m app --allow-live, make up-live, on the lab PC);
- interactive=True always, so run_map asks both gates (the live-run gate before connecting and the alignment
  check before the first contact); here each gate waits for the browser. The live-run gate accepts only the
  origin device typed back, the alignment gate only 'yes'. Stop or decline answers 'no'.
"""

import threading
import time
from collections.abc import Callable
from typing import Any

from app.models import clean
from measure import device_map as dm
from measure import run_map

ACTIVE = ("starting", "running", "awaiting_confirm", "stopping")
DRYRUN_DATA_DIR = ".ignore/web_dryrun"


class RunError(Exception):
    """A run request that cannot be honoured in the current state (busy, nothing to confirm, live disabled)."""


def site_id(c: dict, key: str, multi: bool) -> str:
    """The id a device has on the map: its key, or '<die> Lc<n>|<key>' in a multi-segment run."""
    return f"{run_map.segment_label(c)}|{key}" if multi else key


class RunManager:
    """The one active run (one bench, one GPIB bus) and the events of the latest run."""

    def __init__(
        self,
        allow_live: bool = False,
        dryrun_data_dir: str = DRYRUN_DATA_DIR,
        runner: Callable[..., Any] | None = None,
        max_events: int = 50000,
    ) -> None:
        self.allow_live = bool(allow_live)
        self.dryrun_data_dir = dryrun_data_dir
        self._runner = runner or run_map.run
        self._max_events = max_events
        self._cond = threading.Condition()
        self._events = []
        self._seq = 0
        self._count = 0
        self.state = "idle"
        self.run_id = None
        self.info = {}
        self._stop = threading.Event()
        self._answered = threading.Event()
        self._answer = False
        self._expect = None
        self._thread = None

    # --- events ---------------------------------------------------------------------------------------
    def _emit(self, kind, **data):
        with self._cond:
            self._seq += 1
            self._events.append({"seq": self._seq, "run": self.run_id, "type": kind, **clean(data)})
            if len(self._events) > self._max_events:
                del self._events[: len(self._events) - self._max_events]
            self._cond.notify_all()

    def _set_state(self, state, **extra):
        with self._cond:
            self.state = state
            self.info.update(extra)
        self._emit("state", state=state, **extra)

    def events_since(self, seq: int = 0, timeout: float = 0.0) -> list[dict]:
        """Events with seq > seq; waits up to timeout s for the first one."""
        with self._cond:
            if timeout and self._seq <= seq:
                self._cond.wait_for(lambda: self._seq > seq, timeout)
            return [e for e in self._events if e["seq"] > seq]

    def status(self) -> dict:
        with self._cond:
            return clean(
                {
                    "state": self.state,
                    "run": self.run_id,
                    "allow_live": self.allow_live,
                    "seq": self._seq,
                    "expect": self._expect if self.state == "awaiting_confirm" else None,
                    **self.info,
                }
            )

    # --- control --------------------------------------------------------------------------------------
    def start(self, cfg: dict, live: bool = False, options: dict | None = None, config_name: str | None = None) -> str:
        """Start a run in a background thread; options are run_map.run keyword arguments. Returns the run id."""
        opts = dict(options or {})
        if live and not self.allow_live:
            raise RunError("live runs are disabled: start the server on the lab PC with --allow-live")
        # figures go to the browser; show_plots would switch matplotlib to Tk inside this worker thread
        cfg = {**cfg, "show_plots": False}
        with self._cond:
            if self.state in ACTIVE:
                raise RunError(f"a run is already {self.state} ({self.run_id})")
            self._count += 1
            self.run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{self._count}"
            self._stop.clear()
            self._answered.clear()
            self._expect = None
            data_dir = cfg["data_dir"] if live else self.dryrun_data_dir
            self.info = {
                "live": bool(live),
                "config": config_name,
                "data_dir": data_dir,
                "options": opts,
                "experiment": cfg.get("experiment", "idvg"),
                "n_planned": None,
                "n_rows": 0,
                "error": None,
            }
            self.state = "starting"
        self._emit("state", state="starting", **self.info)
        origin_key = dm.parse_keys(opts.get("origin") or cfg["origin_device"])[0]
        self._thread = threading.Thread(
            target=self._work,
            args=(cfg, bool(live), data_dir, opts, origin_key),
            name=f"run-{self.run_id}",
            daemon=True,
        )
        self._thread.start()
        return self.run_id

    def stop(self) -> None:
        with self._cond:
            if self.state not in ACTIVE:
                raise RunError("no run in progress")
            self._stop.set()
            self._answer = False
        self._answered.set()
        self._set_state("stopping")

    def confirm(self, text: str) -> None:
        """Answer the pending gate; the text must match what the gate expects."""
        with self._cond:
            if self.state != "awaiting_confirm":
                raise RunError("nothing to confirm")
            if str(text).strip() != self._expect:
                raise RunError(f"type {self._expect!r} to confirm")
            self._answer = True
        self._answered.set()

    def decline(self) -> None:
        """Answer 'no' to the pending gate."""
        with self._cond:
            if self.state != "awaiting_confirm":
                raise RunError("nothing to decline")
            self._answer = False
        self._answered.set()

    def wait(self, timeout: float | None = None) -> str:
        """Block until the run thread ends (or timeout); returns the state."""
        if self._thread is not None:
            self._thread.join(timeout)
        return self.state

    # --- worker ---------------------------------------------------------------------------------------
    def _gate(self, origin_key):
        def confirm_fn(prompt):
            expect = origin_key if prompt.startswith("LIVE RUN") else "yes"
            with self._cond:
                if self._stop.is_set():
                    return False
                self._expect = expect
                self._answer = False
                self._answered.clear()
            self._set_state("awaiting_confirm", prompt=prompt)
            self._emit("confirm", prompt=prompt, expect=expect)
            self._answered.wait()
            with self._cond:
                ok = self._answer and not self._stop.is_set()
                self._expect = None
            self._emit("log", line=f"[web] gate {'confirmed' if ok else 'declined'}: {prompt.splitlines()[0][:80]}")
            if not self._stop.is_set():
                self._set_state("running", prompt=None)
            return ok

        return confirm_fn

    def _work(self, cfg, live, data_dir, opts, origin_key):
        multi = bool(cfg.get("segments"))

        def on_plan(plan):
            self._set_state("running", n_planned=len(plan))
            self._emit("plan", sites=[site_id(p.get("cfg", cfg), p["key"], multi) for p in plan])

        def on_row(row):
            with self._cond:
                self.info["n_rows"] += 1
            c = {"sample": {"die_idx": row.get("die_idx"), "Lc_nm": row.get("Lc_nm")}}
            self._emit("row", row=row, site=site_id(c, row.get("key"), multi))

        try:
            rows, _ = self._runner(
                cfg,
                live=live,
                data_dir=data_dir,
                interactive=True,
                log_sink=lambda line: self._emit("log", line=line),
                confirm_fn=self._gate(origin_key),
                stop_event=self._stop,
                on_plan=on_plan,
                on_row=on_row,
                **opts,
            )
            self._set_state("stopped" if self._stop.is_set() else "done", n_rows=len(rows))
        except Exception as e:
            self._emit("log", line=f"[web] run failed: {type(e).__name__}: {e}")
            self._set_state("failed", error=f"{type(e).__name__}: {e}")
