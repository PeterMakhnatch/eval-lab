"""Portable record construction shared by the state-journal watcher and fixtures."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, TextIO


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def digest(path: Path, size: int, *, max_hash_bytes: int) -> tuple[str | None, str]:
    if size > max_hash_bytes:
        return None, "size_limit"
    try:
        value = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                value.update(chunk)
        return f"sha256:{value.hexdigest()}", "complete"
    except (OSError, PermissionError):
        return None, "unreadable"


def describe(path: Path, *, root: Path, max_hash_bytes: int) -> dict[str, Any] | None:
    try:
        info = path.lstat()
    except (FileNotFoundError, OSError):
        return None
    relative = "." if path == root else path.relative_to(root).as_posix()
    result: dict[str, Any] = {
        "path": relative,
        "mode": stat.filemode(info.st_mode),
        "size_bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
    }
    if stat.S_ISREG(info.st_mode):
        result["type"] = "file"
        result["sha256"], result["hash_status"] = digest(
            path, info.st_size, max_hash_bytes=max_hash_bytes
        )
    elif stat.S_ISDIR(info.st_mode):
        result["type"] = "directory"
    elif stat.S_ISLNK(info.st_mode):
        result["type"] = "symlink"
        try:
            result["target"] = os.readlink(path)
        except OSError:
            result["target"] = None
    else:
        result["type"] = "other"
    return result


def build_event(
    *,
    sequence: int,
    timestamp: str,
    path: str,
    operations: list[str],
    cookie: int | None,
    is_directory: bool,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "sequence": sequence,
        "timestamp": timestamp,
        "path": path,
        "operations": operations,
        "cookie": cookie,
        "is_directory": is_directory,
    }
    if state is not None:
        record["state"] = state
    return record


def append_event(stream: TextIO, record: dict[str, Any]) -> None:
    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def change_record(
    path: str,
    change_type: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    matching = [event for event in events if event.get("path") == path]
    return {
        "path": path,
        "change_type": change_type,
        "before": before,
        "after": after,
        "event_count": len(matching),
        "first_event_at": matching[0]["timestamp"] if matching else None,
        "last_event_at": matching[-1]["timestamp"] if matching else None,
    }


def build_diff(
    before: dict[str, Any],
    after: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    schema_version: int = 1,
    watch_root: str = "/app",
) -> dict[str, Any]:
    before_by_path = {item["path"]: item for item in before["entries"]}
    after_by_path = {item["path"]: item for item in after["entries"]}
    changes: list[dict[str, Any]] = []
    for path in sorted(before_by_path.keys() | after_by_path.keys()):
        old = before_by_path.get(path)
        new = after_by_path.get(path)
        if old is None:
            changes.append(change_record(path, "added", old, new, events))
        elif new is None:
            changes.append(change_record(path, "deleted", old, new, events))
        elif old != new:
            changes.append(change_record(path, "modified", old, new, events))
    return {
        "schema_version": schema_version,
        "status": "partial" if before["truncated"] or after["truncated"] else "available",
        "reason": (
            "snapshot_entry_limit" if before["truncated"] or after["truncated"] else None
        ),
        "root": watch_root,
        "before_captured_at": before["captured_at"],
        "after_captured_at": after["captured_at"],
        "event_count": len(events),
        "change_count": len(changes),
        "changes": changes,
    }
