"""``evallab failures bank``: the issue-bank inventory (build, stats, verify)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evallab.failure_library.bank import BankContext, IssueBank, available_sources


def _run(args: argparse.Namespace, root: Path, **_: Any) -> int:
    bank = IssueBank(root, state_root=args.state_root)
    if args.bank_command == "gold-counts":
        from evallab.gold import load_rows
        from evallab.gold.counts import counts

        result = counts(load_rows(root, args.state_root))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.bank_command == "sources":
        for slug, module in available_sources().items():
            print(f"{slug}\t{module}")
        return 0
    if args.bank_command == "verify":
        problems = bank.verify()
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    if args.bank_command == "build":
        ctx = BankContext(repo_root=root, state_root=bank.state_root)
        manifest = bank.build(ctx, args.source)
    else:
        manifest = bank.read_manifest()
    if getattr(args, "json", False):
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    _print(manifest)
    return 0


def _print(manifest: dict[str, Any]) -> None:
    totals = manifest.get("totals", {})
    print(f"issue bank: {totals.get('items', 0)} items")
    for combo, number in totals.get("by_polarity_split", {}).items():
        print(f"  {combo}: {number}")
    for slug, entry in manifest.get("sources", {}).items():
        kinds = ", ".join(f"{k}={v}" for k, v in entry.get("by_kind", {}).items())
        splits = ", ".join(f"{k}={v}" for k, v in entry.get("by_split", {}).items())
        print(f"{slug}: {entry.get('count', 0)} items [{kinds}] [{splits}]")
        for summary in entry.get("summaries", []):
            counts = ", ".join(f"{k}={v}" for k, v in summary["counts"].items())
            print(f"  summary only ({summary['split']}, {summary['kind']}): {counts}")
