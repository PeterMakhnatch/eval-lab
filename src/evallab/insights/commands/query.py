"""Read-only SQL over the ledger (the same guard the analysts' ``q sql`` uses)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evallab.insights.agent_tool import readonly_sql
from evallab.insights.commands._common import add_state_dir, state_dir
from evallab.insights.store import DB_NAME, InsightStore


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "query", help="Run one read-only SELECT over the ledger (views v_claims, v_insights, ...)"
    )
    parser.add_argument("sql")
    parser.add_argument("--limit", type=int, default=200)
    add_state_dir(parser)
    parser.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    try:
        statement = readonly_sql(args.sql)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    with InsightStore.open_readonly(state_dir(args) / DB_NAME) as store:
        rows = store.query(f"SELECT * FROM ({statement}) LIMIT ?", (max(1, args.limit),))
    for row in rows:
        print(json.dumps(row, sort_keys=True, default=str))
    return 0
