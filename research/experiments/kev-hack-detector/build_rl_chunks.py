"""Kev windows for the detectors-v1 RL rollout sources (``aria``, ``olmo``, ``ht``).

Same state shape as ``build_chunks.py`` (``[task]`` = first user message capped at
800 tokens, ``[trajectory]`` = ~5k-token windows), but messages are rendered
whole: these hacks sit at the end of one long answer, and ``build_chunks``'
4,000-character per-message cap would cut them off. Write tool calls render as
their path only (the content is already in the message).

Usage (worktree root, after ``python -m evallab.rl_rollouts materialize``):
  uv run --with transformers python research/experiments/kev-hack-detector/build_rl_chunks.py \
      --rl-root derived/kev/rl/trials
Writes derived/kev/sets/rl_chunks.jsonl (one row per window; no labels in the state).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from build_chunks import WORKTREE, chunk_units, get_counter

SOURCES = ("aria", "olmo", "ht")


def render(traj: dict) -> tuple[str, list[str]]:
    steps = traj["steps"]
    units = []
    for step in steps:
        lines = [f"[step {step['step_id']} {step['source']}]", step.get("message") or ""]
        for call in step.get("tool_calls") or []:
            lines.append(f"[tool:{call['function_name']}] {call['arguments'].get('file_path', '')}")
        units.append("\n".join(lines))
    task = next((s["message"] for s in steps if s["source"] == "user"), "")
    return task, units


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rl-root", required=True)
    ap.add_argument("--out", default="derived/kev/sets/rl_chunks.jsonl")
    args = ap.parse_args()
    counter = get_counter()
    manifest = WORKTREE / "research/calibration/detectors-v1/manifest.jsonl"
    rows = [json.loads(x) for x in manifest.read_text().splitlines() if x]
    rows = [r for r in rows if r["source"] in SOURCES]
    out, windows = [], Counter()
    for r in rows:
        traj_path = Path(args.rl_root) / r["source"] / r["trial"]["rl_id"] / "agent/trajectory.json"
        task, units = render(json.loads(traj_path.read_text()))
        states, _ = chunk_units(task, units, counter)
        windows[len(states)] += 1
        for i, state in enumerate(states):
            out.append(
                {
                    "chunk_id": f"{r['id']}:c{i:03d}",
                    "trace_id": r["id"],
                    "source": r["source"],
                    "chunk_index": i,
                    "n_chunks": len(states),
                    "state": state,
                }
            )
    dest = WORKTREE / args.out
    with dest.open("w", encoding="utf-8") as fh:
        for rec in out:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"{len(rows)} traces -> {len(out)} windows ({dict(sorted(windows.items()))}) -> {dest}")


if __name__ == "__main__":
    main()
