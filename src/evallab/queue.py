from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import math
import os
import platform
import secrets
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evallab import database
from evallab.credentials import (
    DEFAULT_AGENT_MODELS,
    available_credentials,
    missing_credential_for,
)
from evallab.eventlog import event_log_lock, read_event_log_lines
from evallab.evidence.atif import IngestProjectionResult, ingest_and_project, project_trial
from evallab.evidence_store import (
    EvidenceArchive,
    EvidenceLocator,
    archive_evidence,
    materialize_evidence,
    materialize_evidence_at,
)
from evallab.execution_contracts import (
    ZAI_OPENCODE_AGENT,
    DispatchCapacity,
    PaidRunAuthorization,
    is_lease_generation,
    load_policy,
    new_ulid,
)
from evallab.interpretation.trajectory_compliance import (
    ComplianceDisposition,
    PlatformSettlement,
    TrialEvidenceBundle,
    evaluate_trial_compliance,
)
from evallab.network_isolation_runtime import current_dispatch_isolation_identity
from evallab.profiles import (
    CONTROL_ADAPTERS,
    AgentProfile,
    ProfileState,
    SecurityRunner,
    compute_qualification_digest,
    evaluate_profile_readiness,
    load_readiness_record,
    save_readiness_record,
)
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
    task_runtime_identity,
)
from evallab.results import duration_seconds, load_job
from evallab.runner import (
    CONTROL_AGENTS,
    SUPPORT_COMMAND_TIMEOUT_SECONDS,
    ExecutionFailure,
    RunRequest,
    SettledRun,
    TransientHarnessFailure,
    _evidence_store_root,
    _settle_completed_job,
    assert_no_secret_material,
    collected_secret_values,
    database_url_from_environment,
    run_experiment,
    subscription_environment,
    tool_version,
    transient_provider_exception,
)
from evallab.schemas import (
    CAUSAL_EXPERIMENT_PURPOSES,
    EXPERIMENT_PURPOSES,
    AgentGateEvaluations,
    AgentQualificationDigest,
    AgentReadinessRecord,
    AgentSmokeRecord,
    AutoRunRule,
    ExperimentSpec,
    NetworkIsolationDispatchIdentityV1,
    NetworkIsolationEvidenceV1,
    PolicyDecision,
    QueueEvent,
    QueueReason,
    QueueState,
    RunProvenance,
    StandingApprovalsPolicy,
    canonical_grid_point_id,
)
from evallab.storage.paths import derived_root_from_environment


class _CampaignDispatchValidationError(ExecutionFailure):
    """Campaign authority failed before runner execution."""


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
    subscription = provider_subscription_description(spec.agent)
    return (
        f"{spec.agent} is a billable agent. Paid execution here draws on "
        f"{subscription}, so it never runs unattended: this spec waits until "
        "a named human authorises it.\n"
        f"  authorise: uv run evallab approve {spec_id} --actor <you>\n"
        f'  refuse:    uv run evallab reject {spec_id} --actor <you> --reason "<why>"\n'
        "  then run:  uv run evallab tick\n"
        "The free oracle and nop controls are unaffected and still run unattended."
    )


def purposeless_spec_message(spec: ExperimentSpec) -> str:
    """The refusal for a spec that does not say why it exists.

    `ExperimentSpec.purpose` is required, so validation is the first and
    strongest rejection: a purposeless spec cannot be submitted at all. This
    message backs the *dispatch-time* refusal asked for by `docs/build-plan.md`
    WS-E item 1, which is not redundant with validation. Readers that
    deliberately tolerate an unparseable spec are growing — `status.py`
    reports one as an error string rather than raising, and the digest does the
    same — so a spec can be *read* without ever being validated. This keeps
    "tolerant enough to display" from becoming "tolerant enough to run", and
    also catches a `model_construct` bypass.

    Names the allowed values, because the operator's next action is to pick one.
    """
    declared = getattr(spec, "purpose", None)
    allowed = " | ".join(EXPERIMENT_PURPOSES)
    subject = spec.spec_id or spec.name
    heading = (
        f"spec {subject} declares no purpose"
        if declared is None
        else (
            f"spec {subject} declares purpose {declared!r}, which is not a purpose "
            "this lab recognises"
        )
    )
    return (
        f"{heading}. Every experiment spec must declare its intent so work can be "
        "grouped, budgeted, and reviewed by intent rather than merely listed.\n"
        f"  allowed values: {allowed}\n"
        '  fix: set "purpose" in the spec file to one of the values above, then '
        "resubmit with `uv run evallab submit <spec.json>`"
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
) -> str:
    """What the operator is told about one provider's allowance."""
    provider = (
        f"{provider_subscription_description(agent)} allowance/policy state"
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
    ) -> PolicyDecision:
        # First, because a spec that does not say why it exists is not a
        # question this gate can answer — and because this costs no filesystem
        # read, unlike the registry resolution below. A purposeless *billable*
        # spec is still refused, just named by its more proximate defect; no
        # spec is admitted here that was admitted before.
        if getattr(spec, "purpose", None) not in EXPERIMENT_PURPOSES:
            return PolicyDecision(
                admitted=False,
                reason_code="purposeless_spec",
                message=purposeless_spec_message(spec),
            )

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

        if spec.billable:
            # Paid execution is authorised one spec at a time by a named human.
            # No standing rule is consulted below this point for billable work,
            # so an unattended cycle cannot reach Harbor with a paid agent.
            if authorization is None:
                # The refusal is also where the operator first sees what the run
                # would cost them: they are about to be asked to authorise it.
                return PolicyDecision(
                    admitted=False,
                    reason_code="paid_run_unauthorized",
                    message=(
                        f"{authorization_required_message(spec)}\n"
                        f"{render_headroom_notice(self.headroom(spec.agent), agent=spec.agent)}"
                    ),
                )
            if spec.submitted_at is None:
                return PolicyDecision(
                    admitted=False,
                    reason_code="paid_run_authorization_mismatch",
                    message=(
                        "this spec records no submission time, so the authorisation "
                        "cannot be shown to cover it; resubmit it with "
                        "`uv run evallab submit` and authorise the id that prints"
                    ),
                )
            if authorization.authorized_at < spec.submitted_at:
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
            if exhausted is not None and not authorization.quota_override:
                return PolicyDecision(
                    admitted=False,
                    reason_code="subscription_quota_exhausted",
                    message=(
                        f"the provider reports the subscription exhausted: {exhausted}. "
                        "This is the provider's own account of its allowance, not a "
                        "threshold this lab invented.\n"
                        f"{render_headroom_notice(headroom, agent=spec.agent)}\n"
                        "  override, only if you have reason to believe the reading is "
                        f"wrong: uv run evallab approve {spec.spec_id} --actor <you> "
                        "--despite-quota"
                    ),
                )
            threshold_reached = lab_threshold_reached(
                headroom, threshold=self.policy.refuse_billable_at_used_percent
            )
            if threshold_reached is not None and not authorization.quota_override:
                return PolicyDecision(
                    admitted=False,
                    reason_code="subscription_quota_ceiling",
                    message=(
                        f"{threshold_reached}.\n"
                        f"{render_headroom_notice(headroom, agent=spec.agent)}\n"
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
            if spent_today_usd + spec.est_cost_usd > self.policy.daily_cost_ceiling_usd:
                return PolicyDecision(
                    admitted=False,
                    reason_code="daily_cost_ceiling",
                    message=(
                        f"estimated daily total {spent_today_usd + spec.est_cost_usd:.2f} "
                        f"exceeds ceiling {self.policy.daily_cost_ceiling_usd:.2f}"
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

        if authorization is not None:
            notes = [
                f"admitted by {authorization.actor}'s authorisation recorded at "
                f"{authorization.authorized_at.isoformat()}"
            ]
            if spec.billable:
                # Whoever authorised this is entitled to see, in the admission
                # itself, the allowance they just spent against.
                admitted_headroom = self.headroom(spec.agent)
                notes.append(render_headroom_notice(admitted_headroom, agent=spec.agent))
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
        cas_locator: EvidenceLocator | None = None,
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
                cas_store_root=(str(cas_locator.store_root) if cas_locator is not None else None),
                cas_record_kind=cas_locator.kind if cas_locator is not None else None,
                cas_record_id=cas_locator.record_id if cas_locator is not None else None,
                cas_record_digest=(
                    cas_locator.expected_record_digest if cas_locator is not None else None
                ),
                cas_content_digest=(
                    cas_locator.expected_content_digest if cas_locator is not None else None
                ),
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

    def resume(self) -> None:
        self.stop_path.unlink(missing_ok=True)

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
RunCallable = Callable[[RunRequest], SettledRun]
IngestCallable = Callable[[EvidenceLocator], IngestProjectionResult | None]
IsolationIdentityProvider = Callable[
    [NetworkIsolationEvidenceV1], NetworkIsolationDispatchIdentityV1
]
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


def _atomic_no_replace_rename(source: Path, destination: Path) -> None:
    """Atomically publish a directory without replacing any existing target."""
    import ctypes
    import ctypes.util
    import errno
    import platform

    system = platform.system()
    if system == "Darwin":
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            RENAME_EXCL = 0x00000004
            AT_FDCWD = -2
            res = libc.renameatx_np(
                AT_FDCWD,
                str(source).encode("utf-8"),
                AT_FDCWD,
                str(destination).encode("utf-8"),
                ctypes.c_uint(RENAME_EXCL),
            )
            if res != 0:
                err = ctypes.get_errno()
                if err in (errno.EEXIST, errno.ENOTEMPTY):
                    raise ExecutionFailure(
                        "control_bootstrap_job_conflict",
                        f"durable control-bootstrap job destination already exists: {destination}",
                    )
                raise OSError(err, os.strerror(err), str(destination))
            return
        except AttributeError:
            pass
    elif system == "Linux":
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            RENAME_NOREPLACE = 1
            AT_FDCWD = -100
            if hasattr(libc, "renameat2"):
                res = libc.renameat2(
                    AT_FDCWD,
                    str(source).encode("utf-8"),
                    AT_FDCWD,
                    str(destination).encode("utf-8"),
                    ctypes.c_uint(RENAME_NOREPLACE),
                )
            else:
                syscall_nr = 276 if platform.machine().startswith("aarch") else 316
                res = libc.syscall(
                    ctypes.c_long(syscall_nr),
                    AT_FDCWD,
                    str(source).encode("utf-8"),
                    AT_FDCWD,
                    str(destination).encode("utf-8"),
                    ctypes.c_uint(RENAME_NOREPLACE),
                )
            if res != 0:
                err = ctypes.get_errno()
                if err in (errno.EEXIST, errno.ENOTEMPTY):
                    raise ExecutionFailure(
                        "control_bootstrap_job_conflict",
                        f"durable control-bootstrap job destination already exists: {destination}",
                    )
                raise OSError(err, os.strerror(err), str(destination))
            return
        except Exception:
            pass

    raise ExecutionFailure(
        "platform_unsupported",
        "atomic no-replace directory publication is unavailable on this platform",
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
        isolation_identity_provider: IsolationIdentityProvider | None = None,
        max_transient_retries: int = MAX_TRANSIENT_RETRIES,
        parallel: int = 1,
        capacity: DispatchCapacity | None = None,
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
        self._isolation_identity_provider = (
            isolation_identity_provider or current_dispatch_isolation_identity
        )
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
        if parallel < 1:
            raise ValueError("parallel must be at least 1")
        self.parallel = parallel
        self.capacity = capacity
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
        parallel: int = 1,
        progress: ProgressCallable | None = None,
        capacity: DispatchCapacity | None = None,
        max_transient_retries: int = MAX_TRANSIENT_RETRIES,
        create_queue: bool = True,
    ) -> Executor:
        return cls(
            repo_root=root,
            queue=DirectoryQueue(root / "queue", create=create_queue),
            policy=load_policy(root / "policy/standing-approvals.yaml"),
            parallel=parallel,
            capacity=capacity,
            progress=progress,
            max_transient_retries=max_transient_retries,
        )

    def submit(self, spec: ExperimentSpec) -> tuple[Path, PolicyDecision]:
        return self.queue.submit(
            spec,
            gate=self.gate,
            spent_today_usd=self._effective_spend_today(),
            consecutive_harness_failures=self._consecutive_harness_failures(),
        )

    def tick(
        self,
        parallel: int | None = None,
        *,
        spec_ids: Sequence[str] | None = None,
    ) -> int:
        effective_parallel = parallel if parallel is not None else self.parallel
        if effective_parallel < 1:
            raise ValueError("parallel must be at least 1")
        with self.queue.tick_lock() as acquired:
            if not acquired:
                self.last_tick_reason = "executor_busy"
                return 0
            self.last_tick_reason = None
            return self._tick_locked(parallel=effective_parallel, spec_ids=spec_ids)

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
            self._safe_repo_path(spec.campaign_evidence_store or "derived/evidence-cas"),
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
            task_success=(
                trial.primary_reward == 1.0 if trial.primary_reward is not None else None
            ),
            result_present=True,
            atif_present=any(
                path.name in {"trajectory.json", "mini-swe-agent.trajectory.json"}
                for path in trial.path.rglob("*.json")
            ),
            finished_at=(
                str(result["finished_at"]) if result.get("finished_at") is not None else None
            ),
        )
        report = evaluate_trial_compliance(bundle)
        payload = (report.model_dump_json(indent=2) + "\n").encode()
        report_dir = (
            self._safe_repo_path(spec.campaign_evidence_store or "derived/evidence-cas")
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
        jobs_root = self._safe_repo_path(spec.jobs_dir)
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
        settled_run: SettledRun,
        spec: ExperimentSpec,
        *,
        actor: str,
    ) -> PolicyDecision | None:
        stage = "artifact_scan"
        try:
            with materialize_evidence(settled_run.cas_locator) as job_dir:
                self._assert_persistent_artifacts_safe(spec, job_dir)
                archive: EvidenceArchive | None = None
                if spec.campaign_ledger is not None:
                    stage = "post_run_archive"
                    archive = self._archive_post_run(job_dir, spec)
                stage = "catalog_ingest"
                ingest_result = self._ingester(settled_run.cas_locator)
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
                            message=(
                                f"post-run compliance refused queue completion: {disposition}"
                            ),
                        )
        except Exception as exc:
            reason_code = (
                exc.reason_code if isinstance(exc, ExecutionFailure) else f"{stage}_failed"
            )
            return PolicyDecision(
                admitted=False,
                reason_code=reason_code,
                message=f"post-run settlement failed during {stage}: {exc}",
            )
        return None

    @staticmethod
    def _is_control_bootstrap_spec(spec: ExperimentSpec) -> bool:
        return (
            spec.agent in {"oracle", "nop"}
            and spec.purpose == "baseline"
            and spec.task.startswith("registered/")
        )

    @staticmethod
    def _has_campaign_provenance(spec: ExperimentSpec) -> bool:
        return any(
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

    def _validate_campaign_dispatch_spec(
        self,
        spec: ExperimentSpec,
        *,
        source: Path,
        live_rebind: bool = True,
    ) -> None:
        spec_id = str(spec.spec_id or "")
        provenance_present = self._has_campaign_provenance(spec)
        campaign_source = "campaign-" in source.name
        control_bootstrap = self._is_control_bootstrap_spec(spec)
        if control_bootstrap and not provenance_present:
            raise _CampaignDispatchValidationError(
                "control_bootstrap_binding_missing",
                "registered baseline controls require a frozen campaign runtime binding",
            )
        if not provenance_present and not campaign_source and not spec_id.startswith("campaign-"):
            return
        if (
            not spec_id.startswith("campaign-")
            or spec.campaign_ledger is None
            or spec.campaign_manifest_digest is None
        ):
            raise _CampaignDispatchValidationError(
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
            raise _CampaignDispatchValidationError(
                "campaign_manifest_unavailable",
                "frozen campaign manifest cannot be validated at dispatch",
            ) from exc
        matches = [attempt for attempt in manifest.attempts if attempt.spec_id == spec_id]
        if len(matches) != 1:
            raise _CampaignDispatchValidationError(
                "campaign_attempt_unbound",
                "queued campaign spec is not uniquely present in the frozen manifest",
            )
        attempt = matches[0]
        if (
            manifest.manifest_digest != spec.campaign_manifest_digest
            or spec.campaign_spec_digest != attempt.spec_digest
            or experiment_spec_digest(spec) != attempt.spec_digest
        ):
            raise _CampaignDispatchValidationError(
                "campaign_spec_drifted",
                "queued campaign spec differs from its frozen attempt",
            )
        runtime_identity = attempt.runtime_identity
        if spec.billable or control_bootstrap:
            if runtime_identity is None:
                label = "billable" if spec.billable else "control-bootstrap"
                raise _CampaignDispatchValidationError(
                    "campaign_isolation_identity_missing",
                    f"{label} campaign attempt has no isolation-bound runtime identity",
                )
            if not live_rebind:
                return
            evidence = runtime_identity.network_isolation_evidence
            try:
                live_identity = self._isolation_identity_provider(evidence)
            except Exception as exc:
                raise _CampaignDispatchValidationError(
                    "campaign_isolation_identity_unavailable",
                    "live isolation runtime identity cannot be established",
                ) from exc
            if (
                live_identity.runtime_identity != evidence.runtime_identity
                or live_identity.probe_identity != evidence.probe_identity
            ):
                raise _CampaignDispatchValidationError(
                    "campaign_isolation_identity_drift",
                    "live runtime/image/adapter/probe identity differs from isolation evidence",
                )
            projection = runtime_identity.network_isolation_evidence.project(
                as_of=datetime.now(UTC)
            )
            if (
                runtime_identity.network_isolation_status,
                runtime_identity.network_isolation_reason,
                runtime_identity.analysis_eligibility,
            ) != (
                projection.status,
                projection.reason,
                projection.analysis_eligibility,
            ):
                raise _CampaignDispatchValidationError(
                    "campaign_isolation_evidence_stale",
                    "campaign isolation evidence no longer matches its current projection",
                )
            if (
                spec.purpose in CAUSAL_EXPERIMENT_PURPOSES
                and projection.analysis_eligibility != "causal-eligible"
            ):
                raise _CampaignDispatchValidationError(
                    "campaign_isolation_ineligible",
                    "causal campaign attempt lacks enforced current isolation evidence",
                )

    def _dispatch_one(
        self,
        path: Path,
        spec: ExperimentSpec,
        authorizations: dict[str, PaidRunAuthorization],
        credentials: frozenset[str],
    ) -> bool:
        try:
            self._validate_campaign_dispatch_spec(
                spec,
                source=path,
                live_rebind=False,
            )
        except _CampaignDispatchValidationError as exc:
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
        missing = missing_credential_for(spec.agent, credentials)
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
        decision = self.gate.decide(
            spec,
            spent_today_usd=self._effective_spend_today(),
            consecutive_harness_failures=self._consecutive_harness_failures(),
            authorization=authorization,
        )
        if not decision.admitted:
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
        self._report_progress(
            f"child started for {spec.name}; progress log: "
            f"{self.repo_root / spec.jobs_dir / '.executor' / (spec.name + '.log')}"
        )
        try:
            try:
                settled_run = self.execute_spec(
                    spec,
                    lease_generation=lease_generation,
                )
            except Exception as execution_error:
                failed_job_dir = self._safe_repo_path(spec.jobs_dir) / spec.name
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
                failure = PolicyDecision(
                    admitted=False,
                    reason_code=reason_code,
                    message=(
                        "execution failed; inspect the immutable job evidence and logs "
                        f"({type(failure_error).__name__})"
                    ),
                )
                failed = self.queue.transition(
                    running,
                    "failed",
                    actor="executor",
                    event="dispatch_failed",
                    reason_code=failure.reason_code,
                )
                self.queue.write_reason(self.queue.load(failed), failure)
                self._report_progress(f"failed {spec.name} ({failure.reason_code}); state: failed")
                if isinstance(execution_error, _CampaignDispatchValidationError):
                    return False
            else:
                failure = self._settle_post_run(
                    settled_run,
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
                        cas_locator=settled_run.cas_locator,
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
                        cas_locator=settled_run.cas_locator,
                    )
                    self._report_progress(f"completed {spec.name}; state: done")
            return True
        finally:
            self.queue.release_lease(spec, lease_generation=lease_generation)

    def _capacity_batch(
        self,
        approved_specs: list[tuple[Path, ExperimentSpec]],
    ) -> list[tuple[Path, ExperimentSpec]]:
        if self.capacity is None:
            return approved_specs
        limit = self.capacity.max_specs_per_tick
        selected: list[tuple[Path, ExperimentSpec]] = []
        active_trials = 0
        by_agent: dict[str, int] = {}
        for path, spec in approved_specs:
            if limit is not None and len(selected) >= limit:
                break
            slots = min(spec.attempts, spec.concurrency)
            if (
                self.capacity.max_active_trials is not None
                and active_trials + slots > self.capacity.max_active_trials
            ):
                continue
            agent_limit = (self.capacity.per_agent_active_trials or {}).get(spec.agent)
            if agent_limit is not None and by_agent.get(spec.agent, 0) + slots > agent_limit:
                continue
            selected.append((path, spec))
            active_trials += slots
            by_agent[spec.agent] = by_agent.get(spec.agent, 0) + slots
        if not selected:
            self.last_tick_reason = "capacity_no_approved_spec_fits"
        return selected

    def _tick_locked(
        self,
        parallel: int = 1,
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
        approved_specs = self._capacity_batch(approved_specs)
        if not approved_specs:
            return 0

        if parallel == 1:
            dispatched = 0
            for path, spec in approved_specs:
                if self.queue.stop_path.exists():
                    break
                if self._dispatch_one(path, spec, authorizations, credentials):
                    dispatched += 1
            return dispatched

        dispatched = 0
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = [
                pool.submit(self._dispatch_one, path, spec, authorizations, credentials)
                for path, spec in approved_specs
            ]
            for future in futures:
                if future.result():
                    dispatched += 1
        return dispatched

    def execute_spec(
        self,
        spec: ExperimentSpec,
        *,
        lease_generation: str | None = None,
    ) -> SettledRun:
        self._validate_campaign_dispatch_spec(
            spec,
            source=Path(),
        )
        task_path = self._safe_repo_path(spec.executable_task_path)
        task_version = spec.task_version
        verifier_digest = spec.verifier_digest
        package_digest = None
        timeout_seconds = spec.timeout_seconds
        canonical_task_path = spec.executable_task_path
        resolved_task_runtime = spec.task_runtime_identity
        resolved_registry_record = None
        task_id = spec.task_id

        if spec.task.startswith("registered/"):
            reg = TaskRegistry.from_repo(self.repo_root)
            resolved = reg.resolve_spec(spec, self.repo_root)
            if resolved is None:
                raise ExecutionFailure(
                    "unregistered_task",
                    f"task {spec.task!r} is not registered in library/registry/",
                )
            task_path = self._safe_repo_path(resolved.task_path)
            canonical_task_path = resolved.task_path
            task_version = resolved.version
            verifier_digest = resolved.digests.verifier
            package_digest = resolved.digests.package
            resolved_registry_record = resolved
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
        if resolved_registry_record is not None:
            current_task_runtime = task_runtime_identity(resolved_registry_record)
            if resolved_task_runtime is not None and resolved_task_runtime != current_task_runtime:
                raise ExecutionFailure(
                    "task_runtime_identity_mismatch",
                    "resolved registry revision differs from the frozen task runtime identity",
                )
            resolved_task_runtime = current_task_runtime
        if resolved_task_runtime is not None and (
            resolved_task_runtime.registry_admission_state != "registered"
            or resolved_task_runtime.task_id != task_id
            or resolved_task_runtime.task_version != task_version
            or resolved_task_runtime.certified_runtime_package_digest != package_digest
        ):
            raise ExecutionFailure(
                "task_runtime_identity_mismatch",
                "execution task bytes or registry admission differ from the frozen identity",
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

        jobs_dir = self._safe_repo_path(spec.jobs_dir)
        # A field the dispatcher never forwards is the defect class this repo keeps
        # finding, so the elicitation preamble is resolved here beside jobs_dir.
        extra_instruction_path = (
            self._safe_repo_path(spec.extra_instruction_path)
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
        request = RunRequest(
            task=task_path,
            extra_instruction_path=extra_instruction_path,
            agent=spec.agent,
            name=spec.name,
            jobs_dir=jobs_dir,
            environment=spec.environment,
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
            lease_path=self.queue.lease_path(spec),
            lease_generation=lease_generation,
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
                task_family=spec.task_family,
                task_id=task_id,
                task_instance_id=spec.task_instance_id,
                generator_seed=spec.generator_seed,
                campaign_ledger=spec.campaign_ledger,
                campaign_cell_id=spec.campaign_cell_id,
                campaign_attempt_id=spec.campaign_attempt_id,
                campaign_attempt_index=spec.campaign_attempt_index,
                campaign_manifest_digest=spec.campaign_manifest_digest,
                task_runtime_identity=resolved_task_runtime,
                network_isolation_evidence=spec.network_isolation_evidence,
                network_isolation_evidence_digest=spec.network_isolation_evidence_digest,
                network_isolation_status=spec.network_isolation_status,
                network_isolation_reason=spec.network_isolation_reason,
                analysis_eligibility=spec.analysis_eligibility,
                campaign_spec_digest=spec.campaign_spec_digest,
            ),
        )
        settled_run = self._run_with_transient_retries(spec, request)
        with materialize_evidence(settled_run.cas_locator) as restored_job:
            self._assert_persistent_artifacts_safe(spec, restored_job)
        if self._is_control_bootstrap_spec(spec):
            self._promote_control_bootstrap_job(settled_run, spec)
        return settled_run

    def _promote_control_bootstrap_job(
        self,
        settled_run: SettledRun,
        spec: ExperimentSpec,
    ) -> Path:
        durable_root = (self.repo_root / "research/evidence/runs").resolve()
        durable_root.mkdir(parents=True, exist_ok=True)
        destination = (durable_root / spec.name).resolve()
        if destination.parent != durable_root:
            raise ExecutionFailure(
                "control_bootstrap_job_path_invalid",
                f"destination escapes durable root: {destination}",
            )
        if destination.exists():
            raise ExecutionFailure(
                "control_bootstrap_job_conflict",
                f"durable control-bootstrap job destination already exists: {destination}",
            )
        staging_dir = durable_root / f".staging-{spec.name}-{secrets.token_hex(12)}"
        staging_dir.mkdir(mode=0o700, exist_ok=False)
        try:
            materialize_evidence_at(settled_run.cas_locator, staging_dir)
            load_job(staging_dir)
            self._assert_persistent_artifacts_safe(spec, staging_dir)
            for path in staging_dir.rglob("*"):
                if path.is_file():
                    with path.open("rb") as handle:
                        os.fsync(handle.fileno())
                elif path.is_dir():
                    fd = os.open(path, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            _atomic_no_replace_rename(staging_dir, destination)
            parent_fd = os.open(durable_root, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            return destination
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def _run_with_transient_retries(
        self,
        spec: ExperimentSpec,
        request: RunRequest,
    ) -> SettledRun:
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

    def execute_direct(self, request: RunRequest, *, ingest: bool = True) -> SettledRun:
        if request.agent not in CONTROL_AGENTS:
            raise ValueError(
                "direct execution is restricted to oracle/nop; --allow-billable records "
                "spend consent but does not bypass the standing-policy queue"
            )
        settled_run = self._runner(request)
        if ingest:
            ingest_result = self._ingester(settled_run.cas_locator)
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
        return settled_run

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

    def _docker_daemon_check(self) -> tuple[bool, str]:
        if not shutil.which("docker"):
            return False, "docker executable not found in PATH"
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
            if completed.returncode != 0:
                return False, "Docker daemon unreachable"
            return True, "Docker daemon reachable"
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"Docker daemon check failed: {type(exc).__name__}"

    def _resolve_readiness_canary(self, task_ref: str) -> tuple[Path, str]:
        """Resolve one digest-pinned suite member; arbitrary direct tasks are forbidden."""
        if not task_ref.startswith("canary/"):
            raise ValueError("readiness smoke only accepts a registered canary/<name> task")
        member_name = task_ref.removeprefix("canary/")
        if not member_name or "/" in member_name:
            raise ValueError("readiness smoke task must name exactly one registered canary")

        from evallab.canary import load_canary_suite, task_directory_digest

        suite = load_canary_suite(self.repo_root / "policy/canary-suite.yaml")
        matches = [member for member in suite.members if member.name == member_name]
        if len(matches) != 1:
            raise ValueError(f"unknown readiness canary {task_ref!r}")
        member = matches[0]
        task_path = (self.repo_root / member.task_path).resolve()
        if self.repo_root.resolve() not in task_path.parents:
            raise ValueError(f"readiness canary escapes repository: {member.task_path}")
        actual_digest = task_directory_digest(task_path)
        if actual_digest != member.task_digest:
            raise ValueError(
                f"readiness canary digest mismatch for {member.name}; "
                "update policy/canary-suite.yaml through review"
            )
        return task_path, member.task_digest

    def execute_agent_smoke(
        self,
        profile: AgentProfile,
        *,
        task_ref: str = "canary/event-summary",
        is_installed_fn: Callable[[str], bool] | None = None,
        docker_checker: Callable[[], tuple[bool, str]] | None = None,
        cli_runner: Callable[[Sequence[str]], tuple[int, str]] | None = None,
        security_runner: SecurityRunner | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> tuple[bool, AgentSmokeRecord | None, str | None]:
        """Run one fixed-budget fresh-container transport and capture smoke."""
        readiness = evaluate_profile_readiness(
            profile,
            root=self.repo_root,
            is_installed_fn=is_installed_fn or (lambda binary: shutil.which(binary) is not None),
            docker_checker=docker_checker or self._docker_daemon_check,
            cli_runner=cli_runner,
            security_runner=security_runner,
            environment=environment,
        )
        for gate_name in (
            "declared",
            "installed",
            "host_credential",
            "harbor_transport",
            "environment_network",
            "structured_trajectory",
        ):
            if getattr(readiness.gates, gate_name) != "pass":
                blocker_reason = (
                    readiness.blocker.reason
                    if readiness.blocker
                    else f"Preflight gate '{gate_name}' failed"
                )
                return False, None, blocker_reason

        try:
            task_path, task_digest = self._resolve_readiness_canary(task_ref)
        except ValueError as exc:
            return False, None, str(exc)

        job_profile_id = profile.profile_id.replace(".", "-").replace("_", "-")
        job_name = f"smoke-{job_profile_id}-{new_ulid().lower()}"
        metered_proxy = profile.adapter in {"mini-swe-agent", ZAI_OPENCODE_AGENT}
        request = RunRequest(
            task=task_path,
            agent=profile.adapter,
            model=profile.model,
            name=job_name,
            jobs_dir=self.repo_root / "runs",
            timeout_seconds=300,
            max_requests=16 if metered_proxy else None,
            max_input_tokens=200_000 if metered_proxy else None,
            max_output_tokens=64_000 if metered_proxy else None,
            max_total_tokens=264_000 if metered_proxy else None,
            cost_limit_usd=1.0 if metered_proxy else None,
            allow_billable=True,
            concurrency=1,
            attempts=1,
        )

        try:
            settled_run = self._runner(request)
        except Exception as exc:
            return False, None, f"Runner execution failed: {exc}"

        with suppress(Exception):
            self._ingester(settled_run.cas_locator)

        try:
            with materialize_evidence(settled_run.cas_locator) as job_dir:
                job = load_job(job_dir)
        except Exception as exc:
            return False, None, f"Failed to load job result: {exc}"
        if len(job.trials) != 1:
            return False, None, f"Smoke run must produce exactly one trial, found {len(job.trials)}"

        trial = job.trials[0]
        exception_info = trial.result.get("exception_info")
        agent_exception_type = (
            str(exception_info["exception_type"])
            if isinstance(exception_info, dict) and exception_info.get("exception_type")
            else None
        )

        reward = trial.primary_reward
        if reward is None:
            reward_txt = trial.path / "verifier/reward.txt"
            if reward_txt.is_file():
                try:
                    reward = float(reward_txt.read_text().strip())
                except ValueError:
                    reward = None
        if reward is not None and not math.isfinite(reward):
            reward = None

        runtime_seconds = duration_seconds(
            trial.result.get("started_at"), trial.result.get("finished_at")
        )
        if runtime_seconds is None:
            return False, None, "Smoke trial did not record a complete runtime interval"

        projection = project_trial(job, trial)
        trajectory = next(
            (
                item
                for item in projection.trajectories
                if item.embedded_path is None and item.validation_status == "valid"
            ),
            None,
        )
        if trajectory is None or trajectory.step_count < 1:
            return False, None, "Smoke trial did not capture one valid structured ATIF trajectory"
        tool_call_count = sum(
            step.tool_call_count
            for step in projection.steps
            if step.document_id == trajectory.document_id
        )
        native_evidence_path: str | None = None
        native_evidence_digest: str | None = None
        if profile.adapter == ZAI_OPENCODE_AGENT:
            native_path = trial.path / "agent/opencode.txt"
            if not native_path.is_file() or native_path.stat().st_size == 0:
                return (
                    False,
                    None,
                    "OpenCode runtime smoke did not capture native opencode.txt evidence",
                )
            native_evidence_path = native_path.relative_to(trial.path).as_posix()
            native_evidence_digest = (
                "sha256:" + hashlib.sha256(native_path.read_bytes()).hexdigest()
            )

        smoke_record = AgentSmokeRecord(
            schema_version=2,
            profile_id=profile.profile_id,
            profile_digest=profile.digest,
            task=task_ref,
            task_digest=task_digest,
            job_name=job.name,
            trial_name=trial.name,
            reward=reward,
            agent_exception_type=agent_exception_type,
            runtime_seconds=runtime_seconds,
            step_count=trajectory.step_count,
            tool_call_count=tool_call_count,
            atif_path=trajectory.source_path,
            atif_digest=trajectory.source_sha256,
            native_evidence_path=native_evidence_path,
            native_evidence_digest=native_evidence_digest,
            fresh_container=True,
            transport_status="complete",
            capture_status="complete",
            secret_safety_status="pass",
            executed_at=datetime.now(UTC),
        )

        updated_readiness = evaluate_profile_readiness(
            profile,
            root=self.repo_root,
            is_installed_fn=is_installed_fn or (lambda binary: shutil.which(binary) is not None),
            docker_checker=docker_checker or self._docker_daemon_check,
            cli_runner=cli_runner,
            security_runner=security_runner,
            environment=environment,
            persisted_record=AgentReadinessRecord(
                schema_version=1,
                profile_id=profile.profile_id,
                adapter=profile.adapter,
                model=profile.model,
                profile_digest=profile.digest,
                state=ProfileState.SMOKE_PASSED.value,
                gates=AgentGateEvaluations(
                    declared="pass",
                    installed="pass",
                    host_credential="pass",
                    harbor_transport="pass",
                    environment_network="pass",
                    structured_trajectory="pass",
                    smoke="pass",
                    canary="blocked",
                ),
                last_smoke=smoke_record,
                network_isolation_evidence=readiness.network_isolation_evidence,
                network_isolation_evidence_digest=readiness.network_isolation_evidence_digest,
                network_isolation_status=readiness.network_isolation_status,
                network_isolation_reason=readiness.network_isolation_reason,
                analysis_eligibility=readiness.analysis_eligibility,
                updated_at=datetime.now(UTC),
            ),
        )
        save_readiness_record(updated_readiness, root=self.repo_root)
        return True, smoke_record, None

    def execute_agent_qualify(
        self,
        profile: AgentProfile,
        *,
        repeats: int = 3,
        task_ref: str = "canary/event-summary",
        is_installed_fn: Callable[[str], bool] | None = None,
        docker_checker: Callable[[], tuple[bool, str]] | None = None,
        cli_runner: Callable[[Sequence[str]], tuple[int, str]] | None = None,
        security_runner: SecurityRunner | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> tuple[bool, AgentQualificationDigest | None, str | None]:
        """Require exactly three fresh-container transport/capture smokes."""
        if repeats != 3:
            return False, None, "Qualification requires exactly 3 fresh-container smokes"

        smoke_records: list[AgentSmokeRecord] = []
        for index in range(repeats):
            ok, smoke_rec, err = self.execute_agent_smoke(
                profile,
                task_ref=task_ref,
                is_installed_fn=is_installed_fn,
                docker_checker=docker_checker,
                cli_runner=cli_runner,
                security_runner=security_runner,
                environment=environment,
            )
            if not ok or smoke_rec is None:
                return False, None, f"Repeat {index + 1}/{repeats} failed: {err}"
            smoke_records.append(smoke_rec)

        current_readiness = load_readiness_record(profile.profile_id, root=self.repo_root)
        if current_readiness is None:
            return False, None, "Qualification lost its persisted readiness evidence"

        qualification = AgentQualificationDigest(
            schema_version=2,
            profile_id=profile.profile_id,
            profile_digest=profile.digest,
            qualification_basis="transport-capture",
            repeats=repeats,
            success_count=len(smoke_records),
            smoke_records=smoke_records,
            qualification_digest=compute_qualification_digest(smoke_records),
            qualified_at=datetime.now(UTC),
        )

        updated_readiness = evaluate_profile_readiness(
            profile,
            root=self.repo_root,
            is_installed_fn=is_installed_fn or (lambda binary: shutil.which(binary) is not None),
            docker_checker=docker_checker or self._docker_daemon_check,
            cli_runner=cli_runner,
            security_runner=security_runner,
            environment=environment,
            persisted_record=AgentReadinessRecord(
                schema_version=1,
                profile_id=profile.profile_id,
                adapter=profile.adapter,
                model=profile.model,
                profile_digest=profile.digest,
                state=ProfileState.CANARY_QUALIFIED.value,
                gates=AgentGateEvaluations(
                    declared="pass",
                    installed="pass",
                    host_credential="pass",
                    harbor_transport="pass",
                    environment_network="pass",
                    structured_trajectory="pass",
                    smoke="pass",
                    canary="pass",
                ),
                last_smoke=smoke_records[-1],
                qualification=qualification,
                network_isolation_evidence=current_readiness.network_isolation_evidence,
                network_isolation_evidence_digest=(
                    current_readiness.network_isolation_evidence_digest
                ),
                network_isolation_status=current_readiness.network_isolation_status,
                network_isolation_reason=current_readiness.network_isolation_reason,
                analysis_eligibility=current_readiness.analysis_eligibility,
                updated_at=datetime.now(UTC),
            ),
        )
        save_readiness_record(updated_readiness, root=self.repo_root)
        return True, qualification, None

    def _running_state_timed_out(self, spec: ExperimentSpec) -> bool:
        state_path = self._safe_repo_path(spec.jobs_dir) / ".executor" / f"{spec.name}.state.json"
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
                        f"log under {self._safe_repo_path(spec.jobs_dir) / '.executor'}"
                    ),
                )
                continue
            job_dir = self._safe_repo_path(spec.jobs_dir) / spec.name
            archive_root = self._safe_repo_path(spec.jobs_dir) / ".transient-attempts" / spec.name
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
                locator, archive = _settle_completed_job(
                    job_dir,
                    store_root=_evidence_store_root(),
                    record_id=spec.name,
                )
            except ExecutionFailure as exc:
                self._fail_reconciled_running(
                    path,
                    spec,
                    reason_code=exc.reason_code,
                    message=str(exc),
                )
                continue
            try:
                with materialize_evidence(locator) as settled_job_dir:
                    job = load_job(settled_job_dir)
            except Exception:
                self._fail_reconciled_running(
                    path,
                    spec,
                    reason_code="running_reconcile_incomplete_evidence",
                    message="terminal trial evidence is unreadable; refusing reconciliation",
                    cas_locator=locator,
                )
                continue
            if not job.trials:
                self._fail_reconciled_running(
                    path,
                    spec,
                    reason_code="running_reconcile_incomplete_evidence",
                    message="terminal job has no trial evidence; refusing reconciliation",
                    cas_locator=locator,
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
                    cas_locator=locator,
                )
                continue
            failure = self._settle_post_run(
                SettledRun(cas_locator=locator, cas_record=archive),
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
                    cas_locator=locator,
                )
                continue
            self.queue.transition(
                path,
                "done",
                actor="executor-reconcile",
                event="running_reconciled",
                policy_rule=spec.policy_rule,
                cas_locator=locator,
            )

    def _fail_reconciled_running(
        self,
        path: Path,
        spec: ExperimentSpec,
        *,
        reason_code: str,
        message: str,
        cas_locator: EvidenceLocator | None = None,
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
            cas_locator=cas_locator,
        )
        self.queue.write_reason(self.queue.load(failed), decision)

    def _safe_repo_path(self, relative: str) -> Path:
        candidate = (self.repo_root / relative).resolve()
        if candidate != self.repo_root and self.repo_root not in candidate.parents:
            raise ValueError(f"path escapes repository: {relative}")
        return candidate

    def _run_harbor(self, request: RunRequest) -> SettledRun:
        return run_experiment(request, repo_root=self.repo_root)

    def _ingest(self, locator: EvidenceLocator) -> IngestProjectionResult:
        url = database_url_from_environment()
        with materialize_evidence(locator) as job_dir:
            job = load_job(job_dir)
            return ingest_and_project(
                url,
                [job],
                root=self.repo_root,
                output_root=derived_root_from_environment(self.repo_root),
                source_locators={job.id: locator},
            )

    def _catalog_spend(self) -> float:
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


def write_spec(path: Path, spec: ExperimentSpec) -> None:
    path.write_text(json.dumps(spec.model_dump(mode="json", exclude_none=True), indent=2) + "\n")
