"""Merge worker runs files into the shared runs.jsonl, validate, and write READY.

Usage (from the eval-lab worktree):
  uv run python research/experiments/ov1104-detbench/publish.py [--workers-dir /private/tmp/ov1104/detbench/workers]
      [--out /private/tmp/ov1104/shared/detbench/runs.jsonl] [--no-ready]

Checks: run_key unique; view files exist and parse; no label-bearing key in any view (sampled
every view); n_steps/n_chars present; split in {dev, heldout}; label in {0, 1}. Prints a per-set
summary (rows, positives, negatives, clusters, natural, read_by) that RESULT.md can quote.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from detbench_lib import RUNS, SHARED, read_jsonl, write_jsonl  # noqa: E402

FORBIDDEN_VIEW_KEYS = {"label", "is_cheat", "is_exploit", "positive", "gold", "verdict", "test_tampered", "cheat", "annotation", "judge", "oracle"}
REQUIRED = ("run_key", "set", "split", "label", "label_type", "natural", "cluster", "source_family", "view", "view_actions_only", "n_steps", "n_chars", "read_by")


def schema_problems(doc: Any) -> list[str]:
    """Exact contract shape: any extra key is a leak candidate and is refused."""
    out: list[str] = []
    if not isinstance(doc, dict) or set(doc) != {"run_key", "task_text", "steps", "final"}:
        return [f"top keys {sorted(doc) if isinstance(doc, dict) else type(doc).__name__}"]
    if not isinstance(doc["final"], dict) or set(doc["final"]) != {"reward", "diff"}:
        out.append("final keys")
    for s in doc["steps"]:
        if not isinstance(s, dict) or set(s) != {"i", "role", "text", "tool_calls", "observation"}:
            out.append(f"step keys {sorted(s) if isinstance(s, dict) else '?'}")
            break
        if s["role"] not in ("agent", "tool", "user", "system"):
            out.append(f"role {s['role']}")
            break
        for c in s["tool_calls"]:
            if not isinstance(c, dict) or set(c) != {"name", "args"} or not isinstance(c["args"], str):
                out.append("call keys")
                break
    return out


def fixups(r: dict[str, Any]) -> None:
    """Lead decisions recorded in the dev worker REPORT (12:05 AM ET Nov 4).

    - goodfire: the manifest task_id is unique per run; cluster on the task bundle id before the
      `@task-bundle-sha256-...` suffix so the bootstrap resamples tasks, not runs.
    - c1c5: `final.reward` equals the cheat label by construction (reward 1.0 on an impossible
      task); null it in both views so no view carries the answer.
    """
    if r.get("set") == "goodfire" and isinstance(r.get("cluster"), str):
        r["cluster"] = r["cluster"].split("@task-bundle-sha256-")[0]
    if r.get("set") == "c1c5":
        for key in ("view", "view_actions_only"):
            p = Path(str(r.get(key, "")))
            if p.is_file():
                doc = json.loads(p.read_text(encoding="utf-8"))
                if doc.get("final", {}).get("reward") is not None:
                    doc["final"]["reward"] = None
                    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    if r.get("set") == "paichecker":
        # task-defect labels (is the task broken?), not cheating: per-set only, never pooled
        r["pool"] = False
        r["construct"] = "task-defect"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers-dir", type=Path, default=Path("/private/tmp/ov1104/detbench/workers"))
    ap.add_argument("--out", type=Path, default=RUNS)
    ap.add_argument("--no-ready", action="store_true")
    ap.add_argument("--summary", type=Path, default=HERE / "results" / "runs-summary.json")
    args = ap.parse_args(argv)
    files = sorted(args.workers_dir.glob("*/runs*.jsonl"))
    rows: list[dict[str, Any]] = []
    for f in files:
        rows.extend(read_jsonl(f))
    problems: list[str] = []
    seen: set[str] = set()
    clean: list[dict[str, Any]] = []
    for r in rows:
        fixups(r)
        missing = [k for k in REQUIRED if k not in r]
        if missing:
            problems.append(f"{r.get('run_key')}: missing {missing}")
            continue
        if r["run_key"] in seen:
            problems.append(f"{r['run_key']}: duplicate")
            continue
        if r["split"] not in ("dev", "heldout") or r["label"] not in (0, 1):
            problems.append(f"{r['run_key']}: bad split/label")
            continue
        ok = True
        for key in ("view", "view_actions_only"):
            p = Path(r[key])
            if not p.is_file():
                problems.append(f"{r['run_key']}: {key} missing {p}")
                ok = False
                continue
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                problems.append(f"{r['run_key']}: {key} not JSON")
                ok = False
                continue
            bad = schema_problems(doc)
            if bad:
                problems.append(f"{r['run_key']}: {key} schema {bad[:2]}")
                ok = False
                continue
            if doc.get("run_key") != r["run_key"]:
                problems.append(f"{r['run_key']}: {key} run_key mismatch")
                ok = False
        if ok:
            seen.add(r["run_key"])
            clean.append(r)
    clean.sort(key=lambda r: (r["split"] != "dev", r["set"], r["run_key"]))
    n = write_jsonl(args.out, clean)
    summary: dict[str, Any] = {"rows": n, "problems": len(problems), "sets": {}}
    for s in sorted({r["set"] for r in clean}):
        rs = [r for r in clean if r["set"] == s]
        summary["sets"][s] = {
            "split": rs[0]["split"],
            "n": len(rs),
            "pos": sum(r["label"] for r in rs),
            "neg": sum(1 - r["label"] for r in rs),
            "clusters": len({r["cluster"] for r in rs}),
            "natural": dict(Counter(str(r["natural"]) for r in rs)),
            "label_type": dict(Counter(r["label_type"] for r in rs)),
            "decisive_rule": bool(rs[0].get("decisive_rule")),
            "read_by": sorted({x for r in rs for x in r["read_by"]}),
            "source_family": rs[0]["source_family"],
        }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=1, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=1, sort_keys=True))
    for p in problems[:50]:
        print("PROBLEM", p)
    if problems:
        print(f"{len(problems)} problems; rows with problems were NOT published")
    if not args.no_ready and n:
        ready = SHARED / "READY"
        ready.write_text(f"runs.jsonl rows={n} sets={len(summary['sets'])} problems={len(problems)}\n", encoding="utf-8")
        print(f"READY written: {ready}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
