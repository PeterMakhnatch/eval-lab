#!/usr/bin/env python3
"""Build ``cohort.json`` for breaker-bench-heldout-v1 from the `breaker-bench-v1` answer key.

Cohort = test-split benchflow pairs whose upstream package is stageable on this machine
(``run_dev_pairs.resolve_upstream_task_dir``) plus the five staged Terminal Wrench tasks of
``auditor-sft-v1/holdout_manifest.json``. Pairs the key lists but this machine cannot stage are
recorded under ``excluded_unstageable`` with the reason; nothing is silently dropped.

    uv run python research/experiments/breaker-bench-heldout-v1/build_cohort.py          # write
    uv run python research/experiments/breaker-bench-heldout-v1/build_cohort.py --check  # verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import run_heldout as rh

KEY_DIR = rh.REPO_ROOT / "research" / "answer-keys" / rh.KEY_ID
HOLDOUT_MANIFEST = rh.EXPERIMENTS_DIR / "auditor-sft-v1" / "holdout_manifest.json"
TW_SUBSPLIT = "tw_holdout_5_staged"
SPLIT = "test"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def build() -> dict[str, Any]:
    pairs = _read_jsonl(KEY_DIR / "pairs.jsonl")
    items = {row["item_id"]: row for row in _read_jsonl(KEY_DIR / "items.jsonl")}

    cohort_pairs: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for pair in sorted(
        (p for p in pairs if p["split"] == SPLIT), key=lambda p: (p["benchmark"], p["task_id"])
    ):
        slug = pair["task_id"].split("/", 1)[1]
        child, parent = items[pair["child"]], items[pair["parent"]]
        if (child["sandbox_version"], parent["sandbox_version"]) != ("0.2.0", "0.2.2"):
            raise ValueError(f"{pair['pair_id']}: child/parent are not 0.2.0/0.2.2 items")
        if child["split"] != SPLIT or parent["split"] != SPLIT:
            raise ValueError(f"{pair['pair_id']}: pair straddles splits")
        try:
            rh.dev.resolve_upstream_task_dir(pair["benchmark"], slug)
        except FileNotFoundError as exc:
            excluded.append(
                {
                    "pair_id": pair["pair_id"],
                    "benchmark": pair["benchmark"],
                    "slug": slug,
                    "reason": f"no local upstream package ({exc})",
                }
            )
            continue
        cohort_pairs.append(
            {
                "pair_id": pair["pair_id"],
                "benchmark": pair["benchmark"],
                "slug": slug,
                "mechanism": pair["mechanism"],
                "category": pair["category"],
                "timeout_seconds": rh.DEFAULT_TIMEOUT_SECONDS,
                "old_item_id": child["item_id"],
                "old_subject_task_id": child["subject"]["task_id"],
                "fixed_item_id": parent["item_id"],
                "fixed_subject_task_id": parent["subject"]["task_id"],
            }
        )

    manifest = json.loads(HOLDOUT_MANIFEST.read_text(encoding="utf-8"))
    frozen = {t["task_id"]: t for t in manifest["tasks"]}
    tw_tasks: list[dict[str, Any]] = []
    for item in sorted(
        (i for i in items.values() if i.get("subsplit") == TW_SUBSPLIT and i["split"] == SPLIT),
        key=lambda i: i["subject"]["task_id"],
    ):
        task_id = item["subject"]["task_id"].split("/", 1)[1]
        task = frozen.get(task_id)
        if task is None or task["task_package_digest"] != item["subject"]["task_digest"]:
            raise ValueError(
                f"{task_id}: key item and holdout manifest disagree on the package digest"
            )
        tw_tasks.append(
            {
                "task_id": task_id,
                "item_id": item["item_id"],
                "subject_task_id": item["subject"]["task_id"],
                "source_benchmark": item["benchmark"],
                "expected_category": item["expected_category"],
                "expected_root_cause": item["expected_root_cause"],
                "expected_mechanisms": item["expected_mechanisms"],
                "cache": item["staged_cache"],
                "package_digest": task["task_package_digest"],
                "timeout_seconds": rh.DEFAULT_TIMEOUT_SECONDS,
            }
        )
    if len(tw_tasks) != len(frozen):
        raise ValueError(
            f"expected {len(frozen)} staged TW tasks in the key, found {len(tw_tasks)}"
        )

    by_benchmark: dict[str, int] = {}
    for pair in cohort_pairs:
        by_benchmark[pair["benchmark"]] = by_benchmark.get(pair["benchmark"], 0) + 1
    return {
        "schema": rh.COHORT_SCHEMA,
        "experiment_id": rh.EXPERIMENT_ID,
        "answer_key_id": rh.KEY_ID,
        "split": SPLIT,
        "model": "openai/gpt-6-luna",
        "agent_import_path": "evallab.harbor_codex:PinnedCodex",
        "max_attempts": rh.MAX_ATTEMPTS,
        "arms": list(rh.ARMS),
        "ladder_version": rh.LADDER_VERSION,
        "source": {
            "pairs_sha256": _sha256(KEY_DIR / "pairs.jsonl"),
            "items_sha256": _sha256(KEY_DIR / "items.jsonl"),
            "holdout_manifest_sha256": _sha256(HOLDOUT_MANIFEST),
        },
        "counts": {
            "test_pairs_in_key": sum(1 for p in pairs if p["split"] == SPLIT),
            "pairs_in_cohort": len(cohort_pairs),
            "pairs_in_cohort_by_benchmark": dict(sorted(by_benchmark.items())),
            "pairs_excluded_unstageable": len(excluded),
            "terminal_wrench_tasks": len(tw_tasks),
        },
        "pairs": cohort_pairs,
        "terminal_wrench": tw_tasks,
        "excluded_unstageable": excluded,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", type=Path, default=rh.DEFAULT_COHORT_PATH)
    ap.add_argument(
        "--check", action="store_true", help="fail when --out differs from the rebuilt cohort"
    )
    args = ap.parse_args()
    rendered = json.dumps(build(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.out.is_file() or args.out.read_text(encoding="utf-8") != rendered:
            print(f"{args.out} is stale: rerun build_cohort.py", file=sys.stderr)
            return 1
        print(f"{args.out} is current")
        return 0
    args.out.write_text(rendered, encoding="utf-8")
    counts = json.loads(rendered)["counts"]
    print(f"wrote {args.out}: {json.dumps(counts, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
