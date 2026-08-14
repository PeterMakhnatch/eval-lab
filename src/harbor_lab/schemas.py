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
    report_date: str | None = None


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


class HeadlessDoctorChecks(ContractModel):
    keychain_readable: bool
    codex_auth_present: bool
    docker_reachable: bool
    postgres_reachable: bool
    disk_headroom: bool


class HeadlessDoctorReport(ContractModel):
    schema_version: Literal[1] = 1
    checked_at: datetime
    healthy: bool
    checks: HeadlessDoctorChecks

    @model_validator(mode="after")
    def healthy_matches_checks(self) -> HeadlessDoctorReport:
        infrastructure_ok = (
            self.checks.docker_reachable
            and self.checks.postgres_reachable
            and self.checks.disk_headroom
        )
        credentials_ok = self.checks.keychain_readable or self.checks.codex_auth_present
        expected = infrastructure_ok and credentials_ok
        if self.healthy != expected:
            raise ValueError(
                "healthy must equal: all infrastructure checks and at least one credential"
            )
        return self


class DigestJob(ContractModel):
    job_name: str
    task_name: str | None = None
    agent_name: str | None = None
    reward: float | None = None
    exception_type: str | None = None
    cost_usd: float | None = None
    policy_rule: str | None = None


class DailyDigestData(ContractModel):
    schema_version: Literal[1] = 1
    date: str
    quarantined: bool
    quarantine_reasons: list[str] = Field(default_factory=list)
    jobs: list[DigestJob] = Field(default_factory=list)
    spend_usd: float = Field(default=0.0, ge=0)
    daily_cost_ceiling_usd: float = Field(ge=0)
    disk_bytes: int = Field(default=0, ge=0)
    queue_depths: dict[str, int] = Field(default_factory=dict)
    waiting_proposals: list[str] = Field(default_factory=list)


class RunProvenance(ContractModel):
    schema_version: Literal[1] = 1
    spec_id: str
    task: str
    task_version: str | None = None
    verifier_digest: str | None = None
    policy_rule: str | None = None


class CanaryMember(ContractModel):
    name: str = Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9-]+$")
    task_path: str = Field(min_length=1)
    task_version: str = Field(min_length=1)
    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_ref: str = Field(min_length=1)
    source_content_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    source_task_name: str | None = None
    est_cost_usd: float = Field(gt=0)

    @field_validator("task_path")
    @classmethod
    def task_path_is_repo_relative(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("task_path must stay relative to the repository")
        return value

    @field_validator("source_ref")
    @classmethod
    def source_ref_is_immutable(cls, value: str) -> str:
        ref = value.rsplit("@", 1)[-1].lower()
        if "@" not in value or ref in {"latest", "head", "main", "master"}:
            raise ValueError("source_ref must include an immutable revision")
        return value


class CanarySuite(ContractModel):
    version: Literal[1] = 1
    attempts: Literal[3] = 3
    agents: list[Literal["codex", "claude-code"]] = Field(min_length=1)
    members: list[CanaryMember] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def member_names_are_unique(self) -> CanarySuite:
        names = [member.name for member in self.members]
        if len(names) != len(set(names)):
            raise ValueError("canary member names must be unique")
        return self


class CanaryDriftObservation(ContractModel):
    schema_version: Literal[1] = 1
    task_name: str
    task_version: str
    agent_name: str
    reward: float | None = None
    attempt_count: int = Field(ge=0)
    exception_count: int = Field(ge=0)
    baseline_n: int = Field(ge=0)
    baseline_mean: float | None = None
    baseline_stddev: float | None = Field(default=None, ge=0)
    previous_task_version: str | None = None
    task_version_changed: bool
    is_harness_drift_suspect: bool
    drift_reason: Literal[
        "task_version_changed",
        "reward_excursion",
        "canary_exception",
    ] | None = None

    @model_validator(mode="after")
    def suspect_has_reason(self) -> CanaryDriftObservation:
        if self.is_harness_drift_suspect != (self.drift_reason is not None):
            raise ValueError("drift suspects require a reason and clean rows require none")
        return self
