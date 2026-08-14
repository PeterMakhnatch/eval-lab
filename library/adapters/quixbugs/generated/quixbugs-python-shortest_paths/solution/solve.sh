#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/shortest_paths.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def shortest_paths(source, weight_by_edge):
    weight_by_node = {
        v: float('inf') for u, v in weight_by_edge
    }
    weight_by_node[source] = 0

    for i in range(len(weight_by_node) - 1):
        for (u, v), weight in weight_by_edge.items():
            weight_by_node[v] = min(
                weight_by_node[u] + weight,
                weight_by_node[v]
            )

    return weight_by_node
QUIXBUGS_REFERENCE_SOLUTION_EOF
