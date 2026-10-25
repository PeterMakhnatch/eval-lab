"""Admit public DEV corpus records without inventing executable work.

Every native DEV record gets an immutable ``queue.corpus_admitted`` event,
including TB4 task records with no finding. Only an existing digest-bound actor
trajectory becomes ``misalignment.replay_task``. The source indices do not
supply owned candidates or same-submission differential replay inputs, so their
package/pair metadata alone never creates a Task Quality or Seeker job.

Preflight calls the existing candidate runner in read-only ``plan=True`` mode
with an empty artifact mapping. This is a lifecycle/asset check, not a candidate,
Docker probe, executed control, model generation, or proof of measurability.
Static admission therefore remains ``unknown`` until real execution. Raw index
rows (including evaluator labels) are hashed for custody but never exported.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from evallab.production.corpus_queue import (
    DATASETS,
    ContinuousWorkQueue,
    CorpusItem,
    IdempotencyConflict,
)
from evallab.results_home import results_root

_GYM_PATH = Path("research/experiments/seeker-gym-v0/gym.json")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _portable(path: Path, root: Path) -> str:
    resolved = path.resolve()
    return resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else str(resolved)


def _path(value: Any, root: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _package_locators(item: CorpusItem, root: Path) -> list[Path]:
    metadata = item.metadata
    materialization = metadata.get("materialization") or {}
    package = metadata.get("package") or {}
    candidates: list[Path] = []
    for raw in (
        materialization.get("resolved_locator"),
        materialization.get("locator"),
        metadata.get("task_package"),
        package.get("path"),
    ):
        path = _path(raw, root)
        if path is not None and path not in candidates:
            candidates.append(path)
    package_root = _path(package.get("root_hint"), root)
    if package_root is not None and isinstance(package.get("name"), str):
        name = Path(package["name"])
        if not name.is_absolute() and len(name.parts) == 1 and name.name not in (".", ".."):
            candidates.append(package_root / name)
    return candidates


def resolve_task_package(item: CorpusItem, *, repo_root: Path) -> dict[str, Any] | None:
    """Resolve only an existing, digest-matched registry-owned DEV package.

    A payload path, gym pair, or latest-version alias is not authority. The
    returned path is repository-relative or a canonical external absolute path;
    its digest is the registry's full ``task_directory_digest``, not a Harbor
    subset digest. Callers must still detect mutation before/during execution.
    """
    if item.split != "dev" or item.task_digest is None:
        return None
    from evallab.registry import task_directory_digest

    root = Path(repo_root).resolve()
    for path in _package_locators(item, root):
        if not path.is_dir() or not (path / "task.toml").is_file():
            continue
        try:
            observed = task_directory_digest(path)
        except (OSError, ValueError):
            continue
        if observed == item.task_digest:
            return {
                "task_package": _portable(path, root),
                "task_package_digest": observed,
            }
    return None


def _trace_preflight(item: CorpusItem, root: Path) -> dict[str, Any]:
    if item.split != "dev":
        return {"available": False, "reason": "TEST/heldout actor data is sealed"}
    metadata = item.metadata
    if metadata.get("actions_only_ref") is not None:
        reference = metadata["actions_only_ref"]
        expected = metadata.get("actions_only_sha256")
        view = "actions_only"
    else:
        reference = metadata.get("trial_ref")
        expected = metadata.get("trajectory_sha256")
        view = "full"
    if not isinstance(reference, dict):
        return {"available": False, "reason": "source index supplies no public actor trajectory"}
    if reference.get("kind") not in ("published", "repo"):
        return {"available": False, "reason": "public trajectory reference kind is unsupported"}
    raw = reference.get("path")
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute() or ".." in Path(raw).parts:
        return {"available": False, "reason": "public trajectory reference must stay within its source root"}
    base = results_root().expanduser().resolve() if reference["kind"] == "published" else root
    trial = (base / raw).resolve()
    trajectory = trial / "agent/trajectory.json"
    if not trajectory.resolve().is_relative_to(base):
        return {"available": False, "reason": "public trajectory resolves outside its source root"}
    details = {"path": _portable(trajectory, root), "view": view}
    bare_digest = expected.removeprefix("sha256:") if isinstance(expected, str) else ""
    if len(bare_digest) != 64 or any(c not in "0123456789abcdef" for c in bare_digest):
        return {**details, "available": False, "reason": "source index supplies no valid trajectory SHA-256"}
    if not trajectory.is_file():
        return {**details, "available": False, "reason": "indexed public actor trajectory is unavailable locally"}
    try:
        raw = trajectory.read_bytes()
        observed = hashlib.sha256(raw).hexdigest()
    except OSError as exc:
        return {**details, "available": False, "reason": f"public actor trajectory is unreadable: {type(exc).__name__}"}
    if observed != bare_digest:
        return {
            **details,
            "available": False,
            "reason": "public actor trajectory SHA-256 differs from its source record",
            "expected_sha256": bare_digest,
            "observed_sha256": observed,
        }
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {**details, "available": False, "reason": "digest-bound public trajectory is not valid JSON"}
    steps = document.get("steps") if isinstance(document, dict) else None
    if not isinstance(steps, list) or not steps or not all(isinstance(step, dict) for step in steps):
        return {**details, "available": False, "reason": "digest-bound public trajectory has no replayable ATIF step objects"}
    from evallab.cheatbench_fidelity import blind_atif, find_forbidden_json

    projected = blind_atif(document, item_id=item.item_id)
    if find_forbidden_json(projected):
        return {**details, "available": False, "reason": "public actor projection contains evaluator-only fields or annotations"}
    actor_steps = len(projected["steps"])
    if not actor_steps:
        return {**details, "available": False, "reason": "public trajectory contains no actor steps after evaluator blinding"}
    descriptor = {
        "trajectory_path": details["path"],
        "trajectory_sha256": bare_digest,
        "view": view,
    }
    return {
        **details,
        "available": True,
        "reason": "existing actor trajectory matches the public index SHA-256",
        "actor_steps": actor_steps,
        "descriptor": descriptor,
    }


def resolve_public_trace(item: CorpusItem, *, repo_root: Path) -> dict[str, Any] | None:
    """Resolve only registry-owned actor references, never a queued path override.

    Published paths are canonical absolute external paths; ``repo`` references
    remain repository-relative. Missing, sealed, unsafe, unreadable, or changed
    bytes return ``None``. A corrupt actions-only view does not fall back to full
    prose. Consumers must recheck the digest on the bytes they actually process.
    """
    result = _trace_preflight(item, Path(repo_root).resolve())
    return result.get("descriptor")


def _index_revision(root: Path, path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(root).as_posix()
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", relative],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and len(revision) == 40 else None


def _pair_index(root: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any] | None]:
    path = root / _GYM_PATH
    if not path.is_file():
        return {}, None
    raw = path.read_bytes()
    document = json.loads(raw)
    pairs: dict[str, list[dict[str, Any]]] = {}
    for pair in document.get("pairs", []):
        if isinstance(pair, dict) and isinstance(pair.get("task"), str):
            pairs.setdefault(pair["task"], []).append(pair)
    return pairs, {"path": _GYM_PATH.as_posix(), "sha256": _digest(raw)}


class _Preflight:
    def __init__(self, root: Path, *, split_digest: str | None) -> None:
        self.root = root
        self.pairs, self.pair_source = _pair_index(root)
        self.packages: dict[Path, dict[str, Any]] = {}
        self.plans: dict[tuple[Path, Path | None, str], dict[str, Any]] = {}
        self.revisions: dict[Path, str | None] = {}
        split_path = root / "research/answer-keys/splits/assignments.csv"
        self.split_authority = {
            "path": "research/answer-keys/splits/assignments.csv",
            "sha256": split_digest,
            "source_revision": _index_revision(root, split_path),
            "policy": "canonical TEST/heldout seals only tighten source splits",
        }

    def source(self, item: CorpusItem) -> dict[str, Any]:
        if item.source_path not in self.revisions:
            self.revisions[item.source_path] = _index_revision(self.root, item.source_path)
        return {
            "path": _portable(item.source_path, self.root),
            "record_id": item.item_id,
            "sha256": item.source_record_sha256,
            "digest_basis": "canonical original source record JSON (no record content exported)",
            "source_revision": self.revisions[item.source_path],
            "split_authority": self.split_authority,
        }

    def package(self, path: Path) -> dict[str, Any]:
        if path not in self.packages:
            result: dict[str, Any] = {"path": _portable(path, self.root), "available": False}
            if not path.is_dir() or not (path / "task.toml").is_file():
                result["reason"] = "indexed task directory/task.toml is unavailable locally"
            else:
                from evallab.registry import task_directory_digest

                try:
                    result.update(
                        available=True,
                        observed_digest=task_directory_digest(path),
                        reason="existing task package; exact bytes hashed without executing code",
                    )
                except (OSError, ValueError) as exc:
                    result["reason"] = f"task package cannot be hashed: {type(exc).__name__}"
            self.packages[path] = result
        return dict(self.packages[path])

    def plan(self, original: Path, fixed: Path | None = None, kind: str = "package") -> dict[str, Any]:
        key = original, fixed, kind
        if key not in self.plans:
            from evallab.candidate_runner import run_candidate

            result = run_candidate(
                fixed if fixed is not None and kind != "pm_port" else original,
                files={},
                grader="port" if kind == "pm_port" else "upstream",
                port_task=fixed if kind == "pm_port" else None,
                backend="docker",
                plan=True,
            )
            refusals = result.get("refusals") or []
            blocked = result.get("runnable") is not True
            adapter = result.get("grader_provenance") or {}
            native = result.get("native_plan") or {}
            reason = (
                "; ".join(str(reason) for reason in refusals)
                or result.get("error")
                or (adapter.get("support") or {}).get("local")
                or native.get("docker_admission")
                or result.get("admission")
                or "no runtime-qualified Docker execution evidence is supplied"
            )
            reason = str(reason).replace(str(self.root) + "/", "")
            self.plans[key] = {
                "state": "blocked" if blocked else "unknown",
                "reason": reason,
                "basis": result.get("schema", "evallab.candidate.plan/v1"),
                "static_admission": not blocked,
                "runtime_qualified": False,
                "executed": False,
                "execution_proof": False,
                "grader": "port" if kind == "pm_port" else "upstream",
            }
        return dict(self.plans[key])

    def inspect(self, item: CorpusItem) -> dict[str, Any]:
        metadata = item.metadata
        materialization = metadata.get("materialization") or {}
        candidates = _package_locators(item, self.root)
        pairs = self.pairs.get((item.task_id or "").rsplit("/", 1)[-1], [])
        # A pair is only a locator candidate. Its train/eval labels never override
        # the registry split, and latest repaired bytes cannot replace a frozen
        # historical package unless their actual digest agrees.
        if item.task_digest:
            for pair in pairs:
                path = _path(pair.get("original_task"), self.root)
                if path is not None and path not in candidates:
                    candidates.append(path)
        inspected = [self.package(path) for path in candidates]
        selected: Path | None = None
        asset: dict[str, Any] = {
            "available": False,
            "identity_verified": False,
            "expected_digest": item.task_digest,
            "reason": "source index supplies no runnable exact task package",
            "locators": inspected,
        }
        for path, observation in zip(candidates, inspected, strict=True):
            if not observation["available"]:
                continue
            if item.task_digest and observation["observed_digest"] != item.task_digest:
                asset["reason"] = "available package differs from the source record's exact historical digest"
                continue
            selected = path
            asset.update(
                observation,
                identity_verified=item.task_digest is not None,
                expected_digest=item.task_digest,
            )
            break
        if selected is None and inspected and all(not row["available"] for row in inspected):
            asset["reason"] = "all indexed task package locators are unavailable locally"
        if materialization.get("state") is not None:
            asset["recorded_materialization_state"] = materialization["state"]
        docker = self.plan(selected) if selected is not None else {
            "state": "blocked",
            "reason": asset["reason"],
            "runtime_qualified": False,
            "executed": False,
            "execution_proof": False,
        }
        differential: dict[str, Any] = {
            "available": False,
            "reason": "source metadata supplies no existing exact-package differential pair",
        }
        if selected is not None and item.task_digest is not None:
            for pair in pairs:
                original = _path(pair.get("original_task"), self.root)
                fixed = _path(pair.get("fixed_task"), self.root)
                if original is None or fixed is None:
                    continue
                original_asset = self.package(original)
                if not original_asset["available"] or original_asset.get("observed_digest") != item.task_digest:
                    continue
                fixed_exists = fixed.is_dir() and (
                    (fixed / "Containerfile").is_file()
                    if pair.get("fixed_kind") == "pm_port"
                    else (fixed / "task.toml").is_file()
                )
                differential = {
                    "available": fixed_exists,
                    "pair_id": pair.get("pair_id"),
                    "manifest": self.pair_source,
                    "manifest_runnable": pair.get("runnable"),
                    "manifest_reason": pair.get("not_runnable_reason"),
                    "original_task": _portable(original, self.root),
                    "fixed_task": _portable(fixed, self.root),
                    "fixed_kind": pair.get("fixed_kind"),
                    "reason": "existing exact-package pair; no candidate input or execution proof"
                    if fixed_exists else "indexed fixed task package is unavailable locally",
                }
                if fixed_exists:
                    differential["docker_local"] = self.plan(original, fixed, pair.get("fixed_kind", "package"))
                break
        trace = _trace_preflight(item, self.root)
        unavailable = []
        if selected is None:
            unavailable.append("exact task package unavailable")
        if not differential["available"]:
            unavailable.append("exact differential pair unavailable")
        unavailable.append("source index supplies no owned candidate artifacts or retained same-submission replay inputs")
        return {
            "schema": "production.corpus-preflight/v1",
            "source_record": self.source(item),
            "task_package": asset,
            "public_trajectory": trace,
            "docker_local": docker,
            "differential_pair": differential,
            "capabilities": {
                "misalignment_replay": {"available": trace["available"], "reason": trace["reason"]},
                "task_quality_audit": {"available": False, "unavailable_reasons": unavailable},
                "reward_seeker": {"available": False, "unavailable_reasons": unavailable},
            },
            "executed": False,
            "execution_proof": False,
        }


def _persist(queue: ContinuousWorkQueue, *, event_type: str, payload: dict[str, Any], run_id: str) -> None:
    key = f"production-seed:v1:{event_type}:{payload['dataset_id']}:{payload['item_id']}"
    event_id = str(uuid5(NAMESPACE_URL, key))

    def unchanged(existing: dict[str, Any]) -> bool:
        return existing["event_type"] == event_type and existing["payload"] == payload

    existing = queue.get_event(event_id)
    if existing is not None:
        if not unchanged(existing):
            raise IdempotencyConflict(
                f"corpus source/preflight changed for {payload['dataset_id']}/{payload['item_id']}; "
                "native identity requires explicit reconciliation"
            )
        return
    event = {
        "event_id": event_id,
        "run_id": run_id,
        "system": "queue",
        "event_type": event_type,
        "ts": datetime.now(UTC).isoformat(),
        "prefix_step": None,
        "status": "applied",
        "payload": payload,
    }
    try:
        queue.append_event(event, idempotency_key=key)
    except IdempotencyConflict:
        # Independent ingesters may race with different presentation run IDs.
        # The native item and payload, not that caller label, own seed identity.
        existing = queue.get_event(event_id)
        if existing is None or not unchanged(existing):
            raise


def ingest_corpus(
    queue: ContinuousWorkQueue,
    *,
    dataset_ids: Sequence[str] | None = None,
    run_id: str = "production-seed",
) -> dict[str, Any]:
    """Persist every requested DEV record and return a deterministic seed report.

    ``blocked`` counts admitted records with no executable job, not exclusions.
    Excluded counts are aggregate only: no TEST/heldout identity, source record,
    trajectory or labels are returned or written. Repeated runs preserve the
    first event/run identity without growing queue depth. Changed source or
    preflight under an existing native identity raises ``IdempotencyConflict``
    rather than overwriting custody or creating an unbounded replacement job.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    if isinstance(dataset_ids, str):
        raise ValueError("dataset_ids must be a sequence, not a string")
    requested = set(DATASETS if dataset_ids is None else dataset_ids)
    unknown = requested.difference(DATASETS)
    if unknown:
        raise ValueError(f"unknown production dataset IDs: {sorted(unknown)}")
    root = queue.registry.repo_root.resolve()
    preflight = _Preflight(root, split_digest=queue.registry.split_assignments_digest)
    datasets: dict[str, dict[str, Any]] = {}
    for dataset in DATASETS:
        if dataset not in requested:
            continue
        source = sorted(queue.registry.items(dataset_id=dataset), key=lambda item: item.item_id)
        admitted = [item for item in source if item.split == "dev"]
        excluded = [item for item in source if item.split != "dev"]
        task_counts = Counter(
            item.task_id for item in source
            if item.task_id and not item.task_id.startswith("__benchmark__/")
        )
        reasons: Counter[str] = Counter()
        for item in excluded:
            if dataset == "mimo-defect-map-v1" and str(item.metadata.get("split", "")).lower() in ("test", "heldout"):
                reasons["heldout_all_versions"] += 1
            elif str(item.metadata.get("split", "")).lower() in ("test", "heldout"):
                reasons["test_or_heldout"] += 1
            else:
                reasons["canonical_test_lock"] += 1
        report: dict[str, Any] = {
            "source": len(source),
            "admitted": len(admitted),
            "queued": 0,
            "blocked": 0,
            "excluded": len(excluded),
            "excluded_by_reason": dict(sorted(reasons.items())),
            "source_dev": sum(str(item.metadata.get("split", "")).lower() == "dev" for item in source),
            "effective_admitted_dev": len(admitted),
            "excluded_due_cross_key_seal": reasons["canonical_test_lock"],
            "split_authority": preflight.split_authority,
            "source_task_count": len(task_counts),
            "admitted_task_count": len({
                item.task_id for item in admitted
                if item.task_id and not item.task_id.startswith("__benchmark__/")
            }),
            "task_count_basis": "distinct subject task IDs; public trace IDs can be grouping units, not runnable packages",
            "task_records": sum(item.source_path.name == "tasks.jsonl" for item in admitted),
            "finding_records": sum(item.source_path.name == "items.jsonl" for item in admitted)
            if dataset == "tb4-audit-v1" else 0,
            "capabilities": {"misalignment_replay": 0, "task_quality_audit": 0, "reward_seeker": 0},
            "items": [],
        }
        if dataset == "tb4-audit-v1":
            report["record_kinds"] = {
                kind: {
                    "source": len(records),
                    "source_dev": sum(str(item.metadata.get("split", "")).lower() == "dev" for item in records),
                    "admitted_dev": sum(item.split == "dev" for item in records),
                    "excluded_due_cross_key_seal": sum(
                        str(item.metadata.get("split", "")).lower() == "dev" and item.split != "dev"
                        for item in records
                    ),
                }
                for kind, records in (
                    ("tasks", [item for item in source if item.source_path.name == "tasks.jsonl"]),
                    ("findings", [item for item in source if item.source_path.name == "items.jsonl"]),
                )
            }
            report["benchmark_context_records"] = sum(
                bool(item.task_id and item.task_id.startswith("__benchmark__/"))
                for item in admitted
            )
        if dataset == "mimo-defect-map-v1":
            permanent_heldout = [
                item for item in excluded
                if str(item.metadata.get("split", "")).lower() in ("test", "heldout")
            ]
            report["historical_versions"] = {
                "package_records": len(source),
                "task_ids": len(task_counts),
                "tasks_with_multiple_versions": sum(count > 1 for count in task_counts.values()),
                "source_dev_package_records": report["source_dev"],
                "eligible_package_records": len(admitted),
                "permanently_heldout_package_records": len(permanent_heldout),
                "permanently_heldout_task_ids": len({item.task_id for item in permanent_heldout}),
                "canonical_sealed_package_records": reasons["canonical_test_lock"],
                "excluded_package_records": len(excluded),
            }
        for item in admitted:
            # Re-admit at persistence time; a caller's DEV request is never a token.
            item = queue.registry.admit(dataset, item.item_id)
            inspection = preflight.inspect(item)
            payload = {
                "schema": "production.corpus-admission/v1",
                "dataset_id": dataset,
                "item_id": item.item_id,
                "task_id": item.task_id,
                "task_digest": item.task_digest,
                "split": "dev",
                "preflight": inspection,
            }
            _persist(queue, event_type="queue.corpus_admitted", payload=payload, run_id=run_id)
            jobs: list[str] = []
            trace = inspection["public_trajectory"].get("descriptor")
            if trace is not None:
                _persist(
                    queue,
                    event_type="misalignment.replay_task",
                    payload={**payload, "public_trace": trace},
                    run_id=run_id,
                )
                jobs.append("misalignment.replay_task")
                report["capabilities"]["misalignment_replay"] += 1
            report["queued"] += len(jobs)
            report["blocked"] += not jobs
            report["items"].append(
                {
                    "item_id": item.item_id,
                    "task_id": item.task_id,
                    "task_digest": item.task_digest,
                    "jobs": jobs,
                    "preflight": inspection,
                }
            )
        datasets[dataset] = report
    return {
        "schema": "production.corpus-ingest/v1",
        "run_id": run_id,
        "datasets": datasets,
        "totals": {
            field: sum(report[field] for report in datasets.values())
            for field in ("source", "admitted", "queued", "blocked", "excluded")
        },
    }
