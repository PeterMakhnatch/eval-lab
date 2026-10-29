from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from evallab.production import live_supervisor as ls
from evallab.production import live_supervisor_fleet as fl

NOW = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)
LANE = ls.Subject("omp_lane", "w1C:pJ", "fleet")


def _at(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def start(ago: float, name: str = "bash", call_id: str = "c", **args: Any) -> dict[str, Any]:
    return {"type": "custom", "customType": "tool_execution_start", "timestamp": _at(ago),
            "data": {"toolCallId": call_id, "toolName": name, "args": args, "intent": "x"}}


def result(ago: float, name: str = "bash", call_id: str = "c", text: str = "ok", error: bool = False) -> dict[str, Any]:
    return {"type": "message", "timestamp": _at(ago),
            "message": {"role": "toolResult", "toolCallId": call_id, "toolName": name, "isError": error,
                        "content": [{"type": "text", "text": text}]}}


def assistant(ago: float, tokens: int = 0, stop: str = "toolUse", error: str = "") -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "stopReason": stop, "usage": {"totalTokens": tokens},
                               "content": []}
    if error:
        message["errorMessage"] = error
    return {"type": "message", "timestamp": _at(ago), "message": message}


def model_change(ago: float, fallback: bool) -> dict[str, Any]:
    return {"type": "model_change", "timestamp": _at(ago), "model": "m/x", "resolvedModelIsFallback": fallback}


def observe(events: list[dict[str, Any]], **kwargs: Any) -> ls.Observation:
    return fl.lane_observation(events, subject=LANE, now=NOW, **kwargs)


def hits(events: list[dict[str, Any]], **kwargs: Any) -> dict[str, ls.TriggerHit]:
    observation = observe(events, **kwargs)
    return {h.trigger: h for h in fl.FleetSource().triggers(observation)}


# --------------------------------------------------------------------------- silence


@pytest.mark.parametrize(("age", "fires"), [(1799, False), (1800, True), (7200, True)])
def test_silence_boundary_for_a_working_lane(age: int, fires: bool) -> None:
    found = hits([result(age)])
    assert ("silence" in found) is fires
    if fires:
        assert found["silence"].kind == "latency"
        assert found["silence"].score == pytest.approx(age / 1800)


def test_silence_needs_a_working_agent() -> None:
    assert hits([result(7200)], agent_status="blocked") == {}


def test_open_call_with_unelapsed_timeout_excuses_silence() -> None:
    long_timeout = [start(3000, timeout=3600)]
    assert "silence" not in hits(long_timeout)
    elapsed = [start(3700, timeout=3600)]
    assert "silence" in hits(elapsed)
    # no declared timeout (or 0 = no deadline) is not an excuse
    assert "silence" in hits([start(3000)])
    assert "silence" in hits([start(3000, timeout=0)])
    # a result closes the call, so nothing is open and the timeout no longer excuses
    assert "silence" in hits([start(3500, timeout=3600), result(3000)])


def test_recent_subagent_activity_counts_as_liveness() -> None:
    events = [result(7200)]
    assert "silence" in hits(events)
    assert "silence" not in hits(events, activity_at=NOW - timedelta(seconds=60))


def test_events_after_now_are_ignored_and_empty_session_is_unknown() -> None:
    future = [result(-600)]
    observation = observe(future)
    assert observation.events == () and not observation.actor_inputs_available
    assert fl.FleetSource().triggers(observation) == []


def test_session_exit_is_terminal() -> None:
    exit_event = {"type": "custom", "customType": "session_exit", "timestamp": _at(5), "data": {"reason": "quit"}}
    assert observe([result(60), exit_event]).terminal


# --------------------------------------------------------------------------- repetition


def _identical_calls(n: int, name: str = "read", **args: Any) -> list[dict[str, Any]]:
    return [start(100 - i, name, f"c{i}", **args) for i in range(n)]


@pytest.mark.parametrize(("n", "fires"), [(2, False), (3, True), (6, True)])
def test_identical_consecutive_calls(n: int, fires: bool) -> None:
    found = hits(_identical_calls(n, path="a.py"))
    assert ("repetition" in found) is fires
    if fires:
        assert found["repetition"].kind == "loop"
        assert found["repetition"].score == pytest.approx(n / 3)
        assert len(found["repetition"].evidence_steps) == n


def test_intent_label_does_not_make_calls_different_and_other_args_do() -> None:
    same = [start(100, "read", "a", path="a.py", i="first"), start(90, "read", "b", path="a.py", i="second"),
            start(80, "read", "c", path="a.py", i="third")]
    assert "repetition" in hits(same)
    varied = _identical_calls(2, path="a.py") + [start(10, "read", "z", path="b.py")]
    assert "repetition" not in hits(varied)


def test_waits_are_not_identical_call_loops_but_empty_wait_returns_are() -> None:
    waits = [start(100 - i, "wait", f"w{i}") for i in range(3)]
    assert "repetition" not in hits(waits)

    def empty(n: int, final: str = "## Still Running (1)\n\n- bg_1") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(n):
            out += [start(100 - 10 * i, "wait", f"w{i}"),
                    result(95 - 10 * i, "wait", f"w{i}", "## Still Running (1)\n\n- bg_1")]
        out[-1] = result(5, "wait", f"w{n - 1}", final)
        return out

    assert "repetition" not in hits(empty(2))
    found = hits(empty(3))
    assert found["repetition"].kind == "loop"
    assert found["repetition"].detail.startswith("3 consecutive wait returns")
    # one completion at the end resets the run; so does any other tool result
    assert "repetition" not in hits(empty(3, final="## Completed (1)\n\n### bg_1"))
    interleaved = empty(3) + [start(3, "bash", "b"), result(2, "bash", "b")]
    assert "repetition" not in hits(interleaved)


# --------------------------------------------------------------------------- error_signature


def _storm(errors: int, fallbacks: int, ago: float = 30) -> list[dict[str, Any]]:
    return ([assistant(ago + i, stop="error", error="rate limit") for i in range(errors)]
            + [model_change(ago + 50 + i, True) for i in range(fallbacks)])


def test_error_signature_counts_errors_and_fallbacks_in_ten_minutes() -> None:
    assert "error_signature" not in hits(_storm(2, 2))
    found = hits(_storm(3, 2))
    assert found["error_signature"].kind == "infra"
    assert found["error_signature"].score == pytest.approx(1.0)
    assert "error_signature" in hits(_storm(5, 0))


def test_error_signature_window_edge_and_non_fallback_changes() -> None:
    assert "error_signature" in hits(_storm(5, 0, ago=599 - 4))
    assert "error_signature" not in hits(_storm(5, 0, ago=601))
    assert "error_signature" not in hits([model_change(10 + i, False) for i in range(8)])


# --------------------------------------------------------------------------- burn


def _hourly(buckets: int, tokens: int) -> list[dict[str, Any]]:
    """``tokens`` in each of ``buckets`` hours that precede the trailing hour."""
    return [assistant(3600 * (b + 1) + 1800, tokens) for b in range(buckets)]


def test_burn_compares_the_trailing_hour_with_the_lane_median() -> None:
    history = _hourly(8, 1_000_000)
    assert "burn" not in hits(history + [assistant(60, 2_999_999)])
    found = hits(history + [assistant(60, 3_000_000)])
    assert found["burn"].kind == "runaway"
    assert found["burn"].score == pytest.approx(1.0)


def test_burn_needs_a_baseline_of_enough_active_hours() -> None:
    assert "burn" not in hits(_hourly(5, 1_000_000) + [assistant(60, 50_000_000)])
    assert "burn" in hits(_hourly(6, 1_000_000) + [assistant(60, 50_000_000)])


def test_burn_uses_an_injected_baseline() -> None:
    assert "burn" in hits([assistant(60, 3_000)], median_hourly_tokens=1_000)
    assert "burn" not in hits([assistant(60, 2_999)], median_hourly_tokens=1_000)
    assert "burn" not in hits([assistant(60, 9_999_999)], median_hourly_tokens=None)


# --------------------------------------------------------------------------- wall_time and lane idle


def test_wall_time_from_open_queue_item() -> None:
    def queue(hours: float) -> fl.LaneQueue:
        return fl.LaneQueue(1, "CM-18", NOW - timedelta(hours=hours))

    assert "wall_time" not in hits([result(60)], queue=queue(2.99))
    found = hits([result(60)], queue=queue(3))
    assert found["wall_time"].kind == "runaway"
    assert found["wall_time"].score == pytest.approx(1.0)


def test_idle_lane_with_pending_items_is_silent_after_an_hour() -> None:
    assert hits([result(3599)], agent_status="idle", queue=fl.LaneQueue(2)) == {}
    found = hits([result(3600)], agent_status="done", queue=fl.LaneQueue(2))
    assert found["silence"].kind == "other"
    assert hits([result(7200)], agent_status="idle", queue=fl.LaneQueue(0)) == {}


# --------------------------------------------------------------------------- session tail and window


def test_events_window_is_the_last_twenty_with_digest_and_error_text() -> None:
    events = [start(300 - i, "bash", f"c{i}", command=f"echo {i}") for i in range(30)]
    events.append(result(10, "bash", "c29", "boom", error=True))
    observation = observe(events)
    assert len(observation.events) == 20
    assert observation.events[-1].text == "result bash ERROR boom"
    assert observation.events[-1].index == 30
    assert observation.events[0].text.startswith("call bash ") and "echo 11" in observation.events[0].text
    assert observation.metrics["last_event_age_s"] == 10


def test_read_session_tail_reads_backwards_and_survives_a_cut_first_line(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    filler = {"type": "message", "timestamp": _at(86400),
              "message": {"role": "user", "content": "x" * 1000}}
    recent = [result(50 - i, call_id=f"r{i}") for i in range(5)]
    path.write_text("\n".join(json.dumps(r) for r in [filler] * 800 + recent) + "\n", encoding="utf-8")
    records, truncated = fl.read_session_tail(path, since=NOW - timedelta(seconds=3600), min_records=3)
    assert not truncated
    assert records[-5:] == recent
    assert 5 <= len(records) < 800  # only a tail was read, not the 800 filler lines
    capped, cut = fl.read_session_tail(path, since=NOW - timedelta(days=30), max_bytes=fl.TAIL_START_BYTES)
    assert cut and capped[-5:] == recent


def test_read_session_tail_skips_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text("not json\n" + json.dumps(result(5)) + "\n{broken", encoding="utf-8")
    records, _ = fl.read_session_tail(path, since=NOW)
    assert len(records) == 1


def test_latest_subagent_activity(tmp_path: Path) -> None:
    session = tmp_path / "2026-10-28_abc.jsonl"
    session.write_text("", encoding="utf-8")
    assert fl.latest_subagent_activity(session) is None
    nested = tmp_path / "2026-10-28_abc" / "0-Agent"
    nested.mkdir(parents=True)
    (tmp_path / "2026-10-28_abc" / "A.jsonl").write_text("", encoding="utf-8")
    (nested / "B.jsonl").write_text("", encoding="utf-8")
    import os
    os.utime(nested / "B.jsonl", (1_800_000_000, 1_800_000_000))
    os.utime(tmp_path / "2026-10-28_abc" / "A.jsonl", (1_700_000_000, 1_700_000_000))
    assert fl.latest_subagent_activity(session) == datetime.fromtimestamp(1_800_000_000, UTC)


# --------------------------------------------------------------------------- owners


@pytest.mark.parametrize(
    ("pane", "label", "owner"),
    [
        ("wK:pH", "\N{PUSHPIN} Research", "peter"),  # pinned
        ("w1C:pJ", "\N{ROBOT FACE} QA Agent - Task", "peter"),  # routed chat
        ("w1C:pX", "\N{ROBOT FACE} Trace Tools", "fleet"),  # in the fleet registry although also routed
        ("w1C:p12", "Live Supervisor", "fleet"),
        ("w1C:pQ", "scratch", "fleet"),  # unknown defaults to fleet
        ("w1C:pX", "\N{PUSHPIN} Trace Tools", "peter"),  # pin wins over the registry
    ],
)
def test_owner_classification(pane: str, label: str, owner: str) -> None:
    got = fl.classify_owner(pane, label, routed_panes=frozenset({"w1C:pJ", "w1C:pX"}),
                            fleet_panes=frozenset({"w1C:pX", "w1C:p12"}))
    assert got == owner


def test_fleet_registry_panes_reads_first_column_only() -> None:
    table = "| Pane | Route |\n|---|---|\n| `w1C:pX` | `trace-tools` |\n| `wK:p1J` | `night-planner` |\n"
    assert fl.fleet_registry_panes(table) == frozenset({"w1C:pX", "wK:p1J"})


# --------------------------------------------------------------------------- keeper log and queue


LOG = """\
2026-10-27 16:58:24 EDT queue keeper start: 10 panes, 25 items
2026-10-28 10:00:00 EDT dispatch w1C:pS CE-11 status=idle -> ok
2026-10-28 10:01:00 EDT dispatch w1C:pJ OQ-6 status=idle -> ok
2026-10-28 10:05:00 EDT dispatch w1C:pK CM-1 -> FAILED: focused
2026-10-28 11:00:00 EDT outcome w1C:pS CE-11: done
2026-10-28 11:01:00 EDT dispatch w1C:pS CE-12 status=done -> ok
2026-10-28 11:30:00 EDT dispatch w1C:pT pT-SELF-abc-1 (self-directed) -> ok
2026-10-28 11:40:00 EDT dispatch w1C:pT pT-SELF-abc-2 (self-directed) -> ok
2026-10-28 11:41:00 EDT nudge w1C:pJ OQ-6 status=idle waiting=6min -> ok
"""


def test_parse_queue_log_tracks_open_items_and_timezones() -> None:
    log = fl.parse_queue_log(LOG)
    assert {p: i[0] for p, i in log.open_items.items()} == {
        "w1C:pJ": "OQ-6", "w1C:pS": "CE-12", "w1C:pT": "pT-SELF-abc-2"}
    assert log.open_items["w1C:pJ"][1] == datetime(2026, 10, 28, 14, 1, tzinfo=UTC)  # EDT is UTC-4
    assert not log.stopped_cleanly
    assert fl.parse_queue_log(LOG + "2026-10-28 12:00:00 EST queue keeper stop: time limit\n").stopped_cleanly
    assert fl.parse_queue_log("2026-10-28 12:00:00 XYZ dispatch a b -> ok\ngarbage\n").lines == ()


def test_pending_by_pane_counts_open_and_undispatched_items_and_skips_closed() -> None:
    queue = {"panes": {"p1": {"items": [{"id": "A"}, {"id": "B"}, {"id": "C"}]},
                       "p2": {"items": [{"id": "X"}]},
                       "p3": {"items": [{"id": "Y"}]},
                       "p4": {"items": [{"id": "Z"}]}}}
    state = {"p1": {"index": 1, "extra": [{"id": "D"}], "ledger": {"f1": {"id": "C", "outcome": "blocked"}}},
             "p2": {"index": 0, "extra": [], "ledger": {"f": {"id": "X", "outcome": "done"}}},
             "p3": {"index": 0, "extra": [], "parked": "quota block", "ledger": {}},
             "p4": {"index": -1, "extra": [], "ledger": {}}}
    assert fl.pending_by_pane(queue, state) == {"p1": 2, "p2": 0, "p4": 1}  # p1: B open + D; C blocked


# --------------------------------------------------------------------------- quota


def _usage(*limits: tuple[str, str, int | None, float]) -> dict[str, Any]:
    return {"reports": [{"provider": p, "limits": [
        {"id": f"{p}:{lid}", "label": lid, "window": {"durationMs": d},
         "amount": {"usedFraction": used}} for q, lid, d, used in limits if q == p]}
        for p in sorted({limit[0] for limit in limits})]}


def test_quota_windows_keeps_only_weekly_or_longer_limits() -> None:
    week = 7 * 86400 * 1000
    got = fl.quota_windows(_usage(("anthropic", "5 Hour", 5 * 3600 * 1000, 0.3),
                                  ("anthropic", "7 Day", week, 0.08),
                                  ("kimi", "Monthly total", None, 0.75),
                                  ("kimi", "5h limit", None, 0.4)))
    assert got == {"anthropic|anthropic:7 Day": ("7 Day", pytest.approx(8.0)),
                   "kimi|kimi:Monthly total": ("Monthly total", pytest.approx(75.0))}


def test_quota_rise_is_measured_against_the_six_hour_floor() -> None:
    history = [(NOW - timedelta(hours=7), 0.0), (NOW - timedelta(hours=5), 10.0), (NOW - timedelta(hours=1), 25.0)]
    assert fl.quota_rise(history, NOW, 31.0) == pytest.approx(21.0)  # the 7 h old point is outside the window
    assert fl.quota_rise([], NOW, 31.0) is None


@pytest.mark.parametrize(("rise", "fires"), [(20.0, False), (20.5, True)])
def test_quota_trigger_boundary(rise: float, fires: bool) -> None:
    observation = ls.Observation(ls.Subject("provider_quota", "anthropic", "other"), NOW, (),
                                 {"window": "7 Day", "used_points": 50.0, "rise_points": rise})
    got = fl.fleet_triggers(observation)
    assert bool(got) is fires
    if fires:
        assert got[0].trigger == "burn" and got[0].kind == "runaway"


# --------------------------------------------------------------------------- the source end to end


class FakeMachine:
    """Runner + home directory standing in for herdr, ps, omp usage and the filesystem."""

    def __init__(self, home: Path, *, keeper_running: bool = True) -> None:
        self.home = home
        self.keeper_running = keeper_running
        self.calls: list[tuple[str, ...]] = []
        self.usage = _usage(("anthropic", "7 Day", 7 * 86400 * 1000, 0.10))
        self.session = home / "sessions" / "lane.jsonl"
        self.session.parent.mkdir(parents=True)
        self.panes = [
            {"pane_id": "w1C:pJ", "tab_id": "w1C:tJ", "agent": "omp", "agent_status": "working",
             "agent_session": {"value": str(self.session)}},
            {"pane_id": "wK:pH", "tab_id": "wK:tH", "agent": "omp", "agent_status": "idle", "agent_session": {}},
            {"pane_id": "w1C:pF", "tab_id": "w1C:tF", "agent_status": "unknown"},
        ]
        self.tabs = [{"tab_id": "w1C:tJ", "label": "\N{ROBOT FACE} QA Agent - Task"},
                     {"tab_id": "wK:tH", "label": "\N{PUSHPIN} Research"}, {"tab_id": "w1C:tF", "label": "fxtr"}]

    def __call__(self, argv: Sequence[str]) -> str:
        self.calls.append(tuple(argv))
        if argv[:3] == ["herdr", "pane", "list"]:
            return json.dumps({"result": {"panes": self.panes}})
        if argv[:3] == ["herdr", "tab", "list"]:
            return json.dumps({"result": {"tabs": self.tabs}})
        if argv[0] == "ps":
            return "  1 /sbin/launchd\n" + (
                " 68325 /opt/Python.app/Contents/MacOS/Python queue_keeper.py --run run-x --hours 10\n"
                if self.keeper_running else " 99 grep queue_keeper.py --run run-x\n")
        if argv[:3] == ["omp", "usage", "--json"]:
            return json.dumps(self.usage)
        raise AssertionError(f"unexpected command {argv}")

    def write_config(self, *, log_lines: str, pending: bool = True) -> None:
        herdr = self.home / ".config/herdr"
        herdr.mkdir(parents=True)
        (herdr / "agent-routing.json").write_text(json.dumps({"agents": {
            "qa-task": {"pane_id": "w1C:pJ", "label": "QA Agent - Task"}, "foreman": {"kind": "service"}}}))
        doc = self.home / "Developer/research-context/omp/fleet.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("| Pane |\n|---|\n| `w1C:p12` |\n")
        run = self.home / "Developer/research-context/bridge/run-x"
        run.mkdir(parents=True)
        (run / "_queue.log").write_text(log_lines)
        (run / "queue.json").write_text(json.dumps(
            {"panes": {"w1C:pJ": {"items": [{"id": "OQ-6"}, {"id": "OQ-7"}] if pending else []}}}))
        (run / "queue-state.json").write_text(json.dumps({"w1C:pJ": {"index": 0, "extra": [], "ledger": {}}}))


def _write_session(path: Path, events: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def _fresh_log(ago_minutes: float, extra: str = "") -> str:
    local = (NOW - timedelta(minutes=ago_minutes) - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
    return f"{local} EDT dispatch w1C:pJ OQ-6 status=idle -> ok\n{extra}"


def test_observe_builds_subjects_owners_and_never_killable(tmp_path: Path) -> None:
    machine = FakeMachine(tmp_path)
    machine.write_config(log_lines=_fresh_log(10))
    _write_session(machine.session, [start(7200, "bash", "a", timeout=60), result(7190, "bash", "a")])
    source = fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x")
    observations = source.observe(NOW)
    lanes = {o.subject.id: o for o in observations if o.subject.kind == "omp_lane"}
    assert {i: o.subject.owner for i, o in lanes.items()} == {"w1C:pJ": "peter", "wK:pH": "peter", "w1C:pF": "fleet"}
    assert not any(o.subject.killable for o in observations)
    assert lanes["w1C:pJ"].subject.label.endswith("QA Agent - Task")
    # the non-omp pane and the pane without a session are subjects with no actor input
    assert not lanes["w1C:pF"].actor_inputs_available and not lanes["wK:pH"].actor_inputs_available
    assert lanes["w1C:pJ"].metrics["queue_pending"] == 2 and lanes["w1C:pJ"].metrics["queue_open_item"] == "OQ-6"
    assert [h.trigger for h in source.triggers(lanes["w1C:pJ"])] == ["silence"]
    keeper = next(o for o in observations if o.subject.kind == "queue_keeper")
    assert keeper.subject.owner == "fleet" and keeper.subject.id == "run-x"
    assert source.triggers(keeper) == []


def test_source_is_read_only_toward_subjects_and_uses_the_fake_runner(tmp_path: Path) -> None:
    machine = FakeMachine(tmp_path)
    machine.write_config(log_lines=_fresh_log(10))
    _write_session(machine.session, [result(60)])
    fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x").observe(NOW)
    assert {c[0] for c in machine.calls} == {"herdr", "ps", "omp"}
    assert all(c[1:3] in (("pane", "list"), ("tab", "list"), ("-axo", "pid=,command="), ("usage", "--json"))
               for c in machine.calls)


def test_keeper_hits(tmp_path: Path) -> None:
    machine = FakeMachine(tmp_path, keeper_running=False)
    machine.write_config(log_lines=_fresh_log(10))
    _write_session(machine.session, [result(60)])
    source = fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x")

    def keeper_hits(now: datetime = NOW) -> list[ls.TriggerHit]:
        obs = next(o for o in source.observe(now) if o.subject.kind == "queue_keeper")
        return source.triggers(obs)

    assert [h.trigger for h in keeper_hits()] == ["silence"]  # no process, items pending
    machine.keeper_running = True
    assert keeper_hits() == []
    assert keeper_hits(NOW + timedelta(minutes=49)) == []  # log is 59 min old
    late = keeper_hits(NOW + timedelta(minutes=50))
    assert late and late[0].score == pytest.approx(1.0) and "_queue.log silent" in late[0].detail
    # nothing pending: a missing process or a quiet log is not a problem
    (tmp_path / "Developer/research-context/bridge/run-x/queue.json").write_text(json.dumps({"panes": {}}))
    machine.keeper_running = False
    assert keeper_hits(NOW + timedelta(hours=5)) == []


def test_keeper_that_stopped_on_its_time_limit_is_not_reported(tmp_path: Path) -> None:
    machine = FakeMachine(tmp_path, keeper_running=False)
    machine.write_config(log_lines=_fresh_log(10, "2026-10-28 15:55:00 EDT queue keeper stop: time limit\n"))
    _write_session(machine.session, [result(60)])
    source = fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x")
    keeper = next(o for o in source.observe(NOW) if o.subject.kind == "queue_keeper")
    assert source.triggers(keeper) == []


def test_wall_time_end_to_end_from_the_queue_log(tmp_path: Path) -> None:
    machine = FakeMachine(tmp_path)
    machine.write_config(log_lines=_fresh_log(181))
    _write_session(machine.session, [result(60)])
    source = fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x")
    lane = next(o for o in source.observe(NOW) if o.subject.id == "w1C:pJ")
    assert [h.trigger for h in source.triggers(lane)] == ["wall_time"]


def test_quota_state_persists_and_fires_on_a_twenty_point_rise(tmp_path: Path) -> None:
    machine = FakeMachine(tmp_path)
    machine.write_config(log_lines=_fresh_log(10))
    _write_session(machine.session, [result(60)])
    source = fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x")

    def quota_hits(now: datetime) -> list[ls.TriggerHit]:
        obs = [o for o in source.observe(now) if o.subject.kind == "provider_quota"]
        assert [o.subject.id for o in obs] == ["anthropic"]
        return source.triggers(obs[0])

    assert quota_hits(NOW - timedelta(hours=5)) == []  # first snapshot: no history yet
    machine.usage = _usage(("anthropic", "7 Day", 7 * 86400 * 1000, 0.30))
    calls_before = sum(c[0] == "omp" for c in machine.calls)
    assert quota_hits(NOW - timedelta(hours=4, minutes=55)) == []  # inside the fetch interval: no new fetch
    assert sum(c[0] == "omp" for c in machine.calls) == calls_before
    got = quota_hits(NOW)  # +20 points exactly is not above 20; the snapshot is fresh and 30.0 - 10.0 = 20.0
    assert got == []
    machine.usage = _usage(("anthropic", "7 Day", 7 * 86400 * 1000, 0.3101))
    hit = quota_hits(NOW + timedelta(minutes=11))
    assert hit and hit[0].kind == "runaway" and hit[0].score > 1.0
    # a new source instance reads the same snapshots back from the state file
    assert (tmp_path / ".local/state/evallab-prod-live/live-supervisor/fleet-state.json").is_file()


def test_failing_herdr_raises_so_the_scan_reports_the_source(tmp_path: Path) -> None:
    def broken(argv: Sequence[str]) -> str:
        raise RuntimeError(f"herdr down: {argv[0]}")

    source = fl.FleetSource(runner=broken, home=tmp_path)
    log = ls.DecisionLog(tmp_path / "log")
    report = ls.scan_once([source], ls.RuleDecider(), log, now=NOW)
    assert report.errors == {"fleet": "RuntimeError: herdr down: herdr"}
