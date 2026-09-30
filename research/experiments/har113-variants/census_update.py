#!/usr/bin/env python3
"""Rebuild HAR-108's Python census with HAR-113's and HAR-115's nops.

1. ``evallab tasks qualify-collect`` over every nop job the census has used
   (HAR-104's ``mimo-qual-*``, HAR-95, HAR-105, HAR-108) plus HAR-113's
   ``har113-nop-*`` (originals), ``har113-vnop-*`` and ``har113-rnop-*``
   (variants, in its locked worktree) and HAR-115's ``har115-nop-*`` and
   ``har115-rnop-*`` (this checkout), rewriting the shared catalog's
   ``task_qualification.parquet``. Oracle probes and diagnoses
   (``*-probe-*``, ``*-diag*``) are not nops and stay out.
2. ``evallab tasks health-collect`` over HAR-108's pool into
   ``research/experiments/har108-python-census/task_health.parquet`` and its
   ``SUMMARY.md``. Variant nops carry variant digests, which are not in the
   pool, so they never relabel an original task.

Job lists go through argv, never a shell string. A worktree the nightly
``wt-prune`` shelved keeps its ``runs/`` under ``~/.local/share/wt-archive``;
that copy is read instead (HAR-104's ``mimo-ops``).

Usage (from the worktree root):
    uv run python research/experiments/har113-variants/census_update.py
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from common import CENSUS, ROOT

WORKTREES = ROOT.parent
ARCHIVE = Path.home() / ".local/share/wt-archive/eval-lab"


def runs_of(worktree: str) -> Path:
    """A worktree's ``runs/``, or its wt-prune archive copy once shelved."""
    live = WORKTREES / worktree / "runs"
    return live if live.is_dir() else ARCHIVE / worktree / "runs"


PATTERNS = {
    runs_of("mimo-ops"): ("mimo-qual-*",),
    runs_of("har95-nop-20260929"): ("har95-qual-*",),
    runs_of("har105-explore-20260929"): ("har105-qual-*",),
    runs_of("har105-taskfix-20260929"): (),
    runs_of("har105-python-20260930"): ("har105-qual-py-*",),
    runs_of("har108-census-20260930"): ("har108-nop-*",),
    runs_of("har113-variants-20260930"): ("har113-nop-*", "har113-vnop-*", "har113-rnop-*"),
    ROOT / "runs": ("har115-nop-*", "har115-rnop-*"),
}
FIXES = WORKTREES / "har105-taskfix-20260929/research/experiments/har105-exploration/fixes.json"


def taskfix_jobs() -> list[Path]:
    """HAR-105's after-fix nops, named in its fixes.json."""
    if not FIXES.is_file():
        return []
    names: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "nop_after" and isinstance(value, dict) and value.get("job_name"):
                    names.append(value["job_name"])
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(FIXES.read_text()))
    return [runs_of("har105-taskfix-20260929") / name for name in names]


def jobs() -> list[str]:
    found = [
        path
        for root, patterns in PATTERNS.items()
        for pattern in patterns
        for path in root.glob(pattern)
        if not path.name.endswith("-hung-01")
    ]
    found += taskfix_jobs()
    return sorted({str(path) for path in found if path.is_dir()})


def main() -> None:
    job_dirs = jobs()
    print(f"{len(job_dirs)} nop jobs")
    subprocess.run(
        [
            "uv",
            "run",
            "evallab",
            "tasks",
            "qualify-collect",
            *job_dirs,
            "--backend-rate-card",
            "daytona",
        ],
        cwd=ROOT,
        check=True,
    )
    roots = [arg for root in PATTERNS if root.is_dir() for arg in ("--jobs-root", str(root))]
    subprocess.run(
        [
            "uv",
            "run",
            "evallab",
            "tasks",
            "health-collect",
            "--pool",
            str(CENSUS / "pool.json"),
            *roots,
            "--pypi",
            str(CENSUS / "pypi.json"),
            "--output",
            str(CENSUS / "task_health.parquet"),
            "--summary",
            str(CENSUS / "SUMMARY.md"),
        ],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    main()
