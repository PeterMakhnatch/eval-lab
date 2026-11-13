"""Deterministic cheat-audit command registration."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

DEFAULT_LADDER_VERSION = "1.4.0"
LADDER_VERSIONS = ("1.4.0", "2.0.0", "3.0.0", "4.0.0")
CHEAT_DESCRIPTION = "Cheat-audit lane: fixed attack ladder, verdicts from the verifier (HAR-204)."


def _cheat_run_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.cheat import _cheat_run_command as command

    return command(args, root, **kwargs)


def register_parser(commands: argparse._SubParsersAction) -> None:
    """Register the ``evallab cheat`` subcommand (one self-contained block)."""
    cheat = commands.add_parser(
        "cheat",
        help="Deterministic cheat audit on local Docker ($0, model-free)",
        description=CHEAT_DESCRIPTION,
    )
    sub = cheat.add_subparsers(dest="cheat_cmd", required=True)
    runner = sub.add_parser("run", help="Run the fixed attack ladder against one task")
    runner.add_argument("--task", type=Path, required=True, help="Task directory")
    runner.add_argument("--name", required=True, help="Harbor job name")
    runner.add_argument("--jobs-dir", type=Path, default=Path("runs"))
    runner.add_argument("--attempts", type=int, default=1)
    runner.add_argument(
        "--attacks",
        default=None,
        help="comma-separated subset for the selected ladder; default: full ladder",
    )
    runner.add_argument(
        "--ladder-version",
        choices=list(LADDER_VERSIONS),
        default=None,
        help=f"ladder generation (default: {DEFAULT_LADDER_VERSION})",
    )
    runner.add_argument(
        "--timeout-seconds",
        type=int,
        default=600,
        help="executor wall-clock allowance per attempted trial",
    )
    runner.set_defaults(func=_cheat_run_command)
