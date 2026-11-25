from __future__ import annotations

import argparse
import json
from pathlib import Path

from muster.model import InputError

DESCRIPTION = "Replay findings against deterministic family policies and check regression status."


def configure(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="finding_command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("files", type=Path, nargs="+")
    verify.add_argument("--json", action="store_true")
    regress = commands.add_parser("regress")
    regress.add_argument("family")
    regress.add_argument("--json", action="store_true")


def run(args: argparse.Namespace) -> int:
    from muster.lab import finding
    from muster.lab.load import LabError

    try:
        if args.finding_command == "verify":
            rows = [finding.verify(path) for path in args.files]
            ok = all(row["verdict"] == "CONFIRMED" for row in rows)
        else:
            rows = finding.regress(args.family)
            ok = rows is None or all(row["ok"] for row in rows)
        if args.json:
            print(json.dumps(rows or [], sort_keys=True, indent=2, allow_nan=False))
        elif not rows:
            print("no findings")
        else:
            for row in rows:
                suffix = ""
                if "ok" in row:
                    suffix = f" ({row['status']}: {'PASS' if row['ok'] else 'FAIL'})"
                print(f"{row['id']}: {row['verdict']}{suffix}")
        return int(not ok)
    except (LabError, AssertionError, OSError, ValueError, TypeError, KeyError) as error:
        raise InputError(str(error) or "invalid finding input") from error
