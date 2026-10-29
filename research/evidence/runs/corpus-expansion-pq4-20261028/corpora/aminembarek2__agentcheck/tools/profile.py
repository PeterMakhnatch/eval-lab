#!/usr/bin/env python3
"""Reverify the locked agentcheck bundle; publish aggregates, digests and hashed ids only."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import stat
from collections import Counter
from pathlib import Path
from typing import Any

from adapter import REPO, REVISION, ROOT, iter_runs, label_state, selfcheck

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"
MANIFEST_SHA256 = "699e531e8ddc0a1dbc0941a9f2d05e0ddfa8c71245d1a3a8b9183c00bd3f0707"
NOTICE_SHA256 = "0b2f84062d7e0f31db08d48e1af96ce99b81526ac78b178bbc8741a331ffb2d2"
LICENCE = "mit"
ASSIGNMENTS = Path(__file__).resolve().parents[6] / "answer-keys/splits/assignments.csv"
CLOUD_DB = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalise_id(value: str, mode: str) -> str:
    value = value.strip().lower()
    if mode == "bare":
        return re.sub(r"^\d+-", "", value.rsplit("/", 1)[-1])
    if mode == "full":
        return value
    raise ValueError(mode)


def hashed(values: set[str], mode: str) -> set[str]:
    return {sha256(normalise_id(v, mode).encode()) for v in values}


def verify_bundle(root: Path) -> dict[str, Any]:
    manifest_bytes = (root / "STAGE-MANIFEST.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    problems: list[str] = []
    if sha256(manifest_bytes) != MANIFEST_SHA256:
        problems.append("manifest_sha256")
    if sha256((root / "CROSS_TEST_ONLY.md").read_bytes()) != NOTICE_SHA256:
        problems.append("notice_sha256")
    if manifest["repo"] != REPO or manifest["revision"] != REVISION:
        problems.append("repo_or_revision")
    if manifest["licence"] != LICENCE or manifest["gated"] is not False:
        problems.append("licence_or_gate")
    listed = {f["path"] for f in manifest["files"]}
    for f in manifest["files"]:
        data = (root / f["path"]).read_bytes()
        if len(data) != f["bytes"] or sha256(data) != f["sha256"]:
            problems.append(f"digest:{f['path']}")
    on_disk = set()
    writable = 0
    for cur, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(cur) / name
            if p.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                writable += 1
            if name in files:
                on_disk.add(str(p.relative_to(root)))
    extra = sorted(on_disk - listed - {"STAGE-MANIFEST.json", "CROSS_TEST_ONLY.md"})
    if extra or listed - on_disk:
        problems.append("file_set_mismatch")
    assert writable == 0, f"{writable} writable paths"
    return {
        "verified_after_lock": not problems,
        "problems": problems,
        "writable_paths": writable,
        "manifest_file_count": len(manifest["files"]),
        "manifest_total_bytes": sum(f["bytes"] for f in manifest["files"]),
        "revision": manifest["revision"],
        "licence": manifest["licence"],
    }


def overlap(task_ids: set[str], run_ids: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sets: dict[str, dict[str, set[str]]] = {
        "dev": {},
        "test": {},
        "other_split": {},
        "cloud_store": {},
    }
    for mode in ("bare", "full"):
        for k in sets:
            sets[k][mode] = set()
    with ASSIGNMENTS.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            split = (row["split"] or "").lower()
            bucket = (
                "dev"
                if split == "dev"
                else "test"
                if split in {"test", "heldout"}
                else "other_split"
            )
            for col in ("task", "item_id"):
                for mode in ("bare", "full"):
                    if row[col]:
                        sets[bucket][mode].add(sha256(normalise_id(row[col], mode).encode()))
    if CLOUD_DB.exists():
        con = sqlite3.connect(f"file:{CLOUD_DB}?mode=ro", uri=True)
        for (tid,) in con.execute("select task_id from corpus_tasks"):
            for mode in ("bare", "full"):
                sets["cloud_store"][mode].add(sha256(normalise_id(tid, mode).encode()))
        con.close()
    for mode in ("bare", "full"):
        mine = hashed(task_ids, mode)
        out[mode] = {k: len(mine & sets[k][mode]) for k in sets}
    out["run_ids_checked"] = len(run_ids)
    out["task_ids_checked"] = len(task_ids)
    out["run_id_full_hits"] = sum(len(hashed(run_ids, "full") & sets[k]["full"]) for k in sets)
    return out


def profile(root: Path = ROOT) -> dict[str, Any]:
    integrity = verify_bundle(root)
    states: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    by_task: dict[str, Counter[str]] = {}
    by_model: dict[str, Counter[str]] = {}
    kinds: Counter[str] = Counter()
    run_ids: set[str] = set()
    task_ids: set[str] = set()
    pos_tasks: dict[str, set[str]] = {}
    for stem, run in iter_runs(root):
        state, reason = label_state(run)
        states[state] += 1
        reasons[f"{state}:{reason}"] += 1
        run_ids.add(stem)
        task_ids.add(run["task_id"])
        by_task.setdefault(run["task_id"], Counter())[state] += 1
        by_model.setdefault(run["model"], Counter())[state] += 1
        if state == "positive":
            for f in run["score"]["findings"]:
                if f["severity"] == "cheat":
                    kinds[f["kind"]] += 1
            pos_tasks.setdefault(run["task_id"], set()).add(stem)
    check = selfcheck(root)
    assert check["leak_count"] == 0 and not check["issues"], check
    return {
        "repo": REPO,
        "revision": REVISION,
        "licence": LICENCE,
        "integrity": integrity,
        "predicate": {
            "unit": "runs/*.json top level only (archive/, calibration/, sweeps/ excluded)",
            "positive": "score.cheats non-empty (rule detector); slices: scored vs unscoreable",
            "negative": "score.status == scored and score.is_clean and score.cheats empty",
            "ambiguous": "everything else: unscoreable without a flag, missing score",
            "labels_are_rule_detector_output": True,
            "human_confirmed": False,
        },
        "counts": {
            "runs": sum(states.values()),
            "positive": states["positive"],
            "negative": states["negative"],
            "ambiguous": states["ambiguous"],
            "excluded": 0,
            "reasons": dict(sorted(reasons.items())),
            "by_task": {k: dict(v) for k, v in sorted(by_task.items())},
            "by_model": {k: dict(v) for k, v in sorted(by_model.items())},
            "positive_finding_kinds": dict(kinds),
            "tasks": len(task_ids),
        },
        "adapter_selfcheck": check,
        "overlap": overlap(task_ids, run_ids),
        "task_id_hashes": sorted(hashed(task_ids, "full")),
        "run_id_hashes": sorted(hashed(run_ids, "full")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--write", action="store_true")
    result = profile(parser.parse_args().root)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if parser.parse_args().write:
        OUTPUT.write_text(text, encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("counts", "overlap")}, indent=1)[:2500])


if __name__ == "__main__":
    main()
