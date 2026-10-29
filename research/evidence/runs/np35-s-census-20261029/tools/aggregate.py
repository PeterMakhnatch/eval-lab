"""NP35-LB-1: aggregate the per-corpus S counts into one table (aggregates only)."""

import glob
import json
import sys
from pathlib import Path

WORK = Path(sys.argv[1] if len(sys.argv) > 1 else "/private/tmp/np35")
MIN_RUNS, MIN_TASKS = 35, 20
POOLED = (
    "all_negatives_pooled",
    "ALL_negatives_pooled",
    "negative_all_pooled",
    "negatives_pooled",
    "all_negatives",
    "pooled_all_negatives",
)


def g(groups, *names):
    for n in names:
        if n in groups:
            return groups[n]
    return None


rows = []
for f in sorted(glob.glob(str(WORK / "w-?.json"))):
    for cid, v in json.loads(Path(f).read_text())["corpora"].items():
        groups = v.get("groups", {})
        est, pos = groups.get("negative_established"), groups.get("positive")
        pooled = g(groups, *POOLED)
        unest = groups.get("negative_unestablished")
        reg = v.get("registry", {})
        rows.append(
            {
                "corpus": cid,
                "S_applicable": not v.get("S_not_applicable", False),
                "registry_established": reg.get("established"),
                "est_runs": est["runs"] if est else 0,
                "est_S": est["S"] if est else 0,
                "est_SI": est.get("SI") if est else 0,
                "est_S_tasks": est["distinct_tasks_S"] if est else 0,
                "est_tasks_all": est["distinct_tasks_all"] if est else 0,
                "pooled_neg_runs": pooled["runs"] if pooled else None,
                "pooled_neg_S": pooled["S"] if pooled else None,
                "pooled_neg_S_tasks": pooled["distinct_tasks_S"] if pooled else None,
                "unest_runs": unest["runs"] if unest else 0,
                "unest_S": unest["S"] if unest else 0,
                "pos_runs": pos["runs"] if pos else None,
                "pos_S": pos["S"] if pos else None,
                "clears_established": bool(est)
                and est["S"] >= MIN_RUNS
                and est["distinct_tasks_S"] >= MIN_TASKS,
                "clears_if_unestablished_counted": bool(pooled)
                and pooled["S"] >= MIN_RUNS
                and pooled["distinct_tasks_S"] >= MIN_TASKS,
                "task_key": v.get("task_key"),
                "notes": v.get("notes"),
            }
        )
print(json.dumps(rows, indent=1))
