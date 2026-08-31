"""Explicit task registry and admission trust boundary for eval-lab.

Task registration is an explicit, inspectable, human-owned fact.
Filesystem location, existence of task.toml, a curated card, or canary
membership never implies registration.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from evallab.schemas import (
    ControlEvidenceRef,
    ExperimentSpec,
    ExternalImportLineageV1,
    ExternalImportTransformationRecordV1,
    TaskAdmissionState,
    TaskAllowedUse,
    TaskCertificationEnvelope,
    TaskContamination,
    TaskControlEvidence,
    TaskDigests,
    TaskLimits,
    TaskRegistryRecord,
)
from evallab.storage.paths import shared_checkout_root
from evallab.task_workbench import (
    ISOLATION_DIAGNOSTIC_CODES,
    LEAKAGE_DIAGNOSTIC_CODES,
)

IGNORED_FILE_NAMES = {".DS_Store", ".git", "__pycache__", ".pytest_cache"}
IGNORED_EXTENSIONS = {".pyc", ".pyo", ".tmp"}
SUPPORTED_TASK_WORKBENCH_VERSIONS = frozenset({"m049-v1", "m049-v2"})


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
    """Raised when a spec task_version does not match the registered record version."""


class TaskControlEvidenceError(RegistryError):
    """Raised when control evidence files are missing, unparseable, tampered, or invalid."""


class TaskCertificationError(RegistryError):
    """Raised when a bound workbench certificate is missing, replayed, or tampered."""


class TaskUsageNotAllowedError(RegistryError):
    """Raised when a task is used for a purpose not permitted in allowed_uses."""


class TaskComponentMissingError(RegistryError):
    """Raised when a registered task package is missing a required component."""


class TaskInventoryPolicyError(RegistryError):
    """Raised when the canary policy cannot support deterministic inventory."""


def _should_ignore_file(path: Path) -> bool:
    if any(part in IGNORED_FILE_NAMES for part in path.parts):
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


def compute_task_digests(task_dir: Path) -> TaskDigests:
    """Compute cryptographic digests for a task package and its sub-components."""
    task_dir = task_dir.resolve()
    if not task_dir.is_dir():
        raise ValueError(f"task directory not found: {task_dir}")

    task_toml = task_dir / "task.toml"
    if not task_toml.is_file():
        raise ValueError(f"task.toml missing in {task_dir}")

    task_toml_digest = compute_subpath_digest(task_toml)

    instruction_path = task_dir / "instruction.md"
    if not instruction_path.exists():
        instruction_path = task_dir / "instructions.md"
    instruction_digest = compute_subpath_digest(instruction_path)

    env_path = task_dir / "environment"
    if not env_path.exists():
        env_path = task_dir / "Dockerfile"
    environment_digest = compute_subpath_digest(env_path)

    verifier_path = task_dir / "tests"
    if not verifier_path.exists():
        verifier_path = task_dir / "verifier"
    verifier_digest = compute_subpath_digest(verifier_path)

    package_digest = task_directory_digest(task_dir)

    return TaskDigests(
        task_toml=task_toml_digest,
        instruction=instruction_digest,
        environment=environment_digest,
        verifier=verifier_digest,
        package=package_digest,
    )


def harbor_task_digest(task_dir: Path) -> str:
    """Reproduce Harbor's default local-task package digest."""
    files: list[Path] = []
    for relative in ("task.toml", "instruction.md", "README.md"):
        path = task_dir / relative
        if path.is_file():
            files.append(path)
    for relative in ("environment", "tests", "solution", "steps"):
        path = task_dir / relative
        if path.exists():
            files.extend(item for item in path.rglob("*") if item.is_file())

    def ignored(path: Path) -> bool:
        relative = path.relative_to(task_dir)
        return bool(
            "__pycache__" in relative.parts
            or path.name == ".DS_Store"
            or path.suffix in {".pyc", ".swp", ".swo"}
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


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _external_import_artifact(repo_root: Path, relative: str, label: str) -> Path:
    path = Path(relative)
    resolved = (repo_root / path).resolve()
    imports_root = (repo_root / "research/registration/imports").resolve()
    try:
        resolved.relative_to(imports_root)
    except ValueError as exc:
        raise TaskCertificationError(f"{label} is outside research/registration/imports") from exc
    if path.is_absolute() or ".." in path.parts or not resolved.is_file():
        raise TaskCertificationError(f"{label} is missing or has an unsafe path")
    return resolved


def _verify_external_import_binding(
    repo_root: Path,
    lineage: ExternalImportLineageV1,
    *,
    source_uri: str,
    source_ref: str,
    task_id: str,
    task_version: str,
    package_digest: str,
) -> None:
    record_path = _external_import_artifact(
        repo_root, lineage.transformation_record_path, "transformation record"
    )
    raw_bytes = record_path.read_bytes()
    if _digest_bytes(raw_bytes) != lineage.transformation_record_digest:
        raise TaskCertificationError("transformation record digest mismatch")
    try:
        record = ExternalImportTransformationRecordV1.model_validate_json(raw_bytes)
    except ValidationError as exc:
        raise TaskCertificationError(f"transformation record is invalid: {exc}") from exc
    exact_source = {
        "source_uri": source_uri,
        "source_ref": source_ref,
        "source_task_id": lineage.source_task_id,
        "source_checkpoint_ref": lineage.source_checkpoint_ref,
        "source_package_digest": lineage.source_package_digest,
    }
    if record.source.model_dump(mode="json") != exact_source:
        raise TaskCertificationError("transformation record input source lineage mismatch")
    if (
        record.output.task_id != task_id
        or record.output.task_version != task_version
        or record.output.registry_package_digest != package_digest
    ):
        raise TaskCertificationError("transformation record output runtime package mismatch")
    evidence_path = _external_import_artifact(
        repo_root,
        record.semantic_equivalence.evidence_path,
        "semantic equivalence evidence",
    )
    if evidence_path == record_path:
        raise TaskCertificationError(
            "semantic equivalence evidence and transformation record paths are ambiguous"
        )
    if _digest_bytes(evidence_path.read_bytes()) != record.semantic_equivalence.evidence_digest:
        raise TaskCertificationError("semantic equivalence evidence digest mismatch")


def verify_external_import_lineage(repo_root: Path, record: TaskRegistryRecord) -> None:
    """Reopen and verify a registry record's external transformation lineage."""
    lineage = record.external_import_lineage
    successor_external = (
        record.provenance_zone == "01-external"
        and record.certification.workbench_version == "m049-v2"
    )
    if lineage is None:
        if successor_external:
            raise TaskCertificationError(
                "m049-v2 external registry record requires external import lineage"
            )
        return
    if record.provenance_zone != "01-external":
        raise TaskCertificationError(
            "external import lineage is only valid for 01-external registry records"
        )
    if record.source_ref is None:
        raise TaskCertificationError(
            "external import lineage requires an exact registry source_ref"
        )
    _verify_external_import_binding(
        repo_root.resolve(),
        lineage,
        source_uri=record.source_uri,
        source_ref=record.source_ref,
        task_id=record.task_id,
        task_version=record.version,
        package_digest=record.digests.package,
    )


def _packet_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TaskCertificationError(f"{label} must be an object")
    return value


def _verify_packet_artifacts(repo_root: Path, packet_dir: Path, items: Any, label: str) -> None:
    if not isinstance(items, list):
        raise TaskCertificationError(f"{label} must be a list")
    for raw in items:
        item = _packet_mapping(raw, f"{label} item")
        relative = item.get("path")
        digest = item.get("digest")
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise TaskCertificationError(f"{label} item requires path and digest")
        artifact = (repo_root / relative).resolve()
        try:
            artifact.relative_to(packet_dir)
        except ValueError as exc:
            raise TaskCertificationError(
                f"{label} artifact escapes its certificate packet"
            ) from exc
        if not artifact.is_file() or _digest_bytes(artifact.read_bytes()) != digest:
            raise TaskCertificationError(f"{label} artifact is missing or tampered: {relative}")


def certification_envelope_from_packet(
    repo_root: Path,
    packet_path: Path | str,
    *,
    task_id: str,
    task_version: str,
    task_path: str,
    package_digest: str,
    external_import_lineage: ExternalImportLineageV1 | None = None,
) -> TaskCertificationEnvelope:
    """Read and validate a durable workbench packet against one exact task identity."""
    repo_root = repo_root.resolve()
    packet = (
        (repo_root / packet_path).resolve()
        if not Path(packet_path).is_absolute()
        else Path(packet_path).resolve()
    )
    try:
        relative = packet.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise TaskCertificationError("certificate packet escapes the repository") from exc
    expected_root = (repo_root / "research/registration/candidates").resolve()
    try:
        packet.relative_to(expected_root)
    except ValueError as exc:
        raise TaskCertificationError(
            "certificate packet is outside research/registration/candidates"
        ) from exc
    if packet.name != "certification.json" or not packet.is_file():
        raise TaskCertificationError("certificate packet must name an existing certification.json")
    raw_bytes = packet.read_bytes()
    try:
        body = _packet_mapping(json.loads(raw_bytes), "certificate packet")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TaskCertificationError("certificate packet is not valid UTF-8 JSON") from exc
    if body.get("kind") != "task_workbench_certification" or body.get("certified") is not True:
        raise TaskCertificationError("packet is not a certified task_workbench certificate")
    if body.get("status") != "certified_for_review":
        raise TaskCertificationError("certificate packet disposition is not certified_for_review")
    if body.get("workbench_version") not in SUPPORTED_TASK_WORKBENCH_VERSIONS:
        raise TaskCertificationError(
            f"unsupported task workbench version: {body.get('workbench_version')!r}"
        )
    certification_id = body.get("certification_id")
    unsigned_body = dict(body)
    unsigned_body.pop("certification_id", None)
    expected_id = "cert-" + hashlib.sha256(_canonical_bytes(unsigned_body)).hexdigest()[:24]
    if certification_id != expected_id:
        raise TaskCertificationError("certificate certification_id is invalid")

    candidate_path = packet.parent / "candidate.json"
    if not candidate_path.is_file():
        raise TaskCertificationError("certificate candidate.json is missing")
    try:
        candidate = _packet_mapping(json.loads(candidate_path.read_bytes()), "candidate packet")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TaskCertificationError("candidate packet is not valid UTF-8 JSON") from exc
    candidate_digest = candidate.get("candidate_record_digest")
    candidate_unsigned = dict(candidate)
    candidate_unsigned.pop("candidate_record_digest", None)
    if candidate_digest != _digest_bytes(_canonical_bytes(candidate_unsigned)):
        raise TaskCertificationError("candidate record digest is invalid")
    if (
        body.get("candidate_id") != candidate.get("candidate_id")
        or body.get("candidate_record_digest") != candidate_digest
        or body.get("workbench_version") != candidate.get("workbench_version")
    ):
        raise TaskCertificationError("certificate and candidate identities do not match")

    binding = _packet_mapping(body.get("task_binding"), "task_binding")
    exact_identity = {
        "task_id": task_id,
        "task_version": task_version,
        "task_path": task_path,
        "package_digest": package_digest,
    }
    for key, expected in exact_identity.items():
        if binding.get(key) != expected:
            raise TaskCertificationError(
                f"certificate replay/identity mismatch for {key}: "
                f"expected {expected!r}, got {binding.get(key)!r}"
            )
    if (
        candidate.get("task_id") != task_id
        or candidate.get("task_version") != task_version
        or candidate.get("task_path") != task_path
    ):
        raise TaskCertificationError("candidate packet was created for another task")
    candidate_digests = _packet_mapping(candidate.get("digests"), "candidate digests")
    if binding.get("candidate_package_digest") != candidate_digests.get("package") or binding.get(
        "package_digest"
    ) != candidate_digests.get("registry_package"):
        raise TaskCertificationError("certificate package digest lineage is inconsistent")
    source = _packet_mapping(candidate.get("source"), "candidate source")
    raw_lineage = source.get("external_import_lineage")
    packet_version = body.get("workbench_version")
    external_source = source.get("provenance_zone") == "01-external"
    if packet_version == "m049-v2" and external_source and raw_lineage is None:
        raise TaskCertificationError("m049-v2 external packet requires external import lineage")
    if packet_version == "m049-v2" and not external_source and raw_lineage is not None:
        raise TaskCertificationError(
            "external import lineage is ambiguous on a non-external packet"
        )
    if raw_lineage is None:
        if external_import_lineage is not None:
            raise TaskCertificationError(
                "promotion lineage does not match candidate source metadata"
            )
    else:
        try:
            candidate_lineage = ExternalImportLineageV1.model_validate(raw_lineage)
        except ValidationError as exc:
            raise TaskCertificationError(
                f"candidate external import lineage is invalid: {exc}"
            ) from exc
        if external_import_lineage is None or candidate_lineage != external_import_lineage:
            raise TaskCertificationError(
                "promotion lineage does not match candidate source metadata"
            )
        source_uri = source.get("source_uri")
        source_ref = source.get("source_ref")
        if not isinstance(source_uri, str) or not isinstance(source_ref, str):
            raise TaskCertificationError("candidate external import source URI/ref is invalid")
        _verify_external_import_binding(
            repo_root,
            candidate_lineage,
            source_uri=source_uri,
            source_ref=source_ref,
            task_id=task_id,
            task_version=task_version,
            package_digest=package_digest,
        )

    generator = _packet_mapping(body.get("generator_identity"), "generator_identity")
    validator = _packet_mapping(body.get("validator_identity"), "validator_identity")
    if generator == validator:
        raise TaskCertificationError("circular generator/validator self-check is not admissible")
    if generator.get("model") is not None and generator.get("model") == validator.get("model"):
        raise TaskCertificationError("same-model generator/validator claims are circular")

    retained_evidence = body.get("retained_evidence")
    retained_replays = body.get("retained_replays")
    _verify_packet_artifacts(repo_root, packet.parent, retained_evidence, "retained evidence")
    _verify_packet_artifacts(repo_root, packet.parent, retained_replays, "retained replay")
    if not isinstance(retained_evidence, list) or not isinstance(retained_replays, list):
        raise TaskCertificationError("retained evidence and replay claims must be lists")
    evidence_ids = {
        _packet_mapping(item, "retained evidence item").get("control_id")
        for item in retained_evidence
    }
    replay_ids = {
        _packet_mapping(item, "retained replay item").get("control_id") for item in retained_replays
    }
    if (
        "please-hack" not in evidence_ids
        or {
            "fair-alternative",
            "please-hack",
        }
        - replay_ids
    ):
        raise TaskCertificationError(
            "certificate lacks replayable fair-alternative or please-hack evidence"
        )
    bundle = _packet_mapping(body.get("control_bundle"), "control_bundle")
    observations = bundle.get("observations")
    plan = body.get("control_plan")
    if not isinstance(observations, list) or not isinstance(plan, list):
        raise TaskCertificationError("control plan and observations must be lists")
    observation_by_id = {
        _packet_mapping(item, "control observation").get("control_id"): item
        for item in observations
    }
    if len(observation_by_id) != len(observations) or len(observations) != len(plan):
        raise TaskCertificationError("control observations are duplicate or incomplete")

    def controls(kind: str) -> list[dict[str, Any]]:
        ids = [
            _packet_mapping(item, "control plan entry").get("control_id")
            for item in plan
            if _packet_mapping(item, "control plan entry").get("kind") == kind
        ]
        try:
            return [
                _packet_mapping(observation_by_id[control_id], "control observation")
                for control_id in ids
            ]
        except KeyError as exc:
            raise TaskCertificationError("control plan observation is missing") from exc

    oracle = controls("oracle")
    nop = controls("nop")
    invalid = controls("adversarial")
    fair = controls("fair_alternative")
    hack = controls("please_hack")

    def exact(values: list[dict[str, Any]], count: int, reward: float) -> bool:
        return len(values) == count and all(
            item.get("status") == "completed"
            and item.get("exception_type") is None
            and item.get("reward") == reward
            and isinstance(item.get("verifier_output_digest"), str)
            and isinstance(item.get("evidence_digest"), str)
            for item in values
        )

    oracle_exact = exact(oracle, 3, 1.0)
    oracle_outputs = [item.get("verifier_output_digest") for item in oracle]
    diagnostics = body.get("diagnostics")
    if not isinstance(diagnostics, list):
        raise TaskCertificationError("certificate diagnostics must be a list")
    normalized_diagnostics: list[dict[str, Any]] = []
    diagnostic_fields = {"severity", "code", "classification", "path", "message"}
    for raw_diagnostic in diagnostics:
        diagnostic = _packet_mapping(raw_diagnostic, "certificate diagnostic")
        if set(diagnostic) != diagnostic_fields:
            raise TaskCertificationError("certificate diagnostic fields are invalid")
        if (
            diagnostic.get("severity") not in {"error", "warning", "info"}
            or diagnostic.get("classification")
            not in {"task_defect", "harness_defect", "agent_failure", "expected"}
            or any(not isinstance(diagnostic.get(key), str) for key in ("code", "path", "message"))
        ):
            raise TaskCertificationError("certificate diagnostic values are invalid")
        normalized_diagnostics.append(diagnostic)
    plan_ids = {_packet_mapping(item, "control plan entry").get("control_id") for item in plan}
    bundle_valid = not any(
        item["severity"] == "error" and item["path"] == "$controls"
        for item in normalized_diagnostics
    )
    static = not any(
        item["severity"] == "error" and item["path"] not in plan_ids and item["path"] != "$controls"
        for item in normalized_diagnostics
    )
    all_controls_completed = (
        bundle_valid
        and bool(observations)
        and all(
            _packet_mapping(item, "control observation").get("status") == "completed"
            and _packet_mapping(item, "control observation").get("exception_type") is None
            for item in observations
        )
    )
    derived = {
        "all_controls_completed": all_controls_completed,
        "oracle_exact_1_x3": oracle_exact,
        "oracle_stable_output": oracle_exact
        and len(set(oracle_outputs)) == 1
        and oracle_outputs[0] is not None,
        "nop_exact_0_x2": exact(nop, 2, 0.0),
        "invalid_outputs_rejected": len(invalid) >= 3 and exact(invalid, len(invalid), 0.0),
        "fair_alternative_exact_1": exact(fair, 1, 1.0),
        "please_hack_executed": exact(hack, 1, 0.0),
        "hack_detected": False,
        "static": bundle_valid and static,
        "leakage_scan_clean": bundle_valid
        and static
        and not any(item["code"] in LEAKAGE_DIAGNOSTIC_CODES for item in normalized_diagnostics),
        "isolation": static
        and all_controls_completed
        and not any(item["code"] in ISOLATION_DIAGNOSTIC_CODES for item in normalized_diagnostics),
    }
    summary = _packet_mapping(body.get("control_summary"), "control_summary")
    expected_summary = {
        "oracle_runs": len(oracle),
        "nop_runs": len(nop),
        "invalid_probe_runs": len(invalid),
        "fair_alternative_runs": len(fair),
        "please_hack_runs": len(hack),
        "result_digests": [_digest_bytes(_canonical_bytes(item)) for item in observations],
    }
    if summary != expected_summary:
        raise TaskCertificationError("control summary contradicts digest-verified observations")
    vector = _packet_mapping(body.get("check_vector"), "check_vector")
    if any(vector.get(key) != value for key, value in derived.items()):
        raise TaskCertificationError("check vector contradicts digest-verified observations")
    required_true = {
        "all_controls_completed",
        "static",
        "oracle_exact_1_x3",
        "oracle_stable_output",
        "nop_exact_0_x2",
        "invalid_outputs_rejected",
        "fair_alternative_exact_1",
        "please_hack_executed",
        "leakage_scan_clean",
        "isolation",
    }
    if any(vector.get(key) is not True for key in required_true):
        raise TaskCertificationError("certificate required check vector is not fully satisfied")
    if vector.get("hack_detected") is not False:
        raise TaskCertificationError("certificate reports an accepted please-hack exploit")
    axes = _packet_mapping(body.get("axes"), "axes")
    for axis in (
        "task_correctness",
        "verifier_soundness",
        "verifier_completeness",
        "solvability",
    ):
        if _packet_mapping(axes.get(axis), axis).get("status") != "passed":
            raise TaskCertificationError(f"certificate required axis {axis} did not pass")

    envelope_data = {
        "state": "bound",
        "reason": "bound_workbench_certificate_packet",
        "certification_id": certification_id,
        "packet_path": relative,
        "packet_sha256": _digest_bytes(raw_bytes),
        "candidate_id": body.get("candidate_id"),
        "candidate_record_digest": candidate_digest,
        "candidate_package_digest": binding.get("candidate_package_digest"),
        "package_digest": binding.get("package_digest"),
        "workbench_version": body.get("workbench_version"),
        "check_vector": vector,
        "control_summary": body.get("control_summary"),
        "axes": axes,
        "generator_identity": generator,
        "validator_identity": validator,
    }
    try:
        return TaskCertificationEnvelope.model_validate(envelope_data)
    except ValidationError as exc:
        raise TaskCertificationError(f"certificate envelope is invalid: {exc}") from exc


def verify_certification_packet(repo_root: Path, record: TaskRegistryRecord) -> None:
    """Re-read a bound packet and require its strict envelope to match the registry."""
    verify_external_import_lineage(repo_root, record)
    if record.certification.state == "legacy_missing":
        return
    rebuilt = certification_envelope_from_packet(
        repo_root,
        record.certification.packet_path or "",
        task_id=record.task_id,
        task_version=record.version,
        task_path=record.task_path,
        package_digest=record.digests.package,
        external_import_lineage=record.external_import_lineage,
    )
    if rebuilt != record.certification:
        raise TaskCertificationError("stored certification envelope does not match packet bytes")


def _extract_reward_and_agent(
    result_data: dict[str, Any],
    metadata_data: dict[str, Any] | None = None,
    trial_data: dict[str, Any] | None = None,
) -> tuple[str | None, float | None]:
    """Extract agent name and primary reward from Harbor result/metadata/trial dictionaries."""
    # Check stats.evals
    stats = result_data.get("stats")
    if isinstance(stats, dict) and "evals" in stats:
        evals = stats.get("evals", {})
        for key, eval_data in evals.items():
            if not isinstance(eval_data, dict):
                continue
            agent_name = key.split("__")[0] if "__" in key else key
            metrics = eval_data.get("metrics", [])
            observed_reward = None
            if metrics and isinstance(metrics, list) and isinstance(metrics[0], dict):
                if "reward" in metrics[0] and metrics[0]["reward"] is not None:
                    observed_reward = float(metrics[0]["reward"])
                elif "mean" in metrics[0] and metrics[0]["mean"] is not None:
                    observed_reward = float(metrics[0]["mean"])
                elif "correctness" in metrics[0] and metrics[0]["correctness"] is not None:
                    observed_reward = float(metrics[0]["correctness"])
            if observed_reward is None:
                reward_stats = eval_data.get("reward_stats", {}).get("reward", {})
                if isinstance(reward_stats, dict) and reward_stats:
                    with contextlib.suppress(ValueError):
                        observed_reward = float(next(iter(reward_stats.keys())))
            if observed_reward is not None:
                return agent_name, observed_reward

    config = result_data.get("config", {})
    agent_info = config.get("agent", {}) if isinstance(config, dict) else {}
    agent_name = (
        agent_info.get("name")
        if isinstance(agent_info, dict)
        else result_data.get("agent_name", result_data.get("agent"))
    )

    if not agent_name and metadata_data:
        cmd = metadata_data.get("command", [])
        if isinstance(cmd, list) and "--agent" in cmd:
            idx = cmd.index("--agent")
            if idx + 1 < len(cmd):
                agent_name = cmd[idx + 1]

    if not agent_name and trial_data:
        t_cfg = trial_data.get("config", {})
        t_agent = t_cfg.get("agent", {}) if isinstance(t_cfg, dict) else {}
        if isinstance(t_agent, dict):
            agent_name = t_agent.get("name")

    observed_reward = result_data.get("primary_reward", result_data.get("reward"))
    if observed_reward is None and trial_data:
        observed_reward = trial_data.get("primary_reward", trial_data.get("reward"))
        if observed_reward is None:
            verifier_result = trial_data.get("verifier_result", {})
            if isinstance(verifier_result, dict):
                rewards = verifier_result.get("rewards", {})
                if isinstance(rewards, dict) and "reward" in rewards:
                    observed_reward = rewards["reward"]

    if observed_reward is not None:
        try:
            return agent_name, float(observed_reward)
        except (ValueError, TypeError):
            pass

    return agent_name, None


def _verify_control_result(
    data: dict[str, Any],
    lock_data: dict[str, Any],
    *,
    expected_agent: str,
    expected_reward: float,
    record: TaskRegistryRecord,
    evidence_ref: ControlEvidenceRef,
) -> None:
    """Validate one Harbor trial and its lock against the registered package."""
    if "stats" in data or not isinstance(data.get("trial_name"), str):
        raise TaskControlEvidenceError(
            "control evidence must cite a trial result, not a job-level result"
        )
    agent_info = data.get("agent_info")
    agent_name = agent_info.get("name") if isinstance(agent_info, dict) else None
    if agent_name != expected_agent:
        raise TaskControlEvidenceError(
            f"control evidence agent mismatch: expected {expected_agent!r}, got {agent_name!r}"
        )
    verifier_result = data.get("verifier_result")
    rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else None
    observed_reward = rewards.get("reward") if isinstance(rewards, dict) else None
    if observed_reward != expected_reward:
        raise TaskControlEvidenceError(
            f"control evidence reward mismatch for {expected_agent!r}: "
            f"expected {expected_reward}, got {observed_reward}"
        )
    if data["trial_name"] != evidence_ref.trial_name:
        raise TaskControlEvidenceError("control evidence trial_name does not match its reference")

    task_lock = lock_data.get("task")
    if not isinstance(task_lock, dict):
        raise TaskControlEvidenceError("control evidence trial lock is missing task identity")
    if (
        task_lock.get("name") != record.task_id
        or task_lock.get("version") != record.version
        or task_lock.get("type") != "local"
        or task_lock.get("digest") != evidence_ref.harbor_task_digest
    ):
        raise TaskControlEvidenceError(
            f"control evidence task identity mismatch for {record.task_id!r}"
        )
    lock_agent = lock_data.get("agent")
    if not isinstance(lock_agent, dict) or lock_agent.get("name") != expected_agent:
        raise TaskControlEvidenceError("control evidence trial lock has the wrong agent")

    task_name = data.get("task_name")
    result_task_id = data.get("task_id")
    result_config = data.get("config")
    result_task = result_config.get("task") if isinstance(result_config, dict) else None
    task_path = result_task_id.get("path") if isinstance(result_task_id, dict) else None
    config_path = result_task.get("path") if isinstance(result_task, dict) else None
    if (
        not isinstance(task_name, str)
        or task_name.rsplit("/", 1)[-1] != record.task_id
        or not isinstance(task_path, str)
        or Path(task_path).name != record.task_id
        or not isinstance(config_path, str)
        or Path(config_path).name != record.task_id
    ):
        raise TaskControlEvidenceError(
            f"control evidence result identity mismatch for {record.task_id!r}"
        )


def discover_control_evidence(
    task_dir: Path,
    repo_root: Path,
    jobs_roots: Sequence[Path] | None = None,
    *,
    task_version: str | None = None,
) -> TaskControlEvidence:
    """Discover committed trial-level oracle and nop evidence for a task package."""
    repo_root = repo_root.resolve()
    task_dir = task_dir.resolve()
    if not task_dir.is_dir():
        raise ValueError(f"task directory not found: {task_dir}")

    task_id = task_dir.name
    task_toml = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    task_table = task_toml.get("task")
    task_version = task_version or str(
        (task_table.get("version") if isinstance(task_table, dict) else None)
        or task_toml.get("version")
        or "1.0.0"
    )
    task_digests = compute_task_digests(task_dir)
    harbor_digest = harbor_task_digest(task_dir)
    durable_root = (repo_root / "research/evidence/runs").resolve()
    roots = list(jobs_roots) if jobs_roots is not None else [durable_root]

    matches: dict[str, list[tuple[datetime, ControlEvidenceRef]]] = {
        "oracle": [],
        "nop": [],
    }
    for jobs_root in roots:
        jobs_root = jobs_root.resolve()
        try:
            jobs_root.relative_to(durable_root)
        except ValueError as exc:
            raise TaskControlEvidenceError(
                f"control evidence root {jobs_root} is not durable; promotion requires "
                "research/evidence/runs"
            ) from exc
        if not jobs_root.is_dir():
            continue
        for result_path in sorted(jobs_root.rglob("result.json")):
            lock_path = result_path.with_name("lock.json")
            if not lock_path.is_file():
                continue
            try:
                data = json.loads(result_path.read_text())
                lock_data = json.loads(lock_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if (
                not isinstance(data, dict)
                or not isinstance(lock_data, dict)
                or "stats" in data
                or not isinstance(data.get("trial_name"), str)
            ):
                continue
            task_lock = lock_data.get("task")
            agent_lock = lock_data.get("agent")
            if (
                not isinstance(task_lock, dict)
                or task_lock.get("name") != task_id
                or task_lock.get("version") != task_version
                or task_lock.get("type") != "local"
                or task_lock.get("digest") != harbor_digest
                or not isinstance(agent_lock, dict)
            ):
                continue
            agent_name = agent_lock.get("name")
            if agent_name not in matches:
                continue
            agent_info = data.get("agent_info")
            if not isinstance(agent_info, dict) or agent_info.get("name") != agent_name:
                continue
            verifier_result = data.get("verifier_result")
            rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else None
            reward = rewards.get("reward") if isinstance(rewards, dict) else None
            if not isinstance(reward, (int, float)):
                continue
            result_task_id = data.get("task_id")
            result_config = data.get("config")
            result_task = result_config.get("task") if isinstance(result_config, dict) else None
            identity_paths = (
                result_task_id.get("path") if isinstance(result_task_id, dict) else None,
                result_task.get("path") if isinstance(result_task, dict) else None,
            )
            if (
                not isinstance(data.get("task_name"), str)
                or data["task_name"].rsplit("/", 1)[-1] != task_id
                or any(
                    not isinstance(path, str) or Path(path).name != task_id
                    for path in identity_paths
                )
            ):
                continue
            observed_at_str = data.get("finished_at") or data.get("started_at")
            try:
                observed_at = datetime.fromisoformat(str(observed_at_str).replace("Z", "+00:00"))
            except ValueError:
                continue
            evidence_path = result_path.relative_to(repo_root).as_posix()
            job_name = result_path.parent.parent.name
            ref = ControlEvidenceRef(
                job_name=job_name,
                trial_name=data["trial_name"],
                reward=float(reward),
                evidence_path=evidence_path,
                evidence_digest=(f"sha256:{hashlib.sha256(result_path.read_bytes()).hexdigest()}"),
                lock_digest=f"sha256:{hashlib.sha256(lock_path.read_bytes()).hexdigest()}",
                observed_at=observed_at,
                task_id=task_id,
                task_version=task_version,
                task_digests=task_digests,
                harbor_task_digest=harbor_digest,
            )
            matches[agent_name].append((observed_at, ref))

    for agent_name in ("oracle", "nop"):
        matches[agent_name].sort(key=lambda item: item[0], reverse=True)
        if not matches[agent_name]:
            raise TaskControlEvidenceError(
                f"missing durable trial-level {agent_name} control evidence for "
                f"task {task_id!r} under research/evidence/runs"
            )

    oracle_ref = matches["oracle"][0][1]
    nop_ref = matches["nop"][0][1]
    if oracle_ref.reward != 1.0:
        raise TaskControlEvidenceError(
            f"oracle control evidence for {task_id!r} did not pass "
            f"(reward: {oracle_ref.reward}, expected: 1.0)"
        )
    if nop_ref.reward != 0.0:
        raise TaskControlEvidenceError(
            f"nop control evidence for {task_id!r} did not fail "
            f"(reward: {nop_ref.reward}, expected: 0.0)"
        )
    return TaskControlEvidence(oracle=oracle_ref, nop=nop_ref)


def promote_task(
    task_path: Path | str,
    repo_root: Path,
    *,
    registry_dir: Path | None = None,
    task_id: str | None = None,
    task_family: str | None = None,
    version: str | None = None,
    source_uri: str | None = None,
    source_ref: str | None = None,
    external_import_lineage: ExternalImportLineageV1 | None = None,
    license_str: str | None = None,
    provenance_zone: (
        Literal["01-external", "02-local-evidence", "03-synthetic", "04-curated"] | None
    ) = None,
    is_synthetic: bool | None = None,
    timeout_seconds: int | None = None,
    max_memory_mb: int | None = None,
    max_cpus: float | None = None,
    allowed_uses: list[TaskAllowedUse] | None = None,
    contamination: TaskContamination | None = None,
    human_minutes: int | None = None,
    state: TaskAdmissionState = "candidate",
    actor: str | None = None,
    approved_at: datetime | None = None,
    jobs_roots: Sequence[Path] | None = None,
    certification_path: Path | str | None = None,
) -> TaskRegistryRecord:
    """Promote a task package on disk into the explicit task registry."""
    repo_root = repo_root.resolve()
    target_path = (
        (repo_root / task_path).resolve()
        if not Path(task_path).is_absolute()
        else Path(task_path).resolve()
    )
    if not target_path.is_dir():
        raise TaskComponentMissingError(f"task directory not found on disk: {target_path}")

    # Verify completeness
    if not (target_path / "task.toml").is_file():
        raise TaskComponentMissingError(f"task.toml missing in {target_path}")
    if not (
        (target_path / "instruction.md").is_file() or (target_path / "instructions.md").is_file()
    ):
        raise TaskComponentMissingError(f"instruction.md missing in {target_path}")
    if not ((target_path / "environment").exists() or (target_path / "Dockerfile").is_file()):
        raise TaskComponentMissingError(f"environment/Dockerfile missing in {target_path}")
    if not ((target_path / "tests").exists() or (target_path / "verifier").exists()):
        raise TaskComponentMissingError(f"verifier (tests/ or verifier/) missing in {target_path}")

    # Parse task.toml
    try:
        toml_data = tomllib.loads((target_path / "task.toml").read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"failed to parse task.toml in {target_path}: {exc}") from exc

    task_table = toml_data.get("task", {}) if isinstance(toml_data.get("task"), dict) else {}
    meta_table = (
        toml_data.get("metadata", {}) if isinstance(toml_data.get("metadata"), dict) else {}
    )
    env_table = (
        toml_data.get("environment", {}) if isinstance(toml_data.get("environment"), dict) else {}
    )
    ver_table = toml_data.get("verifier", {}) if isinstance(toml_data.get("verifier"), dict) else {}
    agent_table = toml_data.get("agent", {}) if isinstance(toml_data.get("agent"), dict) else {}

    if not task_id:
        task_id = target_path.name
    declared_task_family = task_family or task_table.get("family") or meta_table.get("task_family")
    if not isinstance(declared_task_family, str) or not declared_task_family.strip():
        raise ValueError(
            "task promotion requires an explicit task_family or task.family declaration"
        )
    task_family = declared_task_family.strip()

    if version is None:
        version = str(task_table.get("version") or toml_data.get("version") or "1.0.0")

    try:
        rel_task_path = target_path.relative_to(repo_root).as_posix()
    except ValueError:
        rel_task_path = str(target_path)

    # Inferred defaults
    if provenance_zone is None:
        if rel_task_path.startswith("library/benchmarks/"):
            provenance_zone = "01-external"
        elif rel_task_path.startswith("library/synthetic/"):
            provenance_zone = "03-synthetic"
        elif rel_task_path.startswith("library/curated/"):
            provenance_zone = "04-curated"
        else:
            provenance_zone = "02-local-evidence"

    if is_synthetic is None:
        is_synthetic = provenance_zone == "03-synthetic"

    if license_str is None:
        license_str = meta_table.get("license") or toml_data.get("license")
        if not license_str and (target_path / "LICENSE").is_file():
            license_str = "custom"
        if not license_str and provenance_zone == "02-local-evidence":
            license_str = "MIT"

    if source_uri is None:
        source_uri = f"local/{task_id}@{version}"

    if timeout_seconds is None:
        ver_timeout = float(ver_table.get("timeout_sec", 60.0))
        agent_timeout = float(agent_table.get("timeout_sec", 120.0))
        timeout_seconds = int(ver_timeout + agent_timeout)
        if timeout_seconds < 1:
            timeout_seconds = 1800

    if max_memory_mb is None and "memory_mb" in env_table:
        with contextlib.suppress(ValueError, TypeError):
            max_memory_mb = int(env_table["memory_mb"])

    if max_cpus is None and "cpus" in env_table:
        with contextlib.suppress(ValueError, TypeError):
            max_cpus = float(env_table["cpus"])

    if human_minutes is None:
        if "expert_time_estimate_min" in meta_table:
            with contextlib.suppress(ValueError, TypeError):
                human_minutes = int(float(meta_table["expert_time_estimate_min"]))
        elif "expert_time_estimate_hours" in meta_table:
            with contextlib.suppress(ValueError, TypeError):
                human_minutes = int(float(meta_table["expert_time_estimate_hours"]) * 60)

    if allowed_uses is None:
        allowed_uses = ["measurement", "training"]

    # Compute digests
    digests = compute_task_digests(target_path)
    certification = (
        certification_envelope_from_packet(
            repo_root,
            certification_path,
            task_id=task_id,
            task_version=version,
            task_path=rel_task_path,
            package_digest=digests.package,
            external_import_lineage=external_import_lineage,
        )
        if certification_path is not None
        else TaskCertificationEnvelope()
    )

    reg_dir = (registry_dir or (repo_root / "library/registry")).resolve()
    record_file = reg_dir / f"{task_id}.json"

    # Idempotence and integrity check
    if record_file.is_file():
        try:
            existing_data = json.loads(record_file.read_text())
            existing_record = TaskRegistryRecord.model_validate(existing_data)
        except Exception:
            existing_record = None

        if existing_record is not None and existing_record.version == version:
            if (
                existing_record.digests.package != digests.package
                or existing_record.digests.verifier != digests.verifier
                or existing_record.digests.task_toml != digests.task_toml
                or existing_record.digests.instruction != digests.instruction
                or existing_record.digests.environment != digests.environment
            ):
                raise TaskDigestMismatchError(
                    f"task package bytes on disk have changed for {task_id!r} "
                    f"version {version!r} (existing package digest "
                    f"{existing_record.digests.package}, current {digests.package}); "
                    "bump --version to register a new version"
                )
            if existing_record.task_family != task_family:
                raise ValueError(
                    f"registered task family {existing_record.task_family!r} does not match "
                    f"requested family {task_family!r}"
                )
            if existing_record.external_import_lineage != external_import_lineage:
                raise TaskCertificationError(
                    "requested external import lineage does not match the existing registry record"
                )

            if existing_record.state == "candidate":
                try:
                    discovered_evidence = discover_control_evidence(
                        target_path,
                        repo_root,
                        jobs_roots=jobs_roots,
                        task_version=version,
                    )
                except TaskControlEvidenceError:
                    if state == "registered":
                        raise
                    return existing_record

                updates: dict[str, Any] = {
                    "control_evidence": discovered_evidence,
                    "state_reason": None,
                }
                if certification_path is not None:
                    updates["certification"] = certification
                if state == "registered":
                    if not actor or not actor.strip():
                        raise ValueError("registered task records require approved_by / --actor")
                    if certification.state != "bound":
                        raise TaskCertificationError(
                            "new registered promotion requires a valid --certification-packet"
                        )
                    updates.update(
                        {
                            "state": "registered",
                            "approved_by": actor,
                            "approved_at": approved_at or datetime.now(UTC),
                        }
                    )
                updated_record = TaskRegistryRecord.model_validate(
                    existing_record.model_copy(update=updates).model_dump()
                )
                if updated_record.state == "registered":
                    verify_control_evidence(repo_root, updated_record)
                    verify_certification_packet(repo_root, updated_record)
                record_file.write_text(
                    json.dumps(updated_record.model_dump(mode="json"), indent=2) + "\n"
                )
                return updated_record

            return existing_record

    # Discover control evidence
    control_evidence = discover_control_evidence(
        target_path,
        repo_root,
        jobs_roots=jobs_roots,
        task_version=version,
    )
    if state == "registered":
        if not actor or not actor.strip():
            raise ValueError("registered task records require approved_by / --actor")
        if certification.state != "bound":
            raise TaskCertificationError(
                "new registered promotion requires a valid --certification-packet"
            )
        approved_by = actor
        approved_timestamp = approved_at or datetime.now(UTC)
    else:
        approved_by = None
        approved_timestamp = None
    record = TaskRegistryRecord(
        schema_version=2,
        task_id=task_id,
        task_family=task_family,
        version=version,
        task_path=rel_task_path,
        digests=digests,
        source_uri=source_uri,
        source_ref=source_ref,
        external_import_lineage=external_import_lineage,
        license=license_str,
        provenance_zone=provenance_zone,
        is_synthetic=is_synthetic,
        limits=TaskLimits(
            timeout_seconds=timeout_seconds,
            max_memory_mb=max_memory_mb,
            max_cpus=max_cpus,
        ),
        control_evidence=control_evidence,
        certification=certification,
        state=state,
        allowed_uses=allowed_uses,
        contamination=contamination,
        human_minutes=human_minutes,
        approved_by=approved_by,
        approved_at=approved_timestamp,
    )
    verify_external_import_lineage(repo_root, record)

    reg_dir.mkdir(parents=True, exist_ok=True)
    record_file.write_text(json.dumps(record.model_dump(mode="json"), indent=2) + "\n")
    return record


def register_task(
    task_id: str,
    actor: str,
    repo_root: Path,
    *,
    registry_dir: Path | None = None,
    approved_at: datetime | None = None,
    certification_path: Path | str | None = None,
) -> TaskRegistryRecord:
    """Explicitly register a candidate task in the task registry with human approval."""
    if not actor or not actor.strip():
        raise ValueError("registered task records require approved_by / --actor")

    repo_root = repo_root.resolve()
    reg_dir = (registry_dir or (repo_root / "library/registry")).resolve()
    record_file = reg_dir / f"{task_id}.json"
    if not record_file.is_file():
        raise TaskNotRegisteredError(f"task {task_id!r} is not present in registry {reg_dir}")

    raw = json.loads(record_file.read_text())
    record = TaskRegistryRecord.model_validate(raw)
    if certification_path is not None:
        certification = certification_envelope_from_packet(
            repo_root,
            certification_path,
            task_id=record.task_id,
            task_version=record.version,
            task_path=record.task_path,
            package_digest=record.digests.package,
            external_import_lineage=record.external_import_lineage,
        )
        record = TaskRegistryRecord.model_validate(
            record.model_copy(update={"certification": certification}).model_dump()
        )
    if record.state != "registered" and record.certification.state != "bound":
        raise TaskCertificationError("new registration requires a valid --certification-packet")

    if record.state == "registered" and record.approved_by == actor:
        verify_certification_packet(repo_root, record)
        if certification_path is not None:
            record_file.write_text(json.dumps(record.model_dump(mode="json"), indent=2) + "\n")
        return record

    target_path = (repo_root / record.task_path).resolve()
    if not target_path.is_dir():
        raise TaskComponentMissingError(
            f"task package directory missing on disk: {record.task_path}"
        )

    verify_package_completeness(repo_root, record)

    current_digests = compute_task_digests(target_path)
    if (
        current_digests.package != record.digests.package
        or current_digests.verifier != record.digests.verifier
        or current_digests.task_toml != record.digests.task_toml
        or current_digests.instruction != record.digests.instruction
        or current_digests.environment != record.digests.environment
    ):
        raise TaskDigestMismatchError(
            f"task package bytes on disk have changed for {task_id!r} "
            f"(expected {record.digests.package}, got {current_digests.package})"
        )

    final_record = TaskRegistryRecord.model_validate(
        record.model_copy(
            update={
                "state": "registered",
                "approved_by": actor,
                "approved_at": approved_at or datetime.now(UTC),
                "state_reason": None,
            }
        ).model_dump()
    )
    verify_control_evidence(repo_root, final_record)
    verify_certification_packet(repo_root, final_record)

    reg_dir.mkdir(parents=True, exist_ok=True)
    record_file.write_text(json.dumps(final_record.model_dump(mode="json"), indent=2) + "\n")
    return final_record


def verify_control_evidence(root: Path, record: TaskRegistryRecord) -> None:
    """Verify committed trial evidence, lock identity, and registered package binding."""
    if record.state != "registered":
        return
    if record.control_evidence is None:
        raise TaskControlEvidenceError(
            f"registered task {record.task_id!r} has no control evidence"
        )

    task_dir = (root / record.task_path).resolve()
    current_harbor_digest = harbor_task_digest(task_dir)
    for agent_name, expected_reward, evidence_ref in (
        ("oracle", 1.0, record.control_evidence.oracle),
        ("nop", 0.0, record.control_evidence.nop),
    ):
        if (
            evidence_ref.task_id != record.task_id
            or evidence_ref.task_version != record.version
            or evidence_ref.task_digests != record.digests
            or evidence_ref.harbor_task_digest != current_harbor_digest
        ):
            raise TaskControlEvidenceError(
                f"{agent_name} control evidence identity or package digest mismatch "
                f"for {record.task_id!r}"
            )
        evidence_path = (root / evidence_ref.evidence_path).resolve()
        durable_root = (root / "research/evidence/runs").resolve()
        try:
            evidence_path.relative_to(durable_root)
        except ValueError as exc:
            raise TaskControlEvidenceError(
                f"{agent_name} control evidence is outside the durable owned root"
            ) from exc
        if not evidence_path.is_file():
            raise TaskControlEvidenceError(
                f"{agent_name} control evidence file missing on disk: {evidence_ref.evidence_path}"
            )
        if evidence_path.parent.parent.name != evidence_ref.job_name:
            raise TaskControlEvidenceError(
                f"{agent_name} control evidence job_name does not match its path"
            )
        lock_path = evidence_path.with_name("lock.json")
        if not lock_path.is_file():
            raise TaskControlEvidenceError(
                f"{agent_name} control evidence trial lock missing on disk"
            )
        current_evidence_digest = f"sha256:{hashlib.sha256(evidence_path.read_bytes()).hexdigest()}"
        current_lock_digest = f"sha256:{hashlib.sha256(lock_path.read_bytes()).hexdigest()}"
        if current_evidence_digest != evidence_ref.evidence_digest:
            raise TaskControlEvidenceError(
                f"{agent_name} control evidence digest mismatch for {record.task_id!r}"
            )
        if current_lock_digest != evidence_ref.lock_digest:
            raise TaskControlEvidenceError(
                f"{agent_name} control evidence lock digest mismatch for {record.task_id!r}"
            )
        try:
            data = json.loads(evidence_path.read_text())
            lock_data = json.loads(lock_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskControlEvidenceError(
                f"failed to parse {agent_name} trial evidence JSON: {exc}"
            ) from exc
        observed_at_raw = data.get("finished_at") or data.get("started_at")
        try:
            observed_at = datetime.fromisoformat(str(observed_at_raw).replace("Z", "+00:00"))
        except ValueError as exc:
            raise TaskControlEvidenceError(
                f"{agent_name} control evidence has no valid observation timestamp"
            ) from exc
        if observed_at != evidence_ref.observed_at:
            raise TaskControlEvidenceError(
                f"{agent_name} control evidence observed_at does not match the trial"
            )
        _verify_control_result(
            data,
            lock_data,
            expected_agent=agent_name,
            expected_reward=expected_reward,
            record=record,
            evidence_ref=evidence_ref,
        )


def verify_package_completeness(root: Path, record: TaskRegistryRecord) -> None:
    """Verify that a task package contains runnable task.toml, instruction, environment,
    and separate verifier.
    """
    verify_external_import_lineage(root, record)
    target_path = (root / record.task_path).resolve()
    if not target_path.is_dir():
        primary = shared_checkout_root(root)
        if primary != root and (primary / record.task_path).is_dir():
            target_path = (primary / record.task_path).resolve()
        else:
            raise TaskComponentMissingError(
                f"task package directory missing on disk: {record.task_path}"
            )

    if not (target_path / "task.toml").is_file():
        raise TaskComponentMissingError(
            f"task.toml missing in package directory: {record.task_path}"
        )

    has_instruction = (target_path / "instruction.md").is_file() or (
        target_path / "instructions.md"
    ).is_file()
    if not has_instruction:
        raise TaskComponentMissingError(
            f"instruction.md missing in package directory: {record.task_path}"
        )

    has_env = (target_path / "environment").exists() or (target_path / "Dockerfile").is_file()
    if not has_env:
        raise TaskComponentMissingError(
            f"environment/Dockerfile missing in package directory: {record.task_path}"
        )

    has_verifier = (target_path / "tests").exists() or (target_path / "verifier").exists()
    if not has_verifier:
        raise TaskComponentMissingError(
            "separate verifier (tests/ or verifier/) missing in package "
            f"directory: {record.task_path}"
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
        registry = cls.from_dir(repo_root / "library/registry")
        for record in registry.records.values():
            verify_external_import_lineage(repo_root, record)
        return registry

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

        if "measurement" not in record.allowed_uses:
            raise TaskUsageNotAllowedError(
                f"task {task_id!r} allows uses {record.allowed_uses!r}; "
                "measurement is not permitted"
            )

        if spec.task_path is None or not spec.task_path.strip():
            spec.task_path = record.task_path
        elif spec.task_path != record.task_path:
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
        verify_package_completeness(repo_root, record)

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

        if spec.verifier_digest is not None and spec.verifier_digest != record.digests.verifier:
            raise TaskDigestMismatchError(
                f"spec verifier_digest {spec.verifier_digest!r} does not match "
                f"registered verifier {record.digests.verifier!r}"
            )

        verify_control_evidence(repo_root, record)
        verify_certification_packet(repo_root, record)

        return record

    def promote(
        self,
        task_path: Path | str,
        repo_root: Path | None = None,
        **kwargs: Any,
    ) -> TaskRegistryRecord:
        root = repo_root or self.root.parent.parent
        record = promote_task(task_path, root, registry_dir=self.root, **kwargs)
        self.records[record.task_id] = record
        return record

    def register(
        self,
        task_id: str,
        actor: str,
        repo_root: Path | None = None,
        **kwargs: Any,
    ) -> TaskRegistryRecord:
        root = repo_root or self.root.parent.parent
        record = register_task(task_id, actor, root, registry_dir=self.root, **kwargs)
        self.records[record.task_id] = record
        return record

    def save_record(self, record: TaskRegistryRecord) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        dest = self.root / f"{record.task_id}.json"
        dest.write_text(json.dumps(record.model_dump(mode="json"), indent=2) + "\n")
        self.records[record.task_id] = record
        return dest


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
        return not any(f.severity == "error" for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_records": self.total_records,
            "registered_count": self.registered_count,
            "candidate_count": self.candidate_count,
            "retired_count": self.retired_count,
            "passed": self.passed,
            "findings": [
                {
                    "severity": f.severity,
                    "category": f.category,
                    "target": f.target,
                    "message": f.message,
                }
                for f in self.findings
            ],
        }


def audit_registry(root: Path) -> RegistryAuditReport:
    """Audit explicit task registry records, package digests, control evidence, and queue claims."""
    root = root.resolve()
    registry_dir = root / "library/registry"
    findings: list[AuditFinding] = []

    records: dict[str, TaskRegistryRecord] = {}
    if registry_dir.is_dir():
        for record_file in sorted(registry_dir.glob("*.json")):
            try:
                raw = json.loads(record_file.read_text())
                record = TaskRegistryRecord.model_validate(raw)
                if record.task_id in records:
                    findings.append(
                        AuditFinding(
                            severity="error",
                            category="duplicate_task_id",
                            target=record.task_id,
                            message=f"duplicate task id in registry: {record.task_id}",
                        )
                    )
                else:
                    records[record.task_id] = record
            except Exception as exc:
                findings.append(
                    AuditFinding(
                        severity="error",
                        category="malformed_registry_record",
                        target=record_file.name,
                        message=f"invalid registry record JSON or schema: {exc}",
                    )
                )

    reg = TaskRegistry(root=registry_dir, records=records)

    registered_records = reg.list_records("registered")
    candidate_records = reg.list_records("candidate")
    retired_records = reg.list_records("retired")

    seen_paths: dict[str, str] = {}

    # 1. Audit explicit registry records
    for record in reg.list_records():
        # Duplicate path check
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

        # Existence and completeness of package directory
        target_path = root / record.task_path
        if not target_path.is_dir():
            findings.append(
                AuditFinding(
                    severity="error",
                    category="missing_task_directory",
                    target=record.task_id,
                    message=f"task directory does not exist on disk: {record.task_path}",
                )
            )
            continue

        try:
            verify_package_completeness(root, record)
        except TaskComponentMissingError as exc:
            findings.append(
                AuditFinding(
                    severity="error",
                    category="missing_package_component",
                    target=record.task_id,
                    message=str(exc),
                )
            )

        # Digest verification on disk
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
            try:
                verify_control_evidence(root, record)
            except TaskControlEvidenceError as exc:
                findings.append(
                    AuditFinding(
                        severity="error",
                        category="invalid_control_evidence",
                        target=record.task_id,
                        message=str(exc),
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

        if record.certification.state == "legacy_missing":
            findings.append(
                AuditFinding(
                    severity="warning",
                    category="legacy_missing_certification",
                    target=record.task_id,
                    message=record.certification.reason,
                )
            )
            try:
                verify_external_import_lineage(root, record)
            except TaskCertificationError as exc:
                findings.append(
                    AuditFinding(
                        severity="error",
                        category="invalid_external_import_lineage",
                        target=record.task_id,
                        message=str(exc),
                    )
                )
        else:
            try:
                verify_certification_packet(root, record)
            except TaskCertificationError as exc:
                findings.append(
                    AuditFinding(
                        severity="error",
                        category="invalid_certification_packet",
                        target=record.task_id,
                        message=str(exc),
                    )
                )

        # External record checks
        if record.provenance_zone == "01-external":
            if not record.license:
                findings.append(
                    AuditFinding(
                        severity="error",
                        category="missing_license",
                        target=record.task_id,
                        message="external record requires declared license",
                    )
                )
            if record.source_ref is not None and any(
                char in record.source_ref for char in ("latest", "head", "main", "master")
            ):
                findings.append(
                    AuditFinding(
                        severity="warning",
                        category="floating_ref",
                        target=record.task_id,
                        message=f"external source_ref appears unpinned: {record.source_ref!r}",
                    )
                )

    # 2. Audit Queue state for false registered/* claims and malformed specs
    queue_root = root / "queue"
    if queue_root.is_dir():
        for state in ("proposed", "pending", "approved", "waiting", "running"):
            state_dir = queue_root / state
            if not state_dir.is_dir():
                continue
            for spec_file in sorted(state_dir.glob("*.json")):
                try:
                    raw_spec = json.loads(spec_file.read_text())
                except Exception as exc:
                    findings.append(
                        AuditFinding(
                            severity="error",
                            category="malformed_queue_spec",
                            target=f"{state}/{spec_file.name}",
                            message=f"failed to parse JSON in queue spec: {exc}",
                        )
                    )
                    continue

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
                        spec_version = raw_spec.get("task_version")
                        if spec_version and spec_version != record.version:
                            findings.append(
                                AuditFinding(
                                    severity="error",
                                    category="task_version_mismatch",
                                    target=f"{state}/{spec_file.name}",
                                    message=(
                                        f"spec task_version {spec_version!r} does not "
                                        f"match registered version {record.version!r}"
                                    ),
                                )
                            )
                        spec_verifier = raw_spec.get("verifier_digest")
                        if spec_verifier and spec_verifier != record.digests.verifier:
                            findings.append(
                                AuditFinding(
                                    severity="error",
                                    category="verifier_digest_mismatch",
                                    target=f"{state}/{spec_file.name}",
                                    message=(
                                        f"spec verifier_digest {spec_verifier!r} does not "
                                        f"match registered verifier {record.digests.verifier!r}"
                                    ),
                                )
                            )

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

    # 4. The committed inventory is a deterministic projection of repository truth.
    inventory_path = root / "research/registration/inventory.json"
    registry_is_well_formed = not any(
        finding.category == "malformed_registry_record" for finding in findings
    )
    if registry_is_well_formed:
        try:
            expected_inventory = inventory_tasks(root).to_dict()
        except TaskInventoryPolicyError as exc:
            findings.append(
                AuditFinding(
                    severity="error",
                    category="registration_inventory_policy_invalid",
                    target="policy/canary-suite.yaml",
                    message=str(exc),
                )
            )
        else:
            try:
                committed_inventory = json.loads(inventory_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                findings.append(
                    AuditFinding(
                        severity="error",
                        category="registration_inventory_missing_or_invalid",
                        target="research/registration/inventory.json",
                        message=f"registration inventory cannot be read: {exc}",
                    )
                )
            else:
                if committed_inventory != expected_inventory:
                    findings.append(
                        AuditFinding(
                            severity="error",
                            category="registration_inventory_drift",
                            target="research/registration/inventory.json",
                            message=(
                                "committed registration inventory differs from the "
                                "deterministic repository inventory"
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "path": self.path,
            "category": self.category,
            "has_task_toml": self.has_task_toml,
            "has_environment": self.has_environment,
            "has_verifier": self.has_verifier,
            "is_canary": self.is_canary,
            "registration_state": self.registration_state,
        }


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
            "items": [item.to_dict() for item in self.items],
        }


def inventory_tasks(root: Path) -> TaskInventory:
    """Mechanically inventory all task surfaces across library/ and policy/."""
    root = root.resolve()
    reg = TaskRegistry.from_repo(root)
    items: list[TaskInventoryItem] = []

    # Canary membership is policy truth; malformed or missing policy cannot mean zero.
    canary_policy = root / "policy/canary-suite.yaml"
    if not canary_policy.is_file():
        raise TaskInventoryPolicyError("canary inventory requires policy/canary-suite.yaml")
    import yaml

    try:
        raw_suite = yaml.safe_load(canary_policy.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise TaskInventoryPolicyError(f"invalid canary policy: {exc}") from exc
    members = raw_suite.get("members") if isinstance(raw_suite, dict) else None
    if not isinstance(members, list):
        raise TaskInventoryPolicyError("canary policy requires a members list")
    canary_paths: set[str] = set()
    for index, member in enumerate(members):
        task_path = member.get("task_path") if isinstance(member, dict) else None
        if not isinstance(task_path, str) or not task_path:
            raise TaskInventoryPolicyError(f"canary policy member {index} requires task_path")
        canary_paths.add(task_path)

    # 1. Scan library/ for all task.toml packages
    library_dir = root / "library"
    if library_dir.is_dir():
        for task_toml in sorted(library_dir.rglob("task.toml")):
            task_dir = task_toml.parent
            rel_path = task_dir.relative_to(root).as_posix()
            task_id = task_dir.name

            if "task-template" in rel_path:
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
                reg_record.state if reg_record and reg_record.task_path == rel_path else None
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
                    reg_record.state if reg_record and reg_record.task_path == rel_path else None
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
