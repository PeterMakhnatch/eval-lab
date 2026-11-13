"""DEV line command registration, separate from decision implementations."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def _line_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.line.cli import _command

    return _command(args, root, **kwargs)


def register_parser(commands: argparse._SubParsersAction) -> None:
    native_cli_parser = commands.add_parser("line", help="Run and audit the DEV assembly line")
    native_cli_commands = native_cli_parser.add_subparsers(dest="line_command", required=True)
    for native_cli_name in ('run', 'check', 'replay'):
        native_cli_command = native_cli_commands.add_parser(native_cli_name)
        native_cli_command.add_argument(
            "path",
            type=Path,
            help=(
                "spec JSON"
                if native_cli_name in ("run", "replay")
                else "batch directory, or a live state directory"
            ),
        )
        native_cli_command.add_argument("--json", action="store_true")
        if native_cli_name == "replay":
            native_cli_command.add_argument("--baseline", type=Path, required=True)
            native_cli_command.add_argument("--write-baseline", action="store_true")
        if native_cli_name == "check":
            native_cli_command.add_argument(
                "--fast",
                action="store_true",
                help="skip the determinism recompute; the result says determinism is not "
                "verified, exits 3 at best, and leaves check.json and the scoreboard untouched",
            )
        native_cli_command.set_defaults(func=_line_command)
    native_cli_watch = native_cli_commands.add_parser(
        "watch",
        help="Process finished DEV runs as they arrive (live mode, one owner per state dir)",
    )
    native_cli_watch.add_argument("config", type=Path, help="evallab.line_watch/v1 JSON")
    native_cli_watch.add_argument("--state-dir", type=Path, required=True)
    native_cli_watch.add_argument("--once", action="store_true", help="one cycle, then exit")
    native_cli_watch.add_argument("--max-cycles", type=int, default=None)
    native_cli_watch.add_argument("--json", action="store_true")
    native_cli_watch.set_defaults(func=_line_command)
