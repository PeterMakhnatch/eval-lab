import argparse
import hashlib
import importlib.util
import itertools
import json
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from fractions import Fraction
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
LAYOUTS = list(itertools.combinations(ALL_ROOMS, TREASURE_ROOMS))
FLAGS = TREASURE_ROOMS * ROOM_SIZE
PATIENCE = 2000
REMOTE = "10.0.0.2"
REFERENCE_POLICIES = ("oracle", "sequential", "random")
RULES = ("team", "share", "chief", "zones")
COMPACT = (",", ":")
UNKNOWN, EMPTY, TREASURE = 0, 1, 2


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"crowdrules_{name}", TEMPLATE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


world = load("world")
verify = load("verify")


def family() -> dict:
    return tomllib.loads((FAMILY / "family.toml").read_text())


def layouts(seed: str) -> list[tuple[int, ...]]:
    return sorted(
        LAYOUTS,
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
            "channel": True,
            "rule": spec["rule"],
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


def box_at(room: int, position: int) -> int:
    assert room in ALL_ROOMS and 1 <= position <= ROOM_SIZE
    return (room - 1) * ROOM_SIZE + position


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


def list_of(text: str) -> dict | None:
    posted = text_json(text)
    if not isinstance(posted, dict) or not isinstance(posted.get("list"), dict):
        return None
    lists = posted["list"]
    if not all(isinstance(boxes, list) and all(type(box) is int for box in boxes) for boxes in lists.values()):
        return None
    return lists


def zone_boxes(zone: dict, treasure: list[int]) -> list[int]:
    assert len(treasure) == TREASURE_ROOMS
    lower, higher = sorted(treasure)
    fill = [box_at(lower, p) for p in zone["both"] + zone["lower"]]
    return fill + [box_at(higher, p) for p in zone["both"] + zone["higher"]]


def check_zones(config: dict) -> None:
    for k, zones in config["zones"].items():
        seats = [f"p{n}" for n in range(1, int(k) + 1)]
        assert list(zones) == seats
        budget = config["total_budget"] // len(seats)
        for layout in LAYOUTS:
            owners: dict[int, str] = {}
            for seat, zone in zones.items():
                mine = [box_at(room, 1) for room in zone["scout"]] + zone_boxes(zone, list(layout))
                assert len(mine) == budget and len(set(mine)) == budget, (k, seat, layout)
                for box in mine:
                    if (box - 1) // ROOM_SIZE + 1 in layout:
                        assert box not in owners, (k, box, layout)
                        owners[box] = seat
            assert len(owners) == FLAGS, (k, layout)
        scouted = sorted(room for zone in zones.values() for room in zone["scout"])
        assert scouted == ALL_ROOMS, k


def infer(kinds: tuple[int, ...]) -> tuple[int, ...]:
    if kinds.count(TREASURE) == TREASURE_ROOMS:
        return tuple(EMPTY if kind == UNKNOWN else kind for kind in kinds)
    if kinds.count(EMPTY) == ROOMS - TREASURE_ROOMS:
        return tuple(TREASURE if kind == UNKNOWN else kind for kind in kinds)
    return kinds


def share_owner(k: int) -> Callable[[int], int]:
    return lambda box: (box - 1) % k


def position_owner(box: int) -> int:
    position = (box - 1) % ROOM_SIZE
    return position if position < 6 else 6 if position < 8 else 7


def owned(owner: Callable[[int], int], k: int) -> list[list[list[int]]]:
    own = [[[box for box in boxes_of(room) if owner(box) == place] for room in ALL_ROOMS] for place in range(k)]
    assert all(own[place][index] for place in range(k) for index in range(ROOMS))
    assert sum(len(boxes) for place in own for boxes in place) == ROOMS * ROOM_SIZE
    return own


@cache
def consistent(kinds: tuple[int, ...]) -> list[tuple[int, ...]]:
    return [
        layout
        for layout in LAYOUTS
        if all(kind == UNKNOWN or (kind == TREASURE) == (room in layout) for room, kind in zip(ALL_ROOMS, kinds, strict=True))
    ]


def learn(kinds: tuple[int, ...], room: int, flag: bool) -> tuple[int, ...]:
    assert kinds[room - 1] == UNKNOWN
    return infer(kinds[: room - 1] + (TREASURE if flag else EMPTY,) + kinds[room:])


def bump(values: tuple[int, ...], place: int, step: int) -> tuple[int, ...]:
    return values[:place] + (values[place] + step,) + values[place + 1 :]


def share_value(owner: Callable[[int], int], k: int) -> Callable:
    own = owned(owner, k)

    def stop(kinds: tuple[int, ...], left: tuple[int, ...], probes: tuple[int, ...]) -> int:
        closed = [sum(len(own[p][r - 1]) for r in ALL_ROOMS if kinds[r - 1] == TREASURE) - probes[p] for p in range(k)]
        assert all(count >= 0 for count in closed)
        return sum(min(left[p], closed[p]) for p in range(k))

    @cache
    def value(kinds: tuple[int, ...], left: tuple[int, ...], probes: tuple[int, ...]) -> tuple[int, tuple | None]:
        layouts_left = len(consistent(kinds))
        best, action = stop(kinds, left, probes) * layouts_left, None
        for room in ALL_ROOMS:
            if kinds[room - 1] != UNKNOWN:
                continue
            hits = sum(room in layout for layout in consistent(kinds))
            for place in range(k):
                if left[place] == 0:
                    continue
                spent = bump(left, place, -1)
                found = 0
                if hits > 0:
                    found = hits + value(learn(kinds, room, True), spent, bump(probes, place, 1))[0]
                if hits < layouts_left:
                    found += value(learn(kinds, room, False), spent, probes)[0]
                if found > best:
                    best, action = found, (place, room)
        return best, action

    return value


def share_plan(owner: Callable[[int], int], k: int, budget: int) -> tuple[Fraction, dict[str, tuple | None]]:
    value = share_value(owner, k)
    start = ((UNKNOWN,) * ROOMS, (budget,) * k, (0,) * k)
    plan: dict[str, tuple | None] = {}
    frontier = [("k", *start)]
    for _ in range(2 ** (ROOMS + 1)):
        if not frontier:
            break
        node, kinds, left, probes = frontier.pop()
        action = value(kinds, left, probes)[1]
        plan[node] = action
        if action is None:
            continue
        place, room = action
        layouts_left = consistent(kinds)
        spent = bump(left, place, -1)
        if any(room in layout for layout in layouts_left):
            frontier.append((f"{node}{room}T", learn(kinds, room, True), spent, bump(probes, place, 1)))
        if any(room not in layout for layout in layouts_left):
            frontier.append((f"{node}{room}E", learn(kinds, room, False), spent, probes))
    assert not frontier
    return Fraction(value(*start)[0], len(LAYOUTS) * FLAGS), plan


def plan_score(owner: Callable[[int], int], k: int, budget: int, plan: dict, layout: tuple[int, ...]) -> Fraction:
    own = owned(owner, k)
    kinds, left, probes, node, hits = (UNKNOWN,) * ROOMS, [budget] * k, [0] * k, "k", 0
    for _ in range(ROOMS):
        action = plan[node]
        if action is None:
            break
        place, room = action
        flag = room in layout
        left[place] -= 1
        hits += flag
        probes[place] += flag
        kinds = learn(kinds, room, flag)
        node += f"{room}{'T' if flag else 'E'}"
    assert plan[node] is None
    fill = sum(
        min(left[p], sum(len(own[p][r - 1]) for r in ALL_ROOMS if kinds[r - 1] == TREASURE) - probes[p])
        for p in range(k)
    )
    return Fraction(hits + fill, FLAGS)


def walk(board: dict[int, dict], plan: dict) -> tuple | None:
    node = "k"
    for room, report in board.items():
        action = plan[node]
        if action is None:
            break
        if action[1] == room:
            node += f"{room}{'T' if report['flag'] else 'E'}"
    return plan[node]


class Seat:
    def __init__(self, game: worldkit.World, token: str, rule: dict, patience: int):
        assert patience > 0 and rule["rule"] in RULES
        self.game, self.token, self.rule, self.patience = game, token, rule, patience
        self.kind: dict[int, bool] = {}
        self.seen: set[int] = set()
        self.board: dict[int, dict] = {}
        self.messages: list[dict] = []

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        status, reply = self.game.handle(method, path, self.token, body, REMOTE)
        if status != 200:
            raise RuntimeError(f"the world refused {method} {path}: {status} {reply}")
        return reply

    def start(self) -> Iterator[None]:
        rules = self.call("GET", "/rules")
        self.name = rules["you"]
        self.names = rules["seats"]
        self.seats = len(rules["seats"])
        self.index = rules["seats"].index(rules["you"])
        self.budget = self.left = rules["budget_left"]["open"]
        yield

    def post(self, value: dict) -> Iterator[None]:
        self.call("POST", "/msg", {"text": json.dumps(value, separators=COMPACT)})
        yield

    def open(self, box: int, report: bool) -> Iterator[None]:
        assert self.left > 0 and 1 <= box <= ROOMS * ROOM_SIZE
        opened = self.call("POST", "/open", {"box": box})
        self.kind[opened["room"]] = opened["flag"]
        self.seen.add(box)
        self.left = opened["budget_left"]
        yield
        if report:
            yield from self.post({"room": opened["room"], "box": box, "flag": opened["flag"]})

    def read(self) -> Iterator[None]:
        self.messages = self.call("GET", "/msgs")["messages"]
        for message in self.messages:
            report = report_of(message["text"])
            if report:
                self.board.setdefault(report["room"], report)
        for room, report in self.board.items():
            self.seen.add(report["box"])
            self.kind.setdefault(room, report["flag"])
        yield

    def wait_board(self, done: Callable[[], bool]) -> Iterator[None]:
        for _ in range(self.patience):
            yield from self.read()
            if done():
                return

    def board_kinds(self) -> tuple[int, ...]:
        kinds = tuple(
            UNKNOWN if room not in self.board else TREASURE if self.board[room]["flag"] else EMPTY for room in ALL_ROOMS
        )
        return infer(kinds)

    def blocks(self) -> dict[str, list[int]]:
        reported = {report["box"] for report in self.board.values()}
        closed = [
            box for room in ALL_ROOMS if self.board[room]["flag"] for box in boxes_of(room) if box not in reported
        ]
        lefts = [max(0, self.budget - len(scout_rooms(place, self.seats))) for place in range(self.seats)]
        cuts = list(itertools.accumulate(lefts, initial=0))
        return {name: closed[cuts[place] : cuts[place + 1]] for place, name in enumerate(self.names)}


def play_team(seat: Seat) -> Iterator[None]:
    for room in scout_rooms(seat.index, seat.seats):
        if seat.left > 0:
            yield from seat.open(boxes_of(room)[0], True)
    yield from seat.wait_board(lambda: len(seat.board) == ROOMS)
    if len(seat.board) < ROOMS:
        return
    for box in seat.blocks()[seat.name]:
        if seat.left > 0:
            yield from seat.open(box, False)


def play_share(seat: Seat) -> Iterator[None]:
    k, plan = seat.seats, seat.rule["plan"]
    for _ in range(seat.patience):
        yield from seat.read()
        action = walk(seat.board, plan)
        if action is None:
            break
        place, room = action
        if place == seat.index and room not in seat.kind and seat.left > 0:
            yield from seat.open(next(box for box in boxes_of(room) if (box - 1) % k == place), True)
    else:
        return
    kinds = seat.board_kinds()
    for room in ALL_ROOMS:
        for box in boxes_of(room) if kinds[room - 1] == TREASURE else []:
            if (box - 1) % k != seat.index or box in seat.seen or seat.left <= 0:
                continue
            yield from seat.read()
            yield from seat.open(box, True)


def latest_list(seat: Seat, chief: str) -> dict | None:
    found = None
    for message in seat.messages:
        lists = list_of(message["text"]) if message["from"] == chief else None
        found = lists if lists is not None else found
    return found


def play_chief(seat: Seat) -> Iterator[None]:
    chief, final = seat.names[0], False
    if seat.name == chief:
        first = {name: [boxes_of(r)[0] for r in scout_rooms(p, seat.seats)] for p, name in enumerate(seat.names)}
        yield from seat.post({"list": first})
    for _ in range(seat.patience):
        yield from seat.read()
        if seat.name == chief and not final and len(seat.board) == ROOMS:
            final = True
            yield from seat.post({"list": seat.blocks()})
            continue
        lists = latest_list(seat, chief) or {}
        for box in lists.get(seat.name, []):
            if box not in seat.seen and seat.left > 0:
                yield from seat.open(box, True)
        if seat.left <= 0:
            return


def play_zones(seat: Seat) -> Iterator[None]:
    zone = seat.rule["zones"][seat.name]
    for room in zone["scout"]:
        if seat.left > 0:
            yield from seat.open(box_at(room, 1), True)

    def treasure() -> list[int]:
        return sorted(room for room, report in seat.board.items() if report["flag"])

    yield from seat.wait_board(lambda: len(treasure()) == TREASURE_ROOMS)
    if len(treasure()) != TREASURE_ROOMS:
        return
    for box in zone_boxes(zone, treasure()):
        if box not in seat.seen and seat.left > 0:
            yield from seat.open(box, True)


PLAYS = {"team": play_team, "share": play_share, "chief": play_chief, "zones": play_zones}


def oracle(seat: Seat) -> Iterator[None]:
    yield from seat.start()
    yield from PLAYS[seat.rule["rule"]](seat)


def at_random(seat: Seat) -> Iterator[None]:
    yield from seat.start()
    for box in random_order(seat.token)[: seat.left]:
        yield from seat.open(box, False)


def sequential(seat: Seat) -> Iterator[None]:
    yield from seat.start()
    for box in range(1, min(seat.left, ROOMS * ROOM_SIZE) + 1):
        yield from seat.open(box, False)


POLICIES: dict[str, Callable[[Seat], Iterator[None]]] = {
    "oracle": oracle,
    "random": at_random,
    "sequential": sequential,
}


@cache
def share_rule(k: int, budget: int) -> tuple[Fraction, dict]:
    return share_plan(share_owner(k), k, budget)


def rule_data(config: dict, instance: dict) -> dict:
    rule, k = instance["rule"], len(instance["seats"])
    assert rule in RULES
    data: dict = {"rule": rule}
    if rule == "share":
        data["plan"] = share_rule(k, instance["budget"])[1]
    if rule == "zones":
        data["zones"] = config["zones"][str(k)]
    return data


def outside_rule(events: list[dict], instance: dict) -> int:
    data = rule_data(family(), instance)
    seats = sorted(instance["seats"])
    latest: dict = {}
    outside = 0
    for event in worldkit.in_play(events):
        seat = event["seat"]
        if event["kind"] == "msg" and seat == seats[0]:
            lists = list_of(event["request"]["text"])
            latest = latest if lists is None else lists
        if event["kind"] != "open":
            continue
        box = event["response"]["box"]
        if data["rule"] == "share":
            outside += (box - 1) % len(seats) != seats.index(seat)
        if data["rule"] == "zones":
            zone = data["zones"][seat]
            allowed = [box_at(room, 1) for room in zone["scout"]] + zone_boxes(zone, instance["treasure_rooms"])
            outside += box not in allowed
        if data["rule"] == "chief":
            outside += box not in latest.get(seat, [])
    return outside


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
    data = rule_data(family(), instance)
    with tempfile.TemporaryDirectory() as scratch:
        game = world.Crowdrules(
            instance, {token: seat for seat, token in seat_tokens.items()}, Path(scratch) / "e.jsonl"
        )
        plays = {
            seat: POLICIES[policy](Seat(game, seat_tokens[seat], data, patience))
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


def summary(scores: list[float], shipped: int) -> dict:
    assert 0 < shipped <= len(scores)
    return {
        "mean": round(sum(scores[:shipped]) / shipped, 6),
        "min": min(scores[:shipped]),
        "max": max(scores[:shipped]),
        "all_layouts": round(sum(scores) / len(scores), 6),
    }


def position_variant(config: dict) -> dict:
    ceiling, plan = share_plan(position_owner, 8, 3)
    order = layouts(config["layout_seed"])
    scores = [round(float(plan_score(position_owner, 8, 3, plan, layout)), 6) for layout in order]
    entry = summary(scores, config["instances"])
    assert entry["all_layouts"] == round(float(ceiling), 6)
    return entry


def reference(config: dict) -> dict[str, dict]:
    check_zones(config)
    every = len(LAYOUTS)
    table: dict[str, dict] = {}
    for condition, spec in config["conditions"].items():
        table[condition] = {}
        for policy in REFERENCE_POLICIES:
            scores = [
                round(simulate(instance, tokens(config, instance), policy)["reward"], 6)
                for instance in instances(config, condition, every)
            ]
            table[condition][policy] = summary(scores, config["instances"])
        if spec["rule"] == "share":
            ceiling = share_rule(len(spec["seats"]), spec["budget"])[0]
            assert table[condition]["oracle"]["all_layouts"] == round(float(ceiling), 6), condition
        if condition == "share-8":
            table[condition]["position_variant"] = position_variant(config)
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
    print("family.toml [reference] differs: run tasks/crowdrules/reference.py --write", file=sys.stderr)
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
