"""Reward-hacking audits, verifier mutation, and certification registration."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

MAX_WORKERS = 2
DEFAULT_MUTANTS = 3


def _hack_scan_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.reward_hack import _hack_scan_command as command

    return command(args, root, **kwargs)


def _hack_run_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.reward_hack import _hack_run_command as command

    return command(args, root, **kwargs)


def _mutate_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.verifier_mutation import _mutate_command as command

    return command(args, root, **kwargs)


def _audit_mutants_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.mutant_auditor import _audit_mutants_command as command

    return command(args, root, **kwargs)


def _certify_gold_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.gold_certifier import _certify_gold_command as command

    return command(args, root, **kwargs)


def add_mutate_parser(hack_commands: Any) -> None:
    mutate = hack_commands.add_parser(
        "mutate",
        help=(
            "Verifier mutation testing: grade small behavioural edits of the reference "
            "solution's output with the task's own verifier (local, free)"
        ),
    )
    mutate.add_argument("package", type=Path)
    mutate.add_argument(
        "--target",
        action="append",
        default=[],
        help="Absolute container path to capture and mutate; repeatable "
        "(default: the task's declared artifacts)",
    )
    mutate.add_argument("--max-mutants", type=int, default=24)
    mutate.add_argument("--seed", default="", help="Changes which mutants are sampled")
    mutate.add_argument(
        "--mutant",
        action="append",
        default=[],
        help="Run exactly this mutant id from a previous report (repeatable); ids are stable "
        "for the same reference output",
    )
    mutate.add_argument("--workers", type=int, default=1, choices=range(1, MAX_WORKERS + 1))
    mutate.add_argument("--execute", action="store_true", help="Run the local control matrices")
    mutate.add_argument("--timeout-seconds", type=int, default=1_800)
    mutate.add_argument("--output-dir", type=Path)
    mutate.add_argument(
        "--no-coverage",
        action="store_true",
        help="Skip the instrumented reference run and mutate every line (one fewer trial)",
    )
    mutate.add_argument("--json", action="store_true")
    mutate.set_defaults(func=_mutate_command)


def build_audit_mutants_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "audit-mutants",
        help="STING-style reference-patch semantic mutants through a task's grader (Modal, capped)",
    )
    parser.add_argument("--package", type=Path, required=True, help="Original task package")
    parser.add_argument("--reference-patch", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="Audit output directory")
    parser.add_argument("--repaired-package", type=Path, help="Repaired grader snapshot")
    parser.add_argument("--limit", type=int, default=DEFAULT_MUTANTS)
    parser.add_argument(
        "--plan", action="store_true", help="Offline: list the Python mutation targets and exit"
    )
    parser.add_argument("--approval", help="Recorded approval text for the Modal spend gate")
    parser.add_argument("--max-usd", type=float, default=1.75)
    parser.add_argument("--per-execution-usd", type=float, default=0.005)
    parser.add_argument("--ledger", type=Path, help="Persisted spend ledger (outside evidence)")
    parser.add_argument("--app-name", default="mutant-auditor")
    parser.set_defaults(func=_audit_mutants_command)


def build_certify_gold_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "certify-gold",
        help="Certify a task with behavioral tests, separate-verifier hardening and seven gates",
    )
    parser.add_argument("--package", type=Path, required=True, help="Base task package directory")
    parser.add_argument("--reference-patch", type=Path, required=True, help="Reference patch file")
    parser.add_argument("--out", type=Path, help="Output directory for hardened package")
    parser.add_argument("--task-id", type=str, help="Task identifier (e.g. format-code-task-000079)")
    parser.add_argument("--approval", help="Recorded approval text for Modal execution")
    parser.add_argument("--max-usd", type=float, help="Required explicit Modal cap in USD")
    parser.add_argument("--per-execution-usd", type=float, default=0.005)
    parser.add_argument("--ledger", type=Path, help="Persisted spend ledger (defaults beside --out)")
    parser.add_argument("--app-name", default="gold-certifier")
    parser.add_argument("--alt-solution", action="append", type=Path, default=[],
                        help="Independent correct patch; repeat for Gate 7")
    parser.add_argument("--witness", action="append", type=Path, default=[],
                        help="Contained witness script; requires --injection-controls")
    parser.add_argument("--injection-controls", action="store_true",
                        help="Explicitly run integrity controls through the fixed native Modal runner")
    parser.add_argument("--contract-test", type=Path, help="Use an operator-supplied unittest instead of synthesis")
    parser.add_argument("--hook-command", help="Override supplementary test interpreter command")
    parser.add_argument("--python-version", default="3", choices=("3", "2.7"))
    parser.add_argument("--expected-tests", type=int, help="Reference test count for strict L3 hardening")
    parser.add_argument("--limit", type=int, default=8, help="Max mutants to enumerate")
    parser.add_argument(
        "--model",
        type=str,
        default="google-antigravity/gemini-3.8-flash",
        help="Model identifier for synthesis",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Offline planning: show parameters and Python targets without executing or synthesizing",
    )
    parser.add_argument("--json", action="store_true", help="Output summary as JSON")
    parser.set_defaults(func=_certify_gold_command)


def register_parser(commands: argparse._SubParsersAction) -> None:
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
    build_audit_mutants_parser(commands)
    build_certify_gold_parser(commands)
