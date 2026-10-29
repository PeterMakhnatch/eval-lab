"""Live Supervisor ``harbor`` source: trigger boundaries, replay cut-off, discovery and ownership.

Every file lives under ``tmp_path``; the clock, home, roots and findings path are injected.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from evallab.production import run_health
from evallab.production.live_supervisor import Subject
from evallab.production.live_supervisor_harbor import (
    FEEDBACK_REPEATS_LIVE,
    PROMPT_TOKENS_PER_STEP,
    SILENCE_SECONDS,
    HarborSource,
    default_roots,
    live_trials,
    trial_observation,
    triggers_for,
)

NOW = datetime(2026, 10, 28, 20, 0, 0, tzinfo=UTC)
SUBJECT = Subject(kind="harbor_trial", id="job/trial__abc", owner="lab", killable=True, run_id="trial__abc")


def _iso(offset: float) -> str:
    return (NOW + timedelta(seconds=offset)).isoformat()


def _step(step_id: int | str, command: str | None = "ls", *, offset: float | None = None, source: str = "agent",
          observation: str | None = None, prompt: int | None = None, message: str = "") -> dict[str, Any]:
    step: dict[str, Any] = {"step_id": step_id, "source": source, "message": message}
    if command is not None:
        step["tool_calls"] = [{"function_name": "bash_command", "arguments": {"keystrokes": command + "\n"}}]
    if observation is not None:
        step["observation"] = {"results": [{"content": observation}]}
    if offset is not None:
        step["timestamp"] = _iso(offset)
    if prompt is not None:
        step["metrics"] = {"prompt_tokens": prompt, "completion_tokens": 10}
    return step


def _words(n: int) -> list[str]:
    """n distinct digit-free commands (the loop normalizer maps digits away)."""
    out = []
    for i in range(n):
        letters, k = "", i
        for _ in range(3):
            letters += chr(ord("a") + k % 26)
            k //= 26
        out.append(f"echo {letters}")
    return out


def _run(steps: list[dict[str, Any]], *, source: str = "harbor", **meta: Any) -> run_health.HealthRun:
    return run_health.HealthRun(run_id="trial__abc", source=source, steps=steps, meta=meta)


def _hits(run: run_health.HealthRun, *, now: datetime = NOW, **kwargs: Any) -> dict[str, Any]:
    observation = trial_observation(run, subject=SUBJECT, now=now, **kwargs)
    return {h.trigger: h for h in triggers_for(observation)}


def _alert(rule: str, **extra: Any) -> dict[str, Any]:
    return {"rule": rule, "trial": "trial__abc", "step_ref": "head#7", "detail": f"{rule} fired", **extra}


# --------------------------------------------------------------------------- silence


def test_silence_fires_at_the_threshold_not_one_second_before() -> None:
    last = -SILENCE_SECONDS
    steps = [_step(1, offset=last - 100), _step(2, "pwd", offset=last)]
    hit = _hits(_run(steps))["silence"]
    assert (hit.kind, hit.score) == ("latency", 1.0)
    assert "silence" not in _hits(_run([_step(1, offset=last - 100), _step(2, "pwd", offset=last + 1)]))


def test_silence_score_is_seconds_over_threshold() -> None:
    assert _hits(_run([_step(1, offset=-1800)]))["silence"].score == 2.0


def test_trajectory_write_after_last_step_resets_silence() -> None:
    steps = [_step(1, offset=-3000)]
    assert "silence" in _hits(_run(steps))
    assert "silence" not in _hits(_run(steps), mtime=NOW - timedelta(seconds=SILENCE_SECONDS - 1))
    assert "silence" in _hits(_run(steps), mtime=NOW - timedelta(seconds=SILENCE_SECONDS))


def test_mtime_in_the_future_of_now_is_ignored() -> None:
    steps = [_step(1, offset=-3000)]
    assert "silence" in _hits(_run(steps), mtime=NOW + timedelta(seconds=60))


def test_silence_is_unknown_without_any_timestamp() -> None:
    observation = trial_observation(_run([_step(1)]), subject=SUBJECT, now=NOW)
    assert observation.metrics["silence_seconds"] is None
    assert triggers_for(observation) == []


# --------------------------------------------------------------------------- replay cut-off


def test_steps_after_now_are_ignored_and_a_step_at_now_is_kept() -> None:
    steps = [_step(1, offset=-30), _step(2, "pwd", offset=0), _step(3, "whoami", offset=1), _step(4, "id", offset=500)]
    observation = trial_observation(_run(steps), subject=SUBJECT, now=NOW)
    assert observation.metrics["agent_steps"] == 2
    assert [e.index for e in observation.events] == [1, 2]
    assert observation.metrics["silence_seconds"] == 0.0


def test_stepping_now_forward_reveals_later_steps_and_their_silence() -> None:
    steps = [_step(1, offset=0), _step(2, "pwd", offset=60)]
    early = trial_observation(_run(steps), subject=SUBJECT, now=NOW + timedelta(seconds=30))
    late = trial_observation(_run(steps), subject=SUBJECT, now=NOW + timedelta(seconds=60 + 1000))
    assert early.metrics["agent_steps"] == 1
    assert late.metrics["agent_steps"] == 2
    assert "silence" in {h.trigger for h in triggers_for(late)}


def test_alert_first_seen_after_now_does_not_exist_yet() -> None:
    alerts = [_alert("hidden_test_read", first_seen=_iso(10))]
    assert "trace_alarm" not in _hits(_run([_step(1, offset=-1)]), alerts=alerts)
    assert "trace_alarm" in _hits(_run([_step(1, offset=-1)]), alerts=alerts, now=NOW + timedelta(seconds=10))


def test_events_are_the_last_twenty_steps_with_original_indexes() -> None:
    steps = [_step(100 + i, f"echo {chr(97 + i % 26)}", offset=-200 + i) for i in range(30)]
    steps.append(_step("131", "whoami", offset=-1, message="done"))
    steps.append(_step("x", None, source="user", message="hello"))
    events = trial_observation(_run(steps), subject=SUBJECT, now=NOW).events
    assert len(events) == 20
    assert [e.index for e in events][-3:] == [129, 131, 32]
    assert events[-2].text.startswith("done | $ whoami")
    assert events[0].at == NOW + timedelta(seconds=-200 + 12)


# --------------------------------------------------------------------------- repetition


def test_repetition_is_the_run_health_loop_alarm() -> None:
    n = run_health.LOOP_COMMAND_RUN
    hit = _hits(_run([_step(i + 1, "pytest -x", offset=-100 + i) for i in range(n)]))["repetition"]
    assert (hit.kind, hit.evidence_steps) == ("loop", (1,))
    assert hit.score >= 1.0
    assert "repetition" not in _hits(_run([_step(i + 1, "pytest -x", offset=-100 + i) for i in range(n - 1)]))


def _resubmissions(failures: int) -> list[dict[str, Any]]:
    """A task prompt, then ``failures`` rounds of (new command, identical failure feedback)."""
    steps = [_step(1, None, source="user", message="implement func.py")]
    for i, command in enumerate(_words(failures)):
        steps += [_step(2 + 2 * i, command), _step(3 + 2 * i, None, source="user", message="Tests failed: line 13")]
    return steps


def test_identical_feedback_needs_the_live_repeat_count_though_run_health_alarms_at_one() -> None:
    n = FEEDBACK_REPEATS_LIVE
    one_repeat = _run(_resubmissions(2))
    assert {r["kind"]: r["verdict"] for r in run_health.detect(one_repeat)}["loop"] == "alarm"
    assert "repetition" not in _hits(one_repeat)
    assert "repetition" not in _hits(_run(_resubmissions(n)))  # n failures = n - 1 repeats
    hit = _hits(_run(_resubmissions(n + 1)))["repetition"]  # n repeats
    assert (hit.kind, hit.score) == ("loop", 1.0)
    assert f"repeated_feedback={n} (>= {n} live)" in hit.detail
    assert hit.evidence_steps == (5,)  # the step of the first repeat
    assert _hits(_run(_resubmissions(n + 3)))["repetition"].score == (n + 2) / n


def test_other_loop_components_still_alarm_with_few_feedback_repeats() -> None:
    n = run_health.LOOP_COMMAND_RUN
    steps = [_step(1, None, source="user", message="implement func.py")]
    steps += [_step(2 + i, "pytest -x", observation="FAILED") for i in range(n)]
    steps += [_step(2 + n, None, source="user", message="Tests failed"), _step(3 + n, None, source="user",
                                                                          message="Tests failed")]
    hit = _hits(_run(steps))["repetition"]
    assert "identical_command_run" in hit.detail
    assert "repeated_feedback" not in hit.detail


# --------------------------------------------------------------------------- wall_time


def test_wall_time_agent_steps_boundary() -> None:
    limit = run_health.RUNAWAY_AGENT_STEPS
    commands = _words(limit)
    at = _hits(_run([_step(i + 1, commands[i]) for i in range(limit)]))["wall_time"]
    assert (at.kind, at.score, at.evidence_steps) == ("runaway", 1.0, (limit,))
    assert "wall_time" not in _hits(_run([_step(i + 1, commands[i]) for i in range(limit - 1)]))


@pytest.mark.parametrize(("source", "p99"), [("goodfire", 5877.0), ("tb4", 5371.0),
                                             ("harbor", float(run_health.RUNAWAY_WALL_SECONDS))])
def test_wall_time_elapsed_boundary_uses_the_per_source_p99(source: str, p99: float) -> None:
    started = (NOW - timedelta(seconds=p99)).timestamp()
    steps = [_step(1, offset=-1)]
    hit = _hits(_run(steps, source=source, started_at=started))["wall_time"]
    assert hit.score == 1.0
    assert "wall_time" not in _hits(_run(steps, source=source, started_at=started + 1))


def test_wall_time_start_falls_back_to_the_first_step_timestamp() -> None:
    steps = [_step(1, offset=-7200), _step(2, "pwd", offset=-1)]
    assert "wall_time" in _hits(_run(steps))
    assert "wall_time" not in _hits(_run([_step(1, offset=-7199), _step(2, "pwd", offset=-1)]))


def test_trial_started_after_now_has_no_elapsed() -> None:
    observation = trial_observation(_run([_step(1)], started_at=(NOW + timedelta(seconds=5)).timestamp()),
                                    subject=SUBJECT, now=NOW)
    assert observation.metrics["elapsed_seconds"] is None


# --------------------------------------------------------------------------- error_signature


DOCKER = "Cannot connect to the Docker daemon at x"


def _failing(n: int, observation: str = DOCKER, *, first: int = 2) -> list[dict[str, Any]]:
    return [_step(first + i, f"echo {chr(97 + i)}", offset=-5 + i, observation=observation) for i in range(n)]


def test_error_signature_infra_needs_the_class_in_the_last_three_tool_outputs() -> None:
    steps = [_step(1, offset=-9)] + _failing(3)
    hit = _hits(_run(steps))["error_signature"]
    assert hit.kind == "infra"
    assert hit.evidence_steps == (2, 3, 4)
    assert "tail:sandbox in the last 3 tool outputs" in hit.detail


@pytest.mark.parametrize("failing", [1, 2])
def test_error_signature_infra_one_or_two_matching_outputs_are_transient(failing: int) -> None:
    # Run Health alarms (signature in the tail); the live trigger waits for it to persist.
    steps = [_step(1, offset=-9)] + _failing(failing)
    observation = trial_observation(_run(steps), subject=SUBJECT, now=NOW)
    assert "infra" in observation.metrics["health"]
    assert "error_signature" not in {h.trigger for h in triggers_for(observation)}


def test_error_signature_infra_a_clean_output_after_the_failures_resets_the_run() -> None:
    steps = [_step(1, offset=-9)] + _failing(3) + [_step(5, "ls", offset=-1, observation="file.txt")]
    assert "error_signature" not in _hits(_run(steps))


def test_error_signature_infra_blank_outputs_do_not_break_the_run() -> None:
    steps = [_step(1, offset=-9), *_failing(2), _step(4, "true", offset=-2, observation=""),
             _step(5, "echo z", offset=-1, observation=DOCKER)]
    assert _hits(_run(steps))["error_signature"].evidence_steps == (2, 3, 5)


def test_error_signature_infra_only_persistent_classes_are_reported() -> None:
    oom = "MemoryError: Out of memory"
    steps = [_step(1, offset=-9), _step(2, "a", offset=-4, observation=DOCKER + " " + oom),
             _step(3, "b", offset=-3, observation=DOCKER), _step(4, "c", offset=-2, observation=DOCKER + " " + oom)]
    hit = _hits(_run(steps))["error_signature"]
    assert "tail:sandbox" in hit.detail and "tail:oom" not in hit.detail
    assert hit.evidence_steps == (2, 3, 4)


def test_error_signature_signature_outside_the_tail_is_not_a_hit() -> None:
    steps = [_step(1, offset=-9, observation="Cannot connect to the Docker daemon at x")]
    steps += [_step(i, f"echo {chr(96 + i)}", offset=-8 + i) for i in range(2, 7)]
    assert "error_signature" not in _hits(_run(steps))


@pytest.mark.parametrize("rule", ["infra_error", "infra_spike", "proxy_errors", "proxy_error_spike"])
def test_error_signature_from_unacked_live_watch_alerts(rule: str) -> None:
    hit = _hits(_run([_step(1, offset=-1)]), alerts=[_alert(rule)])["error_signature"]
    assert (hit.kind, hit.score, hit.evidence_steps) == ("infra", 1.0, (7,))


def test_acked_alerts_are_ignored() -> None:
    alerts = [_alert("infra_error", acknowledged_by="peter"), _alert("hidden_test_read", acknowledged_by="peter"),
              _alert("budget_burn", acked=True)]
    observation = trial_observation(_run([_step(1, offset=-1)]), subject=SUBJECT, now=NOW, alerts=alerts)
    assert observation.metrics["alerts"] == []
    assert triggers_for(observation) == []


def test_alerts_outside_the_mapped_rules_are_not_triggers() -> None:
    assert _hits(_run([_step(1, offset=-1)]), alerts=[_alert("stalled"), _alert("parse_errors")]) == {}


# --------------------------------------------------------------------------- burn


def test_burn_prompt_tokens_per_step_boundary() -> None:
    def run(prompt: int) -> run_health.HealthRun:
        return _run([_step(1, "ls", offset=-1, prompt=prompt)])

    at = _hits(run(int(PROMPT_TOKENS_PER_STEP)))["burn"]
    assert (at.kind, at.score) == ("runaway", 1.0)
    assert "burn" not in _hits(run(int(PROMPT_TOKENS_PER_STEP) - 1))


def test_burn_without_token_metrics_abstains() -> None:
    assert "burn" not in _hits(_run([_step(1, offset=-1)]))


@pytest.mark.parametrize("rule", ["budget_burn", "spend", "spend_cap_warning", "spend_cap_exceeded"])
def test_burn_from_live_watch_alerts(rule: str) -> None:
    assert _hits(_run([_step(1, offset=-1)]), alerts=[_alert(rule)])["burn"].kind == "runaway"


# --------------------------------------------------------------------------- trace_alarm


@pytest.mark.parametrize("rule", ["hidden_test_read", "grader_tamper", "history_mining", "git_object_read",
                                  "copy_acquired", "harness_log_read"])
def test_trace_alarm_from_integrity_alerts(rule: str) -> None:
    hit = _hits(_run([_step(1, offset=-1)]), alerts=[_alert(rule)])["trace_alarm"]
    assert (hit.kind, hit.score) == ("cheat", 1.0)



@pytest.mark.parametrize(("mechanisms", "fires"), [
    (["script_replace"], False),
    (["sed_inplace"], False),
    (["script_replace", "sed_inplace"], False),
    (["preexisted"], True),
    (["sed_inplace", "preexisted"], True),
    (["always"], True),
    (["settings"], True),
    (["whole"], True),
    (["weakening"], True),
])
def test_grader_tamper_triggers_only_on_mechanisms_that_are_not_ordinary_test_edits(mechanisms: list[str],
                                                                                    fires: bool) -> None:
    hits = _hits(_run([_step(1, offset=-1)]), alerts=[_alert("grader_tamper", mechanisms=mechanisms)])
    assert ("trace_alarm" in hits) is fires


def test_grader_tamper_without_mechanisms_still_triggers() -> None:
    """File-side observations and older records carry no mechanism list and are not demoted."""
    assert "trace_alarm" in _hits(_run([_step(1, offset=-1)]), alerts=[_alert("grader_tamper")])

def test_trace_alarm_from_a_trace_harness_cheat_alarm_only() -> None:
    def record(**over: Any) -> dict[str, Any]:
        return {"run_id": "trial__abc", "detector": "tripwire", "verdict": "alarm", "kind": "cheat", "score": 1.5,
                "summary": "hackbook:x", "evidence_steps": [3, "no"], **over}

    run = _run([_step(1, offset=-1)])
    hit = _hits(run, trace_alarms=[record()])["trace_alarm"]
    assert (hit.score, hit.evidence_steps) == (1.5, (3,))
    assert "tripwire" in hit.detail
    for ignored in (record(verdict="clear"), record(verdict="abstain"), record(kind="loop")):
        assert "trace_alarm" not in _hits(run, trace_alarms=[ignored])


# --------------------------------------------------------------------------- transcript


def test_no_actor_transcript_marks_inputs_unavailable() -> None:
    steps = [_step(1, None, source="user", message="fix the bug", offset=-5), _step(2, None, offset=-1)]
    assert trial_observation(_run(steps), subject=SUBJECT, now=NOW).actor_inputs_available is False
    assert trial_observation(_run([]), subject=SUBJECT, now=NOW).actor_inputs_available is False
    with_steps = trial_observation(_run([_step(1, "ls", offset=-1)]), subject=SUBJECT, now=NOW)
    assert with_steps.actor_inputs_available is True


# --------------------------------------------------------------------------- source: discovery


def _trial(root: Path, job: str, trial: str = "task__abc", *, age: float = 60.0,
           steps: list[dict[str, Any]] | None = None, result: bool = False, agent: bool = True) -> Path:
    trial_dir = root / job / trial
    trial_dir.mkdir(parents=True, exist_ok=True)
    if agent:
        (trial_dir / "agent").mkdir(exist_ok=True)
    if steps is not None:
        (trial_dir / "agent" / "trajectory.json").write_text(
            json.dumps({"schema_version": "ATIF-v1.5", "session_id": "s", "agent": {"name": "x", "version": "1"},
                        "steps": steps}))
    if result:
        (trial_dir / "result.json").write_text("{}")
    stamp = NOW.timestamp() - age
    inside = list((trial_dir / "agent").iterdir()) if agent else []
    for path in [*inside, *([trial_dir / "agent"] if agent else []), trial_dir]:
        os.utime(path, (stamp, stamp))
    return trial_dir


def _source(tmp_path: Path, roots: list[Path], **kwargs: Any) -> HarborSource:
    return HarborSource(roots, findings_path=tmp_path / "findings.jsonl", **kwargs)


def _observe(source: HarborSource) -> dict[str, Any]:
    return {o.subject.id: o for o in source.observe(NOW)}


STEPS = [_step(1, "ls", offset=-30, prompt=100), _step(2, "pwd", offset=-10, prompt=100)]


def test_a_live_trial_has_an_agent_dir_no_result_and_a_recent_file(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root, "j1", "live__a", steps=STEPS)
    _trial(root, "j1", "done__b", steps=STEPS, result=True)
    _trial(root, "j1", "noagent__c", agent=False)
    _trial(root, "j2", "stale__d", steps=STEPS, age=24 * 3600 + 1)
    found = {t.trial_dir.name for t in live_trials([root], NOW)}
    assert found == {"live__a"}


def test_the_24_hour_window_includes_its_boundary(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root, "j", "edge__a", steps=STEPS, age=24 * 3600)
    assert [t.trial_dir.name for t in live_trials([root], NOW)] == ["edge__a"]


def test_a_finished_job_has_no_live_trials(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root, "j", "killed__a", steps=STEPS)
    (root / "j" / "result.json").write_text(json.dumps({"finished_at": "2026-10-28T19:00:00Z"}))
    assert live_trials([root], NOW) == []
    (root / "j" / "result.json").write_text(json.dumps({"finished_at": None}))
    assert len(live_trials([root], NOW)) == 1


def test_exec_stage_jobs_are_scanned_and_other_dot_dirs_are_not(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root / ".exec-stage", "staged", "t__a", steps=STEPS)
    _trial(root, ".executor", "t__b", steps=STEPS)
    assert [t.trial_dir.name for t in live_trials([root], NOW)] == ["t__a"]


def test_missing_roots_are_skipped(tmp_path: Path) -> None:
    assert live_trials([tmp_path / "nope"], NOW) == []


def test_default_roots_cover_checkout_worktrees_and_sibling_worktrees(tmp_path: Path) -> None:
    lab, home = tmp_path / "eval-lab", tmp_path / "home"
    (lab / ".worktrees" / "w1" / "runs").mkdir(parents=True)
    (home / "Developer" / "eval-lab-worktrees" / "w2" / "runs").mkdir(parents=True)
    assert default_roots(lab, home) == [lab / "runs", lab / ".worktrees" / "w1" / "runs",
                                        home / "Developer" / "eval-lab-worktrees" / "w2" / "runs"]


# --------------------------------------------------------------------------- source: observations


def test_subject_fields_and_killable_only_under_a_lab_root(tmp_path: Path) -> None:
    lab, other = tmp_path / "lab-runs", tmp_path / "other-runs"
    _trial(lab, "jl", "t__a", steps=STEPS)
    _trial(other, "jo", "t__b", steps=STEPS)
    observed = _observe(_source(tmp_path, [lab, other], lab_roots=[lab]))
    mine, theirs = observed["jl/t__a"].subject, observed["jo/t__b"].subject
    assert (mine.kind, mine.owner, mine.killable, mine.run_id) == ("harbor_trial", "lab", True, "t__a")
    assert (theirs.owner, theirs.killable, theirs.run_id) == ("lab", False, "t__b")


def test_lab_roots_default_to_every_scanned_root(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root, "j", "t__a", steps=STEPS)
    assert _observe(_source(tmp_path, [root]))["j/t__a"].subject.killable is True


def test_the_same_subject_id_in_two_roots_is_observed_once(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _trial(a, "j", "t__a", steps=STEPS)
    _trial(b, "j", "t__a", steps=STEPS)
    assert len(list(_source(tmp_path, [a, b]).observe(NOW))) == 1


def test_observation_carries_run_health_metrics_and_events(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root, "j", "t__a", steps=STEPS)
    (root / "j" / "lock.json").write_text(json.dumps({"created_at": _iso(-100)}))
    observation = _observe(_source(tmp_path, [root]))["j/t__a"]
    m = observation.metrics
    assert (m["agent_steps"], m["prompt_tokens_per_step"], m["elapsed_seconds"]) == (2, 100.0, 100.0)
    assert m["silence_seconds"] == 10.0  # the last step is newer than the 60 s old trajectory file
    assert m["features"]["agent_steps"] == 2
    assert [e.index for e in observation.events] == [1, 2]
    assert observation.actor_inputs_available is True
    assert observation.terminal is False


def test_trial_start_prefers_the_hook_start_event_over_the_job_lock(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root, "j", "t__a", steps=STEPS)
    (root / "j" / "lock.json").write_text(json.dumps({"created_at": _iso(-1000)}))
    (root / "j" / "watch").mkdir()
    (root / "j" / "watch" / "hooks.jsonl").write_text(
        json.dumps({"at": _iso(-300), "event": "start", "kind": "event", "trial": "t__a"}) + "\n")
    assert _observe(_source(tmp_path, [root]))["j/t__a"].metrics["elapsed_seconds"] == 300.0


def test_a_trial_with_a_terminal_hook_is_reported_terminal(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root, "j", "t__a", steps=STEPS)
    (root / "j" / "watch").mkdir()
    (root / "j" / "watch" / "hooks.jsonl").write_text(
        json.dumps({"at": _iso(-5), "event": "end", "kind": "event", "trial": "t__a"}) + "\n")
    assert _observe(_source(tmp_path, [root]))["j/t__a"].terminal is True


def test_a_hook_event_counts_as_activity_for_silence(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root, "j", "t__a", steps=[_step(1, "ls", offset=-5000)], age=5000)
    (root / "j" / "watch").mkdir()
    (root / "j" / "watch" / "hooks.jsonl").write_text(
        json.dumps({"at": _iso(-100), "event": "verification-start", "kind": "event", "trial": "t__a"}) + "\n")
    assert _observe(_source(tmp_path, [root]))["j/t__a"].metrics["silence_seconds"] == 100.0


def test_a_trial_without_a_trajectory_has_no_transcript(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root, "j", "t__a")
    observation = _observe(_source(tmp_path, [root]))["j/t__a"]
    assert observation.actor_inputs_available is False
    assert observation.events == ()


# --------------------------------------------------------------------------- source: alerts and findings


def _watch(root: Path, job: str, *, alerts: list[dict[str, Any]], acks: list[dict[str, Any]] | None = None) -> None:
    watch = root / job / "watch"
    watch.mkdir(parents=True, exist_ok=True)
    (watch / "alerts.jsonl").write_text("".join(json.dumps(a) + "\n" for a in alerts))
    if acks:
        (watch / "acks.jsonl").write_text("".join(json.dumps(a) + "\n" for a in acks))


def test_live_watch_alerts_feed_triggers_and_acks_silence_them(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root, "j", "t__a", steps=STEPS)
    alerts = [_alert("hidden_test_read", trial="t__a"), _alert("infra_error", trial="t__a"),
              _alert("grader_tamper", trial="other__z"),
              {"rule": "proxy_error_spike", "trial": "fleet:proxy_error_spike", "detail": "spike"}]
    _watch(root, "j", alerts=alerts)
    source = _source(tmp_path, [root])
    hits = source.triggers(_observe(source)["j/t__a"])
    # the fleet-wide spike also applies to this trial; the alert of other__z does not
    assert sorted((h.trigger, h.kind) for h in hits) == [
        ("error_signature", "infra"), ("error_signature", "infra"), ("trace_alarm", "cheat")]

    acks = [{"rule": "hidden_test_read", "actor": "peter", "covers": [{"trial": "t__a", "rule": "hidden_test_read"}]},
            {"rule": "infra_error", "actor": "peter", "covers": [{"trial": "t__a", "rule": "infra_error"}]},
            {"rule": "proxy_error_spike", "actor": "peter",
             "covers": [{"trial": "fleet:proxy_error_spike", "rule": "proxy_error_spike"}]}]
    _watch(root, "j", alerts=alerts, acks=acks)
    assert source.triggers(_observe(source)["j/t__a"]) == []


def test_an_ack_covering_another_trial_does_not_silence_this_one(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root, "j", "t__a", steps=STEPS)
    _watch(root, "j", alerts=[_alert("hidden_test_read", trial="t__a")],
           acks=[{"rule": "hidden_test_read", "actor": "peter",
                  "covers": [{"trial": "t__other", "rule": "hidden_test_read"}]}])
    source = _source(tmp_path, [root])
    assert [h.trigger for h in source.triggers(_observe(source)["j/t__a"])] == ["trace_alarm"]


def test_trace_harness_findings_alarm_the_matching_run_only(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root, "j", "t__a", steps=STEPS)
    _trial(root, "j", "t__b", steps=STEPS)
    rows = [
        {"run_id": "t__a", "detector": "tripwire", "verdict": "alarm", "kind": "cheat", "score": 1.0,
         "summary": "hackbook", "evidence_steps": [2]},
        {"run_id": "t__b", "detector": "facets", "verdict": "clear", "kind": "cheat", "score": 0.0},
        {"run_id": "t__b", "detector": "scout", "verdict": "alarm", "kind": "loop", "score": 1.0},
        {"run_id": "t__b", "detector": "live_supervisor", "verdict": "alarm", "kind": "cheat", "score": 1.0},
    ]
    (tmp_path / "findings.jsonl").write_text("not json\n" + "".join(json.dumps(r) + "\n" for r in rows))
    source = _source(tmp_path, [root])
    observed = _observe(source)
    assert [(h.trigger, h.evidence_steps) for h in source.triggers(observed["j/t__a"])] == [("trace_alarm", (2,))]
    assert source.triggers(observed["j/t__b"]) == []


def test_a_missing_findings_file_is_no_alarm(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _trial(root, "j", "t__a", steps=STEPS)
    source = _source(tmp_path, [root])
    assert source.triggers(_observe(source)["j/t__a"]) == []


def test_source_name_and_no_arg_construction() -> None:
    source = HarborSource()
    assert source.name == "harbor"
    assert source.triggers(trial_observation(_run([_step(1, offset=-1)]), subject=SUBJECT, now=NOW)) == []
