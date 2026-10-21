"""``evallab hackbook`` CLI: list, show, search, and match Hackbook entries ($0)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from evallab.hackbook.matcher import match_target
from evallab.hackbook.models import (
    HackbookEntry,
    coverage_summary,
    get_entry,
    list_entries,
    load_hackbook,
    search_entries,
)


def _row(entry: HackbookEntry) -> str:
    return f"{entry.id:<12} {entry.taxonomy_class_id} {entry.side:<4} {entry.slug}  {entry.title}"


def _list_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    entries = list_entries(
        load_hackbook(root),
        taxonomy_class=args.taxonomy_class,
        side=args.side,
        source=args.source,
        evidence_status=args.evidence,
    )
    if args.json:
        print(json.dumps([e.dump() for e in entries], indent=2))
    else:
        for entry in entries:
            print(_row(entry))
        print(f"{len(entries)} entries", file=sys.stderr)
    return 0


def _show_text(entry: HackbookEntry) -> str:
    sig = entry.signatures
    lines = [
        f"{entry.id}  {entry.slug}",
        entry.title,
        f"class: {entry.taxonomy_class_id} {entry.taxonomy_class}  side: {entry.side}  "
        f"direction: {entry.direction}  qa_category: {entry.qa_category}",
        f"enabled_by: {', '.join(entry.enabled_by) or '-'}",
        f"sources: {', '.join(entry.sources)}",
        "",
        "Mechanism:",
        f"  {entry.mechanism}",
        "",
        "Signatures:",
        f"  structural_signals: {', '.join(sig.structural_signals) or '-'}",
        f"  file_globs: {', '.join(sig.file_globs) or '-'}",
    ]
    lines += [
        f"  file_regex [{r.surface}] {r.pattern}  ({r.description})" for r in sig.file_regexes
    ]
    lines += [f"  trace_command {r.pattern}  ({r.description})" for r in sig.trace_command_regexes]
    lines += [f"  trace_diff {r.pattern}  ({r.description})" for r in sig.trace_diff_regexes]
    recipe = entry.detection_recipe
    lines += ["", f"Detection ({recipe.kind} / {recipe.method_id}):", f"  {recipe.summary}"]
    lines += [f"  {i}. {step}" for i, step in enumerate(recipe.steps, 1)]
    if recipe.rule_or_probe:
        lines.append(f"  rule/probe: {recipe.rule_or_probe}")
    exploit = entry.exploit_template
    lines += ["", f"Exploit template (applicable={exploit.applicable}, {exploit.mode}):"]
    if exploit.goal:
        lines.append(f"  goal: {exploit.goal}")
    lines += [f"  pre: {p}" for p in exploit.preconditions]
    lines += [f"  {i}. {step}" for i, step in enumerate(exploit.template_steps, 1)]
    fix = entry.fix_pattern
    lines += ["", f"Fix ({fix.fix_location}):", f"  {fix.summary}"]
    lines += [f"  [ ] {item}" for item in fix.checklist]
    lines += ["", "Examples:"]
    lines += [
        f"  - [{ex.evidence_status}] {ex.source} {ex.item_or_task_id}: {ex.summary}"
        for ex in entry.examples
    ]
    return "\n".join(lines)


def _show_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    try:
        entry = get_entry(args.entry, load_hackbook(root))
    except KeyError as exc:
        print(f"error: evallab hackbook show: {exc.args[0]}", file=sys.stderr)
        return 2
    print(json.dumps(entry.dump(), indent=2) if args.json else _show_text(entry))
    return 0


def _search_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    rows = search_entries(
        args.query,
        load_hackbook(root),
        taxonomy_class=args.taxonomy_class,
        side=args.side,
        source=args.source,
        limit=args.limit,
    )
    if args.json:
        payload = [
            {"score": r["score"], "fields": r["fields"], "entry": r["entry"].dump()} for r in rows
        ]
        print(json.dumps(payload, indent=2))
    else:
        for r in rows:
            print(f"{r['score']:>7.2f}  {_row(r['entry'])}")
        print(f"{len(rows)} results", file=sys.stderr)
    return 0


def _print_hits(hits: list[dict[str, Any]], indent: str = "") -> None:
    for hit in hits:
        print(
            f"{indent}{hit['score']:>6.2f}  {hit['entry_id']:<12} {hit['taxonomy_class_id']} "
            f"{hit['slug']}"
        )
        if hit["matched_signals"]:
            print(f"{indent}        signals: {', '.join(hit['matched_signals'])}")
        for ref in hit["evidence_refs"][:4]:
            print(f"{indent}        - {ref}")


def _match_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    target = args.target if args.target.is_absolute() else Path.cwd() / args.target
    emit = args.emit_findings
    if emit is not None and not emit.is_absolute():
        emit = Path.cwd() / emit
    result = match_target(
        target,
        kind=args.kind,
        taxonomy_class=args.taxonomy_class,
        top=args.top,
        emit_findings_dir=emit,
        root=root,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    if result["kind"] == "view":
        print(f"view {result['target']}: {len(result['tasks'])} tasks")
        for row in result["aggregate"]:
            print(
                f"  {row['n_tasks']:>3} tasks  {row['entry_id']:<12} {row['taxonomy_class_id']} "
                f"{row['slug']}"
            )
        for task in result["tasks"]:
            print(f"task {task['task_id']}: {len(task['matches'])} matches")
            _print_hits(task["matches"][:5], indent="  ")
    else:
        label = result["task_id"] or result["target"]
        print(f"{result['kind']} {label}: {len(result['matches'])} matches")
        print(f"signals: {', '.join(sorted(result['signals'])) or '-'}")
        _print_hits(result["matches"])
    if "findings" in result:
        print(f"wrote {len(result['findings'])} qa.finding/v1 files to {emit}")
    return 0


def _coverage_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    cov = coverage_summary(load_hackbook(root), root=root)
    if args.json:
        print(json.dumps(cov, indent=2))
        return 0
    print(
        f"Hackbook coverage: {cov['total_entries']} entries "
        f"({cov['task_side_entries']} task-side, {cov['run_side_entries']} run-side), "
        f"{cov['entries_with_executed_evidence']}/{cov['total_entries']} "
        f"({cov['executed_entry_share'] * 100:.1f}%) with executed evidence, "
        f"{cov['total_examples']} examples ({cov['executed_examples']} executed, "
        f"{cov['claimed_examples']} claimed)"
    )
    print("\nBy taxonomy class:")
    for row in cov["by_class"]:
        print(
            f"  {row['taxonomy_class_id']} {row['taxonomy_class']:<32} "
            f"entries={row['entries']:>2} (exec={row['executed_entries']:>2})  "
            f"examples={row['examples']:>2} (exec={row['executed_examples']:>2})"
        )
    print("\nBy source:")
    for row in cov["by_source"]:
        print(
            f"  {row['source']:<26} entries={row['entries']:>2} (exec={row['executed_entries']:>2})  "
            f"examples={row['examples']:>2} (exec={row['executed_examples']:>2})  "
            f"classes={','.join(row['classes'])}"
        )
    return 0


def _filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--class", dest="taxonomy_class", help="TS01..RS07 or class name")
    parser.add_argument("--side", choices=("task", "run"))
    parser.add_argument("--source", help="Seed source, e.g. benchjack or preference_model_tb4")


def build_hackbook_parser(commands: argparse._SubParsersAction) -> None:
    """Register the ``evallab hackbook`` subcommand."""
    hackbook = commands.add_parser(
        "hackbook",
        help="Query the Hackbook of known cheats and task defects; match tasks/traces ($0)",
        description="List, show, search, and statically match Hackbook entries.",
    )
    sub = hackbook.add_subparsers(dest="hackbook_cmd", required=True)

    list_parser = sub.add_parser("list", help="List entries")
    _filters(list_parser)
    list_parser.add_argument("--evidence", choices=("executed", "claimed"))
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=_list_command)

    show = sub.add_parser("show", help="Show one entry by id or slug")
    show.add_argument("entry", metavar="id_or_slug")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=_show_command)

    search = sub.add_parser("search", help="Relevance search over entries")
    search.add_argument("query")
    _filters(search)
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=_search_command)

    match = sub.add_parser("match", help="Match a task dir, exported view, or trace")
    match.add_argument("target", type=Path)
    match.add_argument("--kind", choices=("auto", "task", "trace", "view"), default="auto")
    match.add_argument("--class", dest="taxonomy_class", help="Only entries of this class")
    match.add_argument("--top", type=int, default=25)
    match.add_argument("--emit-findings", type=Path, metavar="DIR")
    match.add_argument("--json", action="store_true")
    match.set_defaults(func=_match_command)

    coverage = sub.add_parser(
        "coverage", help="Print coverage tables by taxonomy class, source, and executed evidence"
    )
    coverage.add_argument("--json", action="store_true")
    coverage.set_defaults(func=_coverage_command)
