"""Incorrect-pass contract of the repaired-data SFT scorer (`analyze.py`).

Regression for the Verifier's 2026-10-16 synthetic case: with no original-grader replays
and 31 unresolved v6 failures, the scorer printed `incorrect_pass_rate: 0.0`, so missing
data looked like a measured zero.
"""

from __future__ import annotations

import runpy
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ANALYZE = runpy.run_path(
    str(REPO_ROOT / "research" / "experiments" / "repaired-data-sft-v1" / "analyze.py")
)
summary = ANALYZE["incorrect_pass_summary"]


def test_unreplayed_failures_are_unmeasured_not_zero() -> None:
    # Verifier's arm B: 2 v6 passes, 31 v6 failures, no replay input.
    out = summary([(True, None)] * 2 + [(False, None)] * 31)
    assert out["incorrect_pass_rate"] is None
    assert out["incorrect_pass_ci95"] is None
    assert out["incorrect_pass_status"] == "unmeasured"
    assert out["incorrect_pass_unresolved_failures"] == 31
    assert out["incorrect_pass_v6_passes"] == 2
    assert out["incorrect_pass_bounds"] == [0.0, round(31 / 33, 4)]


def test_all_v6_passes_are_structurally_zero() -> None:
    # Verifier's arm A: every trial passes v6, so no replay is needed.
    out = summary([(True, None)] * 33)
    assert out["incorrect_pass_status"] == "complete"
    assert out["incorrect_pass_rate"] == 0.0
    assert out["incorrect_pass_unresolved_failures"] == 0
    assert out["incorrect_pass_bounds"] is None


def test_partial_replays_are_bounded_and_complete_replays_are_rated() -> None:
    partial = summary([(True, None)] * 3 + [(False, True), (False, False)] + [(False, None)] * 5)
    assert partial["incorrect_pass_status"] == "partial"
    assert partial["incorrect_pass_rate"] is None
    assert partial["incorrect_passes"] == 1
    assert partial["incorrect_pass_unresolved_failures"] == 5
    assert partial["incorrect_pass_bounds"] == [0.1, 0.6]

    complete = summary([(True, None)] * 3 + [(False, True)] * 2 + [(False, False)] * 5)
    assert complete["incorrect_pass_status"] == "complete"
    assert complete["incorrect_pass_rate"] == 0.2
    assert complete["incorrect_pass_ci95"] is not None
    assert complete["incorrect_pass_bounds"] is None
