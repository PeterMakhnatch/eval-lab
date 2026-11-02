"""Discover and index changed experiment evidence."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from evallab.insights.config import load_config
from evallab.insights.sources import scan
from evallab.insights.store import DB_NAME, InsightStore, default_state_root


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("scan", help="Index new or changed experiment evidence")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    state_dir = default_state_root() if args.state_dir is None else Path(args.state_dir).expanduser()
    config = load_config(state_dir)
    with InsightStore(state_dir / DB_NAME) as store:
        result = scan(store, config, now=datetime.now(UTC))
    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    return 0
