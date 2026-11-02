"""Start one lens pass now, ignoring cooldowns and daily caps."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast, get_args

from evallab.insights.commands._common import add_state_dir, emit, state_dir
from evallab.insights.engine import Engine, EngineBusy, engine_lock
from evallab.insights.models import Lens


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("run", help="Start one lens pass now and wait for its result")
    parser.add_argument("lens", choices=list(get_args(Lens)))
    add_state_dir(parser)
    parser.add_argument("--no-wait", action="store_true", help="Return once the worker starts")
    parser.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    target = state_dir(args)
    engine = Engine(target)
    try:
        with engine_lock(target):
            record = engine.run_lens(cast(Lens, args.lens), wait=not args.no_wait)
    except EngineBusy as exc:
        print(f"error: {exc} (stop the always-on engine first, or let it schedule)", file=sys.stderr)
        return 3
    if record is None:
        print(f"nothing to do: {args.lens} has no inputs ready", file=sys.stderr)
        return 1
    emit(record.model_dump(mode="json"))
    return 0 if record.status in {"running", "succeeded"} else 1
