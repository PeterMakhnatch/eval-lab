"""Read-only drift check: does the task package on disk still match a job lock?

A Harbor job records, per trial, the task package it ran in the job-level
``lock.json`` (``trials[].task.digest`` and ``trials[].task.path``). Task
packages are edited in place over time, so a lock digest from the past may no
longer describe the directory that sits at that path today. This module
recomputes the digest of the directory as it exists *now* and reports, per
lock trial, whether it still matches.

Digest algorithm
----------------
The recomputation uses Harbor's default local-task package digest, exactly as
implemented by ``harbor_task_digest`` in ``evallab.registry`` (this module
re-implements it with the standard library only, because ``registry`` imports
pydantic; the algorithm below is a verbatim copy of the file set, ignore
rules, ordering, and framing):

* file set: top-level ``task.toml``, ``instruction.md``, ``README.md`` (when
  present) plus every file under ``environment/``, ``tests/``, ``solution/``,
  and ``steps/`` (when present);
* ignored: anything under a ``__pycache__`` directory, ``.DS_Store``,
  ``*.pyc`` / ``*.swp`` / ``*.swo``, and names ending in ``~``;
* ordering: ascending POSIX relative path;
* framing: for each file, feed ``f"{relative}\\0{sha256_hex_of_bytes}\\n"``
  into one SHA-256; the result is ``"sha256:" + hexdigest``.

This is *not* ``task_directory_digest`` (the registry's whole-tree digest,
which frames lines as ``f"{file_digest}  ./{relative}\\n"`` over every file).
Verified against a real job on 2026-11-18: for
``runs/checkpoint-oracle-20260814`` the lock digest
``sha256:2c16dfb2...`` equals ``harbor_task_digest`` of
``library/tasks/event-summary`` and differs from its
``task_directory_digest``, so the lock namespace is the Harbor one.

Two digest namespaces — never join them
---------------------------------------
Job lock digests (``lock.json`` -> ``trials[].task.digest``, Harbor package
digest, ``sha256:``-prefixed) and trial ``task_checksum`` values
(``<trial>/result.json`` -> ``task_checksum``, a bare hex string computed by
Harbor over a different definition) are **different namespaces**. They are
both SHA-256-shaped strings about "the task", which makes them easy to
confuse, but they are not comparable and must never be joined, compared, or
used as keys for one another. This module reads only lock digests and never
reads ``task_checksum``. (``evallab.job_verify`` states the same rule.)

The check is strictly read-only: it opens files to hash them and writes
nothing, to the job directory, the task directory, or anywhere else.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from evallab.results import read_json

ALGORITHM = "harbor_task_digest"
ALGORITHM_NOTE = (
    "Recomputed with Harbor's default local-task package digest "
    "(evallab.registry.harbor_task_digest, re-implemented stdlib-only). "
    "Verified to reproduce the lock digest of "
    "runs/checkpoint-oracle-20260814 exactly. Job lock digests and trial "
    "task_checksum values are different namespaces and are never joined."
)

_TOP_LEVEL_FILES = ("task.toml", "instruction.md", "README.md")
_SUBTREES = ("environment", "tests", "solution", "steps")
_IGNORED_SUFFIXES = {".pyc", ".swp", ".swo"}


def harbor_task_digest(task_dir: Path) -> str:
    """Reproduce Harbor's default local-task package digest for ``task_dir``.

    Verbatim re-implementation of ``evallab.registry.harbor_task_digest``;
    see the module docstring for the exact file set and framing.
    """
    files: list[Path] = []
    for relative in _TOP_LEVEL_FILES:
        path = task_dir / relative
        if path.is_file():
            files.append(path)
    for relative in _SUBTREES:
        path = task_dir / relative
        if path.exists():
            files.extend(item for item in path.rglob("*") if item.is_file())

    def ignored(path: Path) -> bool:
        relative = path.relative_to(task_dir)
        return bool(
            "__pycache__" in relative.parts
            or path.name == ".DS_Store"
            or path.suffix in _IGNORED_SUFFIXES
            or path.name.endswith("~")
        )

    digest = hashlib.sha256()
    for path in sorted(
        (path for path in files if not ignored(path)),
        key=lambda item: item.relative_to(task_dir).as_posix(),
    ):
        relative = path.relative_to(task_dir).as_posix()
        file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f"{relative}\0{file_digest}\n".encode())
    return f"sha256:{digest.hexdigest()}"


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _read_object(path: Path) -> dict[str, Any]:
    """Best-effort JSON object read; ``{}`` for missing/malformed/non-object."""
    if not path.is_file():
        return {}
    try:
        value = read_json(path)
    except (OSError, ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _trial_names(job_dir: Path) -> list[dict[str, Any]]:
    """Trial identities from trial subdirectories, for naming lock entries.

    Job lock trials carry no trial name (verified across the local run
    corpus: trial keys are schema/task/agent/environment/... only), so names
    come from each trial directory's ``result.json`` (``trial_name``), with
    the trial's own ``lock.json`` task block kept for matching. Directories
    without a readable ``result.json`` object are skipped.
    """
    found: list[dict[str, Any]] = []
    if not job_dir.is_dir():
        return found
    for candidate in sorted(job_dir.iterdir()):
        if not candidate.is_dir():
            continue
        result = _read_object(candidate / "result.json")
        trial_name = _as_str(result.get("trial_name"))
        if trial_name is None:
            config = _read_object(candidate / "config.json")
            trial_name = _as_str(config.get("trial_name"))
        if trial_name is None:
            continue
        trial_lock = _read_object(candidate / "lock.json")
        task = trial_lock.get("task")
        found.append(
            {
                "trial_name": trial_name,
                "dir_name": candidate.name,
                "task": task if isinstance(task, dict) else {},
            }
        )
    return found


def _match_trial_name(
    task: dict[str, Any],
    pool: list[dict[str, Any]],
) -> str | None:
    """Pop the trial-directory entry that belongs to one lock trial.

    Prefer an exact (name, digest, path) match against the trial's own lock
    task block, then (name, digest), then name alone; fall back to pool order
    (lock trials of one task are interchangeable, so positional assignment
    among same-task trials does not change any status).
    """
    if not pool:
        return None
    lock_name = _as_str(task.get("name"))
    lock_digest = _as_str(task.get("digest"))
    lock_path = _as_str(task.get("path"))

    def score(entry: dict[str, Any]) -> int:
        theirs = entry["task"]
        points = 0
        if lock_name is not None and _as_str(theirs.get("name")) == lock_name:
            points += 1
        if lock_digest is not None and _as_str(theirs.get("digest")) == lock_digest:
            points += 2
        if lock_path is not None and _as_str(theirs.get("path")) == lock_path:
            points += 4
        return points

    best = max(range(len(pool)), key=lambda i: score(pool[i]))
    # Every candidate describes a *different* task; do not mislabel. (A pool
    # entry with an empty task block carries no evidence either way, so
    # positional assignment remains acceptable there.)
    if (
        score(pool[best]) == 0
        and pool[best]["task"]
        and all(entry["task"] for entry in pool)
    ):
        return None
    return str(pool.pop(best)["trial_name"])


def _resolve_task_dir(
    task: dict[str, Any],
    *,
    job_dir: Path,
    task_root: Path | None,
) -> Path | None:
    """Locate the on-disk task directory for one lock task block.

    Lock ``path`` values in the wild are both absolute
    (``/Users/.../library/tasks/event-summary``) and repo-relative
    (``library/tasks/event-summary``), and an absolute path can be stale
    after a checkout moves. ``task_root``, when given, is tried as both a
    repository root (repo-relative lock paths resolve under it) and a tasks
    root (``<task_root>/<task name>``). The first existing directory wins;
    ``None`` means the task cannot be found on this machine.
    """
    raw_path = _as_str(task.get("path"))
    task_name = _as_str(task.get("name"))
    candidates: list[Path] = []
    if raw_path is not None:
        lock_path = Path(raw_path).expanduser()
        if lock_path.is_absolute():
            candidates.append(lock_path)
        else:
            if task_root is not None:
                candidates.append(task_root / lock_path)
            candidates.append(lock_path)
            candidates.append(job_dir / lock_path)
    if task_root is not None:
        if task_name is not None:
            candidates.append(task_root / task_name)
        if raw_path is not None:
            candidates.append(task_root / Path(raw_path).name)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def verify_job_digests(job_dir: Path, *, task_root: Path | None = None) -> dict[str, Any]:
    """Compare a job's lock task digests with the task directories on disk now.

    Parameters
    ----------
    job_dir:
        Harbor job directory containing the job-level ``lock.json``.
    task_root:
        Optional root used to resolve task directories when the lock path is
        relative or stale (tried as repository root and as tasks root; see
        ``_resolve_task_dir``). When omitted, lock paths are used as recorded.

    Returns a JSON-serializable dict::

        {
          "job_dir": str,
          "lock_path": str,
          "algorithm": "harbor_task_digest",
          "algorithm_note": str,
          "harbor_version": str | None,
          "trials": [
            {
              "trial_name": str | None,      # from trial result.json
              "task_name": str | None,       # lock task name
              "lock_digest": str | None,     # lock task digest, verbatim
              "current_digest": str | None,  # recomputed now, if found
              "status": "match" | "drift" | "task_missing" | "no_lock_digest",
              "algorithm": "harbor_task_digest",
              "task_path": str | None,        # lock task path, verbatim
              "resolved_task_path": str | None,
            }, ...
          ],
          "summary": {"match": int, "drift": int,
                      "task_missing": int, "no_lock_digest": int},
          "ok": bool,  # True iff trials is non-empty and every status is match
        }

    A missing or unreadable job ``lock.json`` is reported, not raised:
    ``trials`` is empty and an ``"error"`` key explains why. Trial
    ``task_checksum`` values are never read (different namespace; see the
    module docstring).
    """
    job_dir = Path(job_dir)
    resolved_root = Path(task_root) if task_root is not None else None
    lock_path = job_dir / "lock.json"
    report: dict[str, Any] = {
        "job_dir": str(job_dir),
        "lock_path": str(lock_path),
        "algorithm": ALGORITHM,
        "algorithm_note": ALGORITHM_NOTE,
        "harbor_version": None,
        "trials": [],
        "summary": {"match": 0, "drift": 0, "task_missing": 0, "no_lock_digest": 0},
        "ok": False,
    }

    if not lock_path.is_file():
        report["error"] = f"job lock.json not found: {lock_path}"
        return report
    try:
        lock = read_json(lock_path)
    except (OSError, ValueError, UnicodeError) as exc:
        report["error"] = f"job lock.json unreadable: {type(exc).__name__}: {exc}"
        return report
    if not isinstance(lock, dict):
        report["error"] = "job lock.json is not a JSON object"
        return report

    harbor = lock.get("harbor")
    if isinstance(harbor, dict):
        report["harbor_version"] = _as_str(harbor.get("version"))

    raw_trials = lock.get("trials")
    if not isinstance(raw_trials, list):
        report["error"] = "job lock.json has no trials list"
        return report

    name_pool = _trial_names(job_dir)
    trials: list[dict[str, Any]] = []
    for raw_trial in raw_trials:
        trial = raw_trial if isinstance(raw_trial, dict) else {}
        task = trial.get("task")
        task = task if isinstance(task, dict) else {}
        lock_digest = _as_str(task.get("digest"))
        resolved = _resolve_task_dir(task, job_dir=job_dir, task_root=resolved_root)
        current_digest = harbor_task_digest(resolved) if resolved is not None else None

        if lock_digest is None:
            status = "no_lock_digest"
        elif resolved is None:
            status = "task_missing"
        elif current_digest == lock_digest.strip().lower() or current_digest == lock_digest:
            status = "match"
        else:
            status = "drift"

        trials.append(
            {
                "trial_name": _match_trial_name(task, name_pool),
                "task_name": _as_str(task.get("name")),
                "lock_digest": lock_digest,
                "current_digest": current_digest,
                "status": status,
                "algorithm": ALGORITHM,
                "task_path": _as_str(task.get("path")),
                "resolved_task_path": str(resolved) if resolved is not None else None,
            }
        )
        report["summary"][status] += 1

    report["trials"] = trials
    report["ok"] = bool(trials) and all(entry["status"] == "match" for entry in trials)
    return report


__all__ = ["ALGORITHM", "ALGORITHM_NOTE", "harbor_task_digest", "verify_job_digests"]
