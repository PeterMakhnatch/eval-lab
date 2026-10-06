"""Select Harbor trials whose executed task carries required health tags (HAR-172).

Read-only filter over Harbor job/trial directories. It joins each trial to the
generated ``evallab.task_health_tags/v1`` manifest by retained package
evidence, never by task/job name, then keeps the trials whose manifest row
carries every requested tag (repeat tags are AND).

Binding precedence per trial:

1. The immutable trial ``config.json`` task path is joined to its own trial
   ``lock.json`` task, or to the unique job-``lock.json`` task with the same
   path. Anything else leaves the trial ``unbound``.
2. When the job ``lab-metadata.json`` ``task_staging`` sidecar's
   ``staged_harbor_digest`` agrees with that executed lock digest, the
   sidecar's ``source_package_digest`` is the verified identity and is joined
   against the manifest parent/variant package digests. This branch is
   exclusive: a contradictory verified source never falls back to the weaker
   native match. The sidecar only ever applies to the trial whose lock it
   agrees with, so it cannot label another task in a multi-task job.
3. Otherwise the executed lock digest may match a manifest parent/variant
   Harbor digest directly.
4. If no manifest row has that identity, recorded variants are followed by
   parent digest to the nearest manifest ancestor. Full/Harbor parent pairs
   must agree; missing links, conflicts and cycles never inherit a label.
   These are the ancestor's health labels, not independent variant validation.

Missing/unmatched identities stay excluded with explicit reasons; nothing is
guessed from name suffixes and nothing unverified is reported as sound.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.task_health_tags import KNOWN_TAGS, SCHEMA
from evallab.task_variants import VariantRecord

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


INCLUDED = "included"
TAG_MISMATCH = "tag_mismatch"
UNBOUND = "unbound"
DIGEST_MISMATCH = "digest_mismatch"
AMBIGUOUS = "ambiguous"

_ROW_DIGEST_FIELDS = (
    "parent_digest",
    "parent_harbor_digest",
    "variant_digest",
    "variant_harbor_digest",
)


def _norm(path: str) -> str:
    return os.path.normpath(path)


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


@dataclass(frozen=True)
class _VariantLink:
    record: VariantRecord
    path: Path
    sha256: str

    def proof(self) -> dict[str, str]:
        return {
            "record": str(self.path),
            "record_sha256": self.sha256,
            "variant_digest": self.record.variant_digest,
            "variant_harbor_digest": self.record.variant_harbor_digest,
            "parent_digest": self.record.parent.digest,
            "parent_harbor_digest": self.record.parent.harbor_digest,
            "transform": self.record.transform,
            "variant_status": self.record.status,
        }


class TaskHealthFilter:
    """Filter Harbor trials by manifest tags, bound by package evidence."""

    def __init__(
        self, manifest_path: Path, tags: list[str], *, records_dirs: Sequence[Path] = ()
    ) -> None:
        self._manifest_path = Path(manifest_path)
        try:
            raw = self._manifest_path.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"task-health manifest is unreadable: {self._manifest_path}: {exc}"
            ) from exc
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"task-health manifest is not valid JSON: {self._manifest_path}: {exc}"
            ) from exc
        if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
            raise ValueError(
                f"task-health manifest schema must be {SCHEMA!r}: {self._manifest_path}"
            )
        tasks = manifest.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError(
                f"task-health manifest needs a non-empty tasks list: {self._manifest_path}"
            )
        self._rows = [self._checked_row(index, row) for index, row in enumerate(tasks)]
        self._tags = self._checked_tags(tags)
        self._manifest_sha256 = hashlib.sha256(raw).hexdigest()
        self._package: dict[str, list[int]] = {}
        self._harbor: dict[str, list[int]] = {}
        for index, row in enumerate(self._rows):
            for digest in {row["parent_digest"], row["variant_digest"]}:
                self._package.setdefault(digest, []).append(index)
            for digest in {row["parent_harbor_digest"], row["variant_harbor_digest"]}:
                self._harbor.setdefault(digest, []).append(index)
        self._lineage_packages: dict[str, list[_VariantLink]] = {}
        self._lineage_harbor: dict[str, list[_VariantLink]] = {}
        seen_paths: set[Path] = set()
        for directory in records_dirs:
            directory = directory.resolve()
            if not directory.is_dir():
                raise ValueError(f"task-variant records directory is missing: {directory}")
            for path in sorted(directory.rglob("*.json")):
                path = path.resolve()
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                try:
                    raw = path.read_bytes()
                    record = VariantRecord.model_validate_json(raw)
                except (OSError, ValueError) as exc:
                    raise ValueError(f"invalid task-variant record: {path}: {exc}") from exc
                link = _VariantLink(record, path, hashlib.sha256(raw).hexdigest())
                self._lineage_packages.setdefault(record.variant_digest, []).append(link)
                self._lineage_harbor.setdefault(record.variant_harbor_digest, []).append(link)

    @staticmethod
    def _checked_row(index: int, row: Any) -> dict[str, Any]:
        if not isinstance(row, dict):
            raise ValueError(f"task-health manifest task {index} must be an object")
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"task-health manifest task {index} needs a task_id")
        for field in _ROW_DIGEST_FIELDS:
            digest = row.get(field)
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise ValueError(f"task-health manifest task {task_id!r} has an invalid {field}")
        tags = row.get("tags")
        if (
            not isinstance(tags, list)
            or not tags
            or any(not isinstance(tag, str) or not tag for tag in tags)
        ):
            raise ValueError(f"task-health manifest task {task_id!r} needs a non-empty tags list")
        return {
            "task_id": task_id,
            "parent_digest": row["parent_digest"],
            "parent_harbor_digest": row["parent_harbor_digest"],
            "variant_digest": row["variant_digest"],
            "variant_harbor_digest": row["variant_harbor_digest"],
            "tags": list(tags),
        }

    @staticmethod
    def _checked_tags(tags: list[str]) -> list[str]:
        if not isinstance(tags, list) or not tags:
            raise ValueError("task-health filter needs a non-empty tags list")
        cleaned: list[str] = []
        for tag in tags:
            if not isinstance(tag, str) or tag not in KNOWN_TAGS:
                raise ValueError(f"unknown task-health filter tag: {tag!r}")
            if tag not in cleaned:
                cleaned.append(tag)
        return cleaned

    def select(
        self, trials_by_job: dict[Path, list[Path]]
    ) -> tuple[dict[Path, list[Path]], dict[str, Any]]:
        """Split trials by manifest tags; every input job key is preserved."""
        selected: dict[Path, list[Path]] = {}
        entries: dict[str, dict[str, Any]] = {}
        totals = {
            "trials": 0,
            "included": 0,
            "tag_mismatch": 0,
            "unbound": 0,
            "digest_mismatch": 0,
            "ambiguous": 0,
        }
        for job_dir, trials in trials_by_job.items():
            job = Path(job_dir)
            job_tasks = self._job_lock_tasks(job)
            sidecar = self._task_staging(job)
            kept: list[Path] = []
            for trial in trials:
                entry = self._classify(Path(trial), job_tasks, sidecar)
                totals["trials"] += 1
                totals[entry["status"]] += 1
                entries[str(trial.absolute())] = entry
                if entry["status"] == INCLUDED:
                    kept.append(trial)
            selected[job_dir] = kept
        report = {
            "manifest": {"path": str(self._manifest_path), "sha256": self._manifest_sha256},
            "tags": list(self._tags),
            "totals": totals,
            "trials": entries,
        }
        return selected, report

    @staticmethod
    def _job_lock_tasks(job: Path) -> list[dict[str, Any]]:
        doc = _read_json(job / "lock.json")
        if not isinstance(doc, dict) or not isinstance(doc.get("trials"), list):
            return []
        tasks = []
        for item in doc["trials"]:
            task = item.get("task") if isinstance(item, dict) else None
            if isinstance(task, dict):
                tasks.append(task)
        return tasks

    @staticmethod
    def _task_staging(job: Path) -> dict[str, Any] | None:
        doc = _read_json(job / "lab-metadata.json")
        if not isinstance(doc, dict):
            return None
        staging = doc.get("task_staging")
        return staging if isinstance(staging, dict) else None

    def _classify(
        self,
        trial: Path,
        job_tasks: list[dict[str, Any]],
        sidecar: dict[str, Any] | None,
    ) -> dict[str, Any]:
        config = _read_json(trial / "config.json")
        task = config.get("task") if isinstance(config, dict) else None
        config_path = task.get("path") if isinstance(task, dict) else None
        if not isinstance(config_path, str) or not config_path:
            return {"status": UNBOUND, "reason": "trial config is missing task.path"}
        wanted = _norm(config_path)
        executed, lock_via, problem = self._executed_digest(trial, wanted, job_tasks)
        if executed is None:
            return {"status": UNBOUND, "reason": problem}
        base = {"executed_digest": executed, "lock": lock_via}
        source = self._verified_source(sidecar, executed)
        if source is not None:
            binding = {
                **base,
                "via": "staged-source",
                "staged_harbor_digest": executed,
                "source_package_digest": source,
            }
            return self._resolve("package", source, binding)
        binding = {**base, "via": "native-lock"}
        return self._resolve("harbor", executed, binding)

    def _resolve(self, kind: str, digest: str, binding: dict[str, Any]) -> dict[str, Any]:
        """Walk retained identities, independent of old parent source paths."""
        seen: set[str] = set()
        lineage: list[dict[str, str]] = []
        expected_harbor: str | None = None

        def failure(code: str, reason: str, *, ambiguous: bool = False) -> dict[str, Any]:
            return {
                "status": AMBIGUOUS if ambiguous else DIGEST_MISMATCH,
                "binding": {**binding, "lineage": lineage, "lineage_error": code},
                "reason": reason,
            }

        while True:
            rows = (self._package if kind == "package" else self._harbor).get(digest, [])
            trace_binding = {**binding, "lineage": lineage} if lineage else binding
            if len(rows) > 1:
                return self._ambiguous(rows, trace_binding, digest)
            if rows:
                row = self._rows[rows[0]]
                if expected_harbor is not None and not any(
                    row[package] == digest and row[harbor] == expected_harbor
                    for package, harbor in (
                        ("parent_digest", "parent_harbor_digest"),
                        ("variant_digest", "variant_harbor_digest"),
                    )
                ):
                    return failure(
                        "parent-harbor-mismatch",
                        f"lineage parent {digest} disagrees with the manifest Harbor digest",
                    )
                # Stop at the nearest identity even when its tags do not match.
                return self._tagged(row, trace_binding)

            links = (self._lineage_packages if kind == "package" else self._lineage_harbor).get(
                digest, []
            )
            if not links:
                return failure(
                    "missing-record",
                    f"{kind} digest {digest} matches no manifest identity or retained variant",
                )
            identities = {
                (
                    link.record.variant_digest,
                    link.record.variant_harbor_digest,
                    link.record.parent.digest,
                    link.record.parent.harbor_digest,
                )
                for link in links
            }
            if len(identities) != 1:
                return failure(
                    "conflicting-records",
                    f"{kind} digest {digest} has conflicting variant lineage records: "
                    + ", ".join(str(link.path) for link in links),
                    ambiguous=True,
                )
            link = links[0]
            record = link.record
            if expected_harbor is not None and record.variant_harbor_digest != expected_harbor:
                return failure(
                    "parent-harbor-mismatch",
                    f"lineage parent {digest} disagrees with its retained Harbor digest",
                )
            if record.variant_digest in seen:
                return failure("cycle", f"lineage cycle at {record.variant_digest}")
            seen.add(record.variant_digest)
            lineage.append(link.proof())
            kind = "package"
            digest = record.parent.digest
            expected_harbor = record.parent.harbor_digest

    @staticmethod
    def _executed_digest(
        trial: Path, wanted: str, job_tasks: list[dict[str, Any]]
    ) -> tuple[str | None, str | None, str | None]:
        own_path = trial / "lock.json"
        if own_path.exists():
            doc = _read_json(own_path)
            own = doc.get("task") if isinstance(doc, dict) else None
            if not isinstance(own, dict):
                return None, None, "trial lock task is unreadable"
            path = own.get("path")
            digest = own.get("digest")
            if not isinstance(path, str) or not path or _norm(path) != wanted:
                return None, None, "trial lock task path differs from trial config task.path"
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                return None, None, "trial lock task has no valid digest"
            return digest, "trial-lock", None
        matches = [
            task
            for task in job_tasks
            if isinstance(task.get("path"), str) and task["path"] and _norm(task["path"]) == wanted
        ]
        if not matches:
            return None, None, "trial lock missing and no job-lock task matches the config path"
        if any(
            not isinstance(task.get("digest"), str) or not _DIGEST.fullmatch(task["digest"])
            for task in matches
        ):
            return None, None, "matching job-lock task has no valid digest"
        digests = {task["digest"] for task in matches}
        if len(digests) != 1:
            return None, None, "multiple job-lock task digests match the trial config path"
        return next(iter(digests)), "job-lock", None

    @staticmethod
    def _verified_source(sidecar: dict[str, Any] | None, executed: str) -> str | None:
        if not isinstance(sidecar, dict):
            return None
        staged = sidecar.get("staged_harbor_digest")
        source = sidecar.get("source_package_digest")
        if (
            not isinstance(staged, str)
            or not _DIGEST.fullmatch(staged)
            or staged != executed
            or not isinstance(source, str)
            or not source
        ):
            return None
        return source

    def _tagged(self, row: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
        missing = [tag for tag in self._tags if tag not in row["tags"]]
        if missing:
            return {
                "status": TAG_MISMATCH,
                "task_id": row["task_id"],
                "tags": list(row["tags"]),
                "binding": binding,
                "reason": f"manifest tags lack required {missing}",
            }
        return {
            "status": INCLUDED,
            "task_id": row["task_id"],
            "tags": list(row["tags"]),
            "binding": binding,
        }

    def _ambiguous(self, rows: list[int], binding: dict[str, Any], digest: str) -> dict[str, Any]:
        candidates = sorted({self._rows[index]["task_id"] for index in rows})
        return {
            "status": AMBIGUOUS,
            "binding": {**binding, "candidates": candidates},
            "reason": f"digest {digest} matches multiple manifest rows: {candidates}",
        }
