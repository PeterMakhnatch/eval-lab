"""Fail-closed, immutable sidecars for promoted historical trial evidence.

Historical runs predate benchmark-event emission.  This module never writes to a
legacy trial; it either emits a complete regenerated bundle in a separate root
or emits a typed irrecoverable manifest.  A sidecar is descriptive-only unless
an independent future provenance authority supplies immutable enforcement
receipts.  In particular, legacy paths, config/lock platform flags, host
allowlists, and arbitrary ``evidence/*.json`` files have no provenance power.
Each manifest records the emitting module's digest: regeneration by a later
refactor changes emitted bytes for identical sources.  Harmless (additive and
digest-bound) but expected.
"""


from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, ValidationError, model_validator

from evallab.interpretation.benchmark_events import TrialBundle, load_trial_bundle
from evallab.registry import (
    RegistryError,
    TaskRegistry,
    compute_task_digests,
    harbor_task_digest,
    task_runtime_identity,
)
from evallab.schemas import (
    ContractModel,
    TaskRuntimeIdentityV1,
    TrialSourceDigestsV1,
    TrialSourcePathsV1,
    build_trial_admissibility,
)

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
SIDECAR_SCHEMA_VERSION = "historical-sidecar/v1"
GENERATOR_ID = "evallab.historical-sidecar"
GENERATOR_VERSION = "1"
_MANIFEST_FILENAME = "historical-sidecar.json"
_BUNDLE_FILES = (
    "bundle/benchmark_contract.json",
    "bundle/benchmark-events.jsonl",
    "bundle/final-state.json",
)


class HistoricalSidecarError(ValueError):
    """Base error for historical sidecar generation and verification."""


class HistoricalSidecarRefusal(HistoricalSidecarError):
    """A fail-closed typed refusal for a sidecar or its immutable source."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SourceFileDigestV1(ContractModel):
    """Digest of one no-follow source file, relative only to its source root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    digest: Digest

    @model_validator(mode="after")
    def path_is_safe(self) -> SourceFileDigestV1:
        candidate = Path(self.path)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in self.path:
            raise ValueError("source file path is not a safe relative POSIX path")
        return self


class ReconstructionFieldV1(ContractModel):
    """How a sidecar field was reconstructed, or why it cannot be exact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal[
        "registered-harbor-runtime-binding/v1",
        "trajectory-structural-projection/v1",
        "outcome-digest-projection/v1",
        "verifier-digest-binding/v1",
        "irrecoverable",
    ]
    source_digests: tuple[Digest, ...] = ()
    irrecoverable_reason: str | None = None

    @model_validator(mode="after")
    def method_and_reason_match(self) -> ReconstructionFieldV1:
        if self.method == "irrecoverable":
            if not self.irrecoverable_reason:
                raise ValueError("irrecoverable reconstruction requires a typed reason")
        elif self.irrecoverable_reason is not None:
            raise ValueError("reconstructed field cannot carry an irrecoverable reason")
        if not self.source_digests:
            raise ValueError("reconstruction requires at least one canonical source digest")
        return self


class SidecarBundleDigestsV1(ContractModel):
    """Digests of exactly the three loader-consumable regenerated artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: Digest
    events: Digest
    final_state: Digest


class HistoricalSidecarV1(ContractModel):
    """Self-authenticating manifest binding a sidecar to immutable legacy bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[SIDECAR_SCHEMA_VERSION] = SIDECAR_SCHEMA_VERSION
    generator_id: Literal[GENERATOR_ID] = GENERATOR_ID
    generator_version: Literal[GENERATOR_VERSION] = GENERATOR_VERSION
    generator_module_digest: Digest
    source_job_id: str = Field(min_length=1)
    source_trial_id: str = Field(min_length=1)
    source_job_files: tuple[SourceFileDigestV1, ...]
    source_trial_files: tuple[SourceFileDigestV1, ...]
    source_job_identity_digest: Digest
    source_trial_tree_digest: Digest
    task_runtime_identity: TaskRuntimeIdentityV1 | None = None
    task_runtime_harbor_digest: Digest | None = None
    provenance_disposition: Literal["descriptive-only"] = "descriptive-only"
    provenance_reason: Literal[
        "historical_sidecar_irrecoverable:missing-immutable-isolation-receipts"
    ] = "historical_sidecar_irrecoverable:missing-immutable-isolation-receipts"
    reconstruction: dict[str, ReconstructionFieldV1]
    status: Literal["ready", "irrecoverable"]
    irrecoverable_reason: str | None = None
    bundle_digests: SidecarBundleDigestsV1 | None = None
    manifest_digest: Digest

    @model_validator(mode="after")
    def binding_and_digest_match(self) -> HistoricalSidecarV1:
        if _manifest_of_files(self.source_job_files) != self.source_job_identity_digest:
            raise ValueError("source job identity digest mismatch")
        if _manifest_of_files(self.source_trial_files) != self.source_trial_tree_digest:
            raise ValueError("source trial tree digest mismatch")
        if self.status == "ready":
            if (
                self.task_runtime_identity is None
                or self.task_runtime_harbor_digest is None
                or self.bundle_digests is None
                or self.irrecoverable_reason is not None
            ):
                raise ValueError("ready sidecar lacks a complete runtime or bundle binding")
        elif self.irrecoverable_reason is None:
            raise ValueError("irrecoverable sidecar lacks a typed reason")
        body = self.model_dump(mode="json", exclude={"manifest_digest"})
        if self.manifest_digest != _digest_bytes(_canonical_json_bytes(body)):
            raise ValueError("historical sidecar manifest digest mismatch")
        return self


@dataclass(frozen=True)
class HistoricalSidecarExclusion:
    source_job_id: str
    source_trial_id: str
    code: str


@dataclass(frozen=True)
class HistoricalCorpusLoadReport:
    corpus_count: int
    loaded: tuple[TrialBundle, ...]
    exclusions: tuple[HistoricalSidecarExclusion, ...]

    @property
    def loaded_count(self) -> int:
        return len(self.loaded)

    def exclusion_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for exclusion in self.exclusions:
            counts[exclusion.code] = counts.get(exclusion.code, 0) + 1
        return dict(sorted(counts.items()))


def _canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _digest_file(path: Path) -> str:
    return _digest_bytes(path.read_bytes())


def _manifest_of_files(files: tuple[SourceFileDigestV1, ...]) -> str:
    payload = [{"path": item.path, "digest": item.digest} for item in files]
    return _digest_bytes(_canonical_json_bytes(payload))


def _source_file_manifest(root: Path, *, recursive: bool) -> tuple[SourceFileDigestV1, ...]:
    if root.is_symlink() or not root.is_dir():
        raise HistoricalSidecarRefusal("historical_sidecar_refused:unsafe-or-missing-source-root")
    candidates = root.rglob("*") if recursive else root.iterdir()
    files: list[SourceFileDigestV1] = []
    for candidate in sorted(candidates, key=lambda item: item.as_posix()):
        if candidate.is_symlink():
            raise HistoricalSidecarRefusal("historical_sidecar_refused:symlink-source")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise HistoricalSidecarRefusal("historical_sidecar_refused:nonregular-source")
        files.append(
            SourceFileDigestV1(
                path=candidate.relative_to(root).as_posix(), digest=_digest_file(candidate)
            )
        )
    if not files:
        raise HistoricalSidecarRefusal("historical_sidecar_refused:empty-source")
    return tuple(files)


def _job_file_manifest(job_dir: Path) -> tuple[SourceFileDigestV1, ...]:
    if job_dir.is_symlink() or not job_dir.is_dir():
        raise HistoricalSidecarRefusal("historical_sidecar_refused:unsafe-or-missing-job-root")
    files: list[SourceFileDigestV1] = []
    for candidate in sorted(job_dir.iterdir(), key=lambda item: item.name):
        if candidate.is_symlink():
            raise HistoricalSidecarRefusal("historical_sidecar_refused:symlink-job-source")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise HistoricalSidecarRefusal("historical_sidecar_refused:nonregular-job-source")
        files.append(SourceFileDigestV1(path=candidate.name, digest=_digest_file(candidate)))
    if not files:
        raise HistoricalSidecarRefusal("historical_sidecar_refused:empty-job-identity")
    return tuple(files)


def _file_digest(manifest: HistoricalSidecarV1, relative: str) -> str:
    for item in manifest.source_trial_files:
        if item.path == relative:
            return item.digest
    raise HistoricalSidecarRefusal(f"historical_sidecar_irrecoverable:missing-source:{relative}")


def _require_json(path: Path, code: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise HistoricalSidecarRefusal(code)
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise HistoricalSidecarRefusal(code) from exc
    if not isinstance(value, dict):
        raise HistoricalSidecarRefusal(code)
    return value


def _aggregate_digest(members: tuple[tuple[str, str], ...]) -> str:
    return _digest_bytes(_canonical_json_bytes({"members": [{"path": path, "digest": digest} for path, digest in members]}))


def _verifier_digest(manifest: HistoricalSidecarV1) -> tuple[str, tuple[str, ...]]:
    members = tuple(
        (item.path, item.digest)
        for item in manifest.source_trial_files
        if item.path.startswith("verifier/")
    )
    if not members:
        raise HistoricalSidecarRefusal(
            "historical_sidecar_irrecoverable:missing-verifier-evidence"
        )
    return _aggregate_digest(members), tuple(path for path, _ in members)


def _resolve_runtime_identity(
    trial_dir: Path, repo_root: Path
) -> tuple[TaskRuntimeIdentityV1, str, str]:
    lock = _require_json(
        trial_dir / "lock.json", "historical_sidecar_irrecoverable:missing-or-malformed-lock"
    )
    task = lock.get("task")
    if not isinstance(task, dict):
        raise HistoricalSidecarRefusal("historical_sidecar_irrecoverable:missing-task-runtime-identity")
    task_id, version, claimed_harbor_digest = (
        task.get("name"),
        task.get("version"),
        task.get("digest"),
    )
    if (
        not isinstance(task_id, str)
        or not isinstance(version, str)
        or not isinstance(claimed_harbor_digest, str)
    ):
        raise HistoricalSidecarRefusal("historical_sidecar_irrecoverable:missing-task-runtime-identity")
    if not _is_canonical_digest(claimed_harbor_digest):
        raise HistoricalSidecarRefusal("historical_sidecar_refused:noncanonical-runtime-digest")
    registry = TaskRegistry.from_repo(repo_root)
    record = registry.get(task_id)
    if record is None or record.state != "registered":
        raise HistoricalSidecarRefusal("historical_sidecar_irrecoverable:unregistered-task-identity")
    if record.version != version:
        raise HistoricalSidecarRefusal("historical_sidecar_irrecoverable:task-version-drift")
    task_path = (repo_root / record.task_path).resolve()
    try:
        current = compute_task_digests(task_path)
        current_harbor = harbor_task_digest(task_path)
    except (OSError, RegistryError, ValueError) as exc:
        raise HistoricalSidecarRefusal("historical_sidecar_refused:registry-runtime-unreadable") from exc
    if current != record.digests:
        raise HistoricalSidecarRefusal("historical_sidecar_refused:registry-package-digest-drift")
    if current_harbor != claimed_harbor_digest:
        raise HistoricalSidecarRefusal("historical_sidecar_irrecoverable:runtime-package-digest-mismatch")
    return task_runtime_identity(record), claimed_harbor_digest, _digest_file(trial_dir / "lock.json")


def _is_canonical_digest(value: str) -> bool:
    return len(value) == 71 and value.startswith("sha256:") and all(
        char in "0123456789abcdef" for char in value[7:]
    )


def _project_trajectory_events(trajectory_path: Path) -> tuple[bytes, int]:
    trajectory = _require_json(
        trajectory_path, "historical_sidecar_irrecoverable:missing-or-malformed-trajectory"
    )
    steps = trajectory.get("steps")
    if not isinstance(steps, list):
        raise HistoricalSidecarRefusal("historical_sidecar_irrecoverable:missing-trajectory-steps")
    records: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise HistoricalSidecarRefusal("historical_sidecar_irrecoverable:malformed-trajectory-step")
        tool_calls = step.get("tool_calls", [])
        if not isinstance(tool_calls, list):
            raise HistoricalSidecarRefusal("historical_sidecar_irrecoverable:malformed-trajectory-step")
        calls: list[dict[str, str]] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                raise HistoricalSidecarRefusal("historical_sidecar_irrecoverable:malformed-tool-call")
            call_id, function_name = call.get("tool_call_id"), call.get("function_name")
            if not isinstance(call_id, str) or not isinstance(function_name, str):
                raise HistoricalSidecarRefusal("historical_sidecar_irrecoverable:malformed-tool-call")
            calls.append({"tool_call_id": call_id, "function_name": function_name})
        source = step.get("source")
        payload: dict[str, Any] = {
            "source": source if source in {"agent", "system", "tool", "user"} else "unknown",
            "tool_calls": calls,
        }
        if isinstance(step.get("step_id"), int):
            payload["step_id"] = step["step_id"]
        records.append(
            {
                "event_index": index,
                "event_type": "historical_trajectory_step",
                "payload": payload,
            }
        )
    return b"".join(_canonical_json_bytes(record) for record in records), len(records)

def _parse_finished_at(result_path: Path) -> datetime:
    result = _require_json(
        result_path, "historical_sidecar_irrecoverable:missing-or-malformed-result"
    )
    value = result.get("finished_at")
    if not isinstance(value, str):
        raise HistoricalSidecarRefusal("historical_sidecar_irrecoverable:missing-finished-at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoricalSidecarRefusal("historical_sidecar_irrecoverable:malformed-finished-at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoricalSidecarRefusal("historical_sidecar_irrecoverable:naive-finished-at")
    return parsed.astimezone(UTC)


def _bundle_payloads(
    manifest: HistoricalSidecarV1,
    trial_dir: Path,
) -> tuple[dict[str, bytes], dict[str, ReconstructionFieldV1]]:
    identity = manifest.task_runtime_identity
    assert identity is not None
    trajectory_path = trial_dir / "agent/trajectory.json"
    events_bytes, step_count = _project_trajectory_events(trajectory_path)
    trajectory_digest = _file_digest(manifest, "agent/trajectory.json")
    result_digest = _file_digest(manifest, "result.json")
    verifier_digest, _ = _verifier_digest(manifest)
    contract = {
        "benchmark_family": "historical-sidecar",
        "version": identity.task_version,
        "construct": "descriptive-historical-reconstruction",
        "task_id": identity.task_id,
        "verifier_truth_digest": verifier_digest,
        "artifact_paths": {
            "events": "benchmark-events.jsonl",
            "final_state": "final-state.json",
        },
        "cell_factors": {
            "historical_sidecar": "v1",
            "seed": "irrecoverable",
        },
    }
    final_state = {
        "initial_digest": "",
        "final_digest": result_digest,
        "step_count": step_count,
        "mutations": [],
        "invariants_passed": False,
        "details": {
            "reconstruction": "outcome-digest-projection/v1",
            "irrecoverable_fields": ["initial_digest", "mutations", "invariants_passed"],
            "outcome_digest": result_digest,
        },
    }
    payloads = {
        _BUNDLE_FILES[0]: _canonical_json_bytes(contract),
        _BUNDLE_FILES[1]: events_bytes,
        _BUNDLE_FILES[2]: _canonical_json_bytes(final_state),
    }
    reconstruction = {
        "task_runtime_identity": ReconstructionFieldV1(
            method="registered-harbor-runtime-binding/v1",
            source_digests=(
                _file_digest(manifest, "lock.json"),
                identity.registry_record_digest,
                identity.certified_runtime_package_digest,
            ),
        ),
        "benchmark_contract": ReconstructionFieldV1(
            method="registered-harbor-runtime-binding/v1",
            source_digests=(identity.registry_record_digest, manifest.task_runtime_harbor_digest),
        ),
        "benchmark_events": ReconstructionFieldV1(
            method="trajectory-structural-projection/v1", source_digests=(trajectory_digest,)
        ),
        "final_state": ReconstructionFieldV1(
            method="outcome-digest-projection/v1", source_digests=(result_digest, trajectory_digest)
        ),
        "verifier": ReconstructionFieldV1(
            method="verifier-digest-binding/v1", source_digests=(verifier_digest,)
        ),
    }
    return payloads, reconstruction


def _initial_manifest(job_dir: Path, trial_dir: Path) -> HistoricalSidecarV1:
    job_files = _job_file_manifest(job_dir)
    trial_files = _source_file_manifest(trial_dir, recursive=True)
    body: dict[str, Any] = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "generator_id": GENERATOR_ID,
        "generator_version": GENERATOR_VERSION,
        "generator_module_digest": _digest_file(Path(__file__)),
        "source_job_id": job_dir.name,
        "source_trial_id": trial_dir.name,
        "source_job_files": [item.model_dump(mode="json") for item in job_files],
        "source_trial_files": [item.model_dump(mode="json") for item in trial_files],
        "source_job_identity_digest": _manifest_of_files(job_files),
        "source_trial_tree_digest": _manifest_of_files(trial_files),
        "task_runtime_identity": None,
        "task_runtime_harbor_digest": None,
        "provenance_disposition": "descriptive-only",
        "provenance_reason": "historical_sidecar_irrecoverable:missing-immutable-isolation-receipts",
        "reconstruction": {
            "task_runtime_identity": {
                "method": "irrecoverable",
                "source_digests": [_digest_file(trial_dir / "lock.json")]
                if (trial_dir / "lock.json").is_file()
                else ["sha256:" + "0" * 64],
                "irrecoverable_reason": "historical_sidecar_irrecoverable:pending-runtime-resolution",
            }
        },
        "status": "irrecoverable",
        "irrecoverable_reason": "historical_sidecar_irrecoverable:pending-runtime-resolution",
        "bundle_digests": None,
    }
    body["manifest_digest"] = _digest_bytes(_canonical_json_bytes(body))
    return HistoricalSidecarV1.model_validate(body)


def _irrecoverable_manifest(base: HistoricalSidecarV1, code: str) -> HistoricalSidecarV1:
    body = base.model_dump(mode="json", exclude={"manifest_digest"})
    body.update(
        {
            "task_runtime_identity": None,
            "task_runtime_harbor_digest": None,
            "reconstruction": {
                "task_runtime_identity": {
                    "method": "irrecoverable",
                    "source_digests": [
                        _file_digest(base, "lock.json")
                        if any(item.path == "lock.json" for item in base.source_trial_files)
                        else base.source_trial_tree_digest
                    ],
                    "irrecoverable_reason": code,
                }
            },
            "status": "irrecoverable",
            "irrecoverable_reason": code,
            "bundle_digests": None,
        }
    )
    body["manifest_digest"] = _digest_bytes(_canonical_json_bytes(body))
    return HistoricalSidecarV1.model_validate(body)


def _ready_manifest(
    base: HistoricalSidecarV1,
    identity: TaskRuntimeIdentityV1,
    harbor_digest: str,
    payloads: dict[str, bytes],
    reconstruction: dict[str, ReconstructionFieldV1],
) -> HistoricalSidecarV1:
    body = base.model_dump(mode="json", exclude={"manifest_digest"})
    body.update(
        {
            "task_runtime_identity": identity.model_dump(mode="json"),
            "task_runtime_harbor_digest": harbor_digest,
            "reconstruction": {
                name: value.model_dump(mode="json") for name, value in reconstruction.items()
            },
            "status": "ready",
            "irrecoverable_reason": None,
            "bundle_digests": {
                "contract": _digest_bytes(payloads[_BUNDLE_FILES[0]]),
                "events": _digest_bytes(payloads[_BUNDLE_FILES[1]]),
                "final_state": _digest_bytes(payloads[_BUNDLE_FILES[2]]),
            },
        }
    )
    body["manifest_digest"] = _digest_bytes(_canonical_json_bytes(body))
    return HistoricalSidecarV1.model_validate(body)


def _write_new(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError as exc:
        raise HistoricalSidecarRefusal("historical_sidecar_refused:destination-already-exists") from exc
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_sidecar(destination: Path, manifest: HistoricalSidecarV1, payloads: dict[str, bytes]) -> None:
    if destination.exists() or destination.is_symlink():
        raise HistoricalSidecarRefusal("historical_sidecar_refused:destination-already-exists")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for relative, payload in payloads.items():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_new(target, payload)
        _write_new(destination / _MANIFEST_FILENAME, _canonical_json_bytes(manifest.model_dump(mode="json")))
    except BaseException:
        # A partial directory is intentionally left manifest-less and therefore unloadable.
        raise


def emit_historical_sidecar(
    *, job_dir: Path, trial_dir: Path, sidecar_root: Path, repo_root: Path
) -> HistoricalSidecarV1:
    """Emit one additive, self-authenticating sidecar without touching legacy bytes."""
    base = _initial_manifest(job_dir, trial_dir)
    payloads: dict[str, bytes] = {}
    try:
        identity, harbor_digest, _ = _resolve_runtime_identity(trial_dir, repo_root)
        provisional = _ready_manifest(base, identity, harbor_digest, {
            _BUNDLE_FILES[0]: b"{}\n",
            _BUNDLE_FILES[1]: b"",
            _BUNDLE_FILES[2]: b"{}\n",
        }, {
            "task_runtime_identity": ReconstructionFieldV1(
                method="registered-harbor-runtime-binding/v1",
                source_digests=(
                    _file_digest(base, "lock.json"),
                    identity.registry_record_digest,
                    identity.certified_runtime_package_digest,
                ),
            )
        })
        payloads, reconstruction = _bundle_payloads(provisional, trial_dir)
        manifest = _ready_manifest(base, identity, harbor_digest, payloads, reconstruction)
    except HistoricalSidecarRefusal as exc:
        manifest = _irrecoverable_manifest(base, exc.code)
    destination = sidecar_root / job_dir.name / trial_dir.name
    _publish_sidecar(destination, manifest, payloads)
    return manifest


def _read_manifest(sidecar_dir: Path) -> HistoricalSidecarV1:
    path = sidecar_dir / _MANIFEST_FILENAME
    if path.is_symlink() or not path.is_file():
        raise HistoricalSidecarRefusal("historical_sidecar_refused:missing-manifest")
    try:
        raw = path.read_bytes()
        manifest = HistoricalSidecarV1.model_validate_json(raw)
    except (OSError, ValidationError, ValueError) as exc:
        raise HistoricalSidecarRefusal("historical_sidecar_refused:malformed-or-noncanonical-manifest") from exc
    if raw != _canonical_json_bytes(manifest.model_dump(mode="json")):
        raise HistoricalSidecarRefusal("historical_sidecar_refused:noncanonical-manifest-encoding")
    return manifest


def _verify_source_binding(manifest: HistoricalSidecarV1, job_dir: Path, trial_dir: Path) -> None:
    if job_dir.name != manifest.source_job_id or trial_dir.name != manifest.source_trial_id:
        raise HistoricalSidecarRefusal("historical_sidecar_refused:source-identity-drift")
    if _job_file_manifest(job_dir) != manifest.source_job_files:
        raise HistoricalSidecarRefusal("historical_sidecar_refused:source-job-digest-drift")
    if _source_file_manifest(trial_dir, recursive=True) != manifest.source_trial_files:
        raise HistoricalSidecarRefusal("historical_sidecar_refused:source-trial-digest-drift")


def _verify_bundle(manifest: HistoricalSidecarV1, sidecar_dir: Path) -> None:
    if manifest.status != "ready" or manifest.bundle_digests is None:
        raise HistoricalSidecarRefusal(manifest.irrecoverable_reason or "historical_sidecar_refused:missing-ready-binding")
    expected = set(_BUNDLE_FILES)
    entries = tuple(sidecar_dir.rglob("*"))
    for candidate in entries:
        relative = candidate.relative_to(sidecar_dir).as_posix()
        if candidate.is_symlink():
            raise HistoricalSidecarRefusal("historical_sidecar_refused:symlink-bundle-artifact")
        if relative == "bundle/verifier" or relative.startswith("bundle/verifier/"):
            raise HistoricalSidecarRefusal("historical_sidecar_refused:verifier-content-present")
        if candidate.is_dir() and relative != "bundle":
            raise HistoricalSidecarRefusal("historical_sidecar_refused:unexpected-bundle-directory")
    actual = {
        candidate.relative_to(sidecar_dir).as_posix()
        for candidate in entries
        if candidate.is_file() and candidate.name != _MANIFEST_FILENAME
    }
    if actual != expected:
        if any(path.startswith("bundle/verifier/") for path in actual):
            raise HistoricalSidecarRefusal("historical_sidecar_refused:verifier-content-present")
        raise HistoricalSidecarRefusal("historical_sidecar_refused:unexpected-or-missing-bundle-artifact")
    expected_digests = {
        _BUNDLE_FILES[0]: manifest.bundle_digests.contract,
        _BUNDLE_FILES[1]: manifest.bundle_digests.events,
        _BUNDLE_FILES[2]: manifest.bundle_digests.final_state,
    }
    for relative, expected_digest in expected_digests.items():
        candidate = sidecar_dir / relative
        if candidate.is_symlink() or _digest_file(candidate) != expected_digest:
            raise HistoricalSidecarRefusal("historical_sidecar_refused:bundle-digest-drift")


def _sidecar_admissibility(
    manifest: HistoricalSidecarV1, trial_dir: Path
):
    assert manifest.task_runtime_identity is not None
    assert manifest.bundle_digests is not None
    verifier_digest, verifier_paths = _verifier_digest(manifest)
    return build_trial_admissibility(
        trial_id=manifest.source_trial_id,
        task_runtime_identity=manifest.task_runtime_identity,
        source_digests=TrialSourceDigestsV1(
            contract=manifest.bundle_digests.contract,
            trajectory=_file_digest(manifest, "agent/trajectory.json"),
            final_state=manifest.bundle_digests.final_state,
            verifier=verifier_digest,
            outcome=_file_digest(manifest, "result.json"),
            interpretation=manifest.manifest_digest,
        ),
        source_paths=TrialSourcePathsV1(
            contract=("bundle/benchmark_contract.json",),
            trajectory=("source:agent/trajectory.json",),
            final_state=("bundle/final-state.json",),
            verifier=tuple(f"source:{path}" for path in verifier_paths),
            outcome=("source:result.json",),
            interpretation=(_MANIFEST_FILENAME,),
        ),
        network_isolation_evidence=None,
        evaluated_at=_parse_finished_at(trial_dir / "result.json"),
    )


def load_historical_sidecar_bundle(
    *, sidecar_dir: Path, job_dir: Path, trial_dir: Path
) -> TrialBundle:
    """Verify a complete sidecar first, then load its regenerated bundle atomically."""
    manifest = _read_manifest(sidecar_dir)
    _verify_source_binding(manifest, job_dir, trial_dir)
    _verify_bundle(manifest, sidecar_dir)
    if manifest.task_runtime_identity is None:
        raise HistoricalSidecarRefusal("historical_sidecar_refused:missing-task-runtime-identity")
    # The generic loader parses the regenerated artifacts; the replacement binds its
    # downstream admissibility object to the verified immutable sidecar manifest.
    bundle = load_trial_bundle(sidecar_dir / "bundle", trial_id=manifest.source_trial_id)
    return replace(bundle, admissibility=_sidecar_admissibility(manifest, trial_dir))


def discover_promoted_historical_trials(runs_root: Path) -> tuple[tuple[Path, Path], ...]:
    """Discover only promoted historical trials: job directories with PROMOTION.json."""
    if runs_root.is_symlink() or not runs_root.is_dir():
        raise HistoricalSidecarRefusal("historical_sidecar_refused:unsafe-or-missing-runs-root")
    found: list[tuple[Path, Path]] = []
    for job_dir in sorted(runs_root.iterdir(), key=lambda item: item.name):
        if job_dir.is_symlink() or not job_dir.is_dir():
            continue
        promotion = job_dir / "PROMOTION.json"
        if promotion.is_symlink() or not promotion.is_file():
            continue
        for trial_dir in sorted(job_dir.iterdir(), key=lambda item: item.name):
            if trial_dir.is_symlink():
                raise HistoricalSidecarRefusal("historical_sidecar_refused:symlink-trial")
            if trial_dir.is_dir():
                found.append((job_dir, trial_dir))
    return tuple(found)


def emit_historical_corpus(*, repo_root: Path, sidecar_root: Path) -> tuple[HistoricalSidecarV1, ...]:
    """Additively emit one manifest per promoted legacy trial, never overwriting."""
    runs_root = repo_root / "research/evidence/runs"
    return tuple(
        emit_historical_sidecar(
            job_dir=job_dir,
            trial_dir=trial_dir,
            sidecar_root=sidecar_root,
            repo_root=repo_root,
        )
        for job_dir, trial_dir in discover_promoted_historical_trials(runs_root)
    )


def load_historical_corpus(*, repo_root: Path, sidecar_root: Path) -> HistoricalCorpusLoadReport:
    """Fail closed per trial: a manifest either fully loads or has one typed exclusion."""
    trials = discover_promoted_historical_trials(repo_root / "research/evidence/runs")
    loaded: list[TrialBundle] = []
    exclusions: list[HistoricalSidecarExclusion] = []
    for job_dir, trial_dir in trials:
        sidecar_dir = sidecar_root / job_dir.name / trial_dir.name
        try:
            loaded.append(
                load_historical_sidecar_bundle(
                    sidecar_dir=sidecar_dir, job_dir=job_dir, trial_dir=trial_dir
                )
            )
        except HistoricalSidecarRefusal as exc:
            exclusions.append(HistoricalSidecarExclusion(job_dir.name, trial_dir.name, exc.code))
    return HistoricalCorpusLoadReport(len(trials), tuple(loaded), tuple(exclusions))


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--sidecar-root", type=Path, required=True)
    args = parser.parse_args()
    emit_historical_corpus(repo_root=args.repo_root, sidecar_root=args.sidecar_root)
    report = load_historical_corpus(repo_root=args.repo_root, sidecar_root=args.sidecar_root)
    print(
        json.dumps(
            {
                "corpus_count": report.corpus_count,
                "loaded_count": report.loaded_count,
                "exclusion_counts": report.exclusion_counts(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
