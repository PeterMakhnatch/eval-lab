from __future__ import annotations

import argparse
import importlib
import pkgutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from muster import render

TIMEOUT_HELP = "multiply the task's agent timeout by X; verifier and build timeouts keep their values"


def modules() -> list[tuple[str, ModuleType]]:
    names = sorted(info.name for info in pkgutil.iter_modules(__path__) if not info.name.startswith("_"))
    return [(name, importlib.import_module(f"{__name__}.{name}")) for name in names]


def emit(report: dict[str, Any], as_json: bool, text: Callable[[dict[str, Any]], str]) -> None:
    print(render.to_json(report) if as_json else text(report), end="")


def names(value: str | None) -> list[str]:
    return [name.strip() for name in (value or "").split(",") if name.strip()]


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def positive_int(value: str) -> int:
    if int(value) < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return int(value)


def positive_float(value: str) -> float:
    if not 0 < float(value) < float("inf"):
        raise argparse.ArgumentTypeError("must be above 0")
    return float(value)


def add_run_options(parser: argparse.ArgumentParser) -> None:
    add = parser.add_argument
    add("--attempts", type=positive_int, default=1, metavar="K", help="trials for each task (default: 1)")
    add("--out", type=Path, metavar="DIR", help="output directory (default: under runs/)")
    add(
        "--concurrency",
        type=positive_int,
        default=1,
        metavar="C",
        help="trials at the same time (default: 1)",
    )
    add("--timeout-multiplier", type=positive_float, metavar="X", help=TIMEOUT_HELP)
    add("--harbor", metavar="PATH", help="the harbor of olamlabs/harbor-multiagent (default: $MUSTER_HARBOR)")
    add("--dry-run", action="store_true", help="write the configs and stop; start nothing")
    add("--json", action="store_true", help="print the report as JSON")
