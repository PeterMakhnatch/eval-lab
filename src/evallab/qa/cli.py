"""``evallab qa`` CLI: score a findings directory against an answer key."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evallab.qa.answer_key import resolve_answer_key
from evallab.qa.models import load_findings_dir
from evallab.qa.score import append_scoreboard, score_findings


def _score_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    try:
        key = resolve_answer_key(args.key, root)
        findings_dir = args.findings if args.findings.is_absolute() else root / args.findings
        findings = load_findings_dir(findings_dir)
        rows = score_findings(key, findings, split=args.split)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"error: evallab qa score: {exc}", file=sys.stderr)
        return 2
    scoreboard = (
        args.scoreboard
        if args.scoreboard.is_absolute()
        else root / args.scoreboard
    )
    append_scoreboard(rows, scoreboard)
    if args.json:
        print(json.dumps([row.model_dump() for row in rows], indent=2))
    else:
        for row in rows:
            print(
                f"{row.key_id} split={row.split} items={row.items} "
                f"recall={row.recall} root_cause_match={row.root_cause_match} "
                f"false_alarm_rate={row.false_alarm_rate} "
                f"paired_discrimination={row.paired_discrimination} "
                f"cost_per_item={row.cost_per_item:.4f}"
            )
        print(f"scoreboard: {scoreboard}")
    return 0


def build_qa_parser(commands: argparse._SubParsersAction) -> None:
    """Register the ``evallab qa`` subcommand."""
    qa = commands.add_parser(
        "qa",
        help="Shared QA contract: score findings against answer keys ($0)",
        description="Score qa.finding/v1 findings against a qa.answer_key/v1 key.",
    )
    sub = qa.add_subparsers(dest="qa_cmd", required=True)
    score = sub.add_parser("score", help="Score a findings directory against an answer key")
    score.add_argument("--key", required=True, help="Answer-key id or directory path")
    score.add_argument("--findings", type=Path, required=True, help="Findings directory")
    score.add_argument("--split", default=None, help="Only this split (default: every split)")
    score.add_argument(
        "--scoreboard",
        type=Path,
        default=Path("research/qa-scoreboard/scoreboard.csv"),
        help="Scoreboard CSV to append to",
    )
    score.add_argument("--json", action="store_true")
    score.set_defaults(func=_score_command)
