from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from campaign.spend_keys import KeyProbeSystem
from campaign.spend_types import amount
from campaign.system import RealSystem

Capture = Callable[[list[str], float, int], tuple[int | None, str]]
Usage = tuple[Decimal, Decimal, Decimal, Decimal]

PROBE_TIMEOUT_SECONDS = 60.0
MAX_PROBE_JSON_CHARS = 65_536
MAX_PROBE_ARGS = 64
MAX_PROBE_ARG_CHARS = 16_384
MAX_USAGE_CHARS = 200
MAX_INSTANT_CHARS = 40
NUMBER = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,15})?(?:[eE][-+]?[0-9]{1,3})?")
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

DESCRIPTION = """\
Recorded model spend by lane, UTC day, ET day and model from the roots in ROOTS.toml.
--since is the first UTC day (inclusive); --until is the UTC day after the last (exclusive).
--as-of is the instant of the key counters; records after it stay out of the reconciliation only.
--key-usage takes the key counters total,daily,weekly,monthly in USD;
--key-probe runs a JSON argv array once that prints those 4 numbers (its output is never shown).
--probe-keys reads the current OpenRouter counters once for each key_env in ROOTS.toml.
Exit 0 without problems, 1 with problems (0 with --lenient for input problems only),
1 when the probe or an output fails, 2 for an invalid argument.
"""


@dataclass(frozen=True)
class Options:
    since: date | None
    until: date | None
    as_of: datetime
    usage: Usage | None
    probe: list[str] | None
    out: Path | None
    probe_keys: bool


def register(sub: Any) -> None:
    child = sub.add_parser("spend", help="recorded model spend by lane, day and model, with key reconciliation",
                           description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter,
                           exit_on_error=False)
    child.add_argument("roots", type=Path, metavar="ROOTS.toml")
    child.add_argument("--since", metavar="DATE", help="first UTC day, inclusive, YYYY-MM-DD")
    child.add_argument("--until", metavar="DATE", help="UTC day after the last, exclusive, YYYY-MM-DD")
    child.add_argument("--as-of", metavar="INSTANT", help="instant of the key counters, ISO 8601 with offset; default now")
    counters = child.add_mutually_exclusive_group()
    counters.add_argument("--key-usage", metavar="TOTAL,DAILY,WEEKLY,MONTHLY", help="key counters in USD")
    counters.add_argument("--key-probe", metavar="JSON_ARGV", help="JSON argv array that prints the 4 key counters")
    counters.add_argument("--probe-keys", action="store_true", help="read live OpenRouter counters for each key_env")
    child.add_argument("--json", type=Path, metavar="OUT", help="also write the result to the new file OUT")
    child.add_argument("--lenient", action="store_true", help="list input problems and exit 0")
    child.set_defaults(run=run)


def _say(message: str) -> None:
    assert message
    with contextlib.suppress(OSError, ValueError):
        print(f"campaign: spend: {message}", file=sys.stderr)


def bad_arguments() -> int:
    _say("invalid arguments (values not shown); see campaign spend --help")
    return 2


def parse_usage(text: str) -> Usage | None:
    if not isinstance(text, str) or not 0 < len(text) <= MAX_USAGE_CHARS:
        return None
    parts = [part.strip() for part in text.strip().split(",")]
    if len(parts) != 4 or not all(NUMBER.fullmatch(part) for part in parts):
        return None
    try:
        values = [Decimal(part) for part in parts]
    except InvalidOperation:
        return None
    if any(amount(value) is None for value in values):
        return None
    total, daily, weekly, monthly = values
    if daily > weekly or daily > monthly or weekly > total or monthly > total:
        return None
    return total, daily, weekly, monthly


def _day(text: str | None) -> tuple[date | None, bool]:
    if text is None:
        return None, True
    if not DAY.fullmatch(text):
        return None, False
    try:
        return date.fromisoformat(text), True
    except ValueError:
        return None, False


def _instant(text: str) -> datetime | None:
    if not 0 < len(text) <= MAX_INSTANT_CHARS:
        return None
    try:
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None or moment.utcoffset() is None:
            return None
        return moment.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _probe_argv(text: str) -> list[str] | None:
    if not 0 < len(text) <= MAX_PROBE_JSON_CHARS:
        return None
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, list) or not 0 < len(value) <= MAX_PROBE_ARGS:
        return None
    if not all(isinstance(item, str) and 0 < len(item) <= MAX_PROBE_ARG_CHARS and "\x00" not in item
               for item in value):
        return None
    return [os.path.expanduser(item) if item.startswith("~/") else item for item in value]


def _window(args: argparse.Namespace, now: datetime) -> tuple[date | None, date | None, datetime | None, str]:
    since, since_ok = _day(args.since)
    until, until_ok = _day(args.until)
    if not since_ok or not until_ok:
        return None, None, None, "--since and --until need a calendar date YYYY-MM-DD"
    if since is not None and until is not None and since >= until:
        return None, None, None, "--since must be before --until (until is exclusive)"
    if args.as_of is None:
        return since, until, now, ""
    if args.key_probe is not None:
        return None, None, None, "--as-of is for recorded counters; a live --key-probe reads them now"
    as_of = _instant(args.as_of)
    if as_of is None:
        return None, None, None, "--as-of needs an ISO 8601 instant with an offset, for example 2026-11-24T15:24:28Z"
    if as_of > now:
        return None, None, None, "--as-of is in the future"
    return since, until, as_of, ""


def _out(value: Path | None) -> tuple[Path | None, str]:
    if value is None:
        return None, ""
    try:
        out = value.expanduser()
    except RuntimeError:
        return None, "cannot expand the --json path (path not shown)"
    try:
        present = os.path.lexists(out)
        folder = out.parent.is_dir()
    except OSError:
        return None, "the --json path is not readable (path not shown)"
    if present:
        return None, "the --json file exists already; give a new path (path not shown)"
    if not folder:
        return None, "the folder of the --json file does not exist (path not shown)"
    return out, ""


def options(args: argparse.Namespace, now: datetime) -> tuple[Options | None, str]:
    assert now.tzinfo is not None
    if args.probe_keys and args.as_of is not None:
        return None, "--probe-keys reads current counters and does not accept --as-of"
    since, until, as_of, problem = _window(args, now)
    if problem:
        return None, problem
    assert as_of is not None
    usage = None
    if args.key_usage is not None:
        usage = parse_usage(args.key_usage)
        if usage is None:
            return None, ("--key-usage needs 4 numbers TOTAL,DAILY,WEEKLY,MONTHLY in USD with "
                          "daily <= weekly <= total and daily <= monthly <= total (value not shown)")
    probe = None
    if args.key_probe is not None:
        probe = _probe_argv(args.key_probe)
        if probe is None:
            return None, "--key-probe needs a JSON array of 1 to 64 strings (value not shown)"
    out, problem = _out(args.json)
    if problem:
        return None, problem
    return Options(since, until, as_of, usage, probe, out, args.probe_keys), ""


def read_probe(argv: list[str], capture: Capture) -> tuple[Usage | None, str]:
    assert argv
    code, output = capture(list(argv), PROBE_TIMEOUT_SECONDS, os.getpid())
    if code is None:
        return None, f"the key probe timed out after {PROBE_TIMEOUT_SECONDS:.0f} s or did not start"
    if code != 0:
        return None, f"the key probe exited with {code}"
    usage = parse_usage(output) if isinstance(output, str) else None
    if usage is None:
        return None, "the key probe did not print 4 consistent numbers (output not shown)"
    return usage, ""


def write_json(out: Path, result: dict[str, Any]) -> bool:
    try:
        body = json.dumps(result, indent=1, allow_nan=False) + "\n"
    except (TypeError, ValueError):
        return False
    try:
        handle, staged = tempfile.mkstemp(prefix=".spend-", suffix=".tmp", dir=out.parent)
    except OSError:
        return False
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(staged, out)
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            os.unlink(staged)
    return True


def _print(body: str) -> bool:
    try:
        sys.stdout.write(body if body.endswith("\n") else body + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        return False
    return True


def run(args: argparse.Namespace, *, now: datetime | None = None, capture: Capture | None = None,
        key_system: KeyProbeSystem | None = None) -> int:
    from campaign import spend

    moment = datetime.now(UTC) if now is None else now
    assert moment.tzinfo is not None
    chosen, problem = options(args, moment.astimezone(UTC))
    if chosen is None:
        _say(problem)
        return 2
    usage, probe_problem = chosen.usage, ""
    if chosen.probe is not None:
        usage, probe_problem = read_probe(chosen.probe, capture or RealSystem().capture)
        if now is None:
            moment = datetime.now(UTC)
    try:
        roots = args.roots.expanduser()
    except RuntimeError:
        _say("cannot expand the roots file path (path not shown)")
        return 1
    result = spend.report(roots, since=chosen.since, until=chosen.until,
                          as_of=moment if chosen.probe is not None else chosen.as_of, usage=usage,
                          key_system=(key_system or RealSystem()) if chosen.probe_keys else None)
    problems = result.get("problems")
    assert isinstance(problems, list)
    if probe_problem:
        problems.append(probe_problem)
    failed = bool(probe_problem) or any(row["problem"] for row in result.get("key_reconciliation", ()))
    if chosen.out is not None and not write_json(chosen.out, result):
        _say("cannot create the --json file (it exists now or is not writable; path not shown)")
        failed = True
    if not _print(spend.text(result)):
        failed = True
    if failed:
        return 1
    return 0 if args.lenient or not problems else 1
