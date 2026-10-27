"""``evallab failures mine``: upstream task repairs with two-family attestation."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from evallab.failure_library.fixmine.classify import audit_candidates, classify_candidates
from evallab.failure_library.fixmine.hf import probe_hf_history
from evallab.failure_library.fixmine.miner import (
    digest,
    immutable_write,
    json_bytes,
    mine_repository,
)
from evallab.failure_library.fixmine.models import Candidate
from evallab.failure_library.fixmine.publish import publish_collection
from evallab.failure_library.store import LibraryStore, default_state_root

DEFAULT_MODELS = ["openai-codex/gpt-6.1-sol", "anthropic/claude-opus-5-5"]


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "mine", help="Mine upstream task repairs, preserving parent and fix provenance"
    )
    parser.add_argument("--repo", help="HTTPS Git repository URL")
    parser.add_argument("--task-glob", help="Task directories or table files, e.g. tasks/*")
    parser.add_argument("--since", help="Include commits on or after this ISO date")
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument(
        "--license",
        dest="license_id",
        help="SPDX license at the source revision; unknown if omitted",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch a new snapshot; existing snapshots remain immutable",
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Cache candidates without running subscription classifiers",
    )
    parser.add_argument(
        "--models", nargs=2, default=DEFAULT_MODELS, metavar=("FAMILY_ONE", "FAMILY_TWO")
    )
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument(
        "--limit",
        type=int,
        help="Explicitly bound classification, not history collection; unclassified counts are reported",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Offline: reattest stored labels and rebuild findings without new model or network calls",
    )
    parser.add_argument(
        "--cache", type=Path, help="Use an existing query cache (required for offline rebuild)"
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Audit a deterministic random sample with a third family",
    )
    parser.add_argument("--audit-size", type=int, default=30)
    parser.add_argument("--audit-model", default="xai-oauth/grok-4.7")
    parser.add_argument(
        "--hf-dataset",
        action="append",
        default=[],
        help="Probe HF dataset history metadata instead of Git mining",
    )
    parser.set_defaults(func=_run)


def select_candidates(candidates: list[Candidate], limit: int | None) -> list[Candidate]:
    """Bounded runs explicitly prioritize repair messages, then compact diffs."""
    ordered = sorted(
        candidates,
        key=lambda item: (
            not bool(re.search(r"fix|correct|repair|bug|relax|wrong", item.commit_message, re.I)),
            item.candidate_id,
        ),
    )
    return ordered if limit is None else ordered[:limit]


def _cached_candidates(cache: Path) -> list[Candidate]:
    manifest = json.loads((cache / "manifest.json").read_text())
    raw = (cache / "candidates.jsonl").read_bytes()
    if digest(raw) != manifest["candidates_digest"]:
        raise ValueError("candidate cache digest mismatch")
    return [Candidate.model_validate_json(line) for line in raw.decode().splitlines()]


def _run(args: argparse.Namespace, root: Path, **_: Any) -> int:
    state = args.state_root or default_state_root()
    try:
        if args.hf_dataset:
            if args.rebuild:
                raise ValueError("HF probing is a metadata operation, not an offline rebuild")
            print(
                json.dumps(
                    [probe_hf_history(dataset, state) for dataset in args.hf_dataset],
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if (
            args.batch_size < 1
            or args.audit_size < 1
            or (args.limit is not None and args.limit < 1)
        ):
            raise ValueError("batch size, audit size and limit must be positive")
        if args.rebuild and (args.cache is None or args.refresh or args.collect_only):
            raise ValueError("--rebuild requires --cache and cannot fetch or collect")
        if args.cache is not None:
            mine_root = args.cache.expanduser().resolve()
            candidates = _cached_candidates(mine_root)
        else:
            if not args.repo or not args.task_glob:
                raise ValueError("Git mining requires --repo and --task-glob")
            assignments = root / "research/answer-keys/splits/assignments.csv"
            if not assignments.is_file():
                raise ValueError("split assignments are required for publication-safe mining")
            mine_root, candidates = mine_repository(
                args.repo,
                args.task_glob,
                state,
                since=args.since,
                refresh=args.refresh,
                assignments=assignments,
                license_id=args.license_id,
            )
        if args.collect_only:
            print(
                json.dumps(
                    {"cache": str(mine_root), "candidates": len(candidates), "classified": 0},
                    sort_keys=True,
                )
            )
            return 0
        selection_path = mine_root / "selection.json"
        if args.rebuild:
            if not selection_path.is_file():
                raise ValueError("no stored classification selection; cannot rebuild offline")
            selection = json.loads(selection_path.read_text())
            by_id = {candidate.candidate_id: candidate for candidate in candidates}
            selected = [by_id[key] for key in selection["candidate_ids"]]
            models, batch_size = selection["models"], selection["batch_size"]
        else:
            selected = select_candidates(candidates, args.limit)
            models, batch_size = args.models, args.batch_size
            selection = {
                "candidate_ids": [candidate.candidate_id for candidate in selected],
                "models": models,
                "batch_size": batch_size,
                "limit": args.limit,
                "rule": "repair-message first, candidate-id tie break",
            }
            payload = json_bytes(selection)
            immutable_write(
                mine_root / ("selection-" + digest(payload).removeprefix("sha256:") + ".json"),
                payload,
            )
            selection_path.write_bytes(payload)
        decisions = classify_candidates(
            selected, mine_root, models=models, batch_size=batch_size, cache_only=args.rebuild
        )
        audit = None
        audit_config = None
        audit_path = mine_root / "audit.json"
        if args.rebuild and audit_path.is_file():
            previous = json.loads(audit_path.read_text())
            audit_config = {
                "model": previous["model"],
                "sample_size": previous["requested_sample_size"],
                "seed": previous["seed"],
            }
        elif args.audit:
            audit_config = {"model": args.audit_model, "sample_size": args.audit_size}
        if audit_config is not None:
            audit = audit_candidates(
                selected,
                decisions,
                mine_root,
                **audit_config,
                cache_only=args.rebuild,
            )
        report = publish_collection(
            LibraryStore.for_repo(root, state_root=state),
            candidates,
            decisions,
            mine_root,
            audit=audit,
        )
        print(json.dumps({"cache": str(mine_root), **report}, sort_keys=True, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(f"fixmine: {exc}")
        return 2
