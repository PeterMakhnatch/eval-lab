"""Deterministic design / held-out split of the frozen 123 pairs, by task, stratified by length tertile. Label-free.

Pair length m = (enacted_length + clean_length) / 2 (both lengths are stored step counts). Tertile cut points are nearest
rank over the 123 pair lengths, ``sorted(m)[ceil(j*N/3) - 1]`` for j = 1, 2. A task's stratum is the tertile of the mean
of its pairs' m. Inside each stratum tasks are ordered by ``sha256("np36-tw1|" + task)`` and alternate design, held-out,
design, ... starting with design. All pairs of a task go to the same half.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import data

RESULTS = Path(__file__).resolve().parent.parent / "results"


def split() -> dict:
    pairs = data.pair_rows()
    length = [(p["enacted_length"] + p["clean_length"]) / 2 for p in pairs]
    ordered = sorted(length)
    n = len(ordered)
    cuts = [ordered[math.ceil(j * n / 3) - 1] for j in (1, 2)]

    def stratum(value: float) -> int:
        return 1 + sum(value > cut for cut in cuts)

    by_task: dict[str, list[float]] = defaultdict(list)
    for pair, value in zip(pairs, length, strict=True):
        by_task[pair["task"]].append(value)
    task_stratum = {task: stratum(sum(values) / len(values)) for task, values in by_task.items()}
    half: dict[str, str] = {}
    for level in (1, 2, 3):
        tasks = sorted(
            (t for t, s in task_stratum.items() if s == level),
            key=lambda t: hashlib.sha256(("np36-tw1|" + t).encode()).hexdigest(),
        )
        for position, task in enumerate(tasks):
            half[task] = "design" if position % 2 == 0 else "heldout"
    table: dict[str, dict[str, int]] = {}
    for pair in pairs:
        key = f"{half[pair['task']]}/stratum{task_stratum[pair['task']]}"
        table.setdefault(key, {"pairs": 0})["pairs"] += 1
    return {
        "rule": __doc__.split("\n\n")[0],
        "cuts": cuts,
        "pairs": n,
        "tasks": len(by_task),
        "half_of_task": half,
        "pairs_per_half": {
            h: sum(1 for p in pairs if half[p["task"]] == h) for h in ("design", "heldout")
        },
        "tasks_per_half": {
            h: sum(1 for v in half.values() if v == h) for h in ("design", "heldout")
        },
        "pairs_by_half_and_task_stratum": dict(sorted(table.items())),
    }


if __name__ == "__main__":
    result = split()
    RESULTS.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(json.dumps(result["half_of_task"], sort_keys=True).encode()).hexdigest()
    public = {k: v for k, v in result.items() if k != "half_of_task"} | {
        "half_of_task_sha256": digest
    }
    (RESULTS / "split.json").write_text(json.dumps(public, indent=1, sort_keys=True) + "\n")
    print({k: v for k, v in public.items() if k != "rule"})
