"""Typed DSPy harness components: task decomposition and error recovery.

Importable building blocks (``TaskDecomposer``, ``ErrorRecovery``) whose outputs
are Pydantic-typed so a harness can branch on them without regex parsing. They
are evaluated offline against Lab task instructions and retained ATIF traces;
nothing here runs Harbor or claims live agent improvement.
"""

# Import order is load-bearing: see the note in ``lm.py``.
import lm  # noqa: F401

from .plan import ExecutionPlan, SubGoal, TaskDecomposer
from .recover import ErrorRecovery, RepairStrategy

__all__ = ["ErrorRecovery", "ExecutionPlan", "RepairStrategy", "SubGoal", "TaskDecomposer"]
