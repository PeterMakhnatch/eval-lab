"""Behavioral invariant for the public CLI command tree."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.cli import parser

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
CLI_SURFACE_GOLDEN_PATH = GOLDEN_DIR / "cli_surface.json"


def load_golden_surface() -> dict[str, Any]:
    assert CLI_SURFACE_GOLDEN_PATH.exists(), f"Missing golden file: {CLI_SURFACE_GOLDEN_PATH}"
    return json.loads(CLI_SURFACE_GOLDEN_PATH.read_text(encoding="utf-8"))


def _normalize(value: Any) -> Any:
    """Represent public argument arity and choices as JSON values."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _action_signature(action: argparse.Action) -> dict[str, Any]:
    return {
        "flags": sorted(action.option_strings),
        "positional": action.dest if not action.option_strings else None,
        "nargs": _normalize(action.nargs),
        "choices": sorted(_normalize(c) for c in action.choices) if action.choices else None,
        "required": bool(action.required),
    }


def collect_cli_surface(command_parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Record public commands, flags, positional arguments, arity, and choices.

    Help prose, display metavars, defaults, and argparse implementation types
    are not compatibility contracts. Their behavior belongs in consumer tests.
    """
    surface: dict[str, Any] = {}

    def walk(current: argparse.ArgumentParser, path: str) -> None:
        actions = [
            action
            for action in current._actions
            if not isinstance(action, argparse._SubParsersAction)
        ]
        surface[path or "(root)"] = {
            "options": sorted(
                (_action_signature(action) for action in actions),
                key=lambda item: (item["positional"] or "", str(item["flags"])),
            )
        }
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, subparser in action.choices.items():
                    walk(subparser, f"{path} {name}".strip())

    walk(command_parser, "")
    return surface


def write_golden_surface(command_parser: argparse.ArgumentParser | None = None) -> None:
    """Regenerate the committed golden CLI parser surface."""
    p = command_parser if command_parser is not None else parser()
    surface = collect_cli_surface(p)
    CLI_SURFACE_GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLI_SURFACE_GOLDEN_PATH.write_text(
        json.dumps(surface, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_cli_surface_matches_golden() -> None:
    current = collect_cli_surface(parser())
    golden = load_golden_surface()
    assert json.dumps(current, indent=2, sort_keys=True) == json.dumps(
        golden, indent=2, sort_keys=True
    )


@pytest.mark.parametrize(
    "resolution",
    [
        ["--action", "retry"],
        ["--actor", "operator"],
        ["--action", "discard", "--actor", "operator"],
    ],
)
def test_ambiguous_recovery_requires_explicit_action_and_attribution(resolution) -> None:
    with pytest.raises(SystemExit) as error:
        parser().parse_args(["analyze", "worker-resolve-ambiguous", "request-id", *resolution])
    assert error.value.code == 2
