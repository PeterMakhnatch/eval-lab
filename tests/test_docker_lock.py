"""Deterministic tests for the host-wide FIFO Docker slot lock.

No Docker, no network: contenders are threads in this process (or plain
``sh`` children for ``run``), state lives in a tmp dir, polls are short.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import threading
import time
from pathlib import Path

import pytest

import evallab.docker_lock as docker_lock
from evallab.docker_lock import (
    ENV_DIR,
    ENV_SLOTS,
    DockerLock,
    DockerLockTimeout,
    _docker_lock_run_command,
    _read_queue_file,
    _write_queue_file,
    build_docker_lock_parser,
    default_slots,
    default_state_dir,
    docker_slot,
    pid_alive,
    queue_path,
    run_with_lock,
)


@pytest.fixture(autouse=True)
def _isolate_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "docker-lock-test-host")


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evallab")
    commands = parser.add_subparsers(dest="command_name", required=True)
    build_docker_lock_parser(commands)
    return parser


def _lock(label: str, state_dir: Path, **kwargs: object) -> DockerLock:
    params: dict = {
        "slots": 1,
        "state_dir": state_dir,
        "poll_interval": 0.01,
        "heartbeat_interval": 5.0,
    }
    params.update(kwargs)
    return DockerLock(label, **params)


def _wait_for_queue_len(state_dir: Path, expected: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(_read_queue_file(queue_path(state_dir=state_dir))) >= expected:
            return
        time.sleep(0.005)
    raise AssertionError(
        f"queue never reached {expected} entries: {_read_queue_file(queue_path(state_dir=state_dir))!r}"
    )


def test_default_slots_admit_four_and_queue_the_fifth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_SLOTS, raising=False)
    assert default_slots() == 4
    locks = [
        DockerLock(
            f"holder-{index}",
            state_dir=tmp_path,
            poll_interval=0.01,
            heartbeat_interval=5.0,
        )
        for index in range(5)
    ]
    try:
        assert [lock.try_acquire_step()[0] for lock in locks] == [
            True,
            True,
            True,
            True,
            False,
        ]
    finally:
        for lock in locks:
            lock.release()
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []


def test_env_slots_override_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SLOTS, "2")
    assert default_slots() == 2
    monkeypatch.setenv(ENV_SLOTS, "not-a-number")
    assert default_slots() == 4


# (a) slots=1: second acquirer waits, then gets the slot right after release.


def test_second_acquirer_waits_until_release(tmp_path: Path) -> None:
    holder = _lock("holder", tmp_path)
    holder.acquire()
    acquired = threading.Event()
    errors: list[BaseException] = []

    def contend() -> None:
        try:
            waiter = _lock("waiter", tmp_path)
            waiter.acquire(timeout=5.0)
            try:
                acquired.set()
            finally:
                waiter.release()
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=contend)
    thread.start()
    _wait_for_queue_len(tmp_path, 2)
    time.sleep(0.15)
    assert not acquired.is_set(), "waiter jumped the held slot"
    holder.release()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert errors == []
    assert acquired.is_set(), "waiter never got the slot after release"
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []


def test_try_step_denies_second_until_release(tmp_path: Path) -> None:
    holder = _lock("holder", tmp_path)
    waiter = _lock("waiter", tmp_path)
    try:
        assert holder.try_acquire_step()[0] is True
        assert waiter.try_acquire_step()[0] is False
        holder.release()
        assert waiter.try_acquire_step()[0] is True
    finally:
        holder.release()
        waiter.release()
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []


# (b) FIFO: three waiters are granted the slot in enqueue order.


def test_fifo_order_with_three_waiters(tmp_path: Path) -> None:
    holder = _lock("holder", tmp_path)
    holder.acquire()
    order: list[str] = []
    errors: list[BaseException] = []

    def contend(label: str) -> None:
        try:
            with docker_slot(
                label,
                slots=1,
                state_dir=tmp_path,
                poll_interval=0.01,
                heartbeat_interval=5.0,
            ):
                order.append(label)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads: list[threading.Thread] = []
    for index, label in enumerate(["w1", "w2", "w3"], start=2):
        thread = threading.Thread(target=contend, args=(label,))
        thread.start()
        threads.append(thread)
        _wait_for_queue_len(tmp_path, index)
    holder.release()
    for thread in threads:
        thread.join(timeout=5.0)
    assert errors == []
    assert order == ["w1", "w2", "w3"]
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []


# (c) slots=2 admits two concurrently; the third still waits.


def test_slots_two_admits_two_concurrently(tmp_path: Path) -> None:
    first = _lock("first", tmp_path, slots=2)
    second = _lock("second", tmp_path, slots=2)
    third = _lock("third", tmp_path, slots=2)
    try:
        assert first.try_acquire_step()[0] is True
        assert second.try_acquire_step()[0] is True
        assert third.try_acquire_step()[0] is False
        first.release()
        assert third.try_acquire_step()[0] is True
    finally:
        first.release()
        second.release()
        third.release()
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []


# (d) Dead-pid and heartbeat-expired entries are pruned.


def test_stale_entries_are_pruned(tmp_path: Path) -> None:
    now = time.time()
    host = socket.gethostname()
    stale_entries = [
        {
            "ticket": "dead-pid-entry",
            "pid": 999_999,
            "host": host,
            "label": "dead-holder",
            "cwd": str(tmp_path),
            "enqueued_at": now,
            "started_at": now,
            "heartbeat_at": now,
        },
        {
            "ticket": "expired-entry",
            "pid": os.getpid(),
            "host": host,
            "label": "expired-holder",
            "cwd": str(tmp_path),
            "enqueued_at": now - 10_000,
            "started_at": now - 10_000,
            "heartbeat_at": now - 10_000,
        },
    ]
    (tmp_path / "docker.queue.json").write_text("[]\n", encoding="utf-8")
    _write_queue_file(queue_path(state_dir=tmp_path), stale_entries)
    assert len(_read_queue_file(queue_path(state_dir=tmp_path))) == 2

    lock = _lock("next", tmp_path, ttl=120.0)
    try:
        acquired, _position, _total, _holders = lock.try_acquire_step()
        assert acquired is True
        remaining = _read_queue_file(queue_path(state_dir=tmp_path))
        assert [entry["ticket"] for entry in remaining] == [lock.ticket]
    finally:
        lock.release()
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []


# (e) Release is idempotent and happens even when the body raises.


def test_release_idempotent_and_on_exception(tmp_path: Path) -> None:
    lock = _lock("once", tmp_path)
    lock.acquire()
    lock.release()
    lock.release()  # second release must not raise
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []

    with (
        pytest.raises(RuntimeError, match="boom"),
        docker_slot(
            "raises",
            state_dir=tmp_path,
            poll_interval=0.01,
            heartbeat_interval=5.0,
        ),
    ):
        raise RuntimeError("boom")
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []


# (f) `run` returns the child's exit code and leaves the queue empty.


def test_run_returns_child_exit_code(tmp_path: Path) -> None:
    code = run_with_lock(
        "demo",
        ["sh", "-c", "exit 3"],
        state_dir=tmp_path,
        poll_interval=0.01,
        heartbeat_interval=5.0,
    )
    assert code == 3
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []


def test_run_success_code_zero(tmp_path: Path) -> None:
    code = run_with_lock(
        "demo",
        ["sh", "-c", "exit 0"],
        state_dir=tmp_path,
        poll_interval=0.01,
        heartbeat_interval=5.0,
    )
    assert code == 0
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []


# (g) `--timeout` while waiting exits 124 and removes the waiter entry.


def test_run_timeout_returns_124_and_removes_waiter(tmp_path: Path) -> None:
    holder = _lock("holder", tmp_path)
    holder.acquire()
    try:
        code = run_with_lock(
            "impatient",
            ["sh", "-c", "exit 0"],
            slots=1,
            state_dir=tmp_path,
            timeout=0.3,
            poll_interval=0.01,
            heartbeat_interval=5.0,
        )
        assert code == 124
        remaining = _read_queue_file(queue_path(state_dir=tmp_path))
        assert [entry["label"] for entry in remaining] == ["holder"]
    finally:
        holder.release()
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []


def test_acquire_timeout_raises_and_removes_waiter(tmp_path: Path) -> None:
    holder = _lock("holder", tmp_path)
    waiter = _lock("waiter", tmp_path)
    holder.acquire()
    try:
        with pytest.raises(DockerLockTimeout):
            waiter.acquire(timeout=0.2)
        assert [e["label"] for e in _read_queue_file(queue_path(state_dir=tmp_path))] == ["holder"]
    finally:
        holder.release()
        waiter.release()


# (h) A peer can prune our entry while we are paused; the waiter rejoins at the back.


def test_try_step_rejoins_when_peer_pruned_own_entry(tmp_path: Path) -> None:
    now = 1_000.0
    peer = {
        "ticket": "peer-ticket",
        "pid": 424_242,
        "host": socket.gethostname(),
        "label": "peer",
        "cwd": str(tmp_path),
        "enqueued_at": now,
        "started_at": now,
        "heartbeat_at": now,
    }
    lock = DockerLock(
        "pruned",
        slots=1,
        state_dir=tmp_path,
        clock=lambda: now,
        is_alive=lambda _pid: True,
    )
    lock.enqueue()
    # A peer pruned our entry (e.g. while this process was paused).
    _write_queue_file(queue_path(state_dir=tmp_path), [peer])
    try:
        acquired, position, total, holders = lock.try_acquire_step()
        assert acquired is False
        assert (position, total) == (2, 2)
        assert [entry["ticket"] for entry in holders] == [peer["ticket"]]
        assert [entry["ticket"] for entry in _read_queue_file(queue_path(state_dir=tmp_path))] == [
            peer["ticket"],
            lock.ticket,
        ]
        assert lock.try_acquire_step()[:3] == (False, 2, 2)
    finally:
        lock.release()


# (i) The heartbeat refreshes its entry, then stops once that entry vanishes.


def test_heartbeat_updates_entry_then_stops_when_ticket_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 500.0
    lock = DockerLock(
        "beating",
        state_dir=tmp_path,
        heartbeat_interval=0.01,
        clock=lambda: now,
        is_alive=lambda _pid: True,
    )
    lock.enqueue()
    beat = threading.Event()
    real_locked_queue = docker_lock._locked_queue

    @contextlib.contextmanager
    def spy(name: str, state_dir: Path | None):
        with real_locked_queue(name, state_dir) as pair:
            yield pair
        beat.set()

    monkeypatch.setattr(docker_lock, "_locked_queue", spy)
    lock._start_heartbeat()
    thread = lock._heartbeat_thread
    assert thread is not None
    try:
        assert beat.wait(timeout=5.0), "heartbeat never touched the queue"
        assert _read_queue_file(queue_path(state_dir=tmp_path))[0]["heartbeat_at"] == now
        with real_locked_queue(docker_lock.DEFAULT_NAME, tmp_path) as (_, entries):
            entries.clear()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert lock._stop_heartbeat.is_set()
    finally:
        lock.release()


# (j) An unreadable queue file does not kill the heartbeat; it retries.


def test_heartbeat_continues_after_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 600.0
    lock = DockerLock(
        "flaky",
        state_dir=tmp_path,
        heartbeat_interval=0.01,
        clock=lambda: now,
        is_alive=lambda _pid: True,
    )
    lock.enqueue()
    # No entry remains, so the first successful pass stops the heartbeat.
    _write_queue_file(queue_path(state_dir=tmp_path), [])
    real_locked_queue = docker_lock._locked_queue
    calls = 0

    @contextlib.contextmanager
    def flaky(name: str, state_dir: Path | None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("mutex unavailable")
        with real_locked_queue(name, state_dir) as pair:
            yield pair

    monkeypatch.setattr(docker_lock, "_locked_queue", flaky)
    lock._start_heartbeat()
    thread = lock._heartbeat_thread
    assert thread is not None
    try:
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert calls >= 2
        assert lock._stop_heartbeat.is_set()
    finally:
        lock.release()


# (k) `docker-lock status` text rendering of holders and waiters.


def _status_args(*, name: str = "docker", as_json: bool = False) -> argparse.Namespace:
    return argparse.Namespace(name=name, json=as_json)


# (l) The `evallab docker-lock` parser and `run` dispatch.


def test_parser_parses_run_and_status() -> None:
    parser = _cli_parser()

    run_args = parser.parse_args(
        ["docker-lock", "run", "--slots", "1", "LABEL", "--", "sh", "-c", "true"]
    )
    assert run_args.docker_lock_command == "run"
    assert run_args.slots == 1
    assert run_args.name == "docker"
    assert run_args.timeout is None
    assert run_args.label == "LABEL"
    # Some Python versions keep the "--" in a REMAINDER list; dispatch strips it.
    assert run_args.command in (["sh", "-c", "true"], ["--", "sh", "-c", "true"])
    assert run_args.func is _docker_lock_run_command


def test_parser_requires_a_docker_lock_subcommand() -> None:
    with pytest.raises(SystemExit) as excinfo:
        _cli_parser().parse_args(["docker-lock"])
    assert excinfo.value.code == 2


def test_parsed_run_dispatches_child_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_DIR, str(tmp_path))
    args = _cli_parser().parse_args(
        ["docker-lock", "run", "--slots", "1", "LABEL", "--", "sh", "-c", "exit 5"]
    )
    assert args.func(args, tmp_path) == 5
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []


def _run_args(command: list[str] | None) -> argparse.Namespace:
    return argparse.Namespace(label="LABEL", command=command, slots=1, name="docker", timeout=None)


def test_run_command_strips_dash_separator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_DIR, str(tmp_path))
    calls: list[tuple[str, list[str], dict[str, object]]] = []

    def fake_run(label: str, command: list[str], **kwargs: object) -> int:
        calls.append((label, list(command), kwargs))
        return 7

    monkeypatch.setattr(docker_lock, "run_with_lock", fake_run)
    code = _docker_lock_run_command(_run_args(["--", "sh", "-c", "true"]), tmp_path)
    assert code == 7
    assert calls == [
        ("LABEL", ["sh", "-c", "true"], {"slots": 1, "name": "docker", "timeout": None})
    ]


@pytest.mark.parametrize("command", [None, [], ["--"]])
def test_run_command_without_command_returns_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: list[str] | None,
) -> None:
    monkeypatch.setenv(ENV_DIR, str(tmp_path))
    assert _docker_lock_run_command(_run_args(command), tmp_path) == 2
    assert "LABEL and CMD are required" in capsys.readouterr().err
    assert not queue_path(state_dir=tmp_path).exists()


def test_run_with_lock_without_command_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_with_lock("demo", [], state_dir=tmp_path) == 2
    assert "no command given" in capsys.readouterr().err
    assert not queue_path(state_dir=tmp_path).exists()


def test_run_child_killed_by_sigterm_returns_143(tmp_path: Path) -> None:
    code = run_with_lock(
        "signalled",
        ["sh", "-c", "kill -TERM $$"],
        state_dir=tmp_path,
        poll_interval=0.01,
        heartbeat_interval=5.0,
    )
    assert code == 128 + 15
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []


# (m) State dir resolution, name validation and pid liveness.


def test_default_state_dir_env_override_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_DIR, str(tmp_path / "locks"))
    assert default_state_dir() == tmp_path / "locks"
    assert queue_path() == tmp_path / "locks" / "docker.queue.json"

    monkeypatch.delenv(ENV_DIR)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert default_state_dir() == tmp_path / "home" / ".cache" / "evallab" / "docker-lock"


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b"])
def test_invalid_lock_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="invalid lock name"):
        queue_path(name, state_dir=tmp_path)


def test_pid_alive_handles_nonpositive_and_kill_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert pid_alive(0) is False
    assert pid_alive(-1) is False
    assert pid_alive(os.getpid()) is True

    def raise_with(exc: BaseException):
        def fake_kill(pid: int, sig: int) -> None:
            raise exc

        return fake_kill

    monkeypatch.setattr(docker_lock.os, "kill", raise_with(PermissionError()))
    assert pid_alive(12345) is True
    monkeypatch.setattr(docker_lock.os, "kill", raise_with(ProcessLookupError()))
    assert pid_alive(12345) is False
    monkeypatch.setattr(docker_lock.os, "kill", raise_with(OSError("boom")))
    assert pid_alive(12345) is False


# (n) Missing, unreadable, empty, invalid or non-list queue files read as empty.


@pytest.mark.parametrize(
    "content",
    [None, "", "   \n", "{not json", '{"label": "x"}', '"text"', "42"],
    ids=["missing", "empty", "blank", "invalid-json", "object", "string", "number"],
)
def test_bad_queue_files_read_as_empty(tmp_path: Path, content: str | None) -> None:
    path = queue_path(state_dir=tmp_path)
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert _read_queue_file(path) == []
    assert _read_queue_file(queue_path(state_dir=tmp_path)) == []


def test_unreadable_queue_file_reads_as_empty(tmp_path: Path) -> None:
    path = queue_path(state_dir=tmp_path)
    path.mkdir()
    assert _read_queue_file(path) == []


def test_queue_file_list_drops_non_dict_entries(tmp_path: Path) -> None:
    path = queue_path(state_dir=tmp_path)
    path.write_text('[{"label": "kept"}, "junk", 3, null, ["x"]]', encoding="utf-8")
    assert _read_queue_file(path) == [{"label": "kept"}]
