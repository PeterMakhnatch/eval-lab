import argparse
import hashlib
import http.client
import importlib.util
import io
import json
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from random import Random
from types import ModuleType

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
FIELD_BUILD = FAMILY.parent / "field" / "build" / "solo-9"
sys.path.insert(0, str(FAMILY.parent / "_kit"))

import build  # noqa: E402
import worldkit  # noqa: E402

POLICIES = ("oracle", "sequential", "random", "spoof")
RULES = ("solo", "team", "zones", "chief")
AGENTS = (1, 8, 16, 32, 48)
REMOTE = "10.0.0.2"
WORLD_URL = "http://world:8080"
MAX_LAYOUT_ATTEMPTS = 64
MAX_REQUESTS_PER_SEAT = 4096
SAMPLED_LAYOUTS = 512
FAULT_RATE = 0.25
MAX_FAULT_BURST = 3
FAULTS = ("refused", "reset-before", "reset-after", "timeout-after", "disconnect-after")
_MODULES: dict[str, ModuleType] = {}


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"horde_{name}", TEMPLATE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


world = load("world")
verify = load("verify")


def family() -> dict:
    config = tomllib.loads((FAMILY / "family.toml").read_text())
    assert config["name"] == FAMILY.name and config["instances"] > 0
    assert 0 < config["treasure_rooms"] < config["rooms"] and config["room_size"] > 1
    assert config["total_budget"] == config["rooms"] - config["treasure_rooms"] + flags(config)
    for condition, spec in config["conditions"].items():
        agents, rule = spec["agents"], spec["rule"]
        assert agents in AGENTS and rule in RULES and config["total_budget"] % agents == 0, condition
        assert (agents == 1) == (rule == "solo"), condition
        assert condition == f"{rule}-{config['total_budget'] if rule == 'solo' else agents}", condition
    return config


def flags(config: dict) -> int:
    return config["treasure_rooms"] * config["room_size"]


def _rank(seed: str, number: int, attempt: int, room: int) -> str:
    return hashlib.sha256(f"{seed}/layout/{number}/{attempt}/{room}".encode()).hexdigest()


def layouts(config: dict) -> list[tuple[int, ...]]:
    rooms = list(range(1, config["rooms"] + 1))
    chosen: list[tuple[int, ...]] = []
    for number in range(1, config["instances"] + 1):
        for attempt in range(MAX_LAYOUT_ATTEMPTS):
            ranked = sorted(rooms, key=lambda room, a=attempt, n=number: _rank(config["layout_seed"], n, a, room))
            treasure = tuple(sorted(ranked[: config["treasure_rooms"]]))
            if treasure not in chosen:
                break
        else:
            raise AssertionError(f"no new layout for instance {number}")
        chosen.append(treasure)
    assert len(set(chosen)) == config["instances"]
    return chosen


def check_layouts(config: dict) -> None:
    for number, treasure in enumerate(layouts(config), start=1):
        path = FIELD_BUILD / f"field-i{number:02d}" / "environment" / "world" / "instance.json"
        assert json.loads(path.read_text())["treasure_rooms"] == list(treasure), number


def seats_of(agents: int) -> list[str]:
    assert agents in AGENTS
    return [f"p{number}" for number in range(1, agents + 1)]


def instances(config: dict, condition: str) -> list[dict]:
    spec = config["conditions"][condition]
    seats = seats_of(spec["agents"])
    return [
        {
            "family": config["name"],
            "condition": condition,
            "instance": number,
            "seats": seats,
            "budget": config["total_budget"] // len(seats),
            "channel": spec["rule"] != "solo",
            "rule": spec["rule"],
            "rooms": config["rooms"],
            "room_size": config["room_size"],
            "treasure_rooms": list(treasure),
        }
        for number, treasure in enumerate(layouts(config), start=1)
    ]


def tokens(config: dict, instance: dict) -> dict[str, str]:
    return {seat: build.token(config["seed"], instance["instance"], seat) for seat in instance["seats"]}


def scout_rooms(config: dict, number: int, agents: int) -> list[int]:
    assert 1 <= number <= agents
    return [room for room in range(1, config["rooms"] + 1) if (room - 1) % agents == number - 1]


def box_at(config: dict, room: int, position: int) -> int:
    assert 1 <= room <= config["rooms"] and 1 <= position <= config["room_size"]
    return (room - 1) * config["room_size"] + position


def zones(config: dict, agents: int) -> dict[str, dict]:
    ranks = config["treasure_rooms"]
    assert agents % ranks == 0 and agents in AGENTS
    budget = config["total_budget"] // agents
    cursor = dict.fromkeys(range(1, ranks + 1), 2)
    plan = {}
    for number in range(1, agents + 1):
        scout = scout_rooms(config, number, agents)
        rank, left = (number - 1) % ranks + 1, budget - len(scout)
        assert left > 0
        plan[f"p{number}"] = {"scout": scout, "rank": rank, "positions": list(range(cursor[rank], cursor[rank] + left))}
        cursor[rank] += left
    assert all(end == config["room_size"] + 1 for end in cursor.values()), cursor
    return plan


def zone_boxes(config: dict, part: dict, treasure: list[int]) -> list[int]:
    assert len(treasure) == config["treasure_rooms"]
    room = sorted(treasure)[part["rank"] - 1]
    return [box_at(config, room, position) for position in part["positions"]]


def owners(config: dict, plan: dict, treasure: list[int]) -> dict[int, str]:
    found: dict[int, str] = {}
    for seat, part in plan.items():
        mine = [box_at(config, room, 1) for room in part["scout"]] + zone_boxes(config, part, treasure)
        assert len(mine) == config["total_budget"] // len(plan) == len(set(mine)), seat
        for box in mine:
            assert box not in found, (seat, box, treasure)
            found[box] = seat
    flagged = {box for box in found if (box - 1) // config["room_size"] + 1 in treasure}
    assert len(flagged) == flags(config), treasure
    return found


def check_zones(config: dict) -> None:
    rooms, size, ranks = config["rooms"], config["room_size"], config["treasure_rooms"]
    sample = Random(f"{config['seed']}/zones")
    every = [list(treasure) for treasure in layouts(config)]
    every += [sorted(sample.sample(range(1, rooms + 1), ranks)) for _ in range(SAMPLED_LAYOUTS)]
    for agents in sorted({spec["agents"] for spec in config["conditions"].values() if spec["rule"] == "zones"}):
        plan = zones(config, agents)
        scouted = sorted(room for part in plan.values() for room in part["scout"])
        assert scouted == list(range(1, rooms + 1)), agents
        slots = sorted((part["rank"], position) for part in plan.values() for position in part["positions"])
        assert slots == [(rank, position) for rank in range(1, ranks + 1) for position in range(2, size + 1)]
        for treasure in every:
            owners(config, plan, treasure)


def plan_text(config: dict, condition: str) -> str:
    spec = config["conditions"][condition]
    if spec["rule"] != "zones":
        return "{}"
    return json.dumps(zones(config, spec["agents"]), sort_keys=True, separators=(",", ":"))


def render_policy(config: dict, condition: str, policy: str) -> str:
    assert policy in POLICIES
    text = (TEMPLATE / "policies" / "seat.sh").read_text()
    values = {
        "@ROOMS@": str(config["rooms"]),
        "@SIZE@": str(config["room_size"]),
        "@TREASURE@": str(config["treasure_rooms"]),
        "@RULE@": config["conditions"][condition]["rule"],
        "@POLICY@": policy,
        "@PLAN@": plan_text(config, condition),
        "@CODE@": (TEMPLATE / "policies" / "policy.py").read_text().rstrip("\n"),
    }
    for key, value in values.items():
        assert text.count(key) == 1, key
        text = text.replace(key, value)
    return text


def policy_module(script: str) -> ModuleType:
    if script not in _MODULES:
        _, opener, rest = script.partition("<<'PY'\n")
        code, closer, _ = rest.rpartition("\nPY\n")
        assert opener and closer
        name = "horde_policy_" + hashlib.sha256(script.encode()).hexdigest()[:16]
        module = ModuleType(name)
        exec(compile(code, f"<{name}>", "exec"), module.__dict__)
        _MODULES[script] = module
    return _MODULES[script]


Answer = Callable[[dict], tuple[int, dict]]


def direct(game: worldkit.World) -> Answer:
    def answer(request: dict) -> tuple[int, dict]:
        body = json.loads(json.dumps(request["body"])) if request["method"] == "POST" else None
        return game.handle(request["method"], request["path"].split("?")[0], request["auth"], body, REMOTE)

    return answer


class Reply:
    def __init__(self, status: int, data: bytes):
        self.status, self.data = status, data

    def __enter__(self) -> "Reply":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self.data


def faulty_open(game: worldkit.World, faults: Random) -> Callable[..., Reply]:
    burst = [0]

    def urlopen(message: urllib.request.Request, timeout: float) -> Reply:
        assert timeout > 0 and message.full_url.startswith(WORLD_URL)
        fault = None
        if burst[0] < MAX_FAULT_BURST and faults.random() < FAULT_RATE:
            fault = faults.choice(FAULTS)
        burst[0] = burst[0] + 1 if fault else 0
        if fault == "refused":
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        if fault == "reset-before":
            raise urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
        auth = message.get_header("Authorization") or ""
        token = auth[7:] if auth.startswith("Bearer ") else None
        body = json.loads(message.data) if isinstance(message.data, bytes) else None
        path = message.full_url[len(WORLD_URL) :].split("?")[0]
        status, response = game.handle(message.get_method(), path, token, body, REMOTE)
        if fault == "reset-after":
            raise ConnectionResetError(104, "Connection reset by peer")
        if fault == "timeout-after":
            raise TimeoutError("timed out")
        if fault == "disconnect-after":
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        data = json.dumps(response).encode()
        if status != 200:
            raise urllib.error.HTTPError(message.full_url, status, "refused", http.client.HTTPMessage(), io.BytesIO(data))
        return Reply(status, data)

    return urlopen


def transport(module: ModuleType, game: worldkit.World, faults: Random | None) -> Answer:
    if faults is None:
        return direct(game)
    urlopen = faulty_open(game, faults)

    def answer(request: dict) -> tuple[int, dict]:
        return module.send(WORLD_URL, request, urlopen, lambda seconds: None)

    return answer


def play(
    instance: dict,
    seat_tokens: dict[str, str],
    roster: dict[str, str] | str = "oracle",
    pick: Callable[[list[str], int], str] | None = None,
    faults: Random | None = None,
) -> list[dict]:
    if isinstance(roster, str):
        roster = dict.fromkeys(instance["seats"], roster)
    assert set(roster) == set(instance["seats"]) == set(seat_tokens)
    config = family()
    with tempfile.TemporaryDirectory() as scratch:
        game = world.Horde(instance, {token: seat for seat, token in seat_tokens.items()}, Path(scratch) / "e.jsonl")
        plays, answers = {}, {}
        for seat, policy in roster.items():
            if policy != "nop":
                module = policy_module(render_policy(config, instance["condition"], policy))
                plays[seat] = module.POLICIES[module.POLICY](seat_tokens[seat])
                answers[seat] = transport(module, game, faults)
        replies: dict[str, tuple[int, dict] | None] = dict.fromkeys(plays)
        for turn in range(MAX_REQUESTS_PER_SEAT * len(instance["seats"]) + len(instance["seats"])):
            if not plays:
                break
            seats = sorted(plays)
            seat = pick(seats, turn) if pick else seats[turn % len(seats)]
            try:
                request = plays[seat].send(replies[seat])
            except StopIteration:
                del plays[seat]
                continue
            replies[seat] = answers[seat](request)
        assert not plays, "a policy made too many requests"
        game.handle("POST", "/admin/close", None, {}, "127.0.0.1")
        return worldkit.load_events(game.events_path)


def simulate(
    instance: dict,
    seat_tokens: dict[str, str],
    roster: dict[str, str] | str = "oracle",
    pick: Callable[[list[str], int], str] | None = None,
    faults: Random | None = None,
) -> dict[str, float]:
    return verify.score(play(instance, seat_tokens, roster, pick, faults), instance)


def chief_lists(events: list[dict], lead: str) -> list[tuple[int, dict]]:
    found = []
    for event in worldkit.in_play(events):
        if event["kind"] != "msg" or event["seat"] != lead:
            continue
        try:
            posted = json.loads(event["request"]["text"])
        except ValueError:
            continue
        lists = posted.get("list") if isinstance(posted, dict) else None
        if isinstance(lists, dict):
            found.append((event["seq"], lists))
    return found


def allowed(config: dict, instance: dict, events: list[dict]) -> Callable[[dict], bool]:
    rule = instance["rule"]
    if rule == "zones":
        plan = zones(config, len(instance["seats"]))
        mine = {
            seat: {box_at(config, room, 1) for room in part["scout"]}
            | set(zone_boxes(config, part, instance["treasure_rooms"]))
            for seat, part in plan.items()
        }
        return lambda event: event["response"]["box"] in mine[event["seat"]]
    if rule == "chief":
        posts = chief_lists(events, "p1")

        def given(event: dict) -> bool:
            listed = [lists.get(event["seat"], []) for seq, lists in posts if seq < event["seq"]]
            return any(event["response"]["box"] in boxes for boxes in listed if isinstance(boxes, list))

        return given
    return lambda event: True


def outside_rule(events: list[dict], instance: dict) -> int:
    check = allowed(family(), instance, events)
    return sum(1 for event in worldkit.in_play(events) if event["kind"] == "open" and not check(event))


def rewards(config: dict, condition: str, policy: str) -> list[float]:
    scores = []
    for instance in instances(config, condition):
        events = play(instance, tokens(config, instance), policy)
        result = verify.score(events, instance)
        if policy == "oracle":
            assert result["duplicate_opens"] == 0 and outside_rule(events, instance) == 0, (condition, instance)
            assert result["opens"] <= config["total_budget"] and result["reward"] == 1.0, (condition, instance)
        scores.append(round(result["reward"], 6))
    return scores


def reference(config: dict) -> dict[str, dict]:
    check_layouts(config)
    check_zones(config)
    table: dict[str, dict] = {}
    for condition in config["conditions"]:
        table[condition] = {}
        for policy in POLICIES:
            scores = rewards(config, condition, policy)
            table[condition][policy] = {
                "mean": round(sum(scores) / len(scores), 6),
                "min": min(scores),
                "max": max(scores),
            }
    return table


def reference_toml(table: dict[str, dict]) -> str:
    lines = []
    for condition, policies in table.items():
        for policy, entry in policies.items():
            lines += [f"[reference.{condition}.{policy}]"]
            lines += [f"{key} = {json.dumps(value)}" for key, value in entry.items()] + [""]
    return "\n".join(lines)


def check(config: dict) -> int:
    if config.get("reference") == reference(config):
        return 0
    print("family.toml [reference] differs: run muster/tasks/horde/reference.py --write", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the [reference] tables of family.toml.")
    parser.add_argument("--write", action="store_true", help="put the tables into family.toml")
    parser.add_argument("--check", action="store_true", help="exit 1 when family.toml holds other numbers")
    args = parser.parse_args(argv)
    config = family()
    if args.check:
        return check(config)
    text = reference_toml(reference(config))
    if args.write:
        path = FAMILY / "family.toml"
        head = path.read_text().split("[reference.")[0]
        path.write_text(head + text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
