"""Deterministic tests for the host-wide FIFO Docker slot lock.

No Docker, no network: contenders are threads in this process (or plain
``sh`` children for ``run``), state lives in a tmp dir, polls are short.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest

from evallab.docker_lock import (
    DockerLock,
    DockerLockTimeout,
    _write_queue_file,
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

    with pytest.raises(RuntimeError, match="boom"), docker_slot(
        "raises",
        state_dir=tmp_path,
        poll_interval=0.01,
        heartbeat_interval=5.0,
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
