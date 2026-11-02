"""run_worker_proc.py: Run squad runner in a standalone process for kill testing."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dbos_squad import DBOSSquadRunner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--workflow-id", required=True, type=str)
    parser.add_argument("--app-version", type=str, default="v1.0.0")
    args = parser.parse_args()

    plan_file = args.run_dir / "plan.json"
    if not plan_file.exists():
        sys.stderr.write(f"Plan file {plan_file} not found\n")
        sys.exit(1)

    plan = json.loads(plan_file.read_text(encoding="utf-8"))

    runner = DBOSSquadRunner(
        db_path=args.db_path,
        app_name="dbos_squad",
        app_version=args.app_version,
    )

    routes = plan.get("routes", {})
    max_parallel = int(plan.get("max_parallel", 2))
    runner.launch(routes=routes, max_parallel=max_parallel)

    try:
        # Check if workflow exists for recovery
        try:
            handle = runner.retrieve_workflow(args.workflow_id)
            print(f"Recovering existing workflow {args.workflow_id}...", flush=True)
            res = handle.get_result()
        except Exception:
            print(f"Starting new workflow {args.workflow_id}...", flush=True)
            res = runner.run_squad(plan, args.run_dir, workflow_id=args.workflow_id)

        # Write final status to file
        status_file = args.run_dir / "status.json"
        status_file.write_text(json.dumps(res, indent=2), encoding="utf-8")
        print("Squad finished successfully:", res.get("status"), flush=True)
    finally:
        runner.destroy()


if __name__ == "__main__":
    main()
