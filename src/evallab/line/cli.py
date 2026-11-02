"""CLI registration for the DEV assembly line."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evallab.line.core import check_batch, run_line
from evallab.line.io import read_json, write_json
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
        command.set_defaults(func=_command)


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
        elif args.line_command == "check":
            value = check_batch(args.path.expanduser().resolve(), root)
            write_json(args.path / "check.json", value)
            code = 0 if value["passed"] else 2
        else:
            value = build_scoreboard(args.path.expanduser().resolve(), root)
            code = 0
        if args.json:
            print(json.dumps(value, indent=2, sort_keys=True))
        elif args.line_command == "scoreboard":
            print(render_scoreboard(value))
        elif args.line_command == "replay":
            print("Replay " + ("PASS" if code == 0 else "FAIL"))
            for key in ("regressions", "changed_runs", "error"):
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
