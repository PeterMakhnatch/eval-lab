"""Run the always-on engine loop (what the LaunchAgent starts)."""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from types import FrameType

from evallab.insights.commands._common import add_state_dir, state_dir
from evallab.insights.engine import Engine, EngineBusy


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "watch", help="Run ticks forever under a single-instance lock (the always-on engine)"
    )
    add_state_dir(parser)
    parser.add_argument("--max-ticks", type=int, default=None, help="Stop after N ticks")
    parser.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    stopping = False

    def request_stop(signum: int, frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    engine = Engine(state_dir(args))
    try:
        ticks = engine.watch(max_ticks=args.max_ticks, stop=lambda: stopping)
    except EngineBusy as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    print(f"insight engine stopped after {ticks} ticks; running workers keep going and are "
          "collected on the next start")
    return 0
