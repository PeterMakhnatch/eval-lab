from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from muster.commands import positive_int
from muster.lab import api
from muster.model import InputError

DESCRIPTION = "Play a family in process with deterministic scripted policies and schedules."


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("family")
    parser.add_argument("--condition")
    parser.add_argument("--instance")
    lineup = parser.add_mutually_exclusive_group()
    lineup.add_argument("--policy")
    lineup.add_argument("--lineup")
    parser.add_argument("--schedule", default="round")
    parser.add_argument("--worst", type=positive_int, metavar="TRIES")
    parser.add_argument("--max-steps", type=positive_int, default=api.MAX_STEPS)
    parser.add_argument("--json", action="store_true")


def _lineup(adapter: Any, instance: dict, args: argparse.Namespace) -> dict[str, str]:
    seats = adapter.seats(instance)
    if args.lineup is None:
        lineup = dict.fromkeys(seats, args.policy or "oracle")
    else:
        lineup = {}
        for item in args.lineup.split(","):
            seat, separator, policy = item.partition("=")
            if not separator or not seat or not policy or seat in lineup:
                raise InputError(f"invalid lineup entry {item!r}")
            lineup[seat] = policy
    if set(lineup) != set(seats):
        raise InputError(f"lineup must cover seats {', '.join(seats)} exactly")
    known = set(adapter.policies())
    unknown = {name for name in lineup.values() if not name.startswith("file:")} - known
    if unknown:
        raise InputError(f"unknown policies: {', '.join(sorted(unknown))}")
    return lineup


def _zoo(adapter: Any, lineup: dict[str, str], loaded: dict[str, api.Policy]) -> dict:
    from muster.lab import load

    for name in sorted(set(lineup.values()) - set(loaded)):
        if not name.startswith("file:"):
            continue
        path, separator, function = name.removeprefix("file:").rpartition(":")
        source = Path(path).expanduser()
        if not separator or source.suffix != ".py" or not source.is_file():
            raise InputError(f"expected file:path.py:function, got {name!r}")
        policy = getattr(load.module(source, "policy"), function, None)
        if not callable(policy):
            raise InputError(f"no policy {function!r} in {source}")
        loaded[name] = policy
    return {**adapter.policies(), **loaded}


def _instances(adapter: Any, args: argparse.Namespace) -> list[dict]:
    conditions = adapter.conditions()
    if args.condition is not None:
        if args.condition not in conditions:
            raise InputError(f"unknown condition: {args.condition}")
        conditions = [args.condition]
    instances = [instance for condition in conditions for instance in adapter.instances(condition)]
    selected = [
        instance for instance in instances
        if args.instance is None or instance["id"] == args.instance
    ]
    if not selected:
        raise InputError(
            f"no instances match condition={args.condition!r}, instance={args.instance!r}"
        )
    return selected


def _episodes(args: argparse.Namespace) -> list[api.Episode]:
    from muster.lab import episode, load, search, toy
    from muster.lab.schedule import parse

    if args.family == "toy" or args.family.startswith("toy:"):
        mutant = args.family.partition(":")[2] if ":" in args.family else None
        adapter = toy.family(mutant)
    else:
        adapter = load.family(args.family)
    results = []
    loaded: dict[str, api.Policy] = {}
    for instance in _instances(adapter, args):
        lineup = _lineup(adapter, instance, args)
        zoo = _zoo(adapter, lineup, loaded)
        schedule = parse(args.schedule)
        if args.worst is not None:
            result = search.worst(adapter, instance, lineup, tries=args.worst, seed=0,
                                  max_steps=args.max_steps, zoo=zoo)
        else:
            result = episode.run(adapter, instance, lineup, schedule,
                                 max_steps=args.max_steps, zoo=zoo)
        results.append(result)
    return results


def run(args: argparse.Namespace) -> int:
    from muster.lab.load import LabError

    try:
        results = _episodes(args)
    except (LabError, AssertionError) as error:
        raise InputError(str(error)) from error
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2, allow_nan=False))
    else:
        for result in results:
            print(f"{result.family}/{result.condition}/{result.instance} rewards={result.rewards} "
                  f"steps={result.steps} ended={result.ended}")
            if args.worst is not None:
                print(f"  --schedule order:{','.join(result.order)}")
    return 0
