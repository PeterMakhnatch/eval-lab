from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from evallab.production.live_supervisor import (
    SUMMARY_CHARS,
    Decision,
    DecisionLog,
    Event,
    Observation,
    Subject,
    TriggerHit,
    propose,
    scan_once,
)
from evallab.production.live_supervisor_triage import (
    _SYSTEM,
    BUDGET_FILE,
    BUDGET_PREFIX,
    DECIDE_MODEL,
    DEEP_MODEL,
    EVENT_CHARS,
    PROMPT_CHARS,
    TRIAGE_MODEL,
    ModelDecider,
    ReaderOutputError,
    build_prompt,
)
from evallab.production.run_atlas import Completion

NOW = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)


class FakeTransport:
    """Scripted replies per model; records every call."""

    def __init__(self, replies: Mapping[str, Sequence[str]], *, tokens: tuple[int, int] = (100, 20)) -> None:
        self.replies = {model: list(texts) for model, texts in replies.items()}
        self.tokens = tokens
        self.calls: list[tuple[str, list[dict[str, str]], int]] = []

    def complete(self, model: str, messages: Sequence[Mapping[str, str]], *, max_tokens: int) -> Completion:
        self.calls.append((model, [dict(m) for m in messages], max_tokens))
        text = self.replies[model].pop(0)
        return Completion(text, model, self.tokens[0], self.tokens[1], 0.5)

    @property
    def models(self) -> list[str]:
        return [model for model, _, _ in self.calls]


def answer(action: str, kind: str = "loop", *, steps: Sequence[object] = (3,), confidence: float = 0.8,
           reason: str = "the same call repeats") -> str:
    return json.dumps({"action": action, "kind": kind, "confidence": confidence, "reason": reason,
                       "evidence_steps": list(steps)})


def events(count: int = 25, *, text: str = "bash: pytest -x") -> tuple[Event, ...]:
    return tuple(Event(i, NOW - timedelta(seconds=count - i), "trajectory", f"{text} #{i}") for i in range(count))


def observation(*, kind: str = "harbor_trial", killable: bool = True, owner: str = "lab",
                metrics: Mapping[str, object] | None = None, window: Sequence[Event] | None = None) -> Observation:
    subject = Subject(kind, "trial-1", owner, killable, label="cheatbench/some-task")
    return Observation(subject, NOW, tuple(events() if window is None else window),
                       {"elapsed_seconds": 600, "steps": 25} if metrics is None else dict(metrics))


def lane() -> Observation:
    """An OMP lane: not killable, nudgeable, so a ``nudge`` is a non-kill proposal."""
    return observation(killable=False, owner="fleet", kind="omp_lane")


def repetition_hit() -> TriggerHit:
    return TriggerHit("repetition", "loop", 2.0, "same command 12 times", (3, 4))


def decider(transport: FakeTransport, root: Path, **kwargs: int) -> ModelDecider:
    return ModelDecider(transport, root, clock=lambda: NOW, **kwargs)


def run(obs: Observation, hits: Sequence[TriggerHit], model: ModelDecider) -> Decision:
    return model.decide(obs, hits, propose(obs.subject, hits))


# --------------------------------------------------------------------------- escalation matrix


def test_agreeing_non_kill_answer_stops_at_triage(tmp_path: Path) -> None:
    obs = lane()
    assert propose(obs.subject, [repetition_hit()]).action == "nudge"
    transport = FakeTransport({TRIAGE_MODEL: [answer("nudge")]})
    decision = run(obs, [repetition_hit()], decider(transport, tmp_path))
    assert transport.models == [TRIAGE_MODEL]
    assert (decision.action, decision.reader_model) == ("nudge", TRIAGE_MODEL)
    assert decision.cost_usd == 0.0
    assert (decision.input_tokens, decision.output_tokens, decision.latency_ms) == (100, 20, 500)


def test_kill_answer_agreeing_with_proposal_still_escalates_to_sonnet(tmp_path: Path) -> None:
    obs = observation()
    assert propose(obs.subject, [repetition_hit()]).action == "kill_and_retry"
    transport = FakeTransport({TRIAGE_MODEL: [answer("kill_and_retry")],
                               DECIDE_MODEL: [answer("kill_and_retry", reason="confirmed loop")]})
    decision = run(obs, [repetition_hit()], decider(transport, tmp_path))
    assert transport.models == [TRIAGE_MODEL, DECIDE_MODEL]
    assert decision.reader_model == DECIDE_MODEL
    assert decision.reason == "confirmed loop"
    assert (decision.input_tokens, decision.output_tokens, decision.latency_ms) == (200, 40, 1000)


def test_disagreement_with_proposal_escalates_and_the_final_answer_wins(tmp_path: Path) -> None:
    transport = FakeTransport({TRIAGE_MODEL: [answer("continue", "other")],
                               DECIDE_MODEL: [answer("kill_and_retry")]})
    decision = run(observation(), [repetition_hit()], decider(transport, tmp_path))
    assert transport.models == [TRIAGE_MODEL, DECIDE_MODEL]
    assert (decision.action, decision.reader_model) == ("kill_and_retry", DECIDE_MODEL)


def test_sonnet_may_overrule_a_triage_kill(tmp_path: Path) -> None:
    transport = FakeTransport({TRIAGE_MODEL: [answer("kill_and_retry")], DECIDE_MODEL: [answer("continue", "other")]})
    decision = run(observation(), [repetition_hit()], decider(transport, tmp_path))
    assert (decision.action, decision.reader_model) == ("continue", DECIDE_MODEL)


@pytest.mark.parametrize("action", ["kill_and_retry", "kill_and_mark_infra"])
def test_long_running_kill_goes_to_opus(tmp_path: Path, action: str) -> None:
    obs = observation(metrics={"elapsed_seconds": 1801})
    transport = FakeTransport({TRIAGE_MODEL: [answer(action)], DEEP_MODEL: [answer(action)]})
    decision = run(obs, [repetition_hit()], decider(transport, tmp_path))
    assert transport.models == [TRIAGE_MODEL, DEEP_MODEL]
    assert decision.reader_model == DEEP_MODEL


def test_elapsed_exactly_at_the_limit_stays_on_sonnet(tmp_path: Path) -> None:
    obs = observation(metrics={"elapsed_seconds": 1800})
    transport = FakeTransport({TRIAGE_MODEL: [answer("kill_and_retry")], DECIDE_MODEL: [answer("kill_and_retry")]})
    assert run(obs, [repetition_hit()], decider(transport, tmp_path)).reader_model == DECIDE_MODEL


def test_modal_app_kill_goes_to_opus_even_when_short(tmp_path: Path) -> None:
    obs = observation(kind="modal_app", metrics={"elapsed_seconds": 60})
    transport = FakeTransport({TRIAGE_MODEL: [answer("kill_and_mark_infra", "infra")],
                               DEEP_MODEL: [answer("kill_and_mark_infra", "infra")]})
    hits = [TriggerHit("error_signature", "infra", 1.0, "sandbox not found")]
    decision = run(obs, hits, decider(transport, tmp_path))
    assert transport.models == [TRIAGE_MODEL, DEEP_MODEL]
    assert decision.reader_model == DEEP_MODEL


def test_long_running_non_kill_disagreement_uses_sonnet_not_opus(tmp_path: Path) -> None:
    obs = observation(metrics={"elapsed_seconds": 9000})
    transport = FakeTransport({TRIAGE_MODEL: [answer("alert_research", "other")],
                               DECIDE_MODEL: [answer("alert_research", "other")]})
    decision = run(obs, [repetition_hit()], decider(transport, tmp_path))
    assert transport.models == [TRIAGE_MODEL, DECIDE_MODEL]
    assert decision.reader_model == DECIDE_MODEL


def test_decide_step_sees_the_triage_answer(tmp_path: Path) -> None:
    transport = FakeTransport({TRIAGE_MODEL: [answer("kill_and_retry", reason="triage says loop")],
                               DECIDE_MODEL: [answer("kill_and_retry")]})
    run(observation(), [repetition_hit()], decider(transport, tmp_path))
    first, second = transport.calls[0][1][1]["content"], transport.calls[1][1][1]["content"]
    assert "First-pass triage" not in first
    assert "triage says loop" in second


# --------------------------------------------------------------------------- invalid output


def test_invalid_json_is_retried_once_then_succeeds(tmp_path: Path) -> None:
    transport = FakeTransport({TRIAGE_MODEL: ["not json at all", answer("nudge")]})
    decision = run(lane(), [repetition_hit()], decider(transport, tmp_path))
    assert decision.action == "nudge"
    assert len(transport.calls) == 2
    assert "not JSON" in transport.calls[1][1][-1]["content"]
    assert (decision.input_tokens, decision.output_tokens) == (200, 40)  # the failed attempt is paid for


def test_action_outside_actions_is_invalid_and_raises_after_one_retry(tmp_path: Path) -> None:
    transport = FakeTransport({TRIAGE_MODEL: [answer("reboot_everything"), answer("shutdown")]})
    with pytest.raises(ReaderOutputError, match="no valid decision twice"):
        run(observation(), [repetition_hit()], decider(transport, tmp_path))
    assert len(transport.calls) == 2


def _bad(**override: object) -> str:
    base: dict[str, object] = {"action": "continue", "kind": "other", "confidence": 0.5, "reason": "x",
                               "evidence_steps": []}
    return json.dumps({**base, **override})


@pytest.mark.parametrize("payload", [
    "[1, 2]",
    _bad(kind="bogus"),
    _bad(confidence=1.5),
    _bad(confidence=True),
    _bad(reason="  "),
    _bad(evidence_steps="3"),
])
def test_malformed_decisions_are_rejected(tmp_path: Path, payload: str) -> None:
    transport = FakeTransport({TRIAGE_MODEL: [payload, payload]})
    with pytest.raises(ReaderOutputError):
        run(observation(), [repetition_hit()], decider(transport, tmp_path))


def test_invalid_second_step_raises_and_scan_once_falls_back_to_the_proposal(tmp_path: Path) -> None:
    class Source:
        name = "fake"

        def observe(self, now: datetime) -> list[Observation]:
            return [observation()]

        def triggers(self, obs: Observation) -> list[TriggerHit]:
            return [repetition_hit()]

    transport = FakeTransport({TRIAGE_MODEL: [answer("kill_and_retry")], DECIDE_MODEL: ["nope", "still nope"]})
    report = scan_once([Source()], decider(transport, tmp_path / "reader"), DecisionLog(tmp_path / "log"), now=NOW)
    (record,) = report.records
    assert record["action"] == "kill_and_retry"  # the rule proposal stands
    assert "reader failed: ReaderOutputError" in record["summary"]


def test_fenced_json_is_accepted(tmp_path: Path) -> None:
    transport = FakeTransport({TRIAGE_MODEL: [f"```json\n{answer('nudge')}\n```"]})
    assert run(lane(), [repetition_hit()], decider(transport, tmp_path)).action == "nudge"


# --------------------------------------------------------------------------- evidence


def test_evidence_steps_outside_the_shown_events_are_dropped(tmp_path: Path) -> None:
    # 25 events: indexes 5..24 are shown, 0..4 fall outside the 20-event window.
    transport = FakeTransport({TRIAGE_MODEL: [answer("nudge", steps=[2, 5, 24, 99, 24, -1, True, "7"])]})
    decision = run(lane(), [repetition_hit()], decider(transport, tmp_path))
    assert decision.evidence_steps == (5, 24)


def test_evidence_is_capped_and_reason_is_cut_to_forty_words(tmp_path: Path) -> None:
    reason = " ".join(f"w{i}" for i in range(80))
    transport = FakeTransport({TRIAGE_MODEL: [answer("nudge", steps=list(range(5, 15)), reason=reason)]})
    decision = run(lane(), [repetition_hit()], decider(transport, tmp_path))
    assert decision.evidence_steps == (5, 6, 7, 8, 9)
    assert len(decision.reason.split()) == 40


def test_a_reason_of_very_long_words_is_cut_to_the_summary_chars(tmp_path: Path) -> None:
    transport = FakeTransport({TRIAGE_MODEL: [answer("nudge", reason=" ".join(["w" * 900] * 10))]})
    decision = run(lane(), [repetition_hit()], decider(transport, tmp_path))
    assert len(decision.reason) == SUMMARY_CHARS


# --------------------------------------------------------------------------- budget


def test_budget_exhausted_returns_the_proposal_without_calling_a_model(tmp_path: Path) -> None:
    (tmp_path / BUDGET_FILE).write_text(json.dumps({"day": "2026-10-28", "tokens": 1000}))
    transport = FakeTransport({})
    obs = observation()
    proposal = propose(obs.subject, [repetition_hit()])
    decision = decider(transport, tmp_path, daily_token_budget=1000).decide(obs, [repetition_hit()], proposal)
    assert transport.calls == []
    assert decision.reader_model is None
    assert decision.reason.startswith(BUDGET_PREFIX)
    assert proposal.reason in decision.reason
    assert (decision.action, decision.kind, decision.evidence_steps) == (
        proposal.action, proposal.kind, proposal.evidence_steps)


def test_tokens_are_persisted_and_exhaust_the_budget(tmp_path: Path) -> None:
    transport = FakeTransport({TRIAGE_MODEL: [answer("nudge"), answer("nudge")]}, tokens=(100, 20))
    model = decider(transport, tmp_path, daily_token_budget=200)
    assert run(lane(), [repetition_hit()], model).reader_model == TRIAGE_MODEL  # 120 spent < 200
    assert json.loads((tmp_path / BUDGET_FILE).read_text()) == {"day": "2026-10-28", "tokens": 120}
    assert run(lane(), [repetition_hit()], model).reader_model == TRIAGE_MODEL  # 240 spent after this call
    fresh = decider(transport, tmp_path, daily_token_budget=200)  # a new process reads the file
    again = run(lane(), [repetition_hit()], fresh)
    assert again.reader_model is None and again.reason.startswith(BUDGET_PREFIX)
    assert len(transport.calls) == 2


def test_budget_resets_on_a_new_utc_day(tmp_path: Path) -> None:
    (tmp_path / BUDGET_FILE).write_text(json.dumps({"day": "2026-10-27", "tokens": 10**9}))
    transport = FakeTransport({TRIAGE_MODEL: [answer("nudge")]})
    assert run(lane(), [repetition_hit()], decider(transport, tmp_path)).reader_model == TRIAGE_MODEL
    assert json.loads((tmp_path / BUDGET_FILE).read_text())["day"] == "2026-10-28"


def test_budget_day_is_utc_not_local(tmp_path: Path) -> None:
    # 04:30 on 10-29 at UTC+5 is 23:30 on 10-28 in UTC.
    local = datetime(2026, 10, 28, 23, 30, tzinfo=UTC).astimezone(timezone(timedelta(hours=5)))
    (tmp_path / BUDGET_FILE).write_text(json.dumps({"day": "2026-10-28", "tokens": 500}))
    model = ModelDecider(FakeTransport({}), tmp_path, daily_token_budget=500, clock=lambda: local)
    assert run(observation(), [repetition_hit()], model).reader_model is None


def test_unreadable_budget_file_raises_instead_of_resetting_the_cap(tmp_path: Path) -> None:
    (tmp_path / BUDGET_FILE).write_text("{broken")
    with pytest.raises(ValueError, match="Expecting|JSON"):
        run(observation(), [repetition_hit()], decider(FakeTransport({}), tmp_path))


# --------------------------------------------------------------------------- prompt


FORBIDDEN = ("cheatbench", "some-task", "trial-1", "reward", "dataset", "label", "goodfire", "verdict")


def test_prompt_contains_no_forbidden_fields() -> None:
    obs = observation(metrics={
        "elapsed_seconds": 600, "steps": 25, "reward": 1.0, "dataset": "cheatbench", "label_cheat": True,
        "split": "dev", "verifier_passed": False, "benchmark": "goodfire", "nested": {"a": 1},
        "prompt_tokens_p99": 228000.0,
    })
    prompt, _ = build_prompt(obs, [repetition_hit()], propose(obs.subject, [repetition_hit()]))
    for word in FORBIDDEN:
        assert word not in (prompt + _SYSTEM).lower(), word
    assert "elapsed_seconds=600" in prompt and "prompt_tokens_p99=2.28e+05" in prompt
    assert "kind=harbor_trial owner=lab killable=true" in prompt


def test_prompt_lists_hits_proposal_and_every_allowed_action(tmp_path: Path) -> None:
    transport = FakeTransport({TRIAGE_MODEL: [answer("kill_and_retry")], DECIDE_MODEL: [answer("kill_and_retry")]})
    run(observation(), [repetition_hit()], decider(transport, tmp_path))
    system, user = transport.calls[0][1][0]["content"], transport.calls[0][1][1]["content"]
    for action in ("continue", "nudge", "kill_and_retry", "kill_and_mark_infra", "flag_cheating", "alert_research"):
        assert f"- {action}:" in system
    assert "Rule proposal: kill_and_retry (loop)" in user
    assert "repetition kind=loop score=2.00 steps=[3, 4]: same command 12 times" in user
    assert transport.calls[0][2] == 600


def test_prompt_shows_the_last_twenty_events_each_truncated() -> None:
    obs = observation(window=events(30, text="y" * 2000))
    prompt, shown = build_prompt(obs, [repetition_hit()], propose(obs.subject, [repetition_hit()]))
    lines = [line for line in prompt.splitlines() if line.startswith("[")]
    assert len(lines) == len(shown)
    assert shown[-1] == 29 and shown[0] >= 10
    assert all(len(line) <= EVENT_CHARS + 40 for line in lines)
    assert lines[-1].endswith("…")


def test_prompt_stays_under_the_cap_even_with_huge_hits_metrics_and_events() -> None:
    hits = [TriggerHit("repetition", "loop", 1.0 + i, "d" * 50_000, (i,)) for i in range(40)]
    obs = observation(window=events(20, text="z" * 100_000), metrics={f"m{i}": "v" * 500 for i in range(400)})
    prompt, _ = build_prompt(obs, hits, propose(obs.subject, hits), triage={"reason": "r" * 300})
    assert len(prompt) + len(_SYSTEM) <= PROMPT_CHARS


def test_prompt_with_no_events_still_builds() -> None:
    obs = observation(window=())
    prompt, shown = build_prompt(obs, [repetition_hit()], propose(obs.subject, [repetition_hit()]))
    assert shown == () and "(no events)" in prompt


def test_full_calls_stay_under_the_cap(tmp_path: Path) -> None:
    obs = observation(window=events(20, text="q" * 9000))
    transport = FakeTransport({TRIAGE_MODEL: [answer("kill_and_retry")], DECIDE_MODEL: [answer("kill_and_retry")]})
    run(obs, [repetition_hit()], decider(transport, tmp_path))
    for _, messages, _ in transport.calls:
        assert sum(len(m["content"]) for m in messages) <= PROMPT_CHARS
