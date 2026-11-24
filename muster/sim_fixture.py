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
_FAMILIES = ("boxes", "commons", "crowd", "crowdrules", "field", "horde", "ledger", "scarce")
_BOX_FAMILIES = ("boxes", "crowd", "crowdrules", "field", "horde")
_HORDE_LARGE = ("team-48", "zones-48", "chief-48")
_MAX_SEATS = 48
_MAX_BUILDS = 256
_MAX_FILES = 64
_MAX_BUDGET = 96
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
    assert 1 <= len(task.seats) <= _MAX_SEATS
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
    generator.worldkit.time.ticks = 0
    if case.family == "ledger":
        instance = int(case.instance["instance"][1:])
        order = tuple(rng.sample(case.task.seats, len(case.task.seats)))
        return generator.play(instance, case.condition, case.instance["ledgers"], order)
    return generator.play(case.instance, case.tokens, dict.fromkeys(case.task.seats, "oracle"))


def _horde_measures(reference: ModuleType, instance: dict[str, Any], events: list[Event]) -> Rewards:
    config = reference.family()
    size, treasure = instance["room_size"], set(instance["treasure_rooms"])
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
    paths = next(paths for paths in _builds("horde") if paths[0].parent.name == condition)
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
    if case.family in _BOX_FAMILIES:
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

    def _event(self, step: str, request: Request, status: int) -> Event | None:
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            suffix = stream.read(_MAX_EVENT_BYTES + 1)
        assert len(suffix) <= _MAX_EVENT_BYTES
        self.offset += len(suffix)
        expected = int(request.kind != "health")
        lines = suffix.splitlines()
        self.run.require(step, "one-event-per-request-except-health", len(lines) == expected,
                         f"{len(lines)} events, expected {expected}")
        if not lines:
            return None
        event = json.loads(lines[0])
        self.events.append(event)
        self.run.require(step, "gapless-event-sequence", event.get("seq") == len(self.events))
        self.run.require(step, "event-matches-request", (
            event.get("kind"), event.get("method"), event.get("status"), event.get("ok"),
            event.get("request"), event.get("t"),
        ) == (request.kind, request.method, status, status == 200,
              request.body if isinstance(request.body, dict) else None, 1000 + self.clock.ticks / 8))
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
        before = _state(self.world)
        status, response = self.world.handle(request.method, request.path, token, request.body, client)
        event = self._event(step, request, status)
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
        self.grades(step)


def _primary(case: Scenario, rng: Random) -> tuple[Request, Request]:
    instance = case.instance
    if case.family in _BOX_FAMILIES:
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
    if case.family in _BOX_FAMILIES:
        high = case.instance["rooms"] * case.instance["room_size"]
        return [*common, _get("mine"), _post("open", {"box": rng.randint(1, high)})]
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
        assert kind == good.kind and 0 < limit <= _MAX_BUDGET
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
    play = Play(run, case, world, score, path, clock)
    good, truncate_at = _prelude(play, rng)
    collected_at = len(play.events)
    _exercise(play, rng, good)
    before_close = play.grades("world/before-close")
    if case.family in _BOX_FAMILIES:
        share = _flag_share(play.events, case.instance)
        run.require("world/before-close", "reward-is-distinct-flag-boxes-over-all-flags",
                    before_close.get("reward") == share, f"reward {before_close.get('reward')}, expected {share}")
    play.send("close", _post("admin/close", {}), None, client=rng.choice(["::1", "127.0.0.1"]))
    for seat in case.task.seats:
        play.send("late-write", good, seat)
        play.send("late-read", _get("rules"), seat)
        run.require("world/late", "late-events-do-not-change-grades", score(play.events, case.instance) == before_close)
    forged = {**play.events[truncate_at], "kind": "msg", "seq": len(play.events) + 1,
              "request": {"text": "late"}, "response": {"id": 999, "text": "late", "from": case.task.seats[0]}}
    run.require("world/late", "verifier-ignores-accepted-events-after-close",
                score([*play.events, forged], case.instance) == before_close)
    run.require("world/log", "all-appended-events-preserved", kit.load_events(path) == play.events)
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
