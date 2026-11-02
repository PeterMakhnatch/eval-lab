"""Create the engine state directory with editable copies of the defaults."""

from __future__ import annotations

import argparse
from pathlib import Path

from evallab.insights.commands._common import add_state_dir, emit, state_dir
from evallab.insights.config import write_defaults
from evallab.insights.store import DB_NAME, InsightStore


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "init", help="Create the state directory, ledger and editable config/routes"
    )
    add_state_dir(parser)
    parser.add_argument(
        "--force", action="store_true", help="Overwrite config.yaml and routes.yaml with defaults"
    )
    parser.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    target = state_dir(args)
    written = write_defaults(target, overwrite=args.force)
    with InsightStore(target / DB_NAME):
        pass
    emit(
        {
            "state_dir": str(target),
            "ledger": str(target / DB_NAME),
            "written": [str(path) for path in written],
        }
    )
    return 0
