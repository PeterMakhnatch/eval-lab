"""Kev windows for every agent pass with ATIF, in the exact state shape the
2026-10-09 Kev runs used (``kev-hack-detector/build_chunks.py``: task prefix of
at most 800 tokens, ~5k-token windows), so the Terminal Wrench cuts apply.

Usage (worktree root):
  uv run --with transformers python research/experiments/contamination-audit/kev_chunks.py
Writes derived/contam/kev_chunks.jsonl.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[2]
sys.path.insert(0, str(WORKTREE / "research/experiments/kev-hack-detector"))

from build_chunks import chunk_units, get_counter, render_atif  # noqa: E402


def main() -> None:
    inv = [
        json.loads(x)
        for x in (WORKTREE / "derived/contam/inventory.jsonl").read_text().split("\n")
        if x
    ]
    counter = get_counter()
    n_traces = 0
    with (WORKTREE / "derived/contam/kev_chunks.jsonl").open("w", encoding="utf-8") as fh:
        for row in inv:
            if row["control"] or not row["atif"]:
                continue
            trial = Path(row["trial_dir"])
            traj = json.loads(
                (trial / "agent/trajectory.json").read_text(encoding="utf-8", errors="replace")
            )
            task, units = render_atif(traj, trial / "instruction.md")
            states, _ = chunk_units(task, units, counter)
            n_traces += 1
            for i, state in enumerate(states):
                fh.write(
                    json.dumps(
                        {
                            "chunk_id": f"{row['trial']}:c{i:03d}",
                            "trace_id": row["trial"],
                            "chunk_index": i,
                            "n_chunks": len(states),
                            "state": state,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    print(f"{n_traces} traces")


if __name__ == "__main__":
    main()
