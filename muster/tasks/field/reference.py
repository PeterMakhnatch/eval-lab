import argparse
import hashlib
import importlib.util
import json
import sys
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
sys.path.insert(0, str(FAMILY.parent / "_kit"))

import build  # noqa: E402
import worldkit  # noqa: E402

POLICIES = ("oracle", "sequential", "random", "spoof")
REMOTE = "10.0.0.2"
MAX_LAYOUT_ATTEMPTS = 64
MAX_REQUESTS_PER_SEAT = 4096
_MODULES: dict[str, ModuleType] = {}


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"field_{name}", TEMPLATE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


world = load("world")
verify = load("verify")


def family() -> dict:
    config = tomllib.loads((FAMILY / "family.toml").read_text())
    assert 0 < config["treasure_rooms"] < config["rooms"] and config["room_size"] > 0
    assert 0 < config["instances"]
    return config


def _rank(seed: str, number: int, attempt: int, room: int) -> str:
    return hashlib.sha256(f"{seed}/layout/{number}/{attempt}/{room}".encode()).hexdigest()


def layouts(config: dict) -> list[tuple[int, ...]]:
    rooms = list(range(1, config["rooms"] + 1))
    chosen: list[tuple[int, ...]] = []
    for number in range(1, config["instances"] + 1):
        for attempt in range(MAX_LAYOUT_ATTEMPTS):
            ranked = sorted(rooms, key=lambda room, a=attempt, n=number: _rank(config["seed"], n, a, room))
            treasure = tuple(sorted(ranked[: config["treasure_rooms"]]))
            if treasure not in chosen:
                break
        else:
            raise AssertionError(f"no new layout for instance {number}")
        chosen.append(treasure)
    assert len(set(chosen)) == config["instances"]
    return chosen


def instances(config: dict, condition: str) -> list[dict]:
    spec = config["conditions"][condition]
    assert spec["budget"] > 0 and spec["seats"] == sorted(spec["seats"])
    assert spec["channel"] == ("rule" in spec)
    return [
        {
            "family": config["name"],
            "condition": condition,
            "instance": number,
            "seats": spec["seats"],
            "budget": spec["budget"],
            "channel": spec["channel"],
            "rooms": config["rooms"],
            "room_size": config["room_size"],
            "treasure_rooms": list(treasure),
        }
        for number, treasure in enumerate(layouts(config), start=1)
    ]


def tokens(config: dict, instance: dict) -> dict[str, str]:
    return {seat: build.token(config["seed"], instance["instance"], seat) for seat in instance["seats"]}


def render_policy(config: dict, condition: str, policy: str) -> str:
    assert policy in POLICIES
    text = (TEMPLATE / "policies" / "seat.sh").read_text()
    values = {
        "@ROOMS@": str(config["rooms"]),
        "@SIZE@": str(config["room_size"]),
        "@TREASURE@": str(config["treasure_rooms"]),
        "@RULE@": config["conditions"][condition].get("rule", "none"),
        "@POLICY@": policy,
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
        name = "field_policy_" + hashlib.sha256(script.encode()).hexdigest()[:16]
        module = ModuleType(name)
        exec(compile(code, f"<{name}>", "exec"), module.__dict__)
        _MODULES[script] = module
    return _MODULES[script]


def _answer(game: worldkit.World, request: dict) -> tuple[int, dict]:
    body = json.loads(json.dumps(request["body"])) if request["method"] == "POST" else None
    path = request["path"].split("?")[0]
    return game.handle(request["method"], path, request["auth"], body, REMOTE)


def play(
    instance: dict,
    seat_tokens: dict[str, str],
    roster: dict[str, str] | str = "oracle",
    pick: Callable[[list[str], int], str] | None = None,
) -> list[dict]:
    if isinstance(roster, str):
        roster = dict.fromkeys(instance["seats"], roster)
    assert set(roster) == set(instance["seats"]) == set(seat_tokens)
    config = family()
    with tempfile.TemporaryDirectory() as scratch:
        game = world.Field(instance, {token: seat for seat, token in seat_tokens.items()}, Path(scratch) / "e.jsonl")
        plays = {}
        for seat, policy in roster.items():
            if policy != "nop":
                module = policy_module(render_policy(config, instance["condition"], policy))
                plays[seat] = module.POLICIES[module.POLICY](seat_tokens[seat])
        replies: dict[str, tuple[int, dict] | None] = dict.fromkeys(plays)
        limit = MAX_REQUESTS_PER_SEAT * len(instance["seats"]) + len(instance["seats"])
        for turn in range(limit):
            if not plays:
                break
            seats = sorted(plays)
            seat = pick(seats, turn) if pick else seats[turn % len(seats)]
            try:
                request = plays[seat].send(replies[seat])
            except StopIteration:
                del plays[seat]
                continue
            replies[seat] = _answer(game, request)
        assert not plays, "a policy made too many requests"
        game.handle("POST", "/admin/close", None, {}, "127.0.0.1")
        return worldkit.load_events(game.events_path)


def simulate(
    instance: dict,
    seat_tokens: dict[str, str],
    roster: dict[str, str] | str = "oracle",
    pick: Callable[[list[str], int], str] | None = None,
) -> dict[str, float]:
    return verify.score(play(instance, seat_tokens, roster, pick), instance)


def rewards(config: dict, condition: str, policy: str) -> list[float]:
    scores = []
    for instance in instances(config, condition):
        result = simulate(instance, tokens(config, instance), policy)
        repeats = result["duplicate_opens"]
        assert policy != "oracle" or not instance["channel"] or repeats == 0, (condition, instance["instance"])
        scores.append(round(result["reward"], 6))
    return scores


def reference(config: dict) -> dict[str, dict]:
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


def reference_toml(config: dict) -> str:
    lines = []
    for condition, policies in reference(config).items():
        for policy, entry in policies.items():
            lines += [f"[reference.{condition}.{policy}]"]
            lines += [f"{key} = {json.dumps(value)}" for key, value in entry.items()] + [""]
    return "\n".join(lines)


def check(config: dict) -> int:
    if config.get("reference") == reference(config):
        return 0
    print("family.toml [reference] differs: run muster/tasks/field/reference.py --write", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the [reference] tables of family.toml.")
    parser.add_argument("--write", action="store_true", help="put the tables into family.toml")
    parser.add_argument("--check", action="store_true", help="exit 1 when family.toml holds other numbers")
    args = parser.parse_args(argv)
    config = family()
    if args.check:
        return check(config)
    if args.write:
        path = FAMILY / "family.toml"
        head = path.read_text().split("[reference.")[0]
        path.write_text(head + reference_toml(config))
    else:
        print(reference_toml(config), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
