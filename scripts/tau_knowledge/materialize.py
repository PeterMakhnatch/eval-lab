#!/usr/bin/env python3
"""Materialize selected Tau tasks under ignored derived/harbor-tasks only."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_MANIFEST = Path("library/benchmarks/tau-knowledge/cohort.manifest.json")
DEFAULT_OUTPUT = Path("derived/harbor-tasks/tau")

try:
    from preflight import sha256, validate_source
except ImportError as exc:
    _spec = importlib.util.spec_from_file_location(
        "tau_knowledge_preflight", Path(__file__).with_name("preflight.py")
    )
    if _spec is None or _spec.loader is None:
        raise RuntimeError("cannot load Tau preflight") from exc
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)
    sha256, validate_source = _module.sha256, _module.validate_source


def source_digest(manifest: Mapping[str, Any]) -> str:
    payload = {
        "required_upstream": manifest["required_upstream"],
        "adapter_evidence": manifest["adapter_evidence"],
        "tasks": manifest["tasks"],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_adapter(root: Path, manifest: Mapping[str, Any]) -> Path:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"blocked:missing_adapter_checkout:{root}")
    expected = manifest["adapter_evidence"]["commit"]
    try:
        actual = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"blocked:unreadable_adapter_checkout:{root}") from exc
    if actual != expected:
        raise RuntimeError(
            f"blocked:adapter_commit_mismatch:expected={expected}:actual={actual}"
        )
    return root


def _load_adapter(root: Path) -> type[Any]:
    candidates = [root / "src", root / "adapters" / "tau3-bench" / "src"]
    for candidate in candidates:
        if candidate.is_dir():
            sys.path.insert(0, str(candidate))
            try:
                return importlib.import_module("tau3_bench").Tau3BenchAdapter
            except (ImportError, AttributeError):
                continue
    raise RuntimeError(f"blocked:adapter_package_missing:{root}")


def materialize(
    *,
    manifest_path: Path,
    source_root: Path,
    adapter_root: Path,
    output_root: Path,
    task_id: str,
    overwrite: bool = False,
) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = {str(row["task_id"]): row for row in manifest["tasks"]}
    if task_id not in rows:
        raise RuntimeError(f"task is not selected by immutable cohort: {task_id}")
    source = validate_source(source_root, manifest)
    adapter = _validate_adapter(adapter_root, manifest)
    destination = output_root.expanduser().resolve() / source_digest(manifest)
    task_dir = destination / f"tau3-banking_knowledge-{task_id.replace('_', '-')}"
    if destination.exists():
        if not overwrite:
            raise RuntimeError(
                f"materialization exists; pass --overwrite to replace: {destination}"
            )
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    adapter_type = _load_adapter(adapter)
    adapter_type(
        destination, overwrite=False, task_ids=[task_id], tau2_root=source
    ).run()
    if not (task_dir / "task.toml").is_file() or not (
        task_dir / "tests/config.json"
    ).is_file():
        raise RuntimeError(f"adapter did not produce complete task: {task_dir}")
    metadata = {
        "schema_version": "tau-knowledge-materialization/v1",
        "benchmark": "tau-Knowledge",
        "task_id": task_id,
        "source_digest": "sha256:" + source_digest(manifest),
        "source_commit": manifest["required_upstream"]["commit"],
        "source_task_digest": rows[task_id]["task_sha256"],
        "adapter_commit": manifest["adapter_evidence"]["commit"],
        "generated_task": task_dir.name,
    }
    (destination / "materialization.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", default="task_001")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--adapter", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    source = args.source or (
        Path(os.environ["TAU2_BENCH_ROOT"])
        if os.environ.get("TAU2_BENCH_ROOT")
        else None
    )
    adapter = args.adapter or (
        Path(os.environ["TAU3_BENCH_ADAPTER_ROOT"])
        if os.environ.get("TAU3_BENCH_ADAPTER_ROOT")
        else None
    )
    if source is None or adapter is None:
        raise SystemExit(
            "blocked: TAU2_BENCH_ROOT and TAU3_BENCH_ADAPTER_ROOT are required"
        )
    print(
        materialize(
            manifest_path=args.manifest,
            source_root=source,
            adapter_root=adapter,
            output_root=args.output_root,
            task_id=args.task_id,
            overwrite=args.overwrite,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        print(f"tau-Knowledge materialization refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
