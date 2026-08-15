"""Explicit task registry and admission trust boundary for eval-lab.

Task registration is an explicit, inspectable, human-owned fact.
Filesystem location, existence of task.toml, a curated card, or canary
membership never implies registration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from evallab.schemas import (
    ExperimentSpec,
    TaskAdmissionState,
    TaskDigests,
    TaskRegistryRecord,
)

IGNORED_FILE_NAMES = {".DS_Store", ".git", "__pycache__", ".pytest_cache"}
IGNORED_EXTENSIONS = {".pyc", ".pyo", ".tmp"}


def task_directory_digest(path: Path) -> str:
    """Digest sorted relative paths and file digests, independent of checkout location."""
    if not path.is_dir():
        raise ValueError(f"task directory is missing: {path}")
    aggregate = hashlib.sha256()
    files = sorted(
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file() and not _should_ignore_file(candidate)
    )
    if not files:
        raise ValueError(f"task directory is empty: {path}")
    for candidate in files:
        relative = candidate.relative_to(path).as_posix()
        file_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        aggregate.update(f"{file_digest}  ./{relative}\n".encode())
    return f"sha256:{aggregate.hexdigest()}"


class RegistryError(Exception):
    """Base exception for task registry errors."""


class TaskNotRegisteredError(RegistryError):
    """Raised when a task id is not present in the explicit task registry."""


class TaskStateInvalidError(RegistryError):
    """Raised when a task is in candidate or retired state rather than registered."""


class TaskDigestMismatchError(RegistryError):
    """Raised when on-disk task package bytes do not match the registered digests."""


class TaskPathRedirectionError(RegistryError):
    """Raised when a spec attempts to redirect task_path away from the registered path."""


class TaskVersionMismatchError(RegistryError):
    """Raised when an experiment spec task_version does not match the registered record version."""


def _should_ignore_file(path: Path) -> bool:
    if path.name in IGNORED_FILE_NAMES:
        return True
    return path.suffix in IGNORED_EXTENSIONS


def compute_subpath_digest(path: Path) -> str:
    """Compute deterministic SHA-256 digest of a file or directory tree."""
    if not path.exists():
        return "sha256:" + hashlib.sha256(b"").hexdigest()
    if path.is_file():
        return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    if path.is_dir():
        aggregate = hashlib.sha256()
        files = sorted(
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file() and not _should_ignore_file(candidate)
        )
        for candidate in files:
            relative = candidate.relative_to(path).as_posix()
            file_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            aggregate.update(f"{file_digest}  ./{relative}\n".encode())
        return f"sha256:{aggregate.hexdigest()}"
    return "sha256:" + hashlib.sha256(b"").hexdigest()


def compute_task_digests(task_path: Path) -> TaskDigests:
    """Compute deterministic component and package digests for a task package directory."""
    if not task_path.is_dir():
        raise ValueError(f"task directory does not exist: {task_path}")

    task_toml_file = task_path / "task.toml"
    if not task_toml_file.is_file():
        raise ValueError(f"task package missing task.toml: {task_path}")
    task_toml_digest = compute_subpath_digest(task_toml_file)

    instruction_file = task_path / "instruction.md"
    if instruction_file.is_file():
        instruction_digest = compute_subpath_digest(instruction_file)
    else:
        instruction_digest = task_toml_digest

    environment_dir = task_path / "environment"
    if environment_dir.exists():
        environment_digest = compute_subpath_digest(environment_dir)
    elif (task_path / "Dockerfile").is_file():
        environment_digest = compute_subpath_digest(task_path / "Dockerfile")
    else:
        environment_digest = "sha256:" + hashlib.sha256(b"").hexdigest()

    verifier_dir = task_path / "tests"
    if not verifier_dir.exists():
        verifier_dir = task_path / "verifier"
    verifier_digest = compute_subpath_digest(verifier_dir)

    package_digest = task_directory_digest(task_path)

    return TaskDigests(
        task_toml=task_toml_digest,
        instruction=instruction_digest,
        environment=environment_digest,
        verifier=verifier_digest,
        package=package_digest,
    )


class TaskRegistry:
    """Explicit repository-backed task registry."""

    def __init__(self, root: Path, records: dict[str, TaskRegistryRecord]) -> None:
        self.root = root.resolve()
        self.records = records

    @classmethod
    def from_dir(cls, registry_dir: Path) -> TaskRegistry:
        root = registry_dir.resolve()
        records: dict[str, TaskRegistryRecord] = {}
        if not root.is_dir():
            return cls(root, records)
        for record_file in sorted(root.glob("*.json")):
            try:
                raw = json.loads(record_file.read_text())
                record = TaskRegistryRecord.model_validate(raw)
                if record.task_id in records:
                    raise ValueError(f"duplicate task id in registry: {record.task_id}")
                records[record.task_id] = record
            except (json.JSONDecodeError, ValidationError) as exc:
                raise ValueError(f"invalid registry record {record_file}: {exc}") from exc
        return cls(root, records)

    @classmethod
    def from_repo(cls, repo_root: Path) -> TaskRegistry:
        return cls.from_dir(repo_root / "library/registry")

    def get(self, task_id: str) -> TaskRegistryRecord | None:
        return self.records.get(task_id)

    def list_records(
        self,
        state: TaskAdmissionState | None = None,
    ) -> list[TaskRegistryRecord]:
        records = list(self.records.values())
        if state is not None:
            records = [record for record in records if record.state == state]
        return sorted(records, key=lambda record: record.task_id)

    def resolve_spec(
        self,
        spec: ExperimentSpec,
        repo_root: Path,
    ) -> TaskRegistryRecord | None:
        """Resolve and validate an experiment spec referencing a registered task.

        Returns the TaskRegistryRecord if spec is a registered/* task, or None if
        it is not a registered task reference (e.g. canary/* or local control).
        Raises a RegistryError subclass if registered task invariants are violated.
        """
        if not spec.task.startswith("registered/"):
            return None

        task_id = spec.task.removeprefix("registered/")
        record = self.get(task_id)
        if record is None:
            raise TaskNotRegisteredError(
                f"task {spec.task!r} is not registered in library/registry/"
            )

        if record.state != "registered":
            raise TaskStateInvalidError(
                f"task {task_id!r} has admission state {record.state!r}; "
                "registered state required for registered/* execution"
            )

        if spec.task_path is not None and spec.task_path != record.task_path:
            raise TaskPathRedirectionError(
                f"spec task_path {spec.task_path!r} redirects away from "
                f"registered task_path {record.task_path!r}"
            )

        if spec.task_version is not None and spec.task_version != record.version:
            raise TaskVersionMismatchError(
                f"spec task_version {spec.task_version!r} does not match "
                f"registered version {record.version!r}"
            )

        target_path = (repo_root / record.task_path).resolve()
        if not target_path.is_dir():
            raise TaskDigestMismatchError(
                f"registered task directory does not exist on disk: {record.task_path}"
            )

        current_digests = compute_task_digests(target_path)
        if current_digests.verifier != record.digests.verifier:
            raise TaskDigestMismatchError(
                f"task verifier bytes on disk have changed for {task_id!r} "
                f"(expected {record.digests.verifier}, got {current_digests.verifier})"
            )
        if current_digests.task_toml != record.digests.task_toml:
            raise TaskDigestMismatchError(
                f"task.toml bytes on disk have changed for {task_id!r} "
                f"(expected {record.digests.task_toml}, got {current_digests.task_toml})"
            )
        if current_digests.instruction != record.digests.instruction:
            raise TaskDigestMismatchError(
                f"instruction bytes on disk have changed for {task_id!r} "
                f"(expected {record.digests.instruction}, got {current_digests.instruction})"
            )
        if current_digests.environment != record.digests.environment:
            raise TaskDigestMismatchError(
                f"environment bytes on disk have changed for {task_id!r} "
                f"(expected {record.digests.environment}, got {current_digests.environment})"
            )
        if current_digests.package != record.digests.package:
            raise TaskDigestMismatchError(
                f"task package bytes on disk have changed for {task_id!r} "
                f"(expected {record.digests.package}, got {current_digests.package})"
            )

        return record


@dataclass(frozen=True)
class AuditFinding:
    severity: Literal["error", "warning", "info"]
    category: str
    target: str
    message: str


@dataclass(frozen=True)
class RegistryAuditReport:
    total_records: int
    registered_count: int
    candidate_count: int
    retired_count: int
    findings: list[AuditFinding]

    @property
    def passed(self) -> bool:
        return not any(finding.severity == "error" for finding in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_records": self.total_records,
            "registered_count": self.registered_count,
            "candidate_count": self.candidate_count,
            "retired_count": self.retired_count,
            "passed": self.passed,
            "findings": [
                {
                    "severity": finding.severity,
                    "category": finding.category,
                    "target": finding.target,
                    "message": finding.message,
                }
                for finding in self.findings
            ],
        }


def audit_registry(
    repo_root: Path,
    registry: TaskRegistry | None = None,
) -> RegistryAuditReport:
    """Perform a comprehensive read-only audit of task registration and claims."""
    root = repo_root.resolve()
    reg = registry or TaskRegistry.from_repo(root)
    findings: list[AuditFinding] = []

    registered_records = reg.list_records("registered")
    candidate_records = reg.list_records("candidate")
    retired_records = reg.list_records("retired")

    # 1. Audit Registry Records on disk
    seen_paths: dict[str, str] = {}
    for record in reg.list_records():
        # Duplicate task path checks
        if record.task_path in seen_paths:
            findings.append(
                AuditFinding(
                    severity="error",
                    category="duplicate_task_path",
                    target=record.task_id,
                    message=(
                        f"task_path {record.task_path!r} is also "
                        f"registered by {seen_paths[record.task_path]!r}"
                    ),
                )
            )
        else:
            seen_paths[record.task_path] = record.task_id

        # Existence of package directory
        target_path = root / record.task_path
        if not target_path.is_dir():
            findings.append(
                AuditFinding(
                    severity="error",
                    category="missing_task_directory",
                    target=record.task_id,
                    message=f"task directory {record.task_path!r} does not exist",
                )
            )
            continue

        if not (target_path / "task.toml").is_file():
            findings.append(
                AuditFinding(
                    severity="error",
                    category="missing_task_toml",
                    target=record.task_id,
                    message=f"task directory {record.task_path!r} is missing task.toml",
                )
            )
            continue

        # Digest verification
        try:
            current_digests = compute_task_digests(target_path)
            if current_digests.package != record.digests.package:
                findings.append(
                    AuditFinding(
                        severity="error",
                        category="changed_digest",
                        target=record.task_id,
                        message=(
                            f"package digest mismatch: expected {record.digests.package}, "
                            f"got {current_digests.package}"
                        ),
                    )
                )
            if current_digests.verifier != record.digests.verifier:
                findings.append(
                    AuditFinding(
                        severity="error",
                        category="changed_digest",
                        target=record.task_id,
                        message=(
                            f"verifier digest mismatch: expected {record.digests.verifier}, "
                            f"got {current_digests.verifier}"
                        ),
                    )
                )
        except Exception as exc:
            findings.append(
                AuditFinding(
                    severity="error",
                    category="digest_computation_failed",
                    target=record.task_id,
                    message=f"failed to compute digests for {record.task_path}: {exc}",
                )
            )

        # Control evidence checks
        if record.state == "registered":
            if record.control_evidence.oracle.reward != 1.0:
                findings.append(
                    AuditFinding(
                        severity="error",
                        category="invalid_control_evidence",
                        target=record.task_id,
                        message=(
                            "registered task oracle reward must be 1.0 "
                            f"(got {record.control_evidence.oracle.reward})"
                        ),
                    )
                )
            if record.control_evidence.nop.reward != 0.0:
                findings.append(
                    AuditFinding(
                        severity="error",
                        category="invalid_control_evidence",
                        target=record.task_id,
                        message=(
                            "registered task nop reward must be 0.0 "
                            f"(got {record.control_evidence.nop.reward})"
                        ),
                    )
                )
            if not record.approved_by or not record.approved_at:
                findings.append(
                    AuditFinding(
                        severity="error",
                        category="missing_approval",
                        target=record.task_id,
                        message="registered task requires approved_by and approved_at",
                    )
                )

        # Floating ref check
        if (
            record.source_ref is not None
            and record.provenance_zone == "01-external"
            and any(char in record.source_ref for char in ("latest", "head", "main", "master"))
        ):
            findings.append(
                AuditFinding(
                    severity="warning",
                    category="floating_ref",
                    target=record.task_id,
                    message=f"external source_ref appears unpinned: {record.source_ref!r}",
                )
            )

    # 2. Audit Queue state for false registered/* claims
    queue_root = root / "queue"
    if queue_root.is_dir():
        for state in ("proposed", "pending", "approved", "waiting", "running"):
            state_dir = queue_root / state
            if not state_dir.is_dir():
                continue
            for spec_file in sorted(state_dir.glob("*.json")):
                try:
                    raw_spec = json.loads(spec_file.read_text())
                    task_claim = raw_spec.get("task", "")
                    if isinstance(task_claim, str) and task_claim.startswith("registered/"):
                        task_id = task_claim.removeprefix("registered/")
                        record = reg.get(task_id)
                        if record is None:
                            findings.append(
                                AuditFinding(
                                    severity="error",
                                    category="false_registered_claim",
                                    target=f"{state}/{spec_file.name}",
                                    message=(
                                        f"spec {raw_spec.get('name', spec_file.stem)!r} claims "
                                        f"unregistered task {task_claim!r}"
                                    ),
                                )
                            )
                        elif record.state != "registered":
                            findings.append(
                                AuditFinding(
                                    severity="error",
                                    category="false_registered_claim",
                                    target=f"{state}/{spec_file.name}",
                                    message=(
                                        f"spec {raw_spec.get('name', spec_file.stem)!r} claims "
                                        f"task {task_claim!r} which is in {record.state!r} state"
                                    ),
                                )
                            )
                        else:
                            spec_task_path = raw_spec.get("task_path")
                            if spec_task_path and spec_task_path != record.task_path:
                                findings.append(
                                    AuditFinding(
                                        severity="error",
                                        category="task_path_redirection",
                                        target=f"{state}/{spec_file.name}",
                                        message=(
                                            f"spec redirects task_path to {spec_task_path!r}, "
                                            f"expected {record.task_path!r}"
                                        ),
                                    )
                                )
                except Exception:
                    continue

    # 3. Audit Curated Cards that are non-runnable pointers only
    curated_dir = root / "library/curated"
    if curated_dir.is_dir():
        for card_subdir in sorted(curated_dir.iterdir()):
            if (
                card_subdir.is_dir()
                and not (card_subdir / "task.toml").is_file()
                and (card_subdir / "CARD.md").is_file()
            ):
                findings.append(
                    AuditFinding(
                        severity="info",
                        category="curated_card_pointer_only",
                        target=f"library/curated/{card_subdir.name}",
                        message=(
                            "curated card has provenance documentation but is not a "
                            "local runnable package"
                        ),
                    )
                )

    return RegistryAuditReport(
        total_records=len(reg.records),
        registered_count=len(registered_records),
        candidate_count=len(candidate_records),
        retired_count=len(retired_records),
        findings=findings,
    )


@dataclass(frozen=True)
class TaskInventoryItem:
    task_id: str
    path: str
    category: Literal[
        "runnable_task",
        "curated_card_only",
        "template",
        "benchmark_task",
        "adapter_task",
    ]
    has_task_toml: bool
    has_environment: bool
    has_verifier: bool
    is_canary: bool
    registration_state: TaskAdmissionState | None


@dataclass(frozen=True)
class TaskInventory:
    total_packages: int
    runnable_packages: int
    curated_cards_only: int
    template_packages: int
    canary_tasks: int
    registered_tasks: int
    candidate_tasks: int
    items: list[TaskInventoryItem]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_packages": self.total_packages,
            "runnable_packages": self.runnable_packages,
            "curated_cards_only": self.curated_cards_only,
            "template_packages": self.template_packages,
            "canary_tasks": self.canary_tasks,
            "registered_tasks": self.registered_tasks,
            "candidate_tasks": self.candidate_tasks,
            "items": [
                {
                    "task_id": item.task_id,
                    "path": item.path,
                    "category": item.category,
                    "has_task_toml": item.has_task_toml,
                    "has_environment": item.has_environment,
                    "has_verifier": item.has_verifier,
                    "is_canary": item.is_canary,
                    "registration_state": item.registration_state,
                }
                for item in self.items
            ],
        }


def inventory_tasks(repo_root: Path) -> TaskInventory:
    """Mechanically inventory all task surfaces across the repository."""
    root = repo_root.resolve()
    reg = TaskRegistry.from_repo(root)
    items: list[TaskInventoryItem] = []

    # Canaries from policy
    canary_paths: set[str] = set()
    canary_suite_file = root / "policy/canary-suite.yaml"
    if canary_suite_file.is_file():
        import yaml

        try:
            canary_data = yaml.safe_load(canary_suite_file.read_text()) or {}
            for member in canary_data.get("members", []):
                if "task_path" in member:
                    canary_paths.add(member["task_path"])
        except Exception:
            pass

    # 1. Scan task.toml packages
    library_dir = root / "library"
    if library_dir.is_dir():
        for task_file in sorted(library_dir.rglob("task.toml")):
            task_dir = task_file.parent
            rel_path = task_dir.relative_to(root).as_posix()
            task_id = task_dir.name

            # Determine category
            if "template" in rel_path:
                category = "template"
            elif rel_path.startswith("library/tasks/"):
                category = "runnable_task"
            elif rel_path.startswith("library/benchmarks/"):
                category = "benchmark_task"
            elif rel_path.startswith("library/adapters/"):
                category = "adapter_task"
            else:
                category = "runnable_task"

            has_env = (task_dir / "environment").exists() or (task_dir / "Dockerfile").is_file()
            has_ver = (task_dir / "tests").exists() or (task_dir / "verifier").exists()
            is_canary = rel_path in canary_paths

            reg_record = reg.get(task_id)
            reg_state = (
                reg_record.state
                if reg_record and reg_record.task_path == rel_path
                else None
            )

            items.append(
                TaskInventoryItem(
                    task_id=task_id,
                    path=rel_path,
                    category=category,
                    has_task_toml=True,
                    has_environment=has_env,
                    has_verifier=has_ver,
                    is_canary=is_canary,
                    registration_state=reg_state,
                )
            )

    # 2. Scan curated cards that do not have task.toml
    curated_dir = root / "library/curated"
    if curated_dir.is_dir():
        for card_subdir in sorted(curated_dir.iterdir()):
            if (
                card_subdir.is_dir()
                and not (card_subdir / "task.toml").is_file()
                and (card_subdir / "CARD.md").is_file()
            ):
                rel_path = card_subdir.relative_to(root).as_posix()
                task_id = card_subdir.name
                reg_record = reg.get(task_id)
                reg_state = (
                    reg_record.state
                    if reg_record and reg_record.task_path == rel_path
                    else None
                )

                items.append(
                    TaskInventoryItem(
                        task_id=task_id,
                        path=rel_path,
                        category="curated_card_only",
                        has_task_toml=False,
                        has_environment=False,
                        has_verifier=False,
                        is_canary=False,
                        registration_state=reg_state,
                    )
                )

    items = sorted(items, key=lambda item: (item.category, item.path))
    runnable = sum(1 for item in items if item.has_task_toml and item.category != "template")
    card_only = sum(1 for item in items if item.category == "curated_card_only")
    templates = sum(1 for item in items if item.category == "template")
    canaries = sum(1 for item in items if item.is_canary)
    registered = sum(1 for item in items if item.registration_state == "registered")
    candidates = sum(1 for item in items if item.registration_state == "candidate")

    return TaskInventory(
        total_packages=len(items),
        runnable_packages=runnable,
        curated_cards_only=card_only,
        template_packages=templates,
        canary_tasks=canaries,
        registered_tasks=registered,
        candidate_tasks=candidates,
        items=items,
    )
