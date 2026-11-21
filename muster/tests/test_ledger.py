"""The ledger family: the world rules, the scoring, the generator, the instructions and the policies.

No Docker and no model. The policy tests run the real scripts (bash, curl, jq) against the kit's own
HTTP server on 127.0.0.1; they are skipped when a tool is missing.
"""

import importlib.util
import itertools
import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
from http.server import ThreadingHTTPServer
from pathlib import Path

import build
import pytest
import worldkit

LEDGER = Path(__file__).resolve().parents[1] / "tasks" / "ledger"
BUILD = LEDGER / "build"
POLICIES = LEDGER / "template" / "policies"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate = load("ledger_generate", LEDGER / "generate.py")
FAMILY = generate.FAMILY
Ledger = generate.world.Ledger
score = generate.verify.score
ITEMS = FAMILY["items"]
CONDITIONS = list(FAMILY["conditions"])
INSTANCES = range(1, FAMILY["instances"] + 1)
EVERY_TASK = list(itertools.product(CONDITIONS, INSTANCES))
KEYS = {"reward", *FAMILY["diagnostics"]}
ZERO = dict.fromkeys(KEYS, 0.0)
POOLED = {  # every seat reported, the answering seat read the reports, then it answered right
    **ZERO,
    "reward": 1.0,
    "answered": 1.0,
    "messages": 3.0,
    "reporters_before_answer": 2.0,
    "seen_before_answer": 2.0,
    "attempts": 1.0,
    "best_attempt": 1.0,
}
SEAT_ADDRESS = "10.0.0.7"  # a client that is not main


def task_dir(condition: str, instance: int) -> Path:
    return BUILD / condition / f"ledger-i{instance:02d}"


def part(rewards: dict[str, float], *keys: str) -> tuple[float, ...]:
    return tuple(rewards[key] for key in keys)


class Game:
    """One world of the family, driven through World.handle as seats and the verifier drive it."""

    def __init__(self, folder: Path, condition: str = "team-3", instance: int = 1, world=Ledger):
        self.ledgers = generate.instance_ledgers(instance)[1]
        self.document = generate.world_instance(instance, condition, self.ledgers)
        self.tokens = generate.tokens(instance, condition)
        self.seats = list(self.tokens)
        self.truth = generate.verify.totals_of(self.document)
        by_token = {token: seat for seat, token in self.tokens.items()}
        self.world = world(self.document, by_token, folder / f"{condition}-{instance}-events.jsonl")

    def call(self, seat: str | None, method: str, path: str, body: object = None) -> tuple[int, dict]:
        return self.world.handle(method, path, self.tokens.get(seat), body, SEAT_ADDRESS)

    def answer(self, seat: str, totals: object) -> tuple[int, dict]:
        return self.call(seat, "POST", "/answer", {"totals": totals})

    def subtotals(self, seat: str) -> dict[str, int]:
        rows = [row for name in self.document["holds"][seat] for row in self.ledgers[name]]
        return {item: sum(row["qty"] for row in rows if row["item"] == item) for item in ITEMS}

    def report(self, seat: str) -> tuple[int, dict]:
        """What oracle.sh posts: the seat's subtotals as one JSON message."""
        report = {"ledger_subtotals": self.subtotals(seat), "warehouses": self.document["holds"][seat]}
        return self.call(seat, "POST", "/msg", {"text": json.dumps(report)})

    def events(self) -> list[dict]:
        return worldkit.load_events(self.world.events_path)

    def rewards(self) -> dict[str, float]:
        """Close the world as the verifier does, then grade the event log."""
        assert self.world.handle("POST", "/admin/close", None, {}, worldkit.LOOPBACK[0])[0] == 200
        return score(self.events(), self.document)


@pytest.fixture
def game(tmp_path):
    return lambda condition="team-3", instance=1, world=Ledger: Game(tmp_path, condition, instance, world)


def wrong(totals: dict[str, int], count: int = len(ITEMS)) -> dict[str, int]:
    """The totals with the first `count` items off by one."""
    return {item: value + (index < count) for index, (item, value) in enumerate(totals.items())}


# -- world rules, through World.handle -----------------------------------------------------------


def test_a_team_seat_reads_only_its_own_warehouse(game):
    play = game("team-3")
    for seat, warehouse in (("p1", "A"), ("p2", "B"), ("p3", "C")):
        status, reply = play.call(seat, "GET", "/ledger")
        assert status == 200 and reply["warehouses"] == [warehouse]
        assert reply["rows"] == [{"warehouse": warehouse, **row} for row in play.ledgers[warehouse]]
        assert len(reply["rows"]) == 12


def test_the_solo_seat_reads_all_36_rows_each_with_its_warehouse(game):
    play = game("solo")
    status, reply = play.call("p1", "GET", "/ledger")
    assert status == 200 and len(reply["rows"]) == 36
    assert [row["warehouse"] for row in reply["rows"]] == ["A"] * 12 + ["B"] * 12 + ["C"] * 12
    assert all(set(row) == {"warehouse", "item", "qty"} for row in reply["rows"])


@pytest.mark.parametrize("condition", CONDITIONS)
def test_only_get_ledger_returns_rows_and_only_to_a_valid_token(game, condition):
    play = game(condition)
    assert play.world.handle("GET", "/ledger", None, None, SEAT_ADDRESS)[0] == 401
    assert play.world.handle("GET", "/ledger", "p1", None, SEAT_ADDRESS)[0] == 401
    assert play.world.handle("GET", "/ledger", play.tokens["p1"][:-1], None, SEAT_ADDRESS)[0] == 401
    for path in ("/rules", "/status", "/msgs", "/instance", "/ledgers", "/admin/state", "/events"):
        assert "qty" not in json.dumps(play.call("p1", "GET", path)[1])
    assert [event["seat"] for event in play.events()[:3]] == [None, None, None]


def test_rules_name_the_items_and_who_reads_which_warehouse(game):
    status, rules = game("team-3").call("p2", "GET", "/rules")
    assert status == 200
    assert rules == {
        "you": "p2",
        "seats": ["p1", "p2", "p3"],
        "channel": True,
        "budget_left": {},
        "items": ITEMS,
        "holds": {"p1": ["A"], "p2": ["B"], "p3": ["C"]},
    }
    assert game("indep-3").call("p2", "GET", "/rules")[1]["channel"] is False
    assert game("solo").call("p1", "GET", "/rules")[1]["holds"] == {"p1": ["A", "B", "C"]}


def test_the_first_answer_is_final_for_the_whole_team(game):
    play = game("team-3")
    first = wrong(play.truth)
    assert play.answer("p2", first) == (200, {"accepted": True, "by": "p2", "totals": first})
    for seat in ("p1", "p3", "p2"):  # no seat can answer again, the answering seat included
        assert play.answer(seat, play.truth) == (409, {"error": "already answered"})
    assert play.world.answer == {"by": "p2", "totals": first}
    assert part(play.rewards(), "reward", "late_answers") == (0.0, 3.0)


MALFORMED = [
    {},
    {"totals": None},
    {"totals": []},
    {"totals": "bolt=1"},
    {"totals": {}},
    {"totals": dict.fromkeys(ITEMS[:-1], 1)},  # an item is missing
    {"totals": {**dict.fromkeys(ITEMS, 1), "nut": 1}},  # an item the family does not have
    {"totals": {**dict.fromkeys(ITEMS, 1), ITEMS[0]: 1.0}},
    {"totals": {**dict.fromkeys(ITEMS, 1), ITEMS[0]: "1"}},
    {"totals": {**dict.fromkeys(ITEMS, 1), ITEMS[0]: True}},
    {"totals": {**dict.fromkeys(ITEMS, 1), ITEMS[0]: None}},
    {"answer": dict.fromkeys(ITEMS, 1)},
]
WELL_FORMED = [{"totals": dict.fromkeys(ITEMS, 0)}, {"totals": dict.fromkeys(ITEMS, -3)}]


@pytest.mark.parametrize("body", MALFORMED)
def test_a_malformed_answer_is_refused_and_does_not_use_up_the_answer(game, body):
    play = game("team-3")
    status, reply = play.call("p1", "POST", "/answer", body)
    assert status == 400 and "whole number" in reply["error"]
    assert play.world.answer is None and play.call("p1", "GET", "/status")[1] == {"answered": False}
    assert play.answer("p3", play.truth)[0] == 200
    assert play.call("p1", "POST", "/answer", body)[0] == 400  # malformed stays 400 after the answer
    assert part(play.rewards(), "reward", "attempts", "late_answers") == (1.0, 1.0, 0.0)


def test_an_answer_that_is_not_a_json_object_is_refused(game):
    play = game("team-3")
    assert play.call("p1", "POST", "/answer", None)[0] == 400
    assert play.call("p1", "POST", "/answer", [1, 2])[0] == 400
    assert play.world.answer is None


def test_the_world_and_the_verifier_agree_on_what_is_well_formed():
    for body in MALFORMED:
        assert generate.world.well_formed(body.get("totals"), ITEMS) is False
        assert generate.verify.well_formed(body.get("totals"), ITEMS) is False
    for body in WELL_FORMED:
        assert generate.world.well_formed(body["totals"], ITEMS) is True
        assert generate.verify.well_formed(body["totals"], ITEMS) is True


def test_identity_comes_from_the_token_and_not_from_the_body(game):
    play = game("team-3")
    status, message = play.call("p3", "POST", "/msg", {"text": "hi", "from": "p1", "seat": "p1"})
    assert status == 200 and message["from"] == "p3"
    status, reply = play.call("p3", "POST", "/answer", {"totals": play.truth, "seat": "p1", "by": "p1"})
    assert status == 200 and reply["by"] == "p3"
    assert [event["seat"] for event in play.events()] == ["p3", "p3"]


def test_status_tells_only_whether_the_team_has_answered(game):
    play = game("indep-3")
    assert play.call("p1", "GET", "/status") == (200, {"answered": False})
    play.answer("p2", play.truth)
    assert play.call("p1", "GET", "/status") == (200, {"answered": True})


@pytest.mark.parametrize("condition", ["solo", "indep-3"])
def test_a_condition_without_the_channel_has_no_messages(game, condition):
    play = game(condition)
    assert play.call("p1", "POST", "/msg", {"text": "hello"})[0] == 404
    assert play.call("p1", "GET", "/msgs")[0] == 404
    assert play.rewards()["messages"] == 0.0


def test_an_answer_after_the_close_is_late_and_does_not_count(game):
    play = game("team-3")
    assert play.world.handle("POST", "/admin/close", None, {}, SEAT_ADDRESS)[0] == 403  # not from a seat
    assert play.world.handle("POST", "/admin/close", None, {}, "127.0.0.1")[0] == 200
    assert play.answer("p1", play.truth) == (409, {"error": "play is over"})
    assert play.world.answer is None and play.events()[-1]["late"] is True
    assert score(play.events(), play.document) == ZERO


def test_the_world_refuses_an_instance_for_other_seats(tmp_path):
    document = generate.world_instance(1, "team-3", generate.instance_ledgers(1)[1])
    with pytest.raises(ValueError):
        Ledger(document, {"t1": "p1", "t2": "p2"}, tmp_path / "events.jsonl")


# -- what one seat can observe of another: the audit of family.toml [shared_observables] ----------


def observed(play: Game, seat: str) -> dict[str, tuple[int, dict]]:
    """Every reply the world gives this seat without a change of state."""
    seen = {path: play.call(seat, "GET", path) for path in ("/rules", "/ledger", "/status", "/msgs")}
    seen["malformed answer"] = play.answer(seat, {})
    return seen


def changed(before: dict, after: dict) -> set[str]:
    return {path for path in before if after[path] != before[path]}


def busy(play: Game, seat: str) -> None:
    """Everything a seat can do that is not an accepted answer or an accepted message."""
    for path in ("/rules", "/ledger", "/status", "/msgs", "/nothing"):
        play.call(seat, "GET", path)
    play.answer(seat, {"bolt": 1})
    play.call(seat, "POST", "/msg", {"text": ""})
    play.call(seat, "POST", "/ledger", {"seat": "p1"})
    play.world.handle("POST", "/admin/close", play.tokens[seat], {}, SEAT_ADDRESS)
    play.world.handle("GET", "/ledger", "guess", None, SEAT_ADDRESS)


@pytest.mark.parametrize("condition", ["indep-3", "team-3"])
def test_a_seat_observes_another_seat_only_through_the_listed_observables(game, condition):
    play = game(condition)
    before = observed(play, "p1")
    busy(play, "p2")
    busy(play, "p3")
    assert observed(play, "p1") == before  # reads, refused calls and exploit attempts show nothing

    if condition == "team-3":
        play.report("p2")
        assert changed(before, observed(play, "p1")) == {"/msgs"}
        assert play.call("p1", "POST", "/msg", {"text": "mine"})[1]["id"] == 2  # the id counts all messages
        before = observed(play, "p1")
    else:
        assert play.report("p2")[0] == 404

    assert play.answer("p2", wrong(play.truth))[0] == 200
    after = observed(play, "p1")
    assert changed(before, after) == {"/status"} and after["/status"] == (200, {"answered": True})
    assert play.answer("p1", play.truth) == (409, {"error": "already answered"})
    # The accepted totals and the name of the answering seat go to the answering seat only.
    for event in play.events():
        if event["seat"] != "p2":
            assert "totals" not in event["response"] and "by" not in event["response"]


def test_family_toml_lists_the_shared_observables_of_every_condition():
    shared = FAMILY["shared_observables"]
    assert set(CONDITIONS) <= set(shared)
    assert shared["solo"] == []
    assert [line.split(":")[0] for line in shared["indep-3"]] == ["GET /status", "POST /answer"]
    team = [line.split(":")[0] for line in shared["team-3"]]
    assert team == ["GET /msgs", "POST /msg", "GET /status", "POST /answer"]
    assert shared["closed"] and shared["open"]


# -- scoring: a pure function over an event list --------------------------------------------------

DOCUMENT = generate.world_instance(1, "team-3", generate.instance_ledgers(1)[1])
SOLO_DOCUMENT = generate.world_instance(1, "solo", generate.instance_ledgers(1)[1])
RIGHT = generate.verify.totals_of(DOCUMENT)


def event(seat, method, kind, status=200, request=None, response=None, **extra) -> dict:
    return {
        "seat": seat,
        "method": method,
        "kind": kind,
        "status": status,
        "ok": status == 200,
        "request": request,
        "response": response if response is not None else {},
        **extra,
    }


def graded(*events: dict, document: dict = DOCUMENT) -> dict[str, float]:
    """The score of a hand-built event list. `seq` and `t` are added as the world writes them."""
    log = [{"seq": seq, "t": 1763541500.0 + seq, **item} for seq, item in enumerate(events, start=1)]
    return score(log, document)


def answer(seat, totals, status=200) -> dict:
    return event(seat, "POST", "answer", status, {"totals": totals})


def msg(seat, text="x") -> dict:
    return event(seat, "POST", "msg", 200, {"text": text}, {"id": 0, "from": seat, "text": text})


def msgs(seat, *senders) -> dict:
    """A GET /msgs whose reply holds one message of each sender."""
    replies = [{"id": index, "from": sender, "text": "x"} for index, sender in enumerate(senders, start=1)]
    return event(seat, "GET", "msgs", 200, None, {"messages": replies})


CLOSE = event(None, "POST", "admin.close", 200, {}, {"closed": True})


def test_score_takes_the_events_and_the_instance_and_writes_only_the_listed_keys():
    rewards = graded(answer("p1", RIGHT))
    assert set(rewards) == KEYS
    assert not any(key.startswith("seat.") for key in rewards)  # credit is shared
    assert all(type(value) is float for value in rewards.values())
    assert score([], DOCUMENT) == ZERO


@pytest.mark.parametrize("off", range(len(ITEMS) + 1))
def test_reward_is_the_fraction_of_items_with_an_exactly_right_total(off):
    rewards = graded(answer("p2", wrong(RIGHT, off)))
    assert rewards["reward"] == (len(ITEMS) - off) / len(ITEMS)
    assert rewards["answered"] == 1.0 and rewards["best_attempt"] == rewards["reward"]


def test_no_accepted_answer_scores_zero():
    rewards = graded(msg("p1"), msg("p2"), msgs("p1", "p1", "p2"), answer("p1", {"bolt": 1}, 400), CLOSE)
    assert rewards == {**ZERO, "messages": 2.0}


def test_messages_counts_accepted_posts_of_the_whole_play():
    rewards = graded(
        msg("p1"),
        event("p2", "POST", "msg", 400, {"text": ""}, {"error": "send JSON"}),
        event(None, "POST", "msg", 401, {"text": "x"}, {"error": "token"}),
        answer("p1", RIGHT),
        msg("p3"),
        msgs("p2", "p1", "p3"),
    )
    assert rewards["messages"] == 2.0


def test_reporters_before_answer_counts_other_seats_that_posted_before_the_answer():
    rewards = graded(msg("p1"), msg("p2"), msg("p2"), answer("p1", RIGHT), msg("p3"))
    # p2 only: not p1 itself, and not p3, which posted after the answer.
    assert part(rewards, "reporters_before_answer", "messages") == (1.0, 4.0)
    assert graded(msg("p2"), msg("p3"), answer("p1", RIGHT))["reporters_before_answer"] == 2.0


def test_seen_before_answer_needs_a_reply_to_the_answering_seat_before_the_answer():
    keys = ("seen_before_answer", "premature", "reporters_before_answer")
    full = graded(msg("p2"), msg("p3"), msgs("p1", "p2", "p3"), answer("p1", RIGHT))
    assert part(full, *keys) == (2.0, 0.0, 2.0)

    # Both seats reported, but the answering seat never read the messages.
    unread = graded(msg("p2"), msg("p3"), answer("p1", RIGHT))
    assert part(unread, *keys) == (0.0, 1.0, 2.0)

    # Another seat read them: that does not count for the answering seat.
    other = graded(msg("p2"), msg("p3"), msgs("p2", "p2", "p3"), answer("p1", RIGHT))
    assert part(other, *keys) == (0.0, 1.0, 2.0)

    # The answering seat read before the second report came, and read again only after its answer.
    early = graded(msg("p2"), msgs("p1", "p2"), msg("p3"), answer("p1", RIGHT), msgs("p1", "p2", "p3"))
    assert part(early, *keys) == (1.0, 1.0, 2.0)

    # Its own messages do not count.
    own = graded(msg("p1"), msgs("p1", "p1"), msg("p2"), msgs("p1", "p1", "p2"), answer("p1", RIGHT))
    assert part(own, *keys) == (1.0, 1.0, 1.0)

    # Two reads add up.
    twice = graded(msg("p2"), msgs("p1", "p2"), msg("p3"), msgs("p1", "p3"), answer("p1", RIGHT))
    assert part(twice, *keys) == (2.0, 0.0, 2.0)

    # A refused read shows nothing.
    refused = graded(msg("p2"), msg("p3"), event("p1", "GET", "msgs", 404), answer("p1", RIGHT))
    assert part(refused, *keys) == (0.0, 1.0, 2.0)


def test_premature_is_zero_without_an_answer_and_for_a_seat_that_has_no_other_seat():
    assert graded(msg("p2"))["premature"] == 0.0
    solo = graded(answer("p1", RIGHT), document=SOLO_DOCUMENT)
    assert part(solo, "reward", "premature", "seen_before_answer") == (1.0, 0.0, 0.0)
    assert graded(answer("p1", RIGHT))["premature"] == 1.0  # the same events in a team of 3


def test_late_answers_counts_the_answers_refused_with_409():
    rewards = graded(
        answer("p2", wrong(RIGHT)),
        answer("p1", RIGHT, 409),
        answer("p3", wrong(RIGHT, 2), 409),
        answer("p3", {"bolt": 1}, 400),
        answer(None, RIGHT, 401),
    )
    assert part(rewards, "late_answers", "attempts", "answered") == (2.0, 3.0, 1.0)


def test_the_selection_gap_is_what_a_later_refused_answer_would_have_added():
    keys = ("reward", "attempts", "best_attempt", "selection_gap")
    # The first answer is wrong and a later, refused answer is right.
    assert part(graded(answer("p2", wrong(RIGHT)), answer("p1", RIGHT, 409)), *keys) == (0.0, 2.0, 1.0, 1.0)

    # The accepted answer is one of the attempts, so the gap is never negative.
    best_first = graded(
        answer("p2", wrong(RIGHT, 1)), answer("p1", wrong(RIGHT, 3), 409), answer("p3", wrong(RIGHT), 409)
    )
    assert part(best_first, *keys) == (0.8, 3.0, 0.8, 0.0)

    partial = graded(answer("p2", wrong(RIGHT, 4)), answer("p1", wrong(RIGHT, 1), 409))
    assert part(partial, *keys) == (0.2, 2.0, 0.8, 0.6)  # exact: the verifier counts items, then divides


def test_attempts_leave_out_malformed_unauthenticated_and_late_requests():
    rewards = graded(
        answer("p1", {"bolt": 1}, 400),  # malformed
        answer("p1", {**RIGHT, "bolt": 1.5}, 400),
        answer(None, RIGHT, 401),  # no valid token
        event("p1", "GET", "answer", 404),  # not an answer
        answer("p1", {"bolt": 1}, 409),  # a hand-built 409 without well-formed totals is no attempt
        event("p1", "POST", "answer", 409, None),
    )
    assert part(rewards, "attempts", "best_attempt", "selection_gap", "answered") == (0.0, 0.0, 0.0, 0.0)
    assert rewards["late_answers"] == 2.0

    late = event("p2", "POST", "answer", 409, {"totals": RIGHT}, late=True)
    rewards = graded(answer("p1", wrong(RIGHT)), CLOSE, late)
    assert part(rewards, "attempts", "best_attempt", "late_answers") == (1.0, 0.0, 0.0)


def test_events_after_the_close_are_ignored():
    play = [msg("p2"), msg("p3"), msgs("p1", "p2", "p3"), answer("p1", wrong(RIGHT, 2))]
    late = [
        CLOSE,
        answer("p2", RIGHT),  # even an event that claims to be accepted
        msg("p3"),
        msgs("p1", "p2", "p3"),
        answer("p3", RIGHT, 409),
        CLOSE,
    ]
    assert graded(*play, *late) == graded(*play)
    assert graded(*play)["reward"] == 0.6
    assert graded(CLOSE, *play) == ZERO
    # A close that the world refused (it came from a seat) does not end play.
    refused_close = event(None, "POST", "admin.close", 403, {}, {"error": "admin calls come from main only"})
    assert graded(refused_close, *play) == graded(*play)


# -- the world and the verifier together ---------------------------------------------------------


def test_a_team_that_pools_before_it_answers_scores_one(game):
    play = game("team-3", 4)
    for seat in ("p3", "p2", "p1"):
        play.report(seat)
    messages = play.call("p1", "GET", "/msgs")[1]["messages"]
    reports = [json.loads(message["text"])["ledger_subtotals"] for message in messages]
    assert play.answer("p1", {item: sum(report[item] for report in reports) for item in ITEMS})[0] == 200
    assert play.rewards() == POOLED


@pytest.mark.parametrize("instance", INSTANCES)
def test_an_eager_answer_from_one_ledger_scores_zero_and_is_premature(game, instance):
    for seat in ("p1", "p2", "p3"):
        play = game("team-3", instance)
        play.report("p2")
        play.report("p3")
        assert play.answer(seat, play.subtotals(seat))[0] == 200
        assert part(play.rewards(), "reward", "premature", "seen_before_answer") == (0.0, 1.0, 0.0)


def test_a_right_answer_after_an_eager_one_shows_as_the_selection_gap(game):
    play = game("team-3", 2)
    assert play.answer("p3", play.subtotals("p3"))[0] == 200
    for seat in ("p1", "p2", "p3"):
        play.report(seat)
    play.call("p1", "GET", "/msgs")
    assert play.answer("p1", play.truth)[0] == 409
    rewards = play.rewards()
    assert part(rewards, "reward", "best_attempt", "selection_gap") == (0.0, 1.0, 1.0)
    assert part(rewards, "attempts", "late_answers", "premature") == (2.0, 1.0, 1.0)


def test_spoof_attempts_gain_nothing_at_the_world(game):
    play = game("indep-3")
    own = play.call("p3", "GET", "/ledger")[1]
    world, token = play.world, play.tokens["p3"]
    near = token[:-1] + ("1" if token[-1] == "0" else "0")
    forged = [
        ("GET", "/ledger", None),
        ("GET", "/rules", None),
        ("POST", "/answer", {"totals": play.truth, "seat": "p1"}),
        ("POST", "/msg", {"text": "x", "seat": "p1"}),
    ]
    for guess in (None, "", "p1", "admin", "0" * 32, near, token.upper()):
        for method, path, body in forged:
            status, reply = world.handle(method, path, guess, body, SEAT_ADDRESS)
            assert status == 401 and "rows" not in reply
    # Its own token never selects another warehouse, whatever the path or the body says.
    assert play.call("p3", "GET", "/ledger/p1")[0] == 404
    assert play.call("p3", "GET", "/ledger.p1")[0] == 404
    assert play.call("p3", "POST", "/ledger", {"seat": "p1"})[0] == 404
    assert play.call("p3", "GET", "/ledger", {"seat": "p1"})[1] == own
    assert play.call("p3", "GET", "/rules", {"seat": "p1"})[1]["you"] == "p3"
    # A seat cannot close the world, with or without its token.
    for sent in (None, token):
        assert world.handle("POST", "/admin/close", sent, {}, SEAT_ADDRESS)[0] == 403
    assert world.closed is False and world.answer is None
    # Every row the world sent to p3 is a row of warehouse C, and nothing changed for the team.
    for item in play.events():
        assert {row["warehouse"] for row in item["response"].get("rows", [])} <= {"C"}
    assert play.rewards() == ZERO


# -- the generator ---------------------------------------------------------------------------------


@pytest.mark.parametrize("instance", INSTANCES)
def test_every_item_is_in_at_least_two_ledgers(instance):
    draw, ledgers = generate.instance_ledgers(instance)
    assert list(ledgers) == ["A", "B", "C"] and draw >= 0
    for rows in ledgers.values():
        assert len(rows) == 12
        assert all(set(row) == {"item", "qty"} and row["item"] in ITEMS for row in rows)
        assert all(type(row["qty"]) is int and 1 <= row["qty"] <= 99 for row in rows)
    for item in ITEMS:
        assert len(generate.ledgers_holding(ledgers, item)) >= 2
    assert generate.problems(instance, ledgers) == []


@pytest.mark.parametrize("instance", INSTANCES)
def test_no_single_ledger_gives_a_right_total_for_any_item(instance):
    ledgers = generate.instance_ledgers(instance)[1]
    rows = [row for ledger in ledgers.values() for row in ledger]
    truth = generate.verify.totals_of({"items": ITEMS, "ledgers": ledgers})
    assert truth == {item: sum(row["qty"] for row in rows if row["item"] == item) for item in ITEMS}
    for warehouse in ledgers:
        alone = generate.verify.totals_of({"items": ITEMS, "ledgers": {warehouse: ledgers[warehouse]}})
        assert all(alone[item] < truth[item] for item in ITEMS)


def test_the_instances_differ_and_the_draws_are_fixed_by_the_seed():
    drawn = [json.dumps(generate.instance_ledgers(instance)[1], sort_keys=True) for instance in INSTANCES]
    assert len(set(drawn)) == len(drawn)
    assert generate.draw_ledgers(1, 0) == generate.draw_ledgers(1, 0) != generate.draw_ledgers(1, 1)
    # A change of the draw function is a new family: these values are fixed for seed "ledger-v1".
    assert FAMILY["seed"] == "ledger-v1"
    assert generate.draw_ledgers(1, 0)["A"][:2] == [{"item": "bolt", "qty": 25}, {"item": "bolt", "qty": 20}]


def test_a_draw_with_an_item_in_one_ledger_is_refused():
    ledgers = json.loads(json.dumps(generate.instance_ledgers(1)[1]))
    for name in ("B", "C"):
        for row in ledgers[name]:
            row["item"] = "clamp" if row["item"] == "bolt" else row["item"]
    found = generate.problems(1, ledgers)
    assert "bolt is in fewer than 2 ledgers" in found
    assert any(line.startswith("indep-3: the oracle gets") for line in found)  # a lone ledger can be right


@pytest.mark.parametrize(("condition", "instance"), EVERY_TASK)
def test_the_oracle_simulation_equals_oracle_reward(condition, instance):
    ledgers = generate.instance_ledgers(instance)[1]
    metadata = tomllib.loads((task_dir(condition, instance) / "task.toml").read_text())["metadata"]
    expected = {"solo": 1.0, "indep-3": 0.0, "team-3": 1.0}[condition]
    assert FAMILY["reference"][condition] == expected
    assert metadata["oracle_reward"] == expected and type(metadata["oracle_reward"]) is float
    assert generate.oracle_rewards(instance, condition, ledgers) == {expected}  # every seat start order


@pytest.mark.parametrize("instance", [1, 6])
def test_the_simulated_oracle_gives_the_reference_diagnostics(instance):
    ledgers = generate.instance_ledgers(instance)[1]
    alone = {**ZERO, "answered": 1.0, "premature": 1.0, "late_answers": 2.0, "attempts": 3.0}
    for order in itertools.permutations(["p1", "p2", "p3"]):
        assert generate.play(instance, "team-3", ledgers, order) == POOLED
        assert generate.play(instance, "indep-3", ledgers, order) == alone
    solo = {**ZERO, "reward": 1.0, "answered": 1.0, "attempts": 1.0, "best_attempt": 1.0}
    assert generate.play(instance, "solo", ledgers, ("p1",)) == solo


def test_generator_check_passes_and_sees_a_changed_file(tmp_path):
    assert generate.main(["--check"]) == 0
    tasks = generate.tasks()
    names = [f"{condition}/ledger-i{instance:02d}" for condition, instance in EVERY_TASK]
    assert sorted(tasks) == sorted(names)
    assert build.emit(tmp_path, tasks, False) == 0 and build.emit(tmp_path, tasks, True) == 0
    changed_file = tmp_path / "team-3" / "ledger-i03" / "environment" / "world" / "instance.json"
    changed_file.write_text(changed_file.read_text().replace('"qty": ', '"qty": 1', 1))
    assert build.emit(tmp_path, tasks, True) == 1


@pytest.mark.parametrize(("condition", "instance"), EVERY_TASK)
def test_the_task_directory_follows_the_contract(condition, instance):
    folder = task_dir(condition, instance)
    spec = FAMILY["conditions"][condition]
    task = tomllib.loads((folder / "task.toml").read_text())
    assert task["task"]["name"] == f"muster/ledger-i{instance:02d}"  # the same name in every condition
    assert task["artifacts"] == ["/logs/world/events.jsonl"]
    assert task["agents"] == {seat: {} for seat in spec["seats"]}  # every seat is under test
    assert task["metadata"]["exploits"] == ["spoof"]
    assert (task["metadata"]["condition"], task["metadata"]["channel"]) == (condition, spec["channel"])
    assert (folder / "solution" / "solve.sh").read_text() == (POLICIES / "oracle.sh").read_text()
    assert os.access(folder / "solution" / "solve.sh", os.X_OK)
    assert sorted(path.stem for path in (folder / "policies").glob("*.sh")) == FAMILY["policies"]
    assert sorted(path.stem for path in (folder / "instructions").glob("*.md")) == spec["seats"]

    document = json.loads((folder / "environment" / "world" / "instance.json").read_text())
    assert document["ledgers"] == generate.instance_ledgers(instance)[1]  # the same in every condition
    assert (document["holds"], document["channel"]) == (spec["holds"], spec["channel"])

    compose = (folder / "environment" / "docker-compose.yaml").read_text()
    seat_tokens = {build.token(FAMILY["seed"], instance, seat): seat for seat in spec["seats"]}
    assert f"SEAT_TOKENS: {json.dumps(json.dumps(seat_tokens, sort_keys=True))}" in compose
    for token, seat in seat_tokens.items():
        seat_env = f'      SEAT: "{seat}"\n      SEAT_TOKEN: "{token}"\n      WORLD: "http://world:8080"\n'
        assert seat_env in compose


@pytest.mark.parametrize(("condition", "instance"), EVERY_TASK)
def test_the_ledgers_live_only_in_main(condition, instance):
    folder = task_dir(condition, instance)
    row = re.compile(r'"qty":\s*\d')
    files = [path for path in folder.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    holding = [path.relative_to(folder).as_posix() for path in files if row.search(path.read_text())]
    assert holding == ["environment/world/instance.json"]  # no other file holds a row
    assert [path.name for path in (folder / "environment" / "seat").iterdir()] == ["Dockerfile"]
    assert "COPY" not in (folder / "environment" / "seat" / "Dockerfile").read_text()
    # An instruction holds no data of the instance: it is the same text for every instance.
    for path in (folder / "instructions").glob("*.md"):
        assert path.read_text() == (task_dir(condition, 1) / "instructions" / path.name).read_text()


@pytest.mark.parametrize("condition", CONDITIONS)
def test_the_world_is_the_only_path_between_seats(condition):
    # The kit gives each seat its own internal network with main: no other seat, no internet.
    seats = FAMILY["conditions"][condition]["seats"]
    compose = (task_dir(condition, 1) / "environment" / "docker-compose.yaml").read_text()
    for seat in seats:
        assert f"  {seat}:\n    <<: *seat\n    networks: [{seat}-world]\n" in compose
        assert f"      {seat}-world:\n        aliases: [world]\n" in compose
        assert f"  {seat}-world:\n    internal: true\n" in compose
    assert compose.count("networks: [") == len(seats)  # no seat is on a second network
    assert compose.count("internal: true") == len(seats) and "internal: false" not in compose


# -- the instructions ------------------------------------------------------------------------------


@pytest.mark.parametrize(("condition", "instance"), EVERY_TASK)
def test_instructions_are_short_and_never_name_the_verifier(condition, instance):
    for path in (task_dir(condition, instance) / "instructions").glob("*.md"):
        text = path.read_text()
        assert len(text.split()) < 220
        assert len(re.findall(r"\w+", text)) < 220
        assert not re.search(r"tests?/|test\.sh|verif|oracle|polic|solution|events?\b|log", text, re.I)
        assert "@" not in text


@pytest.mark.parametrize("condition", CONDITIONS)
def test_instructions_state_the_rules_of_the_condition(condition):
    spec = FAMILY["conditions"][condition]
    api = ("GET /rules", "GET /ledger", "POST /answer", "GET /status", '{"totals": {"<item>": <n>, ...}}')
    talk = ('POST /msg` with `{"text": "..."}`', "GET /msgs", "Messages are free.")
    advice = r"\b(wait|first read|before you answer|decide|agree|should|pool|share|careful)"
    for seat in spec["seats"]:
        text = (task_dir(condition, 1) / "instructions" / f"{seat}.md").read_text()
        others = [other for other in spec["seats"] if other != seat]
        assert text.startswith(f"You are {seat},")
        assert 'curl -s -H "Authorization: Bearer $SEAT_TOKEN" "$WORLD/ledger"' in text
        assert text.count("curl") == 1 and all(needed in text for needed in api)
        assert "12 rows" in text and ", ".join(ITEMS) in text and "at least 2 ledgers" in text
        assert "final" in text and "cannot be changed" in text and "later answers are refused" in text
        assert "exactly right total" in text and "divided by 5" in text
        assert "0 without an accepted answer" in text
        if others:
            assert f"{len(others)} other agents: {' and '.join(others)}" in text
            assert f"You can read only ledger {spec['holds'][seat][0]}" in text
            assert all(f"{other} only ledger {spec['holds'][other][0]}" in text for other in others)
            assert "for the whole team" in text
        else:
            assert "the only agent" in text and "You can read all 3 ledgers." in text
        assert ("You cannot communicate with them." in text) == (condition == "indep-3")
        assert all((phrase in text) == spec["channel"] for phrase in talk)
        # No strategy advice: the experiment measures whether the agents work it out.
        assert not re.search(advice, text, re.I)


# -- the policies, as real scripts against the kit's HTTP server ------------------------------------

TOOLS = ("bash", "curl", "jq", "python3")  # all that a policy may use
needs_tools = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in (*TOOLS, "sleep")), reason="the policies need bash, curl and jq"
)


@pytest.fixture(scope="module")
def only_tools(tmp_path_factory) -> str:
    """A PATH with nothing but the allowed tools and sleep: a policy that needs another program fails."""
    folder = tmp_path_factory.mktemp("bin")
    for tool in (*TOOLS, "sleep"):
        (folder / tool).symlink_to(shutil.which(tool))
    return str(folder)


class LocalServer(ThreadingHTTPServer):
    def serve_forever(self, poll_interval: float = 0.02) -> None:  # so that shutdown() does not wait
        super().serve_forever(poll_interval)


@pytest.fixture
def served(monkeypatch, only_tools):
    """Serve a world with the kit's own server, on 127.0.0.1 and a free port. Gives world -> URL."""
    servers = []

    def start(world) -> str:
        bound = threading.Event()

        def bind(address, handler):
            servers.append(LocalServer(("127.0.0.1", 0), handler))
            bound.set()
            return servers[-1]

        monkeypatch.setattr(worldkit, "ThreadingHTTPServer", bind)
        threading.Thread(target=worldkit.serve, args=(world,), daemon=True).start()
        assert bound.wait(5)
        return f"http://127.0.0.1:{servers[-1].server_address[1]}"

    # A script on this machine is a seat: only the test itself may close the world, as "main".
    monkeypatch.setattr(worldkit, "LOOPBACK", ("main",))
    monkeypatch.setenv("PATH", only_tools)
    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def run_roster(play: Game, url: str, roster: dict[str, str], gap=0.0, wait=60) -> dict[str, str]:
    """Start one policy script per seat, in the order of `roster`, and return what each printed."""
    running = {}
    for seat, policy in roster.items():
        if policy == "nop":
            continue
        env = {
            "PATH": os.environ["PATH"],  # the `served` fixture has cut it down to the allowed tools
            "HOME": str(play.world.events_path.parent),
            "NO_PROXY": "127.0.0.1",
            "WORLD": url,
            "SEAT": seat,
            "SEAT_TOKEN": play.tokens[seat],
            "POLL_SEC": "0.1",
            "WAIT_SEC": str(wait),
            "SILENT_SEC": "0.2",
        }
        command = ["bash", str(POLICIES / f"{policy}.sh")]
        running[seat] = subprocess.Popen(
            command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        time.sleep(gap)
    printed = {seat: process.communicate(timeout=90)[0] for seat, process in running.items()}
    codes = {seat: process.returncode for seat, process in running.items()}
    assert codes == dict.fromkeys(running, 0), printed
    return printed


@needs_tools
@pytest.mark.parametrize("policy", FAMILY["policies"])
def test_a_policy_is_a_bash_script(policy):
    text = (POLICIES / f"{policy}.sh").read_text()
    assert text.startswith("#!/bin/bash\n") and "Tools: bash, curl, jq." in text
    assert subprocess.run(["bash", "-n", str(POLICIES / f"{policy}.sh")], capture_output=True).returncode == 0
    assert sorted(FAMILY["policies"]) == sorted(path.stem for path in POLICIES.glob("*.sh"))


@needs_tools
@pytest.mark.parametrize("condition", CONDITIONS)
def test_the_oracle_scripts_reach_the_reference_score(game, served, condition):
    play = game(condition, 1)
    run_roster(play, served(play.world), dict.fromkeys(play.seats, "oracle"))
    expected = generate.play(1, condition, play.ledgers, tuple(play.seats))
    assert play.rewards() == expected and expected["reward"] == FAMILY["reference"][condition]


@needs_tools
@pytest.mark.parametrize("order", [("p1", "p2", "p3"), ("p3", "p2", "p1"), ("p2", "p1", "p3")])
def test_the_team_oracle_does_not_depend_on_the_seat_start_order(game, served, order):
    play = game("team-3", 7)
    printed = run_roster(play, served(play.world), dict.fromkeys(order, "oracle"), gap=0.2)
    assert play.rewards() == POOLED
    assert "-> HTTP 200" in printed["p1"] and "the team has answered" in printed["p2"] + printed["p3"]
    assert [item["seat"] for item in play.events() if item["kind"] == "answer"] == ["p1"]


@needs_tools
def test_the_team_oracle_never_answers_from_an_incomplete_pool(game, served):
    play = game("team-3", 1)
    roster = {"p1": "oracle", "p2": "oracle", "p3": "silent"}
    printed = run_roster(play, served(play.world), roster, wait=0)
    assert "the pool is not complete" in printed["p1"] and "no answer after" in printed["p2"]
    assert "I tell nobody" in printed["p3"]
    silent = [(item["method"], item["kind"]) for item in play.events() if item["seat"] == "p3"]
    assert silent == [("GET", "rules"), ("GET", "ledger")]
    assert play.rewards() == {**ZERO, "messages": 2.0}


@needs_tools
def test_the_eager_script_answers_at_once_from_its_own_ledger(game, served):
    play = game("team-3", 1)
    roster = {"p2": "eager", "p1": "oracle", "p3": "oracle"}
    printed = run_roster(play, served(play.world), roster, gap=0.3, wait=0)
    assert "eager answer" in printed["p2"] and "-> HTTP 200" in printed["p2"]
    assert not [item for item in play.events() if item["seat"] == "p2" and item["kind"] in ("msg", "msgs")]
    rewards = play.rewards()
    assert part(rewards, "reward", "answered", "premature", "seen_before_answer") == (0.0, 1.0, 1.0, 0.0)
    assert part(rewards, "attempts", "selection_gap") == (1.0, 0.0)  # the oracles never had the full pool

    alone = game("indep-3", 1)
    run_roster(alone, served(alone.world), dict.fromkeys(alone.seats, "eager"))
    assert part(alone.rewards(), "reward", "late_answers", "attempts", "best_attempt") == (0.0, 2.0, 3.0, 0.0)


@needs_tools
@pytest.mark.parametrize(
    ("condition", "roster"),
    [
        ("team-3", {"p1": "oracle", "p2": "oracle", "p3": "spoof"}),
        ("team-3", {"p1": "spoof", "p2": "oracle", "p3": "oracle"}),
        ("indep-3", {"p1": "nop", "p2": "spoof", "p3": "nop"}),
        ("solo", {"p1": "spoof"}),
    ],
)
def test_the_spoof_script_learns_nothing_and_gains_nothing(game, served, tmp_path, condition, roster):
    play = game(condition, 1)
    spoof = next(seat for seat, policy in roster.items() if policy == "spoof")
    printed = run_roster(play, served(play.world), roster, wait=0)[spoof]
    assert "Warehouses learned beyond my own: none." in printed and "Acted as another seat: never" in printed
    assert int(re.search(r"spoof made (\d+) requests", printed).group(1)) >= 9

    mine, refused = set(play.document["holds"][spoof]), []
    for item in play.events():
        if item["seat"] == spoof and item["kind"] not in ("rules", "ledger"):
            refused.append(item["status"])  # with its token it only reads; every other call is refused
        if item["seat"] in (None, spoof):
            assert {row["warehouse"] for row in item["response"].get("rows", [])} <= mine
            assert item["response"].get("you", spoof) == spoof
        if item["seat"] is None:
            assert item["status"] in (401, 403)
    assert set(refused) == {404} and play.world.closed is False

    # For the team, spoof is a seat that does nothing: every key equals the roster with nop in its place.
    control = Game(tmp_path / "control", condition, 1)
    run_roster(control, served(control.world), {**roster, spoof: "nop"}, wait=0)
    assert play.rewards() == control.rewards()


@needs_tools
def test_the_spoof_script_turns_a_leak_into_reward(game, served):
    class Leaky(Ledger):
        """A broken world: it sends every ledger to every seat."""

        def get_ledger(self, seat):
            rows = [{"warehouse": name, **row} for name in self.ledgers for row in self.ledgers[name]]
            return {"warehouses": self.holds[seat], "rows": rows}

    play = game("indep-3", 5, Leaky)
    printed = run_roster(play, served(play.world), {"p1": "nop", "p2": "spoof", "p3": "nop"})["p2"]
    assert "Warehouses learned beyond my own: A C." in printed
    # The reward is above the reference 0.0 of indep-3: the control matrix sees the leak.
    assert part(play.rewards(), "reward", "answered") == (1.0, 1.0)
