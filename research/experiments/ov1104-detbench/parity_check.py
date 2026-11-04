"""Parity check: current-main L0 on the original replay-v1 rollouts vs on the DetBench views.

Usage (from the eval-lab worktree, after l0_rules.py --view full ran on the DEV rows):
  uv run python research/experiments/ov1104-detbench/parity_check.py

Prints, per corpus, how many of the 690 runs get the same L0 reason set from the original rollout
(`hack_drop._load_single_path_rollout`) and from the view (`l0v_full_raw.jsonl`), and the reason
codes that differ. A view that changes an L0 verdict is a view bug.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from detbench_lib import read_jsonl  # noqa: E402

REPLAY = Path.home() / "Developer/eval-lab-results/line/replay-v1"
RAW = Path("/private/tmp/ov1104/detbench/l0/l0v_full_raw.jsonl")
CORPORA = ("c1c5", "cheatbench", "goodfire", "sol-astra", "terminal-wrench")


def _one(args: tuple[str, str, str]) -> tuple[str, list[str]]:
    corpus, run_id, path = args
    from evallab import hack_drop as hd

    item = hd._load_single_path_rollout(Path(path))
    verdict = hd.evaluate_l0_stage(item)
    return f"{corpus}:{run_id}", sorted(verdict.reasons)


def main() -> int:
    jobs = []
    for corpus in CORPORA:
        for m in read_jsonl(REPLAY / "inputs/corpora" / f"{corpus}-manifest.jsonl"):
            jobs.append((corpus, m["id"], m["path"]))
    with ProcessPoolExecutor(max_workers=4) as pool:
        original = dict(pool.map(_one, jobs, chunksize=8))
    view = {r["run_key"]: sorted(r["reasons"]) for r in read_jsonl(RAW)}
    out: dict[str, dict[str, int | dict[str, int]]] = {}
    for corpus in CORPORA:
        keys = [k for k in original if k.startswith(corpus + ":")]
        same = sum(1 for k in keys if view.get(k) == original[k])
        diffs: Counter[str] = Counter()
        for k in keys:
            if view.get(k) != original[k]:
                for r in set(original[k]) ^ set(view.get(k, [])):
                    diffs[("view+" if r in view.get(k, []) else "orig+") + r] += 1
        out[corpus] = {"runs": len(keys), "same": same, "diff_reasons": dict(diffs)}
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
