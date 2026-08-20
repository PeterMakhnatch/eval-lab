"""Contract tests for subscription-quota accounting.

Every probe is injected: job directories are built under ``tmp_path`` and the
instant is passed in, so nothing here depends on ``~/.codex``, the Keychain, the
network, the database, or the wall clock.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

from evallab.quota import (
    CACHED_WEIGHTING_NOTE,
    NO_OBSERVATION_REASON,
    PAID_AGENTS,
    ConsumptionLedger,
    QuotaReport,
    TrialConsumption,
    default_roots,
    label,
    load_quota_report,
    main,
    render_report,
)

NOW = datetime(2026, 8, 16, 18, 0, 0, tzinfo=UTC)

#: Committed evidence bundles: what a fresh clone has when ``runs/`` does not exist.
PROMOTED_RUNS = Path(__file__).resolve().parents[1] / "research/evidence/runs"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def make_job(
    root: Path,
    *,
    name: str = "canary-sample-codex-20260815",
    attempts: int = 3,
    policy_rule: str | None = "canary",
) -> Path:
    job = root / name
    write_json(
        job / "result.json",
        {
            "id": "00000000-0000-0000-0000-0000000000ff",
            "started_at": "2026-08-15T06:00:00Z",
            "finished_at": "2026-08-15T07:00:00Z",
            "n_total_trials": 1,
            "stats": {"n_completed_trials": 1},
        },
    )
    metadata: dict[str, object] = {
        "command": ["harbor", "run", "--n-attempts", str(attempts), "--agent", "codex"]
    }
    if policy_rule is not None:
        metadata["experiment"] = {"policy_rule": policy_rule}
    write_json(job / "lab-metadata.json", metadata)
    return job


def add_trial(
    job: Path,
    *,
    name: str,
    agent: str = "codex",
    task_name: str = "local-lab/event-summary",
    started_at: str = "2026-08-15T06:30:00Z",
    input_tokens: int | None = 1_000,
    cache_tokens: int | None = 800,
    output_tokens: int | None = 20,
    cost_usd: float | None = 0.05,
    exception_type: str | None = None,
) -> Path:
    trial = job / name
    agent_result: dict[str, object] | None = {
        "n_input_tokens": input_tokens,
        "n_cache_tokens": cache_tokens,
        "n_output_tokens": output_tokens,
        "cost_usd": cost_usd,
    }
    write_json(
        trial / "result.json",
        {
            "id": f"trial-{name}",
            "trial_name": name,
            "task_name": task_name,
            "started_at": started_at,
            "finished_at": "2026-08-15T06:32:00Z",
            "agent_info": {
                "name": agent,
                "version": "0.147.0",
                "model_info": {"name": "gpt-5.6-terra", "provider": "openai"},
            },
            "agent_result": agent_result,
            "exception_info": (
                None
                if exception_type is None
                else {"exception_type": exception_type, "exception_message": "boom"}
            ),
        },
    )
    return trial


def add_rollout(
    trial: Path,
    *,
    events: list[dict[str, object]],
    day: str = "2026/08/15",
    session: str = "rollout-2026-08-15T06-30-00-abc.jsonl",
) -> Path:
    rollout = trial / "agent/sessions" / day / session
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    return rollout


def token_count_event(
    *,
    timestamp: str,
    used_percent: float | None = 70.0,
    rate_limits: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "token_count",
        "info": {"total_token_usage": {"input_tokens": 1_000, "output_tokens": 20}},
    }
    if rate_limits:
        payload["rate_limits"] = {
            "limit_id": "codex",
            "limit_name": None,
            "primary": {
                "used_percent": used_percent,
                "window_minutes": 10_080,
                "resets_at": 1787250769,
            },
            "secondary": None,
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            "plan_type": "prolite",
            "rate_limit_reached_type": None,
        }
    return {"timestamp": timestamp, "type": "event_msg", "payload": payload}


def add_quota_sidecar(
    trial: Path,
    *,
    timestamps: list[str],
    used_percent: float | None = 70.0,
    kind: str = "evallab-rate-limits-sidecar",
    stem: str = "rollout-2026-08-15T06-30-00-abc",
) -> Path:
    """An R4 quota sidecar, shaped exactly as ``promote_codex_bundle.py`` writes it.

    The ``rate_limits`` block is taken from :func:`token_count_event`, so the
    fixture cannot drift from the rollout shape the promoter whitelists.
    """
    limits = token_count_event(timestamp=timestamps[0], used_percent=used_percent)
    rate_limits = limits["payload"]["rate_limits"]  # type: ignore[index]
    sidecar = trial / "agent/quota" / f"{stem}.rate-limits.json"
    write_json(
        sidecar,
        {
            "schema_version": 1,
            "rule": "R4",
            "kind": kind,
            "source_path": f"{trial.name}/agent/sessions/2026/08/15/{stem}.jsonl",
            "source_bytes": 47_038,
            "source_sha256": "sha256:4f7d7449f0ae97f83e00784601b94b0b9985b9fbd0510604bd32ef54",
            "source_omitted_by_rule": "R2",
            "dropped_field_names": [],
            "snapshot_count": len(timestamps),
            "snapshots": [
                {"timestamp": timestamp, "rate_limits": rate_limits}
                for timestamp in timestamps
            ],
        },
    )
    return sidecar


# --- consumption ---------------------------------------------------------


def test_only_paid_agents_enter_the_ledger(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    add_trial(job, name="event-summary__codex", agent="codex")
    add_trial(job, name="event-summary__cursor", agent="cursor-cli")
    add_trial(job, name="event-summary__antigravity", agent="antigravity-cli")
    add_trial(job, name="event-summary__claude", agent="claude-code")
    add_trial(job, name="event-summary__free", agent="oracle")
    add_trial(job, name="event-summary__free2", agent="nop")

    report = load_quota_report([tmp_path], now=NOW)

    assert {trial.agent for trial in report.consumed.trials} == {
        "codex",
        "cursor-cli",
        "antigravity-cli",
        "claude-code",
    }
    assert report.paid_agents == ("antigravity-cli", "claude-code", "codex", "cursor-cli")
    assert "oracle" not in PAID_AGENTS and "nop" not in PAID_AGENTS
    assert "cursor-cli" in PAID_AGENTS and "antigravity-cli" in PAID_AGENTS
    assert "codex" in PAID_AGENTS and "claude-code" in PAID_AGENTS

def test_observed_token_counts_are_summed_not_estimated(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    add_trial(job, name="a__1", input_tokens=71_542, cache_tokens=65_280, output_tokens=648)
    add_trial(job, name="a__2", input_tokens=86_942, cache_tokens=79_360, output_tokens=877)

    totals = load_quota_report([tmp_path], now=NOW).consumed.totals()

    assert totals.input_tokens == 158_484
    assert totals.cache_tokens == 144_640
    assert totals.output_tokens == 1_525
    assert totals.total_tokens == 160_009
    assert totals.tokens_availability == "observed"


def test_a_trial_without_usage_is_unavailable_never_zero(tmp_path: Path) -> None:
    """A paid trial that recorded nothing must not be reported as free."""
    job = make_job(tmp_path)
    add_trial(
        job,
        name="a__failed",
        input_tokens=None,
        cache_tokens=None,
        output_tokens=None,
        cost_usd=None,
        exception_type="ValueError",
    )

    report = load_quota_report([tmp_path], now=NOW)
    trial = report.consumed.trials[0]
    totals = report.consumed.totals()

    assert trial.total_tokens is None
    assert trial.usage_availability == "unavailable"
    assert totals.paid_trials == 1
    assert totals.trials_with_observed_usage == 0
    assert totals.trials_without_usage_evidence == 1
    assert totals.tokens_availability == "unavailable"
    assert totals.exception_types == {"ValueError": 1}
    assert totals.reported_cost_usd is None


def test_dispatched_and_consumed_are_counted_separately(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    add_trial(job, name="a__ok")
    add_trial(job, name="a__dead", input_tokens=None, output_tokens=None, cost_usd=None)

    totals = load_quota_report([tmp_path], now=NOW).consumed.totals()

    assert (totals.paid_trials, totals.trials_with_observed_usage) == (2, 1)


def test_model_turns_come_from_the_rollout_and_are_none_without_one(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    with_rollout = add_trial(job, name="a__turns")
    add_rollout(
        with_rollout,
        events=[
            {"timestamp": "2026-08-15T06:30:01Z", "payload": {"type": "session_meta"}},
            token_count_event(timestamp="2026-08-15T06:30:02Z"),
            token_count_event(timestamp="2026-08-15T06:30:03Z"),
        ],
    )
    add_trial(job, name="a__norollout")

    report = load_quota_report([tmp_path], now=NOW)
    trials = {trial.trial_name: trial for trial in report.consumed.trials}

    assert trials["a__turns"].model_turns == 2
    assert trials["a__norollout"].model_turns is None


def test_attempts_are_summed_per_job_not_per_trial(tmp_path: Path) -> None:
    first = make_job(tmp_path, name="job-one", attempts=3)
    second = make_job(tmp_path, name="job-two", attempts=3)
    for job in (first, second):
        for index in range(3):
            add_trial(job, name=f"{job.name}__{index}")

    totals = load_quota_report([tmp_path], now=NOW).consumed.totals()

    assert totals.jobs == 2
    assert totals.paid_trials == 6
    assert totals.attempts_declared == 6


def test_attempts_and_policy_rule_are_unavailable_without_lab_metadata(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    (job / "lab-metadata.json").unlink()
    add_trial(job, name="a__1")

    trial = load_quota_report([tmp_path], now=NOW).consumed.trials[0]

    assert trial.attempts_declared is None
    assert trial.policy_rule is None


def test_policy_rule_grouping_names_what_authorised_the_spend(tmp_path: Path) -> None:
    job = make_job(tmp_path, policy_rule="canary")
    add_trial(job, name="a__1")

    grouping = load_quota_report([tmp_path], now=NOW).consumed.by_policy_rule()

    assert list(grouping) == ["canary"]


def test_groupings_split_by_day_task_and_agent(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    add_trial(job, name="a__1", started_at="2026-08-15T06:30:00Z", task_name="t/one")
    add_trial(job, name="a__2", started_at="2026-08-16T06:30:00Z", task_name="t/two")

    ledger = load_quota_report([tmp_path], now=NOW).consumed

    assert list(ledger.by_day()) == ["2026-08-15", "2026-08-16"]
    assert list(ledger.by_task()) == ["t/one", "t/two"]
    assert list(ledger.by_agent()) == ["codex"]


def test_a_missing_start_groups_as_unavailable_rather_than_a_guess(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    payload = json.loads((trial / "result.json").read_text())
    payload["started_at"] = None
    write_json(trial / "result.json", payload)

    ledger = load_quota_report([tmp_path], now=NOW).consumed

    assert list(ledger.by_day()) == ["unavailable"]
    assert ledger.trials[0].day is None


def test_since_excludes_undated_trials_so_a_gate_undercounts_safely(tmp_path: Path) -> None:
    ledger = ConsumptionLedger(
        trials=(
            TrialConsumption(
                job_name="j",
                trial_name="old",
                agent="codex",
                started_at=datetime(2026, 8, 14, tzinfo=UTC),
            ),
            TrialConsumption(
                job_name="j",
                trial_name="new",
                agent="codex",
                started_at=datetime(2026, 8, 16, tzinfo=UTC),
            ),
            TrialConsumption(job_name="j", trial_name="undated", agent="codex"),
        )
    )

    recent = ledger.since(datetime(2026, 8, 16, tzinfo=UTC))

    assert [trial.trial_name for trial in recent.trials] == ["new"]


def test_for_agent_narrows_the_ledger(tmp_path: Path) -> None:
    ledger = ConsumptionLedger(
        trials=(
            TrialConsumption(job_name="j", trial_name="c", agent="codex"),
            TrialConsumption(job_name="j", trial_name="k", agent="claude-code"),
            TrialConsumption(job_name="j", trial_name="u", agent="cursor-cli"),
            TrialConsumption(job_name="j", trial_name="g", agent="antigravity-cli"),
        )
    )

    assert [t.trial_name for t in ledger.for_agent("claude-code").trials] == ["k"]
    assert [t.trial_name for t in ledger.for_agent("cursor-cli").trials] == ["u"]
    assert [t.trial_name for t in ledger.for_agent("antigravity-cli").trials] == ["g"]
    assert [t.trial_name for t in ledger.for_agent("codex").trials] == ["c"]
    assert ledger.for_agent("nop").trials == ()

def test_reported_cost_is_carried_but_named_as_a_list_price_equivalent(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    add_trial(job, name="a__1", cost_usd=0.033356)

    report = load_quota_report([tmp_path], now=NOW)

    assert report.consumed.totals().reported_cost_usd == 0.033356
    assert "NOT subscription spend" in render_report(report)


# --- headroom ------------------------------------------------------------


def test_headroom_is_read_from_the_trials_own_rollout_snapshot(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    add_rollout(
        trial,
        events=[
            token_count_event(timestamp="2026-08-15T06:30:02Z", used_percent=70.0),
            token_count_event(timestamp="2026-08-15T06:31:02Z", used_percent=92.0),
        ],
    )

    report = load_quota_report([tmp_path], now=NOW)
    headroom = report.headroom

    assert len(report.observations) == 2
    assert headroom.availability == "observed"
    assert headroom.used_percent == 92.0
    assert headroom.remaining_percent == 8.0
    assert headroom.window_minutes == 10_080
    assert headroom.observed_at == datetime(2026, 8, 15, 6, 31, 2, tzinfo=UTC)
    assert headroom.resets_at == datetime(2026, 8, 20, 18, 32, 49, tzinfo=UTC)
    assert headroom.plan_type == "prolite"
    assert headroom.credits_balance == "0"
    assert headroom.has_credits is False


def test_headroom_uses_the_latest_snapshot_across_trials(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    early = add_trial(job, name="a__early")
    late = add_trial(job, name="a__late")
    add_rollout(
        early,
        events=[token_count_event(timestamp="2026-08-15T06:00:00Z", used_percent=70.0)],
    )
    add_rollout(
        late,
        events=[token_count_event(timestamp="2026-08-16T06:00:00Z", used_percent=92.0)],
        day="2026/08/16",
        session="rollout-2026-08-16T06-00-00-def.jsonl",
    )

    assert load_quota_report([tmp_path], now=NOW).headroom.used_percent == 92.0


def test_staleness_is_measured_against_the_injected_instant(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    add_rollout(trial, events=[token_count_event(timestamp="2026-08-16T17:00:00Z")])

    headroom = load_quota_report([tmp_path], now=NOW).headroom

    assert headroom.staleness_seconds == 3_600.0


def test_headroom_is_unavailable_with_a_reason_when_no_snapshot_exists(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    add_rollout(
        trial,
        events=[token_count_event(timestamp="2026-08-15T06:30:02Z", rate_limits=False)],
    )

    headroom = load_quota_report([tmp_path], now=NOW).headroom

    assert headroom.availability == "unavailable"
    assert headroom.used_percent is None
    assert headroom.remaining_percent is None
    assert headroom.staleness_seconds is None
    assert headroom.reason and "unknown" in headroom.reason


def test_cursor_and_antigravity_consumption_keep_quota_unknown(tmp_path: Path) -> None:
    """Non-Codex usage is not mislabelled as a Codex-style rate-limit snapshot."""
    job = make_job(tmp_path)
    cursor_trial = add_trial(
        job,
        name="cursor__1",
        agent="cursor-cli",
        input_tokens=5_000,
        cache_tokens=4_000,
        output_tokens=150,
        cost_usd=0.02,
    )
    add_rollout(
        cursor_trial,
        events=[token_count_event(timestamp="2026-08-15T06:30:00Z", used_percent=45.0)],
    )
    agy_trial = add_trial(
        job,
        name="agy__1",
        agent="antigravity-cli",
        input_tokens=10_000,
        cache_tokens=8_000,
        output_tokens=300,
        cost_usd=0.04,
    )
    add_rollout(
        agy_trial,
        events=[token_count_event(timestamp="2026-08-15T06:35:00Z", used_percent=60.0)],
    )

    cursor_report = load_quota_report(
        [tmp_path], now=NOW, paid_agents=frozenset({"cursor-cli"})
    )
    assert len(cursor_report.consumed.trials) == 1
    assert cursor_report.consumed.trials[0].agent == "cursor-cli"
    assert cursor_report.consumed.totals().input_tokens == 5_000
    assert cursor_report.headroom.availability == "unavailable"
    assert cursor_report.headroom.remaining_percent is None

    agy_report = load_quota_report(
        [tmp_path], now=NOW, paid_agents=frozenset({"antigravity-cli"})
    )
    assert len(agy_report.consumed.trials) == 1
    assert agy_report.consumed.trials[0].agent == "antigravity-cli"
    assert agy_report.consumed.totals().input_tokens == 10_000
    assert agy_report.headroom.availability == "unavailable"
    assert agy_report.headroom.remaining_percent is None


def test_cursor_and_antigravity_unobserved_headroom_gives_honest_reason(tmp_path: Path) -> None:
    """When no snapshot exists for cursor-cli or antigravity-cli, reason is unobserved."""
    job = make_job(tmp_path)
    add_trial(job, name="cursor__nosnap", agent="cursor-cli")
    add_trial(job, name="agy__nosnap", agent="antigravity-cli")

    cursor_report = load_quota_report(
        [tmp_path], now=NOW, paid_agents=frozenset({"cursor-cli"})
    )
    assert cursor_report.headroom.availability == "unavailable"
    assert cursor_report.headroom.reason == NO_OBSERVATION_REASON
    assert cursor_report.headroom.remaining_percent is None

    agy_report = load_quota_report(
        [tmp_path], now=NOW, paid_agents=frozenset({"antigravity-cli"})
    )
    assert agy_report.headroom.availability == "unavailable"
    assert agy_report.headroom.reason == NO_OBSERVATION_REASON
    assert agy_report.headroom.remaining_percent is None

def test_headroom_is_account_scoped_and_never_attributed_to_the_lab(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    add_rollout(trial, events=[token_count_event(timestamp="2026-08-15T06:30:02Z")])

    report = load_quota_report([tmp_path], now=NOW)

    assert report.headroom.scope == "account"
    assert report.observations[0].scope == "account"
    assert report.headroom.lab_attributable == "unavailable"
    assert "cannot be decomposed" in report.headroom.lab_attributable_reason


def test_an_empty_report_reports_unavailable_headroom(tmp_path: Path) -> None:
    report = load_quota_report([tmp_path / "absent"], now=NOW)

    assert report.consumed.trials == ()
    assert report.headroom.availability == "unavailable"
    assert report.observations == ()


# --- the promoted quota sidecar: a fallback, never a second source -------


def test_a_promoted_trial_reads_its_quota_sidecar_when_no_rollout_survives(
    tmp_path: Path,
) -> None:
    """Promotion omits the rollout under R2, so the sidecar is the only reading left."""
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    add_quota_sidecar(trial, timestamps=["2026-08-15T06:30:02Z", "2026-08-15T06:31:02Z"])

    report = load_quota_report([tmp_path], now=NOW)
    headroom = report.headroom

    assert len(report.observations) == 2
    assert headroom.availability == "observed"
    assert headroom.used_percent == 70.0
    assert headroom.remaining_percent == 30.0
    assert headroom.observed_at == datetime(2026, 8, 15, 6, 31, 2, tzinfo=UTC)
    assert headroom.resets_at == datetime(2026, 8, 20, 18, 32, 49, tzinfo=UTC)
    assert headroom.plan_type == "prolite"
    assert headroom.hard_stop is True
    assert report.counter_resolution_percent() == 1.0


def test_a_rollout_and_a_sidecar_on_one_trial_count_the_rollout_once(tmp_path: Path) -> None:
    """The whole subtlety: reading both records would double-count the same readings.

    A live run has the rollout, a promoted bundle has the sidecar, and a tree
    holding both holds one history twice. The rollout wins and the sidecar is
    not read at all.
    """
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    add_rollout(
        trial,
        events=[
            token_count_event(timestamp="2026-08-15T06:30:02Z", used_percent=70.0),
            token_count_event(timestamp="2026-08-15T06:31:02Z", used_percent=71.0),
        ],
    )
    add_quota_sidecar(
        trial,
        timestamps=["2026-08-15T06:30:02Z", "2026-08-15T06:31:02Z"],
        used_percent=71.0,
    )

    report = load_quota_report([tmp_path], now=NOW)

    assert len(report.observations) == 2
    assert [observation.observed_at for observation in report.observations] == [
        datetime(2026, 8, 15, 6, 30, 2, tzinfo=UTC),
        datetime(2026, 8, 15, 6, 31, 2, tzinfo=UTC),
    ]
    assert all(observation.source.endswith(".jsonl") for observation in report.observations)
    assert not any("agent/quota" in observation.source for observation in report.observations)
    assert "rate-limits.json" not in (report.headroom.source or "")


def test_a_sidecar_reading_ages_exactly_as_its_rollout_twin(tmp_path: Path) -> None:
    """A committed number must not read as a fresh one.

    The sidecar keeps the instant the *trial* recorded, so the same reading is
    reported with the same staleness whichever record survived -- and ``source``
    names which kind of record answered.
    """
    live_root = tmp_path / "live"
    promoted_root = tmp_path / "promoted"
    add_rollout(
        add_trial(make_job(live_root), name="a__1"),
        events=[token_count_event(timestamp="2026-08-15T06:30:02Z")],
    )
    add_quota_sidecar(
        add_trial(make_job(promoted_root), name="a__1"),
        timestamps=["2026-08-15T06:30:02Z"],
    )

    live = load_quota_report([live_root], now=NOW).headroom
    promoted = load_quota_report([promoted_root], now=NOW).headroom

    assert promoted.observed_at == live.observed_at
    assert promoted.staleness_seconds == live.staleness_seconds == 127_798.0
    assert promoted.staleness_seconds > 86_400.0  # a day-old committed reading, not fresh
    assert live.source is not None and live.source.endswith(".jsonl")
    assert promoted.source is not None and promoted.source.endswith(".rate-limits.json")
    assert promoted.source.startswith("a__1/agent/quota/")


def test_a_sidecar_reading_stays_account_scoped_and_unattributable_to_the_lab(
    tmp_path: Path,
) -> None:
    """A sidecar makes a *remaining* reading portable; it says nothing about the lab."""
    job = make_job(tmp_path)
    add_quota_sidecar(add_trial(job, name="a__1"), timestamps=["2026-08-15T06:30:02Z"])

    report = load_quota_report([tmp_path], now=NOW)

    assert report.headroom.scope == "account"
    assert report.observations[0].scope == "account"
    assert report.headroom.lab_attributable == "unavailable"


def test_a_sidecar_only_trial_reports_no_model_turns_rather_than_zero(tmp_path: Path) -> None:
    """Turns come from the rollout only; the sidecar carries no turn evidence."""
    job = make_job(tmp_path)
    add_quota_sidecar(add_trial(job, name="a__1"), timestamps=["2026-08-15T06:30:02Z"])

    report = load_quota_report([tmp_path], now=NOW)

    assert report.observations  # the quota reading survived
    assert report.consumed.trials[0].model_turns is None  # the turn count did not


def test_a_document_not_declaring_the_sidecar_kind_is_ignored(tmp_path: Path) -> None:
    """``agent/quota/`` is read by declared kind, never by filename pattern."""
    job = make_job(tmp_path)
    add_quota_sidecar(
        add_trial(job, name="a__1"),
        timestamps=["2026-08-15T06:30:02Z"],
        kind="some-other-document",
    )

    report = load_quota_report([tmp_path], now=NOW)

    assert report.observations == ()
    assert report.headroom.availability == "unavailable"


def test_malformed_sidecars_are_skipped_without_failing(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    quota_dir = trial / "agent/quota"
    quota_dir.mkdir(parents=True, exist_ok=True)
    (quota_dir / "rollout-broken.rate-limits.json").write_text("not json at all")
    write_json(
        quota_dir / "rollout-noentries.rate-limits.json",
        {"kind": "evallab-rate-limits-sidecar", "snapshots": "not a list"},
    )
    write_json(
        quota_dir / "rollout-badinstant.rate-limits.json",
        {
            "kind": "evallab-rate-limits-sidecar",
            "snapshots": [{"timestamp": "nonsense", "rate_limits": {"limit_id": "codex"}}],
        },
    )
    add_quota_sidecar(trial, timestamps=["2026-08-15T06:30:02Z"], stem="rollout-good")

    report = load_quota_report([tmp_path], now=NOW)

    assert len(report.observations) == 1
    assert report.headroom.used_percent == 70.0


def test_committed_evidence_alone_yields_an_observed_headroom() -> None:
    """The acceptance case: a fresh clone has no ``runs/`` and must still report.

    Measured against the committed bundles rather than a fixture. The floor is a
    floor, not the exact count, because a promoted bundle is immutable while more
    bundles may be admitted later -- so this cannot break when one is.
    """
    report = load_quota_report([PROMOTED_RUNS], now=NOW)

    assert len(report.observations) >= 67
    assert report.headroom.availability == "observed"
    assert report.headroom.used_percent is not None
    assert report.headroom.staleness_seconds is not None
    assert report.counter_resolution_percent() == 1.0
    assert all(
        observation.source.endswith(".rate-limits.json") for observation in report.observations
    )


# --- robustness and boundaries ------------------------------------------


def test_malformed_rollout_lines_are_skipped_without_failing(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    rollout = trial / "agent/sessions/2026/08/15/rollout-bad.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(
        "not json at all rate_limits\n"
        '{"timestamp": "nonsense", "payload": {"type": "token_count", "rate_limits": {}}}\n'
        + json.dumps(token_count_event(timestamp="2026-08-15T06:30:02Z"))
        + "\n"
    )

    report = load_quota_report([tmp_path], now=NOW)

    assert len(report.observations) == 1
    assert report.headroom.used_percent == 70.0


QUOTA_SOURCE = Path(__file__).resolve().parents[1] / "src/evallab/quota.py"


def _quota_module() -> ast.Module:
    return ast.parse(QUOTA_SOURCE.read_text())


def _imported_modules(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.add(node.module.split(".")[0])
    return names


def _code_string_literals(tree: ast.Module) -> list[str]:
    """String constants excluding docstrings, so prose cannot fail the check."""
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node not in docstrings
    ]


def test_the_module_imports_no_network_process_or_environment_access() -> None:
    """Quota accounting must be a pure read of injected job directories."""
    imported = _imported_modules(_quota_module())

    assert imported.isdisjoint(
        {"urllib", "http", "socket", "requests", "httpx", "subprocess", "os", "shutil"}
    ), f"quota.py imported a side-effecting module: {sorted(imported)}"


def test_the_module_names_no_credential_store_in_executable_code() -> None:
    literals = " ".join(_code_string_literals(_quota_module()))

    for forbidden in (
        "auth.json",
        "find-generic-password",
        "ANTHROPIC",
        "OPENAI",
        ".codex",
        "Keychain",
    ):
        assert forbidden not in literals, f"quota.py code must not name {forbidden}"


def test_the_module_does_not_import_the_policy_gate() -> None:
    """Measurement stays independent of authorisation, which another mission owns."""
    imported = _imported_modules(_quota_module())

    assert "evallab.queue" not in imported
    assert "evallab.cli" not in imported
    assert imported & {"evallab.results"} == {"evallab.results"}


def test_rollout_message_text_is_never_carried_into_the_report(tmp_path: Path) -> None:
    """Rollouts hold unredacted prompts; the report must stay safe to commit."""
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    secret = "PROMPT-TEXT-THAT-MUST-NOT-LEAK"
    add_rollout(
        trial,
        events=[
            {
                "timestamp": "2026-08-15T06:30:01Z",
                "payload": {"type": "user_message", "message": secret},
            },
            token_count_event(timestamp="2026-08-15T06:30:02Z"),
        ],
    )

    report = load_quota_report([tmp_path], now=NOW)

    assert secret not in report.model_dump_json()
    assert secret not in render_report(report)


# --- surfaces ------------------------------------------------------------


def test_label_matches_the_operator_provenance_convention() -> None:
    assert label("observed") == "[observed]"
    assert label("unavailable") == "[unavailable]"


def test_render_labels_every_section_and_separates_consumed_from_remaining(
    tmp_path: Path,
) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    add_rollout(trial, events=[token_count_event(timestamp="2026-08-15T06:30:02Z")])

    rendered = render_report(load_quota_report([tmp_path], now=NOW))

    assert "CONSUMED by the lab (scope: this lab only)" in rendered
    assert "REMAINING on the subscription (scope: account, NOT the lab)" in rendered
    assert "CONSUMED by job" in rendered
    assert "CONSUMED by day" in rendered
    assert "CONSUMED by task" in rendered
    assert "CONSUMED by agent" in rendered
    assert "[observed]" in rendered
    assert "lab's share of that percentage" in rendered


def test_render_marks_unavailable_headroom_rather_than_printing_a_number(
    tmp_path: Path,
) -> None:
    job = make_job(tmp_path)
    add_trial(job, name="a__1")

    rendered = render_report(load_quota_report([tmp_path], now=NOW))

    assert "remaining allowance                  [unavailable]" in rendered
    assert "used_percent" not in rendered


def test_module_entry_point_prints_a_report(tmp_path: Path, capsys) -> None:
    job = make_job(tmp_path)
    add_trial(job, name="a__1")

    assert main([str(tmp_path)]) == 0

    assert "CONSUMED by the lab" in capsys.readouterr().out


def test_module_entry_point_emits_parsable_json(tmp_path: Path, capsys) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    add_rollout(trial, events=[token_count_event(timestamp="2026-08-15T06:30:02Z")])

    assert main([str(tmp_path), "--json"]) == 0

    payload = QuotaReport.model_validate_json(capsys.readouterr().out)
    assert payload.headroom.used_percent == 70.0
    assert len(payload.consumed.trials) == 1


def test_default_roots_name_both_committed_and_working_evidence() -> None:
    roots = default_roots(Path("/repo"))

    assert roots == (Path("/repo/runs"), Path("/repo/research/evidence/runs"))


# --- tokens, cache, and wall clock --------------------------------------


def test_input_tokens_are_inclusive_of_cache_so_uncached_is_derived(tmp_path: Path) -> None:
    """Harbor's n_input_tokens includes cached input; the fresh work is the remainder."""
    job = make_job(tmp_path)
    add_trial(job, name="a__1", input_tokens=86_542, cache_tokens=79_360, output_tokens=814)

    report = load_quota_report([tmp_path], now=NOW)
    trial = report.consumed.trials[0]
    totals = report.consumed.totals()

    assert trial.uncached_input_tokens == 7_182
    assert trial.cached_input_ratio == 79_360 / 86_542
    assert totals.uncached_input_tokens == 7_182
    assert totals.cached_input_ratio == 79_360 / 86_542


def test_uncached_and_cached_are_never_collapsed_into_one_number(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    add_trial(job, name="a__1", input_tokens=1_000, cache_tokens=900, output_tokens=10)

    rendered = render_report(load_quota_report([tmp_path], now=NOW))

    assert "uncached input tokens                100" in rendered
    assert "cached input tokens                  900" in rendered
    assert "UNVERIFIED" in rendered


def test_cached_weighting_stays_unverified_rather_than_assumed() -> None:
    assert CACHED_WEIGHTING_NOTE.startswith("UNVERIFIED")
    assert QuotaReport(generated_at=NOW).cached_weighting_note == CACHED_WEIGHTING_NOTE


def test_cached_ratio_is_unavailable_without_observed_input(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    add_trial(job, name="a__1", input_tokens=None, cache_tokens=None, output_tokens=None)

    report = load_quota_report([tmp_path], now=NOW)

    assert report.consumed.trials[0].uncached_input_tokens is None
    assert report.consumed.trials[0].cached_input_ratio is None
    assert report.consumed.totals().cached_input_ratio is None


def test_wall_clock_comes_from_the_trial_and_job_phase_timestamps(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    payload = json.loads((trial / "result.json").read_text())
    payload["finished_at"] = "2026-08-15T09:00:00Z"
    payload["agent_setup"] = {
        "started_at": "2026-08-15T06:31:00Z",
        "finished_at": "2026-08-15T07:31:00Z",
    }
    payload["agent_execution"] = {
        "started_at": "2026-08-15T07:31:00Z",
        "finished_at": "2026-08-15T08:59:00Z",
    }
    write_json(trial / "result.json", payload)

    record = load_quota_report([tmp_path], now=NOW).consumed.trials[0]

    assert record.duration_seconds == 9_000.0
    assert record.agent_setup_seconds == 3_600.0
    assert record.agent_execution_seconds == 5_280.0
    assert record.job_duration_seconds == 3_600.0


def test_job_wall_clock_is_counted_once_per_job_not_once_per_trial(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    for index in range(3):
        add_trial(job, name=f"a__{index}")

    totals = load_quota_report([tmp_path], now=NOW).consumed.totals()

    assert totals.job_wall_clock_seconds == 3_600.0


def test_the_longest_trial_is_surfaced_so_a_stall_is_visible(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    quick = add_trial(job, name="a__quick")
    slow = add_trial(job, name="a__slow")
    for trial, finished in ((quick, "2026-08-15T06:32:00Z"), (slow, "2026-08-15T09:00:00Z")):
        payload = json.loads((trial / "result.json").read_text())
        payload["finished_at"] = finished
        write_json(trial / "result.json", payload)

    totals = load_quota_report([tmp_path], now=NOW).consumed.totals()

    assert totals.longest_trial_seconds == 9_000.0
    assert totals.trial_wall_clock_seconds == 9_120.0


def test_by_job_grouping_isolates_a_single_slow_job(tmp_path: Path) -> None:
    fast = make_job(tmp_path, name="job-fast")
    slow = make_job(tmp_path, name="job-slow")
    payload = json.loads((slow / "result.json").read_text())
    payload["finished_at"] = "2026-08-15T11:44:00Z"
    write_json(slow / "result.json", payload)
    add_trial(fast, name="fast__1")
    add_trial(slow, name="slow__1")

    grouping = load_quota_report([tmp_path], now=NOW).consumed.by_job()

    assert list(grouping) == ["job-fast", "job-slow"]
    assert grouping["job-slow"].job_wall_clock_seconds == 20_640.0


# --- hard stop and counter resolution ------------------------------------


def test_no_overflow_credits_is_reported_as_a_hard_stop(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    add_rollout(trial, events=[token_count_event(timestamp="2026-08-15T06:30:02Z")])

    headroom = load_quota_report([tmp_path], now=NOW).headroom

    assert headroom.hard_stop is True
    assert "blocks every paid agent" in headroom.hard_stop_note
    assert headroom.credits_balance == "0"


def test_available_credits_are_not_a_hard_stop(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    event = token_count_event(timestamp="2026-08-15T06:30:02Z")
    limits = event["payload"]["rate_limits"]  # type: ignore[index]
    limits["credits"] = {"has_credits": True, "unlimited": False, "balance": "500"}
    add_rollout(trial, events=[event])

    headroom = load_quota_report([tmp_path], now=NOW).headroom

    assert headroom.hard_stop is False
    assert "not necessarily a lockout" in headroom.hard_stop_note


def test_a_missing_credits_block_leaves_the_hard_stop_unknown(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    event = token_count_event(timestamp="2026-08-15T06:30:02Z")
    del event["payload"]["rate_limits"]["credits"]  # type: ignore[index]
    add_rollout(trial, events=[event])

    headroom = load_quota_report([tmp_path], now=NOW).headroom

    assert headroom.hard_stop is None
    assert headroom.hard_stop_note == f"overflow credits {label('unavailable')}"
    assert headroom.used_percent == 70.0


def test_a_whole_percent_counter_reports_a_one_point_resolution(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    add_rollout(
        trial,
        events=[
            token_count_event(timestamp="2026-08-15T06:30:02Z", used_percent=70.0),
            token_count_event(timestamp="2026-08-15T06:31:02Z", used_percent=71.0),
        ],
    )

    report = load_quota_report([tmp_path], now=NOW)

    assert report.counter_resolution_percent() == 1.0
    assert "counter resolution                   1.0 percentage point" in render_report(report)


def test_a_finer_counter_is_not_claimed_to_be_whole_points(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    add_rollout(
        trial,
        events=[token_count_event(timestamp="2026-08-15T06:30:02Z", used_percent=70.5)],
    )

    assert load_quota_report([tmp_path], now=NOW).counter_resolution_percent() is None


def test_resolution_is_unavailable_with_no_observations(tmp_path: Path) -> None:
    assert load_quota_report([tmp_path], now=NOW).counter_resolution_percent() is None


# --- host-session boundary ----------------------------------------------


def test_no_rollout_payload_other_than_rate_limits_can_reach_the_report(tmp_path: Path) -> None:
    """Rollouts carry personal prompt text wherever they live; only quota fields leave.

    The payload shapes below are the ones a real session rollout contains,
    including the interactive shapes found outside this repository.
    """
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    markers = {
        "user_message": "MARKER-USER-PROMPT",
        "message": "MARKER-ASSISTANT-TEXT",
        "session_meta": "MARKER-SESSION-TITLE",
        "turn_context": "MARKER-CWD-PATH",
        "world_state": "MARKER-WORLD-STATE",
        "task_started": "MARKER-TASK-TEXT",
    }
    events: list[dict[str, object]] = [
        {"timestamp": "2026-08-15T06:30:00Z", "payload": {"type": kind, "message": marker}}
        for kind, marker in markers.items()
    ]
    events.append(token_count_event(timestamp="2026-08-15T06:30:02Z"))

    add_rollout(trial, events=events)
    report = load_quota_report([tmp_path], now=NOW)
    serialised = report.model_dump_json() + render_report(report)

    assert report.headroom.used_percent == 70.0
    for marker in markers.values():
        assert marker not in serialised, f"{marker} escaped into the report"


def test_an_observation_source_is_trial_relative_so_no_host_path_can_appear(
    tmp_path: Path,
) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    add_rollout(trial, events=[token_count_event(timestamp="2026-08-15T06:30:02Z")])

    source = load_quota_report([tmp_path], now=NOW).observations[0].source

    assert not Path(source).is_absolute()
    assert source.startswith("a__1/agent/sessions/")
    assert str(tmp_path) not in source


def test_an_observation_carries_only_quota_fields(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    trial = add_trial(job, name="a__1")
    add_rollout(trial, events=[token_count_event(timestamp="2026-08-15T06:30:02Z")])

    observation = load_quota_report([tmp_path], now=NOW).observations[0]

    assert set(observation.model_dump()) == {
        "job_name",
        "trial_name",
        "agent",
        "observed_at",
        "limit_id",
        "limit_name",
        "plan_type",
        "primary",
        "secondary",
        "has_credits",
        "credits_unlimited",
        "credits_balance",
        "rate_limit_reached_type",
        "source",
        "scope",
    }
