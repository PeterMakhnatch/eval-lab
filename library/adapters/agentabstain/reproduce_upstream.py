"""Capture a no-network reproduction record for the pinned upstream release."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

UPSTREAM_COMMIT = "f581249704b26804e28a39e37396f1be00b71a4d"
DATASET_REVISION = "842228426c2a703347396501af61c7890972c7ee"
DATASET_SHA256 = "165f021e7bb8b3a1ba103cef291eb522ff219769e8e7727f1a669364a225fb63"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    upstream = args.upstream.resolve()
    dataset = args.dataset.resolve()
    commit = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    rows = [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]
    categories = sorted({row["category"] for row in rows})
    pairs = sorted({row["pair_id"] for row in rows})
    evidence = {
        "schema_version": "agentabstain-upstream-reproduction/v1",
        "repository": "https://github.com/AntiQuality/agentabstain",
        "expected_commit": UPSTREAM_COMMIT,
        "observed_commit": commit,
        "commit_match": commit == UPSTREAM_COMMIT,
        "license": {"path": "LICENSE", "sha256": sha(upstream / "LICENSE"), "declared": "MIT"},
        "dataset": {
            "repository": "https://huggingface.co/datasets/antiquality/agentabstain",
            "revision": DATASET_REVISION,
            "path": str(dataset),
            "sha256": sha(dataset),
            "full_official_sha256": DATASET_SHA256,
            "full_official_digest_observed": sha(dataset) == DATASET_SHA256,
        },
        "slice_reproduction": {"rows": len(rows), "pairs": len(pairs), "categories": categories,
                                "two_variants_per_pair": all(sum(r["pair_id"] == p for r in rows) == 2 for p in pairs)},
        "official_commands": [
            "python -m src.scripts.run_inference --runtime-config src/configs/openclaw_deepseek-v4-pro.yaml --smoke",
            "python -m eval.runner --provider openclaw --model openrouter/deepseek/deepseek-v4-pro",
        ],
        "status": "verified" if commit == UPSTREAM_COMMIT and len(rows) == 526 else "blocked",
    }
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    if evidence["status"] != "verified":
        raise SystemExit("upstream reproduction checks did not match pinned revision")


if __name__ == "__main__":
    main()
