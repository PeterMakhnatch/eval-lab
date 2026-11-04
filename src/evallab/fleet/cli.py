"""Fleet commands; all runtime state stays outside the repository."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from evallab.fleet.state import Store, default_state
from evallab.fleet.supervisor import Supervisor


def execute(args: argparse.Namespace, root: Path, *, harbor: object = None) -> int:
    store = Store(args.state_dir)
    supervisor = Supervisor(store, omp=args.omp)
    try:
        if args.fleet_action == "start":
            lane = supervisor.start(
                args.program_dir,
                args.lane,
                args.route,
                args.brief,
                worktree=args.worktree,
                approval=args.approval,
                max_resumes=args.max_resumes,
            )
            print(f"Lane {lane.name} runs as PID {lane.pid} on {lane.route}.")
        elif args.fleet_action == "watch":
            supervisor.watch(args.interval)
        elif args.fleet_action == "stop":
            with store.transaction():
                lane = store.get(args.lane)
                supervisor.stop(lane, "Stopped by operator")
                store.save(lane)
            print(f"Lane {lane.name} stops.")
        else:
            lanes = store.lanes()
            if args.json:
                print(
                    json.dumps(
                        {
                            "heartbeat": store.meta("heartbeat"),
                            "lanes": [asdict(lane) for lane in lanes],
                        },
                        indent=2,
                    )
                )
            else:
                heartbeat = store.meta("heartbeat")
                age = supervisor.clock() - heartbeat if heartbeat else None
                print(
                    f"Supervisor heartbeat: {age:.0f} seconds ago."
                    if age is not None
                    else "Supervisor has no heartbeat."
                )
                for lane in lanes:
                    print(
                        f"{lane.name}: {lane.status}; PID {lane.pid}; route {lane.route}; resumes {lane.resumes}. {lane.reason}"
                    )
        return 0
    except (ValueError, OSError) as error:
        print(f"Fleet refuses: {error}")
        return 1
    finally:
        store.db.close()


def build_fleet_parser(commands: argparse._SubParsersAction) -> None:
    fleet = commands.add_parser("fleet", help="Supervise explicit-route headless lanes")
    fleet.add_argument("--state-dir", type=Path, default=default_state())
    fleet.add_argument("--omp", default="omp", help="OMP executable (tests use an explicit fake)")
    actions = fleet.add_subparsers(dest="fleet_command", required=True)
    lane = actions.add_parser("lane")
    lane_actions = lane.add_subparsers(dest="lane_command", required=True)
    start = lane_actions.add_parser("start")
    start.add_argument("program_dir", type=Path)
    start.add_argument("lane")
    start.add_argument("--route", required=True)
    start.add_argument("--brief", required=True, type=Path)
    start.add_argument("--worktree", type=Path)
    start.add_argument("--approval")
    start.add_argument("--max-resumes", type=int, default=2)
    start.set_defaults(func=execute, fleet_action="start")
    watch = actions.add_parser("watch")
    watch.add_argument("--interval", type=float, default=10)
    watch.set_defaults(func=execute, fleet_action="watch")
    stop = actions.add_parser("stop")
    stop.add_argument("lane")
    stop.set_defaults(func=execute, fleet_action="stop")
    status = actions.add_parser("status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=execute, fleet_action="status")
