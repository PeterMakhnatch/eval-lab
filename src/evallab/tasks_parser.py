"""Task lint and anatomy command registration."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from evallab.cli_support import _resolve

if TYPE_CHECKING:
    from evallab.fetch import HarborBackend


def _tasks_lint_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from dataclasses import asdict

    from evallab.task_lint import discover_tasks, lint_task

    tasks = discover_tasks([_resolve(root, path) for path in args.paths])
    findings = [finding for task in tasks for finding in lint_task(task)]
    if args.json:
        print(json.dumps([asdict(finding) for finding in findings], indent=2))
    else:
        for finding in findings:
            print(f"{finding.severity} {finding.rule} {finding.path}: {finding.message}")
    return 1 if any(finding.severity == "error" for finding in findings) else 0


def _tasks_anatomy_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab.package_anatomy import analyze_package, discover_packages, dumps_card, render_text

    try:
        packages = discover_packages([_resolve(root, path) for path in args.paths])
        if args.reference_patch is not None and len(packages) != 1:
            raise ValueError("--reference-patch applies to exactly one task package")
        patch = None if args.reference_patch is None else _resolve(root, args.reference_patch)
        cards = [analyze_package(package, reference_patch=patch) for package in packages]
    except (FileNotFoundError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.out is not None:
        out = _resolve(root, args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(dumps_card(card) + "\n" for card in cards), encoding="utf-8")
    if args.json:
        print(json.dumps(cards, indent=2, ensure_ascii=False))
    else:
        for card in cards:
            print(render_text(card))
    return 0


def register_parser(commands: argparse._SubParsersAction) -> None:
    tasks_parser = commands.add_parser("tasks", help="Import and manage task corpora")
    tasks_commands = tasks_parser.add_subparsers(dest="tasks_command", required=True)


    tasks_lint = tasks_commands.add_parser(
        "lint", help="Read-only static checks for task verifier trust boundaries"
    )
    tasks_lint.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Task directories or task collections; nested task packages are discovered",
    )
    tasks_lint.add_argument("--json", action="store_true")
    tasks_lint.set_defaults(func=_tasks_lint_command)

    tasks_anatomy = tasks_commands.add_parser(
        "anatomy",
        help="Deterministic grading/container attack-surface card for task packages ($0, static)",
    )
    tasks_anatomy.add_argument(
        "paths", nargs="+", type=Path, help="Task directories or collections of task directories"
    )
    tasks_anatomy.add_argument(
        "--reference-patch",
        type=Path,
        help="DEV reference patch for the one package given; enables symbol/branch exercise checks",
    )
    tasks_anatomy.add_argument("--out", type=Path, help="Write one compact JSON card per line")
    tasks_anatomy.add_argument("--json", action="store_true", help="Print full cards as JSON")
    tasks_anatomy.set_defaults(func=_tasks_anatomy_command)
