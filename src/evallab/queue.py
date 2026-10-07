from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import platform
import secrets
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from evallab import campaign_approval, database
from evallab.campaign_execution import (
    docker_available_resources,
    docker_task_resources,
    qualification_matches_request,
)
from evallab.credentials import (
    DEFAULT_AGENT_MODELS,
    available_credentials,
    missing_credential_for,
)
from evallab.dispatch_guards import (
    DAYTONA_FALLBACK_ALLOWANCE,
    DAYTONA_REASON_CLAMPED,
    DAYTONA_REASON_GUARD_UNAVAILABLE,
    INFRA_STOP_EVENT,
    SELFHOSTED_REASON_KEY_MISSING,
    SELFHOSTED_REASON_NOT_READY,
    SELFHOSTED_REASON_READY,
    SELFHOSTED_REASON_REJECTED,
    SELFHOSTED_REASON_UNCONFIGURED,
    SMOKE_REASON_BLOCKED,
    SMOKE_REASON_DISABLED,
    SelfhostedProbeOutcome,
    daytona_tick_allowance,
    is_daytona_spec,
    is_model_backed,
    is_selfhosted_spec,
    read_unacked_spike_alerts,
    resolve_selfhosted_endpoint,
    resolve_selfhosted_key,
    scan_spike_alerts,
    selfhosted_probe_model,
    smoke_trial_blocks,
    wait_for_selfhosted_ready,
)
from evallab.eventlog import event_log_lock, read_event_log_lines
from evallab.evidence.atif import IngestProjectionResult, ingest_and_project
from evallab.evidence_store import EvidenceArchive, archive_evidence
from evallab.execution_contracts import (
    EGRESS_LOCK_AGENTS,
    TERMINUS_AGENT,
    TERMINUS_LOCAL_MODEL_SELECTOR,
    ZAI_OPENCODE_AGENT,
    ZAI_OPENCODE_MODEL_SELECTORS,
    DispatchCapacity,
    PaidRunAuthorization,
    is_lease_generation,
    is_mimo_selfhosted_model,
    load_policy,
    new_ulid,
)
from evallab.interpretation.trajectory_compliance import (
    ComplianceDisposition,
    PlatformSettlement,
    TrialEvidenceBundle,
    evaluate_trial_compliance,
)
from evallab.job_diff import DiffPreview, preview_diff, render_diff

if TYPE_CHECKING:
    from evallab.modal_ops import ModalTeardownHook


from evallab.profiles import CONTROL_ADAPTERS
from evallab.quota import (
    Headroom,
    default_roots,
    label,
    load_quota_report,
    provider_subscription_description,
)
from evallab.registry import (
    RegistryError,
    TaskComponentMissingError,
    TaskControlEvidenceError,
    TaskDigestMismatchError,
    TaskNotRegisteredError,
    TaskPathRedirectionError,
    TaskRegistry,
    TaskStateInvalidError,
    TaskUsageNotAllowedError,
    TaskVersionMismatchError,
    compute_task_digests,
)
from evallab.results import load_job
from evallab.runner import (
    CONTROL_AGENTS,
    SUPPORT_COMMAND_TIMEOUT_SECONDS,
    ExecutionFailure,
    RunRequest,
    TransientHarnessFailure,
    assert_no_secret_material,
    collected_secret_values,
    database_url_from_environment,
    run_experiment,
    subscription_environment,
    tool_version,
    transient_provider_exception,
)
from evallab.schemas import (
    AutoRunRule,
    ExperimentSpec,
    PolicyDecision,
    QueueEvent,
    QueueReason,
    QueueState,
    RunProvenance,
    StandingApprovalsPolicy,
    canonical_grid_point_id,
    effective_daily_cost_ceiling,
)
from evallab.storage.paths import derived_root_from_environment

QUEUE_STATES: tuple[QueueState, ...] = (
    "proposed",
    "pending",
    "approved",
    "waiting",
    "rejected",
    "running",
    "done",
    "failed",
)

QUEUE_RESUMED_EVENT = "queue_resumed"
DEFAULT_EVENTS_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_EVENT_BACKUPS = 7
DEFAULT_LEASE_STALE_SECONDS = 300.0
_TICK_THREAD_LOCK = threading.Lock()


def approved_spec_digest(spec: ExperimentSpec) -> str:
    payload = spec.model_dump(mode="json", exclude_none=True)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def authorization_required_message(spec: ExperimentSpec) -> str:
    """The refusal an operator reads — in `submit` output and in queue/reasons/."""
    spec_id = spec.spec_id or "<spec-id>"
    subscription = provider_subscription_description(spec.agent, spec.model)
    return (
        f"{spec.agent} is a non-control agent and requires named human approval. "
        f"Cost/account basis: {subscription}. This spec waits until "
        "a named human authorises it.\n"
        f"  authorise: uv run evallab approve {spec_id} --actor <you>\n"
        f'  refuse:    uv run evallab reject {spec_id} --actor <you> --reason "<why>"\n'
        f"  then run:  uv run evallab tick --spec-id {spec_id}\n"
        "The free oracle and nop controls are unaffected and still run unattended."
    )


def standing_rule_admits(rule: AutoRunRule, spec: ExperimentSpec) -> bool:
    """Whether one standing-approvals rule covers this spec.

    Standing approval is a statement about a *class* of work, written once into
    `policy/standing-approvals.yaml`. It can never cover a billable agent:
    paid work is authorised one spec at a time by a human. Enforcing that here
    rather than only in the policy file is the point — no edit to the policy
    file, and no agent added to any `auto_run` entry, can re-open unattended
    spend.
    """
    if spec.billable:
        return False
    if spec.policy_rule and spec.policy_rule != rule.name:
        return False
    if spec.agent not in rule.agents:
        return False
    if rule.tasks and not any(fnmatch.fnmatchcase(spec.task, pattern) for pattern in rule.tasks):
        return False
    if rule.max_attempts is not None and spec.attempts > rule.max_attempts:
        return False
    return set(rule.requires).issubset(spec.requires)


# --- subscription quota at the moment of authorisation ----------------------
#
# `src/evallab/quota.py` measures what remains on the subscription; it
# deliberately authorises nothing and imports nothing from here. This section
# is the one-way link: the gate reads the measurement, shows it to whoever is
# authorising, and refuses only what the *provider itself* says cannot run.
#
# Read `docs/quota-accounting.md`, "Intended integration, not performed here".
# It names two traps and both are honoured below:
#   1. `headroom.availability` is checked before any percentage is read, because
#      an unavailable headroom carries `None` in every numeric field and reading
#      `None` as "plenty left" reproduces the original defect in a new unit.
#   2. `since()` drops trials with no recorded start, so a window count is a
#      lower bound. Nothing here counts trials, precisely because a lower bound
#      cannot support a ceiling that has to bind.

#: Supplies the most recent provider quota snapshot. Injected, so the gate
#: reads no filesystem and no clock of its own (`agents/CHECKS.md`).
HeadroomReader = Callable[[], Headroom]

QUOTA_READER_UNCONFIGURED_REASON = (
    "this gate was built without a quota reader, so the subscription allowance was never looked up"
)

#: Marks a `human_approved` event whose actor accepted the recorded quota state.
#: It lives on the event because the event log is the only record #65 trusts.
QUOTA_OVERRIDE_REASON_CODE = "quota_override"

#: What an operator must understand about an unavailable reading. It is not a
#: reassurance and it is not a zero.
QUOTA_UNKNOWN_WARNING = (
    "UNKNOWN is not 'plenty left'. This says the allowance could not be "
    "measured, not that this run fits inside it. Check the provider yourself "
    "before authorising."
)

#: Why a stale reading warns instead of refusing. Argued in
#: `docs/operations.md`, "What the quota gate does and does not decide".
QUOTA_STALENESS_NOTE = (
    "a stale reading warns; it never refuses. The reading exists only because a "
    "paid trial recorded it, so refusing on age would make the first paid run "
    "after any quiet period impossible. Age is printed above precisely because "
    "you, not this gate, are the one judging whether it is still true."
)

#: The lab's refusal threshold on the account-wide `used_percent` now lives in
#: `policy/standing-approvals.yaml` as `refuse_billable_at_used_percent`, read
#: through `StandingApprovalsPolicy` and passed to `lab_threshold_reached`.
#:
#: It was an interim module constant here (PR #70) only because the schema field
#: it needed could not be added in that PR: `StandingApprovalsPolicy` forbids
#: extras, so the YAML key is a load error until the field exists, and
#: `schemas.py` was leased elsewhere that round. Both now landed together, so
#: setting a threshold is a config edit rather than a code edit — which was the
#: point, since the number is a spend decision belonging to Peter.


def _reader_clock(headroom: Headroom) -> datetime | None:
    """The instant `quota.py` was given when it built this reading.

    Reconstructed from the reading rather than read again, so the gate stays
    clock-free and its tests stay deterministic.
    """
    if headroom.observed_at is None or headroom.staleness_seconds is None:
        return None
    return headroom.observed_at + timedelta(seconds=headroom.staleness_seconds)


def quota_window_expired(headroom: Headroom) -> bool:
    """Whether the reading describes a rate-limit window that has since reset.

    The provider states when its window rolls over. Once it has, the recorded
    `used_percent` and `rate_limit_reached_type` are facts about a window that
    no longer exists, so they cannot refuse anything. Without this, a final
    trial that recorded 100% would lock the lab out permanently: the only thing
    that can produce a fresher reading is another paid trial.
    """
    if headroom.availability != "observed" or headroom.resets_at is None:
        return False
    now = _reader_clock(headroom)
    return now is not None and headroom.resets_at <= now


def provider_reported_exhaustion(headroom: Headroom) -> str | None:
    """The provider's own statement that paid work cannot succeed, or `None`.

    Trap one: `availability` is checked first, and every numeric comparison
    below is unreachable unless the reading is observed. An unavailable reading
    produces no refusal here — it is not evidence of exhaustion — but the caller
    must still print :func:`render_headroom_notice`, which says UNKNOWN out loud
    rather than letting silence read as consent.
    """
    if headroom.availability != "observed" or quota_window_expired(headroom):
        return None
    if headroom.rate_limit_reached_type is not None:
        return (
            f"the provider reports rate_limit_reached_type "
            f"{headroom.rate_limit_reached_type!r} on limit "
            f"{headroom.limit_id or label('unavailable')}"
        )
    if headroom.used_percent is not None and headroom.used_percent >= 100.0:
        return f"the provider reports used_percent {headroom.used_percent} of the window"
    return None


def lab_threshold_reached(headroom: Headroom, *, threshold: float | None) -> str | None:
    """Whether a Sponsor-set `used_percent` threshold has been reached.

    `threshold` is `StandingApprovalsPolicy.refuse_billable_at_used_percent`,
    committed unset, so this returns `None` in the shipped configuration. It is
    passed in rather than read here: the value is policy, and a function that
    loaded it itself would put a filesystem read inside a pure predicate
    (`agents/CHECKS.md`, deterministic-test rule).

    Keyword-only on purpose. A positional float would let a future caller pass
    the wrong number silently, and it makes every call site greppable.

    Kept separate from :func:`provider_reported_exhaustion` so a lab policy is
    never recorded as the provider's statement. Trap one is honoured here too:
    `availability` is checked before `used_percent` is read.
    """
    if threshold is None or headroom.availability != "observed":
        return None
    if quota_window_expired(headroom) or headroom.used_percent is None:
        return None
    if headroom.used_percent < threshold:
        return None
    return (
        f"used_percent {headroom.used_percent} is at or above the lab's "
        f"configured refusal threshold {threshold} "
        "(refuse_billable_at_used_percent in policy/standing-approvals.yaml)"
    )


def _age(seconds: float | None) -> str:
    if seconds is None:
        return label("unavailable")
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _instant(moment: datetime | None) -> str:
    return moment.isoformat() if moment is not None else label("unavailable")


def render_headroom_notice(
    headroom: Headroom,
    *,
    agent: str | None = None,
    model: str | None = None,
) -> str:
    """What the operator is told about one provider's allowance."""
    if agent == TERMINUS_AGENT and model == TERMINUS_LOCAL_MODEL_SELECTOR:
        return (
            f"{provider_subscription_description(agent, model)}: provider quota does not apply; "
            "per-spec approval is still required."
        )
    provider = (
        f"{provider_subscription_description(agent, model)} allowance/policy state"
        if agent is not None
        else "subscription quota"
    )
    header = f"{provider} (scope: account, NOT the lab; provider-reported):"
    if headroom.availability != "observed":
        return "\n".join(
            [
                header,
                f"  remaining allowance  UNKNOWN {label('unavailable')}",
                f"    reason: {headroom.reason or 'not reported'}",
                f"    {QUOTA_UNKNOWN_WARNING}",
            ]
        )
    lines = [
        header,
        f"  used_percent         {headroom.used_percent} {label('observed')}",
        f"  remaining_percent    {headroom.remaining_percent} {label('observed')} "
        "(account-wide, whole percentage points)",
        f"  resets_at            {_instant(headroom.resets_at)}",
        f"  hard_stop            {headroom.hard_stop}",
        f"    {headroom.hard_stop_note}",
        f"  observed_at          {_instant(headroom.observed_at)}",
        f"  staleness            {_age(headroom.staleness_seconds)} old",
        f"    {QUOTA_STALENESS_NOTE}",
        f"  source               {headroom.source or label('unavailable')}",
    ]
    if headroom.rate_limit_reached_type is not None:
        lines.append(f"  rate_limit_reached_type  {headroom.rate_limit_reached_type}")
    if quota_window_expired(headroom):
        lines.append(
            "  NOTE: resets_at has already passed, so this reading describes a "
            "window that has since rolled over. It cannot refuse anything, and "
            "it cannot reassure you either."
        )
    return "\n".join(lines)


class PolicyGate:
    def __init__(
        self,
        policy: StandingApprovalsPolicy,
        *,
        repo_root: Path | None = None,
        registry: TaskRegistry | None = None,
        headroom: HeadroomReader | None = None,
        headroom_by_agent: Callable[[str], Headroom] | None = None,
    ) -> None:
        self.policy = policy
        self.repo_root = repo_root.resolve() if repo_root else None
        self.registry = registry
        self._headroom_reader = headroom
        self._headroom_by_agent = headroom_by_agent
        self._headroom: dict[str, Headroom] = {}

    def headroom(self, agent: str | None = None) -> Headroom:
        """Read one provider's allowance at most once per agent."""
        key = agent or ""
        if key not in self._headroom:
            self._headroom[key] = self._read_headroom(agent)
        return self._headroom[key]

    def _read_headroom(self, agent: str | None = None) -> Headroom:
        if self._headroom_by_agent is not None and agent is not None:
            reader = self._headroom_by_agent
        elif self._headroom_reader is not None:
            base_reader = self._headroom_reader

            def reader(_agent: str) -> Headroom:
                return base_reader()

        else:
            return Headroom(
                availability="unavailable",
                reason=QUOTA_READER_UNCONFIGURED_REASON,
            )
        try:
            return reader(agent or "")
        except (OSError, ValueError) as exc:
            return Headroom(
                availability="unavailable",
                reason=(
                    "the quota reader failed while scanning job directories "
                    f"({type(exc).__name__}: {exc})"
                ),
            )

    def decide(
        self,
        spec: ExperimentSpec,
        *,
        spent_today_usd: float,
        consecutive_harness_failures: int = 0,
        authorization: PaidRunAuthorization | None = None,
        now: datetime | None = None,
    ) -> PolicyDecision:

        if spec.task.startswith("registered/"):
            reg = self.registry
            if reg is None and self.repo_root:
                reg = TaskRegistry.from_repo(self.repo_root)
            elif reg is None:
                reg = TaskRegistry.from_repo(Path.cwd())

            root = self.repo_root or Path.cwd()
            try:
                reg.resolve_spec(spec, root)
            except TaskNotRegisteredError as exc:
                return PolicyDecision(
                    admitted=False,
                    reason_code="unregistered_task",
                    message=str(exc),
                )
            except TaskStateInvalidError as exc:
                return PolicyDecision(
                    admitted=False,
                    reason_code="task_not_registered",
                    message=str(exc),
                )
            except TaskPathRedirectionError as exc:
                return PolicyDecision(
                    admitted=False,
                    reason_code="task_path_redirection",
                    message=str(exc),
                )
            except TaskVersionMismatchError as exc:
                return PolicyDecision(
                    admitted=False,
                    reason_code="task_version_mismatch",
                    message=str(exc),
                )
            except TaskDigestMismatchError as exc:
                reason = (
                    "verifier_digest_mismatch"
                    if "verifier" in str(exc).lower()
                    else "task_digest_mismatch"
                )
                return PolicyDecision(
                    admitted=False,
                    reason_code=reason,
                    message=str(exc),
                )
            except TaskControlEvidenceError as exc:
                return PolicyDecision(
                    admitted=False,
                    reason_code="invalid_control_evidence",
                    message=str(exc),
                )
            except TaskUsageNotAllowedError as exc:
                return PolicyDecision(
                    admitted=False,
                    reason_code="usage_not_allowed",
                    message=str(exc),
                )
            except TaskComponentMissingError as exc:
                return PolicyDecision(
                    admitted=False,
                    reason_code="missing_package_component",
                    message=str(exc),
                )
            except RegistryError as exc:
                return PolicyDecision(
                    admitted=False,
                    reason_code="task_admission_refused",
                    message=str(exc),
                )

        if authorization is not None and authorization.spec_id != spec.spec_id:
            return PolicyDecision(
                admitted=False,
                reason_code="paid_run_authorization_mismatch",
                message=(
                    f"the recorded authorisation names spec {authorization.spec_id}, "
                    f"not {spec.spec_id or '<unidentified>'}; every spec is "
                    "authorised by its own id"
                ),
            )

        campaign_rule: str | None = None
        if spec.campaign_id is not None:
            # HAR-175: one approval per experiment. A spec that claims an
            # approved campaign is admitted by that approval when every
            # campaign check passes; any mismatch refuses with its own
            # reason and the spec is never silently admitted. Specs without
            # a claim keep the per-ID path below unchanged.
            refusal = campaign_approval.campaign_admission_refusal(self, spec)
            if refusal is not None:
                return refusal
            campaign_rule = f"campaign:{spec.campaign_id}"

        if spec.billable:
            # Paid execution is authorised one spec at a time by a named human,
            # or once per experiment by a named campaign approval (above).
            # No standing rule is consulted below this point for billable work,
            # so an unattended cycle cannot reach Harbor with a paid agent.
            if authorization is None and campaign_rule is None:
                # The refusal is also where the operator first sees what the run
                # would cost them: they are about to be asked to authorise it.
                return PolicyDecision(
                    admitted=False,
                    reason_code="paid_run_unauthorized",
                    message=(
                        f"{authorization_required_message(spec)}\n"
                        f"{render_headroom_notice(self.headroom(spec.agent), agent=spec.agent, model=spec.model)}"
                    ),
                )
            if authorization is not None and spec.submitted_at is None:
                return PolicyDecision(
                    admitted=False,
                    reason_code="paid_run_authorization_mismatch",
                    message=(
                        "this spec records no submission time, so the authorisation "
                        "cannot be shown to cover it; resubmit it with "
                        "`uv run evallab submit` and authorise the id that prints"
                    ),
                )
            if (
                authorization is not None
                and spec.submitted_at is not None
                and authorization.authorized_at < spec.submitted_at
            ):
                return PolicyDecision(
                    admitted=False,
                    reason_code="paid_run_authorization_stale",
                    message=(
                        f"the authorisation {authorization.actor} recorded at "
                        f"{authorization.authorized_at.isoformat()} predates this spec "
                        f"(submitted {spec.submitted_at.isoformat()}); a spec id is a "
                        "name, not a reusable token. Authorise the current spec: "
                        f"uv run evallab approve {spec.spec_id} --actor <you>"
                    ),
                )
            # The provider's own statement that paid work cannot succeed. This
            # sits above the dollar ceilings because a lockout is not a budget
            # question: no amount of remaining budget makes a locked-out call
            # run. An unavailable or expired reading refuses nothing here — it
            # is not evidence of exhaustion — but it is never silent either:
            # every branch below carries `render_headroom_notice`.
            headroom = self.headroom(spec.agent)
            exhausted = provider_reported_exhaustion(headroom)
            override = authorization is not None and authorization.quota_override
            if exhausted is not None and not override:
                return PolicyDecision(
                    admitted=False,
                    reason_code="subscription_quota_exhausted",
                    message=(
                        f"the provider reports the subscription exhausted: {exhausted}. "
                        "This is the provider's own account of its allowance, not a "
                        "threshold this lab invented.\n"
                        f"{render_headroom_notice(headroom, agent=spec.agent, model=spec.model)}\n"
                        "  override, only if you have reason to believe the reading is "
                        f"wrong: uv run evallab approve {spec.spec_id} --actor <you> "
                        "--despite-quota"
                    ),
                )
            threshold_reached = lab_threshold_reached(
                headroom, threshold=self.policy.refuse_billable_at_used_percent
            )
            if threshold_reached is not None and not override:
                return PolicyDecision(
                    admitted=False,
                    reason_code="subscription_quota_ceiling",
                    message=(
                        f"{threshold_reached}.\n"
                        f"{render_headroom_notice(headroom, agent=spec.agent, model=spec.model)}\n"
                        f"  override: uv run evallab approve {spec.spec_id} "
                        "--actor <you> --despite-quota"
                    ),
                )
            if spec.est_cost_usd > self.policy.per_job_cost_ceiling_usd:
                return PolicyDecision(
                    admitted=False,
                    reason_code="per_job_cost_ceiling",
                    message=(
                        f"estimated cost {spec.est_cost_usd:.2f} exceeds per-job ceiling "
                        f"{self.policy.per_job_cost_ceiling_usd:.2f}"
                    ),
                )
            ceiling = effective_daily_cost_ceiling(self.policy, now or datetime.now(UTC))
            if spent_today_usd + spec.est_cost_usd > ceiling:
                return PolicyDecision(
                    admitted=False,
                    reason_code="daily_cost_ceiling",
                    message=(
                        f"estimated daily total {spent_today_usd + spec.est_cost_usd:.2f} "
                        f"exceeds ceiling {ceiling:.2f}"
                    ),
                )
            if consecutive_harness_failures >= self.policy.quiet_failure_rule:
                return PolicyDecision(
                    admitted=False,
                    reason_code="quiet_failure_rule",
                    message=(
                        f"{consecutive_harness_failures} consecutive harness failures "
                        "quarantine billable dispatch"
                    ),
                )

        if campaign_rule is not None:
            notes = [
                f"admitted by {campaign_rule} approval recorded for campaign {spec.campaign_id}"
            ]
            if spec.billable:
                # Whoever approved the campaign is entitled to see, in the
                # admission itself, the allowance this spec spends against.
                admitted_headroom = self.headroom(spec.agent)
                notes.append(
                    render_headroom_notice(admitted_headroom, agent=spec.agent, model=spec.model)
                )
            return PolicyDecision(
                admitted=True,
                policy_rule=campaign_rule,
                message="\n".join(notes),
            )

        if authorization is not None:
            notes = [
                f"admitted by {authorization.actor}'s authorisation recorded at "
                f"{authorization.authorized_at.isoformat()}"
            ]
            if spec.billable:
                # Whoever authorised this is entitled to see, in the admission
                # itself, the allowance they just spent against.
                admitted_headroom = self.headroom(spec.agent)
                notes.append(
                    render_headroom_notice(admitted_headroom, agent=spec.agent, model=spec.model)
                )
                if authorization.quota_override and (
                    provider_reported_exhaustion(admitted_headroom)
                    or lab_threshold_reached(
                        admitted_headroom,
                        threshold=self.policy.refuse_billable_at_used_percent,
                    )
                ):
                    notes.append(
                        f"{authorization.actor} authorised this DESPITE the recorded "
                        "quota state (--despite-quota)."
                    )
            return PolicyDecision(
                admitted=True,
                policy_rule="human-approval",
                message="\n".join(notes),
            )

        if spec.environment != "docker":
            return PolicyDecision(
                admitted=False,
                reason_code="cloud_or_remote_environment",
                message="non-Docker environments require human approval",
            )

        for rule in self.policy.auto_run:
            if standing_rule_admits(rule, spec):
                return PolicyDecision(
                    admitted=True,
                    policy_rule=rule.name,
                    message=f"admitted by standing policy rule {rule.name}",
                )

        return PolicyDecision(
            admitted=False,
            reason_code="out_of_policy",
            message="no standing-approvals rule covers this experiment",
        )


class DirectoryQueue:
    def __init__(
        self,
        root: Path,
        *,
        events_max_bytes: int = DEFAULT_EVENTS_MAX_BYTES,
        event_backups: int = DEFAULT_EVENT_BACKUPS,
        create: bool = True,
    ) -> None:
        if events_max_bytes < 1:
            raise ValueError("events_max_bytes must be positive")
        if event_backups < 1:
            raise ValueError("event_backups must be positive")
        self.root = root
        self.events_max_bytes = events_max_bytes
        self.event_backups = event_backups
        self.reasons_dir = root / "reasons"
        if create:
            self.ensure_directories()

    def ensure_directories(self) -> None:
        for state in QUEUE_STATES:
            (self.root / state).mkdir(parents=True, exist_ok=True)
        self.reasons_dir.mkdir(parents=True, exist_ok=True)

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def stop_path(self) -> Path:
        return self.root / "STOP"

    def state_dir(self, state: QueueState) -> Path:
        return self.root / state

    @contextmanager
    def tick_lock(self) -> Iterator[bool]:
        """Try to become the one executor allowed to claim specs from this queue."""
        if not _TICK_THREAD_LOCK.acquire(blocking=False):
            yield False
            return
        lock_path = self.root / ".tick.lock"
        try:
            with lock_path.open("a+b") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    yield False
                else:
                    try:
                        yield True
                    finally:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            _TICK_THREAD_LOCK.release()

    def submit(
        self,
        spec: ExperimentSpec,
        *,
        gate: PolicyGate,
        spent_today_usd: float,
        consecutive_harness_failures: int = 0,
    ) -> tuple[Path, PolicyDecision]:
        now = datetime.now(UTC)
        spec_id = spec.spec_id or new_ulid()
        normalized = spec.model_copy(update={"spec_id": spec_id, "submitted_at": now})
        filename = f"{_safe_component(spec.agent)}-{spec_id}.json"
        pending = self.state_dir("pending") / filename
        self._create_exclusive(pending, normalized)
        self.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=spec_id,
                occurred_at=now,
                event="submitted",
                to_state="pending",
                actor=spec.submitted_by,
                job_name=spec.name,
            )
        )
        decision = gate.decide(
            normalized,
            spent_today_usd=spent_today_usd,
            consecutive_harness_failures=consecutive_harness_failures,
        )
        if decision.admitted:
            normalized = normalized.model_copy(update={"policy_rule": decision.policy_rule})
            self._replace_model(pending, normalized)
            destination = self.transition(
                pending,
                "approved",
                actor="policy-gate",
                event="policy_admitted",
                policy_rule=decision.policy_rule,
            )
        else:
            destination = self.transition(
                pending,
                "waiting",
                actor="policy-gate",
                event="policy_waiting",
                reason_code=decision.reason_code,
            )
            self.write_reason(normalized, decision)
        return destination, decision

    def load(self, path: Path) -> ExperimentSpec:
        try:
            return ExperimentSpec.model_validate_json(path.read_text())
        except (OSError, ValidationError) as exc:
            raise ValueError(f"Invalid queued experiment {path}: {exc}") from exc

    def locate(self, spec_id: str, states: Iterable[QueueState] = QUEUE_STATES) -> Path:
        matches = [
            path for state in states for path in self.state_dir(state).glob(f"*-{spec_id}.json")
        ]
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one queued spec {spec_id}, found {len(matches)}")
        return matches[0]

    def transition(
        self,
        source: Path,
        destination_state: QueueState,
        *,
        actor: str,
        event: str,
        policy_rule: str | None = None,
        reason_code: str | None = None,
        approved_spec_digest: str | None = None,
        approved_campaign_manifest_digest: str | None = None,
        approved_campaign_spec_digest: str | None = None,
    ) -> Path:
        source_state = source.parent.name
        if source_state not in QUEUE_STATES:
            raise ValueError(f"Unknown source queue state: {source_state}")
        spec = self.load(source)
        destination = self.state_dir(destination_state) / source.name
        if destination.exists():
            raise FileExistsError(f"Queue destination already exists: {destination}")
        source.rename(destination)
        self.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=str(spec.spec_id),
                occurred_at=datetime.now(UTC),
                event=event,
                from_state=source_state,
                to_state=destination_state,
                actor=actor,
                policy_rule=policy_rule or spec.policy_rule,
                reason_code=reason_code,
                job_name=spec.name,
                approved_spec_digest=approved_spec_digest,
                approved_campaign_manifest_digest=approved_campaign_manifest_digest,
                approved_campaign_spec_digest=approved_campaign_spec_digest,
            )
        )
        return destination

    def write_reason(self, spec: ExperimentSpec, decision: PolicyDecision) -> Path:
        reason = QueueReason(
            spec_id=str(spec.spec_id),
            occurred_at=datetime.now(UTC),
            code=decision.reason_code or "unspecified",
            message=decision.message,
            policy_rule=decision.policy_rule,
        )
        path = self.reasons_dir / f"{spec.spec_id}-{new_ulid()}.json"
        self._create_exclusive(path, reason)
        return path

    def append_event(self, event: QueueEvent) -> None:
        payload = (event.model_dump_json(exclude_none=True) + "\n").encode()
        with event_log_lock(self.events_path, exclusive=True):
            if (
                self.events_path.is_file()
                and self.events_path.stat().st_size > 0
                and self.events_path.stat().st_size + len(payload) > self.events_max_bytes
            ):
                self._rotate_events()
            descriptor = os.open(
                self.events_path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                0o600,
            )
            try:
                view = memoryview(payload)
                while view:
                    view = view[os.write(descriptor, view) :]
            finally:
                os.close(descriptor)

    def _rotate_events(self) -> None:
        oldest = self.events_path.with_name(f"{self.events_path.name}.{self.event_backups}")
        oldest.unlink(missing_ok=True)
        for index in range(self.event_backups - 1, 0, -1):
            source = self.events_path.with_name(f"{self.events_path.name}.{index}")
            if source.exists():
                source.replace(self.events_path.with_name(f"{self.events_path.name}.{index + 1}"))
        self.events_path.replace(self.events_path.with_name(f"{self.events_path.name}.1"))

    def authorizations(self) -> dict[str, PaidRunAuthorization]:
        """Live human authorisations, read from the append-only event log.

        `approve` writes the grant, `reject` withdraws it. Raises rather than
        returning a partial view if the log cannot be read: an authorisation
        that cannot be proven does not exist.
        """
        granted: dict[str, PaidRunAuthorization] = {}
        for event in load_events(self.events_path):
            if event.event == "human_approved":
                granted[event.spec_id] = PaidRunAuthorization(
                    spec_id=event.spec_id,
                    actor=event.actor,
                    authorized_at=event.occurred_at,
                    quota_override=event.reason_code == QUOTA_OVERRIDE_REASON_CODE,
                    approved_spec_digest=event.approved_spec_digest,
                    campaign_manifest_digest=event.approved_campaign_manifest_digest,
                    campaign_spec_digest=event.approved_campaign_spec_digest,
                )
            elif event.event == "human_rejected":
                granted.pop(event.spec_id, None)
        return granted

    def authorization_for(self, spec: ExperimentSpec) -> PaidRunAuthorization | None:
        if spec.spec_id is None:
            return None
        return self.authorizations().get(spec.spec_id)

    def approve(self, spec_id: str, *, actor: str, quota_override: bool = False) -> Path:
        """Record one human authorisation.

        `quota_override` is stored on the event, not on the spec: the spec file
        is written by the automation, so an override asserted there would be the
        machine authorising itself. It overrides `subscription_quota_exhausted`
        and `subscription_quota_ceiling` only.
        """
        source = self.locate(spec_id, ("proposed", "pending", "waiting"))
        spec = self.load(source).model_copy(update={"policy_rule": "human-approval"})
        self._replace_model(source, spec)
        return self.transition(
            source,
            "approved",
            actor=actor,
            event="human_approved",
            policy_rule="human-approval",
            reason_code=QUOTA_OVERRIDE_REASON_CODE if quota_override else None,
            approved_spec_digest=approved_spec_digest(spec),
            approved_campaign_manifest_digest=spec.campaign_manifest_digest,
            approved_campaign_spec_digest=spec.campaign_spec_digest,
        )

    def reject(self, spec_id: str, *, actor: str, message: str) -> Path:
        source = self.locate(spec_id, ("proposed", "pending", "approved", "waiting"))
        spec = self.load(source)
        decision = PolicyDecision(
            admitted=False,
            reason_code="human_rejected",
            message=message,
        )
        destination = self.transition(
            source,
            "rejected",
            actor=actor,
            event="human_rejected",
            reason_code="human_rejected",
        )
        self.write_reason(spec, decision)
        return destination

    def stop(self) -> None:
        self.stop_path.touch(exist_ok=True)

    def resume(self, *, actor: str = "operator") -> None:
        """Clear the STOP fence and record the resume time (HAR-174).

        The ``queue_resumed`` event is the batch boundary for the
        infra-spike fence: alerts from jobs launched before it no longer
        fence unless their job is still running.
        """
        self.stop_path.unlink(missing_ok=True)
        self.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id="queue",
                occurred_at=datetime.now(UTC),
                event=QUEUE_RESUMED_EVENT,
                actor=actor,
            )
        )

    def last_resume_time(self) -> datetime | None:
        """Latest queue-resume instant, or ``None`` when never resumed."""
        try:
            events = load_events(self.events_path)
        except (OSError, ValueError):
            return None
        latest: datetime | None = None
        for event in events:
            if event.event not in (QUEUE_RESUMED_EVENT, "resumed"):
                continue
            occurred = event.occurred_at
            if occurred.tzinfo is None:
                occurred = occurred.replace(tzinfo=UTC)
            if latest is None or occurred > latest:
                latest = occurred
        return latest

    def list_specs(self, state: QueueState) -> list[tuple[Path, ExperimentSpec]]:
        # Two ticks may run concurrently (launchd schedule plus a manual tick).
        # A file listed here can be claimed — moved to another state — before we
        # read it. That is normal contention, not corruption: skip vanished
        # files instead of failing the whole tick.
        records: list[tuple[Path, ExperimentSpec]] = []
        for path in self.state_dir(state).glob("*.json"):
            try:
                records.append((path, self.load(path)))
            except ValueError as exc:
                if isinstance(exc.__cause__, FileNotFoundError):
                    continue
                raise
        return sorted(
            records,
            key=lambda record: (
                record[1].priority,
                record[1].submitted_at or datetime.min.replace(tzinfo=UTC),
                record[0].name,
            ),
        )

    def lease_path(self, spec: ExperimentSpec | Path | str) -> Path:
        """Return the lease file path in running/ for one experiment spec."""
        if isinstance(spec, ExperimentSpec):
            filename = f"{_safe_component(spec.agent)}-{spec.spec_id}.lease"
        elif isinstance(spec, Path):
            if spec.suffix == ".json":
                filename = f"{spec.stem}.lease"
            elif spec.suffix == ".lease":
                filename = spec.name
            else:
                filename = f"{spec.name}.lease"
        else:
            spec_str = str(spec)
            if spec_str.endswith(".lease"):
                filename = spec_str
            elif spec_str.endswith(".json"):
                filename = f"{Path(spec_str).stem}.lease"
            else:
                matches = list(self.state_dir("running").glob(f"*-{spec_str}.lease"))
                if matches:
                    return matches[0]
                matches_json = list(self.root.glob(f"**/*-{spec_str}.json"))
                if matches_json:
                    return self.state_dir("running") / f"{matches_json[0].stem}.lease"
                filename = f"{spec_str}.lease"
        return self.state_dir("running") / filename

    def cancel_path(
        self,
        spec: ExperimentSpec | Path | str,
        *,
        lease_generation: str | None = None,
    ) -> Path:
        """Return the generation-bound cancellation marker for one lease."""
        lease = self.lease_path(spec)
        generation = lease_generation or self.lease_generation(lease)
        suffix = generation or "unbound"
        return lease.with_name(f"{lease.name}.cancel.{suffix}")

    @staticmethod
    def _read_lease_record(path: Path) -> dict[str, Any] | None:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
        except OSError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return None
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                payload = json.load(source)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        finally:
            os.close(descriptor)
        return payload if isinstance(payload, dict) else None

    def lease_generation(
        self,
        spec: ExperimentSpec | Path | str,
    ) -> str | None:
        record = self._read_lease_record(self.lease_path(spec))
        generation = record.get("lease_generation") if record is not None else None
        if not is_lease_generation(generation):
            return None
        return generation

    @contextmanager
    def _lease_guard(self, path: Path) -> Iterator[None]:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(f".{path.name}.lock")
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def is_lease_stale(
        self,
        lease: Path | ExperimentSpec | str,
        *,
        stale_seconds: float = DEFAULT_LEASE_STALE_SECONDS,
        now: float | None = None,
    ) -> bool:
        """Return True if the lease file is absent, unreadable, or older than stale_seconds."""
        path = lease if isinstance(lease, Path) else self.lease_path(lease)
        if not path.is_file():
            return True
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return True
        current = now if now is not None else time.time()
        return (current - mtime) > stale_seconds

    def acquire_lease(
        self,
        spec: ExperimentSpec | Path | str,
        *,
        owner_pid: int | None = None,
        stale_seconds: float = DEFAULT_LEASE_STALE_SECONDS,
        now: datetime | None = None,
        lease_generation: str | None = None,
    ) -> Path | None:
        """Atomically claim one spec with an immutable lease generation."""
        path = self.lease_path(spec)
        pid = owner_pid if owner_pid is not None else os.getpid()
        timestamp = now or datetime.now(UTC)
        spec_id = spec.spec_id if isinstance(spec, ExperimentSpec) else str(spec)
        generation = lease_generation or secrets.token_hex(16)
        if len(generation) != 32 or any(
            character not in "0123456789abcdef" for character in generation
        ):
            raise ValueError("lease_generation must be 32 lowercase hexadecimal characters")
        payload = (
            json.dumps(
                {
                    "spec_id": spec_id,
                    "pid": pid,
                    "acquired_at": timestamp.isoformat(),
                    "host": platform.node(),
                    "lease_generation": generation,
                },
                indent=2,
            )
            + "\n"
        ).encode()
        # The lease guard is held across O_EXCL creation AND generation write so
        # a concurrent request_cancel/release/heartbeat (all of which take the
        # same guard) can never observe a freshly-created but not-yet-initialized
        # lease: an interleaved reader would otherwise see an empty/torn record,
        # treat it as unowned, and drop a valid cancellation request. The guard
        # also serializes a stale-lease reclaim against concurrent claimants.
        # The record itself is advisory, not a recovery root: the strict
        # generation reader fails closed on a torn write, and a stale lease is
        # reclaimed by mtime. Skip the per-claim fsync so a wide tick does not
        # pay a filesystem barrier for every lease (PERF budget).
        with self._lease_guard(path):
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
            except FileExistsError:
                if not self.is_lease_stale(path, stale_seconds=stale_seconds):
                    return None
                try:
                    path.unlink()
                    descriptor = os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    )
                except (FileExistsError, OSError):
                    return None
            except OSError:
                return None
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as destination:
                    destination.write(payload)
                    destination.flush()
            finally:
                os.close(descriptor)
        return path

    def request_cancel(self, spec: ExperimentSpec) -> bool:
        """Atomically bind a cancellation request to the currently active generation."""
        lease = self.lease_path(spec)
        with self._lease_guard(lease):
            generation = self.lease_generation(lease)
            if generation is None:
                return False
            marker = self.cancel_path(spec, lease_generation=generation)
            payload = (
                json.dumps(
                    {
                        "spec_id": spec.spec_id,
                        "lease_generation": generation,
                        "requested_at": datetime.now(UTC).isoformat(),
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            try:
                descriptor = os.open(
                    marker,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
            except FileExistsError:
                record = self._read_lease_record(marker)
                return (
                    record is not None
                    and record.get("lease_generation") == generation
                    and record.get("spec_id") == spec.spec_id
                )
            except OSError:
                return False
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as destination:
                    destination.write(payload)
                    destination.flush()
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return True

    def release_lease(
        self,
        spec: ExperimentSpec | Path | str,
        *,
        lease_generation: str | None = None,
    ) -> bool:
        """Release only the matching generation and acknowledge its cancellation."""
        path = self.lease_path(spec)
        with self._lease_guard(path):
            generation = lease_generation or self.lease_generation(path)
            if generation is None:
                return False
            current = self.lease_generation(path)
            released = False
            if current == generation:
                try:
                    path.unlink()
                    released = True
                except OSError:
                    return False
            marker = self.cancel_path(spec, lease_generation=generation)
            with suppress(OSError):
                if marker.is_file() and not marker.is_symlink():
                    marker.unlink()
            return released

    def heartbeat_lease(
        self,
        spec: ExperimentSpec | Path | str,
        *,
        lease_generation: str | None = None,
    ) -> bool:
        """Heartbeat only the currently matching lease generation."""
        path = self.lease_path(spec)
        with self._lease_guard(path):
            if lease_generation is not None and self.lease_generation(path) != lease_generation:
                return False
            try:
                descriptor = os.open(
                    path,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                )
            except OSError:
                return False
            try:
                os.utime(descriptor)
                return True
            except OSError:
                return False
            finally:
                os.close(descriptor)

    def list_leases(self) -> list[Path]:
        """Return all active lease files in running/."""
        return sorted(self.state_dir("running").glob("*.lease"))

    @staticmethod
    def _create_exclusive(path: Path, model: Any) -> None:
        payload = model.model_dump_json(indent=2, exclude_none=True) + "\n"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, payload.encode())
        finally:
            os.close(descriptor)

    @staticmethod
    def _replace_model(path: Path, model: Any) -> None:
        temporary = path.with_name(f".{path.name}.{new_ulid()}.tmp")
        DirectoryQueue._create_exclusive(temporary, model)
        temporary.replace(path)


CredentialProbe = Callable[[], frozenset[str]]
RunCallable = Callable[[RunRequest], Path]
IngestCallable = Callable[[Path], IngestProjectionResult | None]
SpendCallable = Callable[[], float]
FailureCallable = Callable[[], int]
Sleeper = Callable[[float], None]
ProgressCallable = Callable[[str], None]
ComplianceCallable = Callable[
    [Path, ExperimentSpec, IngestProjectionResult | None, EvidenceArchive],
    ComplianceDisposition,
]

MAX_TRANSIENT_RETRIES = 2
TRANSIENT_BACKOFF_BASE_SECONDS = 5.0
TRANSIENT_BACKOFF_CAP_SECONDS = 30.0


def record_projection_failures(
    queue: DirectoryQueue,
    result: IngestProjectionResult,
    *,
    actor: str,
    spec_id: str,
) -> None:
    for failure in result.failures:
        queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=spec_id,
                occurred_at=datetime.now(UTC),
                event="projection_failed",
                actor=actor,
                reason_code=failure.reason_code,
                job_name=failure.job_name,
            )
        )


def _safe_repo_path(repo_root: Path, relative: str) -> Path:
    candidate = (repo_root / relative).resolve()
    if candidate != repo_root and repo_root not in candidate.parents:
        raise ValueError(f"path escapes repository: {relative}")
    return candidate


def _is_locked_docker_spec(spec: ExperimentSpec) -> bool:
    return (
        spec.environment == "docker"
        and spec.egress_lock is True
        and spec.agent in EGRESS_LOCK_AGENTS["docker"]
    )


class Executor:
    """The sole application boundary allowed to start Harbor experiments."""

    def __init__(
        self,
        *,
        repo_root: Path,
        queue: DirectoryQueue,
        policy: StandingApprovalsPolicy,
        runner: RunCallable | None = None,
        ingester: IngestCallable | None = None,
        spent_today: SpendCallable | None = None,
        consecutive_harness_failures: FailureCallable | None = None,
        credential_probe: CredentialProbe | None = None,
        headroom: HeadroomReader | None = None,
        progress: ProgressCallable | None = None,
        sleeper: Sleeper = time.sleep,
        compliance: ComplianceCallable | None = None,
        max_transient_retries: int = MAX_TRANSIENT_RETRIES,
        parallel: int | None = None,
        capacity: DispatchCapacity | None = None,
        modal_teardown: ModalTeardownHook | None = None,
        watch_enabled: bool = True,
        watch_interval_seconds: float = 60.0,
        selfhosted_warmup_seconds: float = 600.0,
        selfhosted_probe_timeout_seconds: float = 10.0,
        selfhosted_probe_fn: Callable[[str, str, str, float], SelfhostedProbeOutcome] | None = None,
        daytona_observe_fn: Callable[[], dict[str, Any]] | None = None,
        docker_observe_fn: Callable[[], tuple[float, int]] | None = None,
        smoke_gate_enabled: bool = True,
        notify_runner: Callable[..., Any] | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.queue = queue
        self.gate = PolicyGate(
            policy,
            repo_root=self.repo_root,
            headroom=headroom,
            headroom_by_agent=None if headroom is not None else self._repo_headroom,
        )
        self._runner = runner or self._run_harbor
        self._compliance = compliance or self._evaluate_post_run_compliance
        self._ingester = ingester or self._ingest
        self._spent_today = spent_today or self._catalog_spend
        self._credential_probe = credential_probe or available_credentials
        self._progress = progress
        self._sleeper = sleeper
        if max_transient_retries < 0:
            raise ValueError("max_transient_retries cannot be negative")
        self._max_transient_retries = max_transient_retries
        self._consecutive_harness_failures = (
            consecutive_harness_failures or self._catalog_harness_failures
        )
        if parallel is not None and parallel < 1:
            raise ValueError("parallel must be at least 1")
        self.parallel = parallel
        self.capacity = capacity
        self._modal_teardown = modal_teardown
        if watch_interval_seconds <= 0:
            raise ValueError("watch_interval_seconds must be positive")
        self._watch_enabled = watch_enabled
        self._watch_interval_seconds = watch_interval_seconds
        self._notify_runner = notify_runner
        if selfhosted_warmup_seconds <= 0:
            raise ValueError("selfhosted_warmup_seconds must be positive")
        if selfhosted_probe_timeout_seconds <= 0:
            raise ValueError("selfhosted_probe_timeout_seconds must be positive")
        self._selfhosted_warmup_seconds = selfhosted_warmup_seconds
        self._selfhosted_probe_timeout_seconds = selfhosted_probe_timeout_seconds
        self._selfhosted_probe_fn = selfhosted_probe_fn
        self._daytona_observe_fn = daytona_observe_fn
        self._docker_observe_fn = docker_observe_fn or docker_available_resources
        self._smoke_gate_enabled = smoke_gate_enabled
        self.last_tick_reason: str | None = None

    def _repo_headroom(self, agent: str) -> Headroom:
        """Read only the quota evidence belonging to ``agent``."""
        return load_quota_report(
            default_roots(self.repo_root),
            now=datetime.now(UTC),
            paid_agents=frozenset({agent}),
        ).headroom

    @classmethod
    def from_repo(
        cls,
        root: Path,
        *,
        parallel: int | None = None,
        progress: ProgressCallable | None = None,
        capacity: DispatchCapacity | None = None,
        max_transient_retries: int = MAX_TRANSIENT_RETRIES,
        create_queue: bool = True,
        modal_teardown: ModalTeardownHook | None = None,
        selfhosted_warmup_seconds: float = 600.0,
        selfhosted_probe_timeout_seconds: float = 10.0,
        smoke_gate_enabled: bool = True,
    ) -> Executor:
        return cls(
            repo_root=root,
            queue=DirectoryQueue(root / "queue", create=create_queue),
            policy=load_policy(root / "policy/standing-approvals.yaml"),
            parallel=parallel,
            capacity=capacity,
            progress=progress,
            max_transient_retries=max_transient_retries,
            modal_teardown=modal_teardown,
            selfhosted_warmup_seconds=selfhosted_warmup_seconds,
            selfhosted_probe_timeout_seconds=selfhosted_probe_timeout_seconds,
            smoke_gate_enabled=smoke_gate_enabled,
        )

    def submit(self, spec: ExperimentSpec) -> tuple[Path, PolicyDecision]:
        path, decision = self.queue.submit(
            spec,
            gate=self.gate,
            spent_today_usd=self._effective_spend_today(),
            consecutive_harness_failures=self._consecutive_harness_failures(),
        )
        if (
            spec.campaign_id is not None
            and not decision.admitted
            and campaign_approval.should_escalate(decision.reason_code)
        ):
            # Breach or defect: page Research-Harbor once per (spec, reason).
            # Budget exhaustion also fences the queue: new launches stop via
            # the existing STOP mechanism while running trials finish alone.
            campaign_approval.escalate_after_refusal(self.repo_root, spec, decision)
            if decision.reason_code == campaign_approval.REASON_BUDGET_EXHAUSTED:
                self.queue.stop()
        return path, decision

    def tick(
        self,
        parallel: int | None = None,
        *,
        spec_ids: Sequence[str] | None = None,
    ) -> int:
        effective_parallel = parallel if parallel is not None else self.parallel
        if effective_parallel is not None and effective_parallel < 1:
            raise ValueError("parallel must be at least 1")
        with self.queue.tick_lock() as acquired:
            if not acquired:
                self.last_tick_reason = "executor_busy"
                return 0
            self.last_tick_reason = None
            candidates = [
                spec
                for state in ("approved", "running")
                for _, spec in self.queue.list_specs(state)
            ]
            dispatched = self._tick_locked(parallel=effective_parallel, spec_ids=spec_ids)
            # Make the existing single infra replacement visible before deciding
            # the campaign drained; otherwise every replacement buys a cold start.
            try:
                campaign_approval.reconcile_campaign_replacements(
                    self, report=self._report_progress
                )
            except Exception as exc:  # noqa: BLE001 -- uncertain drain must not stop the server
                self._report_progress(
                    f"campaign replacement scan failed: {type(exc).__name__}: {exc}"
                )
            else:
                self._maybe_stop_selfhosted_app(candidates)
        return dispatched

    def _report_progress(self, message: str) -> None:
        if self._progress is not None:
            self._progress(message)

    def _archive_post_run(
        self,
        job_dir: Path,
        spec: ExperimentSpec,
    ) -> EvidenceArchive:
        return archive_evidence(
            job_dir,
            _safe_repo_path(self.repo_root, spec.campaign_evidence_store or "derived/evidence-cas"),
            record_id=str(spec.campaign_attempt_id or spec.spec_id),
            kind="post-run-compliance",
        )

    def _evaluate_post_run_compliance(
        self,
        job_dir: Path,
        spec: ExperimentSpec,
        ingest_result: IngestProjectionResult | None,
        archive: EvidenceArchive,
    ) -> ComplianceDisposition:
        """Evaluate Data's merged contract over archived, catalog-settled evidence."""
        job = load_job(job_dir)
        if len(job.trials) != 1:
            raise ValueError("post-run compliance requires exactly one trial")
        trial = job.trials[0]
        cataloged = bool(
            ingest_result is not None
            and ingest_result.cataloged_jobs > 0
            and not ingest_result.failures
        )
        result = trial.result
        has_valid_atif = False
        for atif_path in trial.path.rglob("*.json"):
            if atif_path.name in {"trajectory.json", "mini-swe-agent.trajectory.json"}:
                try:
                    json.loads(atif_path.read_text(encoding="utf-8"))
                    has_valid_atif = True
                    break
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
        agent_res = trial.result.get("agent_result")
        exit_code = (
            agent_res.get("exit_code") if isinstance(agent_res, dict) else None
        ) or trial.result.get("exit_code")
        if exit_code is not None and exit_code != 0 and not has_valid_atif:
            task_success = False
        else:
            task_success = trial.primary_reward == 1.0 if trial.primary_reward is not None else None
        bundle = TrialEvidenceBundle(
            settlement=PlatformSettlement(
                job_id=job.id,
                trial_id=trial.id,
                cas_uri=archive.uri,
                cataloged=cataloged,
                cas_settled=True,
            ),
            task_name=str(result.get("task_name") or spec.task_id or spec.task),
            seed=str(spec.generator_seed) if spec.generator_seed is not None else None,
            benchmark_family=(
                spec.campaign_ledger.family.value if spec.campaign_ledger is not None else None
            ),
            model_name=spec.model,
            agent_name=spec.agent,
            task_success=task_success,
            result_present=True,
            atif_present=has_valid_atif,
            finished_at=(
                str(result["finished_at"]) if result.get("finished_at") is not None else None
            ),
        )
        report = evaluate_trial_compliance(bundle)
        payload = (report.model_dump_json(indent=2) + "\n").encode()
        report_dir = (
            _safe_repo_path(self.repo_root, spec.campaign_evidence_store or "derived/evidence-cas")
            / "records/trial-compliance"
        )
        report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        report_path = report_dir / f"{spec.campaign_attempt_id or spec.spec_id}.json"
        try:
            descriptor = os.open(
                report_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            existing = report_path.read_bytes()
            if existing != payload:
                raise ValueError("post-run compliance report replay drift") from None
        else:
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short compliance report write")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return report.disposition

    def _assert_persistent_artifacts_safe(
        self,
        spec: ExperimentSpec,
        job_dir: Path,
    ) -> None:
        secrets = collected_secret_values()
        jobs_root = _safe_repo_path(self.repo_root, spec.jobs_dir)
        executor_root = jobs_root / ".executor"
        paths = [
            job_dir,
            jobs_root / ".transient-attempts" / spec.name,
            executor_root / f"{spec.name}.state.json",
            *executor_root.glob(f"{spec.name}*.log"),
        ]
        assert_no_secret_material(tuple(paths), secrets=secrets)

    def _settle_post_run(
        self,
        job_dir: Path,
        spec: ExperimentSpec,
        *,
        actor: str,
    ) -> PolicyDecision | None:
        stage = "artifact_scan"
        try:
            self._assert_persistent_artifacts_safe(spec, job_dir)
            # Validate trial outcomes and evidence fidelity
            try:
                job = load_job(job_dir)
                trials = getattr(job, "trials", ())
            except Exception:
                trials = ()
            for trial in trials:
                native_traj = trial.path / "agent" / "trajectory.json"
                atif_traj = trial.path / "trajectory.json"
                traj_path = (
                    native_traj
                    if native_traj.is_file()
                    else (atif_traj if atif_traj.is_file() else None)
                )
                if traj_path is not None:
                    try:
                        json.loads(traj_path.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError) as parse_err:
                        return PolicyDecision(
                            admitted=False,
                            reason_code="trajectory_parse_failure",
                            message=f"Trial {trial.path.name} trajectory is malformed: {parse_err}",
                        )
                agent_res = (
                    trial.result.get("agent_result")
                    if isinstance(trial.result.get("agent_result"), dict)
                    else {}
                )
                exit_code = agent_res.get("exit_code")
                if exit_code is None:
                    exit_code = trial.result.get("exit_code")
                if exit_code is not None and exit_code != 0 and traj_path is None:
                    return PolicyDecision(
                        admitted=False,
                        reason_code="agent_nonzero_exit",
                        message=(
                            f"Trial {trial.path.name} agent exited with code {exit_code} "
                            "without trajectory; refusing completion"
                        ),
                    )
            metadata_path = job_dir / "lab-metadata.json"
            if metadata_path.is_file():
                try:
                    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if meta.get("model_identity_mismatch"):
                        return PolicyDecision(
                            admitted=False,
                            reason_code="model_identity_mismatch",
                            message="Provider-returned model identity does not match requested model",
                        )
                except Exception:
                    pass
            archive: EvidenceArchive | None = None
            if spec.campaign_ledger is not None:
                stage = "post_run_archive"
                archive = self._archive_post_run(job_dir, spec)
            stage = "catalog_ingest"
            ingest_result = self._ingester(job_dir)
            if ingest_result is not None:
                record_projection_failures(
                    self.queue,
                    ingest_result,
                    actor=actor,
                    spec_id=str(spec.spec_id),
                )
            if spec.campaign_ledger is not None:
                stage = "post_run_compliance"
                if archive is None:
                    raise ValueError("post-run compliance archive is missing")
                disposition = self._compliance(job_dir, spec, ingest_result, archive)
                if disposition != "QUALITY_PASS":
                    return PolicyDecision(
                        admitted=False,
                        reason_code=f"post_run_compliance_{disposition.casefold()}",
                        message=(f"post-run compliance refused queue completion: {disposition}"),
                    )
        except Exception as exc:
            reason_code = (
                exc.reason_code if isinstance(exc, ExecutionFailure) else f"{stage}_failed"
            )
            return PolicyDecision(
                admitted=False,
                reason_code=reason_code,
                message=(
                    f"{stage.replace('_', ' ')} failed closed before queue completion "
                    f"({type(exc).__name__})"
                ),
            )
        return None

    def _validate_campaign_dispatch_spec(
        self,
        spec: ExperimentSpec,
        *,
        source: Path,
    ) -> None:
        spec_id = str(spec.spec_id or "")
        provenance_present = any(
            value is not None
            for value in (
                spec.campaign_ledger,
                spec.campaign_attempt_id,
                spec.campaign_attempt_index,
                spec.campaign_manifest_digest,
                spec.campaign_spec_digest,
                spec.campaign_evidence_store,
            )
        )
        campaign_source = "campaign-" in source.name
        if not provenance_present and not campaign_source and not spec_id.startswith("campaign-"):
            return
        if (
            not spec_id.startswith("campaign-")
            or spec.campaign_ledger is None
            or spec.campaign_manifest_digest is None
        ):
            raise ExecutionFailure(
                "campaign_binding_missing",
                "campaign queue record lost its frozen manifest binding",
            )
        from evallab.campaigns import CampaignManifest, experiment_spec_digest

        manifest_path = (
            self.repo_root / "runs/campaigns" / spec.campaign_ledger.ledger_id / "manifest.json"
        )
        try:
            descriptor = os.open(manifest_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as handle:
                manifest = CampaignManifest.model_validate_json(handle.read())
        except Exception as exc:
            raise ExecutionFailure(
                "campaign_manifest_unavailable",
                "frozen campaign manifest cannot be validated at dispatch",
            ) from exc
        matches = [attempt for attempt in manifest.attempts if attempt.spec_id == spec_id]
        if len(matches) != 1:
            raise ExecutionFailure(
                "campaign_attempt_unbound",
                "queued campaign spec is not uniquely present in the frozen manifest",
            )
        attempt = matches[0]
        if (
            manifest.manifest_digest != spec.campaign_manifest_digest
            or spec.campaign_spec_digest != attempt.spec_digest
            or experiment_spec_digest(spec) != attempt.spec_digest
        ):
            raise ExecutionFailure(
                "campaign_spec_drifted",
                "queued campaign spec differs from its frozen attempt",
            )

    def _defer_spec_event(self, spec: ExperimentSpec, reason_code: str) -> None:
        """Record a deferral that leaves the spec approved for a later tick."""
        self.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=str(spec.spec_id),
                occurred_at=datetime.now(UTC),
                event="dispatch_deferred",
                actor="executor",
                reason_code=reason_code,
                job_name=spec.name,
            )
        )

    def _ensure_selfhosted_ready(self, spec: ExperimentSpec) -> bool:
        """Probe the self-hosted upstream before dispatch; never launch into a 503.

        Non-self-hosted specs pass through. Every self-hosted dispatch
        re-probes the authenticated ``/v1/chat/completions`` upstream with
        a fresh probe: no cached-ready reuse. Cold endpoints wait inside
        ``selfhosted_warmup_seconds``; a still-cold endpoint defers the
        spec (stays approved) with a clear reason code.
        """
        if not is_selfhosted_spec(spec):
            return True
        endpoint = resolve_selfhosted_endpoint()
        if endpoint is None:
            self._defer_spec_event(spec, SELFHOSTED_REASON_UNCONFIGURED)
            self._report_progress(
                f"deferred {spec.name} ({SELFHOSTED_REASON_UNCONFIGURED}); state: approved"
            )
            return False
        key = resolve_selfhosted_key()
        if not key:
            self._defer_spec_event(spec, SELFHOSTED_REASON_KEY_MISSING)
            self._report_progress(
                f"deferred {spec.name} ({SELFHOSTED_REASON_KEY_MISSING}); state: approved"
            )
            return False
        model = selfhosted_probe_model(spec.model)
        started = time.time()
        ready, attempts, rejected = wait_for_selfhosted_ready(
            endpoint,
            model,
            key,
            warmup_seconds=self._selfhosted_warmup_seconds,
            timeout_seconds=self._selfhosted_probe_timeout_seconds,
            probe_fn=self._selfhosted_probe_fn,
            sleeper=self._sleeper,
            clock=time.time,
        )
        waited = time.time() - started
        if ready:
            self.queue.append_event(
                QueueEvent(
                    event_id=new_ulid(),
                    spec_id=str(spec.spec_id),
                    occurred_at=datetime.now(UTC),
                    event=SELFHOSTED_REASON_READY,
                    actor="executor",
                    reason_code=SELFHOSTED_REASON_READY,
                    job_name=spec.name,
                )
            )
            self._report_progress(
                f"self-hosted probe ready for {spec.name} "
                f"(attempts={attempts}, waited={waited:.1f}s)"
            )
            return True
        reason = SELFHOSTED_REASON_REJECTED if rejected else SELFHOSTED_REASON_NOT_READY
        self._defer_spec_event(spec, reason)
        self._report_progress(
            f"deferred {spec.name} ({reason} after {attempts} probe(s)); state: approved"
        )
        return False

    def _stop_on_infra_spike(
        self,
        approved_specs: list[tuple[Path, ExperimentSpec]],
        *,
        resume_time: datetime | None,
    ) -> bool:
        """Fence the queue when a current-batch watch alert reports a spike.

        Scans ``watch/alerts.jsonl`` under every runs root behind the
        approved batch, but only jobs that are running or were launched
        since the last ``evallab resume`` can fence (HAR-174); acknowledged
        and provably pre-resume rows never fence. Runs once per tick. On a
        hit, sets the existing ``STOP`` fence so no further specs dispatch;
        running trials are never touched. Returns whether the fence was set.
        """
        if not approved_specs:
            return False
        roots: dict[Path, None] = {}
        for _, spec in approved_specs:
            try:
                roots[_safe_repo_path(self.repo_root, spec.jobs_dir)] = None
            except ValueError:
                continue
        default_runs = self.repo_root / "runs"
        if default_runs.is_dir():
            roots[default_runs] = None
        running_dirs = [
            job_dir
            for _, spec in self.queue.list_specs("running")
            if (job_dir := self._job_dir_for(spec)) is not None
        ]
        alerts = scan_spike_alerts(roots, resume_time=resume_time, running_dirs=running_dirs)
        if not alerts:
            return False
        return self._fence_for_spike(
            str(alerts[0].get("rule") or "infra_spike"), len(alerts), approved_specs
        )

    def _stop_on_job_spike(
        self,
        job_dir: Path,
        remaining: list[tuple[Path, ExperimentSpec]],
        *,
        resume_time: datetime | None,
    ) -> bool:
        """Fence the queue when a just-finished job reports an infra spike.

        Reads only that job's own ``watch/alerts.jsonl`` (the watch final
        pass lands there synchronously at dispatch end), so a 100-spec tick
        pays one file probe per dispatch instead of one full scan.
        Acknowledged and provably pre-resume rows never fence (HAR-174).
        """
        if not remaining:
            return False
        alerts = read_unacked_spike_alerts(job_dir, resume_time=resume_time)
        if not alerts:
            return False
        return self._fence_for_spike(
            str(alerts[0].get("rule") or "infra_spike"), len(alerts), remaining
        )

    def _fence_for_spike(
        self, rule: str, count: int, specs: list[tuple[Path, ExperimentSpec]]
    ) -> bool:
        """Set the existing ``STOP`` fence and record why; never touches running trials."""
        self.queue.stop()
        for _, spec in specs:
            self.queue.append_event(
                QueueEvent(
                    event_id=new_ulid(),
                    spec_id=str(spec.spec_id),
                    occurred_at=datetime.now(UTC),
                    event=INFRA_STOP_EVENT,
                    actor="executor",
                    reason_code=f"infra_spike:{rule}",
                    job_name=spec.name,
                )
            )
        self.last_tick_reason = "infra_spike_stop"
        self._report_progress(
            f"stopped launches on {rule} ({count} alert(s)); "
            "running trials untouched; clear with `evallab resume`"
        )
        return True

    def _job_dir_for(self, spec: ExperimentSpec) -> Path | None:
        """Best-effort job directory for a spec; ``None`` when unresolvable."""
        try:
            return _safe_repo_path(self.repo_root, spec.jobs_dir) / spec.name
        except ValueError:
            return None

    def _observe_daytona(self) -> dict[str, Any]:
        """Read the Daytona admission snapshot (injectable for tests)."""
        if self._daytona_observe_fn is not None:
            return self._daytona_observe_fn()
        from evallab.daytona_guard import DaytonaGuard

        return DaytonaGuard().observe()

    def _apply_daytona_clamp(
        self, approved_specs: list[tuple[Path, ExperimentSpec]]
    ) -> list[tuple[Path, ExperimentSpec]]:
        """Clamp the tick plan to Daytona's memory cap up front.

        Non-Daytona specs always pass. Daytona launches are capped at the
        guard's memory allowance (one sandbox held in reserve), preserving
        queue order, so admission never refuses an over-planned tick.
        Trimmed specs retain their current state for a later tick.
        """
        daytona_count = sum(1 for _, spec in approved_specs if is_daytona_spec(spec))
        if daytona_count == 0:
            return approved_specs
        try:
            snapshot = self._observe_daytona()
        except Exception as exc:
            allowance = DAYTONA_FALLBACK_ALLOWANCE
            self._report_progress(
                f"daytona guard unavailable ({type(exc).__name__}); "
                f"falling back conservatively to {allowance} launch(es) this tick"
            )
            reason = DAYTONA_REASON_GUARD_UNAVAILABLE
            detail: str | None = None
        else:
            allowance, detail = daytona_tick_allowance(snapshot)
            self._report_progress(f"daytona tick clamp: {detail}")
            reason = DAYTONA_REASON_CLAMPED
        if daytona_count <= allowance:
            return approved_specs
        kept = 0
        selected: list[tuple[Path, ExperimentSpec]] = []
        for path, spec in approved_specs:
            if not is_daytona_spec(spec):
                selected.append((path, spec))
                continue
            if kept < allowance:
                kept += 1
                selected.append((path, spec))
                continue
            self._defer_spec_event(spec, reason)
            self._report_progress(
                f"deferred {spec.name} ({reason}"
                + (f": {detail}" if detail else "")
                + f"); state: {path.parent.name}"
            )
        if not any(is_daytona_spec(spec) for _, spec in selected):
            self.last_tick_reason = reason
        return selected

    def _load_smoke_job(self, spec: ExperimentSpec) -> Any | None:
        """Best-effort load of a just-dispatched smoke job; never raises."""
        try:
            from evallab.results import load_job

            return load_job(_safe_repo_path(self.repo_root, spec.jobs_dir) / spec.name)
        except Exception:
            return None

    def _dispatch_decision(
        self, spec: ExperimentSpec, authorization: PaidRunAuthorization | None
    ) -> PolicyDecision:
        # Non-billable dispatch skips accounting the policy never inspects.
        return self.gate.decide(
            spec,
            spent_today_usd=self._effective_spend_today() if spec.billable else 0.0,
            consecutive_harness_failures=self._consecutive_harness_failures()
            if spec.billable
            else 0,
            authorization=authorization,
        )

    def _dispatch_one(
        self,
        path: Path,
        spec: ExperimentSpec,
        authorizations: dict[str, PaidRunAuthorization],
        credentials: frozenset[str],
        *,
        preflight_decision: PolicyDecision | None = None,
    ) -> bool:
        try:
            self._validate_campaign_dispatch_spec(spec, source=path)
        except ExecutionFailure as exc:
            failure = PolicyDecision(
                admitted=False,
                reason_code=exc.reason_code,
                message=str(exc),
            )
            failed = self.queue.transition(
                path,
                "failed",
                actor="executor",
                event="dispatch_refused",
                reason_code=failure.reason_code,
            )
            self.queue.write_reason(self.queue.load(failed), failure)
            return False
        if self.queue.stop_path.exists():
            return False
        missing = missing_credential_for(spec.agent, credentials, model=spec.model)
        if missing is not None:
            self.queue.append_event(
                QueueEvent(
                    event_id=new_ulid(),
                    spec_id=str(spec.spec_id),
                    occurred_at=datetime.now(UTC),
                    event="dispatch_deferred",
                    actor="executor",
                    reason_code=f"missing_credential:{missing}",
                    job_name=spec.name,
                )
            )
            return False
        authorization = authorizations.get(str(spec.spec_id))
        if authorization is not None and (
            authorization.approved_spec_digest != approved_spec_digest(spec)
            or authorization.campaign_manifest_digest != spec.campaign_manifest_digest
            or authorization.campaign_spec_digest != spec.campaign_spec_digest
        ):
            failure = PolicyDecision(
                admitted=False,
                reason_code="paid_run_authorization_stale",
                message="queued spec no longer matches the recorded human authorization",
            )
            failed = self.queue.transition(
                path,
                "failed",
                actor="executor",
                event="dispatch_refused",
                reason_code=failure.reason_code,
            )
            self.queue.write_reason(self.queue.load(failed), failure)
            return False
        decision = (
            preflight_decision
            if preflight_decision is not None
            else self._dispatch_decision(spec, authorization)
        )
        if not decision.admitted:
            if spec.campaign_id is not None and campaign_approval.should_escalate(
                decision.reason_code
            ):
                # Same breach/defect escalation as submit (deduplicated per
                # spec and reason); budget exhaustion fences new launches.
                campaign_approval.escalate_after_refusal(self.repo_root, spec, decision)
                if decision.reason_code == campaign_approval.REASON_BUDGET_EXHAUSTED:
                    self.queue.stop()
            try:
                waiting = self.queue.transition(
                    path,
                    "waiting",
                    actor="executor",
                    event="dispatch_refused",
                    reason_code=decision.reason_code,
                )
                self.queue.write_reason(self.queue.load(waiting), decision)
            except (FileNotFoundError, FileExistsError, ValueError):
                pass
            return False
        no_agent_execution = self._spec_runs_no_agent(spec)
        if not no_agent_execution and not self._ensure_selfhosted_ready(spec):
            return False
        lease_generation = secrets.token_hex(16)
        lease_path = self.queue.acquire_lease(
            spec,
            lease_generation=lease_generation,
        )
        if lease_path is None:
            # Lost claim race or actively leased; tolerated vanished/claimed skip
            return False
        try:
            running = self.queue.transition(
                path,
                "running",
                actor="executor",
                event="dispatch_started",
                policy_rule=decision.policy_rule,
            )
        except (FileNotFoundError, FileExistsError, ValueError):
            self.queue.release_lease(spec, lease_generation=lease_generation)
            return False
        self._report_progress(f"dispatching {spec.name} (spec {spec.spec_id}, agent {spec.agent})")
        try:
            try:
                job_dir = self.execute_spec(
                    spec,
                    lease_generation=lease_generation,
                    allow_rerun=not no_agent_execution,
                )
                if isinstance(job_dir, DiffPreview):
                    self.queue.write_reason(
                        spec,
                        PolicyDecision(
                            admitted=True,
                            reason_code="diff_all_reused",
                            message=json.dumps(job_dir.to_dict(), sort_keys=True),
                            policy_rule=decision.policy_rule,
                        ),
                    )
                    self.queue.transition(
                        running,
                        "done",
                        actor="executor",
                        event="dispatch_reused",
                        reason_code="diff_all_reused",
                        policy_rule=decision.policy_rule,
                    )
                    self._report_progress(f"reused {spec.name}; no new trials or spend")
                    return True
            except Exception as execution_error:
                failed_job_dir = _safe_repo_path(self.repo_root, spec.jobs_dir) / spec.name
                failure_error = execution_error
                try:
                    self._assert_persistent_artifacts_safe(spec, failed_job_dir)
                except ExecutionFailure as scan_failure:
                    failure_error = scan_failure
                reason_code = (
                    failure_error.reason_code
                    if isinstance(failure_error, ExecutionFailure)
                    else "execution_failed"
                )
                deferred = reason_code in {"daytona_usage_limit", "daytona_usage_unavailable"}
                failure = PolicyDecision(
                    admitted=False,
                    reason_code=reason_code,
                    message=(
                        str(failure_error)
                        if deferred
                        else "execution failed; inspect the immutable job evidence and logs "
                        f"({type(failure_error).__name__})"
                    ),
                )
                failed = self.queue.transition(
                    running,
                    "waiting" if deferred else "failed",
                    actor="executor",
                    event="dispatch_deferred" if deferred else "dispatch_failed",
                    reason_code=failure.reason_code,
                )
                self.queue.write_reason(self.queue.load(failed), failure)
                state = "waiting" if deferred else "failed"
                self._report_progress(
                    f"{state} {spec.name} ({failure.reason_code}); state: {state}"
                )
            else:
                failure = self._settle_post_run(
                    job_dir,
                    spec,
                    actor="executor",
                )
                if failure is not None:
                    failure_reason = failure.reason_code or "post_run_failed"
                    failed = self.queue.transition(
                        running,
                        "failed",
                        actor="executor",
                        event=(
                            "post_run_compliance_refused"
                            if failure_reason.startswith("post_run_compliance_")
                            else "post_run_refused"
                        ),
                        reason_code=failure_reason,
                    )
                    self.queue.write_reason(self.queue.load(failed), failure)
                    self._report_progress(
                        f"failed {spec.name} ({failure.reason_code}); state: failed"
                    )
                else:
                    self.queue.transition(
                        running,
                        "done",
                        actor="executor",
                        event="dispatch_completed",
                        policy_rule=decision.policy_rule,
                    )
                    self._report_progress(f"completed {spec.name}; state: done")
            return True
        finally:
            self.queue.release_lease(spec, lease_generation=lease_generation)

    def _apply_docker_clamp(
        self, specs: list[tuple[Path, ExperimentSpec]]
    ) -> list[tuple[Path, ExperimentSpec]]:
        """Bound locked containers by observed host resources, without resizing."""
        if not any(_is_locked_docker_spec(spec) for _, spec in specs):
            return specs
        try:
            cpus, memory_mb = self._docker_observe_fn()
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            self.last_tick_reason = "docker_capacity_unavailable"
            for _, spec in specs:
                if _is_locked_docker_spec(spec):
                    self._defer_spec_event(spec, "docker_capacity_unavailable")
            return [(path, spec) for path, spec in specs if not _is_locked_docker_spec(spec)]
        selected: list[tuple[Path, ExperimentSpec]] = []
        for path, spec in specs:
            if not _is_locked_docker_spec(spec):
                selected.append((path, spec))
                continue
            try:
                task_cpu, task_memory = docker_task_resources(
                    _safe_repo_path(self.repo_root, spec.executable_task_path)
                )
            except (OSError, ValueError, TypeError):
                self._defer_spec_event(spec, "docker_task_resources_unavailable")
                continue
            slots = min(spec.attempts, spec.concurrency)
            if task_cpu * slots > cpus or task_memory * slots > memory_mb:
                self._defer_spec_event(spec, "docker_capacity_clamped")
                continue
            cpus -= task_cpu * slots
            memory_mb -= task_memory * slots
            selected.append((path, spec))
        if not selected:
            self.last_tick_reason = "docker_capacity_no_approved_spec_fits"
        return selected

    def _capacity_batch(
        self,
        approved_specs: list[tuple[Path, ExperimentSpec]],
    ) -> list[tuple[Path, ExperimentSpec]]:
        capacity = self.capacity
        limit = capacity.max_specs_per_tick if capacity is not None else None
        campaign_caps: dict[str, int | None] = {}
        campaign_slots: dict[str, int] = {}
        selected: list[tuple[Path, ExperimentSpec]] = []
        active_trials = 0
        by_agent: dict[str, int] = {}
        for path, spec in approved_specs:
            if limit is not None and len(selected) >= limit:
                break
            slots = min(spec.attempts, spec.concurrency)
            campaign_id = spec.campaign_id
            if campaign_id is not None:
                if campaign_id not in campaign_caps:
                    try:
                        campaign, _ = campaign_approval.load_approved_campaign(
                            self.repo_root, campaign_id
                        )
                        campaign_caps[campaign_id] = (
                            campaign.execution.max_concurrent_trials
                            if campaign.execution is not None
                            else None
                        )
                    except (OSError, ValueError):
                        campaign_caps[campaign_id] = None
                cap = campaign_caps[campaign_id]
                if cap is not None and campaign_slots.get(campaign_id, 0) + slots > cap:
                    continue
            if (
                capacity is not None
                and capacity.max_active_trials is not None
                and active_trials + slots > capacity.max_active_trials
            ):
                continue
            agent_limit = (
                (capacity.per_agent_active_trials or {}).get(spec.agent)
                if capacity is not None
                else None
            )
            if agent_limit is not None and by_agent.get(spec.agent, 0) + slots > agent_limit:
                continue
            selected.append((path, spec))
            active_trials += slots
            by_agent[spec.agent] = by_agent.get(spec.agent, 0) + slots
            if campaign_id is not None:
                campaign_slots[campaign_id] = campaign_slots.get(campaign_id, 0) + slots
        if not selected:
            self.last_tick_reason = "capacity_no_approved_spec_fits"
        return selected

    def _campaign_for_batch(
        self, specs: list[tuple[Path, ExperimentSpec]]
    ) -> campaign_approval.ExperimentCampaign | None:
        """Return only one unchanged approved campaign's execution contract."""
        if not specs or not specs[0][1].campaign_id:
            return None
        campaign_id = specs[0][1].campaign_id
        if any(spec.campaign_id != campaign_id for _, spec in specs):
            return None
        try:
            campaign, _ = campaign_approval.load_approved_campaign(self.repo_root, campaign_id)
        except (OSError, ValueError):
            return None
        if campaign.execution is None or any(
            (spec.agent, spec.model, spec.environment, spec.reference_profile)
            != (campaign.agent, campaign.model, campaign.environment, campaign.reference_profile)
            for _, spec in specs
        ):
            return None
        return campaign

    def _qualified_campaign_batch(self, specs: list[tuple[Path, ExperimentSpec]]) -> bool:
        campaign = self._campaign_for_batch(specs)
        if campaign is None or campaign.execution is None:
            return False
        qualification = campaign.execution.qualification
        if qualification is None:
            return False
        try:
            return all(
                qualification_matches_request(
                    qualification,
                    self.prepare_request(spec, repo_root=self.repo_root),
                    repo_root=self.repo_root,
                )
                for _, spec in specs
            )
        except (OSError, ValueError, ExecutionFailure):
            return False

    def _tick_locked(
        self,
        parallel: int | None = None,
        spec_ids: Sequence[str] | None = None,
    ) -> int:
        self.reconcile_running()
        if self.queue.stop_path.exists():
            return 0
        if self.queue.list_specs("running"):
            # A prior executor may have died while Harbor's detached process
            # was still running and billing. Until that evidence becomes
            # terminal (or an operator resolves it), starting any other work
            # could bypass both the single-owner and daily-cost guarantees.
            self.last_tick_reason = "running_specs_unresolved"
            return 0
        # HAR-193: release a priority wave of next uncertain draws under the
        # existing tick lock, preserving the shared warm-server batch.
        campaign_approval.reconcile_adaptive_sampling(
            self, parallel=parallel, spec_ids=set(spec_ids) if spec_ids is not None else None
        )
        if self.queue.stop_path.exists():
            return 0
        try:
            authorizations = self.queue.authorizations()
        except (OSError, ValueError):
            # Fail closed. Authorisation is proven only from the event log; if
            # the log cannot be read, nothing in this queue can be shown to be
            # authorised, so the whole tick stops rather than guessing.
            self.last_tick_reason = "authorization_ledger_unreadable"
            return 0
        credentials = self._credential_probe()
        approved_specs = self.queue.list_specs("approved")
        if spec_ids is None:
            campaign_specs_present = any(
                spec.campaign_ledger is not None for _path, spec in approved_specs
            )
            approved_specs = [
                (path, spec) for path, spec in approved_specs if spec.campaign_ledger is None
            ]
            if campaign_specs_present and not approved_specs:
                self.last_tick_reason = "campaign_specs_require_campaign_resume"
                return 0
        else:
            allowed = frozenset(spec_ids)
            approved_specs = [
                (path, spec) for path, spec in approved_specs if spec.spec_id in allowed
            ]
        # Smoke eligibility is a property of the tick's approved batch,
        # not of the post-clamp selection: capacity/daytona clamps that
        # leave a single model-backed spec of a multi-model batch must not
        # silently drop the gate. A standalone single model-backed spec is
        # still not a batch (no new smoke requirement).
        smoke_batch = approved_specs
        campaign = self._campaign_for_batch(smoke_batch)
        campaign_parallel = (
            campaign.execution.max_concurrent_trials
            if campaign is not None and campaign.execution is not None
            else None
        )
        parallel = min(parallel or campaign_parallel or 1, campaign_parallel or parallel or 1)
        smoke_required = self._smoke_gate_index(smoke_batch) is not None
        approved_specs = self._capacity_batch(approved_specs)
        if not approved_specs:
            return 0
        # HAR-174 batch boundary, read once per tick: only alerts from
        # running jobs or jobs launched since this instant can fence.
        spike_resume = self.queue.last_resume_time()
        if self._stop_on_infra_spike(approved_specs, resume_time=spike_resume):
            return 0
        approved_specs = self._apply_daytona_clamp(approved_specs)
        approved_specs = self._apply_docker_clamp(approved_specs)
        if not approved_specs:
            return 0

        smoke_index = (
            next(
                (
                    position
                    for position, (_, item) in enumerate(approved_specs)
                    if is_model_backed(item) and not self._spec_runs_no_agent(item)
                ),
                None,
            )
            if smoke_required
            else None
        )
        if smoke_index is None:
            self._record_smoke_opt_out(smoke_batch)
            dispatched = self._dispatch_batch(
                approved_specs, parallel, authorizations, credentials, resume_time=spike_resume
            )
            return dispatched
        smoke_path, smoke_spec = approved_specs[smoke_index]
        rest = approved_specs[:smoke_index] + approved_specs[smoke_index + 1 :]
        dispatched = 0
        smoke_ran = self._dispatch_one(smoke_path, smoke_spec, authorizations, credentials)
        if smoke_ran:
            dispatched += 1
        smoke_job_dir = self._job_dir_for(smoke_spec) if smoke_ran else None
        if self.queue.stop_path.exists() or (
            smoke_job_dir is not None
            and self._stop_on_job_spike(smoke_job_dir, rest, resume_time=spike_resume)
        ):
            return dispatched
        if not smoke_ran:
            blocks, why = True, "smoke trial did not dispatch; holding batch"
        else:
            # A dispatched attempt may fail while an older or partial graded
            # job remains at its path. That evidence cannot release this batch.
            try:
                self.queue.locate(str(smoke_spec.spec_id), ("done",))
            except (OSError, ValueError):
                blocks, why = True, "smoke dispatch did not complete successfully"
            else:
                blocks, why = smoke_trial_blocks(self._load_smoke_job(smoke_spec))
        if blocks:
            self._fence_batch_on_smoke_block(smoke_spec, smoke_batch, why)
            return dispatched
        self._report_progress(f"smoke gate passed on {smoke_spec.name} ({why})")
        dispatched += self._dispatch_batch(
            rest, parallel, authorizations, credentials, resume_time=spike_resume
        )
        return dispatched

    def _spec_runs_no_agent(self, spec: ExperimentSpec) -> bool:
        if spec.diff_sources == [] or (
            spec.diff_sources is None and not (spec.grid_id or spec.campaign_attempt_id)
        ):
            return False
        try:
            request = self.prepare_request(spec, repo_root=self.repo_root)
            return preview_diff(request, repo_root=self.repo_root).counts["rerun"] == 0
        except (OSError, ValueError, RuntimeError, ImportError):
            # The authoritative dispatch records the refusal; a failed preview
            # must never exempt real execution from the smoke gate.
            return False

    def _smoke_gate_index(self, approved_specs: list[tuple[Path, ExperimentSpec]]) -> int | None:
        """Position of the smoke trial, or ``None`` when the gate is off.

        Default-on for batches with more than one model-backed spec: the
        first model-backed spec runs alone so an infra or wiring failure
        blocks the rest before they spend.
        """
        if not self._smoke_gate_enabled:
            return None
        backed = [
            index
            for index, (_, spec) in enumerate(approved_specs)
            if is_model_backed(spec) and not self._spec_runs_no_agent(spec)
        ]
        if len(backed) < 2:
            return None
        if self._qualified_campaign_batch([approved_specs[index] for index in backed]):
            return None
        return backed[0]

    def _fence_batch_on_smoke_block(
        self,
        smoke_spec: ExperimentSpec,
        batch: list[tuple[Path, ExperimentSpec]],
        why: str,
    ) -> None:
        """Fence future launches durably before recording the smoke failure."""
        self.queue.stop()
        self.last_tick_reason = SMOKE_REASON_BLOCKED
        self.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=str(smoke_spec.spec_id),
                occurred_at=datetime.now(UTC),
                event=SMOKE_REASON_BLOCKED,
                actor="executor",
                reason_code=SMOKE_REASON_BLOCKED,
                job_name=smoke_spec.name,
            )
        )
        for _, spec in batch:
            if spec.spec_id != smoke_spec.spec_id:
                self._defer_spec_event(spec, f"{SMOKE_REASON_BLOCKED}:{smoke_spec.name}")
        self._report_progress(
            f"smoke gate blocked batch on {smoke_spec.name} ({why}); "
            "remaining specs stay approved; clear STOP only after resolving the cause"
        )

    def _record_smoke_opt_out(self, approved_specs: list[tuple[Path, ExperimentSpec]]) -> None:
        """Record a scoped qualification waiver or the existing explicit opt-out."""
        qualified = self._smoke_gate_enabled and self._qualified_campaign_batch(approved_specs)
        if self._smoke_gate_enabled and not qualified:
            return
        backed = [spec for _, spec in approved_specs if is_model_backed(spec)]
        if len(backed) < 2:
            return
        first = backed[0]
        self.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=str(first.spec_id),
                occurred_at=datetime.now(UTC),
                event="smoke_gate_campaign_qualified" if qualified else SMOKE_REASON_DISABLED,
                actor="executor",
                reason_code=(
                    f"campaign_qualified:{first.campaign_id}"
                    if qualified
                    else SMOKE_REASON_DISABLED
                ),
                job_name=first.name,
            )
        )
        self._report_progress(
            f"smoke gate waived by {'approved setup qualification' if qualified else 'explicit opt-out'} "
            f"for batch of {len(backed)} model-backed specs"
        )

    def _dispatch_serial(
        self,
        batch: list[tuple[Path, ExperimentSpec]],
        authorizations: dict[str, PaidRunAuthorization],
        credentials: frozenset[str],
        *,
        resume_time: datetime | None,
    ) -> int:
        dispatched = 0
        for index, (path, spec) in enumerate(batch):
            if self.queue.stop_path.exists():
                break
            if self._dispatch_one(path, spec, authorizations, credentials):
                dispatched += 1
                job_dir = self._job_dir_for(spec)
                if job_dir is not None and self._stop_on_job_spike(
                    job_dir, batch[index + 1 :], resume_time=resume_time
                ):
                    break
        return dispatched

    def _dispatch_batch(
        self,
        batch: list[tuple[Path, ExperimentSpec]],
        parallel: int,
        authorizations: dict[str, PaidRunAuthorization],
        credentials: frozenset[str],
        *,
        resume_time: datetime | None,
    ) -> int:
        if parallel == 1:
            return self._dispatch_serial(
                batch, authorizations, credentials, resume_time=resume_time
            )
        # Indexed campaign draws form one reserved wave. Decide before workers
        # move specs between queue states: concurrent per-worker snapshots can
        # otherwise count one reservation in both approved and running/done.
        # This remains the existing policy gate and HAR-189 spend accounting.
        preflight = {
            str(spec.spec_id): self._dispatch_decision(spec, authorizations.get(str(spec.spec_id)))
            for _, spec in batch
            if spec.campaign_task_attempt is not None
        }
        dispatched = 0
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = [
                pool.submit(
                    self._dispatch_one,
                    path,
                    spec,
                    authorizations,
                    credentials,
                    preflight_decision=preflight.get(str(spec.spec_id)),
                )
                for path, spec in batch
            ]
            for future in futures:
                if future.result():
                    dispatched += 1
        return dispatched

    def _maybe_stop_selfhosted_app(
        self,
        selected_specs: Sequence[ExperimentSpec],
    ) -> None:
        """Stop the Modal server when self-hosted work just drained.

        Candidates are specs that were running when the tick started plus
        specs approved for this dispatch round; the hook itself checks which
        reached a terminal state and whether any self-hosted work remains.
        A no-op unless a teardown hook was injected. Teardown failures never
        fail the tick: the hook records them in job evidence and queue events.
        """
        hook = self._modal_teardown
        if hook is None:
            return
        candidates = [spec for spec in selected_specs if is_mimo_selfhosted_model(spec.model)]
        if not candidates:
            return
        # The hook already waits while any self-hosted spec remains; skip it
        # only when nothing in this window belongs to the Modal app.
        if all(self._runpod_owned(spec) for spec in candidates):
            self._report_progress("Modal teardown not applicable to the Runpod-owned window")
            return
        try:
            record = hook(self.queue, self.repo_root, candidates)
        except Exception as exc:
            self._report_progress(f"modal teardown skipped: {type(exc).__name__}")
            return
        if record is not None:
            reason = record.get("reason")
            if record.get("stopped"):
                self._report_progress(
                    f"modal teardown: stopped {record.get('app')} "
                    f"(state={record.get('app_state')}, "
                    f"containers={record.get('container_count')})"
                )
            elif reason not in (None, "queue-not-drained"):
                self._report_progress(f"modal teardown skipped: {reason}")

    def _runpod_owned(self, spec: ExperimentSpec) -> bool:
        campaign = self._campaign_for_batch([(Path(), spec)])
        return (
            campaign is not None
            and campaign.execution is not None
            and campaign.execution.model_host == "runpod"
        )

    @staticmethod
    def prepare_request(
        spec: ExperimentSpec,
        *,
        repo_root: Path,
        lease_path: Path | None = None,
        lease_generation: str | None = None,
    ) -> RunRequest:
        """Resolve execution inputs without opening policy, leases, or storage."""
        repo_root = repo_root.resolve()
        if spec.provider_routes:
            raise ExecutionFailure(
                "provider_routes_unsupported",
                "provider route constraints require a route-aware executor; "
                "this executor cannot dispatch them as an unconstrained single route",
            )
        task_path = _safe_repo_path(repo_root, spec.executable_task_path)
        task_version = spec.task_version
        verifier_digest = spec.verifier_digest
        package_digest = None
        timeout_seconds = spec.timeout_seconds
        canonical_task_path = spec.executable_task_path
        task_id = spec.task_id

        if spec.task.startswith("registered/"):
            reg = TaskRegistry.from_repo(repo_root)
            resolved = reg.resolve_spec(spec, repo_root)
            if resolved is None:
                raise ExecutionFailure(
                    "unregistered_task",
                    f"task {spec.task!r} is not registered in library/registry/",
                )
            task_path = _safe_repo_path(repo_root, resolved.task_path)
            canonical_task_path = resolved.task_path
            task_version = resolved.version
            verifier_digest = resolved.digests.verifier
            package_digest = resolved.digests.package
            task_id = resolved.task_id
            timeout_seconds = min(spec.timeout_seconds, resolved.limits.timeout_seconds)
        elif spec.task_package_digest is not None:
            digests = compute_task_digests(task_path)
            if digests.package != spec.task_package_digest or (
                spec.verifier_digest is not None and digests.verifier != spec.verifier_digest
            ):
                raise ExecutionFailure(
                    "task_digest_mismatch",
                    "local campaign task package differs from its frozen digest",
                )
            package_digest = digests.package
        if spec.task_package_digest is not None and package_digest != spec.task_package_digest:
            raise ExecutionFailure(
                "task_digest_mismatch",
                "resolved task package differs from the frozen campaign digest",
            )
        grid_point = spec.grid_point if isinstance(spec.grid_point, dict) else {}
        bound_values = (
            dict(grid_point["bindings"]) if isinstance(grid_point.get("bindings"), dict) else {}
        )
        factor_values = (
            dict(grid_point["factors"]) if isinstance(grid_point.get("factors"), dict) else {}
        )
        factor_bindings = (
            dict(grid_point["factor_bindings"])
            if isinstance(grid_point.get("factor_bindings"), dict)
            else {}
        )
        factor_bindings_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(factor_bindings, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        declared_binding_digest = grid_point.get("factor_bindings_digest")
        if (
            declared_binding_digest is not None
            and declared_binding_digest != factor_bindings_digest
        ):
            raise ExecutionFailure(
                "factor_binding_unhonored",
                "factor-name binding map does not match its declared digest",
            )
        if set(factor_values) != set(factor_bindings):
            raise ExecutionFailure(
                "factor_binding_unhonored",
                "factor values and factor-name bindings do not name the same coordinates",
            )
        for factor_name, level in factor_values.items():
            binding = factor_bindings[factor_name]
            if bound_values.get(binding) != level:
                raise ExecutionFailure(
                    "factor_binding_unhonored",
                    f"factor {factor_name!r} level {level!r} does not match "
                    f"bound execution value {binding!r}={bound_values.get(binding)!r}",
                )
        resolved_execution_values = {
            "concurrency": spec.concurrency,
            "timeout_seconds": timeout_seconds,
        }
        for binding, expected in bound_values.items():
            if (
                binding not in resolved_execution_values
                or resolved_execution_values[binding] != expected
            ):
                raise ExecutionFailure(
                    "factor_binding_unhonored",
                    f"factor binding {binding!r} requested {expected!r} but execution "
                    f"resolved {resolved_execution_values.get(binding)!r}",
                )
        stored_point_id = grid_point.get("point_id")
        if stored_point_id is not None:
            point_agent = str(grid_point.get("agent") or spec.agent)
            point_model = grid_point.get("model") or spec.model
            point_agent_key = (
                f"{point_agent}-{point_model}"
                if point_model and point_agent not in CONTROL_ADAPTERS
                else point_agent
            )
            expected_point_id = canonical_grid_point_id(
                task_ref=str(grid_point.get("task_ref") or grid_point.get("task") or spec.task),
                agent_key=point_agent_key,
                preamble=(
                    str(grid_point["preamble"]) if grid_point.get("preamble") is not None else None
                ),
                k=int(grid_point.get("k") or spec.attempts),
                arm_id=(str(grid_point["arm_id"]) if grid_point.get("arm_id") else None),
                factor_values=factor_values,
                factor_bindings=factor_bindings,
            )
            if stored_point_id != expected_point_id:
                raise ExecutionFailure(
                    "grid_point_identity_mismatch",
                    "stored point_id does not match canonical runnable coordinates",
                )

        jobs_dir = _safe_repo_path(repo_root, spec.jobs_dir)
        # A field the dispatcher never forwards is the defect class this repo keeps
        # finding, so the elicitation preamble is resolved here beside jobs_dir.
        extra_instruction_path = (
            _safe_repo_path(repo_root, spec.extra_instruction_path)
            if spec.extra_instruction_path
            else None
        )
        actual_preamble_hash = (
            f"sha256:{hashlib.sha256(extra_instruction_path.read_bytes()).hexdigest()}"
            if extra_instruction_path is not None and extra_instruction_path.is_file()
            else None
        )
        declared_preamble_hash = spec.extra_instruction_sha256 or (
            str(grid_point["preamble_sha256"]) if grid_point.get("preamble_sha256") else None
        )
        if extra_instruction_path is not None and actual_preamble_hash is None:
            raise ExecutionFailure(
                "preamble_missing",
                f"preamble file does not exist: {spec.extra_instruction_path!r}",
            )
        if declared_preamble_hash and actual_preamble_hash != declared_preamble_hash:
            raise ExecutionFailure(
                "preamble_provenance_mismatch",
                f"preamble {spec.extra_instruction_path!r} no longer matches "
                f"declared digest {declared_preamble_hash}",
            )
        toolbox_path = repo_root / spec.toolbox_path if spec.toolbox_path else None
        if bool(toolbox_path) != bool(spec.toolbox_sha256):
            raise ExecutionFailure(
                "toolbox_pair_required",
                "toolbox_path and toolbox_sha256 must be provided together",
            )
        if toolbox_path is not None:
            if toolbox_path.is_symlink():
                raise ExecutionFailure(
                    "toolbox_symlink_rejected",
                    f"toolbox path cannot be a symlink: {spec.toolbox_path!r}",
                )
            if not toolbox_path.is_file():
                raise ExecutionFailure(
                    "toolbox_missing",
                    f"toolbox file does not exist: {spec.toolbox_path!r}",
                )
            from evallab.toolbox import (
                TOOLBOX_SUPPORTED_AGENTS,
                validate_toolbox_source,
            )

            if spec.agent not in TOOLBOX_SUPPORTED_AGENTS:
                raise ExecutionFailure(
                    "unsupported_toolbox_target",
                    f"agent {spec.agent!r} does not support python toolbox; "
                    f"supported agents are {sorted(TOOLBOX_SUPPORTED_AGENTS)}",
                )
            if spec.agent == ZAI_OPENCODE_AGENT:
                model = spec.model or DEFAULT_AGENT_MODELS.get(spec.agent)
                if model not in ZAI_OPENCODE_MODEL_SELECTORS:
                    raise ExecutionFailure(
                        "unsupported_toolbox_profile",
                        f"zai-opencode requires one of the exact models {sorted(ZAI_OPENCODE_MODEL_SELECTORS)}",
                    )
            if spec.environment != "docker":
                raise ExecutionFailure(
                    "unsupported_toolbox_environment",
                    "toolbox execution requires environment='docker'",
                )
            try:
                validate_toolbox_source(toolbox_path, spec.toolbox_sha256, repo_root=repo_root)
            except ValueError as exc:
                raise ExecutionFailure("toolbox_validation_failed", str(exc)) from exc

        request = RunRequest(
            task=task_path,
            extra_instruction_path=extra_instruction_path,
            toolbox_path=toolbox_path,
            toolbox_sha256=spec.toolbox_sha256,
            harness_tree_path=(
                _safe_repo_path(repo_root, spec.harness_tree_path)
                if spec.harness_tree_path
                else None
            ),
            harness_tree_sha256=spec.harness_tree_sha256,
            agent=spec.agent,
            name=spec.name,
            jobs_dir=jobs_dir,
            environment=spec.environment,
            egress_lock=spec.egress_lock,
            # Harbor's installed agents hard-require a model name; specs that
            # do not pin one fall back to the per-agent default.
            model=spec.model or DEFAULT_AGENT_MODELS.get(spec.agent),
            concurrency=spec.concurrency,
            attempts=spec.attempts,
            timeout_seconds=timeout_seconds,
            allow_billable=spec.billable,
            max_requests=spec.max_requests,
            max_input_tokens=spec.max_input_tokens,
            max_output_tokens=spec.max_output_tokens,
            max_total_tokens=spec.max_total_tokens,
            cost_limit_usd=spec.cost_limit_usd,
            harness_policy=spec.harness_policy,
            verifier_repeat_n=spec.verifier_repeat_n,
            override_storage_mb=spec.override_storage_mb,
            lease_path=lease_path,
            lease_generation=lease_generation,
            experiment_spec=spec.model_copy(deep=True),
            provenance=RunProvenance(
                spec_id=str(spec.spec_id),
                task=spec.task,
                task_version=task_version,
                verifier_digest=verifier_digest,
                policy_rule=spec.policy_rule,
                package_digest=package_digest,
                task_path=canonical_task_path,
                grid_id=spec.grid_id,
                point_id=(str(grid_point["point_id"]) if grid_point.get("point_id") else None),
                arm_id=str(grid_point["arm_id"]) if grid_point.get("arm_id") else None,
                factor_values=factor_values or None,
                factor_bindings=factor_bindings or None,
                factor_bindings_digest=(factor_bindings_digest if factor_bindings else None),
                bound_execution_values=bound_values or None,
                preamble_path=spec.extra_instruction_path,
                preamble_sha256=actual_preamble_hash,
                toolbox_path=spec.toolbox_path,
                toolbox_sha256=spec.toolbox_sha256,
                harness_tree_path=spec.harness_tree_path,
                harness_tree_sha256=spec.harness_tree_sha256,
                task_family=spec.task_family,
                task_id=task_id,
                task_instance_id=spec.task_instance_id,
                generator_seed=spec.generator_seed,
                campaign_ledger=spec.campaign_ledger,
                campaign_cell_id=spec.campaign_cell_id,
                campaign_attempt_id=spec.campaign_attempt_id,
                campaign_attempt_index=spec.campaign_attempt_index,
                campaign_manifest_digest=spec.campaign_manifest_digest,
                campaign_spec_digest=spec.campaign_spec_digest,
                linear_card=spec.linear_card,
            ),
        )
        return request

    def execute_spec(
        self,
        spec: ExperimentSpec,
        *,
        lease_generation: str | None = None,
        allow_rerun: bool = True,
    ) -> Path | DiffPreview:
        request = self.prepare_request(
            spec,
            repo_root=self.repo_root,
            lease_path=self.queue.lease_path(spec),
            lease_generation=lease_generation,
        )
        try:
            preview = preview_diff(request, repo_root=self.repo_root)
        except (OSError, ValueError, ImportError) as exc:
            raise ExecutionFailure("diff_preflight_failed", str(exc)) from exc
        self._report_progress(render_diff(preview).rstrip())
        if preview.all_reused:
            return preview
        if preview.counts["rerun"] and not allow_rerun:
            raise ExecutionFailure(
                "diff_inputs_changed",
                "diff changed after the readiness decision; re-preflight before execution",
            )
        self._report_progress(
            f"child started for {spec.name}; progress log: "
            f"{self.repo_root / spec.jobs_dir / '.executor' / (spec.name + '.log')}"
        )
        request = replace(request, diff_sources=preview.sources)
        job_dir = self._run_with_transient_retries(spec, request)
        if preview.sources or preview.warnings:
            (job_dir / "diff.json").write_text(
                json.dumps(preview.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        self._assert_persistent_artifacts_safe(spec, job_dir)
        return job_dir

    def _run_with_transient_retries(
        self,
        spec: ExperimentSpec,
        request: RunRequest,
    ) -> Path:
        # HAR-162: every agent job gets a read-only live watch, stopped on
        # every terminal path (success, failure, cancel) in the finally.
        # Model-free agents are skipped inside; watch faults never fail here.
        from evallab import auto_watch as _auto_watch

        watch = _auto_watch.start_for_request(
            request,
            enabled=self._watch_enabled,
            interval_seconds=self._watch_interval_seconds,
            notify_runner=self._notify_runner,
        )
        try:
            return self._run_with_transient_retries_watched(spec, request)
        finally:
            _auto_watch.stop_auto_watch(watch, notify_runner=self._notify_runner)

    def _run_with_transient_retries_watched(
        self,
        spec: ExperimentSpec,
        request: RunRequest,
    ) -> Path:
        for retry_number in range(self._max_transient_retries + 1):
            self._reserve_attempt(spec, retry_number + 1)
            try:
                return self._runner(request)
            except TransientHarnessFailure as exc:
                if retry_number >= self._max_transient_retries:
                    raise
                if not self._retry_within_policy(spec):
                    raise
                archived = self._archive_transient_attempt(
                    spec,
                    request,
                    retry_number + 1,
                )
                delay = min(
                    TRANSIENT_BACKOFF_BASE_SECONDS * (2**retry_number),
                    TRANSIENT_BACKOFF_CAP_SECONDS,
                )
                self.queue.append_event(
                    QueueEvent(
                        event_id=new_ulid(),
                        spec_id=str(spec.spec_id),
                        occurred_at=datetime.now(UTC),
                        event="dispatch_retry_scheduled",
                        actor="executor",
                        reason_code=exc.reason_code,
                        job_name=spec.name,
                    )
                )
                if archived is not None:
                    self.queue.append_event(
                        QueueEvent(
                            event_id=new_ulid(),
                            spec_id=str(spec.spec_id),
                            occurred_at=datetime.now(UTC),
                            event="transient_attempt_archived",
                            actor="executor",
                            reason_code="transient_harness:attempt_archived",
                            job_name=spec.name,
                        )
                    )
                self._sleeper(delay)
        raise AssertionError("transient retry loop exhausted without a result")

    def _retry_within_policy(self, spec: ExperimentSpec) -> bool:
        if not spec.billable:
            return True
        try:
            authorization = self.queue.authorization_for(spec)
        except (OSError, ValueError):
            authorization = None
        decision = self.gate.decide(
            spec,
            spent_today_usd=self._effective_spend_today(),
            consecutive_harness_failures=self._consecutive_harness_failures(),
            authorization=authorization,
        )
        if decision.admitted:
            return True
        self.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=str(spec.spec_id),
                occurred_at=datetime.now(UTC),
                event="dispatch_retry_refused",
                actor="executor",
                reason_code=f"transient_retry:{decision.reason_code}",
                job_name=spec.name,
            )
        )
        return False

    def _reserve_attempt(self, spec: ExperimentSpec, attempt_number: int) -> None:
        if not spec.billable:
            return
        self.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=str(spec.spec_id),
                occurred_at=datetime.now(UTC),
                event="dispatch_attempt_reserved",
                actor="executor",
                reason_code="billable_attempt_estimate",
                job_name=spec.name,
                attempt_number=attempt_number,
                estimated_cost_usd=spec.est_cost_usd,
            )
        )

    def _reserved_attempt_spend_today(self, *, now: datetime | None = None) -> float:
        today = (now or datetime.now(UTC)).astimezone(UTC).date()
        reservations: dict[str, list[float]] = {}
        completed: set[str] = set()
        for event in load_events(self.queue.events_path):
            if event.occurred_at.astimezone(UTC).date() != today:
                continue
            if event.event == "dispatch_attempt_reserved" and event.estimated_cost_usd is not None:
                reservations.setdefault(event.spec_id, []).append(event.estimated_cost_usd)
            elif event.event in {"dispatch_completed", "running_reconciled"}:
                completed.add(event.spec_id)
        total = 0.0
        for spec_id, estimates in reservations.items():
            # The catalog accounts for the final successful attempt. Earlier
            # failed attempts, and every attempt of a failed spec, remain
            # conservatively reserved in the event ledger.
            unsettled = estimates[:-1] if spec_id in completed else estimates
            total += sum(unsettled)
        return total

    def _effective_spend_today(self) -> float:
        return self._spent_today() + self._reserved_attempt_spend_today()

    def _archive_transient_attempt(
        self,
        spec: ExperimentSpec,
        request: RunRequest,
        retry_number: int,
    ) -> Path | None:
        job_dir = request.jobs_dir / request.name
        if not job_dir.exists():
            return None
        self._assert_persistent_artifacts_safe(spec, job_dir)
        archive = (
            request.jobs_dir / ".transient-attempts" / request.name / f"attempt-{retry_number}"
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            raise FileExistsError(f"transient attempt archive already exists: {archive}")
        job_dir.replace(archive)
        return archive

    def download_dataset(self, dataset_ref: str, output_dir: Path) -> Path:
        """Download an immutable Harbor dataset through the executor boundary."""
        if "@" not in dataset_ref:
            raise ValueError("dataset downloads require an explicit immutable version")
        ref = dataset_ref.rsplit("@", 1)[1].lower()
        if ref in {"latest", "head", "main", "master"}:
            raise ValueError("dataset downloads cannot use a mutable ref")
        destination = output_dir.resolve()
        if destination.exists() and any(destination.iterdir()):
            raise FileExistsError(f"dataset download destination is not empty: {destination}")
        destination.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                "harbor",
                "dataset",
                "download",
                dataset_ref,
                "--output-dir",
                str(destination),
                "--export",
            ],
            cwd=self.repo_root,
            check=False,
            env=subscription_environment(),
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Harbor dataset download exited {completed.returncode}")
        return destination

    def execute_direct(self, request: RunRequest, *, ingest: bool = True) -> Path:
        if request.agent not in CONTROL_AGENTS:
            raise ValueError(
                "direct execution is restricted to oracle/nop; --allow-billable records "
                "spend consent but does not bypass the standing-policy queue"
            )
        job_dir = self._runner(request)
        if ingest:
            ingest_result = self._ingester(job_dir)
            if ingest_result is not None:
                provenance = request.provenance
                record_projection_failures(
                    self.queue,
                    ingest_result,
                    actor="executor-direct",
                    spec_id=(
                        provenance.spec_id if provenance is not None else f"system-{new_ulid()}"
                    ),
                )
        return job_dir

    def local_runtime_checks(self) -> list[tuple[str, bool, str]]:
        """Inspect executable runtimes through the executor's process boundary."""
        checks: list[tuple[str, bool, str]] = []
        for command in ("harbor", "docker", "uv"):
            version = tool_version(command)
            checks.append((command, version is not None, version or "not found"))
        if shutil.which("docker"):
            try:
                completed = subprocess.run(
                    [
                        "docker",
                        "version",
                        "--format",
                        "client={{.Client.Version}} server={{.Server.Version}}",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=SUPPORT_COMMAND_TIMEOUT_SECONDS,
                    env=subscription_environment(),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                checks.append(("docker-daemon", False, f"unavailable: {type(exc).__name__}"))
            else:
                output = (completed.stdout or completed.stderr).strip().splitlines()
                detail = output[0] if output else "no version output"
                checks.append(("docker-daemon", completed.returncode == 0, detail))
        return checks

    def _running_state_timed_out(self, spec: ExperimentSpec) -> bool:
        state_path = (
            _safe_repo_path(self.repo_root, spec.jobs_dir) / ".executor" / f"{spec.name}.state.json"
        )
        try:
            state = json.loads(state_path.read_text())
            started = datetime.fromisoformat(str(state["started_at"]))
            timeout = float(state.get("job_timeout_seconds", spec.timeout_seconds))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        if str(state.get("status")) != "running":
            return False
        return datetime.now(UTC) >= started + timedelta(seconds=timeout)

    def reconcile_running(self) -> None:
        for path, spec in self.queue.list_specs("running"):
            try:
                self._validate_campaign_dispatch_spec(spec, source=path)
            except ExecutionFailure as failure:
                self._fail_reconciled_running(
                    path,
                    spec,
                    reason_code=failure.reason_code,
                    message=str(failure),
                )
                continue
            if self._running_state_timed_out(spec):
                self._fail_reconciled_running(
                    path,
                    spec,
                    reason_code="trial_wall_clock_timeout",
                    message=(
                        "executor state exceeded the spec timeout; the child was "
                        "not observed to settle after restart. Inspect the progress "
                        f"log under {_safe_repo_path(self.repo_root, spec.jobs_dir) / '.executor'}"
                    ),
                )
                continue
            job_dir = _safe_repo_path(self.repo_root, spec.jobs_dir) / spec.name
            archive_root = (
                _safe_repo_path(self.repo_root, spec.jobs_dir) / ".transient-attempts" / spec.name
            )
            if not job_dir.exists():
                try:
                    interrupted_retry = archive_root.is_dir() and any(
                        child.is_dir() for child in archive_root.iterdir()
                    )
                except OSError:
                    interrupted_retry = False
                if interrupted_retry:
                    self._fail_reconciled_running(
                        path,
                        spec,
                        reason_code="transient_harness:retry_interrupted",
                        message=(
                            "executor stopped between transient attempts; preserved "
                            "attempt evidence requires operator resubmission"
                        ),
                    )
                continue
            result_path = job_dir / "result.json"
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, TypeError, json.JSONDecodeError):
                self._fail_reconciled_running(
                    path,
                    spec,
                    reason_code="running_reconcile_incomplete_evidence",
                    message="terminal job evidence is unreadable; refusing reconciliation",
                )
                continue
            if not isinstance(result, dict) or result.get("finished_at") is None:
                continue
            try:
                job = load_job(job_dir)
            except Exception:
                self._fail_reconciled_running(
                    path,
                    spec,
                    reason_code="running_reconcile_incomplete_evidence",
                    message="terminal trial evidence is unreadable; refusing reconciliation",
                )
                continue
            if not job.trials:
                self._fail_reconciled_running(
                    path,
                    spec,
                    reason_code="running_reconcile_incomplete_evidence",
                    message="terminal job has no trial evidence; refusing reconciliation",
                )
                continue
            transient_reason = next(
                (
                    reason
                    for trial in job.trials
                    if (reason := transient_provider_exception(trial.result)) is not None
                ),
                None,
            )
            if transient_reason is not None:
                self._fail_reconciled_running(
                    path,
                    spec,
                    reason_code=transient_reason,
                    message=(
                        "executor stopped after a transient provider failure; "
                        "preserved evidence requires operator resubmission"
                    ),
                )
                continue
            failure = self._settle_post_run(
                job_dir,
                spec,
                actor="executor-reconcile",
            )
            if failure is not None:
                failure_reason = failure.reason_code or "post_run_failed"
                self._fail_reconciled_running(
                    path,
                    spec,
                    reason_code=failure_reason,
                    message=failure.message,
                )
                continue
            self.queue.transition(
                path,
                "done",
                actor="executor-reconcile",
                event="running_reconciled",
                policy_rule=spec.policy_rule,
            )

    def _fail_reconciled_running(
        self,
        path: Path,
        spec: ExperimentSpec,
        *,
        reason_code: str,
        message: str,
    ) -> None:
        decision = PolicyDecision(
            admitted=False,
            reason_code=reason_code,
            message=message,
        )
        failed = self.queue.transition(
            path,
            "failed",
            actor="executor-reconcile",
            event="running_reconcile_failed",
            reason_code=reason_code,
            policy_rule=spec.policy_rule,
        )
        self.queue.write_reason(self.queue.load(failed), decision)

    def _run_harbor(self, request: RunRequest) -> Path:
        return run_experiment(request, repo_root=self.repo_root)

    def _ingest(self, job_dir: Path) -> IngestProjectionResult:
        url = database_url_from_environment()
        return ingest_and_project(
            url,
            [load_job(job_dir)],
            root=self.repo_root,
            output_root=derived_root_from_environment(self.repo_root),
        )

    def _catalog_spend(self) -> float:
        # Gate rule (HAR-104): the gate spends used + attempted as a
        # conservative ceiling (database.daily_cost_usd sums both columns)
        # while the catalog cost_usd stays settled ledger usage only.
        # In-flight queue reservations ride on top via
        # _effective_spend_today, not in this figure.
        try:
            return database.daily_cost_usd(
                database_url_from_environment(),
                datetime.now(UTC).date(),
            )
        except Exception as exc:
            raise RuntimeError(
                "cannot enforce cost policy because the catalog is unavailable"
            ) from exc

    def _catalog_harness_failures(self) -> int:
        try:
            return database.consecutive_harness_failures(database_url_from_environment())
        except Exception as exc:
            raise RuntimeError(
                "cannot enforce failure policy because the catalog is unavailable"
            ) from exc


def _safe_component(value: str) -> str:
    cleaned = "".join(character if character.isalnum() else "-" for character in value.lower())
    return cleaned.strip("-") or "agent"


def load_events(path: Path) -> list[QueueEvent]:
    events: list[QueueEvent] = []
    for segment, line_number, line in read_event_log_lines(path):
        if not line.strip():
            continue
        try:
            events.append(QueueEvent.model_validate_json(line))
        except ValidationError as exc:
            raise ValueError(f"Invalid queue event at {segment}:{line_number}: {exc}") from exc
    return events


def read_spec(path: Path) -> ExperimentSpec:
    return ExperimentSpec.model_validate_json(path.read_text())
