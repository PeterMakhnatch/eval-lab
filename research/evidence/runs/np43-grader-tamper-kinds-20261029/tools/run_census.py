"""NP43-TW-1 step 1 (label-free): classify every ``grader_tamper`` alarm event of every population.

Reads only agent steps from the private caches; writes ``<state>/events.json`` = ``{population: {run key: run_events}}``.
Population membership uses only run ids (pair membership, cohort membership), never a class label.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import pickle
import sys
from pathlib import Path

import classify

STATE = Path.home() / ".local/state"
NP36 = STATE / "evallab-worker-evidence/np36-tw1-20261029"
NP38 = STATE / "evallab-np38-cm1"
PAIRS = STATE / "evallab-prod-live/trace-harness/np31-length-matched/goodfire-dev-pairs.jsonl"
OUT = STATE / "evallab-worker-evidence/np43-tw1-20261029"
RUNS: list[tuple[str, str, tuple]] = []


def one(index: int) -> tuple[str, str, dict]:
    population, key, steps = RUNS[index]
    return population, key, classify.run_events(list(steps))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    pair_ids = set()
    for line in PAIRS.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            pair_ids.update((row["enacted"], row["clean"]))
    with (NP36 / "pairs.pkl").open("rb") as handle:
        for rid, steps in pickle.load(handle).items():
            RUNS.append(("pairs", rid, tuple(steps)))
    with (NP36 / "goodfire_dev.pkl").open("rb") as handle:
        for rid, steps in pickle.load(handle).items():
            if rid not in pair_ids:
                RUNS.append(("goodfire_other", rid, tuple(steps)))
    with (NP36 / "cheatbench.pkl").open("rb") as handle:
        for rid, steps in pickle.load(handle).items():
            RUNS.append(("cheatbench", rid, tuple(steps)))
    for path in sorted((NP38 / "runs").glob("*.pkl")):
        with path.open("rb") as handle:
            RUNS.append(("linuxarena", path.stem, tuple(pickle.load(handle)["steps"])))
    counts = {
        p: sum(r[0] == p for r in RUNS)
        for p in ("pairs", "goodfire_other", "cheatbench", "linuxarena")
    }
    print("runs", counts, flush=True)
    out: dict[str, dict] = {}
    with mp.get_context("fork").Pool(8) as pool:
        for population, key, result in pool.imap_unordered(one, range(len(RUNS)), chunksize=8):
            out.setdefault(population, {})[key] = result
    (OUT / "events.json").write_text(json.dumps(out, sort_keys=True))
    print("wrote", sum(len(v) for v in out.values()), "runs", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
