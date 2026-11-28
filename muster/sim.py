from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from typing import Any, TypedDict

from muster import events
from muster.cli import main as muster_main
from muster.regrade import replay as regrade_replay
from muster.sim_chain import check_chain
from muster.sim_fixture import (
    Evidence,
    Run,
    Scenario,
    Trial,
    Violation,
    check_horde,
    check_supply,
    play_world,
    select,
    write_json,
    write_log,
    write_trial,
)

_REGRESSIONS = Path(__file__).with_name("sim_regressions.json")
_HMAC_ENV = "MUSTER_SIM_WORLD_HMAC_KEY"
_HMAC_KEY = "sim-regrade-hmac-key"
_FIXED_SEEDS = range(144)
_MAX_REGRESSIONS = 512
_MAX_SEED = 2**64 - 1
_DEFAULT_HUNT_SEED = 261121
_MAX_HUNT_SEEDS = 1_000_000
_HORDE_PERIOD = 8


@dataclass(frozen=True)
class Capture:
    status: int | None
    stdout: str
    stderr: str
    exception: str


def _capture(argv: list[str]) -> Capture:
    stdout, stderr = StringIO(), StringIO()
    status, exception = None, ""
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            status = muster_main(argv)
        except (Exception, SystemExit) as error:
            exception = f"{type(error).__name__}: {error}"
    return Capture(status, stdout.getvalue(), stderr.getvalue(), exception)


def _reject_constant(value: str) -> float:
    raise ValueError(f"invalid JSON constant: {value}")


def _command(
    run: Run, step: str, argv: list[str], status: int, *,
    as_json: bool = True, report_on_error: bool = False,
) -> dict[str, Any]:
    mode = "json" if as_json else "text"
    step = f"{step}/{mode}"
    result = _capture([*argv, "--json"] if as_json else argv)
    trace = result.exception or ("Traceback in stderr" if "Traceback" in result.stderr else "")
    run.require(step, "cli-never-raises-or-prints-traceback", not trace, trace)
    if trace:
        return {}
    run.require(step, "cli-documented-exit-code", result.status == status,
                f"got {result.status}, expected {status}")
    if result.status == 3 and not report_on_error:
        run.require(step, "unusable-input-has-no-report", not result.stdout)
        run.require(step, "unusable-input-has-one-line-diagnostic",
                    result.stderr.startswith("muster: ") and len(result.stderr.splitlines()) == 1)
        return {}
    run.require(step, "report-nonempty", bool(result.stdout.strip()))
    if not as_json:
        return {}
    try:
        report = json.loads(result.stdout, parse_constant=_reject_constant)
    except ValueError as error:
        run.require(step, "json-report-is-valid", False, str(error))
        return {}
    run.require(step, "json-report-is-object", isinstance(report, dict))
    return report if isinstance(report, dict) else {}


def _regrade(run: Run, trial: Trial, name: str, state: str, status: int) -> dict[str, Any]:
    step = f"regrade/{name}"
    args = ["regrade", str(trial.path)]
    report = _command(run, step, args, status, report_on_error=True)
    _command(run, step, args, status, as_json=False, report_on_error=True)
    if report:
        rows = report.get("rows", [])
        run.require(step, "regrade-reports-correct-status",
                    report.get("counts") == {state: 1} and len(rows) == 1
                    and rows[0].get("status") == state, str(report.get("counts")))
        if len(rows) == 1:
            return rows[0]
    return {}


def _measures(run: Run, case: Scenario, trial: Trial, evidence: Evidence) -> None:
    args = ["events", f"scenario={trial.path}", "--trials"]
    report = _command(run, "events/clean", args, 0)
    _command(run, "events/clean", args, 0, as_json=False)
    rows = report.get("arms", [{}])[0].get("rows", [])
    if len(rows) != 1:
        run.require("events/clean", "one-measurement-per-trial", False, f"got {len(rows)}")
        return
    played = [e for e in evidence.events if e["seat"] is not None and not e.get("late")]
    accepted = [e for e in played if e["ok"]]
    expected = {
        "requests": len(played),
        "actions": sum(e["method"] == "POST" and e["kind"] != "msg" for e in accepted),
        "posts": sum(e["kind"] == "msg" for e in accepted),
        "fetches": sum(e["kind"] == "msgs" for e in accepted),
        "probes": sum(e["status"] == 404 for e in played),
        "late": sum(bool(e.get("late")) for e in evidence.events),
        "unauthorized": sum(e["status"] == 401 for e in evidence.events),
        "seats": sorted({*case.task.seats, *case.instance.get("standins", [])}),
    }
    observed = {key: rows[0].get(key) for key in expected}
    run.require("events/clean", "event-measures-agree-with-log", observed == expected,
                f"observed {observed}; expected {expected}")


def _attribution(run: Run, trial: Trial) -> None:
    args = ["attribute", str(trial.path)]
    report = _command(run, "attribute/clean", args, 0)
    _command(run, "attribute/clean", args, 0, as_json=False)
    run.require("attribute/clean", "trial-and-roster-reported",
                (report.get("trials"), report.get("errors"), len(report.get("rosters", []))) == (1, 0, 1))
    config = {**trial.result["config"], "participants": [
        {**entry, "name": "nop"} for entry in trial.result["config"]["participants"]
    ]}
    seats = [{**seat, "agent_info": {"name": "nop"}} for seat in trial.result["seat_results"]]
    result = {**trial.result, "config": config, "seat_results": seats,
              "verifier_result": {"rewards": {"reward": 1.0}}}
    write_json(trial.path / "result.json", result)
    report = _command(run, "attribute/free-pass", args, 1)
    _command(run, "attribute/free-pass", args, 1, as_json=False)
    findings = {(finding["code"], finding["severity"]) for finding in report.get("findings", [])}
    run.require("attribute/free-pass", "positive-reward-with-no-play-is-error", ("free-pass", "error") in findings)
    write_json(trial.path / "result.json", trial.result)


def _reward_faults(run: Run, trial: Trial, evidence: Evidence) -> None:
    result_path = trial.path / "result.json"
    rewards = evidence.rewards
    altered = 1.0 if rewards["reward"] < 0.5 else 0.0
    tampered = {**rewards, "reward": altered}
    write_json(result_path, {**trial.result, "verifier_result": {"rewards": tampered}})
    write_json(trial.reward, tampered)
    row = _regrade(run, trial, "altered-reward", "mismatch", 1)
    run.require("regrade/altered-reward", "exact-reward-difference",
                row.get("differences") == {"reward": [altered, rewards["reward"]]}, str(row.get("differences")))
    unusable = trial.path.with_name("no-log")
    unusable.mkdir()
    write_json(unusable / "result.json", {**trial.result, "trial_name": unusable.name})
    job = ["regrade", str(trial.path.parent)]
    report = _command(run, "regrade/mismatch-and-no-log", job, 1, report_on_error=True)
    run.require("regrade/mismatch-and-no-log", "mismatch-takes-priority",
                report.get("counts") == {"mismatch": 1, "no-log": 1})
    write_json(result_path, trial.result)
    report = _command(run, "regrade/match-and-no-log", job, 3, report_on_error=True)
    run.require("regrade/match-and-no-log", "unusable-trial-not-hidden-by-match",
                report.get("counts") == {"match": 1, "no-log": 1})
    trial.reward.unlink()
    _regrade(run, trial, "missing-reward-file-with-result", "match", 0)
    write_json(result_path, {**trial.result, "verifier_result": None, "reward_valid": False})
    _regrade(run, trial, "missing-recorded-reward", "no-recorded-reward", 3)
    for argv in (["events", str(trial.path)], ["attribute", str(trial.path)]):
        for mode in (True, False):
            _command(run, f"{argv[0]}/missing-reward", argv, 0, as_json=mode)
    write_json(result_path, trial.result)
    write_json(trial.reward, rewards)


def _log_faults(run: Run, trial: Trial, evidence: Evidence) -> None:
    graded = trial.graded.read_bytes()
    collected = trial.collected.read_bytes()
    trial.graded.unlink()
    row = _regrade(run, trial, "fallback", "mismatch", 1)
    run.require("regrade/fallback", "collected-log-used-when-graded-missing", row.get("log") == "collected")
    write_log(trial.graded, evidence.events[:evidence.truncate_at])
    _regrade(run, trial, "truncated-log", "mismatch", 1)
    trial.collected.unlink()
    first, _, rest = graded.partition(b"\n")
    gap = json.dumps({**json.loads(first), "seq": 2}).encode() + b"\n" + rest
    faults = (
        ("sequence-gap", gap, "line 1 is not event 1"),
        ("malformed-json", b"{\n" + rest, "line 1 is not valid JSON"),
        ("invalid-encoding", b"\xff\n" + rest, "events.jsonl is not UTF-8 text"),
    )
    for name, payload, diagnostic in faults:
        trial.graded.write_bytes(payload)
        row = _regrade(run, trial, name, "no-log", 3)
        run.require(f"regrade/{name}", "corruption-is-diagnosed", row.get("graded_log_problem") == diagnostic)
        if name == "invalid-encoding":
            run.require(f"regrade/{name}", "non-utf8-flags-chain-broken", row.get("chain_ok") is False)
        for command in ("events", "attribute"):
            for mode in (True, False):
                report = _command(run, f"{command}/{name}", [command, str(trial.path)], 0, as_json=mode)
                if command == "events" and report:
                    run.require(f"{command}/{name}", "unreadable-log-is-not-measured",
                                report["arms"][0]["with_log"] == 0)
    trial.graded.write_bytes(graded)
    trial.collected.write_bytes(collected)
    missing = str(trial.path.parent / "missing")
    for command in ("regrade", "events", "attribute"):
        _command(run, f"{command}/missing-directory", [command, missing], 3)
        _command(run, f"{command}/missing-directory", [command, missing], 3, as_json=False)


def _chain_regrade(run: Run, trial: Trial, evidence: Evidence) -> None:
    original = trial.graded.read_bytes()
    sealed = events.chain(evidence.events, _HMAC_KEY)
    lines = [json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) for row in sealed]
    trial.graded.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    args = ["regrade", str(trial.path), "--hmac-key-env", _HMAC_ENV]
    os.environ[_HMAC_ENV] = _HMAC_KEY
    try:
        report = _command(run, "regrade/chain-clean", args, 0, report_on_error=True)
        rows = report.get("rows", [{}])
        run.require("regrade/chain-clean", "v2-log-reports-chain-ok",
                    rows[0].get("chain_ok") is True and report.get("chain_failures") == 0, str(rows[0]))
        forged = [*sealed[:-1], {**sealed[-1], "forged": True}]
        lines = [json.dumps(row, sort_keys=True, separators=(",", ":")) for row in forged]
        trial.graded.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        report = _command(run, "regrade/chain-tampered", args, 1, report_on_error=True)
        _command(run, "regrade/chain-tampered", args, 1, as_json=False, report_on_error=True)
        rows = report.get("rows", [{}])
        run.require("regrade/chain-tampered", "tampered-v2-log-reports-chain-broken",
                    rows[0].get("chain_ok") is False and report.get("chain_failures") == 1, str(rows[0]))
    finally:
        os.environ.pop(_HMAC_ENV, None)
    _command(run, "regrade/chain-no-key", args, 3)
    trial.graded.write_bytes(original)
    report = _command(run, "regrade/chain-legacy", ["regrade", str(trial.path)], 0)
    run.require("regrade/chain-legacy", "v1-log-has-no-chain-fields",
                "chain_ok" not in report.get("rows", [{}])[0] and report.get("chain_checked") == 0)


def _trial(run: Run, case: Scenario, root: Path, evidence: Evidence) -> None:
    trial = write_trial(root, case, evidence)
    row = _regrade(run, trial, "match", "match", 0)
    late = [
        {key: event[key] for key in ("seq", "seat", "kind")}
        for event in evidence.events[evidence.collected_at:]
        if event["ok"] and event["seat"] is not None and not event.get("late")
    ]
    run.require("regrade/match", "full-reward-and-artifact-window-parity",
                row.get("replayed") == evidence.rewards and row.get("log") == "graded"
                and row.get("differences") == {} and row.get("late_actions") == late)
    _chain_regrade(run, trial, evidence)
    _measures(run, case, trial, evidence)
    _attribution(run, trial)
    _reward_faults(run, trial, evidence)
    _log_faults(run, trial, evidence)


def _rosters(run: Run, case: Scenario, root: Path, rng: Random) -> None:
    seats = case.task.seats
    path = root / "roster.json"
    args = ["run", str(case.task.path), "--roster", str(path), "--dry-run", "--out", str(root / "roster-run")]
    entries = [{"seat": seat, "name": rng.choice(["oracle", "nop", "simulation"])} for seat in seats]
    each = {"name": "simulation", "model_name": f"fake/m{rng.randrange(20)}"}
    forms = [
        ("participants", {"participants": rng.sample(entries, len(entries))}, entries),
        ("each", {"each": each}, [{"seat": seat, **each} for seat in seats]),
    ]
    for name, payload, expected in forms:
        write_json(path, payload)
        report = _command(run, f"roster/{name}", args, 0)
        participants = report.get("participants", [])
        actual = [
            {key: entry.get(key) for key in item}
            for entry, item in zip(participants, expected, strict=False)
        ]
        run.require(f"roster/{name}", "valid-roster-maps-every-seat-in-declaration-order",
                    len(participants) == len(seats) and actual == expected and report.get("planned_trials") == 1)
        config = Path(report["config"]) if "config" in report else None
        if config is not None and config.is_relative_to(root) and config.is_file():
            observed = json.loads(config.read_bytes())
            run.require(f"roster/{name}", "dry-run-config-preserves-roster", observed.get("participants") == expected)
        else:
            run.require(f"roster/{name}", "dry-run-config-written-in-scratch", False)
    invalid: dict[str, object] = {
        "missing-seat": {"participants": entries[1:]},
        "duplicate-seat": {"participants": [*entries, entries[0]]},
        "unknown-seat": {"participants": [{**entries[0], "seat": "unknown"}, *entries[1:]]},
        "extra-seat-in-each": {"each": {**each, "seat": seats[0]}},
        "both-shapes": {"participants": entries, "each": each},
        "missing-agent": {"each": {"model_name": "fake/m"}},
        "literal-credential": {"each": {**each, "env": {"API_KEY": "simulation-data"}}},
    }
    for name in rng.sample(list(invalid), 4):
        write_json(path, invalid[name])
        _command(run, f"roster/{name}", args, 3)
    path.write_text('{"participants":', encoding="utf-8")
    _command(run, "roster/malformed-json", args, 3)


def _check_kit_v2_replay(run: Run, root: Path) -> None:
    tasks_root = Path(__file__).resolve().parent / "tasks"
    kit_v2 = str(tasks_root / "_kit_v2")
    for fam in ("compactshift_v1", "skewledger_v1", "protofit_v1", "transact_mem_v1"):
        fam_dir = tasks_root / fam
        cfg = tomllib.loads((fam_dir / "family.toml").read_text(encoding="utf-8"))
        sk_cond = str(cfg["triad"]["sk_condition"])
        task_dir = fam_dir / "build" / sk_cond / f"{fam}-i01"
        saved = list(sys.path)
        sys.path[:0] = [str(fam_dir / "template"), kit_v2, str(fam_dir)]
        sys.modules.pop("worldkit", None)
        sys.modules.pop("codec", None)
        try:
            rspec = importlib.util.spec_from_file_location(f"_sim_{fam}_ref", fam_dir / "reference.py")
            vspec = importlib.util.spec_from_file_location(f"_sim_{fam}_ver", fam_dir / "template" / "verify.py")
            assert rspec and rspec.loader and vspec and vspec.loader
            rmod = importlib.util.module_from_spec(rspec)
            vmod = importlib.util.module_from_spec(vspec)
            rspec.loader.exec_module(rmod)
            vspec.loader.exec_module(vmod)
            inst = dict(rmod.instances(cfg, sk_cond)[0])
            toks = dict(rmod.tokens(cfg, inst))
            runner = getattr(rmod, "replay", None) or getattr(rmod, "play", None)
            assert callable(runner)
            for pol, expected_rew in (("oracle", 1.0), ("nop", 0.0)):
                rep = runner(inst, toks, pol)
                evs = list(rep["events"]) if isinstance(rep, dict) else list(rep[0])
                scores = dict(vmod.score(evs, inst))
                tdir = root / f"kv2_{fam}_{pol}"
                (tdir / "verifier").mkdir(parents=True, exist_ok=True)
                canon_lines = [
                    json.dumps(e, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    for e in evs
                ]
                (tdir / "verifier" / "events.jsonl").write_text(
                    "".join(line + "\n" for line in canon_lines),
                    encoding="utf-8",
                )
                write_json(tdir / "verifier" / "reward.json", scores)
                trial_cfg = {
                    "trial_name": tdir.name,
                    "task": {"name": f"muster/{task_dir.name}", "path": str(task_dir)},
                    "participants": [{"seat": s, "name": pol} for s in inst["seats"]],
                }
                res_doc = {
                    "schema_version": 1,
                    "trial_name": tdir.name,
                    "task_name": f"muster/{task_dir.name}",
                    "config": trial_cfg,
                    "seat_results": [{"seat": s, "under_test": True, "agent_info": {"name": pol}} for s in inst["seats"]],
                    "verifier_result": {"rewards": scores},
                    "reward_valid": True,
                }
                write_json(tdir / "config.json", trial_cfg)
                write_json(tdir / "result.json", res_doc)
                report = _command(run, f"kitv2/{fam}/{pol}", ["regrade", str(tdir)], 0)
                rows = report.get("rows", [])
                matched = report.get("counts") == {"match": 1} and len(rows) == 1 and rows[0].get("status") == "match"
                run.require(
                    f"kitv2/{fam}/{pol}",
                    "regrade-and-triad-oracle-parity",
                    matched and abs(float(scores["reward"]) - expected_rew) < 1e-6,
                    f"scores={scores} counts={report.get('counts')}",
                )
        finally:
            sys.path[:] = saved
            sys.modules.pop("worldkit", None)
            sys.modules.pop("codec", None)


def _check_regrade_regressions(run: Run) -> None:
    inst: dict[str, Any] = {"seed": 7, "nested": {"ok": True}}

    def mutating_score(_events: list[dict[str, Any]], doc: dict[str, Any]) -> dict[str, Any]:
        doc["seed"] = 999
        doc["nested"]["mutated"] = True
        return {"reward": 1.0, "flag_bool": True}

    rep1, err1 = regrade_replay(mutating_score, [{"seq": 1}], inst)
    rep2, err2 = regrade_replay(mutating_score, [{"seq": 1}], inst)
    ok = (
        err1 is None
        and err2 is None
        and rep1 == {"reward": 1.0}
        and rep2 == {"reward": 1.0}
        and inst == {"seed": 7, "nested": {"ok": True}}
        and "flag_bool" not in rep1
    )
    run.require("regrade/isolation-bool", "deepcopy-instance-and-drop-bool", ok, f"rep1={rep1} err1={err1} inst={inst}")


def _run_seed(seed: int) -> Run:
    assert 0 <= seed <= _MAX_SEED
    run, rng = Run(seed), Random(seed)
    with run.section("select"):
        case = select(rng)
        run.case = f"{case.family}/{case.condition}/{case.task.path.name}"
        with TemporaryDirectory(prefix="muster-sim-") as directory:
            root = Path(directory)
            with run.section("supply"):
                check_supply(run, case, rng)
            with run.section("world"):
                evidence = play_world(run, case, rng, root / "world-events.jsonl")
                with run.section("trial"):
                    _trial(run, case, root, evidence)
            with run.section("roster"):
                _rosters(run, case, root, rng)
            if seed == 0:
                with run.section("kitv2"):
                    _check_kit_v2_replay(run, root)
                    _check_regrade_regressions(run)
            with run.section("chain"):
                check_chain(run, rng, root)
    if seed % _HORDE_PERIOD == 0:
        with run.section("horde"):
            check_horde(run, Random(f"horde/{seed}"))
    return run


class Regression(TypedDict):
    seed: int
    step: str
    invariant: str
    detail: str
    known: bool


def _regressions() -> list[Regression]:
    rows = json.loads(_REGRESSIONS.read_bytes())
    assert isinstance(rows, list) and len(rows) <= _MAX_REGRESSIONS
    for row in rows:
        assert type(row["seed"]) is int and 0 <= row["seed"] <= _MAX_SEED
        assert isinstance(row["step"], str) and row["step"]
        assert isinstance(row["invariant"], str) and row["invariant"]
        assert isinstance(row["detail"], str) and type(row["known"]) is bool
    return rows


def _signature(failure: Violation | Regression) -> tuple[str, str, str]:
    if isinstance(failure, Violation):
        return failure.step, failure.invariant, failure.detail
    return failure["step"], failure["invariant"], failure["detail"]


def _check() -> int:
    started = time.monotonic()
    rows = _regressions()
    seeds = list(dict.fromkeys((*(row["seed"] for row in rows), *_FIXED_SEEDS)))
    known = {_signature(row) for row in rows if row["known"]}
    shown: set[tuple[str, str, str]] = set()
    checks = unexpected = expected = 0
    cases = set()
    for seed in seeds:
        result = _run_seed(seed)
        checks += result.checks
        cases.add(result.case.rsplit("/", 1)[0])
        for failure in result.failures:
            key = _signature(failure)
            is_known = key in known
            expected += is_known
            unexpected += not is_known
            if key not in shown:
                shown.add(key)
                print(f"{'Known regression' if is_known else 'Failure'}: {failure}",
                      file=sys.stdout if is_known else sys.stderr)
    print(f"Seeds: {len(seeds)}. Conditions: {len(cases)}. Invariants checked: {checks}. "
          f"Unexpected violations: {unexpected}. Known violations: {expected}. "
          f"Time: {time.monotonic() - started:.2f} s.")
    return int(unexpected > 0)


def _hunt(seconds: int, start_seed: int) -> int:
    started = time.monotonic()
    chooser = Random(start_seed)
    rows = _regressions()
    known = {_signature(row) for row in rows if row["known"]}
    seed = start_seed
    for explored in range(1, _MAX_HUNT_SEEDS + 1):
        result = _run_seed(seed)
        new = [failure for failure in result.failures if _signature(failure) not in known]
        if new:
            seen = {(row["seed"], _signature(row)) for row in rows}
            for failure in new:
                if (seed, _signature(failure)) not in seen:
                    rows.append({"seed": seed, "step": failure.step, "invariant": failure.invariant,
                                 "detail": failure.detail, "known": False})
                print(f"Failure: {failure}", file=sys.stderr)
            assert len(rows) <= _MAX_REGRESSIONS
            with TemporaryDirectory(prefix=".muster-sim-", dir=_REGRESSIONS.parent) as directory:
                staged = Path(directory) / _REGRESSIONS.name
                staged.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
                staged.replace(_REGRESSIONS)
            print(f"Seeds explored: {explored}. New violations: {len(new)}. Start seed: {start_seed}.")
            return 1
        if time.monotonic() - started >= seconds:
            break
        seed = chooser.getrandbits(64)
    print(f"Seeds explored: {explored}. No new violations. Start seed: {start_seed}. "
          f"Time: {time.monotonic() - started:.2f} s.")
    return 0


def _replay(seed: int) -> int:
    result = _run_seed(seed)
    for failure in result.failures:
        print(f"Failure: {failure}", file=sys.stderr)
    print(f"Seed: {seed}. Task: {result.case}. Invariants checked: {result.checks}.")
    return int(bool(result.failures))


def _seed(value: str) -> int:
    seed = int(value)
    if not 0 <= seed <= _MAX_SEED:
        raise argparse.ArgumentTypeError("use an unsigned 64-bit seed")
    return seed


def _seconds(value: str) -> int:
    seconds = int(value)
    if not 1 <= seconds <= 86_400:
        raise argparse.ArgumentTypeError("use a duration between 1 and 86400 seconds")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m muster.sim")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="replay fixed and recorded seeds")
    hunt = commands.add_parser("hunt", help="search for and record new violations")
    hunt.add_argument("--seconds", type=_seconds, required=True)
    hunt.add_argument("--start-seed", type=_seed, default=_DEFAULT_HUNT_SEED)
    replay = commands.add_parser("replay", help="replay a single seed")
    replay.add_argument("seed", type=_seed)
    args = parser.parse_args(argv)
    if args.command == "check":
        return _check()
    if args.command == "hunt":
        return _hunt(args.seconds, args.start_seed)
    assert args.command == "replay"
    return _replay(args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
