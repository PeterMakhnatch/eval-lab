from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from typing import TypedDict

from worldwatch._io import atomic_write_text
from worldwatch._sim_cases import (
    SimRun,
    SimViolation,
    exercise_all_detectors,
    exercise_bespoke_and_malformed,
    exercise_incremental_stream,
    exercise_tripwires_xss_and_stop,
    exercise_validity_and_baselines,
)
from worldwatch._sim_f1_f12 import exercise_redteam_regressions
from worldwatch._sim_workflow import exercise_compare_and_preflight
from worldwatch.cli import main as worldwatch_main
from worldwatch.model import canonical_json

_FIXED_SEEDS = tuple(range(32))
_MAX_SEED = 2**64 - 1
_DEFAULT_HUNT_SEED = 20261124
_MAX_REGRESSIONS = 1024
_MAX_HUNT_SEEDS = 1_000_000
_REGRESSIONS_PATH = Path(__file__).with_name("sim_regressions.json")


class RegressionEntry(TypedDict):
    seed: int
    step: str
    invariant: str
    known: bool


def _capture_cli(argv: list[str]) -> tuple[int, str, str, str]:
    out_buf, err_buf = StringIO(), StringIO()
    status = -1
    exc_text = ""
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        try:
            status = worldwatch_main(argv)
        except SystemExit as exc:
            status = exc.code if isinstance(exc.code, int) else 1
        except BaseException as exc:
            exc_text = f"{type(exc).__name__}: {exc}"
    return status, out_buf.getvalue(), err_buf.getvalue(), exc_text


def _single_pass(seed: int) -> tuple[SimRun, str]:
    assert 0 <= seed <= _MAX_SEED
    run = SimRun(seed=seed)
    rng = Random(seed)
    with TemporaryDirectory(prefix="worldwatch-sim-") as tmp_dir:
        root = Path(tmp_dir)
        exercise_incremental_stream(run, root / "s1", Random(f"{seed}:stream"))
        exercise_tripwires_xss_and_stop(run, root / "s2", Random(f"{seed}:tripwires"))
        exercise_all_detectors(run, root / "s3", Random(f"{seed}:detectors"), _capture_cli)
        exercise_validity_and_baselines(run, root / "s4", rng, _capture_cli)
        exercise_bespoke_and_malformed(run, root / "s5", Random(f"{seed}:bespoke"), _capture_cli)
        exercise_redteam_regressions(run, root / "s6", _capture_cli)
        exercise_compare_and_preflight(run, root / "s7", Random(f"{seed}:workflow"), _capture_cli)
    digest = hashlib.sha256(canonical_json(run.digest_items).encode("utf-8")).hexdigest()
    return run, digest


def run_seed(seed: int) -> tuple[SimRun, str]:
    pass_one, digest_one = _single_pass(seed)
    pass_two, digest_two = _single_pass(seed)
    pass_one.require(
        "determinism/two-pass",
        "byte-identical-canonical-sha256",
        digest_one == digest_two and [v.format() for v in pass_one.violations] == [v.format() for v in pass_two.violations],
        f"{digest_one} vs {digest_two}",
    )
    return pass_one, digest_one


def _load_regressions() -> list[RegressionEntry]:
    if not _REGRESSIONS_PATH.is_file():
        return []
    rows = json.loads(_REGRESSIONS_PATH.read_text(encoding="utf-8"))
    assert isinstance(rows, list) and len(rows) <= _MAX_REGRESSIONS
    for row in rows:
        assert type(row["seed"]) is int and 0 <= row["seed"] <= _MAX_SEED
        assert isinstance(row["step"], str) and bool(row["step"])
        assert isinstance(row["invariant"], str) and bool(row["invariant"])
        assert type(row["known"]) is bool
    return rows


def _matches(v: SimViolation, row: RegressionEntry) -> bool:
    return (v.seed, v.step, v.invariant) == (row["seed"], row["step"], row["invariant"])


def cmd_check() -> int:
    started = time.monotonic()
    rows = _load_regressions()
    seeds = list(dict.fromkeys([*(r["seed"] for r in rows), *_FIXED_SEEDS]))
    scenarios = checks = unexpected = known = 0
    digests: list[str] = []
    for seed in seeds:
        result, digest = run_seed(seed)
        digests.append(digest)
        scenarios += result.scenarios
        checks += result.checks
        for violation in result.violations:
            if any(r["known"] and _matches(violation, r) for r in rows):
                known += 1
                print(f"Known regression: {violation.format()}")
            else:
                unexpected += 1
                print(f"Failure: {violation.format()}", file=sys.stderr)
    suite_sha = hashlib.sha256(":".join(digests).encode("ascii")).hexdigest()
    elapsed = time.monotonic() - started
    print(
        f"Seeds run: {len(seeds)} (2 passes each). Scenarios: {scenarios}. "
        f"Invariants checked: {checks}. Unexpected violations: {unexpected}. "
        f"Known violations: {known}. Suite SHA-256: {suite_sha}. Time: {elapsed:.2f} s."
    )
    return 1 if unexpected > 0 else 0


def cmd_hunt(seconds: int, start_seed: int) -> int:
    assert 1 <= seconds <= 86_400
    assert 0 <= start_seed <= _MAX_SEED
    started = time.monotonic()
    chooser = Random(start_seed)
    seed = start_seed
    rows = _load_regressions()
    scenarios = checks = 0
    explored = 0
    for idx in range(1, _MAX_HUNT_SEEDS + 1):
        explored = idx
        result, _ = run_seed(seed)
        scenarios += result.scenarios
        checks += result.checks
        fresh = [v for v in result.violations if not any(_matches(v, r) for r in rows)]
        if fresh:
            assert len(rows) + len(fresh) <= _MAX_REGRESSIONS
            for v in fresh:
                rows.append({"seed": v.seed, "step": v.step, "invariant": v.invariant, "known": False})
                print(f"Failure: {v.format()}", file=sys.stderr)
            atomic_write_text(_REGRESSIONS_PATH, json.dumps(rows, indent=2) + "\n")
            print(f"Seeds explored: {explored}. Recorded violations: {len(fresh)}. Start seed: {start_seed}.")
            return 1
        if time.monotonic() - started >= seconds:
            break
        seed = chooser.getrandbits(64)
    elapsed = time.monotonic() - started
    print(
        f"Seeds explored: {explored} (2 passes each). Scenarios: {scenarios}. "
        f"Invariants checked: {checks}. No new violations. Start seed: {start_seed}. Time: {elapsed:.2f} s."
    )
    return 0


def cmd_replay(seed: int) -> int:
    assert 0 <= seed <= _MAX_SEED
    result, digest = run_seed(seed)
    for v in result.violations:
        print(f"Failure: {v.format()}", file=sys.stderr)
    print(
        f"Seed: {seed} (2 passes). Scenarios: {result.scenarios}. "
        f"Invariants checked: {result.checks}. SHA-256: {digest}."
    )
    return 1 if result.violations else 0


def _seed_arg(text: str) -> int:
    val = int(text)
    if not 0 <= val <= _MAX_SEED:
        raise argparse.ArgumentTypeError("seed must be an unsigned 64-bit integer")
    return val


def _seconds_arg(text: str) -> int:
    val = int(text)
    if not 1 <= val <= 86_400:
        raise argparse.ArgumentTypeError("seconds must be between 1 and 86400")
    return val


def dispatch_parsed(args: argparse.Namespace) -> int:
    sub = getattr(args, "sim_command", None) or getattr(args, "command", None)
    if sub == "check":
        return cmd_check()
    if sub == "hunt":
        if not 1 <= args.seconds <= 86_400 or not 0 <= args.start_seed <= _MAX_SEED:
            print("worldwatch.sim: invalid --seconds or --start-seed", file=sys.stderr)
            return 2
        return cmd_hunt(args.seconds, args.start_seed)
    assert sub == "replay"
    if not 0 <= args.seed <= _MAX_SEED:
        print("worldwatch.sim: invalid seed", file=sys.stderr)
        return 2
    return cmd_replay(args.seed)


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    p = argparse.ArgumentParser(prog="python -m worldwatch.sim")
    subs = p.add_subparsers(dest="command", required=True)
    subs.add_parser("check", help="replay fixed and recorded seeds")
    hunt = subs.add_parser("hunt", help="search for and record new violations")
    hunt.add_argument("--seconds", type=_seconds_arg, required=True)
    hunt.add_argument("--start-seed", type=_seed_arg, default=_DEFAULT_HUNT_SEED)
    rep = subs.add_parser("replay", help="replay a single seed")
    rep.add_argument("seed", type=_seed_arg)
    args = p.parse_args(argv)
    return dispatch_parsed(args)


if __name__ == "__main__":
    raise SystemExit(main())
