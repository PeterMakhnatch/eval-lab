"""Lazy JSON CLI registration for private trace storage and retrieval."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _positive_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be a positive integer") from None
    if limit < 1:
        raise argparse.ArgumentTypeError("limit must be a positive integer")
    return limit


def register_traces(subparsers: Any) -> None:
    """Register without importing SDKs, opening databases, or doing network I/O."""
    traces = subparsers.add_parser("traces", help="Sync and fetch private, label-free trace stores")
    actions = traces.add_subparsers(dest="traces_command", required=True)
    fetch = actions.add_parser("fetch", help="Read a stored run or provider read-only query as JSON")
    fetch.add_argument("--source", choices=("laminar", "docent"), required=True)
    selector = fetch.add_mutually_exclusive_group(required=True)
    selector.add_argument("--run-id", help="Original cloud DB trace_id (not the hosted UUID)")
    selector.add_argument("--query", help="Raw read-only SQL/DQL; provider limits are exposed in JSON")
    fetch.add_argument(
        "--dataset",
        help="Docent source collection, or Laminar {dataset:String} query parameter",
    )
    fetch.set_defaults(func=_command)
    sync = actions.add_parser("sync", help="Upload admitted new traces to the private store")
    sync.add_argument("--target", choices=("laminar", "docent"), required=True)
    sync.add_argument("--dataset", help="Admitted source dataset ID")
    sync.add_argument("--limit", type=_positive_limit, help="Maximum number of traces to upload")
    sync.set_defaults(func=_command)


def _command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del harbor
    try:
        if args.traces_command == "fetch":
            from evallab.trace_store.fetch import fetch

            result = fetch(
                args.source, run_id=args.run_id, query=args.query, dataset=args.dataset,
            )
        else:
            from evallab.trace_store.sync import sync

            result = sync(args.target, repo_root=root, dataset=args.dataset, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 2 if result.get("stopped") else 0
    except (OSError, RuntimeError, ValueError) as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None:
            # Provider error bodies can echo request contents or credentials.
            print(f"error: trace {args.traces_command} refused (HTTP {status})", file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
