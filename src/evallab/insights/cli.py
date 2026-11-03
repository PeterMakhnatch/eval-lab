"""``evallab insights`` command group.

Each module in :mod:`evallab.insights.commands` exposes ``register(subparsers)``
and is discovered in name order, so commands are added without editing a
shared file (the same pattern as ``evallab failures``).
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
from typing import Any

from evallab.insights import commands as command_package


def command_modules() -> list[str]:
    return sorted(
        info.name
        for info in pkgutil.iter_modules(command_package.__path__)
        if not info.name.startswith("_")
    )


def build_insights_parser(commands: Any) -> argparse.ArgumentParser:
    parser = commands.add_parser(
        "insights",
        help="Insight engine: always-on analysts over experiment results",
    )
    subparsers = parser.add_subparsers(dest="insights_command", required=True)
    for name in command_modules():
        module = importlib.import_module(f"{command_package.__name__}.{name}")
        module.register(subparsers)
    return parser
