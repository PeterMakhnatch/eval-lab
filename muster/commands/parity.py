from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from muster.commands import positive_int
from muster.model import InputError

DESCRIPTION = "Compare packaged HTTP task episodes with sequential in-process lab episodes."


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("family")
    parser.add_argument("--condition")
    parser.add_argument("--instances", type=positive_int, default=1)
    parser.add_argument("--policy", default="oracle")
    parser.add_argument("--concurrent", action="store_true")
    parser.add_argument("--json", action="store_true")


def run(args: argparse.Namespace) -> int:
    from muster.lab import parity
    from muster.lab.load import LabError

    try:
        results = parity.run(
            args.family, condition=args.condition, instances=args.instances, policy=args.policy,
            concurrent=args.concurrent,
        )
    except LabError as error:
        raise InputError(str(error)) from error
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        for result in results:
            status = "PASS" if result.passed else "FAIL"
            detail = f": {result.detail}" if result.detail else ""
            task = f"{args.family}/{result.condition}/{result.instance}"
            print(f"{status} {task} {result.policy}{detail}")
    return 0 if all(result.passed for result in results) else 1
