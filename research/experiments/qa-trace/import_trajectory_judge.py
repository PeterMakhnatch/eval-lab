#!/usr/bin/env python3
"""Import trajectory-judge (arXiv 2609.00038, MIT) as a ``qa.answer_key/v1`` trace key.

Reads a clone of https://github.com/mohammadi-hadi/trajectory-judge at the pinned
commit and writes ``research/answer-keys/trajectory-judge/``:

- ``items.jsonl``: one item per trajectory (400): clean runs expect ``pass``,
  injected faults expect ``fail`` with ``failure_cause`` / fault ``agent``;
  ``extra`` carries the exact upstream labels (step, type, outcome survival),
  the stratum (``clean`` | ``silent`` | ``loud``) and the clean parent.
- ``trajectories.jsonl``: the upstream trajectories, byte-identical.
- ``baseline_verdicts.jsonl``: the paper's five judges' committed verdicts,
  reduced to the fields the scorer reads.
- ``SOURCE.json``: commit and sha256 of every upstream file used.

Split: task instances shuffled with seed ``qa-trace-20261014``; the first 10
instances (40 trajectories) are ``dev`` (prompt iteration only), the other 90
are ``heldout`` (report only). Pairs never straddle the split.

Usage:
  git clone https://github.com/mohammadi-hadi/trajectory-judge /tmp/tj-src
  git -C /tmp/tj-src checkout 056eb6c78012028dbc499841f6d89c8a29b91eb3
  uv run python research/experiments/qa-trace/import_trajectory_judge.py /tmp/tj-src
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
from pathlib import Path

COMMIT = "056eb6c78012028dbc499841f6d89c8a29b91eb3"
SEED = "qa-trace-20261014"
DEV_INSTANCES = 10
OUT = Path(__file__).resolve().parents[2] / "answer-keys" / "trajectory-judge"
BASELINE_FIELDS = (
    "trajectory_id",
    "judge_id",
    "faulty",
    "failure_step",
    "failure_type",
    "confidence",
    "error",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", type=Path, help="trajectory-judge clone at the pinned commit")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    head = subprocess.run(
        ["git", "-C", str(args.source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if head != COMMIT:
        raise SystemExit(f"{args.source} is at {head}, expected {COMMIT}")
    traj_path = args.source / "data" / "trajectories.jsonl"
    verdict_path = args.source / "data" / "verdicts.jsonl"
    rows = [json.loads(x) for x in traj_path.read_text().splitlines() if x.strip()]
    ids = {r["trajectory_id"] for r in rows}
    instances = sorted({r["instance_id"] for r in rows})
    random.Random(SEED).shuffle(instances)
    dev = set(instances[:DEV_INSTANCES])
    items = []
    for row in rows:
        label = row["label"]
        parent = f"{row['instance_id']}-clean"
        faulty = bool(label["faulty"])
        # 49 upstream faults have no clean parent in the set ("late pairs"); like
        # analysis/paired_ci.py they count toward recall but not toward pairs.
        has_parent = faulty and parent in ids
        stratum = "clean" if not faulty else ("silent" if label["outcome_correct"] else "loud")
        items.append(
            {
                "schema": "qa.answer_key/v1",
                "subject": {
                    "kind": "trace",
                    "task_id": row["instance_id"],
                    "task_digest": hashlib.sha256(row["goal"].encode()).hexdigest(),
                    "trial_id": row["trajectory_id"],
                },
                "expected_verdict": "fail" if faulty else "pass",
                "expected_category": "failure_cause" if faulty else None,
                "expected_fault": "agent" if faulty else None,
                "label_source": "trajectory-judge fault injection (exact by construction)",
                "split": "dev" if row["instance_id"] in dev else "heldout",
                "extra": {
                    "stratum": stratum,
                    "failure_step": label["failure_step"],
                    "failure_type": label["failure_type"],
                    "outcome_correct": label["outcome_correct"],
                    "parent_trial_id": parent if has_parent else None,
                },
            }
        )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "items.jsonl").write_text(
        "".join(json.dumps(i, sort_keys=True) + "\n" for i in items)
    )
    (args.out / "trajectories.jsonl").write_bytes(traj_path.read_bytes())
    baselines = [json.loads(x) for x in verdict_path.read_text().splitlines() if x.strip()]
    (args.out / "baseline_verdicts.jsonl").write_text(
        "".join(
            json.dumps({k: v.get(k) for k in BASELINE_FIELDS}, sort_keys=True) + "\n"
            for v in baselines
        )
    )
    (args.out / "SOURCE.json").write_text(
        json.dumps(
            {
                "repo": "https://github.com/mohammadi-hadi/trajectory-judge",
                "commit": COMMIT,
                "licence": "MIT",
                "files": {
                    "data/trajectories.jsonl": _sha(traj_path),
                    "data/verdicts.jsonl": _sha(verdict_path),
                },
                "split": {
                    "seed": SEED,
                    "dev_instances": sorted(dev),
                    "dev_items": sum(i["split"] == "dev" for i in items),
                },
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    counts: dict[tuple[str, str], int] = {}
    for item in items:
        key = (item["split"], item["extra"]["stratum"])
        counts[key] = counts.get(key, 0) + 1
    print(json.dumps({f"{s}/{k}": n for (s, k), n in sorted(counts.items())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
