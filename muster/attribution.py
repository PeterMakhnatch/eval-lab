"""Control-matrix analysis (docs/DESIGN.md section 6).

Pure: loaded trials and task facts in, a report dict out. The same input gives the same report.

- Null policy: the policy that stands for "does nothing" in the rules. It is `nop`, unless the
  caller names another one for a task whose verifier wants a record from every seat.
- Score key: `reward` or `seat.<name>`. Other reward keys are diagnostics (section 4, rule 5).
  They are in the tables, but `adversary-wins` and `decorative-seat` read score keys only.
- Timeout: a trial with a valid reward whose only exceptions are AgentTimeoutError. In a roster
  with a seat that is not `oracle` it is a normal outcome, because an oracle can wait for a
  partner that never acts. In the all-oracle roster it is a `trial-error`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from statistics import fmean
from typing import Any

from muster.model import InputError, Problem, SeatTrial
from muster.render import number, table
from muster.roster import NOP, ORACLE
from muster.roster import roster_key as name

REWARD = "reward"
TIMEOUT = "AgentTimeoutError"
EPS = 1e-9
SEVERITY = {  # the stable codes
    "unsolvable": "error",
    "free-pass": "error",
    "tested-seat-not-needed": "error",
    "credit-leak": "error",
    "decorative-seat": "warn",
    "adversary-wins": "error",
    "not-deterministic": "warn",
    "trial-error": "warn",
}

Assignment = tuple[tuple[str, str], ...]  # (seat, policy) in seat declaration order
Means = dict[Assignment, dict[str, float]]  # the mean of every reward key, for each roster


def analyze(
    items: Iterable[SeatTrial | Problem],
    *,
    root: Path | None = None,
    oracle_reward: float | None = None,
    exploits: Sequence[str] = (),
    null: str = NOP,
    planned: Sequence[dict[str, Any]] = (),
    attempts: int | None = None,
    expected_findings: Sequence[str] = (),
) -> dict[str, Any]:
    """The attribution report of one control matrix.

    `planned` lists the rosters of the matrix in order, each {"roster": key} with the optional
    `returncode` and `timed_out` of its harbor run. `attempts` is the planned trials for each roster.
    `expected_findings` are codes that the task's design produces on purpose ([metadata]
    expected_findings), for example `tested-seat-not-needed` in a game where only 2 of 3 seats can score.
    Their findings stay in the report with severity `expected` and do not count as errors or warnings.
    """
    items = list(items)
    trials = sorted((i for i in items if isinstance(i, SeatTrial)), key=lambda t: (t.trial_name, str(t.path)))
    problems = [item for item in items if isinstance(item, Problem)]
    if not trials:
        raise InputError("no seat trial found")
    seats = [(run.seat, run.under_test) for run in trials[0].seats]
    for trial in trials:
        if trial.task != trials[0].task:
            raise InputError(f"the trials are of more than one task: {trials[0].task}, {trial.task}")
        if [(run.seat, run.under_test) for run in trial.seats] != seats:
            raise InputError(f"the trials do not have the same seats: {trials[0].path} and {trial.path}")

    grouped: dict[Assignment, list[SeatTrial]] = {_parse(entry.get("roster"), seats): [] for entry in planned}
    n_planned = len(grouped)
    for trial in sorted(trials, key=SeatTrial.roster_key):  # a stable sort: trial order stays in a roster
        grouped.setdefault(tuple((run.seat, run.label) for run in trial.seats), []).append(trial)
    rows = [_row(assignment, group) for assignment, group in grouped.items()]
    means = {a: {k: c["mean"] for k, c in r["rewards"].items()} for a, r in zip(grouped, rows, strict=True)}
    keys = sorted({k for r in rows for k in r["rewards"]}, key=lambda k: (k != REWARD, not _is_score(k), k))
    expected = 1.0 if oracle_reward is None else float(oracle_reward)
    findings, skipped = _rules(seats, rows, means, keys, expected, list(exploits), null)

    def error(message: str) -> None:
        findings.append({"code": "trial-error", "severity": "warn", "message": message})

    for (assignment, group), row in zip(grouped.items(), rows, strict=True):
        for trial in group:
            unexpected = "timeout" if all(policy == ORACLE for _, policy in assignment) else None
            if _outcome(trial) in ("error", unexpected):
                types = {trial.exception, *(run.exception for run in trial.seats)} - {None}
                what = ", ".join(sorted(types)) or "no valid reward"
                error(f"Roster {row['roster']}: trial {trial.trial_name} has {what}.")
    for row in rows[:n_planned]:
        if attempts is not None and row["trials"] < attempts:
            error(f"Roster {row['roster']} has {row['trials']} of {attempts} trials.")
    failures = [e for e in planned if e.get("timed_out") or e.get("returncode") not in (0, None)]
    for entry in failures:
        how = "did not end in time" if entry.get("timed_out") else f"ended with status {entry['returncode']}"
        error(f"harbor {how} for roster {entry.get('roster')}.")
    for problem in problems:
        error(f"{_relative(problem.path, root)}: {problem.reason}.")
    declared = set(expected_findings)
    for finding in findings:
        if finding["code"] in declared and finding["code"] != "trial-error":
            finding["severity"] = "expected"
    return {
        "task": trials[0].task,
        "task_checksums": sorted({trial.task_checksum for trial in trials if trial.task_checksum}),
        "seats": [{"seat": seat, "under_test": under_test} for seat, under_test in seats],
        "null_policy": null,
        "oracle_reward": expected,
        "exploits": list(exploits),
        "reward_keys": keys,
        "trials": len(trials),
        "rosters": rows,
        "effects": _effects(seats, means, keys, null),
        "findings": findings,
        "not_checked": skipped,
        "problems": [{"path": _relative(p.path, root), "reason": p.reason} for p in problems],
        "harbor_failures": len(failures),
        "errors": sum(finding["severity"] == "error" for finding in findings),
        "warnings": sum(finding["severity"] == "warn" for finding in findings),
        "expected_findings": sorted(declared),
        "expected": sum(finding["severity"] == "expected" for finding in findings),
    }


def status(report: dict[str, Any]) -> int:
    """The exit status of a report: 1 when a finding has severity error, else 0."""
    return 1 if report["errors"] else 0


def _parse(key: Any, seats: list[tuple[str, bool]]) -> Assignment:
    parts = [tuple(part.split("=", 1)) for part in key.split(",")] if isinstance(key, str) else []
    if [p[0] for p in parts] != [seat for seat, _ in seats] or not all(len(p) == 2 for p in parts):
        raise InputError(f"the planned roster {key!r} does not fit the seats of the trials")
    return tuple(parts)


def _is_score(key: str) -> bool:
    return key == REWARD or key.startswith("seat.")


def _relative(path: Path, root: Path | None) -> str:
    return str(path) if root is None else os.path.relpath(path, root)


def _outcome(trial: SeatTrial) -> str:
    """ok, timeout (see the module text) or error."""
    exceptions = {trial.exception, *(run.exception for run in trial.seats)} - {None}
    if not trial.reward_valid or not trial.rewards or exceptions - {TIMEOUT}:
        return "error"
    return "timeout" if exceptions else "ok"


def reward_means(trials: Sequence[SeatTrial]) -> tuple[int, dict[str, dict[str, Any]]]:
    """The number of trials with a valid reward, and the mean of every reward key over them."""
    valid = [trial for trial in trials if trial.reward_valid and trial.rewards]
    rewards = {}
    for key in sorted({key for trial in valid for key in trial.rewards}):
        values = [trial.rewards[key] for trial in valid if key in trial.rewards]
        rewards[key] = {"mean": fmean(values), "n": len(values), "values": values}
    return len(valid), rewards


def _row(assignment: Assignment, trials: list[SeatTrial]) -> dict[str, Any]:
    n, rewards = reward_means(trials)
    timeouts = [trial for trial in trials if _outcome(trial) == "timeout"]
    return {
        "roster": name(assignment),
        "trials": len(trials),
        "n": n,
        "rewards": rewards,
        "timeouts": len(timeouts),
        "timeout_seats": sorted({run.seat for trial in timeouts for run in trial.seats if run.exception}),
        "errors": sum(_outcome(trial) == "error" for trial in trials),
    }


def _swap(assignment: Assignment, seat: str, policy: str) -> Assignment:
    """The same roster with one seat's policy replaced."""
    return tuple((s, policy if s == seat else p) for s, p in assignment)


def _effects(seats: list[tuple[str, bool]], means: Means, keys: list[str], null: str) -> list[dict[str, Any]]:
    """For each reward key and seat: the mean over the rosters where the seat is oracle, minus the
    mean over the same rosters with only that seat as the null policy (matched pairs)."""
    effects = []
    for key in keys:
        for seat, under_test in seats:
            pairs = [
                (values[key], means[_swap(assignment, seat, null)][key])
                for assignment, values in means.items()
                if dict(assignment)[seat] == ORACLE
                and key in values
                and key in means.get(_swap(assignment, seat, null), {})
            ]
            high = fmean(pair[0] for pair in pairs) if pairs else None
            low = fmean(pair[1] for pair in pairs) if pairs else None
            effect = None if high is None or low is None else high - low
            fact = {"key": key, "seat": seat, "under_test": under_test, "pairs": len(pairs)}
            effects.append(fact | {"oracle": high, "null": low, "effect": effect})
    return effects


def _rules(
    seats: list[tuple[str, bool]],
    rows: list[dict[str, Any]],
    means: Means,
    keys: list[str],
    expected: float,
    exploits: list[str],
    null: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """The findings of DESIGN section 6 on the rewards, and the rules that the data could not check."""
    findings: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    def add(code: str, message: str, roster: Assignment | None = None, **facts: Any) -> None:
        where = {} if roster is None else {"roster": name(roster)}
        findings.append({"code": code, "severity": SEVERITY[code], "message": message, **where, **facts})

    def skip(code: str, reason: str) -> None:
        skipped.append({"code": code, "reason": reason})

    all_oracle = tuple((seat, ORACLE) for seat, _ in seats)
    all_null = tuple((seat, null) for seat, _ in seats)
    scored = {assignment: values for assignment, values in means.items() if values}
    score_keys = [key for key in keys if _is_score(key)]

    top = means.get(all_oracle, {}).get(REWARD)
    if top is None:
        skip("unsolvable", f"no {REWARD} for the roster with every seat {ORACLE}")
        skip("tested-seat-not-needed", f"no {REWARD} for the roster with every seat {ORACLE}")
    elif top < expected - EPS:
        what = f"Every seat is {ORACLE} and {REWARD} is {number(top)}, below {number(expected)}."
        add("unsolvable", what, all_oracle)
    for roster, values in scored.items():
        idle = [seat for seat, tested in seats if tested and dict(roster)[seat] == null]
        if idle and top is not None and top > EPS and abs(values.get(REWARD, top + 1) - top) <= EPS:
            what = f"tested seat {', '.join(idle)} is {null} and {REWARD} is {number(top)}, the all-oracle {REWARD}"
            add("tested-seat-not-needed", f"Roster {name(roster)}: {what}.", roster, seats=idle)

    low = means.get(all_null, {}).get(REWARD)
    if low is None:
        skip("free-pass", f"no {REWARD} for the roster with every seat {null}")
    elif low > EPS:
        add("free-pass", f"Every seat is {null} and {REWARD} is {number(low)}, above 0.", all_null)

    if not any(key.startswith("seat.") for key in keys):
        skip("credit-leak", "the verifier wrote no seat.<name> key")
    for roster, values in scored.items():
        for seat, policy in roster:
            credit = values.get(f"seat.{seat}", 0.0)
            if policy == null and credit > EPS:
                what = f"seat {seat} is {null} and seat.{seat} is {number(credit)}, above 0"
                add("credit-leak", f"Roster {name(roster)}: {what}.", roster, seat=seat)

    for seat, _ in seats:
        families: dict[Assignment, list[dict[str, float]]] = {}
        for roster, values in scored.items():
            families.setdefault(tuple(item for item in roster if item[0] != seat), []).append(values)
        spreads = [
            max(column) - min(column)
            for family in families.values()
            for key in score_keys
            if len(column := [values[key] for values in family if key in values]) > 1
        ]
        if not spreads:
            skip("decorative-seat", f"seat {seat}: no two rosters with a score differ only in this seat")
        elif max(spreads) <= EPS:
            add("decorative-seat", f"No score changes with the policy of seat {seat}.", seat=seat)

    used = [p for p in exploits if p != null and any(p in dict(roster).values() for roster in scored)]
    if not exploits:
        skip("adversary-wins", "no exploit policy is named ([metadata] exploits or --exploits)")
    elif not used:
        skip("adversary-wins", f"no roster with a score has an exploit policy ({', '.join(exploits)})")
    for roster, values in scored.items():
        for seat, policy in roster:
            base = _swap(roster, seat, null)
            floor = scored.get(base)
            if policy in used and floor is None:
                skip("adversary-wins", f"roster {name(roster)}: no score for {name(base)}")
            for key in score_keys if policy in used and floor else []:
                if key in values and key in floor and values[key] > floor[key] + EPS:
                    what = f"{seat}={policy} gives {key} {number(values[key])}, above {number(floor[key])} with {seat}={null}"
                    add("adversary-wins", f"Roster {name(roster)}: {what}.", roster, seat=seat, key=key)

    if not any(row["n"] > 1 for row in rows):
        skip("not-deterministic", "no roster has 2 or more trials with a valid reward")
    for row in rows:
        for key, cell in row["rewards"].items():
            if max(cell["values"]) - min(cell["values"]) > EPS or cell["n"] != row["n"]:
                listed = ", ".join(number(value) for value in cell["values"])
                what = f"{key} is {listed} in {row['n']} trials"
                add("not-deterministic", f"Roster {row['roster']}: {what}.", key=key)
    return findings, skipped


def text(report: dict[str, Any]) -> str:
    """The report as text: the roster table, the seat effects and the findings."""
    null, keys = report["null_policy"], report["reward_keys"]
    seats = ", ".join(s["seat"] + (" (under test)" if s["under_test"] else "") for s in report["seats"])
    lines = [
        f"task {report['task']}: {report['trials']} trials in {len(report['rosters'])} rosters",
        f"seats: {seats}",
        f"null policy: {null}   oracle reward: {number(report['oracle_reward'])}   "
        f"exploits: {', '.join(report['exploits']) or 'none'}",
    ]
    if len(report["task_checksums"]) > 1:
        lines.append(f"warning: the trials come from {len(report['task_checksums'])} versions of the task")
    rows = [
        [
            row["roster"],
            row["n"],
            *(number(row["rewards"].get(key, {}).get("mean")) for key in keys),
            _notes(row),
        ]
        for row in report["rosters"]
    ]
    lines += ["", "Rosters: the mean of each reward key over the n trials with a valid reward"]
    lines.append(table(["roster", "n", *keys, "notes"], rows, right=range(1, len(keys) + 2)))
    lines += ["", f"Seat effects: the seat as oracle, minus the same rosters with only that seat as {null}"]
    rows = []
    for e in report["effects"]:
        tested, effect = "yes" if e["under_test"] else "no", number(e["effect"], signed=True)
        rows.append([e["key"], e["seat"], tested, number(e["oracle"]), number(e["null"]), effect, e["pairs"]])
    header = ["key", "seat", "under test", "oracle", null, "effect", "pairs"]
    lines.append(table(header, rows, right=range(3, 7)))
    rows = [[finding["severity"], finding["code"], finding["message"]] for finding in report["findings"]]
    lines += ["", "Findings", table(["severity", "code", "finding"], rows) if rows else "none"]
    if report["not_checked"]:
        lines += ["", "Not checked", *(f"{item['code']}: {item['reason']}" for item in report["not_checked"])]
    total = f"{report['errors']} errors, {report['warnings']} warnings"
    if report.get("expected"):
        total += (
            f", {report['expected']} expected by the task's design ({', '.join(report['expected_findings'])})"
        )
    return "\n".join([*lines, "", total]) + "\n"


def _notes(row: dict[str, Any]) -> str:
    notes = [] if row["trials"] else ["no trial"]
    if row["timeouts"]:
        notes.append(" ".join(["timeout", *row["timeout_seats"], f"{row['timeouts']}/{row['trials']}"]))
    if row["errors"]:
        notes.append(f"error {row['errors']}/{row['trials']}")
    return ", ".join(notes)
