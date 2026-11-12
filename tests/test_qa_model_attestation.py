"""Model attestation: a run's requested model must equal the model that answered."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.qa.agent_lanes import (
    DriverError,
    ModelMismatch,
    attest_model,
    attest_transcript,
    require_attested,
)


def _assistant(provider: str, model: str, kind: str = "message_end") -> dict:
    return {"type": kind, "message": {"role": "assistant", "provider": provider, "model": model}}


def _write_jsonl(path: Path, events: list[dict]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path




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






def test_nothing_observed_is_unreported_not_matched():
    attestation = attest_model("openai-codex/gpt-6.1-sol", [])
    assert attestation["status"] == "unreported"
    assert require_attested(attestation, allow_unreported=True) is attestation
    with pytest.raises(ModelMismatch, match="unreported"):
        require_attested(attestation)
    assert attest_model(None, [{"model": "a/b"}])["status"] == "unreported"
    assert issubclass(ModelMismatch, DriverError)










