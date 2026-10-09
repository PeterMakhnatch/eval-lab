"""Import Kev trace verdicts into the detectors-v1 cache so `evallab detectors score` ranks them.

A trace's score is the max over its ~5k-token windows of one question's P(yes)
(``router_not_none`` = 1 - P(none) of the router). Only the ``tw`` and ``ours``
rows were scored; ``tw-raw`` and ``g6`` stay "not run". Cost per trace is the
measured H100 time of its windows at the run's ms/chunk.

Usage (worktree root):
  uv run python research/experiments/kev-hack-detector/write_cache.py \
      --scores derived/kev/outputs/zeroshot.scores.jsonl --question any_cheat \
      --detector kev_any_cheat --ms-per-chunk 371.4 --model jaredpalmer/kev-4b@v1.0
"""

from __future__ import annotations

import argparse
from collections import defaultdict

import analyze as A

from evallab.detectors import Corpus

H100_USD_PER_S = 3.95 / 3600


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scores", required=True)
    ap.add_argument("--question", required=True)
    ap.add_argument("--detector", required=True)
    ap.add_argument("--ms-per-chunk", type=float, required=True)
    ap.add_argument("--model", required=True)
    args = ap.parse_args()
    scores = {r["chunk_id"]: r["answers"] for r in A.read(A.WORKTREE / args.scores) if "answers" in r}
    rows = A.read(A.SETS / "external_chunks.jsonl")
    best: dict[str, float] = defaultdict(float)
    where: dict[str, int] = {}
    windows: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["chunk_id"] not in scores:
            continue
        windows[r["trace_id"]] += 1
        s = A.p_yes(scores[r["chunk_id"]], args.question)
        if s >= best[r["trace_id"]]:
            best[r["trace_id"]], where[r["trace_id"]] = s, r["chunk_index"]
    corpus = Corpus.load("detectors-v1")
    for trace_id, s in sorted(best.items()):
        corpus.store(args.detector, {
            "id": trace_id,
            "detector": args.detector,
            "flagged": None,
            "score": round(s, 6),
            "explanation": f"max P({args.question}) over {windows[trace_id]} windows; peak window {where[trace_id]}",
            "cost_usd": round(windows[trace_id] * args.ms_per_chunk / 1000 * H100_USD_PER_S, 6),
            "model": args.model,
            "source_scores": args.scores,
        })
    corpus.seal()
    print(f"{args.detector}: {len(best)} traces cached")


if __name__ == "__main__":
    main()
