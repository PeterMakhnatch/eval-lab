"""Idempotent, crash-safe publication of an increment's review/audit runs to a Judge Station queue.

The Station queue format and its idempotent appender are the production factory's
(``append_judge_queue_entry``: one row per ``run_id``, a repeated run is skipped). That helper
alone is not enough for a long-running watcher: two writers can race its read-then-append, and a
crash can leave a torn last line. This module adds, around the same helper, an exclusive lock
and a copy-append-fsync-rename so the queue file is always either the old or the new complete
file. The queue named in a watch config is owned by that watcher (one writer); a foreign writer
that appends without taking this lock is outside what this can protect against.
"""

from __future__ import annotations

import fcntl
import os
from collections import Counter
from pathlib import Path
from typing import Any

from evallab import hack_drop as hd
from evallab.line.io import read_bytes
from evallab.line.watch_state import WatchError, fsync_directory


def read_queue_rows(queue: Path) -> list[dict[str, Any]]:
    """The rows of a queue file; a malformed line raises instead of being skipped."""
    if not os.path.lexists(queue):
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(read_bytes(queue).decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = hd.strict_json_loads(line)  # a repeated key is refused, not resolved
        except ValueError:
            row = None
        if not isinstance(row, dict) or not isinstance(row.get("run_id"), str):
            raise WatchError(f"queue line {number} is not a queue row: {queue}")
        rows.append(row)
    return rows


def read_queue_ids(queue: Path) -> list[str]:
    return [row["run_id"] for row in read_queue_rows(queue)]


def publish(queue: Path, rows: list[dict[str, Any]]) -> int:
    """Append the rows whose run is not queued yet; return how many were added."""
    from evallab.production.factory_autoreview import append_judge_queue_entry
    from evallab.production.judge_station import QueueEntry

    queue.parent.mkdir(parents=True, exist_ok=True)
    with queue.with_name(queue.name + ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = read_queue_ids(queue)
        duplicates = sorted(i for i, count in Counter(existing).items() if count > 1)
        if duplicates:
            raise WatchError(f"queue already holds duplicate runs, e.g. {duplicates[0]!r}: {queue}")
        known = set(existing)
        wanted = [row for row in rows if row["run_id"] not in known]
        if len({row["run_id"] for row in wanted}) != len(wanted):
            raise WatchError("an increment queued the same run twice")
        if not wanted:
            return 0
        scratch = queue.with_name(f".{queue.name}.tmp")
        scratch.write_bytes(queue.read_bytes() if queue.is_file() else b"")
        added = sum(append_judge_queue_entry(scratch, QueueEntry(**row)) for row in wanted)
        with scratch.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(scratch, queue)
        fsync_directory(queue.parent)
        return added
