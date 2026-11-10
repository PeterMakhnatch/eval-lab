"""Cache-only score command. Missing predictions never request inference."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from . import board, inputs, localization


def command(args: argparse.Namespace, repo_root: Path, **_: Any) -> int:
    started = time.process_time()
    wall_started = time.monotonic()
    config = json.loads(args.freeze.read_text())
    if config.get("schema") != "evallab.score_freeze/v1" or config.get("split") != "DEV":
        raise ValueError("score step 1 requires an explicit DEV freeze")
    if config.get("comparison") != ">=":
        raise ValueError("score thresholds must use >=")
    for ref in config["code_sources"]:
        inputs.source(ref, repo_root)
    cohorts, predictions, extra = inputs.load(config["inputs"], repo_root, args.gold)
    coordinates = localization.resolved(extra["gold_rows"], repo_root)
    value, receipt = board.build(cohorts, predictions, config, coordinates, extra["policy_counts"])
    value["freeze_file_sha256"] = inputs.digest(args.freeze)
    value["threshold_calibration"] = config["threshold_calibration"]
    value["length_definition"] = config["length_definition"]
    value["exposure"] = config["exposure"]
    value["first_action_definition"] = config["first_action_definition"]
    value["score_cpu_seconds"] = time.process_time() - started
    value["score_wall_seconds"] = time.monotonic() - wall_started
    args.out.mkdir(parents=True, exist_ok=False)
    path = args.out / "scoreboard.json"
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    # Read the saved authority, not the in-memory object.
    (args.out / "SCOREBOARD.md").write_text(board.render(json.loads(path.read_text())))
    (args.out / "items.jsonl").write_text("".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in receipt))
    print(f"{path}\n{args.out / 'SCOREBOARD.md'}")
    return 0


def build_score_parser(commands: Any) -> None:
    parser = commands.add_parser("score", help="Score only frozen cached DEV outputs; never run a detector")
    parser.add_argument("--gold", type=Path, help="Existing Gold IssueBank state root (default: Failure Library)")
    parser.add_argument("--freeze", type=Path, required=True, help="Pinned DEV input, prediction, and threshold manifest")
    parser.add_argument("--tools", choices=("all-retained",), default="all-retained")
    parser.add_argument("--mode", choices=("cached",), default="cached")
    parser.add_argument("--out", type=Path, required=True, help="New output directory; existing data are never overwritten")
    parser.set_defaults(func=command)
