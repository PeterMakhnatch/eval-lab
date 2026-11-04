"""State directory of a live line: layout, ownership lock, configuration.

A live state directory has exactly one owner process at a time (a non-blocking ``flock`` on
``watch.lock`` held for the process lifetime). Its only durable facts are the committed
increments: ``increments/<n>/`` holds the increment's inputs and its child batch, and exists
as a commit only once ``increments/<n>/commit.json`` does. Nothing derived is stored: the
cumulative view is built on demand from the committed children, and ``status.json`` is only
a progress report.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab import hack_drop as hd
from evallab.line.io import canonical_hash, digest, read_bytes, read_json, write_json
from evallab.line.spec import _refuse_final_link as refuse_final_link
from evallab.line.spec import load_spec

CONFIG_SCHEMA = "evallab.line_watch/v1"
STATE_SCHEMA = "evallab.line_watch_state/v1"
COMMIT_SCHEMA = "evallab.line_watch_commit/v1"
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
INCREMENT_NAME = re.compile(r"\d{6}")
SOURCE_KINDS = ("manifest", "jobs")
#: Tuning that may change without starting a new state directory; everything else is frozen.
TUNING_DEFAULTS: dict[str, float | int] = {
    "poll_seconds": 15,
    "settle_seconds": 30,
    "max_runs_per_increment": 200,
    "watchdog_max_age_seconds": 900,
}


class WatchError(ValueError):
    """A live-line problem that must stop the cycle (never silently skipped)."""


class WatchRefused(WatchError):
    """An input the line must never read: non-DEV, sealed, a link, or an evaluator field."""


def now_iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).isoformat()


def parse_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo else None


def strict_json(path: Path) -> dict[str, Any]:
    """A JSON object from a file, refusing duplicate keys and NaN/Infinity (an operator file)."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({key for key, _ in items}) != len(items):
            raise WatchError(f"duplicate JSON key in {path.name}")
        return dict(items)

    def constant(name: str) -> Any:
        raise WatchError(f"non-finite number {name} in {path.name}")

    try:
        value = json.loads(
            read_bytes(path).decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
    except (OSError, ValueError, RecursionError) as error:
        if isinstance(error, WatchError):
            raise
        raise WatchError(f"{path.name} is not readable JSON: {error}") from error
    if not isinstance(value, dict):
        raise WatchError(f"{path.name} must hold a JSON object")
    return value


class State:
    """Paths of one live state directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.watch_json = self.root / "watch.json"
        self.lock_path = self.root / "watch.lock"
        self.commit_lock_path = self.root / "commit.lock"
        self.status_path = self.root / "status.json"
        self.watchdog_path = self.root / "watchdog.json"
        self.errors_path = self.root / "errors.jsonl"
        self.frozen = self.root / "frozen"
        self.increments = self.root / "increments"
        self.abandoned = self.root / "abandoned"

    @classmethod
    def locate(cls, path: Path) -> State | None:
        """The live state a path is, if any."""
        state = cls(path)
        return state if state.is_live_state() else None

    @classmethod
    def owner_of_child(cls, path: Path) -> State | None:
        """The live state whose committed increment contains ``path`` (a child batch), if any."""
        resolved = Path(path).expanduser().resolve()
        for parent in resolved.parents:
            if parent.name == "increments" and cls(parent.parent).is_live_state():
                return cls(parent.parent)
        return None

    def inc(self, n: int) -> Path:
        return self.increments / f"{n:06d}"

    def is_live_state(self) -> bool:
        """Any live-state marker counts, so deleting ``watch.json`` fails the check, not hides it."""
        return any(p.exists() for p in (self.watch_json, self.increments))

    def folders(self) -> list[Path]:
        """Every entry of ``increments/``, in name order."""
        return sorted(self.increments.iterdir()) if self.increments.is_dir() else []

    def committed(self) -> list[dict[str, Any]]:
        """Commit records in increment order. An unreadable record raises: never skipped."""
        records = []
        for folder in self.folders():
            if INCREMENT_NAME.fullmatch(folder.name) and (folder / "commit.json").exists():
                records.append(read_json(folder / "commit.json"))
        return records

    def open_folders(self) -> list[Path]:
        """Increment folders without a commit record: leftovers of a crash or a failure."""
        return [p for p in self.folders() if not (p / "commit.json").exists()]

    def sweep_open(self) -> list[str]:
        """Move uncommitted increment folders aside (evidence is kept, nothing counts).

        Every move gets a name no earlier one has, so repeated failures never collide or nest.
        """
        moved = []
        for folder in self.open_folders():
            self.abandoned.mkdir(parents=True, exist_ok=True)
            target = self.abandoned / f"{folder.name}-{uuid.uuid4().hex[:12]}"
            shutil.move(str(folder), str(target))
            moved.append(target.name)
        return moved

    def config(self) -> dict[str, Any]:
        return read_json(self.watch_json)["config"]


@contextmanager
def owner_lock(state: State) -> Iterator[None]:
    """Single owner per state directory: a second owner refuses before doing anything."""
    state.root.mkdir(parents=True, exist_ok=True)
    with state.lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WatchError(f"another watcher owns this state directory: {state.root}") from None
        yield


def owner_alive(state: State) -> bool:
    """True while some process holds the owner lock (read-only probe)."""
    if not state.lock_path.exists():
        return False
    with state.lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


# ---------------------------------------------------------------- configuration


def _resolve(value: Any, base: Path, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WatchError(f"{what} must be nonempty text")
    path = Path(value).expanduser()
    spelled = path if path.is_absolute() else base / path
    refuse_final_link(spelled)  # before resolving, as the line's own spec loader does
    return str(hd.check_dev_path_allowed(spelled))


def parse_config(path: Path, state_dir: Path) -> dict[str, Any]:
    """Validate a watch config and resolve it against a state directory. Writes nothing.

    Line-level fields (task status, AutoReview, audit, models, labels policy) are validated by
    the line's own ``load_spec`` so a live watcher can never accept a spec the line would refuse.
    The file is read strictly: duplicate keys and NaN/Infinity are refused.
    """
    path = Path(path).expanduser().absolute()
    refuse_final_link(path)
    path = hd.check_dev_path_allowed(path)
    doc = strict_json(path)
    if doc.get("schema") != CONFIG_SCHEMA or doc.get("split") != "dev":
        raise WatchError(f"watch config must use {CONFIG_SCHEMA} and split=dev")
    watch_id = doc.get("watch_id")
    if not isinstance(watch_id, str) or not SAFE_NAME.fullmatch(watch_id):
        raise WatchError("watch_id must be a safe single name")
    base = path.parent
    sources_in = doc.get("sources")
    if not isinstance(sources_in, list) or not sources_in:
        raise WatchError("sources must be a nonempty list")
    sources: list[dict[str, Any]] = []
    corpora: set[str] = set()
    for item in sources_in:
        if not isinstance(item, dict):
            raise WatchError("every source must be an object")
        corpus, kind = item.get("corpus"), item.get("kind")
        if not isinstance(corpus, str) or not SAFE_NAME.fullmatch(corpus) or corpus in corpora:
            raise WatchError("every source needs a unique, safe corpus name")
        if kind not in SOURCE_KINDS:
            raise WatchError(f"source kind must be one of {SOURCE_KINDS}")
        corpora.add(corpus)
        source: dict[str, Any] = {
            "corpus": corpus,
            "kind": kind,
            "path": _resolve(item.get("path"), base, "source path"),
            "labels": _resolve(item["labels"], base, "labels") if item.get("labels") else None,
        }
        if kind == "jobs":
            depth = item.get("max_depth", 2)
            if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 6:
                raise WatchError("jobs max_depth must be an integer in [0, 6]")
            marker = item.get("ready_marker")
            if marker is not None and (
                not isinstance(marker, str) or not marker or "/" in marker or marker.startswith(".")
            ):
                raise WatchError("ready_marker must be a plain file name")
            source.update(max_depth=depth, ready_marker=marker)
        sources.append(source)

    tuning = dict(TUNING_DEFAULTS)
    given = doc.get("watch", {})
    if not isinstance(given, dict) or set(given) - set(TUNING_DEFAULTS):
        raise WatchError(f"watch tuning keys must be within {sorted(TUNING_DEFAULTS)}")
    tuning.update(given)
    for key, value in tuning.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise WatchError(f"watch.{key} must be a finite nonnegative number")
    if tuning["max_runs_per_increment"] < 1 or tuning["poll_seconds"] <= 0:
        raise WatchError("max_runs_per_increment must be >= 1 and poll_seconds > 0")
    if tuning["watchdog_max_age_seconds"] < 2 * tuning["poll_seconds"]:
        raise WatchError("watchdog_max_age_seconds must be at least twice poll_seconds")
    tuning["max_runs_per_increment"] = int(tuning["max_runs_per_increment"])

    state = State(state_dir)
    judge_in = doc.get("judge", {})
    if not isinstance(judge_in, dict):
        raise WatchError("judge must be an object")
    queue = _resolve(judge_in.get("queue") or str(state.root / "judge-queue.jsonl"), base, "queue")
    verdicts = _resolve(
        judge_in.get("verdicts") or str(state.root / "station" / "verdicts.jsonl"), base, "verdicts"
    )
    write = judge_in.get("write", True)
    if not isinstance(write, bool):
        raise WatchError("judge.write must be true or false")

    autoreview = doc.get("autoreview", {"mode": "off"})
    if isinstance(autoreview, dict) and autoreview.get("mode") == "live":
        raise WatchError("the watcher never calls a model: use autoreview mode off or cached")
    if isinstance(autoreview, dict) and autoreview.get("cache"):
        autoreview = {
            **autoreview,
            "cache": _resolve(autoreview["cache"], base, "autoreview.cache"),
        }
    j3 = autoreview.get("j3") if isinstance(autoreview, dict) else None
    if isinstance(j3, dict):
        if j3.get("mode") == "live":
            raise WatchError("the watcher never calls a model: use J3 mode off or cached")
        if j3.get("cache"):
            autoreview = {
                **autoreview,
                "j3": {**j3, "cache": _resolve(j3["cache"], base, "autoreview.j3.cache")},
            }
    template_doc = {
        "schema": "evallab.line_spec/v1",
        "batch_id": "template",
        "split": "dev",
        "sources": [
            {"corpus": s["corpus"], "rollouts": s["path"], "labels": s["labels"]} for s in sources
        ],
        "task_status": (
            _resolve(doc["task_status"], base, "task_status") if doc.get("task_status") else None
        ),
        "autoreview": autoreview,
        "models": doc.get("models", {"allowed": []}),
        "audit": doc.get("audit", {"rate": 0.05, "seed": watch_id}),
        "judge": {"queue": queue, "write": False, "verdicts": verdicts},
        "labels_policy": doc.get("labels_policy", {"attempted_cheat": "exclude"}),
        "out_root": str(state.root / "increments"),
    }
    for key in ("task_action", "task_policy", "goodfire_review_floor"):
        if key in doc:
            template_doc[key] = doc[key]
    with tempfile.TemporaryDirectory(prefix="line-watch-config-") as scratch:
        template_path = Path(scratch) / "spec.json"
        write_json(template_path, template_doc)
        spec = load_spec(template_path)
    line = {
        key: spec[key]
        for key in (
            "task_status",
            "task_action",
            "task_policy",
            "autoreview",
            "models",
            "audit",
            "labels_policy",
            "goodfire_review_floor",
        )
        if key in spec
    }
    return {
        "schema": CONFIG_SCHEMA,
        "watch_id": watch_id,
        "split": "dev",
        "sources": sources,
        "line": line,
        "judge": {"queue": queue, "write": write, "verdicts": verdicts},
        "watch": tuning,
    }


def config_identity(config: dict[str, Any]) -> str:
    """Hash of everything except tuning: a change needs a new state directory."""
    return canonical_hash({k: v for k, v in config.items() if k != "watch"})


def init_state(state: State, config: dict[str, Any], *, code_rev: str, moment: float) -> None:
    """Create the state directory for ``config`` or confirm it already belongs to it.

    The frozen task status is written first and ``watch.json`` last, so a crash in between
    leaves a folder that is simply initialised again.
    """
    identity = config_identity(config)
    if state.watch_json.is_file():
        stored = read_json(state.watch_json)
        if stored.get("config_sha256") != identity:
            raise WatchError(
                "state directory was created for a different configuration; "
                f"use a new --state-dir (stored {stored.get('config_sha256')}, got {identity})"
            )
        stored["config"]["watch"] = config["watch"]  # tuning may change
        write_json(state.watch_json, stored)
        return
    if state.increments.exists() and any(state.increments.iterdir()):
        raise WatchError("state directory has increments but no watch.json; refusing to adopt it")
    for directory in (state.root, state.frozen, state.increments):
        directory.mkdir(parents=True, exist_ok=True)
    task_status = config["line"]["task_status"]
    task_sha = None
    if task_status:
        frozen = state.frozen / "task-status.json"
        frozen.write_bytes(read_bytes(Path(task_status)))
        task_sha = digest(frozen)
    write_json(
        state.watch_json,
        {
            "schema": STATE_SCHEMA,
            "created_at": now_iso(moment),
            "code_rev": code_rev,
            "config_sha256": identity,
            "frozen_task_status_sha256": task_sha,
            "config": config,
        },
    )


def frozen_task_status(state: State) -> Path | None:
    path = state.frozen / "task-status.json"
    return path if path.is_file() else None


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
