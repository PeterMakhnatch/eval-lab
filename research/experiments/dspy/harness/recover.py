"""Error recovery: (goal, last_action, exit_code, stderr) -> typed repair strategy."""

from __future__ import annotations

from typing import Literal

import dspy
from pydantic import BaseModel, Field

RepairKind = Literal[
    "retry_same",  # transient failure; rerun unchanged
    "fix_command",  # same intent, corrected syntax/flags/quoting
    "fix_path",  # wrong file/dir; correct the path after checking
    "install_or_substitute_tool",  # missing binary/module; install it or use an available equivalent
    "inspect_state",  # gather information before acting (ls, cat, --help, logs)
    "change_approach",  # abandon this method; different tool or algorithm for the same goal
    "fix_environment",  # permissions, env vars, working directory, services
    "stop_and_report",  # cannot proceed; report the blocker instead of looping
]


class RepairStrategy(BaseModel):
    kind: RepairKind
    diagnosis: str = Field(
        min_length=1, description="one sentence: what the error output actually says went wrong"
    )
    next_command: str = Field(
        min_length=1, description="the single shell command to run next; a full command, not prose"
    )
    expected_effect: str = Field(
        min_length=1, description="what output or state change would show the repair worked"
    )
    do_not_repeat: bool = Field(description="true when rerunning last_action unchanged cannot help")


class RecoverSignature(dspy.Signature):
    """Choose the next action after a failed shell command in a terminal agent.

    Read the error output literally: a missing binary is not a syntax error, a
    missing path is not a permission problem. Prefer the cheapest repair that
    addresses the diagnosed cause. Never propose repeating the same command when
    the failure is deterministic. When the goal cannot be reached with the tools
    available, choose stop_and_report.
    """

    goal: str = dspy.InputField(desc="what the agent was trying to accomplish with this step")
    last_action: str = dspy.InputField(desc="the exact shell command that failed")
    exit_code: str = dspy.InputField(desc="numeric exit status, or 'unknown'")
    error_output: str = dspy.InputField(desc="stderr/stdout captured from the failed command")
    recent_history: str = dspy.InputField(
        desc="the few preceding commands, newest last; may be empty"
    )
    strategy: RepairStrategy = dspy.OutputField()


class ErrorRecovery(dspy.Module):
    def __init__(self) -> None:
        super().__init__()
        self.recover = dspy.ChainOfThought(RecoverSignature)

    def forward(
        self,
        goal: str,
        last_action: str,
        exit_code: str,
        error_output: str,
        recent_history: str = "",
    ) -> dspy.Prediction:
        return self.recover(
            goal=goal,
            last_action=last_action,
            exit_code=exit_code,
            error_output=error_output,
            recent_history=recent_history,
        )
