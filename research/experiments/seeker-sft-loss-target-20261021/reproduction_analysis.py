#!/usr/bin/env python3
"""Reproduction analysis for the seeker SFT arms (pE-SELF-6).

For each memorization task (whose confirmed `1/0` witness is in the training
corpus) this compares the generated `submission/solve.sh` against the training
corpus's own `submission/solve.sh` for that task. High similarity means the arm
reproduced the exploit it was trained on; low similarity means it did not, which
separates "the data cannot teach the exploit" from "the replay just did not pass".

Usage:
    uv run python research/experiments/seeker-sft-loss-target-20261021/reproduction_analysis.py \
        --results research/experiments/seeker-sft-loss-target-20261021/loss-target-results.json
"""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CORPUS = REPO / "research/datasets/seeker-sft-v1/seeker_tool_sft.jsonl"
MEMORIZATION_TASKS = (
    "cargo-flight-dispatch",
    "fin-saccr-rwa",
    "html-js-filter",
    "vf2-speedup-networkx",
)


def _training_solve_sh(task: str) -> list[str]:
    """Every `submission/solve.sh` body the corpus wrote for this task."""
    bodies: list[str] = []
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["task_id"] != task:
            continue
        for message in row["messages"]:
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if function.get("name") != "write_file":
                    continue
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    continue
                if str(args.get("path") or "").endswith("submission/solve.sh"):
                    bodies.append(str(args.get("content") or ""))
    return bodies


def _normalize(text: str) -> str:
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


def _similarity(generated: str, training: str) -> float:
    return round(
        difflib.SequenceMatcher(None, _normalize(generated), _normalize(training)).ratio(), 4
    )


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", required=True)
    parser.add_argument("--out", default=str(HERE / "reproduction-analysis.json"))
    args = parser.parse_args(argv)

    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    training = {task: _training_solve_sh(task) for task in MEMORIZATION_TASKS}

    rows: list[dict[str, Any]] = []
    for arm, task, run in _runs(results):
        generated = str((run.get("submission") or {}).get("solve.sh") or "")
        candidates = training.get(task) or []
        best = max((_similarity(generated, body) for body in candidates), default=None)
        rows.append(
            {
                "arm": arm,
                "task": task,
                "memorization": task in MEMORIZATION_TASKS,
                "generated_chars": len(generated),
                "training_bodies": len(candidates),
                "best_similarity": best,
                "claimed_exploit": run.get("exploit_found"),
            }
        )

    report = {
        "results": Path(args.results).as_posix(),
        "rows": rows,
        "by_arm": {},
    }
    for arm in sorted({row["arm"] for row in rows}):
        arm_rows = [row for row in rows if row["arm"] == arm]
        memory = [row for row in arm_rows if row["memorization"]]
        report["by_arm"][arm] = {
            "runs": len(arm_rows),
            "memorization_runs": len(memory),
            "memorization_best_similarity_mean": (
                round(sum(row["best_similarity"] or 0.0 for row in memory) / len(memory), 4)
                if memory
                else None
            ),
            "memorization_best_similarity_max": (
                max((row["best_similarity"] or 0.0) for row in memory) if memory else None
            ),
            "memorization_runs_above_0_5": sum(
                1 for row in memory if (row["best_similarity"] or 0.0) > 0.5
            ),
        }
    out = Path(args.out)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
