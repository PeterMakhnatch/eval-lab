#!/usr/bin/env python3
"""Fail closed unless a BFCL parity task matches the prospective source lock."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

DEFAULT_LOCK = Path("library/benchmarks/bfcl-parity/source-lock.json")


def task_digest(task: Path) -> str:
    records: list[bytes] = []
    for path in sorted(item for item in task.rglob("*") if item.is_file()):
        relative = path.relative_to(task).as_posix()
        file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append(f"{relative}\0{file_digest}\n".encode())
    if not records:
        raise RuntimeError(f"blocked:empty_bfcl_task:{task}")
    return "sha256:" + hashlib.sha256(b"".join(records)).hexdigest()


def validate_task(task: Path, lock: dict[str, Any]) -> dict[str, Any]:
    task = task.expanduser().resolve()
    expected = lock["canary_task"]
    if not task.is_dir() or not (task / "task.toml").is_file():
        raise RuntimeError(f"blocked:incomplete_bfcl_task:{task}")
    if task.name != expected["name"]:
        raise RuntimeError(
            f"blocked:bfcl_task_name_mismatch:expected={expected['name']}:actual={task.name}"
        )
    actual_digest = task_digest(task)
    if actual_digest != expected["material_digest"]:
        raise RuntimeError(
            "blocked:bfcl_task_digest_mismatch:"
            f"expected={expected['material_digest']}:actual={actual_digest}"
        )
    return {
        "status": "proceed",
        "created_trial": False,
        "benchmark": lock["benchmark"],
        "dataset_revision": lock["dataset"]["revision"],
        "task_name": expected["name"],
        "task_digest": actual_digest,
        "registry_status": lock["registry"]["status"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args()
    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    print(json.dumps(validate_task(args.task, lock), indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        print(f"BFCL preflight refused: {exc}")
        raise SystemExit(2) from exc
