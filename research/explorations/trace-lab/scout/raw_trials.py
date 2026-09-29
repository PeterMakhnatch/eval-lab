#!/usr/bin/env python3
"""List the 54 raw trial dirs matching the normalized manifests.

Prints one raw trial path per line: the 44 HAR-81 trials (resolved across
the dispatch-528 / dispatch-531 worktrees) plus the 10 HAR-90 trials.
"""

from __future__ import annotations

import json
from pathlib import Path

TRACE_LAB = Path(__file__).resolve().parent.parent
R528 = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs")
R531 = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs")
R90 = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs")


def main() -> None:
    trials = []
    man81 = json.loads((TRACE_LAB / "normalized" / "har81" / "manifest.json").read_text())
    man90 = json.loads((TRACE_LAB / "normalized" / "har90" / "manifest.json").read_text())
    for t in man81["trials"]:
        for base in (R528, R531):
            p = base / t["job"] / t["trial"]
            if p.is_dir():
                trials.append(p)
                break
        else:
            raise SystemExit(f"raw trial dir missing: {t['job']}/{t['trial']}")
    for t in man90["trials"]:
        p = R90 / t["job"] / t["trial"]
        if not p.is_dir():
            raise SystemExit(f"raw trial dir missing: {t['job']}/{t['trial']}")
        trials.append(p)
    print(f"resolved {len(trials)} raw trial dirs", flush=True)
    for p in trials:
        print(p)


if __name__ == "__main__":
    main()
