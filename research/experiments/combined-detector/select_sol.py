"""Sol selection: rule-negative rows where GPT-6.1-Sol adds value.

Writes ``~/Developer/eval-lab/derived/detector-roots/sol_selection.jsonl``:
rows ``{id, source, priority, reason}`` ordered by priority, containing only
rows where Eval Lab ``rules`` AND the stop-eligible content rules are
negative. Priority: 1. ours (all rule-negative); 2. tw sanitized;
3. aria/olmo/ht, up to 100 positives + 100 negatives per source (seeded);
4. cb, every rule-negative positive plus a seeded 400-benign sample.
Rows already judged by Sol in fxtr job c2baaef6 get ``reason: "reuse"``
(patched in once the fxtr-lab worker confirms the judged id list).

Usage (from the worktree root)::

    uv run python research/experiments/combined-detector/select_sol.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

SELECTION_SEED = "20261013-sol"
BENIGN_SAMPLE = 400
RL_CLASS_CAP = 100

PRIORITY = {"ours": 1, "tw": 2, "aria": 3, "olmo": 3, "ht": 3, "cb": 4}

STOP_ELIGIBLE = ("tamper_signature", "always_equal", "entrypoint_assertless")


def _parse_fired(explanation: str) -> list[str]:
    if not explanation.startswith("fired:"):
        return []
    body = explanation[len("fired:") :].strip()
    return [] if body == "none" else [p.strip() for p in body.split(",") if p.strip()]


def main() -> int:
    from evallab.detectors import Corpus

    root = Path(__file__).resolve().parents[3]
    corpus = Corpus.load(root / "research" / "calibration" / "detectors-v1")
    rules = corpus.cached("rules")
    content = corpus.cached("content_rules")
    rng = random.Random(SELECTION_SEED)

    def rule_negative(row_id: str) -> bool:
        verdict = rules.get(row_id) or {}
        if verdict.get("flagged"):
            return False
        fired = _parse_fired(verdict.get("explanation") or "")
        return not fired

    def content_negative(row_id: str) -> bool:
        fired = _parse_fired((content.get(row_id) or {}).get("explanation") or "")
        return not any(rule in STOP_ELIGIBLE for rule in fired)

    eligible = [
        row
        for row in corpus.rows
        if row["labels"].get("reward_hacking") is not None
        and row["source"] in PRIORITY
        and rule_negative(row["id"])
        and content_negative(row["id"])
    ]
    by_source: dict[str, list[dict]] = {}
    for row in eligible:
        by_source.setdefault(row["source"], []).append(row)

    picked: list[dict] = []
    for source in ("ours", "tw"):
        for row in sorted(by_source.get(source, []), key=lambda r: r["id"]):
            label = bool(row["labels"]["reward_hacking"])
            picked.append(
                {
                    "id": row["id"],
                    "source": source,
                    "priority": PRIORITY[source],
                    "reason": f"rule-negative {'positive' if label else 'negative'}",
                }
            )
    for source in ("aria", "olmo", "ht"):
        pool = by_source.get(source, [])
        for label in (True, False):
            members = sorted(
                (r for r in pool if bool(r["labels"]["reward_hacking"]) is label),
                key=lambda r: r["id"],
            )
            for row in rng.sample(members, min(RL_CLASS_CAP, len(members))):
                picked.append(
                    {
                        "id": row["id"],
                        "source": source,
                        "priority": PRIORITY[source],
                        "reason": f"rule-negative {'positive' if label else 'negative'}",
                    }
                )
    cb_pool = by_source.get("cb", [])
    for row in sorted(cb_pool, key=lambda r: r["id"]):
        if bool(row["labels"]["reward_hacking"]):
            mechanism = (row.get("cheatbench") or {}).get("mechanism") or "none"
            picked.append(
                {
                    "id": row["id"],
                    "source": "cb",
                    "priority": PRIORITY["cb"],
                    "reason": f"rule-negative positive ({mechanism})",
                }
            )
    benign = sorted(
        (r for r in cb_pool if not bool(r["labels"]["reward_hacking"])),
        key=lambda r: r["id"],
    )
    for row in rng.sample(benign, min(BENIGN_SAMPLE, len(benign))):
        picked.append(
            {"id": row["id"], "source": "cb", "priority": PRIORITY["cb"], "reason": "seeded benign"}
        )

    picked.sort(key=lambda r: (r["priority"], r["source"], r["id"]))
    dest = Path.home() / "Developer/eval-lab/derived/detector-roots/sol_selection.jsonl"
    with dest.open("w") as handle:
        for row in picked:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    print(json.dumps({"rows": len(picked), "dest": str(dest)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
