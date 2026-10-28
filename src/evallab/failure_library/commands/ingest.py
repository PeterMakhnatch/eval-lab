"""``evallab failures ingest <collection>``: rebuild pinned finding collections."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from typing import Any

from evallab.failure_library.sources import collection_builders
from evallab.failure_library.store import LibraryStore


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("ingest", help="Rebuild a provenance-pinned finding collection")
    parser.add_argument("collection", choices=sorted(collection_builders()))
    parser.add_argument(
        "--state-root", type=Path, default=None, help="Local raw-source and run-record state root"
    )
    parser.set_defaults(func=_run)


def _run(args: argparse.Namespace, root: Path, **_: Any) -> int:
    try:
        findings = collection_builders()[args.collection](root, state_root=args.state_root)
        path = LibraryStore.for_repo(root, state_root=args.state_root).write_findings(
            args.collection, findings
        )
    except (OSError, ValueError) as exc:
        print(f"Cannot ingest {args.collection}: {exc}")
        return 1
    tiers = ", ".join(
        f"{tier}={n}" for tier, n in sorted(Counter(f.tier for f in findings).items())
    )
    print(f"{args.collection}: {len(findings)} findings ({tiers}) -> {path}")
    return 0
