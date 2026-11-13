"""Argument declarations for trace inspection; implementations load on dispatch."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

DEFAULT_DATABASE = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"
DEFAULT_CAS_DIR = DEFAULT_DATABASE.parent / "cas"


def _anatomy_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.trace_anatomy import _anatomy_command as command

    return command(args, root, **kwargs)


def _autoreview_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.autoreview_monitor import _autoreview_command as command

    return command(args, root, **kwargs)


def register_anatomy(actions: Any) -> None:
    """``evallab trace anatomy``; registration opens nothing."""
    parser = actions.add_parser(
        "anatomy",
        help="Label-free structured case anatomy of one admitted DEV run or local ATIF trial",
        description=(
            "Task contract, exact final patch, file-mutation ledger (protected vs product surfaces), "
            "high-signal actions and claim-vs-reality. Cloud trace ids and CheatBench item ids pass the "
            "DEV admission gate; --atif-file reads a local Harbor/ATIF trial."
        ),
    )
    parser.add_argument("run", nargs="?", help="Cloud trace id or CheatBench item id")
    parser.add_argument(
        "--atif-file", type=Path, help="Local Harbor/ATIF trial JSON or trial directory"
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the anatomy JSON instead of the card"
    )
    parser.add_argument(
        "--include-narration", action="store_true", help="Add agent messages and reasoning"
    )
    parser.add_argument(
        "--include-final-claim", action="store_true", help="Add the agent's final claim text"
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--cas-dir", type=Path, default=DEFAULT_CAS_DIR)
    parser.add_argument(
        "--results-root", type=Path, help="Published results home (CheatBench trajectories)"
    )
    parser.set_defaults(func=_anatomy_command)


def register_autoreview(actions: Any) -> None:
    """``evallab trace autoreview`` subparser."""
    parser = actions.add_parser(
        "autoreview",
        help="Budgeted compact view, targeted raw-step retrieval, and L0 cascade monitor",
        description=(
            "Audit an agent run for benchmark reward hacking without viewing reasoning. "
            "Passes Stage 1 compact view with test/fixture disambiguation, conditionally triggers "
            "Stage 2 budgeted targeted retrieval (capped steps and characters), and optionally integrates deterministic L0 tripwires."
        ),
    )
    parser.add_argument("run_id_or_atif_path", help="Cloud trace id, CheatBench item id, or local ATIF trial JSON path")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE, help="Path to cloud trace database")
    parser.add_argument("--compact-only", action="store_true", help="Deterministically print compact card and Stage-2 status without model calls ($0)")
    parser.add_argument("--model", default="google-antigravity/gemini-3.8-flash", help="Model to evaluate run (default: google-antigravity/gemini-3.8-flash)")
    parser.add_argument("--with-l0-cascade", action="store_true", help="Include L0 tripwire scan and report cascade/union alerts")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    parser.set_defaults(func=_autoreview_command)
