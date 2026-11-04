"""Console entry point with lightweight routes for QA data and PR delivery."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


def run_qa_data(argv: Sequence[str]) -> int:
    from evallab.production.qa_data import register_parser

    root = argparse.ArgumentParser(
        prog="evallab", description="Run, inspect, and analyze agent evaluations through Harbor.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    register_parser(commands)
    args = root.parse_args(list(argv))
    try:
        return args.func(args, Path(__file__).resolve().parents[2], harbor=None)
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def run_ship(argv: Sequence[str]) -> int:
    from evallab.ship import register_parser, run_command

    root = argparse.ArgumentParser(prog="evallab")
    register_parser(root.add_subparsers(dest="command", required=True))
    args = root.parse_args(list(argv))
    return run_command(args, Path.cwd())


def main(argv: Sequence[str] | None = None) -> None:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments and arguments[0] == "qa-data":
        raise SystemExit(run_qa_data(arguments))
    if arguments and arguments[0] == "ship":
        raise SystemExit(run_ship(arguments))
    from evallab.cli import main as full_main

    full_main(argv)
