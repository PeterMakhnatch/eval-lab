"""Behavioral invariant for the public CLI command tree."""

from __future__ import annotations

import argparse

from evallab.cli import parser


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
