"""Run with python -m evallab.gepa_optimizer; shared Lab CLI is unchanged."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from .evaluator import LabEvaluator
from .workflow import _path, load_campaign, run_campaign


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "qualify-meta"))
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--qualification",
        action="store_true",
        help="GEPA with deterministic proposer and real local controls; not learned improvement",
    )
    parser.add_argument(
        "--proposer-approval-ref",
        type=Path,
        help="Explicit operator authorization record for actual proposer execution; never approves target trials",
    )
    args = parser.parse_args()
    if args.command == "run":
        report = run_campaign(
            args.campaign,
            repo_root=args.repo_root,
            qualification=args.qualification,
            proposer_approval_ref=args.proposer_approval_ref,
        )
    else:
        from .meta_engine import qualify_meta_harness

        root = args.repo_root.resolve()
        config = load_campaign(args.campaign, root)
        if config["agent"] not in {"oracle", "nop"}:
            parser.error(
                "qualify-meta permits only native local controls, never a proposer/model invocation"
            )
        output = _path(root, config["output_dir"])
        evaluator = LabEvaluator(
            repo_root=root,
            output_dir=output / "lab",
            examples=config["examples"],
            agent=config["agent"],
            model=None,
            timeout_seconds=config.get("timeout_seconds", 1200),
        )
        report = qualify_meta_harness(
            evaluator=evaluator,
            examples=config["examples"],
            seed_candidate=_path(root, config["seed_candidate_path"]).read_text(),
            output_dir=output / ("meta-qualification-" + uuid.uuid4().hex),
            validation_task_ids=config.get("validation_task_ids", []),
            max_evals=config["max_evals"],
        )
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report.get("status") == "completed" or report.get("qualified") else 2


if __name__ == "__main__":
    raise SystemExit(main())
