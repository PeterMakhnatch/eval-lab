from __future__ import annotations

import json
from typing import Any

import duckdb
import pytest

from dashboard.queries import (
    ELIGIBILITY_CALIBRATION_ONLY,
    ELIGIBILITY_CAUSAL_ADMISSIBLE,
    EXPERIMENT_OPERATIONS_SQL,
    OUTCOME_HARNESS_FAILURE,
    OUTCOME_PROVIDER_ACCESS,
    OUTCOME_REFUSED,
    OUTCOME_SCORED,
    AttachSource,
    ZoneUnavailableError,
    action_memory_contrast_fidelity,
    classify_evidence_eligibility,
    classify_trial_outcome,
    experiment_operations,
    experiment_operations_summary,
    model_access_vs_capability,
    refusal_reason_for_trial,
)
from evallab.storage.attach import AttachResult, ZoneStatus


class OperationsFixtureSource:
    """Mock query source returning fixture rows for experiment operations."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def query(
        self, statement: str, parameters: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        del parameters
        if statement == EXPERIMENT_OPERATIONS_SQL:
            return self.rows
        raise KeyError(f"Unexpected statement: {statement}")

    def relation_exists(self, name: str) -> bool:
        return name in ("trials", "jobs", "deterministic_trial_facts")


def test_production_operations_sql_is_select_only():
    normalized = " ".join(EXPERIMENT_OPERATIONS_SQL.upper().split())
    assert normalized.startswith("SELECT ")
    assert not any(
        token in f" {normalized} "
        for token in (" INSERT ", " UPDATE ", " DELETE ", " CREATE ", " DROP ", " ALTER ")
    )


def test_classify_trial_outcome_invariants():
    # 1. Scored trial
    assert classify_trial_outcome({"primary_reward": 1.0, "exception_type": None}) == OUTCOME_SCORED
    assert classify_trial_outcome({"primary_reward": 0.0, "exception_type": None}) == OUTCOME_SCORED

    # 2. Provider access failure (transient_harness: 429, 5xx, overloaded)
    assert classify_trial_outcome({"primary_reward": None, "exception_type": "transient_harness"}) == OUTCOME_PROVIDER_ACCESS
    assert classify_trial_outcome({"primary_reward": 0.0, "exception_type": "transient_harness"}) == OUTCOME_PROVIDER_ACCESS

    # 3. Infrastructure / harness failure
    assert classify_trial_outcome({"primary_reward": None, "exception_type": "DockerError"}) == OUTCOME_HARNESS_FAILURE
    assert classify_trial_outcome({"primary_reward": None, "exception_type": "AgentTimeoutError"}) == OUTCOME_HARNESS_FAILURE

    # 4. Refused / unscored (no exception, no reward) - NEVER coerced to zero
    assert classify_trial_outcome({"primary_reward": None, "exception_type": None}) == OUTCOME_REFUSED


def test_refusal_reason_extraction_without_zero_coercion():
    # Scored trials have no refusal reason
    assert refusal_reason_for_trial({"primary_reward": 1.0, "exception_type": None}) is None

    # Provider access gives explicit failure reason
    provider_trial = {
        "primary_reward": None,
        "exception_type": "transient_harness",
        "result_exception_message": "Rate limit exceeded (HTTP 429)",
    }
    assert refusal_reason_for_trial(provider_trial) == "provider access failure: Rate limit exceeded (HTTP 429)"

    # Harness failure gives exception type and message
    harness_trial = {
        "primary_reward": None,
        "exception_type": "DockerTimeout",
        "result_exception_message": "container execution timed out after 300s",
    }
    assert refusal_reason_for_trial(harness_trial) == "harness exception: DockerTimeout (container execution timed out after 300s)"

    # Refused trial with explicit agent refusal rationale
    refused_explicit = {
        "primary_reward": None,
        "exception_type": None,
        "raw_refusal_reason": "task budget infeasible: required cost $25 exceeds limit $10",
    }
    assert refusal_reason_for_trial(refused_explicit) == "task budget infeasible: required cost $25 exceeds limit $10"

    # Refused trial without explicit rationale states unmeasured status rather than 0
    refused_generic = {"primary_reward": None, "exception_type": None}
    assert "unscored: no reward recorded" in refusal_reason_for_trial(refused_generic)


def test_evidence_eligibility_classification():
    # 1. Oracle and nop controls are calibration_only
    assert classify_evidence_eligibility({
        "agent_name": "oracle",
        "primary_reward": 1.0,
        "exception_type": None,
        "host_platform": "Linux-5.15",
        "isolation_enforced": True,
        "has_credential_proxy": True,
    }) == ELIGIBILITY_CALIBRATION_ONLY

    # 2. Calibration purpose or policy rule is calibration_only
    assert classify_evidence_eligibility({
        "agent_name": "codex",
        "purpose": "calibration",
        "primary_reward": 1.0,
        "exception_type": None,
        "host_platform": "Linux-5.15",
        "isolation_enforced": True,
        "has_credential_proxy": True,
    }) == ELIGIBILITY_CALIBRATION_ONLY

    # 3. Darwin hosts are calibration_only
    assert classify_evidence_eligibility({
        "agent_name": "codex",
        "primary_reward": 1.0,
        "exception_type": None,
        "host_platform": "Darwin-25.5.0-arm64",
        "isolation_enforced": True,
        "has_credential_proxy": True,
    }) == ELIGIBILITY_CALIBRATION_ONLY

    # 4. Public network mode is calibration_only
    assert classify_evidence_eligibility({
        "agent_name": "codex",
        "primary_reward": 1.0,
        "exception_type": None,
        "host_platform": "Linux-5.15",
        "effective_network": "public",
        "isolation_enforced": True,
        "has_credential_proxy": True,
    }) == ELIGIBILITY_CALIBRATION_ONLY

    # 5. Missing isolation enforcement is calibration_only
    assert classify_evidence_eligibility({
        "agent_name": "codex",
        "primary_reward": 1.0,
        "exception_type": None,
        "host_platform": "Linux-5.15",
        "isolation_enforced": False,
        "has_credential_proxy": True,
    }) == ELIGIBILITY_CALIBRATION_ONLY

    # 6. Missing credential proxy is calibration_only
    assert classify_evidence_eligibility({
        "agent_name": "codex",
        "primary_reward": 1.0,
        "exception_type": None,
        "host_platform": "Linux-5.15",
        "isolation_enforced": True,
        "has_credential_proxy": False,
    }) == ELIGIBILITY_CALIBRATION_ONLY

    # 7. Errored trials cannot be causal_admissible
    assert classify_evidence_eligibility({
        "agent_name": "codex",
        "primary_reward": None,
        "exception_type": "DockerError",
        "host_platform": "Linux-5.15",
        "isolation_enforced": True,
        "has_credential_proxy": True,
    }) == ELIGIBILITY_CALIBRATION_ONLY

    # 8. Linux + enforced isolation + credential proxy + scored -> causal_admissible
    assert classify_evidence_eligibility({
        "agent_name": "codex",
        "primary_reward": 1.0,
        "exception_type": None,
        "host_platform": "Linux-5.15",
        "isolation_enforced": "true",
        "has_credential_proxy": True,
    }) == ELIGIBILITY_CAUSAL_ADMISSIBLE


def test_experiment_operations_query_and_summary_flow():
    fixture_data = [
        # Trial 1: Scored pass on Linux with proxy -> causal_admissible
        {
            "cohort": "campaign-1",
            "job_id": "j1",
            "job_name": "job-alpha",
            "trial_id": "t1",
            "trial_name": "trial-1",
            "task_name": "lab/action-memory-task",
            "agent_name": "codex",
            "model_name": "gpt-5.6-terra",
            "primary_reward": 1.0,
            "exception_type": None,
            "policy_rule": "human-approval",
            "purpose": "baseline",
            "isolation_enforced": "true",
            "effective_network": "no-network",
            "host_platform": "Linux-5.15",
            "has_credential_proxy": True,
            "raw_refusal_reason": None,
            "result_exception_message": None,
            "task_family": "action-memory-v1",
            "task_block_id": "block-101",
            "arm_id": "dl-clean-4096-s42",
            "generator_seed_json": json.dumps({"seed": 42}),
        },
        # Trial 2: Scored failure (reward 0.0) -> scored
        {
            "cohort": "campaign-1",
            "job_id": "j1",
            "job_name": "job-alpha",
            "trial_id": "t2",
            "trial_name": "trial-2",
            "task_name": "lab/action-memory-task",
            "agent_name": "codex",
            "model_name": "gpt-5.6-terra",
            "primary_reward": 0.0,
            "exception_type": None,
            "policy_rule": "human-approval",
            "purpose": "baseline",
            "isolation_enforced": "true",
            "effective_network": "no-network",
            "host_platform": "Linux-5.15",
            "has_credential_proxy": True,
            "raw_refusal_reason": None,
            "result_exception_message": None,
            "task_family": "action-memory-v1",
            "task_block_id": "block-101",
            "arm_id": "dl-neutral-padding-4096-s42",
            "generator_seed_json": json.dumps({"seed": 42}),
        },
        # Trial 3: Provider access failure (HTTP 429) -> provider_access (excluded from capability denominator)
        {
            "cohort": "campaign-1",
            "job_id": "j1",
            "job_name": "job-alpha",
            "trial_id": "t3",
            "trial_name": "trial-3",
            "task_name": "lab/action-memory-task",
            "agent_name": "codex",
            "model_name": "gpt-5.6-terra",
            "primary_reward": None,
            "exception_type": "transient_harness",
            "policy_rule": "human-approval",
            "purpose": "baseline",
            "isolation_enforced": "true",
            "effective_network": "no-network",
            "host_platform": "Linux-5.15",
            "has_credential_proxy": True,
            "raw_refusal_reason": None,
            "result_exception_message": "Rate limit exceeded (HTTP 429)",
            "task_family": "action-memory-v1",
            "task_block_id": "block-102",
            "arm_id": "dl-clean-16384-s42",
            "generator_seed_json": json.dumps({"seed": 42}),
        },
        # Trial 4: Harness failure -> harness_failure
        {
            "cohort": "campaign-1",
            "job_id": "j1",
            "job_name": "job-alpha",
            "trial_id": "t4",
            "trial_name": "trial-4",
            "task_name": "lab/action-memory-task",
            "agent_name": "codex",
            "model_name": "gpt-5.6-terra",
            "primary_reward": None,
            "exception_type": "DockerError",
            "policy_rule": "human-approval",
            "purpose": "baseline",
            "isolation_enforced": "true",
            "effective_network": "no-network",
            "host_platform": "Linux-5.15",
            "has_credential_proxy": True,
            "raw_refusal_reason": None,
            "result_exception_message": "Docker container crashed",
            "task_family": "action-memory-v1",
            "task_block_id": "block-102",
            "arm_id": "dl-neutral-padding-16384-s42",
            "generator_seed_json": json.dumps({"seed": 42}),
        },
        # Trial 5: Refused trial with structured reason -> refused
        {
            "cohort": "campaign-1",
            "job_id": "j1",
            "job_name": "job-alpha",
            "trial_id": "t5",
            "trial_name": "trial-5",
            "task_name": "lab/action-memory-task",
            "agent_name": "codex",
            "model_name": "gpt-5.6-terra",
            "primary_reward": None,
            "exception_type": None,
            "policy_rule": "human-approval",
            "purpose": "baseline",
            "isolation_enforced": "true",
            "effective_network": "no-network",
            "host_platform": "Linux-5.15",
            "has_credential_proxy": True,
            "raw_refusal_reason": "refusal: budget limit exceeded",
            "result_exception_message": None,
            "task_family": "action-memory-v1",
            "task_block_id": "block-103",
            "arm_id": "dl-clean-65536-s42",
            "generator_seed_json": json.dumps({"seed": 42}),
        },
    ]

    source = OperationsFixtureSource(fixture_data)
    trials = experiment_operations(source)
    assert len(trials) == 5

    # Check trial classification
    assert trials[0]["outcome_class"] == OUTCOME_SCORED
    assert trials[0]["eligibility"] == ELIGIBILITY_CAUSAL_ADMISSIBLE
    assert trials[0]["refusal_reason"] is None

    assert trials[1]["outcome_class"] == OUTCOME_SCORED
    assert trials[1]["is_passed"] is False

    assert trials[2]["outcome_class"] == OUTCOME_PROVIDER_ACCESS
    assert "provider access failure" in trials[2]["refusal_reason"]

    assert trials[3]["outcome_class"] == OUTCOME_HARNESS_FAILURE
    assert "harness exception: DockerError" in trials[3]["refusal_reason"]

    assert trials[4]["outcome_class"] == OUTCOME_REFUSED
    assert trials[4]["refusal_reason"] == "refusal: budget limit exceeded"

    # Summarize cohort
    summaries = experiment_operations_summary(trials)
    assert len(summaries) == 1
    summary = summaries[0]

    # Invariants: 5 total trials, but ONLY 2 scored trials in capability denominator
    assert summary["n_total"] == 5
    assert summary["n_scored"] == 2
    assert summary["passes"] == 1
    assert summary["pass_rate"] == 0.5  # 1/2, NOT 1/5!
    assert summary["provider_access_failures"] == 1
    assert summary["harness_failures"] == 1
    assert summary["refusals"] == 1
    assert summary["causal_admissible"] == 2
    assert summary["scorable"] is True
    assert summary["ci_95_low"] is not None


def test_model_access_vs_capability_separation():
    trials_data = [
        # Model A: 2 scored attempts, 1 pass (50% capability), 0 access failures -> accessible
        {
            "agent_name": "codex",
            "model_name": "gpt-5.6-terra",
            "outcome_class": OUTCOME_SCORED,
            "is_passed": True,
            "primary_reward": 1.0,
        },
        {
            "agent_name": "codex",
            "model_name": "gpt-5.6-terra",
            "outcome_class": OUTCOME_SCORED,
            "is_passed": False,
            "primary_reward": 0.0,
        },
        # Model B: 3 attempts, ALL provider access failures (e.g. Highspeed) -> access_blocked, capability undefined
        {
            "agent_name": "zai-agent",
            "model_name": "zai-coding-plan/glm-5.3-highspeed",
            "outcome_class": OUTCOME_PROVIDER_ACCESS,
            "is_passed": False,
            "primary_reward": None,
        },
        {
            "agent_name": "zai-agent",
            "model_name": "zai-coding-plan/glm-5.3-highspeed",
            "outcome_class": OUTCOME_PROVIDER_ACCESS,
            "is_passed": False,
            "primary_reward": None,
        },
        {
            "agent_name": "zai-agent",
            "model_name": "zai-coding-plan/glm-5.3-highspeed",
            "outcome_class": OUTCOME_PROVIDER_ACCESS,
            "is_passed": False,
            "primary_reward": None,
        },
        # Model C: 1 scored pass, 1 access failure -> degraded_access, 100% capability over scored
        {
            "agent_name": "zai-agent",
            "model_name": "zai-coding-plan/glm-5.3",
            "outcome_class": OUTCOME_SCORED,
            "is_passed": True,
            "primary_reward": 1.0,
        },
        {
            "agent_name": "zai-agent",
            "model_name": "zai-coding-plan/glm-5.3",
            "outcome_class": OUTCOME_PROVIDER_ACCESS,
            "is_passed": False,
            "primary_reward": None,
        },
    ]

    report = model_access_vs_capability(trials_data)
    by_model = {r["model"]: r for r in report}

    # Model A: accessible, 50% capability
    terra = by_model["gpt-5.6-terra"]
    assert terra["access_status"] == "accessible"
    assert terra["access_failures"] == 0
    assert terra["access_success_rate"] == 1.0
    assert terra["n_scored"] == 2
    assert terra["capability_pass_rate"] == 0.5

    # Model B: access_blocked, 0 scored trials, capability rate is None (NOT 0.0!)
    highspeed = by_model["zai-coding-plan/glm-5.3-highspeed"]
    assert highspeed["access_status"] == "access_blocked"
    assert highspeed["access_failures"] == 3
    assert highspeed["access_success_rate"] == 0.0
    assert highspeed["n_scored"] == 0
    assert highspeed["capability_pass_rate"] is None  # Never coerced to 0 reward!

    # Model C: degraded_access, 100% capability over scored
    glm = by_model["zai-coding-plan/glm-5.3"]
    assert glm["access_status"] == "degraded_access"
    assert glm["access_failures"] == 1
    assert glm["access_success_rate"] == 0.5
    assert glm["n_scored"] == 1
    assert glm["capability_pass_rate"] == 1.0


def test_action_memory_contrast_fidelity_reporting():
    am_trials = [
        # Pair 1: complete matched contrast (clean vs neutral_padding), seed 42, dose 4096
        {
            "task_family": "action-memory-v1",
            "task_name": "action-memory-dose-ladder-4096",
            "task_block_id": "block-1",
            "arm_id": "dl-clean-4096-s42",
            "generator_seed_json": json.dumps({"seed": 42}),
        },
        {
            "task_family": "action-memory-v1",
            "task_name": "action-memory-dose-ladder-4096",
            "task_block_id": "block-1",
            "arm_id": "dl-neutral-padding-4096-s42",
            "generator_seed_json": json.dumps({"seed": 42}),
        },
        # Pair 2: incomplete contrast (only 1 arm observed), seed 42, dose 16384 -> 1 omitted arm
        {
            "task_family": "action-memory-v1",
            "task_name": "action-memory-dose-ladder-16384",
            "task_block_id": "block-2",
            "arm_id": "dl-clean-16384-s42",
            "generator_seed_json": json.dumps({"seed": 42}),
        },
        # Pair 3: duplicate trial for same arm, seed 1337, dose 65536
        {
            "task_family": "action-memory-v1",
            "task_name": "action-memory-dose-ladder-65536",
            "task_block_id": "block-3",
            "arm_id": "dl-clean-65536-s1337",
            "generator_seed_json": json.dumps({"seed": 1337}),
        },
        {
            "task_family": "action-memory-v1",
            "task_name": "action-memory-dose-ladder-65536",
            "task_block_id": "block-3",
            "arm_id": "dl-clean-65536-s1337",  # Duplicate!
            "generator_seed_json": json.dumps({"seed": 1337}),
        },
        # Trial 4: unknown contrast key (missing seed provenance)
        {
            "task_family": "action-memory-v1",
            "task_name": "action-memory-dose-ladder-4096",
            "task_block_id": "block-4",
            "arm_id": "dl-clean-4096-unknown",
            "generator_seed_json": None,
        },
    ]

    fidelity = action_memory_contrast_fidelity(am_trials)

    assert fidelity["total_trials"] == 6
    assert fidelity["total_contrast_groups"] == 3  # block-1, block-2, block-3
    assert fidelity["matched_pairs"] == 1  # block-1 is complete
    assert fidelity["coverage_fidelity"] == pytest.approx(1 / 3)
    assert fidelity["unknown_count"] == 1  # Trial 4 has no seed
    assert fidelity["omitted_count"] == 1  # block-2 is missing its second arm
    assert fidelity["duplicate_count"] == 1  # block-3 has 2 clean trials
    assert fidelity["order_fidelity_rate"] == pytest.approx(1 / 3)  # only block-1 has perfect order distinction


def test_zone_unavailable_on_unattached_z2(tmp_path):
    fake_zones = (
        ZoneStatus("z2", False, reason="Postgres connection refused", detail="localhost:54329/evallab"),
    )
    fake_result = AttachResult(duckdb.connect(":memory:"), fake_zones, "")
    source = AttachSource(fake_result)
    try:
        with pytest.raises(ZoneUnavailableError, match="zone z2 unavailable: Postgres connection refused"):
            experiment_operations(source)
    finally:
        source.close()
