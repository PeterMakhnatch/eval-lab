"""``evallab failures`` command group.

Each module in :mod:`evallab.failure_library.commands` exposes
``register(subparsers)``. Modules are discovered in name order so parallel
slices can add commands without editing a shared file.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
from typing import Any

from evallab.failure_library import commands as command_package


def command_modules() -> list[str]:
    return sorted(info.name for info in pkgutil.iter_modules(command_package.__path__))


def build_failures_parser(commands: Any) -> argparse.ArgumentParser:
    parser = commands.add_parser(
        "failures",
        help="Failure Library: provenance-first broken tasks and failed or hacked runs",
    )
    subparsers = parser.add_subparsers(dest="failures_command", required=True)
    for name in command_modules():
        module = importlib.import_module(f"{command_package.__name__}.{name}")
        module.register(subparsers)
    return parser
