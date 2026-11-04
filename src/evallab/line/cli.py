"""CLI registration for the DEV assembly line."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evallab.line.core import (
    check_batch,
    invalidate_check,
    preflight_batch,
    refused_check,
    run_line,
    settle_check,
)
from evallab.line.io import read_json
from evallab.line.replay import replay
from evallab.line.scoreboard import build_scoreboard, render_scoreboard


def build_line_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("line", help="Run and audit the DEV assembly line")
    commands = parser.add_subparsers(dest="line_command", required=True)
    for name in ("run", "check", "scoreboard", "replay"):
        command = commands.add_parser(name)
        command.add_argument(
            "path", type=Path, help="spec JSON" if name in ("run", "replay") else "batch directory"
        )
        command.add_argument("--json", action="store_true")
        if name == "replay":
            command.add_argument("--baseline", type=Path, required=True)
            command.add_argument("--write-baseline", action="store_true")
        if name == "check":
            command.add_argument(
                "--fast",
                action="store_true",
                help="skip the determinism recompute; the result says determinism is not "
                "verified, exits 3 at best, and leaves check.json and the scoreboard untouched",
            )
        command.set_defaults(func=_command)


def _batch(given: Path) -> Path:
    """The batch directory, validated as spelled: a symlink is refused before any resolve."""
    spelled = given.expanduser()
    preflight_batch(spelled)
    return spelled.resolve()


def _command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del harbor
    value: dict[str, Any]
    try:
        if args.line_command == "replay":
            value, code = replay(args.path, args.baseline, root, args.write_baseline)
        elif args.line_command == "run":
            batch = run_line(args.path, root)
            result = read_json(batch / "check.json")
            value = {"batch": str(batch), **result}
            code = 0 if result["passed"] else 2
        elif args.line_command == "check" and args.fast:
            # A read-only look: it must never overwrite a verified check.json or scoreboard.
            value = check_batch(_batch(args.path), root, fast=True)
            failed = any(check["status"] == "FAIL" for check in value["checks"])
            code = 2 if failed else 3
        elif args.line_command == "check":
            batch = _batch(args.path)
            found = invalidate_check(batch)  # a crash below must never leave a stale PASS on disk
            try:
                value = check_batch(batch, root)
            except (ValueError, OSError) as error:
                settle_check(batch, refused_check(error), found)
                raise
            settle_check(batch, value, found)  # a failing batch's scoreboard is left as found
            code = 0 if value["passed"] else 2
        else:
            batch = _batch(args.path)
            value = build_scoreboard(batch, root)
            code = 0
        if args.json:
            print(json.dumps(value, indent=2, sort_keys=True))
        elif args.line_command == "scoreboard":
            print(render_scoreboard(value))
        elif args.line_command == "replay":
            print("Replay " + ("PASS" if code == 0 else "FAIL"))
            for key in (
                "regressions",
                "changed_runs",
                "changed_runs_unavailable",
                "inputs_changed",
                "informational",
                "error",
            ):
                if key in value:
                    print(f"{key}: {json.dumps(value[key], sort_keys=True)}")
        else:
            if value.get("batch"):
                print(value["batch"])
            for check in value["checks"]:
                print(f"{check['status']} {check['name']} {check['details']}")
        return code
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}) if args.json else f"line: {error}")
        return 2
