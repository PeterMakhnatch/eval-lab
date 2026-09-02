from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evallab.results import JobRecord, TrialRecord
from evallab.schemas import (
    NetworkIsolationEvidenceV1,
    RunProvenance,
    TaskRuntimeIdentityV1,
    TrialAdmissibilityV1,
    TrialAnalysisSidecar,
    TrialSourceDigestsV1,
    TrialSourcePathsV1,
    build_trial_admissibility,
)

TRIAL_ADMISSIBILITY_FILENAME = "trial-admissibility.json"
TRIAL_ADMISSIBILITY_ROOT = Path("research/evidence/trial-admissibility")

_CONTRACT_CANDIDATES = (
    "benchmark_contract.json",
    "benchmark-contract.json",
    "contract.json",
    "artifacts/app/output/benchmark_contract.json",
    "artifacts/app/output/benchmark-contract.json",
    "artifacts/app/output/contract.json",
)
_TRAJECTORY_CANDIDATES = (
    "agent/trajectory.json",
    "trajectory.json",
    "agent/trajectory.jsonl",
)
_FINAL_STATE_CANDIDATES = (
    "final-state.json",
    "final_state.json",
    "artifacts/app/output/final-state.json",
    "artifacts/app/output/final_state.json",
)
_VERIFIER_RESULT_CANDIDATES = ("verifier/result.json", "verifier_result.json")
_VERIFIER_REWARD_CANDIDATES = ("verifier/reward.txt", "verifier_reward.txt")
_OUTCOME_CANDIDATES = ("artifacts/app/output/result.json", "result.json")
_INTERPRETATION_CANDIDATES = ("analysis/interpretation.json",)


class TrialAdmissibilityError(ValueError):
    """The durable trial authority is malformed, ambiguous, or does not bind its inputs."""


@dataclass(frozen=True)
class VerifiedTrialAdmissibility:
    record: TrialAdmissibilityV1
    artifact_present: bool
    source_binding_verified: bool
    provenance_binding_verified: bool
    registry_binding_verified: bool

    @property
    def causal_eligible(self) -> bool:
        return (
            self.artifact_present
            and self.source_binding_verified
            and self.provenance_binding_verified
            and self.registry_binding_verified
            and self.record.causal_eligible
        )


@dataclass(frozen=True)
class _ResultSnapshot:
    path: Path
    payload: bytes
    digest: str
    stat_identity: tuple[int, int, int, int, int]


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _capture_result_snapshot(
    root: Path,
    *,
    required: bool,
) -> _ResultSnapshot | None:
    path = root / "result.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if not required:
            return None
        raise TrialAdmissibilityError("trial_admissibility_invalid:missing-finished-at") from None
    except OSError as exc:
        raise TrialAdmissibilityError(
            "trial_admissibility_invalid:result-snapshot-unavailable"
        ) from exc
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise TrialAdmissibilityError("trial_admissibility_invalid:result-source-not-regular")
        payload = stream.read()
        after = os.fstat(stream.fileno())
    if _stat_identity(before) != _stat_identity(after) or len(payload) != after.st_size:
        raise TrialAdmissibilityError("trial_admissibility_invalid:result-snapshot-drift")
    return _ResultSnapshot(
        path=path,
        payload=payload,
        digest=f"sha256:{sha256(payload).hexdigest()}",
        stat_identity=_stat_identity(after),
    )


def _assert_result_snapshot_current(snapshot: _ResultSnapshot) -> None:
    try:
        current = os.stat(snapshot.path, follow_symlinks=False)
    except OSError as exc:
        raise TrialAdmissibilityError("trial_admissibility_invalid:result-snapshot-drift") from exc
    if not stat.S_ISREG(current.st_mode) or _stat_identity(current) != snapshot.stat_identity:
        raise TrialAdmissibilityError("trial_admissibility_invalid:result-snapshot-drift")


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest_file(path: Path) -> str:
    return f"sha256:{sha256(path.read_bytes()).hexdigest()}"


def _resolve_one(
    root: Path,
    candidates: Sequence[str],
    source: str,
    *,
    prefer_first: bool = False,
) -> tuple[Path, ...]:
    found: list[Path] = []
    for relative in candidates:
        candidate = root / relative
        if candidate.is_symlink():
            raise TrialAdmissibilityError(
                f"trial_admissibility_invalid:symlink-{source}-source:{relative}"
            )
        if candidate.is_file():
            found.append(candidate)
    if len(found) > 1 and not prefer_first:
        aliases = ",".join(path.relative_to(root).as_posix() for path in found)
        raise TrialAdmissibilityError(
            f"trial_admissibility_invalid:ambiguous-{source}-sources:{aliases}"
        )
    return tuple(found[:1] if prefer_first else found)


def _resolve_sources(
    root: Path,
    *,
    interpretation_path: Path | None = None,
) -> dict[str, tuple[Path, ...]]:
    verifier = (
        *_resolve_one(root, _VERIFIER_RESULT_CANDIDATES, "verifier-result"),
        *_resolve_one(root, _VERIFIER_REWARD_CANDIDATES, "verifier-reward"),
    )
    interpretation = _resolve_one(root, _INTERPRETATION_CANDIDATES, "interpretation")
    if interpretation_path is not None:
        external = interpretation_path.resolve()
        if interpretation_path.is_symlink():
            raise TrialAdmissibilityError(
                "trial_admissibility_invalid:symlink-interpretation-source"
            )
        if interpretation:
            raise TrialAdmissibilityError(
                "trial_admissibility_invalid:ambiguous-interpretation-sources"
            )
        interpretation = (external,) if external.is_file() else ()
    return {
        "contract": _resolve_one(root, _CONTRACT_CANDIDATES, "contract"),
        "trajectory": _resolve_one(root, _TRAJECTORY_CANDIDATES, "trajectory"),
        "final_state": _resolve_one(root, _FINAL_STATE_CANDIDATES, "final-state"),
        "verifier": verifier,
        "outcome": _resolve_one(root, _OUTCOME_CANDIDATES, "outcome", prefer_first=True),
        "interpretation": interpretation,
    }


def _source_path_label(path: Path, *, root: Path, repo_root: Path | None) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        if repo_root is None:
            raise TrialAdmissibilityError(
                "trial_admissibility_invalid:external-source-without-repository-root"
            ) from None
        resolved_repo = repo_root.resolve()
        try:
            relative = path.relative_to(resolved_repo).as_posix()
        except ValueError as exc:
            raise TrialAdmissibilityError(
                "trial_admissibility_invalid:source-outside-repository"
            ) from exc
        return f"repo:{relative}"


def _validate_interpretation_source(
    path: Path,
    *,
    root: Path,
    repo_root: Path | None,
    trial_id: str,
    result_snapshot: _ResultSnapshot | None,
) -> None:
    try:
        sidecar = TrialAnalysisSidecar.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise TrialAdmissibilityError(
            "trial_admissibility_invalid:malformed-interpretation"
        ) from exc
    if sidecar.validation_status != "valid" or sidecar.validation_errors:
        raise TrialAdmissibilityError("trial_admissibility_invalid:invalid-interpretation")
    if str(sidecar.source_trial_id) != trial_id:
        raise TrialAdmissibilityError("trial_admissibility_invalid:interpretation-trial-id-drift")
    declared_source = Path(sidecar.source_trial_path)
    if declared_source.is_absolute():
        bound_source = declared_source.resolve()
    elif repo_root is not None:
        bound_source = (repo_root.resolve() / declared_source).resolve()
    else:
        raise TrialAdmissibilityError(
            "trial_admissibility_invalid:relative-interpretation-source-without-root"
        )
    if bound_source != root:
        raise TrialAdmissibilityError(
            "trial_admissibility_invalid:interpretation-source-path-drift"
        )
    trajectory_path = root / "agent/trajectory.json"
    lock_path = root / "lock.json"
    if result_snapshot is None or not lock_path.is_file():
        raise TrialAdmissibilityError(
            "trial_admissibility_invalid:interpretation-source-digest-drift"
        )
    try:
        lock = json.loads(lock_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise TrialAdmissibilityError(
            "trial_admissibility_invalid:interpretation-source-digest-drift"
        ) from exc
    task = lock.get("task") if isinstance(lock, Mapping) else None
    task_digest = task.get("digest") if isinstance(task, Mapping) else None
    expected_task_digest = (
        task_digest
        if isinstance(task_digest, str)
        and len(task_digest) == 71
        and task_digest.startswith("sha256:")
        else _digest_file(lock_path)
    )
    expected_trajectory_digest = (
        _digest_file(trajectory_path) if trajectory_path.is_file() else None
    )
    if (
        sidecar.source_digests.result != result_snapshot.digest
        or sidecar.source_digests.task != expected_task_digest
        or sidecar.source_digests.trajectory != expected_trajectory_digest
    ):
        raise TrialAdmissibilityError(
            "trial_admissibility_invalid:interpretation-source-digest-drift"
        )
    required_files = {
        "result.json",
        "lock.json",
        *(citation.path for citation in sidecar.output.evidence),
    }
    if set(sidecar.source_digests.files) != required_files:
        raise TrialAdmissibilityError(
            "trial_admissibility_invalid:interpretation-source-digest-drift"
        )
    for relative, digest in sidecar.source_digests.files.items():
        if relative == "result.json":
            expected_digest = result_snapshot.digest
        else:
            source = (root / relative).resolve()
            if root != source and root not in source.parents:
                raise TrialAdmissibilityError(
                    "trial_admissibility_invalid:interpretation-source-path-escape"
                )
            expected_digest = _digest_file(source) if source.is_file() else None
        if expected_digest != digest:
            raise TrialAdmissibilityError(
                "trial_admissibility_invalid:interpretation-source-digest-drift"
            )


def _source_authority(
    root: Path,
    *,
    interpretation_path: Path | None = None,
    repo_root: Path | None = None,
    trial_id: str,
    result_snapshot: _ResultSnapshot | None = None,
) -> tuple[TrialSourcePathsV1, TrialSourceDigestsV1]:
    result_snapshot = result_snapshot or _capture_result_snapshot(
        root,
        required=False,
    )
    resolved = _resolve_sources(root, interpretation_path=interpretation_path)
    interpretation = resolved["interpretation"]
    if interpretation:
        _validate_interpretation_source(
            interpretation[0],
            root=root,
            repo_root=repo_root,
            trial_id=trial_id,
            result_snapshot=result_snapshot,
        )
    labels = {
        name: tuple(_source_path_label(path, root=root, repo_root=repo_root) for path in values)
        for name, values in resolved.items()
    }
    paths = TrialSourcePathsV1.model_validate(labels)
    digests: dict[str, str | None] = {}
    for name, values in resolved.items():
        if not values:
            digests[name] = None
        elif len(values) == 1:
            path = values[0]
            digests[name] = (
                result_snapshot.digest
                if result_snapshot is not None and path == result_snapshot.path
                else _digest_file(path)
            )
        else:
            members = [
                {
                    "path": label,
                    "digest": (
                        result_snapshot.digest
                        if result_snapshot is not None and path == result_snapshot.path
                        else _digest_file(path)
                    ),
                }
                for path, label in zip(values, labels[name], strict=True)
            ]
            digests[name] = f"sha256:{sha256(_canonical_bytes({'members': members})).hexdigest()}"
    return paths, TrialSourceDigestsV1.model_validate(digests)


def _provenance_from(value: RunProvenance | Mapping[str, Any] | None) -> RunProvenance | None:
    if value is None:
        return None
    if isinstance(value, RunProvenance):
        return value
    try:
        return RunProvenance.model_validate(value)
    except ValidationError as exc:
        raise TrialAdmissibilityError(
            "trial_admissibility_invalid:malformed-run-provenance"
        ) from exc


def job_run_provenance(job: JobRecord) -> RunProvenance | None:
    value = job.metadata.get("experiment")
    return _provenance_from(value if isinstance(value, Mapping) else None)


def _trial_finished_at(result_snapshot: _ResultSnapshot) -> datetime:
    try:
        result = json.loads(result_snapshot.payload)
    except ValueError as exc:
        raise TrialAdmissibilityError("trial_admissibility_invalid:malformed-finished-at") from exc
    raw = result.get("finished_at") if isinstance(result, Mapping) else None
    if raw is None:
        raise TrialAdmissibilityError("trial_admissibility_invalid:missing-finished-at")
    if not isinstance(raw, str):
        raise TrialAdmissibilityError("trial_admissibility_invalid:malformed-finished-at")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TrialAdmissibilityError("trial_admissibility_invalid:malformed-finished-at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TrialAdmissibilityError("trial_admissibility_invalid:naive-finished-at")
    return parsed


def _provenance_binding(
    record: TrialAdmissibilityV1,
    provenance: RunProvenance | None,
) -> bool:
    if provenance is None:
        return False
    evidence = provenance.network_isolation_evidence
    return (
        record.task_runtime_identity == provenance.task_runtime_identity
        and record.network_isolation_evidence == evidence
        and record.network_isolation_evidence_digest == provenance.network_isolation_evidence_digest
        and record.network_isolation_status == provenance.network_isolation_status
        and record.network_isolation_reason == provenance.network_isolation_reason
        and record.analysis_eligibility == provenance.analysis_eligibility
    )


def _registry_binding(
    identity: TaskRuntimeIdentityV1 | None,
    repo_root: Path | None,
) -> bool:
    if identity is None or repo_root is None:
        return False
    from evallab.registry import (
        RegistryError,
        TaskRegistry,
        compute_task_digests,
        task_runtime_identity,
    )

    record = TaskRegistry.from_repo(repo_root).get(identity.task_id)
    if record is None:
        return False
    task_path = (repo_root / record.task_path).resolve()
    root = repo_root.resolve()
    if task_path != root and root not in task_path.parents:
        return False
    try:
        current_digests = compute_task_digests(task_path)
    except (OSError, RegistryError):
        return False
    return (
        task_runtime_identity(record) == identity
        and current_digests == record.digests
        and current_digests.package == identity.certified_runtime_package_digest
    )


def canonical_trial_admissibility_path(repo_root: Path, trial_id: str) -> Path:
    if not trial_id or "/" in trial_id or "\\" in trial_id or trial_id in {".", ".."}:
        raise TrialAdmissibilityError("trial_admissibility_invalid:unsafe-trial-identity")
    return repo_root.resolve() / TRIAL_ADMISSIBILITY_ROOT / f"{trial_id}.json"


def _bound_interpretation_path(
    record: TrialAdmissibilityV1,
    *,
    repo_root: Path | None,
) -> Path | None:
    source_paths = record.source_paths
    if source_paths is None or len(source_paths.interpretation) != 1:
        return None
    label = source_paths.interpretation[0]
    if not label.startswith("repo:"):
        return None
    if repo_root is None:
        raise TrialAdmissibilityError(
            "trial_admissibility_invalid:repository-bound-interpretation-without-root"
        )
    relative = label.removeprefix("repo:")
    candidate = (repo_root.resolve() / relative).resolve()
    root = repo_root.resolve()
    if candidate != root and root not in candidate.parents:
        raise TrialAdmissibilityError("trial_admissibility_invalid:interpretation-path-escape")
    return candidate


def _verify_trial_admissibility(
    *,
    trial_dir: Path,
    trial_id: str,
    provenance: RunProvenance | Mapping[str, Any] | None,
    repo_root: Path | None,
    authority_path: Path,
    interpretation_path: Path | None,
    result_snapshot: _ResultSnapshot | None,
) -> VerifiedTrialAdmissibility:
    root = trial_dir.resolve()
    parsed_provenance = _provenance_from(provenance)
    if authority_path.is_symlink():
        raise TrialAdmissibilityError("trial_admissibility_invalid:symlink-admissibility-artifact")
    record: TrialAdmissibilityV1 | None = None
    raw: bytes | None = None
    if authority_path.is_file():
        try:
            raw = authority_path.read_bytes()
            record = TrialAdmissibilityV1.model_validate_json(raw)
        except (OSError, ValueError) as exc:
            raise TrialAdmissibilityError(
                "trial_admissibility_invalid:malformed-or-digest-invalid-artifact"
            ) from exc
        if interpretation_path is None:
            interpretation_path = _bound_interpretation_path(
                record,
                repo_root=repo_root,
            )
        result_snapshot = result_snapshot or _capture_result_snapshot(
            root,
            required=True,
        )
    source_paths, source_digests = _source_authority(
        root,
        interpretation_path=interpretation_path,
        repo_root=repo_root,
        trial_id=trial_id,
        result_snapshot=result_snapshot,
    )
    if record is None or raw is None:
        unavailable = build_trial_admissibility(
            trial_id=trial_id,
            task_runtime_identity=None,
            source_digests=source_digests,
            source_paths=source_paths,
            network_isolation_evidence=None,
            evaluated_at=datetime(1970, 1, 1, tzinfo=UTC),
        )
        return VerifiedTrialAdmissibility(
            record=unavailable,
            artifact_present=False,
            source_binding_verified=False,
            provenance_binding_verified=False,
            registry_binding_verified=False,
        )
    assert result_snapshot is not None
    finished_at = _trial_finished_at(result_snapshot)
    if raw != _canonical_bytes(record.model_dump(mode="json")):
        raise TrialAdmissibilityError("trial_admissibility_invalid:noncanonical-artifact-encoding")
    if record.trial_id != trial_id:
        raise TrialAdmissibilityError("trial_admissibility_invalid:trial-id-drift")
    if record.source_paths != source_paths:
        raise TrialAdmissibilityError("trial_admissibility_invalid:source-path-drift")
    if record.source_digests != source_digests:
        raise TrialAdmissibilityError("trial_admissibility_invalid:source-digest-drift")
    if record.evaluated_at != finished_at:
        raise TrialAdmissibilityError("trial_admissibility_invalid:completion-time-drift")
    provenance_verified = _provenance_binding(record, parsed_provenance)
    if not provenance_verified:
        raise TrialAdmissibilityError("trial_admissibility_invalid:provenance-drift")
    registry_verified = _registry_binding(record.task_runtime_identity, repo_root)
    _assert_result_snapshot_current(result_snapshot)
    return VerifiedTrialAdmissibility(
        record=record,
        artifact_present=True,
        source_binding_verified=True,
        provenance_binding_verified=True,
        registry_binding_verified=registry_verified,
    )


def verify_trial_admissibility(
    *,
    trial_dir: Path,
    trial_id: str,
    provenance: RunProvenance | Mapping[str, Any] | None,
    repo_root: Path | None,
    artifact_path: Path | None = None,
    interpretation_path: Path | None = None,
) -> VerifiedTrialAdmissibility:
    root = trial_dir.resolve()
    expected_authority = (
        canonical_trial_admissibility_path(repo_root, trial_id)
        if repo_root is not None
        else root / TRIAL_ADMISSIBILITY_FILENAME
    )
    if artifact_path is not None and artifact_path.resolve() != expected_authority.resolve():
        raise TrialAdmissibilityError("trial_admissibility_invalid:alternate-authority-path")
    return _verify_trial_admissibility(
        trial_dir=root,
        trial_id=trial_id,
        provenance=provenance,
        repo_root=repo_root,
        authority_path=expected_authority,
        interpretation_path=interpretation_path,
        result_snapshot=None,
    )


def _atomic_publish(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    published = False
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            published = True
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise TrialAdmissibilityError(
                    "trial_admissibility_invalid:conflicting-existing-artifact"
                ) from None
        if published:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def finalize_trial_admissibility(
    *,
    job: JobRecord,
    trial: TrialRecord,
    repo_root: Path,
    interpretation_path: Path | None = None,
    artifact_path: Path | None = None,
) -> VerifiedTrialAdmissibility | None:
    """Publish authority only after every exact causal source exists."""
    destination = canonical_trial_admissibility_path(repo_root, trial.id)
    if artifact_path is not None and artifact_path.resolve() != destination.resolve():
        raise TrialAdmissibilityError("trial_admissibility_invalid:alternate-authority-path")

    root = trial.path.resolve()
    result_snapshot = _capture_result_snapshot(root, required=False)
    provenance = job_run_provenance(job)
    source_paths, source_digests = _source_authority(
        root,
        interpretation_path=interpretation_path,
        repo_root=repo_root,
        trial_id=trial.id,
        result_snapshot=result_snapshot,
    )
    evidence: NetworkIsolationEvidenceV1 | None = (
        provenance.network_isolation_evidence if provenance is not None else None
    )
    if any(value is None for value in source_digests.model_dump().values()):
        return None
    if result_snapshot is None:
        raise TrialAdmissibilityError("trial_admissibility_invalid:missing-finished-at")
    finished_at = _trial_finished_at(result_snapshot)
    record = build_trial_admissibility(
        trial_id=trial.id,
        task_runtime_identity=(
            provenance.task_runtime_identity if provenance is not None else None
        ),
        source_digests=source_digests,
        source_paths=source_paths,
        network_isolation_evidence=evidence,
        evaluated_at=finished_at,
    )
    payload = _canonical_bytes(record.model_dump(mode="json"))
    _assert_result_snapshot_current(result_snapshot)
    _atomic_publish(destination, payload)
    return _verify_trial_admissibility(
        trial_dir=root,
        trial_id=trial.id,
        provenance=provenance,
        repo_root=repo_root,
        authority_path=destination,
        interpretation_path=interpretation_path,
        result_snapshot=result_snapshot,
    )
