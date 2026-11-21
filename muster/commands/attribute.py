from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from muster import attribution, load
from muster.commands import emit, names
from muster.model import InputError, SeatTrial
from muster.roster import NOP

DESCRIPTION = """Read the jobs of a control matrix and print rewards, seat effects and findings.

Exit status: 0 no finding with severity error, 1 at least one, 3 unusable input."""

MANIFEST = "matrix.json"

text = attribution.text


def configure(parser: argparse.ArgumentParser) -> None:
    add = parser.add_argument
    add("directory", type=Path, help="the --out directory of `muster matrix`")
    add("--exploits", metavar="NAMES", help="comma list of adversary policies, added to the recorded ones")
    add(
        "--null",
        metavar="POLICY",
        help='the "does nothing" policy of the rules (default: as recorded, else nop)',
    )
    add("--json", action="store_true", help="print the report as JSON")


def report(directory: Path, exploits: Sequence[str] = (), null: str | None = None) -> dict[str, Any]:
    directory = Path(directory)
    if not os.path.isdir(directory):
        raise InputError(f"{directory} is not a directory")
    manifest: Any = {}
    if os.path.isfile(directory / MANIFEST):
        manifest, error = load.read_json(directory / MANIFEST)
        if error or not isinstance(manifest, dict):
            raise InputError(f"{directory / MANIFEST}: {error or 'not a JSON object'}")
    items = list(load.walk(directory))
    if not any(isinstance(item, SeatTrial) for item in items):
        raise InputError(f"no seat trial under {directory}")
    task = manifest.get("task") if isinstance(manifest.get("task"), dict) else {}
    recorded = [name for name in _list(manifest.get("exploits")) if isinstance(name, str)]
    oracle_reward, attempts = task.get("oracle_reward"), manifest.get("attempts")
    recorded_null = manifest.get("null_policy")
    return attribution.analyze(
        items,
        root=directory,
        oracle_reward=oracle_reward if _is_number(oracle_reward) else None,
        exploits=[*recorded, *(name for name in exploits if name not in recorded)],
        null=null or (recorded_null if isinstance(recorded_null, str) and recorded_null else NOP),
        planned=[entry for entry in _list(manifest.get("rosters")) if isinstance(entry, dict)],
        expected_findings=[
            code for code in _list(manifest.get("expected_findings")) if isinstance(code, str)
        ],
        attempts=attempts if _is_number(attempts) and isinstance(attempts, int) else None,
    )


def build(args: argparse.Namespace) -> dict[str, Any]:
    return report(args.directory, names(args.exploits), args.null)


def run(args: argparse.Namespace) -> int:
    result = build(args)
    emit(result, args.json, text)
    return attribution.status(result)


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
