"""Shared types, the fail-closed DEV split guard, index loading and small statistics helpers.

Authoritative DEV grouping comes from ``research/answer-keys/splits/assignments.csv`` (key
``tb4-audit-v1``, unit prefix ``terminal-bench/``). A task not assigned ``dev`` there is never
DEV, even if it is "unassigned": every export and every loader below fails closed.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from evallab.analysis_statistics import wilson_score_interval

SYSTEM = "tb4-dev-run-census/1"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
ASSIGNMENTS = ROOT / "research/answer-keys/splits/assignments.csv"
KEY_ITEMS = ROOT / "research/answer-keys/tb4-audit-v1/items.jsonl"
KEY_ID = "tb4-audit-v1"
RESULTS = Path.home() / "Developer/eval-lab-results/2026-10-21/tb4-dev-census"
HUB = Path.home() / "Developer/eval-lab-results/tb4-hub"
LEGACY = Path.home() / "Developer/eval-lab-results/2026-10-20/tb4-run-census"
LITE = Path.home() / "Developer/eval-lab-results/2026-10-20/tb4-failure-clusters/trials-lite"

#: The allowlist from the parent's split computation. The CSV must reproduce it exactly.
DEV_ALLOWLIST = frozenset({
    "cad-model", "cargo-flight-dispatch", "distributed-dedup", "fin-saccr-rwa", "freecad-impeller",
    "freecad-platform-drawing", "freecad-spring-clip", "gsea-proteomics", "heat-pump-warranty",
    "html-js-filter", "jax-speedrun-gpu", "layout-config-recreation2", "live-database-cutover",
    "math-eval-grader", "nextjs-performance", "photonic-waveguide-routing",
    "satb-audio-transcription", "sglang-qwen-burst", "uefi-bootkit", "vba-userform-port",
    "vf2-speedup-networkx", "vpp-loss-divergence",
})
#: Of the eight historical RC-1 tasks, only these two are DEV.
LEGACY_DEV_TASKS = frozenset({"distributed-dedup", "vf2-speedup-networkx"})

# Final labels (side-specific) and statuses.
KNOWN_EXPLOIT = "known_exploit"
TASK_DEFECT = "task_defect"
TASK_DEFECT_GRAY = "task_defect_gray"
NEITHER = "neither"
EXPLOIT_CANDIDATE = "exploit_candidate"  # signature or judge hit without a deterministic proof
NO_MECHANISM = "no_documented_mechanism"  # task has no operationalized mechanism on this side
UNMEASURED = "unmeasured"  # evidence cannot decide, missing, judge error or budget refusal
UNSCORED = "unscored"  # reward is null
LABELS = (KNOWN_EXPLOIT, EXPLOIT_CANDIDATE, TASK_DEFECT, TASK_DEFECT_GRAY, NEITHER, NO_MECHANISM,
          UNMEASURED, UNSCORED)


def load_dev_tasks(assignments: Path = ASSIGNMENTS) -> frozenset[str]:
    rows = [r for r in csv.DictReader(assignments.open(encoding="utf-8"))
            if r["key_id"] == KEY_ID and r["unit"].startswith("terminal-bench/")]
    splits: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        splits[row["task"]].add(row["split"])
    ambiguous = sorted(t for t, s in splits.items() if len(s) != 1)
    if ambiguous:
        raise SystemExit(f"tasks with !=1 split in {assignments.name}: {ambiguous}")
    dev = frozenset(t for t, s in splits.items() if s == {"dev"})
    if dev != DEV_ALLOWLIST:
        raise SystemExit(f"DEV set drifted from the allowlist: +{sorted(dev - DEV_ALLOWLIST)} "
                         f"-{sorted(DEV_ALLOWLIST - dev)}")
    return dev


DEV_TASKS = load_dev_tasks()


def assert_dev(task: str) -> str:
    if task not in DEV_TASKS:
        raise SystemExit(f"refusing non-DEV task {task!r}")
    return task


def dev_only(rows: Iterable[dict[str, Any]], key: str = "task") -> list[dict[str, Any]]:
    """Drop non-DEV rows. Callers must filter BEFORE any content-level use."""
    return [r for r in rows if r.get(key) in DEV_TASKS]


def read_jsonl_dev(path: Path, key: str = "task", prefix: str = "") -> list[dict[str, Any]]:
    """Parse only lines that mention a DEV task; never parse (or print) the rest."""
    needles = tuple(f'"{key}":"{prefix}{t}"' for t in DEV_TASKS) + tuple(
        f'"{key}": "{prefix}{t}"' for t in DEV_TASKS)
    out = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if any(n in line for n in needles):
                row = json.loads(line)
                if str(row.get(key)).removeprefix(prefix) in DEV_TASKS:
                    out.append(row)
    return out


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                            + "\n" for r in rows), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rate(k: int, n: int) -> dict[str, Any]:
    if n == 0:
        return {"k": k, "n": 0, "rate": None, "wilson95": None}
    ci = wilson_score_interval(k, n)
    return {"k": k, "n": n, "rate": k / n, "wilson95": [ci.lower, ci.upper]}


def fmt(r: dict[str, Any]) -> str:
    if not r["n"]:
        return "–"
    lo, hi = r["wilson95"]
    return f"{r['k']}/{r['n']} = {r['rate']:.1%} [{lo:.1%}, {hi:.1%}]"


def membership_id(m: dict[str, Any]) -> str:
    """Stable id of one frozen board membership: row x task x Hub trial."""
    return f"{m['row_id']}:{m['task']}:{m['trial_id']}"


def board_row_label(m: dict[str, Any]) -> str:
    return f"{m.get('agent', '?')} · {m['model']} · {m['effort']}"


def out_dir(bootstrap: bool) -> Path:
    """External results dir; ``bootstrap`` (development index from archives on disk) never mixes with full."""
    return RESULTS / ("bootstrap" if bootstrap else "full")


#: One cumulative ledger/cache for the whole item (bootstrap and full runs share it).
JUDGE_DIR = RESULTS / "judge"
