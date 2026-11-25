from __future__ import annotations

import json
import math
import os
from collections import deque
from collections.abc import Generator, Iterable
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
from itertools import islice
from pathlib import Path
from statistics import fmean
from typing import Any

from muster.lab import api, episode, search
from muster.lab.load import LabError, check_adapter
from muster.lab.schedule import Scripted, parse

TOLERANCE = 1e-9
MAX_SCHEDULES = 10_000
MAX_JOBS = 16
_NOT_CHECKABLE = (
    "Instruction fairness (human review or blind implementer)",
    "Actual difficulty and construct attribution",
    "Discrimination between models",
    "Reliability across attempts and scaffolds",
    "Team benefit at equal aggregate cost",
    "Statistical power and contamination",
    "Run loss and provider liveness",
    "Live harness fidelity and container isolation",
)
_BUILTINS = (
    "determinism", "replay", "state_matches_log", "no_world_error", "no_hidden_string",
    "policies_end", "log_continuous", "reward_range", "oracle_ceiling",
    "credited_events_matter", "refused_events_do_not_matter", "floor", "findings_regress",
)
_ROOT = Path(__file__).resolve().parents[2]


def _witness(result: api.Episode | None, observed: Any, **extra: Any) -> dict:
    return {
        "condition": result.condition if result else None,
        "instance": result.instance if result else None,
        "lineup": result.lineup if result else {},
        "order": list(result.order) if result else [],
        "observed": observed, **extra,
    }


@dataclass
class Check:
    id: str
    status: str = "PASS"
    episodes: int = 0
    witness: dict | None = None
    note: str | None = None

    def test(self, valid: bool, result: api.Episode | None, observed: Any, **extra: Any) -> None:
        if not valid and self.status != "FAIL":
            self.status = "FAIL"
            self.witness = _witness(result, observed, **extra)

    def skip(self, reason: str) -> None:
        if self.status != "FAIL":
            self.status = "NOT_CHECKED"
            self.note = reason

    def record(self) -> dict:
        row: dict = {"id": self.id, "status": self.status, "episodes": self.episodes}
        if self.witness is not None:
            row["witness"] = self.witness
        if self.note is not None:
            row["note"] = self.note
        return row


@dataclass(frozen=True)
class Predicate:
    check: Check
    mode: str
    measure: str
    value: float

    def holds(self, result: api.Episode) -> bool:
        if self.mode == "no_error_status":
            return all(event["status"] != self.value for event in result.events)
        value = result.rewards.get(self.measure)
        if value is None or not _finite(value):
            return False
        if self.mode == "equals":
            return abs(value - self.value) <= TOLERANCE
        if self.mode == "at_most":
            return value <= self.value
        assert self.mode == "below"
        return value < self.value

    def evaluate(self, results: list[api.Episode]) -> None:
        assert results
        self.check.episodes += len(results)
        if self.mode == "below":
            if any(self.holds(result) for result in results):
                return
            failed = min(results, key=self.rank)
        else:
            failed = next((result for result in results if not self.holds(result)), None)
            if failed is None:
                return
        observed = (self.value if self.mode == "no_error_status"
                    else failed.rewards.get(self.measure))
        self.check.test(False, failed, _safe_number(observed), measure=self.measure,
                        expected=self.value, comparison=self.mode)

    def rank(self, result: api.Episode) -> float:
        value = result.rewards.get(self.measure)
        return value if value is not None and _finite(value) else math.inf


def _finite(value: Any) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _safe_number(value: Any) -> Any:
    return str(value) if isinstance(value, float) and not math.isfinite(value) else value


def _names(value: Any) -> list[str]:
    assert isinstance(value, list) and value, "expected a nonempty list"
    assert all(isinstance(name, str) and name for name in value), "expected names"
    assert len(set(value)) == len(value), "duplicate names"
    return value


def _number(value: Any) -> float:
    assert _finite(value), "expected finite number"
    return float(value)


def _pairs(claim: dict) -> list[tuple[str, str, str, str, float]]:
    mode = claim["type"]
    if mode in ("equals", "at_most", "below", "no_error_status"):
        measure = claim["measure"] if mode != "no_error_status" else "status"
        value = _number(claim["value"] if mode != "no_error_status" else claim["status"])
        return [(c, claim["policy"], mode, measure, value) for c in _names(claim["conditions"])]
    if mode == "ladder":
        policies, conditions = _names(claim["policies"]), _names(claim["conditions"])
        assert len(policies) == len(conditions), "ladder lengths differ"
        return [(c, p, "equals" if i >= j else "below", claim["measure"], 1.0)
                for i, p in enumerate(policies) for j, c in enumerate(conditions)]
    if mode == "skills":
        policies, conditions = claim["policies"], claim["conditions"]
        assert policies and conditions, "empty skills matrix"
        for skills in (*policies.values(), *conditions.values()):
            assert isinstance(skills, list) and all(isinstance(s, str) for s in skills)
        return [(c, p, "equals" if set(p_skills) >= set(c_skills) else "below",
                 claim["measure"], 1.0) for p, p_skills in policies.items()
                for c, c_skills in conditions.items()]
    raise ValueError(f"unknown type: {mode}")


def _budget(adapter: Any, claim: dict, check: Check, instances: dict[str, list[dict]]) -> None:
    groups = claim.get("groups", [claim.get("conditions")])
    assert isinstance(groups, list) and groups, "empty budget groups"
    if not hasattr(adapter, "budget"):
        check.skip("adapter has no budget hook")
        return
    for group in groups:
        names = _names(group)
        assert all(name in instances for name in names), "unknown budget condition"
        counts = {name: len(instances[name]) for name in names}
        if len(set(counts.values())) != 1:
            check.test(False, None, counts, condition=names[0], reason="instance counts differ")
            continue
        for index in range(counts[names[0]]):
            budgets = {name: adapter.budget(instances[name][index]) for name in names}
            assert all(_finite(value) for value in budgets.values()), "expected finite budget"
            check.test(len(set(budgets.values())) == 1, None, budgets,
                       condition=names[0], instance=instances[names[0]][index]["id"])


def _claims(adapter: Any, instances: dict[str, list[dict]], policies: list[str]) -> tuple:
    checks: list[Check] = []
    predicates: dict[tuple[str, str], list[Predicate]] = {
        (c, p): [] for c in instances for p in policies
    }
    ids: set[str] = set()
    for index, claim in enumerate(adapter.claims()):
        check = Check(str(claim.get("id", f"claim.{index + 1}")))
        checks.append(check)
        try:
            assert check.id not in ids and not check.id.startswith("builtin."), "duplicate claim id"
            ids.add(check.id)
            if claim["type"] == "equal_budget":
                _budget(adapter, claim, check, instances)
                continue
            pairs = _pairs(claim)
            assert pairs and all((c, p) in predicates for c, p, _, _, _ in pairs), (
                "unknown condition or policy"
            )
            for c, p, mode, measure, value in pairs:
                predicates[c, p].append(Predicate(check, mode, measure, value))
        except (AssertionError, KeyError, TypeError, ValueError, AttributeError) as error:
            check.test(False, None, str(error) or "invalid claim")
    return checks, predicates


def schedule_suite(seats: int, schedules: int, seed: int) -> list[str]:
    assert seats >= 1 and 0 <= schedules <= MAX_SCHEDULES
    if seats == 1:
        return ["round"]
    return ["round", *(f"bursty:{seed + i}:8" if i % 3 == 2 else f"seed:{seed + i}"
                       for i in range(schedules))]


def hidden_event(adapter: Any, instance: dict, result: api.Episode) -> dict | None:
    if not hasattr(adapter, "hidden"):
        return None
    strings = {seat: adapter.hidden(instance, seat) for seat in result.lineup}
    for event in result.events:
        if event["seat"] not in strings:
            continue
        text = json.dumps(event["response"], ensure_ascii=False, sort_keys=True)
        for secret in strings[event["seat"]]:
            assert isinstance(secret, str), "hidden hook must return strings"
            if json.dumps(secret, ensure_ascii=False)[1:-1] in text:
                return event
    return None


def _renumber(events: tuple[dict, ...], exclude: set[int]) -> list[dict]:
    return [dict(event, seq=seq) for seq, event in enumerate(
        (event for i, event in enumerate(events) if i not in exclude), 1
    )]


def _rescored(adapter: Any, instance: dict, result: api.Episode, excluded: set[int]) -> Any:
    try:
        return _number(adapter.score(_renumber(result.events, excluded), instance)["reward"])
    except (AssertionError, KeyError, TypeError, ValueError, IndexError) as error:
        return f"score error: {error}"


def _credit_checks(adapter: Any, instance: dict, result: api.Episode, checks: dict[str, Check],
                    policy: str) -> None:
    credit = checks["credited_events_matter"]
    refused = checks["refused_events_do_not_matter"]
    if policy == "oracle" and result.rewards["reward"] == 1.0:
        checked = 0
        free = getattr(adapter, "CARD_FREE_KINDS", ())
        irrelevant = []
        for i, event in enumerate(result.events):
            if (event["seat"] not in result.lineup or not event["ok"]
                    or event["method"] != "POST" or event["kind"] in ("msg", *free)):
                continue
            checked += 1
            reward = _rescored(adapter, instance, result, {i})
            if not isinstance(reward, float) or reward >= 1.0:
                irrelevant.append({"seq": event["seq"], "kind": event["kind"], "reward": reward})
        credit.episodes += int(checked > 0)
        credit.test(not irrelevant, result, irrelevant)
    if any(event["seat"] in result.lineup and not event["ok"] for event in result.events):
        refused.episodes += 1
        rejected = {i for i, event in enumerate(result.events) if not event["ok"]}
        reward = _rescored(adapter, instance, result, rejected)
        refused.test(reward == result.rewards["reward"], result, reward,
                     original=result.rewards["reward"])


def _episode_checks(adapter: Any, instance: dict, result: api.Episode, checks: dict[str, Check],
                    policy: str) -> None:
    events = result.events

    def test(name: str, valid: bool, observed: Any) -> None:
        check = checks[name]
        check.episodes += 1
        check.test(valid, result, observed)

    if checks["state_matches_log"].status != "NOT_CHECKED":
        test("state_matches_log", result.state == result.replayed,
             {"state": result.state, "replayed": result.replayed})
    error = next((event for event in events if event["status"] == 500), None)
    test("no_world_error", error is None, {"seq": error["seq"], "status": 500} if error else None)
    if checks["no_hidden_string"].status != "NOT_CHECKED":
        empty = any("" in adapter.hidden(instance, seat) for seat in result.lineup)
        leak = hidden_event(adapter, instance, result)
        test("no_hidden_string", not empty and leak is None,
             "empty hidden string" if empty else leak["seq"] if leak else None)
    test("policies_end", result.ended == "all_done", result.ended)
    continuous = bool(events) and all(event["seq"] == i for i, event in enumerate(events, 1))
    close = bool(events) and events[-1]["kind"] == "admin.close" and events[-1]["ok"]
    close = close and events[-1]["seat"] is None and events[-1]["status"] == 200
    close = close and events[-1]["method"] == "POST"
    test("log_continuous", continuous and close, {"continuous": continuous, "close": close})
    reward = result.rewards["reward"]
    finite = all(_finite(value) for value in result.rewards.values())
    test("reward_range", 0.0 <= reward <= 1.0 and finite,
         {key: _safe_number(value) for key, value in result.rewards.items()})
    if policy == "oracle":
        test("oracle_ceiling", reward == 1.0, reward)
    if policy == "idle":
        test("floor", reward < 1.0, reward)
    _credit_checks(adapter, instance, result, checks, policy)


def _same_run(a: api.Episode, b: api.Episode) -> bool:
    return (a.events, a.rewards, a.order, a.ended) == (b.events, b.rewards, b.order, b.ended)


@dataclass(frozen=True)
class Played:
    results: list[api.Episode]
    repeated: api.Episode
    replays: list[api.Episode]


@dataclass(frozen=True)
class Cell:
    policy: str
    condition: str
    instances: list[dict]
    below: tuple[tuple[str, float], ...]
    schedules: int
    worst: int
    seed: int


def _play_instance(adapter: Any, instance: dict, cell: Cell) -> Played:
    lineup = dict.fromkeys(adapter.seats(instance), cell.policy)
    texts = schedule_suite(len(lineup), cell.schedules, cell.seed)
    results = [episode.run(adapter, instance, lineup, parse(text)) for text in texts]
    repeated = episode.run(adapter, instance, lineup, parse(texts[0]))
    if len(lineup) > 1 and cell.worst:
        measures = {measure for measure, threshold in cell.below
                    if not any(_finite(result.rewards.get(measure))
                               and result.rewards[measure] < threshold for result in results)}
        for measure in sorted(measures):
            if all(_finite(result.rewards.get(measure)) for result in results):
                results.append(search.worst(adapter, instance, lineup, measure=measure,
                                            tries=cell.worst, seed=cell.seed))
    replays = [episode.run(adapter, instance, lineup, Scripted(result.order))
               for result in results]
    return Played(results, repeated, replays)


def _judge_instance(adapter: Any, instance: dict, policy: str, predicates: list[Predicate],
                    checks: dict[str, Check], played: Played) -> list[float]:
    first = played.results[0]
    check = checks["determinism"]
    check.episodes += 1
    check.test(_same_run(first, played.repeated), first, "repeat differs")
    for result, replayed in zip(played.results, played.replays, strict=True):
        _episode_checks(adapter, instance, result, checks, policy)
        check = checks["replay"]
        check.episodes += 1
        check.test((result.events, result.rewards) == (replayed.events, replayed.rewards),
                   result, "scripted replay differs")
    for predicate in predicates:
        predicate.evaluate(played.results)
    return [result.rewards["reward"] for result in played.results]


def _play_cell(family: str, cell: Cell) -> list[Played]:
    from muster.lab.finding import adapter_for

    try:
        adapter = adapter_for(family)
        return [_play_instance(adapter, instance, cell) for instance in cell.instances]
    except Exception as error:
        raise LabError(str(error) or "invalid card input") from None


def _parallel_cells(family: str, cells: list[Cell], jobs: int) -> Generator[list[Played], None, None]:
    assert 1 < jobs <= MAX_JOBS
    play = partial(_play_cell, family)
    remaining = iter(cells)
    with ProcessPoolExecutor(max_workers=min(jobs, len(cells))) as executor:
        pending: deque[Future[list[Played]]] = deque(
            executor.submit(play, cell) for cell in islice(remaining, jobs)
        )
        for cell in remaining:
            result = pending.popleft().result()
            pending.append(executor.submit(play, cell))
            yield result
        for future in pending:
            yield future.result()


def _judge_cell(adapter: Any, cell: Cell, predicates: list[Predicate],
                checks: dict[str, Check], played: Iterable[Played]) -> dict:
    values = []
    for instance, result in zip(cell.instances, played, strict=True):
        values.extend(_judge_instance(adapter, instance, cell.policy, predicates, checks, result))
    assert values
    return {"mean": fmean(values), "min": min(values), "max": max(values),
            "episodes": len(values)}


def _matrix(adapter: Any, cells: list[Cell], predicates: dict, checks: dict[str, Check],
            family: str | None, jobs: int) -> dict:
    matrix: dict[str, dict] = {}
    if jobs == 1:
        for cell in cells:
            played = (_play_instance(adapter, instance, cell) for instance in cell.instances)
            matrix.setdefault(cell.policy, {})[cell.condition] = _judge_cell(
                adapter, cell, predicates[cell.condition, cell.policy], checks, played)
    else:
        assert family, "parallel card requires an adapter name"
        with closing(_parallel_cells(family, cells, jobs)) as results:
            for cell, played in zip(cells, results, strict=True):
                matrix.setdefault(cell.policy, {})[cell.condition] = _judge_cell(
                    adapter, cell, predicates[cell.condition, cell.policy], checks, played)
    return matrix


def _instances(adapter: Any, limit: int | None) -> dict[str, list[dict]]:
    result = {}
    for condition in _names(adapter.conditions()):
        instances = adapter.instances(condition)[:limit]
        assert instances, f"no instances in {condition}"
        assert all(i["condition"] == condition for i in instances), "wrong instance condition"
        _names([i["id"] for i in instances])
        result[condition] = instances
    return result


def source_paths(adapter: Any) -> dict[str, Path]:
    paths = [Path(path).resolve() for path in adapter.sources()]
    assert paths, "sources hook returned no paths"
    return {str(path.relative_to(_ROOT) if path.is_relative_to(_ROOT) else path): path
            for path in sorted(set(paths))}


def _sources(adapter: Any) -> dict:
    if not hasattr(adapter, "sources"):
        return {"status": "NOT_CHECKED", "note": "adapter has no sources hook", "files": {}}
    files = {name: sha256(path.read_bytes()).hexdigest()
             for name, path in source_paths(adapter).items()}
    return {"status": "PASS", "files": files}


def _findings(adapter: Any, check: Check) -> None:
    from muster.lab.finding import regress

    if adapter.NAME == "counter" and not hasattr(adapter, "__file__"):
        check.skip("fixture family")
        return
    family = Path(adapter.__file__).parent.name if hasattr(adapter, "__file__") else adapter.NAME
    try:
        results = regress(family)
    except (LabError, AssertionError, OSError, KeyError, TypeError, ValueError) as error:
        check.test(False, None, str(error) or "invalid finding")
        return
    if results is None:
        check.skip("no findings directory")
        return
    check.episodes = len(results)
    for row in results:
        check.test(row["ok"], None, row["verdict"], condition=row["condition"],
                   instance=row["instance"], lineup=row["lineup"], order=row["order"],
                   finding=row["id"], status=row["status"])


def build(adapter: Any, *, schedules: int, worst: int, instances: int | None, seed: int,
          jobs: int = 1, family: str | None = None) -> dict:
    check_adapter(adapter)
    assert 0 <= schedules <= MAX_SCHEDULES and 0 <= worst <= MAX_SCHEDULES
    assert instances is None or instances > 0
    assert 0 <= jobs <= MAX_JOBS, f"jobs must be between 0 and {MAX_JOBS}"
    if jobs == 0:
        jobs = min(os.cpu_count() or 1, MAX_JOBS)
    selected = _instances(adapter, instances)
    policies = _names(list(adapter.policies()))
    assert "oracle" in policies, "missing oracle policy"
    claims, predicates = _claims(adapter, selected, policies)
    checks = {name: Check(f"builtin.{name}") for name in _BUILTINS}
    if not hasattr(adapter, "state") or not hasattr(adapter, "replay"):
        checks["state_matches_log"].skip("missing state or replay hook")
    if not hasattr(adapter, "hidden"):
        checks["no_hidden_string"].skip("missing hidden hook")
    if "idle" not in policies:
        checks["floor"].skip("no idle policy")
    cells = [
        Cell(policy, condition, rows,
             tuple((p.measure, p.value) for p in predicates[condition, policy] if p.mode == "below"),
             schedules, worst, seed)
        for policy in policies for condition, rows in selected.items()
    ]
    matrix = _matrix(adapter, cells, predicates, checks, family, jobs)
    for name in ("credited_events_matter", "refused_events_do_not_matter"):
        if checks[name].episodes == 0:
            checks[name].skip("no applicable episodes")
    _findings(adapter, checks["findings_regress"])
    all_checks = [*claims, *checks.values()]
    counts = {status: sum(c.status == status for c in all_checks)
              for status in ("PASS", "FAIL", "NOT_CHECKED")}
    return {
        "schema_version": 1, "family": adapter.NAME, "conditions": list(selected),
        "policies": policies, "parameters": {"schedules": schedules, "worst": worst,
                                            "instances": instances, "seed": seed},
        "sources": _sources(adapter), "matrix": matrix,
        "claims": [c.record() for c in claims], "builtins": [c.record() for c in checks.values()],
        "summary": {**counts, "episodes": sum(cell["episodes"] for row in matrix.values()
                                             for cell in row.values())},
        "ok": counts["FAIL"] == 0 and all(c.status == "PASS" for c in claims),
        "not_checkable_at_build_time": list(_NOT_CHECKABLE),
    }
