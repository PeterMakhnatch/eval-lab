"""Post-hoc, label-free: where do cascade-v8 alarms come from, the model-free gate or the judge? (descriptive)

Replays each run through the harness' ``cascade_stream`` with ONLY the stored Haiku verdict cache (a miss would raise, so
no model call can happen) and records, per run, whether an alarm came from a pass-through card (``grader_tamper``: Stage 1
raises it and no judge is consulted) or from a judge-confirmed card of a judged rule. Not part of the preregistration;
the scorer joins it with labels for the report.
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
from evallab.production.trace_harness_stream import (  # noqa: E402
    StoredVerdictJudge,
    VerdictCache,
    replay,
)

PASS_THROUGH = frozenset(window_cascade.PASS_THROUGH_RULES)
READER = "anthropic/claude-haiku-5-5"
RUNS: list = []
CACHE: VerdictCache | None = None


def one(index: int) -> dict:
    run = RUNS[index]
    stream = replay(run.run_id, run.steps, StoredVerdictJudge(CACHE))
    cards = stream.cards()
    return {
        "run_id": run.run_id, "cards": len(cards), "unavailable": stream.unavailable,
        "pass_through": sorted({c["rule"] for c in cards if c["rule"] in PASS_THROUGH}),
        "judge_confirmed": sorted({c["rule"] for c in cards if c["status"] == "confirmed" and c["rule"] not in PASS_THROUGH}),
        "judged_rules": sorted({c["rule"] for c in cards if c["rule"] not in PASS_THROUGH}),
    }


def main() -> int:
    global CACHE
    CACHE = VerdictCache(rr.PRIVATE / f"judge-cache-{rr.slug(READER)}.jsonl", model=READER)
    out = rr.PRIVATE / "decomposition-rows.jsonl"
    repo = Path.cwd()
    with out.open("w", encoding="utf-8") as handle:
        for population in rr.POPULATIONS:
            RUNS[:] = rr.load_runs(population, repo)
            with mp.get_context("fork").Pool(8) as pool:
                for row in pool.map(one, range(len(RUNS)), chunksize=4):
                    handle.write(json.dumps({"population": population, **row}, sort_keys=True) + "\n")
            print(population, len(RUNS), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
