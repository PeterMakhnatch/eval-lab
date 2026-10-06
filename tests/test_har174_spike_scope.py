"""HAR-174: infra-spike fence scoped to the current batch + watch ack."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab import auto_watch, live_watch
from evallab.process_job import process_job
from evallab.queue import DirectoryQueue, Executor, load_events
from evallab.runner import RunRequest
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy

CREDENTIALS = frozenset(
    {
        "claude_oauth",
        "codex_auth",
        "mimo_selfhosted_api_environment",
        "zai_openapi_api_environment",
        "deepseek_api_environment",
    }
)


def _policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[
            AutoRunRule(name="local-controls", agents=["oracle", "nop"]),
            AutoRunRule(
                name="canary",
                tasks=["canary/*"],
                agents=["codex", "claude-code", "mimoagent"],
                max_attempts=3,
            ),
        ],
    )


def _spec(name: str) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        hypothesis="exercise the har174 spike scope",
        purpose="practice",
        task="canary/event-summary",
        agent="codex",
        submitted_by="test-agent",
        est_cost_usd=0,
    )


def _service(root: Path, *, runner, **overrides) -> Executor:
    params: dict = {
        "repo_root": root,
        "queue": DirectoryQueue(root / "queue"),
        "policy": _policy(),
        "runner": runner,
        "ingester": lambda _path: None,
        "credential_probe": lambda: CREDENTIALS,
        "spent_today": lambda: 0.0,
        "consecutive_harness_failures": lambda: 0,
        "sleeper": lambda _seconds: None,
        "watch_enabled": False,
        "smoke_gate_enabled": False,
    }
    params.update(overrides)
    return Executor(**params)


def _approve(service: Executor, spec: ExperimentSpec) -> str:
    waiting, _ = service.submit(spec)
    spec_id = str(service.queue.load(waiting).spec_id)
    service.queue.approve(spec_id, actor="peter")
    return spec_id


def _mkdir_job(request: RunRequest) -> Path:
    job = Path(request.jobs_dir) / request.name
    job.mkdir(parents=True, exist_ok=True)
    return job


def _write_spike_alert(
    job: Path, *, rule: str = "proxy_error_spike", first_seen: str | None = None
) -> Path:
    watch = job / "watch"
    watch.mkdir(parents=True, exist_ok=True)
    row = {
        "rule": rule,
        "severity": "high",
        "scope": "fleet",
        "job": job.name,
        "trial": "fleet:proxy_error_spike",
        "task": "fleet",
        "step_ref": None,
        "quote": "",
        "detail": "3+ proxy errors within 10.0 min across trials",
    }
    if first_seen is not None:
        row["first_seen"] = first_seen
    (watch / "alerts.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    return job


def _backdate(path: Path, moment: datetime) -> None:
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))


def _reason_codes(service: Executor) -> list[str | None]:
    return [event.reason_code for event in load_events(service.queue.events_path)]


def test_historical_spike_does_not_fence_after_resume(tmp_path: Path) -> None:
    """A pre-resume spike from an old job no longer blocks the next batch."""
    service = _service(tmp_path, runner=_mkdir_job)
    for name in ("scope-a", "scope-b"):
        _approve(service, _spec(name))
    old = tmp_path / "runs" / "earlier-job"
    old.mkdir(parents=True)
    _write_spike_alert(old, first_seen="2026-01-01T00:00:00+00:00")
    _backdate(old / "watch" / "alerts.jsonl", datetime(2026, 1, 1, tzinfo=UTC))
    _backdate(old, datetime(2026, 1, 1, tzinfo=UTC))

    service.queue.resume()
    assert service.tick() == 2
    assert not (tmp_path / "queue" / "STOP").is_file()
    assert service.last_tick_reason != "infra_spike_stop"


def test_live_in_batch_spike_still_fences(tmp_path: Path) -> None:
    """A fresh spike launched since the last resume still stops dispatch."""
    service = _service(tmp_path, runner=_mkdir_job)
    for name in ("live-a", "live-b"):
        _approve(service, _spec(name))
    service.queue.resume()
    fresh = tmp_path / "runs" / "fresh-job"
    fresh.mkdir(parents=True)
    _write_spike_alert(fresh, first_seen=datetime.now(UTC).isoformat())

    assert service.tick() == 0
    assert (tmp_path / "queue" / "STOP").is_file()
    assert "infra_spike:proxy_error_spike" in _reason_codes(service)
    assert service.last_tick_reason == "infra_spike_stop"


def test_acknowledged_spike_does_not_fence(tmp_path: Path) -> None:
    """An acked in-batch spike lets the tick proceed; alerts.jsonl is intact."""
    service = _service(tmp_path, runner=_mkdir_job)
    for name in ("ack-a", "ack-b"):
        _approve(service, _spec(name))
    service.queue.resume()
    spiked = tmp_path / "runs" / "spiked-job"
    spiked.mkdir(parents=True)
    _write_spike_alert(spiked, first_seen=datetime.now(UTC).isoformat())

    record = auto_watch.write_watch_ack(
        spiked,
        rule="proxy_error_spike",
        actor="peter",
        reason="har168 resolved, excluded trial",
    )
    assert record["covers"] == [{"trial": "fleet:proxy_error_spike", "rule": "proxy_error_spike"}]
    alerts_path = spiked / "watch" / "alerts.jsonl"
    assert len(alerts_path.read_text(encoding="utf-8").splitlines()) == 1

    assert service.tick() == 2
    assert not (tmp_path / "queue" / "STOP").is_file()


def test_ack_refusals() -> None:
    """An ack with a blank actor/reason, unknown job, or unknown kind fails."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        job = root / "runs" / "some-job"
        job.mkdir(parents=True)
        _write_spike_alert(job, first_seen="2026-10-06T00:00:00+00:00")
        with pytest.raises(ValueError, match="actor"):
            auto_watch.write_watch_ack(job, rule="proxy_error_spike", actor="  ", reason="why")
        with pytest.raises(ValueError, match="reason"):
            auto_watch.write_watch_ack(job, rule="proxy_error_spike", actor="peter", reason="")
        with pytest.raises(ValueError, match="unknown job"):
            auto_watch.write_watch_ack(
                root / "runs" / "missing-job",
                rule="proxy_error_spike",
                actor="peter",
                reason="why",
            )
        with pytest.raises(ValueError, match="no alert"):
            auto_watch.write_watch_ack(job, rule="infra_spike", actor="peter", reason="why")


def test_ack_command_refuses_and_records(tmp_path: Path) -> None:
    """`evallab watch ack` refuses bad input (exit 2) and records good input."""
    job = tmp_path / "runs" / "cli-job"
    job.mkdir(parents=True)
    _write_spike_alert(job, first_seen="2026-10-06T00:00:00+00:00")

    def _args(**overrides) -> SimpleNamespace:
        base = {
            "job": "cli-job",
            "alert": "proxy_error_spike",
            "actor": "peter",
            "reason": "resolved",
            "runs_dir": [],
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    assert live_watch._ack_command(_args(actor="  "), tmp_path) == 2
    assert live_watch._ack_command(_args(reason=""), tmp_path) == 2
    assert live_watch._ack_command(_args(job="missing-job"), tmp_path) == 2
    assert live_watch._ack_command(_args(alert="infra_spike"), tmp_path) == 2
    assert live_watch._ack_command(_args(), tmp_path) == 0
    acks = auto_watch.read_watch_acks(job)
    assert [ack["actor"] for ack in acks] == ["peter"]


def test_acknowledged_alert_rendered_on_job_page(tmp_path: Path) -> None:
    """An acked alert still shows on the job page, marked by its actor."""
    jobs: list[Path] = []

    def run(request: RunRequest) -> Path:
        job = _mkdir_job(request)
        _write_spike_alert(job, first_seen=datetime.now(UTC).isoformat())
        auto_watch.write_watch_ack(
            job, rule="proxy_error_spike", actor="peter", reason="resolved trial"
        )
        jobs.append(job)
        return job

    service = _service(tmp_path, runner=run)
    _approve(service, _spec("rendered-job"))
    service.queue.resume()
    assert service.tick() == 1
    (job,) = jobs

    report = process_job(job, ingest=False, publish=False)
    job_md = (job / "processed" / "job.md").read_text(encoding="utf-8")
    assert "acknowledged by peter" in job_md
    assert report["watch"]["alerts"][0]["acknowledged_by"] == "peter"
    assert auto_watch.watch_status_suffix(job) == (
        "watch: 1 alerts; latest critical: proxy_error_spike; acknowledged by peter"
    )
