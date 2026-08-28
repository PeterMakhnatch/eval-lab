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


def extract_mcp_recovery_features(
    bundle: TrialBundle,
    step_tokens: Sequence[int] | None = None,
    cache_hits: Sequence[bool] | None = None,
) -> McpRecoveryFeatures:
    """Extract deterministic mechanical facts and L2 metrics from an mcp-recovery trial bundle."""
    contract = bundle.contract
    final_state = bundle.final_state
    events = bundle.events
    calls = bundle.correlated_calls

    cell_factors = contract.cell_factors
    fault_classes = cell_factors.get("fault_classes", [])
    persistence_levels = cell_factors.get("persistence_levels", [1])
    fault_class = str(fault_classes[0]) if fault_classes else None
    persistence_level = int(persistence_levels[0]) if persistence_levels else 1
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

    last_failing_call: CorrelatedToolCall | None = None
    fault_active = False

    for idx, call in enumerate(calls, start=1):
        args = call.arguments
        is_schema_valid = isinstance(args, dict) and not call.is_error
        if is_schema_valid:
            valid_schema_calls += 1

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
                else:
                    # Agent adapted arguments or switched tools -> detected/acknowledged fault
                    fault_detected_count += 1

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

    # Certified autonomous recovery requires:
    # 1. Injected fault occurred
    # 2. Zero human intervention
    # 3. Final state invariants passed
    # 4. Recovery step occurred strictly after fault step (step_to_recovery > step_to_first_fault)
    human_interventions = int(final_state.details.get("human_interventions_count", 0))
    has_valid_step_recovery = (
        step_to_recovery is not None
        and step_to_first_fault is not None
        and step_to_recovery > step_to_first_fault
    )
    is_c3_certified = (
        injected_fault_count > 0
        and task_success
        and human_interventions == 0
        and (has_valid_step_recovery or certified_recovered_faults > 0)
    )
    if is_c3_certified:
        certified_recovered_faults = max(certified_recovered_faults, 1)
    causal_grade = "C3" if is_c3_certified else ("C0" if injected_fault_count == 0 else "C1")
    # Prompt token metrics
    prompt_tokens_per_step: float | None = None
    if step_tokens:
        prompt_tokens_per_step = float(sum(step_tokens) / len(step_tokens))

    prompt_cache_hit_rate: float | None = None
    if cache_hits and len(cache_hits) > 0:
        prompt_cache_hit_rate = float(sum(1 for h in cache_hits if h) / len(cache_hits))

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
        schema_conformance_rate=schema_conformance_rate,
        autonomous_recovery_rate=autonomous_recovery_rate,
        fault_detection_rate=fault_detection_rate,
        blind_retry_rate=blind_retry_rate,
        fault_recovery_latency=fault_recovery_latency,
        citation=citation,
        verifier_truth_digest=contract.verifier_truth_digest,
    )
