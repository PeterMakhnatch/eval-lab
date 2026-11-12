"""``evallab hackbook`` CLI: query the catalog and its defensive playbook cards."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from evallab.hackbook.models import (
    coverage_summary,
    load_hackbook,
)
from evallab.hackbook.playbook_cards import (
    format_playbook_cards,
    list_playbook_cards,
    match_playbook_cards,
)


def _cards_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    if args.trigger_flags is None:
        cards = list_playbook_cards()
        if args.kind is not None:
            prefix = "QA" if args.kind == "task" else "TW"
            cards = [card for card in cards if card["card_id"].startswith(prefix)]
        if args.json:
            print(json.dumps(cards, ensure_ascii=False, separators=(",", ":")))
        else:
            for card in cards:
                print(
                    f"{card['card_id']}  {card['mechanism_summary'].splitlines()[0]}  "
                    f"flags={','.join(card['trigger_flags'])}"
                )
            print("Select full cards with --flag; these are leads, not verdicts.", file=sys.stderr)
        return 0
    try:
        cards = match_playbook_cards(args.trigger_flags, kind=args.kind, max_cards=args.max_cards)
    except ValueError as exc:
        print(f"error: evallab hackbook cards: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(cards, ensure_ascii=False, separators=(",", ":")))
    else:
        text = format_playbook_cards(cards)
        if text:
            print(text)
        print(f"{len(cards)} matching cards; leads, not verdicts.", file=sys.stderr)
    return 0


def _coverage_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    cov = coverage_summary(load_hackbook(root), root=root)
    if args.json:
        print(json.dumps(cov, indent=2))
        return 0
    print(
        f"Hackbook coverage: {cov['total_entries']} entries "
        f"({cov['task_side_entries']} task-side, {cov['run_side_entries']} run-side), "
        f"{cov['entries_with_executed_evidence']}/{cov['total_entries']} "
        f"({cov['executed_entry_share'] * 100:.1f}%) with executed evidence, "
        f"{cov['total_examples']} examples ({cov['executed_examples']} executed, "
        f"{cov['observed_examples']} observed, {cov['claimed_examples']} claimed)"
    )
    print("\nBy taxonomy class:")
    for row in cov["by_class"]:
        print(
            f"  {row['taxonomy_class_id']} {row['taxonomy_class']:<32} "
            f"entries={row['entries']:>2} (exec={row['executed_entries']:>2})  "
            f"examples={row['examples']:>2} (exec={row['executed_examples']:>2} "
            f"obs={row['observed_examples']:>2} claimed={row['claimed_examples']:>2})"
        )
    print("\nBy source:")
    for row in cov["by_source"]:
        print(
            f"  {row['source']:<26} entries={row['entries']:>2} (exec={row['executed_entries']:>2})  "
            f"examples={row['examples']:>2} (exec={row['executed_examples']:>2} "
            f"obs={row['observed_examples']:>2} claimed={row['claimed_examples']:>2})  "
            f"classes={','.join(row['classes'])}"
        )
    return 0


def build_hackbook_parser(commands: argparse._SubParsersAction) -> None:
    """Register the ``evallab hackbook`` subcommand."""
    hackbook = commands.add_parser(
        "hackbook",
        help="Query the Hackbook catalog and defensive playbook cards ($0)",
        description="List, show, search, and inspect Hackbook coverage and cards.",
    )
    sub = hackbook.add_subparsers(dest="hackbook_cmd", required=True)


    cards = sub.add_parser(
        "cards",
        help="List compact playbook leads or disclose up to two matching cards",
        description="Without flags, list a brief index (or the explicit full catalog with --json). "
        "With --flag, disclose matched defensive playbooks, never raw traces or verdicts.",
    )
    cards.add_argument(
        "--flag", dest="trigger_flags", action="append", metavar="NAME",
        help="Fired flag name; repeat to match multiple flags",
    )
    cards.add_argument("--kind", choices=("task", "trace"), help="Task QA/seeker or trace cards")
    cards.add_argument("--max-cards", type=int, default=2, help="Maximum matched cards (default: 2)")
    cards.add_argument("--json", action="store_true", help="Emit card objects as a compact JSON array")
    cards.set_defaults(func=_cards_command)


    coverage = sub.add_parser(
        "coverage", help="Print coverage tables by taxonomy class, source, and executed evidence"
    )
    coverage.add_argument("--json", action="store_true")
    coverage.set_defaults(func=_coverage_command)
