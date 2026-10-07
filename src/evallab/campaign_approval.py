"""One approval per experiment campaign instead of one per run ID (HAR-175).

Built on the existing ``evallab campaign`` machinery, not beside it: admission
runs inside :class:`evallab.queue.PolicyGate.decide` (the same gate that owns
the per-ID ``human_approved`` path), spend is accounted from the existing
queue states plus the ledger-backed settled costs the catalog already
computes, exhaustion reuses the existing queue ``STOP`` fence (running trials
are never killed), setup matching reuses the HAR-149 ``setup_fingerprint``
vocabulary with the HAR-156 ceiling comparison, infra-excluded trials reuse
the canonical :func:`evallab.counts.classify_counts` verdict, and escalation
reuses ``lin comment`` on the campaign's card.

A campaign records the budget (USD) plus the pinned cost envelope
(staging-time expected and worst-case cost with its formula), the allowed
task list (task ids and package digests), the reference profile plus the
allowed declared deviations, the lock requirement, the backstop floor for
trial ceilings, the queue cwd, and the submitter. Research-Harbor approves it ONCE, as Peter's delegate.
The approval is an append-only, attributable record (``approvals.jsonl``) and
the campaign content is digest-pinned and immutable after approval: any later
content change is a gate defect that refuses admission and escalates.

Specs claim a campaign with the explicit ``ExperimentSpec.campaign_id`` field.
Matching specs are admitted automatically with policy rule
``campaign:<campaign-id>``; nothing is approved per ID. Any mismatch is
refused with a specific reason code and the spec stays waiting/refused: it is
never silently admitted. Specs without a campaign keep the per-ID path
exactly as it is.

Standing rules inside a campaign:

- **Replacement:** an infra-excluded trial (canonical counts ``excluded`` /
  ``infra``) gets exactly ONE automatic replacement: a cloned spec with
  ``campaign_replaces`` lineage to the original. A replacement's own infra
  failure is not replaced again.
- **Budget:** reservations plus settled GPU, sandbox and ledger-backed model
  spend stay inside the campaign budget. Self-hosted and zero-estimate Daytona
  specs reserve expected wave-model cost; unavailable settled sources
  retain the remaining reservation rather than becoming free.
- **Exhaustion:** new launches stop via the existing ``STOP`` fence while
  running trials finish untouched.

Escalation to Research-Harbor happens only on a budget or rule breach, or a
gate defect: one escalation event plus one ``lin comment`` to the campaign's
card per (spec, reason). Routine admissions are silent.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from evallab.campaign_execution import CampaignExecutionPolicy, validate_qualification
from evallab.results import JobRecord, TrialRecord, load_job
from evallab.schemas import (
    ContractModel,
    ExperimentSpec,
    PolicyDecision,
    ReferenceDeviation,
    normalize_linear_card,
)

SCHEMA_CAMPAIGN = "experiment-campaign/v1"
SCHEMA_APPROVAL = "experiment-campaign-approval/v1"
SCHEMA_ESCALATION = "experiment-campaign-escalation/v1"

#: Campaign state lives with the existing campaign state root so there is one
#: mechanism, one directory tree, one set of conventions.
CAMPAIGN_STATE_ROOT = Path("runs/campaigns")
CAMPAIGN_FILENAME = "campaign.json"
APPROVALS_FILENAME = "approvals.jsonl"
ESCALATIONS_FILENAME = "escalations.jsonl"

#: Queue states mirrored from evallab.queue.QUEUE_STATES (importing queue here
#: would cycle: queue imports this module for the campaign gate branch).
QUEUE_STATES: tuple[str, ...] = (
    "proposed",
    "pending",
    "approved",
    "waiting",
    "rejected",
    "running",
    "done",
    "failed",
)
RESERVED_STATES = frozenset({"approved", "running"})
SETTLED_STATES = frozenset({"done", "failed"})

#: Reason codes for campaign admission refusals. Each mismatch has its own.
REASON_UNKNOWN = "campaign_unknown"
REASON_NOT_APPROVED = "campaign_not_approved"
REASON_QUEUE_MISMATCH = "campaign_queue_mismatch"
REASON_CONTENT_CHANGED = "campaign_content_changed"
REASON_PROFILE_MISMATCH = "campaign_profile_mismatch"
REASON_SETUP_MISMATCH = "campaign_setup_mismatch"
REASON_LOCK_MISSING = "campaign_lock_missing"
REASON_TASK_NOT_ALLOWED = "campaign_task_not_allowed"
REASON_TASK_DIGEST_MISMATCH = "campaign_task_digest_mismatch"
REASON_CEILING_BELOW_FLOOR = "campaign_ceiling_below_floor"
REASON_BUDGET_EXHAUSTED = "campaign_budget_exhausted"
REASON_GATE_UNCONFIGURED = "campaign_gate_unconfigured"
REASON_ORACLE_UNCONFIRMED = "campaign_oracle_unconfirmed"
REASON_SAMPLING_QUEUED = "campaign_sampling_queued"
REASON_SAMPLING_PENDING = "campaign_sampling_pending"
REASON_SAMPLING_CLASSIFIED = "campaign_sampling_classified"
REASON_SAMPLING_INVALID = "campaign_sampling_invalid"

#: Refusals that escalate to Research-Harbor (escalation event + lin comment).
#: Routine setup/profile/lock/deviation mismatches stay silent in waiting with
#: their reason; only a budget or rule breach, or a gate defect, pages out.
ESCALATING_REASONS = frozenset(
    {
        REASON_BUDGET_EXHAUSTED,
        REASON_TASK_NOT_ALLOWED,
        REASON_TASK_DIGEST_MISMATCH,
        REASON_CEILING_BELOW_FLOOR,
        REASON_CONTENT_CHANGED,
    }
)


class CampaignApprovalError(RuntimeError):
    """A campaign file, approval, or admission check failed closed."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class _FrozenContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CampaignTaskAllowance(_FrozenContract):
    """One task on the campaign allowlist, as the ledger knows it."""

    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    package_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verifier_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    expected_cost_usd: float | None = Field(default=None, gt=0)
    expected_wall_seconds: float | None = Field(default=None, gt=0)


class CampaignAdaptiveSampling(_FrozenContract):
    """Beta(1,1) predictive confidence in the finite HAR-168 outcome band.

    Always means all M gated passes; never means zero; sometimes means mixed.
    A mixed prefix is conclusive. Homogeneous prefixes integrate the remaining
    Bernoulli draws under the posterior; this is not confidence that latent p
    equals exactly zero/one. At .95, M=2 saves nothing; M=4 saves an expected
    5*p*(1-p) draws by stopping mixed prefixes. Predictive misclassification
    risk at an early stop is <= 1-target_confidence under the stated prior,
    not a uniform frequentist guarantee (the simulation measures that error).
    """

    target_confidence: float = Field(default=0.95, gt=0.5, le=1)
    seed: int = 20261007


def adaptive_band(
    outcomes: list[bool], max_attempts: int, target_confidence: float = 0.95
) -> tuple[str | None, float]:
    """Return a certified finite-campaign band and its predictive confidence."""
    if max_attempts < 1 or len(outcomes) > max_attempts or not 0.5 < target_confidence <= 1:
        raise ValueError("invalid adaptive sampling horizon or confidence")
    if not outcomes:
        return None, 0.0
    passes = sum(outcomes)
    if 0 < passes < len(outcomes):
        return "sometimes", 1.0
    k = len(outcomes)
    confidence = math.prod((k + 1 + j) / (k + 2 + j) for j in range(max_attempts - k))
    band = "always" if passes == k else "never"
    return (band if confidence >= target_confidence else None), confidence


class CampaignCeilingFloor(_FrozenContract):
    """Backstop floor: no trial ceiling may bind before these reference limits.

    Only set fields bind. Each set field reuses the HAR-156 comparison
    direction: a spec ceiling below the floor differs from the reference and
    is refused (``campaign_ceiling_below_floor``).
    """

    max_requests: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_total_tokens: int | None = Field(default=None, ge=1)
    cost_limit_usd: float | None = Field(default=None, gt=0)


class CampaignSampling(_FrozenContract):
    """Pinned sampling, exactly as ``setup_fingerprint.sampling_sent`` reports.

    Non-selfhosted routes pin no sampling here (all None); the equality check
    then holds trivially and temperature-style differences must arrive as
    declared deviations, where the allowlist judges them.
    """

    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None


class CampaignCostEstimate(_FrozenContract):
    """Pinned cost envelope for one campaign (HAR-188).

    Part of the approvable content so Research-Harbor approves the envelope,
    not just the budget cap: ``expected_usd`` is the staging-time estimate
    for the full campaign, ``worst_case_usd`` is every trial billing its
    per-trial ceiling (realized spend is still fenced at ``budget_usd`` plus
    at most one wave of in-flight ceilings by the standing budget gate),
    and ``formula`` records how the numbers were derived.
    """

    expected_usd: float = Field(gt=0)
    worst_case_usd: float = Field(gt=0)
    formula: str = Field(min_length=1)


#: HAR-168 measured rates reused by the HAR-188 breadth cost envelope.
#: GPU/bridge upper ($2.90/h) and Daytona ($0.23148/h/sandbox) come from the
#: ignored HAR-168 controller (har164 ``runs/har168/campaign.py``); the $1.28
#: batch is its fresh measured-time upper, not an invoice-backed price.
HAR168_GPU_HOURLY_USD = 2.90
HAR168_DAYTONA_HOURLY_USD = 0.23148
HAR168_FOUR_RUN_BATCH_USD = 1.28
HAR168_TRIAL_CEILING_USD = 0.6
#: Effective batch wall from the HAR-168 fresh 4-run batch, measured at 4
#: concurrent trials; whether 19 trials on one server decode slower is
#: unknown (HAR-168 records no per-trial wall times or server throughput).
HAR168_BATCH_WALL_HOURS = HAR168_FOUR_RUN_BATCH_USD / (
    HAR168_GPU_HOURLY_USD + 4 * HAR168_DAYTONA_HOURLY_USD
)
#: Cold server start before the first wave, in hours.
COLD_START_HOURS = 4 / 60
#: Max concurrent Daytona sandboxes: the HAR-163 clamp
#: ``floor((limit * safety - used - pending - reserve) / per_sandbox)``
#: (``dispatch_guards.daytona_tick_allowance``) admits 19 on an idle account
#: (200 * 0.8 - 0 - 0 - 8) / 8; lower whenever the account is busy.
DAYTONA_WAVE_CONCURRENCY = 19


def wave_cost_estimate(n_trials: int) -> CampaignCostEstimate:
    """Concurrent-wave cost envelope for ``n_trials`` on one warm server.

    The batch runs in waves of at most ``DAYTONA_WAVE_CONCURRENCY`` trials
    while the GPU server stays warm for the whole wall time: ``w =
    ceil(n / 19)`` waves, GPU ``(cold start + w * T) * $2.90/h``, Daytona ``n
    * T * $0.23148/h``. Worst case is every trial billing its $0.60
    per-trial ceiling; realized spend stays inside the budget plus at most
    one wave of in-flight ceilings (see ``fenced_spend_bound``).
    """
    if n_trials < 1:
        raise ValueError(f"wave_cost_estimate needs at least 1 trial, got {n_trials}")
    waves = -(-n_trials // DAYTONA_WAVE_CONCURRENCY)
    expected = (
        COLD_START_HOURS + waves * HAR168_BATCH_WALL_HOURS
    ) * HAR168_GPU_HOURLY_USD + n_trials * HAR168_BATCH_WALL_HOURS * HAR168_DAYTONA_HOURLY_USD
    return CampaignCostEstimate(
        expected_usd=round(expected, 2),
        worst_case_usd=round(n_trials * HAR168_TRIAL_CEILING_USD, 2),
        formula=(
            f"E=(4/60h+w*{HAR168_BATCH_WALL_HOURS:.4f}h)*$2.90/h"
            f"+n*{HAR168_BATCH_WALL_HOURS:.4f}h*$0.23148/h, w=ceil(n/19) "
            "waves (19-wide Daytona clamp, lower if busy), T from HAR-168 "
            "fresh 4-run batch at 4 concurrent (19-wide decode unknown); "
            "W=n*$0.60 ceiling, realized<=budget+19 in-flight ceilings"
        ),
    )


def fenced_spend_bound(budget_usd: float) -> float:
    """Realized-spend bound the HAR-175 budget gate enforces (HAR-188).

    The gate refuses a launch once reservations plus settled actuals plus the
    candidate estimate exceed the budget, so at most one wave of already
    running trials can still settle: ``budget + 19 * $0.60``.
    """
    return round(budget_usd + DAYTONA_WAVE_CONCURRENCY * HAR168_TRIAL_CEILING_USD, 2)


class ExperimentCampaign(_FrozenContract):
    """The approvable content of one experiment campaign (HAR-175)."""

    schema_version: Literal["experiment-campaign/v1"] = SCHEMA_CAMPAIGN
    campaign_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    budget_usd: float = Field(gt=0)
    attempts_per_task: int = Field(default=1, ge=1)
    cost_estimate: CampaignCostEstimate | None = None
    execution: CampaignExecutionPolicy | None = None
    tasks: list[CampaignTaskAllowance] = Field(min_length=1)
    concurrency: int = Field(default=1, ge=1, exclude_if=lambda value: value == 1)
    adaptive_sampling: CampaignAdaptiveSampling | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    reference_profile: str = Field(min_length=1)
    allowed_deviations: list[ReferenceDeviation] = Field(default_factory=list)
    require_egress_lock: bool = True
    ceiling_floor: CampaignCeilingFloor = Field(default_factory=CampaignCeilingFloor)
    agent: str = Field(min_length=1)
    model: str | None = None
    environment: str = Field(min_length=1)
    sampling: CampaignSampling = Field(default_factory=CampaignSampling)
    queue_cwd: str = Field(min_length=1)
    linear_card: str = Field(min_length=1)
    submitted_by: str = Field(min_length=1)
    created_at: datetime

    @field_validator("linear_card")
    @classmethod
    def card_is_explicit(cls, value: str) -> str:
        normalized = normalize_linear_card(value)
        if normalized is None:
            raise ValueError("linear_card must be a HAR issue identifier")
        return normalized

    @model_validator(mode="after")
    def tasks_are_unique(self) -> ExperimentCampaign:
        task_ids = [item.task_id for item in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("campaign task allowlist must not repeat a task_id")
        return self


class CampaignApprovalRecord(_FrozenContract):
    """One append-only, attributable campaign approval (the once-approval)."""

    schema_version: Literal["experiment-campaign-approval/v1"] = SCHEMA_APPROVAL
    campaign_id: str = Field(min_length=1, max_length=80)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actor: str = Field(min_length=1)
    approved_at: datetime


class CampaignEscalation(_FrozenContract):
    """One escalation to Research-Harbor: event plus lin comment."""

    schema_version: Literal["experiment-campaign-escalation/v1"] = SCHEMA_ESCALATION
    campaign_id: str = Field(min_length=1, max_length=80)
    spec_id: str | None = None
    reason_code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    card: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    occurred_at: datetime
    lin_posted: bool = False


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def campaign_content_digest(campaign: ExperimentCampaign | Mapping[str, Any]) -> str:
    """Digest-pin the campaign content (HAR-175 immutability anchor)."""
    payload = (
        campaign.model_dump(mode="json", exclude_none=True)
        if isinstance(campaign, ExperimentCampaign)
        else json.loads(json.dumps(campaign))
    )
    # The new default preserves approvals signed before this pin existed.
    # Non-default attempts remain immutable, approvable campaign content.
    if payload.get("attempts_per_task") == 1:
        payload.pop("attempts_per_task")
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def campaign_dir(repo_root: Path, campaign_id: str) -> Path:
    """State directory for one campaign; the id spelling cannot escape it."""
    if not campaign_id or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in campaign_id
    ):
        raise CampaignApprovalError(
            REASON_UNKNOWN, f"campaign id is not a simple repository identifier: {campaign_id!r}"
        )
    return repo_root.resolve() / CAMPAIGN_STATE_ROOT / campaign_id


def campaigns_root(repo_root: Path) -> Path:
    return repo_root.resolve() / CAMPAIGN_STATE_ROOT


def write_campaign(repo_root: Path, campaign: ExperimentCampaign) -> Path:
    """Freeze a draft campaign file. Refused once the campaign is approved."""
    target_dir = campaign_dir(repo_root, campaign.campaign_id)
    approvals_path = target_dir / APPROVALS_FILENAME
    if approvals_path.is_file() and approvals_path.stat().st_size > 0:
        raise CampaignApprovalError(
            REASON_CONTENT_CHANGED,
            f"campaign {campaign.campaign_id} is already approved; its content is immutable",
        )
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / CAMPAIGN_FILENAME
    path.write_text(campaign.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_campaign(repo_root: Path, campaign_id: str) -> ExperimentCampaign:
    """Load a frozen campaign file (approval checked separately)."""
    path = campaign_dir(repo_root, campaign_id) / CAMPAIGN_FILENAME
    try:
        return ExperimentCampaign.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CampaignApprovalError(
            REASON_UNKNOWN, f"no campaign file for {campaign_id!r}"
        ) from exc
    except ValueError as exc:
        raise CampaignApprovalError(
            REASON_CONTENT_CHANGED,
            f"campaign file for {campaign_id!r} is not a valid campaign: {exc}",
        ) from exc
    except OSError as exc:
        raise CampaignApprovalError(
            REASON_CONTENT_CHANGED,
            f"campaign file for {campaign_id!r} cannot be read: {exc}",
        ) from exc


def read_approvals(repo_root: Path, campaign_id: str) -> list[CampaignApprovalRecord]:
    """Every recorded approval for one campaign (append-only log)."""
    path = campaign_dir(repo_root, campaign_id) / APPROVALS_FILENAME
    if not path.is_file():
        return []
    records: list[CampaignApprovalRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(CampaignApprovalRecord.model_validate_json(line))
    return records


def approve_campaign(
    repo_root: Path,
    campaign_id: str,
    *,
    actor: str,
    approved_at: datetime | None = None,
) -> CampaignApprovalRecord:
    """Record the once-approval by a named delegate (Research-Harbor).

    The approval binds the content digest: later content changes invalidate
    it. A second approval is refused; approval is once per campaign.
    """
    if not actor.strip():
        raise CampaignApprovalError(
            REASON_CONTENT_CHANGED, "approval actor is required and never defaulted"
        )
    campaign = load_campaign(repo_root, campaign_id)
    if campaign.execution is not None and campaign.execution.qualification is not None:
        validate_qualification(campaign.execution.qualification, repo_root=repo_root)
    existing = read_approvals(repo_root, campaign_id)
    if existing:
        raise CampaignApprovalError(
            REASON_CONTENT_CHANGED,
            f"campaign {campaign_id} was already approved by {existing[0].actor} "
            f"at {existing[0].approved_at.isoformat()}; approval is once per campaign",
        )
    record = CampaignApprovalRecord(
        campaign_id=campaign.campaign_id,
        content_digest=campaign_content_digest(campaign),
        actor=actor,
        approved_at=approved_at or datetime.now(UTC),
    )
    path = campaign_dir(repo_root, campaign_id) / APPROVALS_FILENAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record.model_dump_json() + "\n")
    return record


def load_approved_campaign(
    repo_root: Path, campaign_id: str
) -> tuple[ExperimentCampaign, CampaignApprovalRecord]:
    """Load a campaign plus its once-approval, verifying the digest pin."""
    campaign = load_campaign(repo_root, campaign_id)
    approvals = read_approvals(repo_root, campaign_id)
    if not approvals:
        raise CampaignApprovalError(
            REASON_NOT_APPROVED,
            f"campaign {campaign_id} has no recorded approval; "
            "Research-Harbor approves once with "
            f"`uv run evallab campaign approve {campaign_id} --actor <delegate>`",
        )
    approval = approvals[0]
    if approval.content_digest != campaign_content_digest(campaign):
        raise CampaignApprovalError(
            REASON_CONTENT_CHANGED,
            f"campaign {campaign_id} content changed after {approval.actor}'s approval; "
            "the approval no longer binds this content (gate defect)",
        )
    if approval.campaign_id != campaign.campaign_id:
        raise CampaignApprovalError(
            REASON_CONTENT_CHANGED,
            f"campaign {campaign_id} approval names {approval.campaign_id} (gate defect)",
        )
    return campaign, approval


def intended_sampling(agent: str | None, model: str | None) -> CampaignSampling:
    """Pinned sampling for an agent+model, from the same source preflight prints.

    Mirrors ``setup_fingerprint._setup_fields``: the mimoagent route sends
    ``mimoagent_worker.SAMPLING`` (temperature 1.0), every other route sends
    ``sampling_sent(model)``. A model-only comparison is wrong for mimoagent.
    """
    if agent == "mimoagent":
        from evallab.mimoagent_worker import SAMPLING

        return CampaignSampling(
            temperature=float(SAMPLING["temperature"]),
            top_p=float(SAMPLING["top_p"]),
            top_k=int(SAMPLING["top_k"]),
        )
    from evallab.setup_fingerprint import sampling_sent

    sent = sampling_sent(model)
    return CampaignSampling(
        temperature=sent.get("temperature"),
        top_p=sent.get("top_p"),
        top_k=sent.get("top_k"),
    )


def check_campaign_admission(
    spec: ExperimentSpec,
    campaign: ExperimentCampaign,
    *,
    repo_root: Path | None,
    committed_usd: float,
    defer_sampling: bool = False,
) -> PolicyDecision:
    """Pure campaign admission check: every mismatch has its own reason.

    ``committed_usd`` is the campaign spend excluding the candidate spec
    (reservations plus settled actuals); the caller computes it from the
    queue so this stays a pure comparison.
    """
    rule = f"campaign:{campaign.campaign_id}"
    if repo_root is None:
        return PolicyDecision(
            admitted=False,
            reason_code=REASON_GATE_UNCONFIGURED,
            message="campaign gate has no repository root; refusing closed",
        )
    resolved_root = repo_root.resolve()
    try:
        approved_cwd = str(Path(campaign.queue_cwd).resolve())
    except (OSError, ValueError):
        approved_cwd = campaign.queue_cwd
    if approved_cwd != str(resolved_root):
        return PolicyDecision(
            admitted=False,
            reason_code=REASON_QUEUE_MISMATCH,
            message=(
                f"spec claims campaign {campaign.campaign_id}, which approves queue "
                f"{campaign.queue_cwd}, not {resolved_root}"
            ),
        )
    if (spec.reference_profile or None) != campaign.reference_profile:
        return PolicyDecision(
            admitted=False,
            reason_code=REASON_PROFILE_MISMATCH,
            message=(
                f"spec reference_profile {spec.reference_profile!r} does not match "
                f"campaign {campaign.campaign_id} reference {campaign.reference_profile!r}"
            ),
        )
    if (
        spec.agent != campaign.agent
        or (spec.model or None) != (campaign.model or None)
        or spec.environment != campaign.environment
    ):
        return PolicyDecision(
            admitted=False,
            reason_code=REASON_SETUP_MISMATCH,
            message=(
                f"spec setup {spec.agent}/{spec.model}/{spec.environment} does not "
                f"match campaign {campaign.campaign_id} setup "
                f"{campaign.agent}/{campaign.model}/{campaign.environment}"
            ),
        )
    actual_sampling = intended_sampling(spec.agent, spec.model)
    if actual_sampling != campaign.sampling:
        for field in ("temperature", "top_p", "top_k"):
            expected = getattr(campaign.sampling, field)
            actual = getattr(actual_sampling, field)
            if actual != expected:
                return PolicyDecision(
                    admitted=False,
                    reason_code=REASON_SETUP_MISMATCH,
                    message=(
                        f"spec sampling.{field} {actual!r} does not match campaign "
                        f"{campaign.campaign_id} sampling {expected!r}"
                    ),
                )
    allowed = {(item.field, _freeze(item.value)) for item in campaign.allowed_deviations}
    for deviation in spec.deviations:
        if (deviation.field, _freeze(deviation.value)) not in allowed:
            return PolicyDecision(
                admitted=False,
                reason_code=REASON_SETUP_MISMATCH,
                message=(
                    f"spec deviation for {deviation.field!r} ({deviation.value!r}) is not "
                    f"on campaign {campaign.campaign_id}'s allowed list"
                ),
            )
    if campaign.require_egress_lock and spec.egress_lock is not True:
        return PolicyDecision(
            admitted=False,
            reason_code=REASON_LOCK_MISSING,
            message=(
                f"campaign {campaign.campaign_id} requires the egress lock; spec leaves "
                f"egress_lock {spec.egress_lock!r} (set egress_lock: true explicitly)"
            ),
        )
    allowance = next(
        (item for item in campaign.tasks if item.task_id == (spec.task_id or "")),
        None,
    )
    if allowance is None:
        return PolicyDecision(
            admitted=False,
            reason_code=REASON_TASK_NOT_ALLOWED,
            message=(
                f"spec task {spec.task_id!r} is not on campaign {campaign.campaign_id}'s "
                "allowed task list"
            ),
        )
    if spec.task_package_digest != allowance.package_digest:
        return PolicyDecision(
            admitted=False,
            reason_code=REASON_TASK_DIGEST_MISMATCH,
            message=(
                f"spec task package {spec.task_package_digest!r} does not match campaign "
                f"{campaign.campaign_id} digest {allowance.package_digest} "
                f"for task {allowance.task_id}"
            ),
        )
    floor = campaign.ceiling_floor
    for attribute in (
        "max_requests",
        "max_input_tokens",
        "max_output_tokens",
        "max_total_tokens",
        "cost_limit_usd",
    ):
        minimum = getattr(floor, attribute)
        if minimum is None:
            continue
        actual = getattr(spec, attribute)
        if actual is None or actual < minimum:
            return PolicyDecision(
                admitted=False,
                reason_code=REASON_CEILING_BELOW_FLOOR,
                message=(
                    f"spec {attribute} {actual!r} binds before campaign "
                    f"{campaign.campaign_id} floor {minimum!r}; no ceiling may bind "
                    "before the reference limits"
                ),
            )
    if campaign.adaptive_sampling is not None:
        sampling_refusal = adaptive_admission_refusal(spec, campaign, resolved_root)
        if sampling_refusal is not None:
            return sampling_refusal
        if defer_sampling:
            return _sampling_refusal(
                REASON_SAMPLING_QUEUED,
                "adaptive draw staged; the tick admits tasks in seeded priority order",
            )
    estimate = _reservation_usd(spec.model_dump(mode="json"), campaign)
    if committed_usd >= campaign.budget_usd or committed_usd + estimate > campaign.budget_usd:
        return PolicyDecision(
            admitted=False,
            reason_code=REASON_BUDGET_EXHAUSTED,
            message=(
                f"campaign {campaign.campaign_id} budget ${campaign.budget_usd:.2f} "
                f"exhausted: committed ${committed_usd:.2f} plus estimated "
                f"${estimate:.2f} overruns it"
            ),
        )
    return PolicyDecision(
        admitted=True,
        policy_rule=rule,
        message=(
            f"admitted by campaign {campaign.campaign_id} approval "
            f"({campaign.reference_profile}, locked, budget ${campaign.budget_usd:.2f})"
        ),
    )


def _sampling_refusal(code: str, message: str) -> PolicyDecision:
    return PolicyDecision(admitted=False, reason_code=code, message=message)


def _task_csv(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    rows: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            task_id = row.get("task_id") or row.get("task")
            if not task_id or task_id in rows:
                raise ValueError(f"missing or duplicate task in {path}")
            rows[task_id] = row
    return rows


def oracle_confirmation(repo_root: Path, task_id: str, package_digest: str) -> tuple[bool, str]:
    """Confirm an explicit oracle+nop label, never infer one from solve tags.

    HAR-191 sweep rows supersede pilots. If the sweep is absent, the same
    ledger/control sources are used; sound nop or model passes alone do not
    establish a reference solution. Only exact packages or the existing
    verified metadata/verifier-only lineage may inherit a ledger label.
    """
    from evallab.setup_fingerprint import lineage_ledger_binding
    from evallab.task_health_tags import LEDGER, LOCKED_NOP, health_tag

    try:
        ledger = _task_csv(repo_root / LEDGER).get(task_id)
        if ledger is None:
            return False, "no ledger row"
        base = repo_root / "research/experiments/python-task-ledger"
        controls = _task_csv(base / "oracle_pilot.csv")
        controls.update(_task_csv(base / "oracle_sweep.csv"))
        row = controls.get(task_id)
        if row is None or row.get("label") != "oracle:pass+nop:fail":
            return (
                False,
                f"no confirmed oracle:pass+nop:fail label ({(row or {}).get('label', 'absent')})",
            )
        if not (row.get("evidence") or row.get("evidence_path")):
            return False, "oracle label has no evidence reference"
        control_digest = row.get("run_digest")
        if not control_digest:
            return False, "oracle label has no package-bound run_digest"
        if control_digest == ledger.get("run_digest"):
            locked = _task_csv(repo_root / LOCKED_NOP).get(task_id)
            health = health_tag(ledger, locked)
            if health not in {"health:sound", "health:repaired", "health:unchecked"}:
                return False, f"adverse evidence on oracle package: {health}"
        if control_digest == package_digest:
            return True, "oracle:pass+nop:fail confirmed for exact package"
        if control_digest != ledger.get("run_digest"):
            return False, "oracle control digest differs from current ledger package"
        binding = lineage_ledger_binding(repo_root, task_id, package_digest)
        if binding["admitted"]:
            return True, "oracle:pass+nop:fail inherited through verified scoring/metadata lineage"
        return False, f"oracle label does not bind this package: {binding['reason']}"
    except (OSError, ValueError, KeyError) as exc:
        return False, f"oracle evidence unreadable: {type(exc).__name__}: {exc}"


def _campaign_queue_specs(repo_root: Path, campaign_id: str) -> list[tuple[str, ExperimentSpec]]:
    rows = []
    for state in QUEUE_STATES:
        for path in sorted((repo_root / "queue" / state).glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("campaign_id") != campaign_id:
                    continue
                spec = ExperimentSpec.model_validate(raw)
            except (OSError, ValueError):
                continue
            rows.append((state, spec))
    return rows


def _sample_outcome(repo_root: Path, spec: ExperimentSpec) -> bool | None:
    from evallab.results import load_job
    from evallab.step_layers import classify_stop_reason

    candidate = (repo_root / (spec.jobs_dir or "runs") / spec.name).resolve()
    if not candidate.is_relative_to(repo_root.resolve()):
        return None
    try:
        job = load_job(candidate)
        if len(job.trials) != 1:
            return None
        trial = job.trials[0]
        agent = trial.result.get("agent_result") or {}
        metadata = agent.get("metadata") or {}
        stop, _ = classify_stop_reason(
            agent_metadata=metadata, exception_info=trial.result.get("exception_info")
        )
        if stop == "context_exhausted":
            return False
        if trial_is_infra_excluded(trial.result, trial.rewards):
            return None
        reward = trial.rewards.get("reward_gated", trial.rewards.get("reward"))
        return reward == 1 and trial.rewards.get("integrity", 1) == 1
    except (OSError, ValueError, KeyError):
        return None


def adaptive_task_outcomes(
    repo_root: Path, campaign: ExperimentCampaign, task_id: str
) -> list[bool]:
    """Contiguous settled draws; infra consumes no scientific attempt."""
    rows = _campaign_queue_specs(repo_root, campaign.campaign_id)
    originals: dict[int, tuple[str, ExperimentSpec]] = {}
    for state, spec in sorted(
        rows,
        key=lambda row: (
            row[1].submitted_at or datetime.max.replace(tzinfo=UTC),
            str(row[1].spec_id),
        ),
    ):
        index = spec.campaign_task_attempt
        if (
            spec.task_id == task_id
            and spec.campaign_replaces is None
            and index is not None
            and state != "rejected"
        ):
            originals.setdefault(index, (state, spec))
    outcomes: list[bool] = []
    for index in range(1, campaign.attempts_per_task + 1):
        entry = originals.get(index)
        if entry is None or entry[0] not in SETTLED_STATES:
            break
        outcome = _sample_outcome(repo_root, entry[1])
        if outcome is None:
            replacements = [
                (state, spec) for state, spec in rows if spec.campaign_replaces == entry[1].spec_id
            ]
            if len(replacements) != 1 or replacements[0][0] not in SETTLED_STATES:
                break
            outcome = _sample_outcome(repo_root, replacements[0][1])
        if outcome is None:
            break
        outcomes.append(outcome)
    return outcomes


def adaptive_admission_refusal(
    spec: ExperimentSpec, campaign: ExperimentCampaign, repo_root: Path
) -> PolicyDecision | None:
    policy = campaign.adaptive_sampling
    assert policy is not None
    confirmed, reason = oracle_confirmation(
        repo_root, spec.task_id or "", spec.task_package_digest or ""
    )
    if not confirmed:
        return _sampling_refusal(REASON_ORACLE_UNCONFIRMED, reason)
    index = spec.campaign_task_attempt
    if index is None or index > campaign.attempts_per_task or spec.attempts != 1:
        return _sampling_refusal(
            REASON_SAMPLING_INVALID,
            "adaptive campaigns require one native draw and an in-range campaign_task_attempt",
        )
    rows = _campaign_queue_specs(repo_root, campaign.campaign_id)
    peers = [
        other
        for _, other in rows
        if other.task_id == spec.task_id
        and other.campaign_task_attempt == index
        and other.spec_id != spec.spec_id
        and other.campaign_replaces is None
    ]
    first = min(
        [spec, *peers],
        key=lambda candidate: (
            candidate.submitted_at or datetime.max.replace(tzinfo=UTC),
            str(candidate.spec_id),
        ),
    )
    if spec.campaign_replaces is None and first.spec_id != spec.spec_id:
        return _sampling_refusal(REASON_SAMPLING_INVALID, "task draw already staged")
    if spec.campaign_replaces is not None:
        original = next(
            (
                other
                for state, other in rows
                if other.spec_id == spec.campaign_replaces and state in SETTLED_STATES
            ),
            None,
        )
        if (
            original is None
            or original.campaign_replaces is not None
            or (
                original.task_id != spec.task_id
                or original.campaign_task_attempt != index
                or original.task_package_digest != spec.task_package_digest
            )
            or not _job_has_infra_excluded_trial(repo_root, original)
        ):
            return _sampling_refusal(
                REASON_SAMPLING_INVALID, "replacement lacks matching infra-excluded original"
            )
        if any(
            other.campaign_replaces == original.spec_id and other.spec_id != spec.spec_id
            for _, other in rows
        ):
            return _sampling_refusal(
                REASON_SAMPLING_INVALID, "infra draw already has its one replacement"
            )
    outcomes = adaptive_task_outcomes(repo_root, campaign, spec.task_id or "")
    band, confidence = adaptive_band(outcomes, campaign.attempts_per_task, policy.target_confidence)
    if band is not None:
        return _sampling_refusal(
            REASON_SAMPLING_CLASSIFIED,
            f"task band {band} known at confidence {confidence:.6f} after {len(outcomes)} draws",
        )
    if index != len(outcomes) + 1:
        return _sampling_refusal(
            REASON_SAMPLING_PENDING, "preceding task draw or its infra replacement is not settled"
        )
    return None


def adaptive_priority(
    repo_root: Path, campaign: ExperimentCampaign, spec: ExperimentSpec
) -> tuple[float, float, str]:
    """Cheap information first; explicit historical estimates beat fallback estimates.

    Task-history uncertainty only orders tasks: it never certifies capability
    on this model/package. Seeded hashes break ties, independent of submission
    IDs. Settled uncertain follow-ups precede unstarted tasks' first draws.
    """
    allowance = next(item for item in campaign.tasks if item.task_id == spec.task_id)
    history_path = repo_root / "research/experiments/python-task-ledger/task_history.csv"
    history = _task_csv(history_path).get(spec.task_id or "", {})
    a, b = 1 + int(history.get("clean_pass") or 0), 1 + int(history.get("fail") or 0)
    uncertainty = a * b / ((a + b) ** 2 * (a + b + 1))
    cost = allowance.expected_cost_usd or float(spec.est_cost_usd or 1.0)
    wall = allowance.expected_wall_seconds or float(spec.timeout_seconds)
    seed = campaign.adaptive_sampling.seed if campaign.adaptive_sampling else 0
    tie = hashlib.sha256(f"{seed}:{spec.task_id}".encode()).hexdigest()
    return cost / uncertainty, wall, tie


def reconcile_adaptive_sampling(
    executor: Any, *, parallel: int, spec_ids: set[str] | None = None
) -> None:
    """Release a priority wave under the tick lock, using existing admission.

    Wave width is capped by campaign concurrency, executor capacity and the
    HAR-163 Daytona allowance. The existing HAR-189 gate reserves each draw;
    no alternate accounting is introduced. Follow-ups precede unstarted
    tasks, so budget stops leave at most one wave partially informative.
    """
    root = executor.repo_root
    waiting = executor.queue.list_specs("waiting")
    for path, spec in executor.queue.list_specs("approved"):
        if spec.campaign_id is None or (spec_ids is not None and str(spec.spec_id) not in spec_ids):
            continue
        try:
            campaign, _ = load_approved_campaign(root, spec.campaign_id)
        except CampaignApprovalError:
            continue
        if campaign.adaptive_sampling is not None:
            destination = executor.queue.transition(
                path,
                "waiting",
                actor="adaptive-sampling",
                event="policy_waiting",
                reason_code=REASON_SAMPLING_QUEUED,
            )
            waiting.append((destination, spec))
    if spec_ids is not None:
        waiting = [(path, spec) for path, spec in waiting if str(spec.spec_id) in spec_ids]
    campaign_ids = sorted({spec.campaign_id for _, spec in waiting if spec.campaign_id})
    for campaign_id in campaign_ids:
        try:
            campaign, _ = load_approved_campaign(root, campaign_id)
        except CampaignApprovalError:
            continue
        if campaign.adaptive_sampling is None:
            continue
        candidates = []
        for path, spec in waiting:
            if spec.campaign_id != campaign_id:
                continue
            refusal = adaptive_admission_refusal(spec, campaign, root)
            if refusal is not None:
                executor.queue.write_reason(spec, refusal)
                if refusal.reason_code == REASON_SAMPLING_CLASSIFIED:
                    executor.queue.transition(
                        path,
                        "rejected",
                        actor="adaptive-sampling",
                        event="sampling_skipped",
                        reason_code=refusal.reason_code,
                    )
                continue
            outcomes = adaptive_task_outcomes(root, campaign, spec.task_id or "")
            candidates.append(
                (
                    not bool(outcomes or spec.campaign_replaces),
                    adaptive_priority(root, campaign, spec),
                    path,
                    spec,
                )
            )
        if not candidates:
            continue
        ordered = sorted(candidates, key=lambda item: (item[0], item[1]))
        width = min(campaign.concurrency, parallel)
        batch = [(item[2], item[3]) for item in ordered[:width]]
        batch = executor._apply_daytona_clamp(executor._capacity_batch(batch))
        released = 0
        for path, spec in batch:
            decision = executor.gate.decide(
                spec,
                spent_today_usd=executor._effective_spend_today() if spec.billable else 0,
                consecutive_harness_failures=executor._consecutive_harness_failures()
                if spec.billable
                else 0,
            )
            if decision.admitted:
                executor.queue._replace_model(
                    path, spec.model_copy(update={"policy_rule": decision.policy_rule})
                )
                executor.queue.transition(
                    path,
                    "approved",
                    actor="adaptive-sampling",
                    event="policy_admitted",
                    policy_rule=decision.policy_rule,
                )
                released += 1
            else:
                executor.queue.write_reason(spec, decision)
                if decision.reason_code == REASON_BUDGET_EXHAUSTED:
                    if released == 0:
                        escalate_after_refusal(root, spec, decision)
                        executor.queue.stop()
                    break


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _usd(value: Any) -> float | None:
    """A usable cost, preserving unknown/invalid amounts rather than zeroing."""
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return amount if math.isfinite(amount) and amount >= 0 else None


def _campaign_trial_count(campaign: ExperimentCampaign) -> int:
    """Approved task count times the explicit attempts pin, never inferred."""
    return len(campaign.tasks) * campaign.attempts_per_task


def _wave_reservation_per_trial(campaign: ExperimentCampaign) -> float:
    """Average expected wave cost, preserving full-width concurrency.

    Reserving HAR-168's $0.60 per trial would cap a $5 campaign at eight
    concurrent trials and force extra GPU waves. The expected per-trial cost
    instead admits the model's intended wave while measured actuals and the
    printed in-flight overrun bound keep the fence conservative.
    """
    trials = _campaign_trial_count(campaign)
    if campaign.execution is not None and campaign.cost_estimate is not None:
        # A separately approved HAR-192 all-in envelope replaces only the
        # historical Daytona/A100 planning input, not the reservation mechanism.
        return campaign.cost_estimate.expected_usd / trials
    return wave_cost_estimate(trials).expected_usd / trials


def _reservation_usd(raw: Mapping[str, Any], campaign: ExperimentCampaign) -> float:
    """Reserve wave-model expected cost per trial, times spec attempts.

    A larger declared estimate wins. Self-hosted and zero-estimate Daytona
    specs use the campaign wave average; positive non-selfhosted estimates
    retain the existing reservation policy.
    """
    from evallab.modal_ops import is_selfhosted_model

    estimate = _usd(raw.get("est_cost_usd")) or 0.0
    if (
        is_selfhosted_model(raw.get("model"))
        or (raw.get("environment") == "daytona" and estimate == 0)
        # Locked Docker rents a shared VM even for model-free controls.
        or (
            raw.get("environment") == "docker"
            and campaign.execution is not None
            and campaign.cost_estimate is not None
        )
    ):
        attempts = raw.get("attempts", 1)
        count = attempts if isinstance(attempts, int) and attempts > 0 else 1
        return count * max(estimate, _wave_reservation_per_trial(campaign))
    return estimate


def campaign_budget_bound(
    repo_root: Path,
    campaign_id: str,
    *,
    campaign: ExperimentCampaign | None = None,
) -> dict[str, float | int]:
    """Printable overrun for reservations below the per-trial worst ceiling.

    In-flight reservations use expected wave cost so concurrency is not
    throttled. Conditional on trials settling within HAR-168's $0.60 resource
    envelope (or a larger declared ceiling), the extra exposure is the sum of
    positive ``worst_ceiling - reservation`` gaps for all in-flight trials.
    """
    loaded = campaign if campaign is not None else load_campaign(repo_root, campaign_id)
    if loaded.campaign_id != campaign_id:
        raise ValueError("campaign draft does not match campaign_id")
    specs = _queue_meter_specs(repo_root.resolve())
    trials = 0
    reserved = worst = gap = 0.0
    for state, raw in specs:
        if raw.get("campaign_id") != campaign_id or state not in RESERVED_STATES:
            continue
        attempts = raw.get("attempts", 1)
        count = attempts if isinstance(attempts, int) and attempts > 0 else 1
        trials += count
        hold = _reservation_usd(raw, loaded)
        ceiling = count * max(HAR168_TRIAL_CEILING_USD, _usd(raw.get("cost_limit_usd")) or 0.0)
        reserved += hold
        worst += ceiling
        gap += max(0.0, ceiling - hold)
    return {
        "in_flight_trials": int(trials),
        "in_flight_reserved_usd": reserved,
        "in_flight_worst_case_usd": worst,
        "overrun_bound_usd": gap,
        "realized_spend_bound_usd": loaded.budget_usd + gap,
    }


def _read_meter_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _queue_meter_specs(root: Path) -> list[tuple[str, dict[str, Any]]]:
    return [
        (state, raw)
        for state in QUEUE_STATES
        for path in sorted((root / "queue" / state).glob("*.json"))
        if (raw := _read_meter_json(path))
    ]


def _meter_job_path(root: Path, raw: Mapping[str, Any]) -> Path | None:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return None
    candidate = (root / str(raw.get("jobs_dir") or "runs") / name).resolve()
    return candidate if root in candidate.parents else None


def _trial_identity(
    job_result: Mapping[str, Any], trial_result: Mapping[str, Any], path: Path
) -> str:
    job_id, trial_id = job_result.get("id"), trial_result.get("id")
    if job_id and trial_id:
        return f"{job_id}/{trial_id}"
    return path.resolve().as_posix()


def _meter_model_host(root: Path, raw: Mapping[str, Any]) -> str:
    """Keep new Runpod/unknown campaigns out of the historical Modal bill pool."""
    campaign_id = raw.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id:
        return "modal"
    try:
        campaign = load_campaign(root, campaign_id)
    except (OSError, ValueError):
        return "unknown"
    return campaign.execution.model_host if campaign.execution is not None else "modal"


def _modal_allocations(
    root: Path, specs: list[tuple[str, dict[str, Any]]], jobs: list[JobRecord]
) -> dict[str, tuple[float, bool]]:
    """Split one app's UTC-day bill by summed trial/day wall-time overlaps.

    Startup, warm and idle time stay in the billed pool. Concurrent trials
    each contribute their overlap seconds; the denominator includes other
    campaigns and unclaimed jobs, not just the campaign being metered.
    Native identities deduplicate copies retained in sibling worktrees.
    Only existing read-only billing/report and evidence readers are used;
    an absent/lagging/failed report leaves the trial's reservation in place.
    """
    from evallab.modal_billing import fetch_modal_billing_report
    from evallab.modal_ops import MODAL_APP_NAME, is_selfhosted_model
    from evallab.results import discover_job_dirs
    from evallab.spend_day import (
        day_to_window,
        parse_dt,
        sibling_worktree_roots,
        window_overlap_seconds,
    )

    targets: dict[str, tuple[datetime, datetime]] = {}
    for job in jobs:
        for trial in job.trials:
            started, finished = (
                parse_dt(trial.result.get("started_at")),
                parse_dt(trial.result.get("finished_at")),
            )
            if started is not None and finished is not None and finished > started:
                targets[_trial_identity(job.result, trial.result, trial.path)] = (started, finished)
    if not targets:
        return {}
    first = min(start for start, _ in targets.values()).date()
    last = max(finish for _, finish in targets.values()).date() + timedelta(days=1)
    try:
        billing = fetch_modal_billing_report(start=first, end=last, repo_root=root, resolution="d")
    except Exception:
        return {}
    # One daily report only: daily and hourly reports are alternatives, never
    # additive sources. Repeated copies of a row cannot double the app pool.
    billed: dict[tuple[str, str, date], float] = {}
    for row in billing:
        cost = _usd(row.cost_usd)
        moment = row.interval_start
        if isinstance(moment, datetime):
            moment = moment.replace(tzinfo=moment.tzinfo or UTC).astimezone(UTC)
        else:
            moment = parse_dt(moment)
        if row.description != MODAL_APP_NAME or cost is None or moment is None:
            continue
        day = moment.astimezone(UTC).date()
        if first <= day < last:
            key = (row.object_id, row.resource, day)
            billed[key] = max(billed.get(key, 0.0), cost)
    if not billed:
        return {}
    pools: dict[date, float] = {}
    for (_, _, day), cost in billed.items():
        pools[day] = pools.get(day, 0.0) + cost
    paths = {job.path for job in jobs}
    models_by_path: dict[Path, Any] = {}
    roots_by_path = {job.path: root for job in jobs}
    hosts_by_path: dict[Path, str] = {}
    for other_root in [root, *sibling_worktree_roots(root)]:
        discovered = list(discover_job_dirs([other_root / "runs"]))
        paths.update(discovered)
        roots_by_path.update(dict.fromkeys(discovered, other_root))
        other_specs = specs if other_root == root else _queue_meter_specs(other_root)
        for _, raw in other_specs:
            candidate = _meter_job_path(other_root, raw)
            if candidate is not None:
                paths.add(candidate)
                models_by_path[candidate] = raw.get("model")
                roots_by_path[candidate] = other_root
                hosts_by_path[candidate] = _meter_model_host(other_root, raw)
    windows = dict(targets)
    now = datetime.now(UTC)
    for job_path in paths:
        config = _read_meter_json(job_path / "config.json")
        spec = _read_meter_json(job_path / "experiment-spec.json")
        host = hosts_by_path.get(job_path) or _meter_model_host(
            roots_by_path.get(job_path, root), spec
        )
        if host != "modal":
            continue
        agent = config.get("agent")
        model = (
            (agent.get("model_name") if isinstance(agent, Mapping) else None)
            or spec.get("model")
            or models_by_path.get(job_path)
        )
        if not is_selfhosted_model(model):
            continue
        job_result = _read_meter_json(job_path / "result.json")
        for result_path in job_path.glob("*/result.json"):
            result = _read_meter_json(result_path)
            if "trial_name" not in result:
                continue
            start = parse_dt(result.get("started_at"))
            finish = parse_dt(result.get("finished_at")) or now
            if start is not None and finish > start:
                windows[_trial_identity(job_result, result, result_path.parent)] = (start, finish)
    shares: dict[str, float] = {}
    for day, pool in pools.items():
        window_start, window_end = day_to_window(day)
        weights = {
            identity: window_overlap_seconds(start, finish, window_start, window_end)
            for identity, (start, finish) in windows.items()
        }
        denominator = math.fsum(weights.values())
        if denominator > 0:
            for identity, weight in weights.items():
                if identity in targets and weight > 0:
                    shares[identity] = shares.get(identity, 0.0) + pool * weight / denominator
    # Every day touched by a trial must be present: a partially available bill
    # cannot silently turn its missing days into free GPU time.
    incomplete: set[str] = set()
    for identity, (start, finish) in targets.items():
        day = start.astimezone(UTC).date()
        while day < last:
            window_start, window_end = day_to_window(day)
            if (
                window_overlap_seconds(start, finish, window_start, window_end) > 0
                and day not in pools
            ):
                incomplete.add(identity)
                break
            if window_end >= finish:
                break
            day += timedelta(days=1)
    return {identity: (amount, identity not in incomplete) for identity, amount in shares.items()}


def _daytona_trial_usd(job: JobRecord, trial: TrialRecord) -> float | None:
    """Rate recorded sandbox resources and wall time through spend day's card."""
    from evallab.spend_day import _read_task_toml_env, parse_dt, trial_daytona_resources
    from evallab.task_qualification import estimate_cost_usd

    start, finish = (
        parse_dt(trial.result.get("started_at")),
        parse_dt(trial.result.get("finished_at")),
    )
    if start is None or finish is None or finish <= start:
        return None
    usage = _read_meter_json(trial.path / "daytona-usage.json") or _read_meter_json(
        job.path / "daytona-usage.json"
    )
    admission = usage.get("admission")
    requested = admission.get("requested") if isinstance(admission, Mapping) else None
    requested = requested if isinstance(requested, Mapping) else {}
    usage_env: dict[str, Any] = {}
    for source, target, multiplier in (
        ("cpu", "override_cpus", 1),
        ("memory_gib", "override_memory_mb", 1024),
        ("disk_gib", "override_storage_mb", 1024),
    ):
        value = _usd(requested.get(source))
        if value is not None and value > 0:
            usage_env[target] = int(value * multiplier)
    task = trial.lock.get("task") or trial.config.get("task") or {}
    task_path = task.get("path") if isinstance(task, Mapping) else None
    # Recorded usage wins over requested overrides and task-family fallbacks.

    task_name = str(trial.result.get("task_name") or "")
    (cpus, memory, storage), _basis = trial_daytona_resources(
        task_family=task_name.split("/")[0] if "/" in task_name else None,
        environment_configs=(job.config, trial.config, {"environment": usage_env}),
        task_toml_env=_read_task_toml_env(task_path),
    )
    if cpus is None or memory is None:
        return None
    return _usd(
        estimate_cost_usd(
            backend="daytona",
            sandbox_seconds=(finish - start).total_seconds(),
            cpus=cpus,
            memory_mb=memory,
            storage_mb=storage,
        )
    )


def _campaign_meter(
    repo_root: Path,
    campaign_id: str,
    *,
    exclude_spec_id: str | None = None,
    campaign: ExperimentCampaign | None = None,
) -> tuple[float, float, dict[str, float]]:
    from evallab.database import trial_cost_columns
    from evallab.dispatch_guards import MODEL_FREE_AGENTS
    from evallab.modal_ops import is_selfhosted_model

    root = repo_root.resolve()
    loaded = campaign if campaign is not None else load_campaign(root, campaign_id)
    if loaded.campaign_id != campaign_id:
        raise ValueError("campaign draft does not match campaign_id")
    specs = _queue_meter_specs(root)
    relevant = [
        (state, raw)
        for state, raw in specs
        if raw.get("campaign_id") == campaign_id
        and (exclude_spec_id is None or raw.get("spec_id") != exclude_spec_id)
        and state in RESERVED_STATES | SETTLED_STATES
    ]
    jobs: dict[str, JobRecord] = {}
    for state, raw in relevant:
        candidate = _meter_job_path(root, raw)
        if state in SETTLED_STATES and candidate is not None:
            with suppress(Exception):
                jobs[str(raw.get("spec_id") or raw.get("name"))] = load_job(candidate)
    selfhosted_jobs = [
        jobs[str(raw.get("spec_id") or raw.get("name"))]
        for state, raw in relevant
        if state in SETTLED_STATES
        and is_selfhosted_model(raw.get("model"))
        and (loaded.execution is None or loaded.execution.model_host == "modal")
        and str(raw.get("spec_id") or raw.get("name")) in jobs
    ]
    allocations = _modal_allocations(root, specs, selfhosted_jobs) if selfhosted_jobs else {}
    breakdown = dict.fromkeys(("modal_gpu", "daytona", "model_api", "unmeasured_reserved"), 0.0)
    reserved = settled = 0.0
    for state, raw in relevant:
        estimate = _reservation_usd(raw, loaded)
        if state in RESERVED_STATES:
            reserved += estimate
            breakdown["unmeasured_reserved"] += estimate
            continue
        job = jobs.get(str(raw.get("spec_id") or raw.get("name")))
        sources = dict.fromkeys(("modal_gpu", "daytona", "model_api"), 0.0)
        unmeasured = estimate if job is None or not job.trials else 0.0
        if job is not None and job.trials:
            trial_estimate = estimate / len(job.trials)
            for trial in job.trials:
                amounts = dict.fromkeys(("modal_gpu", "daytona", "model_api"), 0.0)
                unknown = False
                if is_selfhosted_model(raw.get("model")):
                    gpu = allocations.get(_trial_identity(job.result, trial.result, trial.path))
                    if gpu is None:
                        unknown = True
                    else:
                        amounts["modal_gpu"] = gpu[0]
                        unknown = not gpu[1]
                elif raw.get("agent") not in MODEL_FREE_AGENTS:
                    try:
                        model = _usd(trial_cost_columns(job, trial).get("cost_usd"))
                    except Exception:
                        model = None
                    if model is None:
                        unknown = True
                    else:
                        amounts["model_api"] = model
                if raw.get("environment") == "daytona":
                    try:
                        sandbox = _daytona_trial_usd(job, trial)
                    except Exception:
                        sandbox = None
                    if sandbox is None:
                        unknown = True
                    else:
                        amounts["daytona"] = sandbox
                elif raw.get("environment") == "docker" and loaded.execution is not None:
                    # Shared VM rent is not present in the Modal GPU report.
                    # Keep its unknown share reserved instead of settling it at $0.
                    unknown = True
                measured_trial = math.fsum(amounts.values())
                # One overrun cannot erase another unmeasurable trial's hold.
                if unknown:
                    unmeasured += max(0.0, trial_estimate - measured_trial)
                for source, amount in amounts.items():
                    sources[source] += amount
        measured = math.fsum(sources.values())
        for source, amount in sources.items():
            breakdown[source] += amount
        breakdown["unmeasured_reserved"] += unmeasured
        settled += measured + unmeasured
    return reserved, settled, breakdown


def campaign_spend_usd(
    repo_root: Path,
    campaign_id: str,
    *,
    exclude_spec_id: str | None = None,
) -> tuple[float, float, float]:
    """(reserved, settled, total), including GPU, sandbox and model API spend.

    Waiting specs hold nothing. Unknown settled components retain the remaining
    reserved resource envelope; zero ledger API cost never implies free GPU.
    ``exclude_spec_id`` excludes charges, not that trial's allocation weight.
    """
    reserved, settled, _breakdown = _campaign_meter(
        repo_root, campaign_id, exclude_spec_id=exclude_spec_id
    )
    return reserved, settled, reserved + settled


def campaign_spend_breakdown(repo_root: Path, campaign_id: str) -> dict[str, float]:
    """Read-only per-source committed totals; sum equals campaign_spend_usd total.

    ``unmeasured_reserved`` includes approved/running reservations and remaining
    conservative envelopes for settled trials with unavailable measurements.
    Modal amounts are app-day allocations, Daytona amounts use the existing
    list-price estimate path, and model API amounts use ledger-backed columns.
    """
    return _campaign_meter(repo_root, campaign_id)[2]


def campaign_admission_refusal(gate: Any, spec: ExperimentSpec) -> PolicyDecision | None:
    """Campaign gate branch for PolicyGate.decide. None means checks passed.

    Failures return the refusal (each mismatch keeps its own reason); the
    caller (PolicyGate) then runs the standing billable ceilings with the
    campaign approval standing in for the per-ID authorization.
    """
    repo_root: Path | None = getattr(gate, "repo_root", None)
    campaign_id = spec.campaign_id or ""
    if repo_root is None:
        return PolicyDecision(
            admitted=False,
            reason_code=REASON_GATE_UNCONFIGURED,
            message="campaign gate has no repository root; refusing closed",
        )
    try:
        campaign, _approval = load_approved_campaign(repo_root, campaign_id)
    except CampaignApprovalError as exc:
        if exc.reason_code == REASON_UNKNOWN:
            # A claim on nothing approvable is a routine mismatch, silent.
            try:
                load_campaign(repo_root, campaign_id)
            except CampaignApprovalError:
                return PolicyDecision(
                    admitted=False,
                    reason_code=REASON_UNKNOWN,
                    message=f"spec claims unknown campaign {campaign_id!r}",
                )
        return PolicyDecision(admitted=False, reason_code=exc.reason_code, message=str(exc))
    except (OSError, ValueError) as exc:
        return PolicyDecision(
            admitted=False,
            reason_code=REASON_CONTENT_CHANGED,
            message=f"campaign {campaign_id} gate defect: {exc}",
        )
    _reserved, _settled, committed = campaign_spend_usd(
        repo_root, campaign_id, exclude_spec_id=spec.spec_id
    )
    return _refusal_or_none(spec, campaign, repo_root, committed)


def _refusal_or_none(
    spec: ExperimentSpec,
    campaign: ExperimentCampaign,
    repo_root: Path,
    committed_usd: float,
) -> PolicyDecision | None:
    decision = check_campaign_admission(
        spec,
        campaign,
        repo_root=repo_root,
        committed_usd=committed_usd,
        defer_sampling=spec.spec_id is not None
        and any((repo_root / "queue" / "pending").glob(f"*-{spec.spec_id}.json")),
    )
    return None if decision.admitted else decision


def should_escalate(reason_code: str | None) -> bool:
    """Only a budget or rule breach, or a gate defect, pages Research-Harbor."""
    return reason_code in ESCALATING_REASONS


def read_escalations(repo_root: Path, campaign_id: str) -> list[CampaignEscalation]:
    """Every recorded escalation for one campaign (corrupt lines are skipped)."""
    path = campaign_dir(repo_root, campaign_id) / ESCALATIONS_FILENAME
    if not path.is_file():
        return []
    records: list[CampaignEscalation] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(CampaignEscalation.model_validate_json(line))
        except ValueError:
            continue
    return records


#: Test seam for the ``lin comment`` call. Production leaves this None so
#: escalation shells out to the real ``lin`` CLI; tests assign a stub.
LIN_RUNNER: Callable[..., Any] | None = None


def escalate(
    repo_root: Path,
    campaign: ExperimentCampaign,
    reason_code: str,
    message: str,
    *,
    spec_id: str | None = None,
    actor: str = "campaign-gate",
    lin_runner: Callable[..., Any] | None = None,
) -> CampaignEscalation:
    """Write one escalation event plus one lin comment to the campaign's card.

    Deduplicated per (spec, reason): a repeated refusal reuses the recorded
    escalation instead of paging again. ``lin_runner`` (or the ``LIN_RUNNER``
    test seam) stubs the ``lin comment`` call; production shells out and
    records whether it posted.
    """
    target_dir = campaign_dir(repo_root, campaign.campaign_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    for existing in read_escalations(repo_root, campaign.campaign_id):
        if existing.spec_id == spec_id and existing.reason_code == reason_code:
            return existing
    body = f"[{campaign.campaign_id}] {reason_code}: {message} (spec {spec_id or 'unknown'})"
    posted = _post_lin_comment(campaign.linear_card, body, runner=lin_runner)
    record = CampaignEscalation(
        campaign_id=campaign.campaign_id,
        spec_id=spec_id,
        reason_code=reason_code,
        message=message,
        card=campaign.linear_card,
        actor=actor,
        occurred_at=datetime.now(UTC),
        lin_posted=posted,
    )
    path = target_dir / ESCALATIONS_FILENAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record.model_dump_json() + "\n")
    return record


def _post_lin_comment(card: str, body: str, *, runner: Callable[..., Any] | None) -> bool:
    effective = runner if runner is not None else LIN_RUNNER
    try:
        if effective is not None:
            effective(["lin", "comment", card, body], check=True)
            return True
        completed = subprocess.run(
            ["lin", "comment", card, body],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return completed.returncode == 0
    except Exception:
        return False


def escalate_after_refusal(
    repo_root: Path,
    spec: ExperimentSpec,
    decision: PolicyDecision,
    *,
    lin_runner: Callable[..., Any] | None = None,
) -> CampaignEscalation | None:
    """Escalate a campaign refusal when the code is a breach or defect."""
    if not should_escalate(decision.reason_code):
        return None
    campaign_id = spec.campaign_id or ""
    try:
        campaign = load_campaign(repo_root, campaign_id)
    except CampaignApprovalError:
        return None
    except (OSError, ValueError):
        return None
    return escalate(
        repo_root,
        campaign,
        decision.reason_code or "campaign_breach",
        decision.message,
        spec_id=spec.spec_id,
        lin_runner=lin_runner,
    )


def trial_is_infra_excluded(trial_result: Mapping[str, Any], rewards: Mapping[str, Any]) -> bool:
    """Canonical infra classification: counts excluded with reason infra."""
    from evallab.counts import classify_counts
    from evallab.step_layers import classify_stop_reason

    agent = trial_result.get("agent_result") or {}
    metadata = agent.get("metadata") or {} if isinstance(agent, Mapping) else {}
    stop, _ = classify_stop_reason(
        agent_metadata=metadata, exception_info=trial_result.get("exception_info")
    )
    if stop == "context_exhausted":
        return False

    reward = rewards.get("reward")
    exception = trial_result.get("exception_info")
    counts = classify_counts(
        reward=float(reward) if isinstance(reward, (int, float)) else None,
        scored="reward" in rewards,
        exception=exception if isinstance(exception, dict) else None,
    )
    return counts["verdict"] == "excluded" and "infra" in counts["reasons"]


def replacement_clone(spec: ExperimentSpec) -> ExperimentSpec:
    """Clone one infra-excluded spec with lineage; exactly one generation."""

    base = spec.name
    suffix = "-r1"
    budget = 80 - len(suffix)
    trimmed = base[:budget].rstrip("-") or "campaign-retry"
    return spec.model_copy(
        update={
            "spec_id": None,
            "submitted_at": None,
            "policy_rule": None,
            "name": f"{trimmed}{suffix}",
            "campaign_replaces": spec.spec_id,
        }
    )


def replacement_needed(spec: ExperimentSpec, queue_specs: list[ExperimentSpec]) -> bool:
    """Whether this spec may still earn its one automatic replacement."""
    if spec.campaign_id is None or spec.spec_id is None:
        return False
    if spec.campaign_replaces is not None:
        # A replacement's own infra failure is not replaced again.
        return False
    return not any(
        item.campaign_replaces == spec.spec_id and item.campaign_id == spec.campaign_id
        for item in queue_specs
    )


def reconcile_campaign_replacements(
    executor: Any,
    *,
    report: Callable[[str], None] | None = None,
) -> int:
    """Submit at most one replacement per infra-excluded campaign trial.

    Scans done/failed campaign specs through the real queue states, classifies
    each landed trial with the canonical counts verdict, and submits a cloned
    spec with lineage for each infra-excluded original that has none yet.
    Escalation on a refused replacement rides the normal submit path.
    Never raises: a scan failure is reported, never a tick failure.
    """
    repo_root: Path = executor.repo_root
    root = campaigns_root(repo_root)
    if not root.is_dir():
        return 0
    approved_ids = [
        child.name
        for child in sorted(root.iterdir())
        if child.is_dir() and (child / CAMPAIGN_FILENAME).is_file()
    ]
    if not approved_ids:
        return 0
    queue_specs: list[tuple[str, ExperimentSpec]] = []
    for state in QUEUE_STATES:
        state_dir = repo_root / "queue" / state
        if not state_dir.is_dir():
            continue
        for path in sorted(state_dir.glob("*.json")):
            try:
                queue_specs.append((state, ExperimentSpec.model_validate_json(path.read_text())))
            except (OSError, ValueError):
                continue
    replaced = 0
    for campaign_id in approved_ids:
        try:
            load_approved_campaign(repo_root, campaign_id)
        except CampaignApprovalError:
            continue
        for state, spec in queue_specs:
            if spec.campaign_id != campaign_id or state not in SETTLED_STATES:
                continue
            if not replacement_needed(spec, [item for _, item in queue_specs]):
                continue
            try:
                if not _job_has_infra_excluded_trial(repo_root, spec):
                    continue
                clone = replacement_clone(spec)
                executor.submit(clone)
                queue_specs.append(("pending", clone))
                replaced += 1
            except Exception as exc:  # noqa: BLE001 -- scan never fails the tick
                if report is not None:
                    report(
                        f"campaign {campaign_id} replacement scan skipped "
                        f"{spec.spec_id}: {type(exc).__name__}: {exc}"
                    )
                continue
    return replaced


def _job_has_infra_excluded_trial(repo_root: Path, spec: ExperimentSpec) -> bool:
    from evallab.results import load_job

    jobs_dir = spec.jobs_dir or "runs"
    candidate = (repo_root / jobs_dir / spec.name).resolve()
    if candidate != repo_root and repo_root not in candidate.parents:
        return False
    job = load_job(candidate)
    return any(trial_is_infra_excluded(trial.result, trial.rewards) for trial in job.trials)


def validate_floor_against_reference(repo_root: Path, campaign: ExperimentCampaign) -> list[str]:
    """Backstop check: the floor must not bind before the reference limits.

    Reuses the HAR-156 ceiling comparison (same references, same direction):
    a floor below a sourced reference budget is an error, not a deviation.
    """
    from evallab.setup_fingerprint import (
        CEILING_REFERENCES,
        _reference_value,
        load_reference_profile,
    )

    profile = load_reference_profile(repo_root, campaign.reference_profile)
    errors: list[str] = []
    floor_map = {
        "budgets.max_requests": campaign.ceiling_floor.max_requests,
        "budgets.max_input_tokens": campaign.ceiling_floor.max_input_tokens,
        "budgets.max_output_tokens": campaign.ceiling_floor.max_output_tokens,
        "budgets.max_total_tokens": campaign.ceiling_floor.max_total_tokens,
        "budgets.cost_limit_usd": campaign.ceiling_floor.cost_limit_usd,
    }
    for field, reference_field in CEILING_REFERENCES.items():
        floor_value = floor_map[field]
        if floor_value is None:
            continue
        expected, sourced, source = _reference_value(profile, reference_field)
        if not sourced or expected is None:
            continue
        if floor_value < expected:
            errors.append(
                f"{field}: campaign floor {floor_value!r} binds before reference "
                f"{expected!r} (source: {source})"
            )
    return errors


def validate_campaign_content(repo_root: Path, campaign: ExperimentCampaign) -> list[str]:
    """Every static defect in a campaign file, for validate/preflight.

    Checks the reference profile exists, the pinned sampling matches the
    pinned model, the queue cwd exists, and the backstop floor against the
    reference (HAR-156 comparison). Admission itself is checked per spec.
    """
    errors: list[str] = []
    try:
        from evallab.setup_fingerprint import load_reference_profile

        load_reference_profile(repo_root, campaign.reference_profile)
    except (OSError, ValueError, KeyError) as exc:
        errors.append(f"reference_profile {campaign.reference_profile!r}: {exc}")
    if intended_sampling(campaign.agent, campaign.model) != campaign.sampling:
        errors.append(
            f"sampling {campaign.sampling.model_dump(mode='json')} does not match "
            f"agent {campaign.agent!r} model {campaign.model!r} "
            "(pin what that agent+model actually sends)"
        )
    if not Path(campaign.queue_cwd).is_dir():
        errors.append(f"queue_cwd {campaign.queue_cwd!r} is not a directory")
    try:
        errors.extend(validate_floor_against_reference(repo_root, campaign))
    except (OSError, ValueError, KeyError) as exc:
        errors.append(f"ceiling floor comparison failed: {exc}")
    if campaign.execution is not None and campaign.execution.qualification is not None:
        try:
            validate_qualification(campaign.execution.qualification, repo_root=repo_root)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"setup qualification: {exc}")
    return errors
