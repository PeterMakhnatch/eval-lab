"""``evallab qa`` CLI: shared scoring, frozen suites, task and trace QA."""

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
        print(f"loaded {len(findings)} qa.finding/v1 findings from {findings_dir}", file=sys.stderr)
        rows = score_findings(
            key,
            findings,
            split=args.split,
            agent=args.agent,
            system=args.system,
            status=args.status,
            notes=args.notes,
        )
    except (OSError, ValueError, ValidationError) as exc:
        print(f"error: evallab qa score: {exc}", file=sys.stderr)
        return 2
    scoreboard = args.scoreboard if args.scoreboard.is_absolute() else root / args.scoreboard
    append_scoreboard(rows, scoreboard)
    if args.json:
        print(json.dumps([row.model_dump() for row in rows], indent=2))
    else:
        for row in rows:
            print(
                f"{row.key_id} split={row.split} category={row.category} "
                f"measured={row.n_measured}/{row.n_items} recall={row.recall} "
                f"false_alarm_rate={row.false_alarm_rate} "
                f"root_cause_match={row.root_cause_match} "
                f"paired_discrimination={row.paired_discrimination}"
            )
        print(f"scoreboard: {scoreboard}")
    return 0


