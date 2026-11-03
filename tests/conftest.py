"""Shared fixtures. Every test runs on fake instruments; `no_connections` makes any real connect() fail."""

import contextlib
import shutil
import time
from pathlib import Path

import pytest

from app import pipeline
from measure import run_map

TMP = Path(".ignore/_webapp_test")  # never inside data/ (AGENTS.md)
CFG_NAME = "lc500_idvg.json"


@pytest.fixture
def cfg() -> dict:
    return pipeline.get_config(CFG_NAME)


@pytest.fixture
def tmp_data():
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True)
    yield TMP
    shutil.rmtree(TMP, ignore_errors=True)


@contextlib.contextmanager
def refuse_connections():
    def refuse(*a, **k):
        raise AssertionError("a test tried to connect to a real instrument")

    saved = [(m, m.connect) for m in (run_map.prober_mod, run_map.b1500_mod, run_map.k4200_mod)]
    for m, _ in saved:
        m.connect = refuse
    try:
        yield
    finally:
        for m, f in saved:
            m.connect = f


@pytest.fixture(autouse=True)
def no_connections():
    with refuse_connections():
        yield


def wait_state(mgr, states, timeout=120):
    t0 = time.time()
    while mgr.state not in states:
        assert time.time() - t0 < timeout, f"timeout waiting for {states}, state {mgr.state}"
        time.sleep(0.02)
    return mgr.state


def events(mgr, kind):
    return [e for e in mgr.events_since(0) if e["type"] == kind]
