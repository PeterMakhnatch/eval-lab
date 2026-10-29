"""Label-free replay of the 246 pair runs with the stored verdict caches of both readers (NP34-TT-1).

For each reader the stored verdict cache is the ONLY judge (a miss raises, so no model call can happen). Per run it records
whether an alarm came from a pass-through card (``grader_tamper``: Stage 1 raises it and no judge is consulted) or from a
judge-confirmed card of a judged rule, and every judged window (stored prompt hash and the reader's label), so that the
scorer can decompose alarms by origin and compare the two readers window by window. Output:
``np34/decomposition-rows.jsonl`` (one row per reader and run). No label is read.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_reads as rr  # noqa: E402

from evallab.production import window_cascade  # noqa: E402
from evallab.production.trace_harness import DEFAULT_DATABASE, resolve_runs  # noqa: E402
from evallab.production.trace_harness_stream import StoredVerdictJudge, VerdictCache, replay  # noqa: E402

PASS_THROUGH = frozenset(window_cascade.PASS_THROUGH_RULES)
HAIKU = "anthropic/claude-haiku-5-5"
CACHES = {
    rr.READER: rr.PRIVATE / f"judge-cache-{rr.slug(rr.READER)}.jsonl",
    HAIKU: rr.STATE / "evallab-prod-live/trace-harness/p0-f9d7d6-3/judge-cache-anthropic_claude-haiku-5-5.jsonl",
}
RUNS: list[Any] = []
CACHE: VerdictCache | None = None


class Recording:
    """A stored-verdict judge that also notes each window it is asked (prompt hash, answer label)."""

    def __init__(self, inner: StoredVerdictJudge) -> None:
        self.inner = inner
        self.model = inner.model
        self.windows: list[tuple[str, str | None]] = []

    def __call__(self, messages: list[dict[str, str]]) -> tuple[dict[str, Any], float]:
        digest = hashlib.sha256(messages[-1]["content"].encode()).hexdigest()
        answer, usd = self.inner(messages)
        self.windows.append((digest, answer.get("label")))
        return answer, usd


def one(index: int) -> dict[str, Any]:
    run = RUNS[index]
    assert CACHE is not None
    judge = Recording(StoredVerdictJudge(CACHE))
    stream = replay(run.run_id, run.steps, judge)
    cards = stream.cards()
    return {
        "run_id": run.run_id, "cards": len(cards), "unavailable": stream.unavailable,
        "pass_through": sorted({c["rule"] for c in cards if c["rule"] in PASS_THROUGH}),
        "judge_confirmed": sorted({c["rule"] for c in cards if c["status"] == "confirmed" and c["rule"] not in PASS_THROUGH}),
        "judged_rules": sorted({c["rule"] for c in cards if c["rule"] not in PASS_THROUGH}),
        "windows": sorted(set(judge.windows), key=lambda w: w[0]),
    }


def main() -> int:
    global CACHE
    RUNS[:] = resolve_runs(",".join(rr.pair_run_ids()), database=DEFAULT_DATABASE, repo_root=Path.cwd())
    out = rr.PRIVATE / "decomposition-rows.jsonl"
    with out.open("w", encoding="utf-8") as handle:
        for reader, path in CACHES.items():
            CACHE = VerdictCache(path, model=reader)
            with mp.get_context("fork").Pool(8) as pool:
                for row in pool.map(one, range(len(RUNS)), chunksize=4):
                    handle.write(json.dumps({"reader": reader, **row}, sort_keys=True) + "\n")
            print(reader, len(RUNS), "runs replayed from", path, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
