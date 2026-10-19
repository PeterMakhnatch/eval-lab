"""Requiring equal measurement keys preserves unmeasured circuit equality."""

import cirq
import pytest
from mitiq.utils import _equal

_Q0, _Q1, _Q2 = cirq.LineQubit.range(3)


def _sequence(*operations):
    return cirq.Circuit([cirq.Moment([op]) for op in operations])


@pytest.mark.parametrize("require_qubits", [False, True])
@pytest.mark.parametrize(
    "operations",
    [
        (),
        (cirq.H(_Q0),),
        tuple(cirq.H.on_each(_Q0, _Q1, _Q2)),
        (cirq.H(_Q0), cirq.CNOT(_Q0, _Q1), cirq.Z(_Q1), cirq.X(_Q0)),
    ],
    ids=["empty", "single-gate", "hadamards", "entangling-sequence"],
)
def test_equal_unmeasured_circuits_with_required_measurement_keys(operations, require_qubits):
    left = _sequence(*operations)
    right = _sequence(*operations)
    for first, second in [(left, right), (right, left)]:
        assert _equal(
            first,
            second,
            require_qubit_equality=require_qubits,
            require_measurement_equality=True,
        )


def _entangling_sequence(qubits):
    q0, q1 = qubits
    return _sequence(cirq.H(q0), cirq.CNOT(q0, q1), cirq.Z(q1))


@pytest.mark.parametrize(
    "other_qubits",
    [
        (cirq.LineQubit(4), cirq.LineQubit(8)),
        (cirq.GridQubit(2, 0), cirq.GridQubit(2, 1)),
        (cirq.NamedQubit("p"), cirq.NamedQubit("r")),
    ],
    ids=["line", "grid", "named"],
)
def test_empty_measurement_keys_preserve_qubit_equality_option(other_qubits):
    left = _entangling_sequence((_Q0, _Q1))
    right = _entangling_sequence(other_qubits)
    for first, second in [(left, right), (right, left)]:
        assert _equal(
            first,
            second,
            require_qubit_equality=False,
            require_measurement_equality=True,
        )
        assert not _equal(
            first,
            second,
            require_qubit_equality=True,
            require_measurement_equality=True,
        )


@pytest.mark.parametrize("require_qubits", [False, True])
@pytest.mark.parametrize("require_measurements", [False, True])
@pytest.mark.parametrize(
    "left_operations,right_operations",
    [
        ((cirq.H(_Q0),), (cirq.X(_Q0),)),
        (
            (cirq.H(_Q0), cirq.X(_Q0)),
            (cirq.X(_Q0), cirq.H(_Q0)),
        ),
        (
            (cirq.H(_Q0), cirq.CNOT(_Q0, _Q1), cirq.Z(_Q1)),
            (cirq.H(_Q0), cirq.CNOT(_Q1, _Q0), cirq.Z(_Q1)),
        ),
        (
            (cirq.H(_Q0), cirq.CNOT(_Q0, _Q1), cirq.Z(_Q1)),
            (cirq.H(_Q0), cirq.CNOT(_Q0, _Q1)),
        ),
    ],
    ids=["different-gate", "causal-order", "cnot-direction", "missing-gate"],
)
def test_empty_measurement_keys_do_not_hide_unequal_gate_structure(
    left_operations, right_operations, require_qubits, require_measurements
):
    left = _sequence(*left_operations)
    right = _sequence(*right_operations)
    for first, second in [(left, right), (right, left)]:
        assert not _equal(
            first,
            second,
            require_qubit_equality=require_qubits,
            require_measurement_equality=require_measurements,
        )
