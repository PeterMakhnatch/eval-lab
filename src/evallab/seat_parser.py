from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evallab.fetch import HarborBackend


def _exists_or_symlink(path: Path) -> bool:
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        return False


def _resolve_input(root: Path, path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute() or _exists_or_symlink(expanded):
        return expanded
    candidate = root / expanded
    if _exists_or_symlink(candidate):
        return candidate
    return expanded


def _positive_int(raw: str) -> int:
    try:
        val = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer value: {raw!r}") from exc
    if val < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {val}")
    return val


def _positive_float(raw: str) -> float:
    import math

    try:
        val = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid float value: {raw!r}") from exc
    if not math.isfinite(val) or val <= 0:
        raise argparse.ArgumentTypeError(f"must be finite and > 0, got {raw!r}")
    return val


def _single_path(
    args: argparse.Namespace, root: Path, entrypoint: Callable[[list[str] | None], int]
) -> int:
    return entrypoint(["--", str(_resolve_input(root, args.path))])


def _seat_inspect_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.evidence.harbor_seats import main

    return _single_path(args, root, main)


def _seat_usage_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.evidence.harbor_seat_usage import main

    return _single_path(args, root, main)


def _seat_trajectories_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.evidence.harbor_seat_trajectories import main

    return _single_path(args, root, main)


def _seat_forensics_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.evidence.seat_forensics import (
        analyze_forensics,
        format_forensics_json,
        render_forensics_text,
    )

    report = analyze_forensics(_resolve_input(root, args.path))
    if args.json:
        print(format_forensics_json(report))
    else:
        print(render_forensics_text(report))
    return 0


def _seat_cda_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.evidence.seat_cda import analyze_cda, format_cda_json, render_cda_text

    report = analyze_cda(_resolve_input(root, args.path))
    if args.json:
        print(format_cda_json(report))
    else:
        print(render_cda_text(report))
    return 0


def _seat_decompose_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.evidence.seat_decompose import (
        decompose_coordination,
        format_decompose_json,
        render_decompose_text,
    )

    eb_solo = (
        _resolve_input(root, args.equal_budget_solo)
        if getattr(args, "equal_budget_solo", None) is not None
        else None
    )
    report = decompose_coordination(
        _resolve_input(root, args.solo),
        _resolve_input(root, args.silent),
        _resolve_input(root, args.team),
        equal_budget_solo_dir=eb_solo,
        best_of_k=getattr(args, "best_of_k", None),
        counting=getattr(args, "trials", "valid"),
        task_key_regex=args.task_key,
        metric=args.metric,
    )
    if args.json:
        print(format_decompose_json(report))
    else:
        print(render_decompose_text(report))
    return 0


def _parse_labeled_path(raw: str, root: Path) -> tuple[str | None, Path]:
    direct = _resolve_input(root, Path(raw))
    if "=" in raw and not _exists_or_symlink(direct):
        maybe_label, maybe_path = raw.split("=", 1)
        label_clean = maybe_label.strip()
        path_clean = maybe_path.strip()
        if label_clean and path_clean:
            cand = _resolve_input(root, Path(path_clean))
            if _exists_or_symlink(cand) or ("/" not in label_clean and "\\" not in label_clean):
                return label_clean, cand
    return None, direct


def _seat_report_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.evidence.seat_html_report import write_seat_html_report

    compare_specs = [_parse_labeled_path(spec, root) for spec in (args.compare or ())]
    decomp_specs: list[tuple[str, Path, Path, Path, Path | None]] = []
    for triple in args.decompose or ():
        d_label, solo_p = _parse_labeled_path(triple[0], root)
        silent_p = _resolve_input(root, Path(triple[1]))
        team_p = _resolve_input(root, Path(triple[2]))
        decomp_specs.append(
            (d_label or solo_p.resolve().parent.name, solo_p, silent_p, team_p, None)
        )
    for quad in getattr(args, "decompose_4", None) or ():
        d_label, solo_p = _parse_labeled_path(quad[0], root)
        silent_p = _resolve_input(root, Path(quad[1]))
        team_p = _resolve_input(root, Path(quad[2]))
        eb_p = _resolve_input(root, Path(quad[3]))
        decomp_specs.append(
            (d_label or solo_p.resolve().parent.name, solo_p, silent_p, team_p, eb_p)
        )
    bp_paths = [_resolve_input(root, p) for p in (args.bespoke_public or ())]

    dest = write_seat_html_report(
        _resolve_input(root, args.path),
        args.html,
        title=args.title,
        label=args.label,
        compare=compare_specs or None,
        decompositions=decomp_specs or None,
        decompose_task_key=getattr(args, "decompose_task_key", None),
        bespoke_public=bp_paths or None,
        max_trials=args.max_trials,
    )
    print(dest.as_posix())
    return 0


def _seat_export_seatlab_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    import json

    from evallab.evidence.seatlab_export import export_seatlab_tape

    summary = export_seatlab_tape(
        _resolve_input(root, args.path),
        args.out,
        include_messages=args.include_messages,
    )
    print(json.dumps(summary.model_dump(mode="json"), allow_nan=False, sort_keys=True, indent=2))
    return 0


def _seat_export_tables_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    import json

    from evallab.evidence.seat_tables import export_forensics_tables

    summary = export_forensics_tables(
        _resolve_input(root, args.path),
        args.out_dir,
        fmt=args.format,
    )
    print(json.dumps(summary.model_dump(mode="json"), allow_nan=False, sort_keys=True, indent=2))
    return 0


def _seat_lint_task_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.evidence.seat_task_lint import (
        BatchTaskLintReport,
        format_task_lint_json,
        lint_task_path,
        render_task_lint_text,
    )

    report = lint_task_path(_resolve_input(root, args.path))
    if args.json:
        print(format_task_lint_json(report))
    else:
        print(render_task_lint_text(report))
    if isinstance(report, BatchTaskLintReport):
        return 0 if report.n_failed == 0 else 1
    return 0 if report.passed else 1


def _seat_bespoke_public_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    import json

    from evallab.evidence.bespoke_public_rollout import (
        analyze_bespoke_public_package,
        render_bespoke_public_summary,
    )

    report = analyze_bespoke_public_package(
        _resolve_input(root, args.path),
        model_slug=args.model,
        run_id=args.run_id,
    )
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), allow_nan=False, sort_keys=True, indent=2))
    else:
        print(render_bespoke_public_summary(report))
    return 0


def _seat_bridge_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.evidence.seat_unified_bridge import (
        analyze_unified_bridge,
        format_bridge_json,
        render_bridge_text,
        write_bridge_html,
    )

    decomps: list[tuple[str, Path, Path, Path]] = []
    for triple in args.decompose or ():
        d_label, solo_p = _parse_labeled_path(triple[0], root)
        silent_p = _resolve_input(root, Path(triple[1]))
        team_p = _resolve_input(root, Path(triple[2]))
        decomps.append((d_label or solo_p.resolve().parent.name, solo_p, silent_p, team_p))
    task_root = _resolve_input(root, args.task_root) if args.task_root is not None else None
    report = analyze_unified_bridge(
        _resolve_input(root, args.path),
        task_root=task_root,
        stall_seconds=args.stall_seconds,
        split_arms=args.split_arms,
        include_regrade=not args.no_regrade,
        decompositions=decomps,
    )
    if args.html is not None:
        dest = write_bridge_html(report, args.html, title=args.title)
        if not args.json:
            print(dest.as_posix())
    if args.json:
        print(format_bridge_json(report))
    elif args.html is None:
        print(render_bridge_text(report))
    return 0


def _register_decompose_parser(seat_commands: argparse._SubParsersAction) -> None:
    decompose = seat_commands.add_parser(
        "decompose",
        help="Decompose solo vs silent vs team gains (S_1, S_N^0, S_N) with PACE resource vectors",
    )
    decompose.add_argument("--solo", type=Path, required=True, help="solo baseline job/dir (S_1)")
    decompose.add_argument(
        "--silent", type=Path, required=True, help="silent/independent team job/dir (S_N^0)"
    )
    decompose.add_argument(
        "--team", type=Path, required=True, help="communicating team job/dir (S_N)"
    )
    decompose.add_argument(
        "--equal-budget-solo",
        type=Path,
        default=None,
        help="optional equal-budget solo job/dir (S_1^(NB))",
    )
    decompose.add_argument(
        "--best-of-k",
        type=_positive_int,
        default=None,
        help="optional k for unbiased Best-of-k solo order-statistic",
    )
    decompose.add_argument(
        "--trials",
        choices=("valid", "all"),
        default="valid",
        help="score valid trials only or count invalid trials as 0.0 (default: valid)",
    )
    decompose.add_argument(
        "--task-key", default=None, help="optional regex extracting shared task key from task_name"
    )
    decompose.add_argument(
        "--metric", default="reward", help="reward key to decompose (default: reward)"
    )
    decompose.add_argument(
        "--json", action="store_true", help="emit deterministic JSON report on stdout"
    )
    decompose.set_defaults(func=_seat_decompose_command)


def _register_report_parser(seat_commands: argparse._SubParsersAction) -> None:
    report_cmd = seat_commands.add_parser(
        "report",
        help="Generate a standalone, zero-CDN interactive Chrome HTML forensics viewer",
    )
    report_cmd.add_argument("path", type=Path, metavar="PATH")
    report_cmd.add_argument("--html", type=Path, required=True, help="output HTML file path")
    report_cmd.add_argument("--title", default=None, help="optional report title")
    report_cmd.add_argument("--label", default=None, help="optional label for primary corpus")
    report_cmd.add_argument(
        "--compare",
        action="append",
        default=[],
        metavar="[LABEL=]PATH",
        help="additional trial/job corpus to compare side by side (repeatable)",
    )
    report_cmd.add_argument(
        "--decompose",
        nargs=3,
        action="append",
        default=[],
        metavar=("[LABEL=]SOLO", "SILENT", "TEAM"),
        help="3-arm S_1/S_N^0/S_N decomposition to embed (repeatable)",
    )
    report_cmd.add_argument(
        "--decompose-4",
        nargs=4,
        action="append",
        default=[],
        metavar=("[LABEL=]SOLO", "SILENT", "TEAM", "EB_SOLO"),
        help="4-arm S_1/S_N^0/S_N/S_1^(NB) decomposition to embed (repeatable)",
    )
    report_cmd.add_argument(
        "--decompose-task-key",
        default=None,
        help="optional task-key regex applied to embedded decompositions",
    )
    report_cmd.add_argument(
        "--bespoke-public",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="Bespoke MAC Bench public rollout .md or index.json to embed (repeatable)",
    )
    report_cmd.add_argument(
        "--max-trials",
        type=_positive_int,
        default=100,
        help="max trials embedded in viewer (default: 100)",
    )
    report_cmd.set_defaults(func=_seat_report_command)


def _register_export_and_lint_parsers(seat_commands: argparse._SubParsersAction) -> None:
    export_sl = seat_commands.add_parser(
        "export-seatlab",
        help="Export leakage-free seatlab.decision/v1 JSONL decision tapes",
    )
    export_sl.add_argument("path", type=Path, metavar="PATH")
    export_sl.add_argument("--out", type=Path, required=True, help="output .jsonl path")
    export_sl.add_argument(
        "--include-messages",
        action="store_true",
        help="also export message_post events as decision records",
    )
    export_sl.set_defaults(func=_seat_export_seatlab_command)

    export_tbl = seat_commands.add_parser(
        "export-tables",
        help="Export normalized trials, seats, events, causal_links, and failure_episodes tables",
    )
    export_tbl.add_argument("path", type=Path, metavar="PATH")
    export_tbl.add_argument("--out-dir", type=Path, required=True, help="output directory path")
    export_tbl.add_argument(
        "--format",
        choices=("parquet", "jsonl"),
        default="parquet",
        help="output table format (default: parquet)",
    )
    export_tbl.set_defaults(func=_seat_export_tables_command)

    lint_cmd = seat_commands.add_parser(
        "lint-task",
        help="Pre-run contract linter for Olam and Bespoke multi-agent Harbor tasks ($0)",
    )
    lint_cmd.add_argument("path", type=Path, metavar="TASK_DIR")
    lint_cmd.add_argument("--json", action="store_true", help="emit deterministic JSON report")
    lint_cmd.set_defaults(func=_seat_lint_task_command)

    bp_cmd = seat_commands.add_parser(
        "bespoke-public",
        help="Ingest and decompose Bespoke MAC Bench public rollout packages (index.json / .md)",
    )
    bp_cmd.add_argument("path", type=Path, metavar="PATH")
    bp_cmd.add_argument("--model", default=None, help="filter by model slug (e.g. opus-5-5)")
    bp_cmd.add_argument("--run-id", default=None, help="filter by runId (e.g. opus-5-5-k8-r1)")
    bp_cmd.add_argument("--json", action="store_true", help="emit deterministic JSON report")
    bp_cmd.set_defaults(func=_seat_bespoke_public_command)


def _register_bridge_parser(seat_commands: argparse._SubParsersAction) -> None:
    bridge = seat_commands.add_parser(
        "bridge",
        help="Join seat forensics, CDA, worldwatch detectors/validity, tidegauge audit, and muster regrade",
    )
    bridge.add_argument("path", type=Path, metavar="PATH")
    bridge.add_argument("--json", action="store_true", help="emit schema-validated JSON on stdout")
    bridge.add_argument("--html", type=Path, default=None, help="write CSP-strict HTML report")
    bridge.add_argument("--title", default=None, help="optional HTML report title")
    bridge.add_argument("--task-root", type=Path, default=None, help="optional task root for muster regrade")
    bridge.add_argument(
        "--stall-seconds",
        type=_positive_float,
        default=300.0,
        help="stall threshold in seconds for worldwatch and tidegauge (default: 300)",
    )
    bridge.add_argument(
        "--split-arms",
        action="store_true",
        help="also run tidegauge audit --split across child arm directories",
    )
    bridge.add_argument("--no-regrade", action="store_true", help="skip optional muster regrade")
    bridge.add_argument(
        "--decompose",
        nargs=3,
        action="append",
        default=[],
        metavar=("[LABEL=]SOLO", "SILENT", "TEAM"),
        help="optional 3-arm decomposition via seat decompose and tidegauge decompose",
    )
    bridge.set_defaults(func=_seat_bridge_command)


def register_parser(commands: argparse._SubParsersAction) -> None:
    seat = commands.add_parser(
        "seat", help="Inspect multi-agent Harbor seat and team evidence (read-only)"
    )
    seat_commands = seat.add_subparsers(dest="seat_command", required=True)

    single = {
        "inspect": (_seat_inspect_command, "Inventory Olam Harbor seat runs"),
        "usage": (_seat_usage_command, "Summarize recorded seat token and USD usage"),
        "trajectories": (_seat_trajectories_command, "Validate seat ATIF trajectories"),
    }
    for name, (func, help_text) in single.items():
        sub = seat_commands.add_parser(name, help=help_text)
        sub.add_argument("path", type=Path, metavar="PATH")
        sub.set_defaults(func=func)

    forensics = seat_commands.add_parser(
        "forensics",
        help="Run deterministic cross-seat causal forensics on a trial or job directory",
    )
    forensics.add_argument("path", type=Path, metavar="PATH")
    forensics.add_argument(
        "--json", action="store_true", help="emit deterministic JSON report on stdout"
    )
    forensics.set_defaults(func=_seat_forensics_command)

    cda = seat_commands.add_parser(
        "cda",
        help="Run deterministic causal message-influence and guard-impact analysis",
    )
    cda.add_argument("path", type=Path, metavar="PATH")
    cda.add_argument("--json", action="store_true", help="emit deterministic JSON report on stdout")
    cda.set_defaults(func=_seat_cda_command)

    _register_decompose_parser(seat_commands)
    _register_report_parser(seat_commands)
    _register_export_and_lint_parsers(seat_commands)
    _register_bridge_parser(seat_commands)
