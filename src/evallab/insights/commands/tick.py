"""Run one scheduler tick (or show what it would start with ``--dry-run``)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evallab.insights.commands._common import add_state_dir, emit, state_dir
from evallab.insights.engine import Engine, EngineBusy, engine_lock


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "tick", help="Collect finished passes, scan, compute signals, start due passes"
    )
    add_state_dir(parser)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report what each lens is waiting for or would start; change nothing",
    )
    parser.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    target = state_dir(args)
    if args.dry_run:
        # A dry run must not create, migrate or write the ledger (even at a mistyped path).
        try:
            engine = Engine(target, readonly=True)
        except FileNotFoundError as exc:
            print(f"error: {exc}; run `evallab insights init` first", file=sys.stderr)
            return 2
        try:
            emit(engine.tick(dry_run=True).as_dict())
        finally:
            engine.store.close()
        return 0
    engine = Engine(target)

    try:
        with engine_lock(target):
            report = engine.tick()
    except EngineBusy as exc:
        print(f"error: {exc} (the always-on engine is running; use `status`)", file=sys.stderr)
        return 3
    emit(report.as_dict())
    return 0
