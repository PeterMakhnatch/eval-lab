"""Laminar signal command registration."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def _laminar_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.laminar import _laminar_command as command

    return command(args, root, **kwargs)


def register_parser(commands: argparse._SubParsersAction) -> None:
    laminar_parser = commands.add_parser("laminar", help="Laminar Signals as code")
    laminar_actions = laminar_parser.add_subparsers(dest="laminar_action", required=True)
    laminar_signals = laminar_actions.add_parser("signals", help="Create or update the four HAR-166 Signals by name")
    laminar_signals.add_argument(
        "--byok-zai",
        action="store_true",
        help="Run the Signals on Z.ai glm-5.3-flash through a workspace LLM profile "
        "(needs ZAI_OPENAPI_API_KEY; required to create Signals through the Cloud API)",
    )
    laminar_parser.set_defaults(func=_laminar_command)
