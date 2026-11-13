"""Detector corpus command registration."""
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


def _detectors_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.detectors import _cmd

    return _cmd(args, root, **kwargs)


def register_parser(commands: argparse._SubParsersAction) -> None:
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
