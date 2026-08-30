"""Isolated feature producer for Error Detection & Autonomous Recovery (mcp-recovery-v1).

Computes:
- L1 Facts (C0, C2, C3, Grade A): task_success, total_tool_calls, injected_fault_record,
  injected_fault_count, fault_detected_count, post_fault_retries, blind_retries,
  certified_recovered_faults, step_to_first_fault, step_to_recovery, prompt metrics.
- L2 Derived Metrics (C0, C2, C3): schema_conformance_rate, autonomous_recovery_rate,
  fault_detection_rate, blind_retry_rate, fault_recovery_latency.
- Strict NULL preservation: missing or zero opportunity denominators yield NULL (None), never 0.0.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from evallab.interpretation.benchmark_events import (
    CorrelatedToolCall,
    TrialBundle,
)
from evallab.interpretation.benchmark_projection import (
    BenchmarkProjectionDimensions,
    build_projection_dimensions,
    parse_native_persistence_level,
    projection_feature_fields,
)
from evallab.interpretation.feature_registry import compute_prompt_cache_hit_rate


@dataclass(frozen=True)
class McpRecoveryFeatures:
    """Feature record for mcp-recovery-v1 trial."""

    # Identity
    trial_id: str
    family: str
    task_id: str
    seed: int
    fault_class: str | None
    persistence_level: int
    mode: str
    construct: str
    causal_grade: str
    job_id: str | None
    cas_uri: str | None
    model_name: str | None
    agent_name: str | None
    task_name: str | None
    harness_version: str | None
    scaffold_version: str | None
    repeat_group_id: str | None
    dose_axis: str | None
    dose_value: float | None
    dose_unit: str | None
    alphabet_id: str | None
    alphabet_version: str | None
    quality_status: str | None
    report_digest: str | None
    source_digest: str | None
    producer_version: str
    projection_identity: str
    dimension_digest: str
    projection_status: str
    analysis_ready: bool
    projection_refusals: str
    # L1 Facts (C0, C2, C3, Grade A)
    task_success: bool
    total_tool_calls: int
    model_call_count: int
    prompt_tokens_per_step: float | None
    prompt_cache_hit_rate: float | None
    injected_fault_record: str | None
    injected_fault_count: int
    fault_detected_count: int
    post_fault_retries: int
    blind_retries: int
    certified_recovered_faults: int
    step_to_first_fault: int | None
    step_to_recovery: int | None
    diagnosis_class: str | None
    source_error_count: int
    propagated_error_count: int
    strategy_changed_after_failure: bool | None
    controlled_replay_available: bool
    controlled_replay_outcome_delta: float | None
    max_blind_retry_streak: int
    recovery_succeeded_at_persistence: bool | None

    # L2 Derived Metrics (C0, C2, C3) - NULL-preserving on zero denominator
    schema_conformance_rate: float | None
    autonomous_recovery_rate: float | None
    fault_detection_rate: float | None
    blind_retry_rate: float | None
    fault_recovery_latency: float | None

    # Provenance / citations
    citation: str
    verifier_truth_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecoveryPersistencePoint:
    """Observed certified recovery at one native fault persistence level."""

    persistence_level: int
    trial_count: int
    recovered_count: int
    recovery_rate: float


def build_recovery_persistence_curve(
    records: Sequence[McpRecoveryFeatures],
) -> tuple[RecoveryPersistencePoint, ...]:
    """Aggregate fault-arm trials without pooling distinct persistence levels."""
    grouped: dict[int, list[McpRecoveryFeatures]] = {}
    for record in records:
        if record.injected_fault_count <= 0:
            continue
        grouped.setdefault(record.persistence_level, []).append(record)
    points = []
    for level, level_records in sorted(grouped.items()):
        recovered = sum(
            record.recovery_succeeded_at_persistence is True for record in level_records
        )
        points.append(
            RecoveryPersistencePoint(
                persistence_level=level,
                trial_count=len(level_records),
                recovered_count=recovered,
                recovery_rate=recovered / len(level_records),
            )
        )
    return tuple(points)


def extract_mcp_recovery_features(
    bundle: TrialBundle,
    step_tokens: Sequence[int] | None = None,
    dimensions: BenchmarkProjectionDimensions | None = None,
    cached_step_tokens: Sequence[int] | None = None,
) -> McpRecoveryFeatures:
    """Extract deterministic mechanical facts and L2 metrics from an mcp-recovery trial bundle."""
    contract = bundle.contract
    final_state = bundle.final_state
    events = bundle.events
    calls = bundle.correlated_calls
    dimensions = dimensions or build_projection_dimensions(bundle, None)
    cell_factors = contract.cell_factors
    fault_classes = cell_factors.get("fault_classes", [])
    raw_persistence_levels = cell_factors.get("persistence_levels", [])
    if not isinstance(raw_persistence_levels, list) or len(raw_persistence_levels) != 1:
        raise ValueError("Recovery contract must declare exactly one native persistence level")
    persistence_level = parse_native_persistence_level(raw_persistence_levels[0])
    if persistence_level is None:
        raise ValueError("Recovery contract persistence level must be a finite positive integer")
    fault_class = str(fault_classes[0]) if fault_classes else None
    mode = str(cell_factors.get("mode", "fault" if fault_class else "clean"))

    task_success = final_state.invariants_passed
    total_tool_calls = len(calls)
    model_call_count = (
        len([e for e in events if e.event_type in ("mcp_call", "tool_call", "model_call")]) or 1
    )

    injected_faults: list[str] = []
    fault_detected_count = 0
    post_fault_retries = 0
    blind_retries = 0
    certified_recovered_faults = 0
    step_to_first_fault: int | None = None
    step_to_recovery: int | None = None
    valid_schema_calls = 0
    source_error_count = 0
    propagated_error_count = 0
    max_blind_retry_streak = 0
    current_blind_retry_streak = 0
    source_fault_seen = False

    last_failing_call: CorrelatedToolCall | None = None
    fault_active = False

    for idx, call in enumerate(calls, start=1):
        args = call.arguments
        is_schema_valid = isinstance(args, dict) and not call.is_error
        if is_schema_valid:
            valid_schema_calls += 1

        if call.is_fault_injected:
            source_error_count += 1
            source_fault_seen = True
        elif call.is_error and source_fault_seen:
            propagated_error_count += 1
        if call.is_fault_injected or call.is_error:
            fc = call.fault_class or "generic_error"
            injected_faults.append(fc)
            if step_to_first_fault is None:
                step_to_first_fault = idx

            last_failing_call = call
            fault_active = True

        elif fault_active:
            # We are after a fault
            post_fault_retries += 1
            # Check if this retry is a blind retry (repeating exact failing call arguments)
            if last_failing_call is not None:
                prev_args = last_failing_call.arguments
                prev_tool = last_failing_call.tool_name
                if call.tool_name == prev_tool and args == prev_args:
                    blind_retries += 1
                    current_blind_retry_streak += 1
                    max_blind_retry_streak = max(
                        max_blind_retry_streak,
                        current_blind_retry_streak,
                    )
                else:
                    # Agent adapted arguments or switched tools -> detected/acknowledged fault
                    fault_detected_count += 1
                    current_blind_retry_streak = 0

            if not call.is_error:
                # Succeeded after fault
                if step_to_recovery is None:
                    step_to_recovery = idx
                fault_active = False

    for ev in events:
        if ev.event_type in ("fault_injected", "fault"):
            fc = str(ev.payload.get("fault_type", ev.payload.get("fault_class", "injected_fault")))
            if fc not in injected_faults:
                injected_faults.append(fc)
        elif ev.event_type in ("autonomous_recovery_observed", "recovery_observed", "recovered"):
            certified_recovered_faults = max(certified_recovered_faults, 1)
            fault_detected_count = max(fault_detected_count, 1)

    if "faults_injected_count" in final_state.details:
        injected_fault_count = max(
            len(injected_faults), int(final_state.details["faults_injected_count"])
        )
    else:
        injected_fault_count = len(injected_faults)

    if "autonomous_recoveries_count" in final_state.details:
        certified_recovered_faults = max(
            certified_recovered_faults, int(final_state.details["autonomous_recoveries_count"])
        )
        if certified_recovered_faults > 0:
            fault_detected_count = max(fault_detected_count, certified_recovered_faults)

    injected_fault_record = json.dumps(injected_faults) if injected_faults else None
    diagnosis_class: str | None = None
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        candidate = payload.get("diagnosis_class") or payload.get("diagnosed_fault_class")
        if candidate is not None:
            diagnosis_class = str(candidate)
            break
        if event.event_type in {"fault_diagnosed", "diagnosis"}:
            fallback = payload.get("fault_class") or payload.get("fault_type")
            if fallback is not None:
                diagnosis_class = str(fallback)
                break

    strategy_changed_after_failure: bool | None = None
    if injected_fault_count > 0:
        strategy_changed_after_failure = (
            post_fault_retries > blind_retries or fault_detected_count > 0
        )

    controlled_replay_ref = (
        cell_factors.get("clean_twin_id")
        or cell_factors.get("paired_trial_id")
        or cell_factors.get("controlled_replay_id")
    )
    controlled_replay_available = controlled_replay_ref is not None
    raw_replay_delta = cell_factors.get("controlled_replay_outcome_delta")
    controlled_replay_outcome_delta = (
        float(raw_replay_delta) if isinstance(raw_replay_delta, (int, float)) else None
    )

    # Certified autonomous recovery requires all 5 gates:
    # 1. Injected fault occurred (injected_fault_count > 0)
    # 2. Zero human intervention (human_interventions == 0 and no human intervention event in stream)
    # 3. Final state invariants passed (task_success is True)
    # 4. Recovery step occurred strictly after first fault (step_to_recovery > step_to_first_fault)
    # 5. Paired fixed-policy failure gate: Agent adapted behavior (blind_retries < post_fault_retries or fault_detected_count > 0)
    human_interventions = int(final_state.details.get("human_interventions_count", 0))
    has_human_intervention_event = any(
        e.event_type
        in (
            "human_intervention",
            "user_intervention",
            "operator_intervention",
            "manual_override",
        )
        or (isinstance(e.payload, dict) and e.payload.get("is_human", False))
        for e in events
    )
    no_human_intervention = human_interventions == 0 and not has_human_intervention_event

    has_valid_step_recovery = (
        step_to_recovery is not None
        and step_to_first_fault is not None
        and step_to_recovery > step_to_first_fault
    )
    fixed_policy_failure_gate = blind_retries < post_fault_retries or fault_detected_count > 0

    is_c3_certified = (
        injected_fault_count > 0
        and task_success
        and no_human_intervention
        and has_valid_step_recovery
        and fixed_policy_failure_gate
    )
    if is_c3_certified:
        certified_recovered_faults = max(certified_recovered_faults, 1)
    causal_grade = "C3" if is_c3_certified else ("C0" if injected_fault_count == 0 else "C1")
    # Prompt token metrics
    prompt_tokens_per_step: float | None = None
    if step_tokens:
        prompt_tokens_per_step = float(sum(step_tokens) / len(step_tokens))

    prompt_cache_hit_rate = compute_prompt_cache_hit_rate(step_tokens, cached_step_tokens)
    # L2 derived metrics with strict NULL preservation
    # 1. schema_conformance_rate: denom is total_tool_calls
    schema_conformance_rate: float | None = None
    if total_tool_calls > 0:
        schema_conformance_rate = float(valid_schema_calls / total_tool_calls)

    # 2. autonomous_recovery_rate: denom is injected_fault_count
    autonomous_recovery_rate: float | None = None
    if injected_fault_count > 0:
        autonomous_recovery_rate = float(certified_recovered_faults / injected_fault_count)

    # 3. fault_detection_rate: denom is injected_fault_count
    fault_detection_rate: float | None = None
    if injected_fault_count > 0:
        fault_detection_rate = float(
            min(fault_detected_count, injected_fault_count) / injected_fault_count
        )

    # 4. blind_retry_rate: denom is post_fault_retries
    blind_retry_rate: float | None = None
    if post_fault_retries > 0:
        blind_retry_rate = float(blind_retries / post_fault_retries)

    # 5. fault_recovery_latency: denom is certified_recovered_faults
    fault_recovery_latency: float | None = None
    if (
        certified_recovered_faults > 0
        and step_to_recovery is not None
        and step_to_first_fault is not None
        and step_to_recovery > step_to_first_fault
    ):
        fault_recovery_latency = float(step_to_recovery - step_to_first_fault)
    recovery_succeeded_at_persistence: bool | None = None
    if injected_fault_count > 0:
        recovery_succeeded_at_persistence = certified_recovered_faults > 0
    citation = bundle.build_citation()

    return McpRecoveryFeatures(
        trial_id=bundle.trial_id,
        family=contract.family,
        task_id=contract.task_id,
        seed=contract.seed,
        fault_class=fault_class,
        persistence_level=persistence_level,
        mode=mode,
        construct=contract.construct
        or str(contract.cell_factors.get("construct", "fault_recovery_survival")),
        causal_grade=causal_grade,
        **projection_feature_fields(dimensions),
        task_success=task_success,
        total_tool_calls=total_tool_calls,
        model_call_count=model_call_count,
        prompt_tokens_per_step=prompt_tokens_per_step,
        prompt_cache_hit_rate=prompt_cache_hit_rate,
        injected_fault_record=injected_fault_record,
        injected_fault_count=injected_fault_count,
        fault_detected_count=fault_detected_count,
        post_fault_retries=post_fault_retries,
        blind_retries=blind_retries,
        certified_recovered_faults=certified_recovered_faults,
        step_to_first_fault=step_to_first_fault,
        step_to_recovery=step_to_recovery,
        diagnosis_class=diagnosis_class,
        source_error_count=source_error_count,
        propagated_error_count=propagated_error_count,
        strategy_changed_after_failure=strategy_changed_after_failure,
        controlled_replay_available=controlled_replay_available,
        controlled_replay_outcome_delta=controlled_replay_outcome_delta,
        max_blind_retry_streak=max_blind_retry_streak,
        recovery_succeeded_at_persistence=recovery_succeeded_at_persistence,
        schema_conformance_rate=schema_conformance_rate,
        autonomous_recovery_rate=autonomous_recovery_rate,
        fault_detection_rate=fault_detection_rate,
        blind_retry_rate=blind_retry_rate,
        fault_recovery_latency=fault_recovery_latency,
        citation=citation,
        verifier_truth_digest=contract.verifier_truth_digest,
    )
