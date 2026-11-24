from __future__ import annotations

import contextlib
import fcntl
import os
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Protocol

MAX_CAPTURE_BYTES = 4_000_000
MAX_PROCESSES = 100_000
PS_TIMEOUT_SECONDS = 30
EDGE = frozenset(" \t\n\"'=")


def command_matches(command: str, needle: str) -> bool:
    assert needle
    start = command.find(needle)
    for _ in range(len(command) + 1):
        if start < 0:
            return False
        end = start + len(needle)
        before = start == 0 or command[start - 1] in EDGE
        after = end == len(command) or command[end] in EDGE
        if before and after:
            return True
        start = command.find(needle, start + 1)
    return False


class System(Protocol):
    def now(self) -> float: ...

    def utc(self) -> str: ...

    def sleep(self, seconds: float) -> None: ...

    def host(self) -> str: ...

    def capture(self, argv: list[str], timeout: float, owner: int) -> tuple[int | None, str]: ...

    def spawn(self, argv: list[str], cwd: Path, env: dict[str, str], log: Path, owner: int) -> int: ...

    def poll(self, pid: int) -> int | None: ...

    def signal_group(self, pgid: int, number: int) -> None: ...

    def alive(self, pid: int) -> bool: ...

    def start_time(self, pid: int) -> str | None: ...

    def processes(self, needles: list[str]) -> list[tuple[int, int, str]]: ...

    def try_lock(self, path: Path, owner: int) -> bool: ...

    def unlock(self, path: Path, owner: int) -> None: ...


class RealSystem:
    def __init__(self) -> None:
        self._children: dict[int, subprocess.Popen[bytes]] = {}
        self._locks: dict[Path, int] = {}

    def now(self) -> float:
        return time.monotonic()

    def utc(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def sleep(self, seconds: float) -> None:
        assert 0 < seconds <= 3600
        time.sleep(seconds)

    def host(self) -> str:
        return socket.gethostname()

    def capture(self, argv: list[str], timeout: float, owner: int) -> tuple[int | None, str]:
        assert argv and timeout > 0 and owner > 0
        try:
            done = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None, ""
        return done.returncode, done.stdout[:MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")

    def spawn(self, argv: list[str], cwd: Path, env: dict[str, str], log: Path, owner: int) -> int:
        assert argv and owner > 0
        stream: IO[bytes] = log.open("xb")
        try:
            child = subprocess.Popen(
                argv, cwd=cwd, env={**os.environ, **env}, stdin=subprocess.DEVNULL,
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
            )
        except BaseException:
            stream.close()
            log.unlink(missing_ok=True)
            raise
        stream.close()
        self._children[child.pid] = child
        return child.pid

    def poll(self, pid: int) -> int | None:
        child = self._children.get(pid)
        if child is not None:
            return child.poll()
        return None if self.alive(pid) else -1

    def signal_group(self, pgid: int, number: int) -> None:
        assert pgid > 1
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, number)

    def alive(self, pid: int) -> bool:
        assert pid > 0
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def start_time(self, pid: int) -> str | None:
        assert pid > 0
        try:
            done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                                  timeout=PS_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        text = done.stdout.decode("utf-8", errors="replace").strip()
        return text if done.returncode == 0 and text else None

    def processes(self, needles: list[str]) -> list[tuple[int, int, str]]:
        assert needles
        done = subprocess.run(["ps", "-axww", "-o", "pid=,pgid=,command="], capture_output=True,
                              timeout=PS_TIMEOUT_SECONDS, check=False)
        if done.returncode != 0:
            raise OSError(f"ps ended with {done.returncode}")
        found: list[tuple[int, int, str]] = []
        lines = done.stdout.decode("utf-8", errors="replace").splitlines()
        assert len(lines) <= MAX_PROCESSES
        for line in lines:
            parts = line.split(None, 2)
            if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            pid, pgid = int(parts[0]), int(parts[1])
            if pid == os.getpid():
                continue
            matched = [needle for needle in needles if command_matches(parts[2], needle)]
            if matched:
                found.append((pid, pgid, matched[0]))
        return found

    def try_lock(self, path: Path, owner: int) -> bool:
        assert path not in self._locks and owner == os.getpid()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return False
        self._locks[path] = descriptor
        return True

    def unlock(self, path: Path, owner: int) -> None:
        assert owner == os.getpid()
        descriptor = self._locks.pop(path)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
