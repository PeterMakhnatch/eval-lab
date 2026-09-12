"""Export explicitly assigned, registry-qualified Harbor tasks for Lego-RL.

The three Lego columns follow utils/create_task_index.py. Qualification means
recorded oracle=1/nop=0 controls, not a fresh runtime check or reward-alignment
certification. This exporter does not execute Harbor or authorize training runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

_SPLIT_SUFFIXES = {"train": "train", "dev": "val", "final": "final"}
_SCHEMA = pa.schema([
    ("prompt", pa.list_(pa.struct([("role", pa.string()), ("content", pa.string())]))),
    ("reward_model", pa.struct([("style", pa.string()), ("ground_truth", pa.null())])),
    ("extra_info", pa.struct([
        ("harbor_task_path", pa.string()),
        ("instance_id", pa.string()),
        ("data_source", pa.string()),
    ])),
    ("lab_task_id", pa.string()),
    ("verifier_sha256", pa.string()),
    ("harbor_task_sha256", pa.string()),
    ("runtime_qualified", pa.bool_()),
    ("admission_source", pa.string()),
    ("split", pa.string()),
    ("registry_record_sha256", pa.string()),
])


class LegoIndexError(ValueError):
    """A task cannot be handed to the trainer under the explicit index contract."""


def _read_record(registry_dir: Path, task_id: str) -> tuple[dict, str]:
    if not task_id or Path(task_id).name != task_id or task_id in {".", ".."}:
        raise LegoIndexError(f"invalid_task_id: {task_id!r}")
    path = registry_dir / f"{task_id}.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise LegoIndexError(f"registry_record_missing: {task_id}") from exc
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LegoIndexError(f"registry_record_invalid: {task_id}") from exc
    if not isinstance(record, dict) or record.get("task_id") != task_id:
        raise LegoIndexError(f"registry_record_invalid: {task_id}")
    return record, f"sha256:{hashlib.sha256(raw).hexdigest()}"


def load_registry_record(registry_dir: Path, task_id: str) -> dict:
    """Read the named JSON record, never infer registration from a directory."""
    return _read_record(registry_dir, task_id)[0]


def _qualified_digests(record: dict, task_id: str) -> tuple[str, str]:
    evidence = record.get("control_evidence")
    if not isinstance(evidence, dict):
        raise LegoIndexError(f"runtime_evidence_missing: {task_id}")
    controls = []
    for agent, expected in (("oracle", 1), ("nop", 0)):
        control = evidence.get(agent)
        if not isinstance(control, dict) or control.get("reward") is None:
            raise LegoIndexError(f"runtime_evidence_missing: {task_id}: {agent}")
        reward = control["reward"]
        if type(reward) not in (int, float) or reward != expected:
            raise LegoIndexError(f"runtime_evidence_invalid: {task_id}: {agent}")
        controls.append(control)
    digests = record.get("digests")
    verifier = digests.get("verifier") if isinstance(digests, dict) else None
    harbor = controls[0].get("harbor_task_digest")
    if not isinstance(verifier, str) or not verifier or not isinstance(harbor, str) or not harbor:
        raise LegoIndexError(f"provenance_missing: {task_id}: verifier or Harbor task digest")
    if controls[1].get("harbor_task_digest") != harbor:
        raise LegoIndexError(f"provenance_mismatch: {task_id}: oracle/nop Harbor task digest")
    return verifier, harbor


def _task_path(record: dict, tasks_root: Path, task_id: str) -> Path:
    recorded = record.get("task_path")
    if not isinstance(recorded, str) or not recorded:
        raise LegoIndexError(f"task_path_missing: {task_id}")
    # Registry paths are repository-relative, including nested experimental tasks.
    try:
        relative = Path(recorded).relative_to("library/tasks")
    except ValueError as exc:
        raise LegoIndexError(f"task_path_invalid: {task_id}: {recorded}") from exc
    root = tasks_root.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not (path / "instruction.md").is_file():
        raise LegoIndexError(f"task_path_invalid: {task_id}: {recorded}")
    return path


def build_index_rows(
    *,
    registry_dir: Path,
    tasks_root: Path,
    assignments: list[tuple[str, str]],
    admitted: set[str],
) -> list[dict]:
    """Build one row per explicit assignment; fail closed before any export."""
    rows = []
    assigned_splits: dict[str, str] = {}
    for task_id, split in assignments:
        if split not in _SPLIT_SUFFIXES:
            raise LegoIndexError(f"invalid_split: {task_id}: {split}")
        if task_id in assigned_splits and assigned_splits[task_id] != split:
            raise LegoIndexError(f"split_conflict: {task_id}")
        assigned_splits[task_id] = split
        record, record_digest = _read_record(registry_dir, task_id)
        if record.get("state") != "registered" or not record.get("approved_by"):
            raise LegoIndexError(f"task_not_registered: {task_id}")
        if split == "train":
            if task_id not in admitted:
                raise LegoIndexError(f"training_admission_missing: {task_id}")
            if "training" not in (record.get("allowed_uses") or []):
                raise LegoIndexError(f"training_use_not_allowed: {task_id}")
        verifier, harbor = _qualified_digests(record, task_id)
        path = _task_path(record, tasks_root, task_id)
        rows.append({
            "prompt": [{"role": "user", "content": str(path)}],
            "reward_model": {"style": "rule", "ground_truth": None},
            "extra_info": {
                "harbor_task_path": str(path),
                "instance_id": path.name,
                "data_source": "harbor",
            },
            "lab_task_id": task_id,
            "verifier_sha256": verifier,
            "harbor_task_sha256": harbor,
            "runtime_qualified": True,
            "admission_source": "explicit-cli-argument",
            "split": split,
            "registry_record_sha256": record_digest,
        })
    return rows


def write_index(rows: list[dict], output: Path) -> dict:
    """Write train/val/final files, including typed empty splits; never backfill val.

    ``output`` is a prefix with an optional .parquet suffix. Returned counts and
    paths use lab split names (train/dev/final); dev's filename uses Lego's val.
    Call with rows from build_index_rows, which owns registry/admission checks.
    """
    partitions: dict[str, list[dict]] = {split: [] for split in _SPLIT_SUFFIXES}
    assigned_splits: dict[str, str] = {}
    for row in rows:
        split = row.get("split")
        if split not in partitions:
            raise LegoIndexError(f"invalid_split: {split}")
        task_id = row["lab_task_id"]
        if task_id in assigned_splits and assigned_splits[task_id] != split:
            raise LegoIndexError(f"split_conflict: {task_id}")
        assigned_splits[task_id] = split
        partitions[split].append(row)
    base = str(output).removesuffix(".parquet")
    paths = {split: f"{base}_{suffix}.parquet" for split, suffix in _SPLIT_SUFFIXES.items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    for split, partition in partitions.items():
        pq.write_table(
            pa.Table.from_pylist(partition, schema=_SCHEMA), paths[split],
            compression="zstd", use_dictionary=False, write_statistics=True,
        )
    return {"row_counts": {split: len(part) for split, part in partitions.items()}, "paths": paths}


def _assignment(value: str) -> tuple[str, str]:
    task_id, separator, split = value.partition("=")
    if not separator or not task_id or split not in _SPLIT_SUFFIXES:
        raise argparse.ArgumentTypeError("expected TASK_ID=train|dev|final")
    return task_id, split


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--tasks", required=True, type=Path)
    parser.add_argument("--assign", required=True, action="append", type=_assignment)
    parser.add_argument("--admit", action="append", default=[])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        rows = build_index_rows(
            registry_dir=args.registry, tasks_root=args.tasks,
            assignments=args.assign, admitted=set(args.admit),
        )
        receipt = write_index(rows, args.output)
    except LegoIndexError as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
