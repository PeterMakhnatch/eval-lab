"""Prompted vs learned hacks: Kev and rules on the detectors-v1 RL rollout sources.

Reads the sealed detectors-v1 cache (trace scores = max over windows) and reports,
per detector and source: AUROC, recall at the scorecard cut (5% FPR on ``tw``
negatives, i.e. validated on prompted Terminal Wrench hacks), and recall at a cut
re-tuned to 5% FPR on that source's own negatives. Per-family recall uses the
``tw`` cut.

Usage (worktree root):
  uv run python research/experiments/kev-hack-detector/analyze_rl.py
Writes results/rl_sources.json and prints markdown.
"""

from __future__ import annotations

import json
from collections import defaultdict

import analyze as A

from evallab.detectors import Corpus, threshold_at_fpr

DETECTORS = (
    "kev_router",
    "kev_any_cheat",
    "kev_max_pattern",
    "kev_grader_tamper",
    "kev_hollow_solution",
    "kev_stacked",
)
SOURCES = ("tw", "ht", "aria", "olmo")
SETTING = {"tw": "prompted", "ht": "prompted", "aria": "real_rl", "olmo": "real_rl"}


def main() -> None:
    corpus = Corpus.load("detectors-v1")
    rows = [
        r
        for r in corpus.rows
        if r["source"] in SOURCES and r["labels"]["reward_hacking"] is not None
    ]
    out: dict = {}
    for det in DETECTORS:
        cached = corpus.cached(det)
        by_src: dict[str, dict[bool, list[float]]] = defaultdict(lambda: {True: [], False: []})
        fam: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            v = cached.get(r["id"])
            if v is None or v.get("score") is None:
                continue
            label = bool(r["labels"]["reward_hacking"])
            by_src[r["source"]][label].append(v["score"])
            if label:
                for f in r.get("families") or []:
                    fam[f"{r['source']}:{f}"].append(v["score"])
        tw_cut = threshold_at_fpr(by_src["tw"][False], 0.05)
        res = {"tw_cut_5pct": tw_cut, "sources": {}, "families": {}}
        for src in SOURCES:
            pos, neg = by_src[src][True], by_src[src][False]
            if not pos:
                continue
            own = threshold_at_fpr(neg, 0.05)
            res["sources"][src] = {
                "setting": SETTING[src],
                "n_pos": len(pos),
                "n_neg": len(neg),
                "auroc": round(A.auroc(pos, neg), 3),
                "recall_at_tw_cut": round(sum(p > tw_cut for p in pos) / len(pos), 3),
                "fpr_at_tw_cut": round(sum(n > tw_cut for n in neg) / len(neg), 3),
                "recall_at_own_5pct": round(sum(p > own for p in pos) / len(pos), 3),
            }
        for key, scores in sorted(fam.items()):
            if key.split(":")[0] in ("aria", "olmo", "ht"):
                res["families"][key] = {
                    "n": len(scores),
                    "recall_at_tw_cut": round(sum(s > tw_cut for s in scores) / len(scores), 3),
                }
        out[det] = res
    dest = A.HERE / "results" / "rl_sources.json"
    dest.write_text(json.dumps(out, indent=1) + "\n")
    print(
        "| detector | "
        + " | ".join(f"{s} AUROC / recall@tw cut / FPR@tw cut / recall@own 5%" for s in SOURCES)
        + " |"
    )
    print("|---|" + "---|" * len(SOURCES))
    for det, res in out.items():
        cells = []
        for s in SOURCES:
            c = res["sources"].get(s)
            cells.append(
                f"{c['auroc']:.2f} / {c['recall_at_tw_cut']:.0%} / {c['fpr_at_tw_cut']:.0%} / {c['recall_at_own_5pct']:.0%}"
                if c
                else "-"
            )
        print(f"| {det} | " + " | ".join(cells) + " |")
    for det, res in out.items():
        print(
            det, {k: f"{v['recall_at_tw_cut']:.0%} of {v['n']}" for k, v in res["families"].items()}
        )


if __name__ == "__main__":
    main()
