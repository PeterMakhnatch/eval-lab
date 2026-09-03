"""Characterization and contract tests for the declarative CLI command registry.

Pins the CLI parser surface against `tests/golden/cli_surface.json` so registry
changes remain explicit: every command, flag, metavar, default, choice list,
required-ness, and help string must match the committed reviewed contract.

The golden records parser *structure*, not rendered `--help` text. An earlier version
of this file snapshotted `format_help()` output and passed on Python 3.12 while failing
on 3.14, because argparse changed its rendering between them. That golden was testing
CPython's formatter, not this CLI. Intentional CLI changes regenerate this artifact in
the same reviewed change; unexpected parser drift still fails the exact comparison.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from evallab import cli

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
CLI_SURFACE_GOLDEN_PATH = GOLDEN_DIR / "cli_surface.json"
CLI_SOURCE_PATH = Path(cli.__file__).resolve()
REPO_ROOT = str(CLI_SOURCE_PATH.parents[2])


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


def collect_cli_surface(parser: argparse.ArgumentParser) -> dict[str, Any]:
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

    walk(parser, "")
    return surface


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


GOLDEN_SURFACE = load_golden_surface()


def test_cli_surface_matches_pre_conversion_golden() -> None:
    """The whole parser surface equals the pre-conversion parser's surface."""
    current = collect_cli_surface(cli.parser())
    assert json.dumps(current, indent=2, sort_keys=True) == json.dumps(
        GOLDEN_SURFACE, indent=2, sort_keys=True
    )


@pytest.mark.parametrize("command_key", sorted(GOLDEN_SURFACE.keys()))
def test_every_command_surface_matches_golden(command_key: str) -> None:
    """Every command keeps its exact arguments: flags, metavars, defaults, choices, help."""
    current = collect_cli_surface(cli.parser())
    assert command_key in current, f"Command {command_key!r} missing from CLI parser"
    assert current[command_key] == GOLDEN_SURFACE[command_key]


def test_no_extra_commands_outside_golden() -> None:
    """No new or renamed commands exist that are absent from the golden."""
    current = collect_cli_surface(cli.parser())
    extra = set(current) - set(GOLDEN_SURFACE)
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
    leaves_with_func = [(path, subp) for path, subp in leaves if callable(subp.get_default("func"))]
    assert len(leaves) == len(leaves_with_func)
    assert len(leaves) == 101, f"Expected exactly 101 leaf commands, found {len(leaves)}"


def test_registry_contract_ast_set_defaults_count_equals_leaf_count() -> None:
    """Every explicit parser handler has one matching AST registration."""
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

    explicit_handler_parsers = [
        (path, subparser)
        for path, subparser in find_all_subparsers(cli.parser())
        if callable(subparser._defaults.get("func"))
    ]
    assert len(ast_set_defaults_func_calls) == len(explicit_handler_parsers)
    assert len(ast_set_defaults_func_calls) == 102


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
