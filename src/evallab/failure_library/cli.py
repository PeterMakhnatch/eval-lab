"""Explicit, lightweight Failure Library command registration."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def _run(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.failure_library.commands.bank import _run as command

    return command(args, root, **kwargs)


def build_failures_parser(commands: Any) -> argparse.ArgumentParser:
    parser = commands.add_parser(
        "failures",
        help="Failure Library: provenance-first broken tasks and failed or hacked runs",
    )
    subparsers = parser.add_subparsers(dest="failures_command", required=True)
    register_bank(subparsers)
    return parser


def register_bank(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "bank", help="Issue bank: one inventory of every known-issue item across sources"
    )
    actions = parser.add_subparsers(dest="bank_command", required=True)
    build = actions.add_parser("build", help="Rebuild source item files and the manifest")
    build.add_argument(
        "--source", action="append", default=None, help="Rebuild only this source (repeatable)"
    )
    stats = actions.add_parser("stats", help="Counts by source, kind, polarity and split")
    stats.add_argument("--json", action="store_true", help="Print the manifest as JSON")
    gold_counts = actions.add_parser("gold-counts", help="Count seed labels, actions, assets and exposure")
    gold_counts.add_argument("--json", action="store_true", help="Print the counts as JSON")
    actions.add_parser("sources", help="List the registered source slugs")
    actions.add_parser("verify", help="Check local item bytes against the manifest")
    for action in (build, stats, gold_counts):
        action.add_argument("--state-root", type=Path, default=None)
    parser.set_defaults(func=_run, state_root=None)
