"""The boxes family: world rules, scoring, the generator, the reference play and the policy scripts.

Nothing here needs Docker. The last part runs the real policy scripts against the world class behind
the kit's HTTP server on loopback; it needs bash, curl, jq and python3.
"""

import http.client
import importlib.util
import itertools
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from functools import cache
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import worldkit

FAMILY = Path(__file__).resolve().parents[1] / "tasks" / "boxes"
BUILD = FAMILY / "build"

_spec = importlib.util.spec_from_file_location("boxes_generate", FAMILY / "generate.py")
generate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generate)
reference = generate.reference
Boxes = reference.world.Boxes
score = reference.verify.score

CONFIG = reference.family()
CONDITIONS = list(CONFIG["conditions"])
NUMBERS = range(1, CONFIG["instances"] + 1)
TOKENS = {"t1": "p1", "t2": "p2", "t3": "p3"}
SEAT = "10.0.0.2"  # where a seat's requests come from: not loopback
MAIN = "127.0.0.1"


def instance(condition: str, number: int = 1, **changes) -> dict:
    """instance.json of one shipped instance, with changes for a test."""
    return {**reference.instances(CONFIG, condition)[number - 1], **changes}


def layout(treasure_rooms: tuple[int, ...], condition: str = "indep-3", **changes) -> dict:
    """An instance with any layout, also one that is not shipped."""
    return instance(condition, treasure_rooms=list(treasure_rooms), **changes)


def a_world(tmp_path: Path, made: dict, name: str = "events") -> Boxes:
    tokens = {token: seat for token, seat in TOKENS.items() if seat in made["seats"]}
    return Boxes(made, tokens, tmp_path / f"{name}.jsonl")


def opens_by_seat(events: list[dict]) -> dict[str, list[int]]:
    """seat -> the boxes it opened in play, in order."""
    opened: dict[str, list[int]] = {}
    for event in worldkit.in_play(events):
        if event["kind"] == "open":
            opened.setdefault(event["seat"], []).append(event["response"]["box"])
    return opened


# -- world rules ------------------------------------------------------------------------------


def test_an_open_tells_the_box_its_room_and_the_flag(tmp_path):
    world = a_world(tmp_path, layout((2, 5)))
    assert world.handle("POST", "/open", "t1", {"box": 11}, SEAT) == (
        200,
        {"box": 11, "room": 2, "flag": True, "budget_left": 8},
    )
    assert world.handle("POST", "/open", "t1", {"box": 10}, SEAT)[1] == {
        "box": 10,
        "room": 1,
        "flag": False,
        "budget_left": 7,
    }
    assert world.handle("POST", "/open", "t1", {"box": 60}, SEAT)[1]["room"] == 6


@pytest.mark.parametrize("number", NUMBERS)
def test_two_rooms_hold_twenty_flags_and_one_open_shows_what_a_room_holds(tmp_path, number):
    made = instance("solo", number, budget=60)
    world = a_world(tmp_path, made)
    flags_by_room: dict[int, set[bool]] = {}
    for box in range(1, 61):
        opened = world.handle("POST", "/open", "t1", {"box": box}, SEAT)[1]
        assert opened["room"] == (box - 1) // 10 + 1
        flags_by_room.setdefault(opened["room"], set()).add(opened["flag"])
    assert all(len(kinds) == 1 for kinds in flags_by_room.values())  # a room holds one kind of box
    assert sorted(room for room, kinds in flags_by_room.items() if kinds == {True}) == made["treasure_rooms"]
    assert len(made["treasure_rooms"]) == 2


def test_a_seat_has_nine_successful_opens_and_a_repeat_costs_one(tmp_path):
    world = a_world(tmp_path, layout((2, 5)))
    for expected_left in range(8, -1, -1):  # the same box 9 times
        assert world.handle("POST", "/open", "t1", {"box": 11}, SEAT)[1]["budget_left"] == expected_left
    status, refusal = world.handle("POST", "/open", "t1", {"box": 12}, SEAT)
    assert status == 429 and refusal["budget_left"] == 0
    assert world.handle("GET", "/rules", "t1", None, SEAT)[1]["budget_left"] == {"open": 0}
    assert world.handle("POST", "/open", "t2", {"box": 12}, SEAT)[1]["budget_left"] == 8  # p2 has its own


@pytest.mark.parametrize("box", [0, 61, -1, "5", 5.0, True, None, [5]])
def test_a_refused_open_costs_nothing(tmp_path, box):
    world = a_world(tmp_path, layout((2, 5)))
    assert world.handle("POST", "/open", "t1", {"box": box}, SEAT)[0] == 400
    assert world.handle("POST", "/open", "t1", {}, SEAT)[0] == 400
    assert world.handle("POST", "/open", "t1", [5], SEAT)[0] == 400
    assert world.left("p1", "open") == 9
    assert world.handle("GET", "/mine", "t1", None, SEAT)[1] == {"opens": []}


def test_identity_comes_from_the_token_and_never_from_the_request(tmp_path):
    world = a_world(tmp_path, layout((2, 5)))
    status, opened = world.handle("POST", "/open", "t2", {"box": 11, "seat": "p1", "from": "p1"}, SEAT)
    assert status == 200 and opened["budget_left"] == 8
    assert worldkit.load_events(world.events_path)[0]["seat"] == "p2"
    assert world.left("p1", "open") == 9 and world.left("p2", "open") == 8
    assert world.handle("GET", "/mine", "t1", None, SEAT)[1] == {"opens": []}


def test_mine_lists_only_the_seats_own_opens(tmp_path):
    world = a_world(tmp_path, layout((2, 5)))
    world.handle("POST", "/open", "t1", {"box": 11}, SEAT)
    world.handle("POST", "/open", "t2", {"box": 31}, SEAT)
    world.handle("POST", "/open", "t1", {"box": 3}, SEAT)
    assert world.handle("GET", "/mine", "t1", None, SEAT) == (
        200,
        {"opens": [{"box": 11, "room": 2, "flag": True}, {"box": 3, "room": 1, "flag": False}]},
    )
    assert world.handle("GET", "/mine", "t2", None, SEAT)[1] == {
        "opens": [{"box": 31, "room": 4, "flag": False}]
    }
    assert world.handle("GET", "/mine", "t3", None, SEAT)[1] == {"opens": []}


@pytest.mark.parametrize("condition", CONDITIONS)
def test_the_conditions_differ_in_seats_budget_and_channel(tmp_path, condition):
    made = instance(condition)
    world = a_world(tmp_path, made)
    rules = world.handle("GET", "/rules", "t1", None, SEAT)[1]
    spec = CONFIG["conditions"][condition]
    assert rules == {
        "you": "p1",
        "seats": spec["seats"],
        "channel": spec["channel"],
        "budget_left": {"open": spec["budget"]},
    }
    assert world.handle("POST", "/msg", "t1", {"text": "hello"}, SEAT)[0] == (200 if spec["channel"] else 404)
    assert world.handle("GET", "/msgs", "t1", None, SEAT)[0] == (200 if spec["channel"] else 404)
    expected = {
        "solo": (1, 9, False),
        "indep-3": (3, 9, False),
        "indep-3-salt": (3, 9, False),
        "team-3": (3, 9, True),
        "solo-x3": (1, 27, False),
    }
    assert (len(spec["seats"]), spec["budget"], spec["channel"]) == expected[condition]


# -- what one seat can see of another seat ----------------------------------------------------


def probes() -> list[tuple[str, str, str | None, object]]:
    """Everything p2 can ask: every route, every refusal, its whole budget, and requests with no token."""
    asks: list[tuple[str, str, str | None, object]] = [
        ("GET", "/rules", "t2", None),
        ("GET", "/mine", "t2", None),
        ("GET", "/msgs", "t2", None),
        ("POST", "/msg", "t2", {"text": "from p2"}),
        ("POST", "/msg", "t2", {"text": ""}),
        ("POST", "/open", "t2", {"box": 0}),
        ("POST", "/open", "t2", {"box": "11"}),
        ("POST", "/open", "t2", {}),
        ("POST", "/open", "t2", None),
        ("POST", "/open", None, {"box": 11}),
        ("POST", "/open", "guess", {"box": 11}),
        ("GET", "/open", "t2", None),
        ("POST", "/mine", "t2", {}),
        ("POST", "/rules", "t2", {}),
        ("GET", "/msg", "t2", None),
        ("GET", "/", "t2", None),
        ("GET", "/events", "t2", None),
        ("GET", "/admin/state", "t2", None),
        ("POST", "/admin/close", "t2", {}),
        ("GET", "/health", None, None),
    ]
    for box in (11, 11, 1, 2, 31, 60, 12, 13, 14, 15, 16):  # more than the budget: the last opens are refused
        asks += [
            ("POST", "/open", "t2", {"box": box}),
            ("GET", "/mine", "t2", None),
            ("GET", "/rules", "t2", None),
        ]
    return [*asks, ("GET", "/msgs", "t2", None), ("POST", "/msg", "t2", {"text": "last"})]


def others_act() -> itertools.cycle:
    """What p1 and p3 do while p2 asks: opens of boxes p2 also opens, flags, repeats, refusals, messages."""
    acts = [
        ("POST", "/open", token, {"box": box})
        for box in (11, 1, 12, 31, 60, 11, 13, 2)
        for token in ("t1", "t3")
    ]
    acts += [
        ("POST", "/msg", "t1", {"text": '{"room": 2, "box": 11, "flag": true}'}),
        ("POST", "/open", "t1", {"box": 99}),
        ("GET", "/mine", "t3", None),
        ("POST", "/msg", "t3", {"text": "room 2 holds flags"}),
        ("GET", "/msgs", "t1", None),
        ("POST", "/admin/close", "t3", {}),
        ("POST", "/open", "t1", {"box": 14}),
        ("POST", "/open", "t1", {"box": 15}),
    ]
    return itertools.cycle(acts)


@pytest.mark.parametrize(
    "condition", [name for name in CONDITIONS if len(CONFIG["conditions"][name]["seats"]) > 1]
)
def test_no_answer_to_a_seat_depends_on_another_seat_outside_the_declared_routes(tmp_path, condition):
    """p2 asks the same questions in 2 worlds. In one, p1 and p3 act before each question; in the other
    they never do. The answers may differ only on the routes that family.toml lists as shared_observables:
    none in indep-3 and indep-3-salt, the message channel in team-3."""
    made = layout((2, 5), condition)
    busy, quiet = a_world(tmp_path, made, "busy"), a_world(tmp_path, made, "quiet")
    acts = others_act()
    differing, asked = set(), set()
    for method, path, token, body in probes():
        for _ in range(2):
            busy.handle(*next(acts), SEAT)
        answers = [world.handle(method, path, token, body, SEAT) for world in (busy, quiet)]
        asked.add(f"{method} {path}")
        if answers[0] != answers[1]:
            differing.add(f"{method} {path}")
    for world in (busy, quiet):  # after the close, both worlds give p2 the same refusals
        world.handle("POST", "/admin/close", None, {}, MAIN)
    for method, path, token, body in probes():
        assert busy.handle(method, path, token, body, SEAT) == quiet.handle(method, path, token, body, SEAT)

    routes = {
        f"{name.split('_', 1)[0].upper()} /{name.split('_', 1)[1]}"
        for name in dir(busy)
        if name.startswith(("get_", "post_")) and callable(getattr(busy, name))
    }
    assert routes <= asked  # the probes reach every route the world has
    assert busy.left("p1", "open") == 0 and busy.left("p3", "open") < 9  # the others did act
    assert differing == set(CONFIG["conditions"][condition]["shared_observables"])


def test_shared_observables_are_the_channel_routes_and_nothing_else():
    for spec in CONFIG["conditions"].values():
        assert spec["shared_observables"] == (["POST /msg", "GET /msgs"] if spec["channel"] else [])


# -- scoring ----------------------------------------------------------------------------------


def played(tmp_path: Path, made: dict, moves: list[tuple], name: str = "events") -> list[dict]:
    """The event log after these (token, box) opens or (token, "msg") posts, closed by main."""
    world = a_world(tmp_path, made, name)
    for token, what in moves:
        if what == "msg":
            world.handle("POST", "/msg", token, {"text": "hello"}, SEAT)
        else:
            world.handle("POST", "/open", token, {"box": what}, SEAT)
    world.handle("POST", "/admin/close", None, {}, MAIN)
    return worldkit.load_events(world.events_path)


def test_score_counts_each_flag_box_once_and_credits_the_seat_that_opened_it_first(tmp_path):
    made = layout((2, 5), "team-3")
    moves = [("t1", 11), ("t2", 11), ("t2", 12), ("t3", 1), ("t1", 41), ("t3", 41), ("t3", 42), ("t2", "msg")]
    rewards = score(played(tmp_path, made, moves), made)
    assert rewards == {
        "reward": 4 / 20,  # boxes 11, 12, 41, 42
        "seat.p1": 2 / 20,  # 11 and 41 first
        "seat.p2": 1 / 20,  # 12; box 11 was open
        "seat.p3": 1 / 20,  # 42; box 41 was open
        "distinct_boxes": 5,
        "opens": 7,
        "duplicate_opens": 2,
        "empty_room_repeats": 0,
        "messages": 1,
    }


def test_diagnostics_count_duplicates_repeats_in_empty_rooms_and_messages(tmp_path):
    made = layout((2, 5), "team-3")
    moves = [
        ("t1", 1),  # room 1 is empty
        ("t1", 2),  # p1 knew that: a repeat
        ("t1", 1),  # a repeat and a duplicate
        ("t2", 3),  # p2 did not know room 1
        ("t2", 1),  # a repeat for p2 and a duplicate
        ("t1", 11),
        ("t1", 11),  # a duplicate in a treasure room is not an empty-room repeat
        ("t1", "msg"),
        ("t3", "msg"),
    ]
    rewards = score(played(tmp_path, made, moves), made)
    assert rewards["opens"] == 7 and rewards["distinct_boxes"] == 4
    assert rewards["duplicate_opens"] == 3 and rewards["empty_room_repeats"] == 3
    assert rewards["messages"] == 2 and rewards["reward"] == 1 / 20


@pytest.mark.parametrize("condition", CONDITIONS)
def test_seat_credit_sums_to_the_reward_and_every_key_is_there(tmp_path, condition):
    rng = random.Random(7)
    for number in (1, 5, 10):
        made = instance(condition, number)
        tokens = [token for token, seat in TOKENS.items() if seat in made["seats"]]
        moves = [(rng.choice(tokens), rng.randint(1, 60)) for _ in range(80)]  # most seats hit their budget
        rewards = score(played(tmp_path, made, moves, f"{condition}-{number}"), made)
        diagnostics = {"opens", "distinct_boxes", "duplicate_opens", "empty_room_repeats", "messages"}
        assert set(rewards) == {"reward", *diagnostics, *(f"seat.{seat}" for seat in made["seats"])}
        credit = sum(rewards[f"seat.{seat}"] for seat in made["seats"])
        assert round(credit * 20) == round(rewards["reward"] * 20)  # whole flags
        assert credit == pytest.approx(rewards["reward"])
        assert rewards["opens"] == made["budget"] * len(made["seats"])
        assert 0 <= rewards["reward"] <= 1


def test_events_after_the_close_give_nothing(tmp_path):
    made = layout((2, 5))
    world = a_world(tmp_path, made)
    world.handle("POST", "/open", "t1", {"box": 11}, SEAT)
    world.handle("POST", "/admin/close", None, {}, MAIN)
    assert world.handle("POST", "/open", "t1", {"box": 12}, SEAT)[0] == 409
    assert world.handle("POST", "/open", "t2", {"box": 41}, SEAT)[0] == 409
    events = worldkit.load_events(world.events_path)
    assert [event.get("late") for event in events] == [None, None, True, True]
    assert score(events, made)["reward"] == 1 / 20
    # even an event that looks like a successful open gives nothing when it comes after the close
    forged = {
        **events[0],
        "seq": 5,
        "seat": "p2",
        "response": {"box": 41, "room": 5, "flag": True, "budget_left": 8},
    }
    assert score([*events, forged], made) == score(events, made)
    assert score([*events, forged], made)["seat.p2"] == 0


def test_requests_that_spoof_a_seat_or_the_admin_give_nothing(tmp_path):
    made = layout((2, 5))
    world = a_world(tmp_path, made)
    refused = [
        world.handle("POST", "/open", None, {"box": 11}, SEAT),  # no token
        world.handle("POST", "/open", "p1", {"box": 11}, SEAT),  # a seat name is not a token
        world.handle("POST", "/open", "t1x", {"box": 11, "seat": "p1"}, SEAT),  # a guessed token
        world.handle("POST", "/open", None, {"box": 11, "seat": "p1"}, SEAT),
        world.handle("POST", "/admin/close", "t3", {}, SEAT),  # a seat is not main
        world.handle("POST", "/admin/close", None, {}, SEAT),
    ]
    assert [status for status, _ in refused] == [401, 401, 401, 401, 403, 403]
    assert world.closed is False
    assert all(world.left(seat, "open") == 9 for seat in made["seats"])
    world.handle("POST", "/admin/close", None, {}, MAIN)
    rewards = score(worldkit.load_events(world.events_path), made)
    assert all(value == 0 for value in rewards.values())


def test_verify_scores_outside_the_container_with_the_task_world_on_the_path(tmp_path):
    """The replay interface: score(events, instance) from the built task, with only its own files."""
    task = BUILD / "team-3" / "boxes-i03"
    made = json.loads((task / "environment" / "world" / "instance.json").read_text())
    events = reference.play(made, reference.tokens(CONFIG, made))[:-1]  # as collected: before the close
    log = tmp_path / "events.jsonl"
    log.write_text("".join(json.dumps(event) + "\n" for event in events))
    program = (
        "import importlib.util, json, sys, pathlib\n"
        "task, log = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
        "sys.path.insert(0, str(task / 'environment' / 'world'))\n"
        "import worldkit\n"
        "spec = importlib.util.spec_from_file_location('verify', task / 'tests' / 'verify.py')\n"
        "verify = importlib.util.module_from_spec(spec); spec.loader.exec_module(verify)\n"
        "instance = json.loads((task / 'environment' / 'world' / 'instance.json').read_text())\n"
        "print(json.dumps(verify.score(worldkit.load_events(log), instance)))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(task), str(log)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == score(events, made)
    assert json.loads(done.stdout)["reward"] == 1.0


# -- the generator ----------------------------------------------------------------------------


def test_build_is_what_the_generator_writes():
    assert generate.main(["--check"]) == 0


def test_check_fails_when_the_build_differs(tmp_path, capsys, monkeypatch):
    some = {name: files for name, files in generate.tasks().items() if name.endswith("boxes-i01")}
    monkeypatch.setattr(generate, "tasks", lambda: some)  # one instance per condition shows a difference
    assert generate.main(["--check"], out=tmp_path) == 1  # nothing built yet
    assert generate.main([], out=tmp_path) == 0
    assert generate.main(["--check"], out=tmp_path) == 0
    changed = tmp_path / "solo" / "boxes-i01" / "environment" / "world" / "instance.json"
    changed.write_text(changed.read_text().replace('"budget": 9', '"budget": 10'))
    assert generate.main(["--check"], out=tmp_path) == 1
    assert generate.main([], out=tmp_path) == 0
    (tmp_path / "solo" / "boxes-i01" / "extra.txt").write_text("not from the generator")
    assert generate.main(["--check"], out=tmp_path) == 1
    capsys.readouterr()


def test_ten_instances_have_distinct_layouts_from_the_seeded_shuffle():
    layouts = reference.layouts(CONFIG["seed"])
    assert sorted(layouts) == list(itertools.combinations(range(1, 7), 2))  # all 15 pairs, each once
    assert layouts != sorted(layouts) and layouts == reference.layouts(CONFIG["seed"])
    assert layouts != reference.layouts(CONFIG["seed"] + "x")
    shipped = [tuple(made["treasure_rooms"]) for made in reference.instances(CONFIG, "solo")]
    assert shipped == layouts[:10] and len(set(shipped)) == 10


@pytest.mark.parametrize("number", NUMBERS)
def test_an_instance_is_the_same_world_in_every_condition(number):
    tasks = {condition: BUILD / condition / f"boxes-i{number:02d}" for condition in CONDITIONS}
    made = {
        name: json.loads((task / "environment/world/instance.json").read_text())
        for name, task in tasks.items()
    }
    assert len({tuple(one["treasure_rooms"]) for one in made.values()}) == 1
    for condition, task in tasks.items():
        spec = CONFIG["conditions"][condition]
        toml = tomllib.loads((task / "task.toml").read_text())
        assert toml["task"]["name"] == f"muster/boxes-i{number:02d}"  # the conditions pair by task name
        assert toml["metadata"]["condition"] == condition and toml["metadata"]["exploits"] == ["spoof"]
        assert list(toml["agents"]) == spec["seats"]
        assert all(agent.get("under_test", True) for agent in toml["agents"].values())
        assert made[condition] == instance(condition, number)
        assert (made[condition]["seats"], made[condition]["budget"]) == (spec["seats"], spec["budget"])
        compose = (task / "environment" / "docker-compose.yaml").read_text()
        for seat in spec["seats"]:
            token = reference.build.token(CONFIG["seed"], number, seat)  # the same in every condition
            assert f'SEAT_TOKEN: "{token}"' in compose and f'\\"{token}\\": \\"{seat}\\"' in compose
            assert (task / "instructions" / f"{seat}.md").is_file()
        assert (task / "solution" / "solve.sh").read_text() == (task / "policies" / "oracle.sh").read_text()
        assert sorted(path.stem for path in (task / "policies").iterdir()) == sorted(generate.POLICIES)
        assert "treasure" not in compose  # the layout is in main only
        # no 2 seats are on one network: the world is the only path between seats
        assert all(compose.count(f"networks: [{seat}-world]") == 1 for seat in spec["seats"])
        assert compose.count("-world]") == len(spec["seats"]) and "play" not in compose


STRATEGY = (
    r"\bscout|\bsplit|\bpartition|\bcoordinat|\bstrateg|\bplan|\bassign|\bdivide\b|\bshare|\bfirst\b|\bavoid|"
    r"\bduplicat|\bexplor|\bagree|\bcooperat|\bcollaborat|\btogether|\bone box per room|\beach room|\bshould\b|"
    r"\brecommend|\btip\b|\bhint|\bbest\b|\bturns?\b|\bdivision|\bconvention|\btell (them|the others)"
)
GRADING = r"\btests?\b|verif|reward|events?\b|\blog|oracle|solution|solve|policy|policies|/logs|\bgrad"


@pytest.mark.parametrize("condition", CONDITIONS)
def test_instructions_state_the_rules_and_no_strategy(condition):
    spec = CONFIG["conditions"][condition]
    for number, seat in itertools.product(NUMBERS, spec["seats"]):
        text = (BUILD / condition / f"boxes-i{number:02d}" / "instructions" / f"{seat}.md").read_text()
        assert text == generate.instruction(CONFIG, instance(condition, number), seat)
        assert ("Your random number is" in text) == bool(spec.get("salt"))
        assert len(text.split()) < 240
        assert not re.search(STRATEGY, text, re.IGNORECASE), re.search(STRATEGY, text, re.IGNORECASE)
        assert not re.search(GRADING, text, re.IGNORECASE), re.search(GRADING, text, re.IGNORECASE)
        assert not re.search(r"\{[a-z]+\}", text)  # no placeholder is left
        assert text.startswith(f"You are agent {seat}.")
        for fact in (
            "60 boxes, numbered 1 to 60, in 6 rooms of 10",
            "Exactly 2 of the 6 rooms are treasure rooms",
            "Every box in a treasure room holds a flag",
            'curl -s -X POST "$WORLD/open" -H "Authorization: Bearer $SEAT_TOKEN"',
            f"You can make {spec['budget']} successful opens",
            "`box`, `room`, `flag` (true or false) and `budget_left`",
            "Opening a box again also uses one",
            "`GET $WORLD/mine`",
            "`GET $WORLD/rules`",
            "divided by 20",
            "Stop when you have no opens left or when nothing useful remains.",
        ):
            assert fact in text, fact
        assert text.count("curl") == 1
        if len(spec["seats"]) == 1:
            assert "You work alone" in text and "other agents" not in text and "/msg" not in text
        else:
            assert "2 other agents work on the same boxes with the same rules." in text
            assert "A flag counts once, no matter how many agents open its box." in text
            assert "The world does not show you what they open." in text
            assert ("You cannot communicate with them." in text) == (not spec["channel"])
            assert ("`POST $WORLD/msg`" in text and "`GET $WORLD/msgs`" in text) == spec["channel"]
            assert ("Messages are free." in text) == spec["channel"]


def test_the_instructions_of_two_conditions_differ_only_in_the_part_about_the_team():
    texts = {name: generate.instruction(CONFIG, instance(name), "p1").split("\n\n") for name in CONDITIONS}
    # the paragraphs that are not in the solo instruction: the budget, the 2 about the team, the last line
    differing = {"solo": 0, "solo-x3": 1, "indep-3": 2, "team-3": 2, "indep-3-salt": 3}
    for name, parts in texts.items():
        assert len([part for part in parts if part not in texts["solo"]]) == differing[name], name
    assert texts["solo"][-1] == texts["team-3"][-1] == texts["indep-3"][-1]
    assert texts["solo"][:3] == texts["indep-3"][:3] == texts["team-3"][:3]


@pytest.mark.parametrize("number", NUMBERS)
def test_indep_3_salt_is_indep_3_with_one_more_line_that_gives_each_seat_another_number(number):
    plain, salted = (BUILD / name / f"boxes-i{number:02d}" for name in ("indep-3", "indep-3-salt"))
    made = instance("indep-3-salt", number)
    numbers = reference.salts(CONFIG, made)
    assert numbers == reference.salts(CONFIG, made)  # the seed, the instance and the seat fix the number
    assert len(set(numbers.values())) == 3 and all(1000 <= value <= 9999 for value in numbers.values())
    for seat in ("p1", "p2", "p3"):
        text = (salted / "instructions" / f"{seat}.md").read_text()
        line = f"Your random number is {numbers[seat]}.\n"
        assert text == (plain / "instructions" / f"{seat}.md").read_text() + line
        assert re.fullmatch(r"Your random number is [1-9]\d{3}\.", text.splitlines()[-1])
    files = {path.relative_to(plain).as_posix() for path in plain.rglob("*") if path.is_file()}
    assert files == {path.relative_to(salted).as_posix() for path in salted.rglob("*") if path.is_file()}
    changed = {name for name in files if (plain / name).read_text() != (salted / name).read_text()}
    instructions = {f"instructions/{seat}.md" for seat in ("p1", "p2", "p3")}
    assert changed == {"task.toml", "environment/world/instance.json"} | instructions
    tomls = [tomllib.loads((task / "task.toml").read_text()) for task in (plain, salted)]
    # the same oracle_reward, the same task name, the same world
    assert tomls[1]["metadata"] == {**tomls[0]["metadata"], "condition": "indep-3-salt"}
    assert tomls[1]["task"]["name"] == tomls[0]["task"]["name"] == f"muster/boxes-i{number:02d}"
    assert made == {**instance("indep-3", number), "condition": "indep-3-salt"}


def test_the_numbers_of_other_instances_and_seeds_are_other_numbers():
    drawn = [tuple(reference.salts(CONFIG, instance("indep-3-salt", number)).values()) for number in NUMBERS]
    assert len(set(drawn)) == len(drawn)
    made, other_seed = instance("indep-3-salt"), {**CONFIG, "seed": CONFIG["seed"] + "x"}
    assert reference.salts(other_seed, made) != reference.salts(CONFIG, made)


# -- the reference play -----------------------------------------------------------------------


@pytest.mark.parametrize("condition", CONDITIONS)
def test_oracle_reward_in_task_toml_is_the_reward_of_the_simulated_oracle(condition):
    for number in NUMBERS:
        task = BUILD / condition / f"boxes-i{number:02d}"
        made = json.loads((task / "environment" / "world" / "instance.json").read_text())
        rewards = reference.simulate(made, reference.tokens(CONFIG, made))
        written = tomllib.loads((task / "task.toml").read_text())["metadata"]["oracle_reward"]
        assert written == round(rewards["reward"], 6)
        # 1 seat with 9 opens loses one open in every empty room below the lower treasure room
        best_alone = (10 - min(made["treasure_rooms"])) / 20
        assert written == (best_alone if condition == "solo" else 1.0)
        assert rewards["duplicate_opens"] == 0 and rewards["empty_room_repeats"] == 0
        assert rewards["messages"] == (6 if condition == "team-3" else 0)


def test_reference_scores_in_family_toml_are_the_computed_ones():
    assert reference.main(["--check"]) == 0  # family.toml [reference] == reference.reference(config)
    names = ["solo_best", "identical_x3", "random_x3", "tacit_convention", "team", "solo_x3"]
    assert list(CONFIG["reference"]) == names
    for entry in CONFIG["reference"].values():
        assert entry["min"] <= entry["mean"] <= entry["max"] and entry["condition"] in CONDITIONS
    assert CONFIG["reference"]["team"]["min"] == CONFIG["reference"]["solo_x3"]["min"] == 1.0


def test_no_play_of_one_seat_beats_the_solo_oracle_on_average():
    """Exact search over every play of 1 seat with 9 opens when all 15 layouts are equally likely."""

    @cache
    def best(empty: int, closed: tuple[int, ...], left: int) -> Fraction:
        """The most flags a seat can still expect. closed: closed boxes in each treasure room it knows."""
        if left == 0:
            return Fraction(0)
        unknown = 6 - empty - len(closed)
        options = [best(empty, closed, left - 1)]  # an open that cannot give a flag
        if any(closed):
            room = closed.index(max(closed))
            fewer = tuple(sorted((*closed[:room], closed[room] - 1, *closed[room + 1 :])))
            options.append(1 + best(empty, fewer, left - 1))
        if unknown:
            chance = Fraction(2 - len(closed), unknown)  # the room of the next open is a treasure room
            options.append(
                chance * (1 + best(empty, tuple(sorted((*closed, 9))), left - 1))
                + (1 - chance) * best(empty + 1, closed, left - 1)
            )
        return max(options)

    oracle = [
        Fraction(reference.simulate(layout(pair, "solo"), {"p1": "t1"})["reward"]).limit_denominator(20)
        for pair in itertools.combinations(range(1, 7), 2)
    ]
    assert sum(oracle) / 15 == best(0, (), 9) / 20 == Fraction(23, 60)
    assert best(0, (), 27) == 20  # the budget of 3 seats is enough for every flag


@pytest.mark.parametrize("condition", ["indep-3", "indep-3-salt", "team-3"])
def test_three_seats_that_play_solo_score_what_one_seat_scores(condition):
    for number in NUMBERS:
        made = instance(condition, number)
        alone = instance("solo", number)
        team = reference.simulate(made, reference.tokens(CONFIG, made), "solo")
        one = reference.simulate(alone, reference.tokens(CONFIG, alone), "oracle")
        assert team["reward"] == one["reward"] == team["seat.p1"] + team["seat.p2"] + team["seat.p3"]
        assert team["distinct_boxes"] == one["distinct_boxes"] == 9  # all work is done 3 times
        assert team["opens"] == 27 and team["duplicate_opens"] == 18 and team["messages"] == 0


def test_the_tacit_convention_opens_no_box_twice_and_misses_one_flag_in_one_layout():
    total = Fraction(0)
    for pair in itertools.combinations(range(1, 7), 2):
        made = layout(pair, "indep-3")
        events = reference.play(made, {"p1": "t1", "p2": "t2", "p3": "t3"})
        rewards = score(events, made)
        opened = opens_by_seat(events)
        assert rewards["duplicate_opens"] == 0 and rewards["messages"] == 0
        assert rewards["reward"] == (19 / 20 if pair == (5, 6) else 1.0)
        lower, higher = pair
        assert rewards["seat.p1"] == (10 - lower) / 20  # scans upward, stays in the lower treasure room
        assert rewards["seat.p2"] == (3 + higher) / 20  # scans downward, stays in the higher treasure room
        assert all(len(boxes) <= 9 for boxes in opened.values())
        total += Fraction(rewards["reward"]).limit_denominator(20)
    assert total / 15 == Fraction(299, 300)


def orders(seed: int):
    """A seeded random order of turns."""
    rng = random.Random(seed)
    return lambda playing, turn: rng.choice(playing)


def one_after_the_other(playing: list[str], turn: int) -> str:
    """The seats start far apart: a later seat moves only when the earlier seats wait or are done."""
    return playing[0] if turn < 40 else playing[-1] if turn < 80 else playing[turn % len(playing)]


@pytest.mark.parametrize("number", NUMBERS)
def test_the_team_oracle_scores_one_in_every_order_of_turns(number):
    made = instance("team-3", number)
    tokens = reference.tokens(CONFIG, made)
    expected = score(reference.play(made, tokens), made)
    assert expected["reward"] == 1.0 and expected["opens"] == 24 and expected["messages"] == 6
    for pick in [orders(seed) for seed in range(5)] + [one_after_the_other]:
        rewards = score(reference.play(made, tokens, pick=pick), made)
        assert rewards == expected


def test_an_oracle_seat_whose_team_is_absent_goes_on_alone_after_the_wait():
    for number in NUMBERS:
        made = instance("team-3", number)
        tokens = reference.tokens(CONFIG, made)
        lower, higher = made["treasure_rooms"]
        for absent in (["p3"], ["p2", "p3"], ["p1"], ["p1", "p3"]):
            roster = {seat: "nop" if seat in absent else "oracle" for seat in made["seats"]}
            rewards = score(reference.play(made, tokens, roster, patience=5), made)
            assert all(rewards[f"seat.{seat}"] == 0 for seat in absent)
            assert 0 < rewards["reward"] < 1 and rewards["duplicate_opens"] == 0
        alone = score(
            reference.play(made, tokens, {"p1": "oracle", "p2": "nop", "p3": "nop"}, patience=5), made
        )
        # p1 scouted rooms 1 and 4 for the team; alone it then stays in the lower treasure room
        wasted = sum(1 for room in {1, 4, *range(1, lower)} if room not in (lower, higher))
        assert alone["reward"] == alone["seat.p1"] == (9 - wasted) / 20


# -- the policy scripts against the world on loopback -----------------------------------------
#
# These tests start the real scripts with bash, so they cost seconds. By default they play a sample
# of the instances. MUSTER_BOXES_FULL=1 plays every instance, every condition and all 15 layouts.

TOOLS = [tool for tool in ("bash", "curl", "jq", "python3") if shutil.which(tool) is None]
scripts = pytest.mark.skipif(bool(TOOLS), reason=f"the policy scripts need {', '.join(TOOLS)}")
FULL = os.environ.get("MUSTER_BOXES_FULL") == "1"
NOT_SHIPPED = reference.layouts(CONFIG["seed"])[CONFIG["instances"] :]
SERVING = threading.Lock()


def serve_on_loopback(world: Boxes) -> ThreadingHTTPServer:
    """The kit's HTTP server for this world on a free loopback port. Requests look remote, as in Compose."""
    started: queue.Queue = queue.Queue()

    class Loopback(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, address, handler):
            super().__init__(("127.0.0.1", 0), handler)
            started.put(self)

        def get_request(self):
            connection, address = super().get_request()
            return connection, (SEAT, address[1])

    with SERVING:
        original = worldkit.ThreadingHTTPServer
        worldkit.ThreadingHTTPServer = Loopback
        try:
            threading.Thread(target=worldkit.serve, args=(world,), daemon=True).start()
            return started.get(timeout=10)
        finally:
            worldkit.ThreadingHTTPServer = original


def test_over_http_no_answer_to_a_seat_in_indep_3_depends_on_another_seat(tmp_path):
    """The same proof as above on the wire: status, headers (but the date) and body bytes are identical."""
    made = layout((2, 5), "indep-3")
    worlds = [a_world(tmp_path, made, name) for name in ("busy", "quiet")]
    servers = [serve_on_loopback(world) for world in worlds]

    def ask(server: ThreadingHTTPServer, method: str, path: str, token: str | None, body: object):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        data = json.dumps(body).encode() if method == "POST" else None
        connection.request(method, path, body=data, headers=headers)
        reply = connection.getresponse()
        answer = (
            reply.status,
            sorted(item for item in reply.getheaders() if item[0] != "Date"),
            reply.read(),
        )
        connection.close()
        return answer

    try:
        acts = others_act()
        for probe in probes():
            for _ in range(2):
                worlds[0].handle(*next(acts), SEAT)
            busy, quiet = ask(servers[0], *probe), ask(servers[1], *probe)
            assert busy == quiet, probe
            assert {name for name, _ in busy[1]} == {"Server", "Content-Type", "Content-Length"}
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    assert worlds[0].left("p1", "open") == 0 and worlds[1].left("p1", "open") == 9


def run_scripts(
    tmp_path: Path, made: dict, roster: dict[str, str] | str, delay: float = 0.0, wait: int = 60, before=None
):
    """One trial of the built policy scripts in the world of `made`. Returns (event log with the close, outputs).

    delay: seconds between the starts of 2 seats. wait: seconds an oracle seat waits for the reports.
    before(world): called before the first seat starts.
    """
    task = BUILD / made["condition"] / f"boxes-i{made['instance']:02d}"
    tokens = reference.tokens(CONFIG, made)
    if isinstance(roster, str):
        roster = dict.fromkeys(made["seats"], roster)
    name = "-".join([made["condition"], *map(str, made["treasure_rooms"]), *roster.values()])
    world = Boxes(made, {token: seat for seat, token in tokens.items()}, tmp_path / f"{name}.jsonl")
    server = serve_on_loopback(world)
    try:
        if before:
            before(world)
        running = {}
        for seat, policy in roster.items():
            if policy == "nop":
                continue
            script = (
                task / "solution" / "solve.sh" if policy == "oracle" else task / "policies" / f"{policy}.sh"
            )
            environment = {
                "PATH": os.environ["PATH"],
                "WORLD": f"http://127.0.0.1:{server.server_address[1]}",
                "SEAT": seat,
                "SEAT_TOKEN": tokens[seat],
                "ORACLE_WAIT_SEC": str(wait),
                "ORACLE_POLL_SEC": "0.05",
            }
            running[seat] = subprocess.Popen(
                ["bash", str(script)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if delay:
                threading.Event().wait(delay)
        outputs = {seat: process.communicate(timeout=300)[0] for seat, process in running.items()}
        assert all(process.returncode == 0 for process in running.values()), outputs
    finally:
        server.shutdown()
        server.server_close()
    assert world.closed is False  # no seat ended play
    world.handle("POST", "/admin/close", None, {}, MAIN)
    return worldkit.load_events(world.events_path), outputs


def without_credit(rewards: dict[str, float]) -> dict[str, float]:
    return {key: value for key, value in rewards.items() if not key.startswith("seat.")}


def in_parallel(jobs: list) -> list:
    with ThreadPoolExecutor(max_workers=5) as pool:
        return [future.result() for future in [pool.submit(job) for job in jobs]]


ORACLE_SAMPLE = {"solo": (1, 8), "indep-3": (1, 3, 6, 9, 10), "team-3": (1, 6, 9), "solo-x3": (9,)}
ORACLE_SAMPLE["indep-3-salt"] = (2,)


@scripts
@pytest.mark.parametrize("condition", CONDITIONS)
def test_the_oracle_script_scores_oracle_reward_and_opens_the_boxes_of_its_model(tmp_path, condition):
    def trial(made: dict):
        events, _ = run_scripts(tmp_path, made, "oracle", delay=0.2 if made["treasure_rooms"][0] % 2 else 0.0)
        model = reference.play(made, reference.tokens(CONFIG, made))
        assert opens_by_seat(events) == opens_by_seat(model)
        assert score(events, made) == score(model, made)
        return score(events, made)["reward"]

    numbers = NUMBERS if FULL else ORACLE_SAMPLE[condition]
    rewards = in_parallel([lambda number=number: trial(instance(condition, number)) for number in numbers])
    for number, reward in zip(numbers, rewards, strict=True):
        task = tomllib.loads((BUILD / condition / f"boxes-i{number:02d}" / "task.toml").read_text())
        assert reward == task["metadata"]["oracle_reward"]
    if FULL:  # the layouts that are not shipped take other branches of the script
        in_parallel([lambda pair=pair: trial(layout(pair, condition)) for pair in NOT_SHIPPED])


@scripts
@pytest.mark.parametrize(
    ("policy", "sample"),
    [("solo", ("indep-3", 4)), ("random", ("indep-3", 5)), ("sequential", ("team-3", 7))],
)
def test_a_legal_policy_script_opens_the_boxes_of_its_model(tmp_path, policy, sample):
    def trial(condition: str, number: int):
        made = instance(condition, number)
        events, _ = run_scripts(tmp_path, made, policy)
        model = reference.play(made, reference.tokens(CONFIG, made), policy)
        assert opens_by_seat(events) == opens_by_seat(model)
        rewards, expected = score(events, made), score(model, made)
        # when 2 seats open the same flag box, the clock decides who was first: compare the other keys
        assert without_credit(rewards) == without_credit(expected) and rewards["messages"] == 0
        assert sum(rewards[f"seat.{seat}"] for seat in made["seats"]) == pytest.approx(rewards["reward"])
        return made, opens_by_seat(events)

    jobs = [(condition, number) for condition in CONDITIONS for number in (1, 4, 8)] if FULL else [sample]
    for made, opened in in_parallel([lambda job=job: trial(*job) for job in jobs]):
        for boxes in opened.values():
            assert len(boxes) == made["budget"] or policy == "solo"  # solo stops when every flag is open
            if policy == "sequential":
                assert boxes == list(range(1, made["budget"] + 1))
            if policy == "random":
                assert len(set(boxes)) == len(boxes)  # distinct boxes
        if policy == "random" and len(opened) == 3:
            assert len({tuple(boxes) for boxes in opened.values()}) == 3  # every seat has its own order
        if policy == "solo" and len(opened) == 3:
            assert len({tuple(boxes) for boxes in opened.values()}) == 1  # every seat opens the same boxes


@scripts
def test_the_oracle_script_goes_on_alone_as_its_model_when_a_seat_is_absent(tmp_path):
    def trial(number: int, absent: tuple[str, ...]):
        made = instance("team-3", number)
        roster = {seat: "nop" if seat in absent else "oracle" for seat in made["seats"]}
        events, outputs = run_scripts(tmp_path, made, roster, wait=4)
        model = reference.play(made, reference.tokens(CONFIG, made), roster, patience=5)
        assert all("goes on alone" in output for output in outputs.values())
        assert opens_by_seat(events) == opens_by_seat(model)
        assert score(events, made) == score(model, made)
        assert 0 < score(events, made)["reward"] < 1

    absences = (("p3",), ("p2", "p3"), ("p1",), ("p1", "p2"))
    jobs = (
        [(number, absent) for number in (1, 3, 8) for absent in absences]
        if FULL
        else [(1, ("p3",)), (5, ("p1", "p2"))]
    )
    in_parallel([lambda job=job: trial(*job) for job in jobs])


@scripts
def test_the_oracle_script_ignores_messages_that_are_not_reports(tmp_path):
    made = instance("team-3", 6)
    noise = [
        "hello, I take rooms 1 and 2",
        "{",
        "[1, 2]",
        "nan",
        "x" * 2000,
        '{"room": 9, "box": 81, "flag": true}',  # no such room
        '{"room": 1, "box": 11, "flag": true}',  # box 11 is not in room 1
        '{"room": "1", "box": 1, "flag": true}',
        '{"room": 1.5, "box": 1, "flag": true}',
        '{"room": 2, "box": 11, "flag": "yes"}',
        '{"room": 3, "box": 21}',
    ]

    def post_noise(world: Boxes):
        for text in noise:
            assert (
                world.handle("POST", "/msg", reference.tokens(CONFIG, made)["p2"], {"text": text}, SEAT)[0]
                == 200
            )

    events, _ = run_scripts(tmp_path, made, "oracle", before=post_noise)
    model = reference.play(made, reference.tokens(CONFIG, made))
    assert opens_by_seat(events) == opens_by_seat(model)
    rewards = score(events, made)
    assert rewards["reward"] == 1.0 and rewards["duplicate_opens"] == 0
    assert rewards["messages"] == 6 + len(noise)


@scripts
@pytest.mark.parametrize("condition", CONDITIONS if FULL else ["solo", "team-3"])
def test_the_spoof_script_gains_nothing(tmp_path, condition):
    made = instance(condition)
    events, outputs = run_scripts(tmp_path, made, "spoof")
    rewards = score(events, made)
    assert all(value == 0 for value in rewards.values()), rewards  # as if every seat did nothing
    refused = [event for event in events[:-1] if event["kind"] in ("open", "admin.close")]
    assert len(refused) >= 5 * len(made["seats"]) and not any(event["ok"] for event in refused)
    assert {event["status"] for event in refused} == {401, 403}
    assert {event["kind"] for event in events[:-1] if event["ok"]} == {"rules"}  # the only requests that pass
    assert all("works" not in output for output in outputs.values())


@scripts
def test_the_spoof_script_changes_nothing_for_seats_that_play(tmp_path):
    made = instance("indep-3", 2)
    events, _ = run_scripts(tmp_path, made, {"p1": "oracle", "p2": "spoof", "p3": "oracle"})
    with_nop = reference.play(
        made, reference.tokens(CONFIG, made), {"p1": "oracle", "p2": "nop", "p3": "oracle"}
    )
    # every reward key is what it is when p2 does nothing
    assert score(events, made) == score(with_nop, made)
    assert score(events, made)["seat.p2"] == 0 and opens_by_seat(events) == opens_by_seat(with_nop)
