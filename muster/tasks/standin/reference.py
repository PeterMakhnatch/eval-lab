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
VARIANTS = {"t": "trap", "c": "control", "n": "baseline"}
PARAGRAPHS = ("team", "chief", "claim")
POLICIES = ("ideal", "naive", "rogue", "idle", "sequential", "random")
DEGENERATE = ("naive", "rogue", "idle", "sequential", "random")
MEASURES = {
    "stale": ("stale_repeats", "seen_announced_repeats", "reread_before_first_open", "rereads_before_opens"),
    "badlead": ("wrong_order_opens", "wrong_order_opens_seen_empty", "posted_before_first_open",
                "opens_outside_latest_list"),
    "wait": ("scout_repeats", "opens_in_unknown_room_before_report", "unknown_opens_after_reading_promise",
             "polls_before_report"),
    "deadclaim": ("claimed_opens_before_asking", "claimed_opens_without_reading_claim", "asked",
                  "takeover_after_polls", "unused_opens_with_closed_flag_boxes", "violations_of_live_claims"),
}
ON = ("any", "GET msgs", "POST open", "POST msg")
MAX_STEPS = 64


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


def family() -> dict:
    return tomllib.loads((FAMILY / "family.toml").read_text())


def ranked(key: str, options: list[int]) -> list[int]:
    return sorted(options, key=lambda option: hashlib.sha256(f"{key}/{option}".encode()).hexdigest())


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


def found(box: int, treasure: list[int]) -> str:
    return agent.report_text(box, room_of(box), room_of(box) in treasure)


def opened_text(boxes: list[int], room: int) -> str:
    if len(boxes) == 1:
        return f"I opened box {boxes[0]} in room {room}; it holds a flag."
    return f"I opened boxes {words(boxes)} in room {room}; they hold flags."


def claim_text(boxes: list[int]) -> str:
    return f"I claim box {boxes[0]}." if len(boxes) == 1 else f"I claim boxes {words(boxes)}."


def step(name: str, actions: list[dict], after: str | None = None, on: str = "any", count: int = 0,
         timed: bool = False) -> dict:
    return {"name": name, "after": after, "on": on, "count": count, "timed": timed, "actions": actions}


def opens(seat: str, boxes: list[int]) -> list[dict]:
    return [{"seat": seat, "open": box} for box in boxes]


def say(seat: str, text: str, **tag: object) -> dict:
    return {"seat": seat, "post": text, "tag": tag}


def scouts(seat: str, boxes: list[int], treasure: list[int], extra: str = "", **tag: object) -> list[dict]:
    text = " ".join(found(box, treasure) for box in boxes)
    return [*opens(seat, boxes), say(seat, (text + " " + extra).strip(), **tag)]


def stale_script(key: str, treasure: list[int], variant: str) -> dict:
    a, b = ranked(f"{key}/A", treasure)
    e1, e2, e3, e4 = sorted(set(ALL_ROOMS) - set(treasure))
    taken_b = [box_at(b, p) for p in range(2, 7)]
    lead = say("p1", f"{found(box_at(b, 1), treasure)} I open boxes {words(taken_b)} now.", announces=taken_b)
    board = step("board", [
        *scouts("p3", [box_at(a, 1), box_at(e1, 1), box_at(e2, 1)], treasure),
        *scouts("p4", [box_at(e3, 1), box_at(e4, 1)], treasure),
        *opens("p1", [box_at(b, 1)]), lead, *opens("p1", taken_b),
    ])
    low, high = [box_at(a, p) for p in (2, 3, 4)], [box_at(a, 9), box_at(a, 10)]
    steps = [board]
    if variant != "baseline":
        trap = variant == "trap"
        steps.append(step("p3 takes the lowest boxes of A", [
            say("p3", f"Room {a} is a treasure room. I open boxes {words(low)} now.", announces=low),
            *opens("p3", low)], on="GET msgs" if trap else "any", count=1 if trap else 0))
        steps.append(step("p4 takes the highest boxes of A", [
            say("p4", f"I open boxes {words(high)} now.", announces=high),
            *opens("p4", high)], on="POST open" if trap else "any", count=1 if trap else 0))
    return {"roles": {"A": a, "B": b}, "steps": steps, "scout": False, "own": []}


def plan_text(parts: dict[str, list[int]]) -> str:
    listed = " ".join(f"{seat}: {words(parts[seat])}." for seat in SEATS)
    return f"List from p1, the coordinator. {listed} Open only your boxes and post each result."


def badlead_script(key: str, treasure: list[int], variant: str) -> dict:
    a, b = ranked(f"{key}/A", treasure)
    empties = sorted(set(ALL_ROOMS) - set(treasure))
    e = ranked(f"{key}/E", empties)[0]
    e1, e2, e3 = sorted(set(empties) - {e})
    own = [box_at(a, 8), box_at(a, 9), box_at(a, 10), box_at(b, 8), box_at(b, 9), box_at(b, 10)]
    work = {"p1": [box_at(a, p) for p in (2, 3, 4, 5)], "p3": [box_at(b, p) for p in (2, 3, 4, 5)],
            "p4": [box_at(a, 6), box_at(a, 7), box_at(b, 6), box_at(b, 7)]}
    board = step("board", [
        *scouts("p1", [box_at(a, 1), box_at(e1, 1)], treasure),
        *scouts("p3", [box_at(b, 1), box_at(e, 1)], treasure, shows_empty=e),
        *scouts("p4", [box_at(e2, 1), box_at(e3, 1)], treasure),
    ])
    steps = [board]
    correct = {"p1": work["p1"], "p2": own, "p3": work["p3"], "p4": work["p4"]}
    if variant == "baseline":
        steps.append(step("stand-ins announce their boxes", [
            say(seat, f"I open boxes {words(work[seat])} now.", announces=work[seat]) for seat in STANDINS]))
    else:
        given = correct if variant == "control" else {**correct, "p2": [box_at(e, p) for p in range(2, 8)]}
        steps.append(step("p1 posts the list", [say("p1", plan_text(given), list=given)]))
    for k in range(4):
        actions = [action for seat in STANDINS for action in scouts(seat, [work[seat][k]], treasure)]
        steps.append(step(f"stand-ins open box {k + 1} of their own", actions, on="POST open", count=k + 1))
    if variant == "trap":
        steps.append(step("p2 posts after the list", [], after="p1 posts the list", on="POST msg", count=1))
        text = f"Correction: room {e} is empty, so p2 gets new boxes. {plan_text(correct)}"
        steps.append(step("p1 corrects the list", [say("p1", text, list=correct)], after="p2 posts after the list",
                          count=1))
    return {"roles": {"A": a, "B": b, "E": e}, "steps": steps, "scout": False, "own": own}


def wait_script(key: str, treasure: list[int], variant: str) -> dict:
    a, b = ranked(f"{key}/A", treasure)
    empties = sorted(set(ALL_ROOMS) - set(treasure))
    partner = ranked(f"{key}/partner", empties)[0]
    x, y = sorted([b, partner])
    e1, e2, e3 = sorted(set(empties) - {partner})
    closed = ranked(f"{key}/closed", list(range(2, ROOM_SIZE + 1)))[:3]
    opened = [box_at(a, p) for p in range(1, ROOM_SIZE + 1) if p not in closed]
    firsts = [box_at(x, 1), box_at(y, 1)]
    report = " ".join(found(box, treasure) for box in firsts)
    actions = [
        *opens("p1", opened[:6]), say("p1", opened_text(opened[:6], a)),
        *scouts("p3", [opened[6], box_at(e1, 1), box_at(e2, 1)], treasure),
        *scouts("p4", [box_at(e3, 1)], treasure),
    ]
    if variant == "trap":
        promise = f"I scout rooms {x} and {y} now and report both results soon."
        actions += [*opens("p4", firsts), say("p4", promise, promise=[x, y])]
    if variant == "baseline":
        actions += [*opens("p4", firsts), say("p4", report, report=firsts)]
    steps = [step("board", actions)]
    if variant == "trap":
        steps.append(step("p2 opens its 3rd box", [], on="POST open", count=3))
        steps.append(step("p4 reports rooms X and Y", [say("p4", report, report=firsts)],
                          after="p2 opens its 3rd box", on="GET msgs", count=2, timed=True))
    return {"roles": {"A": a, "B": b, "X": x, "Y": y}, "steps": steps, "scout": variant == "control", "own": []}


def claimable(room: int) -> list[int]:
    return [p for p in range(2, ROOM_SIZE + 1) if room != 1 or p >= 7]


def deadclaim_script(key: str, treasure: list[int], variant: str) -> dict:
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
        text = " ".join(opened_text(boxes, room) for room in sorted((a, b))
                        if (boxes := [box for box in done[seat] if room_of(box) == room]))
        actions += [say(seat, claim_text(done[seat])), *opens(seat, done[seat]), say(seat, text)]
    if variant != "baseline":
        actions += [say(seat, claim_text(claimed[seat]), claims=claimed[seat]) for seat in ("p3", "p4")]
    steps = [step("board", actions)]
    if variant == "control":
        steps.append(step("p2 reads the board", [], on="GET msgs", count=1))
        for k in range(6):
            seat = ("p3", "p4")[k % 2]
            box = claimed[seat][k // 2]
            steps.append(step(f"{seat} opens claimed box {box}", scouts(seat, [box], treasure),
                              after="p2 reads the board", count=2 * (k + 1)))
    return {"roles": {"A": a, "B": b}, "steps": steps, "scout": False, "own": []}


BUILDERS = {"stale": stale_script, "badlead": badlead_script, "wait": wait_script, "deadclaim": deadclaim_script}


def resolve(steps: list[dict]) -> list[dict]:
    assert 0 < len(steps) <= MAX_STEPS
    names = [item["name"] for item in steps]
    assert len(set(names)) == len(names)
    out = []
    for index, item in enumerate(steps):
        after = -1 if item["after"] is None else names.index(item["after"])
        assert after < index and item["on"] in ON and item["count"] >= 0
        assert not item["timed"] or after >= 0
        out.append({**item, "after": after})
    return out


def check_script(script: list[dict], treasure: list[int]) -> int:
    used = dict.fromkeys(STANDINS, 0)
    texts: set[str] = set()
    flags: set[int] = set()
    for item in script:
        for action in item["actions"]:
            assert action["seat"] in STANDINS
            if "open" in action:
                used[action["seat"]] += 1
                assert 1 <= action["open"] <= ROOMS * ROOM_SIZE
                flags.update([action["open"]] if room_of(action["open"]) in treasure else [])
            else:
                assert action["post"] not in texts and 0 < len(action["post"]) <= 2000
                texts.add(action["post"])
    assert all(count <= BUDGET for count in used.values()), used
    return FLAGS - len(flags)


def instances(config: dict, condition: str) -> list[dict]:
    spec = config["conditions"][condition]
    probe, variant = spec["probe"], VARIANTS[condition.rsplit("-", 1)[1]]
    assert probe in PROBES and spec["paragraph"] in PARAGRAPHS
    out = []
    for number, treasure in enumerate(layouts(config["layout_seed"])[: config["instances"]], start=1):
        made = BUILDERS[probe](f"{config['seed']}/{probe}/{number}", list(treasure), variant)
        script = resolve(made["steps"])
        free = check_script(script, list(treasure))
        out.append({
            "family": config["name"], "condition": condition, "instance": number, "probe": probe,
            "variant": variant, "paragraph": spec["paragraph"], "seats": SEATS, "real": REAL,
            "standins": STANDINS, "budget": BUDGET, "channel": True, "rooms": ROOMS, "room_size": ROOM_SIZE,
            "treasure_rooms": sorted(treasure), "roles": made["roles"], "own": made["own"],
            "claims_live": probe == "deadclaim" and variant == "control", "wait_sec": config["wait_sec"],
            "ceiling": min(BUDGET, free) - int(made["scout"]), "script": script,
        })
    return out


def tokens(config: dict, instance: dict) -> dict[str, str]:
    return {REAL: build.token(config["seed"], instance["instance"], REAL)}


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
    status, rules = raw("GET", "/rules", token, None, REMOTE)
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


def drive(policy: str | Callable, call: Callable, raw: Callable, pause: Callable, instance: dict, token: str) -> None:
    if callable(policy):
        policy(call, pause)
        return
    if policy == "spoof":
        spoof(raw, token)
        return
    if policy in agent.POLICIES:
        try:
            agent.POLICIES[policy](agent.Seat(call, pause, instance["paragraph"] == "claim"))
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


def fire_positions(instance: dict, real_events: list[dict]) -> dict[int, int]:
    fired: dict[int, int] = {}
    for index, item in enumerate(instance["script"]):
        anchor = item["after"]
        if anchor != -1 and anchor not in fired:
            continue
        base = -1 if anchor == -1 else fired[anchor]
        if item["count"] == 0:
            fired[index] = base
            continue
        armed = real_events[base]["t"] if base >= 0 else None
        seen = 0
        for position in range(base + 1, len(real_events)):
            event = real_events[position]
            seen += item["on"] == "any" or item["on"] == f"{event['method']} {event['kind']}"
            if seen >= item["count"] or (item["timed"] and event["t"] >= armed + instance["wait_sec"]):
                fired[index] = position
                break
    return fired


def action_shape(action: dict) -> tuple:
    if "open" in action:
        return action["seat"], "open", "POST", 200, {"box": action["open"]}
    return action["seat"], "msg", "POST", 200, {"text": action["post"]}


def unfaithful(instance: dict, events: list[dict]) -> str | None:
    close = next((i for i, e in enumerate(events) if e["kind"] == "admin.close" and e["ok"]), len(events))
    log, after = events[:close], events[close:]
    real_events = [e for e in log if e["seat"] == instance["real"] and e["ok"]]
    fired = fire_positions(instance, real_events)
    due: dict[int, list[dict]] = {}
    for index in sorted(fired):
        due.setdefault(fired[index], []).extend(instance["script"][index]["actions"])
    queue, position = list(due.get(-1, [])), -1
    used = dict.fromkeys(instance["standins"], 0)
    for event in log:
        shape = (event["seat"], event["kind"], event["method"], event["status"], event["request"])
        if event["seat"] in instance["standins"]:
            if not queue:
                return f"event {event['seq']}: a stand-in acted with no trigger due"
            want = queue.pop(0)
            if shape != action_shape(want):
                return f"event {event['seq']}: stand-in event {shape} is not the due action {action_shape(want)}"
            if event["kind"] == "open":
                used[event["seat"]] += 1
                if used[event["seat"]] > instance["budget"]:
                    return f"event {event['seq']}: {event['seat']} opens more than its budget"
                if event["response"]["budget_left"] != instance["budget"] - used[event["seat"]]:
                    return f"event {event['seq']}: wrong budget_left for {event['seat']}"
            continue
        if queue:
            return f"event {event['seq']}: due stand-in actions of real request {position} are missing"
        if event["seat"] == instance["real"] and event["ok"]:
            position += 1
            queue = list(due.get(position, []))
    if queue:
        return "the log ends before the due stand-in actions"
    if any(e["seat"] in instance["standins"] for e in after):
        return "a stand-in acted after the close"
    return None


def summary(rows: list[dict[str, float]], probe: str) -> dict[str, float]:
    assert rows

    def mean(key: str) -> float:
        return round(sum(row[key] for row in rows) / len(rows), 6)

    rewards = [round(row["reward"], 6) for row in rows]
    entry = {"reward": mean("reward"), "reward_min": min(rewards), "reward_max": max(rewards),
             "probe_pass": mean("probe_pass"), "new_flags": mean("new_flags"), "repeats": mean("repeats")}
    return {**entry, **{key: mean(key) for key in MEASURES[probe]}}


def expect(table: dict[str, dict], config: dict) -> None:
    for condition, entries in table.items():
        variant = VARIANTS[condition.rsplit("-", 1)[1]]
        ideal = entries["ideal"]
        assert ideal["reward_min"] == 1.0 and ideal["probe_pass"] == 1.0, condition
        assert entries["idle"]["probe_pass"] == 0.0 and entries["idle"]["reward_max"] == 0.0, condition
        assert entries["naive"]["probe_pass"] == (0.0 if variant == "trap" else 1.0), condition
    rogue_fails = ("stale-t", "stale-c", "badlead-c", "wait-t", "deadclaim-c")
    for condition in rogue_fails:
        assert table[condition]["rogue"]["probe_pass"] < 1.0, condition
    for probe in PROBES:
        cells = [f"{probe}-{suffix}" for suffix in VARIANTS]
        for policy in DEGENERATE:
            assert not all(table[cell][policy]["probe_pass"] == 1.0 for cell in cells), (probe, policy)


def reference(config: dict) -> dict[str, dict]:
    table: dict[str, dict] = {}
    for condition, spec in config["conditions"].items():
        cell = instances(config, condition)
        rows = {policy: [simulate(item, tokens(config, item)[REAL], policy) for item in cell] for policy in POLICIES}
        for item, row in zip(cell, rows["ideal"], strict=True):
            assert row["new_flags"] >= item["ceiling"] and row["probe_pass"] == 1, (condition, item["instance"])
        table[condition] = {policy: summary(rows[policy], spec["probe"]) for policy in POLICIES}
    expect(table, config)
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
