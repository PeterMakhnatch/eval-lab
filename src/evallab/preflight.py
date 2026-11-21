from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from evallab.cohort import minimum_detectable_effect, pass_at_k_probability
from evallab.job_diff import DiffPreview, preview_diff, render_diff
from evallab.quota import (
    PAID_AGENTS,
    Headroom,
    default_roots,
    label,
    load_quota_report,
)
from evallab.schemas import ExperimentPurpose, ExperimentSpec, QueueState

RefusalReader = Callable[[Headroom], str | None]

ACTIVE_QUEUE_STATES: tuple[QueueState, ...] = (
    "proposed",
    "pending",
    "approved",
    "waiting",
    "running",
)

COMPARISON_PURPOSE: ExperimentPurpose = "comparison"


UNKNOWN_IS_NOT_PLENTY = (
    "UNKNOWN is not 'plenty left'. This says the allowance could not be "
    "measured, not that a run fits inside it. Check the provider yourself "
    "before authorising anything billable."
)

POOLING_NOTE = (
    "The queue carries no field linking two specs into one comparison, so every "
    "queued comparison spec is pooled into a single cohort here. Pooling can "
    "only overstate n_tasks, so a warning raised at the pooled n holds for every "
    "finer partition of these specs; a clean bill of health at the pooled n does "
    "not."
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


@dataclass(frozen=True)
class ProviderQuota:

    agent: str
    headroom: Headroom
    snapshots: int
    paid_trials: int
    refusal: str | None = None

    @property
    def observed(self) -> bool:
        return self.headroom.availability == "observed"


@dataclass(frozen=True)
class QueuedSpecView:

    state: QueueState
    path: Path
    spec_id: str | None
    name: str
    agent: str
    task: str
    attempts: int
    billable: bool
    expected_reward: float | None
    purpose: ExperimentPurpose


@dataclass(frozen=True)
class UnreadableQueuedSpec:

    state: QueueState
    path: Path
    error: str


@dataclass(frozen=True)
class QueueSurvey:

    queue_root: Path
    present: bool
    groups: dict[ExperimentPurpose, tuple[QueuedSpecView, ...]] = field(default_factory=dict)
    unreadable: tuple[UnreadableQueuedSpec, ...] = ()

    @property
    def total(self) -> int:
        return sum(len(group) for group in self.groups.values())

    def comparisons(self) -> tuple[QueuedSpecView, ...]:
        return self.groups.get(COMPARISON_PURPOSE, ())


@dataclass(frozen=True)
class PowerNotEvaluated:

    reason: str
    evaluated: bool = field(default=False, init=False)
    warnings: tuple[str, ...] = field(default=(), init=False)


@dataclass(frozen=True)
class PowerEvaluation:

    reason: str
    n_tasks: int
    k: int
    baseline: float | None
    minimum_detectable_effect: float | None
    useful_effect: float | None
    warnings: tuple[str, ...] = ()
    evaluated: bool = field(default=True, init=False)


PowerAssessment = PowerNotEvaluated | PowerEvaluation


@dataclass(frozen=True)
class PreflightReport:
    generated_at: datetime
    repo_root: Path
    quota_roots: tuple[Path, ...]
    providers: tuple[ProviderQuota, ...]
    queue: QueueSurvey
    power: PowerAssessment
    refuse_at_used_percent: float | None = None
    trial_diffs: tuple[tuple[str, DiffPreview], ...] = ()
    diff_errors: tuple[str, ...] = ()

    def refusals(self) -> tuple[str, ...]:
        return tuple(
            f"{provider.agent}: {provider.refusal}"
            for provider in self.providers
            if provider.refusal
        )

    def hard_stopped(self) -> tuple[str, ...]:
        return tuple(
            provider.agent
            for provider in self.providers
            if provider.observed and provider.headroom.hard_stop
        )


def _provider_quota(
    agent: str,
    *,
    roots: Sequence[Path],
    now: datetime,
    refusal: RefusalReader | None,
) -> ProviderQuota:
    try:
        report = load_quota_report(roots, now=now, paid_agents=frozenset({agent}))
    except (OSError, ValueError) as exc:
        return ProviderQuota(
            agent=agent,
            headroom=Headroom(
                availability="unavailable",
                reason=f"the quota scan failed ({type(exc).__name__}: {exc})",
            ),
            snapshots=0,
            paid_trials=0,
        )
    headroom = report.headroom
    return ProviderQuota(
        agent=agent,
        headroom=headroom,
        snapshots=len(report.observations),
        paid_trials=report.consumed.totals().paid_trials,
        refusal=refusal(headroom) if refusal is not None else None,
    )


def _parse_spec_view(
    path: Path,
    state: QueueState,
) -> QueuedSpecView | UnreadableQueuedSpec:
    try:
        spec = ExperimentSpec.model_validate_json(path.read_text())
    except (OSError, ValidationError) as exc:
        return UnreadableQueuedSpec(
            state=state,
            path=path,
            error=f"{type(exc).__name__}: {exc}",
        )
    return QueuedSpecView(
        state=state,
        path=path,
        spec_id=spec.spec_id,
        name=spec.name,
        agent=spec.agent,
        task=spec.task,
        attempts=spec.attempts,
        billable=spec.billable,
        expected_reward=spec.expected_reward,
        purpose=spec.purpose,
    )


def survey_queue(
    queue_root: Path,
    *,
    states: Iterable[QueueState] = ACTIVE_QUEUE_STATES,
) -> QueueSurvey:
    if not queue_root.is_dir():
        return QueueSurvey(queue_root=queue_root, present=False)

    buckets: dict[ExperimentPurpose, list[QueuedSpecView]] = {}
    unreadable: list[UnreadableQueuedSpec] = []
    for state in states:
        state_dir = queue_root / state
        if not state_dir.is_dir():
            continue
        for path in sorted(state_dir.glob("*.json")):
            view = _parse_spec_view(path, state)
            if isinstance(view, UnreadableQueuedSpec):
                unreadable.append(view)
                continue
            buckets.setdefault(view.purpose, []).append(view)
    return QueueSurvey(
        queue_root=queue_root,
        present=True,
        groups={key: tuple(value) for key, value in sorted(buckets.items())},
        unreadable=tuple(unreadable),
    )


def assess_power(survey: QueueSurvey, *, useful_effect: float | None = None) -> PowerAssessment:
    comparisons = survey.comparisons()
    if not comparisons:
        return PowerNotEvaluated(
            reason="no comparison is queued, so no power warning applies",
        )

    warnings: list[str] = []
    tasks = {view.task for view in comparisons}
    n_tasks = len(tasks)
    k = min(view.attempts for view in comparisons)

    if n_tasks < 2:
        warnings.append(
            f"{n_tasks} distinct task(s) across {len(comparisons)} queued comparison spec(s): "
            "a task-paired interval needs at least two, so this comparison cannot produce "
            "one at any attempt count"
        )

    declared = [
        view.expected_reward
        for view in comparisons
        if view.expected_reward is not None and 0.0 <= view.expected_reward < 1.0
    ]
    baseline = min(declared) if declared else None
    if baseline is None:
        warnings.append(
            "no queued comparison spec declares an `expected_reward` inside [0, 1), so the "
            "baseline pass rate is unavailable and the detectable effect cannot be computed "
            "before the spend happens"
        )

    effect: float | None = None
    if baseline is not None and n_tasks >= 2:
        try:
            effect = minimum_detectable_effect(n_tasks=n_tasks, k=k, baseline=baseline)
        except ValueError as exc:
            warnings.append(f"the power estimator refused these inputs: {exc}")
        else:
            if effect is None:
                warnings.append(
                    f"no per-attempt difference is detectable at n_tasks={n_tasks}, k={k}, "
                    f"baseline={baseline:.3f}: this comparison cannot reach an interval at "
                    "its declared attempt count"
                )
            elif useful_effect is not None and effect > useful_effect:
                warnings.append(
                    f"the smallest detectable per-attempt difference is {effect:.4f}, larger "
                    f"than the {useful_effect:.4f} supplied as useful: anything smaller than "
                    f"{effect:.4f} would be invisible to this comparison"
                )

    return PowerEvaluation(
        reason=(f"{len(comparisons)} queued comparison spec(s) across {n_tasks} distinct task(s)"),
        n_tasks=n_tasks,
        k=k,
        baseline=baseline,
        minimum_detectable_effect=effect,
        useful_effect=useful_effect,
        warnings=tuple(warnings),
    )


def _default_refusal() -> RefusalReader:
    from evallab.queue import provider_reported_exhaustion

    return provider_reported_exhaustion


def preview_spec_diff(spec: ExperimentSpec, repo_root: Path) -> DiffPreview:
    from evallab.queue import Executor

    request = Executor.prepare_request(spec, repo_root=repo_root)
    return preview_diff(request, repo_root=repo_root)


def build_preflight_report(
    repo_root: Path,
    *,
    now: datetime,
    paid_agents: Iterable[str] = PAID_AGENTS,
    quota_roots: Sequence[Path] | None = None,
    queue_root: Path | None = None,
    refusal: RefusalReader | None = None,
    refuse_at_used_percent: float | None = None,
    useful_effect: float | None = None,
    diff_reader: Callable[[ExperimentSpec, Path], DiffPreview] = preview_spec_diff,
) -> PreflightReport:
    root = repo_root.resolve()
    roots = tuple(quota_roots) if quota_roots is not None else default_roots(root)
    reader = refusal if refusal is not None else _default_refusal()
    providers = tuple(
        _provider_quota(agent, roots=roots, now=now, refusal=reader)
        for agent in sorted(paid_agents)
    )
    survey = survey_queue(queue_root if queue_root is not None else root / "queue")
    trial_diffs = []
    diff_errors = []
    for group in survey.groups.values():
        for item in group:
            if item.state == "running":
                continue
            try:
                spec = ExperimentSpec.model_validate_json(item.path.read_text())
                trial_diffs.append((item.name, diff_reader(spec, root)))
            except (OSError, ValueError, RuntimeError, ImportError) as exc:
                diff_errors.append(f"{item.name}: {exc}")
    return PreflightReport(
        generated_at=now,
        repo_root=root,
        quota_roots=roots,
        providers=providers,
        queue=survey,
        power=assess_power(survey, useful_effect=useful_effect),
        refuse_at_used_percent=refuse_at_used_percent,
        trial_diffs=tuple(trial_diffs),
        diff_errors=tuple(diff_errors),
    )


def _provider_lines(provider: ProviderQuota) -> list[str]:
    headroom = provider.headroom
    lines = [f"{provider.agent}"]
    if not provider.observed:
        lines += [
            f"  remaining allowance      UNKNOWN {label('unavailable')}",
            f"    reason: {headroom.reason or 'not reported'}",
            f"    {UNKNOWN_IS_NOT_PLENTY}",
            f"  paid trials seen         {provider.paid_trials} {label('observed')}",
            f"  quota snapshots          {provider.snapshots} {label('observed')}",
        ]
        return lines
    lines += [
        f"  used_percent             {headroom.used_percent} {label('observed')}",
        f"  remaining_percent        {headroom.remaining_percent} {label('observed')} "
        "(account-wide, whole percentage points)",
        f"  window                   {headroom.window_minutes} minutes "
        f"({_age((headroom.window_minutes or 0) * 60)})",
        f"  resets_at                {_instant(headroom.resets_at)}",
        f"  observed_at              {_instant(headroom.observed_at)}",
        f"  staleness                {_age(headroom.staleness_seconds)} old",
        f"  credits_balance          {headroom.credits_balance or label('unavailable')}",
        f"  hard stop                {headroom.hard_stop}",
        f"    {headroom.hard_stop_note}",
        f"  plan_type / limit_id     {headroom.plan_type or label('unavailable')} / "
        f"{headroom.limit_id or label('unavailable')}",
        f"  lab's share of that      {label(headroom.lab_attributable)}",
        f"  quota snapshots          {provider.snapshots} {label('observed')}",
        f"  paid trials seen         {provider.paid_trials} {label('observed')}",
    ]
    if headroom.rate_limit_reached_type is not None:
        lines.append(f"  rate_limit_reached_type  {headroom.rate_limit_reached_type}")
    if provider.refusal:
        lines.append(f"  REFUSES BILLABLE WORK    {provider.refusal}")
    return lines


def _queue_lines(survey: QueueSurvey) -> list[str]:
    lines = [f"QUEUE BY PURPOSE (states: {', '.join(ACTIVE_QUEUE_STATES)})"]
    if not survey.present:
        lines.append(f"  no queue directory at {survey.queue_root} {label('unavailable')}")
        return lines
    if not survey.groups and not survey.unreadable:
        lines.append("  nothing queued")
        return lines
    for purpose, views in survey.groups.items():
        billable = sum(1 for view in views if view.billable)
        lines.append(f"  purpose {purpose}: {len(views)} spec(s), {billable} billable")
        for view in views:
            lines.append(
                f"    [{view.state}] {view.name} — task {view.task}, agent {view.agent}, "
                f"{view.attempts} attempt(s)"
            )
    if survey.unreadable:
        lines.append(f"  unreadable: {len(survey.unreadable)} spec file(s) this build cannot parse")
        for view in survey.unreadable:
            lines.append(f"    [{view.state}] {view.path.name} — {view.error}")
    return lines


def _power_lines(power: PowerAssessment) -> list[str]:
    lines = ["POWER WARNINGS (queued comparisons only)"]
    if isinstance(power, PowerNotEvaluated):
        lines.append(f"  none: {power.reason}")
        return lines
    lines.append(f"  {power.reason}")
    lines.append(
        f"  n_tasks {power.n_tasks}, k {power.k}, baseline "
        f"{'unavailable' if power.baseline is None else f'{power.baseline:.3f}'}"
    )
    if power.minimum_detectable_effect is not None:
        baseline = power.baseline or 0.0
        implied = pass_at_k_probability(
            min(1.0, baseline + power.minimum_detectable_effect), power.k
        ) - pass_at_k_probability(baseline, power.k)
        lines.append(
            f"  minimum detectable per-attempt difference {power.minimum_detectable_effect:.4f} "
            f"(implied pass@{power.k} difference {implied:.4f})"
        )
    if power.warnings:
        lines.extend(f"  WARNING: {warning}" for warning in power.warnings)
    else:
        lines.append("  no warning: the queued comparison reaches an interval as declared")
    lines.append(f"  {POOLING_NOTE}")
    return lines


def render_preflight(report: PreflightReport) -> str:
    lines = [
        "evallab preflight — is it safe and sensible to run right now",
        f"generated_at: {report.generated_at.isoformat()}",
        f"repository:   {report.repo_root}",
        f"quota roots:  {', '.join(str(root) for root in report.quota_roots)}",
        "",
        "PER-PROVIDER REMAINING QUOTA (scope: account, NOT the lab; provider-reported)",
    ]
    for provider in report.providers:
        lines.append("")
        lines.extend(_provider_lines(provider))
    lines.append("")
    ceiling = (
        f"{report.refuse_at_used_percent} percent used"
        if report.refuse_at_used_percent is not None
        else "unset, so no lab ceiling refuses anything"
    )
    lines.append(f"  lab refusal ceiling      {ceiling} (reason code subscription_quota_ceiling)")
    lines.append(
        "    A lab ceiling is a spend decision and is recorded under its own reason "
        "code, never as the provider's statement."
    )

    lines.append("")
    lines.extend(_queue_lines(report.queue))
    lines.append("")
    lines.extend(_power_lines(report.power))
    for name, preview in report.trial_diffs:
        lines += ["", f"spec: {name}", render_diff(preview).rstrip()]
    lines.extend(f"DIFF REFUSED: {error}" for error in report.diff_errors)

    lines.append("")
    refusals = report.refusals()
    hard_stops = report.hard_stopped()
    if report.diff_errors:
        lines.append("VERDICT: trial diff refused — " + "; ".join(report.diff_errors))
    elif refusals:
        lines.append("VERDICT: billable work would be refused — " + "; ".join(refusals))
    elif hard_stops:
        lines.append(
            "VERDICT: nothing in these readings refuses, but "
            f"{', '.join(hard_stops)} has no overflow credits, so exhausting the window "
            "is a lockout until it resets, not an extra charge"
        )
    else:
        lines.append(
            "VERDICT: nothing in these readings refuses billable work. That is not the "
            "same as headroom being confirmed — read the per-provider block above."
        )
    return "\n".join(lines)


def digest_section(report: PreflightReport) -> list[str]:
    refusals = report.refusals()
    hard_stops = report.hard_stopped()
    unavailable = [provider.agent for provider in report.providers if not provider.observed]
    lines = [
        "## Preflight",
        "",
        "What `uv run evallab preflight` printed for this digest: remaining quota per "
        "paid provider, the unfinished queue grouped by declared purpose, and power "
        "warnings for queued comparisons. Read from Harbor job directories and "
        "`queue/` only — no network call, no subprocess, no paid call.",
        "",
        f"- Providers refusing billable work: {'; '.join(refusals) if refusals else 'none'}",
        f"- Providers with no readable allowance: "
        f"{', '.join(unavailable) if unavailable else 'none'} "
        f"({label('unavailable')} is not 'plenty left')",
        f"- Providers whose exhaustion is a lockout, not a charge: "
        f"{', '.join(hard_stops) if hard_stops else 'none'}",
        f"- Unfinished specs: {report.queue.total}",
        f"- Power warnings: {len(report.power.warnings) if report.power.evaluated else 0}"
        + ("" if report.power.evaluated else f" ({report.power.reason})"),
        "",
        "```text",
    ]
    lines.extend(render_preflight(report).splitlines())
    lines.append("```")
    return lines
