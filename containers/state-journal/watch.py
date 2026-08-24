from __future__ import annotations

import contextlib
import ctypes
import json
import os
import select
import signal
import struct
import sys
from pathlib import Path
from typing import Any

from producer import append_event, build_diff, build_event, now
from producer import describe as describe_path

SCHEMA_VERSION = 1
MAX_HASH_BYTES = int(os.environ.get("MAX_HASH_BYTES", str(8 * 1024 * 1024)))
MAX_ENTRIES = int(os.environ.get("MAX_ENTRIES", "100000"))
MAX_EVENTS = int(os.environ.get("MAX_EVENTS", "1000000"))
TARGET_PID = int(os.environ["TARGET_PID"])
WATCH_ROOT = os.environ.get("WATCH_ROOT", "/app")
ROOT = Path(f"/proc/{TARGET_PID}/root") / WATCH_ROOT.lstrip("/")
OUTPUT = Path("/journal")

IN_ACCESS = 0x00000001
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_UNMOUNT = 0x00002000
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
WATCH_MASK = (
    IN_MODIFY
    | IN_ATTRIB
    | IN_CLOSE_WRITE
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
    | IN_UNMOUNT
    | IN_Q_OVERFLOW
)
EVENT_STRUCT = struct.Struct("iIII")
EVENT_NAMES = (
    (IN_MODIFY, "modify"),
    (IN_ATTRIB, "attrib"),
    (IN_CLOSE_WRITE, "close_write"),
    (IN_MOVED_FROM, "moved_from"),
    (IN_MOVED_TO, "moved_to"),
    (IN_CREATE, "create"),
    (IN_DELETE, "delete"),
    (IN_DELETE_SELF, "delete_self"),
    (IN_MOVE_SELF, "move_self"),
    (IN_UNMOUNT, "unmount"),
    (IN_Q_OVERFLOW, "queue_overflow"),
    (IN_IGNORED, "ignored"),
)

libc = ctypes.CDLL(None, use_errno=True)
libc.inotify_init1.argtypes = [ctypes.c_int]
libc.inotify_init1.restype = ctypes.c_int
libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
libc.inotify_add_watch.restype = ctypes.c_int

running = True


def describe(path: Path) -> dict[str, Any] | None:
    return describe_path(path, root=ROOT, max_hash_bytes=MAX_HASH_BYTES)


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def snapshot(label: str) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    truncated = False
    if ROOT.exists():
        root_entry = describe(ROOT)
        if root_entry is not None:
            entries.append(root_entry)
        for directory, names, files in os.walk(ROOT, followlinks=False):
            names.sort()
            files.sort()
            for name in [*names, *files]:
                if len(entries) >= MAX_ENTRIES:
                    truncated = True
                    break
                item = describe(Path(directory) / name)
                if item is not None:
                    entries.append(item)
            if truncated:
                break
    result = {
        "schema_version": SCHEMA_VERSION,
        "label": label,
        "captured_at": now(),
        "root": WATCH_ROOT,
        "target_pid": TARGET_PID,
        "entry_count": len(entries),
        "truncated": truncated,
        "entries": entries,
    }
    atomic_json(OUTPUT / f"state-{label}.json", result)
    return result




def add_watch(fd: int, path: Path, watches: dict[int, Path]) -> None:
    try:
        watch = libc.inotify_add_watch(fd, os.fsencode(path), WATCH_MASK)
    except (OSError, ValueError):
        return
    if watch >= 0:
        watches[watch] = path


def add_tree(fd: int, root: Path, watches: dict[int, Path]) -> None:
    if not root.is_dir():
        return
    add_watch(fd, root, watches)
    for directory, names, _ in os.walk(root, followlinks=False):
        names.sort()
        for name in names:
            path = Path(directory) / name
            if path.is_dir() and not path.is_symlink():
                add_watch(fd, path, watches)


def stop(_signum: int, _frame: object) -> None:
    global running
    running = False


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    os.chmod(OUTPUT, 0o700)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    before = snapshot("before")
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        error = ctypes.get_errno()
        atomic_json(
            OUTPUT / "status.json",
            {
                "schema_version": SCHEMA_VERSION,
                "status": "unavailable",
                "reason": os.strerror(error),
            },
        )
        return 0

    watches: dict[int, Path] = {}
    add_tree(fd, ROOT, watches)
    events: list[dict[str, Any]] = []
    dropped_events = 0
    atomic_json(
        OUTPUT / "status.json",
        {
            "schema_version": SCHEMA_VERSION,
            "status": "recording",
            "started_at": now(),
            "root": WATCH_ROOT,
            "target_pid": TARGET_PID,
            "watch_count": len(watches),
        },
    )
    (OUTPUT / "READY").write_text("ready\n", encoding="utf-8")

    with (OUTPUT / "state-events.jsonl").open("a", encoding="utf-8", buffering=1) as stream:
        while running:
            ready, _, _ = select.select([fd], [], [], 0.25)
            if not ready:
                continue
            try:
                payload = os.read(fd, 1024 * 1024)
            except BlockingIOError:
                continue
            offset = 0
            while offset + EVENT_STRUCT.size <= len(payload):
                watch, mask, cookie, name_length = EVENT_STRUCT.unpack_from(payload, offset)
                offset += EVENT_STRUCT.size
                raw_name = payload[offset : offset + name_length]
                offset += name_length
                name = os.fsdecode(raw_name.split(b"\0", 1)[0])
                parent = watches.get(watch, ROOT)
                path = parent / name if name else parent
                if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO):
                    add_tree(fd, path, watches)
                if len(events) >= MAX_EVENTS:
                    dropped_events += 1
                    continue
                try:
                    relative = "." if path == ROOT else path.relative_to(ROOT).as_posix()
                except ValueError:
                    relative = path.as_posix()
                state = (
                    describe(path)
                    if not (mask & IN_ISDIR) and mask & (IN_CLOSE_WRITE | IN_MOVED_TO)
                    else None
                )
                record = build_event(
                    sequence=len(events) + 1,
                    timestamp=now(),
                    path=relative,
                    operations=[name for bit, name in EVENT_NAMES if mask & bit],
                    cookie=cookie or None,
                    is_directory=bool(mask & IN_ISDIR),
                    state=state,
                )
                events.append(record)
                append_event(stream, record)

    os.close(fd)
    after = snapshot("after")
    diff = build_diff(
        before,
        after,
        events,
        schema_version=SCHEMA_VERSION,
        watch_root=WATCH_ROOT,
    )
    diff["dropped_event_count"] = dropped_events
    atomic_json(OUTPUT / "state-diff.json", diff)
    atomic_json(
        OUTPUT / "status.json",
        {
            "schema_version": SCHEMA_VERSION,
            "status": diff["status"],
            "reason": diff["reason"],
            "started_at": before["captured_at"],
            "finished_at": after["captured_at"],
            "root": WATCH_ROOT,
            "target_pid": TARGET_PID,
            "event_count": len(events),
            "dropped_event_count": dropped_events,
            "change_count": len(diff["changes"]),
        },
    )
    with contextlib.suppress(FileNotFoundError):
        (OUTPUT / "READY").unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
