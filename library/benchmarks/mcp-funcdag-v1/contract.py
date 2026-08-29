"""Benchmark contract and cell factor definitions for mcp-funcdag-v1."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

FAMILY = "mcp-funcdag-v1"
VERSION = "1.0.0"
CONSTRUCT = "MCP tool selection, composition, and value propagation over dependency DAGs"

CALIBRATION_SEEDS = [42, 101, 2024]


@dataclass(frozen=True)
class CellFactors:
    depth: int = 3
    width: int = 2
    distractor_count: int = 2
    name_similarity: str = "low"  # low, high
    schema_token_volume: str = "concise"  # concise, verbose
    schema_drift: bool = False
    seed: int = 42


@dataclass(frozen=True)
class OpportunityCounts:
    required_node_count: int
    required_edge_count: int
    tool_opportunity_count: int
    distractor_count: int
    total_tools_exposed: int


@dataclass(frozen=True)
class BenchmarkContract:
    family: str
    version: str
    construct: str
    seed: int
    cell_factors: dict[str, Any]
    task_id: str
    opportunity_counts: dict[str, Any]
    verifier_truth_digest: str
    artifact_paths: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


# Generate Campaign 0 Calibration Grid with 3 deterministic seeds per factor condition
# Minimum floor of >=5 required calls per cell
CAMPAIGN_0_CELLS: list[dict[str, Any]] = []

FACTOR_VARIATIONS = [
    # Baseline (depth=3, width=2 -> 5 nodes)
    {"name": "baseline", "depth": 3, "width": 2, "distractor_count": 2, "name_similarity": "low", "schema_token_volume": "concise", "schema_drift": False},
    # Depth ladder (depth_3 and depth_4, >= 5 nodes)
    {"name": "depth_3", "depth": 3, "width": 2, "distractor_count": 2, "name_similarity": "low", "schema_token_volume": "concise", "schema_drift": False},
    {"name": "depth_4", "depth": 4, "width": 2, "distractor_count": 2, "name_similarity": "low", "schema_token_volume": "concise", "schema_drift": False},
    # Width ladder
    {"name": "width_3", "depth": 3, "width": 3, "distractor_count": 2, "name_similarity": "low", "schema_token_volume": "concise", "schema_drift": False},
    {"name": "width_4", "depth": 3, "width": 4, "distractor_count": 2, "name_similarity": "low", "schema_token_volume": "concise", "schema_drift": False},
    # Distractor ladder
    {"name": "distractors_0", "depth": 3, "width": 2, "distractor_count": 0, "name_similarity": "low", "schema_token_volume": "concise", "schema_drift": False},
    {"name": "distractors_5", "depth": 3, "width": 2, "distractor_count": 5, "name_similarity": "low", "schema_token_volume": "concise", "schema_drift": False},
    # Name similarity
    {"name": "name_similarity_high", "depth": 3, "width": 2, "distractor_count": 2, "name_similarity": "high", "schema_token_volume": "concise", "schema_drift": False},
    # Schema token volume
    {"name": "schema_tokens_verbose", "depth": 3, "width": 2, "distractor_count": 2, "name_similarity": "low", "schema_token_volume": "verbose", "schema_drift": False},
    # Schema drift clean twin
    {"name": "schema_drift_twin", "depth": 3, "width": 2, "distractor_count": 2, "name_similarity": "low", "schema_token_volume": "concise", "schema_drift": True},
]

for var in FACTOR_VARIATIONS:
    for s in CALIBRATION_SEEDS:
        cell_def = dict(var)
        cell_def["seed"] = s
        CAMPAIGN_0_CELLS.append(cell_def)


def make_benchmark_contract(
    factors: CellFactors,
    dag_spec: Any,
    task_id: str,
    artifact_paths: dict[str, str] | None = None,
) -> BenchmarkContract:
    # Compute verifier truth digest
    truth_payload = {
        "target_node_id": dag_spec.target_node_id,
        "expected_target_value": dag_spec.expected_target_value,
        "topological_order": dag_spec.topological_order,
        "reference_node_values": dag_spec.reference_node_values,
        "node_expected_calls": getattr(dag_spec, "node_expected_calls", {}),
    }
    truth_digest = hashlib.sha256(
        json.dumps(truth_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    req_nodes = len(dag_spec.nodes)
    req_edges = sum(len(n.input_bindings) for n in dag_spec.nodes)
    total_tools = len(dag_spec.tools)

    opps = OpportunityCounts(
        required_node_count=req_nodes,
        required_edge_count=req_edges,
        tool_opportunity_count=req_nodes,
        distractor_count=factors.distractor_count,
        total_tools_exposed=total_tools,
    )

    artifacts = artifact_paths or {
        "events": "/app/output/benchmark-events.jsonl",
        "result": "/app/result.json",
    }

    return BenchmarkContract(
        family=FAMILY,
        version=VERSION,
        construct=CONSTRUCT,
        seed=factors.seed,
        cell_factors=asdict(factors),
        task_id=task_id,
        opportunity_counts=asdict(opps),
        verifier_truth_digest=truth_digest,
        artifact_paths=artifacts,
    )
