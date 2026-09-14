"""Export explicitly selected registered tasks as an offline LEGO-RL task index.

This writes references, not trajectories, and neither admits tasks nor authorizes
execution. Training harness metadata requires current upstream mixed-harness configuration;
validation uses its fixed runtime val_harness, not a per-row override.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa

from evallab.evidence.parquet_io import write_table_atomic
from evallab.registry import (
    RegistryError,
    TaskRegistry,
    compute_task_digests,
    verify_certification_packet,
    verify_control_evidence,
    verify_package_completeness,
)
from evallab.schemas import TaskRegistryRecord

UPSTREAM_REVISION = "b1e5662f5a8545d4f444712a0ec6375cb4355731"
LOCAL_CAPTURE_REVISION = "d7b3511a6258c26a7a4dee2d51c9229aef5e88bb"
TASK_INDEX_SCHEMA = pa.schema(
    [
        ("prompt", pa.list_(pa.struct([("role", pa.string()), ("content", pa.string())]))),
        ("reward_model", pa.struct([("style", pa.string()), ("ground_truth", pa.null())])),
        (
            "extra_info",
            pa.struct(
                [
                    ("harbor_task_path", pa.string()),
                    ("instance_id", pa.string()),
                    ("data_source", pa.string()),
                ]
            ),
        ),
    ]
)
_HARNESS_TASK_INDEX_SCHEMA = TASK_INDEX_SCHEMA.set(
    2,
    pa.field(
        "extra_info",
        pa.struct(
            [
                *TASK_INDEX_SCHEMA.field("extra_info").type,
                pa.field("agent_harness", pa.string()),
            ]
        ),
    ),
)


class TrainingPoolError(ValueError):
    """The requested selection or destination cannot safely be exported."""


@dataclass(frozen=True)
class TaskSelection:
    """One explicit registry ID and optional current-upstream training harness name."""

    task_id: str
    harness: str | None = None

    def __post_init__(self) -> None:
        for label, value in (("task_id", self.task_id), ("harness", self.harness)):
            if value is None and label == "harness":
                continue
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise TrainingPoolError(f"{label} must be a nonempty, unpadded string")


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _confined(root: Path, path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise TrainingPoolError(f"source path escapes repository root: {path}")
    return resolved


def _check_tree(root: Path, path: Path) -> Path:
    resolved = _confined(root, path)
    # Registry digests do not recursively follow directory symlinks. Reject them
    # rather than implying that all runtime-visible package bytes were checked.
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink():
            raise TrainingPoolError(f"symlink source is unsupported: {item}")
    if resolved.is_dir():
        for item in resolved.rglob("*"):
            if item.is_symlink():
                raise TrainingPoolError(f"symlink source is unsupported: {item}")
    return resolved


def _validate_record(root: Path, record: TaskRegistryRecord, split: str) -> Path:
    if record.state != "registered":
        raise TrainingPoolError(f"{record.task_id}: registered state required, got {record.state}")
    permission = "training" if split == "train" else "measurement"
    if "heldout" in record.allowed_uses or permission not in record.allowed_uses:
        raise TrainingPoolError(
            f"{record.task_id}: {split} requires {permission} permission and no heldout use"
        )
    if record.license is None or not record.license.strip():
        raise TrainingPoolError(
            f"{record.task_id}: license metadata required (not a grant of rights)"
        )
    task_path = _check_tree(root, root / record.task_path)
    if not task_path.is_dir():
        raise TrainingPoolError(f"{record.task_id}: task package is not a directory")
    # Resolve locally before calling the registry helper: it otherwise permits a
    # linked worktree to borrow a missing package from the shared checkout.
    verify_package_completeness(root, record)
    if compute_task_digests(task_path) != record.digests:
        raise TrainingPoolError(f"{record.task_id}: task digest drift")
    if record.control_evidence is None:
        raise TrainingPoolError(f"{record.task_id}: control evidence required")
    for ref in (record.control_evidence.oracle, record.control_evidence.nop):
        evidence = root / ref.evidence_path
        _check_tree(root, evidence)
        _check_tree(root, evidence.with_name("lock.json"))
    verify_control_evidence(root, record)
    if record.certification.packet_path is not None:
        _check_tree(root, (root / record.certification.packet_path).parent)
    verify_certification_packet(root, record)
    return task_path


def _leakage_keys(record: TaskRegistryRecord, path: Path) -> set[tuple[str, str]]:
    # Use only recorded identities. Refs deliberately do not distinguish versions
    # of the same source; there is no invented parent/generator lineage here.
    return {
        ("task_id", record.task_id),
        ("task_path", str(path)),
        ("package", record.digests.package),
        ("task_family", record.task_family),
        ("source_uri", record.source_uri),
    }


def export_training_pool(
    repo_root: Path,
    output: Path,
    *,
    train: Sequence[TaskSelection],
    validation: Sequence[TaskSelection] = (),
) -> Path:
    """Validate the whole request, then publish train/validation Parquet + manifest.

    Repeating an identical request is a no-op. Any nonidentical existing bundle
    is rejected. Paths reference this exact checkout: no copying or remapping.
    Legacy missing certification and contamination unknowns remain explicit.
    """
    if not train and not validation:
        raise TrainingPoolError("select at least one train or validation task")
    if len({selection.harness is not None for selection in train}) > 1:
        raise TrainingPoolError(
            "train tasks must all specify a harness or all use the runtime default; "
            "mixed optional metadata would become null in Parquet"
        )
    root = repo_root.resolve(strict=True)
    try:
        _check_tree(root, root / "library/registry")
        registry = TaskRegistry.from_repo(root)
        rows: dict[str, list[dict[str, Any]]] = {"train": [], "validation": []}
        entries: list[dict[str, Any]] = []
        split_keys: dict[str, set[tuple[str, str]]] = {"train": set(), "validation": set()}
        for split, selections in (("train", train), ("validation", validation)):
            seen: set[str] = set()
            for selection in sorted(selections, key=lambda item: item.task_id):
                if selection.task_id in seen:
                    raise TrainingPoolError(f"duplicate {split} task: {selection.task_id}")
                if split == "validation" and selection.harness is not None:
                    raise TrainingPoolError(
                        "validation uses runtime val_harness; per-row harness is unsupported"
                    )
                seen.add(selection.task_id)
                record = registry.get(selection.task_id)
                if record is None:
                    raise TrainingPoolError(f"unknown registered task ID: {selection.task_id}")
                task_path = _validate_record(root, record, split)
                split_keys[split].update(_leakage_keys(record, task_path))
                extra_info = {
                    "harbor_task_path": str(task_path),
                    "instance_id": record.task_id,
                    "data_source": "harbor",
                }
                if selection.harness is not None:
                    extra_info["agent_harness"] = selection.harness
                rows[split].append(
                    {
                        "prompt": [{"role": "user", "content": str(task_path)}],
                        "reward_model": {"style": "rule", "ground_truth": None},
                        "extra_info": extra_info,
                    }
                )
                snapshot = record.model_dump(mode="json")
                entries.append(
                    {
                        "split": split,
                        "task_id": record.task_id,
                        "harness": selection.harness,
                        "source_task_path": str(task_path),
                        "registry_record_digest": _digest(_canonical_bytes(snapshot)),
                        "registry_record": snapshot,
                    }
                )
        overlap = split_keys["train"] & split_keys["validation"]
        if overlap:
            raise TrainingPoolError(f"cross-split leakage: {sorted(overlap)!r}")
    except (RegistryError, OSError, ValueError) as exc:
        if isinstance(exc, TrainingPoolError):
            raise
        raise TrainingPoolError(str(exc)) from exc

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "lego_rl_task_index",
        "source_root": str(root),
        "upstream_contract": {
            "revision": UPSTREAM_REVISION,
            "writer": "utils/create_task_index.py",
            "reader": "src/verl_patch/agent_loop/builtin_swe_agent_loop.py",
            "harness_reader": "src/verl_patch/agent_loop/harness.py",
            "local_capture_revision": LOCAL_CAPTURE_REVISION,
            "local_capture_limit": "does not consume harness metadata; capture repairs need integration",
        },
        "scope": {
            "task_bytes_verified": "source checkout only",
            "runtime_path_remapping": False,
            "harness": "train extra_info.agent_harness requires a configured upstream harness; validation uses runtime val_harness",
            "license": "metadata required; no license interpretation or rights granted",
            "leakage": "cross-split task ID, resolved path, package digest, task family, exact source URI",
            "unknown_lineage": "not inferred; distinct unrecorded source relationships remain unknown",
            "runtime_proven": False,
            "reward_validity_proven": False,
            "training_utility_proven": False,
            "execution_authorized": False,
            "admission_changed": False,
        },
        "tasks": entries,
        "files": {},
    }
    output = output.absolute()
    if output.is_symlink():
        raise TrainingPoolError("output bundle must not be a symlink")
    destination = output.resolve()
    protected = [root / "library", root / "research/evidence", root / "research/registration"]
    protected.extend(Path(entry["source_task_path"]) for entry in entries)
    if any(destination.is_relative_to(path.resolve()) for path in protected):
        raise TrainingPoolError("output must not modify task, registry, or evidence source trees")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as staging:
        stage = Path(staging) / "bundle"
        stage.mkdir()
        for split in ("train", "validation"):
            name = f"{split}.parquet"
            schema = (
                _HARNESS_TASK_INDEX_SCHEMA
                if split == "train" and train and train[0].harness is not None
                else TASK_INDEX_SCHEMA
            )
            write_table_atomic(stage / name, rows[split], schema)
            manifest["files"][name] = {
                "rows": len(rows[split]),
                "digest": _digest((stage / name).read_bytes()),
            }
        (stage / "manifest.json").write_bytes(_canonical_bytes(manifest))
        if output.exists():
            expected = {item.name for item in stage.iterdir()}
            if not output.is_dir() or {item.name for item in output.iterdir()} != expected:
                raise TrainingPoolError("existing output is not an identical bundle")
            if any(
                (output / name).is_symlink()
                or not (output / name).is_file()
                or (output / name).read_bytes() != (stage / name).read_bytes()
                for name in sorted(expected)
            ):
                raise TrainingPoolError("existing output is not an identical bundle")
        else:
            stage.rename(output)
    return output / "manifest.json"


def _selection(value: str) -> TaskSelection:
    task_id, separator, harness = value.partition("=")
    try:
        return TaskSelection(task_id, harness if separator else None)
    except TrainingPoolError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new immutable bundle directory")
    for split in ("train", "validation"):
        parser.add_argument(
            f"--{split}",
            type=_selection,
            action="append",
            default=[],
            metavar="TASK_ID[=HARNESS]" if split == "train" else "TASK_ID",
            help="repeat for explicit registry IDs; only train accepts an upstream harness name",
        )
    args = parser.parse_args(argv)
    try:
        manifest = export_training_pool(
            args.repo_root, args.output, train=args.train, validation=args.validation
        )
    except (TrainingPoolError, OSError) as exc:
        parser.exit(2, f"training-pool: {exc}\n")
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
