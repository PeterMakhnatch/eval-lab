"""``evallab failures bank-refresh``: scoped, local own-run bank updates."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from evallab.failure_library.bank import BankContext, IssueBank
from evallab.failure_library.bank.refresh import refresh_once, watch


def _seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("interval must be a finite positive number of seconds")
    return seconds


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "bank-refresh",
        help="Refresh changed seeker, grader-repair and Run Health bank sources locally",
        description=(
            "No models, graders, exploit replay or live database writes. Partial sources retain "
            "the previous bank collection; only the existing verified-witness exporter stages "
            "seeker known-hack labels. No service is installed."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Refresh pending source bytes once")
    mode.add_argument("--watch", action="store_true", help="Refresh immediately, then until interrupted")
    parser.add_argument("--interval", type=_seconds, default=60.0, metavar="SECONDS")
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument(
        "--seeker-cells-root",
        type=Path,
        action="append",
        default=None,
        help="Retained SB-2 cells root (repeatable; default: discover under EVALLAB_RESULTS_ROOT)",
    )
    parser.set_defaults(func=_run)


def _run(args: argparse.Namespace, root: Path, **_: Any) -> int:
    bank = IssueBank(root, state_root=args.state_root)
    ctx = BankContext(repo_root=root, state_root=bank.state_root)
    roots = None if args.seeker_cells_root is None else [
        path if path.is_absolute() else root / path for path in args.seeker_cells_root
    ]
    try:
        if args.once:
            report = refresh_once(ctx, bank=bank, seeker_cells_roots=roots)
            print(json.dumps(report, indent=2, sort_keys=True))
            return int(any(row["status"] == "source-not-ready" for row in report["sources"].values()))
        for report in watch(ctx, interval=args.interval, seeker_cells_roots=roots):
            print(json.dumps(report, sort_keys=True), flush=True)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "error", "error": f"{type(error).__name__}: {error}"}, sort_keys=True))
        return 1
    return 0
