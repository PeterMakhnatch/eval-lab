"""Analyze local experiment reports and receipts."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from evallab.insights.config import EngineConfig
from evallab.insights.experiment import EngineLike, analyze_experiment, experiment_output_dir


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("experiment", help="Check a local experiment folder")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--no-agents", action="store_true")
    parser.set_defaults(func=_cmd)


def _factory(state_root: Path, *, config: EngineConfig) -> EngineLike:
    from evallab.insights.engine import Engine

    return Engine(state_root, config=config)


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    folder = args.folder.resolve(strict=True)
    output = args.out or experiment_output_dir(folder)
    paths = analyze_experiment(
        folder, output, engine_factory=_factory, now=datetime.now(UTC),
        run_agents=not args.no_agents,
    )
    print(json.dumps({"files": [str(path) for path in paths]}, indent=2, sort_keys=True))
    return 0
