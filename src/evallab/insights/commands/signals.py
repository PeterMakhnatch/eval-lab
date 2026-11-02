"""Detect statistical leads and print the highest-priority open signals."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from evallab.insights.config import load_config
from evallab.insights.signals import detect_signals
from evallab.insights.store import DB_NAME, InsightStore, default_state_root


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("limit must be at least 1")
    return parsed


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("signals", help="Find statistical leads across checked claims")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--limit", type=_positive, default=50)
    parser.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    state_dir = default_state_root() if args.state_dir is None else Path(args.state_dir).expanduser()
    load_config(state_dir)
    with InsightStore(state_dir / DB_NAME) as store:
        store.upsert_signals(detect_signals(store.list_claims(), now=datetime.now(UTC)))
        records = store.list_signals(status="open", limit=args.limit)
    print(json.dumps([record.model_dump(mode="json") for record in records],
                     indent=2, sort_keys=True))
    return 0
