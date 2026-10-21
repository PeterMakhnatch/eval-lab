"""Behavioral tests for the tripwires v4 confirmation-gate runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
RUN = REPO / "research/experiments/tripwires-v4-precision-20261021/run.py"
_spec = importlib.util.spec_from_file_location("tripwires_v4_precision_run", RUN)
assert _spec and _spec.loader
run = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run
_spec.loader.exec_module(run)


def _record(role: str, rules: list[str]) -> dict[str, Any]:
    return {
        "status": "measured",
        "role": role,
        "hits": [{"rule": r} for r in rules],
    }


def test_attempt_rules_alone_fire_v3_but_not_drop_gates() -> None:
    upstream_only = frozenset({"upstream_attempts"})
    harness_only = frozenset({"harness_log_reads"})
    assert run.gate_fired(upstream_only, "v3_any")
    assert not run.gate_fired(upstream_only, "drop_attempts")
    assert not run.gate_fired(upstream_only, "drop_upstream")
    assert run.gate_fired(upstream_only, "drop_harness")
    assert not run.gate_fired(harness_only, "drop_attempts")
    assert run.gate_fired(harness_only, "drop_upstream")
    assert not run.gate_fired(harness_only, "drop_harness")


def test_non_attempt_verdict_rules_fire_every_gate() -> None:
    fired = frozenset({"grader_tamper", "upstream_attempts"})
    for gate in ("v3_any", *run.GATE_ORDER):
        assert run.gate_fired(fired, gate)
    assert not run.gate_fired(frozenset(), "drop_attempts")
    with pytest.raises(ValueError, match="unknown gate"):
        run.gate_fired(fired, "nope")


def test_split_is_deterministic_disjoint_and_complete() -> None:
    tasks = [f"task-{i:02d}" for i in range(22)]
    first = run.split_tasks(tasks)
    assert first == run.split_tasks(tasks)
    assert sorted(first) == tasks
    tune = {t for t, side in first.items() if side == "tune"}
    holdout = set(tasks) - tune
    assert len(tune) == 11 and len(holdout) == 11
    assert run.split_tasks(tasks, seed="other-seed") != first


def test_selection_requires_recall_within_one_and_better_precision() -> None:
    records = [
        _record("positive", ["grader_tamper"]),
        _record("positive", ["upstream_attempts"]),
        _record("positive", ["upstream_attempts"]),
        _record("negative", ["upstream_attempts"]),
        _record("negative", ["harness_log_reads"]),
        _record("negative", ["grader_tamper"]),
    ]
    # v3: TP 3/3, FA 3/3 (precision 1/2). drop_attempts: TP 1/3 (loss 2) is
    # inadmissible; drop_harness loses 0 TP and one FA, so it must win.
    selection = run.select_gate(records)
    assert selection["table"]["v3_any"]["true_positives"] == 3
    assert selection["chosen"] == "drop_harness"


def test_selection_keeps_v3_when_no_gate_improves_precision() -> None:
    records = [
        _record("positive", ["grader_tamper"]),
        _record("negative", ["grader_tamper"]),
    ]
    assert run.select_gate(records)["chosen"] is None


def test_counts_ignore_unknown_and_other_roles() -> None:
    records = [
        _record("positive", ["grader_tamper"]),
        {**_record("other", ["grader_tamper"])},
        {**_record("positive", ["grader_tamper"]), "status": "unknown"},
    ]
    result = run.counts(records, "v3_any")
    assert result["true_positives"] == 1 and result["positives"] == 1
    assert result["recall"] == 1.0 and result["precision"] == 1.0
