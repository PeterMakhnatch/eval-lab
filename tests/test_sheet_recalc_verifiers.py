"""Behavioral contract for the sheet-recalc hidden verifiers, without Docker.

Both task packages ship the same hidden verifier (``tests/test_sheet.py``) and
read the submission path from ``SHEET_RECALC_SUBMITTED``. This module runs that
verifier in a child pytest process, never by import, so the verifier's
module-level formula load and its scratch-package runs stay out of the root
suite. The committed solution must pass; the engine shipped in the task image
must fail and name the failing tests (HAR-406).
"""

from __future__ import annotations

import re

import pytest
from sheet_recalc_host import PACKAGES, package_path, run_verifier, transcript

# The shipped engine grades slowly: the full verifier file took 194 s here and
# 17 of 19 tests fail. These two tests fail within two seconds each on the
# shipped engine and still separate it from the solution, which keeps this file
# inside its 120 s bound.
FAILING_SUBSET = (
    "test_replaced_formula_drops_old_dependencies"
    " or test_range_tracks_cells_that_are_still_empty"
)
SOLUTION_TIMEOUT_SECONDS = 100.0
FAILING_TIMEOUT_SECONDS = 30.0

_FAILED_TEST = re.compile(r"^FAILED .*test_sheet\.py::test_\S+", re.MULTILINE)


@pytest.mark.parametrize("package", PACKAGES)
def test_committed_solution_passes(package: str) -> None:
    completed = run_verifier(
        package,
        package_path(package, "solution/engine.py"),
        selector=None,
        timeout=SOLUTION_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0, transcript(completed)


@pytest.mark.parametrize("package", PACKAGES)
def test_shipped_engine_fails(package: str) -> None:
    completed = run_verifier(
        package,
        package_path(package, "environment/sheet/engine.py"),
        selector=FAILING_SUBSET,
        timeout=FAILING_TIMEOUT_SECONDS,
    )
    output = transcript(completed)
    assert completed.returncode != 0, output
    assert _FAILED_TEST.search(output), output
