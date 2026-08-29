"""Focused tests for the deterministic C0 mechanical screening projection.

Covers the C0 causal-grade contract: nested artifact loading, source/trajectory/
verifier digests, explicit opportunity denominators, missing/invalid evidence
coverage, quality/refusal disposition, machine-readable ``causal_grade="C0"``, and
the enumerated refusal of any C0 -> causal/matched/intervention promotion.

Fixtures exercised here are synthetic and self-contained (no paid calls).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from evallab.interpretation.benchmark_events import (
    BenchmarkMissingArtifactError,
    C0PromotionRefusal,
    C0ScreeningProjection,
    correlate_tool_calls,
    discover_promoted_trial_dirs,
    is_application_error,
    load_trial_bundle,
    parse_benchmark_events,
    project_c0_screening,
    project_promoted_trials_c0,
    refuse_causal_promotion,
    validate_projection_digest,
)

_CONTRACT = {
    "benchmark_family": "action-memory-v1",
    "version": "1.0.0",
    "construct": "actionable_entity_memory_and_value_binding",
    "seeds": [42],
    "cells": [
        {
            "cell_id": "clean-baseline-4k",
            "dose_bytes": 4096,
            "arm": "clean",
            "update_opportunity_count": 1,
            "read_opportunity_count": 7,
            "mutation_opportunity_count": 1,
        }
    ],
    "opportunity_counts": {
        "update_opportunity_count": 1,
        "read_opportunity_count": 7,
        "mutation_opportunity_count": 1,
    },
    "verifier_truth_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
}

_VALID_EVENTS = [
    {
        "event_index": 0,
        "timestamp": "2026-08-28T10:00:00Z",
        "event_type": "mcp_call",
        "payload": {"tool_call_id": "call_1", "tool_name": "read_chunk", "arguments": {}},
    },
    {
        "event_index": 1,
        "timestamp": "2026-08-28T10:00:01Z",
        "event_type": "tool_call_success",
        "payload": {"tool_call_id": "call_1", "result": {"ok": True}},
    },
]

_FINAL_STATE = {
    "trial_id": "valid_trial",
    "status": "executed",
    "target_entity": "entity_42",
    "invariants_passed": True,
}

_ATIF_TRAJECTORY = {
    "schema_version": "1.0.0",
    "trial_id": "valid_trial",
    "steps": [],
}


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_events(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e) for e in _VALID_EVENTS) + "\n", encoding="utf-8"
    )


def _write_verifier(trial: Path, *, passed: bool, reward: float | None = None) -> None:
    payload: dict[str, object] = {"passed": passed}
    if reward is not None:
        payload["reward"] = reward
    _write_json(trial / "verifier" / "result.json", payload)


def _write_harness_result(trial: Path, *, task_name: str, exception_info: object | None) -> None:
    _write_json(
        trial / "result.json",
        {"id": "trial-uuid", "task_name": task_name, "exception_info": exception_info},
    )


def _build_valid_trial(tmp_path: Path, name: str = "valid_trial") -> Path:
    trial = tmp_path / name
    _write_json(trial / "benchmark_contract.json", _CONTRACT)
    _write_events(trial / "benchmark-events.jsonl")
    _write_json(trial / "final-state.json", _FINAL_STATE)
    _write_json(trial / "agent" / "trajectory.json", _ATIF_TRAJECTORY)
    _write_verifier(trial, passed=True, reward=1.0)
    _write_harness_result(trial, task_name="evallab/action-memory-clean", exception_info=None)
    return trial


# ---------------------------------------------------------------------------
# Fixtures: valid, missing trajectory, malformed nested artifact,
#           harness exception, scored failure
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_trial(tmp_path: Path) -> Path:
    return _build_valid_trial(tmp_path)


@pytest.fixture
def missing_trajectory_trial(tmp_path: Path) -> Path:
    trial = _build_valid_trial(tmp_path, name="missing_trajectory")
    traj = trial / "agent" / "trajectory.json"
    if traj.is_file():
        traj.unlink()
    return trial


@pytest.fixture
def malformed_nested_artifact_trial(tmp_path: Path) -> Path:
    """Nested Function-DAG style artifact with malformed JSON (extra data)."""
    trial = tmp_path / "malformed_nested"
    nested = trial / "artifacts" / "app" / "output" / "result.json"
    nested.parent.mkdir(parents=True, exist_ok=True)
    # Mirrors the real promoted corpus: "3\n{...}" -> JSONDecodeError "Extra data".
    nested.write_text('3\n{"target": "n_2_0", "value": 3}\n', encoding="utf-8")
    _write_json(trial / "agent" / "trajectory.json", _ATIF_TRAJECTORY)
    _write_verifier(trial, passed=False)
    _write_harness_result(trial, task_name="evallab/syn-funcdag-easy", exception_info=None)
    return trial


@pytest.fixture
def harness_exception_trial(tmp_path: Path) -> Path:
    """Real Harbor shape: exception_info carries ``exception_type``."""
    trial = _build_valid_trial(tmp_path, name="harness_exception")
    _write_harness_result(
        trial,
        task_name="evallab/action-memory-clean",
        exception_info={
            "exception_type": "AgentTimeoutError",
            "exception_message": "agent timed out during execution",
        },
    )
    return trial


@pytest.fixture
def scored_failure_trial(tmp_path: Path) -> Path:
    trial = _build_valid_trial(tmp_path, name="scored_failure")
    _write_verifier(trial, passed=False, reward=0.0)
    return trial


# ---------------------------------------------------------------------------
# Core contract
# ---------------------------------------------------------------------------


def test_valid_trial_projection(valid_trial: Path) -> None:
    proj = project_c0_screening(valid_trial, trial_id="valid_trial", task_name="t")
    assert isinstance(proj, C0ScreeningProjection)
    assert proj.causal_grade == "C0"
    assert proj.causal_claim_allowed is False
    assert proj.synthetic_recipe_eligible is False
    assert proj.claim_scope == "mechanical_screening_only"
    assert proj.mechanical_source == "benchmark_events"
    assert proj.benchmark_contract_present is True
    assert proj.benchmark_events_present is True
    assert proj.final_state_present is True
    assert proj.trajectory_present is True
    assert proj.verifier_present is True
    assert proj.tool_call_count == 1
    assert proj.opportunity_denominator == "tool_call_count"
    assert proj.opportunity_count == 1
    assert proj.quality_disposition == "SCORED_PASS"
    assert proj.verifier_passed is True
    assert proj.source_sha256 and proj.trajectory_sha256 and proj.verifier_sha256
    assert proj.benchmark_events_sha256 and proj.final_state_sha256


def test_missing_trajectory_coverage(missing_trajectory_trial: Path) -> None:
    proj = project_c0_screening(
        missing_trajectory_trial, trial_id="missing_trajectory", task_name="t"
    )
    assert proj.trajectory_present is False
    assert proj.trajectory_sha256 is None
    assert "MISSING_TRAJECTORY" in proj.projection_refusals


def test_malformed_nested_artifact_preserved(malformed_nested_artifact_trial: Path) -> None:
    nested = (
        malformed_nested_artifact_trial / "artifacts" / "app" / "output" / "result.json"
    )
    before = nested.read_bytes()
    # Must not crash on the malformed nested artifact.
    proj = project_c0_screening(
        malformed_nested_artifact_trial, trial_id="m", task_name="t"
    )
    # File is preserved byte-for-byte.
    assert nested.read_bytes() == before
    assert "MALFORMED_NESTED_ARTIFACT" in proj.projection_refusals
    assert "artifacts/app/output/result.json" in proj.malformed_nested_artifacts
    assert proj.causal_grade == "C0"


def test_harness_exception_disposition(harness_exception_trial: Path) -> None:
    proj = project_c0_screening(
        harness_exception_trial, trial_id="h", task_name="t"
    )
    assert proj.quality_disposition == "HARNESS_EXCEPTION"
    assert proj.harness_exception_class == "AgentTimeoutError"
    assert "HARNESS_EXCEPTION" in proj.projection_refusals


def test_scored_failure_disposition(scored_failure_trial: Path) -> None:
    proj = project_c0_screening(scored_failure_trial, trial_id="f", task_name="t")
    assert proj.quality_disposition == "SCORED_FAIL"
    assert proj.verifier_passed is False
    assert proj.verifier_reward == 0.0


def test_missing_trial_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkMissingArtifactError):
        project_c0_screening(tmp_path / "does-not-exist", trial_id="x", task_name="t")


def test_opportunity_denominator_null_preserved(tmp_path: Path) -> None:
    """No events and no ATIF fallback -> NULL denominator, never 0."""
    trial = tmp_path / "empty"
    trial.mkdir(parents=True)
    proj = project_c0_screening(trial, trial_id="empty", task_name="t")
    assert proj.opportunity_count is None
    assert proj.opportunity_denominator is None
    assert proj.tool_error_rate_screening is None
    assert proj.projection_status == "REFUSED"
    assert proj.quality_disposition == "REFUSED"


def test_atif_fallback_counts(valid_trial: Path) -> None:
    # Strip benchmark events -> mechanical facts fall back to ATIF counts.
    (valid_trial / "benchmark-events.jsonl").unlink()
    proj = project_c0_screening(
        valid_trial,
        trial_id="t",
        task_name="t",
        atif_tool_call_count=7,
        atif_tool_error_count=2,
        atif_tool_error_rate=0.2857,
    )
    assert proj.mechanical_source == "atif_trajectory"
    assert proj.tool_call_count == 7
    assert proj.tool_error_count == 2
    assert proj.opportunity_count == 7


def test_deterministic_reprojection(valid_trial: Path) -> None:
    a = project_c0_screening(valid_trial, trial_id="t", task_name="t")
    b = project_c0_screening(valid_trial, trial_id="t", task_name="t")
    assert asdict(a) == asdict(b)


# ---------------------------------------------------------------------------
# Causal promotion refusal (never promote above C0)
# ---------------------------------------------------------------------------


def test_c0_never_promotes_above_c0(valid_trial: Path) -> None:
    proj = project_c0_screening(valid_trial, trial_id="t", task_name="t")
    for requested in ("causal", "matched", "intervention", "C1", "C2", "twin", "effect"):
        refusal = refuse_causal_promotion(proj, requested)
        assert isinstance(refusal, C0PromotionRefusal)
        assert refusal.allowed is False
        assert refusal.refusal_codes
        assert refusal.reason
    # C0 -> causal refusal is enumerated.
    assert refuse_causal_promotion(proj, "causal").refusal_codes == ("C0_MECHANICAL_ONLY",)
    assert refuse_causal_promotion(proj, "matched").refusal_codes == ("C0_NO_MATCHED_CONTROL",)
    assert refuse_causal_promotion(proj, "intervention").refusal_codes == (
        "C0_NO_INTERVENTION_IDENTITY",
    )


def test_c0_projection_never_allows_causal_claim(valid_trial: Path) -> None:
    proj = project_c0_screening(valid_trial, trial_id="t", task_name="t")
    assert proj.causal_claim_allowed is False
    assert proj.causal_grade == "C0"


# ---------------------------------------------------------------------------
# Nested artifact loading (load_trial_bundle / ingest)
# ---------------------------------------------------------------------------


def test_load_trial_bundle_resolves_nested_artifacts(tmp_path: Path) -> None:
    trial = tmp_path / "nested_bundle"
    # Contract at top level; events + final state nested under artifacts/app/output/.
    _write_json(trial / "benchmark_contract.json", _CONTRACT)
    _write_events(trial / "artifacts" / "app" / "output" / "benchmark-events.jsonl")
    _write_json(trial / "artifacts" / "app" / "output" / "final-state.json", _FINAL_STATE)
    bundle = load_trial_bundle(trial)
    assert bundle.contract.family == "action-memory-v1"
    assert len(bundle.events) == 2
    assert len(bundle.correlated_calls) == 1
    assert bundle.final_state.invariants_passed is True


# ---------------------------------------------------------------------------
# Deterministic projection over a promoted-trial corpus
# ---------------------------------------------------------------------------


def test_project_promoted_trials_c0_deterministic(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    job = runs / "zai-flash-synth-r1"
    job.mkdir(parents=True)
    _write_json(job / "PROMOTION.json", {"schema_version": 2, "bundle": "x"})
    _build_valid_trial(job, name="syn__Aa")
    _build_valid_trial(job, name="syn__Bb")
    # Non-promoted job must be skipped by default.
    other = runs / "not-promoted"
    other.mkdir()
    _build_valid_trial(other, name="syn__Cc")

    first = project_promoted_trials_c0(runs)
    second = project_promoted_trials_c0(runs)
    ids = [p.trial_id for p in first]
    assert ids == sorted(ids)
    assert [p.trial_id for p in second] == ids
    assert all(p.causal_grade == "C0" for p in first)
    assert all(p.causal_claim_allowed is False for p in first)
    # Sorted deterministic output; only promoted job's trials included.
    assert "syn__Aa" in ids and "syn__Bb" in ids
    assert "syn__Cc" not in ids


def test_discover_promoted_trial_dirs_missing_root(tmp_path: Path) -> None:
    assert discover_promoted_trial_dirs(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# Regression: canonical application-error predicate (wave2 Action not_found)
# ---------------------------------------------------------------------------


def test_is_application_error_predicate() -> None:
    # wave2 Action get_context_chunk miss: transport success, application error.
    assert is_application_error({"status": "ok", "value": {"error": "not_found"}}) is True
    # Nested result containers.
    assert is_application_error({"result": {"value": {"error": "nope"}}}) is True
    assert is_application_error({"result": {"error": "boom"}}) is True
    assert is_application_error({"output": {"status": "failed"}}) is True
    # Status / flags.
    assert is_application_error({"status": "error"}) is True
    assert is_application_error({"is_error": True}) is True
    # Clean results are NOT relabeled as errors.
    assert is_application_error({"content": "print('hello')"}) is False
    assert is_application_error({"status": "ok", "value": {"data": 1}}) is False
    assert is_application_error({"ok": True}) is False
    assert is_application_error(None) is False


def test_correlate_counts_application_error_not_transport(tmp_path: Path) -> None:
    """A successful transport call with an application error counts as an error."""
    events = [
        {
            "event_index": 0,
            "timestamp": "2026-08-29T10:00:00Z",
            "event_type": "mcp_call",
            "payload": {"tool_call_id": "call_h", "tool_name": "get_context_chunk", "arguments": {}},
        },
        {
            "event_index": 1,
            "timestamp": "2026-08-29T10:00:01Z",
            "event_type": "tool_call_success",
            "payload": {
                "tool_call_id": "call_h",
                "result": {"status": "ok", "value": {"error": "not_found"}},
            },
        },
    ]
    parsed = parse_benchmark_events(events)
    correlated = correlate_tool_calls(parsed)
    assert len(correlated) == 1
    # Transport still succeeded (result event present), but the application payload is an error.
    assert correlated[0].result_event is not None
    assert correlated[0].is_error is True


def test_tool_error_count_counts_application_error(tmp_path: Path) -> None:
    trial = tmp_path / "wave2_fault"
    trial.mkdir(parents=True)
    events = [
        {
            "event_index": 0,
            "timestamp": "2026-08-29T10:00:00Z",
            "event_type": "mcp_call",
            "payload": {"tool_call_id": "call_1", "tool_name": "read_chunk", "arguments": {}},
        },
        {
            "event_index": 1,
            "timestamp": "2026-08-29T10:00:01Z",
            "event_type": "tool_call_success",
            "payload": {
                "tool_call_id": "call_1",
                "result": {"status": "ok", "value": {"error": "not_found"}},
            },
        },
        {
            "event_index": 2,
            "timestamp": "2026-08-29T10:00:02Z",
            "event_type": "mcp_call",
            "payload": {"tool_call_id": "call_2", "tool_name": "read_chunk", "arguments": {}},
        },
        {
            "event_index": 3,
            "timestamp": "2026-08-29T10:00:03Z",
            "event_type": "tool_call_success",
            "payload": {"tool_call_id": "call_2", "result": {"status": "ok", "value": {"data": 1}}},
        },
    ]
    trial.joinpath("benchmark-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    proj = project_c0_screening(trial, trial_id="wave2_fault", task_name="t")
    assert proj.mechanical_source == "benchmark_events"
    assert proj.tool_call_count == 2
    assert proj.tool_error_count == 1  # not_found counted, clean call not relabeled
    assert proj.tool_error_rate_screening == 0.5
    assert proj.causal_grade == "C0"


# ---------------------------------------------------------------------------
# Regression: projection digest identity + auditable refusals
# ---------------------------------------------------------------------------


def test_projection_digest_present_and_valid(valid_trial: Path) -> None:
    proj = project_c0_screening(valid_trial, trial_id="t", task_name="t")
    assert isinstance(proj.projection_digest, str) and len(proj.projection_digest) == 64
    assert validate_projection_digest(proj) is True


def test_projection_digest_detects_body_change(valid_trial: Path) -> None:
    a = project_c0_screening(valid_trial, trial_id="t", task_name="t")
    b = project_c0_screening(valid_trial, trial_id="t", task_name="t")
    # Same body -> same digest.
    assert a.projection_digest == b.projection_digest
    # A different trial_id (projection body) must change the digest.
    c = project_c0_screening(valid_trial, trial_id="other", task_name="t")
    assert c.projection_digest != a.projection_digest


def test_refusal_bound_to_trial_and_digest(valid_trial: Path) -> None:
    proj = project_c0_screening(valid_trial, trial_id="audit_trial", task_name="t")
    refusal = refuse_causal_promotion(proj, "intervention")
    assert refusal.trial_id == "audit_trial"
    assert refusal.projection_digest == proj.projection_digest
    assert validate_projection_digest(proj) is True
