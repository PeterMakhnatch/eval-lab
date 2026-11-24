from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import FrameType
from typing import Any

from campaign import __version__, fill, report
from campaign.plan import Plan, PlanError, load
from campaign.runner import Expand, Runner, Stop, drive, expand_first_run
from campaign.system import RealSystem

DESCRIPTION = """\
Run a paid multi-arm Harbor campaign from 1 plan file.

check   validate the plan and print every command in order, with the plan sha256
run     start the arms of each lane in order, with the money, stop and plan guards
status  per arm: planned, started, with a reward, unmeasured by kind, cost, ledger
fill    the 1 rerun round of unmeasured trials into <arm>.r1-m<missing>
merge   link every trial with a reward, first runs and reruns, under DEST/<arm>
"""


def _print_event(event: dict[str, str]) -> None:
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {event['kind']} lane {event['lane']}: {event['text']}", flush=True)


def _raise_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def _lanes(plan: Plan, name: str | None) -> list:
    return [plan.lane(name)] if name else list(plan.lanes)


def _drive(plan: Plan, lane: str | None, expand: Expand) -> int:
    print(f"plan {plan.path} sha256 {plan.sha256}", flush=True)
    runner = Runner(plan, RealSystem(), os.getpid(), _print_event, _lanes(plan, lane), expand)
    previous = signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        return drive(runner)
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        runner.interrupt("interrupted", "the runner got a signal; running arms were stopped")
        return 130
    except BaseException:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        runner.interrupt("crash", "the runner failed; running arms were stopped")
        raise
    finally:
        signal.signal(signal.SIGTERM, previous)
        signal.signal(signal.SIGINT, signal.default_int_handler)


def cmd_check(args: argparse.Namespace) -> int:
    plan = load(args.plan)
    lines, problems = report.check(plan)
    print("\n".join(lines))
    for problem in problems:
        print(f"problem: {problem}")
    return 1 if problems else 0


def cmd_run(args: argparse.Namespace) -> int:
    return _drive(load(args.plan), args.lane, expand_first_run)


def cmd_fill(args: argparse.Namespace) -> int:
    plan = load(args.plan)
    if not args.dry_run:
        return _drive(plan, args.lane, fill.expand_rerun)
    try:
        lines = fill.dry_run(plan, RealSystem(), os.getpid(), _lanes(plan, args.lane))
    except Stop as stop:
        print(f"campaign: {stop.reason}: {stop.detail}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    result = report.status(load(args.plan))
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        sys.stdout.write(report.status_text(result))
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    for arm, count in report.merge(load(args.plan), args.dest).items():
        print(f"{arm} {count}")
    return 0


def _command(sub: Any, name: str, text: str, handler: Callable[[argparse.Namespace], int]) -> Any:
    child = sub.add_parser(name, help=text, description=text)
    child.add_argument("plan", type=Path)
    child.set_defaults(run=handler)
    return child


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="campaign", description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    top.add_argument("--version", action="version", version=f"campaign {__version__}")
    sub = top.add_subparsers(dest="command", required=True, metavar="COMMAND")
    _command(sub, "check", "validate the plan and print the commands", cmd_check)
    run = _command(sub, "run", "run the lanes", cmd_run)
    run.add_argument("--lane", help="run 1 lane only")
    status = _command(sub, "status", "counts, cost and ledger state for each arm", cmd_status)
    status.add_argument("--json", action="store_true")
    filler = _command(sub, "fill", "the 1 rerun round of unmeasured trials", cmd_fill)
    filler.add_argument("--lane", help="fill 1 lane only")
    filler.add_argument("--dry-run", action="store_true", help="print the rerun plan; start and write nothing")
    merge = _command(sub, "merge", "link every trial with a reward under DEST/<arm>", cmd_merge)
    merge.add_argument("dest", type=Path)
    return top


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except PlanError as error:
        print(f"campaign: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
