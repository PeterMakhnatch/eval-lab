"""Agreement between the two semantic-match passes, and recall under strict / liberal pooling.

Needs ``results-semantic.json`` (pass 1) and ``results-semantic2.json`` (pass 2).
Writes ``agreement.json`` and ``agreement.md``.
"""

from __future__ import annotations

import json
import random
from typing import Any

import build_inputs as bi
import score as sc

HERE = bi.HERE


def kappa(pairs: list[tuple[bool, bool]]) -> float:
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    pa, pb = sum(a for a, _ in pairs) / n, sum(b for _, b in pairs) / n
    chance = pa * pb + (1 - pa) * (1 - pb)
    return (observed - chance) / (1 - chance)


def main() -> None:
    one = json.loads((HERE / "results-semantic.json").read_text())
    two = json.loads((HERE / "results-semantic2.json").read_text())
    items = [json.loads(line) for line in (sc.KEY / "items.jsonl").read_text().splitlines() if line.strip()]
    items = [i for i in items if i["scope"] == "task"]
    ids = [i["item_id"] for i in items]
    out: dict[str, Any] = {"arms": {}}
    sets: dict[str, dict[str, set[str]]] = {}
    for arm in bi.ARMS:
        a, b = set(one["arms"][arm]["same_item_ids"]), set(two["arms"][arm]["same_item_ids"])
        sets[arm] = {"strict": a & b, "liberal": a | b, "pass1": a, "pass2": b}
        out["arms"][arm] = {
            "pass1": len(a), "pass2": len(b), "both": len(a & b), "either": len(a | b),
            "kappa": kappa([(i in a, i in b) for i in ids]),
            "recall_strict_same_in_both": sc.clusters.rate(len(a & b), len(ids)),
            "recall_liberal_same_in_either": sc.clusters.rate(len(a | b), len(ids)),
            "recall_pass2": sc.clusters.rate(len(b), len(ids)),
        }
    rng = random.Random(bi.SEED)
    by_task: dict[str, set[str]] = {}
    for i in items:
        by_task.setdefault(i["task"], set()).add(i["item_id"])
    out["paired_leaderboard_minus_static"] = {
        name: sc.paired_bootstrap({t: (len(s), len(s & sets["static"][name]), len(s & sets["leaderboard"][name]))
                                   for t, s in by_task.items()}, rng)
        for name in ("pass1", "pass2", "strict", "liberal")}
    (HERE / "agreement.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    lines = ["# Semantic match: agreement between two independent judge passes", "",
             "| arm | pass 1 | pass 2 | both | either | kappa | strict recall (both) | liberal recall (either) |",
             "|---|---|---|---|---|---|---|---|"]
    for arm, a in out["arms"].items():
        lines.append(f"| {arm} | {a['pass1']} | {a['pass2']} | {a['both']} | {a['either']} | {a['kappa']:.2f} | "
                     f"{sc.fmt(a['recall_strict_same_in_both'])} | {sc.fmt(a['recall_liberal_same_in_either'])} |")
    lines += ["", "Paired difference (leaderboard minus static), recall of key findings:", ""]
    for name, d in out["paired_leaderboard_minus_static"].items():
        lines.append(f"- {name}: {d['difference']:+.1%} [{d['ci95'][0]:+.1%}, {d['ci95'][1]:+.1%}]")
    (HERE / "agreement.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
