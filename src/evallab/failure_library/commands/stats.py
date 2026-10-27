"""``evallab failures stats``: counts by collection, tier, class, split and status."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evallab.failure_library.store import LibraryStore


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("stats", help="Count findings and runs in the library")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help="Run-record state root (default: $EVALLAB_FAILURE_LIBRARY_STATE or ~/.local/state)",
    )
    parser.set_defaults(func=_run)


def _run(args: argparse.Namespace, root: Path, **_: Any) -> int:
    stats = LibraryStore.for_repo(root, state_root=args.state_root).stats()
    if args.json:
        print(json.dumps(stats, indent=2, sort_keys=True))
        return 0
    for section in ("findings", "runs"):
        block = stats[section]
        print(f"{section}: {block['total']}")
        for name, counts in block.items():
            if name == "total" or not counts:
                continue
            rendered = ", ".join(f"{key}={value}" for key, value in counts.items())
            label = _DISPLAY.get(name, name.removeprefix("by_"))
            print(f"  {label}: {rendered}")
    return 0


_DISPLAY = {
    "by_label_kind": "label occurrences by kind",
    "by_runs_with_label_kind": "runs with a label of kind",
}
