"""Live stop: eligibility, guards, request/record lifecycle, one-trial actuator."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import evallab.harbor_watch_hooks as hooks_mod
import evallab.watch_stop as watch_stop
from evallab.harbor_view import build_merged_result
from evallab.harbor_watch_hooks import WatchHookPlugin
from evallab.live_watch import evaluate_alerts, run_watch
from evallab.watch_stop import (
    INTEGRITY_BOARD_RULES,
    WATCH_STOP_EXCEPTION,
    WATCH_STOP_MESSAGE,
    capture_host_snapshot,
    drive_trial_stops,
    read_stop_record,
    read_stop_request,
    stop_already_handled,
    stop_decision,
    stop_eligible_alert,
    stop_image_tag,
    stop_mode_from_env,
    trial_terminal,
    usage_at_stop,
    verification_started,
    write_stop_record,
    write_stop_request,
)


def _alert(
    rule: str,
    trial: str = "task__a1",
    job: str = "job",
    scope: str = "trial",
    **extra: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "rule": rule,
        "severity": "high",
        "scope": scope,
        "job": job,
        "trial": trial,
        "task": "task",
        "step_ref": "head#3",
        "quote": "git fsck --unreachable",
        "detail": "mining",
    }
    record.update(extra)
    return record


def _hooks(*events: str, terminal: bool = False) -> dict[str, Any]:
    return {
        "events": [[name, "2026-10-10T00:00:00+00:00"] for name in events],
        "phase": events[-1] if events else None,
        "terminal": terminal,
        "exception_type": None,
        "rewards": None,
        "logs": {},
        "last_at": 1.0,
    }


# --- eligibility -----------------------------------------------------------


def test_only_integrity_rules_are_eligible() -> None:
    assert stop_eligible_alert(_alert("history_mining"))[0] is True
    assert stop_eligible_alert(_alert("hidden_test_read"))[0] is True
    for rule in ("stalled", "copy_acquired", "git_object_read", "spend", "budget_burn"):
        eligible, _ = stop_eligible_alert(_alert(rule))
        assert eligible is False, rule


def test_grader_tamper_needs_file_evidence() -> None:
    eligible, reason = stop_eligible_alert(_alert("grader_tamper"))
    assert eligible is False
    assert reason == "grader_tamper_without_file_evidence"
    file_backed = _alert(
        "grader_tamper",
        baseline_ref="baseline-w1",
        before_sha256="a" * 64,
        after_sha256="b" * 64,
    )
    assert stop_eligible_alert(file_backed) == (True, "eligible")


def test_fleet_scope_never_eligible() -> None:
    eligible, reason = stop_eligible_alert(_alert("history_mining", scope="fleet"))
    assert (eligible, reason) == (False, "fleet_scope")


# --- guards -----------------------------------------------------------------


def test_guards_terminal_verification_and_acks() -> None:
    alert = _alert("history_mining")
    running = {"state": "running"}
    assert stop_decision(alert, status=running, hooks=None, acks=[], mode="on")["action"] == "stop"
    assert (
        stop_decision(alert, status=running, hooks=None, acks=[], mode="dry-run")["action"]
        == "would_stop"
    )
    assert stop_decision(alert, status=running, hooks=None, acks=[], mode="off")["action"] == "skip"
    assert (
        stop_decision(alert, status={"state": "finished"}, hooks=None, acks=[], mode="on")["action"]
        == "skip"
    )
    assert (
        stop_decision(
            alert, status=running, hooks=_hooks("end", terminal=True), acks=[], mode="on"
        )["action"]
        == "skip"
    )
    assert (
        stop_decision(
            alert,
            status=running,
            hooks=_hooks("start", "agent-start", "verification-start"),
            acks=[],
            mode="on",
        )["reason"]
        == "verification_started"
    )
    ack = {
        "rule": "history_mining",
        "actor": "op",
        "reason": "benign",
        "covers": [{"trial": "task__a1", "rule": "history_mining"}],
    }
    assert (
        stop_decision(alert, status=running, hooks=None, acks=[ack], mode="on")["reason"] == "acked"
    )


def test_trial_terminal_and_verification_helpers() -> None:
    assert trial_terminal({"state": "finished"}, None) is True
    assert trial_terminal({"state": "running"}, _hooks("end", terminal=True)) is True
    assert trial_terminal({"state": "running"}, None) is False
    assert verification_started(_hooks("start", "verification-start")) is True
    assert verification_started(_hooks("start", "agent-start")) is False
    assert verification_started(None) is False


def test_stop_mode_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert stop_mode_from_env({}) == "dry-run"
    assert stop_mode_from_env({"EVALLAB_WATCH_STOP": "on"}) == "on"
    assert stop_mode_from_env({"EVALLAB_WATCH_STOP": "off"}) == "off"
    assert stop_mode_from_env({"EVALLAB_WATCH_STOP": "bogus"}) == "off"
    monkeypatch.setenv("EVALLAB_WATCH_STOP", "on")
    assert stop_mode_from_env() == "on"


def test_stop_image_tag_is_docker_safe() -> None:
    assert stop_image_tag("task__a1") == "evallab-stop/task--a1"
    assert stop_image_tag("UPPER/x") == "evallab-stop/upper-x"


def test_usage_at_stop_is_an_upper_bound_not_a_claim() -> None:
    usage = usage_at_stop(
        {
            "steps": 10,
            "episodes": 3,
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "cost_usd": 0.01,
            "input_token_limit": 1000,
        }
    )
    assert usage["remaining_input_budget_upper_bound"] == 900
    assert usage_at_stop(None).get("remaining_input_budget_upper_bound") is None


# --- request / record lifecycle ----------------------------------------------


def test_request_first_wins_and_roundtrips(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    path, created = write_stop_request(
        watch,
        trial="task__a1",
        rule="history_mining",
        step_ref="head#3",
        alert={"rule": "history_mining"},
        requested_by="auto",
        mode="on",
    )
    assert created is True
    first = read_stop_request(watch, "task__a1")
    assert first is not None and first["requested_by"] == "auto"
    _, created_again = write_stop_request(
        watch,
        trial="task__a1",
        rule="manual",
        step_ref=None,
        alert=None,
        requested_by="manual",
        mode="on",
    )
    assert created_again is False
    reread = read_stop_request(watch, "task__a1")
    assert reread is not None and reread["requested_by"] == "auto"
    assert stop_already_handled(watch, "task__a1") is True
    assert stop_already_handled(watch, "task__a2") is False
    assert path.name == "task__a1.json"


def test_record_roundtrips_with_manifest(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    manifest = {
        "files": [{"path": "agent/trajectory.json", "sha256": "x", "bytes": 3}],
        "missing": [],
        "errors": [],
    }
    path = write_stop_record(
        watch,
        trial="task__a1",
        outcome="requested",
        rule="history_mining",
        step_ref="head#3",
        requested_by="auto",
        mode="on",
        manifest=manifest,
        verifier_started=False,
        usage={"steps_so_far": 4},
        reason="eligible",
    )
    assert path.name == "task__a1.json"
    record = read_stop_record(watch, "task__a1")
    assert record is not None
    assert record["verifier_never_ran"] is True
    assert record["capture_manifest"] == manifest
    assert stop_already_handled(watch, "task__a1") is True


def test_host_snapshot_copies_files_with_hashes(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "task__a1"
    (trial / "agent").mkdir(parents=True)
    (trial / "agent" / "trajectory.json").write_text('{"steps": []}', encoding="utf-8")
    (trial / "config.json").write_text("{}", encoding="utf-8")
    manifest = capture_host_snapshot(
        job_dir=job,
        trial="task__a1",
        dest_dir=tmp_path / "snap",
        alert={"rule": "history_mining"},
    )
    paths = {entry["path"] for entry in manifest["files"]}
    assert "agent/trajectory.json" in paths
    assert "config.json" in paths
    assert "firing-alert.json" in paths
    for entry in manifest["files"]:
        assert len(entry["sha256"]) == 64
    assert manifest["errors"] == []


# --- run_watch integration ----------------------------------------------------


def _write_mining_trial(runs: Path) -> Path:
    trial = runs / "job" / "task__a1"
    (trial / "agent").mkdir(parents=True)
    steps = [
        {
            "step_id": 3,
            "source": "agent",
            "message": "recon",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "function_name": "bash",
                    "arguments": {"command": "git fsck --unreachable 2>/dev/null | head"},
                }
            ],
            "metrics": {"prompt_tokens": 10, "completion_tokens": 1},
        }
    ]
    (trial / "agent" / "trajectory.json").write_text(json.dumps({"steps": steps}))
    watch = runs / "job" / "watch"
    watch.mkdir(parents=True, exist_ok=True)
    (watch / "hooks.jsonl").write_text(
        json.dumps(
            {
                "schema": "evallab.watch_hooks/v1",
                "kind": "event",
                "event": "agent-start",
                "trial": "task__a1",
                "task": "task",
                "trial_id": "tid-1",
                "at": "2026-10-10T00:00:01+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return trial


def test_run_watch_dry_run_records_would_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_stop, "docker_capture", lambda *a, **k: {"skipped": "test"})
    runs = tmp_path / "runs"
    _write_mining_trial(runs)
    out = runs / "job" / "watch"
    summary = run_watch(runs_dirs=[runs], out_dir=out, stop_mode="dry-run")
    assert summary["stop_mode"] == "dry-run"
    assert len(summary["stops"]) == 1
    stop = summary["stops"][0]
    assert (stop["outcome"], stop["rule"]) == ("would_stop", "history_mining")
    assert not (out / "stop-requests").exists()
    record = read_stop_record(out, "task__a1")
    assert record is not None
    assert record["outcome"] == "would_stop"
    assert record["verifier_never_ran"] is True
    board = (out / "BOARD.md").read_text(encoding="utf-8")
    assert "## Stops (mode: dry-run)" in board
    assert "**Integrity signals:**" in board
    assert "history_mining" in board
    status_doc = json.loads((out / "status.json").read_text(encoding="utf-8"))
    assert status_doc["stop_mode"] == "dry-run"
    assert status_doc["stops"][0]["trial"] == "task__a1"
    # Second pass: no duplicate lifecycle (dedup by trial+rule, one record).
    again = run_watch(runs_dirs=[runs], out_dir=out, stop_mode="dry-run")
    assert again["stops"] == []


def test_run_watch_off_writes_no_stop_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_stop, "docker_capture", lambda *a, **k: {"skipped": "test"})
    runs = tmp_path / "runs"
    _write_mining_trial(runs)
    out = runs / "job" / "watch"
    summary = run_watch(runs_dirs=[runs], out_dir=out, stop_mode="off")
    assert summary["stops"] == []
    assert not (out / "stop-records").exists()
    assert "history_mining" in (out / "BOARD.md").read_text(encoding="utf-8")


def test_run_watch_on_writes_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch_stop, "docker_capture", lambda *a, **k: {"skipped": "test"})
    runs = tmp_path / "runs"
    _write_mining_trial(runs)
    out = runs / "job" / "watch"
    summary = run_watch(runs_dirs=[runs], out_dir=out, stop_mode="on")
    assert summary["stops"][0]["outcome"] == "requested"
    request = read_stop_request(out, "task__a1")
    assert request is not None
    assert request["rule"] == "history_mining"
    assert request["requested_by"] == "auto"


def test_drive_skips_acked_terminal_and_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_stop, "docker_capture", lambda *a, **k: {"skipped": "test"})
    watch = tmp_path / "watch"
    job = tmp_path / "job"
    (job / "task__a1" / "agent").mkdir(parents=True)
    statuses = [{"trial": "task__a1", "job": "job", "state": "running"}]
    jobs = {"job": job}
    ack = {
        "rule": "history_mining",
        "actor": "op",
        "reason": "x",
        "covers": [{"trial": "task__a1", "rule": "history_mining"}],
    }
    assert (
        drive_trial_stops(
            watch_dir=watch,
            job_dirs=jobs,
            statuses=statuses,
            fresh_alerts=[_alert("history_mining")],
            hooks_by_job={},
            acks_by_job={},
            mode="on",
        )
        != []
    )
    # Acked, terminal, verification, fleet, and off-mode all drive nothing.
    assert (
        drive_trial_stops(
            watch_dir=watch,
            job_dirs=jobs,
            statuses=statuses,
            fresh_alerts=[_alert("history_mining")],
            hooks_by_job={},
            acks_by_job={"job": [ack]},
            mode="on",
        )
        == []
    )
    finished = [{"trial": "task__a1", "job": "job", "state": "finished"}]
    assert (
        drive_trial_stops(
            watch_dir=watch,
            job_dirs=jobs,
            statuses=finished,
            fresh_alerts=[_alert("history_mining")],
            hooks_by_job={},
            acks_by_job={},
            mode="on",
        )
        == []
    )
    assert (
        drive_trial_stops(
            watch_dir=watch,
            job_dirs=jobs,
            statuses=statuses,
            fresh_alerts=[_alert("history_mining", scope="fleet")],
            hooks_by_job={},
            acks_by_job={},
            mode="on",
        )
        == []
    )
    assert (
        drive_trial_stops(
            watch_dir=watch,
            job_dirs=jobs,
            statuses=statuses,
            fresh_alerts=[_alert("history_mining")],
            hooks_by_job={},
            acks_by_job={},
            mode="off",
        )
        == []
    )


def test_integrity_board_rule_order_matches_spec() -> None:
    assert INTEGRITY_BOARD_RULES == (
        "grader_tamper",
        "history_mining",
        "hidden_test_read",
        "git_object_read",
        "copy_acquired",
    )


# --- in-process actuator ------------------------------------------------------


class _FakeResult:
    """Harbor TrialResult slice: exception marker plus JSON persistence."""

    def __init__(self, trial_name: str) -> None:
        self.trial_name = trial_name
        self.exception_info: Any = None

    def model_dump_json(self, *, indent: int = 4) -> str:
        info = self.exception_info
        return json.dumps(
            {
                "trial_name": self.trial_name,
                "exception_info": None
                if info is None
                else {
                    "exception_type": info.exception_type,
                    "exception_message": info.exception_message,
                },
            },
            indent=indent,
        )


class _FakeQueue:
    """Mimics Harbor's trial execution: CancelledError records, recovers,
    re-raises; ``finally`` writes result.json and emits END with the result."""

    def __init__(self, job_dir: Path, end_hooks: list[Any]) -> None:
        self.job_dir = job_dir
        self.end_hooks = end_hooks

    async def _run_trial(self, trial_config: Any) -> Any:
        trial_name = str(trial_config.trial_name)
        result = _FakeResult(trial_name)
        try:
            if trial_name.endswith("__a1"):
                await asyncio.sleep(30)
            else:
                await asyncio.sleep(0.1)
        except asyncio.CancelledError as exc:
            result.exception_info = SimpleNamespace(
                exception_type="CancelledError", exception_message=str(exc)
            )
            await asyncio.sleep(0)
            raise
        finally:
            trial_dir = self.job_dir / trial_name
            trial_dir.mkdir(parents=True, exist_ok=True)
            (trial_dir / "result.json").write_text(result.model_dump_json(), encoding="utf-8")
            event = SimpleNamespace(trial_name=trial_name, result=result)
            for hook in self.end_hooks:
                await hook(event)
        return result


class _FakeJob:
    """The slice of ``harbor.job.Job`` the plugin touches."""

    def __init__(self, job_dir: Path, queue: _FakeQueue) -> None:
        self.job_dir = job_dir
        self._trial_queue = queue
        self.end_hooks: list[Any] = []
        for name in (
            "on_trial_started",
            "on_environment_started",
            "on_agent_started",
            "on_agent_ended",
            "on_verification_started",
            "on_trial_ended",
            "on_trial_cancelled",
        ):
            setattr(self, name, self._collect)

    def _collect(self, callback: Any) -> None:
        if getattr(callback, "__name__", "") == "_on_end_capture":
            self._trial_queue.end_hooks.append(callback)


def _config(trial_name: str, job_dir: Path) -> Any:
    return SimpleNamespace(trial_name=trial_name, trials_dir=str(job_dir))


def test_actuator_stops_one_trial_sibling_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks_mod, "STOP_POLL_SECONDS", 0.05)
    job_dir = tmp_path / "job"
    (job_dir / "watch" / "stop-requests").mkdir(parents=True)
    queue = _FakeQueue(job_dir, [])
    job = _FakeJob(job_dir, queue)
    plugin = WatchHookPlugin()

    async def scenario() -> tuple[Any, Any]:
        await plugin.on_job_start(job)
        assert plugin.stop_armed is True
        async with asyncio.TaskGroup() as tg:
            miner = tg.create_task(queue._run_trial(_config("task__a1", job_dir)))
            sibling = tg.create_task(queue._run_trial(_config("task__a2", job_dir)))
            await asyncio.sleep(0.2)
            write_stop_request(
                job_dir / "watch",
                trial="task__a1",
                rule="history_mining",
                step_ref="head#3",
                alert={"rule": "history_mining"},
                requested_by="auto",
                mode="on",
            )
        await plugin.on_job_end(None)
        assert miner.cancelled() is False
        assert sibling.cancelled() is False
        return miner.result(), sibling.result()

    stopped, clean = asyncio.run(scenario())
    assert stopped.exception_info.exception_type == WATCH_STOP_EXCEPTION
    assert WATCH_STOP_MESSAGE in stopped.exception_info.exception_message
    assert clean.exception_info is None
    persisted = json.loads((job_dir / "task__a1" / "result.json").read_text(encoding="utf-8"))
    assert persisted["exception_info"]["exception_type"] == WATCH_STOP_EXCEPTION


def test_actuator_reraises_foreign_cancels(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    (job_dir / "watch").mkdir(parents=True)
    queue = _FakeQueue(job_dir, [])
    job = _FakeJob(job_dir, queue)
    plugin = WatchHookPlugin()

    async def scenario() -> tuple[Any, WatchHookPlugin]:
        await plugin.on_job_start(job)
        assert plugin.stop_armed is True
        async with asyncio.TaskGroup() as tg:
            victim = tg.create_task(queue._run_trial(_config("task__a1", job_dir)))
            await asyncio.sleep(0.2)
            assert "task__a1" in plugin._trial_tasks
            assert plugin._stop_requested == set()
            victim.cancel("external-shutdown")
        # A TaskGroup absorbs an externally-cancelled child; the task itself
        # stays cancelled with the ordinary marker (never converted).
        await plugin.on_job_end(None)
        return victim, plugin

    victim, plugin = asyncio.run(scenario())
    assert victim.cancelled() is True
    with pytest.raises(asyncio.CancelledError, match="external-shutdown"):
        victim.result()
    assert plugin._stop_requested == set()
    ordinary = plugin._end_results["task__a1"]
    assert ordinary.exception_info.exception_type == "CancelledError"


def test_actuator_disarmed_by_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVALLAB_WATCH_STOP", "off")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    queue = _FakeQueue(job_dir, [])
    job = _FakeJob(job_dir, queue)
    plugin = WatchHookPlugin()
    asyncio.run(plugin.on_job_start(job))
    assert plugin.stop_armed is False
    assert "_run_trial" not in queue.__dict__
    asyncio.run(plugin.on_job_end(None))


def test_check_ignores_requests_without_live_tasks(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    (job_dir / "watch" / "stop-requests").mkdir(parents=True)
    plugin = WatchHookPlugin()
    plugin._job_dir = job_dir
    write_stop_request(
        job_dir / "watch",
        trial="ghost__a9",
        rule="history_mining",
        step_ref=None,
        alert=None,
        requested_by="auto",
        mode="on",
    )
    plugin._check_stop_requests()  # never raises; nothing to cancel
    assert plugin._stop_requested == set()


# --- downstream counting ------------------------------------------------------


def test_merged_result_breaks_out_watch_stops() -> None:
    trials = [
        ("task__a1", {"exception_info": {"exception_type": "WatchStopCancelledError"}}),
        ("task__a2", {"exception_info": {"exception_type": "CancelledError"}}),
        ("task__a3", {}),
    ]
    merged = build_merged_result("job", trials)  # type: ignore[arg-type]
    assert merged["stats"]["n_cancelled_trials"] == 2
    assert merged["stats"]["n_stopped_by_watch"] == 1


def _bare_status(**overrides: Any) -> dict[str, Any]:
    status: dict[str, Any] = {
        "job": "job",
        "trial": "task__a1",
        "task": "task",
        "state": "running",
        "minutes_since_update": 0.0,
        "prompt_tokens": 10,
        "input_token_limit": 1000,
        "completion_tokens": 1,
        "cost_usd": 0.0,
        "cost_limit_usd": None,
        "max_identical_run": 0,
        "completions": 0,
        "recent_repo_edit": False,
        "upstream_attempts": [],
        "upstream_confirmed": [],
        "grader_tamper": [],
        "grader_file_changes": [],
        "hidden_reads": [],
        "harness_log_reads": [],
        "history_mining": [],
        "git_object_reads": [],
        "hidden_test_reads": [],
        "proxy_errors": [],
        "total_parse_errors": 0,
        "max_parse_error_streak": 0,
    }
    status.update(overrides)
    return status


def test_grader_tamper_branches_differ_in_eligibility() -> None:
    from evallab.live_watch import WatchThresholds

    trajectory_hit = _bare_status(
        grader_tamper=[{"step": "5", "excerpt": "chmod", "paths": ["verifier/run.py"]}]
    )
    file_backed = _bare_status(
        grader_file_changes=[
            {
                "tamper_evidence": True,
                "path": "/tests/hidden.py",
                "change": "modified",
                "before_sha256": "a" * 64,
                "after_sha256": "b" * 64,
                "baseline_ref": "baseline-w1",
                "line": 3,
            }
        ]
    )
    traj_alert = next(
        a
        for a in evaluate_alerts(trajectory_hit, thresholds=WatchThresholds())
        if a["rule"] == "grader_tamper"
    )
    file_alert = next(
        a
        for a in evaluate_alerts(file_backed, thresholds=WatchThresholds())
        if a["rule"] == "grader_tamper"
    )
    assert "baseline_ref" not in traj_alert
    assert file_alert["baseline_ref"] == "baseline-w1"
    assert stop_eligible_alert(traj_alert)[0] is False
    assert stop_eligible_alert(file_alert) == (True, "eligible")
