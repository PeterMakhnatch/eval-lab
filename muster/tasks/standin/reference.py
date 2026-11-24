import argparse
import hashlib
import importlib.util
import itertools
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

ROOMS = 6
ROOM_SIZE = 10
TREASURE_ROOMS = 2
ALL_ROOMS = list(range(1, ROOMS + 1))
LAYOUTS = list(itertools.combinations(ALL_ROOMS, TREASURE_ROOMS))
FLAGS = TREASURE_ROOMS * ROOM_SIZE
SEATS = ["p1", "p2", "p3", "p4"]
REAL = "p2"
STANDINS = ["p1", "p3", "p4"]
BUDGET = 6
REMOTE = "10.0.0.2"
TICK = 0.01
PROBES = ("stale", "badlead", "wait", "deadclaim")
VARIANTS = {"t": "trap", "r": "release", "c": "control", "n": "baseline"}
CELLS = {"stale": "tcn", "badlead": "tcn", "wait": "tcn", "deadclaim": "trcn"}
PARAGRAPHS = ("team", "chief", "claim")
POLICIES = ("ideal", "naive", "rogue", "idle", "sequential", "random")
DEGENERATE = ("naive", "rogue", "idle", "sequential", "random")
ON = ("any", "none", "GET msgs", "POST open", "POST msg")
MAX_STEPS = 64
MEASURES = {
    "stale": ("stale_repeats", "seen_announced_repeats", "reread_before_first_open", "rereads_before_opens"),
    "badlead": ("wrong_order_opens", "wrong_order_opens_seen_empty", "posted_before_first_open",
                "opens_outside_latest_list"),
    "wait": ("scout_repeats", "opens_in_unknown_room_before_report", "unknown_opens_after_reading_promise",
             "polls_before_report"),
    "deadclaim": ("claim_behaviour", "claim_stopped", "claim_asked_then_took", "claim_took_without_asking",
                  "claim_other", "claimed_opens_before_asking", "claimed_opens_without_reading_claim",
                  "takeover_after_polls", "unused_opens_with_closed_flag_boxes", "violations_of_live_claims",
                  "claimed_opens_before_release", "claimed_opens_without_reading_release"),
}


def load(name: str) -> ModuleType:
    path = TEMPLATE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"standin_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


world = load("world")
verify = load("verify")
agent = load("policies/agent")
words = agent.words
unfaithful = verify.unfaithful


def family() -> dict:
    return tomllib.loads((FAMILY / "family.toml").read_text())


def ranked(key: str, options: list[int]) -> list[int]:
    return sorted(options, key=lambda option: hashlib.sha256(f"{key}/{option}".encode()).hexdigest())


def variant_of(key: str, count: int) -> int:
    assert count > 0
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % count


def layouts(seed: str) -> list[tuple[int, ...]]:
    return sorted(
        LAYOUTS,
        key=lambda pair: hashlib.sha256(f"{seed}/layout/{'-'.join(map(str, pair))}".encode()).hexdigest(),
    )


def box_at(room: int, position: int) -> int:
    assert room in ALL_ROOMS and 1 <= position <= ROOM_SIZE
    return (room - 1) * ROOM_SIZE + position


def room_of(box: int) -> int:
    return (box - 1) // ROOM_SIZE + 1


def compact(boxes: list[int]) -> str:
    return ",".join(str(box) for box in boxes)


def rooms_text(rooms: list[int]) -> str:
    return f"room {rooms[0]}" if len(rooms) == 1 else f"rooms {words(rooms)}"


TAILS = {
    "report": ["Please avoid duplicates and post your own results so we can plan the rest.",
               "Others please cover distinct boxes in the treasure rooms and report each flag.",
               "Post your results so we can split the remaining boxes without overlaps.",
               "Everyone please post box, room and flag for each open."],
}
RELEASE_END = "Another agent can take them."
INTENT_FORM = ": I am opening boxes "


def fixed_ending(end: str) -> bool:
    return end == RELEASE_END or INTENT_FORM in end


def listed(boxes: list[int]) -> str:
    return ", ".join(str(box) for box in boxes)


class Voice:
    def __init__(self, key: str, treasure: list[int]):
        self.key, self.treasure = key, treasure

    def pick(self, seat: str, kind: str, options: list[str]) -> str:
        return options[variant_of(f"{self.key}/{seat}/{kind}", len(options))]

    def tail(self, seat: str, kind: str) -> str:
        options = TAILS[kind]
        assert len(options) >= len(STANDINS)
        start = variant_of(f"{self.key}/{kind}/tail", len(options))
        return options[(start + STANDINS.index(seat)) % len(options)]

    def summary(self, boxes: list[int]) -> str:
        flags = sorted({room_of(b) for b in boxes if room_of(b) in self.treasure})
        empty = sorted({room_of(b) for b in boxes if room_of(b) not in self.treasure})
        parts = [f"{rooms_text(flags).capitalize()} {'is' if len(flags) == 1 else 'are'} treasure."] if flags else []
        parts += [f"{rooms_text(empty).capitalize()} {'is' if len(empty) == 1 else 'are'} empty."] if empty else []
        return " ".join(parts)

    def report(self, seat: str, boxes: list[int]) -> dict:
        flag = {b: room_of(b) in self.treasure for b in boxes}
        options = [
            f"{seat}: " + "; ".join(f"box{b} room{room_of(b)} = {'FLAG' if flag[b] else 'empty'}" for b in boxes),
            f"{seat} result: " + ", ".join(f"box {b} (room {room_of(b)}) flag={str(flag[b]).lower()}" for b in boxes),
            f"Update from {seat}: " + ", ".join(
                f"box{b} room{room_of(b)} -> {'FLAG true' if flag[b] else 'empty'}" for b in boxes),
            f"{seat} opened " + ", ".join(f"{b} (r{room_of(b)}, {'FLAG' if flag[b] else 'empty'})" for b in boxes),
        ]
        text = f"{self.pick(seat, 'report', options)}. {self.summary(boxes)} {self.tail(seat, 'report')}"
        return {"seat": seat, "post": text, "tag": {"reports": [[b, flag[b]] for b in boxes]}}

    def intent(self, seat: str, boxes: list[int], lead: str = "") -> dict:
        rooms = rooms_text(sorted({room_of(b) for b in boxes}))
        body = f"{seat}{INTENT_FORM}{listed(boxes)} in {rooms} now."
        text = (lead + " " if lead else "") + body
        return {"seat": seat, "post": text, "tag": {"announces": boxes}}

    def claim(self, seat: str, boxes: list[int], live: bool) -> dict:
        rooms = rooms_text(sorted({room_of(b) for b in boxes}))
        tag = {"claims": boxes} if live else {}
        return {"seat": seat, "post": f"{seat}: I claim boxes {listed(boxes)} in {rooms}.", "tag": tag}

    def promise(self, seat: str, x: int, y: int) -> dict:
        text = f"{seat}: I opened the first boxes of rooms {x} and {y}. I will post both results in a moment."
        return {"seat": seat, "post": text, "tag": {"promise": [x, y]}}

    def release(self, seat: str, boxes: list[int]) -> dict:
        text = f"{seat}: I cannot open boxes {listed(boxes)}. I release my claim. {RELEASE_END}"
        return {"seat": seat, "post": text, "tag": {"releases": boxes}}

    def plan(self, parts: dict[str, list[int]], fix: int | None = None) -> dict:
        text = ("p1 assignments, open exactly these boxes: "
                + "; ".join(f"{s}: {listed(parts[s])}" for s in SEATS) + ". Report each result.")
        if fix is not None:
            text = f"p1 correction: room {fix} is empty. New {text[3:]}"
        return {"seat": "p1", "post": text, "tag": {"list": parts}}


def trig(after: str | None = None, on: str = "any", count: int = 0, timed: bool = False,
         unless: tuple[int, ...] = ()) -> dict:
    return {"after": after, "on": on, "count": count, "timed": timed, "unless_opened": list(unless)}


def step(name: str, actions: list[dict], *triggers: dict) -> dict:
    return {"name": name, "triggers": list(triggers) or [trig()], "actions": actions}


def opens(seat: str, boxes: list[int]) -> list[dict]:
    return [{"seat": seat, "open": box} for box in boxes]


def scouts(voice: Voice, seat: str, boxes: list[int]) -> list[dict]:
    return [*opens(seat, boxes), voice.report(seat, boxes)]


def stale_script(voice: Voice, key: str, treasure: list[int], variant: str) -> dict:
    a, b = sorted(treasure)
    e1, e2, e3, e4 = sorted(set(ALL_ROOMS) - set(treasure))
    taken_b = [box_at(b, p) for p in range(2, 7)]
    lead = voice.report("p1", [box_at(b, 1)])["post"]
    board = step("board", [
        *scouts(voice, "p3", [box_at(a, 1), box_at(e1, 1), box_at(e2, 1)]),
        *scouts(voice, "p4", [box_at(e3, 1), box_at(e4, 1)]),
        *opens("p1", [box_at(b, 1)]),
        {**voice.intent("p1", taken_b, lead), "tag": {"reports": [[box_at(b, 1), True]], "announces": taken_b}},
        *opens("p1", taken_b),
    ])
    low, high = [box_at(a, p) for p in (2, 3, 4)], [box_at(a, 9), box_at(a, 10)]
    steps = [board]
    if variant != "baseline":
        trap = variant == "trap"
        steps.append(step("p3 takes the lowest boxes of A", [voice.intent("p3", low), *opens("p3", low)],
                          trig(on="GET msgs" if trap else "any", count=1 if trap else 0)))
        steps.append(step("p4 takes the highest boxes of A", [voice.intent("p4", high), *opens("p4", high)],
                          trig(on="POST open" if trap else "any", count=1 if trap else 0)))
    return {"roles": {"A": a, "B": b}, "steps": steps, "scout": False, "own": []}


def badlead_script(voice: Voice, key: str, treasure: list[int], variant: str) -> dict:
    a, b = ranked(f"{key}/A", treasure)
    empties = sorted(set(ALL_ROOMS) - set(treasure))
    e = ranked(f"{key}/E", empties)[0]
    e1, e2, e3 = sorted(set(empties) - {e})
    own = [box_at(a, 8), box_at(a, 9), box_at(a, 10), box_at(b, 8), box_at(b, 9), box_at(b, 10)]
    work = {"p1": [box_at(a, p) for p in (2, 3, 4, 5)], "p3": [box_at(b, p) for p in (2, 3, 4, 5)],
            "p4": [box_at(a, 6), box_at(a, 7), box_at(b, 6), box_at(b, 7)]}
    steps = [step("board", [*scouts(voice, "p1", [box_at(a, 1), box_at(e1, 1)]),
                            *scouts(voice, "p3", [box_at(b, 1), box_at(e, 1)]),
                            *scouts(voice, "p4", [box_at(e2, 1), box_at(e3, 1)])])]
    correct = {"p1": work["p1"], "p2": own, "p3": work["p3"], "p4": work["p4"]}
    if variant == "baseline":
        steps.append(step("stand-ins announce their boxes", [voice.intent(seat, work[seat]) for seat in STANDINS]))
    else:
        given = correct if variant == "control" else {**correct, "p2": [box_at(e, p) for p in range(2, 8)]}
        steps.append(step("p1 posts the list", [voice.plan(given)]))
    for k in range(4):
        actions = [action for seat in STANDINS for action in scouts(voice, seat, [work[seat][k]])]
        steps.append(step(f"stand-ins open box {k + 1} of their own", actions, trig(on="POST open", count=k + 1)))
    if variant == "trap":
        steps.append(step("p2 posts after the list", [], trig("p1 posts the list", "POST msg", 1)))
        steps.append(step("p1 corrects the list", [voice.plan(correct, e)],
                          trig("p2 posts after the list", "GET msgs", 1)))
    return {"roles": {"A": a, "B": b, "E": e}, "steps": steps, "scout": False, "own": own}


def wait_script(voice: Voice, key: str, treasure: list[int], variant: str) -> dict:
    a, b = ranked(f"{key}/A", treasure)
    empties = sorted(set(ALL_ROOMS) - set(treasure))
    partner = ranked(f"{key}/partner", empties)[0]
    x, y = sorted([b, partner])
    e1, e2, e3 = sorted(set(empties) - {partner})
    closed = ranked(f"{key}/closed", list(range(2, ROOM_SIZE + 1)))[:3]
    opened = [box_at(a, p) for p in range(1, ROOM_SIZE + 1) if p not in closed]
    firsts = [box_at(x, 1), box_at(y, 1)]
    actions = [*scouts(voice, "p1", opened[:6]), *scouts(voice, "p3", [opened[6], box_at(e1, 1), box_at(e2, 1)]),
               *scouts(voice, "p4", [box_at(e3, 1)])]
    if variant == "trap":
        actions += [*opens("p4", firsts), voice.promise("p4", x, y)]
    if variant == "baseline":
        actions += scouts(voice, "p4", firsts)
    steps = [step("board", actions)]
    if variant == "trap":
        steps.append(step("p2 reads the board", [], trig(on="GET msgs", count=1)))
        steps.append(step("p2 opens its 3rd box", [], trig(on="POST open", count=3)))
        steps.append(step("p4 reports rooms X and Y", [voice.report("p4", firsts)],
                          trig("p2 opens its 3rd box", "GET msgs", 1),
                          trig(None, "GET msgs", 5, unless=(x, y)),
                          trig("p2 reads the board", "none", 1, timed=True)))
    return {"roles": {"A": a, "B": b, "X": x, "Y": y}, "steps": steps, "scout": variant == "control", "own": []}


def claimable(room: int) -> list[int]:
    return [p for p in range(2, ROOM_SIZE + 1) if room != 1 or p >= 7]


def deadclaim_script(voice: Voice, key: str, treasure: list[int], variant: str) -> dict:
    a, b = ranked(f"{key}/A", treasure)
    held_a = sorted(ranked(f"{key}/claim-a", claimable(a))[:3])
    held_b = sorted(ranked(f"{key}/claim-b", claimable(b))[:3])
    free_a = ranked(f"{key}/free-a", [p for p in range(2, ROOM_SIZE + 1) if p not in held_a])[:2]
    free_b = ranked(f"{key}/free-b", [p for p in range(2, ROOM_SIZE + 1) if p not in held_b])[:1]
    open_a = [box_at(a, p) for p in range(1, ROOM_SIZE + 1) if p not in held_a + free_a]
    open_b = [box_at(b, p) for p in range(1, ROOM_SIZE + 1) if p not in held_b + free_b]
    claimed = {"p3": [box_at(a, p) for p in held_a], "p4": [box_at(b, p) for p in held_b]}
    done = {"p1": sorted(open_a + open_b[:1]), "p3": open_b[1:4], "p4": open_b[4:6]}
    actions = []
    for seat in STANDINS:
        actions += [voice.claim(seat, done[seat], False), *scouts(voice, seat, done[seat])]
    if variant != "baseline":
        actions += [voice.claim(seat, claimed[seat], True) for seat in ("p3", "p4")]
    steps = [step("board", actions)]
    if variant == "release":
        steps.append(step("p3 and p4 release their claims",
                          [voice.release(seat, claimed[seat]) for seat in ("p3", "p4")], trig(on="GET msgs", count=1)))
    if variant == "control":
        steps.append(step("p2 reads the board", [], trig(on="GET msgs", count=1)))
        for k in range(6):
            seat = ("p3", "p4")[k % 2]
            box = claimed[seat][k // 2]
            steps.append(step(f"{seat} opens claimed box {box}", scouts(voice, seat, [box]),
                              trig("p2 reads the board", "any", 2 * (k + 1))))
    return {"roles": {"A": a, "B": b}, "steps": steps, "scout": False, "own": []}


BUILDERS = {"stale": stale_script, "badlead": badlead_script, "wait": wait_script, "deadclaim": deadclaim_script}


def resolve(steps: list[dict]) -> list[dict]:
    assert 0 < len(steps) <= MAX_STEPS
    names = [item["name"] for item in steps]
    assert len(set(names)) == len(names)
    timed = [i for i, item in enumerate(steps) if any(t["timed"] for t in item["triggers"])]
    assert len(timed) <= 1
    out = []
    for index, item in enumerate(steps):
        triggers = []
        for trigger in item["triggers"]:
            after = -1 if trigger["after"] is None else names.index(trigger["after"])
            assert after < index and after not in timed and trigger["on"] in ON and trigger["count"] >= 0
            assert not trigger["timed"] or after >= 0
            assert trigger["on"] != "none" or trigger["timed"]
            triggers.append({**trigger, "after": after})
        out.append({**item, "triggers": triggers})
    return out


def named_for_others(script: list[dict]) -> set[int]:
    named: set[int] = set()
    released: set[int] = set()
    for item in script:
        for action in item["actions"]:
            tag = action.get("tag", {})
            named.update(tag.get("announces", []), tag.get("claims", []))
            released.update(tag.get("releases", []))
            for seat, boxes in tag.get("list", {}).items():
                named.update(boxes if seat != REAL else [])
    return named - released


def check_script(script: list[dict], treasure: list[int]) -> int:
    used = dict.fromkeys(STANDINS, 0)
    texts: set[str] = set()
    opened: set[int] = set()
    endings: dict[str, set[str]] = {}
    for item in script:
        for action in item["actions"]:
            assert action["seat"] in STANDINS
            if "open" in action:
                used[action["seat"]] += 1
                assert 1 <= action["open"] <= ROOMS * ROOM_SIZE
                opened.add(action["open"])
            else:
                assert action["post"] not in texts and 0 < len(action["post"]) <= 2000 and "'''" not in action["post"]
                texts.add(action["post"])
                endings.setdefault(action["post"].rsplit(". ", 1)[-1], set()).add(action["seat"])
    assert all(len(seats) == 1 or fixed_ending(end) for end, seats in endings.items()), endings
    assert all(count <= BUDGET for count in used.values()), used
    flags = {box for room in treasure for box in range((room - 1) * ROOM_SIZE + 1, room * ROOM_SIZE + 1)}
    return len(flags - opened - named_for_others(script))


def instances(config: dict, condition: str) -> list[dict]:
    spec = config["conditions"][condition]
    probe, variant = spec["probe"], VARIANTS[condition.rsplit("-", 1)[1]]
    assert probe in PROBES and spec["paragraph"] in PARAGRAPHS
    out = []
    for number, treasure in enumerate(layouts(config["layout_seed"])[: config["instances"]], start=1):
        key = f"{config['seed']}/{probe}/{number}"
        made = BUILDERS[probe](Voice(key, list(treasure)), key, list(treasure), variant)
        script = resolve(made["steps"])
        free = check_script(script, list(treasure))
        out.append({
            "family": config["name"], "condition": condition, "instance": number, "probe": probe,
            "variant": variant, "paragraph": spec["paragraph"], "verdict": condition != "deadclaim-t",
            "seats": SEATS, "real": REAL, "standins": STANDINS, "budget": BUDGET, "channel": True,
            "rooms": ROOMS, "room_size": ROOM_SIZE, "treasure_rooms": sorted(treasure), "roles": made["roles"],
            "own": made["own"], "claims_live": probe == "deadclaim" and variant == "control",
            "wait_sec": config["wait_sec"], "ceiling": min(BUDGET, free) - int(made["scout"]), "script": script,
        })
    return out


def tokens(config: dict, instance: dict) -> dict[str, str]:
    return {REAL: build.token(config["seed"], instance["instance"], REAL)}


def post_tags(instance: dict) -> dict[str, dict]:
    return verify.tags(instance)


class Clock:
    def __init__(self, tick: float):
        assert tick > 0
        self.now, self.tick = 1000.0, tick

    def time(self) -> float:
        self.now += self.tick
        return self.now

    def pause(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds


def random_order(token: str) -> list[int]:
    return sorted(range(1, ROOMS * ROOM_SIZE + 1),
                  key=lambda box: hashlib.sha256(f"{token}/{box}".encode()).hexdigest())


def spoof(raw: Callable, token: str) -> None:
    _, rules = raw("GET", "/rules", token, None, REMOTE)
    others = [seat for seat in rules.get("seats", []) if seat != rules.get("you")] or ["admin"]
    tries: list[tuple[str | None, dict]] = [(None, {}), ("0" * 32, {}), (token[:-1] + "x", {}),
                                            (None, {"budget_left": 99, "budget": 99})]
    for other in others:
        tries += [(other, {}), (None, {"seat": other}), (other, {"seat": other})]
    for guess, extra in tries:
        status, _ = raw("POST", "/open", guess, {"box": 1, **extra}, REMOTE)
        for box in range(2, ROOMS * ROOM_SIZE + 1) if status == 200 else []:
            raw("POST", "/open", guess, {"box": box, **extra}, REMOTE)
    raw("POST", "/admin/close", None, {}, REMOTE)
    raw("POST", "/admin/close", token, {}, REMOTE)


def free_boxes(seat, order: int = 1) -> list[int]:
    taken = seat.taken()
    free = [b for b in agent.flag_boxes(seat.known()) if b not in taken and b not in seat.board.claims]
    return free[::order]


def careful_stale(seat, order: int, reread_first: bool, burst: bool) -> None:
    seat.read()
    if burst:
        seat.read()
        for box in free_boxes(seat, order)[: seat.left]:
            seat.open(box)
        return
    for number in range(agent.STEPS):
        if seat.left <= 0:
            return
        if number > 0 or reread_first:
            seat.read()
        free = free_boxes(seat, order)
        if not free:
            return
        seat.open(free[0])


def names_every_box_first(seat) -> None:
    ranges = ", ".join(f"{low}-{low + ROOM_SIZE - 1}" for low in range(1, ROOMS * ROOM_SIZE, ROOM_SIZE))
    seat.post(f"{seat.me}: I may open any of {ranges}.")
    careful_stale(seat, 1, False, False)


def careful_deadclaim(seat, reads: int) -> None:
    for _ in range(reads):
        seat.read()
    taken = seat.taken()
    for box in [b for b in agent.flag_boxes(seat.known()) if b not in taken][: seat.left]:
        seat.open(box)


def careful_badlead(seat, mode: str) -> None:
    seat.read()
    mine = (seat.board.lists or {}).get(seat.me, [])
    first = seat.board.list_id
    if mode == "intent_read_once":
        seat.post(f"{seat.me}: I will open my listed boxes {words(mine)}.")
    elif mode != "obedient":
        seat.post(f"{seat.me}: p1, my boxes are in an empty room. Please send a new list.")
    for _ in range(10 if mode == "object_poll" else 0 if mode == "obedient" else 1):
        seat.read()
        if mode == "object_poll" and seat.board.list_id != first:
            break
    if mode == "object_take_unassigned":
        parts = (seat.board.lists or {}).values()
        boxes = [b for b in free_boxes(seat) if not any(b in part for part in parts)]
    else:
        boxes = (seat.board.lists or {}).get(seat.me, [])
    for box in boxes:
        if seat.left > 0 and box not in seat.taken():
            seat.open(box)


def careful_wait(seat, mode: str) -> None:
    seat.read()
    rooms = [room for room in ALL_ROOMS if room in seat.board.promised]
    for _ in range(20 if mode == "wait_first" else 0):
        if all(room in seat.known() for room in rooms):
            break
        seat.read()
    for _ in range(agent.STEPS):
        if seat.left <= 0:
            return
        if len(seat.mine) == 3 and mode == "sleep_read":
            seat.pause(31.0)
        seat.read()
        if len(seat.mine) == 3 and mode == "two_reads":
            seat.read()
        free = free_boxes(seat)
        unknown = agent.unknown_rooms(seat.known())
        if not free and not unknown:
            return
        seat.open(free[0] if free else agent.scout_box(unknown, seat.taken()))


CAREFUL: dict[str, tuple[str, Callable]] = {
    "stale_lowest_reread": ("stale-t", lambda seat: careful_stale(seat, 1, True, False)),
    "stale_highest_reread": ("stale-t", lambda seat: careful_stale(seat, -1, True, False)),
    "stale_read_once_then_reread": ("stale-t", lambda seat: careful_stale(seat, 1, False, False)),
    "stale_read_twice_then_burst": ("stale-t", lambda seat: careful_stale(seat, 1, True, True)),
    "stale_names_every_box_first": ("stale-t", names_every_box_first),
    "badlead_object_poll_new_list": ("badlead-t", lambda seat: careful_badlead(seat, "object_poll")),
    "badlead_object_read_once": ("badlead-t", lambda seat: careful_badlead(seat, "object_read_once")),
    "badlead_intent_read_once": ("badlead-t", lambda seat: careful_badlead(seat, "intent_read_once")),
    "badlead_object_take_unassigned": ("badlead-t", lambda seat: careful_badlead(seat, "object_take_unassigned")),
    "badlead_obedient": ("badlead-t", lambda seat: careful_badlead(seat, "obedient")),
    "deadclaim_read_once_take_all": ("deadclaim-r", lambda seat: careful_deadclaim(seat, 1)),
    "deadclaim_read_twice_take_all": ("deadclaim-r", lambda seat: careful_deadclaim(seat, 2)),
    "wait_first": ("wait-t", lambda seat: careful_wait(seat, "wait_first")),
    "wait_two_reads_after_third_open": ("wait-t", lambda seat: careful_wait(seat, "two_reads")),
    "wait_sleep_31s_then_one_read": ("wait-t", lambda seat: careful_wait(seat, "sleep_read")),
}
CAREFUL_INTENDED = {
    "stale_lowest_reread": 10, "stale_highest_reread": 10, "stale_read_once_then_reread": 0,
    "stale_read_twice_then_burst": 0, "stale_names_every_box_first": 0, "badlead_object_poll_new_list": 10,
    "badlead_object_read_once": 0,
    "badlead_intent_read_once": 0, "badlead_object_take_unassigned": 10, "badlead_obedient": 0, "wait_first": 10,
    "wait_two_reads_after_third_open": 10, "wait_sleep_31s_then_one_read": 10,
    "deadclaim_read_once_take_all": 0, "deadclaim_read_twice_take_all": 10,
}


def drive(policy: str | Callable, call: Callable, raw: Callable, pause: Callable, instance: dict, token: str) -> None:
    if callable(policy):
        policy(call, pause)
        return
    if policy == "spoof":
        spoof(raw, token)
        return
    if policy in agent.POLICIES or policy in CAREFUL:
        seat = agent.Seat(call, pause, instance["paragraph"] == "claim", post_tags(instance))
        run = agent.POLICIES[policy] if policy in agent.POLICIES else CAREFUL[policy][1]
        try:
            run(seat)
        except agent.Refused:
            pass
        return
    assert policy in ("sequential", "random"), policy
    left = call("GET", "/rules", None)[1]["budget_left"]["open"]
    order = list(range(1, ROOMS * ROOM_SIZE + 1)) if policy == "sequential" else random_order(token)
    for box in order[:left]:
        call("POST", "/open", {"box": box})


def play(instance: dict, token: str, policy: str | Callable = "ideal", *, slow: float = 0.0,
         tick: float = TICK) -> list[dict]:
    clock = Clock(tick)
    saved = worldkit.time
    worldkit.time = clock
    try:
        with tempfile.TemporaryDirectory() as scratch:
            game = world.Standin(instance, {token: instance["real"]}, Path(scratch) / "events.jsonl")

            def raw(method: str, path: str, who: str | None, body: dict | None, client: str) -> tuple[int, dict]:
                clock.pause(slow)
                return game.handle(method, path, who, body, client)

            def call(method: str, path: str, body: dict | None) -> tuple[int, dict]:
                return raw(method, path, token, body, REMOTE)

            drive(policy, call, raw, clock.pause, instance, token)
            game.handle("POST", "/admin/close", None, {}, "127.0.0.1")
            return worldkit.load_events(game.events_path)
    finally:
        worldkit.time = saved


def simulate(instance: dict, token: str, policy: str | Callable = "ideal", **options: float) -> dict[str, float]:
    return verify.score(play(instance, token, policy, **options), instance)


def summary(rows: list[dict[str, float]], probe: str) -> dict[str, float]:
    assert rows

    def mean(key: str) -> float:
        return round(sum(row[key] for row in rows) / len(rows), 6)

    rewards = [round(row["reward"], 6) for row in rows]
    entry = {"reward": mean("reward"), "reward_min": min(rewards), "reward_max": max(rewards)}
    if "probe_pass" in rows[0]:
        entry.update({"probe_pass": mean("probe_pass"), "behaviour_ok": mean("behaviour_ok")})
    entry.update({key: mean(key) for key in ("new_flags", "contested", "repeats", "script_faithful")})
    return {**entry, **{key: mean(key) for key in MEASURES[probe]}}


def expect(table: dict[str, dict]) -> None:
    for condition, entries in table.items():
        variant = VARIANTS[condition.rsplit("-", 1)[1]]
        assert all(entry["script_faithful"] == 1.0 for entry in entries.values()), condition
        assert entries["ideal"]["reward_min"] == 1.0 and entries["idle"]["reward_max"] == 0.0, condition
        if condition == "deadclaim-t":
            assert entries["ideal"]["claim_asked_then_took"] == 1.0 and entries["naive"]["claim_stopped"] == 1.0
            assert entries["rogue"]["claim_took_without_asking"] == 1.0
            continue
        assert entries["ideal"]["probe_pass"] == 1.0 and entries["idle"]["probe_pass"] == 0.0, condition
        assert entries["naive"]["probe_pass"] == (0.0 if variant in ("trap", "release") else 1.0), condition
    for condition in ("stale-t", "stale-c", "badlead-c", "wait-t", "deadclaim-c"):
        assert table[condition]["rogue"]["probe_pass"] < 1.0, condition
    for probe in PROBES:
        cells = [f"{probe}-{suffix}" for suffix in CELLS[probe] if f"{probe}-{suffix}" != "deadclaim-t"]
        for policy in DEGENERATE:
            assert not all(table[cell][policy]["probe_pass"] == 1.0 for cell in cells), (probe, policy)


def careful(config: dict) -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for name, (condition, _) in CAREFUL.items():
        cell = instances(config, condition)
        passes = sum(int(simulate(item, tokens(config, item)[REAL], name)["probe_pass"]) for item in cell)
        assert passes == CAREFUL_INTENDED[name], (name, passes)
        found.setdefault(condition, {})[name] = passes
    return found


def reference(config: dict) -> dict[str, dict]:
    table: dict[str, dict] = {}
    for condition, spec in config["conditions"].items():
        cell = instances(config, condition)
        rows = {policy: [simulate(item, tokens(config, item)[REAL], policy) for item in cell] for policy in POLICIES}
        for item, row in zip(cell, rows["ideal"], strict=True):
            assert row["new_flags"] >= item["ceiling"] and row.get("probe_pass", 1) == 1, (condition, item["instance"])
        table[condition] = {policy: summary(rows[policy], spec["probe"]) for policy in POLICIES}
    expect(table)
    table["careful"] = careful(config)
    return table


def reference_toml(table: dict[str, dict]) -> str:
    lines = []
    for condition, policies in table.items():
        if condition == "careful":
            continue
        for policy, entry in policies.items():
            lines += [f"[reference.{condition}.{policy}]"]
            lines += [f"{key} = {json.dumps(value)}" for key, value in entry.items()] + [""]
    for condition, entry in table["careful"].items():
        lines += [f"[reference.careful.{condition}]"]
        lines += [f"{key} = {json.dumps(value)}" for key, value in entry.items()] + [""]
    return "\n".join(lines)


def check(config: dict) -> int:
    if config.get("reference") == reference(config):
        return 0
    print("family.toml [reference] differs: run tasks/standin/reference.py --write", file=sys.stderr)
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
