from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContractModel(BaseModel):
    """Strict base for durable lab contracts."""

    model_config = ConfigDict(extra="forbid")


class ExperimentSpec(ContractModel):
    schema_version: Literal[1] = 1
    spec_id: str | None = None
    name: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]+$")
    hypothesis: str = Field(min_length=1)
    task: str = Field(min_length=1)
    task_path: str | None = None
    agent: str = Field(min_length=1)
    model: str | None = None
    environment: str = "docker"
    jobs_dir: str = "runs"
    attempts: int = Field(default=1, ge=1)
    concurrency: int = Field(default=1, ge=1)
    submitted_by: str = Field(min_length=1)
    priority: int = Field(default=100, ge=0, le=1000)
    est_cost_usd: float = Field(default=0.0, ge=0)
    policy_rule: str | None = None
    requires: list[str] = Field(default_factory=list)
    expected_reward: float | None = None
    task_version: str | None = None
    verifier_digest: str | None = None
    submitted_at: datetime | None = None

    @field_validator("task", "task_path", "jobs_dir")
    @classmethod
    def paths_are_repo_relative(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("paths must stay relative to the repository")
        return value

    @model_validator(mode="after")
    def controls_do_not_name_models(self) -> ExperimentSpec:
        if self.agent in {"oracle", "nop"} and self.model:
            raise ValueError(f"the {self.agent} control does not accept a model")
        return self

    @property
    def billable(self) -> bool:
        return self.agent not in {"oracle", "nop"}

    @property
    def executable_task_path(self) -> str:
        return self.task_path or self.task


class MatrixRun(ContractModel):
    name: str
    agent: str
    model: str | None = None
    attempts: int = Field(default=1, ge=1)
    expect_reward: float | None = None
    allow_billable: bool = False


class ExperimentMatrix(ContractModel):
    schema_version: Literal[1] = 1
    name: str
    hypothesis: str
    task: str
    environment: str = "docker"
    jobs_dir: str = "runs"
    concurrency: int = Field(default=1, ge=1)
    runs: list[MatrixRun] = Field(min_length=1)


class AutoRunRule(ContractModel):
    name: str
    tasks: list[str] | None = None
    agents: list[str] = Field(min_length=1)
    max_attempts: int | None = Field(default=None, ge=1)
    requires: list[str] = Field(default_factory=list)


class StandingApprovalsPolicy(ContractModel):
    version: Literal[1] = 1
    daily_cost_ceiling_usd: float = Field(gt=0)
    per_job_cost_ceiling_usd: float = Field(gt=0)
    quiet_failure_rule: int = Field(ge=1)
    auto_run: list[AutoRunRule] = Field(min_length=1)
    escalate_to_human: list[str] = Field(default_factory=list)


QueueState = Literal[
    "proposed",
    "pending",
    "approved",
    "waiting",
    "rejected",
    "running",
    "done",
    "failed",
]


class QueueEvent(ContractModel):
    schema_version: Literal[1] = 1
    event_id: str
    spec_id: str
    occurred_at: datetime
    event: str
    from_state: QueueState | None = None
    to_state: QueueState | None = None
    actor: str
    policy_rule: str | None = None
    reason_code: str | None = None
    job_name: str | None = None


class QueueReason(ContractModel):
    schema_version: Literal[1] = 1
    spec_id: str
    occurred_at: datetime
    code: str
    message: str
    policy_rule: str | None = None


class PolicyDecision(ContractModel):
    schema_version: Literal[1] = 1
    admitted: bool
    policy_rule: str | None = None
    reason_code: str | None = None
    message: str
