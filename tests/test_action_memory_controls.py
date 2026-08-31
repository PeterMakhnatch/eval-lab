"""Focused certification tests for action-memory state-inversion deterministic controls."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from evallab.interpretation.benchmark_events import (
    BenchmarkContractRecord,
    BenchmarkEventRecord,
    FinalStateRecord,
    TrialBundle,
)
from evallab.interpretation.producers.action_memory import extract_action_memory_features
from evallab.semantic_facts import PairedConditionFact

ROOT = Path(__file__).parents[1] / "library" / "benchmarks" / "action-memory-v1"


def _load(name: str, filename: str | None = None):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{filename or name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


canary = _load("control_canary")
CANARY_DOSE_BYTES = canary.CANARY_DOSE_BYTES
CANARY_PAIR_ID = canary.CANARY_PAIR_ID
CANARY_SEED = canary.CANARY_SEED
CanaryPairSpec = canary.CanaryPairSpec
build_canary_pair_spec = canary.build_canary_pair_spec
emit_canary_paired_condition_fact = canary.emit_canary_paired_condition_fact
synthesize_canary_trial_artifacts = canary.synthesize_canary_trial_artifacts


@pytest.fixture
def canary_spec() -> CanaryPairSpec:
    return build_canary_pair_spec(seed=CANARY_SEED, dose_bytes=CANARY_DOSE_BYTES)


def test_canary_pair_spec_freezes_exact_single_contrast(canary_spec: CanaryPairSpec):
    """The canary pair must hold context budget, tool schema, seed, and entity constant while changing only state inversion."""
    assert canary_spec.pair_id == CANARY_PAIR_ID
    assert canary_spec.seed == 42
    assert canary_spec.dose_bytes == 4096
    assert canary_spec.target_entity.startswith("entity_")
    assert canary_spec.target_attribute == "routing_key"
    assert canary_spec.initial_value != canary_spec.inverted_value

    arm0 = canary_spec.non_inverted_scenario
    arm1 = canary_spec.inverted_scenario

    # Exactly matched invariants
    assert arm0["seed"] == arm1["seed"] == 42
    assert arm0["dose_bytes"] == arm1["dose_bytes"] == 4096
    assert arm0["target_entity"] == arm1["target_entity"]
    assert arm0["target_attribute"] == arm1["target_attribute"]
    assert arm0["read_opportunity_count"] == arm1["read_opportunity_count"] == 7
    assert arm0["mutation_opportunity_count"] == arm1["mutation_opportunity_count"] == 1
    assert len(arm0["chunks"]) == len(arm1["chunks"]) == 7

    # Declared single contrast variable: inversion_count and latest_value
    assert arm0["inversion_count"] == 0
    assert arm1["inversion_count"] == 1
    assert arm0["latest_value"] == canary_spec.initial_value
    assert arm1["latest_value"] == canary_spec.inverted_value
    assert arm0["inversion_steps"] == []
    assert arm1["inversion_steps"] == [canary_spec.inverted_value]


def _build_bundle_from_synthesized(synth: dict) -> TrialBundle:
    contract_rec = BenchmarkContractRecord(
        family=synth["contract"]["family"],
        version=synth["contract"]["version"],
        construct=synth["contract"]["construct"],
        seed=synth["contract"]["seed"],
        task_id=synth["contract"]["task_id"],
        cell_factors=synth["contract"]["cell_factors"],
        opportunity_counts=synth["contract"]["opportunity_counts"],
        verifier_truth_digest=synth["contract"]["verifier_truth_digest"],
        artifact_paths=synth["contract"].get("artifact_paths", {}),
    )
    final_rec = FinalStateRecord(
        initial_digest="sha256:init",
        final_digest="sha256:final",
        step_count=synth["final_state"]["step_count"],
        mutations=synth["final_state"]["mutations"],
        invariants_passed=synth["final_state"]["invariants_passed"],
        details=synth["final_state"]["details"],
    )
    event_recs = [
        BenchmarkEventRecord(
            event_index=e["event_index"],
            event_type=e["event_type"],
            payload=e,
        )
        for e in synth["events"]
    ]
    from evallab.interpretation.benchmark_events import correlate_tool_calls

    calls = correlate_tool_calls(event_recs)
    return TrialBundle(
        trial_id=synth["trial_id"],
        contract=contract_rec,
        final_state=final_rec,
        events=event_recs,
        correlated_calls=calls,
    )


def test_canary_oracle_controls_pass_and_extract_correct_features(canary_spec: CanaryPairSpec):
    """Oracle controls on both arms must achieve task success, binding match, and complete handle coverage."""
    # Arm 0 Oracle (Non-Inverted)
    arm0_oracle = synthesize_canary_trial_artifacts(
        canary_spec, arm="non_inverted", control_type="oracle"
    )
    bundle0 = _build_bundle_from_synthesized(arm0_oracle)
    feat0 = extract_action_memory_features(bundle0)

    assert feat0.task_success is True
    assert feat0.binding_matched is True
    assert feat0.stale_value_bound is False
    assert feat0.bound_target_value == canary_spec.initial_value
    assert feat0.raw_binding_opportunities == 1
    assert feat0.raw_conflicting_opportunities == 0
    assert feat0.binding_survival_rate == 1.0
    assert feat0.stale_value_override_rate is None  # denom is 0, strict NULL
    assert feat0.handle_set_match is True
    assert feat0.handle_order_match is True
    assert feat0.handle_coverage_rate == 1.0

    # Arm 1 Oracle (State Inverted)
    arm1_oracle = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="oracle"
    )
    bundle1 = _build_bundle_from_synthesized(arm1_oracle)
    feat1 = extract_action_memory_features(bundle1)

    assert feat1.task_success is True
    assert feat1.binding_matched is True
    assert feat1.stale_value_bound is False
    assert feat1.bound_target_value == canary_spec.inverted_value
    assert feat1.raw_binding_opportunities == 1
    assert feat1.raw_conflicting_opportunities == 1
    assert feat1.binding_survival_rate == 1.0
    assert (
        feat1.stale_value_override_rate == 1.0
    )  # successfully overrode stale value with latest value
    assert feat1.handle_set_match is True
    assert feat1.handle_order_match is True
    assert feat1.handle_coverage_rate == 1.0


def test_canary_nop_and_stale_mutant_controls_fail_closed(canary_spec: CanaryPairSpec):
    """Nop and stale-value mutants must fail closed and report deterministic failure states."""
    # Nop on Arm 1
    arm1_nop = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="nop"
    )
    bundle_nop = _build_bundle_from_synthesized(arm1_nop)
    feat_nop = extract_action_memory_features(bundle_nop)

    assert feat_nop.task_success is False
    assert feat_nop.binding_matched is False
    assert feat_nop.stale_value_bound is False
    assert feat_nop.total_tool_calls == 0
    assert feat_nop.valid_handle_count == 0
    assert feat_nop.handle_set_match is False
    assert feat_nop.handle_order_match is False
    assert feat_nop.binding_survival_rate == 0.0

    # Stale Value Mutant on Arm 1 (binds v1 when v2 was required)
    arm1_stale = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="stale_mutant"
    )
    bundle_stale = _build_bundle_from_synthesized(arm1_stale)
    feat_stale = extract_action_memory_features(bundle_stale)

    assert feat_stale.task_success is False
    assert feat_stale.binding_matched is False
    assert feat_stale.stale_value_bound is True
    assert feat_stale.bound_target_value == canary_spec.initial_value
    assert feat_stale.binding_survival_rate == 0.0
    assert feat_stale.stale_value_override_rate == 0.0  # bound stale value, failing override


def test_state_journal_absence_classified_as_observability_failure(
    canary_spec: CanaryPairSpec, tmp_path: Path
):
    """State journal absence must be classified as observability failure / hold, NOT task failure or zero state change."""
    arm0_oracle = synthesize_canary_trial_artifacts(
        canary_spec, arm="non_inverted", control_type="oracle", include_state_journal=False
    )
    assert arm0_oracle["state_journal"] is None

    # Write out minimal trial directory without state-journal directory
    trial_dir = tmp_path / "trial_no_journal"
    trial_dir.mkdir()
    (trial_dir / "result.json").write_text(
        json.dumps({"task_name": arm0_oracle["task_id"], "reward": 1.0, "status": "executed"}),
        encoding="utf-8",
    )
    (trial_dir / "benchmark-contract.json").write_text(
        json.dumps(arm0_oracle["contract"]), encoding="utf-8"
    )
    (trial_dir / "benchmark-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in arm0_oracle["events"]) + "\n",
        encoding="utf-8",
    )
    (trial_dir / "final-state.json").write_text(
        json.dumps(arm0_oracle["final_state"]), encoding="utf-8"
    )

    from evallab.interpretation.evidence_pack import compute_evidence_coverage_metrics

    coverage = compute_evidence_coverage_metrics(trial_dir=trial_dir)
    assert coverage.has_state_journal is False
    assert coverage.has_result is True


def test_canonical_paired_condition_facts_emission(canary_spec: CanaryPairSpec):
    """Canonical PairedConditionFact rows must capture lineage, condition, and state diff for both arms."""
    arm0_oracle = synthesize_canary_trial_artifacts(
        canary_spec, arm="non_inverted", control_type="oracle"
    )
    fact0 = emit_canary_paired_condition_fact(arm0_oracle)

    assert isinstance(fact0, PairedConditionFact)
    assert fact0.pair_id == CANARY_PAIR_ID
    assert fact0.task_id == canary_spec.non_inverted_task_id
    assert fact0.variant == "non_inverted"
    assert fact0.condition == "baseline_clean"
    assert fact0.trigger == "initial_fact_binding"
    assert fact0.critical_action == "execute_mutation"
    assert fact0.primary_verdict == "satisfied"
    assert (
        fact0.state_diff
        == f"{canary_spec.target_entity}.{canary_spec.target_attribute}={canary_spec.initial_value}"
    )

    arm1_oracle = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="oracle"
    )
    fact1 = emit_canary_paired_condition_fact(arm1_oracle)

    assert isinstance(fact1, PairedConditionFact)
    assert fact1.pair_id == CANARY_PAIR_ID
    assert fact1.task_id == canary_spec.inverted_task_id
    assert fact1.variant == "state_inverted"
    assert fact1.condition == "stale_value_override"
    assert fact1.trigger == "inversion_override_binding"
    assert fact1.critical_action == "execute_mutation"
    assert fact1.primary_verdict == "satisfied"
    assert (
        fact1.state_diff
        == f"{canary_spec.target_entity}.{canary_spec.target_attribute}={canary_spec.inverted_value}"
    )


def test_campaign_spec_validity_and_zero_billable_guard():
    """Future campaign spec must be well-formed, bounded to 1 pair / 1 repeat, and strictly non-billable."""
    spec_path = (
        Path(__file__).parents[1]
        / "research"
        / "roadmap"
        / "specs"
        / "campaign-action-memory-controls-canary.json"
    )
    assert spec_path.is_file(), f"Missing campaign spec: {spec_path}"

    data = json.loads(spec_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == "campaign-definition/v2"
    assert data["campaign_id"] == "campaign-action-memory-controls-canary-v1"
    assert data["status"] == "designed"
    assert data["execution_policy"]["allow_billable"] is False
    assert data["execution_policy"]["max_cost_usd"] == 0.0
    assert data["execution_policy"]["max_wall_clock_seconds"] == 600
    assert data["execution_policy"]["max_input_tokens"] == 100000

    pair = data["pair_definition"]
    assert pair["pair_id"] == CANARY_PAIR_ID
    assert pair["seed"] == 42
    assert pair["dose_bytes"] == 4096
    assert pair["contrast_variable"] == "state_inversion_status"
    assert len(pair["arms"]) == 2
    assert pair["arms"][0]["inversion_count"] == 0
    assert pair["arms"][1]["inversion_count"] == 1
    assert pair["arms"][0]["repeats"] == 1
    assert pair["arms"][1]["repeats"] == 1
