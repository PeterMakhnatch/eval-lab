"""Subscription-quota accounting for paid agents, measured instead of estimated.

The lab's cost model is denominated in dollars (``est_cost_usd``,
``daily_cost_ceiling_usd``) but its paid agents authenticate from a personal
subscription: Codex from ``~/.codex/auth.json``, Claude through
``scripts/with-claude-auth`` and the Keychain. No dollars move when a paid
trial runs, so every dollar figure in this repository -- including Harbor's own
``agent_result.cost_usd`` -- is an API-list-price *equivalent*, not spend. The
binding constraint is a subscription quota.

This module reports that quota honestly, and its central discipline is to keep
two different questions apart:

*consumption*
    What the lab itself used. Recovered from each trial's own committed
    artifacts: token counts from ``result.json`` and model-turn counts from the
    agent's session rollout. Always scoped to the lab.

*headroom*
    What remains on the subscription. The provider reports this as a single
    account-wide integer percentage covering every client of the account, not
    just the lab. It is observable only where a trial happened to record it,
    and it can never be attributed to the lab.

Conflating those two is how the dollar model misleads, so they are separate
types here and every quantity carries an :data:`Availability` label. Nothing is
estimated: a number that was not observed is reported ``unavailable``.

Boundaries this module keeps deliberately:

- It reads Harbor job directories only. It never touches ``~/.codex``, the
  Keychain, the catalog, the network, or the wall clock (``now`` is injected).
- It reads ``payload.rate_limits`` and token counters out of a session rollout
  and never the message text, so its output is safe to commit even though the
  rollout it parsed is not.
- Where promotion omitted the rollout, it falls back to that trial's redacted
  ``agent/quota/*.rate-limits.json`` sidecar so committed evidence still yields
  a quota reading. Fallback, never addition: a tree holding both records must
  not count the same reading twice.
- It measures. It does not authorise, and it imports nothing from the policy
  gate.

Command line: ``python -m evallab.quota [--json] [ROOT ...]``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from evallab.results import discover_job_dirs

#: Agents that spend a subscription allowance. ``oracle`` and ``nop`` are free
#: local controls and are excluded from every consumption figure.
PAID_AGENTS: frozenset[str] = frozenset(
    {"codex", "claude-code", "cursor-cli", "antigravity-cli"}
)

#: The account named in operator-facing billing and allowance messages. These
#: are deliberately provider-specific: a Codex snapshot cannot describe
#: Antigravity or Cursor.
PROVIDER_SUBSCRIPTIONS: dict[str, str] = {
    "codex": "Peter's ChatGPT/Codex subscription",
    "claude-code": "Peter's Claude subscription",
    "cursor-cli": "Cursor subscription/API-key policy state",
    "antigravity-cli": "Peter's Google subscription (Antigravity OAuth)",
}


def provider_subscription_description(agent: str) -> str:
    return PROVIDER_SUBSCRIPTIONS.get(agent, f"{agent} subscription/policy state")


#: Codex is the only lane whose local artifacts currently expose a measured
#: rate-limit snapshot. Other lanes must remain UNKNOWN until their own
#: provider emits an independently identified snapshot.
MEASURED_QUOTA_AGENTS: frozenset[str] = frozenset({"codex"})

#: Provenance label, matching the vocabulary used by the operator surfaces.
Availability = Literal["observed", "unavailable"]

ROLLOUT_GLOB = "agent/sessions/**/rollout-*.jsonl"

#: Promotion (rule R4, ``scripts/promote_codex_bundle.py``) omits the rollout and
#: leaves the provider's quota reading beside it as a redacted sidecar. It is the
#: only quota signal a promoted bundle carries, and the reader below falls back
#: to it -- never adds it -- so a tree holding both records cannot double-count.
QUOTA_SIDECAR_GLOB = "agent/quota/*.rate-limits.json"
QUOTA_SIDECAR_KIND = "evallab-rate-limits-sidecar"

LAB_METADATA_FILENAME = "lab-metadata.json"

_SECONDS_PER_MINUTE = 60


def label(availability: Availability) -> str:
    """Render a provenance label the way the operator surfaces print it."""
    return f"[{availability}]"


def _availability(value: object) -> Availability:
    return "observed" if value is not None else "unavailable"


def _as_object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _parse_instant(value: object) -> datetime | None:
    """Parse a Harbor/rollout timestamp into an aware UTC instant."""
    text = _as_str(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _parse_epoch(value: object) -> datetime | None:
    seconds = _as_int(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


class QuotaWindow(BaseModel):
    """One provider rate-limit window, exactly as the provider reported it."""

    used_percent: float | None = None
    window_minutes: int | None = None
    resets_at: datetime | None = None

    @property
    def remaining_percent(self) -> float | None:
        """Complement of ``used_percent``.

        Account-wide and as coarse as the source: the provider reports whole
        percentage points, so this is not a fine-grained budget.
        """
        if self.used_percent is None:
            return None
        return max(0.0, 100.0 - self.used_percent)

    @property
    def window_hours(self) -> float | None:
        if self.window_minutes is None:
            return None
        return self.window_minutes / _SECONDS_PER_MINUTE


class QuotaObservation(BaseModel):
    """A provider quota snapshot recovered from a completed trial's artifacts.

    This is the only true quota signal available without a paid call and
    without an API key: the Codex CLI attaches a ``rate_limits`` block to the
    ``token_count`` event it writes into its session rollout, and Harbor copies
    that rollout out of the container into ``<trial>/agent/sessions/``.

    Its scope is the whole subscription account, so it answers "how much of the
    allowance is gone", never "how much of it did the lab use".
    """

    job_name: str
    trial_name: str
    agent: str
    observed_at: datetime
    limit_id: str | None = None
    limit_name: str | None = None
    plan_type: str | None = None
    primary: QuotaWindow | None = None
    secondary: QuotaWindow | None = None
    has_credits: bool | None = None
    credits_unlimited: bool | None = None
    credits_balance: str | None = None
    rate_limit_reached_type: str | None = None
    source: str

    scope: Literal["account"] = "account"


class TrialConsumption(BaseModel):
    """What one paid trial is recorded as having consumed.

    ``input_tokens`` is Harbor's ``n_input_tokens``, which is **inclusive** of
    ``cache_tokens``. The quantity that represents fresh work is therefore
    :attr:`uncached_input_tokens`, and whether the provider charges a cached
    input token against the subscription allowance at the same rate as an
    uncached one is UNVERIFIED -- see :data:`CACHED_WEIGHTING_NOTE`.
    """

    job_name: str
    trial_name: str
    agent: str
    task_name: str | None = None
    model_name: str | None = None
    policy_rule: str | None = None
    attempts_declared: int | None = None
    started_at: datetime | None = None
    input_tokens: int | None = None
    cache_tokens: int | None = None
    output_tokens: int | None = None
    model_turns: int | None = None
    reported_cost_usd: float | None = None
    exception_type: str | None = None
    duration_seconds: float | None = None
    agent_setup_seconds: float | None = None
    agent_execution_seconds: float | None = None
    job_duration_seconds: float | None = None

    @property
    def day(self) -> date | None:
        if self.started_at is None:
            return None
        return (
            self.started_at.astimezone(UTC).date()
            if self.started_at.tzinfo is not None
            else self.started_at.date()
        )
    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    @property
    def uncached_input_tokens(self) -> int | None:
        """Input tokens that were not served from cache."""
        if self.input_tokens is None:
            return None
        return max(0, self.input_tokens - (self.cache_tokens or 0))

    @property
    def cached_input_ratio(self) -> float | None:
        """Share of input tokens served from cache, ``None`` when unobserved."""
        if not self.input_tokens or self.cache_tokens is None:
            return None
        return self.cache_tokens / self.input_tokens

    @property
    def usage_availability(self) -> Availability:
        """Whether this trial's own consumption was observed at all.

        ``unavailable`` never means "consumed nothing" -- it means the trial
        left no usage record. :attr:`exception_type` is the evidence a reader
        needs to judge why, and this module refuses to guess on their behalf.
        """
        return "observed" if (self.total_tokens or 0) > 0 else "unavailable"


class ConsumptionTotals(BaseModel):
    """Summed consumption over a set of paid trials.

    ``attempts_declared`` is summed per distinct job, not per trial: attempts
    are a job-level launch parameter, so summing it across a job's trials would
    triple-count. Comparing it against :attr:`paid_trials` shows how many
    declared attempt slots actually became trials.
    """

    jobs: int = 0
    paid_trials: int = 0
    trials_with_observed_usage: int = 0
    trials_without_usage_evidence: int = 0
    input_tokens: int = 0
    cache_tokens: int = 0
    output_tokens: int = 0
    model_turns: int = 0
    attempts_declared: int | None = None
    reported_cost_usd: float | None = None
    trial_wall_clock_seconds: float | None = None
    job_wall_clock_seconds: float | None = None
    longest_trial_seconds: float | None = None
    exception_types: dict[str, int] = Field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cache_tokens)

    @property
    def cached_input_ratio(self) -> float | None:
        """Cache share of input. UNVERIFIED whether quota weighs it equally."""
        if not self.input_tokens:
            return None
        return self.cache_tokens / self.input_tokens

    @property
    def tokens_availability(self) -> Availability:
        return _availability(self.trials_with_observed_usage or None)


def _totals(trials: Sequence[TrialConsumption]) -> ConsumptionTotals:
    totals = ConsumptionTotals(paid_trials=len(trials))
    costs: list[float] = []
    attempts: dict[str, int] = {}
    job_durations: dict[str, float] = {}
    trial_durations: list[float] = []
    exceptions: defaultdict[str, int] = defaultdict(int)
    for trial in trials:
        if trial.usage_availability == "observed":
            totals.trials_with_observed_usage += 1
        else:
            totals.trials_without_usage_evidence += 1
        totals.input_tokens += trial.input_tokens or 0
        totals.cache_tokens += trial.cache_tokens or 0
        totals.output_tokens += trial.output_tokens or 0
        totals.model_turns += trial.model_turns or 0
        if trial.reported_cost_usd is not None:
            costs.append(trial.reported_cost_usd)
        if trial.attempts_declared is not None:
            attempts[trial.job_name] = trial.attempts_declared
        if trial.job_duration_seconds is not None:
            job_durations[trial.job_name] = trial.job_duration_seconds
        if trial.duration_seconds is not None:
            trial_durations.append(trial.duration_seconds)
        if trial.exception_type:
            exceptions[trial.exception_type] += 1
    totals.jobs = len({trial.job_name for trial in trials})
    totals.reported_cost_usd = sum(costs) if costs else None
    totals.attempts_declared = sum(attempts.values()) if attempts else None
    totals.job_wall_clock_seconds = sum(job_durations.values()) if job_durations else None
    totals.trial_wall_clock_seconds = sum(trial_durations) if trial_durations else None
    totals.longest_trial_seconds = max(trial_durations) if trial_durations else None
    totals.exception_types = dict(sorted(exceptions.items()))
    return totals


class ConsumptionLedger(BaseModel):
    """What the lab spent, per trial, with groupings the Sponsor asked for."""

    trials: tuple[TrialConsumption, ...] = ()

    def totals(self) -> ConsumptionTotals:
        return _totals(self.trials)

    def since(self, instant: datetime) -> ConsumptionLedger:
        """Ledger restricted to trials that started at or after ``instant``.

        Trials with no recorded start are dropped rather than assumed recent;
        the count is therefore a lower bound, which is the safe direction for a
        gate.
        """
        return ConsumptionLedger(
            trials=tuple(
                trial
                for trial in self.trials
                if trial.started_at is not None and trial.started_at >= instant
            )
        )

    def for_agent(self, agent: str) -> ConsumptionLedger:
        return ConsumptionLedger(
            trials=tuple(trial for trial in self.trials if trial.agent == agent)
        )

    def _grouped(self, keys: Iterable[str | None]) -> dict[str, ConsumptionTotals]:
        buckets: defaultdict[str, list[TrialConsumption]] = defaultdict(list)
        for key, trial in zip(keys, self.trials, strict=True):
            buckets[key if key is not None else "unavailable"].append(trial)
        return {key: _totals(value) for key, value in sorted(buckets.items())}

    def by_day(self) -> dict[str, ConsumptionTotals]:
        return self._grouped(
            trial.day.isoformat() if trial.day else None for trial in self.trials
        )

    def by_task(self) -> dict[str, ConsumptionTotals]:
        return self._grouped(trial.task_name for trial in self.trials)

    def by_agent(self) -> dict[str, ConsumptionTotals]:
        return self._grouped(trial.agent for trial in self.trials)

    def by_policy_rule(self) -> dict[str, ConsumptionTotals]:
        return self._grouped(trial.policy_rule for trial in self.trials)

    def by_job(self) -> dict[str, ConsumptionTotals]:
        return self._grouped(trial.job_name for trial in self.trials)


class Headroom(BaseModel):
    """What remains on the subscription -- never what remains for the lab.

    This is the constraint that actually binds. When ``hard_stop`` is true the
    account has no overflow credits, so reaching 100% is not an extra charge:
    it is a lockout until :attr:`resets_at`.
    """

    availability: Availability
    scope: Literal["account"] = "account"
    reason: str | None = None
    used_percent: float | None = None
    remaining_percent: float | None = None
    window_minutes: int | None = None
    observed_at: datetime | None = None
    resets_at: datetime | None = None
    staleness_seconds: float | None = None
    plan_type: str | None = None
    limit_id: str | None = None
    has_credits: bool | None = None
    credits_unlimited: bool | None = None
    credits_balance: str | None = None
    rate_limit_reached_type: str | None = None
    source: str | None = None

    #: True when the account has no overflow credits, so exhausting the window
    #: blocks every paid agent until it resets. ``None`` when the credits block
    #: was not reported.
    hard_stop: bool | None = None

    #: The lab's own share of the account-wide percentage. Structurally
    #: unavailable: the provider reports one integer for the whole account and
    #: it cannot be decomposed, so this module will not pretend otherwise.
    lab_attributable: Availability = "unavailable"
    lab_attributable_reason: str = (
        "the provider reports one account-wide integer percentage that covers every "
        "client of the subscription; it cannot be decomposed into the lab's share"
    )

    @property
    def hard_stop_note(self) -> str:
        if self.hard_stop is None:
            return f"overflow credits {label('unavailable')}"
        if self.hard_stop:
            return (
                "no overflow credits: reaching 100% blocks every paid agent until "
                "the window resets, it does not incur an extra charge"
            )
        return "overflow credits are available, so 100% is not necessarily a lockout"


NO_OBSERVATION_REASON = (
    "no paid trial in the scanned job directories recorded a provider quota "
    "snapshot, so the remaining allowance is unknown"
)

#: Whether the provider charges a cached input token against the subscription
#: allowance at the same rate as an uncached one. Nothing observable locally or
#: in the published documentation settles this, so it stays UNVERIFIED: a wrong
#: assumption here would silently invalidate every consumption figure.
CACHED_WEIGHTING_NOTE = (
    "UNVERIFIED: whether cached input draws on the subscription allowance at the "
    "same rate as uncached input is not observable from these artifacts and is not "
    "stated in the published documentation; uncached and cached input are reported "
    "separately rather than combined into one consumption number"
)


def _headroom(observations: Sequence[QuotaObservation], *, now: datetime) -> Headroom:
    latest = max(observations, key=lambda item: item.observed_at, default=None)
    if latest is None or latest.primary is None or latest.primary.used_percent is None:
        return Headroom(availability="unavailable", reason=NO_OBSERVATION_REASON)
    window = latest.primary
    hard_stop: bool | None = None
    if latest.has_credits is not None or latest.credits_unlimited is not None:
        hard_stop = not (latest.has_credits or latest.credits_unlimited)
    return Headroom(
        availability="observed",
        used_percent=window.used_percent,
        remaining_percent=window.remaining_percent,
        window_minutes=window.window_minutes,
        observed_at=latest.observed_at,
        resets_at=window.resets_at,
        staleness_seconds=(now - latest.observed_at).total_seconds(),
        plan_type=latest.plan_type,
        limit_id=latest.limit_id,
        has_credits=latest.has_credits,
        credits_unlimited=latest.credits_unlimited,
        credits_balance=latest.credits_balance,
        rate_limit_reached_type=latest.rate_limit_reached_type,
        source=latest.source,
        hard_stop=hard_stop,
    )


class QuotaReport(BaseModel):
    """Consumption and headroom side by side, each with its own provenance."""

    generated_at: datetime
    roots: tuple[Path, ...] = ()
    paid_agents: tuple[str, ...] = ()
    consumed: ConsumptionLedger = ConsumptionLedger()
    observations: tuple[QuotaObservation, ...] = ()
    headroom: Headroom = Headroom(availability="unavailable", reason=NO_OBSERVATION_REASON)
    cached_weighting_note: str = CACHED_WEIGHTING_NOTE

    def counter_resolution_percent(self) -> float | None:
        """Smallest change the provider's counter can express, as observed.

        Every snapshot seen so far reports a whole percentage point, so
        consumption below one point of the window registers as no movement at
        all. That floor is why a zero delta across a run is evidence of "not
        detectable", never evidence of "consumed nothing". ``None`` when there
        is nothing to measure.
        """
        values = [
            observation.primary.used_percent
            for observation in self.observations
            if observation.primary is not None and observation.primary.used_percent is not None
        ]
        if not values:
            return None
        return 1.0 if all(float(value).is_integer() for value in values) else None


def _sidecar_snapshots(trial_dir: Path) -> list[tuple[datetime, dict[str, Any], Path]]:
    """Provider quota snapshots from a promoted trial's R4 quota sidecars.

    Written by ``scripts/promote_codex_bundle.py`` beside each rollout that
    promotion omitted, carrying only the event timestamp and a whitelist of
    ``payload.rate_limits`` scalars. A document is read only when it declares
    itself with :data:`QUOTA_SIDECAR_KIND`, so an unrelated JSON file dropped in
    ``agent/quota/`` is ignored rather than guessed at.

    The timestamp kept is the one the *trial* recorded, never the file's, so a
    reading recovered here ages exactly as its rollout-sourced twin would and
    :attr:`Headroom.staleness_seconds` stays honest about a committed number.
    """
    snapshots: list[tuple[datetime, dict[str, Any], Path]] = []
    for sidecar in sorted(trial_dir.glob(QUOTA_SIDECAR_GLOB)):
        try:
            document = _as_object(json.loads(sidecar.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
        if document.get("kind") != QUOTA_SIDECAR_KIND:
            continue
        entries = document.get("snapshots")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            record = _as_object(entry)
            limits = _as_object(record.get("rate_limits"))
            observed_at = _parse_instant(record.get("timestamp"))
            if not limits or observed_at is None:
                continue
            snapshots.append((observed_at, limits, sidecar))
    snapshots.sort(key=lambda item: item[0])
    return snapshots


def _rate_limit_snapshots(
    trial_dir: Path,
    *,
    agent: str,
) -> list[tuple[datetime, dict[str, Any], Path]]:
    """Read only a snapshot format proven to belong to ``agent``.

    Codex rollouts expose the ``rate_limits`` block consumed here. Antigravity
    and Cursor artifacts do not expose a compatible measured allowance, so a
    block found in one of those trials is not silently treated as their quota.
    """
    if agent not in MEASURED_QUOTA_AGENTS:
        return []
    snapshots: list[tuple[datetime, dict[str, Any], Path]] = []
    for rollout in sorted(trial_dir.glob(ROLLOUT_GLOB)):
        try:
            text = rollout.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if "rate_limits" not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = _as_object(_as_object(event).get("payload"))
            if payload.get("type") != "token_count":
                continue
            limits = _as_object(payload.get("rate_limits"))
            observed_at = _parse_instant(_as_object(event).get("timestamp"))
            if not limits or observed_at is None:
                continue
            snapshots.append((observed_at, limits, rollout))
    if not snapshots:
        return _sidecar_snapshots(trial_dir)
    snapshots.sort(key=lambda item: item[0])
    return snapshots


def _model_turns(trial_dir: Path) -> int | None:
    """Count of ``token_count`` events, i.e. model turns the agent actually took."""
    rollouts = sorted(trial_dir.glob(ROLLOUT_GLOB))
    if not rollouts:
        return None
    turns = 0
    for rollout in rollouts:
        try:
            text = rollout.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if "token_count" not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _as_object(_as_object(event).get("payload")).get("type") == "token_count":
                turns += 1
    return turns


def _window(payload: object) -> QuotaWindow | None:
    window = _as_object(payload)
    if not window:
        return None
    return QuotaWindow(
        used_percent=_as_float(window.get("used_percent")),
        window_minutes=_as_int(window.get("window_minutes")),
        resets_at=_parse_epoch(window.get("resets_at")),
    )


def _observation(
    *,
    job_name: str,
    trial_name: str,
    agent: str,
    observed_at: datetime,
    limits: dict[str, Any],
    source: str,
) -> QuotaObservation:
    credits = _as_object(limits.get("credits"))
    balance = credits.get("balance")
    return QuotaObservation(
        job_name=job_name,
        trial_name=trial_name,
        agent=agent,
        observed_at=observed_at,
        limit_id=_as_str(limits.get("limit_id")),
        limit_name=_as_str(limits.get("limit_name")),
        plan_type=_as_str(limits.get("plan_type")),
        primary=_window(limits.get("primary")),
        secondary=_window(limits.get("secondary")),
        has_credits=_as_bool(credits.get("has_credits")),
        credits_unlimited=_as_bool(credits.get("unlimited")),
        credits_balance=str(balance) if balance is not None else None,
        rate_limit_reached_type=_as_str(limits.get("rate_limit_reached_type")),
        source=source,
    )


def _declared_attempts(command: object) -> int | None:
    """``--n-attempts`` as recorded in the job's launch command, or ``None``."""
    if not isinstance(command, list):
        return None
    for index, item in enumerate(command[:-1]):
        if item != "--n-attempts":
            continue
        try:
            return int(str(command[index + 1]))
        except ValueError:
            return None
    return None


def _span_seconds(payload: object) -> float | None:
    """Duration of a Harbor phase block, or of a record's own start/finish."""
    block = _as_object(payload)
    started = _parse_instant(block.get("started_at"))
    finished = _parse_instant(block.get("finished_at"))
    if started is None or finished is None:
        return None
    return (finished - started).total_seconds()


class _JobContext(BaseModel):
    """Job-level facts every trial in the job inherits."""

    attempts_declared: int | None = None
    policy_rule: str | None = None
    duration_seconds: float | None = None


def _job_context(job_dir: Path) -> _JobContext:
    context = _JobContext()
    with contextlib.suppress(OSError, json.JSONDecodeError):
        job_result = json.loads((job_dir / "result.json").read_text())
        context.duration_seconds = _span_seconds(job_result)
    metadata_path = job_dir / LAB_METADATA_FILENAME
    if not metadata_path.is_file():
        return context
    try:
        metadata = _as_object(json.loads(metadata_path.read_text()))
    except (OSError, json.JSONDecodeError):
        return context
    context.attempts_declared = _declared_attempts(metadata.get("command"))
    context.policy_rule = _as_str(_as_object(metadata.get("experiment")).get("policy_rule"))
    return context


def _trial_consumption(
    trial_dir: Path,
    *,
    job_name: str,
    job: _JobContext,
    paid_agents: frozenset[str],
) -> tuple[TrialConsumption | None, list[QuotaObservation]]:
    result_path = trial_dir / "result.json"
    if not result_path.is_file():
        return None, []
    try:
        result = _as_object(json.loads(result_path.read_text()))
    except (OSError, json.JSONDecodeError):
        return None, []
    agent_info = _as_object(result.get("agent_info"))
    agent = _as_str(agent_info.get("name")) or _as_str(
        _as_object(_as_object(result.get("config")).get("agent")).get("name")
    )
    if agent is None or agent not in paid_agents:
        return None, []
    agent_result = _as_object(result.get("agent_result"))
    trial_name = _as_str(result.get("trial_name")) or trial_dir.name
    consumption = TrialConsumption(
        job_name=job_name,
        trial_name=trial_name,
        agent=agent,
        task_name=_as_str(result.get("task_name")),
        model_name=_as_str(_as_object(agent_info.get("model_info")).get("name")),
        policy_rule=job.policy_rule,
        attempts_declared=job.attempts_declared,
        started_at=_parse_instant(result.get("started_at")),
        input_tokens=_as_int(agent_result.get("n_input_tokens")),
        cache_tokens=_as_int(agent_result.get("n_cache_tokens")),
        output_tokens=_as_int(agent_result.get("n_output_tokens")),
        model_turns=_model_turns(trial_dir),
        reported_cost_usd=_as_float(agent_result.get("cost_usd")),
        exception_type=_as_str(_as_object(result.get("exception_info")).get("exception_type")),
        duration_seconds=_span_seconds(result),
        agent_setup_seconds=_span_seconds(result.get("agent_setup")),
        agent_execution_seconds=_span_seconds(result.get("agent_execution")),
        job_duration_seconds=job.duration_seconds,
    )
    observations = [
        _observation(
            job_name=job_name,
            trial_name=trial_name,
            agent=agent,
            observed_at=observed_at,
            limits=limits,
            source=str(rollout.relative_to(trial_dir.parent)),
        )
        for observed_at, limits, rollout in _rate_limit_snapshots(
            trial_dir, agent=agent
        )
    ]
    return consumption, observations


def load_quota_report(
    roots: Iterable[Path],
    *,
    now: datetime,
    paid_agents: frozenset[str] = PAID_AGENTS,
) -> QuotaReport:
    """Build the quota report from Harbor job directories under ``roots``.

    Every external input is injected: the directories to scan and the instant
    used for staleness. No credential store, database, network call, or clock
    read happens here, which is what lets the tests be deterministic.
    """
    resolved = tuple(Path(root).expanduser() for root in roots)
    trials: list[TrialConsumption] = []
    observations: list[QuotaObservation] = []
    for job_dir in discover_job_dirs(resolved):
        job = _job_context(job_dir)
        for trial_dir in sorted(path for path in job_dir.iterdir() if path.is_dir()):
            consumption, snapshots = _trial_consumption(
                trial_dir,
                job_name=job_dir.name,
                job=job,
                paid_agents=paid_agents,
            )
            if consumption is not None:
                trials.append(consumption)
            observations.extend(snapshots)
    epoch = datetime.min.replace(tzinfo=UTC)
    trials.sort(key=lambda item: (item.started_at or epoch, item.trial_name))
    observations.sort(key=lambda item: (item.observed_at, item.trial_name))
    return QuotaReport(
        generated_at=now,
        roots=resolved,
        paid_agents=tuple(sorted(paid_agents)),
        consumed=ConsumptionLedger(trials=tuple(trials)),
        observations=tuple(observations),
        headroom=_headroom(observations, now=now),
    )


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return label("unavailable")
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _instant(moment: datetime | None) -> str:
    return moment.isoformat() if moment is not None else label("unavailable")


def _totals_line(name: str, totals: ConsumptionTotals) -> str:
    tokens = (
        f"{totals.uncached_input_tokens:>9,} uncached in  "
        f"{totals.cache_tokens:>9,} cached  {totals.output_tokens:>7,} out"
        if totals.tokens_availability == "observed"
        else f"tokens {label('unavailable')}".ljust(44)
    )
    return (
        f"  {name:<44} {totals.paid_trials:>3} trials  "
        f"{totals.trials_with_observed_usage:>3} used  "
        f"{totals.model_turns:>4} turns  {tokens}  "
        f"wall {_duration(totals.job_wall_clock_seconds)}"
    )


def _headroom_lines(headroom: Headroom, resolution: float | None) -> list[str]:
    lines = ["", f"REMAINING on the subscription (scope: {headroom.scope}, NOT the lab)"]
    if headroom.availability != "observed":
        return lines + [
            f"  remaining allowance                  {label('unavailable')}",
            f"    reason: {headroom.reason}",
        ]
    lines += [
        f"  used_percent                         {headroom.used_percent} {label('observed')}",
        f"  remaining_percent                    {headroom.remaining_percent} "
        f"{label('observed')}",
        f"  limit_id / plan_type                 "
        f"{headroom.limit_id or label('unavailable')} / "
        f"{headroom.plan_type or label('unavailable')}",
        f"  window                               {headroom.window_minutes} minutes "
        f"({_duration((headroom.window_minutes or 0) * 60)})",
        f"  resets_at                            {_instant(headroom.resets_at)}",
        f"  observed_at                          {_instant(headroom.observed_at)}",
        f"  staleness                            {_duration(headroom.staleness_seconds)}",
        f"  credits_balance                      "
        f"{headroom.credits_balance or label('unavailable')}",
        f"  hard stop                            {headroom.hard_stop}",
        f"    {headroom.hard_stop_note}",
        f"  rate_limit_reached_type              "
        f"{headroom.rate_limit_reached_type or label('unavailable')}",
        f"  counter resolution                   "
        f"{f'{resolution} percentage point' if resolution else label('unavailable')}",
        "    consumption below one point of the window registers as no movement",
        f"  source                               {headroom.source}",
    ]
    return lines


def render_report(report: QuotaReport) -> str:
    """Human-readable report. Every figure carries its provenance label.

    Remaining allowance comes first because it is the constraint that binds;
    consumption follows because it is what the lab controls.
    """
    ledger = report.consumed
    totals = ledger.totals()
    lines: list[str] = [
        "Subscription quota accounting",
        f"generated_at: {report.generated_at.isoformat()}",
        f"paid agents:  {', '.join(report.paid_agents)}",
        f"roots:        {', '.join(str(root) for root in report.roots)}",
    ]
    lines += _headroom_lines(report.headroom, report.counter_resolution_percent())
    lines += [
        f"  lab's share of that percentage       {label(report.headroom.lab_attributable)}",
        f"    reason: {report.headroom.lab_attributable_reason}",
        "",
        "CONSUMED by the lab (scope: this lab only)",
        f"  paid jobs                            {totals.jobs} {label('observed')}",
        f"  paid trials dispatched               {totals.paid_trials} {label('observed')}",
        f"  with observed usage                  {totals.trials_with_observed_usage} "
        f"{label('observed')}",
        f"  without usage evidence               {totals.trials_without_usage_evidence} "
        f"{label('observed')}",
        f"  model turns                          {totals.model_turns} {label('observed')}",
    ]
    if totals.tokens_availability == "observed":
        ratio = totals.cached_input_ratio
        lines += [
            f"  uncached input tokens                {totals.uncached_input_tokens:,} "
            f"{label('observed')}",
            f"  cached input tokens                  {totals.cache_tokens:,} {label('observed')}",
            f"  input tokens (incl. cached)          {totals.input_tokens:,} {label('observed')}",
            f"  output tokens                        {totals.output_tokens:,} {label('observed')}",
            f"  cached share of input                {ratio:.1%} {label('observed')}"
            if ratio is not None
            else f"  cached share of input                {label('unavailable')}",
            f"    {CACHED_WEIGHTING_NOTE}",
        ]
    else:
        lines.append(f"  tokens                               {label('unavailable')}")
    lines += [
        f"  job wall clock                       {_duration(totals.job_wall_clock_seconds)} "
        f"{label('observed')}",
        f"  longest single trial                 {_duration(totals.longest_trial_seconds)} "
        f"{label('observed')}",
    ]
    if totals.attempts_declared is not None:
        lines.append(
            f"  attempt slots declared (per job)     {totals.attempts_declared} "
            f"{label('observed')}"
        )
    if totals.reported_cost_usd is not None:
        lines.append(
            f"  reported_cost_usd                    {totals.reported_cost_usd:.4f} "
            f"{label('observed')} -- API list-price equivalent, NOT subscription spend"
        )
    if totals.exception_types:
        rendered = ", ".join(f"{name}={count}" for name, count in totals.exception_types.items())
        lines.append(f"  exceptions                           {rendered} {label('observed')}")

    for title, grouping in (
        ("by day", ledger.by_day()),
        ("by task", ledger.by_task()),
        ("by agent", ledger.by_agent()),
        ("by policy rule", ledger.by_policy_rule()),
        ("by job", ledger.by_job()),
    ):
        lines += ["", f"CONSUMED {title}"]
        lines += [_totals_line(name, group) for name, group in grouping.items()]

    lines += ["", f"snapshots harvested: {len(report.observations)} {label('observed')}"]
    return "\n".join(lines)


def default_roots(repository_root: Path) -> tuple[Path, ...]:
    """Job directories a lab checkout keeps paid-trial evidence in."""
    return (repository_root / "runs", repository_root / "research" / "evidence" / "runs")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.quota",
        description="Report subscription consumption and, when observable, remaining quota.",
    )
    parser.add_argument(
        "roots",
        nargs="*",
        type=Path,
        help="Harbor job directories or roots to scan (default: this checkout's run roots).",
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    arguments = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[2]
    roots = tuple(arguments.roots) or default_roots(repository_root)
    report = load_quota_report(roots, now=datetime.now(UTC))
    if arguments.json:
        print(report.model_dump_json(indent=2))
    else:
        print(render_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
