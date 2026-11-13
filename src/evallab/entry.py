"""Console entry point with a lightweight route for PR delivery."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


def run_ship(argv: Sequence[str]) -> int:
    from evallab.ship import register_parser, run_command

    root = argparse.ArgumentParser(prog="evallab")
    register_parser(root.add_subparsers(dest="command", required=True))
    args = root.parse_args(list(argv))
    return run_command(args, Path.cwd())


def main(argv: Sequence[str] | None = None) -> None:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments and arguments[0] == "ship":
        raise SystemExit(run_ship(arguments))
    from evallab.cli import main as full_main

    full_main(argv)
