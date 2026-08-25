"""Auto-generated deterministic Function-DAG computational graph."""
from __future__ import annotations
from typing import Any, Dict

# Input definitions
INPUT_VALUES = {
    "in_0": 11,
    "in_1": 15,
    "in_2": 5
}
TARGET_NODE_ID = "n_4_0"

# Node functions
# Node: n_1_0 (xor_op) | Distractor: False
def fn_layer1_node0(in_1: int, in_2: int) -> int:
    return in_1 ^ in_2

# Node: n_1_1 (clamp) | Distractor: False
def fn_layer1_node1(in_2: int) -> int:
    return max(3, min(in_2, 59))

# Node: n_1_2 (linear) | Distractor: False
def fn_layer1_node2(in_1: int, in_2: int) -> int:
    return (1 * in_1) + (3 * in_2) + (1)

# Node: n_2_0 (scale_offset) | Distractor: False
def fn_layer2_node0(n_1_1: int) -> int:
    return (4 * n_1_1) + (4)

# Node: n_2_1 (min_op) | Distractor: False
def fn_layer2_node1(in_1: int, n_1_0: int) -> int:
    return min(in_1, n_1_0)

# Node: n_2_2 (sub) | Distractor: False
def fn_layer2_node2(n_1_0: int, n_1_2: int) -> int:
    return n_1_0 - n_1_2

# Node: n_3_0 (sub) | Distractor: False
def fn_layer3_node0(n_2_2: int, n_2_1: int) -> int:
    return n_2_2 - n_2_1

# Node: n_3_1 (max_op) | Distractor: False
def fn_layer3_node1(n_2_0: int, n_2_2: int) -> int:
    return max(n_2_0, n_2_2)

# Node: n_3_2 (sub) | Distractor: False
def fn_layer3_node2(n_2_0: int, n_2_2: int) -> int:
    return n_2_0 - n_2_2

# Node: n_4_0 (scale_offset) | Distractor: False
def fn_layer4_node0(n_2_1: int) -> int:
    return (2 * n_2_1) + (6)

# Node: n_4_1 (min_op) | Distractor: False
def fn_layer4_node1(n_3_2: int, n_2_2: int) -> int:
    return min(n_3_2, n_2_2)

# Node: d_conn_0 (mul) | Distractor: True
def distractor_connected_0(n_3_0: int, n_4_1: int) -> int:
    return n_3_0 * n_4_1

# Node: d_conn_1 (scale_offset) | Distractor: True
def distractor_connected_1(n_4_1: int) -> int:
    return (3 * n_4_1) + (7)

# Node: n_4_2 (clamp) | Distractor: False
def fn_layer4_node2(n_2_0: int) -> int:
    return max(6, min(n_2_0, 49))
