"""Expose deterministic statistics without a model call or database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from evallab.insights.stats import compute


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("stat", help="Compute a checked statistic")
    parser.add_argument("op", help="Statistical operation, such as wilson or mcnemar")
    parser.add_argument("--args", required=True, help="Named arguments as a JSON object")
    parser.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    del root, harbor
    try:
        parameters = json.loads(args.args)
        result = compute(args.op, parameters)
    except ValueError as exc:
        print(f"Cannot compute statistic: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0
