"""HAR-162: dispatch-attached live watch (behavioral, through real dispatch)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from evallab import auto_watch
from evallab.process_job import process_job
from evallab.queue import DirectoryQueue, Executor
from evallab.runner import RunRequest
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy
from evallab.status import build_status_snapshot

LOOP_CMD = "cd /workspace/repo && grep -rn apply_delta dulwich/ --include='*.py' | cat\n"


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
                agents=["codex", "claude-code"],
                max_attempts=3,
            ),
        ],
        escalate_to_human=["anything_exceeding_ceilings"],
    )


def _spec(name: str, **overrides) -> ExperimentSpec:
    params = {
        "name": name,
        "hypothesis": "exercise the auto watch",
        "purpose": "practice",
        "task": "canary/event-summary",
        "agent": "codex",
        "submitted_by": "test-agent",
        "est_cost_usd": 0,
    }
    params.update(overrides)
    return ExperimentSpec(**params)


def _service(
    root: Path,
    *,
    runner,
    notify_runner=None,
    interval_seconds: float = 60.0,
    watch_enabled: bool = True,
) -> Executor:
    return Executor(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=_policy(),
        runner=runner,
        ingester=lambda path: None,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: frozenset({"claude_oauth", "codex_auth"}),
        sleeper=lambda _seconds: None,
        watch_enabled=watch_enabled,
        watch_interval_seconds=interval_seconds,
        notify_runner=notify_runner,
    )


def _submit_billable(service: Executor, item: ExperimentSpec) -> None:
    waiting, _ = service.submit(item)
    service.queue.approve(str(service.queue.load(waiting).spec_id), actor="peter")


def _step(step_id: int, keystrokes: str) -> dict:
    return {
        "step_id": step_id,
        "source": "agent",
        "message": f"analysis for step {step_id}",
        "tool_calls": [
            {
                "tool_call_id": f"c{step_id}",
                "function_name": "bash_command",
                "arguments": {"keystrokes": keystrokes, "duration": 1.0},
            }
        ],
        "metrics": {"prompt_tokens": 1000, "completion_tokens": 10},
    }


def _write_stalled_loop_job(
    request: RunRequest, *, n_identical: int = 8, mtime_ago_min: float = 15.0
) -> Path:
    """Stub Harbor run: one running trial with a stall and a repetition loop."""
    job = Path(request.jobs_dir) / request.name
    agent_dir = job / "event-summary__a1" / "agent"
    agent_dir.mkdir(parents=True)
    traj = agent_dir / "trajectory.json"
    traj.write_text(
        json.dumps({"steps": [_step(i, LOOP_CMD) for i in range(1, n_identical + 1)]}),
        encoding="utf-8",
    )
    old = time.time() - mtime_ago_min * 60.0
    os.utime(traj, (old, old))
    return job


def _alert_rules(job: Path) -> set[str]:
    return {str(row.get("rule")) for row in auto_watch.read_watch_alerts(job)}


def test_dispatch_attaches_watch_and_renders_alerts_on_job_page(tmp_path: Path) -> None:
    jobs: list[Path] = []

    def run(request: RunRequest) -> Path:
        job = _write_stalled_loop_job(request)
        jobs.append(job)
        return job

    service = _service(tmp_path, runner=run)
    _submit_billable(service, _spec("watched-job"))
    assert service.tick() == 1

    (job,) = jobs
    alerts_path = job / "watch" / "alerts.jsonl"
    assert alerts_path.is_file()
    assert {"stalled", "repetition"} <= _alert_rules(job)

    report = process_job(job, ingest=False, publish=False)
    job_md = (job / "processed" / "job.md").read_text(encoding="utf-8")
    assert "## Live watch alerts" in job_md
    assert "`stalled`" in job_md
    assert "`repetition`" in job_md
    assert report["watch"]["n_alerts"] >= 2

    # No manual step: alerts rendered by the same dispatch that ran the job.
    assert auto_watch.active_watch_count() == 0


def test_watcher_is_attached_during_the_run_and_exits_when_job_ends(
    tmp_path: Path,
) -> None:
    seen: list[int] = []

    def run(request: RunRequest) -> Path:
        job = _write_stalled_loop_job(request)
        seen.append(auto_watch.active_watch_count())
        time.sleep(0.6)
        seen.append(auto_watch.active_watch_count())
        return job

    service = _service(tmp_path, runner=run, interval_seconds=0.05)
    _submit_billable(service, _spec("live-watched-job"))
    tick_thread = threading.Thread(target=service.tick)
    tick_thread.start()
    tick_thread.join(timeout=60)
    assert not tick_thread.is_alive()
    assert seen and all(count >= 1 for count in seen)
    assert auto_watch.active_watch_count() == 0


def test_watcher_crash_does_not_fail_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("watch exploded")

    monkeypatch.setattr("evallab.live_watch.run_watch", boom)

    def run(request: RunRequest) -> Path:
        return _write_stalled_loop_job(request)

    service = _service(tmp_path, runner=run, interval_seconds=0.05)
    _submit_billable(service, _spec("crash-watched-job"))
    assert service.tick() == 1

    job = tmp_path / "runs" / "crash-watched-job"
    done = service.queue.list_specs("done")
    assert [item.name for _, item in done] == ["crash-watched-job"]
    assert "watcher_error" in _alert_rules(job)
    assert auto_watch.active_watch_count() == 0


def test_model_free_agents_skip_watch(tmp_path: Path) -> None:
    def run(request: RunRequest) -> Path:
        destination = Path(request.jobs_dir) / request.name
        destination.mkdir(parents=True)
        return destination

    service = _service(tmp_path, runner=run)
    service.submit(
        ExperimentSpec(
            name="skipped-oracle-control",
            hypothesis="controls skip the watch",
            purpose="practice",
            task="library/tasks/event-summary",
            agent="oracle",
            submitted_by="test-agent",
        )
    )
    assert service.tick() == 1
    assert not (tmp_path / "runs" / "skipped-oracle-control" / "watch").exists()
    assert auto_watch.active_watch_count() == 0


def test_notify_lin_opt_in_posts_critical_once_per_job(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_lin(cmd: list[str], **kwargs) -> None:
        calls.append(cmd)

    requests: list[RunRequest] = []

    def run(request: RunRequest) -> Path:
        requests.append(request)
        return _write_stalled_loop_job(request)

    service = _service(tmp_path, runner=run, notify_runner=fake_lin)
    _submit_billable(service, _spec("notified-job", linear_card="har-162", watch_notify_lin=True))
    assert service.tick() == 1

    assert len(calls) == 1
    assert calls[0][:3] == ["lin", "comment", "HAR-162"]
    assert "stalled" in calls[0][3]

    # A second finalize finds every critical kind already posted: no re-post.
    (request,) = requests
    handle = auto_watch.start_for_request(request, notify_runner=fake_lin)
    auto_watch.stop_auto_watch(handle, notify_runner=fake_lin)
    assert len(calls) == 1


def test_notify_lin_off_by_default(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(request: RunRequest) -> Path:
        return _write_stalled_loop_job(request)

    service = _service(tmp_path, runner=run, notify_runner=lambda cmd, **kw: calls.append(cmd))
    _submit_billable(service, _spec("quiet-job", linear_card="HAR-162"))
    assert service.tick() == 1
    assert calls == []


def test_watch_notify_lin_requires_linear_card() -> None:
    with pytest.raises(Exception, match="linear_card"):
        _spec("cardless-notify", watch_notify_lin=True)


def test_status_shows_watch_counts_for_running_job(tmp_path: Path) -> None:
    (tmp_path / "jobs").mkdir()
    item = _spec("running-watched", linear_card="HAR-162")
    item = item.model_copy(update={"spec_id": "01TESTWATCHEDJOB00000001"})
    (tmp_path / "queue" / "running").mkdir(parents=True)
    (tmp_path / "queue" / "running" / "spec.json").write_text(
        item.model_dump_json(), encoding="utf-8"
    )
    watch_dir = tmp_path / "runs" / "running-watched" / "watch"
    watch_dir.mkdir(parents=True)
    watch_dir.joinpath("alerts.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "rule": "stalled",
                        "severity": "medium",
                        "trial": "event-summary__a1",
                        "detail": "quiet",
                    }
                ),
                json.dumps(
                    {
                        "rule": "infra_error",
                        "severity": "high",
                        "trial": "event-summary__a1",
                        "detail": "daytona down",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = build_status_snapshot(
        tmp_path, postgres_probe=lambda: False, phoenix_probe=lambda: False
    )
    details = [entry.detail or "" for entry in snapshot.Now.items]
    match = [text for text in details if "running-watched" in text or "watch:" in text]
    assert match, details
    assert any("watch: 2 alerts" in text for text in match)
    assert any("latest critical: infra_error" in text for text in match)
