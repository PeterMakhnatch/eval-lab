"""Behavioral invariant for the public CLI command tree."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evallab.cli import parser

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
CLI_SURFACE_GOLDEN_PATH = GOLDEN_DIR / "cli_surface.json"
REPO_ROOT = str(Path(__file__).resolve().parents[1])


def load_golden_surface() -> dict[str, Any]:
    assert CLI_SURFACE_GOLDEN_PATH.exists(), f"Missing golden file: {CLI_SURFACE_GOLDEN_PATH}"
    return json.loads(CLI_SURFACE_GOLDEN_PATH.read_text(encoding="utf-8"))


def _normalize(value: Any) -> Any:
    """Stringify a default, hiding checkout-specific absolute paths."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value).replace(REPO_ROOT, "<REPO>")


def _action_signature(action: argparse.Action) -> dict[str, Any]:
    return {
        "flags": sorted(action.option_strings),
        "dest": action.dest,
        "metavar": _normalize(action.metavar),
        "nargs": _normalize(action.nargs),
        "default": _normalize(action.default),
        "choices": sorted(_normalize(c) for c in action.choices) if action.choices else None,
        "required": bool(action.required),
        "help": action.help,
        "type": getattr(action.type, "__name__", None) if action.type else None,
        "action": type(action).__name__,
    }


def collect_cli_surface(command_parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Walk every parser node, recording its arguments rather than its rendered help."""
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
                key=lambda item: (item["dest"], str(item["flags"])),
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


def _leaf_parsers(
    command_parser: argparse.ArgumentParser,
) -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    leaves: list[tuple[tuple[str, ...], argparse.ArgumentParser]] = []

    def walk(current: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        subparser_actions = [
            action for action in current._actions if isinstance(action, argparse._SubParsersAction)
        ]
        if not subparser_actions:
            leaves.append((path, current))
            return
        for action in subparser_actions:
            for name, child in action.choices.items():
                walk(child, (*path, name))

    walk(command_parser, ())
    return leaves


def test_every_cli_leaf_dispatches_to_a_callable_handler() -> None:
    missing = [
        " ".join(path)
        for path, leaf in _leaf_parsers(parser())
        if not callable(leaf.get_default("func"))
    ]

    assert missing == []


def test_leaf_command_count_is_pinned() -> None:
    leaves = _leaf_parsers(parser())
    assert len(leaves) == 83, f"Expected exactly 83 leaf commands, found {len(leaves)}"


def test_cli_surface_matches_golden() -> None:
    current = collect_cli_surface(parser())
    golden = load_golden_surface()
    assert json.dumps(current, indent=2, sort_keys=True) == json.dumps(
        golden, indent=2, sort_keys=True
    )
