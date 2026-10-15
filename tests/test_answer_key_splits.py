"""Split guard: committed keys keep their split rules, and tuned items cannot be
reported on a held-out split without being excluded or acknowledged."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "split_guard", REPO / "research/answer-keys/splits/guard.py"
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def test_committed_keys_pass_the_guard() -> None:
    """No unit straddles splits, exposures resolve, published assignments are fresh,
    and no scoreboard row reports tuned items on a held-out split."""
    keys = guard.load_keys()
    exposures = guard._jsonl(guard.EXPOSURE)
    errors = guard.validate_exposures(exposures, keys)
    assignments, report, straddles = guard.build(keys, exposures)
    violations, _ = guard.audit_scoreboard(keys, exposures, guard.scoreboard_rows())
    assert errors + straddles + violations == []
    assert guard.ASSIGNMENTS.read_text(encoding="utf-8") == assignments, (
        "run research/answer-keys/splits/guard.py"
    )
    assert guard.REPORT.read_text(encoding="utf-8") == report, (
        "run research/answer-keys/splits/guard.py"
    )


def _key(split_by_task: dict[str, str], pairs: list[dict] | None = None) -> dict:
    items = [
        {
            "item_id": f"k:{task}",
            "subject": {"kind": "task", "task_id": f"bench/{task}"},
            "split": split,
            "expected_verdict": "fail",
        }
        for task, split in split_by_task.items()
    ]
    return {"items": items, "pairs": pairs or []}


def test_pair_spanning_splits_is_a_straddle() -> None:
    key = _key({"a": "dev", "b": "test"}, pairs=[{"parent": "k:a", "child": "k:b"}])
    _, _, straddles = guard.build({"k": key}, [])
    assert len(straddles) == 1 and "straddles" in straddles[0]


def _row(**overrides: str) -> dict:
    row = {
        "key_id": "k",
        "split": "test",
        "agent": "QA Trace",
        "system": "qa-trace/3",
        "date_utc": "2026-10-15T12:00:00Z",
        "n_items": "4",
        "n_measured": "4",
    }
    return row | overrides


TUNED = {
    "exposure_id": "x",
    "kind": "tuned",
    "applies_to": [{"agent": "QA Trace", "system_prefix": "qa-trace/"}],
    "tasks": ["a", "b"],
}


@pytest.mark.parametrize(
    ("record", "row", "violations"),
    [
        pytest.param(TUNED, _row(), 1, id="tuned-items-measured-on-test"),
        pytest.param(TUNED, _row(split="dev"), 0, id="dev-row-is-allowed"),
        pytest.param(TUNED, _row(agent="QA Task"), 0, id="other-agent"),
        pytest.param(
            TUNED | {"applies_to": [{"agent": "QA Trace", "from": "2026-10-16"}]},
            _row(),
            0,
            id="row-before-exposure-applies",
        ),
        pytest.param(
            TUNED | {"acknowledged_rows": [{"key_id": "k", "how": "excluded", "reason": "r"}]},
            _row(n_measured="3"),
            0,
            id="exclusion-feasible",
        ),
        pytest.param(
            TUNED | {"acknowledged_rows": [{"key_id": "k", "how": "excluded", "reason": "r"}]},
            _row(n_measured="4"),
            1,
            id="exclusion-claimed-but-impossible",
        ),
        pytest.param(TUNED | {"kind": "inspected"}, _row(), 0, id="inspected-only-warns"),
    ],
)
def test_scoreboard_rows_on_reported_splits(record: dict, row: dict, violations: int) -> None:
    keys = {"k": _key({"a": "test", "c": "test", "d": "test", "e": "dev"})}
    found, _ = guard.audit_scoreboard(keys, [record], [row])
    assert len(found) == violations
