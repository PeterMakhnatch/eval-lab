#!/usr/bin/env python3
"""Regenerate controls.json from tonight's loopool control job directories.

Reads each runs/loopool-{oracle,nop}-* job directory in this worktree and
records agent, task identity, pinned package digest, reward, exception, wall
seconds, and a verdict. Deterministic; no execution.

Usage: uv run python research/experiments/reef-loop-pool-20260925/controls.py
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

EXP05_ROOT = Path.home() / "Developer/research-context/reef/experiments/exp05/tasks"
EXP05_KINDS = {
    "fix-median": "bugfix", "fix-slugify": "bugfix",
    "sales-total": "data", "inventory-value": "data",
    "error-count": "logs", "status-codes": "logs",
    "top-words": "text", "top-tags": "text",
}
REGISTRY_DIGESTS = {
    "transaction-reconciliation": "sha256:f2bb698dbcb990ce1be2a6319efc1c4264da4f7394637d33f103ebb053262820",
    "query-optimize": "sha256:d593fa7d67498305ebcc32605455abd16e6d5495f6f455b458c024572617bad1",
    "terminal-bench-html-js-filter": "sha256:36bef48eb1f5a2ed2211705ddd23dab1f98cb0158196e892fad8d3dd3a4aa956",
}


def trial_rows(job: Path) -> list[dict[str, object]]:
    rows = []
    for result in sorted(job.glob("loopool-*/result.json")):
        payload = json.loads(result.read_text())
        verifier = payload.get("verifier_result") or {}
        rewards = verifier.get("rewards") or {}
        started, finished = payload.get("started_at"), payload.get("finished_at")
        wall = None
        if started and finished:
            with contextlib.suppress(ValueError):
                wall = round(
                    (
                        datetime.fromisoformat(finished) - datetime.fromisoformat(started)
                    ).total_seconds(),
                    3,
                )
        rows.append(
            {
                "trial": result.parent.name,
                "reward": rewards.get("reward"),
                "exception": (payload.get("exception_info") or {}).get("exception_type"),
                "started_at": started,
                "finished_at": finished,
                "wall_seconds": wall,
                "result_path": str(result.relative_to(REPO)),
            }
        )
    return rows


def main() -> int:
    runs = REPO / "runs"
    jobs = sorted(path for path in runs.glob("loopool-*") if path.is_dir())
    entries = []
    for job in jobs:
        _, agent, *task_parts = job.name.split("-")
        task_key = "-".join(task_parts)
        meta_path = job / "lab-metadata.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        staging = meta.get("task_staging", {})
        package_digest = staging.get("source_package_digest")
        base = staging.get("source_task_basename") or task_key
        exp05 = (EXP05_ROOT / "search" / base).exists() or (EXP05_ROOT / "holdout" / base).exists()
        duration = None
        for state in job.rglob("*.state.json"):
            with contextlib.suppress(json.JSONDecodeError):
                duration = json.loads(state.read_text()).get("duration_seconds", duration)
        rows = trial_rows(job)
        rewards = [row["reward"] for row in rows]
        exceptions = [row["exception"] for row in rows if row["exception"]]
        verdict = "infra-failed" if exceptions or not rows else None
        if verdict is None:
            expected = 1.0 if agent == "oracle" else 0.0
            verdict = "valid" if all(reward == expected for reward in rewards) else "invalid"
        entries.append(
            {
                "job": job.name,
                "agent": agent,
                "task": base,
                "source": "exp05" if exp05 else "library/tasks",
                "package_digest": package_digest,
                "at_registry_current_digest": (
                    package_digest == REGISTRY_DIGESTS.get(base) if base in REGISTRY_DIGESTS else None
                ),
                "trials": rows,
                "wall_seconds": duration,
                "verdict": verdict,
            }
        )
    known = {e["job"] for e in entries}
    job_aliases = {"terminal-bench-html-js-filter": "html-js-filter"}
    expected_prefixes = tuple(
        f"loopool-{agent}-{job_aliases.get(task, task)}"
        for task in [*EXP05_KINDS, *REGISTRY_DIGESTS]
        for agent in ("oracle", "nop")
    )
    missing = [prefix for prefix in expected_prefixes if not any(e.startswith(prefix) for e in known)]
    report = {
        "schema_version": 1,
        "generated": "2026-09-25",
        "rule": "policy/standing-approvals.yaml auto_run local-controls (oracle, nop) on local Docker, <=2 concurrent Harbor trials",
        "execution": "uv run evallab run in this worktree; ephemeral catalog evallab_loopool_20260925 (dropped after proof); owned derived root",
        "verdict_rule": "valid iff the expected control reward was observed with no exception; infra-failed when any trial excepted or no trial ran",
        "entries": entries,
        "missing_jobs": missing,
    }
    target = Path(__file__).resolve().parent / "controls.json"
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {target} ({len(entries)} entries, missing: {missing})")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
