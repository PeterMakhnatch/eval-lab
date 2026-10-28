from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evallab.production.live_supervisor import Decision, Event, Observation, Subject
from evallab.production.live_supervisor_act import (
    ACTIONS_FILE,
    ALERTS_HEADING,
    CAPS_FILE,
    MODAL_STOP,
    LabActuator,
    insert_alert,
)

T0 = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class FakeRunner:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(tuple(argv))
        return self.code


def _actuator(tmp_path: Path, *, dry_run: bool = False, runner: FakeRunner | None = None,
              clock: Clock | None = None, **kw) -> LabActuator:
    return LabActuator(root=tmp_path / "ls", dry_run=dry_run, runner=runner or FakeRunner(),
                       queue_path=tmp_path / "queue-append.jsonl",
                       alerts_path=tmp_path / "live-supervisor.md", clock=clock or Clock(), **kw)


def _obs(subject: Subject, *, events: tuple[Event, ...] = (), metrics: dict | None = None) -> Observation:
    return Observation(subject, T0, events, metrics or {})


def _app(app_id: str, *, owner: str = "lab", label: str = "sb1-seeker") -> Observation:
    return _obs(Subject("modal_app", app_id, owner, owner == "lab", label=label),
                metrics={"description": label})


KILL = Decision("kill_and_retry", "loop", "repetition: same call 12x", evidence_steps=(3,))


def _receipts(tmp_path: Path) -> list[dict]:
    path = tmp_path / "ls" / ACTIONS_FILE
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


# --------------------------------------------------------------------------- caps


def test_third_kill_in_an_hour_is_allowed_fourth_refused_until_the_first_ages_out(tmp_path: Path) -> None:
    clock, runner = Clock(), FakeRunner()
    act = _actuator(tmp_path, runner=runner, clock=clock)
    for i in range(3):
        clock.at = T0 + timedelta(minutes=20 * i)
        assert act.act(_app(f"ap-{i}"), KILL) is True
    clock.at = T0 + timedelta(minutes=59)
    assert act.act(_app("ap-3"), KILL) is False
    assert _receipts(tmp_path)[-1]["detail"].startswith("cap_hour")
    clock.at = T0 + timedelta(hours=1)  # the first kill leaves the rolling hour
    assert act.act(_app("ap-3"), KILL) is True
    assert [c[-1] for c in runner.calls] == ["ap-0", "ap-1", "ap-2", "ap-3"]


def test_tenth_kill_in_a_utc_day_is_allowed_eleventh_refused_next_day_allowed(tmp_path: Path) -> None:
    clock, runner = Clock(), FakeRunner()
    act = _actuator(tmp_path, runner=runner, clock=clock)
    for i in range(10):
        clock.at = T0 + timedelta(hours=2 * i)  # 01:00 .. 19:00 UTC, one per two hours
        assert act.act(_app(f"ap-{i}"), KILL) is True, i
    clock.at = T0 + timedelta(hours=22, minutes=30)  # 23:30 UTC, same day, hour cap clear
    assert act.act(_app("ap-10"), KILL) is False
    assert _receipts(tmp_path)[-1]["detail"].startswith("cap_day")
    clock.at = datetime(2026, 10, 29, 0, 0, tzinfo=UTC)
    assert act.act(_app("ap-10"), KILL) is True
    assert len(runner.calls) == 11


def test_second_kill_on_the_same_subject_is_refused_even_after_a_failed_first(tmp_path: Path) -> None:
    clock, runner = Clock(), FakeRunner(code=1)
    act = _actuator(tmp_path, runner=runner, clock=clock)
    assert act.act(_app("ap-1"), KILL) is False  # attempted, exit 1: still counts
    clock.at = T0 + timedelta(days=3)
    runner.code = 0
    assert act.act(_app("ap-1"), Decision("kill_and_mark_infra", "infra", "dead")) is False
    assert len(runner.calls) == 1
    rows = _receipts(tmp_path)
    assert [r["status"] for r in rows] == ["failed", "refused"]
    assert rows[-1]["detail"].startswith("cap_subject")


def test_kill_and_retry_records_retry_requested_only(tmp_path: Path) -> None:
    act = _actuator(tmp_path)
    assert act.act(_app("ap-1"), KILL) is True
    assert act.act(_app("ap-2"), Decision("kill_and_mark_infra", "infra", "dead backend")) is True
    caps = json.loads((tmp_path / "ls" / CAPS_FILE).read_text())
    assert [k["subject"] for k in caps["kills"]] == ["modal_app:ap-1", "modal_app:ap-2"]
    assert caps["retries"] == [{"subject": "modal_app:ap-1", "status": "retry_requested",
                                "reason": KILL.reason, "at": "2026-10-28T01:00:00Z"}]
    assert [r["retry_requested"] for r in _receipts(tmp_path)] == [True, False]


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize("observation", [
    _app("ap-prod", label="evallab-cloud-production"),
    _app("evallab-cloud-production"),
    _obs(Subject("modal_app", "ap-prod", "production"), metrics={"description": "evallab-cloud-production"}),
])
def test_production_modal_app_is_never_stopped(tmp_path: Path, observation: Observation) -> None:
    runner = FakeRunner()
    act = _actuator(tmp_path, runner=runner)
    assert act.act(observation, KILL) is False
    assert runner.calls == []
    assert "never stopped" in _receipts(tmp_path)[-1]["detail"]
    assert not (tmp_path / "ls" / CAPS_FILE).exists()


def test_non_lab_subjects_are_refused_without_running_anything(tmp_path: Path) -> None:
    runner = FakeRunner()
    act = _actuator(tmp_path, runner=runner)
    assert act.act(_app("ap-x", owner="other", label="someone-else"), KILL) is False
    lane = _obs(Subject("omp_lane", "w1C:pZ", "fleet"))
    assert act.act(lane, KILL) is False
    assert act.act(_obs(Subject("launchd_service", "com.x", "lab")), KILL) is False
    assert runner.calls == []
    assert not (tmp_path / "queue-append.jsonl").exists()
    assert [r["status"] for r in _receipts(tmp_path)] == ["refused"] * 3


# --------------------------------------------------------------------------- harbor


def _job(tmp_path: Path, *, hooks: list[dict] = (), result: bool = False) -> Path:
    job = tmp_path / "runs" / "job-a"
    (job / "task__a1" / "agent").mkdir(parents=True)
    (job / "watch").mkdir()
    (job / "watch" / "hooks.jsonl").write_text("".join(json.dumps(h) + "\n" for h in hooks))
    if result:
        (job / "task__a1" / "result.json").write_text("{}")
    return job


def _trial() -> Observation:
    return _obs(Subject("harbor_trial", "job-a/task__a1", "lab", True, run_id="task__a1"),
                events=(Event(3, None, "trajectory", "ran the same grep again"),))


START = {"trial": "task__a1", "event": "start", "at": "2026-10-28T00:30:00Z"}


@pytest.mark.parametrize(("hooks", "result", "why"), [
    ([START, {"trial": "task__a1", "event": "verification-start", "at": "2026-10-28T00:50:00Z"}],
     False, "verification"),
    ([START], True, "finished"),
    ([START, {"trial": "task__a1", "event": "end", "at": "2026-10-28T00:55:00Z"}], False, "finished"),
])
def test_harbor_stop_refused_once_verification_started_or_finished(tmp_path: Path, hooks, result, why) -> None:
    job = _job(tmp_path, hooks=hooks, result=result)
    act = _actuator(tmp_path, harbor_roots=[tmp_path / "runs"])
    assert act.act(_trial(), KILL) is False
    assert not (job / "watch" / "stop-requests").exists()
    assert why in _receipts(tmp_path)[-1]["detail"]
    assert not (tmp_path / "ls" / CAPS_FILE).exists()


def test_harbor_stop_writes_the_watch_stop_request_once(tmp_path: Path) -> None:
    job = _job(tmp_path, hooks=[START])
    act = _actuator(tmp_path, harbor_roots=[tmp_path / "runs"])
    assert act.act(_trial(), KILL) is True
    request = json.loads((job / "watch" / "stop-requests" / "task__a1.json").read_text())
    assert (request["requested_by"], request["rule"], request["step_ref"]) == (
        "live_supervisor", "live_supervisor:loop", "step 3")
    assert json.loads((job / "watch" / "stop-records" / "task__a1.json").read_text())["outcome"] == "requested"
    assert act.act(_trial(), KILL) is False  # request exists, and the subject cap holds anyway


def test_harbor_trial_outside_lab_roots_is_refused(tmp_path: Path) -> None:
    _job(tmp_path, hooks=[START])
    act = _actuator(tmp_path, harbor_roots=[tmp_path / "elsewhere"])
    assert act.act(_trial(), KILL) is False
    assert "not under a lab runs root" in _receipts(tmp_path)[-1]["detail"]


# --------------------------------------------------------------------------- dry run


def test_dry_run_never_invokes_the_runner_and_logs_the_command(tmp_path: Path) -> None:
    runner = FakeRunner()
    job = _job(tmp_path, hooks=[START])
    act = LabActuator(root=tmp_path / "ls", runner=runner, harbor_roots=[tmp_path / "runs"],
                      queue_path=tmp_path / "q.jsonl", alerts_path=tmp_path / "a.md", clock=Clock())
    assert act.dry_run is True
    assert act.act(_app("ap-1"), KILL) is False
    assert act.act(_trial(), KILL) is False
    lane = _obs(Subject("omp_lane", "w1C:pZ", "fleet"))
    assert act.act(lane, Decision("nudge", "latency", "idle 40 min")) is False
    assert act.act(_app("ap-2"), Decision("alert_research", "other", "odd")) is False
    assert runner.calls == []
    assert not (job / "watch" / "stop-requests").exists()
    assert not (tmp_path / "q.jsonl").exists() and not (tmp_path / "a.md").exists()
    assert not (tmp_path / "ls" / CAPS_FILE).exists()
    rows = _receipts(tmp_path)
    assert [r["status"] for r in rows] == ["dry_run"] * 4
    assert rows[0]["command"] == [*MODAL_STOP, "ap-1"]
    assert rows[1]["command"][:5] == ["evallab", "watch", "stop", str(job), "task__a1"]


# --------------------------------------------------------------------------- nudge / alert / cheat


def test_nudge_appends_one_queue_item_with_pane_id_text(tmp_path: Path) -> None:
    act = _actuator(tmp_path)
    lane = _obs(Subject("omp_lane", "w1C:pZ", "fleet"),
                events=(Event(7, None, "session", "waiting   on\nquota"),))
    decision = Decision("nudge", "latency", "silence: idle 40 min", evidence_steps=(7, 99))
    assert act.act(lane, decision) is True
    (line,) = (tmp_path / "queue-append.jsonl").read_text().splitlines()
    item = json.loads(line)
    assert set(item) == {"pane", "id", "text"}
    assert item["pane"] == "w1C:pZ"
    assert re.fullmatch(r"LS-NUDGE-[0-9a-f]{8}", item["id"])
    assert "silence: idle 40 min" in item["text"] and "step 7: waiting on quota" in item["text"]


def test_peter_owned_lane_is_never_nudged(tmp_path: Path) -> None:
    act = _actuator(tmp_path)
    assert act.act(_obs(Subject("omp_lane", "wK:pH", "peter")), Decision("nudge", "latency", "idle")) is False
    assert not (tmp_path / "queue-append.jsonl").exists()


def test_alert_is_deduped_per_subject_per_utc_day(tmp_path: Path) -> None:
    clock = Clock()
    md = tmp_path / "live-supervisor.md"
    md.write_text("# live-supervisor\n\n## LS-1\n\n- old receipt\n")
    act = _actuator(tmp_path, clock=clock)
    alert = Decision("alert_research", "infra", "error_signature: sandbox gone", confidence=0.8)
    assert act.act(_app("ap-1"), alert) is True
    clock.at = T0 + timedelta(hours=20)  # 21:00 UTC, same UTC day
    assert act.act(_app("ap-1"), alert) is False
    assert act.act(_app("ap-2"), alert) is True
    clock.at = T0 + timedelta(hours=23)  # next UTC day
    assert act.act(_app("ap-1"), alert) is True
    text = md.read_text()
    assert text.startswith("# live-supervisor\n\n## LS-1\n\n- old receipt\n\n## Live alerts\n\n- **")
    lines = [ln for ln in text.splitlines() if ln.startswith("- **")]
    assert len(lines) == 3
    assert "→ live supervisor (rules) at 9:00 PM ET → **infra, confidence 0.80** → error_signature" in lines[0]
    assert _receipts(tmp_path)[1]["detail"] == "already alerted for this subject today"


def test_insert_alert_appends_at_end_of_existing_section() -> None:
    text = f"# t\n\n{ALERTS_HEADING}\n\n- a\n\n## Later\n\nbody\n"
    assert insert_alert(text, "- b") == f"# t\n\n{ALERTS_HEADING}\n\n- a\n- b\n\n## Later\n\nbody\n"


def test_flag_cheating_defaults_to_nothing_and_delegates_to_the_router(tmp_path: Path) -> None:
    flag = Decision("flag_cheating", "cheat", "trace_alarm: read hidden tests")
    assert _actuator(tmp_path).act(_trial(), flag) is False
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ls"]
    routed: list[str] = []
    act = _actuator(tmp_path, cheat_router=lambda obs, d: routed.append(obs.subject.id) is None)
    assert act.act(_trial(), flag) is True
    assert routed == ["job-a/task__a1"]
