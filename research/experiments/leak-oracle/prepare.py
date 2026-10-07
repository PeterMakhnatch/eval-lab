#!/usr/bin/env python3
"""Freeze selected MiMo package identities for a direct oracle sweep; no cloud calls."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import tomllib
from pathlib import Path

from extract import atomic_text
from results import IMAGE_REF, RUN_DIGEST, TASK_ID

from evallab.registry import task_directory_digest

REQUIRED_FILES = (
    "task.toml",
    "instruction.md",
    "tests/test.patch",
    "tests/test.sh",
    "tests/test_command.sh",
)


def prepare_cohort(ledger: Path, snapshot: Path, variants: Path, expected_tasks: int) -> dict:
    """Bind actual selected package bytes, never swap in an original for a repair."""
    ledger, snapshot, variants = ledger.resolve(), snapshot.resolve(), variants.resolve()
    with ledger.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    selected = [row for row in rows if row["status"] in {"usable", "review"}]
    ids = [row["task_id"] for row in selected]
    if len(ids) != len(set(ids)) or len(ids) != expected_tasks:
        raise ValueError(f"Expected {expected_tasks} distinct selected tasks, found {len(ids)}")
    tasks = []
    for row in selected:
        task_id, expected_digest = row["task_id"], row["run_digest"]
        if not TASK_ID.fullmatch(task_id) or not RUN_DIGEST.fullmatch(expected_digest):
            raise ValueError(f"Invalid selected task identity: {task_id}")
        original = snapshot / task_id
        if row["run"] == "original":
            package = original
        elif row["run"] in {"repair", "leak-closed"}:
            package = (
                variants / f"mimo-v2.6-rl__{task_id}" / expected_digest.removeprefix("sha256:")[:12]
            )
        else:
            raise ValueError(f"Unsupported selected package type for {task_id}: {row['run']}")
        actual_digest = task_directory_digest(package)
        if actual_digest != expected_digest:
            raise ValueError(f"{task_id}: selected package digest mismatch ({actual_digest})")
        definition = tomllib.loads((package / "task.toml").read_text())
        original_definition = tomllib.loads((original / "task.toml").read_text())
        environment = definition["environment"]
        image = environment.get("docker_image")
        if not isinstance(image, str) or not IMAGE_REF.fullmatch(image):
            raise ValueError(f"{task_id}: direct replay requires an immutable image digest")
        files = {
            name: hashlib.sha256((package / name).read_bytes()).hexdigest()
            for name in REQUIRED_FILES
        }
        tasks.append(
            {
                "task_id": task_id,
                "run_digest": actual_digest,
                "image": image,
                "test_patch_sha256": files["tests/test.patch"],
                "task_path": str(package),
                "original_task_path": str(original),
                "original_run_digest": task_directory_digest(original),
                "original_image": original_definition["environment"].get("docker_image"),
                "selected_run": row["run"],
                "selected_status": row["status"],
                "split": row["split"],
                "image_mib": int(row["image_mib"]) if row["image_mib"] else None,
                "workdir": environment.get("workdir"),
                "cpus": environment.get("cpus"),
                "memory_mb": environment.get("memory_mb"),
                "declared_network": environment.get("network_mode"),
                "setup_timeout_sec": environment.get("healthcheck", {}).get("timeout_sec"),
                "verifier_timeout_sec": definition.get("verifier", {}).get("timeout_sec"),
                "build_timeout_sec": environment.get("build_timeout_sec"),
                "files_sha256": files,
            }
        )
    return {
        "schema_version": 1,
        "origin": "read-only input preparation; no execution or approval",
        "ledger_path": str(ledger),
        "ledger_sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
        "selection": "status in {usable,review}; independent of oracle-driven verdict",
        "requested_count": len(tasks),
        "tasks": tasks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--snapshot-tasks", required=True, type=Path)
    parser.add_argument("--variants", required=True, type=Path)
    parser.add_argument("--expected-tasks", type=int, default=1148)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.expected_tasks <= 0:
        parser.error("expected task count must be positive")
    destination = args.output.resolve()
    if destination == args.ledger.resolve() or any(
        root.resolve() == destination or root.resolve() in destination.parents
        for root in (args.snapshot_tasks, args.variants)
    ):
        parser.error("manifest output must not overwrite source inputs")
    manifest = prepare_cohort(args.ledger, args.snapshot_tasks, args.variants, args.expected_tasks)
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_text(destination, encoded)
    print(
        json.dumps(
            {
                "tasks": len(manifest["tasks"]),
                "ledger_sha256": manifest["ledger_sha256"],
                "manifest_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                "output": str(destination),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
