"""Bounded source discovery, stable sections, and read-only mirror maintenance.

Oversize files are counted under ``refused['oversize']`` but their bounded prefix
is still indexed. A truncated JSON document is refused rather than guessed.
Section line numbers always refer to ``read_source_text`` (flattened for JSON).
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from zoneinfo import ZoneInfo

from evallab.insights.config import EngineConfig
from evallab.insights.models import (
    SourceKind,
    SourceRecord,
    SourceSection,
    section_identity,
    source_identity,
)
from evallab.insights.store import InsightStore

MIRROR_ROOT = Path.home() / "Developer/eval-lab/.worktrees/insights-readonly"
_SECTION_CHARS = 12_000
_DATE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)?\s*ET\b", re.IGNORECASE)
_HEADING = re.compile(r"^ {0,3}(#{1,3})\s+(.+?)(?:\s+#+)?\s*$")
_NEW_YORK = ZoneInfo("America/New_York")
GitRunner = Callable[..., subprocess.CompletedProcess[str]]
#: Every mirror git command runs under a deadline (60 s in the engine); a command
#: killed at that deadline leaves git's ``index.lock`` behind and every later
#: checkout fails with exit 128. A lock older than this belongs to no live refresh.
STALE_INDEX_LOCK_SECONDS = 600


@dataclass(frozen=True)
class ScanResult:
    sources_seen: int
    sources_changed: int
    sections_pending: int
    refused: dict[str, int]


def _expanded(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))


def _date_in(value: str) -> date | None:
    for match in _DATE.finditer(value):
        try:
            return date.fromisoformat(match[1])
        except ValueError:
            continue
    return None


def _heading_time(heading: str, hint: date | None) -> datetime | None:
    match = _TIME.search(heading)
    day = _date_in(heading) or hint
    if match is None or day is None:
        return None
    hour, minute = int(match[1]), int(match[2])
    period = match[3]
    if minute > 59 or (period is None and hour > 23):
        return None
    if period is not None:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if period.upper() == "PM" else 0)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_NEW_YORK).astimezone(UTC)


def _line_parts(lines: list[str], start: int, end: int) -> Iterator[tuple[int, int]]:
    """Split an oversized blank-delimited part without cutting a line."""
    part_start = start
    length = 0
    for index in range(start, end):
        if length and length + len(lines[index]) > 2 * _SECTION_CHARS:
            yield part_start, index
            part_start = index
            length = 0
        length += len(lines[index])
    if part_start < end:
        yield part_start, end


def _parts(lines: list[str], start: int, end: int) -> Iterator[tuple[int, int]]:
    """Prefer blank lines, falling back to whole-line cuts for oversized parts."""
    part_start = start
    length = 0
    last_blank: int | None = None
    index = start
    while index < end:
        length += len(lines[index])
        if not lines[index].strip():
            last_blank = index + 1
        if length >= _SECTION_CHARS and last_blank is not None and last_blank > part_start:
            yield from _line_parts(lines, part_start, last_blank)
            part_start = last_blank
            length = sum(len(line) for line in lines[part_start : index + 1])
            last_blank = None
        index += 1
    if part_start < end:
        yield from _line_parts(lines, part_start, end)


def split_sections(
    text: str,
    *,
    source_id: str,
    as_of_default: datetime | None,
    date_hint: date | None,
) -> list[SourceSection]:
    """Split ATX levels 1–3; heading paths and per-path ordinals define ids."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return []
    boundaries: list[tuple[int, str, datetime | None]] = []
    stack: list[tuple[int, str]] = []
    as_of = as_of_default
    last_time: datetime | None = None
    hint = date_hint
    fence: str | None = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        fence_match = re.match(r"(`{3,}|~{3,})", stripped)
        if fence_match:
            marker = fence_match[1]
            if fence is None:
                fence = marker
            elif (
                marker[0] == fence[0]
                and len(marker) >= len(fence)
                and not stripped[fence_match.end() :].strip(" \t\r\n")
            ):
                fence = None
            continue
        match = None if fence is not None else _HEADING.match(line.rstrip("\r\n"))
        if match is None:
            continue
        level, heading = len(match[1]), match[2]
        heading_date = _date_in(heading)
        hint = heading_date or hint
        stamp = _heading_time(heading, hint)
        if stamp is not None:
            if heading_date is None and last_time is not None and stamp < last_time:
                # A time-only heading earlier than the previous stamp is the next day.
                assert hint is not None
                hint += timedelta(days=1)
                stamp = _heading_time(heading, hint)
            as_of = last_time = stamp
        elif heading_date is not None and last_time is not None:
            # A date-only heading after timed ones must not inherit their stale stamp.
            as_of = last_time = datetime(
                heading_date.year, heading_date.month, heading_date.day, tzinfo=_NEW_YORK
            ).astimezone(UTC)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
        boundaries.append((index, " > ".join(item[1] for item in stack), as_of))
    if not boundaries or boundaries[0][0] > 0:
        boundaries.insert(0, (0, "Preamble", as_of_default))
    result: list[SourceSection] = []
    ordinals: Counter[str] = Counter()
    for offset, (start, heading, stamp) in enumerate(boundaries):
        end = boundaries[offset + 1][0] if offset + 1 < len(boundaries) else len(lines)
        for part_start, part_end in _parts(lines, start, end):
            ordinal = ordinals[heading]
            ordinals[heading] += 1
            body = "".join(lines[part_start:part_end])
            result.append(
                SourceSection(
                    section_id=section_identity(source_id, heading, ordinal),
                    source_id=source_id,
                    heading=heading,
                    ordinal=ordinal,
                    start_line=part_start + 1,
                    end_line=part_end,
                    # Trailing blank lines are ignored so appending a new section to a
                    # growing log does not make the previous section look edited.
                    sha256=hashlib.sha256((heading + "\n" + body.rstrip()).encode()).hexdigest(),
                    as_of=stamp,
                )
            )
    return result


def _read_bytes(path: Path, max_bytes: int, max_line: int | None) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(max_bytes)
    if max_line is not None:
        data = b"".join(data.splitlines(keepends=True)[:max_line])
    return data


def _flatten(value: object, pointer: str = "") -> Iterator[str]:
    if isinstance(value, dict):
        for key in sorted(value):
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield from _flatten(value[key], f"{pointer}/{escaped}")
    elif isinstance(value, list):
        for index, item in enumerate(value[:50]):
            yield from _flatten(item, f"{pointer}/{index}")
        if len(value) > 50:
            yield f"{pointer or '/'} = ... {len(value) - 50} more"
    else:
        yield f"{pointer or '/'} = {json.dumps(value, ensure_ascii=False, sort_keys=True)}"


def _source_text(data: bytes, source: SourceRecord) -> str:
    text = data.decode("utf-8", errors="replace")
    suffix = Path(source.path).suffix.lower()
    if suffix == ".jsonl":
        cut = source.size > len(data)
        lines = text.splitlines(keepends=True)
        if cut and lines and not lines[-1].endswith(("\n", "\r")):
            lines.pop()
        rows = [line for line in lines if line.strip()]
        flattened: list[str] = []
        for index, row in enumerate(rows[:30]):
            flattened.extend(_flatten(json.loads(row), f"/{index}"))
        count = f">= {len(rows)} (truncated)" if cut else str(len(rows))
        flattened.append(f"/row_count = {count}")
        text = "\n".join(flattened)
    elif source.kind == "receipt" or suffix == ".json":
        text = "\n".join(_flatten(json.loads(text)))
    return text.encode("utf-8")[: source.max_bytes].decode("utf-8", errors="ignore")


def read_source_text(source: SourceRecord) -> str:
    """Read the same bounded view indexed by scan, including JSON flattening."""
    data = _read_bytes(Path(source.path), source.max_bytes, source.max_line)
    return _source_text(data, source)


def section_text(source: SourceRecord, section: SourceSection) -> str:
    if section.source_id != source.source_id:
        raise ValueError("section belongs to another source")
    lines = read_source_text(source).splitlines(keepends=True)
    return "".join(lines[section.start_line - 1 : section.end_line])


def git_dates(repo_root: Path) -> dict[str, datetime]:
    """Latest commit date per absolute file path, from one history traversal."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "log", "--name-only", "--format=%x00%cI"],
        check=True,
        capture_output=True,
        text=True,
    )
    dates: dict[str, datetime] = {}
    current: datetime | None = None
    for line in result.stdout.splitlines():
        if line.startswith("\x00"):
            current = datetime.fromisoformat(line[1:]).astimezone(UTC)
        elif line and current is not None:
            dates.setdefault(str((repo_root / line).resolve()), current)
    return dates


def refresh_mirror(
    repo: Path, mirror: Path, *, run: GitRunner = subprocess.run
) -> str:
    """Update only the designated detached worktree; never the main checkout."""
    if mirror.exists():
        # Only main: a full `fetch origin` races the primary-checkout sync timer
        # and every agent worktree for the same remote-tracking ref locks.
        run(["git", "-C", str(repo), "fetch", "-q", "origin", "main"], check=True,
            capture_output=True, text=True)
        git_dir = run(["git", "-C", str(mirror), "rev-parse", "--absolute-git-dir"],
                      check=True, capture_output=True, text=True).stdout.strip()
        _clear_stale_index_lock(Path(git_dir) / "index.lock")
        # Forced: the mirror is engine-owned and read-only, and a checkout killed
        # mid-write leaves files a plain checkout refuses to overwrite.
        run(["git", "-C", str(mirror), "checkout", "-q", "-f", "--detach", "origin/main"],
            check=True, capture_output=True, text=True)
    else:
        run(["git", "-C", str(repo), "worktree", "add", "--detach", str(mirror), "origin/main"],
            check=True, capture_output=True, text=True)
    result = run(["git", "-C", str(mirror), "rev-parse", "HEAD"],
                 check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _clear_stale_index_lock(lock: Path) -> None:
    try:
        age = time.time() - lock.stat().st_mtime
    except FileNotFoundError:
        return
    if age > STALE_INDEX_LOCK_SECONDS:
        lock.unlink(missing_ok=True)


@dataclass(frozen=True)
class _Candidate:
    path: Path
    family: str
    kind: SourceKind
    max_bytes: int
    max_line: int | None = None
    max_age_days: float | None = None


def _candidates(config: EngineConfig) -> list[_Candidate]:
    found: dict[Path, _Candidate] = {}
    for family in config.sources:
        for pattern in family.globs:
            for name in sorted(glob.iglob(_expanded(pattern), recursive=True)):
                path = Path(name).resolve()
                if any(fnmatch(str(path), _expanded(exclude)) or fnmatch(path.name, exclude)
                       for exclude in family.exclude):
                    continue
                found.setdefault(
                    path,
                    _Candidate(
                        path, family.family, family.kind, family.max_bytes,
                        max_age_days=family.max_age_days,
                    ),
                )
    for override in config.overrides:
        path = Path(_expanded(override.path)).resolve()
        previous = found.get(path)
        max_bytes = 400_000 if previous is None else previous.max_bytes
        found[path] = _Candidate(path, override.family, override.kind, max_bytes, override.max_line)
    return [found[path] for path in sorted(found)]


def scan(store: InsightStore, config: EngineConfig, *, now: datetime) -> ScanResult:
    refused: Counter[str] = Counter()
    seen = changed = pending = 0
    mirror = (
        Path(_expanded(config.repo_mirror.path)).resolve()
        if config.repo_mirror is not None
        else MIRROR_ROOT.resolve()
    )
    dates: dict[str, datetime] | None = None
    for candidate in _candidates(config):
        path = candidate.path
        seen += 1
        try:
            stat = path.stat()
            if not path.is_file():
                refused["not_file"] += 1
                continue
            mtime = datetime.fromtimestamp(stat.st_mtime, UTC)
            if path.is_relative_to(mirror):
                if dates is None:
                    dates = git_dates(mirror)
                if str(path) not in dates:
                    refused["missing_git_date"] += 1
                    continue
                mtime = dates[str(path)]
            if candidate.max_age_days is not None and now - mtime > timedelta(
                days=candidate.max_age_days
            ):
                refused["too_old"] += 1
                continue
            if stat.st_size > candidate.max_bytes:
                refused["oversize"] += 1
            data = _read_bytes(path, candidate.max_bytes, candidate.max_line)
            source = SourceRecord(
                source_id=source_identity(str(path)), path=str(path), family=candidate.family,
                kind=candidate.kind, sha256=hashlib.sha256(data).hexdigest(), size=stat.st_size,
                mtime=mtime, max_bytes=candidate.max_bytes, max_line=candidate.max_line,
                folder=str(path.parent) if candidate.kind in {"report", "readme", "receipt"}
                else None,
            )
            text = _source_text(data, source)
        except (OSError, subprocess.SubprocessError):
            refused["unreadable"] += 1
            continue
        except (ValueError, RecursionError):
            refused["invalid_json"] += 1
            continue
        old = store.get_source(source.source_id)
        bytes_changed = store.upsert_source(source)
        view_changed = old is not None and (old.kind != source.kind or old.mtime != source.mtime)
        if bytes_changed or view_changed:
            changed += int(bytes_changed)
            if source.kind == "receipt" or path.suffix.lower() in {".json", ".jsonl"}:
                sections = [] if not text else [SourceSection(
                    section_id=section_identity(source.source_id, "JSON", 0),
                    source_id=source.source_id, heading="JSON", ordinal=0, start_line=1,
                    end_line=max(1, len(text.splitlines())),
                    sha256=hashlib.sha256(("JSON\n" + text).encode()).hexdigest(), as_of=mtime,
                )]
            else:
                sections = split_sections(text, source_id=source.source_id, as_of_default=mtime,
                                          date_hint=_date_in(str(path)))
            pending += len(store.replace_sections(source.source_id, sections))
    return ScanResult(seen, changed, pending, dict(sorted(refused.items())))
