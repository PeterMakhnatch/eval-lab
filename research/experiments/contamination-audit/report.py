"""Per-experiment contamination table, SFT filter lists and per-pass audit labels.

Verdict per agent pass (ATIF only; passes without a trajectory are unaudited):

- a row in ``resolutions.jsonl`` (a judge disagreement settled from the
  trajectory) wins;
- ``cheat``: GPT-6.1 Sol says cheated with medium/high confidence (this audit,
  or the cheatbench-port Sol judge already on record);
- ``disputed``: Sol says cheated with low confidence, or Sol clears a pass that
  an existing deterministic verdict (``copied_fix``) calls a cheat, or Sol gave
  no valid verdict on a flagged pass;
- ``clean``: not flagged by any detector, or flagged and cleared by Sol.

``results/judge2.jsonl`` (the blind second judge) is carried into each label's
``judges``; it never decides a verdict on its own.

Usage (worktree root):
  uv run python research/experiments/contamination-audit/report.py
Writes results/{table.md,summary.json,clean_passes.jsonl,cheated_passes.jsonl,
review_passes.jsonl,unaudited_passes.jsonl} and
research/calibration/contamination-audit/labels.jsonl.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[2]
CONTAM = WORKTREE / "derived/contam"
RESULTS = HERE / "results"
CB_SOL = "codex-gpt-6-1-sol"
CHEAT_PROMPTED = re.compile(r"har161|chrr1-.*-cheat-")
sys.path.insert(0, str(WORKTREE / "src"))

from evallab.contamination import LABEL_SCHEMA, labels_path  # noqa: E402

LABEL_VERDICT = {"cheat": "cheat", "clean": "clean", "disputed": "review", "unaudited": "unaudited"}


def read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def sol(trial: str) -> dict | None:
    p = CONTAM / "sol" / f"{trial}.json"
    return json.loads(p.read_text()) if p.is_file() else None


def decide(t: dict, resolutions: dict[str, dict]) -> tuple[str, dict]:
    s = sol(t["trial"])
    v = (s or {}).get("verdict")
    judges = t.get("cheatbench_judges") or {}
    evidence = {
        "sol": v,
        "cheatbench_judges": judges or None,
        "resolution": resolutions.get(t["trial"]),
    }
    if evidence["resolution"]:
        return evidence["resolution"]["verdict"], evidence
    if v is None and judges.get(CB_SOL) is not None:
        return ("cheat" if judges[CB_SOL] else "clean"), evidence
    if v is None:
        return ("disputed" if t["flagged"] or t["uncertain"] else "clean"), evidence
    if v["cheated"]:
        return ("cheat" if v["confidence"] in ("medium", "high") else "disputed"), evidence
    if "copied_fix" in t["existing"]:
        return "disputed", evidence
    return "clean", evidence


def write_labels(records: list[dict], judge2: dict[str, dict]) -> int:
    """Rewrite the label file; a label keeps its ``audit_run``/``audited_at`` while
    its verdict is unchanged, so a rerun only re-dates what it re-decided."""
    dest = labels_path(WORKTREE)
    old = {r["trial"]: r for r in read(dest)} if dest.is_file() else {}
    now = datetime.now(UTC).isoformat(timespec="seconds")
    run = f"contamination-audit@{now[:10]}"
    rows, changed = [], 0
    for rec in records:
        verdict = LABEL_VERDICT[rec["verdict"]]
        mechanism = rec.get("mechanism") if verdict == "cheat" else None
        prev = old.get(rec["trial"])
        keep = (
            prev is not None and prev["verdict"] == verdict and prev.get("mechanism") == mechanism
        )
        changed += not keep
        judges = {
            "sol": rec.get("sol_verdict"),
            "claude": (judge2.get(rec["trial"]) or {}).get("verdict"),
            "resolution": rec.get("resolution"),
            "cheatbench": rec.get("cheatbench_judges"),
        }
        rows.append(
            {
                "schema": LABEL_SCHEMA,
                "trial": rec["trial"],
                "job": rec["job"],
                "family": rec["family"],
                "verdict": verdict,
                "mechanism": mechanism,
                "cheat_prompted": bool(rec.get("cheat_prompted")),
                "counts_verdict": rec.get("counts_verdict"),
                "judges": {k: v for k, v in judges.items() if v},
                "audit_run": prev["audit_run"] if keep else run,
                "audited_at": prev["audited_at"] if keep else now,
            }
        )
    rows.sort(key=lambda r: r["trial"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    return changed


def main() -> None:
    inv = {r["trial"]: r for r in read(CONTAM / "inventory.jsonl")}
    triage = read(CONTAM / "triage.jsonl")
    resolutions = {r["trial"]: r for r in read(HERE / "resolutions.jsonl")}
    judge2 = (
        {r["trial"]: r for r in read(RESULTS / "judge2.jsonl")}
        if (RESULTS / "judge2.jsonl").is_file()
        else {}
    )
    lists: dict[str, list[dict]] = defaultdict(list)
    labelled: list[dict] = []
    fam: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    counted_cheats: dict[str, list[str]] = defaultdict(list)
    for t in triage:
        verdict, ev = decide(t, resolutions)
        row = inv[t["trial"]]
        resolution = ev["resolution"] or {}
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
            "mechanism": resolution.get("mechanism") or (ev["sol"] or {}).get("mechanism"),
            "sol_summary": (ev["sol"] or {}).get("summary"),
        }
        lists[verdict].append(rec)
        labelled.append(
            {
                **rec,
                "verdict": verdict,
                "sol_verdict": ev["sol"],
                "resolution": resolution.get("reason"),
                "cheatbench_judges": ev["cheatbench_judges"],
            }
        )
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
            rec = {
                "trial": row["trial"],
                "trial_dir": row["trial_dir"],
                "job": row["job"],
                "family": row["family"],
                "model": row["model"],
                "harness": row["harness"],
                "reason": "no ATIF trajectory",
            }
            lists["unaudited"].append(rec)
            labelled.append(
                {**rec, "verdict": "unaudited", "counts_verdict": row["counts_verdict"]}
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
    changed = write_labels(labelled, judge2)
    print(
        f"{len(labelled)} labels written to {labels_path(WORKTREE).relative_to(WORKTREE)} ({changed} new or changed)"
    )


if __name__ == "__main__":
    main()
