"""Live trial monitoring command registration."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def _watch_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.live_watch import _watch_command as command

    return command(args, root, **kwargs)


def register_parser(commands: argparse._SubParsersAction) -> None:
    native_live_watch_watch = commands.add_parser("watch", help="Live-monitor in-progress Harbor trials")
    native_live_watch_watch.add_argument(
        "--runs-dir",
        action="append",
        type=Path,
        default=[],
        help="Runs root or job directory to watch (repeatable)",
    )
    native_live_watch_watch.add_argument("--out", type=Path, default=None, help="State directory")
    native_live_watch_watch.add_argument("--once", action="store_true", help="Single pass and exit")
    native_live_watch_watch.add_argument(
        "--interval",
        type=float,
        default=0,
        help="Seconds between passes; loops until interrupted (default: single pass)",
    )
    native_live_watch_watch.add_argument(
        "--limits-from-config",
        action="store_true",
        help="Read token limits from trial/job config instead of defaults",
    )
    native_live_watch_watch.add_argument(
        "--spend-cap-usd",
        type=float,
        default=None,
        help="Fleet spend cap in USD for running trials (alerts at 80%% and 100%%)",
    )
    native_live_watch_watch.set_defaults(func=_watch_command)
