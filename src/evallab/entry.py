"""Console-script entry point: ``qa-data`` skips the full CLI, everything else is unchanged.

``evallab.cli`` imports the whole analysis stack and builds every command's parser, about 0.7 s
before any command runs. ``qa-data`` is called many times per QA task and needs none of it, so
it gets a parser of its own; any other command goes to ``evallab.cli.main`` as before.
"""

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


def main(argv: Sequence[str] | None = None) -> None:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments and arguments[0] == "qa-data":
        raise SystemExit(run_qa_data(arguments))
    from evallab.cli import main as full_main

    full_main(argv)
