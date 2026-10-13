"""Claude quota probes and gates use sanitized fixtures and injected process I/O.

The recorded shape comes from ``omp usage``; fixture timestamps are fixed and
account identifiers are omitted. No test invokes OMP, a model, or host auth.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from evallab import queue as queue_module
from evallab import quota as quota_module
from evallab import results_home
from evallab import runner as runner_module
from evallab.execution_contracts import (
    HarborProcessResult,
    PaidRunAuthorization,
    RunRequest,
    TransientHarnessFailure,
)
from evallab.queue import (
    DirectoryQueue,
    Executor,
    PolicyGate,
    load_policy,
    provider_reported_exhaustion,
    render_headroom_notice,
)
from evallab.quota import (
    Headroom,
    claude_dispatch_quota_record,
    load_quota_report,
    parse_claude_headroom,
    persist_claude_dispatch_quota,
    read_claude_headroom,
)
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/claude-quota/usage-headroom.json"
NOW = datetime(2026, 10, 13, 4, 0, 0, tzinfo=UTC)
FETCHED_AT = NOW - timedelta(seconds=30)
SHORT_RESET = NOW + timedelta(hours=1)
WEEKLY_RESET = NOW + timedelta(days=2)
COMMAND = ["omp", "usage", "-p", "anthropic", "--json", "-r"]


def payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def reading(*, weekly: float = 29, short: float = 98) -> Headroom:
    captured = payload()
    limits = captured["reports"][0]["limits"]
    limits[0]["amount"]["used"] = short
    limits[0]["amount"]["usedFraction"] = short / 100
    limits[1]["amount"]["used"] = weekly
    limits[1]["amount"]["usedFraction"] = weekly / 100
    return parse_claude_headroom(captured, now=NOW)


def assert_unavailable(result: Headroom) -> None:
    assert result.availability == "unavailable"
    assert result.used_percent is None
    assert result.remaining_percent is None
    assert result.window_minutes is None
    assert result.resets_at is None
    assert result.staleness_seconds is None
    assert result.reason
    notice = render_headroom_notice(result, agent="claude-code")
    assert "[unavailable]" in notice
    assert "UNKNOWN" in notice


def policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
        escalate_to_human=["any_billable_agent"],
        refuse_billable_at_used_percent=60,
    )


def specimen(name: str = "claude-quota-probe") -> ExperimentSpec:
    return ExperimentSpec(
        spec_id=name,
        submitted_at=NOW - timedelta(minutes=1),
        name=name,
        hypothesis="exercise the Claude subscription gate without dispatch",
        purpose="practice",
        task="canary/event-summary",
        task_path="library/tasks/event-summary",
        agent="claude-code",
        model="claude-gateway/claude-haiku-5-5",
        submitted_by="test-agent",
        est_cost_usd=1,
    )


def authorize(spec: ExperimentSpec, *, override: bool = False) -> PaidRunAuthorization:
    return PaidRunAuthorization(
        spec_id=str(spec.spec_id),
        actor="test-operator",
        authorized_at=NOW - timedelta(seconds=10),
        quota_override=override,
    )


def decide(result: Headroom, *, override: bool = False):
    spec = specimen()
    gate = PolicyGate(policy(), headroom=lambda: result)
    return gate.decide(
        spec, spent_today_usd=0, authorization=authorize(spec, override=override), now=NOW
    )


def test_recorded_claude_windows_preserve_reading_and_reset_times() -> None:
    result = parse_claude_headroom(payload(), now=NOW)
    assert result.availability == "observed"
    assert result.fetched_at == FETCHED_AT
    assert result.observed_at == FETCHED_AT
    assert result.staleness_seconds == 30
    assert result.used_percent == 29
    assert result.remaining_percent == 71
    assert result.window_minutes == 10080
    assert result.resets_at == WEEKLY_RESET
    assert set(result.windows) == {"5h", "7d"}
    assert result.windows["5h"].used_percent == 98
    assert result.windows["5h"].window_minutes == 300
    assert result.windows["5h"].resets_at == SHORT_RESET
    assert result.windows["5h"].status == "warning"
    assert result.windows["7d"].status == "ok"
    notice = render_headroom_notice(result, agent="claude-code")
    assert "[observed]" in notice
    assert SHORT_RESET.isoformat() in notice
    assert WEEKLY_RESET.isoformat() in notice


def test_fraction_only_amounts_are_percentages() -> None:
    captured = payload()
    for limit in captured["reports"][0]["limits"]:
        limit["amount"].pop("used")
    result = parse_claude_headroom(captured, now=NOW)
    assert result.availability == "observed"
    assert result.used_percent == pytest.approx(29)
    assert result.windows["5h"].used_percent == pytest.approx(98)


def test_only_anthropic_report_and_general_windows_describe_claude_headroom() -> None:
    captured = payload()
    unrelated = json.loads(FIXTURE.read_text())["reports"][0]
    unrelated["provider"] = "openai-codex"
    for limit in unrelated["limits"]:
        limit["amount"]["used"] = 100
    captured["reports"].insert(0, unrelated)
    result = parse_claude_headroom(captured, now=NOW)
    assert result.availability == "observed"
    assert result.used_percent == 29
    assert result.windows["5h"].used_percent == 98
    assert decide(result).admitted


def test_persistable_reading_whitelists_status_and_drops_account_identifiers() -> None:
    captured = payload()
    identifier = "synthetic-account-identifier-do-not-retain"
    email = "fixture-account@example.invalid"
    report = captured["reports"][0]
    captured["accountId"] = identifier
    report["accountId"] = identifier
    report["email"] = email
    report["limits"][0]["label"] = email
    report["limits"][0]["status"] = identifier
    report["limits"][1]["account"] = {"id": identifier, "email": email}
    result = parse_claude_headroom(captured, now=NOW)
    serialized = result.model_dump_json()
    assert identifier not in serialized
    assert email not in serialized
    assert "accountId" not in serialized
    assert "7d:fable" not in serialized
    assert result.windows.get("5h") is None or result.windows["5h"].status is None


@pytest.mark.parametrize("captured", [None, [], {}, {"reports": []}, {"reports": "invalid"}])
def test_missing_reports_are_unavailable(captured: object) -> None:
    assert_unavailable(parse_claude_headroom(captured, now=NOW))


@pytest.mark.parametrize("window_index", [0, 1])
def test_missing_either_required_window_is_unavailable(window_index: int) -> None:
    captured = payload()
    captured["reports"][0]["limits"].pop(window_index)
    assert_unavailable(parse_claude_headroom(captured, now=NOW))


@pytest.mark.parametrize("invalid", [True, -1, float("nan"), float("inf"), "29"])
def test_invalid_percentages_are_unavailable(invalid: object) -> None:
    captured = payload()
    captured["reports"][0]["limits"][1]["amount"] = {"used": invalid, "unit": "percent"}
    assert_unavailable(parse_claude_headroom(captured, now=NOW))


def test_invalid_explicit_percent_does_not_fall_back_to_a_valid_fraction() -> None:
    captured = payload()
    captured["reports"][0]["limits"][1]["amount"]["used"] = "29"
    assert_unavailable(parse_claude_headroom(captured, now=NOW))


@pytest.mark.parametrize("status", [{"state": "ok"}, ["ok"]])
def test_unhashable_provider_status_is_unavailable(status: object) -> None:
    captured = payload()
    captured["reports"][0]["limits"][1]["status"] = status
    assert_unavailable(parse_claude_headroom(captured, now=NOW))


@pytest.mark.parametrize("invalid", [None, True, -1, float("inf"), "yesterday"])
def test_invalid_fetched_timestamp_is_unavailable(invalid: object) -> None:
    captured = payload()
    captured["reports"][0]["fetchedAt"] = invalid
    assert_unavailable(parse_claude_headroom(captured, now=NOW))


def test_reader_uses_redacted_command_and_bounded_process_timeout() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, FIXTURE.read_text(), "")

    result = read_claude_headroom(now=NOW, timeout_seconds=3.5, run=run)
    assert result.availability == "observed"
    assert calls == [
        (
            COMMAND,
            {"capture_output": True, "text": True, "check": False, "timeout": 3.5},
        )
    ]


@pytest.mark.parametrize(
    "failure", ["missing-command", "timeout", "nonzero", "malformed", "decode-error", "process-error"]
)
def test_reader_failure_is_unavailable_without_retaining_process_output(failure: str) -> None:
    private_output = "synthetic-private-account-text-do-not-retain"

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if failure == "missing-command":
            raise FileNotFoundError(private_output)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 3, output=private_output, stderr=private_output)
        if failure == "nonzero":
            return subprocess.CompletedProcess(command, 1, private_output, private_output)
        if failure == "decode-error":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, private_output)
        if failure == "process-error":
            raise subprocess.CalledProcessError(1, command, private_output, private_output)
        return subprocess.CompletedProcess(command, 0, "not json " + private_output, private_output)

    result = read_claude_headroom(now=NOW, timeout_seconds=3, run=run)
    assert_unavailable(result)
    assert private_output not in result.model_dump_json()


def test_committed_subscription_weekly_ceiling_is_sixty_percent() -> None:
    committed = load_policy(ROOT / "policy/standing-approvals.yaml")
    assert committed.refuse_billable_at_used_percent == 60


@pytest.mark.parametrize("weekly", [60, 60.01, 85, 99.9])
def test_weekly_ceiling_refuses_claude_subscription_spec(weekly: float) -> None:
    decision = decide(reading(weekly=weekly, short=20))
    assert not decision.admitted
    assert decision.reason_code == "subscription_quota_ceiling"
    assert WEEKLY_RESET.isoformat() in decision.message


@pytest.mark.parametrize("short", [100, 100.1])
def test_exhausted_five_hour_window_refuses_below_weekly_ceiling(short: float) -> None:
    decision = decide(reading(weekly=29, short=short))
    assert not decision.admitted
    assert decision.reason_code == "subscription_quota_exhausted"
    assert SHORT_RESET.isoformat() in decision.message


@pytest.mark.parametrize("weekly,short", [(0, 0), (29, 98), (59.99, 99.99)])
def test_below_weekly_ceiling_passes_even_when_five_hour_use_exceeds_sixty(
    weekly: float, short: float
) -> None:
    decision = decide(reading(weekly=weekly, short=short))
    assert decision.admitted
    assert decision.reason_code is None
    assert "[observed]" in decision.message


@pytest.mark.parametrize("override", [False, True])
def test_unavailable_claude_headroom_refuses_even_with_a_quota_override(override: bool) -> None:
    decision = decide(Headroom(availability="unavailable", reason="probe unavailable"), override=override)
    assert not decision.admitted
    assert decision.reason_code == "subscription_quota_unavailable"
    assert "UNKNOWN" in decision.message


@pytest.mark.parametrize("window_index", [0, 1])
def test_expired_claude_window_requires_a_fresh_reading(window_index: int) -> None:
    captured = payload()
    captured["reports"][0]["limits"][window_index]["window"]["resetsAt"] = int(
        NOW.timestamp() * 1000
    )
    result = parse_claude_headroom(captured, now=NOW)
    decision = decide(result)
    assert not decision.admitted
    assert decision.reason_code == "subscription_quota_unavailable"


def test_expired_weekly_window_does_not_hide_current_five_hour_exhaustion() -> None:
    captured = payload()
    captured["reports"][0]["limits"][1]["window"]["resetsAt"] = int(NOW.timestamp() * 1000)
    captured["reports"][0]["limits"][0]["amount"]["used"] = 100
    result = parse_claude_headroom(captured, now=NOW)
    assert provider_reported_exhaustion(result) is not None


def executor(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    readings: list[Headroom],
    run: Callable[[RunRequest], Path],
    *,
    retries: int = 0,
) -> tuple[Executor, list[datetime]]:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == UTC
            return NOW

    monkeypatch.setattr(queue_module, "datetime", FixedDateTime)
    sequence = count(1)
    monkeypatch.setattr(queue_module, "new_ulid", lambda: f"{next(sequence):026d}")
    calls: list[datetime] = []
    snapshots = iter(readings)

    def live_read(*, now: datetime) -> Headroom:
        calls.append(now)
        assert now == NOW
        return next(snapshots)

    monkeypatch.setattr(queue_module, "read_claude_headroom", live_read)
    service = Executor(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=policy(),
        runner=run,
        ingester=lambda _path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: frozenset({"claude_gateway"}),
        sleeper=lambda _seconds: None,
        max_transient_retries=retries,
        watch_enabled=False,
    )
    return service, calls


def request(root: Path, spec: ExperimentSpec, **kwargs: Any) -> RunRequest:
    return RunRequest(
        task=root / "task",
        agent=spec.agent,
        model=spec.model,
        name=spec.name,
        jobs_dir=root / "runs",
        allow_billable=True,
        **kwargs,
    )


def write_fake_trials(run_request: RunRequest) -> Path:
    job_dir = run_request.jobs_dir / run_request.name
    for name in ("trial-a", "trial-b"):
        trial = job_dir / name
        trial.mkdir(parents=True)
        (trial / "config.json").write_text("{}\n")
    (job_dir / "scratch").mkdir()
    return job_dir


def test_dispatch_refreshes_a_cached_admission_reading_before_any_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches: list[RunRequest] = []

    def run(run_request: RunRequest) -> Path:
        launches.append(run_request)
        return write_fake_trials(run_request)

    service, calls = executor(tmp_path, monkeypatch, [reading(weekly=29), reading(weekly=61)], run)
    spec = specimen()
    assert service.gate.headroom(spec.agent).used_percent == 29
    decision = service._dispatch_decision(spec, authorize(spec))
    assert not decision.admitted
    assert decision.reason_code == "subscription_quota_ceiling"
    assert calls == [NOW, NOW]
    assert launches == []


def test_consecutive_dispatches_stop_when_the_short_window_becomes_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches: list[RunRequest] = []

    def run(run_request: RunRequest) -> Path:
        launches.append(run_request)
        return write_fake_trials(run_request)

    service, calls = executor(tmp_path, monkeypatch, [reading(short=98), reading(short=100)], run)
    first = specimen("claude-quota-first")
    second = specimen("claude-quota-second")
    assert service._dispatch_decision(first, authorize(first)).admitted
    service._run_with_transient_retries_watched(first, request(tmp_path, first))
    decision = service._dispatch_decision(second, authorize(second))
    assert not decision.admitted
    assert decision.reason_code == "subscription_quota_exhausted"
    assert [launch.name for launch in launches] == [first.name]
    assert calls == [NOW, NOW]
    assert not (tmp_path / "runs" / second.name).exists()


def test_each_spec_persists_its_own_dispatch_reading_on_every_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots = [reading(weekly=29, short=98), reading(weekly=45, short=99)]
    launches: list[RunRequest] = []

    def run(run_request: RunRequest) -> Path:
        launches.append(run_request)
        return write_fake_trials(run_request)

    service, calls = executor(tmp_path, monkeypatch, snapshots, run)
    first = specimen("claude-quota-first")
    second = specimen("claude-quota-second")
    assert service._dispatch_decision(first, authorize(first)).admitted
    assert service._dispatch_decision(second, authorize(second)).admitted
    assert service.gate.headroom(first.agent).used_percent == 45
    for spec, expected in zip((first, second), snapshots, strict=True):
        job_dir = service._run_with_transient_retries_watched(spec, request(tmp_path, spec))
        record = claude_dispatch_quota_record(expected)
        assert launches[-1].subscription_quota == record
        assert json.loads((job_dir / "claude-dispatch-quota.json").read_text()) == record
        for trial in ("trial-a", "trial-b"):
            sidecar = job_dir / trial / "agent/quota/claude-dispatch.json"
            assert json.loads(sidecar.read_text()) == record
        assert not (job_dir / "scratch/agent/quota/claude-dispatch.json").exists()
    assert calls == [NOW, NOW]


def test_transient_retry_rechecks_live_quota_and_stops_before_a_second_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches: list[RunRequest] = []

    def run(run_request: RunRequest) -> Path:
        launches.append(run_request)
        write_fake_trials(run_request)
        raise TransientHarnessFailure("transient_harness:stub")

    first_snapshot = reading(weekly=29)
    service, calls = executor(
        tmp_path, monkeypatch, [first_snapshot, reading(weekly=61)], run, retries=1
    )
    spec = specimen()
    authorization = authorize(spec)
    monkeypatch.setattr(service.queue, "authorization_for", lambda _spec: authorization)
    assert service._dispatch_decision(spec, authorization).admitted
    with pytest.raises(TransientHarnessFailure, match="transient_harness:stub"):
        service._run_with_transient_retries_watched(spec, request(tmp_path, spec))
    assert len(launches) == 1
    assert calls == [NOW, NOW]
    events = [json.loads(line) for line in service.queue.events_path.read_text().splitlines()]
    assert any(event["reason_code"] == "transient_retry:subscription_quota_ceiling" for event in events)
    sidecar = tmp_path / "runs" / spec.name / "trial-a/agent/quota/claude-dispatch.json"
    assert json.loads(sidecar.read_text()) == claude_dispatch_quota_record(first_snapshot)


def test_runner_metadata_retains_the_exact_sanitized_dispatch_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = specimen()
    record = claude_dispatch_quota_record(reading())
    run_request = request(tmp_path, spec, subscription_quota=record)
    job_dir = write_fake_trials(run_request)
    monkeypatch.setattr(runner_module, "tool_version", lambda _tool: "fixture-version")
    monkeypatch.setattr(runner_module.platform, "platform", lambda: "fixture-platform")
    monkeypatch.setattr(runner_module, "collected_secret_values", lambda: ())
    monkeypatch.setattr(runner_module, "_capture_binding_for_metadata", lambda _request: None)
    monkeypatch.setattr(results_home, "capture_repository", lambda _root: {})
    monkeypatch.setattr(results_home, "write_run_provenance", lambda _job, _repository: None)
    runner_module._write_run_metadata(
        run_request,
        repo_root=tmp_path,
        command=["harbor", "run", "--agent", "claude-code"],
        started=NOW,
        finished=NOW + timedelta(seconds=1),
        process=HarborProcessResult(
            returncode=0,
            timed_out=False,
            log_path=run_request.jobs_dir / ".executor/fixture.log",
        ),
    )
    metadata = json.loads((job_dir / "lab-metadata.json").read_text())
    assert metadata["subscription_quota"] == record


def test_persistence_preserves_a_reused_trials_original_dispatch_sidecar(tmp_path: Path) -> None:
    spec = specimen()
    job_dir = write_fake_trials(request(tmp_path, spec))
    original_record = claude_dispatch_quota_record(reading(weekly=29))
    current_record = claude_dispatch_quota_record(reading(weekly=45))
    reused_sidecar = job_dir / "trial-a/agent/quota/claude-dispatch.json"
    reused_sidecar.parent.mkdir(parents=True)
    original_bytes = (json.dumps(original_record, separators=(",", ":")) + "\n").encode()
    reused_sidecar.write_bytes(original_bytes)

    persist_claude_dispatch_quota(job_dir, current_record)

    assert reused_sidecar.read_bytes() == original_bytes
    assert json.loads((job_dir / "trial-b/agent/quota/claude-dispatch.json").read_text()) == current_record
    assert json.loads((job_dir / "claude-dispatch-quota.json").read_text()) == current_record


def disk_job(root: Path) -> tuple[Path, Path]:
    job = root / "claude-disk-job"
    trial = job / "trial-a"
    trial.mkdir(parents=True)
    (job / "result.json").write_text(
        json.dumps(
            {
                "started_at": NOW.isoformat(),
                "finished_at": (NOW + timedelta(seconds=1)).isoformat(),
                "n_total_trials": 1,
                "stats": {"n_completed_trials": 1},
            }
        )
    )
    (trial / "config.json").write_text("{}\n")
    (trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": trial.name,
                "started_at": NOW.isoformat(),
                "agent_info": {"name": "claude-code"},
                "agent_result": {
                    "n_input_tokens": 10,
                    "n_cache_tokens": 0,
                    "n_output_tokens": 2,
                },
            }
        )
    )
    return job, trial


def test_disk_report_recovers_dispatch_windows_without_probing_or_counting_transcript_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_probe(*_args: Any, **_kwargs: Any):
        pytest.fail("disk-only accounting must not invoke OMP or subprocesses")

    monkeypatch.setattr(quota_module, "read_claude_headroom", unexpected_probe)
    monkeypatch.setattr(quota_module.subprocess, "run", unexpected_probe)
    job, trial = disk_job(tmp_path)
    persist_claude_dispatch_quota(job, claude_dispatch_quota_record(reading()))
    rollout = trial / "agent/sessions/rollout-fixture.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "timestamp": NOW.isoformat(),
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "limit_id": "unrelated-codex-format",
                        "primary": {"used_percent": 99, "window_minutes": 10080},
                    },
                },
            }
        ) + "\n"
    )
    transcript = trial / "agent/sessions/projects/fixture/session.jsonl"
    transcript.parent.mkdir(parents=True)
    event = {
        "type": "assistant",
        "message": {"usage": {"input_tokens": 10, "output_tokens": 2}},
    }
    transcript.write_text((json.dumps(event) + "\n") * 2)

    report = load_quota_report((job,), now=NOW + timedelta(seconds=60), paid_agents=frozenset({"claude-code"}))

    assert len(report.observations) == 1
    observation = report.observations[0]
    assert observation.agent == "claude-code"
    assert observation.fetched_at == FETCHED_AT
    assert observation.observed_at == FETCHED_AT
    assert observation.primary == reading().windows["7d"]
    assert observation.secondary == reading().windows["5h"]
    assert observation.windows == reading().windows
    assert observation.source.endswith("agent/quota/claude-dispatch.json")
    assert not Path(observation.source).is_absolute()
    assert report.headroom.availability == "observed"
    assert report.headroom.used_percent == 29
    assert report.headroom.fetched_at == FETCHED_AT
    assert report.headroom.windows["5h"].status == "warning"
    assert report.headroom.windows["7d"].status == "ok"
    assert report.headroom.staleness_seconds == 90
    assert report.consumed.totals().input_tokens == 10


@pytest.mark.parametrize("malformed", ["not json", "[]", '{"kind":"wrong-kind"}'])
def test_disk_report_ignores_malformed_dispatch_sidecars(tmp_path: Path, malformed: str) -> None:
    job, trial = disk_job(tmp_path)
    sidecar = trial / "agent/quota/claude-dispatch.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(malformed)

    report = load_quota_report((job,), now=NOW, paid_agents=frozenset({"claude-code"}))

    assert report.observations == ()
    assert report.headroom.availability == "unavailable"
    assert report.headroom.used_percent is None
    assert len(report.consumed.trials) == 1
