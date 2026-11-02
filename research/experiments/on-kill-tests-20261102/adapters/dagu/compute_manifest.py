#!/usr/bin/env python3
"""Compute sha256 manifests for receipts, scripts, and artifacts."""

import hashlib
import json
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def main():
    base_dir = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/on-f8-dagu-trial-20261102/research/experiments/on-dagu-trial-20261102")
    receipts_dir = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F8-DAGU-TRIAL/receipts")
    results_dir = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F8")

    manifest = {}

    # Experiment files
    for p in sorted(base_dir.glob("*")):
        if p.is_file() and p.name != "manifest.json":
            manifest[f"repo:{p.name}"] = {
                "sha256": sha256_file(p),
                "sha256_12": sha256_file(p)[:12],
                "size_bytes": p.stat().st_size,
            }

    # Binary
    dagu_bin = results_dir / "dagu/dagu"
    if dagu_bin.exists():
        manifest["binary:dagu"] = {
            "sha256": sha256_file(dagu_bin),
            "sha256_12": sha256_file(dagu_bin)[:12],
            "size_bytes": dagu_bin.stat().st_size,
        }

    # Receipts
    for p in sorted(receipts_dir.glob("**/*")):
        if p.is_file():
            rel = p.relative_to(receipts_dir)
            manifest[f"receipt:{rel}"] = {
                "sha256": sha256_file(p),
                "sha256_12": sha256_file(p)[:12],
                "size_bytes": p.stat().st_size,
            }

    out_file = base_dir / "manifest.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    with open(receipts_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    with open(results_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Manifest written with {len(manifest)} entries.")
    for k, v in manifest.items():
        print(f"  {k}: {v['sha256_12']} ({v['size_bytes']}B)")


if __name__ == "__main__":
    main()
