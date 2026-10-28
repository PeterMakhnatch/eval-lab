"""``evallab failures pack``: compile and retrieve provenance-grounded DEV knowledge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evallab.failure_library.knowledge.compiler import compile_curated, compile_library
from evallab.failure_library.knowledge.export import write_agent_pack, write_knowledge
from evallab.failure_library.knowledge.packs import make_pack
from evallab.failure_library.store import LibraryStore


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "pack", help="DEV-only failure cards and leave-subject-out examples"
    )
    parser.add_argument(
        "--for", dest="agent", required=True, choices=("task-qa", "trace-qa")
    )
    parser.add_argument(
        "--task", help="Subject <source_id>/<task_id>; always excluded with its group"
    )
    parser.add_argument(
        "--task-text", type=Path, help="Optional blind task instruction text for BM25"
    )
    parser.add_argument("--max-chars", type=int, default=12_000)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--exclude-id",
        action="append",
        default=[],
        help="Exclude admitted raw id or COLLECTION::RAW_ID and its subject group; repeatable",
    )
    parser.add_argument(
        "--exclude-task",
        action="append",
        default=[],
        help="Exclude SOURCE/TASK and its group even without a library record; repeatable",
    )
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument(
        "--curated-only",
        action="store_true",
        help="Use defensive taxonomy metadata only; read no library examples",
    )
    parser.add_argument(
        "--output-dir", type=Path, help="Create agent-native artifacts in a NEW directory"
    )
    parser.add_argument(
        "--rubric-base",
        type=Path,
        help="Task QA baseline TOML (default: Harbor's installed rubric)",
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        help="Rebuild cards.jsonl, hackbook-proposals.jsonl and build.json here",
    )
    parser.set_defaults(func=_run)


def _run(args: argparse.Namespace, root: Path, **_: Any) -> int:
    try:
        knowledge = (
            compile_curated(root)
            if args.curated_only
            else compile_library(root, LibraryStore.for_repo(root, state_root=args.state_root))
        )
        pack = make_pack(
            knowledge,
            agent=args.agent,
            task=args.task,
            task_text=args.task_text.read_text(encoding="utf-8") if args.task_text else "",
            max_chars=args.max_chars,
            top_k=args.top_k,
            exclude_ids=args.exclude_id,
            exclude_tasks=args.exclude_task,
        )
        criteria = None
        if args.output_dir and args.agent == "task-qa":
            from evallab.qa.task import load_rubric

            criteria = load_rubric(args.rubric_base)
        if args.output_dir:
            write_agent_pack(pack, args.output_dir, rubric_criteria=criteria)
        if args.build_dir:
            write_knowledge(knowledge, args.build_dir)
    except (OSError, ValueError, KeyError, ImportError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 2
    print(pack.markdown, end="")
    return 0
