from __future__ import annotations

import argparse
import json
import sys
import time
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from muster.commands import names, now

DESCRIPTION = """Replicate a family's shortcut cells on held-out cohorts regenerated with new seeds.
Plays policies on the built instances and on K cohorts of seed <family seed>-<suffix><n>
that pass the generator's own checks, in process, and applies a rule fixed before the run.
Exit 0: no cell meets the rule; 1: at least 1 cell replicates; 2: invalid cohort or usage error."""


def _fraction(text: str, low: Fraction, high: Fraction, what: str) -> Fraction:
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError) as error:
        raise argparse.ArgumentTypeError(f"{what} must be a number or P/Q") from error
    if not low <= value < high or (what == "chance" and value == 0):
        raise argparse.ArgumentTypeError(f"{what} must be in [{low}, {high})")
    return value


def _chance(text: str) -> Fraction:
    return _fraction(text, Fraction(0), Fraction(1), "chance")


def _share(text: str) -> Fraction:
    return _fraction(text, Fraction(0), Fraction(1), "fraction")


def _count(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def configure(parser: argparse.ArgumentParser) -> None:
    add = parser.add_argument
    add("family")
    add("--policy", required=True, metavar="NAME[,NAME...]",
        help="zoo or --policy-file names; shell patterns such as 'writer/*' are expanded")
    add("--policy-file", type=Path, metavar="FILE.py",
        help="file with POLICIES = {name: generator policy} and optional SKILLS = {name: [skill]}")
    add("--condition", action="append", default=[], metavar="C",
        help="condition to play (repeat; default: the conditions of the family's skills claim)")
    add("--cohorts", type=int, default=3, metavar="K", help="held-out cohorts (default: 3)")
    add("--seed-suffix", default="h", metavar="h", help="held-out seed suffix (default: h)")
    add("--first-seed", type=int, default=1, metavar="N",
        help="first held-out ordinal n (default: 1)")
    add("--built-over", type=_count, default=2, metavar="N", help="built passes must exceed N")
    add("--heldout-over-fraction", type=_share, default=Fraction(1, 5), metavar="F",
        help="held-out passes must exceed F x held-out count (default: 0.2)")
    add("--chance", type=_chance, metavar="P/Q", help="per-instance chance for the binomial tail")
    add("--measure", default="auto", help="reward measure for a full pass (default: auto)")
    add("--schedule", default="round", help="lab schedule (default: round)")
    add("--max-tries", type=int, metavar="N", help="seed candidates to try (default: 32 x K)")
    add("--out", type=Path, metavar="DIR", help="write DIR/holdout.json (outside the repository)")
    add("--json", action="store_true", help="print the report as JSON")


def _options(args: argparse.Namespace, holdout: Any) -> Any:
    from muster.lab.schedule import parse

    cohorts = args.cohorts
    if not 1 <= cohorts <= holdout.MAX_COHORTS:
        raise holdout.UsageError(f"--cohorts must be 1 to {holdout.MAX_COHORTS}")
    tries = 32 * cohorts if args.max_tries is None else args.max_tries
    if not cohorts <= tries <= holdout.MAX_TRIES:
        raise holdout.UsageError(f"--max-tries must be {cohorts} to {holdout.MAX_TRIES}")
    if not holdout.SUFFIX_TEXT.fullmatch(args.seed_suffix):
        raise holdout.UsageError("--seed-suffix must be 1 to 16 letters, '_' or '-'")
    if not 1 <= args.first_seed <= holdout.MAX_FIRST:
        raise holdout.UsageError(f"--first-seed must be 1 to {holdout.MAX_FIRST}")
    policies = tuple(names(args.policy))
    if not policies:
        raise holdout.UsageError("--policy needs at least 1 name")
    if args.policy_file is not None and not args.policy_file.is_file():
        raise holdout.UsageError(f"no policy file: {args.policy_file}")
    try:
        parse(args.schedule)
    except Exception as error:
        raise holdout.UsageError(str(error)) from error
    return holdout.Options(
        policies=policies, policy_file=args.policy_file, conditions=tuple(args.condition),
        cohorts=cohorts, suffix=args.seed_suffix, built_over=args.built_over,
        fraction=args.heldout_over_fraction, chance=args.chance, measure=args.measure,
        schedule=args.schedule, max_tries=tries, first=args.first_seed)


def _out(path: Path | None, repository: Path) -> Path | None:
    if path is None:
        return None
    target = path.expanduser().resolve()
    if target.is_relative_to(repository):
        raise ValueError(f"--out {path} is inside the repository {repository}")
    return target


def _count_text(tally: dict[str, Any]) -> str:
    text = f"{tally['passes']}/{tally['total']}"
    return text + (f" ({tally['errors']} err)" if tally["errors"] else "")


def _rows(report: dict[str, Any]) -> list[tuple[str, ...]]:
    rows = [("policy", "condition", "lacks", "built", "held-out", "by seed", "P(X>=h)", "decision")]
    for cell in report["cells"]:
        tail = "-" if cell["heldout_tail"] is None else f"{cell['heldout_tail']:.3g}"
        lacks = "?" if cell["lacks"] is None else ",".join(cell["lacks"]) or "-"
        rows.append((cell["policy"], cell["condition"], lacks, _count_text(cell["built"]),
                     _count_text(cell["heldout"]), "+".join(map(str, cell["heldout_by_seed"])),
                     tail, cell["decision"]))
    return rows


def render_text(report: dict[str, Any]) -> str:
    rule = report["rule"]
    title = " ".join(filter(None, (report["family"], report.get("version"))))
    lines = [f"holdout {title} (seed {report['seed']})",
             f"rule: {rule['text']}; chance {rule['chance'] or 'not given'}",
             f"measure: {report['measure']} ({report['measure_basis']})"]
    for seed in report["seeds"]:
        oracle = seed["oracle"]
        detail = "" if oracle is None else f" oracle {oracle['passes']}/{oracle['total']}"
        why = f": {seed['reason']}" if seed["reason"] else ""
        lines.append(f"seed {seed['seed']}: {seed['status']}{detail}{why}")
    rows = _rows(report) if report["cells"] else []
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))] if rows else []
    lines += ["  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
              for row in rows]
    summary = report["summary"]
    verdict = "INVALID: " + report["reason"] if not report["valid"] else (
        f"{summary['replicated']} of {summary['applicable']} applicable cells replicated "
        f"({summary['controls']} controls)")
    lines.append(f"result: {verdict}")
    wall = report.get("time", {}).get("wall_seconds")
    if wall is not None:
        lines.append(f"wall: {wall:.2f} s")
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> int:
    from muster.lab import holdout

    started, stamp = time.monotonic(), now()
    try:
        options = _options(args, holdout)
        out = _out(args.out, holdout.REPOSITORY)
        with TemporaryDirectory(prefix="muster-holdout-") as folder:
            if Path(folder).resolve().is_relative_to(holdout.REPOSITORY):
                raise holdout.UsageError("the temporary directory is inside the repository; set TMPDIR")
            source = holdout.FamilySource(args.family, Path(folder))
            report = holdout.study(source, options)
    except (holdout.UsageError, ValueError, OSError) as error:
        print(f"muster holdout: {' '.join(str(error).split())}", file=sys.stderr)
        return 2
    report["time"] = {"started": stamp, "wall_seconds": round(time.monotonic() - started, 3)}
    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "holdout.json").write_text(text, encoding="utf-8")
    print(text if args.json else render_text(report), end="")
    return holdout.exit_code(report)
