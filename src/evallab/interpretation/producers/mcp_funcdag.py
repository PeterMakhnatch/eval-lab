"""Isolated feature producer for Tool Selection, Composition, & Value Propagation (mcp-funcdag-v1).

Computes:
- L1 Facts (C0, C1, Grade A): task_success, total_tool_calls, required_dag_edges,
  required_value_bindings, executed_dag_edges, correct_value_bindings, redundant_tool_calls,
  satisfied_edge_opportunities, first_edge_step, prompt metrics.
- L2 Derived Metrics (C0, C1): schema_conformance_rate, value_propagation_accuracy,
  dag_edge_conformance_rate, redundant_call_ratio, first_edge_latency.
- Strict NULL preservation: missing or zero opportunity denominators yield NULL (None), never 0.0.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from evallab.interpretation.benchmark_events import (
    TrialBundle,
)
from evallab.interpretation.benchmark_projection import (
    BenchmarkProjectionDimensions,
    build_projection_dimensions,
    projection_feature_fields,
)
from evallab.interpretation.feature_registry import compute_prompt_cache_hit_rate


@dataclass(frozen=True)
class McpFuncDagFeatures:
    """Feature record for mcp-funcdag-v1 trial."""

    # Identity
    trial_id: str
    family: str
    task_id: str
    seed: int
    depth: int
    width: int
    distractor_count: int
    name_similarity: str
    schema_drift: bool
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
    # L1 Facts (C0, C1, Grade A)
    task_success: bool
    total_tool_calls: int
    model_call_count: int
    prompt_tokens_per_step: float | None
    prompt_cache_hit_rate: float | None
    required_dag_edges: int
    required_value_bindings: int
    executed_dag_edges: int
    correct_value_bindings: int
    redundant_tool_calls: int
    cycle_violations: int
    satisfied_edge_opportunities: int
    first_edge_step: int | None
    # L2 Derived Metrics (C0, C1) - NULL-preserving on zero denominator
    schema_conformance_rate: float | None
    value_propagation_accuracy: float | None
    dag_edge_conformance_rate: float | None
    redundant_call_ratio: float | None
    first_edge_latency: float | None

    # Provenance / citations
    citation: str
    verifier_truth_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_mcp_funcdag_features(
    bundle: TrialBundle,
    step_tokens: Sequence[int] | None = None,
    dimensions: BenchmarkProjectionDimensions | None = None,
    cached_step_tokens: Sequence[int] | None = None,
) -> McpFuncDagFeatures:
    """Extract deterministic mechanical facts and L2 metrics from an mcp-funcdag trial bundle."""
    contract = bundle.contract
    final_state = bundle.final_state
    events = bundle.events
    calls = bundle.correlated_calls
    dimensions = dimensions or build_projection_dimensions(bundle, None)
    cell_factors = contract.cell_factors
    opp_counts = contract.opportunity_counts

    depth = int(cell_factors.get("depth", 3))
    width = int(cell_factors.get("width", 2))
    distractor_count = int(cell_factors.get("distractor_count", 2))
    name_similarity = str(cell_factors.get("name_similarity", "low"))
    schema_drift = bool(cell_factors.get("schema_drift", False))

    task_success = final_state.invariants_passed
    total_tool_calls = len(calls)
    model_call_count = (
        len([e for e in events if e.event_type in ("mcp_call", "tool_call", "model_call")]) or 1
    )

    declared_edges = (
        cell_factors.get("declared_dag_edges")
        or opp_counts.get("required_edge_count")
        or opp_counts.get("required_dag_edges")
    )
    if isinstance(declared_edges, list):
        required_dag_edges = len(declared_edges)
    elif declared_edges is not None:
        required_dag_edges = int(declared_edges)
    else:
        required_dag_edges = depth * (width - 1) if depth > 1 else 1

    required_value_bindings = int(opp_counts.get("required_node_count", depth * width))
    # Track tool calls and node executions
    seen_call_signatures: set[str] = set()
    executed_nodes: set[str] = set()
    executed_dag_edges = 0
    correct_value_bindings = 0
    redundant_tool_calls = 0
    cycle_violations = 0
    valid_schema_calls = 0
    first_edge_step: int | None = None
    dep_graph: dict[str, set[str]] = {}
    # Track intermediate outputs from results
    node_outputs: dict[str, Any] = {}

    for idx, call in enumerate(calls, start=1):
        tool_name = call.tool_name
        args = call.arguments
        is_valid_schema = isinstance(args, dict) and not call.is_error

        if is_valid_schema:
            valid_schema_calls += 1

        # Redundant call detection: repeating exact (tool_name, args_digest)
        if isinstance(args, dict):
            try:
                args_str = json.dumps(args, sort_keys=True, default=str)
            except Exception:
                args_str = str(sorted(((str(k), str(v)) for k, v in args.items())))
        else:
            args_str = str(args)
        sig = f"{tool_name}:{args_str}"
        if sig in seen_call_signatures:
            redundant_tool_calls += 1
        else:
            seen_call_signatures.add(sig)
        # Distractor calls count as redundant if not part of expected DAG
        if (
            "distractor" in tool_name
            or tool_name.startswith("dummy_")
            or tool_name.startswith("noop_")
        ):
            redundant_tool_calls += 1

        # Check edge propagation: arguments referencing previous outputs
        if isinstance(args, dict):
            has_edge_ref = False
            for v in args.values():
                for out_node, out_val in node_outputs.items():
                    if v == out_val or (out_val is not None and str(v) == str(out_val)):
                        has_edge_ref = True
                        dep_graph.setdefault(out_node, set()).add(tool_name)
                        # BFS cycle check
                        visited = set()
                        q = [tool_name]
                        while q:
                            curr = q.pop(0)
                            if curr == out_node:
                                cycle_violations += 1
                                break
                            if curr not in visited:
                                visited.add(curr)
                                q.extend(dep_graph.get(curr, set()))
                        break
                    if isinstance(v, str) and (
                        out_node in v or "node_" in v or "step_" in v or "res_" in v
                    ):
                        has_edge_ref = True
                        break
                if has_edge_ref:
                    break
            if has_edge_ref:
                executed_dag_edges += 1
                if first_edge_step is None:
                    first_edge_step = idx
        # Record output if call succeeded
        if call.result_payload and not call.is_error:
            node_outputs[tool_name] = call.result_payload
            executed_nodes.add(tool_name)
    # Invariants and state verification
    if final_state.invariants_passed:
        correct_value_bindings = required_value_bindings
        if executed_dag_edges < required_dag_edges:
            executed_dag_edges = required_dag_edges
    else:
        correct_value_bindings = min(len(executed_nodes), required_value_bindings)

    if "cycle_violations" in final_state.details:
        cv_val = final_state.details["cycle_violations"]
        cycle_violations = max(
            cycle_violations, len(cv_val) if isinstance(cv_val, list) else int(cv_val)
        )

    satisfied_edge_opps = min(executed_dag_edges, required_dag_edges)
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

    # 2. value_propagation_accuracy: denom is required_value_bindings
    value_propagation_accuracy: float | None = None
    if required_value_bindings > 0:
        value_propagation_accuracy = float(correct_value_bindings / required_value_bindings)

    # 3. dag_edge_conformance_rate: denom is required_dag_edges
    dag_edge_conformance_rate: float | None = None
    if required_dag_edges > 0:
        dag_edge_conformance_rate = float(
            min(executed_dag_edges, required_dag_edges) / required_dag_edges
        )

    # 4. redundant_call_ratio: denom is total_tool_calls
    redundant_call_ratio: float | None = None
    if total_tool_calls > 0:
        redundant_call_ratio = float(redundant_tool_calls / total_tool_calls)

    # 5. first_edge_latency: denom is satisfied_edge_opportunities
    first_edge_latency: float | None = None
    if satisfied_edge_opps > 0 and first_edge_step is not None:
        first_edge_latency = float(first_edge_step)

    citation = bundle.build_citation()

    return McpFuncDagFeatures(
        trial_id=bundle.trial_id,
        family=contract.family,
        task_id=contract.task_id,
        seed=contract.seed,
        depth=depth,
        width=width,
        distractor_count=distractor_count,
        name_similarity=name_similarity,
        schema_drift=schema_drift,
        construct=contract.construct
        or str(contract.cell_factors.get("construct", "tool_call_dag_conformance")),
        causal_grade=str(contract.cell_factors.get("causal_grade", "L2_derived")),
        **projection_feature_fields(dimensions),
        task_success=task_success,
        total_tool_calls=total_tool_calls,
        model_call_count=model_call_count,
        prompt_tokens_per_step=prompt_tokens_per_step,
        prompt_cache_hit_rate=prompt_cache_hit_rate,
        required_dag_edges=required_dag_edges,
        required_value_bindings=required_value_bindings,
        executed_dag_edges=executed_dag_edges,
        correct_value_bindings=correct_value_bindings,
        redundant_tool_calls=redundant_tool_calls,
        cycle_violations=cycle_violations,
        satisfied_edge_opportunities=satisfied_edge_opps,
        first_edge_step=first_edge_step,
        schema_conformance_rate=schema_conformance_rate,
        value_propagation_accuracy=value_propagation_accuracy,
        dag_edge_conformance_rate=dag_edge_conformance_rate,
        redundant_call_ratio=redundant_call_ratio,
        first_edge_latency=first_edge_latency,
        citation=citation,
        verifier_truth_digest=contract.verifier_truth_digest,
    )
