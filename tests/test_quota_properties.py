"""Property-based tests for quota accounting, UTC day rollover, and reserve/release invariants."""

from __future__ import annotations

import math
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from evallab.queue import (
    DirectoryQueue,
    Executor,
    PaidRunAuthorization,
    PolicyGate,
    new_ulid,
)
from evallab.quota import (
    ConsumptionLedger,
    QuotaObservation,
    QuotaWindow,
    TrialConsumption,
    _headroom,
    _parse_instant,
)
from evallab.schemas import (
    AutoRunRule,
    ExperimentSpec,
    QueueEvent,
    StandingApprovalsPolicy,
)

# --- Strategies ---

TIMEZONES = [
    UTC,
    ZoneInfo("America/New_York"),
    ZoneInfo("America/Los_Angeles"),
    ZoneInfo("Europe/London"),
    ZoneInfo("Asia/Tokyo"),
    ZoneInfo("Pacific/Auckland"),
    ZoneInfo("Pacific/Honolulu"),
]


@st.composite
def aware_datetimes(draw: st.DrawFn) -> datetime:
    tz = draw(st.sampled_from(TIMEZONES))
    dt = draw(
        st.datetimes(
            min_value=datetime(2020, 1, 1),
            max_value=datetime(2030, 1, 1),
        )
    )
    return dt.replace(tzinfo=tz)


@st.composite
def trial_consumptions(draw: st.DrawFn) -> TrialConsumption:
    dt = draw(st.one_of(st.none(), aware_datetimes()))
    has_tokens = draw(st.booleans())
    if has_tokens:
        input_tokens = draw(st.integers(min_value=0, max_value=1_000_000))
        cache_tokens = draw(st.integers(min_value=0, max_value=input_tokens))
        output_tokens = draw(st.integers(min_value=0, max_value=100_000))
    else:
        input_tokens = None
        cache_tokens = None
        output_tokens = None
    cost_usd = draw(st.one_of(st.none(), st.floats(min_value=0.0, max_value=50.0)))
    agent = draw(st.sampled_from(["codex", "claude-code", "oracle", "nop"]))
    task_name = draw(st.sampled_from(["task-a", "task-b", "task-c"]))
    job_name = draw(st.sampled_from(["job-1", "job-2", "job-3"]))
    policy_rule = draw(st.one_of(st.none(), st.sampled_from(["canary", "manual", "auto"])))

    return TrialConsumption(
        job_name=job_name,
        trial_name=f"trial-{draw(st.integers(min_value=1, max_value=1000))}",
        agent=agent,
        task_name=task_name,
        started_at=dt,
        input_tokens=input_tokens,
        cache_tokens=cache_tokens,
        output_tokens=output_tokens,
        reported_cost_usd=round(cost_usd, 4) if cost_usd is not None else None,
        policy_rule=policy_rule,
    )


# --- UTC-Day Rollover & Normalization Properties ---


@given(aware_datetimes())
@settings(max_examples=100, deadline=None)
def test_property_parse_instant_normalizes_to_utc(dt: datetime) -> None:
    """Any timezone offset is faithfully converted to UTC with tzinfo=UTC."""
    iso_text = dt.isoformat()
    parsed = _parse_instant(iso_text)
    assert parsed is not None
    assert parsed.tzinfo == UTC
    assert parsed == dt.astimezone(UTC)
    assert parsed.date() == dt.astimezone(UTC).date()


@given(
    st.lists(trial_consumptions(), min_size=1, max_size=30),
)
@settings(max_examples=50, deadline=None)
def test_property_consumption_ledger_utc_day_bucketing(trials: list[TrialConsumption]) -> None:
    """ConsumptionLedger.by_day partitions trials strictly by UTC date."""
    ledger = ConsumptionLedger(trials=tuple(trials))
    by_day = ledger.by_day()

    for day_key, day_totals in by_day.items():
        assert day_totals.paid_trials >= 0
        if day_key != "unavailable":
            parsed_date = date.fromisoformat(day_key)
            matching_trials = [
                t
                for t in trials
                if t.started_at and t.started_at.astimezone(UTC).date() == parsed_date
            ]
            assert day_totals.paid_trials == len(matching_trials)

    total_paid = sum(t.paid_trials for t in by_day.values())
    assert total_paid == len(trials)


@given(
    st.integers(min_value=0, max_value=23),
    st.sampled_from([-12, -8, -5, -4, 0, 1, 3, 5, 8, 9, 12, 14]),
)
@settings(max_examples=50, deadline=None)
def test_property_utc_day_boundary_rollover(hour: int, offset_hours: int) -> None:
    """Timestamps near midnight in local time are bucketed by UTC midnight, not local midnight."""
    tz = ZoneInfo("UTC") if offset_hours == 0 else ZoneInfo(f"Etc/GMT{-offset_hours:+d}")
    local_dt = datetime(2026, 8, 15, hour, 30, 0, tzinfo=tz)
    utc_dt = local_dt.astimezone(UTC)

    trial = TrialConsumption(
        job_name="job-rollover",
        trial_name="trial-1",
        agent="codex",
        started_at=local_dt,
        input_tokens=100,
        output_tokens=50,
    )

    expected_utc_day = utc_dt.date()
    assert trial.day == expected_utc_day
    ledger = ConsumptionLedger(trials=(trial,))
    by_day = ledger.by_day()
    assert expected_utc_day.isoformat() in by_day


def test_regression_trial_consumption_day_timezone_shrunk_counterexample() -> None:
    """Regression test for fuzz-found bug: TrialConsumption.day must normalize to UTC."""
    tz_plus_3 = ZoneInfo("Etc/GMT-3")
    local_dt = datetime(2026, 8, 15, 1, 30, 0, tzinfo=tz_plus_3)
    trial = TrialConsumption(
        job_name="job-test",
        trial_name="trial-1",
        agent="codex",
        started_at=local_dt,
    )
    assert trial.day == date(2026, 8, 14)


# --- Consumption Totals & Conservation Invariants ---


@given(st.lists(trial_consumptions(), min_size=0, max_size=40))
@settings(max_examples=50, deadline=None)
def test_property_consumption_totals_invariants_and_conservation(
    trials: list[TrialConsumption],
) -> None:
    """Totals are never negative, ratios are in [0, 1], and partitionings conserve sums."""
    ledger = ConsumptionLedger(trials=tuple(trials))
    totals = ledger.totals()

    assert totals.paid_trials == len(trials)
    assert totals.input_tokens >= 0
    assert totals.output_tokens >= 0
    assert totals.cache_tokens >= 0
    assert totals.total_tokens >= 0
    assert totals.uncached_input_tokens >= 0

    assert totals.total_tokens == totals.input_tokens + totals.output_tokens
    assert totals.uncached_input_tokens == max(0, totals.input_tokens - totals.cache_tokens)

    if totals.cached_input_ratio is not None:
        assert 0.0 <= totals.cached_input_ratio <= 1.0

    if totals.reported_cost_usd is not None:
        assert totals.reported_cost_usd >= 0.0

    # Test conservation across all groupings
    for grouping in (
        ledger.by_day(),
        ledger.by_agent(),
        ledger.by_task(),
        ledger.by_job(),
        ledger.by_policy_rule(),
    ):
        summed_trials = sum(b.paid_trials for b in grouping.values())
        summed_inputs = sum(b.input_tokens for b in grouping.values())
        summed_outputs = sum(b.output_tokens for b in grouping.values())
        summed_cache = sum(b.cache_tokens for b in grouping.values())
        summed_cost = sum(b.reported_cost_usd or 0.0 for b in grouping.values())

        assert summed_trials == totals.paid_trials
        assert summed_inputs == totals.input_tokens
        assert summed_outputs == totals.output_tokens
        assert summed_cache == totals.cache_tokens
        if totals.reported_cost_usd is not None:
            assert math.isclose(summed_cost, totals.reported_cost_usd, rel_tol=1e-3, abs_tol=1e-3)


# --- Headroom & Window Invariants ---


@given(
    st.floats(min_value=0.0, max_value=100.0),
    st.integers(min_value=1, max_value=10080),
)
@settings(max_examples=50, deadline=None)
def test_property_quota_window_invariants(used_pct: float, window_mins: int) -> None:
    """QuotaWindow properties are mathematically consistent and never negative."""
    w = QuotaWindow(used_percent=used_pct, window_minutes=window_mins)
    assert w.remaining_percent is not None
    assert round(w.remaining_percent + used_pct, 6) == 100.0
    assert w.window_hours is not None
    assert round(w.window_hours * 60, 6) == float(window_mins)


@given(
    st.lists(
        st.tuples(
            aware_datetimes(),
            st.floats(min_value=0.0, max_value=100.0),
        ),
        min_size=1,
        max_size=10,
    )
)
@settings(max_examples=50, deadline=None)
def test_property_headroom_selection_uses_latest_observation(
    obs_data: list[tuple[datetime, float]],
) -> None:
    """_headroom always selects the observation with the maximal observed_at timestamp."""
    now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
    observations = [
        QuotaObservation(
            job_name="test-job",
            trial_name=f"trial-{i}",
            agent="codex",
            source="rollout",
            observed_at=dt,
            primary=QuotaWindow(used_percent=pct, window_minutes=60),
        )
        for i, (dt, pct) in enumerate(obs_data)
    ]

    latest_obs = max(observations, key=lambda o: o.observed_at)
    headroom = _headroom(observations, now=now)

    assert headroom.availability == "observed"
    assert headroom.used_percent == latest_obs.primary.used_percent
    assert headroom.observed_at == latest_obs.observed_at


# --- Stateful Queue Reserve / Release Spending Fuzz ---


class QuotaReserveReleaseStateMachine(RuleBasedStateMachine):
    """Fuzzes Executor attempt reservation, completion release, failure retention,
    and UTC-day rollover in queue spend accounting."""

    def __init__(self) -> None:
        super().__init__()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.queue = DirectoryQueue(self.root / "queue")
        self.current_date = date(2026, 8, 16)
        self.clock_time = datetime(2026, 8, 16, 8, 0, 0, tzinfo=UTC)

        self.executor = Executor(
            repo_root=self.root,
            queue=self.queue,
            policy=StandingApprovalsPolicy(
                daily_cost_ceiling_usd=50.0,
                per_job_cost_ceiling_usd=10.0,
                quiet_failure_rule=3,
                auto_run=[AutoRunRule(name="controls", agents=["oracle"])],
                escalate_to_human=[],
            ),
            runner=lambda req: req.jobs_dir / req.name,
            ingester=lambda _path: None,
            spent_today=lambda: 0.0,
            consecutive_harness_failures=lambda: 0,
        )

        self.spec_attempts: dict[str, list[float]] = {}
        self.completed_specs: set[str] = set()
        self.spec_dates: dict[str, date] = {}
        self.next_id = 0

    def teardown(self) -> None:
        self.tempdir.cleanup()

    def _now(self) -> datetime:
        return self.clock_time

    @rule(cost=st.floats(min_value=0.5, max_value=10.0))
    def reserve_attempt(self, cost: float) -> None:
        self.next_id += 1
        spec_id = f"spec-{self.next_id}"
        est_cost = round(cost, 2)
        attempt_num = len(self.spec_attempts.get(spec_id, [])) + 1

        self.spec_attempts.setdefault(spec_id, []).append(est_cost)
        self.spec_dates[spec_id] = self.current_date

        self.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=spec_id,
                occurred_at=self._now(),
                event="dispatch_attempt_reserved",
                actor="executor",
                estimated_cost_usd=est_cost,
                attempt_number=attempt_num,
            )
        )

    @rule(cost=st.floats(min_value=0.5, max_value=5.0))
    def retry_existing_spec(self, cost: float) -> None:
        active = [
            sid
            for sid in self.spec_attempts
            if sid not in self.completed_specs and self.spec_dates[sid] == self.current_date
        ]
        if not active:
            return
        spec_id = active[0]
        est_cost = round(cost, 2)
        attempt_num = len(self.spec_attempts[spec_id]) + 1
        self.spec_attempts[spec_id].append(est_cost)

        self.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=spec_id,
                occurred_at=self._now(),
                event="dispatch_attempt_reserved",
                actor="executor",
                estimated_cost_usd=est_cost,
                attempt_number=attempt_num,
            )
        )

    @rule()
    def complete_spec(self) -> None:
        active = [
            sid
            for sid in self.spec_attempts
            if sid not in self.completed_specs and self.spec_dates[sid] == self.current_date
        ]
        if not active:
            return
        spec_id = active[0]
        self.completed_specs.add(spec_id)

        self.queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=spec_id,
                occurred_at=self._now(),
                event="dispatch_completed",
                actor="executor",
            )
        )

    @rule()
    def advance_to_next_utc_day(self) -> None:
        """Advance calendar to the next UTC day."""
        self.current_date += timedelta(days=1)
        self.clock_time = datetime(
            self.current_date.year,
            self.current_date.month,
            self.current_date.day,
            8,
            0,
            0,
            tzinfo=UTC,
        )

    @invariant()
    def reserved_spend_never_negative(self) -> None:
        reserved = self.executor._reserved_attempt_spend_today(now=self._now())
        assert reserved >= 0.0, f"Reserved spend went negative: {reserved}"

    @invariant()
    def reserved_spend_matches_unsettled_model(self) -> None:
        """The queue's calculation must match our independent model of unsettled attempts."""
        reserved = self.executor._reserved_attempt_spend_today(now=self._now())

        expected = 0.0
        for sid, estimates in self.spec_attempts.items():
            if self.spec_dates.get(sid) != self.current_date:
                # Prior day reservations do not count toward today
                continue
            if sid in self.completed_specs:
                # Final attempt is assumed in catalog; earlier attempts remain in reserve
                expected += sum(estimates[:-1])
            else:
                # In-flight or failed spec keeps all attempt reservations
                expected += sum(estimates)

        assert round(reserved, 2) == round(expected, 2), (
            f"Reserved spend mismatch on {self.current_date}: "
            f"computed={reserved} vs expected={expected}"
        )


TestQuotaAccountingProperties = QuotaReserveReleaseStateMachine.TestCase
TestQuotaAccountingProperties.settings = settings(
    max_examples=100, stateful_step_count=25, deadline=None
)


# --- PolicyGate Ceiling Invariant Property ---


@given(
    st.floats(min_value=0.0, max_value=30.0),
    st.floats(min_value=0.1, max_value=15.0),
    st.floats(min_value=5.0, max_value=20.0),
)
@settings(max_examples=50, deadline=None)
def test_property_policy_gate_cost_ceiling_invariant(
    spent_today: float,
    est_cost: float,
    ceiling: float,
) -> None:
    """PolicyGate strictly admits within ceiling and refuses with daily_cost_ceiling."""
    policy = StandingApprovalsPolicy(
        daily_cost_ceiling_usd=ceiling,
        per_job_cost_ceiling_usd=100.0,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="controls", agents=["oracle"])],
        escalate_to_human=[],
    )
    gate = PolicyGate(policy)

    now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
    spec_id = "01TESTSPEC0000000000000000"
    s = ExperimentSpec(
        spec_id=spec_id,
        name="test-spec",
        hypothesis="test",
        purpose="practice",
        task="library/tasks/event-summary",
        agent="codex",
        submitted_by="peter",
        submitted_at=now,
        est_cost_usd=est_cost,
    )
    auth = PaidRunAuthorization(spec_id=spec_id, actor="peter", authorized_at=now)

    decision = gate.decide(s, spent_today_usd=spent_today, authorization=auth)

    if spent_today + est_cost > ceiling:
        assert not decision.admitted
        assert decision.reason_code == "daily_cost_ceiling"
    else:
        assert decision.admitted
