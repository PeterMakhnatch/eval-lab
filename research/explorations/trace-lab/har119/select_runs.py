"""HAR-119 part 2: pick the 12 HAR-110 v2 runs for blind hand labels.

Rule (fixed before any rater or tool output exists):
- population: the 24 fresh HAR-110 split-v2 runs (dev + held-out) in
  research/experiments/har110-python-gepa/results-v2-trials.jsonl
  (source == "HAR-110", split in {development, heldout});
- 4 runs per arm (plain, seed-addendum, gepa-candidate);
- every verifier pass is included (seed 001832 and candidate 002864);
- the rest of each arm's quota is filled by ascending
  sha256("har119-oos-v1:" + trial name).
plain has exactly 4 v2 runs (held-out), so all four are in.

Usage: python select_runs.py <trial dirs file> > selection.json
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
TRIALS = ROOT / "research/experiments/har110-python-gepa/results-v2-trials.jsonl"
SALT = "har119-oos-v1:"
PER_ARM = 4


def main() -> int:
    dirs = [Path(line) for line in Path(sys.argv[1]).read_text().split() if line]
    by_job = {d.parent.name: d for d in dirs}
    rows = []
    for row in map(json.loads, TRIALS.read_text().splitlines()):
        if row["source"] != "HAR-110" or row["split"] not in {"development", "heldout"}:
            continue
        trial_dir = by_job[row["job"]]
        rows.append({**row, "trial": trial_dir.name, "trial_dir": str(trial_dir)})
    picked = []
    for arm in ("plain", "seed-addendum", "gepa-candidate"):
        pool = [r for r in rows if r["arm"] == arm]
        passes = [r for r in pool if r["reward"] == 1.0]
        rest = sorted(
            (r for r in pool if r["reward"] != 1.0),
            key=lambda r: hashlib.sha256((SALT + r["trial"]).encode()).hexdigest(),
        )
        picked += passes + rest[: PER_ARM - len(passes)]
    out = {
        "rule": __doc__.split("Usage:")[0].strip(),
        "population": len(rows),
        "runs": [
            {k: r[k] for k in ("trial", "job", "arm", "split", "task", "reward", "trial_dir")}
            for r in picked
        ],
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
