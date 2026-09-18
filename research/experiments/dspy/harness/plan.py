"""Task decomposition: instruction + environment listing -> typed execution plan."""

from __future__ import annotations

import dspy
from pydantic import BaseModel, Field


class SubGoal(BaseModel):
    id: int = Field(ge=1, description="1-based order of execution")
    description: str = Field(min_length=1, description="one concrete, checkable step")
    target_files: list[str] = Field(
        default_factory=list,
        description="absolute paths this step reads or writes; only paths named or implied by the instruction/listing",
    )
    done_when: str = Field(
        min_length=1, description="observable condition proving the step is complete"
    )


class ExecutionPlan(BaseModel):
    sub_goals: list[SubGoal] = Field(min_length=1, max_length=8)
    required_outputs: list[str] = Field(
        default_factory=list,
        description="absolute paths of every artifact the instruction requires at the end",
    )
    assumptions: list[str] = Field(
        default_factory=list, description="facts assumed but not stated; empty if none"
    )
    verification_commands: list[str] = Field(
        default_factory=list,
        description="shell commands that check required_outputs exist and are well-formed",
    )


class DecomposeSignature(dspy.Signature):
    """Turn a coding/terminal task into a short, strictly ordered execution plan.

    Plan only what the instruction asks. Every target path must be an absolute
    path that appears in the instruction or the environment listing, or is the
    declared output location. Prefer few sub-goals with observable done_when
    conditions over many vague ones. Do not invent tools that are not in the
    listing; note missing tools under assumptions instead.
    """

    instruction: str = dspy.InputField(desc="the task text the agent receives")
    environment_listing: str = dspy.InputField(
        desc="`find /app -maxdepth 3` style listing of the starting container"
    )
    plan: ExecutionPlan = dspy.OutputField()


class TaskDecomposer(dspy.Module):
    def __init__(self) -> None:
        super().__init__()
        self.decompose = dspy.ChainOfThought(DecomposeSignature)

    def forward(self, instruction: str, environment_listing: str) -> dspy.Prediction:
        return self.decompose(instruction=instruction, environment_listing=environment_listing)
