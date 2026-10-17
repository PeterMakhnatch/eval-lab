"""Reproduce all 567 reference packages and their actual VariantRecord lineage.

This is a source/package build, not native oracle/nop qualification. Existing
packages and records remain immutable; all newly derived records are candidates.
"""
from __future__ import annotations

import argparse
import csv
import json
from functools import partial
from pathlib import Path

import repair_environments
from prepare_references import INDEX, RELEASE, prepare

from evallab.cli import run_cli
from evallab.mimo_clean import (
    CLEAN_SET_VERSION_V6,
    LEDGER_REL,
    VARIANTS_REL,
    ChainBuilder,
    run_package_rel,
    variant_source,
)
from evallab.storage.paths import shared_checkout_root


def prepare_ledger(repo_root: Path, primary: Path, members: set[str]) -> dict[str, object]:
    with (repo_root / LEDGER_REL).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        rows = sorted(
            (dict(row) for row in reader if row["task_id"] in members),
            key=lambda row: row["task_id"],
        )
    if not fields or len({row["task_id"] for row in rows}) != len(rows):
        raise ValueError("Python source ledger must have unique task membership and a header")
    repairs: list[str] = []
    builder = ChainBuilder(
        repo_root, primary, variants_root=primary / VARIANTS_REL,
        clean_version=CLEAN_SET_VERSION_V6,
        created_by="mimo-v6-full-fleet",
    )
    for row in rows:
        if row["verdict"] not in {"keep", "fix"}:
            raise ValueError(f"reference-bearing Python task is not buildable: {row['task_id']}")
        if row["task_id"] != "format-code-task-000838":
            continue
        parent = primary / run_package_rel(row)
        planned = repair_environments.build_changes(parent, task_id=row["task_id"])
        if planned is None:
            raise ValueError("the accepted ntfy dependency failure requires a real environment repair")
        record, _reused = builder.find_or_derive(
            row["task_id"], repair_environments.TRANSFORM_ID, parent, row["run_digest"],
            partial(repair_environments.derive_environment_repair, task_id=row["task_id"]),
            parent_source=variant_source(row["task_id"], row["run_digest"], row),
            expected_inputs=planned[1],
        )
        row.update(
            run=record.transform,
            run_digest=record.variant_digest,
            run_transform=record.transform,
            run_variant_status=record.status,
        )
        row["evidence"] += f";fleet_environment_repair={record.record_relpath().as_posix()}"
        repairs.append(record.record_relpath().as_posix())
    output = repo_root / RELEASE / "release-ledger.csv"
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return {"python_roots": len(rows), "environment_repair_records": repairs}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    reference_summary = prepare(repo_root, repo_root / INDEX, repo_root / RELEASE)
    with (repo_root / RELEASE / "references.csv").open(newline="", encoding="utf-8") as handle:
        members = {row["task_id"] for row in csv.DictReader(handle)}
    ledger_summary = prepare_ledger(repo_root, shared_checkout_root(repo_root), members)
    manifest = repo_root / RELEASE / "manifest.csv"
    result = run_cli([
        "mimo-clean", "build", "--clean-version", CLEAN_SET_VERSION_V6,
        "--ledger", str(repo_root / RELEASE / "release-ledger.csv"),
        "--reference-index", str(repo_root / RELEASE / "references.csv"),
        "--manifest", str(manifest), "--tasks", ",".join(sorted(members)),
        "--workers", str(args.workers),
    ], workspace=repo_root)
    if result:
        return result
    with manifest.open(newline="", encoding="utf-8") as handle:
        built = list(csv.DictReader(handle))
    if len(built) != 567 or {row["task_id"] for row in built} != members:
        raise ValueError("canonical build did not preserve the exact 567-task membership")
    failed = [row for row in built if row["status"] != "built"]
    if failed:
        raise ValueError(f"canonical build has {len(failed)} unresolved packages: {failed}")
    lineage = json.loads(manifest.with_suffix(".lineage.json").read_bytes())
    if {row["task_id"] for row in lineage["tasks"]} != members:
        raise ValueError("canonical build did not register actual lineage for all members")
    print(json.dumps({
        "reference_preparation": reference_summary,
        "release_ledger": ledger_summary,
        "built": len(built),
        "native_qualification": "not performed by this source/package build",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
