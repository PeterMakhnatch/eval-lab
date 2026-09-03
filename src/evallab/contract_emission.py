"""Registry-bound benchmark contract emission for bundle promotion (Gate Zero).

Every newly promoted evidence bundle must carry a per-trial
``benchmark_contract.json`` that the strict ingestion path
(``evallab.interpretation.benchmark_events.parse_benchmark_contract`` /
``load_trial_bundle``) can consume. This module mints that contract and is the
only new emission path; existing promotion behavior is otherwise untouched.

Authority model — fail-closed by construction:

- The ONLY authority for a contract is a ``registered`` ``TaskRegistryRecord``
  whose on-disk task package digests still equal the registered digests — the
  same predicate ``trial_admissibility._registry_binding`` enforces downstream,
  so a contract emitted here is exactly the binding admissibility later
  re-checks.
- Task identity comes from the trial's own ``result.json`` (``task_name``,
  whose promoted digest PROMOTION.json pins). A Harbor namespace prefix
  (``local-lab/<task_id>``) is resolved against the registry, never trusted:
  only registry membership turns a name into authority. When the evidence also
  carries ``task_id.path``, it must resolve to the registered ``task_path`` or
  promotion refuses.
- Every digest read or written is canonical ``sha256:<64 lowercase hex>``.
- Nothing is derived from path shapes, host platform, config/lock files, or
  auxiliary JSON: an extra ``*.json`` file in a trial cannot override identity,
  family, or class. No runtime/admissibility authority field is ever minted —
  those are exactly the fields ``parse_benchmark_contract`` refuses in
  ``cell_factors``.
- Every missing, contradictory, or unknown state is a typed
  :class:`ContractEmissionRefusal` raised BEFORE the promotion writes a single
  byte, so a refused promotion cannot leave a partial bundle, and an existing
  contract in trial evidence is never replaced (additive-only).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from evallab.registry import (
    TaskRegistry,
    TaskRegistryRecord,
    compute_task_digests,
    task_registry_record_digest,
)

#: Canonical digest shape. Every digest this module reads or writes must match.
CANONICAL_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

#: The emitted file name — first candidate of the downstream readers
#: (``trial_admissibility._CONTRACT_CANDIDATES`` and the interpretation
#: loader's nested candidates), so admissibility digests the binding.
CONTRACT_FILENAME = "benchmark_contract.json"

#: Mirror of ``trial_admissibility``'s contract candidates. A trial already
#: carrying any of these carries contract authority; emission never replaces it.
CONTRACT_CANDIDATES = (
    "benchmark_contract.json",
    "benchmark-contract.json",
    "contract.json",
    "artifacts/app/output/benchmark_contract.json",
    "artifacts/app/output/benchmark-contract.json",
    "artifacts/app/output/contract.json",
)

#: Descriptive, bundle-relative pointers written into ``artifact_paths``.
#: These are conveniences for readers, never authority: the binding lives in
#: the registry digests, not in where files sit.
_EVIDENCE_ARTIFACTS = (
    ("result", "result.json"),
    ("trajectory", "agent/trajectory.json"),
    ("verifier_result", "verifier/result.json"),
    ("verifier_reward", "verifier/reward.txt"),
    ("benchmark_events", "benchmark-events.jsonl"),
    ("final_state", "final-state.json"),
)


class ContractEmissionRefusal(Exception):
    """Typed, fail-closed refusal to emit a benchmark contract.

    Carries a machine-readable ``reason`` so callers (and the promotion
    manifest tooling) can audit exactly which binding failed.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"benchmark_contract_emission_refused:{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ContractPlan:
    """One pre-validated, registry-bound contract ready to be written."""

    trial_name: str
    task_id: str
    body: bytes
    registry_record_digest: str
    certified_runtime_package_digest: str
    certified_environment_digest: str
    trial_run_id: str | None
    identity_source: str
    identity_source_bytes: int
    identity_source_sha256: str


def atomic_write_bytes(path: Path, body: bytes) -> None:
    """Publish *body* once and atomically; never replace existing authority."""
    if path.exists() or path.is_symlink():
        raise ContractEmissionRefusal(
            "contract_publish_overwrite",
            f"refusing to replace existing contract target {path}",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        # Exclusive creation refuses a pre-existing/symlinked temp target rather
        # than following it. ``os.replace`` then makes the completed bytes
        # visible atomically to readers of the final path.
        with temporary.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except FileExistsError as exc:
        raise ContractEmissionRefusal(
            "contract_publish_conflict",
            f"atomic publish temporary already exists for {path}",
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)




def require_canonical_digest(value: object, label: str) -> str:
    """Return *value* only if it is a canonical ``sha256:<64 hex>`` digest."""
    if not isinstance(value, str) or not CANONICAL_DIGEST.fullmatch(value):
        raise ContractEmissionRefusal(
            "noncanonical_digest",
            f"{label} must match sha256:<64 lowercase hex>, got {value!r}",
        )
    return value


def resolve_registry_binding(
    trial_evidence: Mapping[str, Any],
    registry: TaskRegistry,
) -> TaskRegistryRecord:
    """Resolve the trial's ``task_name`` to its registered record.

    An exact registry hit wins; otherwise a Harbor namespace suffix
    (``local-lab/<task_id>``) is resolved against the registry. The registry,
    not the name, is the authority: anything that does not land on a
    ``registered`` record is a typed refusal.
    """
    raw = trial_evidence.get("task_name")
    if not isinstance(raw, str) or not raw.strip():
        raise ContractEmissionRefusal(
            "missing_task_identity",
            "trial result carries no usable task_name; refusing to mint a contract "
            "without runtime task identity",
        )
    task_name = raw.strip()
    record = registry.get(task_name)
    if record is None and "/" in task_name:
        record = registry.records.get(task_name.rsplit("/", 1)[1])
    if record is None:
        raise ContractEmissionRefusal(
            "task_not_in_registry",
            f"no task registry record for task_name {task_name!r}",
        )
    if record.state != "registered":
        raise ContractEmissionRefusal(
            "registry_state_not_registered",
            f"registry record {record.task_id!r} is {record.state!r}; "
            "only registered tasks bind trial evidence",
        )
    return record


def verify_registry_binding(
    record: TaskRegistryRecord,
    trial_evidence: Mapping[str, Any],
    repo_root: Path,
) -> None:
    """Prove the record actually binds: canonical digests, live package, evidence agreement.

    This is the same predicate downstream admissibility enforces
    (``current digests == registered digests``), applied before emission.
    """
    for component in ("task_toml", "instruction", "environment", "verifier", "package"):
        require_canonical_digest(
            getattr(record.digests, component), f"registry digests.{component}"
        )

    root = repo_root.resolve()
    task_path = root / record.task_path
    try:
        task_path = task_path.resolve()
    except OSError as exc:  # pragma: no cover - pathological filesystem
        raise ContractEmissionRefusal(
            "registry_package_unreadable", f"cannot resolve {record.task_path!r}: {exc}"
        ) from exc
    if task_path != root and root not in task_path.parents:
        raise ContractEmissionRefusal(
            "registry_task_path_escape",
            f"registered task_path {record.task_path!r} escapes the repository",
        )
    try:
        current = compute_task_digests(task_path)
    except (OSError, ValueError) as exc:
        raise ContractEmissionRefusal(
            "registry_package_unreadable",
            f"registered task package {record.task_path!r} cannot be digested: {exc}",
        ) from exc
    if current != record.digests:
        raise ContractEmissionRefusal(
            "registry_package_drift",
            f"on-disk task package {record.task_path!r} no longer matches the "
            "registered digests; refusing to bind drifted authority",
        )

    raw_identity = trial_evidence.get("task_id")
    evidence_path = (
        raw_identity.get("path") if isinstance(raw_identity, Mapping) else raw_identity
    )
    if isinstance(evidence_path, str) and evidence_path.strip():
        candidate = PurePosixPath(evidence_path.strip())
        if candidate.is_absolute() or (len(candidate.parts) > 1 and "\\" in evidence_path):
            # Absolute host path (the Harbor local-task shape): authoritative
            # comparison is repo-relative, so reduce it. A path outside the
            # repository is an unknown state, not a hint to be ignored.
            filesystem_path = Path(evidence_path.strip())
            try:
                resolved = filesystem_path.resolve()
            except OSError as exc:
                raise ContractEmissionRefusal(
                    "task_path_mismatch",
                    f"trial task path {evidence_path!r} cannot be resolved: {exc}",
                ) from exc
            try:
                relative = resolved.relative_to(root)
            except ValueError as exc:
                raise ContractEmissionRefusal(
                    "task_path_mismatch",
                    f"trial task path {evidence_path!r} lies outside the repository",
                ) from exc
            candidate = PurePosixPath(relative.as_posix())
        if ".." in candidate.parts:
            raise ContractEmissionRefusal(
                "task_path_mismatch",
                f"trial task path {evidence_path!r} contains a directory escape",
            )
        if candidate.as_posix() != record.task_path:
            raise ContractEmissionRefusal(
                "task_path_mismatch",
                f"trial ran {candidate.as_posix()!r} but the registry binds "
                f"{record.task_path!r}",
            )


def emit_benchmark_contract(
    trial_evidence: Mapping[str, Any],
    registry_record: TaskRegistryRecord,
    *,
    artifact_paths: Mapping[str, str] | None = None,
) -> bytes:
    """Mint the ``benchmark_contract.json`` bytes for one registry-bound trial.

    The content matches ``parse_benchmark_contract``'s schema exactly and is
    proven to: the emitted dict is round-tripped through the real parser and
    every binding field is re-checked before a single byte is returned.
    """
    family = registry_record.task_family
    if not family:
        raise ContractEmissionRefusal(
            "missing_benchmark_family",
            f"registry record {registry_record.task_id!r} carries no task_family",
        )
    verifier_truth_digest = require_canonical_digest(
        registry_record.digests.verifier, "registry digests.verifier"
    )

    contract: dict[str, Any] = {
        "benchmark_family": family,
        "version": registry_record.version,
        "task_id": registry_record.task_id,
        "task_name": registry_record.task_id,
        "verifier_truth_digest": verifier_truth_digest,
    }
    if artifact_paths:
        safe_paths: dict[str, str] = {}
        for name, value in artifact_paths.items():
            path = PurePosixPath(value)
            if (
                not value
                or path.is_absolute()
                or "\\" in value
                or ".." in path.parts
                or value.startswith("/")
            ):
                raise ContractEmissionRefusal(
                    "unsafe_artifact_path",
                    f"artifact path for {name!r} must be a clean bundle-relative "
                    f"POSIX path, got {value!r}",
                )
            safe_paths[str(name)] = value
        contract["artifact_paths"] = dict(sorted(safe_paths.items()))

    # Prove enforcement, not presence: the real parser must accept the content
    # and land on exactly the binding this module claims.
    from evallab.interpretation.benchmark_events import (  # noqa: PLC0415 - avoids import cycle at module load
        BenchmarkIngestionError,
        parse_benchmark_contract,
    )

    try:
        parsed = parse_benchmark_contract(contract)
    except BenchmarkIngestionError as exc:
        raise ContractEmissionRefusal("contract_schema_violation", str(exc)) from exc
    if (
        parsed.family != family
        or parsed.task_id != registry_record.task_id
        or not parsed.task_id_explicit
        or parsed.verifier_truth_digest != verifier_truth_digest
    ):
        raise ContractEmissionRefusal(
            "contract_binding_mismatch",
            "emitted contract does not round-trip onto the registry binding",
        )

    return json.dumps(contract, indent=2, ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    ) + b"\n"




def _read_trial_result(result_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractEmissionRefusal(
            "malformed_trial_result", f"{result_path} is unreadable: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ContractEmissionRefusal(
            "malformed_trial_result", f"{result_path} is not a JSON object"
        )
    return payload


def _existing_contract(trial_dir: Path) -> str | None:
    for relative in CONTRACT_CANDIDATES:
        candidate = trial_dir / relative
        if candidate.is_symlink():
            raise ContractEmissionRefusal(
                "symlinked_contract_authority",
                f"{trial_dir.name}/{relative} is a symlink; promotion never "
                "dereferences contract authority",
            )
        if candidate.is_file():
            return relative
    return None


def _evidence_artifact_paths(trial_dir: Path) -> dict[str, str]:
    present = {
        name: relative
        for name, relative in _EVIDENCE_ARTIFACTS
        if (trial_dir / relative).is_file()
    }
    return dict(sorted(present.items()))


def plan_contract_emission(job_dir: Path, repo_root: Path) -> list[ContractPlan]:
    """Resolve, bind, and emit a contract for every trial under *job_dir*.

    Runs before the promotion walk touches the destination, so any refusal
    leaves the destination untouched (no partial bundles). Returns plans
    sorted by trial name; the caller writes them additively.
    """
    registry = TaskRegistry.from_repo(repo_root)
    plans: list[ContractPlan] = []
    for trial_dir in sorted(p for p in job_dir.iterdir() if p.is_dir() and not p.is_symlink()):
        result_path = trial_dir / "result.json"
        if result_path.is_symlink():
            raise ContractEmissionRefusal(
                "symlinked_trial_result",
                f"{trial_dir.name}/result.json is a symlink; promotion never "
                "dereferences host content into a trial identity",
            )
        if not result_path.is_file():
            # Not a bindable trial: no runtime identity exists to bind. This
            # preserves the pre-existing promotion shape (e.g. session-tree-only
            # job scaffolding) unchanged — it simply carries no contract.
            continue
        existing = _existing_contract(trial_dir)
        if existing is not None:
            raise ContractEmissionRefusal(
                "existing_contract_present",
                f"{trial_dir.name} already carries contract authority at "
                f"{existing!r}; promotion is additive-only and never replaces "
                "an existing contract",
            )

        trial_evidence = _read_trial_result(result_path)
        record = resolve_registry_binding(trial_evidence, registry)
        verify_registry_binding(record, trial_evidence, repo_root)
        body = emit_benchmark_contract(
            trial_evidence,
            record,
            artifact_paths=_evidence_artifact_paths(trial_dir),
        )
        identity_bytes = result_path.read_bytes()
        identity_digest = f"sha256:{hashlib.sha256(identity_bytes).hexdigest()}"
        plans.append(
            ContractPlan(
                trial_name=trial_dir.name,
                task_id=record.task_id,
                body=body,
                registry_record_digest=require_canonical_digest(
                    task_registry_record_digest(record), "registry record digest"
                ),
                certified_runtime_package_digest=require_canonical_digest(
                    record.digests.package, "registry digests.package"
                ),
                certified_environment_digest=require_canonical_digest(
                    record.digests.environment, "registry digests.environment"
                ),
                trial_run_id=(
                    trial_evidence["id"]
                    if isinstance(trial_evidence.get("id"), str)
                    else None
                ),
                identity_source=f"{trial_dir.name}/result.json",
                identity_source_bytes=len(identity_bytes),
                identity_source_sha256=require_canonical_digest(
                    identity_digest, "identity source digest"
                ),
            )
        )
    return plans
