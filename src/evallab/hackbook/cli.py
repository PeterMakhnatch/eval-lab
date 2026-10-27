"""``evallab hackbook`` CLI: query, match, calibrate, and QC Hackbook rules ($0)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.hackbook.matcher import (
    NON_DISCRIMINATING_RATE,
    calibrate_pool,
    fleet_rates_from_calibration,
    match_target,
)
from evallab.hackbook.models import (
    HackbookEntry,
    coverage_summary,
    get_entry,
    list_entries,
    load_hackbook,
    search_entries,
)
from evallab.hackbook.qc import ScoredCase, scan_trace_rules, score_cases


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
        flag = "  [non-discriminating]" if hit.get("non_discriminating") else ""
        rate = f" fleet={hit['fleet_rate']:.0%}" if hit.get("fleet_rate") is not None else ""
        print(
            f"{indent}{hit['score']:>6.2f}  {hit['entry_id']:<12} {hit['taxonomy_class_id']} "
            f"{hit['slug']}{rate}{flag}"
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
        outcome=None if args.outcome == "none" else args.outcome,
        task_dir=args.task,
        fleet_rates=args.fleet_rates,
        non_discriminating_above=args.non_discriminating_above,
        direction=args.direction,
        reference=args.reference,
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
        if result.get("outcome"):
            dropped = ", ".join(result["dropped_by_outcome"]) or "none"
            print(f"outcome: {result['outcome']} (dropped as inapplicable: {dropped})")
        if result.get("task_gate") is not None:
            print(f"task gate: {', '.join(result['task_gate']) or 'no task-side entry fires'}")
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
        f"{cov['observed_examples']} observed, {cov['claimed_examples']} claimed)"
    )
    print("\nBy taxonomy class:")
    for row in cov["by_class"]:
        print(
            f"  {row['taxonomy_class_id']} {row['taxonomy_class']:<32} "
            f"entries={row['entries']:>2} (exec={row['executed_entries']:>2})  "
            f"examples={row['examples']:>2} (exec={row['executed_examples']:>2} "
            f"obs={row['observed_examples']:>2} claimed={row['claimed_examples']:>2})"
        )
    print("\nBy source:")
    for row in cov["by_source"]:
        print(
            f"  {row['source']:<26} entries={row['entries']:>2} (exec={row['executed_entries']:>2})  "
            f"examples={row['examples']:>2} (exec={row['executed_examples']:>2} "
            f"obs={row['observed_examples']:>2} claimed={row['claimed_examples']:>2})  "
            f"classes={','.join(row['classes'])}"
        )
    return 0


def _calibrate_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    pool = args.pool if args.pool.is_absolute() else Path.cwd() / args.pool
    report = calibrate_pool(pool, kind=args.kind, threshold=args.threshold, root=root)
    if args.write_rates is not None:
        rates = fleet_rates_from_calibration(report)
        args.write_rates.write_text(json.dumps(rates, indent=2, sort_keys=True) + "\n")
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(
        f"calibration of {report['pool']}: {report['n']} {report['kind']}s, "
        f"{report['zero_hit']} with no hit, {report['zero_strong']} with no strong hit"
    )
    flagged = report["universal"]
    print(
        f"entries firing on >= {report['threshold']:.0%} of the pool "
        f"(cannot tell its {report['kind']}s apart): {', '.join(flagged) or 'none'}"
    )
    top = sorted(report["entries"].items(), key=lambda kv: -kv[1]["share"])[:10]
    for entry_id, row in top:
        print(f"  {entry_id:<12} {row['share']:>6.1%} fire  {row['strong_share']:>6.1%} strong")
    return 0


def _qc_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    from evallab.tripwires import HACKBOOK_RULES

    cases: list[ScoredCase] = []
    with args.input.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            trace = json.loads(line)
            cases.append(
                ScoredCase(
                    case_id=trace["id"],
                    label=trace["label"],
                    fired=scan_trace_rules(trace["agent_steps"], trace.get("steps")),
                )
            )
    report, detail = score_cases(
        cases,
        (rule["id"] for rule in HACKBOOK_RULES),
        dataset_id=args.dataset_id,
        generated_at=args.generated_at or datetime.now(UTC).isoformat(),
    )
    if args.out is not None:
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.detail_out is not None:
        args.detail_out.write_text(
            json.dumps(detail, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(
        f"{'rule':<42} {'pos':>5} {'clean':>5} {'precision':>9} {'recall':>8} "
        f"{'false-alarm':>11} {'marginal':>8} class"
    )
    for row in sorted(
        detail["rules"], key=lambda row: (-row["clean_false_alarm_rate"], row["rule_id"])
    ):
        precision = "-" if row["precision"] is None else f"{row['precision']:.1%}"
        print(
            f"{row['rule_id']:<42} {row['positives_hit_n']:>5} {row['clean_hit_n']:>5} "
            f"{precision:>9} {row['recall']:>8.1%} {row['clean_false_alarm_rate']:>11.1%} "
            f"{row['marginal_true_positive_n']:>8} {row['classification']}"
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
        help="Query the Hackbook; statically match tasks/traces and QC rules ($0)",
        description="List, show, search, statically match, and QC Hackbook entries.",
    )
    sub = hackbook.add_subparsers(dest="hackbook_cmd", required=True)

    list_parser = sub.add_parser("list", help="List entries")
    _filters(list_parser)
    list_parser.add_argument("--evidence", choices=("executed", "observed", "claimed"))
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
    match.add_argument(
        "--direction",
        choices=("false_pass", "false_fail"),
        help="Only entries that let a wrong run pass / fail a right one (plus either); "
        "a reward seeker wants false_pass",
    )
    match.add_argument(
        "--outcome",
        choices=("auto", "pass", "fail", "none"),
        default="auto",
        help="Trace verdict: drops entries that cannot explain it (auto reads the verifier reward)",
    )
    match.add_argument(
        "--task",
        type=Path,
        metavar="TASK_DIR",
        help="Task package of a trace: task-side entries count in full only if they fire on it",
    )
    match.add_argument(
        "--reference",
        type=Path,
        metavar="PATCH",
        help="Task only: the reference fix as a unified diff; flags its blocks no test names",
    )
    match.add_argument(
        "--fleet-rates",
        type=Path,
        metavar="FILE",
        help="calibrate --write-rates output: flag entries firing on most of that fleet",
    )
    match.add_argument(
        "--non-discriminating-above",
        type=float,
        default=NON_DISCRIMINATING_RATE,
        help="Fleet share above which a hit is flagged non_discriminating (default 0.95)",
    )
    match.add_argument("--emit-findings", type=Path, metavar="DIR")
    match.add_argument("--json", action="store_true")
    match.set_defaults(func=_match_command)

    coverage = sub.add_parser(
        "coverage", help="Print coverage tables by taxonomy class, source, and executed evidence"
    )
    coverage.add_argument("--json", action="store_true")
    coverage.set_defaults(func=_coverage_command)

    calibrate = sub.add_parser(
        "calibrate",
        help="Per-entry firing share over a pool of tasks or traces; flags non-discriminating entries",
    )
    calibrate.add_argument("pool", type=Path, help="Directory of task packages or trace dirs")
    calibrate.add_argument("--kind", choices=("task", "trace"), default="task")
    calibrate.add_argument("--threshold", type=float, default=0.4)
    calibrate.add_argument(
        "--write-rates",
        type=Path,
        metavar="FILE",
        help="Write per-entry strong firing shares for `match --fleet-rates`",
    )
    calibrate.add_argument("--json", action="store_true")
    calibrate.set_defaults(func=_calibrate_command)

    qc = sub.add_parser(
        "qc",
        help="Score static rule hits on labelled dev traces; recommend noisy alert demotions",
        description="Read JSONL objects with id, label (positive/clean/unscored), agent_steps, "
        "and optional steps. Print per-rule QC; never execute trace commands. "
        "The after union excludes only demoted rules, retaining other verdict and diagnostic rules.",
    )
    qc.add_argument("--input", type=Path, required=True, metavar="TRACES.jsonl")
    qc.add_argument("--dataset-id", required=True, metavar="NAME")
    qc.add_argument("--out", type=Path, metavar="REPORT.json", help="Write evallab.hackbook_qc/v1")
    qc.add_argument("--detail-out", type=Path, metavar="DETAIL.json", help="Write rates and classes")
    qc.add_argument("--generated-at", metavar="ISO", help="Report timestamp (default: current UTC)")
    qc.set_defaults(func=_qc_command)
