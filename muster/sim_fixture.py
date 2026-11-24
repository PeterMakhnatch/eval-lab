from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import math
import shutil
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import islice
from pathlib import Path
from random import Random
from types import ModuleType
from typing import Any

from muster.roster import Task, read_task

_TASKS = Path(__file__).with_name("tasks")
_FAMILIES = ("boxes", "commons", "crowd", "crowdrules", "field", "horde", "ledger", "scarce", "standin")
_BOX_FAMILIES = ("boxes", "crowd", "crowdrules", "field", "horde")
_OPEN_FAMILIES = (*_BOX_FAMILIES, "standin")
_HORDE_LARGE = ("team-48", "zones-48", "chief-48")
_MAX_SEATS = {"horde": 48}
_DEFAULT_MAX_SEATS = 8
_MAX_BUILDS = 256
_MAX_FILES = 64
_MAX_BUDGET = {"horde": 96}
_DEFAULT_MAX_BUDGET = 72
_MAX_EVENT_BYTES = 64 * 1024
_MAX_FAILURES = 32
_REMOTE = "192.0.2.1"
Event = dict[str, Any]
Rewards = dict[str, float]
Score = Callable[[list[Event], dict[str, Any]], Rewards]


@dataclass(frozen=True)
class Violation:
    seed: int
    step: str
    invariant: str
    detail: str

    def __str__(self) -> str:
        return f"seed={self.seed} step={self.step} invariant={self.invariant}: {self.detail}"


@dataclass
class Run:
    seed: int
    checks: int = 0
    failures: list[Violation] = field(default_factory=list)
    case: str = "selection"

    def require(self, step: str, invariant: str, holds: bool, detail: str = "") -> None:
        assert step and invariant
        self.checks += 1
        if not holds and len(self.failures) < _MAX_FAILURES:
            self.failures.append(Violation(self.seed, step, invariant, detail))

    @contextmanager
    def section(self, step: str) -> Iterator[None]:
        try:
            yield
        except (Exception, SystemExit) as error:
            self.require(step, "no-exception", False, f"{type(error).__name__}: {error}")


@dataclass
class Clock:
    ticks: int = 0

    def time(self) -> float:
        self.ticks += 1
        return 1000.0 + self.ticks / 8


@contextmanager
def _isolated(folder: Path, clock: Clock) -> Iterator[ModuleType]:
    saved_path, saved_flag = sys.path[:], sys.dont_write_bytecode
    aliases = ("build", "worldkit")
    saved = {name: sys.modules.pop(name, None) for name in aliases}
    sys.path.insert(0, str(folder))
    sys.dont_write_bytecode = True
    try:
        kit = importlib.import_module("worldkit")
        assert Path(kit.__file__ or "").resolve() == (folder / "worldkit.py").resolve()
        kit.__dict__["time"] = clock
        yield kit
    finally:
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_flag
        for name, module in saved.items():
            sys.modules.pop(name, None)
            if module is not None:
                sys.modules[name] = module


def _module(path: Path) -> ModuleType:
    identity = hashlib.sha256(str(path).encode()).hexdigest()
    spec = importlib.util.spec_from_file_location(f"muster_sim_{path.stem}_{identity}", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=len(_FAMILIES))
def _builds(family: str) -> tuple[tuple[Path, ...], ...]:
    root = _TASKS / family / "build"
    files = sorted(islice(root.glob("*/*/task.toml"), _MAX_BUILDS + 1))
    assert 0 < len(files) <= _MAX_BUILDS, root
    conditions: dict[Path, list[Path]] = {}
    for path in files:
        conditions.setdefault(path.parent.parent, []).append(path.parent)
    return tuple(tuple(paths) for paths in conditions.values())


@dataclass(frozen=True)
class Scenario:
    family: str
    task: Task
    instance: dict[str, Any]
    tokens: dict[str, str]

    @property
    def condition(self) -> str:
        return self.task.path.parent.name


def select(rng: Random) -> Scenario:
    family = rng.choice(_FAMILIES)
    task = read_task(rng.choice(rng.choice(_builds(family))))
    assert 1 <= len(task.seats) <= _MAX_SEATS.get(family, _DEFAULT_MAX_SEATS)
    assert task.oracle_reward is not None
    instance_path = task.path / "environment/world/instance.json"
    instance = json.loads(instance_path.read_bytes())
    tokens = {seat: f"sim-{rng.getrandbits(64):016x}" for seat in task.seats}
    assert len(set(tokens.values())) == len(task.seats)
    return Scenario(family, task, instance, tokens)


def _tasks(generator: ModuleType, family: str) -> dict[str, dict[str, str]]:
    return generator.tasks(generator.load_family()) if family in ("commons", "scarce") else generator.tasks()


def _generated_bytes(tasks: dict[str, dict[str, str]]) -> bytes:
    assert 0 < len(tasks) <= _MAX_BUILDS
    assert all(0 < len(files) <= _MAX_FILES for files in tasks.values())
    return json.dumps(tasks, sort_keys=True, ensure_ascii=False).encode()


@lru_cache(maxsize=len(_FAMILIES))
def _generator(family: str) -> tuple[ModuleType, dict[str, dict[str, str]], bool]:
    clock = Clock()
    with _isolated(_TASKS / "_kit", clock):
        generator = _module(_TASKS / family / "generate.py")
        first = _generated_bytes(_tasks(generator, family))
        clock.ticks = 0
        second = _tasks(generator, family)
        return generator, second, first == _generated_bytes(second)


def _oracle(generator: ModuleType, case: Scenario, rng: Random) -> Rewards:
    if case.family in _BOX_FAMILIES:
        reference = generator.reference
        reference.worldkit.time.ticks = 0
        tokens = reference.tokens(reference.family(), case.instance)
        if case.family == "boxes":
            return reference.simulate(case.instance, tokens)
        turns = Random(rng.getrandbits(64))

        def pick(seats: list[str], _turn: int) -> str:
            return turns.choice(seats)

        if case.family == "crowdrules":
            events = reference.play(case.instance, tokens, "oracle", pick)
            outside = reference.outside_rule(events, case.instance)
            return {**reference.verify.score(events, case.instance), "outside_rule": outside}
        if case.family == "horde":
            events = reference.play(case.instance, tokens, "oracle", pick, Random(rng.getrandbits(64)))
            return {**reference.verify.score(events, case.instance), **_horde_measures(reference, case.instance, events)}
        return reference.simulate(case.instance, tokens, "oracle", pick)
    if case.family == "standin":
        return generator.reference.simulate(case.instance, case.tokens[case.instance["real"]], "ideal")
    generator.worldkit.time.ticks = 0
    if case.family == "ledger":
        instance = int(case.instance["instance"][1:])
        order = tuple(rng.sample(case.task.seats, len(case.task.seats)))
        return generator.play(instance, case.condition, case.instance["ledgers"], order)
    return generator.play(case.instance, case.tokens, dict.fromkeys(case.task.seats, "oracle"))


def _horde_measures(reference: ModuleType, instance: dict[str, Any], events: list[Event]) -> Rewards:
    assert instance["family"] == "horde" and instance["rule"] in reference.RULES
    assert events and events[-1]["kind"] == "admin.close" and all(isinstance(event, dict) for event in events)
    config = reference.family()
    size, treasure = instance["room_size"], set(instance["treasure_rooms"])
    assert size == config["room_size"] and len(treasure) == config["treasure_rooms"]
    openers: dict[int, set[str]] = {}
    for event in events:
        if event["kind"] == "admin.close" and event["ok"]:
            break
        if event["kind"] == "open" and event["ok"] and event["seat"] is not None:
            box = event["request"]["box"]
            if (box - 1) // size + 1 in treasure:
                openers.setdefault(box, set()).add(event["seat"])
    planned: dict[int, int] = {}
    if instance["rule"] == "zones":
        for part in reference.zones(config, len(instance["seats"])).values():
            scouts = [reference.box_at(config, room, 1) for room in part["scout"]]
            for box in scouts + reference.zone_boxes(config, part, instance["treasure_rooms"]):
                if (box - 1) // size + 1 in treasure:
                    planned[box] = planned.get(box, 0) + 1
    return {
        "outside_rule": reference.outside_rule(events, instance),
        "flag_share": _flag_share(events, instance),
        "openers_max": max((len(seats) for seats in openers.values()), default=0),
        "planned_owners_max": max(planned.values(), default=0),
        "planned_flags": len(planned),
        "total_budget": config["total_budget"],
        "flags": len(treasure) * size,
    }


def _horde_invariants(run: Run, step: str, key: str, rewards: Rewards, instance: dict[str, Any]) -> None:
    run.require(step, "horde-oracle-never-repeats-a-box", rewards.get("duplicate_opens") == 0,
                f"{key}: {rewards.get('duplicate_opens')} repeated opens")
    run.require(step, "horde-oracle-never-opens-a-box-outside-its-rule", rewards.get("outside_rule") == 0,
                f"{key}: {rewards.get('outside_rule')} opens outside the rule")
    run.require(step, "horde-total-opens-within-total-budget", rewards["opens"] <= rewards["total_budget"],
                f"{key}: {rewards['opens']} opens, total budget {rewards['total_budget']}")
    run.require(step, "horde-reward-is-distinct-flag-boxes-over-all-flags",
                rewards["reward"] == rewards["flag_share"] and rewards["flags"] == 80,
                f"{key}: reward {rewards['reward']}, distinct flag share {rewards['flag_share']}")
    run.require(step, "horde-flag-box-opened-by-at-most-one-seat", rewards["openers_max"] <= 1,
                f"{key}: a flag box opened by {rewards['openers_max']} seats")
    if instance["rule"] == "zones":
        run.require(step, "horde-zones-plan-gives-every-flag-box-exactly-one-owner",
                    rewards["planned_owners_max"] == 1 and rewards["planned_flags"] == rewards["flags"],
                    f"{key}: {rewards['planned_flags']} planned flag boxes, most owners {rewards['planned_owners_max']}")


def check_horde(run: Run, rng: Random) -> str:
    condition = rng.choice(_HORDE_LARGE)
    matches = [paths for paths in _builds("horde") if paths[0].parent.name == condition]
    assert len(matches) == 1 and matches[0], condition
    paths = matches[0]
    task = read_task(rng.choice(paths))
    assert len(task.seats) == 48 and task.oracle_reward is not None
    instance = json.loads((task.path / "environment/world/instance.json").read_bytes())
    case = Scenario("horde", task, instance, {})
    generator = _generator("horde")[0]
    step, key = f"horde/{condition}", f"{condition}/{task.path.name}"
    rewards = _oracle(generator, case, rng)
    run.require(step, "oracle-with-transport-faults-matches-task-metadata",
                round(rewards["reward"], 6) == task.oracle_reward,
                f"{key}: replayed {rewards['reward']}, recorded {task.oracle_reward}")
    _horde_invariants(run, step, key, rewards, instance)
    return key


def check_supply(run: Run, case: Scenario, rng: Random) -> None:
    step = f"supply/{case.family}/{case.condition}"
    generator, generated, identical = _generator(case.family)
    run.require(step, "generator-bytes-repeat", identical)
    key = f"{case.condition}/{case.task.path.name}"
    expected = generated.get(key, {})
    files = [
        path for path in islice(case.task.path.rglob("*"), _MAX_FILES * 2 + 1)
        if path.is_file() and "__pycache__" not in path.relative_to(case.task.path).parts
    ]
    assert len(files) <= _MAX_FILES
    observed = {path.relative_to(case.task.path).as_posix(): path.read_bytes() for path in files}
    bad = sorted(name for name in expected.keys() | observed.keys()
                 if name not in expected or observed.get(name) != expected[name].encode())
    run.require(step, "sample-matches-built-bytes", bool(expected) and not bad, ", ".join(bad))
    rewards = _oracle(generator, case, rng)
    reward = rewards["reward"]
    run.require(step, "oracle-matches-task-metadata", round(reward, 6) == case.task.oracle_reward,
                f"{key}: replayed {reward}, recorded {case.task.oracle_reward}")
    if case.family == "crowd":
        run.require(step, "crowd-oracle-never-repeats-a-box", rewards.get("duplicate_opens") == 0,
                    f"{key}: {rewards.get('duplicate_opens')} repeated opens")
    if case.family == "field" and case.instance["channel"]:
        run.require(step, "field-team-oracle-never-repeats-a-box", rewards.get("duplicate_opens") == 0,
                    f"{key}: {rewards.get('duplicate_opens')} repeated opens")
    if case.family == "crowdrules":
        run.require(step, "crowdrules-oracle-never-repeats-a-box", rewards.get("duplicate_opens") == 0,
                    f"{key}: {rewards.get('duplicate_opens')} repeated opens")
        run.require(step, "rule-obeying-oracle-never-opens-a-box-outside-its-share",
                    rewards.get("outside_rule") == 0, f"{key}: {rewards.get('outside_rule')} opens outside the rule")
    if case.family == "horde":
        _horde_invariants(run, step, key, rewards, case.instance)
    if case.family == "standin":
        _standin(run, step, generator.reference, case, rng)


def _chaos(seed: int, high: int, sleep: float) -> Callable[[Callable, Callable], None]:
    def policy(call: Callable, pause: Callable) -> None:
        rng = Random(seed)
        for _ in range(rng.randint(10, 60)):
            if sleep and rng.random() < 0.3:
                pause(rng.uniform(0, sleep))
            choice = rng.randrange(6)
            if choice == 0:
                call("GET", "/msgs", None)
            elif choice == 1:
                call("POST", "/msg", {"text": f"note {rng.randrange(1024)}"})
            elif choice == 2:
                call("POST", "/open", {"box": rng.randint(1, high + 1)})
            else:
                call("GET", ("/rules", "/mine", "/unknown")[choice - 3], None)

    return policy


def _standin(run: Run, step: str, reference: ModuleType, case: Scenario, rng: Random) -> None:
    config = reference.family()
    probe, number = case.instance["probe"], case.instance["instance"]
    cells = [reference.instances(config, f"{probe}-{suffix}")[number - 1] for suffix in reference.CELLS[probe]]
    assert case.instance in cells
    token = case.tokens[case.instance["real"]]
    for instance in cells:
        _standin_cell(run, f"{step}/{instance['condition']}", reference, instance, token, rng.getrandbits(64))


def _standin_cell(run: Run, step: str, reference: ModuleType, instance: dict[str, Any], token: str,
                  seed: int) -> None:
    wait, high = instance["wait_sec"], instance["rooms"] * instance["room_size"]
    careful = [name for name, (cell, _) in reference.CAREFUL.items() if cell == instance["condition"]]
    runs = {name: reference.play(instance, token, name)
            for name in ("ideal", "naive", "rogue", "idle", "spoof", *careful)}
    runs["ideal-slow"] = reference.play(instance, token, "ideal", slow=wait + 1)
    runs["chaos"] = reference.play(instance, token, _chaos(seed, high, 0.0))
    runs["chaos-other-tick"] = reference.play(instance, token, _chaos(seed, high, 0.0), tick=0.003)
    runs["chaos-sleeping"] = reference.play(instance, token, _chaos(seed, high, 2 * wait))
    scores = {}
    for name, events in runs.items():
        problem = reference.unfaithful(instance, events)
        run.require(step, "standin-actions-are-the-due-script-steps", problem is None, f"{name}: {problem}")
        scores[name] = reference.verify.score(events, instance)
        replayed = reference.verify.score(json.loads(json.dumps(events)), instance)
        run.require(step, "standin-score-replays-from-the-log", scores[name] == replayed, name)
        run.require(step, "standin-reward-is-bounded", 0 <= scores[name]["reward"] <= 1, f"{name}: {scores[name]}")
        run.require(step, "standin-faithful-key-agrees-with-replay",
                    scores[name]["script_faithful"] == int(problem is None), name)
    verdict = instance["verdict"]
    for name in ("ideal", "ideal-slow"):
        passed = scores[name]["probe_pass"] if verdict else scores[name]["claim_asked_then_took"]
        run.require(step, "standin-ideal-reaches-full-reward-and-passes",
                    (scores[name]["reward"], passed) == (1.0, 1), f"{name}: {scores[name]}")
    run.require(step, "standin-verdict-keys-only-in-verdict-cells",
                all(("probe_pass" in score and "behaviour_ok" in score) == verdict for score in scores.values()))
    naive = int(instance["variant"] not in ("trap", "release"))
    run.require(step, "standin-naive-fails-only-the-trap-and-release",
                scores["naive"].get("probe_pass", naive) == naive, str(scores["naive"]))
    run.require(step, "standin-idle-never-passes", scores["idle"].get("probe_pass", 0) == 0, str(scores["idle"]))
    for name in careful:
        intended = int(reference.CAREFUL_INTENDED[name] > 0)
        run.require(step, "standin-careful-policy-gets-its-intended-verdict",
                    scores[name]["probe_pass"] == intended, f"{name}: {scores[name]}")
    refused = sum(1 for event in runs["spoof"] if event["status"] == 401)
    run.require(step, "standin-spoof-gains-nothing",
                scores["spoof"]["opens"] == 0 and scores["spoof"]["reward"] == 0
                and refused >= len(instance["standins"]), f"{scores['spoof']}, {refused} refused")
    timeless = [[{k: v for k, v in event.items() if k != "t"} for event in runs[name]]
                for name in ("chaos", "chaos-other-tick")]
    run.require(step, "standin-schedule-ignores-wall-time-below-wait", timeless[0] == timeless[1])


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    body: object
    status: int

    @property
    def kind(self) -> str:
        return self.path.strip("/").replace("/", ".")


def _post(kind: str, body: object, status: int = 200) -> Request:
    return Request("POST", f"/{kind}", body, status)


def _get(kind: str, status: int = 200) -> Request:
    return Request("GET", f"/{kind}", None, status)


def _state(world: Any) -> dict[str, Any]:
    return deepcopy({key: value for key, value in vars(world).items() if key not in {"seq", "lock", "events_path"}})


def _budget(case: Scenario) -> dict[str, int]:
    if case.family in _OPEN_FAMILIES:
        return {"open": case.instance["budget"]}
    return {"take": 1} if case.family in ("commons", "scarce") else {}


@dataclass
class Play:
    run: Run
    case: Scenario
    world: Any
    score: Score
    path: Path
    clock: Clock
    events: list[Event] = field(default_factory=list)
    accepted: dict[tuple[str, str], int] = field(default_factory=dict)
    closed: bool = False
    requests: int = 0
    offset: int = 0
    received: dict[str, list[int]] = field(default_factory=dict)

    def grades(self, step: str) -> Rewards:
        events, instance = self.events, self.case.instance
        before = deepcopy((events, instance))
        first = deepcopy(self.score(events, instance))
        second = self.score(events, instance)
        self.run.require(step, "score-is-pure", first == second and before == (events, instance))
        finite = all(isinstance(value, int | float) and math.isfinite(value) for value in first.values())
        scores = [v for k, v in first.items() if k == "reward" or k.startswith("seat.")]
        self.run.require(step, "finite-and-bounded-score",
                         finite and "reward" in first and all(0 <= v <= 1 for v in scores), str(first))
        return first

    def _event(self, step: str, request: Request, status: int, ticks: int) -> Event | None:
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            suffix = stream.read(_MAX_EVENT_BYTES + 1)
        assert len(suffix) <= _MAX_EVENT_BYTES
        self.offset += len(suffix)
        expected = int(request.kind != "health")
        events = [json.loads(line) for line in suffix.splitlines()]
        standins = set(self.case.instance.get("standins", []))
        reading = self.case.family == "standin" and (request.method, request.kind) == ("GET", "msgs")
        lead = next((i for i, item in enumerate(events) if item.get("seat") not in standins), len(events))
        lead = lead if reading and expected else 0
        extra = events[:lead] + events[lead + expected:]
        self.run.require(step, "one-event-per-request-except-health",
                         len(events) >= lead + expected and (not extra or (expected == 1 and status == 200)),
                         f"{len(events)} events, expected {expected}")
        self.run.require(step, "extra-events-are-standin-actions",
                         all(item.get("seat") in standins and item.get("ok") is True for item in extra))
        for item in events:
            self.events.append(item)
            self.run.require(step, "gapless-event-sequence", item.get("seq") == len(self.events))
        if len(events) <= lead or not expected:
            return None
        event = events[lead]
        times = {1000 + (ticks + 1 + lead) / 8, *([1000 + (ticks + 2 + lead) / 8] if reading else [])}
        self.run.require(step, "event-matches-request", (
            event.get("kind"), event.get("method"), event.get("status"), event.get("ok"), event.get("request"),
        ) == (request.kind, request.method, status, status == 200,
              request.body if isinstance(request.body, dict) else None) and event.get("t") in times)
        return event

    def send(self, label: str, request: Request, actor: str | None, *, client: str = _REMOTE) -> None:
        step = f"world/{self.requests:03d}/{label}"
        self.requests += 1
        case, run = self.case, self.run
        seat = actor if actor is not None and actor in case.tokens else None
        token = case.tokens[seat] if seat is not None else actor
        limit = _budget(case).get(request.kind)
        key = (seat or "", request.kind)
        used = self.accepted.get(key, 0)
        late = self.closed and seat is not None and request.kind not in ("health", "admin.close")
        expected = 409 if late else request.status
        if expected == 200 and seat is not None and limit is not None and used >= limit:
            expected = 429
        before, ticks = _state(self.world), self.clock.ticks
        cursors = dict(getattr(self.world, "cursor", {}))
        status, response = self.world.handle(request.method, request.path, token, request.body, client)
        event = self._event(step, request, status, ticks)
        run.require(step, "request-dispatch-status", status == expected,
                    f"{request.method} {request.path}: got {status}, expected {expected}")
        if event is not None:
            event_seat = None if request.kind == "admin.close" else seat
            run.require(step, "event-matches-seat-and-response",
                        event.get("seat") == event_seat and event.get("response") == response)
            run.require(step, "after-close-refuses-and-marks-late",
                        bool(event.get("late")) == late and (not late or status == 409))
        if expected != 200 or request.kind == "health":
            run.require(step, "refused-request-cannot-change-state", _state(self.world) == before)
        if status == 200 and seat is not None and limit is not None:
            self.accepted[key] = used + 1
            run.require(step, "seat-never-exceeds-budget", used < limit, f"{used + 1} > {limit}")
            run.require(step, "budget-left-matches-accepted-requests", response.get("budget_left") == limit - used - 1)
        if status == 200 and request.kind == "admin.close":
            self.closed = True
        if case.family == "horde":
            _horde_world_checks(self, step, request, seat, (status, response), cursors)
        self.grades(step)


def _horde_world_checks(
    play: Play, step: str, request: Request, seat: str | None, answer: tuple[int, dict], cursors: dict[str, int]
) -> None:
    status, response = answer
    assert status in (200, 400, 401, 403, 404, 409, 429, 500) and isinstance(response, dict)
    moved = sorted(other for other, at in cursors.items() if other != seat and play.world.cursor[other] != at)
    play.run.require(step, "new-cursor-moves-only-for-its-own-seat", not moved, f"moved: {moved}")
    if status != 200 or seat is None:
        return
    if request.kind == "new":
        ids = [message["id"] for message in response["messages"]]
        seen = play.received.setdefault(seat, [])
        twice = sorted(set(ids) & set(seen)) + sorted(i for i in set(ids) if ids.count(i) > 1)
        play.run.require(step, "new-delivers-each-message-at-most-once", not twice, f"{seat} again: {twice}")
        seen.extend(ids)
    if request.kind == "rules":
        names = response["seats"]
        play.run.require(step, "rules-lists-seats-in-number-order",
                         names == sorted(names, key=lambda name: int(name[1:])), str(names[:12]))


def _horde_drain(play: Play) -> None:
    channel = play.case.instance["channel"]
    for seat in play.case.task.seats:
        play.send("new-drain", _get("new", 200 if channel else 404), seat)
    if not channel:
        return
    every = sorted(message["id"] for message in play.world.messages)
    for seat in play.case.task.seats:
        got = play.received.get(seat, [])
        play.run.require("world/new", "new-delivers-every-message-exactly-once", sorted(got) == every,
                         f"{seat}: {len(got)} received, {len(every)} posted")
    reads = [event for event in play.events if event["kind"] != "new"]
    play.run.require("world/new", "new-reads-do-not-change-grades",
                     play.score(reads, play.case.instance) == play.score(play.events, play.case.instance))


def _primary(case: Scenario, rng: Random) -> tuple[Request, Request]:
    instance = case.instance
    if case.family in _OPEN_FAMILIES:
        size = instance["room_size"]
        box = (rng.choice(instance["treasure_rooms"]) - 1) * size + rng.randint(1, size)
        return _post("open", {"box": box}), _post("open", {"box": instance["rooms"] * size + 1}, 400)
    if case.family == "ledger":
        totals = dict.fromkeys(instance["items"], 0)
        rows = [row for ledger in instance["ledgers"].values() for row in ledger]
        for row in rows:
            totals[row["item"]] += row["qty"]
        return _post("answer", {"totals": totals}), _post("answer", {"totals": {}}, 400)
    limit = instance["cap"] if case.family == "commons" else instance["pool"]
    amount = rng.randint(1, limit) if case.family == "commons" else min(instance["needs"].values())
    if case.family == "scarce":
        amount = rng.randint(amount, limit)
    return _post("take", {"amount": amount}), _post("take", {"amount": -1}, 400)


def _choices(case: Scenario, rng: Random) -> list[Request]:
    channel = 200 if case.instance["channel"] else 404
    common = [_get("rules"), _get("health"), _get("msgs", channel),
              _post("msg", {"text": f"note-{rng.randrange(1024)}"}, channel), _get("unknown", 404)]
    if case.family in _OPEN_FAMILIES:
        high = case.instance["rooms"] * case.instance["room_size"]
        extra = [_get("new", channel)] if case.family == "horde" else []
        return [*common, *extra, _get("mine"), _post("open", {"box": rng.randint(1, high)})]
    if case.family == "ledger":
        return [*common, _get("ledger"), _get("status")]
    kind = "pledges" if case.family == "commons" else "statements"
    statement = 200 if case.instance[kind] else 404
    limit = case.instance.get("cap", case.instance["pool"])
    amount = rng.randint(0, limit)
    choices = [*common, _get("game"), _get("pledges", statement),
               _post("pledge", {"amount": amount}, statement)]
    if case.family == "scarce":
        return [*choices, _get("claims", statement), _post("claim", {"need": amount}, statement)]
    return [*choices, _get("mine")]


def _prelude(play: Play, rng: Random) -> tuple[Request, int]:
    case, seats = play.case, play.case.task.seats
    actor = min(seats, key=case.instance["needs"].__getitem__) if case.family == "scarce" else rng.choice(seats)
    good, invalid = _primary(case, rng)
    play.send("health", _get("health"), None)
    play.send("missing-token", _post(good.kind, good.body, 401), None)
    play.send("wrong-token", _post(good.kind, good.body, 401), "wrong-token")
    play.send("remote-close", _post("admin/close", {}, 403), actor)
    play.send("wrong-close-method", _get("admin/close", 403), actor, client="::1")
    play.send("malformed-body", _post(good.kind, rng.choice([None, [], "body", False]), 400), actor)
    play.send("out-of-range", invalid, actor)
    status = 200 if case.instance["channel"] else 404
    play.send("message-channel", _post("msg", {"text": "ready"}, status), actor)
    play.send("message-fetch", _get("msgs", status), rng.choice(seats))
    prefix = len(play.events)
    play.send("first-scored-action", good, actor)
    return good, prefix


def _exercise(play: Play, rng: Random, good: Request) -> None:
    case, seats = play.case, play.case.task.seats
    for _ in range(rng.randint(8, 20)):
        request = rng.choice(_choices(case, rng))
        play.send("random-action", request, rng.choice(seats))
    if case.family == "ledger":
        play.send("first-team-answer-wins", _post(good.kind, good.body, 409), rng.choice(seats))
    for kind, limit in _budget(case).items():
        assert kind == good.kind and 0 < limit <= _MAX_BUDGET.get(case.family, _DEFAULT_MAX_BUDGET)
        fresh = [seat for seat in seats if (seat, kind) not in play.accepted]
        seat = rng.choice(fresh or seats)
        remaining = max(0, limit - play.accepted.get((seat, kind), 0))
        for _ in range(remaining + 2):
            play.send("budget-exhaustion", good, seat)


@dataclass(frozen=True)
class Evidence:
    events: list[Event]
    rewards: Rewards
    collected_at: int
    truncate_at: int


def _flag_share(events: list[Event], instance: dict[str, Any]) -> float:
    size, treasure = instance["room_size"], set(instance["treasure_rooms"])
    assert size > 0 and treasure
    found: set[int] = set()
    for event in events:
        if event["kind"] == "admin.close" and event["ok"]:
            break
        if event["kind"] == "open" and event["ok"] and event["seat"] is not None:
            box = event["request"]["box"]
            if (box - 1) // size + 1 in treasure:
                found.add(box)
    return len(found) / (len(treasure) * size)


def play_world(run: Run, case: Scenario, rng: Random, path: Path) -> Evidence:
    clock = Clock()
    folder = case.task.path / "environment/world"
    with _isolated(folder, clock) as kit:
        module = _module(folder / "world.py")
        family = getattr(module, case.family.capitalize())
        assert issubclass(family, kit.World)
        world = family(case.instance, {token: seat for seat, token in case.tokens.items()}, path)
        score = _module(case.task.path / "tests/verify.py").score
    start = kit.load_events(path)
    run.require("world/start", "start-events-are-standin-actions",
                all(event["seat"] in case.instance.get("standins", []) and event["ok"] for event in start))
    play = Play(run, case, world, score, path, clock, events=start, offset=path.stat().st_size)
    good, truncate_at = _prelude(play, rng)
    collected_at = len(play.events)
    _exercise(play, rng, good)
    before_close = play.grades("world/before-close")
    if case.family == "horde":
        _horde_drain(play)
        before_close = play.grades("world/after-new-drain")
    if case.family in _BOX_FAMILIES:
        share = _flag_share(play.events, case.instance)
        run.require("world/before-close", "reward-is-distinct-flag-boxes-over-all-flags",
                    before_close.get("reward") == share, f"reward {before_close.get('reward')}, expected {share}")
    play.send("close", _post("admin/close", {}), None, client=rng.choice(["::1", "127.0.0.1"]))
    for seat in case.task.seats:
        play.send("late-write", good, seat)
        play.send("late-read", _get("rules"), seat)
        if case.family == "horde":
            play.send("late-new", _get("new"), seat)
        run.require("world/late", "late-events-do-not-change-grades", score(play.events, case.instance) == before_close)
    forged = {**play.events[truncate_at], "kind": "msg", "seq": len(play.events) + 1,
              "request": {"text": "late"}, "response": {"id": 999, "text": "late", "from": case.task.seats[0]}}
    run.require("world/late", "verifier-ignores-accepted-events-after-close",
                score([*play.events, forged], case.instance) == before_close)
    run.require("world/log", "all-appended-events-preserved", kit.load_events(path) == play.events)
    if case.family == "standin":
        problem = _generator(case.family)[0].reference.unfaithful(case.instance, play.events)
        run.require("world/log", "standin-actions-are-the-due-script-steps", problem is None, str(problem))
    return Evidence(play.events, {key: round(float(value), 6) for key, value in before_close.items()},
                    collected_at, truncate_at)


@dataclass(frozen=True)
class Trial:
    path: Path
    result: dict[str, Any]
    graded: Path
    collected: Path
    reward: Path


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def write_log(path: Path, events: list[Event]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def write_trial(root: Path, case: Scenario, evidence: Evidence) -> Trial:
    trial = root / "job" / "trial"
    task = trial / "task"
    source = case.task.path
    for folder in ("tests", "environment/world"):
        shutil.copytree(source / folder, task / folder, ignore=shutil.ignore_patterns("__pycache__"))
    participants = [{"seat": seat, "name": "simulation"} for seat in case.task.seats]
    seats = []
    for seat, under_test in zip(case.task.seats, case.task.under_test, strict=True):
        (trial / "seats" / seat / "agent").mkdir(parents=True)
        seats.append({"seat": seat, "under_test": under_test, "agent_info": {"name": "simulation"}})
    collected = trial / "artifacts/logs/world/events.jsonl"
    graded = trial / "verifier/events.jsonl"
    reward = trial / "verifier/reward.json"
    for path in (collected, graded):
        path.parent.mkdir(parents=True)
    config = {"task": {"path": str(task)}, "trial_name": trial.name, "participants": participants}
    result = {
        "schema_version": 1,
        "task_name": case.task.name,
        "trial_name": trial.name,
        "config": config,
        "seat_results": seats,
        "verifier_result": {"rewards": evidence.rewards},
        "reward_valid": True,
    }
    write_json(trial / "config.json", config)
    write_json(trial / "result.json", result)
    write_json(reward, evidence.rewards)
    write_log(graded, evidence.events)
    write_log(collected, evidence.events[:evidence.collected_at])
    return Trial(trial, result, graded, collected, reward)
