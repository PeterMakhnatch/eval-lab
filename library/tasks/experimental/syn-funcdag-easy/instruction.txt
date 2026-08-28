# Function-DAG Target Evaluation Task

## Context
You are provided with a cleanroom deterministic Function Dependency DAG (Directed Acyclic Graph) in the current environment.

The environment contains:
- `/app/src/inputs.json`: Root input parameter values.
- `/app/src/dag_spec.json`: JSON specification of all graph nodes, their operations, parameters, and input dependencies.
- `/app/src/dag_code.py`: Executable Python functions corresponding to the DAG nodes.

## Goal
Evaluate the dependency chain starting from the root inputs in `/app/src/inputs.json` to compute the exact integer value of the target node:
- **Target Node ID**: `n_2_0`

Note: The graph contains 2 computational layers and 0 distractor nodes (including dead-end branches and disconnected components) that must be filtered out or handled correctly.

## Output Specification
Write a JSON file to `/app/output/result.json` with this exact top-level schema:
```json
{
  "target": "n_2_0",
  "value": <computed_integer_value>,
  "dependency_trace": [
    {
      "node": "<required_node_id>",
      "inputs": [
        {"id": "<input_or_node_id>", "value": <exact_integer_value>}
      ],
      "value": <exact_integer_value>
    }
  ]
}
```

`dependency_trace` must contain every node in the target's transitive dependency
chain exactly once, in topological order, and no distractor nodes. Each `inputs`
array must preserve the node's declared input order and report the exact resolved
integer value for every root input or upstream node. No extra top-level or trace
entry fields are allowed.
