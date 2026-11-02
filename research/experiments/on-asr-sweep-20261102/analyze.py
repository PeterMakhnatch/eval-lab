#!/usr/bin/env python3
"""K7 analysis: ASR per variant, paired McNemar, and the exclusion list, from ``results.jsonl``.

    analyze.py            # table + headline numbers
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight")
import stats  # noqa: E402

ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/K7")


def load() -> dict[str, dict]:
    """Best record per task: a complete record wins over an aborted one."""
    best: dict[str, dict] = {}
    path = ROOT / "results.jsonl"
    for line in path.read_text().splitlines() if path.is_file() else []:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        current = best.get(rec["task_id"])
        if current is None or (rec.get("complete") and not current.get("complete")):
            best[rec["task_id"]] = rec
    return best


def main() -> int:
    best = load()
    l0: list[bool] = []
    hs: list[bool] = []
    usable: list[str] = []
    excluded: list[tuple[str, str]] = []
    for task_id, rec in sorted(best.items()):
        l_reward = (rec.get("L0") or {}).get("reward")
        h_reward = (rec.get("HS") or {}).get("reward")
        if not rec.get("complete") or l_reward is None or h_reward is None:
            reason = rec.get("reason") or (rec.get("L0") or {}).get("reason") or "no reward"
            excluded.append((task_id, str(reason).splitlines()[0][:90]))
            continue
        usable.append(task_id)
        l0.append(l_reward == 1.0)
        hs.append(h_reward == 1.0)
    print(f"usable pairs: {len(usable)} | excluded: {len(excluded)}")
    print(f"ASR L0: {stats.fmt(sum(l0), len(l0))}")
    print(f"ASR HS: {stats.fmt(sum(hs), len(hs))}")
    print(f"paired: {stats.paired(l0, hs)}")
    print("\nper package:")
    for task_id, a, b in zip(usable, l0, hs, strict=True):
        print(f"  {task_id}  L0={int(a)}  HS={int(b)}")
    print("\nexcluded:")
    for task_id, reason in excluded:
        print(f"  {task_id}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
