"""An admitted self-hosted LoRA adapter selector runs under its base model's profile."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.execution_contracts import MIMO_SELFHOSTED_ADAPTERS, MIMO_SELFHOSTED_MODEL_SELECTOR
from evallab.profiles import validate_model_pin
from evallab.runner import RunRequest, profile_for_request


def _request(tmp_path: Path, model: str) -> RunRequest:
    return RunRequest(
        task=tmp_path, agent="terminus-2", model=model, name="profile-check", jobs_dir=tmp_path
    )


def test_adapter_selector_inherits_the_base_selfhosted_profile(tmp_path: Path) -> None:
    base = profile_for_request(_request(tmp_path, MIMO_SELFHOSTED_MODEL_SELECTOR))
    for adapter in MIMO_SELFHOSTED_ADAPTERS:
        selector = f"{MIMO_SELFHOSTED_MODEL_SELECTOR}:{adapter}"
        profile = profile_for_request(_request(tmp_path, selector))
        assert profile == base
        validate_model_pin(profile, selector)


def test_unadmitted_adapter_suffix_has_no_profile(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no profile pins model"):
        profile_for_request(_request(tmp_path, f"{MIMO_SELFHOSTED_MODEL_SELECTOR}:har999"))


def test_unadmitted_adapter_suffix_fails_the_base_pin(tmp_path: Path) -> None:
    base = profile_for_request(_request(tmp_path, MIMO_SELFHOSTED_MODEL_SELECTOR))
    with pytest.raises(ValueError, match="change profiles, not pins"):
        validate_model_pin(base, f"{MIMO_SELFHOSTED_MODEL_SELECTOR}:har999")
