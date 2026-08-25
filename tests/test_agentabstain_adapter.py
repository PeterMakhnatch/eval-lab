from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "library/adapters"))

from agentabstain import (
    atif_document,
    load_variants,
    pair_report,
    primary_verdict,
    secondary_judgment,
    validate_corpus,
)

CORPUS = Path(__file__).parents[1] / "library/adapters/agentabstain/data/tasks.jsonl"


def test_official_slice_has_two_pairs_per_trigger_class() -> None:
    variants = load_variants(CORPUS)
    validate_corpus(variants)
    assert len(variants) == 32
    assert len({variant.pair_id for variant in variants}) == 16


def test_primary_withheld_and_committed_boundaries() -> None:
    variant = next(v for v in load_variants(CORPUS) if v.pair_id.startswith("ambiguous_action_specification/preview_002") and v.task_type == "abstain")
    critical = variant.critical_actions[0]
    withheld = primary_verdict(variant, [], initial_state={"x": 1}, final_state={"x": 1})
    committed = primary_verdict(variant, [{"tool": critical, "success": True}], initial_state={"x": 1}, final_state={"x": 2})
    assert withheld.verdict == "pass"
    assert committed.verdict == "fail"
    assert committed.termination_reason == "critical_action_committed"


def test_informational_state_mutation_fails() -> None:
    variant = next(v for v in load_variants(CORPUS) if v.pair_id.startswith("conflicting_constraints/preview_001") and v.task_type == "act")
    result = primary_verdict(variant, [], initial_state={"x": 1}, final_state={"x": 2})
    assert result.verdict == "fail"
    assert result.termination_reason == "unexpected_state_diff"


def test_secondary_judgment_does_not_change_primary() -> None:
    variant = next(v for v in load_variants(CORPUS) if v.task_type == "abstain")
    primary = primary_verdict(variant, [], initial_state={}, final_state={})
    secondary = secondary_judgment(variant, verdict="does_not_abstain", source="judge-run", judge_id="judge/v1", response="I cannot proceed")
    assert primary.verdict == "pass"
    assert secondary.verdict == "does_not_abstain"


def test_atif_retains_call_and_result_identity() -> None:
    variant = next(v for v in load_variants(CORPUS) if v.task_type == "act" and v.critical_actions)
    document = atif_document(trial_id="trial-1", variant=variant, agent="oracle", calls=[
        {"call_id": "c-1", "tool": variant.critical_actions[0], "arguments": {"id": "x"}, "result": {"status": "ok"}, "timestamp": "2026-01-01T00:00:00Z"}
    ])
    assert document["schema_version"] == "ATIF-v1.0"
    assert document["steps"][0]["tool_calls"][0]["call_id"] == "c-1"
    assert document["steps"][0]["observation"] == {"status": "ok"}


def test_pair_report_never_aggregates_secondary() -> None:
    variants = load_variants(CORPUS)
    primary = {f"{v.pair_id}:{v.task_type}": primary_verdict(v, [], initial_state={}, final_state={}) for v in variants}
    report = pair_report(variants, primary)
    assert report["pair_count"] == 16
    assert report["secondary_is_separate"] is True
    assert all(row["secondary_verdict"] is None for row in report["paired_condition_facts"])
