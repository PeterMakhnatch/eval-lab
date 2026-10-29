"""Label-free: per run, does the current gate and each candidate raise an alarm? (steps only)

usage: compute.py <population> ...   reads ``<cache>/<population>.pkl``, writes ``<cache>/alarms-<population>.json``
"""

from __future__ import annotations

import json
import multiprocessing as mp
import pickle
import sys
from pathlib import Path

import gate

CACHE = Path.home() / ".local/state/evallab-worker-evidence/np36-tw1-20261029"
RUNS: dict = {}
IDS: list = []


def one(index: int) -> tuple[str, dict]:
    run_id = IDS[index]
    steps = RUNS[run_id]
    agent = gate.agent_steps_of(steps)
    hits = gate.current_hits(agent)
    row = {
        "length": len(steps),
        "replica_ok": gate.check(agent),
        "current": bool(hits),
        "current_hits": len(hits),
        "reasons": sorted({p["reason"] for h in hits for p in h["paths"]}),
    }
    for name, candidate in gate.CANDIDATES.items():
        kept = candidate(agent, hits) if hits else []
        row[name] = bool(kept)
        row[name + "_hits"] = len(kept)
    return run_id, row


def main() -> int:
    for population in sys.argv[1:]:
        RUNS.clear()
        RUNS.update(pickle.loads((CACHE / f"{population}.pkl").read_bytes()))
        IDS[:] = sorted(RUNS)
        with mp.get_context("fork").Pool(6) as pool:
            rows = dict(pool.map(one, range(len(IDS)), chunksize=8))
        (CACHE / f"alarms-{population}.json").write_text(json.dumps(rows, sort_keys=True))
        flagged = sum(r["current"] for r in rows.values())
        print(
            population,
            len(rows),
            "replica_ok",
            sum(r["replica_ok"] for r in rows.values()),
            "current",
            flagged,
            {n: sum(r[n] for r in rows.values()) for n in gate.CANDIDATES},
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
