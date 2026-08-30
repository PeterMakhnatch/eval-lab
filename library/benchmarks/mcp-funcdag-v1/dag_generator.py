"""Seeded deterministic typed DAG generator for MCP tool composition benchmarks."""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

MINIMUM_CALL_FLOOR = 5

# Certified structural difficulty axis. Higher levels are cumulative: each adds
# distractor/name-collision structure to the same unique correct DAG (the real
# tool path stays in topological_order / node_expected_calls) rather than more
# depth. All names are derived deterministically from the seed and real tool
# names so materialized tasks vary only by the declared difficulty factor.
DIFFICULTY_LEVELS = ("none", "aliases", "near_collision", "dead_end")
DEAD_END_BRANCH_COUNT = 2
DEAD_END_NODES_PER_BRANCH = 2


@dataclass(frozen=True)
class ToolParameter:
    name: str
    type_name: str
    description: str
    required: bool = True


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: list[ToolParameter]
    output_type: str
    is_distractor: bool = False
    op_kind: str = "compute"


@dataclass
class DAGNode:
    node_id: str
    tool_name: str
    op_name: str
    input_bindings: dict[str, str]  # param_name -> node_id or "input:<key>"
    output_type: str


@dataclass
class DAGSpec:
    seed: int
    depth: int
    width: int
    distractor_count: int
    name_similarity: str  # "low" or "high"
    schema_token_volume: str  # "concise" or "verbose"
    schema_drift: bool  # False = clean, True = drifted twin
    difficulty: str  # "none", "aliases", "near_collision", "dead_end"
    tools: list[ToolSpec]
    nodes: list[DAGNode]
    initial_inputs: dict[str, Any]
    target_node_id: str
    expected_target_value: Any
    topological_order: list[str]
    reference_node_values: dict[str, Any]
    node_expected_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    alias_tools: list[ToolSpec] = field(default_factory=list)
    near_collision_tools: list[ToolSpec] = field(default_factory=list)
    dead_end_tools: list[ToolSpec] = field(default_factory=list)
    dead_end_nodes: list[DAGNode] = field(default_factory=list)


OP_REGISTRY: dict[str, Callable[..., Any]] = {
    "add_integers": lambda x, y: int(x) + int(y),
    "multiply_integers": lambda x, y: int(x) * int(y),
    "subtract_integers": lambda x, y: int(x) - int(y),
    "scale_factor": lambda base, factor: int(base) * int(factor) + 3,
    "combine_metrics": lambda a, b: (int(a) * 2) + int(b) + 5,
    "transform_signal": lambda val, offset: (int(val) + int(offset)) * 2,
    "merge_checksums": lambda u, v: (int(u) ^ int(v)) + (int(u) & int(v)) + 7,
}


def _sha256_canonical(obj: Any) -> str:
    serialized = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _unique_tool_name(base: str, used: set[str]) -> str:
    """Return a deterministic name derived from `base` that does not collide with `used`."""
    candidate = base
    counter = 0
    while candidate in used:
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def _alias_variants(real_name: str) -> list[str]:
    """Deterministic high-similarity aliases for a real tool name.

    Each variant shares a long prefix with the real name so a selector that
    matches on name shape alone is drawn to a distractor. Variants never equal a
    real tool name (the real name is unique); collisions with other variants are
    resolved by the caller's used-name set.
    """
    variants = [real_name + "_", real_name + "x", real_name + "_alt"]
    if len(real_name) > 1:
        # Swap the final character to a visually adjacent shape (e.g. `1` -> `l`).
        variants.append(real_name[:-1] + "l")
    return variants


def _near_collision_variants(real_name: str) -> list[str]:
    """Deterministic near-collision dependency names (edit-distance one variants)."""
    variants = [real_name + "a", real_name + "b", real_name + "_dup"]
    if len(real_name) > 1:
        variants.append(real_name[:-1] + "z")
    return variants


def _alias_tool(real: ToolSpec, variant: str, idx: int) -> ToolSpec:
    """High-similarity alias: near-identical name, distinct non-marker description, distractor."""
    return ToolSpec(
        name=variant,
        description=(
            f"Compute stage {idx} variant: performs the secondary numeric transition "
            f"for graph stage {idx}. Independent of the primary dependency chain."
        ),
        parameters=real.parameters,
        output_type=real.output_type,
        is_distractor=True,
        op_kind="distractor",
    )


def _near_collision_tool(real: ToolSpec, variant: str, idx: int) -> ToolSpec:
    """Near-collision tool: one-edit-away dependency name, distractor with real-shaped args."""
    return ToolSpec(
        name=variant,
        description=(
            f"Compute stage {idx} auxiliary: evaluates the alternate numeric binding "
            f"for graph stage {idx}. Not part of the primary dependency chain."
        ),
        parameters=real.parameters,
        output_type=real.output_type,
        is_distractor=True,
        op_kind="distractor",
    )


def _make_dead_end_branch(
    rng: random.Random,
    branch_idx: int,
    initial_inputs: dict[str, Any],
    used_names: set[str],
) -> tuple[ToolSpec, list[DAGNode]]:
    """Create one deterministic dead-end branch: a real-shaped compute tool plus a
    short chain of nodes that never feeds the target node. The tool computes an
    integer so the branch is a plausible trap, but it is excluded from the real
    topological order / node_expected_calls, preserving the unique correct DAG."""
    input_ids = list(initial_inputs.keys())
    base = f"dead_end_metric_{branch_idx + 1}"
    tool_name = _unique_tool_name(base, used_names)
    used_names.add(tool_name)
    op_name = rng.choice(["combine_metrics", "scale_factor", "transform_signal", "merge_checksums"])
    params = [
        ToolParameter("a", "integer", "First side-branch metric input"),
        ToolParameter("b", "integer", "Second side-branch metric input"),
    ]
    tool = ToolSpec(
        name=tool_name,
        description=(
            f"Evaluates side-branch metric {branch_idx + 1}: derives a numeric "
            f"summary for an auxiliary graph branch."
        ),
        parameters=params,
        output_type="integer",
        is_distractor=False,
        op_kind=op_name,
    )
    nodes: list[DAGNode] = []
    prev = input_ids[0]
    for n in range(DEAD_END_NODES_PER_BRANCH):
        node_id = f"deadend_{branch_idx}_{n}"
        bindings = {"a": prev, "b": prev}
        nodes.append(DAGNode(node_id, tool_name, op_name, bindings, "integer"))
        prev = node_id
    return tool, nodes


def generate_dag_spec(
    seed: int = 42,
    depth: int = 3,
    width: int = 2,
    distractor_count: int = 2,
    name_similarity: str = "low",
    schema_token_volume: str = "concise",
    schema_drift: bool = False,
    difficulty: str = "none",
) -> DAGSpec:
    if difficulty not in DIFFICULTY_LEVELS:
        raise ValueError(
            f"Unknown difficulty level: {difficulty}; expected one of {DIFFICULTY_LEVELS}"
        )
    rng = random.Random(seed)

    # Initial inputs
    initial_inputs = {
        f"input_{i}": rng.randint(2, 10)
        for i in range(max(2, width))
    }

    op_keys = list(OP_REGISTRY.keys())
    tools: list[ToolSpec] = []
    nodes: list[DAGNode] = []
    node_values: dict[str, Any] = dict(initial_inputs)
    topological_order: list[str] = []
    node_expected_calls: dict[str, dict[str, Any]] = {}

    current_layer_nodes = [f"input_{i}" for i in range(max(2, width))]
    node_counter = 0

    for d in range(depth):
        next_layer_nodes = []
        layer_width = width if d < depth - 1 else 1
        for w in range(layer_width):
            node_id = f"node_{d}_{w}"
            op_name = op_keys[(node_counter + d + w) % len(op_keys)]
            # Catalog-visible tool names are role-neutral and opaque. Only descriptions expose operation semantics.
            tool_name = f"compute_unit_{node_counter + 1}"
            if schema_drift:
                tool_name = f"compute_unit_{node_counter + 1}_v2"

            available_inputs = current_layer_nodes
            if len(available_inputs) == 1:
                p1 = available_inputs[0]
                p2 = available_inputs[0]
            else:
                p1 = available_inputs[w % len(available_inputs)]
                p2 = available_inputs[(w + 1) % len(available_inputs)]

            v1 = node_values[p1]
            v2 = node_values[p2]
            op_fn = OP_REGISTRY[op_name]

            if op_name in ("add_integers", "multiply_integers", "subtract_integers"):
                params = [
                    ToolParameter("x", "integer", "First operand integer value"),
                    ToolParameter("y", "integer", "Second operand integer value"),
                ]
                bindings = {"x": p1, "y": p2}
                val = op_fn(v1, v2)
                expected_args = {"x": v1, "y": v2}
            elif op_name == "scale_factor":
                params = [
                    ToolParameter("base", "integer", "Base integer to scale"),
                    ToolParameter("factor", "integer", "Scaling multiplier"),
                ]
                bindings = {"base": p1, "factor": p2}
                val = op_fn(v1, v2)
                expected_args = {"base": v1, "factor": v2}
            elif op_name == "combine_metrics":
                params = [
                    ToolParameter("a", "integer", "First primary metric"),
                    ToolParameter("b", "integer", "Second secondary metric"),
                ]
                bindings = {"a": p1, "b": p2}
                val = op_fn(v1, v2)
                expected_args = {"a": v1, "b": v2}
            elif op_name == "transform_signal":
                params = [
                    ToolParameter("val", "integer", "Signal value"),
                    ToolParameter("offset", "integer", "Signal offset"),
                ]
                bindings = {"val": p1, "offset": p2}
                val = op_fn(v1, v2)
                expected_args = {"val": v1, "offset": v2}
            else:
                params = [
                    ToolParameter("u", "integer", "Upper component"),
                    ToolParameter("v", "integer", "Lower component"),
                ]
                bindings = {"u": p1, "v": p2}
                val = op_fn(v1, v2)
                expected_args = {"u": v1, "v": v2}

            public_operation_desc = {
                "add_integers": "Addition transformation: adds x + y.",
                "multiply_integers": "Multiplication transformation: multiplies x * y.",
                "subtract_integers": "Subtraction transformation: subtracts x - y.",
                "scale_factor": "Affine scaling transformation: base * factor + 3.",
                "combine_metrics": "Linear metric combination: (a * 2) + b + 5.",
                "transform_signal": "Signal transformation: (val + offset) * 2.",
                "merge_checksums": "Checksum merge: (u ^ v) + (u & v) + 7.",
            }
            desc = public_operation_desc[op_name]
            if schema_token_volume == "verbose":
                desc += " This tool strictly requires integer inputs and computes deterministic algebraic transitions across the dependency graph. Ensure all prerequisite dependencies are resolved prior to execution."

            tool_spec = ToolSpec(
                name=tool_name,
                description=desc,
                parameters=params,
                output_type="integer",
                is_distractor=False,
                op_kind=op_name,
            )
            if not any(t.name == tool_name for t in tools):
                tools.append(tool_spec)

            node = DAGNode(
                node_id=node_id,
                tool_name=tool_name,
                op_name=op_name,
                input_bindings=bindings,
                output_type="integer",
            )
            nodes.append(node)
            node_values[node_id] = val
            topological_order.append(node_id)
            next_layer_nodes.append(node_id)
            node_expected_calls[node_id] = {
                "tool_name": tool_name,
                "expected_args": expected_args,
                "expected_result": val,
            }
            node_counter += 1

        current_layer_nodes = next_layer_nodes

    target_node_id = topological_order[-1]
    expected_target_value = node_values[target_node_id]

    if len(nodes) < MINIMUM_CALL_FLOOR:
        raise ValueError(
            f"Generated DAG node count {len(nodes)} is below mandatory floor {MINIMUM_CALL_FLOOR}"
        )

    # Distractor tools with role-neutral names and descriptions
    for i in range(distractor_count):
        if name_similarity == "high":
            dist_tool_name = f"compute_context_{i+1}" if not schema_drift else f"compute_context_{i+1}_v2"
            dist_desc = f"Computes algebraic context for parallel graph branch {i+1}."
        else:
            dist_tool_name = f"compute_context_{i+1}" if not schema_drift else f"compute_context_{i+1}_v2"
            dist_desc = f"Evaluates contextual metadata for pipeline stage {i+1}."

        if schema_token_volume == "verbose":
            dist_desc += " Processes pipeline telemetry and evaluates contextual metadata across stage transitions."

        dist_spec = ToolSpec(
            name=dist_tool_name,
            description=dist_desc,
            parameters=[
                ToolParameter("input_payload", "string", "Context payload string", required=False),
                ToolParameter("flag", "boolean", "Execution modifier flag", required=False),
            ],
            output_type="string",
            is_distractor=True,
            op_kind="distractor",
        )
        tools.append(dist_spec)

    # Certified structural difficulty axis. The real DAG (tools/nodes above) is
    # unchanged; every added element is a distractor or a dead-end branch that is
    # excluded from the required call sequence, so exactly one correct DAG remains.
    used_names = {t.name for t in tools}
    alias_tools: list[ToolSpec] = []
    near_collision_tools: list[ToolSpec] = []
    dead_end_tools: list[ToolSpec] = []
    dead_end_nodes: list[DAGNode] = []

    real_tools = [t for t in tools if not t.is_distractor]

    if difficulty in ("aliases", "near_collision", "dead_end"):
        idx = 0
        for real in real_tools:
            for variant in _alias_variants(real.name):
                name = _unique_tool_name(variant, used_names)
                used_names.add(name)
                alias = _alias_tool(real, name, idx)
                alias_tools.append(alias)
                tools.append(alias)
                idx += 1

    if difficulty in ("near_collision", "dead_end"):
        idx = 0
        for real in real_tools:
            for variant in _near_collision_variants(real.name):
                name = _unique_tool_name(variant, used_names)
                used_names.add(name)
                nc = _near_collision_tool(real, name, idx)
                near_collision_tools.append(nc)
                tools.append(nc)
                idx += 1

    if difficulty == "dead_end":
        for k in range(DEAD_END_BRANCH_COUNT):
            tool, branch_nodes = _make_dead_end_branch(rng, k, initial_inputs, used_names)
            dead_end_tools.append(tool)
            tools.append(tool)
            dead_end_nodes.extend(branch_nodes)

    return DAGSpec(
        seed=seed,
        depth=depth,
        width=width,
        distractor_count=distractor_count,
        name_similarity=name_similarity,
        schema_token_volume=schema_token_volume,
        schema_drift=schema_drift,
        difficulty=difficulty,
        tools=tools,
        nodes=nodes,
        initial_inputs=initial_inputs,
        target_node_id=target_node_id,
        expected_target_value=expected_target_value,
        topological_order=topological_order,
        reference_node_values=node_values,
        node_expected_calls=node_expected_calls,
        alias_tools=alias_tools,
        near_collision_tools=near_collision_tools,
        dead_end_tools=dead_end_tools,
        dead_end_nodes=dead_end_nodes,
    )
