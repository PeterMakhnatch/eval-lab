"""Run a bounded set of MiMo clean-set cells on Modal and record rewards.

Usage:
  run_cells.py --tasks 000079,000081 --cells nop,oracle [--attacks a,b] [--workers 2]

Every cell is one Harbor trial through the census runner's own `run_cell`
(`evallab.mimo_census`), i.e. the same path the fleet census uses. Package
paths come from the pinned clean-set manifest; no package is edited.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(os.environ.get("EVALLAB_REPO_ROOT", Path.cwd())).resolve()
PACKAGE_ROOT = Path(os.environ.get("EVALLAB_PACKAGE_ROOT", REPO))
if not (REPO / "src" / "evallab").is_dir():
    raise SystemExit("run from the eval-lab checkout, or set EVALLAB_REPO_ROOT")
sys.path.insert(0, str(REPO / "src"))

from evallab.execution_contracts import CHEAT_AGENT  # noqa: E402
from evallab.mimo_census import run_cell  # noqa: E402
from evallab.mimo_clean import summarize_trials  # noqa: E402

MANIFEST = REPO / "research/experiments/mimo-clean-v5/manifest.csv"


def rows() -> dict[str, dict[str, str]]:
    import csv

    with MANIFEST.open(newline="", encoding="utf-8") as handle:
        return {row["task_id"]: row for row in csv.DictReader(handle)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", required=True, help="comma-separated task suffixes or full ids")
    parser.add_argument("--cells", default="nop,oracle")
    parser.add_argument("--attacks", default=None, help="cheat attack subset (ladder v4 names)")
    parser.add_argument("--ladder", default=None, help="EVALLAB_CHEAT_LADDER_VERSION for cheat cells")
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--app", default="mimo-clean-census")
    parser.add_argument("--prefix", default="h2")
    args = parser.parse_args()

    os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
    if args.ladder:
        os.environ["EVALLAB_CHEAT_LADDER_VERSION"] = args.ladder
    table = rows()
    wanted = []
    for raw in args.tasks.split(","):
        token = raw.strip()
        if not token:
            continue
        task_id = token if token.startswith("format-code-task-") else f"format-code-task-{token}"
        wanted.append(task_id)
    missing = [t for t in wanted if t not in table]
    if missing:
        raise SystemExit(f"unknown tasks: {missing}")
    cells = [c.strip() for c in args.cells.split(",") if c.strip()]

    def one(job: tuple[str, str]) -> dict[str, object]:
        task_id, cell = job
        row = table[task_id]
        package = (PACKAGE_ROOT / row["package_path"]).resolve()
        agent = {"nop": "nop", "oracle": "oracle"}.get(cell, CHEAT_AGENT)
        attacks = args.attacks if cell == "cheat" else None
        name = f"{args.prefix}-{task_id.rsplit('-', 1)[-1]}-{cell}"
        job_dir = run_cell(
            package=package,
            agent=agent,
            name=name,
            jobs_dir=args.jobs_dir,
            backend="modal",
            timeout_seconds=args.timeout_seconds,
            attacks=attacks,
            modal_resource_policy="limit",
            modal_app_name=args.app,
            spend_tag="h2-pth-verifier",
            expected_digest=row["final_digest"],
            root=REPO,
        )
        rewards = summarize_trials(job_dir)
        return {
            "task_id": task_id,
            "cell": cell,
            "job": job_dir.name,
            "rewards": rewards,
            "reward": rewards[0] if rewards else None,
        }

    jobs = [(task_id, cell) for task_id in wanted for cell in cells]
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(one, jobs))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    for item in results:
        print(json.dumps(item, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
