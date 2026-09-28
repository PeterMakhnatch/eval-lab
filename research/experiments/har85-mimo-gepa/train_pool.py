"""HAR-85 train pool: provisional split train ids minus recorded exclusions.

The split manifest stays frozen (its digest is pinned by make_paired_specs.py
and bound into the DSPy approvals). A train task whose grader cannot score an
honest solution is excluded beside it, in ``train-exclusions.json``, with the
reason and the evidence, so the pool both arms draw from is auditable:

    pool_digest = "sha256:" + sha256(json.dumps(
        [{"task_id": ..., "task_package_digest": ...} for each pool row,
         sorted by task_id], sort_keys=True, separators=(",", ":")))

Usage (stdlib only, except ``materialize``, which needs the Lab env):

    python train_pool.py digest                   # count + digest; refuses on drift
    python train_pool.py check-campaign PATH      # every example in the pool, split digest
    uv run --no-sync python train_pool.py materialize --src DIR   # copy the pool into tasks/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

EXPERIMENT_DIR = Path(__file__).resolve().parent
SPLIT_PATH = EXPERIMENT_DIR / "split.provisional.json"
EXCLUSIONS_PATH = EXPERIMENT_DIR / "train-exclusions.json"
TASKS_DIR = EXPERIMENT_DIR / "tasks"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is unreadable: {exc}") from exc


def excluded_rows(split: dict, exclusions: dict) -> dict[str, dict]:
    """Excluded train rows by task id; each must name a train task's exact bytes."""
    if exclusions.get("split_manifest_digest") != split.get("manifest_digest"):
        raise ValueError(
            "train-exclusions.json was recorded against split "
            f"{exclusions.get('split_manifest_digest')}, not {split.get('manifest_digest')}: "
            "re-review the exclusions for the new split"
        )
    rows = {row["task_id"]: row for row in split["tasks"]}
    excluded: dict[str, dict] = {}
    for entry in exclusions["excluded"]:
        task_id = entry["task_id"]
        row = rows.get(task_id)
        if row is None or row["assignment"] != "train":
            raise ValueError(f"excluded task {task_id} is not a train task of this split")
        if entry["task_package_digest"] != row["task_package_digest"]:
            raise ValueError(f"excluded task {task_id} names different bytes than the split")
        if not entry.get("reason") or not entry.get("evidence"):
            raise ValueError(f"excluded task {task_id} lacks a recorded reason and evidence")
        excluded[task_id] = entry
    return excluded


def pool_rows(split: dict, exclusions: dict) -> list[dict]:
    excluded = excluded_rows(split, exclusions)
    return sorted(
        (
            {"task_id": row["task_id"], "task_package_digest": row["task_package_digest"]}
            for row in split["tasks"]
            if row["assignment"] == "train" and row["task_id"] not in excluded
        ),
        key=lambda row: row["task_id"],
    )


def pool_digest(rows: list[dict]) -> str:
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def load_pool() -> tuple[dict, dict, list[dict]]:
    split = load_json(SPLIT_PATH)
    exclusions = load_json(EXCLUSIONS_PATH)
    rows = pool_rows(split, exclusions)
    recorded = exclusions["train_pool"]
    if recorded["count"] != len(rows) or recorded["digest"] != pool_digest(rows):
        raise ValueError(
            f"recorded train pool ({recorded['count']}, {recorded['digest']}) does not match "
            f"the split minus exclusions ({len(rows)}, {pool_digest(rows)})"
        )
    return split, exclusions, rows


def check_campaign(campaign_path: Path, rows: list[dict], split: dict) -> None:
    pool = {row["task_id"]: row["task_package_digest"] for row in rows}
    heldout = set(split["heldout_task_ids"])
    campaign = load_json(campaign_path)
    for example in campaign["examples"]:
        task_id = example["task_id"]
        if task_id in heldout:
            raise ValueError(f"held-out task {task_id} must never feed the search")
        if task_id not in pool:
            raise ValueError(f"{task_id} is not in the train pool (excluded or unknown)")
        if example["task_package_digest"] != pool[task_id]:
            raise ValueError(f"{task_id} digest differs from the split manifest")


def materialize(src: Path, rows: list[dict], excluded: dict[str, dict]) -> None:
    from evallab.registry import task_directory_digest

    TASKS_DIR.mkdir(exist_ok=True)
    for row in rows:
        target = TASKS_DIR / row["task_id"]
        if not target.exists():
            shutil.copytree(src / row["task_id"], target)
        if task_directory_digest(target) != row["task_package_digest"]:
            raise ValueError(f"materialized {row['task_id']} does not match the split digest")
    for task_id in excluded:
        stale = TASKS_DIR / task_id
        if stale.exists():
            shutil.rmtree(stale)
            print(f"removed excluded task copy {stale.relative_to(EXPERIMENT_DIR)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("digest")
    check = sub.add_parser("check-campaign")
    check.add_argument("campaign", type=Path)
    mat = sub.add_parser("materialize")
    mat.add_argument("--src", type=Path, required=True, help="pinned MiMo terminal tasks dir")
    args = parser.parse_args(argv)
    try:
        split, exclusions, rows = load_pool()
        if args.command == "check-campaign":
            check_campaign(args.campaign, rows, split)
            print(f"campaign ok: every example is in the {len(rows)}-task train pool")
        elif args.command == "materialize":
            materialize(args.src, rows, excluded_rows(split, exclusions))
            print(f"train pool materialized: {len(rows)} tasks, digests match the split")
        excluded = ", ".join(entry["task_id"] for entry in exclusions["excluded"])
        print(f"train pool: {len(rows)} tasks, {pool_digest(rows)} (excluded: {excluded})")
    except ValueError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
