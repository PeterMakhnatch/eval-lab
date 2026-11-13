"""Cache-only score argument declarations."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from evallab.results_home import results_root


def command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.score.cli import command as run

    return run(args, root, **kwargs)


def build_score_parser(commands: Any) -> None:
    parser = commands.add_parser("score", help="Score frozen DEV outputs and causal watcher replay; never call a model")
    parser.add_argument("--gold", type=Path, help="Existing Gold IssueBank state root (default: Failure Library)")
    parser.add_argument("--freeze", type=Path, required=True, help="Pinned DEV input, prediction, and threshold manifest")
    parser.add_argument("--tools", choices=("all-retained",), default="all-retained")
    parser.add_argument("--mode", choices=("cached",), default="cached")
    parser.add_argument("--unlock-cohort", action="append", default=None, metavar="NAME",
                        help="Explicitly permit one named labelled-only cohort selected by the freeze")
    parser.add_argument("--out", type=Path, default=results_root() / "score" / "qa-v1",
                        help="New output directory (default: results home/score/qa-v1); never overwrite existing data")
    parser.set_defaults(func=command)
