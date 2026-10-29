from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evallab.production import live_supervisor as ls

NOW = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)


def _trial(killable: bool = True) -> ls.Subject:
    return ls.Subject("harbor_trial", "job/trial-1", "lab" if killable else "other", killable=killable)


def _lane() -> ls.Subject:
    return ls.Subject("omp_lane", "w1C:pJ", "fleet")


def _hit(trigger: str, kind: str, score: float = 1.0, steps: tuple[int, ...] = ()) -> ls.TriggerHit:
    return ls.TriggerHit(trigger, kind, score, f"{trigger} fired", steps)


def _obs(subject: ls.Subject, *, steps: int = 3, actor: bool = True, terminal: bool = False) -> ls.Observation:
    events = tuple(ls.Event(i, NOW, "agent", f"step {i}") for i in range(steps))
    return ls.Observation(subject, NOW, events, {}, actor_inputs_available=actor, terminal=terminal)


def test_only_lab_subjects_can_be_killable() -> None:
    with pytest.raises(ValueError, match="only lab subjects"):
        ls.Subject("omp_lane", "wK:pH", "peter", killable=True)
    assert ls.Subject("modal_app", "ap-1", "production").finding_run_id == "live:modal_app:ap-1"
    assert ls.Subject("harbor_trial", "t", "lab", run_id="retained-trace:x").finding_run_id == "retained-trace:x"


def test_trigger_hit_must_have_fired() -> None:
    with pytest.raises(ValueError, match="score >= 1.0"):
        _hit("silence", "latency", 0.5)


@pytest.mark.parametrize(
    ("subject", "hits", "action", "kind"),
    [
        # cheating outranks a dead environment on the same subject
        (_trial(), [_hit("error_signature", "infra", 5), _hit("trace_alarm", "cheat")], "flag_cheating", "cheat"),
        (_trial(), [_hit("error_signature", "infra")], "kill_and_mark_infra", "infra"),
        (_trial(killable=False), [_hit("error_signature", "infra")], "alert_research", "infra"),
        (_trial(), [_hit("repetition", "loop")], "kill_and_retry", "loop"),
        (_trial(), [_hit("silence", "latency", 2.9)], "continue", "latency"),
        (_trial(), [_hit("silence", "latency", 3.0)], "kill_and_retry", "latency"),
        (_lane(), [_hit("repetition", "loop")], "nudge", "loop"),
        (_lane(), [_hit("silence", "latency")], "nudge", "latency"),
        (_lane(), [_hit("frozen", "infra")], "alert_research", "infra"),  # a frozen lane cannot read a nudge
        # the frozen lane outranks the wall time it also shows: a nudge would never be read
        (_lane(), [_hit("wall_time", "runaway", 2.0), _hit("frozen", "infra")], "alert_research", "infra"),
        (_lane(), [_hit("burn", "runaway")], "alert_research", "runaway"),
        (ls.Subject("launchd_service", "docent-watch", "lab"), [_hit("repetition", "infra")], "alert_research",
         "infra"),
    ],
)
def test_propose_rule_table(subject: ls.Subject, hits: list[ls.TriggerHit], action: str, kind: str) -> None:
    decision = ls.propose(subject, hits)
    assert (decision.action, decision.kind) == (action, kind)


def test_guard_refuses_kills_and_nudges_without_a_channel() -> None:
    kill = ls.Decision("kill_and_retry", "loop", "model says kill", reader_model="anthropic/x")
    assert ls.guard(_lane(), kill).action == "alert_research"
    assert ls.guard(_trial(killable=False), kill).action == "alert_research"
    assert ls.guard(_trial(), kill) == kill
    nudge = ls.Decision("nudge", "loop", "poke it")
    assert ls.guard(_trial(), nudge).action == "alert_research"
    assert ls.guard(_lane(), nudge) == nudge


def test_finding_verdicts_and_bounds() -> None:
    proposal = ls.Decision("continue", "latency", "slow")
    no_actor = _obs(_trial(), actor=False)
    record = ls.finding(no_actor, [_hit("silence", "latency")], proposal, proposal=proposal, mode="shadow",
                        acted=False, produced_at=NOW)
    assert record["verdict"] == "unknown"
    assert ls.finding(_obs(_trial()), [_hit("silence", "latency")], proposal, proposal=proposal,
                      mode="shadow", acted=False, produced_at=NOW)["verdict"] == "clear"

    long_reason = " ".join(["word"] * 80)
    decision = ls.Decision("kill_and_retry", "loop", long_reason, evidence_steps=(2, 99, 1, 0, 2))
    record = ls.finding(_obs(_trial()), [_hit("repetition", "loop", 1.5, (7,))], decision, proposal=decision,
                        mode="shadow", acted=False, produced_at=NOW)
    assert record["verdict"] == "alarm"
    assert len(record["summary"].split()) == ls.SUMMARY_WORDS
    # 99 is not a step of the observation or a hit; duplicates collapse
    assert record["evidence_steps"] == [2, 1, 0]
    assert record["detector"] == "live_supervisor" and record["detector_version"] == "live-supervisor/1"
    json.dumps(record)

    with pytest.raises(ValueError, match="shadow-mode"):
        ls.finding(_obs(_trial()), [], decision, proposal=decision, mode="shadow", acted=True, produced_at=NOW)


class _Source:
    def __init__(self, name: str, observations: list[ls.Observation], hits: list[ls.TriggerHit],
                 fail: bool = False) -> None:
        self.name, self._obs, self._hits, self._fail = name, observations, hits, fail

    def observe(self, now: datetime) -> list[ls.Observation]:
        if self._fail:
            raise RuntimeError("source down")
        return self._obs

    def triggers(self, observation: ls.Observation) -> list[ls.TriggerHit]:
        return self._hits if observation.subject.id != "quiet" else []


class _Boom:
    def decide(self, *_: object) -> ls.Decision:
        raise TimeoutError("reader")


class _Actuator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def act(self, observation: ls.Observation, decision: ls.Decision) -> bool:
        self.calls.append(f"{observation.subject.id}:{decision.action}")
        return True


def test_scan_once_isolates_failures_falls_back_and_suppresses(tmp_path: Path) -> None:
    quiet = ls.Subject("harbor_trial", "quiet", "lab", killable=True)
    sources = [
        _Source("broken", [], [], fail=True),
        _Source("harbor", [_obs(_trial()), _obs(quiet), _obs(_trial(), terminal=True)],
                [_hit("repetition", "loop", 2.0, (1,))]),
    ]
    log = ls.DecisionLog(tmp_path)
    report = ls.scan_once(sources, _Boom(), log, now=NOW)
    assert report.errors["broken"] == "RuntimeError: source down"
    assert (report.observed, report.quiet, report.terminal, len(report.records)) == (3, 1, 1, 1)
    record = report.records[0]
    assert record["action"] == "alert_research"
    assert record["mode"] == "shadow" and record["acted"] is False

    # same fingerprint inside the cooldown is suppressed, also across a fresh log object
    again = ls.scan_once(sources, ls.RuleDecider(), ls.DecisionLog(tmp_path), now=NOW + timedelta(minutes=10))
    assert (again.suppressed, len(again.records)) == (1, 0)
    later = ls.scan_once(sources, ls.RuleDecider(), ls.DecisionLog(tmp_path), now=NOW + timedelta(minutes=31))
    assert len(later.records) == 1
    lines = (tmp_path / ls.DECISIONS_FILE).read_text().splitlines()
    assert len(lines) == 2


def test_actuator_runs_only_in_act_mode_and_never_for_continue(tmp_path: Path) -> None:
    actuator = _Actuator()
    loop_source = _Source("harbor", [_obs(_trial())], [_hit("repetition", "loop")])
    ls.scan_once([loop_source], ls.RuleDecider(), ls.DecisionLog(tmp_path / "a"), now=NOW, actuator=actuator)
    assert actuator.calls == []
    report = ls.scan_once([loop_source], ls.RuleDecider(), ls.DecisionLog(tmp_path / "b"), now=NOW,
                          mode="act", actuator=actuator)
    assert actuator.calls == ["job/trial-1:kill_and_retry"] and report.records[0]["acted"] is True
    slow = _Source("harbor", [_obs(_trial())], [_hit("silence", "latency", 1.2)])
    ls.scan_once([slow], ls.RuleDecider(), ls.DecisionLog(tmp_path / "c"), now=NOW, mode="act",
                 actuator=actuator)
    assert actuator.calls == ["job/trial-1:kill_and_retry"]


def test_reader_failure_never_turns_a_proposal_into_a_live_stop(tmp_path: Path) -> None:
    actuator = _Actuator()
    source = _Source("harbor", [_obs(_trial())], [_hit("repetition", "loop")])
    report = ls.scan_once([source], _Boom(), ls.DecisionLog(tmp_path), now=NOW,
                          mode="act", actuator=actuator)
    assert actuator.calls == ["job/trial-1:alert_research"]
    assert report.records[0]["proposal"] == "kill_and_retry"
    assert report.records[0]["action"] == "alert_research"
    assert report.errors["reader:harbor_trial:job/trial-1"] == "TimeoutError: reader"


def test_missing_transcript_is_unknown_even_when_an_action_is_proposed() -> None:
    observation = _obs(_trial(), actor=False)
    hit = _hit("error_signature", "infra")
    decision = ls.propose(observation.subject, [hit])
    record = ls.finding(observation, [hit], decision, proposal=decision, mode="shadow",
                        acted=False, produced_at=NOW)
    assert record["verdict"] == "unknown"
    assert record["action"] == "kill_and_mark_infra"


def test_broken_trigger_does_not_hide_other_subjects(tmp_path: Path) -> None:
    class BrokenTrigger(_Source):
        def triggers(self, observation: ls.Observation) -> list[ls.TriggerHit]:
            raise ValueError("invalid observation")

    broken = BrokenTrigger("broken", [_obs(_trial())], [])
    healthy = _Source("healthy", [_obs(_lane())], [_hit("silence", "latency")])
    report = ls.scan_once([broken, healthy], ls.RuleDecider(), ls.DecisionLog(tmp_path), now=NOW)
    assert report.errors["trigger:broken:job/trial-1"] == "ValueError: invalid observation"
    assert [record["subject"]["id"] for record in report.records] == ["w1C:pJ"]


def test_action_failure_is_recorded_and_other_subjects_still_run(tmp_path: Path) -> None:
    class BrokenActuator:
        def act(self, observation: ls.Observation, decision: ls.Decision) -> bool:
            raise OSError("stop unavailable")

    sources = [_Source("harbor", [_obs(_trial()), _obs(_lane())], [_hit("repetition", "loop")])]
    report = ls.scan_once(sources, ls.RuleDecider(), ls.DecisionLog(tmp_path), now=NOW,
                          mode="act", actuator=BrokenActuator())
    assert {record["subject"]["id"] for record in report.records} == {"job/trial-1", "w1C:pJ"}
    assert all(record["acted"] is False for record in report.records)
    assert report.errors["action:harbor_trial:job/trial-1"] == "OSError: stop unavailable"
    records = [json.loads(line) for line in (tmp_path / ls.DECISIONS_FILE).read_text().splitlines()]
    assert records == report.records
