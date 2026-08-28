"""Seeded deterministic typed DAG generator for MCP tool composition benchmarks."""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


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
    tools: list[ToolSpec]
    nodes: list[DAGNode]
    initial_inputs: dict[str, Any]
    target_node_id: str
    expected_target_value: Any
    topological_order: list[str]
    reference_node_values: dict[str, Any]


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


def generate_dag_spec(
    seed: int = 42,
    depth: int = 3,
    width: int = 2,
    distractor_count: int = 2,
    name_similarity: str = "low",
    schema_token_volume: str = "concise",
    schema_drift: bool = False,
) -> DAGSpec:
    rng = random.Random(seed)
    
    # Generate initial input values
    initial_inputs = {
        f"input_{i}": rng.randint(2, 10)
        for i in range(max(2, width))
    }
    
    # Available operation kinds
    op_keys = list(OP_REGISTRY.keys())
    
    tools: list[ToolSpec] = []
    nodes: list[DAGNode] = []
    node_values: dict[str, Any] = dict(initial_inputs)
    topological_order: list[str] = []
    
    # Generate layers of DAG
    current_layer_nodes = [f"input_{i}" for i in range(max(2, width))]
    node_counter = 0
    
    for d in range(depth):
        next_layer_nodes = []
        layer_width = width if d < depth - 1 else 1  # converge to 1 target at final layer
        for w in range(layer_width):
            node_id = f"node_{d}_{w}"
            op_name = op_keys[(node_counter + d + w) % len(op_keys)]
            tool_name = f"dag_tool_{op_name}"
            if schema_drift:
                tool_name = f"dag_tool_{op_name}_v2"
                
            # Pick 2 inputs from previous layers
            available_inputs = current_layer_nodes
            if len(available_inputs) == 1:
                p1 = available_inputs[0]
                p2 = available_inputs[0]
            else:
                p1 = available_inputs[w % len(available_inputs)]
                p2 = available_inputs[(w + 1) % len(available_inputs)]
                
            # Evaluate operation deterministically
            v1 = node_values[p1]
            v2 = node_values[p2]
            op_fn = OP_REGISTRY[op_name]
            
            # Param names
            if op_name in ("add_integers", "multiply_integers", "subtract_integers"):
                params = [
                    ToolParameter("x", "integer", "First operand integer value"),
                    ToolParameter("y", "integer", "Second operand integer value"),
                ]
                bindings = {"x": p1, "y": p2}
                val = op_fn(v1, v2)
            elif op_name == "scale_factor":
                params = [
                    ToolParameter("base", "integer", "Base integer to scale"),
                    ToolParameter("factor", "integer", "Scaling multiplier"),
                ]
                bindings = {"base": p1, "factor": p2}
                val = op_fn(v1, v2)
            elif op_name == "combine_metrics":
                params = [
                    ToolParameter("a", "integer", "First primary metric"),
                    ToolParameter("b", "integer", "Second secondary metric"),
                ]
                bindings = {"a": p1, "b": p2}
                val = op_fn(v1, v2)
            elif op_name == "transform_signal":
                params = [
                    ToolParameter("val", "integer", "Signal value"),
                    ToolParameter("offset", "integer", "Signal offset"),
                ]
                bindings = {"val": p1, "offset": p2}
                val = op_fn(v1, v2)
            else:
                params = [
                    ToolParameter("u", "integer", "Upper component"),
                    ToolParameter("v", "integer", "Lower component"),
                ]
                bindings = {"u": p1, "v": p2}
                val = op_fn(v1, v2)
                
            desc = f"Executes {op_name} computation."
            if schema_token_volume == "verbose":
                desc += " This tool strictly requires integer inputs and computes deterministic algebraic transitions across the dependency graph. Ensure all prerequisite node dependencies are resolved prior to execution."
                
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
            node_counter += 1
            
        current_layer_nodes = next_layer_nodes
        
    target_node_id = topological_order[-1]
    expected_target_value = node_values[target_node_id]
    
    # Generate distractors
    for i in range(distractor_count):
        if name_similarity == "high":
            dist_tool_name = f"dag_tool_add_integers_aux_{i+1}" if not schema_drift else f"dag_tool_add_integers_aux_{i+1}_v2"
            dist_desc = f"Auxiliary computation for node branch {i+1}."
        else:
            dist_tool_name = f"noise_unrelated_logger_{i+1}"
            dist_desc = f"Unrelated auxiliary logging utility {i+1}."
            
        if schema_token_volume == "verbose":
            dist_desc += " Distractor utility service. Unused for target root computation."
            
        dist_spec = ToolSpec(
            name=dist_tool_name,
            description=dist_desc,
            parameters=[
                ToolParameter("input_payload", "string", "Unused input payload string", required=False),
                ToolParameter("flag", "boolean", "Optional execution flag", required=False),
            ],
            output_type="string",
            is_distractor=True,
            op_kind="distractor",
        )
        tools.append(dist_spec)
        
    return DAGSpec(
        seed=seed,
        depth=depth,
        width=width,
        distractor_count=distractor_count,
        name_similarity=name_similarity,
        schema_token_volume=schema_token_volume,
        schema_drift=schema_drift,
        tools=tools,
        nodes=nodes,
        initial_inputs=initial_inputs,
        target_node_id=target_node_id,
        expected_target_value=expected_target_value,
        topological_order=topological_order,
        reference_node_values=node_values,
    )
