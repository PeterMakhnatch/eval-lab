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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
        self.snapshot_calls = 0

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
            "extras": [],
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
            return json.dumps(self.status_doc or {}).encode()
        name = remote.rsplit("/", 1)[-1]
        return self.chunk_payloads.get(name)

    async def exec(self, command: str, timeout_sec: int | None = None) -> ExecResult:
        _ = timeout_sec
        self.commands.append(command)
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
