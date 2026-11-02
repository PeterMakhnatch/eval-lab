"""Helpers shared by the integrator-owned ``evallab insights`` subcommands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evallab.insights.store import default_state_root


def add_state_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Engine state directory (default $EVALLAB_INSIGHTS_STATE or "
        "~/.local/state/evallab-insights)",
    )


def state_dir(args: argparse.Namespace) -> Path:
    value = getattr(args, "state_dir", None)
    return default_state_root() if value is None else Path(value).expanduser()


def emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
