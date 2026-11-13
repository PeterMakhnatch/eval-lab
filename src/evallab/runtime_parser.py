"""Runtime command declarations; callbacks import their implementations on demand."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from evallab.results_home import results_root

DETECTOR_CHOICES = (
    "combined_v1", "content_rules", "harbor_analyze", "heldout_regrade", "kev_any_cheat",
    "kev_ft1_any_cheat", "kev_grader_tamper", "kev_hollow_solution", "kev_max_pattern",
    "kev_router", "kev_stacked", "laminar_copied", "laminar_false_completion",
    "laminar_stuck_loop", "metr_reward_hacking", "qa_trace", "qa_trace_t0", "rules",
    "run_health_infra", "run_health_loop", "sol_codex",
)


def _watch_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.live_watch import _watch_command as command

    return command(args, root, **kwargs)


def _hack_scan_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.reward_hack import _hack_scan_command as command

    return command(args, root, **kwargs)


def _hack_run_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.reward_hack import _hack_run_command as command

    return command(args, root, **kwargs)


def _line_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.line.cli import _command

    return _command(args, root, **kwargs)


def _laminar_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.laminar import _laminar_command as command

    return command(args, root, **kwargs)


def _detectors_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.detectors import _cmd

    return _cmd(args, root, **kwargs)


def register_runtime_commands(commands: Any) -> None:
    native_live_watch_watch = commands.add_parser("watch", help="Live-monitor in-progress Harbor trials")
    native_live_watch_watch.add_argument(
        "--runs-dir",
        action="append",
        type=Path,
        default=[],
        help="Runs root or job directory to watch (repeatable)",
    )
    native_live_watch_watch.add_argument("--out", type=Path, default=None, help="State directory")
    native_live_watch_watch.add_argument("--once", action="store_true", help="Single pass and exit")
    native_live_watch_watch.add_argument(
        "--interval",
        type=float,
        default=0,
        help="Seconds between passes; loops until interrupted (default: single pass)",
    )
    native_live_watch_watch.add_argument(
        "--limits-from-config",
        action="store_true",
        help="Read token limits from trial/job config instead of defaults",
    )
    native_live_watch_watch.add_argument(
        "--spend-cap-usd",
        type=float,
        default=None,
        help="Fleet spend cap in USD for running trials (alerts at 80%% and 100%%)",
    )
    native_live_watch_watch.set_defaults(func=_watch_command)

    from evallab.command_parsers import add_mutate_parser

    hack = commands.add_parser(
        "hack",
        help="Reward-hacking audit: static V1-V8 ledger, exploit replay, verifier mutation "
        "testing (local, free)",
    )
    hack_commands = hack.add_subparsers(dest="hack_command", required=True)
    scan = hack_commands.add_parser(
        "scan", help="Deterministic V1-V8 flaw ledger for one task package; offline, no execution"
    )
    scan.add_argument("package", type=Path)
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--output", type=Path, help="Write the scan JSON to this path")
    scan.set_defaults(func=_hack_scan_command)
    run = hack_commands.add_parser(
        "run",
        help="Plan (default) or replay exploit scripts through the task's own verifier",
    )
    run.add_argument("package", type=Path)
    run.add_argument(
        "--script",
        type=Path,
        action="append",
        default=[],
        help="Exploit script to replay; repeatable (default: <package>/controls/*.sh)",
    )
    run.add_argument("--execute", action="store_true", help="Run the local control matrix")
    run.add_argument("--timeout-seconds", type=int, default=1_800)
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=_hack_run_command)
    add_mutate_parser(hack_commands)
    from evallab.command_parsers import build_audit_mutants_parser

    build_audit_mutants_parser(commands)
    from evallab.command_parsers import build_certify_gold_parser

    build_certify_gold_parser(commands)


    native_cli_parser = commands.add_parser("line", help="Run and audit the DEV assembly line")
    native_cli_commands = native_cli_parser.add_subparsers(dest="line_command", required=True)
    for native_cli_name in ('run', 'check', 'replay'):
        native_cli_command = native_cli_commands.add_parser(native_cli_name)
        native_cli_command.add_argument(
            "path",
            type=Path,
            help=(
                "spec JSON"
                if native_cli_name in ("run", "replay")
                else "batch directory, or a live state directory"
            ),
        )
        native_cli_command.add_argument("--json", action="store_true")
        if native_cli_name == "replay":
            native_cli_command.add_argument("--baseline", type=Path, required=True)
            native_cli_command.add_argument("--write-baseline", action="store_true")
        if native_cli_name == "check":
            native_cli_command.add_argument(
                "--fast",
                action="store_true",
                help="skip the determinism recompute; the result says determinism is not "
                "verified, exits 3 at best, and leaves check.json and the scoreboard untouched",
            )
        native_cli_command.set_defaults(func=_line_command)
    native_cli_watch = native_cli_commands.add_parser(
        "watch",
        help="Process finished DEV runs as they arrive (live mode, one owner per state dir)",
    )
    native_cli_watch.add_argument("config", type=Path, help="evallab.line_watch/v1 JSON")
    native_cli_watch.add_argument("--state-dir", type=Path, required=True)
    native_cli_watch.add_argument("--once", action="store_true", help="one cycle, then exit")
    native_cli_watch.add_argument("--max-cycles", type=int, default=None)
    native_cli_watch.add_argument("--json", action="store_true")
    native_cli_watch.set_defaults(func=_line_command)
    from evallab.score.parser import build_score_parser

    build_score_parser(commands)

    laminar_parser = commands.add_parser("laminar", help="Laminar Signals as code")
    laminar_actions = laminar_parser.add_subparsers(dest="laminar_action", required=True)
    laminar_signals = laminar_actions.add_parser("signals", help="Create or update the four HAR-166 Signals by name")
    laminar_signals.add_argument(
        "--byok-zai",
        action="store_true",
        help="Run the Signals on Z.ai glm-5.3-flash through a workspace LLM profile "
        "(needs ZAI_OPENAPI_API_KEY; required to create Signals through the Cloud API)",
    )
    laminar_parser.set_defaults(func=_laminar_command)

    from evallab.command_parsers import build_view_parser as _build_view_parser

    _build_view_parser(commands)
    from evallab.command_parsers import build_results_viewer_parser

    build_results_viewer_parser(commands)


    native_detectors_parser = commands.add_parser(
        "detectors",
        help="Score cheat detectors on a labelled trace corpus (cached, $0 to re-score)",
        description="``evallab detectors``: score cheat detectors on a labelled, versioned trace corpus.",
    )
    native_detectors_sub = native_detectors_parser.add_subparsers(dest="detectors_cmd", required=True)
    native_detectors_p = native_detectors_sub.add_parser("verify", help="Check the corpus and cache checksums")
    native_detectors_p.add_argument("--corpus", default="detectors-v1")
    native_detectors_p.set_defaults(func=_detectors_command)
    native_detectors_p = native_detectors_sub.add_parser(
        "score", help="Recall / false positives from cached verdicts (no model calls)"
    )
    native_detectors_p.add_argument("--corpus", default="detectors-v1")
    native_detectors_p.add_argument("--detector", action="append", help="repeatable; default: every cached detector")
    native_detectors_p.add_argument("--check", help="label to score against (default: the detector's own)")
    native_detectors_p.add_argument(
        "--calibrate-on",
        default="tw",
        help="source whose negatives set score cuts",
    )
    native_detectors_p.add_argument("--families", action="store_true", help="also print per-family recall")
    native_detectors_p.add_argument("--json", action="store_true")
    native_detectors_p.set_defaults(func=_detectors_command)
    native_detectors_p = native_detectors_sub.add_parser(
        "run", help="Judge uncached corpus traces with a detector and cache the verdicts"
    )
    native_detectors_p.add_argument("--corpus", default="detectors-v1")
    native_detectors_p.add_argument("--detector", required=True, choices=list(DETECTOR_CHOICES))
    native_detectors_p.add_argument("--source", action="append", help="only rows from this source (repeatable)")
    native_detectors_p.add_argument(
        "--tw-root", type=Path, help="materialized Terminal Wrench trials: <root>/<source>/<tw_id>"
    )
    native_detectors_p.add_argument(
        "--cb-root", type=Path, help="materialized CheatBench trials: <root>/<source>/<cb_id>"
    )
    native_detectors_p.add_argument(
        "--rl-root", type=Path, help="materialized RL rollout trials: <root>/<source>/<rl_id>"
    )
    native_detectors_p.add_argument("--results-root", type=Path, default=results_root())
    native_detectors_p.add_argument("--limit", type=int)
    native_detectors_p.add_argument("--ids", type=Path, help="only these row ids (JSON list or one per line)")
    native_detectors_p.add_argument("--refresh", action="store_true", help="re-judge rows already in the cache")
    native_detectors_p.add_argument("--allow-paid", action="store_true")
    native_detectors_p.set_defaults(func=_detectors_command)
    from evallab.command_parsers import build_cheat_parser

    build_cheat_parser(commands)
    from evallab.qa.parser import build_qa_parser

    build_qa_parser(commands)
    from evallab.failure_library.cli import build_failures_parser

    build_failures_parser(commands)
    from evallab.docker_lock import build_docker_lock_parser

    build_docker_lock_parser(commands)
    from evallab.production.parser import register_production_parser

    register_production_parser(commands)

    from evallab.production.parser import register_run_health_parser

    register_run_health_parser(commands)
    from evallab.production.parser import register_judge_parser

    register_judge_parser(commands)

    from evallab.ship import register_parser as register_ship_parser

    register_ship_parser(commands)
