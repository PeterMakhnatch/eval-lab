from __future__ import annotations

import json
import subprocess
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
        self.extra_ps: list[str] = []
        self.process_info: dict[str, dict[str, Any]] = {}  # pane -> herdr process_info; absent = probe fails
        self.tabs = [{"tab_id": "w1C:tJ", "label": "\N{ROBOT FACE} QA Agent - Task"},
                     {"tab_id": "wK:tH", "label": "\N{PUSHPIN} Research"}, {"tab_id": "w1C:tF", "label": "fxtr"}]

    def __call__(self, argv: Sequence[str]) -> str:
        self.calls.append(tuple(argv))
        if argv[:3] == ["herdr", "pane", "list"]:
            return json.dumps({"result": {"panes": self.panes}})
        if argv[:3] == ["herdr", "tab", "list"]:
            return json.dumps({"result": {"tabs": self.tabs}})
        if argv[0] == "ps":
            return "    1     0     1 Ss   /sbin/launchd\n" + (
                " 68325     1 68325 S    /opt/Python.app/Contents/MacOS/Python queue_keeper.py --run run-x --hours 10\n"
                if self.keeper_running else " 99     1    99 S    grep queue_keeper.py --run run-x\n"
            ) + "".join(f"{row}\n" for row in self.extra_ps)
        if argv[:3] == ["herdr", "pane", "process-info"]:
            info = self.process_info.get(argv[4])
            if info is None:
                raise subprocess.CalledProcessError(1, list(argv))
            return json.dumps({"result": {"process_info": info}})
        if argv[:3] == ["omp", "usage", "--json"]:
            return json.dumps(self.usage)
        raise AssertionError(f"unexpected command {argv}")

    def add_pane(self, pane_id: str, label: str, session: Path | None, *, status: str = "working",
                 agent: str | None = "omp") -> None:
        tab_id = f"tab:{pane_id}"
        pane: dict[str, Any] = {"pane_id": pane_id, "tab_id": tab_id, "agent_status": status,
                                "agent_session": {"value": str(session)} if session else {}}
        if agent:
            pane["agent"] = agent
        self.panes.append(pane)
        self.tabs.append({"tab_id": tab_id, "label": label})

    def add_omp(self, pane_id: str, *, shell: int, omp: int, state: str = "S+", foreground: int | None = None,
                resume: Path | None = None, extra: str = "omp") -> None:
        """A pane whose shell ``shell`` runs ``omp``; ``foreground`` is the pane's foreground group (default the
        omp itself, i.e. a healthy lane)."""
        tail = f" --resume {resume}" if resume else ""
        self.extra_ps.append(f"{shell:6d}     1 {shell:5d} Ss   -zsh")
        self.extra_ps.append(f"{omp:6d} {shell:5d} {omp:5d} {state:<4} {extra}{tail} --model m/x")
        group = foreground if foreground is not None else omp
        name, pid = ("omp", omp) if group == omp else ("zsh", shell)
        self.process_info[pane_id] = {"foreground_process_group_id": group, "shell_pid": shell,
                                      "foreground_processes": [{"name": name, "pid": pid}]}

    def write_config(self, *, log_lines: str, pending: bool = True, fleet_md: str = "| `w1C:p12` |",
                     queue_panes: dict[str, list[str]] | None = None,
                     routing: dict[str, Any] | None = None) -> None:
        herdr = self.home / ".config/herdr"
        herdr.mkdir(parents=True)
        (herdr / "agent-routing.json").write_text(json.dumps({"agents": routing if routing is not None else {
            "qa-task": {"pane_id": "w1C:pJ", "label": "QA Agent - Task"}, "foreman": {"kind": "service"}}}))
        doc = self.home / "Developer/research-context/omp/fleet.md"
        doc.parent.mkdir(parents=True)
        doc.write_text(f"| Pane |\n|---|\n{fleet_md}\n")
        run = self.home / "Developer/research-context/bridge/run-x"
        run.mkdir(parents=True)
        (run / "_queue.log").write_text(log_lines)
        panes = queue_panes if queue_panes is not None else {
            "w1C:pJ": [{"id": "OQ-6"}, {"id": "OQ-7"}] if pending else []}
        (run / "queue.json").write_text(json.dumps(
            {"panes": {pane: {"items": items} for pane, items in panes.items()}}))
        (run / "queue-state.json").write_text(json.dumps(
            {pane: {"index": 0, "extra": [], "ledger": {}} for pane in panes}))


PRIVATE = "PRIVATE-CHAT-SENTINEL-9f31"


class ReadSpy:
    """Records every session path the source touches, and calls through."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths: list[Path] = []
        for name in ("read_session_tail", "hourly_baseline", "latest_subagent_activity"):
            monkeypatch.setattr(fl, name, self._wrap(getattr(fl, name)))

    def _wrap(self, fn: Any) -> Any:
        def spy(path: Path, *args: Any, **kwargs: Any) -> Any:
            self.paths.append(Path(path))
            return fn(path, *args, **kwargs)

        return spy


def test_unrelated_and_pinned_sessions_are_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    machine = FakeMachine(tmp_path)
    fleet_session = machine.session
    _write_session(fleet_session, [result(60)])
    private = tmp_path / "sessions" / "private.jsonl"
    _write_session(private, [start(60, "bash", "p", command=f"echo {PRIVATE}")])
    unrelated = tmp_path / "sessions" / "unrelated.jsonl"
    _write_session(unrelated, [start(60, "bash", "u", command=f"echo {PRIVATE}")])
    # a pinned tab, a routed-but-unlisted chat, and a plain unlisted pane all point at real sessions
    machine.panes[1]["agent_session"] = {"value": str(private)}  # wK:pH, label starts with the pin
    machine.add_pane("w19:pD", "Automation", unrelated)
    machine.write_config(log_lines=_fresh_log(10),
                         routing={"qa-task": {"pane_id": "w1C:pJ", "label": "QA Agent - Task"},
                                  "pinned": {"pane_id": "wK:pH", "session_ref": str(private)},
                                  "chat": {"pane_id": "w19:pD", "session_ref": str(unrelated)}})
    spy = ReadSpy(monkeypatch)
    source = fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x")
    observations = source.observe(NOW)

    lanes = {o.subject.id: o for o in observations if o.subject.kind == "omp_lane"}
    assert set(spy.paths) == {fleet_session}  # only the lane queue.json authorizes; nothing else is opened
    for pane_id in ("wK:pH", "w19:pD"):
        assert lanes[pane_id].events == ()
        assert lanes[pane_id].metrics["session_read"] is False
        assert lanes[pane_id].metrics["authorized"] is False
        assert not lanes[pane_id].actor_inputs_available
        assert fl.FleetSource().triggers(lanes[pane_id]) == []
    assert lanes["w1C:pJ"].metrics["session_read"] is True
    dumped = json.dumps([o.metrics for o in observations], default=str)
    assert PRIVATE not in dumped
    assert PRIVATE not in (tmp_path / ".local/state/evallab-prod-live/live-supervisor/fleet-state.json").read_text()
    assert str(private) not in (tmp_path / ".local/state/evallab-prod-live/live-supervisor/fleet-state.json").read_text()


def test_registry_lane_is_read_without_a_queue_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    machine = FakeMachine(tmp_path)
    _write_session(machine.session, [start(60, "bash", "a", command="echo fleet")])
    machine.write_config(log_lines=_fresh_log(10), fleet_md="| `w1C:pJ` |", queue_panes={})
    spy = ReadSpy(monkeypatch)
    source = fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x")
    lanes = {o.subject.id: o for o in source.observe(NOW) if o.subject.kind == "omp_lane"}
    assert set(spy.paths) == {machine.session}
    assert lanes["w1C:pJ"].metrics["authorized"] is True and lanes["w1C:pJ"].metrics["session_read"] is True


def test_pinned_lane_is_metadata_only_even_when_queued(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    machine = FakeMachine(tmp_path)
    _write_session(machine.session, [start(60, "bash", "a", command="echo pinned")])
    machine.write_config(log_lines=_fresh_log(10), fleet_md="| `w1C:p12` |",  # neither pane is authorized
                         queue_panes={"wK:pH": [{"id": "X"}]})
    machine.panes[1]["agent_session"] = {"value": str(machine.session)}
    spy = ReadSpy(monkeypatch)
    source = fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x")
    lanes = {o.subject.id: o for o in source.observe(NOW) if o.subject.kind == "omp_lane"}
    assert spy.paths == []  # w1C:pJ is not queued here, wK:pH is pinned
    assert lanes["wK:pH"].metrics["authorized"] is True and lanes["wK:pH"].metrics["session_read"] is False


def test_read_scope_helpers() -> None:
    authorized = fl.authorized_lanes(["w1C:pJ"], frozenset({"w1C:pX"}))
    assert authorized == frozenset({"w1C:pJ", "w1C:pX"})
    assert fl.may_read_session("w1C:pJ", "QA Agent - Task", authorized=authorized)
    assert fl.may_read_session("w1C:pX", "Trace Tools", authorized=authorized)
    # not in queue.json and not in the registry: no read, however Peter-owned or unrouted it is
    assert not fl.may_read_session("wK:pH", "\N{PUSHPIN} Research", authorized=authorized)
    assert not fl.may_read_session("w19:pD", "Automation", authorized=authorized)
    # a pinned tab is never read even when both lists name it
    assert not fl.may_read_session("w1C:pX", "\N{PUSHPIN} Trace Tools", authorized=authorized)


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
    assert all(c[1:3] in (("pane", "list"), ("tab", "list"), ("-axo", "pid=,ppid=,pgid=,stat=,command="),
                          ("usage", "--json"), ("pane", "process-info")) for c in machine.calls)


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


# --------------------------------------------------------------------------- frozen lanes (LS-FREEZE)


def process(rows: list[str], *, shell: int = 100, group: int | None = 101) -> fl.LaneProcess:
    """``lane_process`` over ps rows ``pid ppid pgid stat command``; ``group`` is the pane's foreground group."""
    return fl.lane_process(shell, group, ["omp" if group == 101 else "zsh"], fl.parse_process_table("\n".join(rows)))


SHELL = "100 1 100 Ss -zsh"


def test_healthy_lane_is_in_the_pane_foreground() -> None:
    lane = process([SHELL, "101 100 101 S+ omp --resume /s/a.jsonl --model m/x"])
    assert (lane.pids, lane.stopped, lane.background, lane.resume) == ((101,), None, False, Path("/s/a.jsonl"))


def test_omp_alive_but_outside_the_foreground_group_is_background() -> None:  # the 8 lanes in state S
    lane = process([SHELL, "101 100 101 S omp --model m/x"], group=100)
    assert (lane.pids, lane.stopped, lane.background) == ((101,), None, True)
    assert lane.foreground == "zsh"


def test_job_control_stopped_omp_and_tool_child_are_reported() -> None:  # the 4 lanes in state T
    assert process([SHELL, "101 100 101 T omp"], group=100).stopped == "omp pid 101 state T"
    stopped_child = process([SHELL, "101 100 101 S+ omp", "102 101 102 Ss bash -c x", "103 102 102 TN sleep 9"])
    assert stopped_child.stopped == "sleep pid 103 state TN" and stopped_child.background is False


def test_only_the_lanes_own_tree_and_only_interactive_omp_count() -> None:
    # a headless worker, a stopped editor beside omp and an omp under a wrapper
    rows = [SHELL, "101 100 101 S+ omp", "110 100 110 T vim notes", "120 100 120 S env omp -p x"]
    assert process(rows).stopped is None
    only_headless = process([SHELL, "101 100 101 S omp -p do-it", "102 100 102 S omp --print x"], group=100)
    assert (only_headless.pids, only_headless.background) == ((), False)
    wrapped = process([SHELL, "105 100 105 S+ env FOO=1 omp", "106 105 105 T+ /opt/bin/omp --resume /s/b.jsonl"])
    assert (wrapped.pids, wrapped.stopped) == ((106,), "omp pid 106 state T+")
    assert wrapped.resume == Path("/s/b.jsonl")
    assert process([SHELL, "101 100 101 S omp"], group=None).background is None


def test_parse_process_table_skips_malformed_rows() -> None:
    table = fl.parse_process_table("PID PPID PGID STAT COMMAND\n 7 1 7 Ss /sbin/x --a b\n\n 8 1\n 9 1 9 R\n")
    assert list(table) == [7] and table[7].argv == ("/sbin/x", "--a", "b")


def frozen_metrics(**extra: Any) -> ls.Observation:
    return ls.Observation(LANE, NOW, (), {"agent_status": "unknown", "omp_pids": [101], "omp_stopped": None,
                                          "omp_background": False, **extra}, actor_inputs_available=False)


@pytest.mark.parametrize(("extra", "text"), [
    ({"omp_background": True, "pane_foreground": "zsh"}, "alive outside the pane foreground (foreground: zsh)"),
    ({"omp_stopped": "omp pid 101 state T"}, "job-control stopped (omp pid 101 state T)"),
])
def test_frozen_process_is_a_frozen_trigger_the_rules_send_to_research(extra: dict[str, Any], text: str) -> None:
    (hit,) = fl.fleet_triggers(frozen_metrics(**extra))
    assert (hit.trigger, hit.kind) == ("frozen", "infra") and text in hit.detail and "omp pid 101" in hit.detail
    assert ls.propose(LANE, [hit]).action == "alert_research"
    assert fl.fleet_triggers(frozen_metrics()) == []  # omp in the foreground, nothing stopped


def stale(events: list[dict[str, Any]], *, status: str = "unknown", item: bool = True, **kwargs: Any) -> bool:
    queue = fl.LaneQueue(1, "OQ-6" if item else None, NOW - timedelta(hours=2) if item else None)
    return any(h.trigger == "frozen" for h in fl.fleet_triggers(observe(events, agent_status=status, queue=queue, **kwargs)))


@pytest.mark.parametrize(("age", "fires"), [(3599, False), (3600, True)])
def test_unknown_status_lane_mid_turn_with_an_item_in_flight_goes_stale_after_an_hour(age: int, fires: bool) -> None:
    assert stale([result(age)]) is fires
    assert stale([start(age)]) is fires  # an open call, with no declared timeout, is not an excuse


def test_stale_heartbeat_needs_an_open_turn_an_item_and_an_unknown_status() -> None:
    assert not stale([result(7200)], item=False)  # waiting for dispatch
    assert not stale([assistant(7200, stop="stop")])  # the turn ended: waiting for input
    assert not stale([start(7200, "ask", "q")])  # waiting for a human
    assert not stale([start(7200, "bash", "c", timeout=9000)])  # waiting on CI inside its declared timeout
    assert not stale([result(7200)], activity_at=NOW - timedelta(seconds=60))  # a subagent is writing
    for status in ("working", "idle", "done", "blocked"):  # herdr still sees the agent: other rules apply
        assert not stale([result(7200)], status=status)


def test_supervisor_gap_is_suspect_not_silent_and_ages_count_from_the_wake(tmp_path: Path) -> None:
    machine = FakeMachine(tmp_path)
    machine.write_config(log_lines=_fresh_log(10))
    _write_session(machine.session, [result(60)])
    source = fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x")

    def lane(at: datetime) -> tuple[ls.Observation, list[ls.TriggerHit]]:
        observation = next(o for o in source.observe(at) if o.subject.id == "w1C:pJ")
        return observation, source.triggers(observation)

    first, found = lane(NOW)
    assert first.metrics["supervisor_gap_s"] is None and found == []  # no previous cycle, no gap
    warm, found = lane(NOW + timedelta(seconds=fl.SUPERVISOR_GAP_S - 1))
    assert warm.metrics["supervisor_gap_s"] is None and not warm.metrics["heartbeat_suspect"]
    assert [h.trigger for h in found] == []

    wake = NOW + timedelta(hours=7)  # the host slept: the lane's last event and dispatch are 7 h old
    woken, found = lane(wake)
    assert woken.metrics["supervisor_gap_s"] == 7 * 3600 - (fl.SUPERVISOR_GAP_S - 1)
    assert woken.metrics["heartbeat_suspect"]
    assert woken.metrics["last_event_age_s"] == 0 and woken.metrics["queue_open_item_age_s"] == 0
    assert [(h.trigger, h.kind) for h in found] == [("frozen", "infra")]  # no wall_time, no 7 h silence
    assert "host sleep" in found[0].detail and ls.propose(LANE, found).action == "alert_research"

    again, found = lane(wake + timedelta(minutes=2))  # one decision per gap, then quiet
    assert again.metrics["supervisor_gap_s"] is None and again.metrics["last_event_age_s"] == 120 and found == []
    for minutes in (12, 22):  # cycles stay under the gap threshold
        lane(wake + timedelta(minutes=minutes))
    _, found = lane(wake + timedelta(minutes=31))  # silence is counted from the wake, so it still fires
    assert [h.kind for h in found] == ["latency"]
    state = json.loads((tmp_path / ".local/state/evallab-prod-live/live-supervisor/fleet-state.json").read_text())
    assert state["awake_since"] == wake.isoformat()


def test_supervisor_gap_ignores_lanes_with_no_work_in_flight(tmp_path: Path) -> None:
    machine = FakeMachine(tmp_path)
    machine.write_config(log_lines="", pending=False)  # nothing dispatched, nothing queued
    machine.panes[0]["agent_status"] = "idle"
    _write_session(machine.session, [result(60)])
    source = fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x")
    source.observe(NOW)
    lane = next(o for o in source.observe(NOW + timedelta(hours=7)) if o.subject.id == "w1C:pJ")
    assert lane.metrics["supervisor_gap_s"] > 0 and source.triggers(lane) == []


def frozen_machine(tmp_path: Path, *, state: str, group: int | None) -> FakeMachine:
    """w1C:pJ after the herdr agent was lost: no ``agent``, status unknown, omp only in the process table."""
    machine = FakeMachine(tmp_path)
    machine.write_config(log_lines=_fresh_log(10))
    _write_session(machine.session, [result(60)])
    pane = machine.panes[0]
    del pane["agent"]
    pane["agent_session"] = {}
    pane["agent_status"] = "unknown"
    machine.add_omp("w1C:pJ", shell=200, omp=201, state=state, foreground=group, resume=machine.session)
    return machine


@pytest.mark.parametrize(("state", "group"), [("T", 200), ("S", 200)])
def test_frozen_lane_is_observed_through_the_process_table(tmp_path: Path, state: str, group: int) -> None:
    machine = frozen_machine(tmp_path, state=state, group=group)
    source = fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x")
    lane = next(o for o in source.observe(NOW) if o.subject.id == "w1C:pJ")
    assert lane.metrics["omp_background"] is True and lane.metrics["omp_probe"] == "ok"
    assert (lane.metrics["omp_stopped"] is not None) is (state == "T")
    assert lane.metrics["session_read"] is True  # joined by ``omp --resume``, so the heartbeat is read again
    (hit,) = source.triggers(lane)
    assert (hit.trigger, hit.kind) == ("frozen", "infra")
    assert ls.propose(lane.subject, [hit]).action == "alert_research"


def test_healthy_lane_and_unprobed_pane_raise_nothing(tmp_path: Path) -> None:
    machine = frozen_machine(tmp_path, state="S+", group=None)  # omp is its own foreground group
    source = fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x")
    lanes = {o.subject.id: o for o in source.observe(NOW) if o.subject.kind == "omp_lane"}
    assert lanes["w1C:pJ"].metrics["omp_background"] is False and source.triggers(lanes["w1C:pJ"]) == []
    # herdr could not report w1C:pF: unprobed is not frozen
    assert lanes["w1C:pF"].metrics["omp_probe"] == "unavailable" and source.triggers(lanes["w1C:pF"]) == []


def test_frozen_unauthorized_lane_is_flagged_from_metadata_without_reading_its_session(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    machine = FakeMachine(tmp_path)
    private = tmp_path / "sessions" / "private.jsonl"
    _write_session(private, [start(60, "bash", "p", command=f"echo {PRIVATE}")])
    machine.write_config(log_lines=_fresh_log(10), fleet_md="| `w1C:p12` |", queue_panes={"w1C:pJ": [{"id": "X"}]})
    del machine.panes[1]["agent"]  # wK:pH is pinned and not authorized
    machine.add_omp("wK:pH", shell=300, omp=301, state="T", foreground=300, resume=private)
    spy = ReadSpy(monkeypatch)
    source = fl.FleetSource(runner=machine, home=tmp_path, run_name="run-x")
    observations = source.observe(NOW)
    lane = next(o for o in observations if o.subject.id == "wK:pH")
    assert [h.trigger for h in source.triggers(lane)] == ["frozen"]
    assert lane.metrics["session_read"] is False and private not in spy.paths
    assert str(private) not in json.dumps([o.metrics for o in observations], default=str)
