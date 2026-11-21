from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from random import Random
from tempfile import NamedTemporaryFile
from typing import TypedDict

from evallab.sim.engine import InvariantViolation, run

REGRESSIONS = Path(__file__).with_name("regressions.json")
_FIXED_SEEDS = range(64)


class FailureKey(TypedDict):
    seed: int
    invariant: str
    step: int
    subsystem: str


class Regression(FailureKey, total=False):
    found_at_utc: str
    known: bool


def failure_key(failure: InvariantViolation) -> FailureKey:
    return {
        "seed": failure.seed,
        "invariant": failure.invariant,
        "step": failure.step,
        "subsystem": failure.subsystem,
    }


def regressions() -> list[Regression]:
    return json.loads(REGRESSIONS.read_bytes())


def matches(row: Regression, key: FailureKey) -> bool:
    return all(row.get(field) == value for field, value in key.items())


def record(failure: InvariantViolation) -> None:
    rows = regressions()
    key = failure_key(failure)
    if any(matches(row, key) for row in rows):
        return
    rows.append({**key, "found_at_utc": datetime.now(UTC).isoformat()})
    path = None
    try:
        with NamedTemporaryFile(mode="w", dir=REGRESSIONS.parent, delete=False) as handle:
            path = Path(handle.name)
            json.dump(rows, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        path.replace(REGRESSIONS)
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def check() -> int:
    rows = regressions()
    seeds = list(dict.fromkeys([*(row["seed"] for row in rows), *_FIXED_SEEDS]))
    suite_digest = hashlib.sha256()
    checks = 0
    identical = 0
    failed = False
    for seed in seeds:
        first = run(seed)
        second = run(seed)
        checks += first.checks + second.checks
        digest = first.digest
        suite_digest.update(bytes.fromhex(digest))
        if digest != second.digest or first.trace != second.trace:
            print(f"Determinism mismatch: seed={seed} {digest} != {second.digest}", file=sys.stderr)
            failed = True
        else:
            identical += 1
        if first.failure is None:
            if any(row["seed"] == seed and row.get("known") for row in rows):
                print(f"Previously known regression passes: seed={seed}")
            continue
        key = failure_key(first.failure)
        if any(row.get("known") and matches(row, key) for row in rows):
            print(f"Known regression: {first.failure}")
        else:
            print(f"Failure: {first.failure}", file=sys.stderr)
            failed = True
    print(f"Seeds run: {len(seeds)} (2 runs each). Invariants checked: {checks}.")
    print(
        f"Determinism hash match: {identical}/{len(seeds)}. Suite SHA-256: {suite_digest.hexdigest()}"
    )
    return int(failed)


def hunt(seconds: int, start_seed: int | None) -> int:
    stop = time.monotonic() + seconds
    seed = secrets.randbits(64) if start_seed is None else start_seed
    chooser = Random(seed)
    for explored in range(1, 10_000_001):
        result = run(seed)
        if result.failure is not None:
            record(result.failure)
            print(f"Seeds explored: {explored}. Failure: {result.failure}")
            return 1
        if time.monotonic() >= stop:
            break
        seed = chooser.getrandbits(64)
    print(f"Seeds explored: {explored}. No invariant failures.")
    return 0


def replay(seed: int, trace: bool) -> int:
    result = run(seed)
    if trace:
        sys.stdout.write(result.trace.decode())
    print(
        f"Seed: {seed}. Invariants checked: {result.checks}. Trace SHA-256: {result.digest}",
        file=sys.stderr if trace else sys.stdout,
    )
    if result.failure is not None:
        print(f"Failure: {result.failure}", file=sys.stderr)
        return 1
    return 0


def parse_seed(value: str) -> int:
    seed = int(value)
    if not 0 <= seed < 2**64:
        raise argparse.ArgumentTypeError("seed must be an unsigned 64-bit integer")
    return seed


def parse_seconds(value: str) -> int:
    seconds = int(value)
    if not 1 <= seconds <= 86_400:
        raise argparse.ArgumentTypeError("seconds must be between 1 and 86400")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evallab.sim")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="run fixed and recorded seeds twice")
    hunt_parser = commands.add_parser("hunt", help="find and record a new failing seed")
    hunt_parser.add_argument("--seconds", required=True, type=parse_seconds)
    hunt_parser.add_argument("--start-seed", type=parse_seed)
    replay_parser = commands.add_parser("replay", help="replay one seed")
    replay_parser.add_argument("--seed", required=True, type=parse_seed)
    replay_parser.add_argument("--trace", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "check":
        return check()
    if args.command == "hunt":
        return hunt(args.seconds, args.start_seed)
    assert args.command == "replay"
    return replay(args.seed, args.trace)


if __name__ == "__main__":
    raise SystemExit(main())
