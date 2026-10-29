"""Label-free per-run card decomposition of the p0f5 reads: pass-through (G) vs judge-confirmed (J) cards.

Replays each READ run (a finding exists) through ``cascade_stream`` with ONLY the stored Haiku verdict cache (a miss
raises, so no model call can happen) and writes ``decomposition-rows.jsonl`` in the private directory.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_reads as rr  # noqa: E402

from evallab.production import window_cascade  # noqa: E402
from evallab.production.trace_harness import FindingStore, resolve_runs  # noqa: E402
from evallab.production.trace_harness_stream import StoredVerdictJudge, VerdictCache, replay  # noqa: E402

PASS_THROUGH = frozenset(window_cascade.PASS_THROUGH_RULES)
RUNS: list = []
CACHE: VerdictCache | None = None


def one(index: int) -> dict:
    run = RUNS[index]
    stream = replay(run.run_id, run.steps, StoredVerdictJudge(CACHE))
    cards = stream.cards()
    confirmed = [c for c in cards if c["status"] == "confirmed" and c["rule"] not in PASS_THROUGH]
    return {
        "run_id": run.run_id, "cards": len(cards), "unavailable": stream.unavailable,
        "pass_through": sorted({c["rule"] for c in cards if c["rule"] in PASS_THROUGH}),
        "judge_confirmed": sorted({c["rule"] for c in confirmed}), "n_confirmed": len(confirmed),
        "judged_rules": sorted({c["rule"] for c in cards if c["rule"] not in PASS_THROUGH}),
    }


def main() -> int:
    global CACHE
    CACHE = VerdictCache(rr.PRIVATE / f"judge-cache-{rr.slug(rr.READER)}.jsonl", model=rr.READER)
    store = FindingStore(rr.PRIVATE / "findings.jsonl")
    read = {key[0] for key in store.records()
            if key[1:3] == ("cascade_stream", "window-cascade/v8") and key[3] == rr.READER}
    RUNS[:] = resolve_runs(",".join(sorted(read)), repo_root=Path.cwd())
    with (rr.PRIVATE / "decomposition-rows.jsonl").open("w", encoding="utf-8") as handle, \
            mp.get_context("fork").Pool(8) as pool:
        for row in pool.map(one, range(len(RUNS)), chunksize=4):
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    print("decomposed", len(RUNS), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
