from __future__ import annotations

import fcntl
import fnmatch
import json
import os
import platform
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import ValidationError

from evallab import database
from evallab.atif import IngestProjectionResult, ingest_and_project
from evallab.credentials import (
    DEFAULT_AGENT_MODELS,
    available_credentials,
    missing_credential_for,
)
from evallab.eventlog import event_log_lock, read_event_log_lines
from evallab.paths import derived_root_from_environment
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
)
from evallab.results import load_job
from evallab.runner import (
    CONTROL_AGENTS,
    SUPPORT_COMMAND_TIMEOUT_SECONDS,
    ExecutionFailure,
    RunRequest,
    TransientHarnessFailure,
    database_url_from_environment,
    run_experiment,
    subscription_environment,
    tool_version,
    transient_provider_exception,
)
from evallab.schemas import (
    EXPERIMENT_PURPOSES,
    AutoRunRule,
    ExperimentSpec,
    PolicyDecision,
    QueueEvent,
    QueueReason,
    QueueState,
    RunProvenance,
    StandingApprovalsPolicy,
)

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
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
DEFAULT_EVENTS_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_EVENT_BACKUPS = 7
DEFAULT_LEASE_STALE_SECONDS = 300.0
_TICK_THREAD_LOCK = threading.Lock()


def new_ulid(*, timestamp_ms: int | None = None, randomness: int | None = None) -> str:
    """Return a lexically sortable ULID without adding a runtime ID dependency."""
    millis = timestamp_ms if timestamp_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    if not 0 <= millis < 2**48:
        raise ValueError("ULID timestamp is outside the 48-bit range")
    random_bits = randomness if randomness is not None else secrets.randbits(80)
    if not 0 <= random_bits < 2**80:
        raise ValueError("ULID randomness is outside the 80-bit range")
    value = (millis << 80) | random_bits
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def load_policy(path: Path) -> StandingApprovalsPolicy:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot load standing-approvals policy: {exc}") from exc
    try:
        return StandingApprovalsPolicy.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid standing-approvals policy: {exc}") from exc


@dataclass(frozen=True)
class PaidRunAuthorization:
    """One recorded human decision to let a specific queued spec spend money.

    The record lives in `queue/events.jsonl`, not in the spec file. A spec's
    own `policy_rule` field cannot be the proof of authorisation, because the
    automation that submits paid work is what writes that file; the event log
    is append-only, locked, and retained, so it is the only place consent can
    be shown to have come from outside the machine.
    """

    spec_id: str
    actor: str
    authorized_at: datetime

    #: Whether the human who recorded this authorisation also said, in the same
    #: recorded act, that they accept a provider-reported quota exhaustion. It
    #: overrides `subscription_quota_exhausted` and nothing else — every other
    #: refusal in `PolicyGate.decide` still applies. Recorded as
    #: `reason_code: quota_override` on the `human_approved` event, so it lives
    #: in the same append-only ledger as the consent it qualifies and cannot be
    #: asserted by the spec file the automation writes.
    quota_override: bool = False


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
    "this gate was built without a quota reader, so the subscription allowance "
    "was never looked up"
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
                notes.append(
                    render_headroom_notice(admitted_headroom, agent=spec.agent)
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
    ) -> None:
        if events_max_bytes < 1:
            raise ValueError("events_max_bytes must be positive")
        if event_backups < 1:
            raise ValueError("event_backups must be positive")
        self.root = root
        self.events_max_bytes = events_max_bytes
        self.event_backups = event_backups
        self.reasons_dir = root / "reasons"
        for state in QUEUE_STATES:
            (root / state).mkdir(parents=True, exist_ok=True)
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
            path
            for state in states
            for path in self.state_dir(state).glob(f"*-{spec_id}.json")
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
                from_state=cast(QueueState, source_state),
                to_state=destination_state,
                actor=actor,
                policy_rule=policy_rule or spec.policy_rule,
                reason_code=reason_code,
                job_name=spec.name,
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
        oldest = self.events_path.with_name(
            f"{self.events_path.name}.{self.event_backups}"
        )
        oldest.unlink(missing_ok=True)
        for index in range(self.event_backups - 1, 0, -1):
            source = self.events_path.with_name(f"{self.events_path.name}.{index}")
            if source.exists():
                source.replace(
                    self.events_path.with_name(f"{self.events_path.name}.{index + 1}")
                )
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
    ) -> Path | None:
        """Atomically claim one spec via O_EXCL lease file in running/.

        Returns the lease path on successful claim, or None if the spec is
        actively claimed by another executor. A stale lease (> stale_seconds)
        is reclaimed atomically rather than permanently blocking the spec.
        """
        path = self.lease_path(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        pid = owner_pid if owner_pid is not None else os.getpid()
        timestamp = now or datetime.now(UTC)
        spec_id = spec.spec_id if isinstance(spec, ExperimentSpec) else str(spec)
        payload = (
            json.dumps(
                {
                    "spec_id": spec_id,
                    "pid": pid,
                    "acquired_at": timestamp.isoformat(),
                    "host": platform.node(),
                },
                indent=2,
            )
            + "\n"
        ).encode()

        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if self.is_lease_stale(path, stale_seconds=stale_seconds):
                with suppress(OSError):
                    path.unlink(missing_ok=True)
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except (FileExistsError, OSError):
                    return None
            else:
                return None
        except OSError:
            return None

        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return path

    def release_lease(self, spec: ExperimentSpec | Path | str) -> bool:
        """Release a held lease by unlinking the lease file in running/."""
        path = self.lease_path(spec)
        if path.is_file():
            try:
                path.unlink()
                return True
            except OSError:
                return False
        return False

    def heartbeat_lease(self, spec: ExperimentSpec | Path | str) -> bool:
        """Touch the lease file to update its mtime heartbeat."""
        path = self.lease_path(spec)
        if path.is_file():
            try:
                path.touch()
                return True
            except OSError:
                return False
        return False

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


@dataclass(frozen=True)
class DispatchCapacity:
    """Explicit global limits for one concurrent dispatch batch."""

    max_specs_per_tick: int | None = None
    max_active_trials: int | None = None
    per_agent_active_trials: dict[str, int] | None = None

    def __post_init__(self) -> None:
        values = [
            self.max_specs_per_tick,
            self.max_active_trials,
            *(self.per_agent_active_trials or {}).values(),
        ]
        if any(value is not None and value < 1 for value in values):
            raise ValueError("dispatch capacity values must be positive")


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
    ) -> Executor:
        return cls(
            repo_root=root,
            queue=DirectoryQueue(root / "queue"),
            policy=load_policy(root / "policy/standing-approvals.yaml"),
            parallel=parallel,
            capacity=capacity,
            progress=progress,
        )
    def submit(self, spec: ExperimentSpec) -> tuple[Path, PolicyDecision]:
        return self.queue.submit(
            spec,
            gate=self.gate,
            spent_today_usd=self._effective_spend_today(),
            consecutive_harness_failures=self._consecutive_harness_failures(),
        )

    def tick(self, parallel: int | None = None) -> int:
        effective_parallel = parallel if parallel is not None else self.parallel
        if effective_parallel < 1:
            raise ValueError("parallel must be at least 1")
        with self.queue.tick_lock() as acquired:
            if not acquired:
                self.last_tick_reason = "executor_busy"
                return 0
            self.last_tick_reason = None
            return self._tick_locked(parallel=effective_parallel)

    def _report_progress(self, message: str) -> None:
        if self._progress is not None:
            self._progress(message)

    def _dispatch_one(
        self,
        path: Path,
        spec: ExperimentSpec,
        authorizations: dict[str, PaidRunAuthorization],
        credentials: frozenset[str],
    ) -> bool:
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
        decision = self.gate.decide(
            spec,
            spent_today_usd=self._effective_spend_today(),
            consecutive_harness_failures=self._consecutive_harness_failures(),
            authorization=authorizations.get(str(spec.spec_id)),
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
        lease_path = self.queue.acquire_lease(spec)
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
            self.queue.release_lease(spec)
            return False
        self._report_progress(
            f"dispatching {spec.name} (spec {spec.spec_id}, agent {spec.agent})"
        )
        self._report_progress(
            f"child started for {spec.name}; progress log: "
            f"{self.repo_root / spec.jobs_dir / '.executor' / (spec.name + '.log')}"
        )
        try:
            try:
                job_dir = self.execute_spec(spec)
            except Exception as exc:
                reason_code = (
                    exc.reason_code
                    if isinstance(exc, ExecutionFailure)
                    else "execution_failed"
                )
                failure = PolicyDecision(
                    admitted=False,
                    reason_code=reason_code,
                    message=(
                        "execution failed; inspect the immutable job evidence and logs "
                        f"({type(exc).__name__})"
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
                self._report_progress(
                    f"failed {spec.name} ({failure.reason_code}); state: failed"
                )
            else:
                try:
                    ingest_result = self._ingester(job_dir)
                except Exception as exc:
                    failure = PolicyDecision(
                        admitted=False,
                        reason_code="catalog_ingest_failed",
                        message=(
                            "completed evidence could not be cataloged; retry ingestion "
                            f"before interpreting the result ({type(exc).__name__})"
                        ),
                    )
                    failed = self.queue.transition(
                        running,
                        "failed",
                        actor="executor",
                        event="catalog_ingest_failed",
                        reason_code=failure.reason_code,
                    )
                    self.queue.write_reason(self.queue.load(failed), failure)
                    self._report_progress(
                        f"failed {spec.name} ({failure.reason_code}); state: failed"
                    )
                else:
                    if ingest_result is not None:
                        record_projection_failures(
                            self.queue,
                            ingest_result,
                            actor="executor",
                            spec_id=str(spec.spec_id),
                        )
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
            self.queue.release_lease(spec)

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


    def _tick_locked(self, parallel: int = 1) -> int:
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

    def execute_spec(self, spec: ExperimentSpec) -> Path:
        task_path = self._safe_repo_path(spec.executable_task_path)
        task_version = spec.task_version
        verifier_digest = spec.verifier_digest
        package_digest = None
        timeout_seconds = spec.timeout_seconds
        canonical_task_path = spec.executable_task_path

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
            timeout_seconds = min(spec.timeout_seconds, resolved.limits.timeout_seconds)

        jobs_dir = self._safe_repo_path(spec.jobs_dir)
        # A field the dispatcher never forwards is the defect class this repo keeps
        # finding, so the elicitation preamble is resolved here beside jobs_dir.
        extra_instruction_path = (
            self._safe_repo_path(spec.extra_instruction_path)
            if spec.extra_instruction_path
            else None
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
            lease_path=self.queue.lease_path(spec),
            provenance=RunProvenance(
                spec_id=str(spec.spec_id),
                task=spec.task,
                task_version=task_version,
                verifier_digest=verifier_digest,
                policy_rule=spec.policy_rule,
                package_digest=package_digest,
                task_path=canonical_task_path,
            ),
        )
        return self._run_with_transient_retries(spec, request)

    def _run_with_transient_retries(
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
                archived = self._archive_transient_attempt(request, retry_number + 1)
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
            if (
                event.event == "dispatch_attempt_reserved"
                and event.estimated_cost_usd is not None
            ):
                reservations.setdefault(event.spec_id, []).append(
                    event.estimated_cost_usd
                )
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

    @staticmethod
    def _archive_transient_attempt(
        request: RunRequest,
        retry_number: int,
    ) -> Path | None:
        job_dir = request.jobs_dir / request.name
        if not job_dir.exists():
            return None
        archive = (
            request.jobs_dir
            / ".transient-attempts"
            / request.name
            / f"attempt-{retry_number}"
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
                "direct execution is restricted to oracle/nop; submit billable work "
                "through the standing-policy queue"
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
                        provenance.spec_id
                        if provenance is not None
                        else f"system-{new_ulid()}"
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
                checks.append(
                    ("docker-daemon", False, f"unavailable: {type(exc).__name__}")
                )
            else:
                output = (completed.stdout or completed.stderr).strip().splitlines()
                detail = output[0] if output else "no version output"
                checks.append(("docker-daemon", completed.returncode == 0, detail))
        return checks

    def _running_state_timed_out(self, spec: ExperimentSpec) -> bool:
        state_path = (
            self._safe_repo_path(spec.jobs_dir)
            / ".executor"
            / f"{spec.name}.state.json"
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
            archive_root = (
                self._safe_repo_path(spec.jobs_dir)
                / ".transient-attempts"
                / spec.name
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
            try:
                job = load_job(job_dir)
            except Exception:
                # Harbor creates the top-level result at job start with
                # finished_at=null. It may still be running and billing, so a
                # restart must never ingest or settle that partial evidence.
                continue
            transient_reason = next(
                (
                    reason
                    for trial in job.trials
                    if (reason := transient_provider_exception(trial.result))
                    is not None
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
            try:
                ingest_result = self._ingester(job_dir)
            except Exception:
                continue
            if ingest_result is not None:
                record_projection_failures(
                    self.queue,
                    ingest_result,
                    actor="executor-reconcile",
                    spec_id=str(spec.spec_id),
                )
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

    def _safe_repo_path(self, relative: str) -> Path:
        candidate = (self.repo_root / relative).resolve()
        if candidate != self.repo_root and self.repo_root not in candidate.parents:
            raise ValueError(f"path escapes repository: {relative}")
        return candidate

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
            raise ValueError(
                f"Invalid queue event at {segment}:{line_number}: {exc}"
            ) from exc
    return events


def read_spec(path: Path) -> ExperimentSpec:
    return ExperimentSpec.model_validate_json(path.read_text())


def write_spec(path: Path, spec: ExperimentSpec) -> None:
    path.write_text(json.dumps(spec.model_dump(mode="json", exclude_none=True), indent=2) + "\n")
