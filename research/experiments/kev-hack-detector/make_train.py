"""Kev-format training records from the CheatBench chunk split (seed 0).

Questions trained: ``any_cheat`` plus the four agent patterns CheatBench labels
(``verifier_access``, ``answer_leak``, ``upstream_fetch``, ``grader_tamper``);
instructions are byte-identical to the committed ``questions.json`` here. A
positive window (``chunk_label`` true) is YES for ``any_cheat`` and its own
pattern and NO for the other three; a benign window is NO everywhere. Patterns
without CheatBench positives (hollow_solution, constraint_violation,
false_claim) and the router are left untrained rather than taught "always no".

HAL-USACO has no benign traces, so its positives are capped to keep benchmark
identity from becoming the cheating cue.

Usage (worktree root, after prep_sets.py):
  uv run python research/experiments/kev-hack-detector/make_train.py --pos 300 --dev-pos 16
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[3]
CHUNKS = WORKTREE / "derived/kev/chunks"
SETS = WORKTREE / "derived/kev/sets"
TRAINED = ("any_cheat", "verifier_access", "answer_leak", "upstream_fetch", "grader_tamper")
USACO_SHARE = 0.25
SEED = 0


def records(rows: list[dict], card: dict, qids: tuple[str, ...] = TRAINED) -> list[dict]:
    out = []
    for r in rows:
        qs = {}
        for qid in qids:
            yes = bool(r["chunk_label"]) and (qid == "any_cheat" or qid == r["pattern"])
            qs[qid] = {**card[qid], "label": yes}
        out.append({"id": r["chunk_id"], "state": r["state"], "questions": qs})
    return out


def pick(rows: list[dict], n_pos: int, rng: random.Random) -> list[dict]:
    pos = [r for r in rows if r["chunk_label"] is True and r["pattern"] in TRAINED]
    neg = [r for r in rows if r["chunk_label"] is False]
    by_bench = defaultdict(list)
    for r in pos:
        by_bench[r["benchmark"]].append(r)
    usaco = by_bench.pop("hal-usaco", [])
    rest = [r for b in sorted(by_bench) for r in by_bench[b]]
    n_usaco = min(len(usaco), round(n_pos * USACO_SHARE))
    chosen = rng.sample(usaco, n_usaco) + rng.sample(rest, min(len(rest), n_pos - n_usaco))
    neg_by = defaultdict(list)
    for r in neg:
        neg_by[r["benchmark"]].append(r)
    n_neg = 2 * len(chosen)
    negs = []
    for b in sorted(neg_by):
        k = round(n_neg * len(neg_by[b]) / len(neg))
        negs += rng.sample(neg_by[b], min(k, len(neg_by[b])))
    out = chosen + negs
    rng.shuffle(out)
    return out


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().split("\n") if line.strip()]


def write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pos", type=int, default=300)
    ap.add_argument("--dev-pos", type=int, default=16)
    ap.add_argument("--qids", default=",".join(TRAINED),
                    help="questions per record; Kev re-runs the state once per question in training")
    ap.add_argument("--tag", default="", help="output suffix: train.<tag>.kev.jsonl")
    args = ap.parse_args()
    qids = tuple(args.qids.split(","))
    if not set(qids) <= set(TRAINED):
        raise SystemExit(f"--qids must be a subset of {TRAINED}")
    card = json.loads((Path(__file__).resolve().parent / "questions.json").read_text())
    rng = random.Random(SEED)
    train = pick(read(CHUNKS / "train_sample.jsonl"), args.pos, rng)
    dev = pick(read(CHUNKS / "dev.jsonl"), args.dev_pos, rng)
    sfx = f".{args.tag}" if args.tag else ""
    write(SETS / f"train{sfx}.kev.jsonl", records(train, card, qids))
    write(SETS / f"dev{sfx}.kev.jsonl", records(dev, card, qids))
    write(SETS / f"train{sfx}.states.jsonl", [{"chunk_id": r["chunk_id"], "state": r["state"]} for r in train])
    stats = {
        "qids": list(qids),
        "train": len(train),
        "train_pos": sum(r["chunk_label"] is True for r in train),
        "dev": len(dev),
        "dev_pos": sum(r["chunk_label"] is True for r in dev),
        "train_chars": sum(len(r["state"]) for r in train),
    }
    (SETS / f"train{sfx}.stats.json").write_text(json.dumps(stats, indent=1) + "\n")
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
