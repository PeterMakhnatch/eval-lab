"""Model attestation: a run's requested model must equal the model that answered."""

from __future__ import annotations

import pytest

from evallab.qa.agent_lanes import DriverError, ModelMismatch, require_attested


def test_opus_label_over_codex_transcript_is_a_mismatch():
    attestation = {"status": "mismatch", "requested": "anthropic/claude-opus-5-5",
                   "observed": ["openai-codex/gpt-6.1-sol"]}
    with pytest.raises(ModelMismatch, match="mismatch"):
        require_attested(attestation)
    with pytest.raises(ModelMismatch):
        require_attested(attestation, allow_unreported=True)


def test_matching_attestation_is_accepted():
    attestation = {"status": "matched", "requested": "anthropic/claude-opus-5-5",
                   "observed": ["anthropic/claude-opus-5-5"]}
    assert require_attested(attestation) is attestation


def test_nothing_observed_is_unreported_not_matched():
    attestation = {"status": "unreported", "requested": "openai-codex/gpt-6.1-sol", "observed": []}
    assert require_attested(attestation, allow_unreported=True) is attestation
    with pytest.raises(ModelMismatch, match="unreported"):
        require_attested(attestation)
    assert issubclass(ModelMismatch, DriverError)


