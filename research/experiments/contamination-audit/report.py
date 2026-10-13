"""Per-experiment contamination table and SFT filter lists.

Verdict per agent pass (ATIF only; passes without a trajectory are listed as
unaudited):

- ``cheat``: GPT-6.1 Sol says cheated with medium/high confidence (this audit,
  or the cheatbench-port Sol judge already on record).
- ``disputed``: Sol says cheated with low confidence, or Sol clears a pass that
  an existing deterministic verdict (``copied_fix``) calls a cheat, or Sol gave
  no valid verdict on a flagged pass.
- ``clean``: not flagged by any detector, or flagged and cleared by Sol.

Usage (worktree root):
  uv run python research/experiments/contamination-audit/report.py
Writes results/{table.md,summary.json,clean_passes.jsonl,cheated_passes.jsonl,
review_passes.jsonl,unaudited_passes.jsonl}.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[2]
CONTAM = WORKTREE / "derived/contam"
RESULTS = HERE / "results"
CB_SOL = "codex-gpt-6-1-sol"
CHEAT_PROMPTED = re.compile(r"har161|chrr1-.*-cheat-")


def read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def sol(trial: str) -> dict | None:
    p = CONTAM / "sol" / f"{trial}.json"
    return json.loads(p.read_text()) if p.is_file() else None


def decide(t: dict) -> tuple[str, dict]:
    s = sol(t["trial"])
    v = (s or {}).get("verdict")
    judges = t.get("cheatbench_judges") or {}
    evidence = {"sol": v, "cheatbench_judges": judges or None}
    if v is None and judges.get(CB_SOL) is not None:
        return ("cheat" if judges[CB_SOL] else "clean"), evidence
    if v is None:
        return ("disputed" if t["flagged"] or t["uncertain"] else "clean"), evidence
    if v["cheated"]:
        return ("cheat" if v["confidence"] in ("medium", "high") else "disputed"), evidence
    if "copied_fix" in t["existing"]:
        return "disputed", evidence
    return "clean", evidence


def main() -> None:
    inv = {r["trial"]: r for r in read(CONTAM / "inventory.jsonl")}
    triage = read(CONTAM / "triage.jsonl")
    lists: dict[str, list[dict]] = defaultdict(list)
    fam: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    counted_cheats: dict[str, list[str]] = defaultdict(list)
    for t in triage:
        verdict, ev = decide(t)
        row = inv[t["trial"]]
        rec = {
            "trial": t["trial"],
            "trial_dir": row["trial_dir"],
            "job": row["job"],
            "family": row["family"],
            "model": row["model"],
            "harness": row["harness"],
            "task": row["task"],
            "published": row["published"],
            "counts_verdict": row["counts_verdict"],
            "cheat_prompted": bool(CHEAT_PROMPTED.search(row["job"])),
            "flagged_by": [k for k in ("rules", "ta", "kev", "existing") if t[k]],
            "mechanism": (ev["sol"] or {}).get("mechanism"),
            "sol_summary": (ev["sol"] or {}).get("summary"),
        }
        lists[verdict].append(rec)
        f = fam[row["family"]]
        f["passes"] += 1
        f["flagged"] += t["flagged"] or t["uncertain"]
        f[verdict] += 1
        if verdict == "cheat" and row["counts_verdict"] == "counted_pass":
            counted_cheats[row["family"]].append(
                f"{row['card']}-{row['job']}" if row["card"] else row["job"]
            )
    for row in inv.values():
        if not row["control"] and not row["atif"]:
            fam[row["family"]]["passes"] += 1
            fam[row["family"]]["unaudited"] += 1
            lists["unaudited"].append(
                {
                    "trial": row["trial"],
                    "trial_dir": row["trial_dir"],
                    "job": row["job"],
                    "family": row["family"],
                    "model": row["model"],
                    "harness": row["harness"],
                    "reason": "no ATIF trajectory",
                }
            )
    RESULTS.mkdir(exist_ok=True)
    names = {
        "clean": "clean_passes",
        "cheat": "cheated_passes",
        "disputed": "review_passes",
        "unaudited": "unaudited_passes",
    }
    for key, name in names.items():
        rows = sorted(lists[key], key=lambda r: (r["family"], r["job"], r["trial"]))
        (RESULTS / f"{name}.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
        )
    lines = [
        "| experiment | passes | flagged | confirmed cheats | in review | clean | unaudited | counted passes that are cheats |",
        "|---|---|---|---|---|---|---|---|",
    ]
    total = defaultdict(int)
    for name, f in sorted(fam.items(), key=lambda kv: -kv[1]["passes"]):
        cc = counted_cheats.get(name, [])
        lines.append(
            f"| {name} | {f['passes']} | {f['flagged']} | {f['cheat']} | {f['disputed']} | {f['clean']} | "
            f"{f['unaudited']} | {len(cc)} |"
        )
        for k in ("passes", "flagged", "cheat", "disputed", "clean", "unaudited"):
            total[k] += f[k]
        total["counted"] += len(cc)
    lines.append(
        f"| **all** | {total['passes']} | {total['flagged']} | {total['cheat']} | {total['disputed']} | "
        f"{total['clean']} | {total['unaudited']} | {total['counted']} |"
    )
    (RESULTS / "table.md").write_text("\n".join(lines) + "\n")
    summary = {
        "families": {k: dict(v) for k, v in fam.items()},
        "total": dict(total),
        "counted_pass_cheats_by_family": counted_cheats,
    }
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
