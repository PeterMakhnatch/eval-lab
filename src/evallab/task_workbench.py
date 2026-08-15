"""Deterministic, non-admitting quality workbench for Harbor task candidates.

The workbench has deliberately narrow powers:

* read a task package and pinned source metadata;
* plan and run only local ``oracle``/``nop`` Harbor controls;
* replace the Oracle solution in isolated copies with declared invalid probes;
* emit candidate-only review records under ``research/registration/candidates``.

It cannot submit queue work, create registry records, approve policy, freeze a
task, or publish anything. Human-created ``library/registry`` records remain the
only admission boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast

from evallab.results import load_job
from evallab.runner import subscription_environment

SCHEMA_VERSION = 1
WORKBENCH_VERSION = "m007-v1.1"
MAX_CONTROL_CONCURRENCY = 2
ORACLE_REPETITIONS = 3
MIN_ADVERSARIAL_CASES = 3
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
FLOATING_REFS = {"head", "latest", "main", "master", "trunk", "tip"}
FORBIDDEN_AGENT_IMAGE_PARTS = {"solution", "tests", "verifier", "workbench"}
NETWORK_SCRIPT_PATTERN = re.compile(
    r"(?:https?://|\bcurl\b|\bwget\b|\bapt(?:-get)?\b|\bpip(?:3)?\s+install\b|"
    r"\buvx\b|\bnpm\s+(?:install|ci)\b|\byarn\s+install\b|\bgit\s+clone\b|"
    r"\b(?:ssh|scp|nc|ncat|netcat|telnet)\b|\bsocket\.(?:socket|create_connection)\b|"
    r"\burllib\.|\brequests\.|\bhttpx\.|\baiohttp\.)",
    re.IGNORECASE,
)
NONDETERMINISM_PATTERN = re.compile(
    r"(?:\brandom\.|\bsecrets\.|\buuid\.uuid4\b|\btime\.time\b|"
    r"\bdatetime\.now\b|/dev/(?:u?random)|\bdate\s+\+)",
    re.IGNORECASE,
)
NETWORK_OVERLAY_RELATIVE = "environment/.workbench-network-none.yaml"
NETWORK_OVERLAY_CONTENT = b"services:\n  main:\n    network_mode: none\n"

Severity = Literal["error", "warning", "info"]
Classification = Literal["task_defect", "harness_defect", "agent_failure", "expected"]
ControlStatus = Literal["completed", "harness_error", "interrupted"]
Disposition = Literal[
    "needs_changes",
    "controls_pending",
    "harness_blocked",
    "certified_for_review",
]


class WorkbenchError(RuntimeError):
    """Base error for safe workbench refusals."""


class UnsafePathError(WorkbenchError):
    """Raised when an input or output would escape its allowed root."""


class PacketConflictError(WorkbenchError):
    """Raised rather than replacing a non-identical review record."""


class ControlsNotAdmittedError(WorkbenchError):
    """Raised when static acceptance has not admitted local controls."""


class ControlInterrupted(WorkbenchError):
    """An injected control backend may use this to preserve an interrupted result."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _tree_entries(root: Path) -> list[tuple[str, str, int, str]]:
    """Return stable path/type/size/digest tuples without following symlinks."""
    if not root.exists():
        return []
    if root.is_file() and not root.is_symlink():
        return [(root.name, "file", root.stat().st_size, _sha256_file(root))]
    entries: list[tuple[str, str, int, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            entries.append(
                (relative, "symlink", len(target.encode()), _sha256_bytes(target.encode()))
            )
        elif path.is_file():
            entries.append((relative, "file", path.stat().st_size, _sha256_file(path)))
    return entries


def _tree_digest(root: Path) -> str:
    payload = [
        {"path": path, "type": entry_type, "size_bytes": size, "digest": digest}
        for path, entry_type, size, digest in _tree_entries(root)
    ]
    return _sha256_bytes(_canonical_bytes(payload))


def _empty_digest() -> str:
    return _sha256_bytes(b"")


def _subpath_digest(path: Path) -> str:
    if path.is_file() and not path.is_symlink():
        return _sha256_file(path)
    if path.is_dir() or path.is_symlink():
        return _tree_digest(path)
    return _empty_digest()


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise UnsafePathError(f"path escapes repository: {path}") from exc


def _role_for_path(relative: str) -> str:
    first = PurePosixPath(relative).parts[0] if PurePosixPath(relative).parts else ""
    return {
        "task.toml": "config",
        "instruction.md": "instruction",
        "instructions.md": "instruction",
        "environment": "image",
        "solution": "oracle",
        "tests": "verifier",
        "verifier": "verifier",
        "workbench": "adversarial-control",
    }.get(first, "source")


@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    code: str
    classification: Classification
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "classification": self.classification,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True)
class CandidateSource:
    source_uri: str
    source_ref: str
    license: str
    provenance_zone: Literal["01-external", "02-local-evidence", "03-synthetic", "04-curated"] = (
        "03-synthetic"
    )

    def to_dict(self) -> dict[str, str]:
        return {
            "source_uri": self.source_uri,
            "source_ref": self.source_ref,
            "license": self.license,
            "provenance_zone": self.provenance_zone,
        }


@dataclass(frozen=True)
class ControlPlanEntry:
    control_id: str
    kind: Literal["oracle", "nop", "adversarial"]
    agent: Literal["oracle", "nop"]
    expected_reward: float
    mutation_path: str | None
    command: tuple[str, ...]
    command_digest: str
    concurrency: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "kind": self.kind,
            "agent": self.agent,
            "expected_reward": self.expected_reward,
            "mutation_path": self.mutation_path,
            "command": list(self.command),
            "command_digest": self.command_digest,
            "concurrency": self.concurrency,
        }


@dataclass(frozen=True)
class Inspection:
    candidate: dict[str, Any]
    diagnostics: tuple[Diagnostic, ...]
    control_plan: tuple[ControlPlanEntry, ...]

    @property
    def static_passed(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "task_workbench_plan",
            "candidate": self.candidate,
            "static_passed": self.static_passed,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "control_plan": [item.to_dict() for item in self.control_plan],
        }


@dataclass(frozen=True)
class ControlObservation:
    control_id: str
    status: ControlStatus
    reward: float | None
    reward_vector: dict[str, float]
    verifier_output_digest: str | None
    evidence_digest: str | None
    image_digest: str
    verifier_digest: str
    source_package_digest: str
    staged_package_digest: str
    command: tuple[str, ...]
    command_digest: str
    job_path: str | None = None
    exception_type: str | None = None
    diagnostic: str | None = None
    failure_classification: Classification | None = None

    def to_dict(self) -> dict[str, Any]:
        value = {
            "control_id": self.control_id,
            "status": self.status,
            "reward": self.reward,
            "reward_vector": dict(sorted(self.reward_vector.items())),
            "verifier_output_digest": self.verifier_output_digest,
            "evidence_digest": self.evidence_digest,
            "image_digest": self.image_digest,
            "verifier_digest": self.verifier_digest,
            "source_package_digest": self.source_package_digest,
            "staged_package_digest": self.staged_package_digest,
            "command": list(self.command),
            "command_digest": self.command_digest,
            "job_path": self.job_path,
            "exception_type": self.exception_type,
            "diagnostic": self.diagnostic,
        }
        if self.failure_classification is not None:
            value["failure_classification"] = self.failure_classification
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ControlObservation:
        allowed = {
            "control_id",
            "status",
            "reward",
            "reward_vector",
            "verifier_output_digest",
            "evidence_digest",
            "image_digest",
            "verifier_digest",
            "source_package_digest",
            "staged_package_digest",
            "command",
            "command_digest",
            "job_path",
            "exception_type",
            "diagnostic",
            "failure_classification",
        }
        unknown = set(value) - allowed
        if unknown:
            raise WorkbenchError(f"control observation has unknown fields: {sorted(unknown)}")
        status = value.get("status")
        if status not in {"completed", "harness_error", "interrupted"}:
            raise WorkbenchError(f"invalid control status: {status!r}")
        reward = value.get("reward")
        if reward is not None and not isinstance(reward, int | float):
            raise WorkbenchError("control reward must be numeric or null")
        vector = value.get("reward_vector")
        if not isinstance(vector, dict) or any(
            not isinstance(key, str) or not isinstance(item, int | float)
            for key, item in vector.items()
        ):
            raise WorkbenchError("control reward_vector must map strings to numbers")
        command = value.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(item, str) for item in command)
        ):
            raise WorkbenchError("control command must be a non-empty string list")
        failure_classification = value.get("failure_classification")
        if failure_classification not in {
            None,
            "task_defect",
            "harness_defect",
            "agent_failure",
            "expected",
        }:
            raise WorkbenchError("control failure_classification is invalid")
        return cls(
            control_id=_required_string(value, "control_id"),
            status=cast(ControlStatus, status),
            reward=float(reward) if reward is not None else None,
            reward_vector={str(key): float(item) for key, item in vector.items()},
            verifier_output_digest=_optional_string(value, "verifier_output_digest"),
            evidence_digest=_optional_string(value, "evidence_digest"),
            image_digest=_required_digest(value, "image_digest"),
            verifier_digest=_required_digest(value, "verifier_digest"),
            source_package_digest=_required_digest(value, "source_package_digest"),
            staged_package_digest=_required_digest(value, "staged_package_digest"),
            command=tuple(command),
            command_digest=_required_digest(value, "command_digest"),
            job_path=_optional_string(value, "job_path"),
            exception_type=_optional_string(value, "exception_type"),
            diagnostic=_optional_string(value, "diagnostic"),
            failure_classification=cast(Classification | None, failure_classification),
        )


@dataclass(frozen=True)
class ControlBundle:
    candidate_id: str
    source_package_digest: str
    observations: tuple[ControlObservation, ...]
    bundle_digest: str

    @classmethod
    def build(
        cls,
        *,
        candidate_id: str,
        source_package_digest: str,
        observations: Sequence[ControlObservation],
    ) -> ControlBundle:
        body = {
            "schema_version": SCHEMA_VERSION,
            "kind": "task_workbench_controls",
            "candidate_id": candidate_id,
            "source_package_digest": source_package_digest,
            "observations": [item.to_dict() for item in observations],
        }
        return cls(
            candidate_id=candidate_id,
            source_package_digest=source_package_digest,
            observations=tuple(observations),
            bundle_digest=_sha256_bytes(_canonical_bytes(body)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "task_workbench_controls",
            "candidate_id": self.candidate_id,
            "source_package_digest": self.source_package_digest,
            "observations": [item.to_dict() for item in self.observations],
            "bundle_digest": self.bundle_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ControlBundle:
        allowed = {
            "schema_version",
            "kind",
            "candidate_id",
            "source_package_digest",
            "observations",
            "bundle_digest",
        }
        unknown = set(value) - allowed
        if unknown:
            raise WorkbenchError(f"control bundle has unknown fields: {sorted(unknown)}")
        if value.get("schema_version") != SCHEMA_VERSION:
            raise WorkbenchError("unsupported control bundle schema_version")
        if value.get("kind") != "task_workbench_controls":
            raise WorkbenchError("control bundle kind is invalid")
        raw_observations = value.get("observations")
        if not isinstance(raw_observations, list):
            raise WorkbenchError("control observations must be a list")
        observations = tuple(
            ControlObservation.from_dict(_required_mapping(item, "observation"))
            for item in raw_observations
        )
        rebuilt = cls.build(
            candidate_id=_required_string(value, "candidate_id"),
            source_package_digest=_required_digest(value, "source_package_digest"),
            observations=observations,
        )
        if value.get("bundle_digest") != rebuilt.bundle_digest:
            raise WorkbenchError("control bundle digest mismatch")
        return rebuilt


@dataclass(frozen=True)
class CheckReport:
    inspection: Inspection
    controls: ControlBundle | None
    diagnostics: tuple[Diagnostic, ...]
    disposition: Disposition

    @property
    def passed(self) -> bool:
        return self.disposition == "certified_for_review"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "task_workbench_check",
            "candidate_id": self.inspection.candidate["candidate_id"],
            "static_passed": self.inspection.static_passed,
            "controls_present": self.controls is not None,
            "passed": self.passed,
            "disposition": self.disposition,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "control_bundle_digest": self.controls.bundle_digest if self.controls else None,
        }


class ControlBackend(Protocol):
    def run(
        self,
        *,
        repo_root: Path,
        task_dir: Path,
        candidate: Mapping[str, Any],
        plan: ControlPlanEntry,
        run_root: Path,
    ) -> ControlObservation: ...


def _required_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkbenchError(f"{label} must be an object")
    return value


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise WorkbenchError(f"{key} must be a non-empty string")
    return item


def _optional_string(value: Mapping[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise WorkbenchError(f"{key} must be a non-empty string or null")
    return item


def _required_digest(value: Mapping[str, Any], key: str) -> str:
    item = _required_string(value, key)
    if not SHA256_PATTERN.fullmatch(item):
        raise WorkbenchError(f"{key} must be a sha256 digest")
    return item


def _diag(
    code: str,
    path: str,
    message: str,
    *,
    severity: Severity = "error",
    classification: Classification = "task_defect",
) -> Diagnostic:
    return Diagnostic(
        severity=severity,
        code=code,
        classification=classification,
        path=path,
        message=message,
    )


def _sort_diagnostics(values: Sequence[Diagnostic]) -> tuple[Diagnostic, ...]:
    return tuple(
        sorted(
            values,
            key=lambda item: (
                {"error": 0, "warning": 1, "info": 2}[item.severity],
                item.code,
                item.path,
                item.message,
            ),
        )
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


def _parse_task_toml(path: Path, diagnostics: list[Diagnostic]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        diagnostics.append(_diag("task_toml_invalid", "task.toml", type(exc).__name__))
        return {}
    if not isinstance(parsed, dict):
        diagnostics.append(_diag("task_toml_invalid", "task.toml", "top level is not a table"))
        return {}
    return parsed


def _is_pinned_ref(value: str) -> bool:
    normalized = value.strip()
    if not normalized or normalized.lower() in FLOATING_REFS:
        return False
    lowered = normalized.lower()
    if any(f"/{item}" in lowered or f"@{item}" in lowered for item in FLOATING_REFS):
        return False
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", normalized, re.IGNORECASE):
        return True
    if re.fullmatch(r"v?\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?", normalized):
        return True
    if re.fullmatch(r"local/[a-z0-9][a-z0-9._/-]+@\d+\.\d+(?:\.\d+)?", normalized):
        return True
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9._/-]+@v?\d+(?:\.\d+){0,2}", normalized))


def _validate_source(source: CandidateSource, diagnostics: list[Diagnostic]) -> None:
    if not source.source_uri.strip():
        diagnostics.append(_diag("source_uri_missing", "$source", "source_uri is required"))
    if not _is_pinned_ref(source.source_ref):
        diagnostics.append(
            _diag(
                "source_ref_unpinned",
                "$source",
                "source_ref must be an immutable commit, release, or local version pin",
            )
        )
    if not source.license.strip() or source.license.strip().lower() in {"unknown", "none", "n/a"}:
        diagnostics.append(
            _diag("license_missing", "$source", "a concrete redistribution license is required")
        )


def _validate_layout(task_dir: Path, diagnostics: list[Diagnostic]) -> None:
    required = (
        "task.toml",
        "instruction.md",
        "environment/Dockerfile",
        "solution/solve.sh",
        "tests/Dockerfile",
        "tests/test.sh",
    )
    for relative in required:
        if not (task_dir / relative).is_file():
            diagnostics.append(_diag("required_file_missing", relative, "required file is missing"))

    for path in sorted(task_dir.rglob("*")):
        if not path.is_symlink():
            continue
        relative = path.relative_to(task_dir).as_posix()
        if not _is_under(path, task_dir):
            diagnostics.append(
                _diag("path_escape", relative, "symlink resolves outside the candidate package")
            )

    for relative in ("solution/solve.sh", "tests/test.sh"):
        path = task_dir / relative
        if path.is_file() and not os.access(path, os.X_OK):
            diagnostics.append(
                _diag("script_not_executable", relative, "script must be executable")
            )
    if (task_dir / ".gitignore").exists():
        diagnostics.append(
            _diag(
                "custom_package_ignore_unsupported",
                ".gitignore",
                "v1 cannot bind Harbor task digests with custom package ignore rules",
            )
        )


def _validate_task_metadata(
    config: Mapping[str, Any], task_dir: Path, diagnostics: list[Diagnostic]
) -> tuple[str, str | None, list[str]]:
    schema = config.get("schema_version")
    if not isinstance(schema, str) or not re.fullmatch(r"1\.\d+", schema):
        diagnostics.append(
            _diag("schema_version_invalid", "task.toml", "schema_version must be a 1.x string")
        )
    task = config.get("task")
    task_table = task if isinstance(task, dict) else {}
    raw_name = task_table.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name.strip() else task_dir.name
    if not isinstance(raw_name, str) or not raw_name.strip():
        diagnostics.append(_diag("task_name_missing", "task.toml", "[task].name is required"))
    raw_version = task_table.get("version")
    version = raw_version if isinstance(raw_version, str) and raw_version.strip() else None
    if version is None:
        diagnostics.append(_diag("task_version_missing", "task.toml", "[task].version is required"))
    description = task_table.get("description")
    if not isinstance(description, str) or not description.strip():
        diagnostics.append(
            _diag("task_description_missing", "task.toml", "[task].description is required")
        )
    keywords = task_table.get("keywords")
    normalized_keywords = (
        [item for item in keywords if isinstance(item, str) and item.strip()]
        if isinstance(keywords, list)
        else []
    )
    if not 3 <= len(normalized_keywords) <= 8 or len(normalized_keywords) != len(
        set(normalized_keywords)
    ):
        diagnostics.append(
            _diag(
                "task_keywords_invalid",
                "task.toml",
                "[task].keywords must contain 3-8 unique non-empty strings",
            )
        )
    authors = task_table.get("authors")
    if not isinstance(authors, list) or not authors:
        diagnostics.append(
            _diag("task_authors_missing", "task.toml", "[task].authors must name an author")
        )
    metadata = config.get("metadata")
    metadata_table = metadata if isinstance(metadata, dict) else {}
    for key in ("difficulty", "category", "tags"):
        item = metadata_table.get(key)
        if item is None or item == "" or item == []:
            diagnostics.append(
                _diag("metadata_incomplete", "task.toml", f"[metadata].{key} is required")
            )
    return name, version, normalized_keywords


def _validate_timeouts_and_artifacts(
    config: Mapping[str, Any], diagnostics: list[Diagnostic]
) -> list[str]:
    for section in ("agent", "verifier"):
        raw = config.get(section)
        table = raw if isinstance(raw, dict) else {}
        timeout = table.get("timeout_sec")
        if (
            not isinstance(timeout, int | float)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= 21_600
        ):
            diagnostics.append(
                _diag(
                    "timeout_invalid",
                    "task.toml",
                    f"[{section}].timeout_sec must be between 1 and 21600 seconds",
                )
            )
    raw_artifacts = config.get("artifacts")
    if not isinstance(raw_artifacts, list):
        diagnostics.append(
            _diag("artifacts_invalid", "task.toml", "artifacts must be an explicit list")
        )
        return []
    artifacts: list[str] = []
    for item in raw_artifacts:
        if not isinstance(item, str):
            diagnostics.append(
                _diag("artifact_path_invalid", "task.toml", "artifact paths must be strings")
            )
            continue
        pure = PurePosixPath(item)
        if not pure.is_absolute() or ".." in pure.parts or not item.startswith("/app/"):
            diagnostics.append(
                _diag(
                    "artifact_path_escape",
                    "task.toml",
                    f"artifact path {item!r} must be absolute under /app",
                )
            )
        if any(part in FORBIDDEN_AGENT_IMAGE_PARTS for part in pure.parts):
            diagnostics.append(
                _diag(
                    "hidden_artifact_exposure",
                    "task.toml",
                    f"artifact path {item!r} exposes a hidden task component",
                )
            )
        artifacts.append(item)
    if len(artifacts) != len(set(artifacts)):
        diagnostics.append(
            _diag("artifact_duplicate", "task.toml", "artifact paths must be unique")
        )
    return artifacts


def _docker_logical_lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\\\n", " ").splitlines() if line.strip()]


def _docker_copy_sources(arguments: str) -> tuple[list[str], bool]:
    """Parse COPY/ADD sources and fail closed on unsupported dynamic syntax."""
    payload = arguments.strip()
    while payload.startswith("--"):
        match = re.match(r"--[^\s]+\s+", payload)
        if match is None:
            return [], True
        payload = payload[match.end() :].lstrip()
    if payload.startswith("["):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return [], True
        if (
            not isinstance(value, list)
            or len(value) < 2
            or any(not isinstance(item, str) for item in value)
        ):
            return [], True
        return list(value[:-1]), False
    try:
        tokens = shlex.split(payload)
    except ValueError:
        return [], True
    if len(tokens) < 2:
        return [], True
    return tokens[:-1], False


def _validate_dockerfile(task_dir: Path, diagnostics: list[Diagnostic]) -> str | None:
    path = task_dir / "environment/Dockerfile"
    if not path.is_file():
        return None
    text = _read_text(path)
    base_ref: str | None = None
    for line in _docker_logical_lines(text):
        if line.startswith("#"):
            continue
        if re.match(r"(?i)^FROM\s+", line):
            parts = shlex.split(line)
            references = [
                part for part in parts[1:] if not part.startswith("--") and part.upper() != "AS"
            ]
            if references:
                base_ref = references[0]
                if base_ref != "scratch" and not re.search(r"@sha256:[0-9a-f]{64}$", base_ref):
                    diagnostics.append(
                        _diag(
                            "base_image_unpinned",
                            "environment/Dockerfile",
                            "every FROM image must be pinned by @sha256 digest",
                        )
                    )
        copy_match = re.match(r"(?i)^(?:COPY|ADD)\s+(.+)$", line)
        if copy_match:
            sources, unsupported = _docker_copy_sources(copy_match.group(1))
            if unsupported:
                diagnostics.append(
                    _diag(
                        "agent_image_copy_unsupported",
                        "environment/Dockerfile",
                        "COPY/ADD syntax must be statically resolvable",
                    )
                )
            for source in sources:
                pure = PurePosixPath(source)
                if (
                    source in {".", "./"}
                    or pure.is_absolute()
                    or ".." in pure.parts
                    or bool(re.search(r"[$*?\[\]{}]", source))
                    or any(part in FORBIDDEN_AGENT_IMAGE_PARTS for part in pure.parts)
                ):
                    diagnostics.append(
                        _diag(
                            "agent_image_hidden_leak",
                            "environment/Dockerfile",
                            "COPY/ADD may not include hidden components, '.', or escaping paths",
                        )
                    )

    normalized = text.replace("\\\n", " ")
    for match in re.finditer(r"\bpip(?:3)?\s+install\s+([^;&\n]+)", normalized, re.IGNORECASE):
        try:
            dependencies = [
                item for item in shlex.split(match.group(1)) if not item.startswith("-")
            ]
        except ValueError:
            dependencies = []
        for dependency in dependencies:
            if "==" not in dependency and "@" not in dependency:
                diagnostics.append(
                    _diag(
                        "dependency_unpinned",
                        "environment/Dockerfile",
                        "pip dependencies must use exact == or immutable @ pins",
                    )
                )
                break
    for match in re.finditer(r"\bapt(?:-get)?\s+install\s+([^;&\n]+)", normalized, re.IGNORECASE):
        try:
            dependencies = [
                item for item in shlex.split(match.group(1)) if not item.startswith("-")
            ]
        except ValueError:
            dependencies = []
        for dependency in dependencies:
            if dependency in {"install"}:
                continue
            if "=" not in dependency:
                diagnostics.append(
                    _diag(
                        "dependency_unpinned",
                        "environment/Dockerfile",
                        "apt dependencies must use exact package=version pins",
                    )
                )
                break
    return base_ref


def _validate_verifier_image(task_dir: Path, diagnostics: list[Diagnostic]) -> None:
    path = task_dir / "tests/Dockerfile"
    if not path.is_file():
        return
    text = _read_text(path)
    found_from = False
    for line in _docker_logical_lines(text):
        if line.startswith("#") or not re.match(r"(?i)^FROM\s+", line):
            continue
        found_from = True
        parts = shlex.split(line)
        references = [
            part for part in parts[1:] if not part.startswith("--") and part.upper() != "AS"
        ]
        if (
            references
            and references[0] != "scratch"
            and not re.search(r"@sha256:[0-9a-f]{64}$", references[0])
        ):
            diagnostics.append(
                _diag(
                    "verifier_image_unpinned",
                    "tests/Dockerfile",
                    "the separate verifier FROM image must be pinned by @sha256 digest",
                )
            )
    if not found_from:
        diagnostics.append(
            _diag(
                "verifier_image_invalid",
                "tests/Dockerfile",
                "separate verifier Dockerfile must declare a FROM image",
            )
        )


def _validate_network_and_isolation(
    config: Mapping[str, Any], task_dir: Path, diagnostics: list[Diagnostic]
) -> None:
    environment = config.get("environment")
    environment_table = environment if isinstance(environment, dict) else {}
    network_mode = environment_table.get("network_mode")
    if network_mode not in {"no-network", "public", "allowlist"}:
        diagnostics.append(
            _diag(
                "network_policy_invalid",
                "task.toml",
                "[environment].network_mode must be explicit and supported",
            )
        )
    verifier = config.get("verifier")
    verifier_table = verifier if isinstance(verifier, dict) else {}
    if verifier_table.get("environment_mode") != "separate":
        diagnostics.append(
            _diag(
                "verifier_not_isolated",
                "task.toml",
                "[verifier].environment_mode must be 'separate'",
            )
        )
    verifier_network = verifier_table.get("network_mode")
    verifier_environment = verifier_table.get("environment")
    if isinstance(verifier_environment, dict):
        verifier_network = verifier_environment.get("network_mode", verifier_network)
    if verifier_network not in {None, "no-network", "public", "allowlist"}:
        diagnostics.append(
            _diag(
                "verifier_network_invalid",
                "task.toml",
                "verifier network_mode is invalid",
            )
        )
    compose_path = task_dir / "environment/docker-compose.yaml"
    if compose_path.exists():
        diagnostics.append(
            _diag(
                "custom_compose_unsupported",
                "environment/docker-compose.yaml",
                "v1 cannot prove network isolation for task-authored Compose services",
            )
        )

    for root_name in ("tests", "verifier", "solution"):
        root = task_dir / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            text = _read_text(path)
            relative = path.relative_to(task_dir).as_posix()
            if NETWORK_SCRIPT_PATTERN.search(text):
                diagnostics.append(
                    _diag(
                        "runtime_network_use",
                        relative,
                        "control/verifier scripts may not fetch or install over "
                        "the network at runtime",
                    )
                )
            if root_name in {"tests", "verifier"} and NONDETERMINISM_PATTERN.search(text):
                diagnostics.append(
                    _diag(
                        "verifier_nondeterminism_static",
                        relative,
                        "verifier references a nondeterministic clock/random source",
                    )
                )


def _sensitive_lines(task_dir: Path) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    roots = (task_dir / "solution", task_dir / "tests", task_dir / "verifier")
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name in {"Dockerfile", "test.sh"}:
                # Verifier-image plumbing commonly mirrors the agent image pin
                # and absolute paths. Those are not golden task content.
                continue
            relative = path.relative_to(task_dir).as_posix()
            text = _read_text(path)
            if "fixtures" not in path.relative_to(root).parts:
                for line in text.splitlines():
                    normalized = " ".join(line.strip().split())
                    if (
                        len(normalized) >= 32
                        and not normalized.startswith(("#", "//", "/*", "*"))
                        and re.search(r"[A-Za-z]", normalized)
                    ):
                        candidates.append((relative, normalized))
            if any(token in path.name.lower() for token in ("golden", "expected", "answer")):
                compact = " ".join(text.split())
                if len(compact) >= 12:
                    candidates.append((relative, compact))
    return candidates


def _validate_golden_leak(task_dir: Path, diagnostics: list[Diagnostic]) -> None:
    visible_paths: list[Path] = []
    for relative in ("instruction.md", "instructions.md"):
        path = task_dir / relative
        if path.is_file():
            visible_paths.append(path)
    environment = task_dir / "environment"
    if environment.exists():
        visible_paths.extend(
            path
            for path in sorted(environment.rglob("*"))
            if path.is_file() and not path.is_symlink()
        )
    visible = "\n".join(" ".join(_read_text(path).split()) for path in visible_paths)
    for source_path, span in _sensitive_lines(task_dir):
        if span and span in visible:
            diagnostics.append(
                _diag(
                    "golden_data_leak",
                    source_path,
                    "a hidden solution/verifier span is present in agent-visible task bytes",
                )
            )
            break


def _validate_test_contract(task_dir: Path, diagnostics: list[Diagnostic]) -> None:
    test_script = task_dir / "tests/test.sh"
    if test_script.is_file():
        text = _read_text(test_script)
        verifier_text = "\n".join(
            _read_text(path)
            for path in sorted((task_dir / "tests").rglob("*"))
            if path.is_file() and not path.is_symlink()
        )
        writes_reward = "/logs/verifier/reward" in verifier_text or (
            "/logs/verifier" in verifier_text
            and ("reward.json" in verifier_text or "reward.txt" in verifier_text)
        )
        if not writes_reward:
            diagnostics.append(
                _diag(
                    "verifier_reward_missing",
                    "tests/test.sh",
                    "verifier must write an absolute /logs/verifier/reward output",
                )
            )
        if "/tests/" not in text:
            diagnostics.append(
                _diag(
                    "verifier_path_relative",
                    "tests/test.sh",
                    "verifier entrypoint must use an absolute /tests path",
                )
            )
    instruction = task_dir / "instruction.md"
    if instruction.is_file() and not instruction.read_text(encoding="utf-8").strip():
        diagnostics.append(_diag("instruction_empty", "instruction.md", "instruction is empty"))


def _adversarial_scripts(task_dir: Path, diagnostics: list[Diagnostic]) -> list[Path]:
    root = task_dir / "workbench/adversarial"
    scripts = sorted(root.glob("*.sh")) if root.is_dir() else []
    if len(scripts) < MIN_ADVERSARIAL_CASES:
        diagnostics.append(
            _diag(
                "adversarial_cases_insufficient",
                "workbench/adversarial",
                f"at least {MIN_ADVERSARIAL_CASES} invalid-solution .sh probes are required",
            )
        )
    for path in scripts:
        if not SAFE_SLUG.fullmatch(path.stem):
            diagnostics.append(
                _diag(
                    "adversarial_name_invalid",
                    path.relative_to(task_dir).as_posix(),
                    "adversarial case names must be safe lowercase slugs",
                )
            )
        if not os.access(path, os.X_OK):
            diagnostics.append(
                _diag(
                    "script_not_executable",
                    path.relative_to(task_dir).as_posix(),
                    "adversarial solution must be executable",
                )
            )
    return scripts


def _detect_forged_registration(
    repo_root: Path,
    task_dir: Path,
    task_id: str,
    config: Mapping[str, Any],
    diagnostics: list[Diagnostic],
) -> dict[str, Any]:
    task_relative = _repo_relative(task_dir, repo_root)
    if task_relative.startswith("library/registry/") or task_relative.startswith("registered/"):
        diagnostics.append(
            _diag(
                "forged_registration",
                task_relative,
                "candidate packages cannot occupy registry or registered namespaces",
            )
        )
    suspicious_names = {"registration.json", "registry.json", ".registered"}
    for path in sorted(task_dir.rglob("*")):
        if path.is_file() and path.name in suspicious_names:
            diagnostics.append(
                _diag(
                    "forged_registration",
                    path.relative_to(task_dir).as_posix(),
                    "candidate-local files cannot assert registration",
                )
            )
    metadata = config.get("metadata")
    if isinstance(metadata, dict) and (
        metadata.get("state") == "registered" or metadata.get("registered") is True
    ):
        diagnostics.append(
            _diag(
                "forged_registration",
                "task.toml",
                "task metadata cannot self-assert registered state",
            )
        )

    record_path = repo_root / "library/registry" / f"{task_id}.json"
    observation: dict[str, Any] = {
        "record_present": record_path.is_file(),
        "state": "unregistered",
        "record_digest": _sha256_file(record_path) if record_path.is_file() else None,
        "path_matches": False,
    }
    if record_path.is_file():
        try:
            value = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            observation["state"] = "malformed"
        else:
            if isinstance(value, dict):
                state = value.get("state")
                observation["state"] = state if isinstance(state, str) else "malformed"
                observation["path_matches"] = value.get("task_path") == task_relative
    return observation


def _control_command(candidate_id: str, task_id: str, entry_id: str, agent: str) -> tuple[str, ...]:
    safe_task = re.sub(r"[^a-z0-9-]+", "-", task_id.lower()).strip("-") or "task"
    safe_task = safe_task[-24:]
    job_name = f"m007-{safe_task}-{candidate_id[-8:]}-{entry_id}"
    staging = f"$REPO/runs/task-workbench/{candidate_id}/staging/{entry_id}"
    jobs = f"$REPO/runs/task-workbench/{candidate_id}/jobs"
    network_overlay = f"{staging}/{NETWORK_OVERLAY_RELATIVE}"
    return (
        "harbor",
        "run",
        "--path",
        staging,
        "--agent",
        agent,
        "--env",
        "docker",
        "--extra-docker-compose",
        network_overlay,
        "--job-name",
        job_name,
        "--jobs-dir",
        jobs,
        "--n-concurrent",
        "1",
        "--n-attempts",
        "1",
        "-y",
    )


def _build_control_plan(
    candidate_id: str, task_id: str, task_dir: Path, adversarial: Sequence[Path]
) -> tuple[ControlPlanEntry, ...]:
    specs: list[
        tuple[
            str,
            Literal["oracle", "nop", "adversarial"],
            Literal["oracle", "nop"],
            float,
            str | None,
        ]
    ] = []
    for index in range(1, ORACLE_REPETITIONS + 1):
        specs.append((f"oracle-{index}", "oracle", "oracle", 1.0, None))
    specs.append(("nop-1", "nop", "nop", 0.0, None))
    for path in adversarial:
        relative = path.relative_to(task_dir).as_posix()
        specs.append((f"adversarial-{path.stem}", "adversarial", "oracle", 0.0, relative))
    entries: list[ControlPlanEntry] = []
    for control_id, kind, agent, expected, mutation_path in specs:
        command = _control_command(candidate_id, task_id, control_id, agent)
        entries.append(
            ControlPlanEntry(
                control_id=control_id,
                kind=kind,
                agent=agent,
                expected_reward=expected,
                mutation_path=mutation_path,
                command=command,
                command_digest=_sha256_bytes(_canonical_bytes(list(command))),
            )
        )
    return tuple(entries)


def inspect_candidate(*, repo_root: Path, task_path: Path, source: CandidateSource) -> Inspection:
    repo_root = repo_root.resolve()
    task_dir = task_path if task_path.is_absolute() else repo_root / task_path
    if not _is_under(task_dir, repo_root):
        raise UnsafePathError(f"candidate path escapes repository: {task_path}")
    task_dir = task_dir.resolve()
    if not task_dir.is_dir():
        raise WorkbenchError(f"candidate directory is missing: {task_path}")

    diagnostics: list[Diagnostic] = []
    _validate_source(source, diagnostics)
    _validate_layout(task_dir, diagnostics)
    config = _parse_task_toml(task_dir / "task.toml", diagnostics)
    task_name, task_version, keywords = _validate_task_metadata(config, task_dir, diagnostics)
    artifacts = _validate_timeouts_and_artifacts(config, diagnostics)
    base_image = _validate_dockerfile(task_dir, diagnostics)
    _validate_verifier_image(task_dir, diagnostics)
    _validate_network_and_isolation(config, task_dir, diagnostics)
    _validate_golden_leak(task_dir, diagnostics)
    _validate_test_contract(task_dir, diagnostics)
    adversarial = _adversarial_scripts(task_dir, diagnostics)

    task_id = task_name.rsplit("/", 1)[-1]
    if not SAFE_SLUG.fullmatch(task_id):
        diagnostics.append(
            _diag("task_id_invalid", "task.toml", "task name suffix must be a safe slug")
        )
        task_id = re.sub(r"[^a-z0-9-]+", "-", task_dir.name.lower()).strip("-") or "task"
    registration = _detect_forged_registration(repo_root, task_dir, task_id, config, diagnostics)

    files = [
        {
            "path": path,
            "role": _role_for_path(path),
            "type": entry_type,
            "size_bytes": size,
            "digest": digest,
        }
        for path, entry_type, size, digest in _tree_entries(task_dir)
    ]
    digests = {
        "package": _tree_digest(task_dir),
        "task_toml": _subpath_digest(task_dir / "task.toml"),
        "instruction": _subpath_digest(task_dir / "instruction.md"),
        "image_definition": _subpath_digest(task_dir / "environment"),
        "solution": _subpath_digest(task_dir / "solution"),
        "verifier": _subpath_digest(task_dir / "tests"),
        "adversarial_controls": _subpath_digest(task_dir / "workbench/adversarial"),
        "artifact_config": _sha256_bytes(_canonical_bytes(artifacts)),
        "source_metadata": _sha256_bytes(_canonical_bytes(source.to_dict())),
    }
    identity = {
        "workbench_version": WORKBENCH_VERSION,
        "task_id": task_id,
        "task_version": task_version,
        "task_path": _repo_relative(task_dir, repo_root),
        "source": source.to_dict(),
        "package_digest": digests["package"],
    }
    candidate_id = "candidate-" + hashlib.sha256(_canonical_bytes(identity)).hexdigest()[:24]
    plan = _build_control_plan(candidate_id, task_id, task_dir, adversarial)
    candidate: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "task_workbench_candidate",
        "candidate_id": candidate_id,
        "workbench_version": WORKBENCH_VERSION,
        "task_id": task_id,
        "task_name": task_name,
        "task_version": task_version,
        "task_path": identity["task_path"],
        "source": source.to_dict(),
        "declared_base_image": base_image,
        "network_policy": {
            "environment": (
                config.get("environment", {}).get("network_mode")
                if isinstance(config.get("environment"), dict)
                else None
            ),
            "verifier": (
                config.get("verifier", {}).get("network_mode")
                if isinstance(config.get("verifier"), dict)
                else None
            ),
            "control_enforcement": "docker-compose main network_mode=none",
            "control_overlay_digest": _sha256_bytes(NETWORK_OVERLAY_CONTENT),
        },
        "keywords": keywords,
        "artifacts": artifacts,
        "digests": digests,
        "files": files,
        "registration_observation": registration,
        "admission_boundary": {
            "candidate_only": True,
            "can_queue": False,
            "can_register": False,
            "can_freeze": False,
            "can_publish": False,
            "can_edit_policy": False,
            "required_next_actor": "human-created library/registry record",
        },
    }
    candidate["candidate_record_digest"] = _sha256_bytes(_canonical_bytes(candidate))
    return Inspection(
        candidate=candidate,
        diagnostics=_sort_diagnostics(diagnostics),
        control_plan=plan,
    )


def _materialize_command(command: Sequence[str], repo_root: Path) -> tuple[str, ...]:
    prefix = "$REPO/"
    return tuple(
        str(repo_root / item.removeprefix(prefix)) if item.startswith(prefix) else item
        for item in command
    )


def _scrub_diagnostic(value: str, repo_root: Path, *, limit: int = 2_000) -> str:
    text = value.replace(str(repo_root), "$REPO")
    return text[-limit:]


def _reward_vector_from_trial(result: Mapping[str, Any]) -> dict[str, float]:
    verifier = result.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, Mapping) else None
    if not isinstance(rewards, Mapping):
        return {}
    return {
        str(key): float(value)
        for key, value in rewards.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def _runner_failure_classification(message: str) -> Classification:
    normalized = message.lower()
    task_markers = (
        "dockerfile",
        "environmentbuilderror",
        "failed to build",
        "failed to solve",
        "imagepullerror",
        "invalid task",
        "taskconfigerror",
        "taskvalidationerror",
    )
    infrastructure_markers = (
        "cannot connect to the docker daemon",
        "credential",
        "docker daemon is not running",
        "operation timed out",
        "permission denied",
        "service unavailable",
    )
    if any(marker in normalized for marker in task_markers):
        return "task_defect"
    if any(marker in normalized for marker in infrastructure_markers):
        return "harness_defect"
    return "harness_defect"


class HarborControlBackend:
    """Fixed-command local Harbor backend; only oracle and nop are accepted."""

    def __init__(
        self,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        environment_provider: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self._command_runner = command_runner or subprocess.run
        self._environment_provider = environment_provider or subscription_environment

    def run(
        self,
        *,
        repo_root: Path,
        task_dir: Path,
        candidate: Mapping[str, Any],
        plan: ControlPlanEntry,
        run_root: Path,
    ) -> ControlObservation:
        if plan.agent not in {"oracle", "nop"}:
            raise WorkbenchError("task workbench controls permit only oracle and nop")
        if plan.concurrency < 1 or plan.concurrency > MAX_CONTROL_CONCURRENCY:
            raise WorkbenchError("control concurrency exceeds the hard cap of 2")
        candidate_id = _required_string(candidate, "candidate_id")
        source_digest = _required_digest(
            _required_mapping(candidate.get("digests"), "digests"), "package"
        )
        image_digest = _required_digest(
            _required_mapping(candidate.get("digests"), "digests"), "image_definition"
        )
        verifier_digest = _required_digest(
            _required_mapping(candidate.get("digests"), "digests"), "verifier"
        )
        expected_run_root = repo_root / "runs/task-workbench" / candidate_id
        if run_root.resolve() != expected_run_root.resolve() or not _is_under(
            run_root, repo_root / "runs"
        ):
            raise UnsafePathError(
                "control run root must be the candidate's runs/task-workbench path"
            )
        stage = run_root / "staging" / plan.control_id
        jobs = run_root / "jobs"
        job_name = plan.command[plan.command.index("--job-name") + 1]
        job_dir = jobs / job_name
        if stage.exists():
            raise WorkbenchError(f"refusing to replace existing control staging path: {stage}")
        stage.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(task_dir, stage, symlinks=False)
        if plan.mutation_path is not None:
            mutation = stage / plan.mutation_path
            if not mutation.is_file() or not _is_under(mutation, stage):
                raise WorkbenchError(
                    f"adversarial mutation is missing or unsafe: {plan.mutation_path}"
                )
            solution = stage / "solution/solve.sh"
            shutil.copyfile(mutation, solution)
            solution.chmod(mutation.stat().st_mode)
        overlay = stage / NETWORK_OVERLAY_RELATIVE
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_bytes(NETWORK_OVERLAY_CONTENT)
        staged_digest = _tree_digest(stage)
        canonical_command = tuple(plan.command)
        materialized = _materialize_command(canonical_command, repo_root)
        if Path(materialized[materialized.index("--path") + 1]).resolve() != stage.resolve():
            raise WorkbenchError("materialized control command does not name its isolated stage")
        jobs.mkdir(parents=True, exist_ok=True)
        if job_dir.exists():
            return self._observation_from_existing(
                repo_root=repo_root,
                plan=plan,
                job_dir=job_dir,
                source_digest=source_digest,
                staged_digest=staged_digest,
                image_digest=image_digest,
                verifier_digest=verifier_digest,
            )
        try:
            completed = self._command_runner(
                list(materialized),
                cwd=repo_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=21_600,
                env=dict(self._environment_provider()),
            )
        except KeyboardInterrupt as exc:
            raise ControlInterrupted("operator interrupted Harbor control") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ControlObservation(
                control_id=plan.control_id,
                status="harness_error",
                reward=None,
                reward_vector={},
                verifier_output_digest=None,
                evidence_digest=None,
                image_digest=image_digest,
                verifier_digest=verifier_digest,
                source_package_digest=source_digest,
                staged_package_digest=staged_digest,
                command=canonical_command,
                command_digest=plan.command_digest,
                job_path=_repo_relative(job_dir, repo_root) if job_dir.exists() else None,
                exception_type=type(exc).__name__,
                diagnostic=_scrub_diagnostic(str(exc), repo_root),
                failure_classification="harness_defect",
            )
        if completed.returncode != 0 and not (job_dir / "result.json").is_file():
            diagnostic = completed.stderr or completed.stdout or "Harbor returned nonzero"
            failure_classification = _runner_failure_classification(diagnostic)
            return ControlObservation(
                control_id=plan.control_id,
                status="harness_error",
                reward=None,
                reward_vector={},
                verifier_output_digest=None,
                evidence_digest=None,
                image_digest=image_digest,
                verifier_digest=verifier_digest,
                source_package_digest=source_digest,
                staged_package_digest=staged_digest,
                command=canonical_command,
                command_digest=plan.command_digest,
                job_path=_repo_relative(job_dir, repo_root) if job_dir.exists() else None,
                exception_type="HarborNonZeroExit",
                diagnostic=_scrub_diagnostic(diagnostic, repo_root),
                failure_classification=failure_classification,
            )
        runner_diagnostic = None
        if completed.returncode != 0:
            runner_diagnostic = _scrub_diagnostic(
                completed.stderr or completed.stdout or "Harbor returned nonzero",
                repo_root,
            )
        return self._observation_from_existing(
            repo_root=repo_root,
            plan=plan,
            job_dir=job_dir,
            source_digest=source_digest,
            staged_digest=staged_digest,
            image_digest=image_digest,
            verifier_digest=verifier_digest,
            runner_diagnostic=runner_diagnostic,
        )

    def _observation_from_existing(
        self,
        *,
        repo_root: Path,
        plan: ControlPlanEntry,
        job_dir: Path,
        source_digest: str,
        staged_digest: str,
        image_digest: str,
        verifier_digest: str,
        runner_diagnostic: str | None = None,
    ) -> ControlObservation:
        canonical_command = tuple(plan.command)
        try:
            job = load_job(job_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return ControlObservation(
                control_id=plan.control_id,
                status="interrupted",
                reward=None,
                reward_vector={},
                verifier_output_digest=None,
                evidence_digest=_tree_digest(job_dir) if job_dir.exists() else None,
                image_digest=image_digest,
                verifier_digest=verifier_digest,
                source_package_digest=source_digest,
                staged_package_digest=staged_digest,
                command=canonical_command,
                command_digest=plan.command_digest,
                job_path=_repo_relative(job_dir, repo_root),
                exception_type=("HarborNonZeroExit" if runner_diagnostic else type(exc).__name__),
                diagnostic=runner_diagnostic or _scrub_diagnostic(str(exc), repo_root),
                failure_classification="harness_defect",
            )
        if len(job.trials) != 1:
            return ControlObservation(
                control_id=plan.control_id,
                status="harness_error",
                reward=None,
                reward_vector={},
                verifier_output_digest=None,
                evidence_digest=_tree_digest(job_dir),
                image_digest=image_digest,
                verifier_digest=verifier_digest,
                source_package_digest=source_digest,
                staged_package_digest=staged_digest,
                command=canonical_command,
                command_digest=plan.command_digest,
                job_path=_repo_relative(job_dir, repo_root),
                exception_type="UnexpectedTrialCount",
                diagnostic=f"expected one trial, found {len(job.trials)}",
                failure_classification="harness_defect",
            )
        trial = job.trials[0]
        vector = _reward_vector_from_trial(trial.result)
        exception = trial.result.get("exception_info")
        exception_type = None
        if isinstance(exception, Mapping):
            raw_type = exception.get("exception_type")
            exception_type = str(raw_type) if raw_type else "HarborTrialException"
        status: ControlStatus = "harness_error" if exception_type else "completed"
        failure_classification = (
            classify_trial_outcome(
                agent=plan.agent,
                reward=trial.primary_reward,
                exception_type=exception_type,
                expected_reward=plan.expected_reward,
            )
            if exception_type
            else None
        )
        return ControlObservation(
            control_id=plan.control_id,
            status=status,
            reward=trial.primary_reward,
            reward_vector=vector,
            verifier_output_digest=_sha256_bytes(_canonical_bytes(vector)) if vector else None,
            evidence_digest=_tree_digest(job_dir),
            image_digest=image_digest,
            verifier_digest=verifier_digest,
            source_package_digest=source_digest,
            staged_package_digest=staged_digest,
            command=canonical_command,
            command_digest=plan.command_digest,
            job_path=_repo_relative(job_dir, repo_root),
            exception_type=exception_type,
            diagnostic=("trial contains a Harbor exception" if exception_type else None),
            failure_classification=failure_classification,
        )


def _interrupted_observation(
    inspection: Inspection, plan: ControlPlanEntry, message: str
) -> ControlObservation:
    digests = _required_mapping(inspection.candidate.get("digests"), "digests")
    return ControlObservation(
        control_id=plan.control_id,
        status="interrupted",
        reward=None,
        reward_vector={},
        verifier_output_digest=None,
        evidence_digest=None,
        image_digest=_required_digest(digests, "image_definition"),
        verifier_digest=_required_digest(digests, "verifier"),
        source_package_digest=_required_digest(digests, "package"),
        staged_package_digest=_required_digest(digests, "package"),
        command=plan.command,
        command_digest=plan.command_digest,
        exception_type="ControlInterrupted",
        diagnostic=message,
        failure_classification="harness_defect",
    )


def _atomic_create_or_verify(path: Path, content: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise PacketConflictError(f"refusing to replace non-identical record: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise PacketConflictError(f"temporary record already exists: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_controls(
    *,
    inspection: Inspection,
    repo_root: Path,
    task_path: Path,
    backend: ControlBackend,
    run_root: Path | None = None,
) -> ControlBundle:
    if not inspection.static_passed:
        raise ControlsNotAdmittedError("static checks failed; zero controls were called")
    repo_root = repo_root.resolve()
    task_dir = task_path if task_path.is_absolute() else repo_root / task_path
    task_dir = task_dir.resolve()
    candidate_id = _required_string(inspection.candidate, "candidate_id")
    source_digest = _required_digest(
        _required_mapping(inspection.candidate.get("digests"), "digests"), "package"
    )
    target = run_root or repo_root / "runs/task-workbench" / candidate_id
    if target.resolve() != (repo_root / "runs/task-workbench" / candidate_id).resolve():
        raise UnsafePathError("controls may write only to the candidate's runs/task-workbench root")
    observations: list[ControlObservation] = []
    target.mkdir(parents=True, exist_ok=True)
    bundle_path = target / "controls.json"
    if bundle_path.is_file():
        existing = load_control_bundle(bundle_path)
        if (
            existing.candidate_id == candidate_id
            and existing.source_package_digest == source_digest
            and len(existing.observations) == len(inspection.control_plan)
        ):
            return existing
        raise PacketConflictError("existing controls.json is partial or belongs to another source")
    for plan in inspection.control_plan:
        try:
            observation = backend.run(
                repo_root=repo_root,
                task_dir=task_dir,
                candidate=inspection.candidate,
                plan=plan,
                run_root=target,
            )
        except ControlInterrupted as exc:
            observation = _interrupted_observation(inspection, plan, str(exc))
            observations.append(observation)
            partial = ControlBundle.build(
                candidate_id=candidate_id,
                source_package_digest=source_digest,
                observations=observations,
            )
            _atomic_create_or_verify(bundle_path, _canonical_bytes(partial.to_dict()))
            return partial
        observations.append(observation)
    bundle = ControlBundle.build(
        candidate_id=candidate_id,
        source_package_digest=source_digest,
        observations=observations,
    )
    _atomic_create_or_verify(bundle_path, _canonical_bytes(bundle.to_dict()))
    return bundle


def load_control_bundle(path: Path) -> ControlBundle:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchError(f"invalid control bundle {path}: {type(exc).__name__}") from exc
    return ControlBundle.from_dict(_required_mapping(value, "control bundle"))


def classify_trial_outcome(
    *,
    agent: str,
    reward: float | None,
    exception_type: str | None,
    expected_reward: float | None,
) -> Classification:
    """Keep infrastructure, task, and ordinary-agent outcomes separate."""
    if exception_type:
        if exception_type in {
            "DockerfileBuildError",
            "EnvironmentBuildError",
            "ImagePullError",
            "RewardFileNotFoundError",
            "RewardFileEmptyError",
            "TaskConfigError",
            "TaskValidationError",
            "VerifierOutputParseError",
        }:
            return "task_defect"
        agent_execution_errors = {
            "AgentRunError",
            "NonZeroAgentExitCodeError",
            "AgentTimeoutError",
        }
        if agent == "oracle" and exception_type in agent_execution_errors:
            return "task_defect"
        if agent not in {"oracle", "nop"} and exception_type in {
            *agent_execution_errors,
            "AgentSafetyRefusalError",
        }:
            return "agent_failure"
        return "harness_defect"
    if agent == "oracle" and expected_reward == 1.0 and reward != 1.0:
        return "task_defect"
    if agent == "nop" and expected_reward == 0.0 and reward != 0.0:
        return "task_defect"
    if agent not in {"oracle", "nop"} and expected_reward is not None and reward != expected_reward:
        return "agent_failure"
    return "expected"


def _trial_exception_type(result: Mapping[str, Any]) -> str | None:
    exception = result.get("exception_info")
    if not isinstance(exception, Mapping):
        return None
    raw_type = exception.get("exception_type")
    return str(raw_type) if raw_type else "HarborTrialException"


def _expected_stage_digest(inspection: Inspection, plan: ControlPlanEntry) -> str:
    raw_files = inspection.candidate.get("files")
    if not isinstance(raw_files, list):
        raise WorkbenchError("candidate files manifest is invalid")
    entries = [dict(_required_mapping(item, "candidate file")) for item in raw_files]
    if plan.mutation_path is not None:
        mutation = next(
            (item for item in entries if item.get("path") == plan.mutation_path),
            None,
        )
        solution = next(
            (item for item in entries if item.get("path") == "solution/solve.sh"),
            None,
        )
        if mutation is None or solution is None:
            raise WorkbenchError("adversarial plan cannot be reconstructed from manifest")
        solution["size_bytes"] = mutation["size_bytes"]
        solution["digest"] = mutation["digest"]
    entries.append(
        {
            "path": NETWORK_OVERLAY_RELATIVE,
            "role": "image",
            "type": "file",
            "size_bytes": len(NETWORK_OVERLAY_CONTENT),
            "digest": _sha256_bytes(NETWORK_OVERLAY_CONTENT),
        }
    )
    tree_payload = [
        {
            "path": item["path"],
            "type": item["type"],
            "size_bytes": item["size_bytes"],
            "digest": item["digest"],
        }
        for item in sorted(entries, key=lambda item: str(item["path"]))
    ]
    return _sha256_bytes(_canonical_bytes(tree_payload))


def _harbor_task_digest(task_dir: Path) -> str:
    """Reproduce Harbor Packager's default local-task content digest."""
    files: list[Path] = []
    for relative in ("task.toml", "instruction.md", "README.md"):
        path = task_dir / relative
        if path.is_file():
            files.append(path)
    for relative in ("environment", "tests", "solution", "steps"):
        root = task_dir / relative
        if not root.exists():
            continue
        files.extend(path for path in root.rglob("*") if path.is_file())

    def ignored(path: Path) -> bool:
        relative = path.relative_to(task_dir)
        return bool(
            "__pycache__" in relative.parts
            or path.name == ".DS_Store"
            or path.suffix == ".pyc"
            or path.suffix in {".swp", ".swo"}
            or path.name.endswith("~")
        )

    outer = hashlib.sha256()
    for path in sorted(
        (path for path in files if not ignored(path)),
        key=lambda item: item.relative_to(task_dir).as_posix(),
    ):
        relative = path.relative_to(task_dir).as_posix()
        file_digest = _sha256_file(path).removeprefix("sha256:")
        outer.update(f"{relative}\0{file_digest}\n".encode())
    return f"sha256:{outer.hexdigest()}"


def _validate_control_evidence(
    *,
    inspection: Inspection,
    plan: ControlPlanEntry,
    observation: ControlObservation,
    repo_root: Path | None,
) -> tuple[Diagnostic, ...]:
    if observation.status != "completed":
        return ()
    if repo_root is None:
        return (
            _diag(
                "control_evidence_root_missing",
                observation.control_id,
                "completed controls require a repository root for evidence verification",
                classification="harness_defect",
            ),
        )
    candidate_id = _required_string(inspection.candidate, "candidate_id")
    run_root = repo_root.resolve() / "runs/task-workbench" / candidate_id
    job_name = plan.command[plan.command.index("--job-name") + 1]
    expected_job = run_root / "jobs" / job_name
    expected_job_relative = _repo_relative(expected_job, repo_root)
    stage = run_root / "staging" / plan.control_id
    diagnostics: list[Diagnostic] = []
    if observation.job_path != expected_job_relative:
        diagnostics.append(
            _diag(
                "control_job_path_invalid",
                observation.control_id,
                "job_path does not name the frozen control job",
            )
        )
        return tuple(diagnostics)
    if not expected_job.is_dir():
        diagnostics.append(
            _diag(
                "control_evidence_missing",
                observation.control_id,
                "the cited Harbor job directory is not retained",
                classification="harness_defect",
            )
        )
        return tuple(diagnostics)
    actual_evidence_digest = _tree_digest(expected_job)
    if observation.evidence_digest != actual_evidence_digest:
        diagnostics.append(
            _diag(
                "control_evidence_tampered",
                observation.control_id,
                "retained Harbor job bytes do not match evidence_digest",
            )
        )
    if not stage.is_dir():
        diagnostics.append(
            _diag(
                "control_stage_missing",
                observation.control_id,
                "the isolated staged task is not retained",
                classification="harness_defect",
            )
        )
    else:
        overlay = stage / NETWORK_OVERLAY_RELATIVE
        if not overlay.is_file() or overlay.read_bytes() != NETWORK_OVERLAY_CONTENT:
            diagnostics.append(
                _diag(
                    "control_network_isolation_missing",
                    observation.control_id,
                    "the deterministic Docker no-network overlay is absent or changed",
                )
            )
        actual_stage_digest = _tree_digest(stage)
        expected_stage_digest = _expected_stage_digest(inspection, plan)
        if (
            observation.staged_package_digest != actual_stage_digest
            or actual_stage_digest != expected_stage_digest
        ):
            diagnostics.append(
                _diag(
                    "control_stage_tampered",
                    observation.control_id,
                    "staged task bytes do not reconstruct from candidate and control plan",
                )
            )
    try:
        job = load_job(expected_job)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        diagnostics.append(
            _diag(
                "control_evidence_invalid",
                observation.control_id,
                f"retained Harbor job cannot be loaded: {type(exc).__name__}",
            )
        )
        return tuple(diagnostics)
    if len(job.trials) != 1:
        diagnostics.append(
            _diag(
                "control_trial_count_invalid",
                observation.control_id,
                f"retained Harbor job has {len(job.trials)} trials instead of one",
            )
        )
        return tuple(diagnostics)
    trial = job.trials[0]
    reward_vector = _reward_vector_from_trial(trial.result)
    verifier_output_digest = (
        _sha256_bytes(_canonical_bytes(reward_vector)) if reward_vector else None
    )
    actual_agent = trial.result.get("agent_info")
    actual_agent_name = actual_agent.get("name") if isinstance(actual_agent, Mapping) else None
    expected_stage_path = str(stage.resolve())
    expected_overlay_path = str((stage / NETWORK_OVERLAY_RELATIVE).resolve())
    candidate_name = _required_string(inspection.candidate, "task_name")
    candidate_version = _required_string(inspection.candidate, "task_version")
    result_task_id = trial.result.get("task_id")
    result_config = trial.result.get("config")
    result_task_config = result_config.get("task") if isinstance(result_config, Mapping) else None
    result_environment = (
        result_config.get("environment") if isinstance(result_config, Mapping) else None
    )
    task_checksum = trial.result.get("task_checksum")
    lock_task = trial.lock.get("task")
    lock_agent = trial.lock.get("agent")
    lock_environment = trial.lock.get("environment")
    lock_verifier = trial.lock.get("verifier")
    lock_compose = trial.lock.get("extra_docker_compose")
    task_identity_matches = bool(
        trial.result.get("task_name") == candidate_name
        and isinstance(result_task_id, Mapping)
        and result_task_id.get("path") == expected_stage_path
        and isinstance(result_task_config, Mapping)
        and result_task_config.get("path") == expected_stage_path
        and isinstance(task_checksum, str)
        and re.fullmatch(r"[0-9a-f]{64}", task_checksum)
        and isinstance(lock_task, Mapping)
        and lock_task.get("name") == plan.control_id
        and lock_task.get("version") == candidate_version
        and lock_task.get("type") == "local"
        and lock_task.get("path") == expected_stage_path
    )
    if not task_identity_matches:
        diagnostics.append(
            _diag(
                "control_task_identity_mismatch",
                observation.control_id,
                "retained trial task identity does not name the frozen candidate stage",
            )
        )
    expected_harbor_digest = _harbor_task_digest(stage)
    if not isinstance(lock_task, Mapping) or lock_task.get("digest") != expected_harbor_digest:
        diagnostics.append(
            _diag(
                "control_task_digest_mismatch",
                observation.control_id,
                "Harbor task lock digest does not match the frozen staged task",
            )
        )
    network_binding_matches = bool(
        isinstance(result_environment, Mapping)
        and result_environment.get("type") == "docker"
        and result_environment.get("extra_docker_compose") == [expected_overlay_path]
        and isinstance(lock_environment, Mapping)
        and lock_environment.get("type") == "docker"
        and lock_environment.get("extra_docker_compose") == [expected_overlay_path]
        and isinstance(lock_compose, list)
        and lock_compose
        == [
            {
                "path": expected_overlay_path,
                "digest": _sha256_bytes(NETWORK_OVERLAY_CONTENT),
            }
        ]
        and isinstance(lock_verifier, Mapping)
        and lock_verifier.get("disable") is False
        and lock_verifier.get("environment_mode") == "separate"
    )
    if not network_binding_matches:
        diagnostics.append(
            _diag(
                "control_network_binding_mismatch",
                observation.control_id,
                "retained trial is not bound to the frozen Docker no-network overlay",
            )
        )
    if actual_agent_name != plan.agent:
        diagnostics.append(
            _diag(
                "control_agent_mismatch",
                observation.control_id,
                "retained trial did not use the planned free control agent",
            )
        )
    if not isinstance(lock_agent, Mapping) or lock_agent.get("name") != plan.agent:
        diagnostics.append(
            _diag(
                "control_agent_lock_mismatch",
                observation.control_id,
                "Harbor trial lock does not name the planned free control agent",
            )
        )
    if trial.result.get("verifier_environment_mode") != "separate":
        diagnostics.append(
            _diag(
                "control_verifier_not_isolated",
                observation.control_id,
                "retained trial did not use a separate verifier environment",
            )
        )
    if (
        observation.reward != trial.primary_reward
        or observation.reward_vector != reward_vector
        or observation.verifier_output_digest != verifier_output_digest
        or observation.exception_type != _trial_exception_type(trial.result)
    ):
        diagnostics.append(
            _diag(
                "control_result_tampered",
                observation.control_id,
                "control claims do not match the retained Harbor trial result",
            )
        )
    return _sort_diagnostics(diagnostics)


def _assess_controls(
    inspection: Inspection, bundle: ControlBundle, *, repo_root: Path | None
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    candidate_id = _required_string(inspection.candidate, "candidate_id")
    digests = _required_mapping(inspection.candidate.get("digests"), "digests")
    package_digest = _required_digest(digests, "package")
    image_digest = _required_digest(digests, "image_definition")
    verifier_digest = _required_digest(digests, "verifier")
    if bundle.candidate_id != candidate_id or bundle.source_package_digest != package_digest:
        return (
            _diag(
                "control_source_stale",
                "$controls",
                "control bundle identity does not match the inspected candidate",
            ),
        )
    plan_by_id = {item.control_id: item for item in inspection.control_plan}
    seen: set[str] = set()
    oracle_output_digests: list[str] = []
    for observation in bundle.observations:
        if observation.control_id in seen:
            diagnostics.append(
                _diag("control_duplicate", observation.control_id, "control appears more than once")
            )
            continue
        seen.add(observation.control_id)
        plan = plan_by_id.get(observation.control_id)
        if plan is None:
            diagnostics.append(
                _diag(
                    "control_unknown", observation.control_id, "control is not in the frozen plan"
                )
            )
            continue
        if observation.command != plan.command or observation.command_digest != plan.command_digest:
            diagnostics.append(
                _diag(
                    "control_command_drift",
                    observation.control_id,
                    "executed command differs from plan",
                )
            )
        if observation.command_digest != _sha256_bytes(_canonical_bytes(list(observation.command))):
            diagnostics.append(
                _diag(
                    "control_command_digest_invalid",
                    observation.control_id,
                    "command digest is invalid",
                )
            )
        diagnostics.extend(
            _validate_control_evidence(
                inspection=inspection,
                plan=plan,
                observation=observation,
                repo_root=repo_root,
            )
        )
        if observation.source_package_digest != package_digest:
            diagnostics.append(
                _diag(
                    "control_source_stale", observation.control_id, "source package digest changed"
                )
            )
        if observation.image_digest != image_digest:
            diagnostics.append(
                _diag(
                    "control_image_drift", observation.control_id, "image definition digest changed"
                )
            )
        if observation.verifier_digest != verifier_digest:
            diagnostics.append(
                _diag("control_verifier_drift", observation.control_id, "verifier digest changed")
            )
        if observation.status in {"harness_error", "interrupted"}:
            classification = observation.failure_classification or "harness_defect"
            code = {
                "task_defect": "control_task_error",
                "agent_failure": "control_agent_failure",
                "harness_defect": (
                    "control_interrupted"
                    if observation.status == "interrupted"
                    else "control_harness_error"
                ),
                "expected": "control_harness_error",
            }[classification]
            diagnostics.append(
                _diag(
                    code,
                    observation.control_id,
                    observation.diagnostic or "control did not complete",
                    classification=classification,
                )
            )
            continue
        if observation.exception_type:
            diagnostics.append(
                _diag(
                    "control_harness_exception",
                    observation.control_id,
                    f"completed record contains {observation.exception_type}",
                    classification="harness_defect",
                )
            )
            continue
        if observation.reward != plan.expected_reward:
            code = "oracle_false_negative" if plan.expected_reward == 1.0 else "verifier_permissive"
            diagnostics.append(
                _diag(
                    code,
                    observation.control_id,
                    f"expected exact reward {plan.expected_reward}, observed {observation.reward}",
                )
            )
        if observation.verifier_output_digest is None:
            diagnostics.append(
                _diag(
                    "verifier_output_missing",
                    observation.control_id,
                    "completed control has no verifier output digest",
                )
            )
        elif plan.kind == "oracle":
            oracle_output_digests.append(observation.verifier_output_digest)
        if observation.evidence_digest is None or not SHA256_PATTERN.fullmatch(
            observation.evidence_digest
        ):
            diagnostics.append(
                _diag(
                    "control_evidence_digest_missing",
                    observation.control_id,
                    "completed control must retain a valid evidence digest",
                )
            )
    missing = sorted(set(plan_by_id) - seen)
    for control_id in missing:
        diagnostics.append(
            _diag(
                "control_missing",
                control_id,
                "planned control has no observation",
                classification="harness_defect",
            )
        )
    if len(oracle_output_digests) == ORACLE_REPETITIONS and len(set(oracle_output_digests)) != 1:
        diagnostics.append(
            _diag(
                "verifier_nondeterministic",
                "$controls",
                "consecutive Oracle verifier output vectors are not byte-identical",
            )
        )
    return _sort_diagnostics(diagnostics)


def check_candidate(
    inspection: Inspection,
    controls: ControlBundle | None = None,
    *,
    repo_root: Path | None = None,
) -> CheckReport:
    diagnostics = list(inspection.diagnostics)
    control_diagnostics: tuple[Diagnostic, ...] = ()
    if controls is not None:
        control_diagnostics = _assess_controls(
            inspection,
            controls,
            repo_root=repo_root.resolve() if repo_root is not None else None,
        )
        diagnostics.extend(control_diagnostics)
    sorted_diagnostics = _sort_diagnostics(diagnostics)
    if any(item.severity == "error" for item in inspection.diagnostics):
        disposition: Disposition = "needs_changes"
    elif controls is None:
        disposition = "controls_pending"
    elif any(
        item.severity == "error" and item.classification == "harness_defect"
        for item in control_diagnostics
    ):
        disposition = "harness_blocked"
    elif any(item.severity == "error" for item in control_diagnostics):
        disposition = "needs_changes"
    else:
        disposition = "certified_for_review"
    return CheckReport(
        inspection=inspection,
        controls=controls,
        diagnostics=sorted_diagnostics,
        disposition=disposition,
    )


def _certification_record(
    report: CheckReport,
    *,
    retained_evidence: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    observations = (
        {item.control_id: item for item in report.controls.observations}
        if report.controls is not None
        else {}
    )

    def control_matches(plan: ControlPlanEntry) -> bool:
        observation = observations.get(plan.control_id)
        return bool(
            observation is not None
            and observation.status == "completed"
            and observation.exception_type is None
            and observation.reward == plan.expected_reward
            and observation.verifier_output_digest is not None
            and observation.evidence_digest is not None
        )

    oracle_plan = [item for item in report.inspection.control_plan if item.kind == "oracle"]
    nop_plan = [item for item in report.inspection.control_plan if item.kind == "nop"]
    adversarial_plan = [
        item for item in report.inspection.control_plan if item.kind == "adversarial"
    ]
    oracle_exact = len(oracle_plan) == ORACLE_REPETITIONS and all(
        control_matches(item) for item in oracle_plan
    )
    oracle_outputs = [
        observations[item.control_id].verifier_output_digest
        for item in oracle_plan
        if item.control_id in observations
    ]
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "task_workbench_certification",
        "workbench_version": WORKBENCH_VERSION,
        "candidate_id": report.inspection.candidate["candidate_id"],
        "candidate_record_digest": report.inspection.candidate["candidate_record_digest"],
        "status": report.disposition,
        "certified": report.passed,
        "admission_granted": False,
        "diagnostics": [item.to_dict() for item in report.diagnostics],
        "check_vector": {
            "static": report.inspection.static_passed,
            "oracle_exact_1": oracle_exact,
            "nop_exact_0": len(nop_plan) == 1 and all(
                control_matches(item) for item in nop_plan
            ),
            "invalid_outputs_rejected": len(adversarial_plan) >= MIN_ADVERSARIAL_CASES
            and all(control_matches(item) for item in adversarial_plan),
            "verifier_deterministic": oracle_exact
            and len(oracle_outputs) == ORACLE_REPETITIONS
            and len(set(oracle_outputs)) == 1,
            "isolation": report.inspection.static_passed
            and not any(
                item.code
                in {
                    "agent_image_hidden_leak",
                    "golden_data_leak",
                    "hidden_artifact_exposure",
                    "path_escape",
                    "verifier_not_isolated",
                }
                for item in report.diagnostics
            ),
        },
        "control_plan": [item.to_dict() for item in report.inspection.control_plan],
        "control_bundle": report.controls.to_dict() if report.controls else None,
        "retained_evidence": [dict(item) for item in retained_evidence],
        "human_action_required": (
            "Review this candidate packet; admission requires a separate human-created "
            "library/registry record."
        ),
    }
    body["certification_id"] = "cert-" + hashlib.sha256(_canonical_bytes(body)).hexdigest()[:24]
    return body


def _scrub_repo_paths(value: Any, repo_root: Path) -> Any:
    if isinstance(value, str):
        return value.replace(str(repo_root), "$REPO")
    if isinstance(value, list):
        return [_scrub_repo_paths(item, repo_root) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _scrub_repo_paths(item, repo_root)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return value


def _manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path,
            "type": entry_type,
            "size_bytes": size,
            "digest": digest,
        }
        for path, entry_type, size, digest in _tree_entries(root)
    ]


def _retained_evidence_record(
    *,
    repo_root: Path,
    report: CheckReport,
    plan: ControlPlanEntry,
    observation: ControlObservation,
) -> dict[str, Any]:
    if observation.status != "completed" or observation.job_path is None:
        raise WorkbenchError("only completed, cited controls can be retained")
    candidate_id = _required_string(report.inspection.candidate, "candidate_id")
    expected_job_name = plan.command[plan.command.index("--job-name") + 1]
    expected_job = repo_root / "runs/task-workbench" / candidate_id / "jobs" / expected_job_name
    if observation.job_path != _repo_relative(expected_job, repo_root):
        raise WorkbenchError("control job path changed before packet retention")
    stage = repo_root / "runs/task-workbench" / candidate_id / "staging" / plan.control_id
    if not expected_job.is_dir() or not stage.is_dir():
        raise WorkbenchError("control evidence disappeared before packet retention")
    if _tree_digest(expected_job) != observation.evidence_digest:
        raise WorkbenchError("control evidence changed before packet retention")
    if _tree_digest(stage) != observation.staged_package_digest:
        raise WorkbenchError("control stage changed before packet retention")
    job = load_job(expected_job)
    if len(job.trials) != 1:
        raise WorkbenchError("control evidence must retain exactly one Harbor trial")
    trial = job.trials[0]
    job_result_path = expected_job / "result.json"
    trial_result_path = trial.path / "result.json"
    trial_lock_path = trial.path / "lock.json"
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "task_workbench_retained_control_evidence",
        "candidate_id": candidate_id,
        "control_id": plan.control_id,
        "command": list(plan.command),
        "command_digest": plan.command_digest,
        "source_package_digest": observation.source_package_digest,
        "staged_package_digest": observation.staged_package_digest,
        "image_digest": observation.image_digest,
        "verifier_digest": observation.verifier_digest,
        "job_tree_digest": observation.evidence_digest,
        "job_manifest": _manifest(expected_job),
        "stage_manifest": _manifest(stage),
        "raw_job_result_digest": _sha256_file(job_result_path),
        "raw_trial_result_digest": _sha256_file(trial_result_path),
        "raw_trial_lock_digest": _sha256_file(trial_lock_path),
        "job_result": _scrub_repo_paths(job.result, repo_root),
        "trial_result": _scrub_repo_paths(trial.result, repo_root),
        "trial_lock": _scrub_repo_paths(trial.lock, repo_root),
        "claim_extract": {
            "agent": plan.agent,
            "exception_type": _trial_exception_type(trial.result),
            "reward": trial.primary_reward,
            "reward_vector": _reward_vector_from_trial(trial.result),
            "verifier_environment_mode": trial.result.get("verifier_environment_mode"),
        },
        "omitted_content": [
            "agent logs",
            "artifacts",
            "verifier stdout/stderr",
        ],
        "omission_reason": "avoid retaining candidate outputs or hidden verifier content",
    }
    body["evidence_record_digest"] = _sha256_bytes(_canonical_bytes(body))
    return body


def write_packet(
    *, repo_root: Path, report: CheckReport, output_root: Path | None = None
) -> tuple[Path, Path]:
    repo_root = repo_root.resolve()
    allowed_root = (repo_root / "research/registration/candidates").resolve()
    target_root = (output_root or allowed_root).resolve()
    if not _is_under(target_root, allowed_root):
        raise UnsafePathError("packets may be written only under research/registration/candidates")
    candidate_id = _required_string(report.inspection.candidate, "candidate_id")
    packet_dir = target_root / candidate_id
    if not _is_under(packet_dir, allowed_root):
        raise UnsafePathError("candidate packet path escapes its review root")
    candidate_path = packet_dir / "candidate.json"
    certification_path = packet_dir / "certification.json"
    _atomic_create_or_verify(candidate_path, _canonical_bytes(report.inspection.candidate))
    retained: list[dict[str, str]] = []
    if report.controls is not None:
        plan_by_id = {item.control_id: item for item in report.inspection.control_plan}
        for observation in report.controls.observations:
            if observation.status != "completed":
                continue
            plan = plan_by_id.get(observation.control_id)
            if plan is None:
                raise WorkbenchError("cannot retain evidence for an unknown control")
            record = _retained_evidence_record(
                repo_root=repo_root,
                report=report,
                plan=plan,
                observation=observation,
            )
            evidence_path = packet_dir / "evidence" / f"{observation.control_id}.json"
            content = _canonical_bytes(record)
            _atomic_create_or_verify(evidence_path, content)
            retained.append(
                {
                    "control_id": observation.control_id,
                    "path": _repo_relative(evidence_path, repo_root),
                    "digest": _sha256_bytes(content),
                }
            )
    _atomic_create_or_verify(
        certification_path,
        _canonical_bytes(_certification_record(report, retained_evidence=retained)),
    )
    return candidate_path, certification_path


def _source_from_args(args: argparse.Namespace) -> CandidateSource:
    return CandidateSource(
        source_uri=args.source_uri,
        source_ref=args.source_ref,
        license=args.license,
        provenance_zone=args.zone,
    )


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("task", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--license", required=True)
    parser.add_argument(
        "--zone",
        choices=("01-external", "02-local-evidence", "03-synthetic", "04-curated"),
        default="03-synthetic",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.task_workbench",
        description="Inspect and certify Harbor task candidates without admitting them.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan", help="inspect and print frozen local control plan")
    _add_common_arguments(plan)
    check = subparsers.add_parser("check", help="run static checks and assess/run controls")
    _add_common_arguments(check)
    controls = check.add_mutually_exclusive_group()
    controls.add_argument("--controls", type=Path)
    controls.add_argument("--run-controls", action="store_true")
    packet = subparsers.add_parser("packet", help="write deterministic candidate review records")
    _add_common_arguments(packet)
    packet.add_argument("--controls", type=Path)
    packet.add_argument("--output-root", type=Path)
    return parser


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    repo_root = args.repo_root.resolve()
    source = _source_from_args(args)
    try:
        inspection = inspect_candidate(repo_root=repo_root, task_path=args.task, source=source)
        if args.command == "plan":
            sys.stdout.buffer.write(_canonical_bytes(inspection.to_dict()))
            return 0 if inspection.static_passed else 1
        controls: ControlBundle | None = None
        controls_path = getattr(args, "controls", None)
        if controls_path is not None:
            controls = load_control_bundle(controls_path)
        elif getattr(args, "run_controls", False):
            controls = run_controls(
                inspection=inspection,
                repo_root=repo_root,
                task_path=args.task,
                backend=HarborControlBackend(),
            )
        report = check_candidate(inspection, controls, repo_root=repo_root)
        if args.command == "check":
            sys.stdout.buffer.write(_canonical_bytes(report.to_dict()))
            return 0 if report.passed else 1
        output_root = args.output_root.resolve() if args.output_root else None
        candidate_path, certification_path = write_packet(
            repo_root=repo_root,
            report=report,
            output_root=output_root,
        )
        payload = {
            "candidate": _repo_relative(candidate_path, repo_root),
            "certification": _repo_relative(certification_path, repo_root),
            "disposition": report.disposition,
        }
        sys.stdout.buffer.write(_canonical_bytes(payload))
        return 0 if report.passed else 1
    except WorkbenchError as exc:
        sys.stderr.write(f"task-workbench: {exc}\n")
        return 2


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
