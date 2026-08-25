"""Auto-generated deterministic Function-DAG computational graph."""
from __future__ import annotations
from typing import Any, Dict

# Input definitions
INPUT_VALUES = {
    "in_0": 14,
    "in_1": 8,
    "in_2": 12,
    "in_3": 9,
    "disc_in_0": 12,
    "disc_in_1": 24
}
TARGET_NODE_ID = "n_6_0"

# Node functions
# Node: d_disc_0 (scale_offset) | Distractor: True
def distractor_disconnected_0(disc_in_0: int) -> int:
    return (2 * disc_in_0) + (10)

# Node: d_disc_1 (clamp) | Distractor: True
def distractor_disconnected_1(disc_in_1: int) -> int:
    return max(0, min(disc_in_1, 50))

# Node: n_1_0 (linear) | Distractor: False
def fn_layer1_node0(in_1: int, in_2: int) -> int:
    return (4 * in_1) + (1 * in_2) + (8)

# Node: n_1_1 (mod_add) | Distractor: False
def fn_layer1_node1(in_0: int, in_2: int) -> int:
    return (in_0 + in_2) % 133

# Node: n_1_2 (mod_add) | Distractor: False
def fn_layer1_node2(in_1: int, in_3: int) -> int:
    return (in_1 + in_3) % 133

# Node: n_1_3 (abs_diff) | Distractor: False
def fn_layer1_node3(in_3: int, in_2: int) -> int:
    return abs(in_3 - in_2)

# Node: n_2_0 (add) | Distractor: False
def fn_layer2_node0(in_3: int, n_1_2: int) -> int:
    return in_3 + n_1_2

# Node: n_2_1 (mod_add) | Distractor: False
def fn_layer2_node1(n_1_3: int, in_2: int) -> int:
    return (n_1_3 + in_2) % 61

# Node: n_2_2 (mul) | Distractor: False
def fn_layer2_node2(in_1: int, in_0: int) -> int:
    return in_1 * in_0

# Node: n_2_3 (add) | Distractor: False
def fn_layer2_node3(in_2: int, in_1: int) -> int:
    return in_2 + in_1

# Node: n_3_0 (sub) | Distractor: False
def fn_layer3_node0(n_2_1: int, n_1_1: int) -> int:
    return n_2_1 - n_1_1

# Node: n_3_1 (linear) | Distractor: False
def fn_layer3_node1(n_2_2: int, n_1_1: int) -> int:
    return (3 * n_2_2) + (3 * n_1_1) + (1)

# Node: n_3_2 (min_op) | Distractor: False
def fn_layer3_node2(n_2_3: int, n_2_2: int) -> int:
    return min(n_2_3, n_2_2)

# Node: n_3_3 (min_op) | Distractor: False
def fn_layer3_node3(n_1_0: int, n_2_0: int) -> int:
    return min(n_1_0, n_2_0)

# Node: n_4_0 (scale_offset) | Distractor: False
def fn_layer4_node0(n_3_2: int) -> int:
    return (4 * n_3_2) + (6)

# Node: n_4_1 (linear) | Distractor: False
def fn_layer4_node1(n_3_1: int, n_2_2: int) -> int:
    return (1 * n_3_1) + (2 * n_2_2) + (5)

# Node: n_4_2 (scale_offset) | Distractor: False
def fn_layer4_node2(n_3_1: int) -> int:
    return (2 * n_3_1) + (5)

# Node: n_4_3 (mul) | Distractor: False
def fn_layer4_node3(n_3_0: int, n_3_2: int) -> int:
    return n_3_0 * n_3_2

# Node: n_5_0 (mod_add) | Distractor: False
def fn_layer5_node0(n_4_0: int, n_4_1: int) -> int:
    return (n_4_0 + n_4_1) % 82

# Node: n_5_1 (xor_op) | Distractor: False
def fn_layer5_node1(n_4_3: int, n_3_1: int) -> int:
    return n_4_3 ^ n_3_1

# Node: d_conn_0 (add) | Distractor: True
def distractor_connected_0(n_5_1: int, n_3_0: int) -> int:
    return n_5_1 + n_3_0

# Node: n_5_2 (add) | Distractor: False
def fn_layer5_node2(n_3_2: int, n_4_3: int) -> int:
    return n_3_2 + n_4_3

# Node: n_5_3 (mod_add) | Distractor: False
def fn_layer5_node3(n_4_3: int, n_4_1: int) -> int:
    return (n_4_3 + n_4_1) % 84

# Node: n_6_0 (clamp) | Distractor: False
def fn_layer6_node0(n_4_3: int) -> int:
    return max(9, min(n_4_3, 49))

# Node: d_conn_1 (mod_add) | Distractor: True
def distractor_connected_1(n_2_3: int, n_6_0: int) -> int:
    return (n_2_3 + n_6_0) % 75

# Node: n_6_1 (add) | Distractor: False
def fn_layer6_node1(n_4_1: int, n_4_3: int) -> int:
    return n_4_1 + n_4_3

# Node: n_6_2 (min_op) | Distractor: False
def fn_layer6_node2(n_4_3: int, n_5_0: int) -> int:
    return min(n_4_3, n_5_0)

# Node: n_6_3 (sub) | Distractor: False
def fn_layer6_node3(n_4_3: int, n_5_3: int) -> int:
    return n_4_3 - n_5_3
