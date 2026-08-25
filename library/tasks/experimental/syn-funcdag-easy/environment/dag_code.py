"""Auto-generated deterministic Function-DAG computational graph."""
from __future__ import annotations
from typing import Any, Dict

# Input definitions
INPUT_VALUES = {
    "in_0": 12,
    "in_1": 3
}
TARGET_NODE_ID = "n_2_0"

# Node functions
# Node: n_1_0 (scale_offset) | Distractor: False
def fn_layer1_node0(in_0: int) -> int:
    return (3 * in_0) + (3)

# Node: n_1_1 (xor_op) | Distractor: False
def fn_layer1_node1(in_0: int, in_1: int) -> int:
    return in_0 ^ in_1

# Node: n_2_0 (clamp) | Distractor: False
def fn_layer2_node0(in_1: int) -> int:
    return max(3, min(in_1, 87))

# Node: n_2_1 (xor_op) | Distractor: False
def fn_layer2_node1(in_1: int, n_1_0: int) -> int:
    return in_1 ^ n_1_0
