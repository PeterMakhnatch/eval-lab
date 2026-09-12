"""GEPA search and retained-receipt analysis; shared Lab CLI is unchanged.

Analyze existing development receipts without execution or optional GEPA:
    python -m evallab.gepa_optimizer analyze --campaign-dir PATH \\
        --stock-candidate sha256:DIGEST --seed 17 --out REPORT.json

An optional --control-dir reports evaluation-count compatibility, not matched
spend or verified no-feedback provenance. Search scores are not held-out gains.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from .evaluator import LabEvaluator
from .paired_analysis import analyze_campaign
from .workflow import _path, load_campaign, run_campaign


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "qualify-meta", "analyze"))
    parser.add_argument("campaign", type=Path, nargs="?")
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
    parser.add_argument(
        "--campaign-dir",
        type=Path,
        help="analyze: campaign output directory holding evaluations/ receipts",
    )
    parser.add_argument(
        "--stock-candidate",
        help="analyze: exact full sha256:<64-hex> digest of the stock/seed arm",
    )
    parser.add_argument(
        "--control-dir",
        type=Path,
        help="analyze: no-feedback control campaign directory for the method control",
    )
    parser.add_argument(
        "--source-groups",
        type=Path,
        help="analyze: JSON file mapping task_id to bootstrap cluster group",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="analyze: bootstrap seed; required and deliberately without a default",
    )
    parser.add_argument(
        "--resamples",
        type=int,
        default=2000,
        help="analyze: cluster bootstrap resample count (default: 2000)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="analyze: write the JSON report to this path instead of stdout",
    )
    args = parser.parse_args()
    if args.command == "analyze":
        if args.campaign is not None:
            parser.error("analyze takes --campaign-dir, not a positional campaign path")
        if args.campaign_dir is None:
            parser.error("analyze requires --campaign-dir")
        if args.stock_candidate is None:
            parser.error("analyze requires --stock-candidate")
        if args.seed is None:
            parser.error("analyze requires --seed (reports must state their seed)")
        source_groups = None
        try:
            if args.source_groups is not None:
                raw_groups = json.loads(args.source_groups.read_text(encoding="utf-8"))
                if not isinstance(raw_groups, dict) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in raw_groups.items()
                ):
                    raise ValueError(
                        "--source-groups must be a JSON object mapping task_id to group"
                    )
                source_groups = raw_groups
            report = analyze_campaign(
                args.campaign_dir,
                stock_candidate=args.stock_candidate,
                control_dir=args.control_dir,
                source_groups=source_groups,
                seed=args.seed,
                resamples=args.resamples,
            )
        except (ValueError, OSError) as exc:
            print(f"analyze refused: {exc}", file=sys.stderr)
            return 2
        payload = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if args.out is not None:
            args.out.write_text(payload, encoding="utf-8")
        else:
            print(payload, end="")
        return 0
    if args.campaign is None:
        parser.error("the following arguments are required: campaign")
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
