from __future__ import annotations

import contextlib
import json
import math
import subprocess
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from evallab.execution_contracts import (
    MIMO_AGENT,
    TERMINUS_AGENT,
    ZAI_OPENAPI_TERMINUS_MODEL_SELECTORS,
    is_breaker_selfhosted_model,
    is_mimo_selfhosted_model,
    is_openrouter_model,
)
from evallab.results import discover_job_dirs

PAID_AGENTS: frozenset[str] = frozenset(
    {"codex", "claude-code", "cursor-cli", "antigravity-cli", "terminus-2", MIMO_AGENT}
)

PROVIDER_SUBSCRIPTIONS: dict[str, str] = {
    "codex": "Peter's ChatGPT/Codex subscription",
    "claude-code": "Peter's Claude subscription",
    "cursor-cli": "Cursor subscription/API-key policy state",
    "antigravity-cli": "Peter's Google subscription (Antigravity OAuth)",
    "terminus-2": "configured Terminus model provider (model-specific)",
    MIMO_AGENT: "self-hosted Modal SGLang GPU (time-billed, no per-token charge)",
}


def provider_subscription_description(agent: str, model: str | None = None) -> str:
    if agent in {TERMINUS_AGENT, MIMO_AGENT}:
        if is_breaker_selfhosted_model(model):
            return "self-hosted breaker GPU (time-billed; token ceilings do not cap GPU spend)"
        if is_mimo_selfhosted_model(model):
            return "self-hosted Modal SGLang GPU (time-billed, no per-token charge)"
        if is_openrouter_model(model):
            return "OpenRouter metered API balance (pinned upstream endpoint)"
        if model in ZAI_OPENAPI_TERMINUS_MODEL_SELECTORS:
            return "Z.ai Open Platform standard-API balance"
    return PROVIDER_SUBSCRIPTIONS.get(agent, f"{agent} subscription/policy state")


MEASURED_QUOTA_AGENTS: frozenset[str] = frozenset({"codex"})

Availability = Literal["observed", "unavailable"]

ROLLOUT_GLOB = "agent/sessions/**/rollout-*.jsonl"

CLAUDE_SESSION_GLOB = "agent/sessions/projects/**/*.jsonl"

QUOTA_SIDECAR_GLOB = "agent/quota/*.rate-limits.json"
QUOTA_SIDECAR_KIND = "evallab-rate-limits-sidecar"

LAB_METADATA_FILENAME = "lab-metadata.json"

_SECONDS_PER_MINUTE = 60


def label(availability: Availability) -> str:
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

    used_percent: float | None = None
    window_minutes: int | None = None
    resets_at: datetime | None = None
    status: Literal["ok", "warning", "error", "exhausted", "unknown"] | None = None

    @property
    def remaining_percent(self) -> float | None:
        if self.used_percent is None:
            return None
        return max(0.0, 100.0 - self.used_percent)

    @property
    def window_hours(self) -> float | None:
        if self.window_minutes is None:
            return None
        return self.window_minutes / _SECONDS_PER_MINUTE


class QuotaObservation(BaseModel):

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
    fetched_at: datetime | None = None
    windows: dict[str, QuotaWindow] = Field(default_factory=dict)

    scope: Literal["account"] = "account"


class TrialConsumption(BaseModel):

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
        if self.input_tokens is None:
            return None
        return max(0, self.input_tokens - (self.cache_tokens or 0))

    @property
    def cached_input_ratio(self) -> float | None:
        if not self.input_tokens or self.cache_tokens is None:
            return None
        return self.cache_tokens / self.input_tokens

    @property
    def usage_availability(self) -> Availability:
        return "observed" if (self.total_tokens or 0) > 0 else "unavailable"


class ConsumptionTotals(BaseModel):

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

    trials: tuple[TrialConsumption, ...] = ()

    def totals(self) -> ConsumptionTotals:
        return _totals(self.trials)

    def since(self, instant: datetime) -> ConsumptionLedger:
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
        return self._grouped(trial.day.isoformat() if trial.day else None for trial in self.trials)

    def by_task(self) -> dict[str, ConsumptionTotals]:
        return self._grouped(trial.task_name for trial in self.trials)

    def by_agent(self) -> dict[str, ConsumptionTotals]:
        return self._grouped(trial.agent for trial in self.trials)

    def by_policy_rule(self) -> dict[str, ConsumptionTotals]:
        return self._grouped(trial.policy_rule for trial in self.trials)

    def by_job(self) -> dict[str, ConsumptionTotals]:
        return self._grouped(trial.job_name for trial in self.trials)


class Headroom(BaseModel):

    availability: Availability
    scope: Literal["account"] = "account"
    reason: str | None = None
    used_percent: float | None = None
    remaining_percent: float | None = None
    window_minutes: int | None = None
    observed_at: datetime | None = None
    fetched_at: datetime | None = None
    windows: dict[str, QuotaWindow] = Field(default_factory=dict)
    resets_at: datetime | None = None
    staleness_seconds: float | None = None
    plan_type: str | None = None
    limit_id: str | None = None
    has_credits: bool | None = None
    credits_unlimited: bool | None = None
    credits_balance: str | None = None
    rate_limit_reached_type: str | None = None
    source: str | None = None

    hard_stop: bool | None = None

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


CLAUDE_USAGE_COMMAND = ("omp", "usage", "-p", "anthropic", "--json", "-r")
CLAUDE_DISPATCH_QUOTA_KIND = "evallab-claude-dispatch-quota"
CLAUDE_DISPATCH_QUOTA_PATH = Path("agent/quota/claude-dispatch.json")


def _claude_unavailable(
    reason: str,
    *,
    windows: dict[str, QuotaWindow] | None = None,
    fetched_at: datetime | None = None,
) -> Headroom:
    return Headroom(
        availability="unavailable",
        reason=reason,
        source=" ".join(CLAUDE_USAGE_COMMAND),
        windows=windows or {},
        fetched_at=fetched_at,
    )


def _milliseconds_instant(value: object) -> datetime | None:
    number = _as_float(value)
    if number is None or not math.isfinite(number) or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_claude_headroom(payload: object, *, now: datetime) -> Headroom:
    unavailable = "OMP Claude usage did not report valid 5h and 7d quota windows"
    reports = _as_object(payload).get("reports")
    if not isinstance(reports, list):
        return _claude_unavailable(unavailable)
    matches = [report for report in reports if _as_object(report).get("provider") == "anthropic"]
    if len(matches) != 1:
        return _claude_unavailable(unavailable)
    report = _as_object(matches[0])
    fetched_at = _milliseconds_instant(report.get("fetchedAt"))
    limits = report.get("limits")
    if fetched_at is None or not isinstance(limits, list):
        return _claude_unavailable(unavailable)
    windows: dict[str, QuotaWindow] = {}
    for window_id, duration_ms in (("5h", 18000000), ("7d", 604800000)):
        entries = [entry for entry in limits if _as_object(entry).get("id") == f"anthropic:{window_id}"]
        if len(entries) != 1:
            continue
        entry = _as_object(entries[0])
        window = _as_object(entry.get("window"))
        amount = _as_object(entry.get("amount"))
        used = _as_float(amount.get("used"))
        if "used" not in amount:
            fraction = _as_float(amount.get("usedFraction"))
            used = fraction * 100 if fraction is not None else None
        resets_at = _milliseconds_instant(window.get("resetsAt"))
        status = entry.get("status")
        if (
            window.get("id") != window_id
            or window.get("durationMs") != duration_ms
            or amount.get("unit") != "percent"
            or used is None
            or not math.isfinite(used)
            or used < 0
            or resets_at is None
            or not isinstance(status, str)
            or status not in {"ok", "warning", "error", "exhausted", "unknown"}
        ):
            continue
        windows[window_id] = QuotaWindow(
            used_percent=used,
            window_minutes=duration_ms // 60000,
            resets_at=resets_at,
            status=status,
        )
    if len(windows) != 2:
        return _claude_unavailable(unavailable, windows=windows, fetched_at=fetched_at)
    weekly = windows["7d"]
    return Headroom(
        availability="observed",
        used_percent=weekly.used_percent,
        remaining_percent=weekly.remaining_percent,
        window_minutes=weekly.window_minutes,
        resets_at=weekly.resets_at,
        limit_id="anthropic:7d",
        fetched_at=fetched_at,
        observed_at=fetched_at,
        staleness_seconds=max(0.0, (now - fetched_at).total_seconds()),
        windows=windows,
        source=" ".join(CLAUDE_USAGE_COMMAND),
    )


def read_claude_headroom(
    *,
    now: datetime,
    timeout_seconds: float = 60.0,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> Headroom:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Claude quota timeout must be positive and finite")
    try:
        completed = (run or subprocess.run)(
            list(CLAUDE_USAGE_COMMAND),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return _claude_unavailable("OMP Claude usage timed out")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return _claude_unavailable("OMP Claude usage could not be executed")
    if completed.returncode != 0:
        return _claude_unavailable("OMP Claude usage failed")
    try:
        payload = json.loads(completed.stdout)
    except (ValueError, TypeError):
        return _claude_unavailable("OMP Claude usage returned malformed JSON")
    return parse_claude_headroom(payload, now=now)


def claude_dispatch_quota_record(reading: Headroom) -> dict[str, Any]:
    return {
        "kind": CLAUDE_DISPATCH_QUOTA_KIND,
        "version": 1,
        "provider": "anthropic",
        "checked_at": (
            (reading.observed_at + timedelta(seconds=reading.staleness_seconds)).isoformat()
            if reading.observed_at is not None and reading.staleness_seconds is not None
            else None
        ),
        "reading": reading.model_dump(
            mode="json",
            include={
                "availability", "scope", "reason", "used_percent", "remaining_percent",
                "window_minutes", "observed_at", "fetched_at", "resets_at",
                "staleness_seconds", "windows", "limit_id", "source",
            },
        ),
    }


def persist_claude_dispatch_quota(job_dir: Path, record: dict[str, Any]) -> None:
    if not job_dir.is_dir():
        return
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    (job_dir / "claude-dispatch-quota.json").write_text(text, encoding="utf-8")
    for trial_dir in sorted(job_dir.iterdir()):
        if trial_dir.is_dir() and (trial_dir / "config.json").is_file():
            destination = trial_dir / CLAUDE_DISPATCH_QUOTA_PATH
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")


NO_OBSERVATION_REASON = (
    "no paid trial in the scanned job directories recorded a provider quota "
    "snapshot, so the remaining allowance is unknown"
)

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
        fetched_at=latest.fetched_at,
        windows=latest.windows,
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

    generated_at: datetime
    roots: tuple[Path, ...] = ()
    paid_agents: tuple[str, ...] = ()
    consumed: ConsumptionLedger = ConsumptionLedger()
    observations: tuple[QuotaObservation, ...] = ()
    headroom: Headroom = Headroom(availability="unavailable", reason=NO_OBSERVATION_REASON)
    cached_weighting_note: str = CACHED_WEIGHTING_NOTE

    def counter_resolution_percent(self) -> float | None:
        values = [
            observation.primary.used_percent
            for observation in self.observations
            if observation.primary is not None and observation.primary.used_percent is not None
        ]
        if not values:
            return None
        return 1.0 if all(float(value).is_integer() for value in values) else None


def _sidecar_snapshots(trial_dir: Path) -> list[tuple[datetime, dict[str, Any], Path]]:
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


def _claude_dispatch_observations(
    trial_dir: Path, *, job_name: str, trial_name: str
) -> list[QuotaObservation]:
    sidecar = trial_dir / CLAUDE_DISPATCH_QUOTA_PATH
    try:
        document = _as_object(json.loads(sidecar.read_text(encoding="utf-8")))
        if (
            document.get("kind") != CLAUDE_DISPATCH_QUOTA_KIND
            or document.get("version") != 1
            or document.get("provider") != "anthropic"
        ):
            return []
        reading = Headroom.model_validate(document.get("reading"))
    except (OSError, ValueError):
        return []
    if (
        reading.availability != "observed"
        or reading.observed_at is None
        or "7d" not in reading.windows
        or "5h" not in reading.windows
    ):
        return []
    return [
        QuotaObservation(
            job_name=job_name,
            trial_name=trial_name,
            agent="claude-code",
            observed_at=reading.observed_at,
            fetched_at=reading.fetched_at,
            primary=reading.windows["7d"],
            secondary=reading.windows["5h"],
            windows={key: reading.windows[key] for key in ("5h", "7d")},
            limit_id="anthropic:7d",
            source=sidecar.relative_to(trial_dir.parent).as_posix(),
        )
    ]


def _rate_limit_snapshots(
    trial_dir: Path,
    *,
    agent: str,
) -> list[tuple[datetime, dict[str, Any], Path]]:
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


def _claude_session_usage(
    trial_dir: Path,
) -> tuple[int | None, int | None, int | None, int | None]:
    turns = 0
    plain_input = 0
    cache_read_total = 0
    cache_create_total = 0
    output = 0
    counted: set[tuple[int | None, int | None, int | None, int | None]] = set()
    for session in sorted(trial_dir.glob(CLAUDE_SESSION_GLOB)):
        try:
            text = session.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if '"usage"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = _as_object(_as_object(event).get("message")).get("usage")
            usage = _as_object(usage)
            uncached = _as_int(usage.get("input_tokens"))
            cache_creation = _as_int(usage.get("cache_creation_input_tokens"))
            cache_read = _as_int(usage.get("cache_read_input_tokens"))
            out_tokens = _as_int(usage.get("output_tokens"))
            if (
                uncached is None
                and cache_creation is None
                and cache_read is None
                and out_tokens is None
            ):
                continue
            signature = (uncached, cache_creation, cache_read, out_tokens)
            if signature in counted:
                continue
            counted.add(signature)
            turns += 1
            plain_input += uncached or 0
            cache_read_total += cache_read or 0
            cache_create_total += cache_creation or 0
            output += out_tokens or 0
    if not counted:
        return None, None, None, None
    total_in = plain_input + cache_read_total + cache_create_total
    return turns, total_in, cache_read_total, output


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
    block = _as_object(payload)
    started = _parse_instant(block.get("started_at"))
    finished = _parse_instant(block.get("finished_at"))
    if started is None or finished is None:
        return None
    return (finished - started).total_seconds()


class _JobContext(BaseModel):

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
    input_tokens = _as_int(agent_result.get("n_input_tokens"))
    cache_tokens = _as_int(agent_result.get("n_cache_tokens"))
    output_tokens = _as_int(agent_result.get("n_output_tokens"))
    model_turns = _model_turns(trial_dir)
    if agent == "claude-code" and (
        (input_tokens is None and output_tokens is None) or not model_turns
    ):
        turns, session_input, session_cache, session_output = _claude_session_usage(trial_dir)
        if input_tokens is None and output_tokens is None:
            input_tokens, cache_tokens, output_tokens = session_input, session_cache, session_output
        if not model_turns:
            model_turns = turns
    consumption = TrialConsumption(
        job_name=job_name,
        trial_name=trial_name,
        agent=agent,
        task_name=_as_str(result.get("task_name")),
        model_name=_as_str(_as_object(agent_info.get("model_info")).get("name")),
        policy_rule=job.policy_rule,
        attempts_declared=job.attempts_declared,
        started_at=_parse_instant(result.get("started_at")),
        input_tokens=input_tokens,
        cache_tokens=cache_tokens,
        output_tokens=output_tokens,
        model_turns=model_turns,
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
        for observed_at, limits, rollout in _rate_limit_snapshots(trial_dir, agent=agent)
    ]
    if agent == "claude-code":
        observations = _claude_dispatch_observations(
            trial_dir, job_name=job_name, trial_name=trial_name
        )
    return consumption, observations


def load_quota_report(
    roots: Iterable[Path],
    *,
    now: datetime,
    paid_agents: frozenset[str] = PAID_AGENTS,
) -> QuotaReport:
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


def default_roots(repository_root: Path) -> tuple[Path, ...]:
    return (repository_root / "runs", repository_root / "research" / "evidence" / "runs")


