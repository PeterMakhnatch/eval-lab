"""Harbor and published-results viewer command registration."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path.home() / "Library" / "Application Support" / "evallab" / "results-viewer" / "jobs"
DEFAULT_PORT = 8100
DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_SETTLE_SECONDS = 60.0


def _view_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.harbor_view import _view_command as command

    return command(args, root, **kwargs)


def _command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.results_viewer import _command as command

    return command(args, root, **kwargs)


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


def register_parser(commands: argparse._SubParsersAction) -> None:
    build_view_parser(commands)
    build_results_viewer_parser(commands)
