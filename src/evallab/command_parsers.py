"""Lightweight argument declarations for commands with heavy implementations."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

DEFAULT_LADDER_VERSION = "1.4.0"
LADDER_VERSIONS = ("1.4.0", "2.0.0", "3.0.0", "4.0.0")

MAX_WORKERS = 2
DEFAULT_MUTANTS = 3
DEFAULT_ROOT = Path.home() / "Library" / "Application Support" / "evallab" / "results-viewer" / "jobs"
DEFAULT_PORT = 8100
DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_SETTLE_SECONDS = 60.0
CHEAT_DESCRIPTION = "Cheat-audit lane: fixed attack ladder, verdicts from the verifier (HAR-204)."


def _mutate_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.verifier_mutation import _mutate_command as command

    return command(args, root, **kwargs)


def _audit_mutants_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.mutant_auditor import _audit_mutants_command as command

    return command(args, root, **kwargs)


def _certify_gold_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.gold_certifier import _certify_gold_command as command

    return command(args, root, **kwargs)


def _view_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.harbor_view import _view_command as command

    return command(args, root, **kwargs)


def _command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.results_viewer import _command as command

    return command(args, root, **kwargs)


def _cheat_run_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.cheat import _cheat_run_command as command

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


def build_view_parser(commands: argparse._SubParsersAction) -> None:
    """Register the ``evallab view`` subcommand (one self-contained block)."""
    view = commands.add_parser(
        "view",
        help="Serve job dirs in Harbor's own viewer with integrity reward dims",
    )
    view.add_argument(
        "paths",
        type=Path,
        nargs="+",
        help="Harbor job directory or results root (repeatable)",
    )
    view.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Viewer jobs root to build (default: fresh temp dir)",
    )
    view.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Viewer port (default: 8080)",
    )
    view.add_argument(
        "--host",
        default="127.0.0.1",
        help="Viewer bind host (default: 127.0.0.1)",
    )
    view.add_argument(
        "--merge",
        action="append",
        default=[],
        metavar="NAME=GLOB",
        help="Fold matching jobs (by dir name) into one viewer job NAME (repeatable)",
    )
    view.add_argument(
        "--arm-regex",
        default=None,
        help="Regex with a named (?P<arm>...) group matched against source job names "
        "(default: stock|tuned|gepa suffixes; no hit keeps the job name)",
    )
    view.add_argument(
        "--task-health",
        type=Path,
        help="Retained digest-bound task-health manifest (requires --tag)",
    )
    view.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Select trials by health/solve tag before viewing (repeat = AND; requires --task-health)",
    )
    view.add_argument(
        "--task-variants",
        type=Path,
        action="append",
        default=[],
        metavar="DIR",
        help="Additional variant-record tree for task-health lineage (repeatable; "
        "library/task-variants is included when present)",
    )
    view.add_argument(
        "--no-launch",
        action="store_true",
        help="Build the viewer root and print it without launching the viewer",
    )
    view.set_defaults(func=_view_command)


def build_results_viewer_parser(commands: argparse._SubParsersAction) -> None:
    """Register ``evallab results-viewer``."""
    parser = commands.add_parser(
        "results-viewer",
        help="Serve every published run at one fixed URL, kept in sync (read-only)",
    )
    parser.add_argument(
        "sources",
        type=Path,
        nargs="*",
        help="Results roots to mirror (default: the results home, $EVALLAB_RESULTS_HOME)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Persistent viewer root (default: {DEFAULT_ROOT})",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help="Seconds between sync passes (default: 60)",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=DEFAULT_SETTLE_SECONDS,
        help="Mirror a job only after its tree has been quiet this long (default: 60)",
    )
    parser.add_argument(
        "--readers-store",
        type=Path,
        help="Reader verdict store <store>/<job>/<trial>/<reader>.json "
        "(default: $EVALLAB_READERS_STORE or ~/Library/Application Support/evallab/readers)",
    )
    parser.add_argument(
        "--trusted-job",
        action="append",
        default=[],
        metavar="GLOB",
        help="Job-name glob whose trials get task pages (repeatable; default: HAR-168-*)",
    )
    parser.add_argument(
        "--variant-records",
        type=Path,
        help="Variant-record tree for health/solve tags (the installer passes a snapshot of "
        "library/task-variants; without it pages show 'no record')",
    )
    parser.add_argument("--no-task-pages", action="store_true", help="Do not build task-<id> pages")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one sync pass, print the summary and exit (no server)",
    )
    parser.set_defaults(func=_command)


def build_cheat_parser(commands: argparse._SubParsersAction) -> None:
    """Register the ``evallab cheat`` subcommand (one self-contained block)."""
    cheat = commands.add_parser(
        "cheat",
        help="Deterministic cheat audit on local Docker ($0, model-free)",
        description=CHEAT_DESCRIPTION,
    )
    sub = cheat.add_subparsers(dest="cheat_cmd", required=True)
    runner = sub.add_parser("run", help="Run the fixed attack ladder against one task")
    runner.add_argument("--task", type=Path, required=True, help="Task directory")
    runner.add_argument("--name", required=True, help="Harbor job name")
    runner.add_argument("--jobs-dir", type=Path, default=Path("runs"))
    runner.add_argument("--attempts", type=int, default=1)
    runner.add_argument(
        "--attacks",
        default=None,
        help="comma-separated subset for the selected ladder; default: full ladder",
    )
    runner.add_argument(
        "--ladder-version",
        choices=list(LADDER_VERSIONS),
        default=None,
        help=f"ladder generation (default: {DEFAULT_LADDER_VERSION})",
    )
    runner.add_argument(
        "--timeout-seconds",
        type=int,
        default=600,
        help="executor wall-clock allowance per attempted trial",
    )
    runner.set_defaults(func=_cheat_run_command)
