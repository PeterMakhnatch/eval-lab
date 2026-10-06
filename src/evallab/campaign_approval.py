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

A campaign records the budget (USD), the allowed task list (task ids and
package digests), the reference profile plus the allowed declared deviations,
the lock requirement, the backstop floor for trial ceilings, the queue cwd,
and the submitter. Research-Harbor approves it ONCE, as Peter's delegate.
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
- **Budget:** reservations (approved/running estimates) plus settled actuals
  stay inside the campaign budget. Settled actuals come from the existing
  ledger-backed cost columns; specs whose evidence cannot be loaded count
  their estimate conservatively.
- **Exhaustion:** new launches stop via the existing ``STOP`` fence while
  running trials finish untouched.

Escalation to Research-Harbor happens only on a budget or rule breach, or a
gate defect: one escalation event plus one ``lin comment`` to the campaign's
card per (spec, reason). Routine admissions are silent.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

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


class ExperimentCampaign(_FrozenContract):
    """The approvable content of one experiment campaign (HAR-175)."""

    schema_version: Literal["experiment-campaign/v1"] = SCHEMA_CAMPAIGN
    campaign_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    budget_usd: float = Field(gt=0)
    tasks: list[CampaignTaskAllowance] = Field(min_length=1)
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


def expected_sampling(model: str | None) -> CampaignSampling:
    """Pinned sampling for a model, from the same source admission compares."""
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
    actual_sampling = expected_sampling(spec.model)
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
    if committed_usd + float(spec.est_cost_usd or 0.0) > campaign.budget_usd:
        return PolicyDecision(
            admitted=False,
            reason_code=REASON_BUDGET_EXHAUSTED,
            message=(
                f"campaign {campaign.campaign_id} budget ${campaign.budget_usd:.2f} "
                f"exhausted: committed ${committed_usd:.2f} plus estimated "
                f"${float(spec.est_cost_usd or 0.0):.2f} overruns it"
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


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def campaign_spend_usd(
    repo_root: Path,
    campaign_id: str,
    *,
    exclude_spec_id: str | None = None,
) -> tuple[float, float, float]:
    """(reserved, settled, total) campaign spend in USD.

    Reserved counts approved/running estimates; settled counts done/failed
    ledger-backed actuals, falling back to the estimate when evidence cannot
    be loaded (conservative: an unmeasurable trial still holds its envelope).
    Waiting specs hold nothing: they launch nothing until admitted.
    """
    root = repo_root.resolve()
    queue_root = root / "queue"
    reserved = 0.0
    settled = 0.0
    for state in QUEUE_STATES:
        state_dir = queue_root / state
        if not state_dir.is_dir():
            continue
        for path in sorted(state_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict) or raw.get("campaign_id") != campaign_id:
                continue
            if exclude_spec_id is not None and raw.get("spec_id") == exclude_spec_id:
                continue
            estimate = raw.get("est_cost_usd") or 0.0
            try:
                estimate = float(estimate)
            except (TypeError, ValueError):
                estimate = 0.0
            if state in RESERVED_STATES:
                reserved += estimate
            elif state in SETTLED_STATES:
                actual = _settled_cost_usd(root, raw)
                settled += estimate if actual is None else actual
    return reserved, settled, reserved + settled


def _settled_cost_usd(repo_root: Path, raw: Mapping[str, Any]) -> float | None:
    """Ledger-backed actual for one settled spec, or None when unmeasurable."""
    try:
        from evallab.database import trial_cost_columns
        from evallab.results import load_job
    except ImportError:
        return None
    try:
        jobs_dir = raw.get("jobs_dir") or "runs"
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            return None
        candidate = (repo_root / str(jobs_dir) / name).resolve()
        if candidate != repo_root and repo_root not in candidate.parents:
            return None
        job = load_job(candidate)
        total = 0.0
        for trial in job.trials:
            columns = trial_cost_columns(job, trial)
            cost = columns.get("cost_usd")
            if cost is None:
                # Measured absence of ledger cost is zero spend, not unknown.
                continue
            total += float(cost)
        return total
    except Exception:
        return None


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
        spec, campaign, repo_root=repo_root, committed_usd=committed_usd
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
    if expected_sampling(campaign.model) != campaign.sampling:
        errors.append(
            f"sampling {campaign.sampling.model_dump(mode='json')} does not match "
            f"model {campaign.model!r} (compute it with sampling_sent)"
        )
    if not Path(campaign.queue_cwd).is_dir():
        errors.append(f"queue_cwd {campaign.queue_cwd!r} is not a directory")
    try:
        errors.extend(validate_floor_against_reference(repo_root, campaign))
    except (OSError, ValueError, KeyError) as exc:
        errors.append(f"ceiling floor comparison failed: {exc}")
    return errors
