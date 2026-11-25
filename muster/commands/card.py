from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from muster.model import InputError

DESCRIPTION = "Build and compare a family's deterministic eval card without model runs."


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("family")
    parser.add_argument("--schedules", type=int, default=12)
    parser.add_argument("--worst", type=int, default=24)
    parser.add_argument("--instances", type=int)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    files = parser.add_mutually_exclusive_group()
    files.add_argument("--write", action="store_true")
    files.add_argument("--check", action="store_true")
    files.add_argument("--verify", action="store_true")
    parser.add_argument("--markdown", type=Path)


def run(args: argparse.Namespace, *, card_path: Path | None = None) -> int:
    from muster.lab import card, cardfile
    from muster.lab.finding import adapter_for
    from muster.lab.load import LabError

    started = time.monotonic()
    try:
        if card_path is None:
            card_path = cardfile.TASKS / args.family / "card.json"
        if args.verify:
            if args.markdown:
                raise InputError("--verify cannot write Markdown")
            result, source_count = cardfile.verify(card_path, args.family, jobs=args.jobs)
            if args.json:
                print(cardfile.canonical(result).decode("utf-8"), end="")
            else:
                print(cardfile.verification_text(result, source_count))
            return int(not result["ok"])
        adapter = adapter_for(args.family)
        result = card.build(adapter, schedules=args.schedules, worst=args.worst,
                            instances=args.instances, seed=args.seed, jobs=args.jobs,
                            family=args.family)
        difference = None
        if args.write:
            cardfile.write(card_path, result)
        if args.check:
            difference = cardfile.compare(card_path, result)
        if args.markdown:
            args.markdown.write_text(cardfile.markdown(result), encoding="utf-8")
        if args.json:
            print(cardfile.canonical(result).decode("utf-8"), end="")
        else:
            print(cardfile.markdown(result), end="")
        if difference:
            print(difference, file=sys.stderr)
        print(f"Time: {time.monotonic() - started:.2f} s.", file=sys.stderr)
        return int(not result["ok"] or difference is not None)
    except (LabError, AssertionError, OSError, ValueError, TypeError, KeyError) as error:
        raise InputError(str(error) or "invalid card input") from error
