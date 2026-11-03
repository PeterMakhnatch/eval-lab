"""Render the insight board without starting analyst workers."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from evallab.insights.board import render_board
from evallab.insights.config import load_config
from evallab.insights.store import InsightStore, default_state_root


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("board", help="Write the checked insight board")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    state = args.state_dir or default_state_root()
    output = args.out or state / "board"
    # Card commands pin this exact ledger, so snapshots cannot review live state.
    with InsightStore.open(state) as store:
        paths = render_board(
            store, output, now=datetime.now(UTC), top_n=load_config(state).board_top_n,
        )
    print(json.dumps({"files": [str(path) for path in paths]}, indent=2, sort_keys=True))
    return 0
