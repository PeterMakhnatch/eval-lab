"""``evallab failures loop canary``: manual DEV integration heartbeat."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evallab.failure_library.loop.canary import canary


def _run(args: argparse.Namespace, root: Path, **_: Any) -> int:
    result = canary(
        root,
        dry_run=args.dry_run,
        state_root=args.state_root,
        manifest_path=args.manifest,
        output_dir=args.output_dir,
        max_items=args.max_items,
    )
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if args.dry_run or result["status"] == "ok" else 1


def register(subparsers: Any) -> None:
    loop = subparsers.add_parser(
        "loop", help="Manual Failure Library consumer heartbeat (no scheduler)"
    )
    commands = loop.add_subparsers(dest="failure_loop_command", required=True)
    command = commands.add_parser(
        "canary", help="Freeze DEV targets, compare knowledge OFF/ON and retain honest failures"
    )
    command.add_argument(
        "--dry-run",
        action="store_true",
        help="Offline deterministic plan; no writes or child commands",
    )
    command.add_argument(
        "--state-root",
        type=Path,
        help="Library state root (default: EVALLAB_FAILURE_LIBRARY_STATE)",
    )
    command.add_argument(
        "--manifest",
        type=Path,
        help="Frozen slice JSON (default: output-dir/slice.json; never rerolled)",
    )
    command.add_argument(
        "--output-dir",
        type=Path,
        help="Small public receipts (default: research/failure-library/loop)",
    )
    command.add_argument(
        "--max-items",
        type=int,
        default=2,
        help="Balanced execution subset per agent/arm, 1..40 (default: 2)",
    )
    command.set_defaults(func=_run)
