"""Runtime tool names accepted in stored QA execution receipts."""

from __future__ import annotations

RUNTIME_TOOLS = frozenset({
    "run-upstream-grader", "run-port-grader", "run-reference", "run-blank", "probe",
})
