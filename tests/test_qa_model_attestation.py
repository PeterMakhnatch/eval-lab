"""Model attestation: a run's requested model must equal the model that answered."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.qa.agent_lanes import (
    DriverError,
    ModelMismatch,
    attest_audit_session,
    attest_model,
    attest_transcript,
    model_observations,
    normalize_model,
    require_attested,
)


def _assistant(provider: str, model: str, kind: str = "message_end") -> dict:
    return {"type": kind, "message": {"role": "assistant", "provider": provider, "model": model}}


def _write_jsonl(path: Path, events: list[dict]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def test_normalize_model_strips_selector_and_aliases_codex():
    assert normalize_model(" Anthropic/Claude-Opus-5-5:high ") == "anthropic/claude-opus-5-5"
    assert normalize_model("codex/gpt-6.1-sol") == "openai-codex/gpt-6.1-sol"
    assert normalize_model("") is None and normalize_model(None) is None


def test_opus_label_over_codex_transcript_is_a_mismatch(tmp_path):
    """The plain-read baseline: a receipt said Opus, every message was Codex gpt-6.1-sol."""
    events = [_assistant("openai-codex", "gpt-6.1-sol") for _ in range(3)]
    path = _write_jsonl(tmp_path / "reader.jsonl", events)
    attestation = attest_transcript("anthropic/claude-opus-5-5", path)
    assert attestation["status"] == "mismatch"
    assert attestation["observed"] == ["openai-codex/gpt-6.1-sol"]
    assert attestation["answers"] == {"openai-codex/gpt-6.1-sol": 3}
    with pytest.raises(ModelMismatch, match="mismatch"):
        require_attested(attestation)
    with pytest.raises(ModelMismatch):  # allowing unreported never excuses a mismatch
        require_attested(attestation, allow_unreported=True)


def test_matching_transcript_attests_and_ignores_repeated_stream_events(tmp_path):
    events = [
        _assistant("anthropic", "claude-opus-5-5", "message_start"),
        _assistant("anthropic", "claude-opus-5-5", "message_update"),
        _assistant("anthropic", "claude-opus-5-5"),
    ]
    attestation = attest_transcript("anthropic/claude-opus-5-5:high", _write_jsonl(tmp_path / "t.jsonl", events))
    assert attestation["status"] == "matched"
    assert attestation["answers"] == {"anthropic/claude-opus-5-5": 1}
    assert require_attested(attestation) is attestation


def test_model_change_fallback_is_a_mismatch_even_when_names_agree():
    events = [{"type": "model_change", "model": "anthropic/claude-sonnet-5-5", "resolvedModelIsFallback": True}]
    attestation = attest_model("anthropic/claude-sonnet-5-5", model_observations(events))
    assert attestation["status"] == "mismatch" and attestation["fallback"] is True


def test_a_second_served_model_in_one_session_is_a_mismatch():
    events = [_assistant("anthropic", "claude-sonnet-5-5"), _assistant("google-antigravity", "gemini-4-argon-high")]
    attestation = attest_model("anthropic/claude-sonnet-5-5", model_observations(events))
    assert attestation["status"] == "mismatch"
    assert attestation["observed"] == ["anthropic/claude-sonnet-5-5", "google-antigravity/gemini-4-argon-high"]


def test_nothing_observed_is_unreported_not_matched():
    attestation = attest_model("openai-codex/gpt-6.1-sol", [])
    assert attestation["status"] == "unreported"
    assert require_attested(attestation, allow_unreported=True) is attestation
    with pytest.raises(ModelMismatch, match="unreported"):
        require_attested(attestation)
    assert attest_model(None, [{"model": "a/b"}])["status"] == "unreported"
    assert issubclass(ModelMismatch, DriverError)


def _session(tmp_path: Path, *, model: str, journal_models: list[tuple[str, str | None]], actual=None,
             transcript: list[dict] | None = None) -> Path:
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "session.json").write_text(json.dumps(
        {"schema": "qa.env.audit/v2", "model": model, "lane": "omp", "actual_model": actual}))
    _write_jsonl(agent / "journal.jsonl", [
        {"seq": i, "event": "model_turn", "requested_model": asked, "actual_model": got}
        for i, (asked, got) in enumerate(journal_models, 1)])
    if transcript is not None:
        (agent / "model" / "sessions").mkdir(parents=True)
        _write_jsonl(agent / "model" / "sessions" / "s.jsonl", transcript)
    return agent


def test_audit_session_matches_journal_and_transcript(tmp_path):
    model = "meta/muse-wonder-1.0-contributor"
    agent = _session(tmp_path, model=model, journal_models=[(model, model)] * 2, actual=model,
                     transcript=[_assistant("meta", "muse-wonder-1.0-contributor", "message")])
    attestation = attest_audit_session(agent)
    assert attestation["status"] == "matched"
    assert (attestation["model_turns"], attestation["transcripts"], attestation["lane"]) == (2, 1, "omp")


def test_audit_session_codex_lane_without_actual_model_is_unreported(tmp_path):
    agent = _session(tmp_path, model="codex/gpt-6.1-sol", journal_models=[("codex/gpt-6.1-sol", None)])
    assert attest_audit_session(agent)["status"] == "unreported"


def test_audit_session_journal_requesting_another_model_is_a_mismatch(tmp_path):
    agent = _session(tmp_path, model="anthropic/claude-sonnet-5-5", journal_models=[
        ("anthropic/claude-sonnet-5-5", "anthropic/claude-sonnet-5-5"),
        ("openai-codex/gpt-6.1-sol", "anthropic/claude-sonnet-5-5")])
    attestation = attest_audit_session(agent)
    assert attestation["status"] == "mismatch" and "journal turns requested" in attestation["reason"]


def test_audit_session_transcript_disagreeing_with_journal_is_a_mismatch(tmp_path):
    model = "anthropic/claude-opus-5-5"
    agent = _session(tmp_path, model=model, journal_models=[(model, model)],
                     transcript=[_assistant("openai-codex", "gpt-6.1-sol", "message")])
    assert attest_audit_session(agent)["status"] == "mismatch"
