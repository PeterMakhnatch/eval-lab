from __future__ import annotations

from pathlib import Path

from evallab.interpretation.trajectory_semantic_producers import (
    project_agentabstain,
    project_recovery,
)


def test_agentabstain_real_verdict_artifacts_preserve_pair_lineage(tmp_path: Path) -> None:
    verdict = tmp_path / "verdict.json"
    verdict.write_text('{"verdict":"pass","reason":"primary verifier"}\n', encoding="utf-8")
    projection = project_agentabstain(
        [
            {
                "pair_id": "missing_critical_parameter/preview_001",
                "task_id": "preview_001",
                "variant": "act",
                "trigger": "missing_critical_parameter",
                "trial_id": "missing_critical_parameter/preview_001__act",
                "verdict_artifact": verdict,
            },
            {
                "pair_id": "missing_critical_parameter/preview_001",
                "task_id": "preview_001",
                "variant": "abstain",
                "trigger": "missing_critical_parameter",
                "trial_id": "missing_critical_parameter/preview_001__abstain",
                "verdict_artifact": {"verdict": "pass"},
            },
        ]
    )
    assert {row.pair_id for row in projection.paired_condition_facts} == {
        "missing_critical_parameter/preview_001"
    }
    assert [row.primary_verdict for row in projection.paired_condition_facts] == [
        "satisfied",
        "satisfied",
    ]
    assert all(
        row.trial_id.endswith(("__act", "__abstain")) for row in projection.paired_condition_facts
    )
    assert all(row.eligible is True for row in projection.capability_opportunities)
    assert all(row.analysis_ready is True for row in projection.evidence_coverage)


def test_agentabstain_deleted_verdict_is_unknown_and_missing(tmp_path: Path) -> None:
    deleted = tmp_path / "deleted-verdict.json"
    projection = project_agentabstain(
        [
            {
                "pair_id": "ambiguous_action_specification/preview_001",
                "task_id": "preview_001",
                "variant": "abstain",
                "trigger": "ambiguous_action_specification",
                "verdict_artifact": deleted,
                "reward": 1.0,
                "trajectory_text": "ABSTAIN: PRECONDITION_UNSATISFIED",
            }
        ]
    )
    fact = projection.paired_condition_facts[0]
    assert fact.primary_verdict == "unknown"
    assert projection.capability_opportunities[0].eligible is None
    assert "verdict_artifact" in projection.capability_opportunities[0].missing_evidence
    assert (
        "reason:missing_verdict_artifact" in projection.capability_opportunities[0].missing_evidence
    )
    assert "verdict_artifact" in projection.evidence_coverage[0].missing_evidence
    assert "reason:missing_verdict_artifact" in projection.evidence_coverage[0].missing_evidence
    assert projection.evidence_coverage[0].analysis_ready is None


def test_agent_decision_is_not_treated_as_verifier_verdict() -> None:
    projection = project_agentabstain(
        [
            {
                "pair_id": "missing_critical_parameter/preview_002",
                "task_id": "preview_002",
                "variant": "act",
                "verdict_artifact": {"verdict": "abstain"},
            }
        ]
    )
    fact = projection.paired_condition_facts[0]
    assert fact.primary_verdict == "unknown"
    assert "reason:verdict_value_unknown" in projection.evidence_coverage[0].missing_evidence


def test_recovery_distinguishes_autonomous_and_assisted_actions() -> None:
    projection = project_recovery(
        [
            {
                "recovery_fact": {
                    "recovery_trial_id": "recovery-001",
                    "task_id": "constructed/task",
                    "recovery_success": True,
                },
                "certificate": {"overall_status": "PASS"},
                "fault_exposed": True,
                "semantic_actions": [
                    {
                        "action_id": "a1",
                        "intervention_provenance": "autonomous",
                        "outcome": "error",
                    },
                    {
                        "action_id": "a2",
                        "intervention_provenance": "user_assisted",
                        "outcome": "success",
                    },
                ],
            }
        ]
    )
    by_id = {row.opportunity_id: row for row in projection.capability_opportunities}
    assert by_id["recovery:recovery-001:autonomous"].eligible is True
    assert by_id["recovery:recovery-001:user_system_assisted"].eligible is True
    coverage = projection.evidence_coverage[0]
    assert coverage.exposed is True
    assert "autonomous_action" in coverage.observed_evidence
    assert "user_system_assisted_action" in coverage.observed_evidence


def test_recovery_without_fault_exposure_is_unexposed_not_failure() -> None:
    projection = project_recovery(
        [
            {
                "recovery_fact": {
                    "recovery_trial_id": "recovery-control",
                    "recovery_success": False,
                    "final_recovery_reward": 0.0,
                },
                "certificate": {"overall_status": "FAIL"},
                "semantic_actions": [
                    {
                        "action_id": "a1",
                        "intervention_provenance": "autonomous",
                        "outcome": "success",
                    }
                ],
            }
        ]
    )
    coverage = projection.evidence_coverage[0]
    assert coverage.exposed is False
    assert coverage.eligible is None
    assert coverage.analysis_ready is None
    assert all(row.eligible is None for row in projection.capability_opportunities)


def test_expected_negative_action_does_not_imply_fault_exposure() -> None:
    projection = project_recovery(
        [
            {
                "recovery_fact": {
                    "recovery_trial_id": "recovery-negative-control",
                    "recovery_success": False,
                },
                "certificate": {"overall_status": "FAIL"},
                "semantic_actions": [
                    {
                        "action_id": "a1",
                        "intervention_provenance": "autonomous",
                        "outcome": "expected_negative",
                    }
                ],
            }
        ]
    )
    assert projection.evidence_coverage[0].exposed is False
    assert projection.evidence_coverage[0].analysis_ready is None
