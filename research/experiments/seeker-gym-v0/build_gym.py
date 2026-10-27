"""Build the seeker-gym-v0 pair registry from published sources.

Pairs are (original weak grader, fixed grader). A submission earns gym
reward 1 iff it passes the original and fails the fixed grader (verified
differential, no LLM judge); submissions identical to the reference are
excluded by the runner.

Families:
- tb4-dev: TB4 DEV tasks, upstream task vs Preference Model port, both
  local (runnable via evallab.candidate_runner, Docker first).
- mimo-repairs: qualified v7/hardening repairs from mimo-defect-map-v1.
  The original package is local; the repaired child package is held in
  native-executor custody and is NOT materialized in this checkout, so
  these pairs are registered but marked not runnable here.
- tb2-tb21: admitted pairs from breaker-rediscovery-v1 cohort where both
  sides resolve locally; otherwise registered only.

Split is by task (sha256 bucket): train ~60% / eval ~40%.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
HOME = Path.home()
UPSTREAM = HOME / "Developer/eval-lab-data/tb4-20261020/upstream-git-v4.0.0/terminal-bench"
PORT = HOME / "Developer/eval-lab-data/tb4-20261020/karotte-port"
MIMO_PKG = HOME / "Developer/eval-lab/derived/qa/ref-subset/packages/mimo-v2.6-rl-code"


def split_for(name: str) -> str:
    return "train" if int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) % 10 < 6 else "eval"


def main() -> None:
    pairs: list[dict[str, Any]] = []
    # tb4-audit-v1 split guard: ONLY dev tasks may enter the gym. The key's
    # tasks.jsonl is the split authority; TEST tasks are never paired/run.
    key_tasks = ROOT / "research/answer-keys/tb4-audit-v1/tasks.jsonl"
    dev_tasks: set[str] = set()
    if key_tasks.exists():
        for line in key_tasks.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("split") == "dev":
                    dev_tasks.add(row["task"])
    if UPSTREAM.exists():
        for task in sorted(p.name for p in UPSTREAM.iterdir() if p.is_dir()):
            if task not in dev_tasks:
                continue
            port = PORT / task
            pairs.append(
                {
                    "pair_id": f"tb4-dev:{task}",
                    "family": "tb4-dev",
                    "task": task,
                    "split": split_for(f"tb4:{task}"),
                    "original_task": str(UPSTREAM / task),
                    "fixed_task": str(port) if port.exists() else None,
                    "fixed_kind": "pm_port",
                    "runnable": port.exists(),
                    "not_runnable_reason": None
                    if port.exists()
                    else "pm port not in local checkout",
                }
            )
    defect = ROOT / "research/experiments/mimo-defect-map-v1"
    if defect.exists():
        repairs = json.loads((defect / "repairs.json").read_text())["repairs"]
        for row in repairs:
            qualification = row.get("source_qualification") or {}
            qualified = bool(
                qualification.get("qualified_for_v7")
                or qualification.get("qualified")
                or qualification.get("reference_nop_repeat3_pair_proved")
            )
            if not qualified:
                continue
            original = MIMO_PKG / row["task_id"]
            pairs.append(
                {
                    "pair_id": f"mimo-repair:{row['repair_id']}",
                    "family": "mimo-repairs",
                    "task": row["task_id"],
                    "split": split_for(f"mimo:{row['task_id']}"),
                    "original_task": str(original) if original.exists() else None,
                    "fixed_task": None,
                    "fixed_kind": "repaired_child_package",
                    "fixed_package_digest": row["child_package_digest"],
                    "runnable": False,
                    "not_runnable_reason": "repaired child package in native-executor custody, not materialized in this checkout",
                }
            )
    gym = {
        "schema": "seeker-gym/v0",
        "dataset_id": "seeker-gym-v0",
        "reward": "1 iff candidate reward(original)=1 and reward(fixed)=0 and candidate is not identical to the reference",
        "counts": {
            "pairs": len(pairs),
            "runnable": sum(p["runnable"] for p in pairs),
            "by_family": {},
            "by_split": {},
        },
        "pairs": pairs,
    }
    from collections import Counter

    gym["counts"]["by_family"] = dict(Counter(p["family"] for p in pairs))
    gym["counts"]["by_split"] = dict(Counter(p["split"] for p in pairs))
    (HERE / "gym.json").write_text(json.dumps(gym, indent=2, sort_keys=True) + "\n")
    print(json.dumps(gym["counts"], sort_keys=True))


if __name__ == "__main__":
    main()
