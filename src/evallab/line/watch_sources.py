"""List finished DEV runs in live sources. Nothing is read, copied or frozen here beyond a look.

Two source kinds, both explicit DEV roots (nothing scans a whole results home):

* ``manifest``: a growing JSONL manifest of rows ``{"id", "task_id", "path", ...}``. A row is a
  run once its ``path`` exists. An unterminated last line is a write in progress. Rows without a
  ``path`` (inline steps) or naming their task as ``pair`` are not supported and are held.
* ``jobs``: a directory of Harbor jobs. A job counts as finished with exactly the predicate
  ``evallab.results.load_job`` uses (``finished_at``, ``stats`` and ``n_total_trials`` present,
  indexed trial count equal to it); each of its trial folders with a trajectory is one run,
  identified by its ``trial_name``. ``ready_marker`` names a file that must exist too, e.g.
  ``provenance.json``, which ``results_home.publish_job`` writes last.

A run is named to the line by the canonical real path of its trial folder or file, with no link
at or below the folder the operator named. Before it is planned, the Judge Station's own gate
(``rollout_admission.admit_rollout``) must admit that path, which also reads the sidecars of
every folder above it. A forbidden run refuses (and stops the watcher); one that is merely not
readable yet is held. Everything else about a run (DEV rules, label rules, checks) is the line's
own, applied when the line intakes the increment.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab import hack_drop as hd
from evallab import rollout_admission as ra
from evallab.line.core import reject_label_fields
from evallab.line.io import canonical_hash, read_bytes
from evallab.line.watch_state import WatchError, WatchRefused


class HoldRun(Exception):
    """The run cannot be used yet (still being written, unreadable); it is looked at again later."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def tree_signature(path: Path) -> str:
    """Cheap change detector: sizes and mtimes of every file, never contents."""
    entries: list[tuple[str, int, int, bool]] = []
    if path.is_file() and not path.is_symlink():
        info = path.stat()
        entries.append(("", info.st_size, info.st_mtime_ns, False))
    else:
        for directory, dirnames, filenames in os.walk(path):
            dirnames.sort()
            for name in sorted(filenames):
                file = Path(directory) / name
                info = file.lstat()
                entries.append(
                    (
                        file.relative_to(path).as_posix(),
                        info.st_size,
                        info.st_mtime_ns,
                        file.is_symlink(),
                    )
                )
    return canonical_hash(entries)


def _job_signature(job: Path) -> str:
    """The files directly in a job folder (its result, config, provenance, ...), not its trials."""
    entries = []
    for file in sorted(p for p in job.iterdir() if p.is_file() or p.is_symlink()):
        info = file.lstat()
        entries.append((file.name, info.st_size, info.st_mtime_ns))
    return canonical_hash(entries)


@dataclass(frozen=True)
class Candidate:
    corpus: str
    run_id: str
    listed: str  # the canonical real path handed to the line
    shape: str  # "trial" (a Harbor trial folder) or "path" (a manifest row's file or folder)
    row: dict[str, Any]  # what the line is told: id, task_id, path and, when given, model
    signature: str
    finished_at: str | None = None

    @property
    def key(self) -> str:
        return f"{self.corpus}|{self.run_id}"


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_bytes(path).decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _hold(corpus: str, key: str, reason: str) -> dict[str, str]:
    return {"corpus": corpus, "key": key, "reason": reason}


def canonical_path(path: Path, root: Path) -> str:
    """``path`` as the line will see it: ``root`` resolved (an operating-system link above the
    folder the operator named, like macOS ``/tmp``, is fine), everything below it link-free.

    Any link refuses (a link into forbidden ground first, by name), never followed.
    """
    spelled = Path(os.path.abspath(path))
    base = Path(os.path.abspath(root))
    try:
        relative = spelled.relative_to(base)
        listed = os.path.join(os.path.realpath(base), *relative.parts)
    except ValueError:  # outside the named folder: only the top-level name may be a system link
        top, *rest = spelled.parts[1:] or [""]
        listed = os.path.join(os.path.realpath(os.path.join(spelled.anchor, top)), *rest)
    if os.path.realpath(listed) != listed:
        hd.check_dev_path_allowed(spelled)
        raise WatchRefused(f"a link at or below the named folder: {spelled}")
    return listed


def _make(
    corpus: str, run_id: str, listed: str, shape: str, row: dict[str, Any], finished_at: str | None
) -> Candidate:
    path = Path(listed)
    tree = tree_signature(path)
    if path.is_dir():  # a folder rollout also depends on the files of the job folder above it
        tree = canonical_hash([tree, _job_signature(path.parent)])
    return Candidate(corpus, run_id, listed, shape, row, canonical_hash([row, tree]), finished_at)


def _discover_manifest(
    source: dict[str, Any], admitter: Admitter
) -> tuple[list[Candidate], list[dict[str, str]]]:
    manifest = Path(source["path"])
    corpus = source["corpus"]
    candidates: list[Candidate] = []
    held: list[dict[str, str]] = []
    if not manifest.is_file():
        return candidates, held
    try:
        text = read_bytes(manifest).decode("utf-8")
    except UnicodeDecodeError as error:
        raise WatchError(f"manifest is not UTF-8: {manifest}") from error
    for number, line in enumerate(text.split("\n")[:-1], 1):  # the last piece may be half-written
        if not line.strip():
            continue
        try:
            row = hd.strict_json_loads(line)
        except hd.DuplicateKeyError as error:
            raise WatchRefused(f"manifest {manifest.name} line {number}: {error}") from error
        except ValueError:
            row = None
        if not isinstance(row, dict):
            held.append(_hold(corpus, f"line {number}", "bad_row"))
            continue
        hd._check_row_dev(row)  # a non-DEV row aborts the cycle, it is never skipped
        try:
            reject_label_fields(row, f"manifest {manifest.name} line {number}")
        except ValueError as error:
            raise WatchRefused(str(error)) from error
        admitter.check_row(row)
        run_id = row.get("id") or row.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            held.append(_hold(corpus, f"line {number}", "no_id"))
            continue
        if not row.get("path") or "pair" in row or "steps" in row:
            held.append(_hold(corpus, run_id, "unsupported_row"))
            continue
        raw = Path(str(row["path"])).expanduser()
        path = raw if raw.is_absolute() else manifest.parent / raw
        hd.check_dev_path_allowed(path)
        if not os.path.lexists(path):
            held.append(_hold(corpus, run_id, "missing_path"))
            continue
        try:
            listed = canonical_path(path, manifest.parent)
            # The line gets the row as written (only the path made canonical): every field it
            # reads, such as a reward or a task mapping, reaches it unchanged.
            candidates.append(
                _make(corpus, run_id, listed, "path", {**row, "id": run_id, "path": listed}, None)
            )
        except HoldRun as hold:
            held.append(_hold(corpus, run_id, hold.reason))
        except OSError:
            held.append(_hold(corpus, run_id, "unreadable"))
    return candidates, held


def _is_finished_job(result: dict[str, Any]) -> bool:
    """The predicate ``evallab.results.load_job`` applies to a job-level result.json."""
    return "n_total_trials" in result and "stats" in result and bool(result.get("finished_at"))


def _job_dirs(root: Path, depth: int) -> Iterable[Path]:
    """Folders holding a job-level ``result.json``, found within ``depth`` levels of ``root``."""
    if (root / "result.json").is_file():
        yield root
        return
    if depth <= 0 or not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_symlink():
            hd.check_dev_path_allowed(child)
            raise WatchRefused(f"a link among the jobs of {root.name}: {child.name}")
        if child.is_dir():
            yield from _job_dirs(child, depth - 1)


def _discover_jobs(source: dict[str, Any]) -> tuple[list[Candidate], list[dict[str, str]]]:
    corpus = source["corpus"]
    candidates: list[Candidate] = []
    held: list[dict[str, str]] = []
    root = Path(source["path"])
    marker = source.get("ready_marker")
    for job in _job_dirs(root, source["max_depth"]):
        hd.check_dev_path_allowed(job)
        result = _json_object(job / "result.json")
        if not _is_finished_job(result):
            held.append(_hold(corpus, job.name, "job_unfinished"))
            continue
        if marker and not (job / marker).is_file():
            held.append(_hold(corpus, job.name, "job_unpublished"))
            continue
        trials: list[tuple[Path, dict[str, Any]]] = []
        for child in sorted(job.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_symlink():
                hd.check_dev_path_allowed(child)
                raise WatchRefused(f"a link inside job {job.name}: {child.name}")
            elif child.is_dir():
                trial = _json_object(child / "result.json")
                if "task_name" in trial and "trial_name" in trial:
                    trials.append((child, trial))
        if result["n_total_trials"] != len(trials):
            held.append(_hold(corpus, job.name, "job_trial_count_mismatch"))
            continue
        for directory, trial in trials:
            run_id = str(trial["trial_name"])
            if not (
                (directory / "agent" / "trajectory.json").is_file()
                or (directory / "trajectory.json").is_file()
            ):
                held.append(_hold(corpus, run_id, "no_trajectory"))
                continue
            hd.check_dev_path_allowed(str(trial["task_name"]))
            finished = trial.get("finished_at")
            try:
                listed = canonical_path(directory, root)
                told = {"id": run_id, "task_id": str(trial["task_name"]), "path": listed}
                candidates.append(
                    _make(
                        corpus,
                        run_id,
                        listed,
                        "trial",
                        told,
                        finished if isinstance(finished, str) else None,
                    )
                )
            except HoldRun as hold:
                held.append(_hold(corpus, run_id, hold.reason))
            except OSError:
                held.append(_hold(corpus, run_id, "unreadable"))
    return candidates, held


class Admitter:
    """The Judge Station's rollout gate, applied to each candidate once per file signature."""

    def __init__(self, repo_root: Path) -> None:
        try:
            registry = hd._get_corpus_registry(repo_root)
        except RuntimeError as error:
            raise WatchError(f"cannot admit rollouts without a corpus registry: {error}") from error
        if registry is None or not hasattr(registry, "assigned_test_anywhere"):
            raise WatchError("cannot admit rollouts without a corpus registry")
        self.keys = repo_root / "research" / "answer-keys"
        self.registry = registry
        self.sealed: frozenset[tuple[int, int]] = frozenset()
        self._done: dict[str, str] = {}
        self.inodes: dict[str, frozenset[tuple[int, int]]] = {}
        self.begin_cycle()

    def begin_cycle(self) -> None:
        """Re-read the sealed answer keys (a file added since the last cycle counts at once); a
        run admitted before the set grew is looked at again."""
        sealed = ra.sealed_inodes(self.keys) if self.keys.is_dir() else frozenset()
        if sealed != self.sealed:
            self.sealed = sealed
            self._done.clear()

    def check_row(self, row: dict[str, Any]) -> None:
        """A manifest row the watcher will not plan (held) is still refused when it names TEST
        or sealed material, a forbidden path, a non-ASCII name or another split."""
        if ra.declares_non_dev(row):
            raise WatchRefused("manifest row declares a split that is not dev")
        names, digests = ra.identities(row)
        for key in ("id", "run_id", "path", "pair", "trial_name"):
            value = row.get(key)
            if isinstance(value, str):
                names.append(value)
            elif isinstance(value, dict):
                names += [v for k in ("path", "name") if isinstance(v := value.get(k), str)]
        try:
            for name in names:
                ra._ascii(name, "manifest row name")
                hd.check_dev_path_allowed(name)
            ra._refuse_test_identity(self.registry, names, digests)
        except ra.AdmissionRefused as error:
            raise WatchRefused(f"manifest row is not admitted: {error.detail}") from error

    def shares_a_file(self, keys: set[tuple[Any, ...]], candidates: list[Candidate]) -> bool:
        """Whether a declared file (a label file or a cache file, by ``("id", dev, inode)``) is
        also one of the files an admitted run was read from: the line's own check."""
        from evallab.line.core import _shares_an_inode

        return any(
            _shares_an_inode(keys, self.inodes.get(candidate.key, frozenset()))
            for candidate in candidates
        )

    def admit(self, candidate: Candidate) -> None:
        if self._done.get(candidate.key) == candidate.signature:
            return
        try:
            admitted = ra.admit_rollout(
                candidate.listed,
                registry=self.registry,
                row=candidate.row,
                run_id=candidate.run_id,
                sealed=self.sealed,
                require_trajectory=candidate.shape == "trial",
            )
        except ra.AdmissionRefused as error:
            if error.unreadable:
                raise HoldRun("unreadable") from error
            raise WatchRefused(
                f"run {candidate.run_id!r} is not admitted: {error.detail}"
            ) from error
        self._done[candidate.key] = candidate.signature
        self.inodes[candidate.key] = admitted.inodes


@dataclass
class Found:
    fresh: list[Candidate]  # admitted, not yet committed
    held: list[dict[str, str]]
    visible: int  # runs seen, committed ones included


def discover(sources: list[dict[str, Any]], admitter: Admitter, committed: set[str]) -> Found:
    """Every run currently visible in the sources, admitted, plus why anything else is not usable.

    A committed run id is never planned again. A new run id seen twice with identical content is
    one run; with different content (or in two corpora) every copy is held, because the line
    refuses duplicate run ids.
    """
    admitter.begin_cycle()
    found: list[Candidate] = []
    held: list[dict[str, str]] = []
    for source in sources:
        hd.check_dev_path_allowed(source["path"])
        candidates, reasons = (
            _discover_manifest(source, admitter)
            if source["kind"] == "manifest"
            else _discover_jobs(source)
        )
        found.extend(candidates)
        held.extend(reasons)
    # Every copy of every run is admitted first, committed and duplicate ones too: holding a
    # run for another reason never hides a forbidden one.
    admitted: list[Candidate] = []
    for candidate in found:
        try:
            admitter.admit(candidate)
        except HoldRun as hold:
            held.append(_hold(candidate.corpus, candidate.run_id, hold.reason))
        else:
            admitted.append(candidate)
    by_id: dict[str, list[Candidate]] = {}
    for candidate in admitted:
        by_id.setdefault(candidate.run_id, []).append(candidate)
    fresh: list[Candidate] = []
    for run_id, copies in sorted(by_id.items()):
        if run_id in committed:
            held.extend(_hold(c.corpus, run_id, "already_committed") for c in copies[1:])
            continue
        if len({c.corpus for c in copies}) != 1 or len({c.signature for c in copies}) != 1:
            held.extend(_hold(c.corpus, run_id, "conflicting_duplicate") for c in copies)
            continue
        fresh.append(min(copies, key=lambda c: c.listed))
    return Found(fresh, held, len(by_id))


def increment_rows(candidates: list[Candidate]) -> dict[str, list[dict[str, Any]]]:
    """The rows the line is given, per corpus: what ``discover`` named, nothing copied."""
    rows: dict[str, list[dict[str, Any]]] = {}
    for candidate in sorted(candidates, key=lambda c: c.run_id):
        rows.setdefault(candidate.corpus, []).append(candidate.row)
    return rows


def freeze_labels(
    source: dict[str, Any], run_ids: Iterable[str], repo_root: Path, attempted_cheat: str
) -> list[dict[str, Any]]:
    """This increment's labels, read through the one shared loader, for the scoreboard only."""
    attempted: set[str] = set()
    labels = hd.load_labels(
        source["labels"],
        repo_root=repo_root,
        attempted_cheat=attempted_cheat,
        attempted_ids=attempted,
    )
    rows: list[dict[str, Any]] = []
    for run_id in sorted(set(run_ids)):
        if run_id in attempted:
            rows.append({"id": run_id, "label": "attempted_cheat"})
        elif run_id in labels:
            rows.append({"id": run_id, "is_cheat": labels[run_id]})
    return rows


def freeze_cache(cache: str, run_ids: Iterable[str]) -> list[dict[str, Any]]:
    """The stored AutoReview verdicts of this increment's runs, re-validated by the line itself.

    A run with no stored verdict is simply absent: the line sends it to review (cache miss).
    """
    from evallab.line import core

    verdicts, _ = core._cache({"autoreview": {"mode": "cached", "cache": cache}})
    rows: list[dict[str, Any]] = []
    for run_id in sorted(set(run_ids)):
        verdict = verdicts.get(run_id)
        if verdict is not None:
            rows.append(
                {
                    "id": run_id,
                    "stage_decisions": {
                        "autoreview": {
                            "decision": verdict.decision,
                            "reasons": list(verdict.reasons),
                            "details": dict(verdict.details),
                            "evidence_steps": list(verdict.evidence_steps),
                        }
                    },
                }
            )
    return rows
