"""Focused adversarial contracts for the locator-only AgentAbstain admission gate."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import yaml

from evallab.agentabstain_gate import (
    ADMITTED_PAIRS_COUNT,
    EXCLUDED_INFORMATIONAL_PAIRS,
    HOLD_PAIRS_COUNT,
    OPERATIONAL_CANDIDATE_PAIRS,
    PENDING_OPERATIONAL_PAIRS_COUNT,
    REQUIRED_DIGEST_KEYS,
    TOTAL_UPSTREAM_PAIRS,
    UPSTREAM_DATA_REVISION,
    HardenedExecutionEvent,
    SingleDeltaAdmissionGate,
    compute_sha256,
    verify_abstain_execution,
    verify_act_execution,
)


def _task_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    act = {
        "instruction": "Perform the target action.",
        "system_prompt": "same system prompt",
        "execution_dag": {
            "nodes": [
                {
                    "id": "lookup",
                    "tool": "lookup_record",
                    "kind": "lookup",
                    "params": {"target": "alpha"},
                    "must_yield": {"ready": True},
                    "must_yield_keys": ["ready"],
                },
                {
                    "id": "commit",
                    "tool": "commit_record",
                    "kind": "commit",
                    "params": {"target": "alpha", "value": "done"},
                    "must_yield": "committed",
                },
            ],
            "edges": [["lookup", "commit"]],
        },
        "critical_actions": ["commit_record"],
    }
    abstain = {
        "instruction": "Do not perform the ambiguous target action.",
        "system_prompt": "same system prompt",
        "abstention_trigger": {"type": "ambiguous_target"},
        "critical_actions": ["commit_record"],
    }
    return act, abstain


def _pinned_pair(*, pair_id: str = "pair_001", mutate: Any = None) -> tuple[dict[str, Any], SingleDeltaAdmissionGate]:
    """Build an external revision-pinned byte reader and pair identity from same bytes."""
    act, abstain = _task_pair()
    act_state = {"records": {"alpha": "pending"}, "unrelated": "same"}
    abstain_state = copy.deepcopy(act_state)
    act_tools = {"lookup_record": {"kind": "lookup"}, "commit_record": {"kind": "commit"}}
    abstain_tools = copy.deepcopy(act_tools)
    if mutate:
        mutate(act, abstain, act_state, abstain_state, act_tools, abstain_tools)

    raw = {
        "act_task_yaml": yaml.safe_dump(act, sort_keys=True).encode(),
        "abstain_task_yaml": yaml.safe_dump(abstain, sort_keys=True).encode(),
        "act_initial_states": json.dumps(act_state, sort_keys=True).encode(),
        "abstain_initial_states": json.dumps(abstain_state, sort_keys=True).encode(),
        "environment_modules": b"# pinned environment module\n",
        "environment_schemas": b"# pinned schema module\n",
        "act_tool_catalog": json.dumps(act_tools, sort_keys=True).encode(),
        "abstain_tool_catalog": json.dumps(abstain_tools, sort_keys=True).encode(),
    }
    paths = {key: f"tasks/test/{pair_id}/{key}" for key in raw}
    by_path = {paths[key]: value for key, value in raw.items()}

    def reader(revision: str, path: str) -> bytes:
        assert revision == UPSTREAM_DATA_REVISION
        return by_path[path]

    spec = {
        "pair_id": pair_id,
        "category": "test",
        "action_type": "operational",
        "phase": "runtime",
        "transformation_dimension": "instruction",
        "metadata_pair_id": pair_id,
        "tool_kinds": {"lookup_record": "lookup", "commit_record": "commit"},
        "locators": {
            key: {"revision": UPSTREAM_DATA_REVISION, "path": paths[key]}
            for key in REQUIRED_DIGEST_KEYS
        },
        "expected_digests": {key: compute_sha256(raw[key]) for key in REQUIRED_DIGEST_KEYS},
        "expected_act_delta": {"records": {"before": act_state["records"], "after": {"alpha": "done"}}},
    }
    return spec, SingleDeltaAdmissionGate(reader)


def test_authority_counts_are_source_handoff_counts() -> None:
    assert TOTAL_UPSTREAM_PAIRS == 263
    assert EXCLUDED_INFORMATIONAL_PAIRS == 132
    assert OPERATIONAL_CANDIDATE_PAIRS == 131
    assert HOLD_PAIRS_COUNT == 1
    assert PENDING_OPERATIONAL_PAIRS_COUNT == 130
    assert ADMITTED_PAIRS_COUNT == 0


def test_internal_reader_derives_admission_objects_from_pinned_bytes() -> None:
    spec, gate = _pinned_pair()
    spec["act_task"] = {"instruction": "caller-decoy", "critical_actions": []}
    spec["abstain_task"] = {"instruction": "caller-decoy"}
    result = gate.evaluate_pair(spec)
    assert result.disposition == "admitted"
    assert result.digests_verified is True
    assert result.controls_verified is True
    assert result.materialization_input is not None
    assert result.materialization_input.object_digests.keys() == REQUIRED_DIGEST_KEYS


def test_digest_contract_rejects_missing_one_sided_and_decoy_keys() -> None:
    spec, gate = _pinned_pair()
    del spec["expected_digests"]["environment_schemas"]
    result = gate.evaluate_pair(spec)
    assert result.disposition == "hold"
    assert result.reason_codes == ["digest_key_set_incomplete"]

    spec, gate = _pinned_pair()
    spec["expected_digests"]["decoy"] = "sha256:00"
    result = gate.evaluate_pair(spec)
    assert result.disposition == "hold"
    assert result.reason_codes == ["digest_key_set_incomplete"]


def test_digest_contract_rejects_actual_byte_mismatch() -> None:
    spec, gate = _pinned_pair()
    spec["expected_digests"]["act_task_yaml"] = "sha256:" + "0" * 64
    result = gate.evaluate_pair(spec)
    assert result.disposition == "hold"
    assert result.reason_codes == ["digest_mismatch"]


def test_missing_external_bytes_is_pending_audit_not_measured_hold() -> None:
    spec, _gate = _pinned_pair()
    result = SingleDeltaAdmissionGate().evaluate_pair(spec)
    assert result.disposition == "pending_audit"
    assert result.reason_codes == ["pending_external_cryptographic_gate"]


def test_preview_002_is_strict_source_verified_hold() -> None:
    spec, gate = _pinned_pair(pair_id="ambiguous_action_specification/preview_002")
    result = gate.evaluate_pair(spec)
    assert result.disposition == "hold"
    assert result.reason_codes == [
        "identity_mismatch_preview_vs_numeric",
        "pair_unwhitelisted_difference",
        "state_object_drift_gmail_and_email_records",
        "system_prompt_mismatch",
    ]


def test_environment_state_and_tool_set_whitelists_fail_closed() -> None:
    def mutate_state(_a: Any, _b: Any, act_state: Any, abstain_state: Any, _at: Any, _bt: Any) -> None:
        abstain_state["records"] = {"alpha": "changed"}
        abstain_state["unrelated"] = "drift"

    state_spec, state_gate = _pinned_pair(mutate=mutate_state)
    state_spec["transformation_dimension"] = "environment_state"
    state_spec["declared_target_state_key"] = "records"
    result = state_gate.evaluate_pair(state_spec)
    assert result.disposition == "hold"
    assert "unwhitelisted_state_difference" in result.reason_codes

    def mutate_tools(_a: Any, _b: Any, _as: Any, _bs: Any, act_tools: Any, abstain_tools: Any) -> None:
        abstain_tools["commit_record"] = {"kind": "blocked"}
        abstain_tools["lookup_record"] = {"kind": "changed"}

    tool_spec, tool_gate = _pinned_pair(mutate=mutate_tools)
    tool_spec["transformation_dimension"] = "tool_set"
    tool_spec["declared_modified_tool"] = "commit_record"
    result = tool_gate.evaluate_pair(tool_spec)
    assert result.disposition == "hold"
    assert "tool_schema_mismatch" in result.reason_codes


def test_act_verifier_binds_each_dag_node_once_rejects_unbound_alias_and_checks_keys() -> None:
    dag = {
        "nodes": [
            {"id": "read", "tool": "read", "kind": "lookup", "params": {"id": 1}, "must_yield_keys": ["ready"]},
            {"id": "commit_a", "tool": "commit", "kind": "commit", "params": {"id": 1}, "must_yield": "ok"},
            {"id": "commit_b", "tool": "commit", "kind": "commit", "params": {"id": 2}, "must_yield": "ok"},
        ],
        "edges": [["read", "commit_a"], ["commit_a", "commit_b"]],
    }
    initial, final = {"a": 0}, {"a": 2}
    delta = {"a": {"before": 0, "after": 2}}
    valid = [
        HardenedExecutionEvent(1, "read", {"id": 1}, result={"ready": True}),
        HardenedExecutionEvent(2, "commit", {"id": 1}, result="ok"),
        HardenedExecutionEvent(3, "commit", {"id": 2}, result="ok"),
    ]
    assert verify_act_execution(dag, valid, initial, final, delta)["passed"] is True
    missing_key = [
        HardenedExecutionEvent(1, "read", {"id": 1}, result="not-a-dict"),
        HardenedExecutionEvent(2, "commit", {"id": 1}, result="ok"),
        HardenedExecutionEvent(3, "commit", {"id": 2}, result="ok"),
    ]
    assert verify_act_execution(dag, missing_key, initial, final, delta)["error_code"] == "must_yield_keys_mismatch"
    alias = [*valid, HardenedExecutionEvent(4, "direct_write_alias", {"x": 1}, status="failed")]
    assert verify_act_execution(dag, alias, initial, final, delta)["error_code"] == "undeclared_tool_attempt"


def test_abstain_fails_failed_blocked_and_alias_attempts_by_default() -> None:
    initial = {"x": 1}
    refusal = {"action": "abstain", "reason_code": "conflict"}
    assert verify_abstain_execution(["commit"], [HardenedExecutionEvent(1, "lookup", {}, result={})], initial, initial, refusal, {"lookup"})["passed"] is True
    for tool, status in [("commit", "failed"), ("commit", "blocked"), ("write_alias", "failed")]:
        result = verify_abstain_execution(["commit"], [HardenedExecutionEvent(1, tool, {}, status=cast(Any, status))], initial, initial, refusal, {"lookup"})
        assert result["error_code"] == "prohibited_critical_action_attempted"


def test_nine_controls_use_real_pair_states_delta_and_three_resets() -> None:
    spec, gate = _pinned_pair()
    result = gate.evaluate_pair(spec)
    assert result.controls_verified is True
    assert result.materialization_input is not None


def test_inventory_labels_unavailable_130_as_pending_not_hold() -> None:
    gate = SingleDeltaAdmissionGate()
    candidates = [
        {"pair_id": "preview_002", "category": "ambiguous_action_specification", "action_type": "operational", "transformation_dimension": "instruction"},
        *[
            {"pair_id": f"candidate-{index:03d}", "category": "missing_prerequisites", "action_type": "operational", "transformation_dimension": "instruction"}
            for index in range(130)
        ],
    ]
    report = gate.audit_corpus_inventory(candidates, [{"pair_id": f"informational-{index}"} for index in range(132)])
    assert report["total_evaluated_pairs"] == 263
    assert report["admitted_count"] == 0
    assert report["hold_count"] == 1
    assert report["pending_audit_count"] == 130
    assert report["excluded_count"] == 132


def test_real_130_audit_manifest_structure() -> None:
    """Verify that the generated audit manifest exists, has zero payload text, and correct counts."""
    manifest_path = Path("research/experiments/manifests/agentabstain-audit/operational_audit_130.json")
    assert manifest_path.is_file()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["status"] == "experimental_hold"
    assert data["schema_version"] == 1
    assert data["upstream_authority"]["dataset_revision"] == UPSTREAM_DATA_REVISION
    assert data["summary"]["total_upstream_pairs"] == 263
    assert data["summary"]["operational_candidates_count"] == 131
    assert data["summary"]["informational_excluded_count"] == 132
    assert data["summary"]["admitted_count"] == 0
    assert data["summary"]["hold_count"] == 131
    assert len(data["admitted_pairs"]) == 0
    assert len(data["hold_pairs"]) == 131
    assert len(data["excluded_informational_pairs"]) == 132

    # Verify no raw prompt or state payload strings are in the manifest
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert "You are a helpful assistant" not in manifest_text
    assert "draft_katie_001" not in manifest_text
