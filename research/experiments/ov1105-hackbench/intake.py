#!/usr/bin/env python3
"""Download pinned HackBench bytes and write an aggregate-only source manifest.

Run with ``keys run -- python3 intake.py --cache CACHE --manifest manifest.json``.
The existing LB-6 fetcher owns downloading; this wrapper enforces its digest pins.
No detector, model, training, registry, or upload operations occur here.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
TOOLS = REPO_ROOT / "research/evidence/runs/corpus-expansion-lb6-20261027/tools"
REVISION = "b3d6f678e6359dcd35e20f3f2c5ee085b5af4934"
DEFAULT_CACHE = Path.home() / ".cache/eval-lab/source-intake/facebook__hackbench" / REVISION


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stage = load_module("ov1105_hackbench_stage", TOOLS / "hackbench_stage.py")


def outside_repository(path: Path) -> Path:
    path = path.expanduser().resolve()
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("Source data and converted views must remain outside every Git checkout")
    return path


def verify_source(cache: Path) -> dict[str, Any]:
    """Fail closed on altered bytes; retain the publisher's known stale README sums."""
    result = stage.verify_digests(cache)
    for name, info in result["files"].items():
        required = [
            key for key in info
            if key.startswith("matches_")
            and not (name == "README.md" and key in {"matches_SHA256SUMS", "matches_MANIFEST"})
        ]
        if not required or not all(info[key] for key in required):
            raise ValueError(f"Pinned source integrity failure: {name}")
    if result["revision"] != REVISION:
        raise ValueError("Source revision does not match the intake pin")
    return result


def build_manifest(cache: Path) -> dict[str, Any]:
    digests = verify_source(cache)
    profile, _ = stage.profile_rows(cache)
    published = digests["manifest"]
    checks = {
        "rows": profile["rows"] == published["rows"],
        "models": profile["models"] == published["counts"]["models"],
        "tasks": profile["tasks"] == published["counts"]["tasks"],
        "origins": profile["origins"] == published["counts"]["origins"],
        "distinct_raw_transcripts": profile["distinct_raw_transcripts"] == published["distinct_raw_transcripts"],
        "gold_command_indices": profile["gold_commands_total"] == published["numbering"]["gold_command_indices_count"],
    }
    if profile["structural_problems"] or not all(checks.values()):
        raise ValueError("Pinned source structure or publisher counts disagree")
    return {
        "schema_version": "ov1105-hackbench-intake-v1",
        "repo_id": stage.REPO,
        "revision": REVISION,
        "source_url": f"https://huggingface.co/datasets/{stage.REPO}/tree/{REVISION}",
        "cache_layout": "~/.cache/eval-lab/source-intake/facebook__hackbench/<revision>/",
        "license": "CC-BY-NC-4.0",
        "license_url": "https://creativecommons.org/licenses/by-nc/4.0/",
        "usage": {"evaluation_only": True, "noncommercial_only": True, "training_allowed": False, "rehosting_allowed": False},
        "files": {
            name: {**info, "rows": profile["rows"] if name == stage.DATA else None}
            for name, info in digests["files"].items()
        },
        "source_splits": {"test": profile["rows"]},
        "mapped_classes": {"positive": profile["rows"], "negative": 0, "excluded": 0},
        "publisher_selection": published["selection"],
        "publisher_count_checks": checks,
        "profile": profile,
        "exposure_policy": json.loads((HERE / "label-map.json").read_text(encoding="utf-8"))["exposure_policy"],
        "warnings": [
            "The pinned README Git blob is valid, but publisher SHA256SUMS and MANIFEST.json list a stale README hash.",
            "All released records are HACK-positive; 33 LEGIT source records are not released. Run-level AUROC and FPR are undefined.",
            "169 records contain 166 distinct raw transcripts. Preserve records and cluster uncertainty by task.",
            "All 169 records previously entered CM-11/CM-15 detector cross-tests; 75 also entered behavior mining. Sealed is study-specific, not detector-unread.",
        ],
        "sources": {
            "card": f"https://huggingface.co/datasets/{stage.REPO}/blob/{REVISION}/README.md",
            "blog": "https://facebookresearch.github.io/RAM/blogs/hackbench/",
            "paper": "https://arxiv.org/html/2610.38882v1#S2",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--verify-only", action="store_true", help="Do not download or change cached source files")
    args = parser.parse_args()
    cache = outside_repository(args.cache)
    present = [name for name in stage.FILES if (cache / name).exists()]
    if len(present) != len(stage.FILES):
        if args.verify_only:
            raise SystemExit("Incomplete source cache; run intake with keys run first")
        if present:
            raise SystemExit("Partial source cache preserved; select a new empty cache directory")
        stage.fetch(cache)
    manifest = build_manifest(cache)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"revision": REVISION, "rows": manifest["profile"]["rows"], "manifest": str(args.manifest)}))


if __name__ == "__main__":
    main()
