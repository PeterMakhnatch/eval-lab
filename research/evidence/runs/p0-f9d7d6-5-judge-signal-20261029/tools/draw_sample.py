"""Seeded stratified draw for P0-SELF-F9D7D6-5 (label use limited to filling the stratum quota).

Population: strict-gate ``goodfire-dev`` items with an enacted-or-clean label (`goodfire_dev_items`), minus the 246 runs of
the 123 NP31 matched pairs and minus the 150 Goodfire sample runs (`load_corpus_labels('goodfire')` ids). Strata: length
tertile (NP31 cuts 40 and 89: stratum = 1 + #(cuts < steps)) x label. Quota 100 per cell.

Task rule (applied to the whole sample, task = `agent_traces.task_id`): cells are filled in the order
(1,enacted) (1,clean) (2,enacted) (2,clean) (3,enacted) (3,clean); within a cell candidates are ordered by
sha256(f"{SEED}:{run_id}"); a candidate is skipped while its task already has TASK_CAP=2 runs in the sample; if a cell
is short after one pass, the skipped candidates are taken in the same hash order with the cap raised to 3, 4, ... until
the quota is met or the cell is exhausted (the raise is reported). A cell with fewer than 100 eligible runs takes all.

Writes ``sample.jsonl`` (run id, stratum, steps; NO label, NO task) for the reader, and prints cell sizes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")

from evallab.production.trace_harness import DEFAULT_DATABASE  # noqa: E402
from evallab.production.trace_harness_corpora import load_corpus_labels  # noqa: E402
from evallab.production.trace_harness_matched import goodfire_dev_items  # noqa: E402

SEED = 20261029
QUOTA = 100
CUTS = (40, 89)
TASK_CAP = 2
PAIRS = Path.home() / ".local/state/evallab-prod-live/trace-harness/np31-length-matched/goodfire-dev-pairs.jsonl"


def stratum(length: int) -> int:
    return 1 + sum(1 for cut in CUTS if cut < length)


def digest(run_id: str) -> str:
    return hashlib.sha256(f"{SEED}:{run_id}".encode()).hexdigest()


def main(out: Path) -> None:
    repo = Path.cwd()
    items = goodfire_dev_items(database=DEFAULT_DATABASE, repo_root=repo)
    pair_runs = set()
    for line in PAIRS.read_text().splitlines():
        pair = json.loads(line)
        pair_runs |= {pair["enacted"], pair["clean"]}
    labels = load_corpus_labels("goodfire", database=DEFAULT_DATABASE, repo_root=repo)
    g150 = {r for r in labels.run_ids if labels.positive.get(r) is not None}
    eligible = [i for i in items if i.run_id not in pair_runs and i.run_id not in g150]
    report = {"gate_items": len(items), "pair_runs": len(pair_runs), "g150_runs": len(g150),
              "pair_runs_in_gate": len(pair_runs & {i.run_id for i in items}),
              "g150_in_gate": len(g150 & {i.run_id for i in items}), "eligible": len(eligible), "cells": {}}
    tasks: Counter[str] = Counter()
    chosen: list[tuple[int, bool, object]] = []
    for s in (1, 2, 3):
        for label in (True, False):
            pool = sorted((i for i in eligible if stratum(i.length) == s and i.label == label), key=lambda i: digest(i.run_id))
            taken: list = []
            leftover = list(pool)
            cap = TASK_CAP
            while leftover and len(taken) < QUOTA:
                skipped = []
                for item in leftover:
                    if len(taken) < QUOTA and tasks[item.cluster] < cap:
                        taken.append(item)
                        tasks[item.cluster] += 1
                    else:
                        skipped.append(item)
                leftover = skipped
                if len(taken) < QUOTA and leftover:
                    cap += 1
            report["cells"][f"t{s}-{'enacted' if label else 'clean'}"] = {
                "eligible": len(pool), "drawn": len(taken), "task_cap_used": cap}
            chosen += [(s, label, i) for i in taken]
    report["drawn"] = len(chosen)
    report["max_runs_per_task"] = max(tasks.values())
    report["tasks"] = len(tasks)
    report["tasks_over_two"] = sum(1 for v in tasks.values() if v > 2)
    rows = sorted(({"run_id": i.run_id, "stratum": s, "steps": i.length} for s, _label, i in chosen), key=lambda r: digest(r["run_id"]))
    out.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    report["ids_sha256"] = hashlib.sha256("\n".join(sorted(r["run_id"] for r in rows)).encode()).hexdigest()
    print(json.dumps(report, indent=1, sort_keys=True))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
