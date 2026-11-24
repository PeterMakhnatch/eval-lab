from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Protocol

MAX_CAPTURE_BYTES = 4_000_000


class System(Protocol):
    def now(self) -> float: ...

    def utc(self) -> str: ...

    def sleep(self, seconds: float) -> None: ...

    def host(self) -> str: ...

    def capture(self, argv: list[str], timeout: float, owner: int) -> tuple[int | None, str]: ...

    def spawn(self, argv: list[str], cwd: Path, env: dict[str, str], log: Path, owner: int) -> int: ...

    def poll(self, pid: int) -> int | None: ...

    def terminate(self, pid: int, grace: float) -> int: ...

    def alive(self, pid: int) -> bool: ...

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
        finally:
            stream.close()
        self._children[child.pid] = child
        return child.pid

    def poll(self, pid: int) -> int | None:
        return self._children[pid].poll()

    def terminate(self, pid: int, grace: float) -> int:
        child = self._children[pid]
        for sent in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, sent)
            try:
                return child.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                continue
        return child.wait(timeout=grace)

    def alive(self, pid: int) -> bool:
        assert pid > 0
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

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
