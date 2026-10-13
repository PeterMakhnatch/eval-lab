"""Every passing trial Eval Lab has recorded, one row each.

Sources (read-only): the published results home (``~/Developer/eval-lab-results``,
``result.json`` with reward >= 1) and the trials census (``evallab trials``: runs
and jobs dirs in the primary checkout and every worktree, which also covers runs
never published). The two overlap through byte copies; rows are keyed by trial
directory name and the published copy wins.

Each row carries what is already known: the published counts verdict
(``processed/trial-*.json``), the census ``copy_verdict``, whether the harness is
a control (oracle, nop, probe, scripted CheatAgent) and an experiment family.

Usage (worktree root):
  EVALLAB_DERIVED_ROOT=~/Developer/eval-lab/derived/parquet \
    uv run python research/experiments/contamination-audit/inventory.py
Writes derived/contam/inventory.jsonl.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[3]
RESULTS = Path.home() / "Developer/eval-lab-results"
OUT = WORKTREE / "derived/contam"

CONTROL_HARNESS = re.compile(r"^(oracle|nop|probe|cheat)$|CheatAgent", re.I)

#: (family, regex on job name) in priority order; the published card prefix
#: (``HAR-123-``) wins when present.
FAMILIES = [
    ("HAR-126 G5 (ovn-g5)", r"ovn-g5|g5r[23]|har126"),
    ("HAR-168", r"har168"),
    ("HAR-161 exploit", r"har161"),
    ("HAR-202 cheater-recall", r"chrr1"),
    ("cheatbench-port", r"^cb-"),
    ("GEPA", r"gepa"),
    ("zai-opencode baselines", r"zai|opencode"),
    ("canary (codex)", r"canary"),
    ("antigravity", r"^agy|antigravity"),
    ("tamper/skip/ghfix probes", r"tamper|skip|ghfix|calib|git-00|admit"),
]


def family(job: str, card: str | None) -> str:
    if card and card != "unknown":
        named = {
            "HAR-126": "HAR-126 G5 (ovn-g5)",
            "HAR-161": "HAR-161 exploit",
            "HAR-202": "HAR-202 cheater-recall",
        }
        return named.get(card, card)
    for name, pattern in FAMILIES:
        if re.search(pattern, job, re.I):
            return name
    return "other"


def published() -> dict[str, dict]:
    out = {}
    for rj in RESULTS.glob("*/*/*/result.json"):
        trial = rj.parent
        try:
            r = json.loads(rj.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if "trial_name" not in r:
            continue
        reward = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        if not isinstance(reward, int | float) or reward < 1:
            continue
        job_dir = trial.parent
        m = re.match(r"(HAR-\d+|unknown)-(.*)", job_dir.name)
        card, job = (m.group(1), m.group(2)) if m else (None, job_dir.name)
        info = r.get("agent_info") or {}
        proc = job_dir / "processed" / f"trial-{trial.name}.json"
        counts = None
        if proc.is_file():
            try:
                counts = json.loads(proc.read_text()).get("counts")
            except (OSError, json.JSONDecodeError):
                counts = None
        out[trial.name] = {
            "trial": trial.name,
            "trial_dir": str(trial),
            "published": True,
            "card": card,
            "job": job,
            "task": (r.get("task_name") or r.get("task_id") or ""),
            "harness": info.get("name") or "",
            "model": (info.get("model_info") or {}).get("name"),
            "reward": reward,
            "counts_verdict": (counts or {}).get("verdict"),
            "counts_reasons": (counts or {}).get("reasons") or [],
        }
    return out


def census() -> list[dict]:
    sql = (
        "SELECT job, trial, task, model, harness, reward, copy_verdict, source_trial_dir "
        "FROM trials WHERE reward >= 1"
    )
    proc = subprocess.run(
        ["uv", "run", "evallab", "trials", "--sql", sql],
        cwd=WORKTREE,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [x for x in proc.stdout.split("\n") if x.strip()]
    head = lines[0].split("\t")
    return [dict(zip(head, x.split("\t"), strict=True)) for x in lines[1:]]


def main() -> None:
    rows = published()
    for c in census():
        name = Path(c["source_trial_dir"]).name
        row = rows.get(name)
        if row is None:
            row = rows[name] = {
                "trial": name,
                "trial_dir": c["source_trial_dir"],
                "published": False,
                "card": None,
                "job": c["job"],
                "task": c["task"],
                "harness": c["harness"],
                "model": None if c["model"] == "NULL" else c["model"],
                "reward": float(c["reward"]),
                "counts_verdict": None,
                "counts_reasons": [],
            }
        row["census_copy_verdict"] = None if c["copy_verdict"] == "NULL" else c["copy_verdict"]
        row.setdefault("census_dir", c["source_trial_dir"])
    for row in rows.values():
        row["control"] = bool(CONTROL_HARNESS.search(row["harness"] or ""))
        row["family"] = family(row["job"], row["card"])
        row["atif"] = (Path(row["trial_dir"]) / "agent/trajectory.json").is_file()
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "inventory.jsonl").open("w") as fh:
        for row in sorted(rows.values(), key=lambda r: (r["family"], r["job"], r["trial"])):
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    agents = [r for r in rows.values() if not r["control"]]
    print(
        f"{len(rows)} passes, {len(agents)} agent passes ({sum(r['atif'] for r in agents)} with ATIF)"
    )
    for fam, n in Counter(r["family"] for r in agents).most_common():
        print(f"  {n:4d} {fam}")


if __name__ == "__main__":
    sys.exit(main())
