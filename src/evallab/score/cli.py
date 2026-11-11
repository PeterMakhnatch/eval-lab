"""Cache-only score command. Missing predictions never request inference."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from evallab.results_home import results_root

from . import board, inputs, localization, online, qa, qa_inputs


def command(args: argparse.Namespace, repo_root: Path, **_: Any) -> int:
    started = time.process_time()
    wall_started = time.monotonic()
    config = json.loads(args.freeze.read_text())
    if config.get("schema") != "evallab.score_freeze/v1" or config.get("split") != "DEV":
        raise ValueError("cached scoring requires an explicit DEV freeze")
    if config.get("comparison") != ">=":
        raise ValueError("score thresholds must use >=")
    for ref in config["code_sources"]:
        inputs.source(ref, repo_root)
    cohorts, predictions, extra = inputs.load(
        config["inputs"], repo_root, args.gold, selected_cohorts=set(config["cohorts"]),
        unlocked_cohorts=set(getattr(args, "unlock_cohort", None) or ()))
    coordinates = localization.resolved(extra["gold_rows"], repo_root)
    value, receipt = board.build(cohorts, predictions, config, coordinates, extra["policy_counts"])
    value["freeze_file_sha256"] = inputs.digest(args.freeze)
    value["threshold_calibration"] = config["threshold_calibration"]
    value["length_definition"] = config["length_definition"]
    value["exposure"] = config["exposure"]
    value["first_action_definition"] = config["first_action_definition"]
    if "qa" in config:
        joined = qa_inputs.load(config["qa"], repo_root, extra["gold_rows"])
        value["qa"], qa_receipts = qa.build(joined, config["qa"], config["bootstrap"])
        receipt.extend(qa_receipts)
        value["contracts"] = {name: dict(spec) for name, spec in value["contracts"].items()}
        for name in ("task_qa", "grader_qa"):
            value["contracts"][name].update(status="DEV diagnostic only", reason="Frozen native subject-level joins; no run-level pooling.")
    if "online" in config:
        value["online"], online_receipts = online.build(extra["gold_rows"], cohorts, config["online"], repo_root)
        receipt.extend(online_receipts)
        value["contracts"] = {name: dict(spec) for name, spec in value["contracts"].items()}
        value["contracts"]["online"].update(
            status="DEV diagnostic only", reason="Native causal replay; rules execute locally, models use exact-prefix caches only.")
    value["score_cpu_seconds"] = time.process_time() - started
    value["score_wall_seconds"] = time.monotonic() - wall_started
    args.out.mkdir(parents=True, exist_ok=False)
    path = args.out / "scoreboard.json"
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    # Read the saved authority, not the in-memory object.
    (args.out / "SCOREBOARD.md").write_text(board.render(json.loads(path.read_text())))
    (args.out / "items.jsonl").write_text("".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in receipt))
    if "qa" in value:
        (args.out / "qa-links.jsonl").write_text("".join(
            json.dumps(row, sort_keys=True, allow_nan=False) + "\n"
            for row in receipt if row.get("contract") == "gold_run_qa_link"
        ))
    (args.out / "freeze.json").write_bytes(args.freeze.read_bytes())
    receipts = {name: {"path": str((args.out / name).resolve()), "sha256": inputs.digest(args.out / name)}
                for name in ("scoreboard.json", "freeze.json", "items.jsonl", *(
                    ("qa-links.jsonl",) if "qa" in value else ()))}
    summary = board.summary(value, receipts)
    (args.out / "summary.json").write_text(json.dumps(summary, separators=(",", ":"), allow_nan=False) + "\n")
    print(f"{path}\n{args.out / 'SCOREBOARD.md'}\n{args.out / 'summary.json'}")
    return 0


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
