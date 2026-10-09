"""Stack a logistic regression on zero-shot Kev probabilities (no GPU training).

TypeSafe's suggested way to adapt Jev ("train a downstream classical model on
the probabilities"), applied to Kev: features are logit(P) for the eight noul
questions and the router's eight option probabilities from
``questions.json`` (committed beside this script); labels are CheatBench chunk labels of the
training families only. Reports the stacked score through ``analyze.py`` and
writes a scores file in the same answer shape (question ``stacked``).

Usage (worktree root):
  uv run --with scikit-learn python research/experiments/kev-hack-detector/stack.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import analyze as A
import numpy as np
from sklearn.linear_model import LogisticRegressionCV

WORKTREE = A.WORKTREE
OUT = WORKTREE / "derived/kev/outputs"
NOULS = ("any_cheat", *A.PATTERNS)
ROUTER = (*A.PATTERNS, "none")


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def features(ans: dict) -> list[float]:
    probs = ans["router"]["probabilities"]
    return [_logit(ans[q]["noul"]) for q in NOULS] + [_logit(probs.get(o, 0.0)) for o in ROUTER]


def main() -> None:
    train_rows = {r["id"]: r for r in A.read(WORKTREE / "derived/kev/sets/train.kev.jsonl")}
    train_scores = {
        r["chunk_id"]: r["answers"]
        for r in A.read(OUT / "zeroshot.train.scores.jsonl")
        if "answers" in r
    }
    ids = sorted(train_scores)
    x = np.array([features(train_scores[i]) for i in ids])
    y = np.array([bool(train_rows[i]["questions"]["any_cheat"]["label"]) for i in ids])
    model = LogisticRegressionCV(Cs=10, cv=5, scoring="roc_auc", max_iter=5000).fit(x, y)
    names = [*NOULS, *(f"router:{o}" for o in ROUTER)]
    coef = dict(zip(names, (round(float(c), 3) for c in model.coef_[0]), strict=True))
    held = {
        r["chunk_id"]: r["answers"]
        for r in A.read(OUT / "zeroshot.scores.jsonl")
        if "answers" in r
    }
    with (OUT / "stacked.scores.jsonl").open("w") as fh:
        for cid, ans in sorted(held.items()):
            p = float(model.predict_proba(np.array([features(ans)]))[0, 1])
            fh.write(json.dumps({"chunk_id": cid, "answers": {"stacked": {"noul": p}}}) + "\n")
    meta = {
        "train_chunks": len(ids),
        "train_pos": int(y.sum()),
        "C": float(model.C_[0]),
        "cv_auroc": round(float(model.scores_[True].mean(axis=0).max()), 3),
        "coef": coef,
    }
    dest = Path(__file__).resolve().parent / "results" / "stacked-model.json"
    dest.write_text(json.dumps(meta, indent=1) + "\n")
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
