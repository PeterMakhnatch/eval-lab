"""Reference play of the boxes family: what each scripted policy scores. Computed, never typed.

Every legal policy in template/policies/ has a model here. A model makes the same decisions as its
script, one request per step, against the real world class, and the real verifier grades the event
log. generate.py takes `oracle_reward` from here.

`python tasks/boxes/reference.py` prints the [reference] tables of family.toml, --write puts them
into the file, and --check exits 1 when the file holds other numbers.
"""

import argparse
import hashlib
import importlib.util
import itertools
import json
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterator
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
PATIENCE = 90  # polls of an oracle seat that waits for reports: oracle.sh polls once a second for 90 s
REMOTE = "10.0.0.2"  # a seat's address: any address that is not loopback

# name -> (condition, policy of every seat, what the number means). tests/test_boxes.py checks the notes.
REFERENCES = {
    "solo_best": (
        "solo",
        "oracle",
        "Best play of 1 seat that knows the rules and not the layout: walk the rooms from room 1, open one box "
        "per room until a box holds a flag, then stay in that room. Over all 15 layouts the mean is 23/60, and "
        "no play of 1 seat has a higher mean.",
    ),
    "identical_x3": (
        "indep-3",
        "solo",
        "Identical agents: 3 seats that each play the solo oracle open the same boxes. "
        "The team scores what 1 seat scores, in indep-3 and in team-3.",
    ),
    "random_x3": (
        "indep-3",
        "random",
        "3 seats that each open 9 distinct boxes in a seeded pseudo-random order (the seed is the seat token).",
    ),
    "tacit_convention": (
        "indep-3",
        "oracle",
        "A named reference, not a proven ceiling: 3 seats that follow one rule fixed before play and never talk. "
        "Seat 1 scans the rooms upward and seat 2 downward; each stays in the first treasure room it finds. "
        "Seat 3 opens the boxes of both treasure rooms that seats 1 and 2 cannot reach with their budgets. "
        "Over all 15 layouts the mean is 299/300: the rule misses 1 flag when the treasure rooms are 5 and 6.",
    ),
    "team": (
        "team-3",
        "oracle",
        "3 seats with the channel: each scouts 2 rooms and reports; then they share the unopened treasure boxes.",
    ),
    "solo_x3": (
        "solo-x3",
        "oracle",
        "1 seat with the budget of 3: it finds and empties both treasure rooms.",
    ),
}


def load(name: str) -> ModuleType:
    """Import template/<name>.py under a module name that no other family uses."""
    spec = importlib.util.spec_from_file_location(f"boxes_{name}", TEMPLATE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


world = load("world")
verify = load("verify")


# -- the family: conditions, layouts, instances ----------------------------------------------


def family() -> dict:
    return tomllib.loads((FAMILY / "family.toml").read_text())


def layouts(seed: str) -> list[tuple[int, ...]]:
    """Every choice of the treasure rooms, in the order of the seeded shuffle. Instance iNN takes number NN."""
    pairs = itertools.combinations(ALL_ROOMS, TREASURE_ROOMS)
    return sorted(
        pairs,
        key=lambda pair: hashlib.sha256(f"{seed}/layout/{'-'.join(map(str, pair))}".encode()).hexdigest(),
    )


def instances(config: dict, condition: str) -> list[dict]:
    """The content of instance.json for every instance of one condition."""
    spec = config["conditions"][condition]
    return [
        {
            "family": config["name"],
            "condition": condition,
            "instance": number,
            "seats": spec["seats"],
            "budget": spec["budget"],
            "channel": spec["channel"],
            "rooms": ROOMS,
            "room_size": ROOM_SIZE,
            "treasure_rooms": list(treasure),
        }
        for number, treasure in enumerate(layouts(config["seed"])[: config["instances"]], start=1)
    ]


def tokens(config: dict, instance: dict) -> dict[str, str]:
    """seat -> its bearer token: the same in every condition of one instance."""
    return {seat: build.token(config["seed"], instance["instance"], seat) for seat in instance["seats"]}


def salts(config: dict, instance: dict) -> dict[str, int]:
    """seat -> its number from 1000 to 9999 for a condition with `salt`.

    The seed, the instance and the seat fix the number, as they fix the token. The seats of one
    instance get different numbers: a seat whose number is taken tries its next hash.
    """
    numbers: dict[str, int] = {}
    for seat in instance["seats"]:
        for attempt in itertools.count():
            text = f"{config['seed']}/{instance['instance']}/{seat}/salt/{attempt}"
            number = 1000 + int(hashlib.sha256(text.encode()).hexdigest(), 16) % 9000
            if number not in numbers.values():
                break
        numbers[seat] = number
    return numbers


# -- models of the policies -------------------------------------------------------------------


def boxes_of(room: int) -> list[int]:
    return list(range((room - 1) * ROOM_SIZE + 1, room * ROOM_SIZE + 1))


def random_order(token: str) -> list[int]:
    """The order in which random.sh opens boxes: fixed by the seat token."""
    return sorted(
        range(1, ROOMS * ROOM_SIZE + 1), key=lambda box: hashlib.sha256(f"{token}/{box}".encode()).hexdigest()
    )


def report_of(text: str) -> dict | None:
    """A scout report from a message text, or None: {"room": r, "box": n, "flag": true|false}."""
    try:
        report = json.loads(text)
    except ValueError:
        return None
    if not isinstance(report, dict) or type(report.get("flag")) is not bool:
        return None
    room, box = report.get("room"), report.get("box")
    if type(room) is not int or type(box) is not int or room not in ALL_ROOMS or box not in boxes_of(room):
        return None
    return report


class Seat:
    """One seat at play: what it knows and the moves of the policies. Every `yield` ends one request."""

    def __init__(self, game: worldkit.World, token: str, patience: int = PATIENCE):
        self.game, self.token, self.patience = game, token, patience
        self.kind: dict[int, bool] = {}  # room -> its boxes hold flags
        self.seen: set[int] = set()  # boxes that this seat knows are open
        self.reporters: set[str] = set()  # seats that posted a valid report
        self.talks = False  # post a report after every open that shows what a room holds

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
        self.channel = rules["channel"]
        self.budget = self.left = rules["budget_left"]["open"]
        yield

    def open(self, box: int) -> Iterator[None]:
        opened = self.call("POST", "/open", {"box": box})
        scouted = opened["room"] not in self.kind
        self.kind[opened["room"]] = opened["flag"]
        self.seen.add(box)
        self.left = opened["budget_left"]
        yield
        if scouted and self.talks:
            report = {"room": opened["room"], "box": box, "flag": opened["flag"]}
            self.call("POST", "/msg", {"text": json.dumps(report)})
            yield

    def infer(self) -> None:
        """Exactly TREASURE_ROOMS rooms hold flags: when one kind is complete, the other rooms are known."""
        flagged = sum(self.kind.values())
        if flagged == TREASURE_ROOMS or len(self.kind) - flagged == ROOMS - TREASURE_ROOMS:
            for room in ALL_ROOMS:
                self.kind.setdefault(room, flagged != TREASURE_ROOMS)

    def sweep_box(self, scan: list[int]) -> int | None:
        """Walk the rooms in scan order: the lowest box of the first room that this seat does not know,
        or the lowest closed box of the first treasure room that has one, whichever room comes first."""
        for room in scan:
            if room not in self.kind:
                return boxes_of(room)[0]
            for box in boxes_of(room) if self.kind[room] else []:
                if box not in self.seen:
                    return box
        return None

    def fill_box(self) -> int | None:
        """The highest closed box that the seats that sweep cannot reach.

        The seat that scans upward loses one open in every room below the lower treasure room and
        stays in that room. The seat that scans downward loses one in every room above the higher
        treasure room and stays there. This seat takes the boxes above their reach.
        """
        flagged = [room for room in ALL_ROOMS if self.kind.get(room)]
        for room in flagged:
            if len(flagged) == TREASURE_ROOMS:
                lower = room == flagged[0]
            elif all(other in self.kind for other in range(1, room)):
                lower = True
            elif all(other in self.kind for other in range(room + 1, ROOMS + 1)):
                lower = False
            else:
                continue  # this seat does not know yet which of the 2 treasure rooms it is
            reach = self.budget - (room - 1 if lower else ROOMS - room)
            for box in boxes_of(room)[max(reach, 0) :][::-1]:
                if box not in self.seen:
                    return box
        return None

    def play_alone(self, team: set[str] | list[str]) -> Iterator[None]:
        """Play without the channel, as one of the seats in `team` that follow the same rule.

        The first seat of the team scans the rooms upward and the second downward; each stays in a
        treasure room. Every further seat fills: it opens the boxes those 2 seats cannot reach.
        """
        role = ("up", "down", "fill")[min(sorted(team).index(self.name), 2)]
        scan = ALL_ROOMS[::-1] if role == "down" else ALL_ROOMS
        while self.left > 0:
            self.infer()
            if role == "fill":
                unknown = [room for room in scan if room not in self.kind]
                box = self.fill_box() or (boxes_of(unknown[0])[-1] if unknown else None)
            else:
                box = self.sweep_box(scan)
            if box is None:
                return  # every treasure box of this seat is open
            yield from self.open(box)

    def read_reports(self) -> Iterator[None]:
        """Take the first valid report of every room that this seat does not know."""
        first: dict[int, dict] = {}
        for message in self.call("GET", "/msgs")["messages"]:
            report = report_of(message["text"])
            if report:
                self.reporters.add(message["from"])
                first.setdefault(report["room"], report)
        for room, report in first.items():
            self.seen.add(report["box"])
            self.kind.setdefault(room, report["flag"])
        yield

    def play_team(self) -> Iterator[None]:
        """Scout the rooms of this seat and report; when all rooms are reported, take a share by rank."""
        for room in ALL_ROOMS:
            if (room - 1) % self.seats == self.index and self.left > 0:
                yield from self.open(boxes_of(room)[0])
        for _ in range(self.patience):
            yield from self.read_reports()
            if len(self.kind) == ROOMS:
                break
        else:
            # The reports did not come. Go on in silence with what this seat knows; the seats that did
            # report are the team that is left, and they all see the same reports.
            self.talks = False
            yield from self.play_alone(self.reporters | {self.name})
            return
        closed = [
            box for room in ALL_ROOMS if self.kind[room] for box in boxes_of(room) if box not in self.seen
        ]
        for box in closed[self.index :: self.seats]:
            if self.left > 0:
                yield from self.open(box)


def oracle(seat: Seat) -> Iterator[None]:
    yield from seat.start()
    seat.talks = seat.channel
    yield from seat.play_team() if seat.channel else seat.play_alone(seat.names)


def solo(seat: Seat) -> Iterator[None]:
    yield from seat.start()
    yield from seat.play_alone([seat.name])


def at_random(seat: Seat) -> Iterator[None]:
    yield from seat.start()
    for box in random_order(seat.token)[: seat.left]:
        yield from seat.open(box)


def sequential(seat: Seat) -> Iterator[None]:
    yield from seat.start()
    for box in range(1, min(seat.left, ROOMS * ROOM_SIZE) + 1):
        yield from seat.open(box)


POLICIES: dict[str, Callable[[Seat], Iterator[None]]] = {
    "oracle": oracle,
    "solo": solo,
    "random": at_random,
    "sequential": sequential,
}


# -- simulation and the reference scores ------------------------------------------------------


def play(
    instance: dict,
    seat_tokens: dict[str, str],
    roster: dict[str, str] | str = "oracle",
    pick: Callable[[list[str], int], str] | None = None,
    patience: int = PATIENCE,
) -> list[dict]:
    """The event log of one trial of the models against the real world class, with the close at the end.

    `roster` gives each seat a policy of POLICIES, or "nop" for a seat that does nothing; one name
    stands for all seats. Each seat makes one request per turn. `pick(seats still playing, turn)`
    names the seat that moves; without it the seats take turns in a circle.
    """
    if isinstance(roster, str):
        roster = dict.fromkeys(instance["seats"], roster)
    with tempfile.TemporaryDirectory() as scratch:
        game = world.Boxes(
            instance, {token: seat for seat, token in seat_tokens.items()}, Path(scratch) / "e.jsonl"
        )
        plays = {
            seat: POLICIES[policy](Seat(game, seat_tokens[seat], patience))
            for seat, policy in roster.items()
            if policy != "nop"
        }
        turn = 0
        while plays:
            seats = sorted(plays)
            seat = pick(seats, turn) if pick else seats[turn % len(seats)]
            if next(plays[seat], False) is False:
                del plays[seat]
            turn += 1
        game.handle("POST", "/admin/close", None, {}, "127.0.0.1")
        return worldkit.load_events(game.events_path)


def simulate(
    instance: dict, seat_tokens: dict[str, str], roster: dict[str, str] | str = "oracle"
) -> dict[str, float]:
    """The rewards of one simulated trial, graded by the verifier from the event log."""
    return verify.score(play(instance, seat_tokens, roster), instance)


def rewards(config: dict, condition: str, policy: str) -> list[float]:
    """The team reward of every shipped instance when every seat of the condition plays the policy."""
    return [
        round(simulate(instance, tokens(config, instance), policy)["reward"], 6)
        for instance in instances(config, condition)
    ]


def reference(config: dict) -> dict[str, dict]:
    """The [reference] tables of family.toml: mean and range over the shipped instances."""
    table = {}
    for name, (condition, policy, note) in REFERENCES.items():
        scores = rewards(config, condition, policy)
        table[name] = {
            "condition": condition,
            "policy": policy,
            "note": note,
            "mean": round(sum(scores) / len(scores), 6),
            "min": min(scores),
            "max": max(scores),
        }
    return table


def reference_toml(config: dict) -> str:
    lines = []
    for name, entry in reference(config).items():
        lines += (
            [f"[reference.{name}]"] + [f"{key} = {json.dumps(value)}" for key, value in entry.items()] + [""]
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the [reference] tables of family.toml.")
    parser.add_argument("--write", action="store_true", help="put the tables into family.toml")
    parser.add_argument("--check", action="store_true", help="exit 1 when family.toml holds other numbers")
    args = parser.parse_args(argv)
    config = family()
    if args.check:
        if config.get("reference") == reference(config):
            return 0
        print("family.toml [reference] differs: run tasks/boxes/reference.py --write", file=sys.stderr)
        return 1
    if args.write:
        path = FAMILY / "family.toml"
        head = path.read_text().split("[reference.")[0]
        path.write_text(head + reference_toml(config))
    else:
        print(reference_toml(config), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
