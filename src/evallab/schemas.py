from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal, cast, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContractModel(BaseModel):
    """Strict base for durable lab contracts."""

    model_config = ConfigDict(extra="forbid")


#: The one jobs root a submitted spec may write into.
#:
#: A job is addressed as ``<jobs-root>/<job>/<trial>``: the shape the executor
#: writes (``runner.py:601``), the only shape Harbor's viewer scans
#: (Harbor 0.21.0 ``harbor/viewer/scanner.py:50,86`` — one ``iterdir()`` per
#: level, no recursion), and the depth the run explorer walks
#: (``explorer.py:_discover_jobs``).
#:
#: Discovery does not read this field. Its roots are fixed
#: (``dashboard/explorer.py:80``: ``runs`` and ``research/evidence/runs``), so
#: the field is honoured only when it *is* a scanned root. Depth alone is not
#: the rule — a flat ``my-runs`` is exactly as invisible as a nested
#: ``runs/nightly/jobs``, because neither is scanned.
#:
#: ``research/evidence/runs`` is scanned but deliberately excluded here: a
#: promoted evidence bundle is immutable and is produced by promotion, never by
#: a run (``AGENTS.md`` "Working rules").
EXPLORATION_JOBS_ROOT = "runs"

#: Reserved scratch for the lab's own self-tests, deliberately not browsable.
#:
#: ``evallab smoke`` writes ``runs/_smoke/<job>/jobs`` (``smoke.py:167,222``)
#: and reads it back by direct path (``smoke.py:232``); every job it creates
#: carries the reserved ``smoke-`` name prefix and is excluded from the digest
#: (``digest.py:47-60``). Nothing browses this area, so nesting under it hides
#: nothing an operator is looking for — which is precisely what distinguishes it
#: from the F-04 shape refused below.
SELF_TEST_JOBS_SCRATCH = "runs/_smoke"


def validated_jobs_dir(value: str) -> str:
    """Refuse a ``jobs_dir`` no reader can honour, at submission time.

    The field was free-form, so a run could be written where nothing looks. That
    did not merely drop the run: the explorer read the intermediate directory as
    a job, took the nested job's roll-up ``result.json`` for a trial of it, and
    rendered a fabricated trial annotated ``trajectory unavailable: missing
    trajectory.json`` while the real run vanished (M009 F-04, fixed downstream
    in #66). Refusing here costs one validation error at submission; the
    alternative was a phantom trial discovered hours later.

    Shared by ``ExperimentSpec`` and ``ExperimentMatrix`` so the two cannot
    drift apart.
    """
    normalised = value.rstrip("/")
    if normalised == EXPLORATION_JOBS_ROOT:
        return value
    if normalised.split("/")[:2] == SELF_TEST_JOBS_SCRATCH.split("/"):
        return value
    raise ValueError(
        f"jobs_dir {value!r} is not a jobs root this lab reads. Jobs are "
        "addressed as <jobs-root>/<job>/<trial> — the shape the executor writes "
        "and the only shape Harbor's viewer scans — and discovery scans fixed "
        "roots, not this value, so the run would be written and then be "
        "invisible to `evallab status` and the run explorer (M009 F-04). Set "
        f'"jobs_dir": "{EXPLORATION_JOBS_ROOT}" in the spec and let the run\'s '
        "`name` distinguish it, then resubmit with `uv run evallab submit "
        "<spec.json>`"
    )


#: Why a spec exists. Required on every ``ExperimentSpec``, because until now
#: nothing recorded *intent*: the queue could be listed but never grouped,
#: budgeted, or reasoned about by what the lab was trying to learn
#: (``docs/architecture-review-2026-08-16.md`` §4, "every spec declares WHY";
#: ``docs/build-plan.md`` WS-E item 1, which fixes this exact value set).
#:
#: The taxonomy is Peter's. Do not add a member to make a call site fit — an
#: ill-fitting call site is a finding to report, because a purpose is read as
#: research intent by ``evallab preflight`` and by purpose-scoped budgeting,
#: and a value invented to silence a constructor quietly corrupts both.
ExperimentPurpose = Literal[
    "baseline",
    "comparison",
    "elicitation",
    "drift",
    "calibration",
    "craft",
    "practice",
]

#: The allowed values as data, derived from the type so a refusal can name them
#: and the two can never drift. ``queue.purposeless_spec_message`` prints these.
EXPERIMENT_PURPOSES: tuple[ExperimentPurpose, ...] = get_args(ExperimentPurpose)


class ElicitationSpec(ContractModel):
    """§2.1 / §4 Elicitation tuple describing agent prompt and environment conditions.

    A spec with purpose=elicitation must differ from its reference spec in exactly
    one elicitation field.
    """

    preamble_hash: str | None = Field(
        default=None,
        description="digest or identifier of the preamble / system prompt text",
    )
    toolset: list[str] = Field(
        default_factory=list,
        description="list of tool identifiers configured for the agent",
    )
    env_overrides: dict[str, str] = Field(
        default_factory=dict,
        description="environment variable overrides applied to the execution container",
    )

    def diff_fields(self, other: ElicitationSpec) -> list[str]:
        """Return field names that differ between two elicitation tuples."""
        diffs: list[str] = []
        if self.preamble_hash != other.preamble_hash:
            diffs.append("preamble_hash")
        if self.toolset != other.toolset:
            diffs.append("toolset")
        if self.env_overrides != other.env_overrides:
            diffs.append("env_overrides")
        return diffs


class PreregSpec(ContractModel):
    """§2.1 / §4 Preregistration block for comparison experiments.

    Stored verbatim and quoted by the eval card to prevent post-hoc goalpost moving.
    Both expected outcome and decision rule survive round-tripping unmodified.
    """

    expected: str = Field(
        min_length=1,
        description="expected result or hypothesis outcome statement verbatim",
    )
    decision_rule: str = Field(
        min_length=1,
        description="pre-agreed decision rule for hypothesis acceptance verbatim",
    )


class PowerSpec(ContractModel):
    """§2.1 Statistical power and sample size planning.

    Records minimum detectable difference (mdd) and planned sample size.
    """

    mdd: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="minimum detectable difference target",
    )
    planned_n: int | None = Field(
        default=None,
        ge=1,
        description="planned sample size (number of trials or tasks)",
    )


class ExperimentSpec(ContractModel):
    schema_version: Literal[1] = 1
    spec_id: str | None = None
    name: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]+$")
    hypothesis: str = Field(min_length=1)
    purpose: ExperimentPurpose
    question_ref: str | None = Field(
        default=None,
        description="free string linking this spec to the research question it answers",
    )
    elicitation: ElicitationSpec | None = Field(
        default=None,
        description="elicitation tuple (preamble_hash, toolset, env_overrides)",
    )
    prereg: PreregSpec | None = Field(
        default=None,
        description="preregistration block (expected result, decision rule) stored verbatim",
    )
    power: PowerSpec | None = Field(
        default=None,
        description="power planning block (mdd, planned_n)",
    )
    task: str = Field(min_length=1)
    task_path: str | None = None
    extra_instruction_path: str | None = Field(
        default=None,
        description=(
            "repo-relative path to an extra instruction file appended to the task "
            "instruction (Harbor's --extra-instruction-path). This is the elicitation "
            "lever: the preamble is part of the measured tuple, not neutral background, "
            "so EXP-S03's treatment arm varies it while the control leaves it unset."
        ),
    )
    agent: str = Field(min_length=1)
    model: str | None = None
    environment: str = "docker"
    jobs_dir: str = EXPLORATION_JOBS_ROOT
    attempts: int = Field(default=1, ge=1)
    concurrency: int = Field(default=1, ge=1)
    timeout_seconds: int = Field(default=1_800, ge=1, le=21_600)
    submitted_by: str = Field(min_length=1)
    priority: int = Field(default=100, ge=0, le=1000)
    est_cost_usd: float = Field(default=0.0, ge=0)
    policy_rule: str | None = None
    requires: list[str] = Field(default_factory=list)
    expected_reward: float | None = None
    task_version: str | None = None
    verifier_digest: str | None = None
    submitted_at: datetime | None = None
    grid_id: str | None = None
    grid_point: dict[str, Any] | None = None

    @field_validator("task", "task_path", "jobs_dir", "extra_instruction_path")
    @classmethod
    def paths_are_repo_relative(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("paths must stay relative to the repository")
        return value

    @field_validator("jobs_dir")
    @classmethod
    def jobs_dir_is_a_readable_root(cls, value: str) -> str:
        return validated_jobs_dir(value)

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
    jobs_dir: str = EXPLORATION_JOBS_ROOT
    concurrency: int = Field(default=1, ge=1)
    timeout_seconds: int = Field(default=1_800, ge=1, le=21_600)
    runs: list[MatrixRun] = Field(min_length=1)

    # `purpose` is deliberately NOT declared here, and the asymmetry is the
    # considered decision, not an oversight — do not "fix" it by symmetry.
    #
    # The rule above (`jobs_dir` declared twice) exists because both contracts
    # *use* the field: each resolves a filesystem path. `purpose` is used by
    # exactly one dispatch path. It exists so the queue can be grouped,
    # budgeted, and refused by intent, and a matrix never enters the queue:
    # `cli._matrix_command` calls `Executor.execute_direct`
    # (`cli.py:687-699`), which consults no `PolicyGate`, writes no queue state,
    # and appends nothing to `queue/events.jsonl`. A purpose declared here would
    # be read by nothing.
    #
    # It also could not be budgeted against: `execute_direct` refuses any agent
    # outside `CONTROL_AGENTS` (`queue.py:1405-1409`), so a matrix is
    # structurally incapable of spending regardless of `MatrixRun.allow_billable`.
    # Requiring the field here would invalidate all five committed matrices to
    # record a value no reader consults.

    # A matrix is not built from ``ExperimentSpec`` — ``runner.request_from_matrix``
    # expands it straight into ``RunRequest`` objects (``runner.py:710-716``), and
    # ``runner.load_matrix`` validates it from its own file. So ``jobs_dir`` is
    # declared twice, and every rule on it has to be applied twice or the two
    # contracts drift. This class carried *no* path validation at all: it accepted
    # ``/etc`` and ``../../escape``, which ``runner.py:716`` resolves outside the
    # repository, against ``agents/WORKFLOW.md`` ("never point jobs_dir outside
    # your worktree"). The shared validator closes that too.
    @field_validator("jobs_dir")
    @classmethod
    def jobs_dir_is_a_readable_root(cls, value: str) -> str:
        return validated_jobs_dir(value)


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

    #: The lab's own refusal threshold on the account-wide `used_percent`,
    #: committed **unset**. This is the one place a number goes, and it is
    #: Peter's to set: refusing above some percentage trades the risk of a
    #: lockout against the certainty of work that will not happen.
    #:
    #: Set it to a float and billable dispatch refuses at or above it under
    #: reason code `subscription_quota_ceiling`, kept deliberately distinct from
    #: the provider's own `subscription_quota_exhausted` so `queue/reasons/`
    #: never records a lab policy as the provider's statement
    #: (`docs/quota-accounting.md`; PR #70).
    #:
    #: `None` is the unset state and must stay loadable: this model forbids
    #: extras, and the committed YAML omits the key, so a non-optional field
    #: here would make `load_policy` raise for every command that reads policy.
    #: `gt=0` refuses `0` rather than accepting it, because `0` reads as "off"
    #: while meaning "refuse at or above 0%" — that is, refuse all paid work
    #: silently. A load error naming the field is the honest response.
    refuse_billable_at_used_percent: float | None = Field(default=None, gt=0, le=100)


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
    attempt_number: int | None = Field(default=None, ge=1)
    estimated_cost_usd: float | None = Field(default=None, ge=0)


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
        expected = infrastructure_ok
        if self.healthy != expected:
            raise ValueError("healthy must equal all infrastructure checks")
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
    package_digest: str | None = None
    task_path: str | None = None


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
    constraints: dict[Literal["task_digest", "verifier_digest", "environment_digest"], str] = Field(
        default_factory=dict
    )
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


#: On-disk contract for one durable analysis, rooted at the destination root
#: an analysis was written to:
#:
#:     <destination_root>/<analysis_id>/analysis.json               the sidecar
#:     <destination_root>/<analysis_id>/reviews/<review_id>.json    its reviews
#:
#: Discovery MUST select sidecars *positively* by ``ANALYSIS_SIDECAR_FILENAME``.
#: Globbing ``*.json`` under the destination root sweeps in reviews — and any
#: artifact type added later — and reports them as malformed sidecars.
ANALYSIS_SIDECAR_FILENAME = "analysis.json"
ANALYSIS_REVIEWS_DIRNAME = "reviews"


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
    drift_reason: (
        Literal[
            "task_version_changed",
            "reward_excursion",
            "canary_exception",
        ]
        | None
    ) = None

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


class TaskDigests(ContractModel):
    task_toml: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    instruction: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    environment: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verifier: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    package: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class TaskLimits(ContractModel):
    timeout_seconds: int = Field(default=1_800, ge=1, le=21_600)
    max_memory_mb: int | None = Field(default=None, ge=1)
    max_cpus: float | None = Field(default=None, gt=0)


class ControlEvidenceRef(ContractModel):
    job_name: str = Field(min_length=1)
    reward: float = Field(ge=0.0, le=1.0)
    evidence_path: str | None = None
    evidence_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    observed_at: datetime | None = None

    @field_validator("evidence_path")
    @classmethod
    def evidence_path_is_relative(cls, value: str | None) -> str | None:
        if value is not None and (value.startswith("/") or ".." in value.split("/")):
            raise ValueError("evidence_path must stay relative to the repository")
        return value


class TaskControlEvidence(ContractModel):
    oracle: ControlEvidenceRef
    nop: ControlEvidenceRef


TaskAdmissionState = Literal["candidate", "registered", "retired"]
TaskAllowedUse = Literal[
    "canary",
    "measurement",
    "heldout",
    "foundry-seed",
    "training",
]


PretrainStatus = Literal["y", "n", "unknown"]
PRETRAIN_STATUSES: tuple[PretrainStatus, ...] = get_args(PretrainStatus)


class TaskContamination(ContractModel):
    """§2.1 Contamination assessment for benchmark and task packages.

    Records whether the task was likely in pretraining corpora, since when it has
    been public, and the evidentiary basis for the assessment.
    """

    public_since: date | None = Field(
        default=None,
        description="date the task or source repository became public",
    )
    in_pretrain: PretrainStatus = Field(
        default="unknown",
        description="whether task is suspected in model pretraining data (y, n, unknown)",
    )
    basis: str = Field(
        default="",
        description="evidentiary basis or rationale for contamination assessment",
    )


ContaminationRecord = TaskContamination


class TaskRegistryRecord(ContractModel):
    schema_version: Literal[1] = 1
    task_id: str = Field(
        min_length=3,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9-]+$",
    )
    version: str = Field(min_length=1)
    task_path: str = Field(min_length=1)
    digests: TaskDigests
    source_uri: str = Field(min_length=1)
    source_ref: str | None = None
    license: str | None = None
    provenance_zone: Literal[
        "01-external",
        "02-local-evidence",
        "03-synthetic",
        "04-curated",
    ]
    is_synthetic: bool
    limits: TaskLimits = Field(default_factory=TaskLimits)
    control_evidence: TaskControlEvidence
    state: TaskAdmissionState
    allowed_uses: list[TaskAllowedUse] = Field(min_length=1)
    contamination: TaskContamination | None = Field(
        default=None,
        description="contamination assessment (public_since, in_pretrain, basis)",
    )
    human_minutes: int | None = Field(
        default=None,
        ge=0,
        description="expert human completion time estimate in minutes, if known",
    )
    approved_by: str | None = None
    approved_at: datetime | None = None

    @field_validator("task_path")
    @classmethod
    def task_path_is_relative(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("task_path must stay relative to the repository")
        return value

    @field_validator("allowed_uses")
    @classmethod
    def allowed_uses_unique(cls, values: list[TaskAllowedUse]) -> list[TaskAllowedUse]:
        if len(values) != len(set(values)):
            raise ValueError("allowed_uses items must be unique")
        return values

    @model_validator(mode="after")
    def validate_state_invariants(self) -> TaskRegistryRecord:
        if self.state == "registered":
            if not self.approved_by or not self.approved_by.strip():
                raise ValueError("registered task records require approved_by")
            if self.approved_at is None:
                raise ValueError("registered task records require approved_at")
            if self.control_evidence.oracle.reward != 1.0:
                raise ValueError(
                    "registered task requires oracle reward 1.0 "
                    f"(got {self.control_evidence.oracle.reward})"
                )
            if self.control_evidence.nop.reward != 0.0:
                raise ValueError(
                    "registered task requires nop reward 0.0 "
                    f"(got {self.control_evidence.nop.reward})"
                )
            oracle_ref = self.control_evidence.oracle
            if (
                not oracle_ref.evidence_path
                or not oracle_ref.evidence_digest
                or oracle_ref.observed_at is None
            ):
                raise ValueError(
                    "registered task oracle control requires evidence_path, "
                    "evidence_digest, and observed_at"
                )
            nop_ref = self.control_evidence.nop
            if (
                not nop_ref.evidence_path
                or not nop_ref.evidence_digest
                or nop_ref.observed_at is None
            ):
                raise ValueError(
                    "registered task nop control requires evidence_path, "
                    "evidence_digest, and observed_at"
                )
            if self.provenance_zone == "01-external":
                if not self.license or not self.license.strip():
                    raise ValueError("external registered task requires license")
                if not self.source_ref or any(
                    char in self.source_ref.lower() for char in ("latest", "head", "main", "master")
                ):
                    raise ValueError(
                        "external registered task requires immutable pinned source_ref "
                        "(commit SHA or release tag)"
                    )
        return self


#: ULID format per queue.new_ulid (Crockford base32, 26 chars, time-sortable).
ULID_PATTERN = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
ULID_RE = re.compile(ULID_PATTERN)

#: Content digest format required by all contracts (T1 provenance).
SHA256_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
SHA256_RE = re.compile(SHA256_DIGEST_PATTERN)

#: Discovery ID format per journal convention (D-YYYYMMDD-SUFFIX).
DISCOVERY_ID_PATTERN = r"^D-[0-9]{8}-[0-9A-Za-z]+$"
DISCOVERY_ID_RE = re.compile(DISCOVERY_ID_PATTERN)


def _validate_discovery_id(value: str) -> str:
    """Reject malformed discovery identifiers at construction time."""
    if not DISCOVERY_ID_RE.fullmatch(value):
        raise ValueError(
            f"identifier must match discovery ID format (D-YYYYMMDD-SUFFIX), got {value!r}"
        )
    return value


def _validate_ulid(value: str) -> str:
    """Reject non-ULID identifiers at construction time."""
    if not ULID_RE.fullmatch(value):
        raise ValueError(f"identifier must be ULID, got {value!r}")
    return value


def _validate_sha256_digest(value: str) -> str:
    """Reject bare or malformed digests; every digest field must carry provenance."""
    if not SHA256_RE.fullmatch(value):
        raise ValueError(f"digest must be sha256: prefixed 64-hex, got {value!r}")
    return value


class EvidenceCitation(ContractModel):
    """Single evidence pointer inside an AnalysisRecord (path + optional step)."""

    path: str = Field(min_length=1, description="relative filesystem path to cited artifact")
    step: int | None = Field(
        default=None, ge=0, description="step index within trajectory if citation is step-specific"
    )


class ConfidenceClaim(ContractModel):
    """Automated claim with explicit uncertainty (T4). Never a bare float."""

    level: Literal["low", "medium", "high"] = Field(
        description="qualitative confidence label for the claim"
    )
    n: int | None = Field(
        default=None, ge=0, description="sample size backing statistical claim (None if N/A)"
    )
    interval: tuple[float, float] | None = Field(
        default=None,
        description="95% interval (low, high) when claim is numeric",
    )
    provenance_digest: str | None = Field(
        default=None,
        pattern=SHA256_DIGEST_PATTERN,
        description="sha256: of source data or rubric that produced this claim",
    )


class CriterionAgreement(ContractModel):
    """Per-criterion agreement score carrying n and rate (T4, not bare float)."""

    agreements: int = Field(ge=0, description="number of matching judgments on this criterion")
    total: int = Field(ge=0, description="total judgments rendered on this criterion")
    rate: float = Field(ge=0, le=1, description="agreements / total")


class Suite(ContractModel):
    """§2.1 Suite entity: named frozen collection of TaskVersion members.

    frozen_at set makes the instance reject all further mutation (enforced via
    __setattr__ guard + model validator). This is the contract that prevents
    post-freeze drift in parallel epic work.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=80, description="human-readable suite identifier")
    version: str = Field(
        min_length=1, max_length=40, description="suite version; increments on content change"
    )
    members: list[str] = Field(
        default_factory=list,
        description="TaskVersion references (task_ref@version strings) that constitute the suite",
    )
    frozen_at: datetime | None = Field(
        default=None,
        description="timestamp at which the suite was frozen; when set the instance is immutable",
    )

    _is_frozen: bool = False

    @model_validator(mode="after")
    def _mark_frozen(self) -> Suite:
        if self.frozen_at is not None:
            object.__setattr__(self, "_is_frozen", True)
        return self

    def __setattr__(self, name: str, value: Any) -> None:
        frozen = getattr(self, "_is_frozen", False)
        protected = {"_is_frozen", "_pydantic_fields_set"}
        if frozen and name not in protected:
            raise ValueError("frozen Suite is immutable per §2.1")
        super().__setattr__(name, value)


class AnalysisRecord(ContractModel):
    """§2.1 AnalysisRecord: model-assisted analysis output for one trial.

    analysis_id and trial_id are ULIDs. rubric_digest and all digests are
    validated sha256:. confidence carries explicit uncertainty (T4).
    """

    schema_version: Literal[1] = 1
    analysis_id: str = Field(description="ULID primary key for this analysis record")
    trial_id: str = Field(description="ULID of the source trial (join spine)")
    rubric_digest: str = Field(description="sha256: of the rubric applied")
    model: str = Field(min_length=1, description="judge model identifier")
    category: str = Field(
        min_length=1, description="analysis category (e.g. failure mode, capability)"
    )
    evidence: list[EvidenceCitation] = Field(
        default_factory=list, description="cited evidence locations (path, optional step)"
    )
    confidence: ConfidenceClaim = Field(
        description="claim confidence with n/interval/provenance per T4"
    )

    @field_validator("analysis_id", "trial_id")
    @classmethod
    def _ulid_ids(cls, v: str) -> str:
        return _validate_ulid(v)

    @field_validator("rubric_digest")
    @classmethod
    def _rubric_digest(cls, v: str) -> str:
        return _validate_sha256_digest(v)


class ObservationRecord(ContractModel):
    """§2.1 ObservationRecord: factual extraction per OBSERVATORY template.

    All factual fields taken verbatim from research/observations/TEMPLATE.md
    (template_version through evidence_files). trial_id is ULID. No invented
    taxonomy or capability fields.
    """

    schema_version: Literal[1] = 1
    template_version: Literal["observatory-1"] = Field(
        default="observatory-1", description="version of the observatory extraction template"
    )
    trial_id: str = Field(description="ULID of the observed trial (record key)")
    trial_name: str = Field(min_length=1, description="human-readable trial name")
    job: str = Field(min_length=1, description="job name that produced the trial")
    agent: str = Field(min_length=1, description="agent profile name")
    model: str | None = Field(default=None, description="model used (none for controls)")
    task: str = Field(min_length=1, description="task_ref@version or task path")
    reward: float | str = Field(description="verifier reward value (float or 'none')")
    steps_taken: int = Field(ge=0, description="number of steps executed")
    first_failure_step: int | Literal["none"] = Field(
        default="none", description="step of first failure or 'none'"
    )
    loop_detected: Literal["yes", "no"] = Field(
        default="no", description="whether a loop was detected"
    )
    loop_step: int | Literal["none"] = Field(
        default="none", description="step at which loop began or 'none'"
    )
    verified_before_done: Literal["yes", "no"] = Field(
        default="no", description="verifier passed before done signal"
    )
    tool_errors: int = Field(ge=0, description="count of tool invocation errors")
    summary: str = Field(description="one-sentence factual summary from trajectory/result.json")
    evidence_files: str = Field(
        default="", description="comma-separated list of evidence files examined"
    )

    @field_validator("trial_id")
    @classmethod
    def _trial_ulid(cls, v: str) -> str:
        return _validate_ulid(v)


class CalibrationRecord(ContractModel):
    """§2.1 CalibrationRecord: judge calibration result on a corpus.

    per_criterion_agreement uses explicit Agreement objects (T4) rather than
    bare floats. All digests validated.
    """

    schema_version: Literal[1] = 1
    calib_id: str = Field(description="ULID primary key for this calibration run")
    judge_model: str = Field(min_length=1, description="model that performed the judging")
    rubric_digest: str = Field(description="sha256: of rubric used for calibration")
    corpus_digest: str = Field(description="sha256: of the corpus of trials judged")
    per_criterion_agreement: dict[str, CriterionAgreement] = Field(
        min_length=1, description="agreement rate per rubric criterion (with n and rate)"
    )
    date: datetime = Field(description="calendar date of the calibration batch")

    @field_validator("calib_id")
    @classmethod
    def _calib_ulid(cls, v: str) -> str:
        return _validate_ulid(v)

    @field_validator("rubric_digest", "corpus_digest")
    @classmethod
    def _calib_digests(cls, v: str) -> str:
        return _validate_sha256_digest(v)


class Verdict(ContractModel):
    """§2.1 Verdict: human (or authorized) disposition on a discovery.

    status restricted to the literal set in §2.1. discovery_id follows the
    journal's discovery identifier format (D-YYYYMMDD-SUFFIX).
    """

    schema_version: Literal[1] = 1
    discovery_id: str = Field(
        pattern=DISCOVERY_ID_PATTERN,
        description="discovery identifier being verdicted (composite key)",
    )
    status: Literal["accepted", "rejected", "needs_evidence", "pending"] = Field(
        description="disposition per §2.1"
    )
    by: str = Field(min_length=1, description="actor or session that issued the verdict")
    at: datetime = Field(description="timestamp of the verdict decision")
    note: str | None = Field(default=None, description="free-text rationale or pointer to evidence")

    @field_validator("discovery_id")
    @classmethod
    def _discovery_id(cls, v: str) -> str:
        return _validate_discovery_id(v)


class TaskSpec(ContractModel):
    """Specification of a task in the grid."""

    task: str = Field(min_length=1)
    task_path: str | None = None
    expected_reward: float | None = None
    task_version: str | None = None
    verifier_digest: str | None = None


class AgentSpec(ContractModel):
    """Specification of an agent in the grid."""

    agent: str = Field(min_length=1)
    model: str | None = None
    environment: str | None = None
    est_cost_per_trial_usd: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def validate_control_models(self) -> AgentSpec:
        if self.agent in {"oracle", "nop"} and self.model:
            raise ValueError(f"Control agent {self.agent!r} must not declare a model")
        return self


class ProviderLimit(ContractModel):
    """Quota and batch limits for a single provider/agent."""

    max_specs: int | None = Field(default=None, ge=1)
    max_trials: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0.0)


class GridLimits(ContractModel):
    """Global and per-provider bounds on grid expansion."""

    max_specs: int | None = Field(default=None, ge=1)
    max_trials: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0.0)
    per_provider: dict[str, ProviderLimit] = Field(default_factory=dict)


class GridAxes(ContractModel):
    """Experimental axes for Cartesian grid expansion: tasks x agents x preambles x k."""

    task_refs: list[TaskSpec] = Field(min_length=1)
    agents: list[AgentSpec] = Field(min_length=1)
    preamble: list[str] = Field(default_factory=lambda: ["none"])
    k: list[int] = Field(default_factory=lambda: [1])

    @field_validator("task_refs", mode="before")
    @classmethod
    def _normalize_task_refs(cls, value: Any) -> list[TaskSpec]:
        if isinstance(value, (str, dict, TaskSpec)):
            value = [value]
        if not value:
            raise ValueError("task_refs must not be empty")
        res: list[TaskSpec] = []
        for v in value:
            if isinstance(v, str):
                res.append(TaskSpec(task=v))
            elif isinstance(v, dict):
                res.append(TaskSpec.model_validate(v))
            elif isinstance(v, TaskSpec):
                res.append(v)
            else:
                raise ValueError(f"Invalid task reference item: {v}")
        return res

    @field_validator("agents", mode="before")
    @classmethod
    def _normalize_agents(cls, value: Any) -> list[AgentSpec]:
        if isinstance(value, (str, dict, AgentSpec)):
            value = [value]
        if not value:
            raise ValueError("agents must not be empty")
        from evallab.profiles import builtin_profiles

        builtins = builtin_profiles()
        res: list[AgentSpec] = []
        for v in value:
            if isinstance(v, str):
                if v in builtins:
                    profile = builtins[v]
                    res.append(AgentSpec(agent=profile.adapter, model=profile.model))
                else:
                    res.append(AgentSpec(agent=v))
            elif isinstance(v, dict):
                res.append(AgentSpec.model_validate(v))
            elif isinstance(v, AgentSpec):
                res.append(v)
            else:
                raise ValueError(f"Invalid agent item: {v}")
        return res

    @field_validator("preamble", mode="before")
    @classmethod
    def _normalize_preamble(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not value:
            return ["none"]
        return list(value)

    @field_validator("k", mode="before")
    @classmethod
    def _normalize_k(cls, value: Any) -> list[int]:
        if isinstance(value, int):
            if value < 1:
                raise ValueError("k must be >= 1")
            return [value]
        if not value:
            return [1]
        res: list[int] = []
        for item in value:
            int_val = int(item)
            if int_val < 1:
                raise ValueError(f"k value {int_val} must be >= 1")
            res.append(int_val)
        return res


class GridSpec(ContractModel):
    """Declared specification for an evaluation grid expansion (v2 §4)."""

    schema_version: Literal[1] = 1
    grid_id: str | None = None
    name: str | None = None
    purpose: ExperimentPurpose
    axes: GridAxes | None = None
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    daily_budget_units: int | float | None = Field(default=None, ge=0)

    # Execution defaults and limits
    environment: str = "docker"
    jobs_dir: str = EXPLORATION_JOBS_ROOT
    concurrency: int = Field(default=1, ge=1)
    timeout_seconds: int = Field(default=1_800, ge=1, le=21_600)
    submitted_by: str = Field(default="ladder-generator", min_length=1)
    priority: int = Field(default=100, ge=0, le=1000)
    hypothesis: str | None = None
    hypothesis_template: str | None = None
    est_cost_per_trial_usd: dict[str, float] | float = Field(default_factory=dict)
    limits: GridLimits = Field(default_factory=GridLimits)
    check_quota_headroom: bool = True
    policy_rule: str | None = None
    requires: list[str] = Field(default_factory=list)

    # Backward-compatible flat fields
    tasks: list[str | TaskSpec] | None = None
    agents: list[str | AgentSpec] | None = None
    preambles: list[str] | None = None
    attempts: list[int] | None = None

    @field_validator("jobs_dir")
    @classmethod
    def _jobs_dir_is_readable_root(cls, value: str) -> str:
        return validated_jobs_dir(value)

    @field_validator("constraints", mode="before")
    @classmethod
    def _normalize_constraints(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [v if isinstance(v, dict) else dict(v) for v in value]
        raise ValueError("constraints must be a list of condition dicts or a condition dict")

    @model_validator(mode="after")
    def _populate_axes_and_identity(self) -> GridSpec:
        if self.axes is None:
            tasks_val = self.tasks
            agents_val = self.agents
            if tasks_val is None or len(tasks_val) == 0:
                raise ValueError("grid specification must provide axes.task_refs or tasks")
            if agents_val is None or len(agents_val) == 0:
                raise ValueError("grid specification must provide axes.agents or agents")
            preambles_val = self.preambles or ["none"]
            attempts_val = self.attempts or [1]
            if isinstance(attempts_val, int):
                attempts_val = [attempts_val]
            self.axes = GridAxes(
                task_refs=tasks_val,
                agents=agents_val,
                preamble=preambles_val,
                k=attempts_val,
            )
        self.tasks = cast(list[str | TaskSpec], list(self.axes.task_refs))
        self.agents = cast(list[str | AgentSpec], list(self.axes.agents))
        self.preambles = self.axes.preamble
        self.attempts = self.axes.k
        if not self.name and not self.grid_id:
            raise ValueError("grid specification must declare either grid_id or name")
        if not self.grid_id and self.name:
            self.grid_id = self.name
        if not self.name and self.grid_id:
            self.name = self.grid_id
        return self


LadderGridSpec = GridSpec


# --------------------------------------------------------------------------- #
# Authoring / Generation Proposal Specs (SG-1..3)
# --------------------------------------------------------------------------- #

AuthoringSeedClass = Literal["mutation", "scenario", "craft-gap", "inversion"]
AUTHORING_SEED_CLASSES: tuple[AuthoringSeedClass, ...] = get_args(AuthoringSeedClass)


class ProposalAxes(ContractModel):
    """Axis coordinates for a task proposal spec (SG-2)."""

    category: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    difficulty: str = Field(min_length=1)
    target_facets: dict[str, Any] | None = None


class ProposalSpec(ContractModel):
    """Specification for a task proposal generated by authoring / meta-loop."""

    schema_version: Literal["spec/1"] = "spec/1"
    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    difficulty: str = Field(min_length=1)
    summary: str | None = None
    seed_class: str = "craft-gap"
    target_facets: dict[str, Any] | None = None
    scenario_path: str | None = None
    ref_task: str | None = None
    provenance: str | None = None
    axes: dict[str, Any] | ProposalAxes | None = None


class InversionAnalysis(ContractModel):
    """§2.1 / SG-3 Inversion analysis metadata binding data asset, code, and verified key.

    The answer key is correct by construction because it was computed by executing
    analysis code against real data.
    """

    schema_version: Literal["inversion/1"] = "inversion/1"
    data_asset_path: str = Field(
        min_length=1, description="relative path to source data asset in repo"
    )
    data_asset_digest: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$", description="content hash of data asset"
    )
    analysis_code: str = Field(
        min_length=1, description="reference Python code executed against data asset"
    )
    analysis_digest: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$", description="content hash of analysis code"
    )
    computed_value: Any = Field(description="exact answer key produced by code execution")
    executed_at: str = Field(description="ISO-8601 UTC timestamp of execution")
    output_path: str = Field(
        default="output/summary.json", description="target output path relative to container root"
    )


class InversionSpec(ContractModel):
    """Specification for generating an inversion task proposal."""

    schema_version: Literal["spec/1"] = "spec/1"
    name: str = Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9-]+$")
    seed_class: Literal["inversion"] = "inversion"
    data_asset_path: str = Field(min_length=1)
    data_asset_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    analysis_code: str | None = None
    category: str = "data-processing"
    difficulty: str = "medium"
    summary: str = Field(min_length=1)
