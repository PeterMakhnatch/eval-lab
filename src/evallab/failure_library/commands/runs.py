"""``evallab failures runs``: pinned source ingestion and local manifest verification."""

from __future__ import annotations

import argparse
import json
import urllib.error
from pathlib import Path
from typing import Any

from evallab.failure_library.runs.ingest import ingest_collection
from evallab.failure_library.runs.io import (
    COLLECTIONS,
    MALT_REDISTRIBUTION_REVISION,
    download_source,
    raw_root,
    stage_input,
)
from evallab.failure_library.store import LibraryStore, default_state_root


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "runs", help="Ingest and verify provenance-preserving run collections"
    )
    commands = parser.add_subparsers(dest="runs_command", required=True)
    ingest = commands.add_parser(
        "ingest", help="Normalize a pinned native dataset without copying transcripts into records"
    )
    ingest.add_argument("collection", choices=COLLECTIONS)
    ingest.add_argument(
        "--input",
        type=Path,
        help="Existing cached native directory or parquet; archived once under raw/<collection>/<revision>",
    )
    ingest.add_argument(
        "--download",
        action="store_true",
        help="Download pinned native files; no model or compute calls",
    )
    ingest.add_argument(
        "--transcript-root", type=Path, help="Existing Wrench payload cache at its pinned revision"
    )
    ingest.add_argument(
        "--findings-root",
        type=Path,
        action="append",
        default=[],
        help="Additional existing finding JSONL directory for identity links (repeatable)",
    )
    ingest.add_argument(
        "--sample-limit",
        type=int,
        default=20_000,
        help="HackTrace bottom-hash sample (maximum 20000)",
    )
    ingest.add_argument("--state-root", type=Path, default=None)
    ingest.set_defaults(func=_run)
    verify = commands.add_parser(
        "verify", help="Verify local run bytes against committed manifests"
    )
    verify.add_argument("collection", nargs="?", choices=COLLECTIONS)
    verify.add_argument("--state-root", type=Path, default=None)
    verify.set_defaults(func=_run)


def _run(args: argparse.Namespace, root: Path, **_: Any) -> int:
    state = args.state_root or default_state_root()
    store = LibraryStore.for_repo(root, state_root=state)
    if args.runs_command == "verify":
        names = (
            [args.collection]
            if args.collection
            else sorted(
                path.name.removesuffix(".manifest.json")
                for path in store.manifest_root.glob("*.manifest.json")
            )
        )
        problems = {name: store.verify_runs(name) for name in names}
        print(json.dumps({"verified": len(names), "problems": problems}, indent=2, sort_keys=True))
        return int(any(problems.values()))
    source = raw_root(state, args.collection)
    if args.collection == "metr-malt":
        redistributed = state / "raw/metr-malt" / MALT_REDISTRIBUTION_REVISION
        if (
            args.input is not None
            and args.input.is_file()
            or not args.download
            and (redistributed / "redistributed.parquet").is_file()
        ):
            source = redistributed
    try:
        if args.input is not None:
            stage_input(args.collection, args.input, source)
        if args.download:
            download_source(args.collection, source)
        result = ingest_collection(
            args.collection,
            store,
            repo_root=root,
            state_root=state,
            source_root=source,
            transcript_root=args.transcript_root,
            sample_limit=args.sample_limit,
            findings_roots=tuple(args.findings_root),
        )
    except urllib.error.HTTPError as exc:
        message = f"pinned source download failed: HTTP {exc.code}"
        if args.collection == "metr-malt" and exc.code in {401, 403}:
            message += "; request/accept access at https://huggingface.co/datasets/metr-evals/malt-public, then retry with keys run -- evallab ..."
        print(json.dumps({"error": message}, sort_keys=True))
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
