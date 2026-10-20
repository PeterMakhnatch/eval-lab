"""Bounded, frozen TB4 Hub acquisition through Harbor's native read/download CLI.

Run ``python -m evallab.hub_trials {list,download,verify} --help``. There is no
model execution, SDK import, automatic network retry, or evidence repair here.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from collections import Counter
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

BOARD = "terminal-bench/terminal-bench/4-0-0"
BOARD_ID = "9f966760-00f1-424e-90f5-c964fb6f6091"
PACKAGE = "terminal-bench/terminal-bench"
DATASET_ID = "1922072f-a433-429a-8929-350d5e1bcf02"
DATASET_DIGEST = "sha256:39d9f44b40420cde8fdcc087579c0d72a7e14fa3656d603c3f0d22fb35e27732"
SCHEMA = "evallab.hub_trials/v1"
TERMINAL = {"complete", "missing_evidence", "identity_mismatch"}


class HubError(RuntimeError):
    """A native acquisition or schema failure, without credential-bearing stderr."""


class IntegrityError(HubError):
    """Existing evidence is unsafe, untracked, missing, or changed; never repair it."""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _uuid(value: str) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise HubError("Expected a canonical native UUID")
    return value


def _component(value: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise IntegrityError("Unsafe native trial name")
    if any(char in value for char in ("/", "\\", "\x00")):
        raise IntegrityError("Unsafe native trial name")
    return value


def _safe(path: Path) -> Path:
    """Reject symlinks in every existing component, including the target."""
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise IntegrityError(f"Symlink target is forbidden: {part}")
    return path


def _inside(root: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    if not relative or posix.is_absolute() or ".." in posix.parts or "\\" in relative:
        raise IntegrityError("Unsafe manifest path")
    target = _safe(root.joinpath(*posix.parts))
    if not target.is_relative_to(root):
        raise IntegrityError("Manifest path escapes acquisition root")
    return target


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path: Path, data: bytes, *, replace: bool = False) -> None:
    _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".hub-write-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # Unlike rename(), link() cannot replace a racing existing file.
            os.link(temporary, path)
        _sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _inventory(root: Path) -> dict[str, dict[str, Any]]:
    _safe(root)
    if not root.is_dir():
        raise IntegrityError(f"Missing evidence directory: {root}")
    inventory = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory, name)
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise IntegrityError(f"Non-regular evidence path: {path}")
            with path.open("rb") as stream:
                sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
            inventory[path.relative_to(root).as_posix()] = {
                "sha256": sha256, "bytes": path.stat().st_size,
            }
    return inventory


def _check_inventory(root: Path, expected: dict[str, Any]) -> None:
    if _inventory(root) != expected:
        raise IntegrityError(f"Evidence inventory/hash mismatch: {root}")


class NativeHarbor:
    """Only the observed Harbor 0.24 read/download protocol; one call per attempt."""

    def __init__(self, executable: str = "harbor", timeout_seconds: int = 600):
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def _run(self, args: Sequence[str]) -> bytes:
        try:
            result = subprocess.run(
                [self.executable, *args], capture_output=True, check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise HubError("Native Harbor command timed out; no automatic retry") from exc
        except OSError as exc:
            raise HubError("Native Harbor executable could not be started") from exc
        if result.returncode:
            # Native errors may contain signed URLs/tokens. Never persist or print them.
            raise HubError(f"Native Harbor command failed (exit {result.returncode})")
        return result.stdout

    def json(self, args: Sequence[str]) -> bytes:
        return self._run([*args, "--json"])

    def download(self, trial_id: str, output: Path) -> None:
        self._run(["hub", "trial", "download", trial_id, "--output-dir", str(output)])


def _board(payload: dict[str, Any]) -> None:
    board = payload["leaderboard"]
    if board["id"] != BOARD_ID or board["dataset_version_ids"] != [DATASET_ID]:
        raise HubError("TB4 leaderboard UUID/dataset pin changed")
    if board["package"] != PACKAGE:
        raise HubError("TB4 leaderboard package changed")


def _rows(payload: dict[str, Any], models: Sequence[str], row_ids: Sequence[str]) -> list[dict]:
    _board(payload)
    rows = payload["rows"]
    selected = []
    for model in models:
        matches = [row for row in rows if row["metadata"]["model_display"]["label"] == model]
        if len(matches) != 1:
            raise HubError(f"Model label {model!r} matches {len(matches)} rows; use --row-id")
        selected.extend(matches)
    for row_id in row_ids:
        matches = [row for row in rows if row["id"] == row_id]
        if len(matches) != 1:
            raise HubError(f"Unknown leaderboard row: {row_id}")
        selected.extend(matches)
    if not models and not row_ids:
        selected = rows
    return [
        {
            "id": _uuid(row["id"]), "rank": row.get("rank"),
            "model_label": row["metadata"]["model_display"]["label"],
            "reasoning_effort": row["metadata"].get("reasoning_effort"),
            "n_trials": row["n_trials"], "metrics": row.get("metrics"),
        }
        for row in sorted({row["id"]: row for row in selected}.values(), key=lambda row: row["id"])
    ]


class Acquisition:
    def __init__(self, root: Path, manifest: dict[str, Any], native: NativeHarbor):
        self.root, self.manifest, self.native = root, manifest, native

    def save(self) -> None:
        _atomic(self.root / "manifest.json", _json_bytes(self.manifest), replace=True)

    def fetch(self, *args: str) -> dict[str, Any]:
        """Cache original CLI stdout, never replacing or adopting untracked bytes."""
        key = _digest(_json_bytes(args))
        relative = f"metadata/{key}.json"
        path = _inside(self.root, relative)
        stored = self.manifest["metadata"].get(relative)
        if stored is not None:
            raw = path.read_bytes()
            if {"sha256": _digest(raw), "bytes": len(raw)} != stored:
                raise IntegrityError(f"Metadata hash mismatch: {relative}")
        else:
            if path.exists():
                raise IntegrityError(f"Untracked metadata; not adopting: {relative}")
            raw = self.native.json(args)
            try:
                parsed = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise HubError("Native Harbor returned invalid JSON") from exc
            if not isinstance(parsed, dict):
                raise HubError("Native Harbor returned a non-object JSON response")
            _atomic(path, raw)
            self.manifest["metadata"][relative] = {"sha256": _digest(raw), "bytes": len(raw)}
            self.save()
        return json.loads(raw)

    def pages(self, command: Sequence[str], id_key: str) -> list[dict]:
        items: dict[str, dict] = {}
        total = None
        request = self.manifest["request"]
        for page in range(1, request["max_pages"] + 1):
            result = self.fetch(*command, "--limit", str(request["page_size"]), "--page", str(page))
            if result["page"] != page or result["page_size"] <= 0:
                raise HubError("Invalid native pagination identity")
            if total is None:
                total = result["total"]
            if result["total"] != total:
                raise HubError("Native pagination total changed during census")
            expected_pages = max(1, (total + result["page_size"] - 1) // result["page_size"])
            if result["total_pages"] not in ({0, 1} if total == 0 else {expected_pages}):
                raise HubError("Inconsistent native pagination count")
            for item in result["items"]:
                identity = _uuid(item[id_key])
                if identity in items:
                    raise HubError("Duplicate native identity across pages; census is not stable")
                items[identity] = item
            if page >= result["total_pages"]:
                if len(items) != total:
                    raise HubError("Native pagination omitted entries")
                return [items[key] for key in sorted(items)]
        raise HubError("Native pagination exceeds --max-pages; census was not frozen")

    def discover(self) -> dict[str, Any]:
        request = self.manifest["request"]
        board = self.fetch("hub", "leaderboard", "show", BOARD)
        rows = _rows(board, request["models"], request["row_ids"])
        dataset = self.fetch("version", "show", f"{PACKAGE}@{DATASET_DIGEST}", "--tasks")
        if dataset["id"] != DATASET_ID or dataset["content_hash"] != DATASET_DIGEST:
            raise HubError("Native dataset UUID/digest differs from pinned TB4")
        tasks = {}
        for item in dataset["tasks"]:
            version = item["task_version"]
            package = version["package"]
            name = f"{package['org']['name']}/{package['name']}"
            if name in request["tasks"]:
                tasks[name] = {
                    "task_version_id": item["task_version_id"],
                    "content_hash": "sha256:" + version["content_hash"].removeprefix("sha256:"),
                    "available": item["available"],
                }
        if set(tasks) != set(request["tasks"]):
            raise HubError("Requested task is absent from pinned TB4 task inventory")
        membership: dict[str, list[str]] = {}
        row_trials = {}
        for row in rows:
            entries = self.pages(
                ["hub", "leaderboard", "row", "trial", "list", row["id"]], "trial_id",
            )
            if len(entries) != row["n_trials"]:
                raise HubError("Leaderboard row count differs from native association census")
            row_trials[row["id"]] = entries
            for entry in entries:
                membership.setdefault(entry["trial_id"], []).append(row["id"])
        summaries: dict[str, dict] = {}
        details: dict[str, dict] = {}
        jobs = {}
        remaining = set(membership)
        while remaining:
            seed = min(remaining)
            detail = self.fetch("hub", "trial", "show", seed)
            details[seed] = detail
            if detail["id"] != seed:
                raise HubError("Native trial detail UUID changed")
            job_id = _uuid(detail["job_id"])
            if job_id in jobs:
                raise HubError("Board trial missing from its native job census")
            job = self.fetch("hub", "job", "show", job_id)
            refs = [entry["ref"] for entry in job["config"].get("datasets", []) if entry["name"] == PACKAGE]
            resolved = []
            for ref in sorted(set(refs)):
                version = self.fetch("version", "show", f"{PACKAGE}@{ref}")
                resolved.append({
                    "native_ref": ref, "id": version["id"],
                    "content_hash": version["content_hash"], "version": version.get("version"),
                })
            matching = bool(resolved) and all(
                version["id"] == DATASET_ID and version["content_hash"] == DATASET_DIGEST
                for version in resolved
            )
            trials = self.pages(["hub", "job", "trials", job_id, "--include-retries"], "id")
            if seed not in {trial["id"] for trial in trials}:
                raise HubError("Seed trial missing from native include-retries job census")
            jobs[job_id] = {
                "id": job_id, "name": job.get("name"), "datasets": resolved,
                "dataset_matches": matching, "all_trial_ids": [trial["id"] for trial in trials],
            }
            for trial in trials:
                if trial.get("job_id") != job_id:
                    raise HubError("Job trial has inconsistent native job identity")
                previous = summaries.get(trial["id"])
                if previous is not None and previous != trial:
                    raise HubError("Trial belongs to inconsistent native job censuses")
                summaries[trial["id"]] = trial
            remaining.difference_update(trial["id"] for trial in trials)
        trials = []
        identity_keys = (
            "id", "job_id", "name", "task_name", "model_provider", "model_name", "agent_name",
            "agent_version", "attempt", "n_attempts", "retry_index", "retry_count", "status",
            "reward", "error_type", "started_at", "finished_at",
        )
        for trial_id in sorted(summaries):
            summary = summaries[trial_id]
            task = summary["task_name"]
            if task not in tasks:
                continue
            record: dict[str, Any] = {key: summary.get(key) for key in identity_keys}
            record["row_ids"] = sorted(membership.get(trial_id, []))
            record["selected"] = False
            record["eligibility"] = "not_board_associated"
            if record["row_ids"]:
                detail = details.get(trial_id) or self.fetch("hub", "trial", "show", trial_id)
                if detail["id"] != trial_id or detail["job_id"] != record["job_id"] or detail["task_name"] != task:
                    raise HubError("Native trial detail differs from job identity")
                _component(detail["trial_name"])
                model = detail["agent_info"]["model_info"]
                if model["name"] != record["model_name"] or model["provider"] != record["model_provider"]:
                    raise HubError("Native trial model differs from job census")
                record.update({
                    "trial_name": detail["trial_name"], "agent_info": detail["agent_info"],
                    "lock_task": detail.get("lock", {}).get("task", {}),
                    "native_verifier_result": detail.get("verifier_result"),
                    "native_exception_info": detail.get("exception_info"),
                    "archive_available": bool(detail.get("archive_path")),
                })
                lock = record["lock_task"]
                # Only the lock pins task bytes; config.task.ref is legitimately absent.
                record["lock_task"] = {key: lock.get(key) for key in ("source", "digest")}
                if not jobs[record["job_id"]]["dataset_matches"]:
                    record["eligibility"] = "job_dataset_mismatch"
                elif lock.get("source") != PACKAGE or lock.get("digest") != tasks[task]["content_hash"]:
                    record["eligibility"] = "task_lock_mismatch"
                else:
                    record["eligibility"] = "eligible"
            trials.append(record)
        cells = []
        limit = request["smoke_trials_per_cell"]
        for row in rows:
            for task in sorted(tasks):
                associated = [trial for trial in trials if row["id"] in trial["row_ids"] and trial["task_name"] == task]
                eligible = sorted(
                    (trial for trial in associated if trial["eligibility"] == "eligible"),
                    key=lambda trial: (trial["job_id"], trial["id"]),
                )
                selected = eligible if limit is None else eligible[:limit]
                for trial in selected:
                    trial["selected"] = True
                cells.append({
                    "row_id": row["id"], "model_label": row["model_label"], "task": task,
                    "associated_trial_ids": [trial["id"] for trial in associated],
                    "eligible_trial_ids": [trial["id"] for trial in eligible],
                    "selected_trial_ids": [trial["id"] for trial in selected],
                    "excluded_trial_ids": [trial["id"] for trial in associated if trial["eligibility"] != "eligible"],
                    "sample_omitted_trial_ids": [trial["id"] for trial in eligible if trial not in selected],
                })
        return {
            "request": request, "board": {"ref": BOARD, "id": BOARD_ID, "package_id": board["leaderboard"]["package_id"]},
            "dataset": {key: dataset.get(key) for key in ("id", "content_hash", "revision", "version", "tags")},
            "tasks": tasks, "rows": rows, "row_trials": row_trials,
            "jobs": [jobs[key] for key in sorted(jobs)], "trials": trials, "cells": cells,
            "selection_order": "lexicographic (native job UUID, native trial UUID) within each row/task cell",
            "scope": "full_census" if limit is None else "explicit_smoke_sample",
        }

    def acquire_trial(self, trial: dict[str, Any]) -> None:
        trial_id = trial["id"]
        state = self.manifest["downloads"].setdefault(trial_id, {"status": "pending", "attempts": []})
        if state["status"] in TERMINAL:
            return
        if state["status"] == "staged":
            self.publish(state)
            return
        # Interrupted bytes are inventoried as interrupted, never as complete evidence.
        if state["status"] == "downloading":
            interrupted = state["attempts"][-1]
            interrupted["status"] = "interrupted"
            interrupted["files"] = _inventory(_inside(self.root, interrupted["path"]))
        attempt = {"path": f"staging/{trial_id}/{len(state['attempts']) + 1:04d}", "status": "downloading"}
        stage = _inside(self.root, attempt["path"])
        if stage.exists():
            raise IntegrityError("Untracked download staging directory")
        stage.mkdir(parents=True)
        state["attempts"].append(attempt)
        state["status"] = "downloading"
        self.save()
        try:
            if not trial["archive_available"]:
                raise HubError("Native trial has no archive on record")
            self.native.download(trial_id, stage)
            tree = stage / _component(trial["trial_name"])
            # Inventory the whole stage too: reject links/special files outside the expected tree.
            _inventory(stage)
            if sorted(path.name for path in stage.iterdir()) != [trial["trial_name"]]:
                raise IntegrityError("Native download produced an unexpected directory layout")
            files = _inventory(tree)
            evidence = _evidence(tree, trial)
        except IntegrityError:
            state["status"] = "integrity_error"
            attempt["status"] = "integrity_error"
            self.save()
            raise
        except (HubError, OSError, ValueError) as exc:
            attempt["status"] = "failed"
            attempt["files"] = _inventory(stage)
            state["status"] = "failed"
            state["error"] = str(exc) if isinstance(exc, HubError) else type(exc).__name__
            self.save()
            return
        attempt["status"] = "staged"
        state.update({
            "status": "staged", "staged_path": tree.relative_to(self.root).as_posix(),
            "path": f"trials/{trial['job_id']}/{trial_id}", "files": files, "evidence": evidence,
        })
        self.save()  # Receipt precedes publication, so a crash cannot bless unreceipted bytes.
        self.publish(state)

    def publish(self, state: dict[str, Any]) -> None:
        target = _inside(self.root, state["path"])
        source = _inside(self.root, state["staged_path"])
        if target.exists():
            if source.exists():
                raise IntegrityError("Both staged and published evidence exist")
            _check_inventory(target, state["files"])
        else:
            _check_inventory(source, state["files"])
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, target)
            _sync_dir(target.parent)
        state["status"] = state["evidence"]["status"]
        state["attempts"][-1]["status"] = "published"
        self.save()


def _evidence(tree: Path, trial: dict[str, Any]) -> dict[str, Any]:
    missing, invalid = [], []
    parsed = {}
    for name in ("result.json", "lock.json", "agent/trajectory.json"):
        path = tree / name
        if not path.is_file():
            missing.append(name)
        else:
            try:
                value = json.loads(path.read_bytes())
                if not isinstance(value, dict):
                    invalid.append(name)
                else:
                    parsed[name] = value
            except (ValueError, UnicodeError):
                invalid.append(name)
    trajectory = parsed.get("agent/trajectory.json")
    if trajectory is not None and not isinstance(trajectory.get("steps"), list):
        invalid.append("agent/trajectory.json:steps")
    if not (tree / "verifier/test-stdout.txt").is_file():
        missing.append("verifier/test-stdout.txt")
    reward_files = [name for name in ("verifier/reward.txt", "verifier/reward.json") if (tree / name).is_file()]
    if not reward_files:
        missing.append("verifier/reward.{txt,json}")
    result = parsed.get("result.json", {})
    verifier = result.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    if not isinstance(rewards, dict) or not rewards:
        missing.append("result.json:verifier_result.rewards")
    mismatch = []
    if "lock.json" in parsed:
        lock = parsed["lock.json"].get("task")
        if not isinstance(lock, dict) or any(lock.get(key) != trial["lock_task"].get(key) for key in ("source", "digest")):
            mismatch.append("downloaded task lock differs from frozen native detail")
    if result:
        if result.get("task_name") != trial["task_name"] or result.get("trial_name") != trial["trial_name"]:
            mismatch.append("downloaded task/trial name differs from frozen native detail")
        if result.get("agent_info") != trial["agent_info"]:
            mismatch.append("downloaded agent/model identity differs from frozen native detail")
    return {
        "status": "identity_mismatch" if mismatch else "missing_evidence" if missing or invalid else "complete",
        "missing": missing, "invalid": invalid, "identity_errors": mismatch,
        "hub_trial_id": trial["id"], "result_trial_id": result.get("id"),
        "native_rewards": rewards, "reward_files": reward_files,
        "exception_info": result.get("exception_info"),
        "trial_outcome": "failed" if result.get("exception_info") or trial["status"] == "failed" else trial["status"],
    }


@contextmanager
def _locked(root: Path):
    root = _safe(root)
    root.mkdir(parents=True, exist_ok=True)
    lock = _safe(root / ".download.lock")
    with lock.open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HubError("Another downloader owns this target") from exc
        yield root


def _load(root: Path) -> dict[str, Any]:
    path = _inside(root, "manifest.json")
    try:
        manifest = json.loads(path.read_bytes())
    except (OSError, ValueError, UnicodeError) as exc:
        raise IntegrityError("Missing or invalid acquisition manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise IntegrityError("Unsupported acquisition manifest schema")
    if any(not isinstance(manifest.get(key), dict) for key in ("request", "metadata", "downloads")):
        raise IntegrityError("Invalid acquisition manifest structure")
    selection = manifest.get("selection")
    if selection is not None and not isinstance(selection, dict):
        raise IntegrityError("Invalid frozen selection structure")
    if selection is not None and _digest(_json_bytes(selection)) != manifest.get("selection_sha256"):
        raise IntegrityError("Frozen selection hash mismatch")
    if selection is not None and selection["request"] != manifest["request"]:
        raise IntegrityError("Frozen request differs from frozen selection")
    return manifest


def _integrity(root: Path, manifest: dict[str, Any]) -> list[str]:
    errors = []
    try:
        metadata_root = _inside(root, "metadata")
        actual = _inventory(metadata_root) if metadata_root.exists() else {}
        expected = {}
        for relative, receipt in manifest["metadata"].items():
            path = _inside(root, relative)
            if not path.is_relative_to(metadata_root):
                raise IntegrityError("Metadata receipt is outside metadata/")
            expected[path.relative_to(metadata_root).as_posix()] = receipt
        for relative in sorted(expected):
            if actual.get(relative) != expected[relative]:
                errors.append(f"Metadata hash mismatch: metadata/{relative}")
        if set(actual) - set(expected):
            errors.append("Untracked metadata files; not adopting existing bytes")
    except (OSError, IntegrityError) as exc:
        errors.append(f"Missing/unsafe metadata: {exc}")
    for trial_id, state in manifest["downloads"].items():
        try:
            if state["status"] == "integrity_error":
                raise IntegrityError("Prior integrity failure requires a new target, not repair")
            if state["status"] in TERMINAL or state["status"] == "staged":
                path = _inside(root, state["path"])
                if state["status"] == "staged" and not path.exists():
                    path = _inside(root, state["staged_path"])
                _check_inventory(path, state["files"])
            for attempt in state["attempts"]:
                if "files" in attempt:
                    _check_inventory(_inside(root, attempt["path"]), attempt["files"])
        except (OSError, IntegrityError) as exc:
            errors.append(f"{trial_id}: {exc}")
    return errors


def _report(manifest: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    selection = manifest.get("selection")
    downloads = manifest["downloads"]
    cells = []
    for cell in (selection or {}).get("cells", []):
        states = [downloads.get(identity, {}).get("status", "pending") for identity in cell["selected_trial_ids"]]
        complete = bool(states) and all(state == "complete" for state in states) and not cell["excluded_trial_ids"]
        cells.append({**cell, "evidence_complete": complete, "download_statuses": dict(Counter(states))})
    return {
        "schema": SCHEMA, "state": manifest["state"], "scope": (selection or {}).get("scope"),
        "selection_sha256": manifest.get("selection_sha256"), "integrity_ok": not errors,
        "integrity_errors": errors, "evidence_complete": bool(cells) and all(cell["evidence_complete"] for cell in cells) and not errors,
        "download_statuses": dict(Counter(state["status"] for state in downloads.values())), "cells": cells,
    }


def download(
    output: Path, *, tasks: Sequence[str], models: Sequence[str] = (), row_ids: Sequence[str] = (),
    smoke_trials_per_cell: int | None = None, page_size: int = 1000, max_pages: int = 100,
    native: NativeHarbor | None = None,
) -> dict[str, Any]:
    """Freeze all native identities, then acquire each selected trial once per invocation."""
    if not tasks or not (models or row_ids):
        raise HubError("Download requires explicit --task and --model/--row-id filters")
    if page_size < 1 or page_size > 1000 or max_pages < 1 or (smoke_trials_per_cell is not None and smoke_trials_per_cell < 1):
        raise HubError("Invalid pagination or explicit smoke bound")
    request = {
        "tasks": sorted(set(tasks)), "models": sorted(set(models)), "row_ids": sorted({_uuid(row) for row in row_ids}),
        "smoke_trials_per_cell": smoke_trials_per_cell, "page_size": page_size, "max_pages": max_pages,
    }
    with _locked(output) as root:
        manifest: dict[str, Any]
        if (root / "manifest.json").exists():
            manifest = _load(root)
            if manifest["request"] != request:
                raise HubError("Target has a different frozen request; use a new output directory")
            errors = _integrity(root, manifest)
            if errors:
                raise IntegrityError("; ".join(errors))
        else:
            if any(path.name != ".download.lock" for path in root.iterdir()):
                raise IntegrityError("Nonempty target without a manifest; refusing to adopt evidence")
            manifest = {"schema": SCHEMA, "request": request, "state": "discovering", "metadata": {}, "downloads": {}, "selection": None}
        acquisition = Acquisition(root, manifest, native or NativeHarbor())
        acquisition.save()
        if manifest["selection"] is None:
            try:
                selection = acquisition.discover()
            except (HubError, OSError, KeyError, TypeError, ValueError):
                manifest["state"] = "discovery_failed"
                acquisition.save()
                raise
            manifest["selection"] = selection
            manifest["selection_sha256"] = _digest(_json_bytes(selection))
            manifest["state"] = "acquiring"
            acquisition.save()
        frozen_selection: dict[str, Any] = manifest["selection"]
        for trial in frozen_selection["trials"]:
            if trial["selected"]:
                acquisition.acquire_trial(trial)
        manifest["state"] = "finished"
        acquisition.save()
        return _report(manifest, [])


def verify(output: Path) -> dict[str, Any]:
    """Offline, read-only verification. Hash integrity is not evidence completeness."""
    root = _safe(output)
    manifest = _load(root)
    return _report(manifest, _integrity(root, manifest))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harbor", default="harbor", help="Native Harbor 0.24 executable (download/read only)")
    parser.add_argument("--timeout-seconds", type=int, default=600, help="Deadline per native command; never automatically retried")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List exact TB4 board rows, including effort and stable row UUID")
    acquisition = commands.add_parser("download", help="Freeze a filtered census and download original native evidence")
    for command in (listing, acquisition):
        command.add_argument("--model", action="append", default=[], help="Exact public label; ambiguous labels require --row-id")
        command.add_argument("--row-id", action="append", default=[], help="Exact native row UUID")
    acquisition.add_argument("--task", action="append", required=True, help="Exact task name, e.g. terminal-bench/foodstuff-beta-activity")
    acquisition.add_argument("--output", type=Path, required=True)
    acquisition.add_argument("--smoke-trials-per-cell", type=int, help="Explicit lexicographic sample; omitted means every board-associated attempt")
    acquisition.add_argument("--page-size", type=int, default=1000)
    acquisition.add_argument("--max-pages", type=int, default=100)
    verification = commands.add_parser("verify", help="Verify frozen selection and every recorded evidence hash offline")
    verification.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.timeout_seconds < 1:
            raise HubError("--timeout-seconds must be positive")
        native = NativeHarbor(args.harbor, args.timeout_seconds)
        if args.command == "list":
            payload = json.loads(native.json(["hub", "leaderboard", "show", BOARD]))
            result = {"board": BOARD, "rows": _rows(payload, args.model, args.row_id)}
        elif args.command == "verify":
            result = verify(args.output)
        else:
            result = download(
                args.output, tasks=args.task, models=args.model, row_ids=args.row_id,
                smoke_trials_per_cell=args.smoke_trials_per_cell, page_size=args.page_size,
                max_pages=args.max_pages, native=native,
            )
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if args.command == "list" or result["evidence_complete"] else 2
    except (HubError, OSError, KeyError, TypeError, ValueError) as exc:
        # Never expose native stdout/stderr or arbitrary schema values on failure.
        message = str(exc) if isinstance(exc, HubError) else f"Native schema/local file error ({type(exc).__name__})"
        print(json.dumps({"error": message, "evidence_complete": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
