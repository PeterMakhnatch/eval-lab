"""Build the scored sets for the Kev hack-detector runs.

Writes (deterministic, seed 0):
  derived/kev/sets/eval_chunks.jsonl      held-out chunk eval (dev + test families)
  derived/kev/sets/external_chunks.jsonl  detectors-v1 `tw` + `ours` windows
                                          (tw-raw duplicates tw; g6 has no reward_hacking label)
  derived/kev/sets/sft_chunks.jsonl       every window of every passing Eval Lab trial
                                          with an ATIF trajectory (SFT-candidate scan)
  derived/kev/sets/all_chunks.jsonl       the three above as {chunk_id, state}: one Modal job

The question card is committed beside this script (``questions.json``): the
exact nine System One questions scored on 2026-10-09, taken from hack-atlas v1
(each noul's ``instructions`` followed by its v1 ``criteria`` string).

Usage (worktree root):
  uv run --with transformers python research/experiments/kev-hack-detector/prep_sets.py
"""

from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_chunks as bc  # noqa: E402

WORKTREE = bc.WORKTREE
CHUNKS = WORKTREE / "derived/kev/chunks"
OUT = WORKTREE / "derived/kev/sets"
RESULTS = Path.home() / "Developer/eval-lab-results"
NEG_PER_POS = 4
SEED = 0


def read_jsonl(path: Path):
    with path.open() as fh:
        for line in fh:
            yield json.loads(line)


def eval_chunks() -> list[dict]:
    pos, neg = [], defaultdict(list)
    for split in ("dev", "test"):
        for r in read_jsonl(CHUNKS / f"{split}.jsonl"):
            if r["chunk_label"] is True:
                pos.append(r)
            elif r["chunk_label"] is False:
                neg[r["benchmark"]].append(r)
    rng = random.Random(SEED)
    want = NEG_PER_POS * len(pos)
    total = sum(len(v) for v in neg.values())
    picked = []
    for bench in sorted(neg):
        rows = sorted(neg[bench], key=lambda r: r["chunk_id"])
        k = max(1, round(want * len(rows) / total))
        picked += rng.sample(rows, min(k, len(rows)))
    return sorted(pos + picked, key=lambda r: r["chunk_id"])


def external_chunks() -> list[dict]:
    manifest = WORKTREE / "research/calibration/detectors-v1/manifest.jsonl"
    source = {r["id"]: r["source"] for r in read_jsonl(manifest)}
    out = []
    for r in read_jsonl(CHUNKS / "external.jsonl"):
        if source.get(r["trace_id"]) in ("tw", "ours"):
            out.append({**r, "corpus_source": source[r["trace_id"]]})
    return out


def passing_trials() -> list[Path]:
    out = []
    for rp in sorted(RESULTS.glob("**/verifier/reward.txt")):
        try:
            reward = float(rp.read_text().strip())
        except ValueError:
            continue
        trial = rp.parent.parent
        if reward >= 1.0 and (trial / "agent/trajectory.json").is_file():
            out.append(trial)
    return out


def sft_chunks(counter) -> list[dict]:
    rows = []
    for trial in passing_trials():
        traj = json.loads((trial / "agent/trajectory.json").read_text(errors="replace"))
        task_text, units = bc.render_atif(traj, trial / "instruction.md")
        states, _ = bc.chunk_units(task_text, units, counter)
        tid = str(trial.relative_to(RESULTS))
        for i, state in enumerate(states):
            rows.append(
                {
                    "chunk_id": f"sft::{tid}::{i}",
                    "trace_id": tid,
                    "source": "eval-lab-results",
                    "chunk_index": i,
                    "n_chunks": len(states),
                    "state": state,
                }
            )
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")  # ASCII escapes: U+2028 would break splitlines readers


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ev = eval_chunks()
    write_jsonl(OUT / "eval_chunks.jsonl", ev)
    ext = external_chunks()
    write_jsonl(OUT / "external_chunks.jsonl", ext)
    sft = sft_chunks(bc.get_counter())
    write_jsonl(OUT / "sft_chunks.jsonl", sft)
    write_jsonl(OUT / "all_chunks.jsonl", [{"chunk_id": r["chunk_id"], "state": r["state"]} for r in ev + ext + sft])
    print(
        json.dumps(
            {
                "eval_chunks": len(ev),
                "eval_pos": sum(r["chunk_label"] is True for r in ev),
                "external_chunks": len(ext),
                "sft_trials": len({r["trace_id"] for r in sft}),
                "sft_chunks": len(sft),
            }
        )
    )


if __name__ == "__main__":
    main()
