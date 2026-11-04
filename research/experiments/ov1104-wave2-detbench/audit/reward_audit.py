"""Audit recorded reward availability and its relation to labels.

Reads only the admitted DetBench run manifest and its derived views; writes
nothing outside this directory. Deterministic, no model calls.
"""

import collections
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path("/private/tmp/ov1104/shared/detbench")
MANIFEST = ROOT / "runs.jsonl"


def reward_of(view_path: str | None) -> float | None:
    if not view_path:
        return None
    try:
        view = json.loads(Path(view_path).read_text())
    except (OSError, ValueError):
        return "unreadable"
    return (view.get("final") or {}).get("reward")


def main() -> None:
    rows = [json.loads(line) for line in MANIFEST.open() if line.strip()]
    digest = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    per_set = collections.defaultdict(collections.Counter)
    rewards = {}
    for row in rows:
        reward = reward_of(row.get("view"))
        rewards[row["run_key"]] = reward
        key = (row["set"], row["split"])
        per_set[key]["runs"] += 1
        per_set[key]["reward_present" if isinstance(reward, (int, float)) else "reward_absent"] += 1
        if isinstance(reward, (int, float)):
            per_set[key][f"reward={reward}|label={row['label']}"] += 1

    # Sets that record a reward and have both labels among reward==1 runs.
    passing_evaluable = []
    for set_id, split in {k for k in per_set}:
        counts = per_set[(set_id, split)]
        cheat = counts.get("reward=1.0|label=1", 0)
        honest = counts.get("reward=1.0|label=0", 0)
        if cheat and honest:
            passing_evaluable.append(
                {"set": set_id, "split": split, "passing_cheats": cheat, "passing_honest": honest}
            )

    # Where a reward exists, is it independent of the label?
    independence = {}
    for (set_id, split), counts in sorted(per_set.items()):
        present = counts["reward_present"]
        if not present:
            continue
        pairs = {k: v for k, v in counts.items() if k.startswith("reward=")}
        total = sum(pairs.values())
        cheat_pass = counts.get("reward=1.0|label=1", 0)
        cheat_fail = counts.get("reward=0.0|label=1", 0)
        honest_pass = counts.get("reward=1.0|label=0", 0)
        honest_fail = counts.get("reward=0.0|label=0", 0)
        # Perfect inverse association: every reward==1 row honest and every cheat row reward!=1.
        independence[set_id] = {
            "split": split,
            "rows_with_reward": present,
            "rows_scored": total,
            "reward1_honest": honest_pass,
            "reward1_cheat": cheat_pass,
            "reward0_honest": honest_fail,
            "reward0_cheat": cheat_fail,
            "label_is_inverse_of_reward": bool(total)
            and cheat_pass == 0
            and honest_fail == 0
            and cheat_fail > 0,
        }

    out = {
        "manifest_sha256": digest,
        "total_runs": len(rows),
        "sets_with_reward": sum(1 for c in per_set.values() if c["reward_present"]),
        "passing_sets_with_both_classes": passing_evaluable,
        "reward_label_association": independence,
        "per_set_counts": {f"{k[0]}:{k[1]}": dict(v) for k, v in sorted(per_set.items())},
    }
    (HERE / "reward-audit.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                k: out[k]
                for k in ("total_runs", "sets_with_reward", "passing_sets_with_both_classes")
            },
            indent=2,
        )
    )
    for set_id, info in independence.items():
        print(
            set_id,
            info["split"],
            info["rows_with_reward"],
            "label_is_inverse_of_reward",
            info["label_is_inverse_of_reward"],
        )


if __name__ == "__main__":
    main()
