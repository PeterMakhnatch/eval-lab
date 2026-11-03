"""Lifecycle tests for the file-access Harbor plugin without Harbor.

``harbor`` is an optional dependency absent from the default unit
environment, so the plugin binds through the trial's own hook registry
(hook keys carrying a ``.value`` name, or the plain name string) instead of
importing ``harbor.trial.hooks``. A scripted fake sandbox answers at exec
granularity: probe manifests, baseline/after snapshots, READY polling,
sealed-chunk listings and downloads, and cleanup. Every lifecycle runs on
one event loop per test (like production), with the poll task dormant
unless the test drives passes explicitly. Assertions target observable
behavior -- live records before agent end, hash diffs, ordering, backstop
cleanup, consolidation -- not mock echoes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab import harbor_file_access
from evallab.file_access import MAX_DOWNLOAD_BYTES, POLL_SECONDS
from evallab.harbor_file_access import FileAccessPlugin

TRIAL_NAME = "task__har180"
CONTROL_BEFORE = hashlib.sha256(b"control-v1").hexdigest()
CONTROL_AFTER = hashlib.sha256(b"control-v2").hexdigest()
TRANSIENT_HASH = hashlib.sha256(b"transient").hexdigest()


class HookKey:
    """Stand-in for a hook-registry key: carries the lifecycle name."""

    def __init__(self, value: str) -> None:
        self.value = value

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, HookKey) and other.value == self.value


class ExecResult:
    def __init__(self, return_code: int, stdout: str = "", stderr: str = "") -> None:
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


def _ok(stdout: str = "") -> ExecResult:
    return ExecResult(0, stdout, "")


def _fail() -> ExecResult:
    return ExecResult(1, "", "")


class FakeEnv:
    """Scripted sandbox: probe/snapshot/chunks/cleanup at exec granularity."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.cleaned = False
        self.ready_polls = 0
        self.ready_after = 0
        self.pid_alive = True
        self.inotifywait: str | None = "/usr/bin/inotifywait"
        self.refs_present = False
        self.probe_history: list[dict[str, Any]] = []
        self.probe_extras: list[dict[str, Any]] = []
        self.snapshot_calls = 0
        #: Per-command override: return an ExecResult to answer, ``None`` to
        #: fall through to the scripted defaults; may raise to simulate a
        #: transport failure.
        self.hook: Callable[[str], ExecResult | None] | None = None
        self.status_missing = False
        self.status_raw: bytes | None = None

    def probe_doc(self) -> dict[str, Any]:
        return {
            "schema": "evallab.file_access_probe/v1",
            "inotifywait": self.inotifywait,
            "repos": [
                {
                    "candidate": "/testbed",
                    "exists": True,
                    "git_dir": "/testbed/.git",
                    "method": "dotgit",
                    "objects": "present",
                    "refs": "present" if self.refs_present else "absent",
                }
            ],
            "mimo_git_hidden": {"candidate": "/var/lib/mimo/git-hidden", "git_dir": None},
            "git_history": self.probe_history,
            "tests": {"path": "/tests", "status": "present"},
            "extras": list(self.probe_extras),
        }

    def baseline_doc(self) -> dict[str, Any]:
        return {
            "schema": "evallab.file_access_snapshot/v1",
            "truncated": False,
            "entries": [
                {
                    "path": "/tests/har180-control.txt",
                    "category": "grader",
                    "type": "file",
                    "size_bytes": 10,
                    "sha256": CONTROL_BEFORE,
                    "hash_status": "complete",
                },
                {
                    "path": "/tests/har180-transient.txt",
                    "category": "grader",
                    "type": "file",
                    "size_bytes": 9,
                    "sha256": TRANSIENT_HASH,
                    "hash_status": "complete",
                },
            ],
        }

    def after_doc(self) -> dict[str, Any]:
        entries = [
            {
                "path": "/tests/har180-control.txt",
                "category": "grader",
                "type": "file",
                "size_bytes": 10,
                "sha256": CONTROL_AFTER,
                "hash_status": "complete",
            },
            {
                "path": "/tests/har180-transient.txt",
                "category": "grader",
                "type": "file",
                "size_bytes": 9,
                "sha256": TRANSIENT_HASH,
                "hash_status": "complete",
            },
            {
                "path": "/tests/har180-created.txt",
                "category": "grader",
                "type": "file",
                "size_bytes": 3,
                "sha256": TRANSIENT_HASH,
                "hash_status": "complete",
            },
        ]
        return {"schema": "evallab.file_access_snapshot/v1", "truncated": False, "entries": entries}

    def _payload(self, remote: str) -> bytes | None:
        if "capture-status.json" in remote:
            if self.status_missing:
                return None
            if self.status_raw is not None:
                return self.status_raw
            return json.dumps(self.status_doc or {}).encode()
        name = remote.rsplit("/", 1)[-1]
        return self.chunk_payloads.get(name)

    async def exec(self, command: str, timeout_sec: int | None = None) -> ExecResult:
        _ = timeout_sec
        self.commands.append(command)
        if self.hook is not None:
            answered = self.hook(command)
            if answered is not None:
                return answered
        if " probe" in command:
            return _ok(json.dumps(self.probe_doc()))
        if " snapshot" in command:
            self.snapshot_calls += 1
            doc = self.baseline_doc() if self.snapshot_calls == 1 else self.after_doc()
            return _ok(json.dumps(doc))
        if "setsid nohup" in command:
            return _ok("12345\n")
        if command.startswith("test -f "):
            self.ready_polls += 1
            return _ok("") if self.ready_polls > self.ready_after else _fail()
        if command.startswith("test -d "):
            return _ok("")
        if command.startswith("ls -1 "):
            return _ok("".join(f"{name}\n" for name in self.chunk_names))
        if command.startswith("kill -0 "):
            return _ok("") if self.pid_alive else _fail()
        if command.startswith("kill "):
            self.pid_alive = False
            return _ok("")
        if command.startswith("rm -rf "):
            self.cleaned = True
            return _ok("")
        if command.startswith("stat ") or command.startswith("wc -c "):
            payload = self._payload(command)
            if payload is None:
                return _fail()
            return _ok(f"{len(payload)}\n")
        if command.startswith("cat "):
            return _ok("")
        raise AssertionError(f"unexpected sandbox command: {command!r}")

    async def upload_file(self, source_path: str, target_path: str) -> None:
        _ = (source_path, target_path)

    async def download_file(self, source_path: str, target_path: str | Path) -> None:
        payload = self._payload(source_path)
        assert payload is not None, f"nothing staged for {source_path!r}"
        Path(target_path).write_bytes(payload)


class FakeTrial:
    def __init__(self, name: str, env: FakeEnv, *, string_keys: bool = False) -> None:
        self.config = SimpleNamespace(trial_name=name)
        self.agent_environment = env
        names = ("start", "agent-start", "agent-end", "verification-start", "end", "cancel")
        self._hooks: dict[Any, list] = {}
        for value in names:
            self._hooks[HookKey(value) if not string_keys else value] = []

    def add_hook(self, key: Any, callback: Any) -> None:
        self._hooks[key].append(callback)


class FakeQueue:
    def __init__(self) -> None:
        self.trials: list[FakeTrial] = []

    def _setup_hooks(self, trial: FakeTrial) -> None:
        self.trials.append(trial)


class FakeJob:
    def __init__(self) -> None:
        self._trial_queue = FakeQueue()


def _event(trials_dir: Path, name: str = TRIAL_NAME) -> SimpleNamespace:
    return SimpleNamespace(
        trial_id="trial-1",
        trial_name=name,
        config=SimpleNamespace(trials_dir=trials_dir),
        result=None,
        task_name="task",
    )


async def _fire(trial: FakeTrial, name: str, event: SimpleNamespace) -> None:
    for key, callbacks in trial._hooks.items():
        if getattr(key, "value", key) == name:
            for callback in list(callbacks):
                await callback(event)


async def _arrange(
    plugin: FileAccessPlugin, trial: FakeTrial, event: SimpleNamespace
) -> FakeJob:
    job = FakeJob()
    await plugin.on_job_start(job)
    job._trial_queue._setup_hooks(trial)
    await _fire(trial, "agent-start", event)
    return job


def _log_lines(trials_dir: Path) -> list[dict[str, Any]]:
    path = trials_dir / TRIAL_NAME / "agent" / "file-access.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _terminal(records: list[dict[str, Any]]) -> dict[str, Any]:
    return [record for record in records if record["kind"] == "coverage"][-1]


def _frame(events: str, path: str) -> bytes:
    return events.encode() + b"\x00" + path.encode() + b"\x00EVALLAB_END\x00"


def _enabled_env() -> FakeEnv:
    env = FakeEnv()
    env.chunk_names = ["c000000.bin", "c000001.bin"]
    env.chunk_payloads = {
        "c000000.bin": _frame("OPEN", "/tests/har180-control.txt"),
        "c000001.bin": (
            _frame("ACCESS", "/testbed/.git/objects/ab/cdef")
            + _frame("OPEN,ISDIR", "/tests")
            + _frame("Q_OVERFLOW", "")
        ),
    }
    env.status_doc = {"state": "complete", "loss_events": True}
    return env


def test_disabled_trial_gets_disabled_coverage(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("EVALLAB_FILE_ACCESS", raising=False)

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        trial = FakeTrial(TRIAL_NAME, FakeEnv())
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        await _fire(trial, "agent-end", event)

    asyncio.run(main())
    records = _log_lines(tmp_path)
    assert len(records) == 1
    assert records[0]["kind"] == "coverage" and records[0]["state"] == "disabled"


def test_live_records_precede_agent_end(tmp_path: Path, monkeypatch: Any) -> None:
    """Sealed chunks publish while the agent runs, not only at teardown."""
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        window = plugin._windows["trial-1"]
        await plugin._poll_pass(window)  # sealed chunks land mid-window
        mid = _log_lines(tmp_path)
        mid_coverages = [record for record in mid if record["kind"] == "coverage"]
        assert len(mid_coverages) == 1 and mid_coverages[0]["state"] == "active"
        assert [record["path"] for record in mid if record["kind"] == "access"] == [
            "/tests/har180-control.txt",
            "/testbed/.git/objects/ab/cdef",
            "/tests",
        ]
        await plugin._poll_pass(window)  # nothing new: no duplicates
        await _fire(trial, "agent-end", event)

    asyncio.run(main())
    records = _log_lines(tmp_path)
    accesses = [record for record in records if record["kind"] == "access"]
    assert [(record["path"], record["category"]) for record in accesses] == [
        ("/tests/har180-control.txt", "grader"),
        ("/testbed/.git/objects/ab/cdef", "git_objects"),
        ("/tests", "grader"),
    ]
    assert accesses[2]["is_directory"] is True
    terminal = _terminal(records)
    assert terminal["state"] == "partial"  # overflow token in the stream


def test_full_window_reports_access_and_hash_change(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        window = plugin._windows["trial-1"]
        assert window.poll_task is not None  # cancellable poll task is kept
        started = _log_lines(tmp_path)
        assert started[-1]["state"] == "active"
        assert started[-1]["watched_paths"] == ["/testbed/.git/objects", "/tests"]
        assert started[-1]["missing_paths"] == ["/testbed/.git/refs"]
        probe_at = next(i for i, c in enumerate(env.commands) if " probe" in c)
        snap_at = next(i for i, c in enumerate(env.commands) if " snapshot" in c)
        launch_at = next(i for i, c in enumerate(env.commands) if "setsid nohup" in c)
        assert probe_at < snap_at < launch_at  # baseline hashes precede the watcher
        baseline = tmp_path / TRIAL_NAME / "evaluator" / "file-access" / "baseline-w1.json"
        assert baseline.is_file()
        assert not (tmp_path / TRIAL_NAME / "agent" / "evaluator").exists()
        await _fire(trial, "agent-end", event)
    asyncio.run(main())
    records = _log_lines(tmp_path)
    changes = {record["path"]: record for record in records if record["kind"] == "file_change"}
    assert changes["/tests/har180-control.txt"]["change"] == "modified"
    assert changes["/tests/har180-control.txt"]["before_sha256"] == CONTROL_BEFORE
    assert changes["/tests/har180-control.txt"]["after_sha256"] == CONTROL_AFTER
    assert changes["/tests/har180-created.txt"]["change"] == "created"
    assert all(
        record["baseline_ref"] == "evaluator/file-access/baseline-w1.json"
        for record in changes.values()
    )
    assert "12345" not in (tmp_path / TRIAL_NAME / "agent" / "file-access.jsonl").read_text()


def test_split_frame_reassembles_across_chunks(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        env.chunk_payloads = {
            "c000000.bin": b"OPEN\x00/tests/har180-con",
            "c000001.bin": b"trol.txt\x00EVALLAB_END\x00",
        }
        env.chunk_names = ["c000000.bin"]
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        window = plugin._windows["trial-1"]
        await plugin._poll_pass(window)
        assert [r for r in _log_lines(tmp_path) if r["kind"] == "access"] == []
        env.chunk_names.append("c000001.bin")
        await plugin._poll_pass(window)
        accesses = [r for r in _log_lines(tmp_path) if r["kind"] == "access"]
        assert [record["path"] for record in accesses] == ["/tests/har180-control.txt"]
        await _fire(trial, "agent-end", event)

    asyncio.run(main())
    assert _terminal(_log_lines(tmp_path))["state"] == "stopped"


def test_missing_inotifywait_is_unavailable_not_clean(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = FakeEnv()
        env.inotifywait = None
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        await _fire(trial, "agent-end", event)

    asyncio.run(main())
    records = _log_lines(tmp_path)
    assert len(records) == 1
    assert records[0]["state"] == "unavailable"
    assert "inotifywait" in records[0]["reason"]


def test_observer_death_before_ready_cleans_up(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    cleaned: list[bool] = []

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = FakeEnv()
        env.ready_after = 10_000
        env.pid_alive = False  # liveness check fails while READY never appears
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        cleaned.append(env.cleaned)

    asyncio.run(main())
    records = _log_lines(tmp_path)
    assert records[-1]["state"] == "unavailable"
    assert "died before ready" in records[-1]["reason"]
    assert cleaned == [True]


def test_cancel_backstop_closes_window_and_leaves_no_task(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    outcome: dict[str, Any] = {}

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        task = plugin._windows["trial-1"].poll_task
        await _fire(trial, "cancel", event)
        outcome["done"] = task.done() if task is not None else None
        outcome["popped"] = "trial-1" not in plugin._windows
        outcome["cleaned"] = env.cleaned
        before = len(_log_lines(tmp_path))
        await _fire(trial, "agent-end", event)  # already closed: no duplicate window
        outcome["before"] = before
        outcome["after"] = len(_log_lines(tmp_path))

    asyncio.run(main())
    assert outcome["done"] is True
    assert outcome["popped"] is True
    assert outcome["cleaned"] is True
    assert outcome["before"] == outcome["after"]
    records = _log_lines(tmp_path)
    assert "trial ended" in _terminal(records)["reason"]


def test_verification_backstop_stops_before_verifier(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        await _fire(trial, "verification-start", event)

    asyncio.run(main())
    records = _log_lines(tmp_path)
    assert "verification started" in _terminal(records)["reason"]


def test_invalid_targets_leave_capture_unavailable(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    monkeypatch.setenv("EVALLAB_FILE_ACCESS_PATHS", "not-json")

    async def main() -> None:
        plugin = FileAccessPlugin()
        trial = FakeTrial(TRIAL_NAME, _enabled_env())
        await _arrange(plugin, trial, _event(tmp_path))
        await _fire(trial, "agent-end", _event(tmp_path))

    asyncio.run(main())
    records = _log_lines(tmp_path)
    assert _terminal(records)["state"] == "unavailable"
    assert not any(record["kind"] == "access" for record in records)
    assert not (tmp_path / TRIAL_NAME / "evaluator" / "file-access").exists()


def test_discovery_off_watches_only_declared_targets(tmp_path: Path, monkeypatch: Any) -> None:
    """Discovery off ignores probe repos and the default tests dir."""
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    monkeypatch.setenv("EVALLAB_FILE_ACCESS_DISCOVERY", "off")
    monkeypatch.setenv(
        "EVALLAB_FILE_ACCESS_PATHS", json.dumps({"grader": ["/honeypot/blob"]})
    )

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        env.probe_extras = [{"path": "/honeypot/blob", "exists": True, "is_dir": False}]
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        await _fire(trial, "agent-end", event)

    asyncio.run(main())
    records = _log_lines(tmp_path)
    started = [record for record in records if record["kind"] == "coverage"][0]
    assert started["state"] == "active"
    assert started["watched_paths"] == ["/honeypot/blob"]
    assert "EVALLAB_FILE_ACCESS_DISCOVERY=off" in started["reason"]
    discovery = json.loads(
        (tmp_path / TRIAL_NAME / "evaluator" / "file-access" / "discovery-w1.json")
        .read_text(encoding="utf-8")
    )
    assert discovery["discovery"] == "off"
    assert discovery["watched"] == ["/honeypot/blob"]


def test_discovery_on_keeps_default_targets(tmp_path: Path, monkeypatch: Any) -> None:
    """Explicit on matches today's discovery behavior."""
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    monkeypatch.setenv("EVALLAB_FILE_ACCESS_DISCOVERY", "on")

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        await _fire(trial, "agent-end", event)

    asyncio.run(main())
    records = _log_lines(tmp_path)
    started = [record for record in records if record["kind"] == "coverage"][0]
    assert started["state"] == "active"
    assert started["watched_paths"] == ["/testbed/.git/objects", "/tests"]
    discovery = json.loads(
        (tmp_path / TRIAL_NAME / "evaluator" / "file-access" / "discovery-w1.json")
        .read_text(encoding="utf-8")
    )
    assert discovery["discovery"] == "on"
    assert discovery["watched"] == ["/testbed/.git/objects", "/tests"]


def test_discovery_bad_value_leaves_capture_unavailable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Unknown discovery values fail closed, like bad paths JSON."""
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    monkeypatch.setenv("EVALLAB_FILE_ACCESS_DISCOVERY", "bogus")

    async def main() -> None:
        plugin = FileAccessPlugin()
        trial = FakeTrial(TRIAL_NAME, _enabled_env())
        await _arrange(plugin, trial, _event(tmp_path))
        await _fire(trial, "agent-end", _event(tmp_path))

    asyncio.run(main())
    records = _log_lines(tmp_path)
    assert _terminal(records)["state"] == "unavailable"
    assert "EVALLAB_FILE_ACCESS_DISCOVERY" in _terminal(records)["reason"]
    assert not any(record["kind"] == "access" for record in records)
    assert not (tmp_path / TRIAL_NAME / "evaluator" / "file-access").exists()


def test_discovery_off_without_declared_paths_refuses(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Off with no declared paths must not silently watch nothing."""
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    monkeypatch.setenv("EVALLAB_FILE_ACCESS_DISCOVERY", "off")
    monkeypatch.delenv("EVALLAB_FILE_ACCESS_PATHS", raising=False)

    async def main() -> None:
        plugin = FileAccessPlugin()
        trial = FakeTrial(TRIAL_NAME, _enabled_env())
        await _arrange(plugin, trial, _event(tmp_path))
        await _fire(trial, "agent-end", _event(tmp_path))

    asyncio.run(main())
    records = _log_lines(tmp_path)
    assert _terminal(records)["state"] == "unavailable"
    assert "EVALLAB_FILE_ACCESS_DISCOVERY=off" in _terminal(records)["reason"]
    assert not any(record["kind"] == "access" for record in records)
    assert not (tmp_path / TRIAL_NAME / "evaluator" / "file-access").exists()


def test_windows_are_monotone_per_trial(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        await _fire(trial, "agent-end", event)
        await _fire(trial, "agent-start", event)
        await _fire(trial, "agent-end", event)

    asyncio.run(main())
    coverages = [record for record in _log_lines(tmp_path) if record["kind"] == "coverage"]
    assert [record["window_id"] for record in coverages] == [1, 1, 2, 2]
    assert (tmp_path / TRIAL_NAME / "evaluator" / "file-access" / "baseline-w2.json").is_file()


def test_job_end_sweeps_open_window(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        job = FakeJob()
        await plugin.on_job_start(job)
        job._trial_queue._setup_hooks(trial)
        await _fire(trial, "agent-start", event)
        await plugin.on_job_end(None)

    asyncio.run(main())
    assert "job ended" in _terminal(_log_lines(tmp_path))["reason"]


def test_end_consolidates_relocated_step_fragments(tmp_path: Path, monkeypatch: Any) -> None:
    """Multi-step Harbor moves root agent/ into steps/N/agent/ per step."""
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        await _fire(trial, "agent-end", event)
        trial_dir = tmp_path / TRIAL_NAME
        root_log = trial_dir / "agent" / "file-access.jsonl"
        # Harbor archives the step: move the window-1 log aside (move mode).
        step_log = trial_dir / "steps" / "s1" / "agent" / "file-access.jsonl"
        step_log.parent.mkdir(parents=True)
        step_log.write_bytes(root_log.read_bytes())
        root_log.unlink()
        await _fire(trial, "agent-start", event)
        await _fire(trial, "agent-end", event)
        await _fire(trial, "end", event)

    asyncio.run(main())
    records = _log_lines(tmp_path)
    windows = sorted({record.get("window_id", 0) for record in records} - {None})
    assert 1 in windows and 2 in windows  # both windows retained in the root log
    assert _terminal(records)["state"] == "stopped"
    assert "consolidated 1 relocated step fragment" in _terminal(records)["reason"]


def test_end_consolidation_dedupes_copied_fragments(tmp_path: Path, monkeypatch: Any) -> None:
    """Resume steps copy rather than move: identical lines must not double."""
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        await _fire(trial, "agent-end", event)
        trial_dir = tmp_path / TRIAL_NAME
        root_log = trial_dir / "agent" / "file-access.jsonl"
        step_log = trial_dir / "steps" / "s1" / "agent" / "file-access.jsonl"
        step_log.parent.mkdir(parents=True)
        step_log.write_bytes(root_log.read_bytes())  # copy mode: root keeps its copy
        before = len(root_log.read_text(encoding="utf-8").splitlines())
        await _fire(trial, "end", event)
        after = _log_lines(tmp_path)
        access_lines = [record for record in after if record["kind"] == "access"]
        assert len(after) == before + 1  # only the consolidation coverage is new
        assert len(access_lines) == len(
            {json.dumps(record, sort_keys=True) for record in access_lines}
        )

    asyncio.run(main())


def test_binding_needs_no_harbor_enum(tmp_path: Path, monkeypatch: Any) -> None:
    """Plain string registry keys bind too: Harbor is never imported."""
    monkeypatch.delenv("EVALLAB_FILE_ACCESS", raising=False)

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        trial = FakeTrial(TRIAL_NAME, FakeEnv(), string_keys=True)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        assert all(trial._hooks[key] for key in trial._hooks if key != "start")

    asyncio.run(main())
    assert _log_lines(tmp_path)[-1]["state"] == "disabled"


def test_cancelling_agent_end_waits_for_observer_cleanup(tmp_path: Path, monkeypatch: Any) -> None:
    import pytest

    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        stopping = asyncio.Event()
        allow_stop = asyncio.Event()
        execute = env.exec

        async def delayed_stop(command: str, timeout_sec: int | None = None):
            if command.startswith("kill -TERM"):
                stopping.set()
                await allow_stop.wait()
            return await execute(command, timeout_sec)

        env.exec = delayed_stop
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        poll = plugin._windows["trial-1"].poll_task
        closing = asyncio.create_task(_fire(trial, "agent-end", event))
        await stopping.wait()
        closing.cancel()
        allow_stop.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert env.cleaned and not env.pid_alive
        assert poll.done()

    asyncio.run(main())


def test_missing_chunk_cannot_fabricate_a_different_path(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        env.chunk_names = ["c000000.bin", "c000002.bin"]
        env.chunk_payloads = {
            "c000000.bin": b"OPEN\x00/tests/",
            "c000002.bin": b"hidden.txt\x00EVALLAB_END\x00",
        }
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        window = plugin._windows["trial-1"]
        try:
            await plugin._poll_pass(window)
            assert not any(r["kind"] == "access" for r in _log_lines(tmp_path))
            env.chunk_names.insert(1, "c000001.bin")
            await plugin._poll_pass(window)
            assert not any(r["kind"] == "access" for r in _log_lines(tmp_path))
            env.chunk_payloads["c000001.bin"] = b"missing-"
            await plugin._poll_pass(window)
            accesses = [r for r in _log_lines(tmp_path) if r["kind"] == "access"]
            assert [r["path"] for r in accesses] == ["/tests/missing-hidden.txt"]
        finally:
            await _fire(trial, "agent-end", event)

    asyncio.run(main())


def _history_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "repository": "/testbed",
        "git_dir": "/testbed/.git",
        "base_commit": "a" * 40,
        "base_source": "mimo_base",
        "ancestor_commits": ["a" * 40, "b" * 40],
        "complete": True,
        "reason": None,
    }
    entry.update(overrides)
    return entry


def _baseline_doc(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / TRIAL_NAME / "evaluator" / "file-access" / "baseline-w1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_baseline_persists_probe_git_history(tmp_path: Path, monkeypatch: Any) -> None:
    """Pre-agent ancestry lands in the evaluator baseline before the watcher starts."""
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        env.probe_history = [_history_entry()]
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        probe_at = next(i for i, c in enumerate(env.commands) if " probe" in c)
        snap_at = next(i for i, c in enumerate(env.commands) if " snapshot" in c)
        assert probe_at < snap_at  # ancestry is probed before the agent runs
        assert _baseline_doc(tmp_path)["git_history"] == [_history_entry()]
        await _fire(trial, "agent-end", event)

    asyncio.run(main())
    # The after-agent snapshot closes the window but never rewrites ancestry.
    assert _baseline_doc(tmp_path)["git_history"] == [_history_entry()]


def test_baseline_preserves_initial_host_ancestry(tmp_path: Path, monkeypatch: Any) -> None:
    """A re-probe must not overwrite the initial evaluator-owned ancestry."""
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    snapshot_dir = tmp_path / TRIAL_NAME / "evaluator" / "file-access"
    snapshot_dir.mkdir(parents=True)
    initial = [_history_entry(base_commit="c" * 40, ancestor_commits=["c" * 40])]
    (snapshot_dir / "baseline-w1.json").write_text(
        json.dumps({"schema": "evallab.file_access_snapshot/v1", "git_history": initial}),
        encoding="utf-8",
    )

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        env.probe_history = [_history_entry()]
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        await _fire(trial, "agent-end", event)

    asyncio.run(main())
    assert _baseline_doc(tmp_path)["git_history"] == initial


def test_incomplete_graph_is_not_promoted(tmp_path: Path, monkeypatch: Any) -> None:
    """Partial ancestry persists verbatim: incomplete stays incomplete."""
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    partial = _history_entry(
        base_source="pre_agent_head",
        ancestor_commits=["a" * 40],
        complete=False,
        reason="shallow",
    )

    async def main() -> None:
        plugin = FileAccessPlugin(poll_seconds=3600)
        env = _enabled_env()
        env.probe_history = [partial]
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        await _fire(trial, "agent-end", event)

    asyncio.run(main())
    assert _baseline_doc(tmp_path)["git_history"] == [partial]


# ---------------------------------------------------------------------------
# Failure paths: setup or capture failures must never look clean.
#
# The module promises every trial a ``coverage`` record and that a failed
# probe/snapshot/launch is ``unavailable`` while lost or doubtful observation
# is ``partial``; only an unbroken capture may end ``stopped``.
# ---------------------------------------------------------------------------


def _clean_env() -> FakeEnv:
    """One sealed chunk, complete status, no loss tokens: a truly clean capture."""
    env = _enabled_env()
    env.chunk_names = ["c000000.bin"]
    env.chunk_payloads = {"c000000.bin": _frame("OPEN", "/tests/har180-control.txt")}
    return env


def _answer(
    env: FakeEnv, needle: str | tuple[str, ...], result: ExecResult | Callable[[], ExecResult]
) -> None:
    """Answer sandbox commands containing every needle, chaining earlier hooks."""
    needles = (needle,) if isinstance(needle, str) else needle
    previous = env.hook

    def hook(command: str) -> ExecResult | None:
        if all(item in command for item in needles):
            return result() if callable(result) else result
        return previous(command) if previous is not None else None

    env.hook = hook


def _script_snapshots(
    env: FakeEnv, *, baseline: ExecResult | None = None, after: ExecResult | None = None
) -> None:
    """Override the baseline and/or after snapshot; the other keeps its scripted doc."""
    calls = {"count": 0}
    previous = env.hook

    def hook(command: str) -> ExecResult | None:
        if " snapshot" not in command:
            return previous(command) if previous is not None else None
        calls["count"] += 1
        if calls["count"] == 1:
            return baseline or _ok(json.dumps(env.baseline_doc()))
        return after or _ok(json.dumps(env.after_doc()))

    env.hook = hook


def _drive(
    tmp_path: Path,
    monkeypatch: Any,
    env: FakeEnv,
    *,
    close: str = "agent-end",
    poll_seconds: float = 3600,
    configure: Callable[[FileAccessPlugin], None] | None = None,
    between: Callable[[FileAccessPlugin], Any] | None = None,
) -> tuple[FileAccessPlugin, list[dict[str, Any]]]:
    """Run one enabled window end to end and return the plugin and the host log."""
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    monkeypatch.delenv("EVALLAB_FILE_ACCESS_PATHS", raising=False)
    monkeypatch.delenv("EVALLAB_FILE_ACCESS_DISCOVERY", raising=False)

    async def main() -> FileAccessPlugin:
        plugin = FileAccessPlugin(poll_seconds=poll_seconds)
        if configure is not None:
            configure(plugin)
        trial = FakeTrial(TRIAL_NAME, env)
        event = _event(tmp_path)
        await _arrange(plugin, trial, event)
        if between is not None:
            await between(plugin)
        await _fire(trial, close, event)
        return plugin

    plugin = asyncio.run(main())
    return plugin, _log_lines(tmp_path)


def _accesses(records: list[dict[str, Any]]) -> list[str]:
    return [record["path"] for record in records if record["kind"] == "access"]


def _changes(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if record["kind"] == "file_change"]


def _baseline_file(tmp_path: Path) -> Path:
    return tmp_path / TRIAL_NAME / "evaluator" / "file-access" / "baseline-w1.json"


def test_unbroken_capture_is_the_only_stopped_window(tmp_path: Path, monkeypatch: Any) -> None:
    """Control for the failure tests: they differ from this run in one fault each."""
    _, records = _drive(tmp_path, monkeypatch, _clean_env())
    terminal = _terminal(records)
    assert terminal["state"] == "stopped"
    assert "1 access event(s) recorded from 1 chunk(s)" in terminal["reason"]
    assert _accesses(records) == ["/tests/har180-control.txt"]


# -- capture status fetch ------------------------------------------------------


def test_missing_capture_status_is_partial(tmp_path: Path, monkeypatch: Any) -> None:
    env = _clean_env()
    env.status_missing = True
    _, records = _drive(tmp_path, monkeypatch, env)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert "capture status missing" in terminal["reason"]
    assert _accesses(records) == ["/tests/har180-control.txt"]  # earlier observations kept


def test_oversized_capture_status_is_not_downloaded(tmp_path: Path, monkeypatch: Any) -> None:
    env = _clean_env()
    _answer(env, ("stat -c", "capture-status.json"), _ok(f"{MAX_DOWNLOAD_BYTES + 1}\n"))
    _, records = _drive(tmp_path, monkeypatch, env)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert f"over the {MAX_DOWNLOAD_BYTES} bound" in terminal["reason"]


@pytest.mark.parametrize("raw", [b"not json", b"\xff\xfe\x00", b"[1, 2]", b"null"])
def test_non_document_capture_status_is_partial(
    tmp_path: Path, monkeypatch: Any, raw: bytes
) -> None:
    env = _clean_env()
    env.status_raw = raw
    _, records = _drive(tmp_path, monkeypatch, env)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert "capture status is not a JSON document" in terminal["reason"]


def test_status_reported_watch_loss_and_truncation_degrade(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = _clean_env()
    env.status_doc = {
        "state": "complete",
        "loss_events": "fallback-unverified",
        "watch_errors": ["cannot watch /x"],
        "truncated": True,
        "stdbuf": "unavailable",
        "stderr_tail": "inotify: boom",
    }
    _, records = _drive(tmp_path, monkeypatch, env)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    for fragment in (
        "watch: cannot watch /x",
        "sandbox stream hit its byte bound; watching stopped early",
        "stdbuf unavailable: live chunks may lag buffer fills",
        "observer stderr: inotify: boom",
    ):
        assert fragment in terminal["reason"]


def test_unverified_loss_events_alone_degrade(tmp_path: Path, monkeypatch: Any) -> None:
    env = _clean_env()
    env.status_doc = {"state": "complete", "loss_events": "fallback-unverified"}
    _, records = _drive(tmp_path, monkeypatch, env)
    assert _terminal(records)["state"] == "partial"


def test_error_status_with_observations_is_partial(tmp_path: Path, monkeypatch: Any) -> None:
    env = _clean_env()
    env.status_doc = {"state": "error"}
    _, records = _drive(tmp_path, monkeypatch, env)
    assert _terminal(records)["state"] == "partial"
    assert _accesses(records) == ["/tests/har180-control.txt"]


def test_error_status_with_nothing_observed_is_unavailable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = _clean_env()
    env.chunk_names = []
    env.chunk_payloads = {}
    env.status_doc = {"state": "error"}
    _script_snapshots(env, after=_fail())  # no after image: nothing to diff either
    _, records = _drive(tmp_path, monkeypatch, env)
    assert _terminal(records)["state"] == "unavailable"
    assert not _accesses(records) and not _changes(records)


# -- final drain -----------------------------------------------------------------


def _listing_loss() -> tuple[FakeEnv, str]:
    env = _clean_env()
    _answer(env, "ls -1 ", _fail())  # ``test -d`` still succeeds: the listing is broken
    return env, "final chunk listing failed: chunk listing failed while the chunks directory exists"


def _chunk_loss() -> tuple[FakeEnv, str]:
    env = _clean_env()
    env.chunk_names = ["c000000.bin", "c000001.bin"]  # second chunk cannot be downloaded
    return env, "final chunk c000001.bin missed"


@pytest.mark.parametrize("scenario", [_listing_loss, _chunk_loss], ids=["listing", "chunk"])
def test_final_drain_loss_is_recorded_in_the_coverage_reason(
    tmp_path: Path, monkeypatch: Any, scenario: Callable[[], tuple[FakeEnv, str]]
) -> None:
    env, fragment = scenario()
    _, records = _drive(tmp_path, monkeypatch, env)
    assert fragment in _terminal(records)["reason"]


@pytest.mark.parametrize("scenario", [_listing_loss, _chunk_loss], ids=["listing", "chunk"])
def test_final_drain_loss_is_never_clean(
    tmp_path: Path, monkeypatch: Any, scenario: Callable[[], tuple[FakeEnv, str]]
) -> None:
    env, _ = scenario()
    _, records = _drive(tmp_path, monkeypatch, env)
    assert _terminal(records)["state"] in {"partial", "unavailable"}


def test_torn_tail_is_counted_not_fabricated(tmp_path: Path, monkeypatch: Any) -> None:
    env = _clean_env()
    env.chunk_payloads = {
        "c000000.bin": _frame("OPEN", "/tests/har180-control.txt")
        + b"OPEN\x00/tests/har180-con"  # the observer died mid-pathname
    }
    _, records = _drive(tmp_path, monkeypatch, env)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert "trailing stream truncated; rejected, not fabricated" in terminal["reason"]
    assert _accesses(records) == ["/tests/har180-control.txt"]


def test_rejected_frames_degrade_and_are_never_recorded(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = _clean_env()
    env.chunk_payloads = {
        "c000000.bin": (
            _frame("OPEN", "/tests/har180-control.txt")
            + _frame("OPEN", "/etc/shadow")  # outside every protected root
            + b"junk\x00EVALLAB_END\x00"  # not an event/path pair
        )
    }
    _, records = _drive(tmp_path, monkeypatch, env)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert "2 invalid frame(s) rejected" in terminal["reason"]
    assert _accesses(records) == ["/tests/har180-control.txt"]


@pytest.mark.parametrize(
    ("token", "fragment"),
    [
        ("IGNORED", "a watch was removed during capture (ignored)"),
        ("UNMOUNT", "a watched filesystem was unmounted during capture (unmount)"),
        ("Q_OVERFLOW", "queue overflow observed; some events were lost"),
    ],
)
def test_loss_tokens_in_the_stream_degrade(
    tmp_path: Path, monkeypatch: Any, token: str, fragment: str
) -> None:
    env = _clean_env()
    env.chunk_payloads = {
        "c000000.bin": _frame("OPEN", "/tests/har180-control.txt") + _frame(token, "")
    }
    _, records = _drive(tmp_path, monkeypatch, env)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert fragment in terminal["reason"]


def test_status_overflow_flag_degrades(tmp_path: Path, monkeypatch: Any) -> None:
    env = _clean_env()
    env.status_doc = {"state": "complete", "overflow_seen": True}
    _, records = _drive(tmp_path, monkeypatch, env)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert "queue overflow observed" in terminal["reason"]


def test_event_record_bound_is_noted_and_degrades(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(harbor_file_access, "MAX_SAVED_EVENTS", 1)
    env = _clean_env()
    env.chunk_payloads = {
        "c000000.bin": _frame("OPEN", "/tests/har180-control.txt")
        + _frame("OPEN", "/tests/har180-transient.txt")
    }
    _, records = _drive(tmp_path, monkeypatch, env)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert "event record bound 1 hit; further events counted, not stored" in terminal["reason"]
    assert _accesses(records) == ["/tests/har180-control.txt"]


# -- snapshots -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "fragment"),
    [
        (ExecResult(2, "", "disk full"), "baseline snapshot failed: disk full"),
        (_ok("not json"), "baseline snapshot returned invalid JSON"),
        (_ok("[]"), "baseline snapshot returned an unexpected document"),
        (_ok(json.dumps({"schema": "other/v1"})), "baseline snapshot returned an unexpected document"),
    ],
)
def test_baseline_snapshot_failure_is_unavailable_and_never_launches(
    tmp_path: Path, monkeypatch: Any, result: ExecResult, fragment: str
) -> None:
    env = _clean_env()
    _script_snapshots(env, baseline=result)
    _, records = _drive(tmp_path, monkeypatch, env)
    assert len(records) == 1
    assert records[0]["state"] == "unavailable"
    assert fragment in records[0]["reason"]
    assert env.cleaned
    assert not any("setsid nohup" in command for command in env.commands)
    assert not _baseline_file(tmp_path).exists()


def test_truncated_baseline_snapshot_makes_changes_unknown(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = _clean_env()
    manifest = env.baseline_doc()
    manifest["truncated"] = True
    _script_snapshots(env, baseline=_ok(json.dumps(manifest)))
    _, records = _drive(tmp_path, monkeypatch, env)
    assert [record["state"] for record in records if record["kind"] == "coverage"] == [
        "active",
        "partial",
    ]
    assert "baseline snapshot truncated; file changes unknown" in _terminal(records)["reason"]
    assert not _changes(records)  # no change report from an incomplete baseline


@pytest.mark.parametrize(
    ("kind", "fragment"),
    [
        ("fail", "after snapshot failed: sandbox after snapshot failed: no output"),
        ("junk", "after snapshot failed: sandbox after snapshot returned invalid JSON"),
        ("truncated", "after snapshot hit the entry bound"),
    ],
)
def test_after_snapshot_failure_is_partial_without_fabricated_changes(
    tmp_path: Path, monkeypatch: Any, kind: str, fragment: str
) -> None:
    env = _clean_env()
    truncated = dict(env.after_doc(), truncated=True)
    result = {
        "fail": _fail(),
        "junk": _ok("junk"),
        "truncated": _ok(json.dumps(truncated)),
    }[kind]
    _script_snapshots(env, after=result)
    _, records = _drive(tmp_path, monkeypatch, env)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert fragment in terminal["reason"]
    assert "after snapshot unavailable; file changes unknown" in terminal["reason"]
    assert not _changes(records)  # never "every baseline file was deleted"
    assert _accesses(records) == ["/tests/har180-control.txt"]


def test_git_storage_changes_are_not_reported_as_content_changes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = _clean_env()
    after = env.after_doc()
    after["entries"].append(
        {
            "path": "/testbed/.git/objects/zz",
            "category": "git_objects",
            "type": "file",
            "size_bytes": 1,
            "sha256": TRANSIENT_HASH,
            "hash_status": "complete",
        }
    )
    _script_snapshots(env, after=_ok(json.dumps(after)))
    _, records = _drive(tmp_path, monkeypatch, env)
    assert {record["path"] for record in _changes(records)} == {
        "/tests/har180-control.txt",
        "/tests/har180-created.txt",
    }


def test_corrupt_prior_baseline_falls_back_to_probe_ancestry(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _baseline_file(tmp_path).parent.mkdir(parents=True)
    _baseline_file(tmp_path).write_text("{not json", encoding="utf-8")
    env = _clean_env()
    env.probe_history = [_history_entry()]
    _, records = _drive(tmp_path, monkeypatch, env)
    assert _terminal(records)["state"] == "stopped"
    assert json.loads(_baseline_file(tmp_path).read_text(encoding="utf-8"))["git_history"] == [
        _history_entry()
    ]


# -- probe, upload and observer launch ------------------------------------------


@pytest.mark.parametrize(
    ("result", "fragment"),
    [
        (ExecResult(127, "", "python3: not found"), "sandbox probe failed: python3: not found"),
        (ExecResult(1, "", ""), "sandbox probe failed: no output"),
        (_ok("not json"), "sandbox probe returned invalid JSON"),
        (_ok("[]"), "sandbox probe returned an unexpected document"),
        (_ok(json.dumps({"schema": "wrong"})), "sandbox probe returned an unexpected document"),
    ],
)
def test_probe_failure_is_unavailable_before_any_snapshot(
    tmp_path: Path, monkeypatch: Any, result: ExecResult, fragment: str
) -> None:
    env = _clean_env()
    _answer(env, " probe", result)
    _, records = _drive(tmp_path, monkeypatch, env)
    assert len(records) == 1
    assert records[0]["state"] == "unavailable"
    assert fragment in records[0]["reason"]
    assert env.cleaned
    assert not any(" snapshot" in command for command in env.commands)


def test_no_protected_targets_is_unavailable(tmp_path: Path, monkeypatch: Any) -> None:
    env = _clean_env()
    doc = env.probe_doc()
    doc["repos"] = []
    doc["tests"] = {"path": "/tests", "status": "absent"}
    _answer(env, " probe", _ok(json.dumps(doc)))
    _, records = _drive(tmp_path, monkeypatch, env)
    assert len(records) == 1
    assert records[0]["state"] == "unavailable"
    assert "no protected targets present (missing: /tests)" in records[0]["reason"]
    assert not any(" snapshot" in command for command in env.commands)


def test_probe_with_partial_repo_data_watches_only_what_exists(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = _clean_env()
    doc = env.probe_doc()
    doc["repos"] = [{"candidate": "/nowhere", "exists": False, "git_dir": None}, *doc["repos"]]
    doc["mimo_git_hidden"] = {
        "candidate": "/var/lib/mimo/git-hidden",
        "git_dir": "/var/lib/mimo/git-hidden",
        "objects": "present",
        "refs": "absent",
    }
    doc["git_history"] = "oops"  # malformed ancestry must not break or leak into the baseline
    _answer(env, " probe", _ok(json.dumps(doc)))
    _, records = _drive(tmp_path, monkeypatch, env)
    started = [record for record in records if record["kind"] == "coverage"][0]
    assert started["state"] == "active"
    assert started["watched_paths"] == [
        "/testbed/.git/objects",
        "/tests",
        "/var/lib/mimo/git-hidden/objects",
    ]
    assert started["missing_paths"] == [
        "/testbed/.git/refs",
        "/var/lib/mimo/git-hidden/refs",
    ]
    assert json.loads(_baseline_file(tmp_path).read_text(encoding="utf-8"))["git_history"] == []


def test_missing_sandbox_helper_is_unavailable(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        FileAccessPlugin, "_helper_source", lambda self: tmp_path / "absent-helper.py"
    )
    env = _clean_env()
    _, records = _drive(tmp_path, monkeypatch, env)
    assert len(records) == 1
    assert records[0]["state"] == "unavailable"
    assert "sandbox helper is missing from the install" in records[0]["reason"]
    assert not any(" probe" in command for command in env.commands)


@pytest.mark.parametrize(
    ("result", "fragment"),
    [
        (ExecResult(1, "", "setsid: not found"), "observer launch failed: setsid: not found"),
        (_ok("started\n"), "observer launch returned no control PID"),
        (_ok(""), "observer launch returned no control PID"),
        (_ok("0\n"), "observer launch returned an invalid control PID"),
        (_ok("-7\n"), "observer launch returned an invalid control PID"),
    ],
)
def test_observer_launch_failure_is_unavailable(
    tmp_path: Path, monkeypatch: Any, result: ExecResult, fragment: str
) -> None:
    env = _clean_env()
    _answer(env, "setsid nohup", result)
    _, records = _drive(tmp_path, monkeypatch, env)
    assert len(records) == 1
    assert records[0]["state"] == "unavailable"
    assert fragment in records[0]["reason"]
    assert env.cleaned


def test_observer_that_never_becomes_ready_is_unavailable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(harbor_file_access, "READY_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(harbor_file_access, "READY_POLL_SEC", 0.005)
    env = _clean_env()
    env.ready_after = 10**9  # READY never appears while the observer stays alive
    _, records = _drive(tmp_path, monkeypatch, env)
    assert len(records) == 1
    assert records[0]["state"] == "unavailable"
    assert "observer was not ready within" in records[0]["reason"]
    assert env.cleaned


def test_observer_that_becomes_ready_late_still_captures(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(harbor_file_access, "READY_POLL_SEC", 0.005)
    env = _clean_env()
    env.ready_after = 2
    _, records = _drive(tmp_path, monkeypatch, env)
    assert env.ready_polls == 3
    assert _terminal(records)["state"] == "stopped"


# -- stopping the observer ---------------------------------------------------------


def test_observer_ignoring_sigterm_is_killed_and_not_clean(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(harbor_file_access, "STOP_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(harbor_file_access, "READY_POLL_SEC", 0.005)
    env = _clean_env()
    env.status_doc = {"state": "running"}  # a killed observer never sealed its status
    _answer(env, "kill -TERM", _ok())  # SIGTERM has no effect: the pid stays alive

    def kill() -> ExecResult:
        env.pid_alive = False
        return _ok()

    _answer(env, "kill -KILL", kill)
    _, records = _drive(tmp_path, monkeypatch, env)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert "observer ignored SIGTERM and was killed" in terminal["reason"]
    assert any("kill -KILL" in command for command in env.commands)


def test_window_without_control_pid_is_noted_and_not_clean(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = _clean_env()
    env.status_doc = {"state": "running"}  # the observer was never stopped or sealed

    async def between(plugin: FileAccessPlugin) -> None:
        window = plugin._windows["trial-1"]
        window.control_pid = None
        task, window.poll_task = window.poll_task, None  # no poll task left to cancel
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    _, records = _drive(tmp_path, monkeypatch, env, between=between)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert "no control PID recorded" in terminal["reason"]
    assert not any(command.startswith("kill -TERM") for command in env.commands)


@pytest.mark.parametrize(
    ("poll_first", "state"), [(False, "unavailable"), (True, "partial")]
)
def test_teardown_exception_reports_unavailable_or_partial(
    tmp_path: Path, monkeypatch: Any, poll_first: bool, state: str
) -> None:
    """A sandbox that dies at teardown: nothing seen is unavailable, something is partial."""
    env = _clean_env()

    def transport_lost() -> ExecResult:
        raise RuntimeError("transport lost")

    _answer(env, "kill -TERM", transport_lost)

    async def between(plugin: FileAccessPlugin) -> None:
        if poll_first:
            await plugin._poll_pass(plugin._windows["trial-1"])

    _, records = _drive(tmp_path, monkeypatch, env, between=between)
    terminal = _terminal(records)
    assert terminal["state"] == state
    assert "RuntimeError: transport lost" in terminal["reason"]
    assert not _changes(records)  # the after snapshot never ran


# -- live polling ----------------------------------------------------------------------


def test_transient_listing_failure_degrades_even_when_the_drain_recovers(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = _clean_env()

    async def between(plugin: FileAccessPlugin) -> None:
        window = plugin._windows["trial-1"]
        _answer(env, "ls -1 ", _fail())
        await plugin._poll_pass(window)
        assert window.poll_notes[0].startswith("chunk listing missed")
        env.hook = None  # the sandbox recovers before the final drain

    _, records = _drive(tmp_path, monkeypatch, env, between=between)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert "chunk listing missed" in terminal["reason"]
    assert _accesses(records) == ["/tests/har180-control.txt"]


def test_transient_download_failure_degrades_even_when_the_drain_recovers(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = _clean_env()

    async def between(plugin: FileAccessPlugin) -> None:
        window = plugin._windows["trial-1"]
        payload = env.chunk_payloads.pop("c000000.bin")
        await plugin._poll_pass(window)
        assert window.poll_notes[0].startswith("chunk c000000.bin missed")
        assert window.saved_events == 0  # nothing past the gap was published
        env.chunk_payloads["c000000.bin"] = payload

    _, records = _drive(tmp_path, monkeypatch, env, between=between)
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert "chunk c000000.bin missed" in terminal["reason"]
    assert _accesses(records) == ["/tests/har180-control.txt"]


def test_absent_chunks_directory_is_not_a_poll_failure(tmp_path: Path, monkeypatch: Any) -> None:
    env = _clean_env()

    async def between(plugin: FileAccessPlugin) -> None:
        window = plugin._windows["trial-1"]
        _answer(env, "ls -1 ", _fail())
        _answer(env, "test -d ", _fail())
        await plugin._poll_pass(window)
        assert window.poll_notes == [] and window.saved_events == 0
        env.hook = None

    _, records = _drive(tmp_path, monkeypatch, env, between=between)
    assert _terminal(records)["state"] == "stopped"


async def _wait_for_note(plugin: FileAccessPlugin) -> None:
    window = plugin._windows["trial-1"]
    for _ in range(500):
        if window.poll_notes:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("poll task never recorded a note")


def test_poll_pass_exception_is_noted_and_degrades(tmp_path: Path, monkeypatch: Any) -> None:
    async def wedged(self: FileAccessPlugin, window: Any) -> None:
        raise RuntimeError("download wedged")

    monkeypatch.setattr(FileAccessPlugin, "_poll_pass", wedged)
    _, records = _drive(
        tmp_path, monkeypatch, _clean_env(), poll_seconds=0.01, between=_wait_for_note
    )
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert "live publication failed: RuntimeError" in terminal["reason"]


def test_poll_task_crash_is_noted_and_degrades(tmp_path: Path, monkeypatch: Any) -> None:
    def break_sleep(plugin: FileAccessPlugin) -> None:
        plugin.poll_seconds = "bad"  # type: ignore[assignment]  # asyncio.sleep raises TypeError

    _, records = _drive(
        tmp_path, monkeypatch, _clean_env(), configure=break_sleep, between=_wait_for_note
    )
    terminal = _terminal(records)
    assert terminal["state"] == "partial"
    assert "poll task failed: TypeError (trial-1)" in terminal["reason"]


def test_poll_pass_without_environment_is_inert(tmp_path: Path, monkeypatch: Any) -> None:
    async def between(plugin: FileAccessPlugin) -> None:
        window = plugin._windows["trial-1"]
        saved_env, window.env = window.env, None
        await plugin._poll_pass(window)
        window.env = saved_env
        assert window.poll_notes == [] and window.saved_events == 0

    _, records = _drive(tmp_path, monkeypatch, _clean_env(), between=between)
    assert _terminal(records)["state"] == "stopped"


# -- plugin construction and binding ----------------------------------------------------


@pytest.mark.parametrize("raw", ["abc", None, 0, -3, "0", "-1.5"])
def test_unusable_poll_interval_falls_back_to_default(raw: Any) -> None:
    assert FileAccessPlugin(poll_seconds=raw).poll_seconds == POLL_SECONDS


def test_usable_poll_interval_is_kept() -> None:
    assert FileAccessPlugin(poll_seconds="0.5").poll_seconds == 0.5


def test_bind_requires_a_hook_registry() -> None:
    with pytest.raises(AttributeError, match="no hook registry"):
        FileAccessPlugin()._bind_trial(SimpleNamespace())


def test_bind_requires_at_least_one_lifecycle_hook() -> None:
    trial = SimpleNamespace(_hooks={"unrelated": []}, add_hook=lambda key, callback: None)
    with pytest.raises(AttributeError, match="no lifecycle hooks"):
        FileAccessPlugin()._bind_trial(trial)


def test_bind_skips_unrelated_hooks() -> None:
    plugin = FileAccessPlugin()
    trial = FakeTrial(TRIAL_NAME, FakeEnv())
    trial._hooks["unrelated"] = []
    plugin._bind_trial(trial)
    assert trial._hooks["unrelated"] == []
    assert all(trial._hooks[key] for key in trial._hooks if key != "unrelated")
    assert plugin._trials[TRIAL_NAME] is trial


def test_unbindable_trial_does_not_break_trial_setup() -> None:
    async def main() -> FakeJob:
        plugin = FileAccessPlugin()
        job = FakeJob()
        await plugin.on_job_start(job)
        trial = FakeTrial(TRIAL_NAME, FakeEnv())
        trial._hooks = {}  # nothing to bind to
        job._trial_queue._setup_hooks(trial)
        return job

    job = asyncio.run(main())
    assert len(job._trial_queue.trials) == 1  # the original setup still ran


def test_unbindable_trial_with_capture_enabled_records_unavailable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    monkeypatch.delenv("EVALLAB_FILE_ACCESS_PATHS", raising=False)
    monkeypatch.delenv("EVALLAB_FILE_ACCESS_DISCOVERY", raising=False)

    async def main() -> None:
        plugin = FileAccessPlugin()
        job = FakeJob()
        await plugin.on_job_start(job)
        trial = FakeTrial(TRIAL_NAME, FakeEnv())
        trial.config.trials_dir = tmp_path
        trial._hooks = {}  # nothing to bind to
        job._trial_queue._setup_hooks(trial)
        await plugin.on_job_end(None)

    asyncio.run(main())
    log = tmp_path / TRIAL_NAME / "agent" / "file-access.jsonl"
    assert log.exists(), "an unbindable trial got no coverage record at all"
    records = _log_lines(tmp_path)
    assert records[-1]["kind"] == "coverage"
    assert records[-1]["state"] == "unavailable"


class CallbackJob:
    """A job exposing only job-level hook registration (no trial-queue seam)."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[Any], Any]] = {}

    def on_agent_started(self, callback: Callable[[Any], Any]) -> None:
        self.handlers["agent_started"] = callback

    def on_agent_ended(self, callback: Callable[[Any], Any]) -> None:
        self.handlers["agent_ended"] = callback

    def on_verification_started(self, callback: Callable[[Any], Any]) -> None:
        self.handlers["verification_started"] = callback

    def on_trial_ended(self, callback: Callable[[Any], Any]) -> None:
        self.handlers["trial_ended"] = callback

    def on_trial_cancelled(self, callback: Callable[[Any], Any]) -> None:
        self.handlers["trial_cancelled"] = callback


def _queue_without_seam() -> CallbackJob:
    job = CallbackJob()
    job._trial_queue = SimpleNamespace()  # type: ignore[attr-defined]
    return job


@pytest.mark.parametrize("make_job", [CallbackJob, _queue_without_seam], ids=["no-queue", "no-seam"])
def test_missing_queue_seam_records_unavailable_without_sandbox_access(
    tmp_path: Path, monkeypatch: Any, make_job: Callable[[], CallbackJob]
) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")

    async def main() -> CallbackJob:
        plugin = FileAccessPlugin()
        job = make_job()
        await plugin.on_job_start(job)
        event = _event(tmp_path)
        for name in (
            "agent_started",
            "agent_ended",
            "verification_started",
            "trial_ended",
            "trial_cancelled",
        ):
            await job.handlers[name](event)
        return job

    job = asyncio.run(main())
    assert sorted(job.handlers) == [
        "agent_ended",
        "agent_started",
        "trial_cancelled",
        "trial_ended",
        "verification_started",
    ]
    records = _log_lines(tmp_path)
    assert [(record["state"], record["window_id"]) for record in records] == [("unavailable", 1)]
    assert "trial queue seam unavailable" in records[0]["reason"]


def test_unbound_trial_is_unavailable(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    monkeypatch.delenv("EVALLAB_FILE_ACCESS_PATHS", raising=False)
    monkeypatch.delenv("EVALLAB_FILE_ACCESS_DISCOVERY", raising=False)
    asyncio.run(FileAccessPlugin()._on_agent_started(_event(tmp_path)))
    records = _log_lines(tmp_path)
    assert len(records) == 1
    assert records[0]["state"] == "unavailable"
    assert "trial object is not bound to this trial name" in records[0]["reason"]


@pytest.mark.parametrize(("enabled", "state"), [(True, "starting"), (False, "disabled")])
def test_trial_start_always_records_a_coverage_state(
    tmp_path: Path, monkeypatch: Any, enabled: bool, state: str
) -> None:
    if enabled:
        monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    else:
        monkeypatch.delenv("EVALLAB_FILE_ACCESS", raising=False)

    async def main() -> None:
        plugin = FileAccessPlugin()
        trial = FakeTrial(TRIAL_NAME, FakeEnv())
        job = FakeJob()
        await plugin.on_job_start(job)
        job._trial_queue._setup_hooks(trial)
        await _fire(trial, "start", _event(tmp_path))

    asyncio.run(main())
    records = _log_lines(tmp_path)
    assert [(record["state"], record["window_id"]) for record in records] == [(state, 0)]


def test_unwritable_log_never_fails_the_trial(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("EVALLAB_FILE_ACCESS", "1")
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the trials directory should be", encoding="utf-8")

    async def main() -> FileAccessPlugin:
        plugin = FileAccessPlugin()
        await plugin._on_trial_started(_event(blocked))
        return plugin

    plugin = asyncio.run(main())
    assert plugin._seen["trial-1"][0] == TRIAL_NAME  # still tracked for later consolidation


# -- consolidation of relocated step fragments ------------------------------------------------


def _line(window_id: Any, observed_at: str) -> str:
    return json.dumps(
        {"kind": "coverage", "window_id": window_id, "observed_at": observed_at}, sort_keys=True
    )


def _fragment(trial_dir: Path, step: str, lines: list[str]) -> Path:
    path = trial_dir / "steps" / step / "agent" / "file-access.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _consolidate(plugin: FileAccessPlugin, trial_dir: Path) -> None:
    asyncio.run(plugin._consolidate_trial("trial-1", TRIAL_NAME, trial_dir))


def test_consolidation_orders_dedupes_and_counts_unparseable_lines(tmp_path: Path) -> None:
    trial_dir = tmp_path / TRIAL_NAME
    first = _line(1, "2026-01-01T00:00:00Z")
    second = _line(2, "2026-01-02T00:00:00Z")
    stray = _line("x", "")  # window id is not an integer: sorted after numbered windows
    _fragment(trial_dir, "s1", [second, "not json", "[1, 2]", "", first])
    _fragment(trial_dir, "s2", [first, stray])  # a resume step copied window 1 again
    # The root log is absent (moved aside by the relocation): that is not an error.
    plugin = FileAccessPlugin()
    _consolidate(plugin, trial_dir)

    text = (trial_dir / "agent" / "file-access.jsonl").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[:3] == [first, second, stray]
    assert "not json" not in text and "[1, 2]" not in text  # skipped, never interpolated
    note = json.loads(lines[3])
    assert note["kind"] == "coverage"
    assert note["reason"] == (
        "consolidated 2 relocated step fragment(s): "
        "1 duplicate line(s) dropped, 2 malformed line(s) skipped"
    )
    assert len(lines) == 4
    _consolidate(plugin, trial_dir)  # a repeat backstop must not merge twice
    assert (trial_dir / "agent" / "file-access.jsonl").read_text(encoding="utf-8") == text


@pytest.mark.parametrize("loss", ["malformed-line", "unreadable-fragment"])
def test_consolidation_that_loses_records_is_never_clean(tmp_path: Path, loss: str) -> None:
    trial_dir = tmp_path / TRIAL_NAME
    _fragment(trial_dir, "s1", [_line(1, "2026-01-01T00:00:00Z")])
    if loss == "malformed-line":
        _fragment(trial_dir, "s2", ["not json"])
    else:
        (trial_dir / "steps" / "s2" / "agent" / "file-access.jsonl").mkdir(parents=True)
    _consolidate(FileAccessPlugin(), trial_dir)
    lines = (trial_dir / "agent" / "file-access.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["state"] in {"partial", "unavailable"}
