"""The HAR-224 ranking must stay reproducible from tracked receipts.

`rank.py` is the only writer of `unverified-ranking.csv`. These tests pin the
properties a reader relies on: the committed file is what the receipts currently
produce, every built row without a verdict lands in exactly one phase, phases
come out in prior-strength order, and the bulk draw is deterministic and drawn
only from the phase it claims.
"""

from __future__ import annotations

import csv
import io
import runpy
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RANK = REPO_ROOT / "research" / "experiments" / "mimo-clean-v5-coverage" / "rank.py"
CSV_PATH = RANK.parent / "unverified-ranking.csv"
MODULE = runpy.run_path(str(RANK))
build_rows = MODULE["build_rows"]
render = MODULE["render"]
largest_remainder = MODULE["largest_remainder"]
PHASE_ORDER = MODULE["PHASE_ORDER"]
BULK_SAMPLE_SEED = MODULE["BULK_SAMPLE_SEED"]


def _rows() -> list[dict[str, str]]:
    return csv.DictReader(io.StringIO(CSV_PATH.read_text(encoding="utf-8")))


def test_committed_ranking_is_reproducible() -> None:
    # A manifest refresh that changes any phase or rank must regenerate the CSV
    # in the same change; drift is a stale artifact, not a review preference.
    committed = CSV_PATH.read_text(encoding="utf-8")
    assert render(build_rows()) == committed


def test_every_built_unverdicted_row_gets_exactly_one_phase() -> None:
    rows = list(_rows())
    manifest = MODULE["read_manifest"]()
    pending = [
        row for row in manifest if row["status"] == "built" and not MODULE["has_verdict"](row)
    ]
    assert len(rows) == len(pending)
    ids = [row["task_id"] for row in rows]
    assert len(ids) == len(set(ids)), "task ids must be unique"
    assert {row["phase"] for row in rows} <= {phase.key for phase in PHASE_ORDER}


def test_phases_are_ordered_by_prior_strength() -> None:
    rows = list(_rows())
    order = [MODULE["RANK_OF"][row["phase"]] for row in rows]
    assert order == sorted(order), "rank order must follow the phase order"
    assert [int(row["rank"]) for row in rows] == list(range(1, len(rows) + 1))


def test_sample_draw_is_deterministic_and_inside_the_reference_phase() -> None:
    drawn = [row for row in _rows() if row["sample_draw"] == "yes"]
    assert drawn, "the plan's pre-drawn sample must exist"
    assert {row["phase"] for row in drawn} == {"P3-reference-bearing"}
    again = [row for row in build_rows() if row["sample_draw"] == "yes"]
    assert [row["task_id"] for row in drawn] == [row["task_id"] for row in again]
    assert len({row["task_id"] for row in drawn}) == len(drawn)
    assert BULK_SAMPLE_SEED == 20261029


def test_largest_remainder_honours_the_floor_and_the_total() -> None:
    # Two tiny strata are taken whole; the rest splits exactly.
    allocation = largest_remainder({"a": 3, "b": 4, "c": 900, "d": 93}, 100)
    assert allocation["a"] == 3 and allocation["b"] == 4
    assert sum(allocation.values()) == 100
    assert allocation["c"] + allocation["d"] == 93


def test_a_sample_larger_than_the_population_is_refused() -> None:
    try:
        largest_remainder({"a": 1, "b": 2}, 10)
    except ValueError:
        return
    raise AssertionError("a sample larger than its population must not allocate")
