from __future__ import annotations

import contextlib
import fcntl
import os
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Protocol

MAX_CAPTURE_BYTES = 4_000_000
MAX_PROCESSES = 100_000
PS_TIMEOUT_SECONDS = 30

Row = tuple[int, int, int, str]
WalkError = Callable[[OSError], None]
Walk = Callable[[Path, WalkError], Iterator[tuple[str, list[str], list[str]]]]


def parse_rows(text: str) -> list[Row]:
    rows: list[Row] = []
    lines = text.splitlines()
    assert len(lines) <= MAX_PROCESSES
    for line in lines:
        parts = line.split(None, 3)
        if len(parts) < 4 or not all(part.isdigit() for part in parts[:3]):
            continue
        rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
    return rows


class System(Protocol):
    def now(self) -> float: ...

    def utc(self) -> str: ...

    def sleep(self, seconds: float) -> None: ...

    def host(self) -> str: ...

    def capture(self, argv: list[str], timeout: float, owner: int) -> tuple[int | None, str]: ...

    def spawn(self, argv: list[str], cwd: Path, env: dict[str, str], log: Path, owner: int) -> int: ...

    def poll(self, pid: int) -> int | None: ...

    def signal_group(self, pgid: int, number: int) -> None: ...

    def signal_process(self, pid: int, number: int) -> None: ...

    def alive(self, pid: int) -> bool: ...

    def start_time(self, pid: int) -> str | None: ...

    def processes(self) -> list[Row]: ...

    def walk(self, root: Path, onerror: WalkError) -> Iterator[tuple[str, list[str], list[str]]]: ...

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
        except (OSError, subprocess.SubprocessError):
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
        assert pgid > 1 and pgid != os.getpgrp()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, number)

    def signal_process(self, pid: int, number: int) -> None:
        assert pid > 1 and pid != os.getpid()
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, number)

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
        except (OSError, subprocess.SubprocessError):
            return None
        text = done.stdout.decode("utf-8", errors="replace").strip()
        return text if done.returncode == 0 and text else None

    def processes(self) -> list[Row]:
        try:
            done = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,pgid=,command="], capture_output=True,
                                  timeout=PS_TIMEOUT_SECONDS, check=False)
        except subprocess.SubprocessError as error:
            raise OSError(f"ps failed: {error}") from error
        if done.returncode != 0:
            raise OSError(f"ps ended with {done.returncode}")
        rows = parse_rows(done.stdout.decode("utf-8", errors="replace"))
        if not rows:
            raise OSError("ps listed no process")
        return rows

    def walk(self, root: Path, onerror: WalkError) -> Iterator[tuple[str, list[str], list[str]]]:
        return os.walk(root, onerror=onerror)

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
