import argparse
import hashlib
import importlib.util
import itertools
import json
import sys
import tempfile
import tomllib
from collections.abc import Callable, Generator, Iterator
from functools import cache
from pathlib import Path
from types import ModuleType

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
sys.path.insert(0, str(FAMILY.parent / "_kit"))

import build  # noqa: E402
import worldkit  # noqa: E402

ROOMS = 6
ROOM_SIZE = 10
TREASURE_ROOMS = 2
ALL_ROOMS = list(range(1, ROOMS + 1))
PATIENCE = 300
REMOTE = "10.0.0.2"
REFERENCE_POLICIES = ("oracle", "sequential", "random")
COMPACT = (",", ":")

Step = Generator[None, None, bool]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"crowd_{name}", TEMPLATE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


world = load("world")
verify = load("verify")


def family() -> dict:
    return tomllib.loads((FAMILY / "family.toml").read_text())


FILL = (0, 0, 0, 0)


def tacit_rules(config: dict) -> dict[int, list[tuple[int, int, int, int]]]:
    rules = {int(k): [tuple(place) for place in places] for k, places in config["tacit"]["rule"].items()}
    assert 1 in rules and rules[1][0] != FILL
    for k, places in rules.items():
        assert len(places) == k
        for place in places:
            if place == FILL:
                assert places[:2] == [(1, 1, 1, 1), (6, -1, 1, 1)]
                continue
            start, step, slot, slot_step = place
            assert start in ALL_ROOMS and step in (1, -1) and 1 <= slot <= ROOM_SIZE and slot_step in (1, -1)
    return rules


def layouts(seed: str) -> list[tuple[int, ...]]:
    pairs = itertools.combinations(ALL_ROOMS, TREASURE_ROOMS)
    return sorted(
        pairs,
        key=lambda pair: hashlib.sha256(f"{seed}/layout/{'-'.join(map(str, pair))}".encode()).hexdigest(),
    )


def instances(config: dict, condition: str, count: int | None = None) -> list[dict]:
    spec = config["conditions"][condition]
    count = config["instances"] if count is None else count
    every = layouts(config["layout_seed"])
    assert 0 < count <= len(every)
    return [
        {
            "family": config["name"],
            "condition": condition,
            "instance": number,
            "seats": spec["seats"],
            "budget": spec["budget"],
            "channel": spec["channel"],
            "claim": spec["claim"],
            "rooms": ROOMS,
            "room_size": ROOM_SIZE,
            "treasure_rooms": list(treasure),
        }
        for number, treasure in enumerate(every[:count], start=1)
    ]


def tokens(config: dict, instance: dict) -> dict[str, str]:
    return {seat: build.token(config["seed"], instance["instance"], seat) for seat in instance["seats"]}


def boxes_of(room: int) -> list[int]:
    return list(range((room - 1) * ROOM_SIZE + 1, room * ROOM_SIZE + 1))


def random_order(token: str) -> list[int]:
    return sorted(
        range(1, ROOMS * ROOM_SIZE + 1), key=lambda box: hashlib.sha256(f"{token}/{box}".encode()).hexdigest()
    )


def scout_rooms(place: int, seats: int) -> list[int]:
    assert 0 <= place < seats
    return [room for room in ALL_ROOMS if (room - 1) % seats == place]


def text_json(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return None


def report_of(text: str) -> dict | None:
    report = text_json(text)
    if not isinstance(report, dict) or type(report.get("flag")) is not bool:
        return None
    room, box = report.get("room"), report.get("box")
    if type(room) is not int or type(box) is not int or room not in ALL_ROOMS or box not in boxes_of(room):
        return None
    return report


def claim_of(text: str) -> int | None:
    claim = text_json(text)
    if not isinstance(claim, dict) or type(claim.get("claim")) is not int:
        return None
    return claim["claim"]


class Seat:
    def __init__(self, game: worldkit.World, token: str, claim: bool, rules: dict, patience: int):
        assert patience > 0
        self.game, self.token, self.claim, self.rules, self.patience = game, token, claim, rules, patience
        self.kind: dict[int, bool] = {}
        self.seen: set[int] = set()
        self.board: dict[int, dict] = {}
        self.talks = False

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        status, reply = self.game.handle(method, path, self.token, body, REMOTE)
        if status != 200:
            raise RuntimeError(f"the world refused {method} {path}: {status} {reply}")
        return reply

    def start(self) -> Iterator[None]:
        rules = self.call("GET", "/rules")
        self.name = rules["you"]
        self.seats = len(rules["seats"])
        self.index = rules["seats"].index(rules["you"])
        self.channel = rules["channel"]
        self.budget = self.left = rules["budget_left"]["open"]
        self.claim = self.claim and self.channel
        yield

    def claimed(self, box: int) -> Step:
        if not self.claim:
            return True
        self.call("POST", "/msg", {"text": json.dumps({"claim": box}, separators=COMPACT)})
        yield
        board = self.call("GET", "/msgs")["messages"]
        yield
        first = next((message["from"] for message in board if claim_of(message["text"]) == box), None)
        assert first is not None
        return first == self.name

    def open(self, box: int) -> Step:
        assert self.left > 0 and 1 <= box <= ROOMS * ROOM_SIZE
        if not (yield from self.claimed(box)):
            self.seen.add(box)
            return False
        opened = self.call("POST", "/open", {"box": box})
        scouted = opened["room"] not in self.kind
        self.kind[opened["room"]] = opened["flag"]
        self.seen.add(box)
        self.left = opened["budget_left"]
        yield
        if scouted and self.talks:
            report = {"room": opened["room"], "box": box, "flag": opened["flag"]}
            self.call("POST", "/msg", {"text": json.dumps(report, separators=COMPACT)})
            yield
        return True

    def infer(self) -> None:
        flagged = sum(self.kind.values())
        if flagged == TREASURE_ROOMS or len(self.kind) - flagged == ROOMS - TREASURE_ROOMS:
            for room in ALL_ROOMS:
                self.kind.setdefault(room, flagged != TREASURE_ROOMS)

    def sweep_box(self, rule: tuple[int, int, int, int]) -> int | None:
        start, step, slot, slot_step = rule
        for i in range(ROOMS):
            room = (start - 1 + step * i) % ROOMS + 1
            if self.kind.get(room) is False:
                continue
            for j in range(ROOM_SIZE):
                box = (room - 1) * ROOM_SIZE + (slot - 1 + slot_step * j) % ROOM_SIZE + 1
                if box not in self.seen:
                    return box
        return None

    def fill_box(self) -> int | None:
        flagged = [room for room in ALL_ROOMS if self.kind.get(room)]
        for room in flagged:
            if len(flagged) == TREASURE_ROOMS:
                lower = room == flagged[0]
            elif all(other in self.kind for other in range(1, room)):
                lower = True
            elif all(other in self.kind for other in range(room + 1, ROOMS + 1)):
                lower = False
            else:
                continue
            reach = self.budget - (room - 1 if lower else ROOMS - room)
            for box in boxes_of(room)[max(reach, 0) :][::-1]:
                if box not in self.seen:
                    return box
        for room in ALL_ROOMS:
            if room in self.kind:
                continue
            for box in boxes_of(room)[::-1]:
                if box not in self.seen:
                    return box
        return None

    def play_tacit(self, place: int) -> Iterator[None]:
        table = self.rules.get(self.seats, self.rules[1])
        rule = table[place % len(table)]
        for _ in range(ROOMS * ROOM_SIZE):
            if self.left <= 0:
                return
            self.infer()
            box = self.fill_box() if rule == FILL else self.sweep_box(rule)
            if box is None:
                return
            yield from self.open(box)

    def read_reports(self) -> Iterator[None]:
        for message in self.call("GET", "/msgs")["messages"]:
            report = report_of(message["text"])
            if report:
                self.board.setdefault(report["room"], report)
        for room, report in self.board.items():
            self.seen.add(report["box"])
            self.kind.setdefault(room, report["flag"])
        yield

    def share(self) -> list[int]:
        reported = {report["box"] for report in self.board.values()}
        closed = [
            box
            for room in ALL_ROOMS
            if self.board[room]["flag"]
            for box in boxes_of(room)
            if box not in reported
        ]
        lefts = [max(0, self.budget - len(scout_rooms(place, self.seats))) for place in range(self.seats)]
        start = sum(lefts[: self.index])
        return closed[start : start + lefts[self.index]]

    def play_team(self) -> Iterator[None]:
        for room in scout_rooms(self.index, self.seats):
            if self.left <= 0:
                break
            yield from self.open(boxes_of(room)[0])
        for _ in range(self.patience):
            yield from self.read_reports()
            if len(self.board) == ROOMS:
                break
        else:
            self.talks = False
            yield from self.play_tacit(self.index)
            return
        for box in self.share():
            if self.left <= 0:
                return
            yield from self.open(box)


def oracle(seat: Seat) -> Iterator[None]:
    yield from seat.start()
    seat.talks = seat.channel
    yield from seat.play_team() if seat.channel else seat.play_tacit(seat.index)


def at_random(seat: Seat) -> Iterator[None]:
    yield from seat.start()
    seat.claim = False
    for box in random_order(seat.token)[: seat.left]:
        yield from seat.open(box)


def sequential(seat: Seat) -> Iterator[None]:
    yield from seat.start()
    seat.claim = False
    for box in range(1, min(seat.left, ROOMS * ROOM_SIZE) + 1):
        yield from seat.open(box)


POLICIES: dict[str, Callable[[Seat], Iterator[None]]] = {
    "oracle": oracle,
    "random": at_random,
    "sequential": sequential,
}
RULES = tacit_rules(family())


def play(
    instance: dict,
    seat_tokens: dict[str, str],
    roster: dict[str, str] | str = "oracle",
    pick: Callable[[list[str], int], str] | None = None,
    patience: int = PATIENCE,
) -> list[dict]:
    if isinstance(roster, str):
        roster = dict.fromkeys(instance["seats"], roster)
    assert set(roster) == set(instance["seats"]) == set(seat_tokens)
    with tempfile.TemporaryDirectory() as scratch:
        game = world.Crowd(
            instance, {token: seat for seat, token in seat_tokens.items()}, Path(scratch) / "e.jsonl"
        )
        plays = {
            seat: POLICIES[policy](Seat(game, seat_tokens[seat], instance["claim"], RULES, patience))
            for seat, policy in roster.items()
            if policy != "nop"
        }
        limit = len(roster) * (patience + 8 * ROOMS * ROOM_SIZE)
        for turn in range(limit):
            if not plays:
                break
            seats = sorted(plays)
            seat = pick(seats, turn) if pick else seats[turn % len(seats)]
            assert seat in plays
            if next(plays[seat], False) is False:
                del plays[seat]
        assert not plays
        game.handle("POST", "/admin/close", None, {}, "127.0.0.1")
        return worldkit.load_events(game.events_path)


def simulate(
    instance: dict,
    seat_tokens: dict[str, str],
    roster: dict[str, str] | str = "oracle",
    pick: Callable[[list[str], int], str] | None = None,
) -> dict[str, float]:
    return verify.score(play(instance, seat_tokens, roster, pick), instance)


@cache
def best_one_seat(state: tuple[int, ...], left: int) -> float:
    if left == 0:
        return 0.0
    consistent = [
        layout
        for layout in itertools.combinations(ALL_ROOMS, TREASURE_ROOMS)
        if all(kind == -1 or (kind >= 0) == (room in layout) for room, kind in zip(ALL_ROOMS, state, strict=True))
    ]
    assert consistent
    best = 0.0
    for index, kind in enumerate(state):
        if 0 <= kind < ROOM_SIZE:
            best = max(best, 1 + best_one_seat(state[:index] + (kind + 1,) + state[index + 1 :], left - 1))
        if kind != -1:
            continue
        chance = sum(ALL_ROOMS[index] in layout for layout in consistent) / len(consistent)
        found = state[:index] + (1,) + state[index + 1 :]
        empty = state[:index] + (-2,) + state[index + 1 :]
        value = chance * (1 + best_one_seat(found, left - 1)) if chance > 0 else 0.0
        value += (1 - chance) * best_one_seat(empty, left - 1) if chance < 1 else 0.0
        best = max(best, value)
    return best


def reference(config: dict) -> dict[str, dict]:
    every = len(layouts(config["layout_seed"]))
    table: dict[str, dict] = {}
    for condition, spec in config["conditions"].items():
        table[condition] = {}
        for policy in REFERENCE_POLICIES:
            scores = [
                round(simulate(instance, tokens(config, instance), policy)["reward"], 6)
                for instance in instances(config, condition, every)
            ]
            shipped = scores[: config["instances"]]
            table[condition][policy] = {
                "mean": round(sum(shipped) / len(shipped), 6),
                "min": min(shipped),
                "max": max(shipped),
                "all_layouts": round(sum(scores) / len(scores), 6),
            }
        if len(spec["seats"]) == 1:
            best = best_one_seat((-1,) * ROOMS, spec["budget"]) / (TREASURE_ROOMS * ROOM_SIZE)
            assert abs(table[condition]["oracle"]["all_layouts"] - best) < 1e-6, (condition, best)
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
    print("family.toml [reference] differs: run tasks/crowd/reference.py --write", file=sys.stderr)
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
