# 0003: RunManager in its own module

**Decision.** `src/app/runs.py` holds `RunManager`; `pipeline.py` calls it.

**Why.** A run lasts minutes to hours and outlives any request: one thread, one bench, a list of events and two
gates waiting for an answer. That is state, not a pipeline from input to result.

**Rules.** One run at a time (one GPIB bus, one chuck). Events are kept in memory and replayed from any `seq`,
so a reloaded page catches up. Closing the page never stops a run; Stop does.
