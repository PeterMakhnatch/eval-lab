"""Isolated feature producer for Context & Actionable Memory (action-memory-v1).

Computes:
- L1 Facts (C0, C1, Grade A): task_success, total_tool_calls, raw_binding_opportunities,
  raw_conflicting_opportunities, binding_matched, stale_value_bound, prompt metrics.
- L2 Derived Metrics (C0, C1): schema_conformance_rate, binding_survival_rate,
  stale_value_override_rate, context_burn_velocity, occupancy_first_failure.
- Strict NULL preservation: missing or zero opportunity denominators yield NULL (None), never 0.0.
"""

from __future__ import annotations

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
    projection_feature_fields,
)


@dataclass(frozen=True)
class ActionMemoryFeatures:
    """Feature record for action-memory-v1 trial."""

    # Identity
    trial_id: str
    family: str
    task_id: str
    seed: int
    cell_id: str
    arm: str
    dose_bytes: int
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
    task_success: bool
    total_tool_calls: int
    model_call_count: int
    prompt_tokens_per_step: float | None
    prompt_cache_hit_rate: float | None
    raw_binding_opportunities: int
    raw_conflicting_opportunities: int
    bound_target_entity: str | None
    bound_target_attribute: str | None
    bound_target_value: str | None
    binding_matched: bool
    stale_value_bound: bool

    # L2 Derived Metrics (C0, C1) - NULL-preserving on zero denominator
    schema_conformance_rate: float | None
    binding_survival_rate: float | None
    stale_value_override_rate: float | None
    context_burn_velocity: float | None
    occupancy_first_failure: float | None

    # Provenance / citations
    citation: str
    verifier_truth_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _compute_cbv_slope(step_tokens: Sequence[int] | None) -> float | None:
    """Compute OLS regression slope of prompt tokens across step indices.

    Returns None if fewer than 2 valid points exist.
    """
    if not step_tokens or len(step_tokens) < 2:
        return None

    n = len(step_tokens)
    sum_x = sum(range(n))
    sum_y = sum(step_tokens)
    sum_xy = sum(i * y for i, y in enumerate(step_tokens))
    sum_x2 = sum(i * i for i in range(n))

    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return None

    slope = (n * sum_xy - sum_x * sum_y) / denom
    return float(slope)


def extract_action_memory_features(
    bundle: TrialBundle,
    step_tokens: Sequence[int] | None = None,
    cache_hits: Sequence[bool] | None = None,
    dimensions: BenchmarkProjectionDimensions | None = None,
) -> ActionMemoryFeatures:
    """Extract deterministic mechanical facts and L2 metrics from an action-memory trial bundle."""
    contract = bundle.contract
    final_state = bundle.final_state
    events = bundle.events
    calls = bundle.correlated_calls
    dimensions = dimensions or build_projection_dimensions(bundle, None)
    cell_factors = contract.cell_factors
    opp_counts = contract.opportunity_counts

    cell_id = str(cell_factors.get("cell_id", "default-cell"))
    arm = str(cell_factors.get("arm", "clean"))
    dose_bytes = int(cell_factors.get("dose_bytes", 0))

    # L1 ground truth / identity
    task_success = final_state.invariants_passed
    total_tool_calls = len(calls)
    model_call_count = (
        len([e for e in events if e.event_type in ("mcp_call", "tool_call", "model_call")]) or 1
    )

    # Extract target entity, attribute, latest value, and initial value from contract/factors
    target_entity = cell_factors.get("target_entity")
    target_attribute = cell_factors.get("target_attribute")
    latest_value = cell_factors.get("latest_value")
    initial_value = cell_factors.get("initial_value")
    inversion_steps = cell_factors.get("inversion_steps", [])
    if "mutation_opportunity_count" in opp_counts:
        raw_binding_opps = int(opp_counts["mutation_opportunity_count"])
    elif "raw_binding_opportunities" in opp_counts:
        raw_binding_opps = int(opp_counts["raw_binding_opportunities"])
    elif "mutation_opportunity_count" in cell_factors:
        raw_binding_opps = int(cell_factors["mutation_opportunity_count"])
    elif "target_entity" in cell_factors or final_state.mutations:
        raw_binding_opps = 1
    else:
        raw_binding_opps = 0

    if "update_opportunity_count" in opp_counts:
        raw_conflicting_opps = int(opp_counts["update_opportunity_count"])
    elif "raw_conflicting_opportunities" in opp_counts:
        raw_conflicting_opps = int(opp_counts["raw_conflicting_opportunities"])
    elif "update_opportunity_count" in cell_factors:
        raw_conflicting_opps = int(cell_factors["update_opportunity_count"])
    elif inversion_steps:
        raw_conflicting_opps = len(inversion_steps)
    else:
        raw_conflicting_opps = 0

    # Inspect tool calls for mutation calls
    bound_entity: str | None = str(target_entity) if target_entity is not None else None
    bound_attribute: str | None = str(target_attribute) if target_attribute is not None else None
    bound_value: str | None = None
    binding_matched = False
    stale_value_bound = False
    valid_schema_calls = 0

    mutation_calls: list[CorrelatedToolCall] = []

    for call in calls:
        args = call.arguments
        is_schema_valid = True
        if not isinstance(args, dict):
            is_schema_valid = False
        else:
            # check tool call args
            if call.tool_name in (
                "mutate_record",
                "bind_fact",
                "write_memory",
                "update_entity",
                "set_value",
            ):
                mutation_calls.append(call)
                if "entity_id" in args:
                    bound_entity = str(args["entity_id"])
                if "attribute" in args:
                    bound_attribute = str(args["attribute"])
                if "bound_value" in args:
                    bound_value = str(args["bound_value"])
                elif "value" in args:
                    bound_value = str(args["value"])

        if is_schema_valid and not call.is_error:
            valid_schema_calls += 1

    # Also inspect final state mutations if no tool calls were explicitly parsed
    if not mutation_calls and final_state.mutations:
        for mut in final_state.mutations:
            if isinstance(mut, dict):
                bound_entity = str(mut.get("entity_id", bound_entity))
                bound_attribute = str(mut.get("attribute", bound_attribute))
                bound_value = str(mut.get("bound_value", mut.get("value", bound_value)))

    if bound_value is not None and latest_value is not None:
        if str(bound_value).strip() == str(latest_value).strip():
            binding_matched = True
        elif (
            initial_value is not None
            and str(bound_value).strip() == str(initial_value).strip()
            or any(str(bound_value).strip() == str(inv).strip() for inv in inversion_steps)
        ):
            stale_value_bound = True

    for ev in events:
        if ev.event_type in ("state_binding_matched", "binding_matched"):
            if ev.payload.get("is_matched", False) or ev.payload.get("matched", False):
                binding_matched = True
        elif ev.event_type in ("stale_value_bound", "binding_stale"):
            stale_value_bound = True

    if not binding_matched and not stale_value_bound and task_success:
        binding_matched = True
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

    # 2. binding_survival_rate: denom is raw_binding_opportunities
    binding_survival_rate: float | None = None
    if raw_binding_opps > 0:
        binding_survival_rate = 1.0 if binding_matched else 0.0

    # 3. stale_value_override_rate: denom is raw_conflicting_opportunities
    stale_value_override_rate: float | None = None
    if raw_conflicting_opps > 0:
        stale_value_override_rate = 1.0 if (not stale_value_bound and binding_matched) else 0.0
    # 4. context_burn_velocity: prompt token slope
    context_burn_velocity: float | None = _compute_cbv_slope(step_tokens)

    # 5. occupancy_first_failure: token occupancy at first binding failure
    occupancy_first_failure: float | None = None
    if not binding_matched and step_tokens and len(step_tokens) > 0:
        first_fail_tokens = step_tokens[-1]
        occupancy_first_failure = float(first_fail_tokens / max(dose_bytes, 1))

    citation = bundle.build_citation()

    return ActionMemoryFeatures(
        trial_id=bundle.trial_id,
        family=contract.family,
        task_id=contract.task_id,
        seed=contract.seed,
        cell_id=cell_id,
        arm=arm,
        dose_bytes=dose_bytes,
        construct=contract.construct
        or str(contract.cell_factors.get("construct", "state_binding_survival")),
        causal_grade=str(contract.cell_factors.get("causal_grade", "L2_derived")),
        **projection_feature_fields(dimensions),
        task_success=task_success,
        total_tool_calls=total_tool_calls,
        model_call_count=model_call_count,
        prompt_tokens_per_step=prompt_tokens_per_step,
        prompt_cache_hit_rate=prompt_cache_hit_rate,
        raw_binding_opportunities=raw_binding_opps,
        raw_conflicting_opportunities=raw_conflicting_opps,
        bound_target_entity=bound_entity,
        bound_target_attribute=bound_attribute,
        bound_target_value=bound_value,
        binding_matched=binding_matched,
        stale_value_bound=stale_value_bound,
        schema_conformance_rate=schema_conformance_rate,
        binding_survival_rate=binding_survival_rate,
        stale_value_override_rate=stale_value_override_rate,
        context_burn_velocity=context_burn_velocity,
        occupancy_first_failure=occupancy_first_failure,
        citation=citation,
        verifier_truth_digest=contract.verifier_truth_digest,
    )
