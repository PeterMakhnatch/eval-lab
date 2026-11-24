from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from random import Random
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from campaign import evidence, report
from campaign.plan import Plan, PlanError, parse
from campaign.runner import Runner, Stop, expand_first_run
from campaign.sim_fixture import (
    BASE,
    SHELL_PID,
    Scenario,
    World,
    plan_data,
    scenario,
    toml_text,
    usd,
)

if TYPE_CHECKING:
    from campaign.sim import Run

MUTATIONS = {
    "no-seat-list-zero": "cap-unknown-trial-charged",
    "ignore-team-cost": "cap-stop-shape-equivalence",
    "charge-every-seat": "cap-scripted-and-idle-seats-free",
    "accept-zero-with-tokens": "cap-zero-with-tokens-needs-price",
}
TOKENS = ("n_input_tokens", "n_cache_tokens", "n_output_tokens")
MODEL = "openrouter/sim/model"
FREE_MODEL = "openrouter/sim/free"
RAW_FREE_MODEL = "sim/raw-free"


@dataclass(frozen=True)
class Trial:
    shape: str
    data: dict[str, Any] | None
    true_micro: int
    recorded_micro: int
    unknown_seats: int = 0
    problem: bool | str = False
    free: bool = False


def _agent(cost: Any) -> dict[str, Any]:
    return {"agent_result": {"cost_usd": cost}}


def _seats(total: int) -> dict[str, Any]:
    return {"seat_results": [_agent(usd(total // 2)), _agent(usd(total - total // 2))]}


def _usage(rng: Random, cost: Any, tokens: int) -> dict[str, Any]:
    assert tokens >= 0
    usage: dict[str, Any] = {name: rng.choice((None, 0)) for name in TOKENS}
    if tokens:
        usage[rng.choice(TOKENS)] = tokens
    return usage | {"cost_usd": cost, "model_usage": None, "rollout_details": None, "metadata": None}


def _info(agent: str, model: bool) -> dict[str, Any]:
    return {"name": agent, "version": "2.0.0", "model_info": {"name": "sim/model", "provider": "sim"} if model else None}


def _seat(name: str, agent: str, model: bool, result: dict[str, Any] | None) -> dict[str, Any]:
    return {"seat": name, "agent_info": _info(agent, model), "agent_result": result,
            "agent_execution": {"ok": True} if result is not None else None, "exception_info": None}


def _participant(name: str, agent: str, model: str | None) -> dict[str, Any]:
    return {"seat": name, "name": agent, "import_path": None, "model_name": model, "kwargs": {}}


def _config(agent: str | None, model: str | None, participants: list[dict[str, Any]] | None) -> dict[str, Any]:
    configured = {"name": agent, "import_path": None, "model_name": model, "kwargs": {}} if agent else None
    return {"config": {"agent": configured, "participants": participants}}


def _team(seats: list[dict[str, Any]], extra: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    assert seats and all(seat["agent_info"]["name"] for seat in seats)
    roster = [_participant(seat["seat"], seat["agent_info"]["name"], MODEL if seat["agent_info"]["model_info"] else None)
              for seat in seats]
    return {"seat_results": seats, "agent_info": None, "agent_result": None} | _config("oracle", None, roster + list(extra))


def _keyed(team: dict[str, Any]) -> dict[str, Any]:
    seats = {seat["seat"]: {key: value for key, value in seat.items() if key != "seat"} for seat in team["seat_results"]}
    assert len(seats) == len(team["seat_results"])
    return team | {"seat_results": seats}


def _solo(agent: str, model: str | None, result: dict[str, Any] | None) -> dict[str, Any]:
    return _config(agent, model, None) | {"agent_info": _info(agent, False), "agent_result": result}


def _trials(rng: Random, reserve: int, expected_seats: int) -> tuple[Trial, ...]:
    known = rng.randint(120_000, 250_000)
    difference = rng.randint(10_001, 40_000)
    modeled = expected_seats + rng.randint(1, 2)
    participants = {"config": {"agent": None,
                               "participants": [_participant(f"p{n + 1}", "terminus-2", MODEL) for n in range(modeled)]}}
    bad = rng.choice(("0.04", True, -1, float("inf")))
    return (
        Trial("seats", _seats(known), known, known),
        Trial("team", _agent(usd(known)), known, known),
        Trial("empty", {}, reserve, 0, expected_seats),
        Trial("configured", participants, expected_seats * reserve, 0, expected_seats),
        Trial("configured-empty-seats", participants | {"seat_results": rng.choice(([], {}))},
              expected_seats * reserve, 0, expected_seats),
        Trial("both", _seats(known) | _agent(usd(known)), known, known),
        Trial("cent", _seats(known) | _agent(usd(known - 10_000)), known, known),
        Trial("larger-team", _seats(known) | _agent(usd(known + difference)),
              known + difference, known + difference, problem=True),
        Trial("unreadable", None, reserve, 0, expected_seats),
        Trial("larger-seats", _seats(known) | _agent(usd(known - difference)), known, known, problem=True),
        Trial("malformed", _agent(bad), reserve, 0, expected_seats),
        Trial("partial-seats", {"seat_results": [_agent(usd(known)), _agent(bad)]},
              known + reserve, known, 1),
        Trial("explicit-zero", _agent(0), 0, 0),
        *_seat_trials(rng, reserve, expected_seats, known),
        *_zero_trials(rng, reserve, expected_seats),
    )


def _seat_trials(rng: Random, reserve: int, expected_seats: int, known: int) -> tuple[Trial, ...]:
    names = [f"p{n + 1}" for n in range(rng.randint(2, 4))]
    scripted = [_seat(name, rng.choice(("oracle", "nop")), False, _usage(rng, None, 0)) for name in names]
    idle = [_seat(name, "terminus-2", True, None) for name in names]
    paid = _seat(names[0], "terminus-2", True, _usage(rng, usd(known), rng.randint(1, 90_000)))
    tokens = _seat(names[0], "claude-code", False, _usage(rng, None, rng.randint(1, 90_000)))
    never_ran = _team(idle) | {"agent_info": _info("terminus-2", True), "exception_info": {
        "exception_type": "RuntimeError", "exception_message": "docker pull: 429 Too Many Requests"}}
    unknown = expected_seats * reserve
    return (
        Trial("scripted", _team(scripted), 0, 0, free=True),
        Trial("never-ran", never_ran, 0, 0, free=True),
        Trial("standin", _team([paid, *scripted[1:]]), known, known),
        Trial("missing-modeled", _team([paid], (_participant("p9", "terminus-2", MODEL),)), known + reserve, known, 1),
        Trial("missing-scripted", _team([paid], (_participant("p9", "oracle", None),)), known, known),
        Trial("missing-modeled-keyed", _keyed(_team([paid, *scripted[1:]], (_participant("p9", "codex", MODEL),))),
              known + reserve, known, 1),
        Trial("missing-scripted-keyed", _keyed(_team([paid, *scripted[1:]], (_participant("p9", "nop", None),))),
              known, known),
        Trial("tokens-no-model", _team([tokens]), reserve, 0, 1),
        Trial("solo-scripted", _solo("oracle", None, _usage(rng, None, 0)), 0, 0, free=True),
        Trial("solo-configured-model", _solo("terminus-2", MODEL, None), unknown, 0, expected_seats),
        Trial("solo-tokens", _solo("claude-code", None, _usage(rng, None, rng.randint(1, 90_000))),
              unknown, 0, expected_seats),
    )


def _zero_team(rng: Random, tokens: int, seats: int) -> dict[str, Any]:
    assert seats > 0
    return _team([_seat(f"p{index + 1}", "terminus-2", True, _usage(rng, 0.0, tokens)) for index in range(seats)])


def _zero_trials(rng: Random, reserve: int, expected_seats: int) -> tuple[Trial, ...]:
    tokens = rng.randint(1, 90_000)
    free = _seat("p1", "terminus-2", True, _usage(rng, 0.0, tokens))
    free["agent_info"]["model_info"] = {"name": "sim/free", "provider": "openrouter"}
    free_team = _team([free])
    free_team["config"]["participants"][0]["model_name"] = FREE_MODEL
    raw = _seat("p1", "terminus-2", True, free["agent_result"])
    raw["agent_info"]["model_info"] = {"name": RAW_FREE_MODEL, "provider": "openrouter"}
    raw_free = _team([raw])
    raw_free["config"]["participants"][0]["model_name"] = f"openrouter/{RAW_FREE_MODEL}"
    nested = _usage(rng, 0.0, 0) | {"model_usage": {MODEL: {"n_cache_tokens": tokens}}}
    free_nested = _usage(rng, 0.0, 0) | {"model_usage": {FREE_MODEL: {"n_output_tokens": tokens}}}
    mixed = _usage(rng, 0.0, tokens + 1) | {
        "model_usage": {FREE_MODEL: {"n_input_tokens": 1}, MODEL: {"n_output_tokens": tokens}},
    }
    seat_problem = f"cost 0.0 with {tokens} tokens: no price for sim/model?"
    team_problem = f"cost 0.0 with {tokens} tokens: no price for {MODEL}?"
    return (
        Trial("zero-seat-tokens", _zero_team(rng, tokens, 1), reserve, 0, 1, seat_problem),
        Trial("zero-seat-idle", _zero_team(rng, 0, 1), 0, 0, free=True),
        Trial("zero-seat-scripted", _team([_seat("p1", "oracle", False, _usage(rng, 0.0, 0))]), 0, 0, free=True),
        Trial("zero-seat-free", _keyed(free_team), 0, 0, free=True),
        Trial("zero-seat-free-exact-observed", raw_free, 0, 0, free=True),
        Trial("zero-solo-tokens", _solo("terminus-2", MODEL, nested), expected_seats * reserve, 0,
              expected_seats, team_problem),
        Trial("zero-solo-free", _solo("terminus-2", FREE_MODEL, _usage(rng, 0.0, tokens)), 0, 0, free=True),
        Trial("zero-solo-free-usage", _solo("terminus-2", None, free_nested), 0, 0, free=True),
        Trial("zero-solo-unnamed", _solo("terminus-2", None, _usage(rng, 0.0, tokens)),
              expected_seats * reserve, 0, expected_seats,
              f"cost 0.0 with {tokens} tokens: no price for an unnamed model?"),
        Trial("zero-solo-wrong-case", _solo("terminus-2", FREE_MODEL.upper(), _usage(rng, 0.0, tokens)),
              expected_seats * reserve, 0, expected_seats,
              f"cost 0.0 with {tokens} tokens: no price for {FREE_MODEL.upper()}?"),
        Trial("zero-seat-mixed-models", _team([free | {"agent_result": mixed}]), reserve, 0, 1,
              f"cost 0.0 with {tokens + 1} tokens: no price for {MODEL}?"),
    )


def _write(folder: Path, data: dict[str, Any] | None) -> None:
    folder.mkdir(parents=True)
    payload = {"trial_name": folder.name, **data} if data is not None else None
    (folder / "result.json").write_text(json.dumps(payload), encoding="utf-8")


def _cost_shapes(run: Run, plan: Plan, rng: Random, reserve: int) -> None:
    run.step = "cost/result-shapes"
    trials = _trials(rng, reserve, plan.expected_seats)
    folders = []
    for trial in trials:
        folder = plan.output_root / f"{trial.shape}__1"
        folders.append(folder)
        _write(folder, trial.data)
        cost = evidence.scan_cost([folder], expected_seats=plan.expected_seats, free_models=plan.free_models)
        charged = cost.charged(plan.unknown_seat_usd)
        run.check(charged >= usd(trial.true_micro), "cap-charge-covers-fixture-cost", trial.shape)
        run.check(cost.recorded_usd == usd(trial.recorded_micro), "cap-recorded-cost", trial.shape)
        expected = usd(trial.recorded_micro + trial.unknown_seats * reserve)
        run.check(cost.unknown_trials == bool(trial.unknown_seats) and cost.seats_without_cost == trial.unknown_seats
                  and charged == expected, "cap-unknown-trial-charged", trial.shape)
        recorded = None if trial.unknown_seats and trial.recorded_micro == 0 else usd(trial.recorded_micro)
        run.check(cost.record()["recorded_usd"] == recorded, "cap-settle-does-not-record-false-zero", trial.shape)
        run.check(len(cost.problems) == bool(trial.problem), "cap-conflicts-reported", trial.shape)
        if isinstance(trial.problem, str):
            run.check(cost.seats_without_cost == trial.unknown_seats and cost.record()["recorded_usd"] is None,
                      "cap-zero-with-tokens-needs-price", trial.shape)
            run.check(cost.problems == (f"{folder.resolve() / 'result.json'}: {trial.problem}",),
                      "cap-unpriced-model-shown", trial.shape)
        run.check(not trial.free or (charged == 0 and not cost.unknown_trials and not cost.seats_without_cost),
                  "cap-scripted-and-idle-seats-free", trial.shape)
    run.check(evidence.scan_cost([folders[2]]).charged(plan.unknown_seat_usd) == usd(reserve),
              "cap-unknown-minimum-one-seat", "no count in the result or plan")
    status = report.status(plan)
    cap = status["cap"]
    run.check(cap["charged_usd"] >= usd(sum(trial.true_micro for trial in trials)),
              "cap-charge-covers-fixture-cost", "all trials")
    unknown = sum(bool(trial.unknown_seats) for trial in trials)
    text = report.status_text(status)
    run.check(cap["unknown_trials"] == unknown and f"{unknown} unknown trials" in text,
              "cap-status-shows-unknown", "JSON and text")
    wanted = {str(folder.resolve() / "result.json")
              for folder, trial in zip(folders, trials, strict=True) if trial.problem}
    found = {entry.split(": ", 1)[0] for entry in cap["cost_problems"]}
    run.check(found == wanted and text.count("cost problem:") == len(wanted),
              "cap-status-lists-conflicts", "JSON and text")


def _stop_trial(
    plan: Plan, case: Scenario, amounts: tuple[int, ...], result: Callable[[int], dict[str, Any]],
) -> tuple[int, str]:
    world = World(case, plan.output_root, Random(case.seed))
    runner = Runner(plan, world, SHELL_PID, lambda _: None, [plan.lanes[0]], expand_first_run)
    for index, amount in enumerate(amounts, 1):
        world.t += 1
        _write(plan.output_root / f"trial__{index}", result(amount))
        try:
            runner._gate_cost()
        except Stop as stop:
            return index, stop.reason
    return 0, ""


def _cap_stop(run: Run, plan: Plan, case: Scenario, rng: Random, root: Path) -> None:
    run.step = "cost/cap-stop"
    fallback = round(plan.unknown_seat_usd * plan.expected_seats * 1_000_000)
    amounts = tuple(fallback + rng.randint(50_000, 200_000) for _ in range(4))
    crossing = rng.randint(2, len(amounts))
    stop_plan = replace(plan, cap_usd=usd(sum(amounts[:crossing])))
    stops = []
    for label, make in (("team", lambda amount: _agent(usd(amount))), ("seat", _seats)):
        folder = root / f"{label}-sequence"
        stops.append(_stop_trial(replace(stop_plan, output_root=folder, cap_roots=(folder,)), case, amounts, make))
    run.check(stops == [(crossing, "cap")] * 2, "cap-stop-shape-equivalence",
              f"seat and team stop at {stops}; expected trial {crossing}")


def _zero_cap_stop(run: Run, plan: Plan, case: Scenario, rng: Random, root: Path, reserve: int) -> None:
    run.step = "cost/zero-cap-stop"
    seats = rng.randint(2, 4)
    crossing = rng.randint(2, 5)
    per_trial = seats * reserve
    cap = (crossing - 1) * per_trial + rng.randint(1, per_trial)
    counts = tuple(rng.randint(1, 90_000) for _ in range(crossing + 1))
    for name, free_models, wanted in (("unpriced", plan.free_models, (crossing, "cap")),
                                      ("free", frozenset({"sim/model"}), (0, ""))):
        folder = root / name
        lane_plan = replace(plan, cap_usd=usd(cap), free_models=free_models, output_root=folder, cap_roots=(folder,))
        stop = _stop_trial(lane_plan, case, counts, lambda tokens: _zero_team(rng, tokens, seats))
        run.check(stop == wanted, "cap-stop-zero-with-tokens", f"{name}: stopped at {stop}, expected {wanted}")


def _check_free_models(run: Run, case: Scenario, root: Path, plan: Plan) -> None:
    run.step = "cost/free-model-plan"
    lines, _ = report.check(plan, BASE, (None, "simulation"))
    run.check(f'money.free_models: ["{FREE_MODEL}", "{RAW_FREE_MODEL}"]' in lines,
              "cap-check-shows-free-models", FREE_MODEL)
    data = plan_data(case, root)
    plan_path = root / "plan.toml"
    default = parse(toml_text(data).encode(), plan_path)
    run.check(not default.free_models, "cap-free-models-default-empty", "omitted free_models")
    full = [f"provider/model-{i}" for i in range(32)]
    data["money"]["free_models"] = full
    maximum = parse(toml_text(data).encode(), plan_path)
    run.check(maximum.free_models == frozenset(full), "cap-free-models-maximum", "32 exact names")
    for bad in (FREE_MODEL, [0], [""], [" padded"], [FREE_MODEL, FREE_MODEL], [*full, "one-more"]):
        data["money"]["free_models"] = bad
        try:
            parse(toml_text(data).encode(), plan_path)
        except PlanError as error:
            run.check("money.free_models" in str(error), "cap-free-models-schema", repr(bad))
        else:
            run.check(False, "cap-free-models-schema", repr(bad))


@contextmanager
def _inject(mutation: str | None) -> Iterator[None]:
    if mutation is None:
        yield
        return
    if mutation == "no-seat-list-zero":
        result_cost = evidence._result_cost

        def zero(
            path: Path, totals: Counter[str], problems: list[str], expected_seats: int, free_models: frozenset[str],
        ) -> tuple[float, bool]:
            result = evidence.read_json(path)
            if isinstance(result, dict) and "trial_name" in result and not result.get("seat_results"):
                totals["trials"] += 1
                return 0.0, True
            return result_cost(path, totals, problems, expected_seats, free_models)

        with patch.object(evidence, "_result_cost", zero):
            yield
        return
    if mutation == "charge-every-seat":
        with patch.object(evidence, "_seat_shows_model_use", lambda seat: True):
            yield
        return
    if mutation == "accept-zero-with-tokens":
        with patch.object(evidence, "_zero_cost_problem", return_value=None):
            yield
        return
    assert mutation == "ignore-team-cost"
    agent_cost = evidence._agent_cost

    def seat_only(result: Any) -> float | None:
        return None if isinstance(result, dict) and "trial_name" in result else agent_cost(result)

    with patch.object(evidence, "_agent_cost", seat_only):
        yield


def replay(run: Run, seed: int, root: Path, mutation: str | None = None) -> None:
    rng = Random(seed ^ 0xCA9)
    case = scenario(seed)
    data = plan_data(case, root)
    reserve = rng.randint(10_000, 30_000)
    data["money"].update(cap_roots=[data["output_root"]], expected_seats=rng.randint(2, 4),
                         unknown_seat_usd=usd(reserve), free_models=[FREE_MODEL, RAW_FREE_MODEL])
    plan = parse(toml_text(data).encode(), root / "plan.toml")
    _check_free_models(run, case, root, plan)
    with _inject(mutation):
        _cost_shapes(run, plan, rng, reserve)
        _cap_stop(run, plan, case, rng, root)
        _zero_cap_stop(run, plan, case, rng, root, reserve)
