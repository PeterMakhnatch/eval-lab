#!/bin/sh
set -eu
mkdir -p /app/output
python - << 'EOF'
#!/usr/bin/env python3
"""Deterministic reference solver for Function-DAG task."""
import json
from pathlib import Path


def solve():
    inputs_path = Path("/app/src/inputs.json")
    spec_path = Path("/app/src/dag_spec.json")

    if not inputs_path.exists() or not spec_path.exists():
        inputs_path = Path(__file__).resolve().parent.parent / "environment" / "inputs.json"
        spec_path = Path(__file__).resolve().parent.parent / "environment" / "dag_spec.json"

    inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    nodes = spec["nodes"]
    target_id = spec["target_node_id"]
    scope = dict(inputs)

    in_degree = {node_id: 0 for node_id in nodes}
    adjacency = {node_id: [] for node_id in nodes}
    for node_id, node in nodes.items():
        for input_id in node["inputs"]:
            if input_id in nodes:
                adjacency[input_id].append(node_id)
                in_degree[node_id] += 1

    queue = sorted(node_id for node_id, degree in in_degree.items() if degree == 0)
    evaluation_order = []
    while queue:
        current = queue.pop(0)
        evaluation_order.append(current)
        node = nodes[current]
        op = node["op"]
        values = [scope[input_id] for input_id in node["inputs"]]
        params = node.get("params", {})

        if op == "add":
            value = values[0] + values[1]
        elif op == "sub":
            value = values[0] - values[1]
        elif op == "mul":
            value = values[0] * values[1]
        elif op == "linear":
            value = (
                values[0] * params.get("c1", 1)
                + values[1] * params.get("c2", 1)
                + params.get("offset", 0)
            )
        elif op == "clamp":
            value = max(params.get("low", 0), min(values[0], params.get("high", 100)))
        elif op == "max_op":
            value = max(values[0], values[1])
        elif op == "min_op":
            value = min(values[0], values[1])
        elif op == "abs_diff":
            value = abs(values[0] - values[1])
        elif op == "mod_add":
            value = (values[0] + values[1]) % params.get("mod", 100)
        elif op == "xor_op":
            value = values[0] ^ values[1]
        elif op == "scale_offset":
            value = values[0] * params.get("scale", 2) + params.get("offset", 1)
        else:
            raise ValueError(f"Unknown op {op}")

        scope[current] = int(value)
        for next_node in adjacency[current]:
            in_degree[next_node] -= 1
            if in_degree[next_node] == 0:
                queue.append(next_node)
                queue.sort()

    required_nodes = set()
    stack = [target_id]
    while stack:
        node_id = stack.pop()
        if node_id in required_nodes:
            continue
        required_nodes.add(node_id)
        stack.extend(
            input_id
            for input_id in nodes[node_id]["inputs"]
            if input_id in nodes
        )

    dependency_trace = [
        {
            "node": node_id,
            "inputs": [
                {"id": input_id, "value": int(scope[input_id])}
                for input_id in nodes[node_id]["inputs"]
            ],
            "value": int(scope[node_id]),
        }
        for node_id in evaluation_order
        if node_id in required_nodes
    ]
    result = {
        "target": target_id,
        "value": int(scope[target_id]),
        "dependency_trace": dependency_trace,
    }

    out_dir = Path("/app/output")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "result.json"
    out_file.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Computed {target_id} = {scope[target_id]} -> {out_file}")


if __name__ == "__main__":
    solve()

EOF
