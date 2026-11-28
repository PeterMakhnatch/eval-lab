from __future__ import annotations

import io
import json
import os
import shutil
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from muster import cli
from muster.commands import events as events_command
from muster.commands import regrade as regrade_command
from muster.lab import contrast, episode, host, hostjob, load, parity
from muster.lab.schedule import RoundRobin

HOST_SEED = 0
FAMILIES = {
    "depot": (("storm-probe", "calm-guard", "skew-solo"), "calm-team"),
    "dossier": (("share-probe", "gone-reader", "mix-solo"), "scarce-team"),
    "inquiry": (("plain-probe", "month-fields"), "range-flagged"),
}
INSTANCES = 10
CONCURRENCY = 6
TIMEOUT_SEC = 1.0
STOP_LIMIT_SEC = 12.0
TEAM_KEYS = ("reward", "focal", "register_sufficient")
REVISION = {"revision": "simulated", "dirty": False}
SLEEPER = ["bash", "-c", "sleep 1000 & echo $! > {seatdir}/child.pid; echo $$ > {seatdir}/agent.pid; wait"]
LEAKER = ["bash", "-c", "echo $SEAT_TOKEN; echo $SEAT_TOKEN {token} > leak.txt; pwd > where.txt; exit 3"]


def _agent(directory: Path, name: str, argv: list[str]) -> host.Agent:
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"about": name, "argv": argv}), encoding="utf-8")
    return host.read_agent(path)


def _cases(rng: Random) -> list[tuple[str, str, str]]:
    cases = []
    for family, (single, team) in FAMILIES.items():
        instance = f"i{rng.randrange(1, INSTANCES + 1):02d}"
        cases.append((family, rng.choice(single), instance))
        cases.append((family, team, instance))
    return cases


def _expected(adapters: dict[str, Any], family: str, task: Path, policy: str) -> dict[str, float]:
    assert family in FAMILIES
    if family not in adapters:
        adapters[family] = load.family(family)
    adapter, chosen = adapters[family], parity.packaged_instance(task)
    lineup = dict.fromkeys(adapter.seats(chosen), policy)
    result = episode.run(adapter, chosen, lineup, RoundRobin())
    assert result.ended == "all_done"
    return {key: round(float(value), 6) for key, value in result.rewards.items()}


def _trials(cases: list[tuple[str, str, str]], timeout: float | None) -> list[host.Trial]:
    trials = []
    for family, condition, instance in cases:
        task = parity.TASKS / family / "build" / condition / f"{family}-{instance}"
        trials.extend(host.plan(task, 1, timeout))
    return trials


def _quiet(*args: str) -> tuple[int, str]:
    stdout = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
        status = cli.main(list(args))
    return status, stdout.getvalue()


def _files(root: Path) -> list[Path]:
    return [Path(base) / name for base, _, names in os.walk(root) for name in names]


def _no_tokens(sim: Any, step: str, root: Path, trials: list[host.Trial]) -> None:
    secrets = {token.encode() for trial in trials for token in parity.seat_tokens(trial.task).values()}
    leaked = [str(path) for path in _files(root) if any(secret in path.read_bytes() for secret in secrets)]
    sim.require(step, "no-token-in-output", not leaked, ", ".join(leaked[:3]))


def _parity(sim: Any, root: Path, agents: Path, cases: list[tuple[str, str, str]]) -> None:
    trials = _trials(cases, None)
    adapters: dict[str, Any] = {}
    for policy in ("oracle", "idle"):
        agent = _agent(agents, policy, ["bash", "{task}/policies/" + policy + ".sh"])
        out = root / policy
        out.mkdir()
        records = host.run_job(trials, agent, out, root / "homes", CONCURRENCY, lambda record: None)
        by_name = {record["trial"]: record for record in records}
        for trial, (family, condition, instance) in zip(trials, cases, strict=True):
            record = by_name[trial.name]
            name = f"{policy}/{family}/{condition}/{instance}"
            want = _expected(adapters, family, trial.task, policy)
            got = record["rewards"] or {}
            if len(trial.seats) > 1:
                keys = [key for key in TEAM_KEYS if key in want]
                got, want = {key: got.get(key) for key in keys}, {key: want[key] for key in keys}
            sim.require("host", f"{name}/rewards", got == want and "reward" in want, f"{got} != {want}")
            sim.require("host", f"{name}/clean-exit", record["error"] is None and not any(
                record["timed_out"].values()) and set(record["exit_codes"].values()) == {0})
            sim.require("host", f"{name}/home-removed", not any(
                os.path.lexists(path) for path in record["home_during_run"].values()))
            sim.require("host", f"{name}/instruction", all(
                (out / trial.name / seat / "instruction.md").read_bytes()
                == (trial.task / "instructions" / f"{seat}.md").read_bytes() for seat in trial.seats))
            log = (out / trial.name / "verifier/events.jsonl").read_text(encoding="utf-8").splitlines()
            seated = sum(json.loads(line)["seat"] in trial.seats for line in log)
            counted = sum(sum(row.values()) for row in record["requests"].values())
            sim.require("host", f"{name}/requests", seated == counted > 0, f"{seated} != {counted}")
        _readers(sim, policy, out, len(trials))
        _no_tokens(sim, "host", out, trials)


def _readers(sim: Any, policy: str, out: Path, count: int) -> None:
    regraded = regrade_command.report(out)
    sim.require("host", f"{policy}/regrade", regraded["counts"] == {"match": count}, str(regraded["counts"]))
    measured = events_command.report([out])["arms"][0]
    sim.require("host", f"{policy}/events", measured["trials"] == measured["with_log"] == count)
    placed = contrast.report([out])
    sim.require("host", f"{policy}/contrast", placed["placed"] == count and not placed["unplaced"])


def _rerun(sim: Any, root: Path, agents: Path, cases: list[tuple[str, str, str]]) -> None:
    out = root / "oracle"
    trials = _trials(cases, None)
    pending, done, parked = host.sort_out(out, trials)
    sim.require("rerun", "skip-finished", not pending and len(done) == len(trials) and not parked)
    (out / trials[0].name / "result.json").unlink()
    pending, done, parked = host.sort_out(out, trials)
    sim.require("rerun", "park-incomplete", [trial.name for trial in pending] == [trials[0].name]
                and len(parked) == 1 and parked[0].parent == out / host.INCOMPLETE
                and not os.path.lexists(out / trials[0].name))


def _alive(path: Path) -> bool:
    pid = int(path.read_text().strip())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _timeout(sim: Any, root: Path, agents: Path, case: tuple[str, str, str]) -> None:
    trial, = _trials([case], TIMEOUT_SEC)
    agent = _agent(agents, "sleeper", SLEEPER)
    out = root / "sleeper"
    out.mkdir()
    started = time.monotonic()
    record = host.run_trial(trial, agent, out, root / "homes", threading.Event(), REVISION)
    elapsed = time.monotonic() - started
    sim.require("timeout", "bounded", elapsed < STOP_LIMIT_SEC, f"{elapsed:.1f} s")
    sim.require("timeout", "flagged", all(record["timed_out"].values()) and record["error"] is None)
    sim.require("timeout", "graded", isinstance(record["reward"], float)
                and (out / trial.name / "result.json").is_file())
    pids = [out / trial.name / seat / name for seat in trial.seats for name in ("agent.pid", "child.pid")]
    sim.require("timeout", "no-stray-process", all(path.is_file() and not _alive(path) for path in pids))


def _failures(sim: Any, root: Path, agents: Path, case: tuple[str, str, str]) -> None:
    trial, = _trials([case], None)
    task = str(trial.task)
    missing = agents / "missing.json"
    missing.write_text(json.dumps({"argv": ["/nonexistent/muster-agent"]}), encoding="utf-8")
    status, text = _quiet("host", task, "--agent", str(missing), "--out", str(root / "missing"),
                          "--home-root", str(root / "homes"), "--json")
    row, = json.loads(text)["trials"] if status in (0, 1) else [{}]
    sim.require("failure", "cannot-start", status == 1 and row.get("reward") is None
                and "cannot start" in str(row.get("error")))
    sim.require("failure", "no-result", not (root / "missing" / trial.name / "result.json").exists())
    leaker = _agent(agents, "leaker", LEAKER)
    out = root / "leaker"
    out.mkdir()
    record = host.run_trial(trial, leaker, out, root / "homes", threading.Event(), REVISION)
    sim.require("failure", "nonzero-graded", set(record["exit_codes"].values()) == {3}
                and record["reward"] is not None and record["error"] is None)
    seat = trial.seats[0]
    where = (out / trial.name / seat / "home" / "where.txt").read_text().strip()
    sim.require("failure", "home-private", os.path.realpath(where) == os.path.realpath(
        record["home_during_run"][seat]) and not where.startswith(str(root / "leaker")))
    sim.require("failure", "masked", (out / trial.name / seat / "agent.stdout").read_text().strip() == "***")
    _no_tokens(sim, "failure", out, [trial])


def run(sim: Any, seed: int) -> None:
    if seed != HOST_SEED:
        return
    with sim.measure("host"), TemporaryDirectory(prefix="muster-host-sim-") as folder:
        root = Path(os.path.realpath(folder))
        agents, homes = root / "agents", root / "homes"
        agents.mkdir()
        homes.mkdir()
        cases = _cases(Random(seed))
        with patch.object(hostjob, "revision", return_value=REVISION):
            _parity(sim, root, agents, cases)
            _rerun(sim, root, agents, cases)
            _timeout(sim, root, agents, cases[0])
            _failures(sim, root, agents, cases[0])
        sim.require("host", "homes-empty", not any(homes.iterdir()))
        shutil.rmtree(root / "agents")
