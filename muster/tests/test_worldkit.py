"""The world kit: identity, the event log, budgets, the channel and the close switch."""

import json

import pytest
import worldkit
from worldkit import Reject, World

TOKENS = {"t1": "p1", "t2": "p2"}


class Boxes(World):
    channel = True
    budgets = {"open": 2}  # noqa: RUF012

    def post_open(self, seat, body):
        if body.get("box") not in (1, 2, 3):
            raise Reject(400, "no such box")
        if body["box"] == 3:
            raise RuntimeError("bug in the family")
        return {"box": body["box"], "flag": body["box"] == 2}


@pytest.fixture
def world(tmp_path):
    return Boxes(TOKENS, tmp_path / "events.jsonl")


def events(world):
    return worldkit.load_events(world.events_path)


def test_identity_comes_from_the_token_and_not_from_the_body(world):
    status, reply = world.handle("POST", "/msg", "t2", {"text": "hi", "from": "p1", "seat": "p1"}, "10.0.0.3")
    assert status == 200 and reply["from"] == "p2"
    assert events(world)[0]["seat"] == "p2"


def test_a_request_without_a_valid_token_is_refused_and_logged(world):
    assert world.handle("GET", "/rules", None, None, "10.0.0.3")[0] == 401
    assert world.handle("GET", "/rules", "nope", None, "10.0.0.3")[0] == 401
    assert [e["seat"] for e in events(world)] == [None, None]


def test_budget_counts_successful_calls_per_seat(world):
    assert world.handle("POST", "/open", "t1", {"box": 9}, "x")[0] == 400  # refused: costs nothing
    assert world.handle("POST", "/open", "t1", {"box": 1}, "x") == (
        200,
        {"box": 1, "flag": False, "budget_left": 1},
    )
    assert world.handle("POST", "/open", "t1", {"box": 2}, "x")[1]["budget_left"] == 0
    assert world.handle("POST", "/open", "t1", {"box": 1}, "x")[0] == 429
    assert world.handle("POST", "/open", "t2", {"box": 1}, "x")[0] == 200  # p2 has its own budget
    assert world.handle("GET", "/rules", "t1", None, "x")[1]["budget_left"] == {"open": 0}


def test_close_needs_loopback_and_makes_later_requests_late(world):
    assert world.handle("POST", "/admin/close", "t1", {}, "10.0.0.3")[0] == 403
    assert world.handle("POST", "/msg", "t1", {"text": "before"}, "x")[0] == 200
    assert world.handle("POST", "/admin/close", None, {}, "127.0.0.1")[0] == 200
    assert world.handle("POST", "/msg", "t1", {"text": "after"}, "x")[0] == 409
    log = events(world)
    assert [e["seq"] for e in log] == [1, 2, 3, 4]
    assert log[3]["late"] is True and log[3]["ok"] is False
    assert [e["request"]["text"] for e in worldkit.in_play(log)] == ["before"]


def test_a_handler_bug_is_an_event_and_not_a_gap(world):
    assert world.handle("POST", "/open", "t1", {"box": 3}, "x")[0] == 500
    assert world.handle("POST", "/open", "t1", {"box": 1}, "x")[0] == 200
    assert [e["status"] for e in events(world)] == [500, 200]
    assert world.left("p1", "open") == 1


def test_channel_can_be_off_and_unknown_routes_are_404(tmp_path):
    class Silent(World):
        pass

    world = Silent(TOKENS, tmp_path / "e.jsonl")
    assert world.handle("POST", "/msg", "t1", {"text": "x"}, "x")[0] == 404
    assert world.handle("GET", "/msgs", "t1", None, "x")[0] == 404
    assert world.handle("GET", "/admin/state", "t1", None, "x")[0] == 404
    assert world.handle("POST", "/handle", "t1", {}, "x")[0] == 404  # no post_handle: methods are not routes
    assert world.handle("GET", "/health", None, None, "x") == (200, {"ok": True})
    assert len(events(world)) == 4


def test_load_events_rejects_a_gap(tmp_path):
    path = tmp_path / "e.jsonl"
    path.write_text(json.dumps({"seq": 1}) + "\n" + json.dumps({"seq": 3}) + "\n")
    with pytest.raises(ValueError):
        worldkit.load_events(path)


def test_write_rewards_refuses_non_finite_values(tmp_path):
    worldkit.write_rewards({"reward": 0.5, "seat.p1": 1}, tmp_path / "r.json")
    assert json.loads((tmp_path / "r.json").read_text()) == {"reward": 0.5, "seat.p1": 1.0}
    with pytest.raises(ValueError):
        worldkit.write_rewards({"reward": float("nan")}, tmp_path / "r.json")


def test_write_rewards_keeps_the_graded_event_log_next_to_the_reward(world, tmp_path):
    world.handle("POST", "/msg", "t1", {"text": "x"}, "x")
    world.handle("POST", "/admin/close", None, {}, "127.0.0.1")
    worldkit.write_rewards({"reward": 1}, tmp_path / "verifier" / "reward.json", world.events_path)
    graded = worldkit.load_events(tmp_path / "verifier" / "events.jsonl")
    assert [event["kind"] for event in graded] == ["msg", "admin.close"]
