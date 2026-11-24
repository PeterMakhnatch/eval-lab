from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from worldwatch.model import (
    MAX_FILE_BYTES,
    MAX_LINES_PER_FILE,
    MAX_SMALL_JSON_BYTES,
    MAX_TRIALS,
    SEAT_NAME_RE,
    canonical_json,
    parse_strict_json,
    validate_json_tree,
)

_READ_CHUNK = 65_536
_MAX_WALK_DIRS = 20_000
_MAX_WALK_DEPTH = 6


@dataclass(frozen=True, slots=True)
class FileStatBytes:
    dev: int
    ino: int
    size: int
    mtime: float
    data: bytes


def stat_regular_file(path: Path) -> tuple[os.stat_result | None, str | None]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot stat {path.name}: {exc.strerror or exc}"
    if stat.S_ISLNK(info.st_mode):
        return None, f"{path.name} is a symlink"
    if not stat.S_ISREG(info.st_mode):
        return None, f"{path.name} is not a regular file"
    return info, None


def read_regular_bytes(
    path: Path,
    offset: int = 0,
    max_bytes: int = MAX_FILE_BYTES,
) -> tuple[FileStatBytes | None, str | None]:
    assert offset >= 0 and max_bytes >= 1
    info, err = stat_regular_file(path)
    if err is not None:
        return None, err
    if info is None:
        return None, None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot open {path.name}: {exc.strerror or exc}"
    chunks: list[bytes] = []
    total = 0
    max_iters = max_bytes // _READ_CHUNK + 4
    try:
        finfo = os.fstat(fd)
        if not stat.S_ISREG(finfo.st_mode):
            return None, f"{path.name} is not a regular file"
        if finfo.st_size - offset > max_bytes:
            return None, f"{path.name} exceeds {max_bytes} bytes"
        if offset > 0:
            os.lseek(fd, offset, os.SEEK_SET)
        for _ in range(max_iters):
            want = max(1, min(_READ_CHUNK, max_bytes + 1 - total))
            piece = os.read(fd, want)
            if not piece:
                break
            chunks.append(piece)
            total += len(piece)
            if total > max_bytes:
                return None, f"{path.name} exceeds {max_bytes} bytes"
        return (
            FileStatBytes(
                dev=finfo.st_dev,
                ino=finfo.st_ino,
                size=finfo.st_size,
                mtime=finfo.st_mtime,
                data=b"".join(chunks),
            ),
            None,
        )
    except OSError as exc:
        return None, f"cannot read {path.name}: {exc.strerror or exc}"
    finally:
        os.close(fd)


def read_regular_text(path: Path, max_bytes: int = MAX_FILE_BYTES) -> tuple[str | None, str | None]:
    res, err = read_regular_bytes(path, offset=0, max_bytes=max_bytes)
    if err is not None or res is None:
        return None, err
    try:
        return res.data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, f"{path.name} is not valid UTF-8"


def read_strict_json_file(
    path: Path,
    max_bytes: int = MAX_SMALL_JSON_BYTES,
) -> tuple[Any | None, str | None]:
    text, err = read_regular_text(path, max_bytes=max_bytes)
    if err is not None or text is None:
        return None, err
    try:
        return parse_strict_json(text), None
    except ValueError as exc:
        return None, f"{path.name}: {exc}"


def read_tolerant_json_object(
    path: Path,
    max_bytes: int = MAX_SMALL_JSON_BYTES,
) -> tuple[dict[str, Any] | None, str | None]:
    text, err = read_regular_text(path, max_bytes=max_bytes)
    if err is not None or text is None:
        return None, err
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        return None, f"{path.name}: {type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, f"{path.name}: root is not a JSON object"
    return data, None


def split_complete_jsonl_bytes(raw: bytes) -> tuple[list[bytes], bytes]:
    assert isinstance(raw, bytes)
    if not raw:
        return [], b""
    parts = raw.split(b"\n")
    if len(parts) - 1 > MAX_LINES_PER_FILE:
        raise ValueError(f"JSONL chunk exceeds {MAX_LINES_PER_FILE} lines")
    complete = [seg[:-1] if seg.endswith(b"\r") else seg for seg in parts[:-1]]
    return complete, parts[-1]


def split_text_lines_strict(text: str) -> list[str]:
    assert isinstance(text, str)
    parts = text.split("\n")
    if len(parts) > MAX_LINES_PER_FILE + 1:
        raise ValueError(f"text exceeds {MAX_LINES_PER_FILE} lines")
    return [seg[:-1] if seg.endswith("\r") else seg for seg in parts]


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    assert isinstance(payload, bytes)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        view = memoryview(payload)
        written = 0
        max_steps = len(payload) // _READ_CHUNK + 4
        for _ in range(max_steps):
            if written >= len(payload):
                break
            n = os.write(fd, view[written : written + _READ_CHUNK])
            assert n > 0
            written += n
        assert written == len(payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, target)
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def atomic_write_json(path: Path, obj: Mapping[str, Any]) -> None:
    validate_json_tree(obj)
    encoded = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    atomic_write_text(path, encoded)


def append_ndjson_rows(path: Path, rows: list[Mapping[str, Any]]) -> None:
    if not rows:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = "".join(canonical_json(row) + "\n" for row in rows).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, 0o644)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)


def _has_bespoke_markers(directory: Path) -> bool:
    agent_dir = directory / "agent"
    if not os.path.isdir(agent_dir) or os.path.islink(agent_dir):
        return False
    for name in ("board-events.jsonl", "proxy-usage.jsonl", "proxy-usage.json", "subscription-usage.json"):
        if os.path.lexists(agent_dir / name):
            return True
    try:
        entries = list(os.scandir(agent_dir))
    except OSError:
        return False
    for entry in entries[:256]:
        if (
            entry.is_dir(follow_symlinks=False)
            and SEAT_NAME_RE.match(entry.name)
            and os.path.lexists(Path(entry.path) / "trajectory.json")
        ):
            return True
    return False


def _has_olam_live_markers(directory: Path) -> bool:
    if os.path.isdir(directory / "seats") and not os.path.islink(directory / "seats"):
        return True
    for rel in (
        "logs/world/events.jsonl",
        "verifier/events.jsonl",
        "artifacts/logs/world/events.jsonl",
    ):
        if os.path.lexists(directory / rel):
            return True
    return False


def classify_trial_dir(directory: Path) -> str | None:
    res_path = directory / "result.json"
    cfg_path = directory / "config.json"
    cfg_doc, _ = read_tolerant_json_object(cfg_path) if os.path.lexists(cfg_path) else (None, None)
    is_job_cfg = isinstance(cfg_doc, dict) and isinstance(cfg_doc.get("tasks"), list)
    if os.path.lexists(res_path):
        res_doc, res_err = read_tolerant_json_object(res_path)
        if res_err is not None:
            if is_job_cfg:
                return None
            return "bespoke" if _has_bespoke_markers(directory) else "olam"
        if isinstance(res_doc, dict):
            if "n_total_trials" in res_doc and "trial_name" not in res_doc:
                return None
            if "trial_name" in res_doc or "seat_results" in res_doc:
                if _has_bespoke_markers(directory) and "seat_results" not in res_doc:
                    return "bespoke"
                env_imp = str(((res_doc.get("config") or {}).get("environment") or {}).get("import_path") or "")
                if "TeamEnvironment" in env_imp and "seat_results" not in res_doc:
                    return "bespoke"
                return "olam"
    if isinstance(cfg_doc, dict) and not is_job_cfg and ("trial_name" in cfg_doc or "participants" in cfg_doc):
        return "bespoke" if _has_bespoke_markers(directory) else "olam"
    if _has_bespoke_markers(directory):
        return "bespoke"
    if _has_olam_live_markers(directory):
        return "olam"
    return None


def discover_trials(root: Path) -> list[tuple[Path, str]]:
    base = Path(root)
    if not os.path.isdir(base):
        return []
    direct = classify_trial_dir(base)
    if direct is not None:
        return [(base, direct)]
    found: list[tuple[Path, str]] = []
    stack: list[tuple[Path, int]] = [(base, 0)]
    visited = 0
    while stack:
        visited += 1
        assert visited <= _MAX_WALK_DIRS
        current, depth = stack.pop()
        if current != base:
            kind = classify_trial_dir(current)
            if kind is not None:
                found.append((current, kind))
                if len(found) >= MAX_TRIALS:
                    break
                continue
        if depth >= _MAX_WALK_DEPTH:
            continue
        try:
            children = sorted(
                (
                    Path(entry.path)
                    for entry in os.scandir(current)
                    if not entry.name.startswith(".")
                    and entry.name != "__pycache__"
                    and entry.is_dir(follow_symlinks=False)
                ),
                reverse=True,
            )
        except OSError:
            continue
        for child in children:
            stack.append((child, depth + 1))
    found.sort(key=lambda item: (item[0].name, str(item[0])))
    assert len(found) <= MAX_TRIALS
    return found
