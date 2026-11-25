from __future__ import annotations

import argparse
from pathlib import Path

from muster.commands import emit
from muster.model import InputError

DESCRIPTION = "Compare paired facet instances across arms, without ranking models."


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("jobs", type=Path, nargs="+")
    parser.add_argument("--family")
    parser.add_argument("--measure", action="extend", nargs="+", default=[])
    parser.add_argument("--base", default="probe")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--seed", type=int, default=0)


def run(args: argparse.Namespace) -> int:
    from muster.lab import contrast

    if not contrast.NAME.fullmatch(args.base):
        raise InputError("invalid base arm")
    try:
        result = contrast.report(args.jobs, family=args.family, measures=args.measure,
                                 base=args.base, seed=args.seed)
    except (OSError, ValueError, OverflowError) as error:
        raise InputError(str(error)) from error
    emit(result, args.json, contrast.text)
    return int(result["placed"] == 0)
