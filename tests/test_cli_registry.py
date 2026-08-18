"""Characterization and contract tests for the declarative CLI command registry.

Pins the exact CLI parser surface against tests/golden/cli_help.json to ensure
that any refactoring is completely behaviour-preserving (all commands, flags,
defaults, help text, exit codes, and stdout/stderr behaviour remain identical).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from evallab import cli

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
CLI_HELP_GOLDEN_PATH = GOLDEN_DIR / "cli_help.json"


def load_golden_help_map() -> dict[str, str]:
    assert CLI_HELP_GOLDEN_PATH.exists(), f"Missing golden file: {CLI_HELP_GOLDEN_PATH}"
    return json.loads(CLI_HELP_GOLDEN_PATH.read_text(encoding="utf-8"))


def collect_cli_help_map(parser: argparse.ArgumentParser) -> dict[str, str]:
    """Traverse all subparsers and sub-subparsers deterministically."""
    help_map: dict[str, str] = {}

    def walk(p: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        key = " ".join(path) if path else "(root)"
        help_map[key] = p.format_help()
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name in sorted(action.choices.keys()):
                    subp = action.choices[name]
                    walk(subp, (*path, name))

    walk(parser, ())
    return dict(sorted(help_map.items()))


def find_all_subparsers(
    parser: argparse.ArgumentParser,
) -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    """Return all (command_path_tuple, subparser) pairs."""
    subparsers: list[tuple[tuple[str, ...], argparse.ArgumentParser]] = []

    def walk(p: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, subp in sorted(action.choices.items()):
                    current = (*path, name)
                    subparsers.append((current, subp))
                    walk(subp, current)

    walk(parser, ())
    return subparsers


GOLDEN_HELP_MAP = load_golden_help_map()


def test_cli_help_golden_matches_complete_inventory() -> None:
    """The entire CLI help surface matches the golden snapshot byte-for-byte."""
    current_help_map = collect_cli_help_map(cli.parser())
    current_json = json.dumps(current_help_map, indent=2, sort_keys=True) + "\n"
    expected_json = CLI_HELP_GOLDEN_PATH.read_text(encoding="utf-8")
    assert current_json == expected_json


@pytest.mark.parametrize("command_key", list(GOLDEN_HELP_MAP.keys()))
def test_every_command_help_matches_golden(command_key: str) -> None:
    """Every individual command's help text matches the frozen golden."""
    current_help_map = collect_cli_help_map(cli.parser())
    assert command_key in current_help_map, f"Command {command_key!r} missing from CLI parser"
    assert current_help_map[command_key] == GOLDEN_HELP_MAP[command_key]


def test_no_extra_commands_outside_golden() -> None:
    """No new or extraneous commands exist that are absent from the golden."""
    current_help_map = collect_cli_help_map(cli.parser())
    extra = set(current_help_map.keys()) - set(GOLDEN_HELP_MAP.keys())
    assert not extra, f"Found unexpected commands outside golden: {extra}"
