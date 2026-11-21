from __future__ import annotations

import fcntl
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_EVENT_THREAD_LOCK = threading.RLock()


@contextmanager
def event_log_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
    """Serialize event-log operations across both threads and processes."""
    lock_path = path.parent / ".events.lock"
    with _EVENT_THREAD_LOCK, lock_path.open("a+b") as lock:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock.fileno(), operation)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def event_log_paths(path: Path) -> tuple[Path, ...]:
    """Return retained event segments from oldest archive to active log."""
    archives: list[tuple[int, Path]] = []
    for candidate in path.parent.glob(f"{path.name}.*"):
        suffix = candidate.name.removeprefix(f"{path.name}.")
        if suffix.isdigit() and candidate.is_file():
            archives.append((int(suffix), candidate))
    return tuple(candidate for _, candidate in sorted(archives, reverse=True)) + (
        (path,) if path.is_file() else ()
    )


def event_line_complete(line: bytes) -> bool:
    if line.endswith(b"\n"):
        return True
    try:
        json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return True


def read_event_log_lines(path: Path) -> tuple[tuple[Path, int, str], ...]:
    if not path.parent.is_dir():
        return ()
    lines: list[tuple[Path, int, str]] = []
    with event_log_lock(path, exclusive=False):
        for segment in event_log_paths(path):
            with segment.open("rb") as source:
                for line_number, line in enumerate(source, start=1):
                    if event_line_complete(line):
                        text = line.removesuffix(b"\n").removesuffix(b"\r").decode()
                        lines.append((segment, line_number, text))
    return tuple(lines)
