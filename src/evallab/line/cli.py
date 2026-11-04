"""CLI registration for the DEV assembly line."""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path
from typing import Any

from evallab.line import core
from evallab.line.io import read_json
from evallab.line.replay import replay
from evallab.line.scoreboard import build_scoreboard, render_scoreboard
from evallab.line.watch import check_state, install_stop_signals, run_watch, watchdog
from evallab.line.watch_rollup import cumulative_scoreboard
from evallab.line.watch_state import State


def build_line_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("line", help="Run and audit the DEV assembly line")
    commands = parser.add_subparsers(dest="line_command", required=True)
    for name in ("run", "check", "scoreboard", "replay"):
        command = commands.add_parser(name)
        command.add_argument(
            "path",
            type=Path,
            help=(
                "spec JSON"
                if name in ("run", "replay")
                else "batch directory, or a live state directory"
            ),
        )
        command.add_argument("--json", action="store_true")
        if name == "replay":
            command.add_argument("--baseline", type=Path, required=True)
            command.add_argument("--write-baseline", action="store_true")
            command.add_argument(
                "--copy-history",
                action="store_true",
                help="enable confirmed history-copy detection for this candidate replay",
            )
        if name == "check":
            command.add_argument(
                "--fast",
                action="store_true",
                help="skip the determinism recompute; the result says determinism is not "
                "verified, exits 3 at best, and leaves check.json and the scoreboard untouched",
            )
        command.set_defaults(func=_command)
    watch = commands.add_parser(
        "watch",
        help="Process finished DEV runs as they arrive (live mode, one owner per state dir)",
    )
    watch.add_argument("config", type=Path, help="evallab.line_watch/v1 JSON")
    watch.add_argument("--state-dir", type=Path, required=True)
    watch.add_argument("--once", action="store_true", help="one cycle, then exit")
    watch.add_argument("--max-cycles", type=int, default=None)
    watch.add_argument("--json", action="store_true")
    watch.set_defaults(func=_command)
    dog = commands.add_parser("watchdog", help="ok / stalled / failed verdict for a live state dir")
    dog.add_argument("path", type=Path, help="live state directory")
    dog.add_argument("--max-age", type=float, default=None, help="seconds without progress")
    dog.add_argument("--json", action="store_true")
    dog.set_defaults(func=_command)


def _batch(given: Path) -> Path:
    """The batch directory, validated as spelled: a symlink is refused before any resolve."""
    spelled = given.expanduser()
    core.preflight_batch(spelled)
    return spelled.resolve()


def _command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del harbor
    value: dict[str, Any]
    try:
        if args.line_command == "replay":
            replay_options = {"copy_history": True} if args.copy_history else {}
            value, code = replay(
                args.path,
                args.baseline,
                root,
                args.write_baseline,
                **replay_options,
            )
        elif args.line_command == "run":
            batch = core.run_line(args.path, root)
            result = read_json(batch / "check.json")
            value = {"batch": str(batch), **result}
            code = 0 if result["passed"] else 2
        elif args.line_command == "check" and args.fast:
            batch = args.path.expanduser().resolve()
            if State.locate(batch) is not None and State.owner_of_child(batch) is None:
                raise ValueError(
                    "--fast checks one batch folder; check a live state directory without it "
                    "(or pass a child batch under increments/)"
                )
            # A read-only look: it must never overwrite a verified check.json or scoreboard.
            value = core.check_batch(_batch(args.path), root, fast=True)
            failed = any(check["status"] == "FAIL" for check in value["checks"])
            code = 2 if failed else 3
        elif args.line_command == "check":
            given = args.path.expanduser().resolve()
            live = State.locate(given)
            if State.owner_of_child(given) is not None:
                # a committed live child: read, never write
                value = core.check_batch(_batch(args.path), root)
                code = 0 if value["passed"] else 2
            elif live is not None:
                value = check_state(live.root, root)
                code = 0 if value["passed"] else 2
            else:
                batch = _batch(args.path)
                found = core.invalidate_check(batch)  # a crash below must never leave a stale PASS
                try:
                    value = core.check_batch(batch, root)
                except (ValueError, OSError) as error:
                    core.settle_check(batch, core.refused_check(error), found)
                    raise
                core.settle_check(batch, value, found)  # a failing batch's scoreboard is left alone
                code = 0 if value["passed"] else 2
        elif args.line_command == "watch":
            stop = threading.Event()
            if not args.once:
                install_stop_signals(stop)
            value = run_watch(
                args.config,
                args.state_dir,
                root,
                once=args.once,
                max_cycles=args.max_cycles,
                stop=stop,
            )
            code = 2 if "error" in value else 0
        elif args.line_command == "watchdog":
            value = watchdog(args.path, max_age=args.max_age)
            code = 0 if value["status"] == "ok" else 2
        else:
            live = State.locate(args.path.expanduser().resolve())
            value = (
                cumulative_scoreboard(live, live.committed(), root)
                if live
                else build_scoreboard(_batch(args.path), root)
            )
            code = 0
        if args.line_command == "watchdog":  # one line per run: launchd appends them to a log
            print(json.dumps(value, sort_keys=True))
        elif args.json:
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
        elif args.line_command == "watch":
            print(json.dumps(value, indent=2, sort_keys=True))
        else:
            if value.get("batch"):
                print(value["batch"])
            for check in value["checks"]:
                print(f"{check['status']} {check['name']} {check['details']}")
        return code
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}) if args.json else f"line: {error}")
        return 2
