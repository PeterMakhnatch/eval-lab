from __future__ import annotations

import hashlib
import json
import posixpath
import re
from datetime import date, datetime
from typing import Any, Literal, cast, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evallab.benchmark_program_contracts import (
    CampaignCalibrationLedger,
    CampaignMeasurementLedger,
)


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


def canonical_preamble_path(value: str | None) -> str | None:
    """Normalize a repo-relative preamble coordinate; ``none`` is absence."""
    if value in (None, "none"):
        return None
    normalized = posixpath.normpath(value)
    if normalized in ("", ".") or normalized.startswith("/") or normalized == "..":
        raise ValueError(f"invalid preamble path {value!r}")
    if normalized.startswith("../"):
        raise ValueError(f"preamble path must stay relative to the repository: {value!r}")
    return normalized


def canonical_grid_point_id(
    *,
    task_ref: str,
    agent_key: str,
    preamble: str | None,
    k: int,
    arm_id: str | None,
    factor_values: dict[str, Any],
    factor_bindings: dict[str, str],
) -> str:
    """Hash the complete runnable grid coordinate without name parsing."""
    bindings_json = json.dumps(
        factor_bindings, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    payload = {
        "task_ref": task_ref,
        "agent_key": agent_key,
        "preamble": canonical_preamble_path(preamble),
        "k": k,
        "arm_id": arm_id,
        "factors": factor_values,
        "factor_bindings": factor_bindings,
        "factor_bindings_digest": (f"sha256:{hashlib.sha256(bindings_json.encode()).hexdigest()}"),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


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

#: The allowed values as data, derived from the type for callers that enumerate
#: valid research purposes.
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
    extra_instruction_sha256: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="content digest of extra_instruction_path at spec generation",
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
    task_package_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    submitted_at: datetime | None = None
    grid_id: str | None = None
    grid_point: dict[str, Any] | None = None
    task_family: str | None = None
    task_id: str | None = None
    task_instance_id: str | None = None
    generator_seed: int | str | None = Field(
        default=None,
        description=("task-generator seed only; model-sampling seed is uncontrolled and absent"),
    )
    max_requests: int | None = Field(
        default=None,
        ge=1,
        description="enforced per-trial provider request ceiling",
    )
    max_input_tokens: int | None = Field(
        default=None,
        ge=1,
        description="enforced per-trial model input-token ceiling",
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        description="enforced per-trial model output-token ceiling",
    )
    max_total_tokens: int | None = Field(
        default=None,
        ge=1,
        description="enforced per-trial combined token ceiling",
    )
    cost_limit_usd: float | None = Field(
        default=None,
        gt=0,
        description="enforced per-trial agent cost ceiling",
    )
    campaign_ledger: CampaignCalibrationLedger | CampaignMeasurementLedger | None = None
    campaign_cell_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9-]+$",
    )
    campaign_attempt_id: str | None = Field(
        default=None,
        pattern=r"^attempt-[0-9a-f]{24}$",
    )
    campaign_attempt_index: int | None = Field(default=None, ge=1)
    campaign_manifest_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    campaign_spec_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    campaign_evidence_store: str | None = None

    @field_validator(
        "task",
        "task_path",
        "jobs_dir",
        "extra_instruction_path",
        "campaign_evidence_store",
    )
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
    def controls_and_campaigns_are_bounded(self) -> ExperimentSpec:
        if self.agent in {"oracle", "nop"} and self.model:
            raise ValueError(f"the {self.agent} control does not accept a model")
        if self.extra_instruction_sha256 and not self.extra_instruction_path:
            raise ValueError("extra_instruction_sha256 requires extra_instruction_path")
        campaign_fields = (
            self.campaign_ledger,
            self.campaign_cell_id,
            self.campaign_attempt_id,
            self.campaign_attempt_index,
            self.campaign_manifest_digest,
            self.campaign_spec_digest,
            self.campaign_evidence_store,
        )
        if any(value is not None for value in campaign_fields):
            if any(value is None for value in campaign_fields):
                raise ValueError("campaign provenance fields must be declared together")
            if self.attempts != 1 or self.concurrency != 1:
                raise ValueError("campaign specs represent exactly one attempt")
            if self.billable and any(
                value is None
                for value in (
                    self.cost_limit_usd,
                    self.max_requests,
                    self.max_input_tokens,
                    self.max_output_tokens,
                    self.max_total_tokens,
                )
            ):
                raise ValueError(
                    "billable campaign specs require request, cost, and token ceilings"
                )
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
    schema_version: Literal[2] = 2
    matrix_id: str = Field(pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")
    name: str
    hypothesis: str
    benchmark_family: str = Field(min_length=1)
    task_id: str = Field(
        min_length=3,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9-]+$",
    )
    task: str
    task_package_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verifier_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
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
    approved_spec_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    approved_campaign_manifest_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    approved_campaign_spec_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )


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
    grid_id: str | None = None
    point_id: str | None = None
    arm_id: str | None = None
    factor_values: dict[str, str | int | float | bool] | None = None
    factor_bindings: dict[str, Literal["concurrency", "timeout_seconds"]] | None = None
    factor_bindings_digest: str | None = None
    bound_execution_values: dict[Literal["concurrency", "timeout_seconds"], int] | None = None
    preamble_path: str | None = None
    preamble_sha256: str | None = None
    task_family: str | None = None
    task_id: str | None = None
    task_instance_id: str | None = None
    generator_seed: int | str | None = Field(
        default=None,
        description=("task-generator seed only; model-sampling seed is uncontrolled and absent"),
    )
    campaign_ledger: CampaignCalibrationLedger | CampaignMeasurementLedger | None = None
    campaign_cell_id: str | None = None
    campaign_attempt_id: str | None = None
    campaign_attempt_index: int | None = Field(default=None, ge=1)
    campaign_manifest_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    campaign_spec_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )


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
        "preamble_content_sha256",
        "toolset_digest",
        "factor_values_digest",
        "factor_bindings_digest",
        "bound_execution_values_digest",
    ]
    mode: Literal["causal", "exploratory"] = "causal"
    reward_name: str = "reward"
    pass_threshold: float = 1.0
    pass_k: list[int] = Field(default_factory=lambda: [1], min_length=1)
    budget_exhaustion_is_failure: bool = False
    pairing_key: Literal["task_block_id", "task_digest", "task_name", "trial_name"] = "task_digest"
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


CurveFactorValue = str | int | float | bool


class CurvePrimaryContrast(ContractModel):
    """The single preregistered inferential contrast on an empirical curve."""

    level: CurveFactorValue
    k: int = Field(ge=1)


class CurveComparisonSource(ContractModel):
    """One reference-versus-level comparison, either live or already frozen."""

    level: CurveFactorValue
    comparison_spec: CohortComparisonSpec | None = None
    comparison_artifact: str | None = None
    comparison_artifact_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exactly_one_source(self) -> CurveComparisonSource:
        if (self.comparison_spec is None) == (self.comparison_artifact is None):
            raise ValueError(
                "curve comparison source requires exactly one of comparison_spec "
                "or comparison_artifact"
            )
        if self.comparison_artifact is not None:
            if self.comparison_artifact.startswith("/") or ".." in self.comparison_artifact.split(
                "/"
            ):
                raise ValueError("curve comparison artifacts must stay relative to the repository")
            if self.comparison_artifact_digest is None:
                raise ValueError("frozen comparison artifacts require a sha256 digest")
        elif self.comparison_artifact_digest is not None:
            raise ValueError("comparison_artifact_digest requires comparison_artifact")
        return self


class CapabilityCurveSpec(ContractModel):
    """Strict empirical curve contract; it intentionally has no fitted-model fields."""

    schema_version: Literal[1] = 1
    curve_id: str = Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9-]+$")
    factor_name: str = Field(min_length=1)
    factor_unit: str = Field(min_length=1)
    factor_kind: Literal["execution", "task_generator"]
    ordered_levels: list[CurveFactorValue] = Field(min_length=2)
    reference_level: CurveFactorValue
    primary_contrast: CurvePrimaryContrast
    prereg: PreregSpec
    treatment_binding: Literal["concurrency", "timeout_seconds"] | None = None
    comparisons: list[CurveComparisonSource] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_curve(self) -> CapabilityCurveSpec:
        if self.factor_kind == "execution" and self.treatment_binding is None:
            raise ValueError("execution capability curves require an explicit treatment_binding")
        if self.factor_kind == "task_generator" and self.treatment_binding is not None:
            raise ValueError(
                "task_generator capability curves must not declare a harness treatment_binding"
            )
        identities = [json.dumps(value, sort_keys=True) for value in self.ordered_levels]
        if len(identities) != len(set(identities)):
            raise ValueError("curve ordered_levels must be unique")
        reference = json.dumps(self.reference_level, sort_keys=True)
        primary = json.dumps(self.primary_contrast.level, sort_keys=True)
        if reference not in identities:
            raise ValueError("curve reference_level must occur in ordered_levels")
        if primary not in identities or primary == reference:
            raise ValueError("curve primary contrast level must be a non-reference ordered level")
        source_levels = [json.dumps(item.level, sort_keys=True) for item in self.comparisons]
        expected_levels = [item for item in identities if item != reference]
        if source_levels != expected_levels:
            raise ValueError(
                "curve comparisons must occur once in ordered non-reference level order"
            )
        for source in self.comparisons:
            comparison = source.comparison_spec
            if comparison is None:
                continue
            if comparison.pairing_key != "task_block_id":
                raise ValueError("curve comparisons require pairing_key='task_block_id'")
            if len(comparison.cohorts) != 2:
                raise ValueError("curve comparisons require exactly two cohorts")
            if comparison.declared_variable != "factor_values_digest":
                raise ValueError(
                    "curve comparisons require declared_variable='factor_values_digest'"
                )
        primary_source = next(
            item for item in self.comparisons if json.dumps(item.level, sort_keys=True) == primary
        )
        if (
            primary_source.comparison_spec is not None
            and self.primary_contrast.k not in primary_source.comparison_spec.pass_k
        ):
            raise ValueError("primary contrast k must be requested by its comparison spec")
        return self


class CurveMetricReport(ContractModel):
    k: int = Field(ge=1)
    n_tasks: int = Field(ge=0)
    rate: float | None = Field(default=None, ge=0, le=1)
    task_interval_95: list[float] | None = None
    passes: int = Field(ge=0)
    selected_task_blocks: list[str]
    insufficient_task_blocks: list[str]

    @field_validator("task_interval_95")
    @classmethod
    def interval_is_ordered_pair(cls, value: list[float] | None) -> list[float] | None:
        if value is not None and (len(value) != 2 or value[0] > value[1]):
            raise ValueError("curve task intervals must be ordered [lower, upper] pairs")
        return value


class CurveContrastReport(ContractModel):
    k: int = Field(ge=1)
    pass_all_first_k_delta: float | None = None
    pass_all_first_k_interval_95: list[float] | None = None
    pass_all_first_k_wins: int = Field(ge=0)
    pass_all_first_k_ties: int = Field(ge=0)
    pass_all_first_k_losses: int = Field(ge=0)
    n_pairs: int = Field(ge=0)
    paired_delta: float | None = None
    paired_interval_95: list[float] | None = None
    wins: int = Field(ge=0)
    ties: int = Field(ge=0)
    losses: int = Field(ge=0)
    rankable: bool
    refusal_reasons: list[str]

    @field_validator("paired_interval_95", "pass_all_first_k_interval_95")
    @classmethod
    def intervals_are_ordered_pairs(cls, value: list[float] | None) -> list[float] | None:
        if value is not None and (len(value) != 2 or value[0] > value[1]):
            raise ValueError("curve paired intervals must be ordered [lower, upper] pairs")
        return value


class CurveExceptionReport(ContractModel):
    trial_id: str
    task_block_id: str | None
    exception_class: str


class CurveLevelReport(ContractModel):
    level: CurveFactorValue
    role: Literal["reference", "primary", "descriptive"]
    exact_pair_set: list[str]
    unpaired_task_blocks: list[str]
    censored_task_blocks: list[str]
    exception_trials: list[CurveExceptionReport]
    missing_reward_trials: list[str]
    pass_any_first_k: list[CurveMetricReport]
    pass_all_first_k: list[CurveMetricReport]
    contrasts: list[CurveContrastReport]


class CapabilityCurveReport(ContractModel):
    """A provenance-backed empirical curve, never a scalar capability score."""

    schema_version: Literal[1] = 1
    curve_id: str
    factor_name: str
    factor_unit: str
    factor_kind: Literal["execution", "task_generator"]
    ordered_levels: list[CurveFactorValue]
    reference_level: CurveFactorValue
    primary_contrast: CurvePrimaryContrast
    prereg: PreregSpec
    common_controlled_fingerprint: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    input_digests: dict[str, str]
    produced_at: datetime
    produced_by: str = Field(min_length=1)
    rankable: bool
    refuse_to_rank_reasons: list[str]
    levels: list[CurveLevelReport]
    contract_note: Literal[
        "empirical paired contract enforcement; not substantive generality evidence"
    ] = "empirical paired contract enforcement; not substantive generality evidence"

    @field_validator("input_digests")
    @classmethod
    def inputs_are_sha256_digests(cls, values: dict[str, str]) -> dict[str, str]:
        if not values or any(
            not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in values.values()
        ):
            raise ValueError("curve input_digests must be a non-empty sha256 manifest")
        return values

    @model_validator(mode="after")
    def rank_and_level_contract_is_coherent(self) -> CapabilityCurveReport:
        identities = [json.dumps(value, sort_keys=True) for value in self.ordered_levels]
        reported = [json.dumps(item.level, sort_keys=True) for item in self.levels]
        if reported != identities:
            raise ValueError("curve report levels must preserve ordered_levels exactly")
        if self.rankable == bool(self.refuse_to_rank_reasons):
            raise ValueError("rankable curves require no refusal reasons and vice versa")
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


class BehaviorLabel(ContractModel):
    """Versioned semantic or mechanical label for any agent-behavior target."""

    schema_version: Literal[1] = 1
    label_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    target_type: Literal["trajectory", "trial", "event", "action"]
    target_id: str = Field(min_length=1)
    job_id: str | None = None
    trial_id: str
    trial_name: str
    task_name: str
    taxonomy: str = Field(min_length=1)
    label: str = Field(min_length=1)
    rationale: str | None = None
    provenance: Literal["human", "heuristic", "model"]
    author: str = Field(min_length=1)
    created_at: datetime
    confidence: Literal["low", "medium", "high"] | None = None
    evidence: list[AnalysisEvidenceCitation] = Field(default_factory=list)
    source_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    analysis_id: UUID | None = None
    model_provenance: AnalysisProvenance | None = None

    @model_validator(mode="after")
    def provenance_fields_are_consistent(self) -> BehaviorLabel:
        if self.provenance == "model":
            if self.model_provenance is None:
                raise ValueError("model labels require model_provenance")
            if self.confidence is None:
                raise ValueError("model labels require confidence")
        elif self.model_provenance is not None:
            raise ValueError("only model labels may carry model_provenance")
        if self.target_type in {"trajectory", "trial"} and self.target_id != self.trial_id:
            raise ValueError("trajectory/trial label target_id must equal trial_id")
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


DURABLE_CONTROL_EVIDENCE_PREFIX = "research/evidence/runs/"


class ControlEvidenceRef(ContractModel):
    job_name: str = Field(min_length=1)
    trial_name: str = Field(min_length=1)
    reward: float = Field(ge=0.0, le=1.0)
    evidence_path: str = Field(min_length=1)
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    lock_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observed_at: datetime
    task_id: str = Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9-]+$")
    task_version: str = Field(min_length=1)
    task_digests: TaskDigests
    harbor_task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("evidence_path")
    @classmethod
    def evidence_path_is_durable_and_relative(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("evidence_path must stay relative to the repository")
        if not value.startswith(DURABLE_CONTROL_EVIDENCE_PREFIX):
            raise ValueError(
                "control evidence must be under the durable owned root "
                f"{DURABLE_CONTROL_EVIDENCE_PREFIX!r}"
            )
        return value


class TaskControlEvidence(ContractModel):
    oracle: ControlEvidenceRef
    nop: ControlEvidenceRef


DURABLE_TASK_CERTIFICATION_PREFIX = "research/registration/candidates/"
CertificationAxisStatus = Literal["passed", "failed", "not_assessed", "not_applicable"]


class CertificationCheckVector(ContractModel):
    all_controls_completed: bool
    static: bool
    oracle_exact_1_x3: bool
    oracle_stable_output: bool
    nop_exact_0_x2: bool
    invalid_outputs_rejected: bool
    fair_alternative_exact_1: bool
    please_hack_executed: bool
    hack_detected: bool
    leakage_scan_clean: bool
    isolation: bool


class CertificationControlSummary(ContractModel):
    oracle_runs: int = Field(ge=0)
    nop_runs: int = Field(ge=0)
    invalid_probe_runs: int = Field(ge=0)
    fair_alternative_runs: int = Field(ge=0)
    please_hack_runs: int = Field(ge=0)
    result_digests: list[str]

    @field_validator("result_digests")
    @classmethod
    def result_digests_are_sha256(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in values):
            raise ValueError("control result digests must be sha256 values")
        return values


class CertificationAxis(ContractModel):
    status: CertificationAxisStatus
    reason: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)


class CertificationAxes(ContractModel):
    task_correctness: CertificationAxis
    verifier_soundness: CertificationAxis
    verifier_completeness: CertificationAxis
    solvability: CertificationAxis
    difficulty_calibration: CertificationAxis
    realism_review: CertificationAxis


class CertificationIdentity(ContractModel):
    code_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    execution: Literal["local"]
    model: str | None
    prompt_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class TaskCertificationEnvelope(ContractModel):
    """Digest-bound workbench packet reference; legacy absence stays explicit."""

    state: Literal["bound", "legacy_missing"] = "legacy_missing"
    reason: str = Field(default="legacy_record_has_no_certificate_packet", min_length=1)
    certification_id: str | None = Field(default=None, pattern=r"^cert-[0-9a-f]{24}$")
    packet_path: str | None = None
    packet_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_id: str | None = Field(default=None, pattern=r"^candidate-[0-9a-f]{24}$")
    candidate_record_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_package_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    package_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    workbench_version: str | None = None
    check_vector: CertificationCheckVector | None = None
    control_summary: CertificationControlSummary | None = None
    axes: CertificationAxes | None = None
    generator_identity: CertificationIdentity | None = None
    validator_identity: CertificationIdentity | None = None

    @field_validator("packet_path")
    @classmethod
    def packet_path_is_durable(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("certificate packet path must stay repository-relative")
        if not value.startswith(DURABLE_TASK_CERTIFICATION_PREFIX):
            raise ValueError(
                f"certificate packet must be under {DURABLE_TASK_CERTIFICATION_PREFIX!r}"
            )
        if not value.endswith("/certification.json"):
            raise ValueError("certificate packet path must end in certification.json")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> TaskCertificationEnvelope:
        bound_fields = (
            "certification_id",
            "packet_path",
            "packet_sha256",
            "candidate_id",
            "candidate_record_digest",
            "candidate_package_digest",
            "package_digest",
            "workbench_version",
            "check_vector",
            "control_summary",
            "axes",
            "generator_identity",
            "validator_identity",
        )
        if self.state == "bound":
            missing = [name for name in bound_fields if getattr(self, name) is None]
            if missing:
                raise ValueError(f"bound certification is missing fields: {', '.join(missing)}")
            if self.generator_identity == self.validator_identity:
                raise ValueError("generator and validator identities cannot be the same self-check")
        elif any(getattr(self, name) is not None for name in bound_fields):
            raise ValueError("legacy_missing certification cannot carry packet claims")
        return self


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
    schema_version: Literal[2] = 2
    task_id: str = Field(
        min_length=3,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9-]+$",
    )
    task_family: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9_-]+$",
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
    control_evidence: TaskControlEvidence | None = None
    certification: TaskCertificationEnvelope = Field(default_factory=TaskCertificationEnvelope)
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
    state_reason: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_]*$",
        description="machine-readable reason for a non-registered admission state",
    )

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
            if self.state_reason is not None:
                raise ValueError("registered task records cannot carry state_reason")
            if self.control_evidence is None:
                raise ValueError("registered task records require control_evidence")
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
            for label, ref in (
                ("oracle", self.control_evidence.oracle),
                ("nop", self.control_evidence.nop),
            ):
                if ref.task_id != self.task_id or ref.task_version != self.version:
                    raise ValueError(
                        f"registered task {label} evidence identity does not match "
                        "the registry record"
                    )
                if ref.task_digests != self.digests:
                    raise ValueError(
                        f"registered task {label} evidence digests do not match the registry record"
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
        elif self.state == "candidate" and self.control_evidence is None:
            if self.state_reason is None:
                raise ValueError("candidate task without control_evidence requires state_reason")
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
    analysis_role: Literal["trial_review", "review_queue_review", "counterexample_review"] = Field(
        default="trial_review",
        description="Analysis role classification: trial_review, review_queue_review, or counterexample_review",
    )
    source_manifest_digest: str | None = Field(
        default=None,
        description="sha256: of source manifest, optional for trial_review but required for queue/counterexample reviews",
    )
    source_snapshot_digest: str | None = Field(
        default=None,
        description="sha256: of source snapshot, optional for trial_review but required for queue/counterexample reviews",
    )
    source_queue_digest: str | None = Field(
        default=None,
        description="sha256: of source queue, optional for trial_review but required for queue/counterexample reviews",
    )
    decision_eligible: Literal[False] = Field(
        default=False,
        description="Analysis records are strictly non-decision eligible",
    )

    @field_validator("analysis_id", "trial_id")
    @classmethod
    def _ulid_ids(cls, v: str) -> str:
        return _validate_ulid(v)

    @field_validator("rubric_digest")
    @classmethod
    def _rubric_digest(cls, v: str) -> str:
        return _validate_sha256_digest(v)

    @field_validator(
        "source_manifest_digest",
        "source_snapshot_digest",
        "source_queue_digest",
    )
    @classmethod
    def _source_digests(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_sha256_digest(v)

    @model_validator(mode="after")
    def _validate_role_and_digests(self) -> AnalysisRecord:
        if self.analysis_role in {
            "review_queue_review",
            "counterexample_review",
        } and (
            self.source_manifest_digest is None
            or self.source_snapshot_digest is None
            or self.source_queue_digest is None
        ):
            raise ValueError(
                f"analysis_role={self.analysis_role!r} requires source_manifest_digest, "
                "source_snapshot_digest, and source_queue_digest to be non-None"
            )
        return self


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
    task_family: str | None = None
    task_id: str | None = None
    instance_id: str | None = None
    generator_seed: int | str | None = Field(
        default=None,
        description=("task-generator seed only; model-sampling seed is uncontrolled and absent"),
    )


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


FactorValue = str | int | float | bool
FactorBinding = Literal["concurrency", "timeout_seconds"]


class GridFactor(ContractModel):
    """A declared treatment coordinate bound to a runner execution lever."""

    binding: FactorBinding
    levels: list[FactorValue] = Field(min_length=1)

    @model_validator(mode="after")
    def _levels_match_binding(self) -> GridFactor:
        encoded = [json.dumps(level, sort_keys=True) for level in self.levels]
        if len(encoded) != len(set(encoded)):
            raise ValueError("factor contains duplicate levels")
        for level in self.levels:
            if not isinstance(level, int) or isinstance(level, bool):
                raise ValueError(f"binding {self.binding!r} requires integer levels, got {level!r}")
            if level < 1:
                raise ValueError(f"binding {self.binding!r} requires levels >= 1")
            if self.binding == "timeout_seconds" and level > 21_600:
                raise ValueError("timeout_seconds factor levels must be <= 21600")
        return self


class ExperimentArm(ContractModel):
    """Named runnable treatment with fixed agent and factor coordinates."""

    arm_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)
    agent: AgentSpec
    preamble: str = "none"
    factor_overrides: dict[str, FactorValue] = Field(default_factory=dict)

    @field_validator("preamble", mode="before")
    @classmethod
    def _canonical_preamble(cls, value: Any) -> str:
        return canonical_preamble_path(str(value)) or "none"

    @field_validator("agent", mode="before")
    @classmethod
    def _normalize_agent(cls, value: Any) -> AgentSpec:
        if isinstance(value, AgentSpec):
            return value
        if isinstance(value, str):
            from evallab.profiles import builtin_profiles

            profiles = builtin_profiles()
            if value in profiles:
                profile = profiles[value]
                return AgentSpec(agent=profile.adapter, model=profile.model)
            return AgentSpec(agent=value)
        return AgentSpec.model_validate(value)

    @field_validator("factor_overrides")
    @classmethod
    def _factor_names_are_identifiers(
        cls, values: dict[str, FactorValue]
    ) -> dict[str, FactorValue]:
        for name in values:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError(f"invalid factor name {name!r}")
        return values


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
    """Tasks crossed with named arms or legacy agent/preamble axes and factors."""

    task_refs: list[TaskSpec] = Field(min_length=1)
    agents: list[AgentSpec] = Field(default_factory=list)
    arms: list[ExperimentArm] = Field(default_factory=list)
    preamble: list[str] = Field(default_factory=lambda: ["none"])
    factors: dict[str, GridFactor] = Field(default_factory=dict)
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
        if value is None:
            return []
        if isinstance(value, (str, dict, AgentSpec)):
            value = [value]
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

    @field_validator("arms", mode="before")
    @classmethod
    def _normalize_arms(cls, value: Any) -> list[ExperimentArm]:
        if value is None:
            return []
        if isinstance(value, (dict, ExperimentArm)):
            value = [value]
        return [
            item if isinstance(item, ExperimentArm) else ExperimentArm.model_validate(item)
            for item in value
        ]

    @field_validator("factors")
    @classmethod
    def _validate_factors(cls, values: dict[str, GridFactor]) -> dict[str, GridFactor]:
        bindings: dict[FactorBinding, str] = {}
        for name, factor in values.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError(f"invalid factor name {name!r}")
            previous = bindings.get(factor.binding)
            if previous is not None:
                raise ValueError(f"factors {previous!r} and {name!r} both bind {factor.binding!r}")
            bindings[factor.binding] = name
        return values

    @model_validator(mode="after")
    def _one_arm_surface(self) -> GridAxes:
        if bool(self.agents) == bool(self.arms):
            raise ValueError("axes must declare exactly one of agents or arms")
        if self.arms and "preamble" in self.model_fields_set:
            raise ValueError("axes.preamble cannot be declared when axes.arms is used")
        arm_ids = [arm.arm_id for arm in self.arms]
        if len(arm_ids) != len(set(arm_ids)):
            raise ValueError("arm_id values must be unique")
        for arm in self.arms:
            for name, value in arm.factor_overrides.items():
                if name not in self.factors:
                    raise ValueError(f"arm {arm.arm_id!r} overrides undeclared factor {name!r}")
                encoded = json.dumps(value, sort_keys=True)
                declared = {
                    json.dumps(level, sort_keys=True) for level in self.factors[name].levels
                }
                if encoded not in declared:
                    raise ValueError(
                        f"arm {arm.arm_id!r} override for factor {name!r} "
                        f"uses undeclared level {value!r}"
                    )
        return self

    @field_validator("preamble", mode="before")
    @classmethod
    def _normalize_preamble(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not value:
            return ["none"]
        normalized = [canonical_preamble_path(str(item)) or "none" for item in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("preamble levels must be unique")
        return normalized

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
        if len(res) != len(set(res)):
            raise ValueError("k values must be unique")
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
    shard_size: int = Field(default=50, ge=1, le=1000)
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
        declared_factors = {
            name: {json.dumps(level, sort_keys=True) for level in factor.levels}
            for name, factor in self.axes.factors.items()
        }
        declared_arms = {arm.arm_id for arm in self.axes.arms}
        for constraint in self.constraints:
            for coordinate, raw_value in constraint.items():
                values = raw_value if isinstance(raw_value, (list, tuple, set)) else [raw_value]
                if coordinate.startswith("factor."):
                    factor_name = coordinate.removeprefix("factor.")
                    if factor_name not in declared_factors:
                        raise ValueError(f"constraint references undeclared factor {factor_name!r}")
                    undeclared = [
                        value
                        for value in values
                        if json.dumps(value, sort_keys=True) not in declared_factors[factor_name]
                    ]
                    if undeclared:
                        raise ValueError(
                            f"constraint for factor {factor_name!r} uses "
                            f"undeclared levels {undeclared!r}"
                        )
                elif coordinate in {"arm", "arm_id", "arms"} and declared_arms:
                    undeclared = [value for value in values if value not in declared_arms]
                    if undeclared:
                        raise ValueError(f"constraint references undeclared arms {undeclared!r}")
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
    seed_class: AuthoringSeedClass = "craft-gap"
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


class StateEventMetadata(ContractModel):
    """Bounded filesystem metadata emitted by the state-journal producer."""

    path: str = Field(min_length=1, max_length=4096)
    mode: str = Field(min_length=1, max_length=16)
    size_bytes: int = Field(ge=0)
    mtime_ns: int
    type: Literal["file", "directory", "symlink", "other"]
    sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    hash_status: Literal["complete", "size_limit", "unreadable"] | None = None
    target: str | None = Field(default=None, max_length=4096)


class StateJournalEvent(ContractModel):
    """One append-only record from ``state-journal/state-events.jsonl``."""

    sequence: int = Field(ge=1)
    timestamp: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=4096)
    operations: list[str] = Field(min_length=1, max_length=16)
    cookie: int | None = Field(default=None, ge=0)
    is_directory: bool
    state: StateEventMetadata | None = None
