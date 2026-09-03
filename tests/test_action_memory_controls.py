"""Focused certification tests for action-memory state-inversion deterministic controls."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from evallab.interpretation.benchmark_events import (
    parse_benchmark_events,
)
from evallab.interpretation.evidence_pack import compute_evidence_coverage_metrics
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
CANARY_TASK_NON_INVERTED = canary.CANARY_TASK_NON_INVERTED
CANARY_TASK_INVERTED = canary.CANARY_TASK_INVERTED
CANARY_TOOL_SCHEMA_DIGEST = canary.CANARY_TOOL_SCHEMA_DIGEST
CanaryPairSpec = canary.CanaryPairSpec
build_canary_pair_spec = canary.build_canary_pair_spec
compute_token_digest = canary.compute_token_digest
emit_canary_paired_condition_fact = canary.emit_canary_paired_condition_fact
materialize_canary_trial_bundle = canary.materialize_canary_trial_bundle
synthesize_canary_trial_artifacts = canary.synthesize_canary_trial_artifacts
extract_read_to_use_linkage = canary.extract_read_to_use_linkage


@pytest.fixture
def canary_spec() -> CanaryPairSpec:
    return build_canary_pair_spec(seed=CANARY_SEED, dose_bytes=CANARY_DOSE_BYTES)


def test_canary_pair_spec_freezes_exact_single_contrast(canary_spec: CanaryPairSpec):
    """The canary pair must hold context budget, tool schema, seed, and entity constant while changing only state inversion."""
    assert canary_spec.pair_id == CANARY_PAIR_ID
    assert canary_spec.seed == 42
    assert canary_spec.dose_bytes == 4096
    assert canary_spec.total_realized_context_bytes == 4096
    assert canary_spec.target_entity.startswith("entity_")
    assert canary_spec.target_attribute == "routing_key"
    assert canary_spec.initial_value != canary_spec.inverted_value
    assert canary_spec.tool_inventory_digest == CANARY_TOOL_SCHEMA_DIGEST

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

    # Exact realized byte parity
    assert sum(c["byte_count"] for c in arm0["chunks"]) == 4096
    assert sum(c["byte_count"] for c in arm1["chunks"]) == 4096
    for c0, c1 in zip(arm0["chunks"], arm1["chunks"], strict=True):
        assert c0["byte_count"] == c1["byte_count"], (
            "Every chunk position must have matching byte length"
        )

    # First-class bound token identity assertions (no log string parsing required)
    assert arm0["chunks"][0]["bound_token"] == canary_spec.initial_value
    assert arm0["chunks"][0]["token_digest"] == compute_token_digest(canary_spec.initial_value)
    assert arm1["chunks"][0]["bound_token"] == canary_spec.initial_value
    assert arm1["chunks"][1]["bound_token"] == canary_spec.inverted_value
    assert arm1["chunks"][1]["token_digest"] == compute_token_digest(canary_spec.inverted_value)

    # Declared single contrast variable: inversion_count and latest_value
    assert arm0["inversion_count"] == 0
    assert arm1["inversion_count"] == 1
    assert arm0["latest_value"] == canary_spec.initial_value
    assert arm1["latest_value"] == canary_spec.inverted_value
    assert arm0["inversion_steps"] == []
    assert arm1["inversion_steps"] == [canary_spec.inverted_value]

    # Structural diff allowlist: only declared inversion keys may differ
    differing_keys = {k for k in arm0 if arm0[k] != arm1[k]}
    assert differing_keys == {
        "arm",
        "cell_id",
        "inversion_count",
        "inversion_steps",
        "latest_value",
        "update_opportunity_count",
        "expected_mutation_call",
        "chunks",
    }
    # For chunks: chunk 0 and chunks 2-6 must be completely identical
    assert arm0["chunks"][0] == arm1["chunks"][0]
    for i in range(2, 7):
        assert arm0["chunks"][i] == arm1["chunks"][i]
    # Chunk 1 differs only in content, id, type, and bound_token, with matching byte_count
    assert arm0["chunks"][1]["byte_count"] == arm1["chunks"][1]["byte_count"] == 256
    assert arm0["chunks"][1]["content"] != arm1["chunks"][1]["content"]


def test_canary_event_stream_parses_through_canonical_parser(canary_spec: CanaryPairSpec):
    """Synthesized event streams must be strictly 1-based, gap-free, and parse via parse_benchmark_events."""
    arm1_oracle = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="oracle"
    )
    events = arm1_oracle["events"]

    assert len(events) == 16  # 7 reads * 2 (req/res) + 1 mutation * 2 (req/res)
    indices = [e["event_index"] for e in events]
    assert indices == list(range(1, 17)), "Event indices must be strictly 1..16 without gaps"

    # Pass through canonical parser
    parsed = parse_benchmark_events(events)
    assert len(parsed) == 16
    assert all(p.event_index == idx for idx, p in enumerate(parsed, start=1))


def test_canary_oracle_controls_pass_and_extract_correct_features(
    canary_spec: CanaryPairSpec, tmp_path: Path
):
    """Oracle controls on both arms must achieve task success, binding match, and complete handle coverage."""
    # Arm 0 Oracle (Non-Inverted) loaded via canonical parser
    arm0_oracle = synthesize_canary_trial_artifacts(
        canary_spec, arm="non_inverted", control_type="oracle"
    )
    bundle0 = materialize_canary_trial_bundle(arm0_oracle, tmp_path / "bundle0")
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

    # Arm 1 Oracle (State Inverted) loaded via canonical parser
    arm1_oracle = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="oracle"
    )
    bundle1 = materialize_canary_trial_bundle(arm1_oracle, tmp_path / "bundle1")
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


def test_canary_nop_and_stale_mutant_controls_fail_closed(
    canary_spec: CanaryPairSpec, tmp_path: Path
):
    """Nop and stale-value mutants must fail closed and report deterministic failure states."""
    # Nop on Arm 1
    arm1_nop = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="nop"
    )
    bundle_nop = materialize_canary_trial_bundle(arm1_nop, tmp_path / "bundle_nop")
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
    bundle_stale = materialize_canary_trial_bundle(arm1_stale, tmp_path / "bundle_stale")
    feat_stale = extract_action_memory_features(bundle_stale)

    assert feat_stale.task_success is False
    assert feat_stale.binding_matched is False
    assert feat_stale.stale_value_bound is True
    assert feat_stale.bound_target_value == canary_spec.initial_value
    assert feat_stale.binding_survival_rate == 0.0
    assert feat_stale.stale_value_override_rate == 0.0  # bound stale value, failing override


def test_read_to_use_linkage_deterministic_oracle(canary_spec: CanaryPairSpec, tmp_path: Path):
    """Read->use linkage must resolve deterministically via first-class token identity, token digest, and step order."""
    # Arm 0 Oracle: read chunk 0 (initial token) -> mutation with initial token
    arm0_oracle = synthesize_canary_trial_artifacts(
        canary_spec, arm="non_inverted", control_type="oracle"
    )
    bundle0 = materialize_canary_trial_bundle(arm0_oracle, tmp_path / "linkage_bundle0")
    linkage0 = extract_read_to_use_linkage(bundle0)

    assert linkage0["read_to_use_linked"] is True
    assert linkage0["matched_read_chunk_id"] == "ctx_000_init"
    assert linkage0["bound_token"] == canary_spec.initial_value
    assert linkage0["token_digest"] == compute_token_digest(canary_spec.initial_value)
    assert linkage0["write_to_read_opportunities"] == 0
    assert linkage0["write_to_read_rate"] is None
    assert linkage0["write_to_read_to_use_rate"] is None

    # Arm 1 Oracle: read chunk 1 (inverted token) -> mutation with inverted token
    arm1_oracle = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="oracle"
    )
    bundle1 = materialize_canary_trial_bundle(arm1_oracle, tmp_path / "linkage_bundle1")
    linkage1 = extract_read_to_use_linkage(bundle1)

    assert linkage1["read_to_use_linked"] is True
    assert linkage1["matched_read_chunk_id"] == "ctx_001_inv"
    assert linkage1["bound_token"] == canary_spec.inverted_value
    assert linkage1["token_digest"] == compute_token_digest(canary_spec.inverted_value)
    assert linkage1["write_to_read_opportunities"] == 0
    assert linkage1["write_to_read_rate"] is None
    assert linkage1["write_to_read_to_use_rate"] is None


def test_read_to_use_negative_regressions(canary_spec: CanaryPairSpec, tmp_path: Path):
    """Read->use must strictly fail closed on tampered digests, missing identity, step order violation, or contract mismatch."""
    # 1. Missing bound_token field in read payload
    arm1_synth = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="oracle"
    )
    for ev in arm1_synth["events"]:
        if ev["event_type"] == "tool_result" and "result" in ev:
            ev["result"].pop("bound_token", None)  # strip first-class token field
    bundle_missing = materialize_canary_trial_bundle(arm1_synth, tmp_path / "neg_missing")
    assert extract_read_to_use_linkage(bundle_missing)["read_to_use_linked"] is False

    # 2. Tampered read token digest (adversarial probe B2)
    arm1_bad_digest = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="oracle"
    )
    for ev in arm1_bad_digest["events"]:
        if ev["event_type"] == "tool_result" and ev["result"].get("chunk_id") == "ctx_001_inv":
            ev["result"]["token_digest"] = "sha256:" + "0" * 64
    bundle_bad_digest = materialize_canary_trial_bundle(
        arm1_bad_digest, tmp_path / "neg_bad_digest"
    )
    linkage_bad_digest = extract_read_to_use_linkage(bundle_bad_digest)
    assert linkage_bad_digest["read_to_use_linked"] is False
    assert linkage_bad_digest["linkage_status"] == "token_digest_mismatch"

    # 3. Missing/mismatched attribute identity (adversarial probe B3)
    arm1_bad_attr = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="oracle"
    )
    for ev in arm1_bad_attr["events"]:
        if ev["event_type"] == "tool_result" and ev["result"].get("chunk_id") == "ctx_001_inv":
            ev["result"].pop("attribute", None)  # missing attribute
    bundle_bad_attr = materialize_canary_trial_bundle(arm1_bad_attr, tmp_path / "neg_bad_attr")
    assert extract_read_to_use_linkage(bundle_bad_attr)["read_to_use_linked"] is False

    # 4. Result observed after mutation request (adversarial probe B4)
    arm1_late_obs = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="oracle"
    )
    # Move chunk 1 result event to index 16 (after mutation request at 15)
    chunk1_res = next(
        e
        for e in arm1_late_obs["events"]
        if e["event_type"] == "tool_result" and e["result"].get("chunk_id") == "ctx_001_inv"
    )
    mut_req = next(
        e
        for e in arm1_late_obs["events"]
        if e["event_type"] == "mcp_call" and e["tool_name"] == "execute_mutation"
    )
    # Swap their indices
    chunk1_res["event_index"], mut_req["event_index"] = (
        mut_req["event_index"],
        chunk1_res["event_index"],
    )
    # Re-sort to satisfy parse order
    arm1_late_obs["events"].sort(key=lambda x: x["event_index"])
    bundle_late = materialize_canary_trial_bundle(arm1_late_obs, tmp_path / "neg_late")
    linkage_late = extract_read_to_use_linkage(bundle_late)
    assert linkage_late["read_to_use_linked"] is False
    assert linkage_late["linkage_status"] == "read_observed_after_mutation_request"

    # 5. Nonzero declared memory_write_opportunities (adversarial probe B5)
    arm1_nonzero_writes = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="oracle"
    )
    arm1_nonzero_writes["contract"]["opportunity_counts"]["memory_write_opportunities"] = 2
    bundle_nonzero_writes = materialize_canary_trial_bundle(
        arm1_nonzero_writes, tmp_path / "neg_writes"
    )
    linkage_nonzero_writes = extract_read_to_use_linkage(bundle_nonzero_writes)
    assert linkage_nonzero_writes["read_to_use_linked"] is False
    assert (
        linkage_nonzero_writes["linkage_status"]
        == "nonzero_contract_write_opportunities_unsupported"
    )

    # 6. Tampered mutation result token digest (adversarial probe B2)
    arm1_bad_mut_digest = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="oracle"
    )
    for ev in arm1_bad_mut_digest["events"]:
        if ev["event_type"] == "tool_result" and "bound_value" in ev.get("result", {}):
            ev["result"]["token_digest"] = "sha256:" + "0" * 64
    bundle_bad_mut_digest = materialize_canary_trial_bundle(
        arm1_bad_mut_digest, tmp_path / "neg_bad_mut_digest"
    )
    linkage_bad_mut_digest = extract_read_to_use_linkage(bundle_bad_mut_digest)
    assert linkage_bad_mut_digest["read_to_use_linked"] is False
    assert linkage_bad_mut_digest["linkage_status"] == "mutation_token_digest_mismatch"

    # 7. Read without use
    arm1_no_use = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="nop"
    )
    bundle_no_use = materialize_canary_trial_bundle(arm1_no_use, tmp_path / "neg_no_use")
    assert extract_read_to_use_linkage(bundle_no_use)["read_to_use_linked"] is False


def test_state_journal_absence_and_tampering_classified_as_hold(
    canary_spec: CanaryPairSpec, tmp_path: Path
):
    """State journal absence or digest tampering must force paired fact verdict to unknown (HOLD)."""
    # 1. State journal absent
    arm0_no_journal = synthesize_canary_trial_artifacts(
        canary_spec, arm="non_inverted", control_type="oracle", include_state_journal=False
    )
    trial_no_journal_dir = tmp_path / "trial_no_journal"
    _ = materialize_canary_trial_bundle(arm0_no_journal, trial_no_journal_dir)
    (trial_no_journal_dir / "result.json").write_text(
        json.dumps({"task_name": arm0_no_journal["task_id"], "reward": 1.0, "status": "executed"}),
        encoding="utf-8",
    )
    coverage = compute_evidence_coverage_metrics(trial_dir=trial_no_journal_dir)
    assert coverage.has_state_journal is False
    assert coverage.has_result is True

    fact_no_journal = emit_canary_paired_condition_fact(arm0_no_journal)
    assert fact_no_journal.primary_verdict == "unknown", (
        "Absent state journal must force verdict to unknown (HOLD)"
    )
    assert fact_no_journal.secondary_verdict == "unknown"

    # 2. State journal tampered token digest (adversarial probe B6)
    arm0_tampered_journal = copy.deepcopy(
        synthesize_canary_trial_artifacts(
            canary_spec, arm="non_inverted", control_type="oracle", include_state_journal=True
        )
    )
    arm0_tampered_journal["state_journal"]["changes"][0]["token_digest"] = "sha256:" + "0" * 64
    fact_tampered = emit_canary_paired_condition_fact(arm0_tampered_journal)
    assert fact_tampered.primary_verdict == "unknown", (
        "Tampered journal digest must force verdict to unknown (HOLD)"
    )
    assert fact_tampered.secondary_verdict == "unknown"

    # 3. State journal wrong bound_token with matching legacy value (adversarial probe B6)
    arm0_wrong_token_journal = copy.deepcopy(
        synthesize_canary_trial_artifacts(
            canary_spec, arm="non_inverted", control_type="oracle", include_state_journal=True
        )
    )
    arm0_wrong_token_journal["state_journal"]["changes"][0]["bound_token"] = "wrong_token_xyz"
    fact_wrong_token = emit_canary_paired_condition_fact(arm0_wrong_token_journal)
    assert fact_wrong_token.primary_verdict == "unknown", (
        "Wrong journal bound_token must force verdict to unknown (HOLD)"
    )

    # 4. State journal missing bound_token field (adversarial probe B6)
    arm0_missing_token_journal = copy.deepcopy(
        synthesize_canary_trial_artifacts(
            canary_spec, arm="non_inverted", control_type="oracle", include_state_journal=True
        )
    )
    arm0_missing_token_journal["state_journal"]["changes"][0].pop("bound_token", None)
    fact_missing_token = emit_canary_paired_condition_fact(arm0_missing_token_journal)
    assert fact_missing_token.primary_verdict == "unknown", (
        "Missing journal bound_token must force verdict to unknown (HOLD)"
    )

    # 5. State journal ambiguous / duplicate target changes (adversarial probe B6)
    arm0_ambiguous_journal = copy.deepcopy(
        synthesize_canary_trial_artifacts(
            canary_spec, arm="non_inverted", control_type="oracle", include_state_journal=True
        )
    )
    change0 = arm0_ambiguous_journal["state_journal"]["changes"][0]
    conflicting_change = copy.deepcopy(change0)
    conflicting_change["bound_token"] = "conflicting_token_v99"
    arm0_ambiguous_journal["state_journal"]["changes"].append(conflicting_change)
    fact_ambiguous = emit_canary_paired_condition_fact(arm0_ambiguous_journal)
    assert fact_ambiguous.primary_verdict == "unknown", (
        "Ambiguous / duplicate target changes in journal must force verdict to unknown (HOLD)"
    )


def test_canonical_paired_condition_facts_emission(canary_spec: CanaryPairSpec):
    """Canonical PairedConditionFact rows must capture lineage, condition, source digest, and state diff for both arms."""
    arm0_oracle = synthesize_canary_trial_artifacts(
        canary_spec, arm="non_inverted", control_type="oracle", include_state_journal=True
    )
    fact0 = emit_canary_paired_condition_fact(arm0_oracle)

    assert isinstance(fact0, PairedConditionFact)
    assert fact0.pair_id == CANARY_PAIR_ID
    assert fact0.task_id == CANARY_TASK_NON_INVERTED
    assert fact0.variant == "non_inverted"
    assert fact0.condition == "baseline_clean"
    assert fact0.trigger == "initial_fact_binding"
    assert fact0.critical_action == "execute_mutation"
    assert fact0.primary_verdict == "satisfied"
    assert fact0.secondary_verdict == "satisfied"
    assert fact0.source_digest.startswith("sha256:")
    assert (
        fact0.state_diff
        == f"{canary_spec.target_entity}.{canary_spec.target_attribute}={canary_spec.initial_value}"
    )

    arm1_oracle = synthesize_canary_trial_artifacts(
        canary_spec, arm="state_inverted", control_type="oracle", include_state_journal=True
    )
    fact1 = emit_canary_paired_condition_fact(arm1_oracle)

    assert isinstance(fact1, PairedConditionFact)
    assert fact1.pair_id == CANARY_PAIR_ID
    assert fact1.task_id == CANARY_TASK_INVERTED
    assert fact1.variant == "state_inverted"
    assert fact1.condition == "stale_value_override"
    assert fact1.trigger == "inversion_override_binding"
    assert fact1.critical_action == "execute_mutation"
    assert fact1.primary_verdict == "satisfied"
    assert fact1.secondary_verdict == "satisfied"
    assert fact1.source_digest.startswith("sha256:")
    assert (
        fact1.state_diff
        == f"{canary_spec.target_entity}.{canary_spec.target_attribute}={canary_spec.inverted_value}"
    )


def test_campaign_spec_validity_and_zero_billable_guard(canary_spec: CanaryPairSpec):
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
    assert data["schema_version"] == "campaign-design-spec/v1"
    assert data["campaign_id"] == "campaign-action-memory-controls-canary-v1"
    assert data["status"] == "designed-fixture-only"
    assert data["execution_policy"]["allow_billable"] is False
    assert data["execution_policy"]["max_cost_usd"] == 0.0
    assert data["execution_policy"]["max_wall_clock_seconds"] == 600
    assert data["execution_policy"]["max_input_tokens"] == 100000

    pair = data["pair_definition"]
    assert pair["pair_id"] == CANARY_PAIR_ID
    assert pair["seed"] == 42
    assert pair["dose_bytes"] == 4096
    assert pair["realized_context_bytes"] == 4096
    assert pair["contrast_variable"] == "state_inversion_status"
    assert canary_spec.tool_inventory_digest == CANARY_TOOL_SCHEMA_DIGEST
    assert pair["tool_schema_digest"] == CANARY_TOOL_SCHEMA_DIGEST
    assert pair["verifier_truth_digest"] == canary_spec.verifier_truth_digest
    assert len(pair["arms"]) == 2
    assert pair["arms"][0]["task_id"] == CANARY_TASK_NON_INVERTED
    assert pair["arms"][1]["task_id"] == CANARY_TASK_INVERTED
    assert pair["arms"][0]["inversion_count"] == 0
    assert pair["arms"][1]["inversion_count"] == 1
    assert pair["arms"][0]["repeats"] == 1
    assert pair["arms"][1]["repeats"] == 1
