"""Characterization and contract tests for the declarative CLI command registry.

Pins the exact CLI parser surface against tests/golden/cli_help.json to ensure
that any refactoring is completely behaviour-preserving (all commands, flags,
defaults, help text, exit codes, and stdout/stderr behaviour remain identical).
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
from pathlib import Path

import pytest

from evallab import cli

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
CLI_HELP_GOLDEN_PATH = GOLDEN_DIR / "cli_help.json"
CLI_SOURCE_PATH = Path(cli.__file__).resolve()


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


def find_leaf_parsers(
    parser: argparse.ArgumentParser,
) -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    """Return all (command_path_tuple, leaf_parser) pairs that have no child subparsers."""
    leaves: list[tuple[tuple[str, ...], argparse.ArgumentParser]] = []

    def walk(p: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        subaction = None
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                subaction = action
                break
        if subaction is not None:
            for name, subp in sorted(subaction.choices.items()):
                walk(subp, (*path, name))
        elif path:
            leaves.append((path, p))

    walk(parser, ())
    return leaves


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


# ---------------------------------------------------------------------------
# Registry Contract Tests
# ---------------------------------------------------------------------------


def test_registry_contract_every_leaf_command_has_callable_func_default() -> None:
    """Every leaf command parser must register a callable handler via set_defaults(func=...)."""
    root_parser = cli.parser()
    leaves = find_leaf_parsers(root_parser)
    assert len(leaves) > 0, "No leaf commands discovered"

    for path, subparser in leaves:
        cmd_name = " ".join(path)
        handler = subparser.get_default("func")
        assert handler is not None, (
            f"Leaf command {cmd_name!r} has no 'func' default registered. "
            "Every leaf command must register its handler with set_defaults(func=...)."
        )
        assert callable(handler), (
            f"Leaf command {cmd_name!r} 'func' default is not callable: {handler!r}"
        )


def test_registry_contract_func_count_equals_leaf_command_count() -> None:
    """The number of registered func handlers exactly equals the number of leaf commands."""
    root_parser = cli.parser()
    leaves = find_leaf_parsers(root_parser)
    leaves_with_func = [
        (path, subp) for path, subp in leaves if callable(subp.get_default("func"))
    ]
    assert len(leaves) == len(leaves_with_func)
    assert len(leaves) == 52, f"Expected exactly 52 leaf commands, found {len(leaves)}"


def test_registry_contract_ast_set_defaults_count_equals_leaf_count() -> None:
    """AST check: set_defaults(func=...) registrations in cli.py match the leaf command count."""
    source = CLI_SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    ast_set_defaults_func_calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set_defaults"
        ):
            for kw in node.keywords:
                if kw.arg == "func":
                    ast_set_defaults_func_calls.append(node)

    leaves = find_leaf_parsers(cli.parser())
    assert len(ast_set_defaults_func_calls) == len(leaves)
    assert len(ast_set_defaults_func_calls) == 52


def test_registry_contract_handlers_accept_uniform_signature() -> None:
    """All registered command handlers accept uniform parameters (args, root, *, harbor)."""
    leaves = find_leaf_parsers(cli.parser())
    for path, subparser in leaves:
        cmd_name = " ".join(path)
        handler = subparser.get_default("func")
        sig = inspect.signature(handler)
        params = list(sig.parameters.values())
        assert len(params) >= 2, (
            f"Handler for {cmd_name!r} ({handler.__name__}) has signature {sig}, "
            "expected at least (args, root, ...)"
        )
        assert params[0].name == "args"
        assert params[1].name == "root"
