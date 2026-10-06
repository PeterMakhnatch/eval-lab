"""Shared constants and pure record utilities for file-access observation.

HAR-180 observes protected files with ``inotifywait -m`` from inside the
agent sandbox and pairs the event stream with evaluator-owned before/after
content hashes. This module stays dependency-free (stdlib only, no Harbor
imports) so the live watch can import the constants and parse the per-trial
log read-only.

Per-trial log: ``agent/file-access.jsonl`` (relative to the trial dir).
Evaluator-owned baselines live under ``evaluator/file-access/`` (relative to
the trial dir), which is never mounted into the sandbox.

Record schema ``evallab.file_access/v1``. Every record carries the common
fields ``schema``/``source``/``phase``/``window_id``/``observed_at`` plus a
``kind`` discriminator:

* ``coverage`` -- exactly how far observation got (``disabled`` when the
  opt-in is off, ``unavailable`` when setup failed, ``active``/``starting``
  while watching, ``stopped`` after a clean drain, ``partial`` when events
  were lost, ``error`` for malformed configuration). Every trial gets at
  least one coverage record, even without capture.
* ``access`` -- one inotify OPEN/ACCESS observation. ``OPEN`` alone is open
  evidence, not proof bytes were read. No PID, command, or step attribution:
  inotify cannot provide it and this module never invents it.
* ``file_change`` -- a protected-file before/after hash comparison with an
  evaluator-owned ``baseline_ref``.

Limits are always explicit on coverage records: no PID/command attribution,
events can coalesce or overflow (absence is not clean proof), and the
same-authority sandbox observer is not tamper-proof.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

FILE_ACCESS_SCHEMA = "evallab.file_access/v1"
FILE_ACCESS_SNAPSHOT_SCHEMA = "evallab.file_access_snapshot/v1"
FILE_ACCESS_PROBE_SCHEMA = "evallab.file_access_probe/v1"

#: Per-trial observation log, relative to the trial directory.
FILE_ACCESS_LOG = Path("agent/file-access.jsonl")

#: Opt-in capture switch. Anything but ``"1"`` means disabled (explicit record).
FILE_ACCESS_ENV = "EVALLAB_FILE_ACCESS"

#: Optional evaluator configuration for extra protected targets (JSON, see
#: :func:`parse_paths_env` for the exact shape).
FILE_ACCESS_PATHS_ENV = "EVALLAB_FILE_ACCESS_PATHS"

#: Evaluator-owned snapshot directory, relative to the trial directory.
#: Never mounted into the sandbox; baselines stay on the evaluator host.
EVALUATOR_SNAPSHOT_DIR = Path("evaluator/file-access")

#: Where file-access observation runs.
SOURCE = "inotifywait"

#: Coverage states (see module docstring).
COVERAGE_STATES = frozenset(
    {"disabled", "starting", "active", "partial", "unavailable", "stopped", "error"}
)


#: Explicit limits stamped on every coverage record.
LIMIT_NO_PID = "no PID/command attribution"
LIMIT_COALESCE_OVERFLOW = "events can coalesce or overflow; absence is not clean proof"
LIMIT_SAME_AUTHORITY = "same-authority sandbox observer is not tamper-proof"
COVERAGE_LIMITS = (LIMIT_NO_PID, LIMIT_COALESCE_OVERFLOW, LIMIT_SAME_AUTHORITY)

#: Protected-path categories.
CATEGORY_GIT_OBJECTS = "git_objects"
CATEGORY_GIT_REFS = "git_refs"
CATEGORY_GRADER = "grader"
CATEGORIES = (CATEGORY_GIT_OBJECTS, CATEGORY_GIT_REFS, CATEGORY_GRADER)

#: Live host-publication cadence: the controller polls sealed sandbox chunks
#: this often while a window is open, so alerts precede agent end.
POLL_SECONDS = 2.0

#: Bounded transport/storage caps (documented on coverage records when hit).
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
MAX_SAVED_EVENTS = 20_000
MAX_HASH_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_FILES = 20_000
REASON_MAX_CHARS = 500
STDERR_TAIL_CHARS = 2_000

#: Sandbox discovery candidates (plus the exec working directory and any
#: evaluator-configured extras). The smoke workdir ``/testbed`` is included.
DISCOVERY_CANDIDATES = ("/testbed", "/app", "/repo", "/workspace")

#: Hidden git-history locations that never appear as a normal ``.git`` entry.
MIMO_GIT_HIDDEN = "/var/lib/mimo/git-hidden"

#: Default grader root inside the sandbox.
DEFAULT_TESTS_DIR = "/tests"

#: inotifywait framing: ``--format '%e%0%w%f%0' --no-newline`` emits NUL-joined
#: ``(events, path)`` pairs with no line terminator.
FRAME_FORMAT = "%e%0%w%f%0"

#: How the sandbox helper is invoked for a live window.
CAPTURE_BASE_EVENTS = ("open", "access")
CAPTURE_LOSS_EVENTS = ("q_overflow", "ignored", "unmount")

#: ``inotifywait`` stderr marker proving the initial watch set is registered.
READINESS_MARKER = "Watches established."

#: Timing bounds (seconds) for the controller side of a live window.
READY_TIMEOUT_SEC = 20.0
READY_POLL_SEC = 0.5
EXEC_TIMEOUT_SEC = 15
SNAPSHOT_TIMEOUT_SEC = 60
STOP_TIMEOUT_SEC = 15


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 observer timestamp (not a kernel time)."""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _common(*, window_id: int, observed_at: str | None = None) -> dict[str, Any]:
    return {
        "schema": FILE_ACCESS_SCHEMA,
        "source": SOURCE,
        "phase": "agent",
        "window_id": window_id,
        "observed_at": observed_at or utc_now_iso(),
    }


def coverage_record(
    *,
    window_id: int,
    state: str,
    reason: str,
    watched_paths: list[str] | None = None,
    missing_paths: list[str] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Build a ``coverage`` record. ``reason`` is truncated to a bound."""
    if state not in COVERAGE_STATES:
        raise ValueError(f"unknown coverage state: {state!r}")
    record = _common(window_id=window_id, observed_at=observed_at)
    record.update(
        {
            "kind": "coverage",
            "state": state,
            "reason": reason[:REASON_MAX_CHARS],
            "watched_paths": list(watched_paths or []),
            "missing_paths": list(missing_paths or []),
            "limits": list(COVERAGE_LIMITS),
        }
    )
    return record


def access_record(
    *,
    window_id: int,
    path: str,
    events: list[str],
    is_directory: bool,
    category: str,
    protected_root: str,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Build an ``access`` record. Never carries PID/command/step attribution."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category!r}")
    record = _common(window_id=window_id, observed_at=observed_at)
    record.update(
        {
            "kind": "access",
            "path": path,
            "events": list(events),
            "is_directory": bool(is_directory),
            "category": category,
            "protected_root": protected_root,
        }
    )
    return record


def file_change_record(
    *,
    window_id: int,
    path: str,
    category: str,
    change: str,
    before_sha256: str | None,
    after_sha256: str | None,
    baseline_ref: str,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Build a ``file_change`` record for a protected grader path."""
    if change not in {"modified", "created", "deleted", "type_changed", "unavailable"}:
        raise ValueError(f"unknown change: {change!r}")
    record = _common(window_id=window_id, observed_at=observed_at)
    record.update(
        {
            "kind": "file_change",
            "path": path,
            "category": category,
            "change": change,
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
            "baseline_ref": baseline_ref,
        }
    )
    return record


def parse_paths_env(raw: str | None) -> dict[str, list[str]]:
    """Parse ``EVALLAB_FILE_ACCESS_PATHS`` into per-category path lists.

    Exact agreed shape::

        {"git_objects": ["<abs sandbox path>", ...],
         "git_refs": ["<abs sandbox path>", ...],
         "grader": ["<abs sandbox path>", ...]}

    Every key is optional; unknown keys, non-absolute paths, and paths
    claimed by two categories are ``ValueError`` (visible misconfiguration,
    never silently narrowed). Unset/blank input means defaults only (``{}``).
    """

    def _check(paths: Any, *, category: str) -> list[str]:
        if not isinstance(paths, list):
            raise ValueError(f"{category!r} must be a list of absolute paths")
        checked: list[str] = []
        for item in paths:
            if not isinstance(item, str) or not item.startswith("/") or "\x00" in item:
                raise ValueError(f"{category!r} holds a non-absolute path: {item!r}")
            if item.startswith("-"):
                raise ValueError(f"{category!r} holds a flag-like path: {item!r}")
            normalized = PurePosixPath(item).as_posix()
            checked.append(normalized)
        return checked

    if raw is None or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("top-level value must be an object keyed by category")
    unknown = sorted(set(value) - set(CATEGORIES))
    if unknown:
        raise ValueError(f"unknown categories: {', '.join(unknown)}")
    parsed = {category: _check(value.get(category, []), category=category) for category in CATEGORIES}
    seen: dict[str, str] = {}
    for category in CATEGORIES:
        for item in parsed[category]:
            if item in seen:
                raise ValueError(f"{item!r} is claimed by {seen[item]!r} and {category!r}")
            seen[item] = category
    return {category: paths for category, paths in parsed.items() if paths}


def classify_path(
    path: str, targets: list[tuple[str, str]]
) -> tuple[str, str] | None:
    """Map an absolute sandbox path to ``(category, protected_root)``.

    ``targets`` holds ``(root, category)`` pairs; the longest matching root
    wins. Returns ``None`` when the path is outside every protected target.
    """
    candidate: tuple[str, str] | None = None
    for root, category in targets:
        if path != root and not path.startswith(root.rstrip("/") + "/"):
            continue
        if candidate is None or len(root) > len(candidate[0]):
            candidate = (category, root)
    if candidate is None:
        return None
    return candidate[0], candidate[1]


def split_frame_events(token: str) -> tuple[list[str], bool] | None:
    """Split one ``%e`` token into ``(events, is_directory)``.

    Returns ``None`` when the token is empty or carries no read-shaped event
    after removing the ``ISDIR`` flag. Loss tokens (overflow/ignored/unmount)
    are reported by the caller, never fabricated into access records here.
    """
    names = [part for part in token.split(",") if part]
    if not names or any(name not in {"OPEN", "ACCESS", "ISDIR"} for name in names):
        return None
    is_directory = "ISDIR" in names
    events = [name for name in names if name != "ISDIR"]
    if not events:
        return None
    return events, is_directory


# A terminal field distinguishes a truncated pathname from the next event.
# Filenames cannot contain NUL, so a pathname cannot forge this delimiter.
FRAME_END = b"\x00EVALLAB_END\x00"


def parse_frames(data: bytes) -> tuple[list[tuple[list[str], bool, str]], int, bool]:
    """Decode complete event/path frames; reject truncated or merged records."""
    frames: list[tuple[list[str], bool, str]] = []
    invalid = 0
    blocks = data.split(FRAME_END)
    for block in blocks[:-1]:
        fields = block.split(b"\x00")
        if len(fields) != 2:
            invalid += 1
            continue
        raw_events, raw_path = fields
        try:
            events_token = raw_events.decode("utf-8")
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            invalid += 1
            continue
        split = split_frame_events(events_token)
        if split is None or not path.startswith("/"):
            invalid += 1
            continue
        events, is_directory = split
        frames.append((events, is_directory, path))
    return frames, invalid, bool(blocks[-1])


def parse_stream_chunk(
    carry: bytes, new: bytes
) -> tuple[list[tuple[list[str], bool, str]], int, bytes]:
    """Publish complete frames and retain a split frame until its end marker."""
    data = carry + new
    boundary = data.rfind(FRAME_END)
    if boundary < 0:
        return [], 0, data
    boundary += len(FRAME_END)
    frames, invalid, _ = parse_frames(data[:boundary])
    return frames, invalid, data[boundary:]


def scan_loss_tokens(data: bytes) -> dict[str, bool]:
    """Report whether the raw stream carries overflow/ignored/unmount tokens."""
    seen = {"overflow": False, "ignored": False, "unmount": False}
    for block in data.split(FRAME_END):
        token = block.partition(b"\x00")[0]
        upper = token.decode("utf-8", errors="ignore").upper()
        if "OVERFLOW" in upper:
            seen["overflow"] = True
        elif upper == "IGNORED":
            seen["ignored"] = True
        elif upper == "UNMOUNT":
            seen["unmount"] = True
    return seen


def diff_entries(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Diff two ``{path: snapshot entry}`` maps into ``file_change`` bodies.

    Each body holds ``path``/``category``/``change``/``before_sha256``/
    ``after_sha256``; the caller wraps them with :func:`file_change_record`.
    Type/absence/unreadable states stay distinct from empty content: a file
    whose hash cannot be compared on either side is ``unavailable``, never
    silently ``modified`` or clean.
    """
    bodies: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        category = (new or old or {}).get("category", CATEGORY_GRADER)
        if old is None:
            bodies.append(
                {
                    "path": path,
                    "category": category,
                    "change": "created",
                    "before_sha256": None,
                    "after_sha256": (new or {}).get("sha256"),
                }
            )
        elif new is None:
            bodies.append(
                {
                    "path": path,
                    "category": category,
                    "change": "deleted",
                    "before_sha256": old.get("sha256"),
                    "after_sha256": None,
                }
            )
        elif old.get("type") != new.get("type"):
            bodies.append(
                {
                    "path": path,
                    "category": category,
                    "change": "type_changed",
                    "before_sha256": old.get("sha256"),
                    "after_sha256": new.get("sha256"),
                }
            )
        elif (
            old.get("type") == "file"
            and new.get("type") == "file"
            and (
                old.get("type"),
                old.get("sha256"),
                old.get("size_bytes"),
            )
            != (
                new.get("type"),
                new.get("sha256"),
                new.get("size_bytes"),
            )
        ):
            if old.get("hash_status") == "unreadable" or new.get("hash_status") == "unreadable":
                change = "unavailable"
            else:
                change = "modified"
            bodies.append(
                {
                    "path": path,
                    "category": category,
                    "change": change,
                    "before_sha256": old.get("sha256"),
                    "after_sha256": new.get("sha256"),
                }
            )
    return bodies
