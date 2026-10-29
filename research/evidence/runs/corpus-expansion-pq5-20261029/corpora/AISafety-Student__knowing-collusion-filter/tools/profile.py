#!/usr/bin/env python3
"""Profile the staged Knowing Collusion bundle: digests, label counts, floors, overlap.

Reads the locked local bundle only; prints and writes counts, hashes and AUROCs, never text.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import adapter

MANIFEST_SHA256 = "86f89499c04b3aa6f2c4884cca9535f5217f70bceecda7f630a64e91a5271b67"
NOTICE_SHA256 = "2c3c590eeda4fe5b92a227500efa6ccb8d32ac60f35badeda13ae5f9417e77de"
OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"
CLOUD_DB = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"


def verify_bundle(root: Path) -> dict[str, Any]:
    manifest_bytes = (root / "STAGE-MANIFEST.json").read_bytes()
    if adapter.sha256(manifest_bytes) != MANIFEST_SHA256:
        raise ValueError("manifest differs from the pinned staging receipt")
    manifest = json.loads(manifest_bytes)
    if manifest["revision"] != adapter.REVISION or manifest["repo"] != adapter.REPO:
        raise ValueError("manifest revision or repo differs from the pin")
    if manifest["gated"] is not False or manifest["licence"] != "cc-by-4.0":
        raise ValueError("licence or gate differs from the registered terms")
    if adapter.sha256((root / "CROSS_TEST_ONLY.md").read_bytes()) != NOTICE_SHA256:
        raise ValueError("local-only evaluation notice changed")
    paths = [root, *root.rglob("*")]
    if any(path.is_symlink() for path in paths):
        raise ValueError("immutable bundles cannot contain symlinks")
    writable = sum(bool(path.stat().st_mode & 0o222) for path in paths)
    if writable:
        raise ValueError("bundle must be locked before profiling")
    for entry in manifest["files"]:
        data = (root / entry["path"]).read_bytes()
        if len(data) != entry["bytes"] or adapter.sha256(data) != entry["sha256"]:
            raise ValueError(f"digest mismatch for {entry['path']}")
    return {
        "revision": manifest["revision"],
        "manifest_sha256": MANIFEST_SHA256,
        "notice_sha256": NOTICE_SHA256,
        "manifest_file_count": len(manifest["files"]),
        "manifest_total_bytes": manifest["total_bytes"],
        "verified_after_lock": True,
        "writable_paths": writable,
    }


def auroc(positive: list[float], negative: list[float]) -> float:
    """Mann-Whitney AUROC with ties at 0.5. Reported as the better of the two orientations."""
    wins = sum((p > n) + 0.5 * (p == n) for p in positive for n in negative)
    value = wins / (len(positive) * len(negative))
    return round(max(value, 1 - value), 4)


def content_free_floors(specs: list[dict[str, Any]]) -> dict[str, Any]:
    """AUROC of features that carry no behavioural content, positives vs scored negatives."""
    pos = [s for s in specs if s["gold"]["label_state"] == "positive"]
    neg = [s for s in specs if s["gold"]["label_state"].startswith("negative")]
    est = [s for s in specs if s["gold"]["label_state"] == "negative"]

    def feature(name: str, spec: dict[str, Any]) -> float:
        steps = spec["_steps"]
        if name == "n_steps":
            return len(steps)
        if name == "total_chars":
            return sum(len(adapter.step_text([step])) for step in steps)
        if name == "n_blocked_observations":
            return sum("NOT delivered" in (step.get("observation") or "") for step in steps)
        if name == "n_send_message_calls":
            return sum(
                call["name"] == "send_message"
                for step in steps
                for call in step.get("tool_calls") or []
            )
        return 0.0

    names = ("n_steps", "total_chars", "n_blocked_observations", "n_send_message_calls")
    out: dict[str, Any] = {}
    for name in names:
        out[name] = {
            "vs_all_negatives": auroc(
                [feature(name, s) for s in pos], [feature(name, s) for s in neg]
            ),
            "vs_established_negatives": auroc(
                [feature(name, s) for s in pos], [feature(name, s) for s in est]
            ),
        }
    out["scenario_id_only"] = scenario_floor(pos, neg)
    out["model_id_only"] = categorical_floor(pos, neg, "model_id")
    return out


def categorical_floor(pos: list[dict[str, Any]], neg: list[dict[str, Any]], key: str) -> float:
    """AUROC of the in-sample positive rate per category (an optimistic upper bound)."""
    totals: Counter[str] = Counter()
    hits: Counter[str] = Counter()
    for spec in pos + neg:
        totals[spec["gold"][key]] += 1
        hits[spec["gold"][key]] += spec["gold"]["label_state"] == "positive"
    score = {k: hits[k] / totals[k] for k in totals}
    return auroc([score[s["gold"][key]] for s in pos], [score[s["gold"][key]] for s in neg])


def scenario_floor(pos: list[dict[str, Any]], neg: list[dict[str, Any]]) -> float:
    return categorical_floor(pos, neg, "scenario")


def overlap(scenarios: set[str], repo_root: Path) -> dict[str, Any]:
    """Counts of hashed scenario ids (bare and full) found in DEV/TEST assignments and corpus_tasks."""
    hashed = {
        "bare": {adapter.sha256(s.encode()): s for s in scenarios},
        "full": {adapter.sha256(f"{adapter.REPO}:{s}".encode()): s for s in scenarios},
    }
    out: dict[str, Any] = {"scenarios": len(scenarios)}
    splits: Counter[str] = Counter()
    path = repo_root / "research/answer-keys/splits/assignments.csv"
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            for field in ("task", "unit", "item_id"):
                value = row[field]
                tail = value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
                if (
                    adapter.sha256(value.encode()) in hashed["full"]
                    or adapter.sha256(tail.encode()) in hashed["bare"]
                ):
                    splits[row["split"]] += 1
    out["assignments_rows_by_split"] = dict(splits)
    found = 0
    if CLOUD_DB.exists():
        db = sqlite3.connect(f"file:{CLOUD_DB}?mode=ro", uri=True)
        for (task_id,) in db.execute("select task_id from corpus_tasks"):
            tail = task_id.rsplit("/", 1)[-1]
            found += (
                adapter.sha256(task_id.encode()) in hashed["full"]
                or adapter.sha256(tail.encode()) in hashed["bare"]
            )
        out["corpus_tasks_checked"] = True
    else:
        out["corpus_tasks_checked"] = False
    out["corpus_tasks_matches"] = found
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=adapter.ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    integrity = verify_bundle(args.root)
    specs = list(adapter.iter_specs(args.root))
    states = Counter(s["gold"]["label_state"] for s in specs)
    reasons = Counter(f"{s['gold']['label_state']}:{s['gold']['label_reason']}" for s in specs)
    scored = [
        s
        for s in specs
        if s["gold"]["label_state"] in {"positive", "negative", "negative_unconfirmed"}
    ]
    no_reasoning = list(adapter.iter_specs(args.root, include_reasoning=False))
    repo_root = Path(
        subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
    )
    scenarios = {s["gold"]["scenario"] for s in specs}
    profile = {
        "repo": adapter.REPO,
        "revision": adapter.REVISION,
        "integrity": integrity,
        "unit": "helper episode (one chat)",
        "counts": {
            "episodes": len(specs),
            "positive": states["positive"],
            "negative": states["negative"] + states["negative_unconfirmed"],
            "negative_established": states["negative"],
            "negative_confession_missing": states["negative_unconfirmed"],
            "ambiguous": states["ambiguous"],
            "excluded": states["excluded"],
            "reasons": dict(sorted(reasons.items())),
            "scored_by_model": dict(
                Counter(
                    (s["gold"]["model_id"], s["gold"]["label_state"][:3]).__str__() for s in scored
                )
            ),
            "scored_by_condition": dict(
                Counter(
                    (s["gold"]["condition"], s["gold"]["label_state"][:3]).__str__() for s in scored
                )
            ),
            "scored_by_blocked": dict(
                Counter(
                    (min(s["gold"]["n_blocked"], 3), s["gold"]["label_state"][:3]).__str__()
                    for s in scored
                )
            ),
            "duplicate_traces": sum(s["gold"]["duplicate_of"] is not None for s in specs),
        },
        "adapter_selfcheck": adapter.selfcheck(args.root),
        "floors_with_reasoning": content_free_floors(scored),
        "floors_without_reasoning": content_free_floors(
            [
                s
                for s in no_reasoning
                if s["gold"]["label_state"] in {"positive", "negative", "negative_unconfirmed"}
            ]
        ),
        "overlap": overlap(scenarios, repo_root),
        "hashed_ids": {
            "scenario_bare_sha256": sorted(adapter.sha256(s.encode()) for s in scenarios),
            "scenario_full_sha256": sorted(
                adapter.sha256(f"{adapter.REPO}:{s}".encode()) for s in scenarios
            ),
            "run_episode_sha256": sorted(
                adapter.sha256(f"{s['gold']['run']}:{s['gold']['episode']}".encode()) for s in specs
            ),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in profile.items() if k != "hashed_ids"}, indent=2, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
