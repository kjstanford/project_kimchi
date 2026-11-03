"""The measurement package's own dry-run suite (src/measure/tests/test_dryrun.py), run by pytest, in its order."""

import contextlib
import io
import shutil

import pytest

from measure.tests import test_dryrun as suite


@pytest.fixture(scope="module")
def full_map_cfg():
    with contextlib.redirect_stdout(io.StringIO()):
        cfg = suite.test_full_map()
    yield cfg
    shutil.rmtree(suite.DATA, ignore_errors=True)


@pytest.mark.parametrize("test", suite.TESTS[1:], ids=lambda t: t.__name__)
def test_dryrun(test, full_map_cfg):
    with contextlib.redirect_stdout(io.StringIO()):
        test(full_map_cfg) if test is suite.test_resume else test()
