"""Deterministic safety boundaries; process/power acceptance runs use a fake OMP."""

from __future__ import annotations

import json
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.fleet import supervisor as runtime
from evallab.fleet.completion import verify
from evallab.fleet.state import Lane, Store
from evallab.fleet.supervisor import Supervisor

ROUTE = "openai-codex/gpt-6.1-sol"
HEAD = "a" * 40


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = Store(tmp_path / "state")
    now = [1000.0]
    supervisor = Supervisor(store, clock=lambda: now[0])
    monkeypatch.setattr(supervisor, "power", lambda active: None)
    monkeypatch.setattr(supervisor, "quota_reason", lambda route: "")
    monkeypatch.setattr(runtime.os, "getpgrp", lambda: 999)
    monkeypatch.setattr(
        runtime,
        "datetime",
        SimpleNamespace(now=lambda tz: datetime(2026, 11, 4, 16, tzinfo=tz)),
    )
    yield supervisor, store, now
    supervisor.verifiers.shutdown(wait=True)
    store.db.close()


def make_lane(tmp_path: Path) -> Lane:
    lane = Lane(
        "test",
        str(tmp_path / "program"),
        ROUTE,
        str(tmp_path / "brief"),
        pid=42,
        pgid=42,
        status="running",
    )
    (lane.directory / "session").mkdir(parents=True)
    return lane


def record(path: Path, route: str, *, error: bool = False) -> None:
    provider, model = route.split("/", 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "provider": provider,
                        "model": model,
                        "stopReason": "error" if error else "stop",
                    },
                }
            )
            + "\n"
        )


@pytest.mark.parametrize(
    "relative",
    ["session/lead.jsonl", "session/lead/Child.jsonl", "helpers/child/session/lead.jsonl"],
)
def test_route_violation_stops_lane_including_error_turns(
    fleet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
):
    supervisor, store, _ = fleet
    lane = make_lane(tmp_path)
    record(lane.directory / relative, "deepseek/deepseek-flash", error=True)
    signals = []
    monkeypatch.setattr(supervisor, "signal", lambda lane, sig: signals.append((lane.name, sig)))
    with store.transaction():
        store.save(lane)
    supervisor.tick()
    saved = store.get(lane.name)
    assert saved.status == "route-violation"
    assert saved.served == {"deepseek/deepseek-flash": 1}
    assert signals == [(lane.name, signal.SIGKILL)]
    assert "deepseek/deepseek-flash" in saved.reason


def test_attestation_snapshots_do_not_double_count_and_discover_new_children(fleet, tmp_path: Path):
    supervisor, _, _ = fleet
    lane = make_lane(tmp_path)
    record(lane.directory / "session/lead.jsonl", ROUTE)
    assert not supervisor.attest(lane)
    assert not supervisor.attest(lane)
    record(lane.directory / "session/lead/Child.jsonl", ROUTE)
    assert not supervisor.attest(lane)
    assert lane.served == {ROUTE: 2}


def test_paid_refusal_precedes_any_process_launch(fleet, tmp_path: Path):
    supervisor, store, _ = fleet
    brief = tmp_path / "brief"
    brief.write_text("work")
    with pytest.raises(ValueError, match="Paid routes require"):
        supervisor.start(tmp_path / "program", "paid", "deepseek/deepseek-flash", brief)
    assert store.lanes() == []


def test_quota_blocks_new_launch_and_pauses_then_continues_existing_lane(
    fleet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    supervisor, store, _ = fleet
    reason = ["quota reaches cap"]
    monkeypatch.setattr(supervisor, "quota_reason", lambda route: reason[0])
    brief = tmp_path / "brief"
    brief.write_text("work")
    with pytest.raises(ValueError, match="quota reaches cap"):
        supervisor.start(tmp_path / "program", "new", ROUTE, brief)
    lane = make_lane(tmp_path)
    signals = []
    monkeypatch.setattr(supervisor, "signal", lambda lane, sig: signals.append(sig))
    monkeypatch.setattr(
        supervisor.launcher, "adopt", lambda *args: SimpleNamespace(poll=lambda: None)
    )
    with store.transaction():
        store.save(lane)
    supervisor.tick()
    assert store.get(lane.name).status == "paused"
    reason[0] = ""
    supervisor.tick()
    assert store.get(lane.name).status == "running"
    assert signals == [signal.SIGSTOP, signal.SIGCONT]


def test_dead_lane_resumes_original_lead_and_exhausts_limit(
    fleet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    supervisor, store, now = fleet
    lane = make_lane(tmp_path)
    lane.max_resumes = 1
    record(lane.directory / "session/lead.jsonl", ROUTE)
    record(lane.directory / "session/lead/Child.jsonl", ROUTE)
    monkeypatch.setattr(supervisor.launcher, "adopt", lambda *args: None)
    monkeypatch.setattr(supervisor, "signal", lambda *args: None)
    resumes = []

    def launch(lane, *, resume=None):
        resumes.append(resume)
        lane.status = "running"

    monkeypatch.setattr(supervisor, "launch", launch)
    with store.transaction():
        store.save(lane)
    supervisor.tick()
    assert store.get(lane.name).status == "recovering"
    now[0] += 44
    supervisor.tick()
    now[0] += 1
    supervisor.tick()
    assert resumes == [lane.directory / "session/lead.jsonl"]
    assert store.get(lane.name).status == "failed"
    assert "resume limit" in store.get(lane.name).reason


def test_sleep_gap_alerts_lane_and_program(fleet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    supervisor, store, now = fleet
    lane = make_lane(tmp_path)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, "Sleep: fixture\nWake: fixture", ""
        ),
    )
    monkeypatch.setattr(
        supervisor.launcher, "adopt", lambda *args: SimpleNamespace(poll=lambda: None)
    )
    with store.transaction():
        store.save(lane)
        store.set_meta("heartbeat", now[0] - 120)
    supervisor.tick()
    assert "ALERT supervisor clock gap 120.0 seconds" in (lane.directory / "STATUS.md").read_text()
    assert "ALERT" in (Path(lane.program) / "ALERTS.md").read_text()
    assert (store.directory / "sleep-1000.log").read_text() == "Sleep: fixture\nWake: fixture"


def test_reused_pid_is_never_signalled(fleet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    supervisor, _, _ = fleet
    lane = make_lane(tmp_path)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, "42 1 42 unrelated-process\n", ""
        ),
    )
    signals = []
    monkeypatch.setattr(runtime.os, "killpg", lambda *args: signals.append(args))
    with pytest.raises(ValueError, match="no longer belongs"):
        supervisor.signal(lane, signal.SIGKILL)
    assert signals == []


def test_stop_includes_detached_helper_and_descendant_groups(
    fleet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    supervisor, _, _ = fleet
    lane = make_lane(tmp_path)
    processes = f"42 1 42 omp --session-dir {lane.directory}/session\n43 42 43 bash\n44 1 44 omp --session-dir {lane.directory}/helpers/h/session\n45 1 45 omp --session-dir /another/lane/session\n"
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, processes, ""),
    )
    signals = []
    monkeypatch.setattr(runtime.os, "killpg", lambda group, sig: signals.append((group, sig)))
    supervisor.signal(lane, signal.SIGKILL)
    assert set(signals) == {(42, signal.SIGKILL), (43, signal.SIGKILL), (44, signal.SIGKILL)}


def completion_run(*, state="MERGED", head=HEAD, dirty=False, failed=False):
    checks = [
        {"name": name, "status": "COMPLETED", "conclusion": "FAILURE" if failed else "SUCCESS"}
        for name in ("quality-required", "typecheck-required")
    ]

    def run(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "view"]:
            output = json.dumps({"state": state, "headRefOid": head, "statusCheckRollup": checks})
        elif argv[:3] == ["git", "status", "--porcelain"]:
            output = " M changed.py" if dirty else ""
        elif argv[:3] == ["git", "rev-parse", "HEAD"]:
            output = HEAD
        elif argv[:3] == ["git", "symbolic-ref", "--short"]:
            output = "topic"
        elif argv[:2] == ["git", "ls-remote"]:
            output = f"{HEAD}\trefs/heads/topic"
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, output, "")

    return run


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"state": "OPEN"}, "not MERGED"),
        ({"head": "b" * 40}, "exact head SHA"),
        ({"dirty": True}, "not clean"),
        ({"failed": True}, "required checks"),
    ],
)
def test_completion_rejects_false_delivery_claims(tmp_path: Path, kwargs, reason):
    lane = make_lane(tmp_path)
    lane.worktree = str(tmp_path / "worktree")
    lane.served = {ROUTE: 1}
    (lane.directory / "RESULT.md").write_text(f"PR #123 is merged at head {HEAD} on {ROUTE}.\n")
    result = verify(lane, run=completion_run(**kwargs))
    assert not result["verified"]
    assert any(reason in item for item in result["reasons"])


def test_completion_requires_route_receipt_and_honors_verifier_hook(tmp_path: Path):
    lane = make_lane(tmp_path)
    lane.worktree = str(tmp_path / "worktree")
    (lane.directory / "RESULT.md").write_text(f"PR #123 is merged at head {HEAD}.\n")
    assert not verify(lane, run=completion_run())["verified"]
    lane.served = {ROUTE: 1}
    assert verify(lane, run=completion_run())["verified"]

    def run(argv, **kwargs):
        if argv[0] == "verify-lane":
            return subprocess.CompletedProcess(argv, 1, "", "receipt arithmetic fails")
        return completion_run()(argv, **kwargs)

    result = verify(lane, run=run, hook=["verify-lane"])
    assert not result["verified"]
    assert "receipt arithmetic fails" in result["reasons"][-1]


def test_partial_turn_survives_restart_then_triggers_route_stop(fleet, tmp_path: Path):
    supervisor, store, _ = fleet
    lane = make_lane(tmp_path)
    journal = lane.directory / "session/lead.jsonl"
    record(journal, ROUTE)
    partial = json.dumps(
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "provider": "deepseek",
                "model": "deepseek-flash",
                "content": "A partial Unicode turn: \u03b1",
            },
        },
        ensure_ascii=False,
    )
    with journal.open("a") as stream:
        stream.write(partial[:40])
    assert not supervisor.attest(lane)
    with store.transaction():
        store.save(lane)
    restored = store.get(lane.name)
    with journal.open("a") as stream:
        stream.write(partial[40:] + "\n")
    assert supervisor.attest(restored)
    assert restored.served == {ROUTE: 1, "deepseek/deepseek-flash": 1}


def test_pmset_timeout_does_not_disable_route_enforcement(
    fleet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    supervisor, store, now = fleet
    lane = make_lane(tmp_path)
    record(lane.directory / "session/lead.jsonl", "deepseek/deepseek-flash")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["pmset", "-g", "log"], 2)

    monkeypatch.setattr(runtime.subprocess, "run", timeout)
    signals = []
    monkeypatch.setattr(supervisor, "signal", lambda lane, sig: signals.append(sig))
    with store.transaction():
        store.save(lane)
        store.set_meta("heartbeat", now[0] - 120)
    supervisor.tick()
    assert store.get(lane.name).status == "route-violation"
    assert signals == [signal.SIGKILL]
    assert "pmset evidence is incomplete" in (store.directory / "sleep-1000.log").read_text()
    assert "ALERT" in (Path(lane.program) / "ALERTS.md").read_text()


def test_cross_repository_pr_url_is_not_checked_in_current_repository(tmp_path: Path):
    lane = make_lane(tmp_path)
    lane.worktree = str(tmp_path / "worktree")
    lane.served = {ROUTE: 1}
    url = "https://github.com/another/repository/pull/123"
    (lane.directory / "RESULT.md").write_text(f"PR {url} MERGED head {HEAD}.\n")
    references = []

    def run(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "view"]:
            references.append(argv[3])
        return completion_run()(argv, **kwargs)

    assert verify(lane, run=run)["verified"]
    assert references == [url]


@pytest.mark.parametrize(
    "line",
    [
        '{"type":"message","message":{"role":"assistant","content":[]}}\n',
        "{malformed complete record}\n",
    ],
)
def test_unverifiable_complete_record_stops_the_lane(
    fleet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str
):
    supervisor, store, _ = fleet
    lane = make_lane(tmp_path)
    (lane.directory / "session/lead.jsonl").write_text(line)
    signals = []
    monkeypatch.setattr(supervisor, "signal", lambda lane, sig: signals.append(sig))
    with store.transaction():
        store.save(lane)
    supervisor.tick()
    assert store.get(lane.name).status == "route-violation"
    assert signals == [signal.SIGKILL]
