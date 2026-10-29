"""``evallab failures bank-matrix``: DEV issue items × detector identities (catch/miss)."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from evallab.failure_library.bank import (
    PROD_LIVE_ENV,
    BankContext,
    IssueBank,
    IssueItem,
    _load_source,
    available_sources,
)
from evallab.failure_library.bank.matrix import (
    RUN_SCOPE,
    TASK_SCOPE,
    Identity,
    build_matrix,
    load_items,
    load_run_findings,
    load_task_findings,
    markdown,
    write_outputs,
)
from evallab.failure_library.store import _atomic_write


def _prod_live() -> Path:
    return Path(os.environ.get(PROD_LIVE_ENV, Path.home() / ".local/state/evallab-prod-live"))


def default_findings() -> list[Path]:
    """The lane inputs: harness findings, every run-health version, FW-2 worker findings."""
    root = _prod_live()
    candidates = [root / "trace-harness" / "findings-v1.jsonl"]
    candidates += sorted((root / "run-health").glob("findings-run-health-*.jsonl"))
    candidates += sorted((root / "data-flywheel" / "fw2").glob("findings-*.jsonl"))
    return [path for path in candidates if path.is_file()]


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "bank-matrix",
        help="Catch/miss matrix: every DEV issue item against every detector identity",
    )
    items = parser.add_mutually_exclusive_group()
    items.add_argument(
        "--items", type=Path, default=None, help="issue-bank/v1 JSONL (DEV rows used)"
    )
    items.add_argument(
        "--build-sources",
        default=None,
        help="Comma-separated source slugs to build in-process instead of reading the bank",
    )
    parser.add_argument(
        "--source", action="append", default=None, help="Read only this bank source (repeatable)"
    )
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument(
        "--findings",
        type=Path,
        action="append",
        default=None,
        help="trace-findings/v1 JSONL (repeatable; default: the lane findings files)",
    )
    parser.add_argument(
        "--task-findings",
        type=Path,
        action="append",
        default=[],
        help="Task-level findings JSONL (Task QA gate shape; repeatable)",
    )
    parser.add_argument(
        "--union",
        action="append",
        default=[],
        help="Comma-separated detector identities or names scored as one any-alarm set (repeatable)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_prod_live() / "data-flywheel" / "fw2" / "matrix",
        help="Output directory (local state only; never commit it)",
    )
    parser.add_argument("--all-rows", action="store_true", help="Print groups with no judged cells")
    parser.set_defaults(func=_run)


def _items(args: argparse.Namespace, root: Path) -> list[IssueItem]:
    if args.items is not None:
        return load_items(args.items)
    if args.build_sources:
        known = available_sources()
        wanted = [slug.strip() for slug in args.build_sources.split(",") if slug.strip()]
        unknown = sorted(set(wanted) - set(known))
        if unknown:
            raise SystemExit(f"unknown bank sources: {unknown}; known: {sorted(known)}")
        ctx = BankContext(repo_root=root)
        if args.state_root is not None:
            ctx.state_root = args.state_root
        found: dict[str, IssueItem] = {}
        for slug in wanted:
            for record in _load_source(known[slug]).build(ctx):
                if isinstance(record, IssueItem) and record.split == "DEV":
                    found[record.item_id] = record
        return [found[key] for key in sorted(found)]
    bank = IssueBank(root, state_root=args.state_root)
    return list(bank.iter_items(args.source, split="DEV"))


def _run(args: argparse.Namespace, root: Path, **_: Any) -> int:
    ctx = BankContext(repo_root=root)
    if not ctx.registry.has_assignment_authority():
        raise SystemExit("bank-matrix requires a validated assignments.csv authority")
    declared = _items(args, root)
    items: list[IssueItem] = []
    excluded: Counter[str] = Counter()
    for item in declared:
        effective = ctx.strict_split(item.split, item_id=item.subject_id, task_id=item.task_id)
        if effective == "DEV":
            items.append(item)
        else:
            excluded[effective] += 1
    findings_paths = args.findings if args.findings is not None else default_findings()
    unions = [[t.strip() for t in spec.split(",") if t.strip()] for spec in args.union]
    matrix = build_matrix(
        items,
        load_run_findings(findings_paths),
        load_task_findings(args.task_findings),
        unions,
        expected=[
            *(Identity(name, "not_observed", level="run") for name in RUN_SCOPE),
            *(Identity(name, "not_observed", level="task") for name in TASK_SCOPE),
        ],
    )
    summary = write_outputs(
        matrix,
        args.out,
        items=items,
        findings_paths=findings_paths,
        task_findings_paths=args.task_findings,
    )
    _atomic_write(
        args.out / "admission.json",
        json.dumps(
            {
                "schema_version": "issue-bank-matrix-admission/v1",
                "assignments_csv": str(root / "research/answer-keys/splits/assignments.csv"),
                "assignments_sha256": ctx.registry.split_assignments_digest,
                "declared_dev_items": len(declared),
                "admitted_dev_items": len(items),
                "excluded_by_effective_split": dict(sorted(excluded.items())),
                "policy": "recheck every item against the loaded cross-key TEST/task seals",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    print(f"items: {len(items)}  identities: {len(matrix.identities)}  cells: {len(matrix.cells)}")
    print("cells: " + ", ".join(f"{k}={v}" for k, v in summary["cells"].items()))
    print(markdown(summary["groups"], all_rows=args.all_rows))
    print(f"\nwrote {args.out}/cells.jsonl, summary.json, misses.jsonl, false_alarms.jsonl")
    return 0
