"""Deterministic tests for the host-wide FIFO Docker slot lock.

No Docker, no network: contenders are threads in this process (or plain
``sh`` children for ``run``), state lives in a tmp dir, polls are short.
"""

from __future__ import annotations

import argparse
import contextlib
import json
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
    _docker_lock_status_command,
    _write_queue_file,
    default_slots,
    docker_slot,
    get_status,
    queue_path,
    read_queue,
    run_with_lock,
)


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
        if len(read_queue(state_dir=state_dir)) >= expected:
            return
        time.sleep(0.005)
    raise AssertionError(
        f"queue never reached {expected} entries: {read_queue(state_dir=state_dir)!r}"
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
    assert read_queue(state_dir=tmp_path) == []


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
    assert read_queue(state_dir=tmp_path) == []


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
    assert read_queue(state_dir=tmp_path) == []


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
    assert read_queue(state_dir=tmp_path) == []


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
    assert read_queue(state_dir=tmp_path) == []


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
    assert len(read_queue(state_dir=tmp_path)) == 2

    lock = _lock("next", tmp_path, ttl=120.0)
    try:
        acquired, _position, _total, _holders = lock.try_acquire_step()
        assert acquired is True
        remaining = read_queue(state_dir=tmp_path)
        assert [entry["ticket"] for entry in remaining] == [lock.ticket]
    finally:
        lock.release()
    assert read_queue(state_dir=tmp_path) == []


def test_status_prunes_stale_and_splits_holders_waiters(tmp_path: Path) -> None:
    holder = _lock("holder", tmp_path)
    waiter = _lock("waiter", tmp_path)
    try:
        assert holder.try_acquire_step()[0] is True
        assert waiter.try_acquire_step()[0] is False
        status = get_status(state_dir=tmp_path)
        assert [e["label"] for e in status["holders"]] == ["holder"]
        assert [e["label"] for e in status["waiters"]] == ["waiter"]
    finally:
        holder.release()
        waiter.release()


# (e) Release is idempotent and happens even when the body raises.


def test_release_idempotent_and_on_exception(tmp_path: Path) -> None:
    lock = _lock("once", tmp_path)
    lock.acquire()
    lock.release()
    lock.release()  # second release must not raise
    assert read_queue(state_dir=tmp_path) == []

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
    assert read_queue(state_dir=tmp_path) == []


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
    assert read_queue(state_dir=tmp_path) == []


def test_run_success_code_zero(tmp_path: Path) -> None:
    code = run_with_lock(
        "demo",
        ["sh", "-c", "exit 0"],
        state_dir=tmp_path,
        poll_interval=0.01,
        heartbeat_interval=5.0,
    )
    assert code == 0
    assert read_queue(state_dir=tmp_path) == []


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
        remaining = read_queue(state_dir=tmp_path)
        assert [entry["label"] for entry in remaining] == ["holder"]
    finally:
        holder.release()
    assert read_queue(state_dir=tmp_path) == []


def test_acquire_timeout_raises_and_removes_waiter(tmp_path: Path) -> None:
    holder = _lock("holder", tmp_path)
    waiter = _lock("waiter", tmp_path)
    holder.acquire()
    try:
        with pytest.raises(DockerLockTimeout):
            waiter.acquire(timeout=0.2)
        assert [e["label"] for e in read_queue(state_dir=tmp_path)] == ["holder"]
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
        assert [entry["ticket"] for entry in read_queue(state_dir=tmp_path)] == [
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
        assert read_queue(state_dir=tmp_path)[0]["heartbeat_at"] == now
        with real_locked_queue(docker_lock.DEFAULT_NAME, tmp_path) as (_, entries):
            entries.clear()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert lock._stop_heartbeat.is_set()
    finally:
        lock.release()


# (j) An unreadable queue file does not kill the heartbeat; it retries.


def test_heartbeat_continues_after_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_status_command_prints_empty_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ENV_DIR, str(tmp_path))
    code = _docker_lock_status_command(_status_args(name="other"), tmp_path)
    assert code == 0
    assert (
        capsys.readouterr().out.strip()
        == "docker-lock 'other': no holders or waiters"
    )


def test_status_command_prints_holders_and_waiters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = 1_000.0
    host = socket.gethostname()
    entries = [
        {
            "ticket": "holder-full",
            "pid": 101,
            "host": host,
            "label": "full-holder",
            "cwd": str(tmp_path / "full"),
            "enqueued_at": now - 50.0,
            "started_at": now - 20.0,
            "heartbeat_at": now,
        },
        {
            "ticket": "holder-bare",
            "pid": 102,
            "host": host,
            "label": "bare-holder",
            "cwd": str(tmp_path / "bare"),
            "enqueued_at": None,
            "started_at": "unknown",
            "heartbeat_at": now,
        },
        {
            "ticket": "waiter-aged",
            "pid": 103,
            "host": host,
            "label": "aged-waiter",
            "cwd": str(tmp_path / "aged"),
            "enqueued_at": now - 7.0,
            "started_at": None,
            "heartbeat_at": now,
        },
        {
            "ticket": "waiter-bare",
            "pid": 104,
            "host": host,
            "label": "bare-waiter",
            "cwd": str(tmp_path / "bare-waiter"),
            "enqueued_at": None,
            "started_at": None,
            "heartbeat_at": now,
        },
    ]
    monkeypatch.setenv(ENV_DIR, str(tmp_path))
    _write_queue_file(queue_path(state_dir=tmp_path), entries)
    real_get_status = docker_lock.get_status

    def fixed_status(*, name: str) -> dict:
        return real_get_status(name, clock=lambda: now, is_alive=lambda _pid: True)

    monkeypatch.setattr(docker_lock, "get_status", fixed_status)
    code = _docker_lock_status_command(_status_args(), tmp_path)
    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        f"holder  full-holder pid=101 cwd={tmp_path / 'full'} age=50s running=20s",
        f"holder  bare-holder pid=102 cwd={tmp_path / 'bare'}",
        f"waiter1  aged-waiter pid=103 cwd={tmp_path / 'aged'} waiting=7s",
        f"waiter2  bare-waiter pid=104 cwd={tmp_path / 'bare-waiter'} waiting=?",
    ]


def test_status_command_emits_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ENV_DIR, str(tmp_path))
    code = _docker_lock_status_command(_status_args(as_json=True), tmp_path)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "docker"
    assert payload["holders"] == []
    assert payload["waiters"] == []
