"""`evallab preflight` — is it safe and sensible to run right now?

The surface an operator (or the nightly, at tick start) reads *before* anything
runs. Three questions, each answered from evidence already on disk:

1. **Per-provider remaining quota.** Built from :mod:`evallab.quota`, one
   reading per paid agent, because `codex` and `claude-code` are two separate
   subscriptions and one account's headroom says nothing about the other's.
   Both documented traps in `docs/quota-accounting.md` are honoured here:
   ``availability`` is checked before ``remaining_percent`` is read, and an
   unavailable reading prints UNKNOWN with the warning attached rather than a
   blank that reads as room to spare.
2. **The queue, grouped by purpose.** `ExperimentSpec.purpose` is WS-E item 1
   and may not exist in this build. Absence is reported as absence: the specs
   are still listed, and no bucket is invented for them.
3. **Power warnings.** Only for comparisons that are actually queued, computed
   with `cohort.py`'s existing estimator. When nothing comparative is queued
   this section says so instead of manufacturing a warning.

Boundaries this module keeps, all load-bearing:

- **No network, no subprocess, no credential store, no database, no paid call.**
  Everything comes from Harbor job directories and `queue/<state>/*.json`.
  `tests/test_preflight.py` asserts the property by making `subprocess` and
  `socket` raise for the whole render path, per `agents/CHECKS.md`.
- **No clock read outside :func:`preflight_at_tick_start`.** `now` is injected
  into :func:`build_preflight_report`, which is what makes staleness testable.
- **Nothing here authorises anything.** The refusal sentence is supplied by the
  caller through :data:`RefusalReader`; this module never decides admission,
  and it never restates a lab policy as the provider's statement (#70).
- **The module body imports nothing from `queue.py`.** `queue.py` is the core
  and this is a surface, so the dependency may only point one way. The default
  refusal reader is resolved by a deferred import at call time, which keeps the
  one-line tick wiring in `Executor._tick_locked` free of an import cycle.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from evallab.cohort import minimum_detectable_effect, pass_at_k_probability
from evallab.quota import (
    PAID_AGENTS,
    Headroom,
    default_roots,
    label,
    load_quota_report,
)
from evallab.schemas import ExperimentSpec, QueueState

#: A sentence explaining why dispatch would refuse, or ``None`` for "nothing in
#: this reading refuses". `queue.provider_reported_exhaustion` has exactly this
#: shape and is the default; injecting it keeps admission logic in `queue.py`
#: instead of being restated — and drifting — here.
RefusalReader = Callable[[Headroom], str | None]

#: Queue states holding work that has not finished. `done`, `failed`, and
#: `rejected` are history: they cannot consume quota and cannot be under-powered.
ACTIVE_QUEUE_STATES: tuple[QueueState, ...] = (
    "proposed",
    "pending",
    "approved",
    "waiting",
    "running",
)

#: The bucket for a spec that declares no purpose. It is deliberately not one of
#: the seven purposes: "we do not know" must never be filed under "baseline".
PURPOSE_UNAVAILABLE = "unavailable"

#: `ExperimentSpec.purpose` (WS-E item 1, `docs/build-plan.md`). Read from the
#: model rather than assumed, so this surface degrades honestly in a build that
#: predates the field instead of crashing or inventing a value.
PURPOSE_FIELD = "purpose"

COMPARISON_PURPOSE = "comparison"

PURPOSE_FIELD_ABSENT_NOTE = (
    "ExperimentSpec has no `purpose` field in this build, so no queued spec can "
    "declare one. WS-E item 1 adds it (docs/build-plan.md). Everything below is "
    "grouped by queue state only; the absence of the field is not the same fact "
    "as a queue of purposeless specs."
)

#: The trap that produced the original defect, restated where an operator reads
#: a number. Kept in step with `queue.QUOTA_UNKNOWN_WARNING`, which says the same
#: thing at the moment of authorisation.
UNKNOWN_IS_NOT_PLENTY = (
    "UNKNOWN is not 'plenty left'. This says the allowance could not be "
    "measured, not that a run fits inside it. Check the provider yourself "
    "before authorising anything billable."
)

#: Why pooling every queued comparison spec into one cohort is safe in the
#: warning direction, and only in that direction.
POOLING_NOTE = (
    "The queue carries no field linking two specs into one comparison, so every "
    "queued comparison spec is pooled into a single cohort here. Pooling can "
    "only overstate n_tasks, so a warning raised at the pooled n holds for every "
    "finer partition of these specs; a clean bill of health at the pooled n does "
    "not."
)


def _age(seconds: float | None) -> str:
    """Duration as the operator surfaces print it (`queue._age`, `quota._duration`)."""
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


def purpose_field_available() -> bool:
    """Whether this build's `ExperimentSpec` carries a `purpose` field."""
    return PURPOSE_FIELD in ExperimentSpec.model_fields


def spec_purpose(spec: ExperimentSpec) -> str | None:
    """The spec's declared purpose as a plain string, or ``None``.

    Tolerates an enum-valued field as well as the `Literal[...]` string the
    build plan specifies, so this surface cannot start printing
    `Purpose.baseline` if the declaration changes shape.
    """
    value = getattr(spec, PURPOSE_FIELD, None)
    if value is None:
        return None
    return str(getattr(value, "value", value))


@dataclass(frozen=True)
class ProviderQuota:
    """One paid provider's remaining allowance, with its own provenance."""

    agent: str
    headroom: Headroom
    snapshots: int
    paid_trials: int
    refusal: str | None = None

    @property
    def observed(self) -> bool:
        # Trap one, in the one place every renderer goes through: no caller may
        # reach `remaining_percent` without passing this first.
        return self.headroom.availability == "observed"


@dataclass(frozen=True)
class QueuedSpecView:
    """The handful of spec fields this surface reports, plus where it came from."""

    state: QueueState
    path: Path
    spec_id: str | None = None
    name: str | None = None
    agent: str | None = None
    task: str | None = None
    attempts: int | None = None
    billable: bool | None = None
    expected_reward: float | None = None
    purpose: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class QueueSurvey:
    """Active queue contents, grouped by declared purpose when there is one."""

    queue_root: Path
    present: bool
    purpose_available: bool
    groups: dict[str, tuple[QueuedSpecView, ...]] = field(default_factory=dict)
    unreadable: tuple[QueuedSpecView, ...] = ()

    @property
    def total(self) -> int:
        return sum(len(group) for group in self.groups.values())

    def comparisons(self) -> tuple[QueuedSpecView, ...]:
        return self.groups.get(COMPARISON_PURPOSE, ())


@dataclass(frozen=True)
class PowerAssessment:
    """Whether a queued comparison can reach a useful interval as declared."""

    evaluated: bool
    reason: str
    n_tasks: int | None = None
    k: int | None = None
    baseline: float | None = None
    minimum_detectable_effect: float | None = None
    useful_effect: float | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreflightReport:
    generated_at: datetime
    repo_root: Path
    quota_roots: tuple[Path, ...]
    providers: tuple[ProviderQuota, ...]
    queue: QueueSurvey
    power: PowerAssessment
    refuse_at_used_percent: float | None = None

    def refusals(self) -> tuple[str, ...]:
        """Every provider-stated reason a billable dispatch would refuse."""
        return tuple(
            f"{provider.agent}: {provider.refusal}"
            for provider in self.providers
            if provider.refusal
        )

    def hard_stopped(self) -> tuple[str, ...]:
        """Providers whose exhaustion is a lockout rather than an overage charge."""
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
    """One provider's reading, scanning only that provider's paid trials.

    `load_quota_report`'s `paid_agents` filter is what makes this per-provider:
    a report built for `{codex}` sees no `claude-code` snapshot, so neither
    account's headroom can be attributed to the other.
    """
    try:
        report = load_quota_report(roots, now=now, paid_agents=frozenset({agent}))
    except (OSError, ValueError) as exc:
        # A failed scan is an unavailable reading with its reason, never a
        # blank and never an exception: preflight must still print the other
        # providers and the queue.
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


def _spec_view(path: Path, state: QueueState) -> QueuedSpecView:
    try:
        spec = ExperimentSpec.model_validate_json(path.read_text())
    except (OSError, ValidationError) as exc:
        # A spec file this build cannot parse is a fact about the queue worth
        # printing. It is emphatically not an empty queue, and it must not stop
        # the surface: WS-E item 1 makes `purpose` required, at which point
        # every spec queued before it landed lands here.
        return QueuedSpecView(state=state, path=path, error=f"{type(exc).__name__}: {exc}")
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
        purpose=spec_purpose(spec),
    )


def survey_queue(
    queue_root: Path,
    *,
    states: Iterable[QueueState] = ACTIVE_QUEUE_STATES,
) -> QueueSurvey:
    """Group the unfinished queue by declared purpose, degrading honestly.

    Reads the state directories directly rather than through `DirectoryQueue`,
    which creates them on construction. A read-only surface must not bring a
    queue into existence as a side effect of reporting that there isn't one.
    """
    available = purpose_field_available()
    if not queue_root.is_dir():
        return QueueSurvey(queue_root=queue_root, present=False, purpose_available=available)

    buckets: dict[str, list[QueuedSpecView]] = {}
    unreadable: list[QueuedSpecView] = []
    for state in states:
        state_dir = queue_root / state
        if not state_dir.is_dir():
            continue
        for path in sorted(state_dir.glob("*.json")):
            view = _spec_view(path, state)
            if view.error is not None:
                unreadable.append(view)
                continue
            buckets.setdefault(view.purpose or PURPOSE_UNAVAILABLE, []).append(view)
    return QueueSurvey(
        queue_root=queue_root,
        present=True,
        purpose_available=available,
        groups={key: tuple(value) for key, value in sorted(buckets.items())},
        unreadable=tuple(unreadable),
    )


def assess_power(survey: QueueSurvey, *, useful_effect: float | None = None) -> PowerAssessment:
    """Can the queued comparison reach a useful interval at its declared attempts?

    Uses `cohort.minimum_detectable_effect`, the estimator the `evallab power`
    command and every comparison report already use. Nothing new is invented,
    including the threshold: "useful" is a spend judgement, so `useful_effect`
    stays unset unless an operator supplies one, exactly as
    `REFUSE_BILLABLE_AT_USED_PERCENT` stays unset in `queue.py`.
    """
    if not survey.purpose_available:
        return PowerAssessment(
            evaluated=False,
            reason=(
                "no comparison can be identified because `ExperimentSpec.purpose` "
                "does not exist in this build, so no power warning is asserted"
            ),
        )
    comparisons = survey.comparisons()
    if not comparisons:
        return PowerAssessment(
            evaluated=False,
            reason="no comparison is queued, so no power warning applies",
        )

    warnings: list[str] = []
    tasks = {view.task for view in comparisons if view.task}
    n_tasks = len(tasks)
    attempts = [view.attempts for view in comparisons if view.attempts is not None]
    # The weakest arm binds: a comparison cannot be paired at more attempts than
    # its least-attempted spec declares.
    k = min(attempts) if attempts else None

    if k is None:
        warnings.append("no queued comparison spec declares an attempt count")
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
    if baseline is not None and k is not None and n_tasks >= 2:
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

    return PowerAssessment(
        evaluated=True,
        reason=(
            f"{len(comparisons)} queued comparison spec(s) across {n_tasks} distinct task(s)"
        ),
        n_tasks=n_tasks,
        k=k,
        baseline=baseline,
        minimum_detectable_effect=effect,
        useful_effect=useful_effect,
        warnings=tuple(warnings),
    )


def _default_refusal() -> RefusalReader:
    """`queue.provider_reported_exhaustion`, imported at call time.

    Deferred on purpose. `queue.py` is the core and this module is a surface, so
    a module-level import here would make the one-line tick wiring in
    `Executor._tick_locked` a circular import. Resolving the name when the
    report is built works in either import order, because this module's body
    never touches `queue.py`.
    """
    from evallab.queue import provider_reported_exhaustion

    return provider_reported_exhaustion


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
) -> PreflightReport:
    """Everything preflight reports, from disk only.

    `now` is injected rather than read: staleness is the whole point of the
    quota block, and a surface that reads the clock cannot be tested against a
    fixed one (`agents/CHECKS.md`).

    `refusal` defaults to `queue.provider_reported_exhaustion`, resolved by a
    deferred import so this module's body stays free of `queue.py`. Pass your
    own reader to test the refusal path without standing up a queue.
    """
    root = repo_root.resolve()
    roots = tuple(quota_roots) if quota_roots is not None else default_roots(root)
    reader = refusal if refusal is not None else _default_refusal()
    providers = tuple(
        _provider_quota(agent, roots=roots, now=now, refusal=reader)
        for agent in sorted(paid_agents)
    )
    survey = survey_queue(queue_root if queue_root is not None else root / "queue")
    return PreflightReport(
        generated_at=now,
        repo_root=root,
        quota_roots=roots,
        providers=providers,
        queue=survey,
        power=assess_power(survey, useful_effect=useful_effect),
        refuse_at_used_percent=refuse_at_used_percent,
    )


def _provider_lines(provider: ProviderQuota) -> list[str]:
    headroom = provider.headroom
    lines = [f"{provider.agent}"]
    if not provider.observed:
        # Trap one. Nothing numeric is printed, and the blank is filled with the
        # warning rather than left for the reader to fill with optimism.
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
    if not survey.purpose_available:
        lines.append(f"  purpose {label('unavailable')}: {PURPOSE_FIELD_ABSENT_NOTE}")
    if not survey.groups and not survey.unreadable:
        lines.append("  nothing queued")
        return lines
    for purpose, views in survey.groups.items():
        billable = sum(1 for view in views if view.billable)
        header = (
            f"purpose not declared {label('unavailable')}"
            if purpose == PURPOSE_UNAVAILABLE
            else f"purpose {purpose}"
        )
        lines.append(f"  {header}: {len(views)} spec(s), {billable} billable")
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
    if not power.evaluated:
        lines.append(f"  none: {power.reason}")
        return lines
    lines.append(f"  {power.reason}")
    lines.append(
        f"  n_tasks {power.n_tasks}, k {power.k}, baseline "
        f"{'unavailable' if power.baseline is None else f'{power.baseline:.3f}'}"
    )
    if power.minimum_detectable_effect is not None and power.k is not None:
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
    """The whole surface as text. Identical bytes go to the CLI and the digest."""
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

    lines.append("")
    refusals = report.refusals()
    hard_stops = report.hard_stopped()
    if refusals:
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


def preflight_at_tick_start(
    repo_root: Path,
    *,
    refusal: RefusalReader | None = None,
    now: datetime | None = None,
    refuse_at_used_percent: float | None = None,
    useful_effect: float | None = None,
    emit: Callable[[str], None] = print,
) -> PreflightReport:
    """Build, print, and return the preflight. The tick-start entry point.

    The one line another mission adds at the top of `Executor._tick_locked` is
    a call to this function. It is the only place in this module that reads the
    clock, and it costs nothing: no paid call, no provider request, no network,
    no subprocess.
    """
    report = build_preflight_report(
        repo_root,
        now=now if now is not None else datetime.now(UTC),
        refusal=refusal,
        refuse_at_used_percent=refuse_at_used_percent,
        useful_effect=useful_effect,
    )
    emit(render_preflight(report))
    return report


def digest_section(report: PreflightReport) -> list[str]:
    """The preflight as Markdown for the nightly digest.

    The body is the byte-identical `render_preflight` text inside a fence, so
    the digest and `evallab preflight` can never disagree about what was read.
    The bullets above it exist so a scanner sees the verdict without reading
    the block.
    """
    refusals = report.refusals()
    hard_stops = report.hard_stopped()
    unavailable = [
        provider.agent for provider in report.providers if not provider.observed
    ]
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
        f"- Unfinished specs: {report.queue.total}"
        + (
            f", purpose {label('unavailable')} in this build"
            if not report.queue.purpose_available
            else ""
        ),
        f"- Power warnings: {len(report.power.warnings) if report.power.evaluated else 0}"
        + ("" if report.power.evaluated else f" ({report.power.reason})"),
        "",
        "```text",
    ]
    lines.extend(render_preflight(report).splitlines())
    lines.append("```")
    return lines
