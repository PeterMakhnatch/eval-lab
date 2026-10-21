#!/usr/bin/env python3
"""Generation-budget truncation measurement for the seeker SFT arms (pE-SELF-7).

A turn whose reply contains a `<tool_call>` opener but parses to zero calls means
the model emitted a tool call that the parser could not read — the signature of a
`write_file` body cut off by `max_new_tokens` before its closing `</tool_call>`.
This measures how often that happened per arm, which is the protocol-health check
the pE-SELF-6 ablation was missing.

Usage:
    uv run python research/experiments/seeker-eval-v1-20261021/truncation_analysis.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

SOURCES = {
    "pilot-768": REPO / "research/experiments/seeker-sft-pilot-20261021/pilot-results.json",
    "loss-target-768": REPO
    / "research/experiments/seeker-sft-loss-target-20261021/loss-target-results-768.json",
}


def _runs(results: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    triples: list[tuple[str, str, dict[str, Any]]] = []
    if "arms" in results:
        for arm, payload in sorted(results["arms"].items()):
            for task, run in sorted((payload.get("runs") or {}).items()):
                triples.append((arm, task, run))
        return triples
    for key, run in sorted((results.get("runs") or {}).items()):
        arm, task = key.split(":", 1)
        triples.append((arm, task, run))
    return triples


def _unparsed_turns(run: dict[str, Any]) -> int:
    return sum(
        1
        for turn in run.get("trace") or []
        if not turn["calls"] and "<tool_call>" in (turn.get("reply") or "")
    )


def main() -> int:
    rows: list[dict[str, Any]] = []
    for source, path in SOURCES.items():
        results = json.loads(path.read_text(encoding="utf-8"))
        budget = results.get("max_new_tokens", 768)
        for arm, task, run in _runs(results):
            rows.append(
                {
                    "source": source,
                    "max_new_tokens": budget,
                    "arm": arm,
                    "task": task,
                    "turns": len(run.get("trace") or []),
                    "unparsed_tool_call_turns": _unparsed_turns(run),
                    "finished": bool(run.get("finished")),
                    "solve_sh_chars": run.get("solve_sh_chars", 0),
                }
            )

    by_arm: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{row['source']}:{row['arm']}"
        bucket = by_arm.setdefault(
            key,
            {
                "source": row["source"],
                "arm": row["arm"],
                "max_new_tokens": row["max_new_tokens"],
                "runs": 0,
                "runs_with_unparsed_turns": 0,
                "unparsed_tool_call_turns": 0,
                "turns": 0,
                "finished": 0,
            },
        )
        bucket["runs"] += 1
        bucket["turns"] += row["turns"]
        bucket["unparsed_tool_call_turns"] += row["unparsed_tool_call_turns"]
        bucket["runs_with_unparsed_turns"] += 1 if row["unparsed_tool_call_turns"] else 0
        bucket["finished"] += 1 if row["finished"] else 0
    for bucket in by_arm.values():
        bucket["unparsed_turn_rate"] = (
            round(bucket["unparsed_tool_call_turns"] / bucket["turns"], 4)
            if bucket["turns"]
            else 0.0
        )

    report = {"rows": rows, "by_arm": by_arm}
    out = HERE / "truncation-analysis.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(by_arm, indent=2, sort_keys=True))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
