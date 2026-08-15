from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal
from uuid import UUID

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


class CohortSelector(ContractModel):
    label: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]+$")
    paths: list[str] = Field(min_length=1)
    trial_names: list[str] = Field(default_factory=list)

    @field_validator("paths")
    @classmethod
    def paths_stay_in_repository(cls, values: list[str]) -> list[str]:
        for value in values:
            if value.startswith("/") or ".." in value.split("/"):
                raise ValueError("cohort paths must stay relative to the repository")
        return values


class CohortComparisonSpec(ContractModel):
    schema_version: Literal[1] = 1
    comparison_id: str = Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9-]+$")
    experiment_id: str = Field(min_length=1)
    declared_variable: Literal[
        "agent_name",
        "agent_version",
        "model_name",
        "model_settings_digest",
        "environment_digest",
        "preamble_hash",
        "toolset_digest",
    ]
    mode: Literal["causal", "exploratory"] = "causal"
    reward_name: str = "reward"
    pass_threshold: float = 1.0
    pass_k: list[int] = Field(default_factory=lambda: [1], min_length=1)
    pairing_key: Literal["task_digest", "task_name", "trial_name"] = "task_digest"
    constraints: dict[
        Literal["task_digest", "verifier_digest", "environment_digest"], str
    ] = Field(default_factory=dict)
    cohorts: list[CohortSelector] = Field(min_length=2)

    @field_validator("pass_k")
    @classmethod
    def pass_k_is_positive_and_unique(cls, values: list[int]) -> list[int]:
        if any(value < 1 for value in values):
            raise ValueError("pass_k values must be positive")
        if len(values) != len(set(values)):
            raise ValueError("pass_k values must be unique")
        return sorted(values)

    @model_validator(mode="after")
    def cohort_labels_are_unique(self) -> CohortComparisonSpec:
        labels = [cohort.label for cohort in self.cohorts]
        if len(labels) != len(set(labels)):
            raise ValueError("cohort labels must be unique")
        return self


FailureCategory = Literal[
    "task_invalid",
    "environment_failure",
    "harness_failure",
    "verifier_false_positive",
    "verifier_false_negative",
    "planning",
    "evidence_use",
    "tool_use",
    "implementation",
    "verification_behavior",
    "context_management",
    "policy_or_refusal",
    "unknown",
]


class AnalysisEvidenceCitation(ContractModel):
    path: str = Field(min_length=1)
    step_id: int | None = Field(default=None, ge=1)
    tool_call_id: str | None = None
    supports: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def path_is_trial_relative(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("analysis evidence paths must stay relative to the trial")
        return value


class TrialAnalysisOutput(ContractModel):
    validity: Literal[
        "valid_agent_attempt",
        "task_defect",
        "environment_failure",
        "harness_failure",
        "verifier_defect",
        "unknown",
    ]
    primary_category: FailureCategory
    summary: str = Field(min_length=1)
    earliest_failure_step_id: int | None = Field(default=None, ge=1)
    evidence: list[AnalysisEvidenceCitation] = Field(min_length=1)
    alternative_explanations: list[str] = Field(default_factory=list)
    proposed_discriminator: str = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]


class AnalysisSourceDigests(ContractModel):
    result: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    trajectory: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    files: dict[str, str] = Field(default_factory=dict)

    @field_validator("files")
    @classmethod
    def file_digests_are_valid(cls, values: dict[str, str]) -> dict[str, str]:
        for path, digest in values.items():
            if path.startswith("/") or ".." in path.split("/"):
                raise ValueError("source digest paths must stay relative to the trial")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError(f"invalid source digest for {path}")
        return values


class AnalysisProvenance(ContractModel):
    agent: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    rubric_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_schema_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: datetime
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class TrialAnalysisSidecar(ContractModel):
    schema_version: Literal[1] = 1
    analysis_id: UUID
    experiment_id: str | None = None
    job_id: UUID
    source_trial_id: UUID
    source_trial_path: str = Field(min_length=1)
    source_digests: AnalysisSourceDigests
    analysis_provenance: AnalysisProvenance
    output: TrialAnalysisOutput
    validation_status: Literal["valid", "invalid"]
    validation_errors: list[str] = Field(default_factory=list)
    raw_response_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validation_status_matches_errors(self) -> TrialAnalysisSidecar:
        if self.validation_status == "valid" and self.validation_errors:
            raise ValueError("valid analyses cannot carry validation errors")
        if self.validation_status == "invalid" and not self.validation_errors:
            raise ValueError("invalid analyses require validation errors")
        return self


class AnalysisReview(ContractModel):
    schema_version: Literal[1] = 1
    review_id: UUID
    analysis_id: UUID
    disposition: Literal["accepted", "needs_revision", "rejected", "superseded"]
    rationale: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    reviewed_at: datetime
    superseded_by: UUID | None = None

    @model_validator(mode="after")
    def superseded_reviews_name_replacement(self) -> AnalysisReview:
        if self.disposition == "superseded" and self.superseded_by is None:
            raise ValueError("superseded reviews require superseded_by")
        if self.disposition != "superseded" and self.superseded_by is not None:
            raise ValueError("superseded_by is only valid for superseded reviews")
        return self


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


class JudgeCriterionVerdict(ContractModel):
    verdict: Literal["yes", "no"]
    rationale: str = Field(min_length=1)


class JudgeDocumentPrediction(ContractModel):
    document_id: str = Field(min_length=1)
    criteria: dict[str, dict[str, JudgeCriterionVerdict]]


class JudgePredictionBundle(ContractModel):
    schema_version: Literal[1] = 1
    family: str = Field(min_length=1)
    judge_backend: str = Field(min_length=1)
    judge_model: str = Field(min_length=1)
    judge_engine_version: str | None = None
    rubric_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    corpus_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    generated_at: datetime
    predictions: list[JudgeDocumentPrediction] = Field(min_length=1)

    @model_validator(mode="after")
    def document_ids_are_unique(self) -> JudgePredictionBundle:
        ids = [prediction.document_id for prediction in self.predictions]
        if len(ids) != len(set(ids)):
            raise ValueError("judge prediction document ids must be unique")
        return self


class CriterionAgreementRate(ContractModel):
    agreements: int = Field(ge=0)
    total: int = Field(ge=1)
    rate: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def rate_matches_counts(self) -> CriterionAgreementRate:
        if self.agreements > self.total:
            raise ValueError("agreements cannot exceed total")
        expected = self.agreements / self.total
        if abs(self.rate - expected) > 1e-12:
            raise ValueError("agreement rate must equal agreements / total")
        return self


class JudgeCalibrationRecord(ContractModel):
    schema_version: Literal[1] = 1
    record_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    family: str = Field(min_length=1)
    status: Literal["measured", "stub"]
    judge_backend: str = Field(min_length=1)
    judge_model: str = Field(min_length=1)
    judge_engine_version: str | None = None
    rubric_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    corpus_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    per_criterion_agreement: dict[str, CriterionAgreementRate] = Field(min_length=1)
    mean_agreement: float = Field(ge=0, le=1)
    agreement_floor: float = Field(default=0.9, ge=0, le=1)
    meets_floor: bool
    reportable: bool
    document_count: int = Field(ge=1)
    evaluated_on: date
    prediction_artifact: str = Field(min_length=1)
    pending_backends: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def summary_matches_criterion_counts(self) -> JudgeCalibrationRecord:
        agreements = sum(item.agreements for item in self.per_criterion_agreement.values())
        total = sum(item.total for item in self.per_criterion_agreement.values())
        expected_mean = agreements / total
        if abs(self.mean_agreement - expected_mean) > 1e-12:
            raise ValueError("mean agreement must equal total agreements / comparisons")
        if self.meets_floor != (self.mean_agreement >= self.agreement_floor):
            raise ValueError("meets_floor must reflect the configured agreement floor")
        if self.reportable != (self.status == "measured"):
            raise ValueError("only measured calibration records are reportable")
        return self


class ProvenanceMetadata(ContractModel):
    """Auditable sidecar for every data item the lab stores or derives.

    One instance accompanies each dataset, trajectory corpus, synthetic task
    batch, or distilled export. Zones are defined in docs/data-architecture.md:
    01 external, 02 local evidence, 03 synthetic, 04 curated distillation.
    """

    schema_version: Literal[1] = 1
    item_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._@-]+$", max_length=120)
    zone: Literal["01-external", "02-local-evidence", "03-synthetic", "04-curated"]
    source_uri: str = Field(min_length=1)
    revision: str | None = None
    material_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    license: str | None = None
    created_at: datetime
    created_by: str = Field(min_length=1)
    transform: str | None = Field(
        default=None,
        description="converter/generator identity as name@version; None for raw acquisitions",
    )
    parent_digests: list[str] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("parent_digests")
    @classmethod
    def parents_are_sha256(cls, value: list[str]) -> list[str]:
        for digest in value:
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError(f"parent digest is not sha256-formatted: {digest!r}")
        return value

    @field_validator("transform")
    @classmethod
    def transform_is_versioned(cls, value: str | None) -> str | None:
        pattern = r"[a-z0-9][a-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*"
        if value is not None and not re.fullmatch(pattern, value):
            raise ValueError("transform must be name@version")
        return value

    @model_validator(mode="after")
    def zone_invariants(self) -> ProvenanceMetadata:
        if self.zone == "01-external" and not self.revision:
            raise ValueError("zone 01 items require an immutable revision pin")
        if self.zone in {"03-synthetic", "04-curated"} and self.transform is None:
            raise ValueError(f"zone {self.zone} items are machine-produced and require a transform")
        if self.zone == "04-curated" and not self.parent_digests:
            raise ValueError("zone 04 distillations must cite parent digests")
        return self
