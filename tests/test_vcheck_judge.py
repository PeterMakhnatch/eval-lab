"""Offline tests for the blind VerifierCheck adjudicator (no models, no Docker)."""

from __future__ import annotations

import json

import pytest

from evallab.vcheck_judge import (
    ADJUDICATOR_MAX_TOKENS,
    ADJUDICATOR_MODEL,
    ADJUDICATOR_REASONING_EFFORT,
    ADJUDICATOR_TEMPERATURE,
    RECOMMENDATIONS,
    adjudicate,
    build_adjudication_prompt,
    confirm_hypothesis,
    parse_adjudication_response,
)


class FakeClient:
    """Minimal CLIENT-compatible fake capturing the exact model inputs."""

    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def chat_completion(self, *, model, messages, temperature, max_tokens, budget):
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "budget": budget,
            }
        )
        return {"content": json.dumps(self.reply)}


def _inputs(**overrides):
    base = {
        "instruction": "Land at the airport whose ICAO code is in the task data.",
        "requirement_map": {
            "items": [
                {"req_id": "R1", "text": "Land at the airport named by the task."},
                {"req_id": "R2", "text": "Use the heading the task data specifies."},
            ]
        },
        "submission_diff": "- heading = data['heading']\n+ heading = -data['heading']",
        "observed_checks": ["checks landing airport", "does not check heading sign"],
    }
    base.update(overrides)
    return base


def test_prompt_states_both_confirmation_questions() -> None:
    prompt = build_adjudication_prompt(**_inputs())
    assert "behaviorally different" in prompt
    assert "equivalent mutants" in prompt
    assert "require the changed behavior" in prompt
    assert "requirement id" in prompt
    assert "R1" in prompt and "heading sign" in prompt


def test_prompt_excludes_reward_and_strategy_even_when_known() -> None:
    # Sentinels stand in for values the caller holds (verifier reward, red
    # strategy/prose) that must never reach the blind adjudicator.
    reward_sentinel = "REWARD_SENTINEL_7f3a_reward_is_1"
    strategy_sentinel = "STRATEGY_SENTINEL_9b2c_flip_the_sign"
    prompt = build_adjudication_prompt(**_inputs())
    assert reward_sentinel not in prompt
    assert strategy_sentinel not in prompt

    fake = FakeClient({"recommend": "confirm", "rationale": "Behaviorally wrong, R2 cited."})
    result = adjudicate(**_inputs(), client=fake, budget=object())
    assert result["recommend"] == "confirm"
    assert len(fake.calls) == 1
    shown = json.dumps(fake.calls[0]["messages"])
    assert reward_sentinel not in shown
    assert strategy_sentinel not in shown


def test_adjudicate_uses_model_defaults() -> None:
    fake = FakeClient({"recommend": "reject", "rationale": "Equivalent mutant."})
    result = adjudicate(**_inputs(), client=fake, budget=object())
    assert result == {"recommend": "reject", "rationale": "Equivalent mutant."}
    call = fake.calls[0]
    assert call["model"] == ADJUDICATOR_MODEL == "glm-5.3"
    assert call["temperature"] == ADJUDICATOR_TEMPERATURE == 0.1
    assert call["max_tokens"] == ADJUDICATOR_MAX_TOKENS == 4096
    assert ADJUDICATOR_REASONING_EFFORT == "low"


def test_adjudicate_recommend_values_constrained() -> None:
    cases = [
        ({"recommend": "confirm", "rationale": "Wrong, R1."}, "confirm"),
        ({"recommend": "reject", "rationale": "Equivalent."}, "reject"),
        ({"recommend": "needs_evidence", "rationale": "Thin."}, "needs_evidence"),
        ({"recommend": "maybe", "rationale": "Unsure."}, "needs_evidence"),
        ({"rationale": "Missing recommend."}, "needs_evidence"),
    ]
    for raw, expected in cases:
        fake = FakeClient(raw)
        result = adjudicate(**_inputs(), client=fake, budget=object())
        assert result["recommend"] == expected, raw
        assert result["recommend"] in RECOMMENDATIONS
        assert result["rationale"]


def test_parse_collapses_garbage_to_needs_evidence() -> None:
    assert parse_adjudication_response("no json here")["recommend"] == "needs_evidence"
    assert parse_adjudication_response("{not json")["recommend"] == "needs_evidence"
    assert parse_adjudication_response("[1,2]")["recommend"] == "needs_evidence"
    parsed = parse_adjudication_response('prefix {"recommend": "REJECT", "rationale": "x"} suffix')
    assert parsed["recommend"] == "reject"


def test_confirm_hypothesis_records_actor_and_history_dict() -> None:
    hypothesis = {"id": "h1", "status": "candidate", "history": []}
    out = confirm_hypothesis(hypothesis, {"reward": 1, "reward_blank": 0}, actor="peter")
    assert out is hypothesis
    assert hypothesis["status"] == "promoted"
    assert len(hypothesis["history"]) == 1
    entry = hypothesis["history"][0]
    assert entry["actor"] == "peter"
    assert entry["at"]
    assert entry["to"] == "promoted"
    assert entry["verdict"] == {"reward": 1, "reward_blank": 0}


def test_confirm_hypothesis_records_actor_and_history_object() -> None:
    class H:
        def __init__(self) -> None:
            self.status = "candidate"
            self.history: list = []

    h = H()
    confirm_hypothesis(h, "objective re-grade: survived", actor="oracle-regrade")
    assert h.status == "promoted"
    assert h.history[0]["actor"] == "oracle-regrade"
    assert h.history[0]["at"]
    assert h.history[0]["verdict"] == "objective re-grade: survived"


def test_confirm_hypothesis_rejects_empty_actor() -> None:
    with pytest.raises(ValueError):
        confirm_hypothesis({"status": "candidate", "history": []}, "v", actor="  ")
