"""One scheduled, guarded tick of private trace-store synchronization.

launchd schedules this command; each invocation is a single bounded pass. The
existing ``sync`` is the only uploader, so source identity, DEV/admission gates,
privacy/secret rejection, receipts, and per-provider monthly byte caps stay there.
This module only decides *whether* to call it, and latches a target when hosted
work was refused so later ticks never repeat a refused upload.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evallab.trace_store.state import now, record_spend, state_root
from evallab.trace_store.sync import sync as run_sync

TARGETS = ("laminar", "docent")
DEFAULT_DB = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"
# A versioned runtime adoption changes this path and so reconsiders earlier
# policy rejections without any operator action.
RUNTIME_SOURCE = str(Path(__file__).resolve())
STATE_VERSION = 1
STOP_TEXT_LIMIT = 300


class WatchError(RuntimeError):
    """Fail closed before any hosted call. The message is a static code."""


def _entry(**fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "paused": False, "reason": None, "error_type": None, "stopped": None,
        "cursor": None, "signature": None, "last": None, "at": None,
    }
    entry.update(fields)
    return entry


def _targets(requested: Sequence[str] | None) -> list[str]:
    if requested is None:
        return list(TARGETS)
    selected = list(dict.fromkeys(requested))
    if not selected or any(name not in TARGETS for name in selected):
        raise WatchError("target_invalid")
    return selected


def _resumed(requested: Sequence[str], selected: list[str]) -> set[str]:
    names = set(requested)
    if not names <= {*TARGETS, "all"}:
        raise WatchError("resume_invalid")
    return set(selected) if "all" in names else names & set(selected)


def _stat(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _signature(database: Path, repo_root: Path) -> dict[str, Any]:
    """Observe the DB (and -wal, never -shm) before the pass that will consume it."""
    try:
        db = _stat(database)
        with database.open("rb"):
            pass
    except OSError:
        raise WatchError("db_unreadable") from None
    try:
        wal: list[int] | None = _stat(database.with_name(database.name + "-wal"))
    except FileNotFoundError:
        wal = None
    except OSError:
        raise WatchError("db_unreadable") from None
    return {"runtime": RUNTIME_SOURCE, "repo_root": str(repo_root), "db": db, "wal": wal}


def _load_state(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        raise WatchError("state_unreadable") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise WatchError("state_malformed") from None
    targets = data.get("targets") if isinstance(data, dict) else None
    if not isinstance(targets, dict) or data.get("version") != STATE_VERSION:
        raise WatchError("state_malformed")
    for name, entry in targets.items():
        if (
            name not in TARGETS or not isinstance(entry, dict)
            or set(entry) != set(_entry()) or not isinstance(entry["paused"], bool)
            or not (entry["signature"] is None or isinstance(entry["signature"], dict))
        ):
            raise WatchError("state_malformed")
    return targets


def _save_state(path: Path, states: dict[str, dict[str, Any]]) -> None:
    encoded = json.dumps({"version": STATE_VERSION, "targets": states}, sort_keys=True).encode()
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    path.chmod(0o600)


def _seed(log: Path, names: list[str]) -> dict[str, dict[str, Any]]:
    """Adopt a manual run's stop: only each target's latest entry in file order."""
    try:
        stream = log.open("rb")
    except FileNotFoundError:
        return {}
    except OSError:
        raise WatchError("sync_log_unreadable") from None
    latest: dict[str, dict[str, Any]] = {}
    with stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                raise WatchError("sync_log_malformed") from None
            if not isinstance(row, dict):
                raise WatchError("sync_log_malformed")
            name = row.get("target")
            if isinstance(name, str) and name in names:
                if row.get("stopped") is not None and not isinstance(row["stopped"], str):
                    raise WatchError("sync_log_malformed")
                latest[name] = row
    seeded = {}
    for name, row in latest.items():
        if row.get("stopped") is not None:
            cursor = row.get("cursor")
            seeded[name] = _entry(
                paused=True, reason="adopted_manual_stop", stopped=row["stopped"][:STOP_TEXT_LIMIT],
                cursor=cursor if isinstance(cursor, str) else None, at=now(),
            )
    return seeded


def new_laminar_target() -> Any:
    # Lazy: unchanged ticks must not import the SDK or read credentials.
    from evallab.trace_store.laminar import LaminarTarget

    return LaminarTarget()


def _preflight() -> tuple[Any | None, str | None]:
    """Verify Laminar Signals are off before its selected pass. Never leaks provider text."""
    requests = 0
    uploader: Any | None = None
    error: str | None = None
    try:
        uploader = new_laminar_target()
        requests = 1  # the GET is attempted only once construction succeeded
        uploader.check_signals()
        if getattr(uploader, "safe", False) is not True:
            raise RuntimeError("unverified")
    except Exception as exc:
        uploader, error = None, type(exc).__name__
    record_spend(
        "laminar-signals-check", requests=requests, ok=error is None, error_type=error,
        basis="read-only GET /v1/signals guard; no model or paid Signal calls",
    )
    return uploader, error


def _summary(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise TypeError("report")
    return {
        "selected": report.get("selected", 0), "synced": report.get("synced", 0),
        "unchanged": report.get("unchanged", 0), "quarantined": report.get("quarantined", 0),
        "rejected": len(report.get("rejected") or []),
        "sources": report.get("sources") or {}, "payload_bytes": report.get("payload_bytes", 0),
        "sent_bytes": report.get("sent_bytes", 0), "requests": report.get("requests"),
        "wall_seconds": report.get("wall_seconds"), "stopped": report.get("stopped"),
        "cursor": report.get("cursor"),
    }


def _view(status: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": status, "reason": entry["reason"], "error_type": entry["error_type"],
        "stopped": entry["stopped"], "cursor": entry["cursor"], "since": entry["at"],
    }


def _tick(
    repo_root: Path, database: Path, root: Path, selected: list[str], resumed: set[str],
) -> dict[str, Any]:
    state_path = root / "watch-state.json"
    states = _load_state(state_path)
    signature = _signature(database, repo_root)  # unreadable DB fails before hosted work
    absent = [name for name in selected if name not in states]
    if absent:
        seeded = _seed(root / "sync-runs.jsonl", absent)
        for name in absent:
            states[name] = seeded.get(name) or _entry()
        _save_state(state_path, states)
    results: dict[str, dict[str, Any]] = {}
    todo: list[str] = []
    for name in selected:
        entry = states[name]
        if entry["paused"] and name not in resumed:
            results[name] = _view("paused", entry)
        elif name not in resumed and entry["signature"] == signature:
            results[name] = {"status": "unchanged", "last": entry["last"]}
        else:
            todo.append(name)

    def latch(name: str, status: str, **fields: Any) -> None:
        previous = states[name]
        # Signature stays unadvanced and the cursor is retained until resume.
        states[name] = _entry(
            paused=True, signature=previous["signature"], last=previous["last"],
            cursor=fields.pop("cursor", None) or previous["cursor"], at=now(), **fields,
        )
        results[name] = _view(status, states[name])
        _save_state(state_path, states)

    guard: Any | None = None
    # Keep the shared refusal for both-target ticks, even if only Docent is due.
    if todo and "laminar" in selected:
        guard, refusal = _preflight()
        if guard is None:
            for name in todo:
                latch(name, "refused", reason="signals_guard_refused", error_type=refusal)
            todo = []
    for name in todo:
        try:
            pass_signature = _signature(database, repo_root)
            report = run_sync(
                name, repo_root=repo_root, db_path=database, root=root,
                uploader=guard if name == "laminar" else None,
            )
            summary = _summary(report)
        except Exception as exc:  # provider/SDK text may echo content; keep the type only
            latch(name, "error", reason="sync_raised", error_type=type(exc).__name__)
            continue
        if summary["stopped"] is not None:
            latch(
                name, "stopped", reason="sync_stopped", stopped=str(summary["stopped"])[:STOP_TEXT_LIMIT],
                cursor=summary["cursor"],
            )
            continue
        states[name] = _entry(signature=pass_signature, last=summary, cursor=summary["cursor"], at=now())
        results[name] = {"status": "synced", **summary}
        _save_state(state_path, states)
    return {
        "at": now(), "ok": all(r["status"] in {"synced", "unchanged"} for r in results.values()),
        "resumed": sorted(resumed), "targets": {name: results[name] for name in selected},
    }


def watch_once(
    *, repo_root: Path, db_path: Path | None = None, root: Path | None = None,
    targets: Sequence[str] | None = None, resume: Sequence[str] = (),
) -> dict[str, Any]:
    """Run one tick. Raises WatchError (static code) when it must fail closed."""
    if not repo_root.is_absolute() or not repo_root.is_dir():
        raise WatchError("repo_root_invalid")
    selected = _targets(targets)
    resumed = _resumed(resume, selected)
    root = root or state_root()
    root.mkdir(parents=True, exist_ok=True)
    # Distinct from sync.lock, which sync() takes itself; never held across it.
    fd = os.open(root / "watch.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _tick(repo_root, db_path or DEFAULT_DB, root, selected, resumed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evallab.trace_store.watch", description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--target", action="append", choices=TARGETS)
    parser.add_argument("--resume", action="append", choices=(*TARGETS, "all"))
    args = parser.parse_args(argv)
    try:
        report = watch_once(
            repo_root=args.repo_root, db_path=args.db_path, root=args.state_root,
            targets=args.target, resume=args.resume or (),
        )
    except WatchError as exc:
        report = {"at": now(), "ok": False, "error": str(exc)}
    except Exception as exc:
        report = {"at": now(), "ok": False, "error": "unexpected", "error_type": type(exc).__name__}
    sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())