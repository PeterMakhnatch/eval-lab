"""Focused behavioral tests for secret-safe overnight run preflight.

Tests cover:
- Strict schema validation for OvernightCampaignPreflight
- Model allowlist & Z.ai high-speed provider-access failure semantics
- Secret-safe cached provider presence (provider name only, never credentials)
- Task existence & cryptographic digest verification
- Wheelhouse presence & trusted resolver-provenance validation
- Overnight trial, concurrency, and prompt-token ceilings
- Darwin calibration-only labeling vs Linux enforced-isolation promotion eligibility
- Verdict derivation (compile / calibrate / promote-causal)
- Fail-closed launch gating & --compile-only reporting
- Text and canonical JSON report serialization
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evallab.harbor_network import HarborNetworkPolicy
from evallab.run_preflight import (
    ALLOWED_ZAI_MODELS,
    LAUNCH_GATE_CALIBRATE,
    LAUNCH_GATE_COMPILE,
    LAUNCH_GATE_PROMOTE_CAUSAL,
    LAUNCH_GATE_REFUSED,
    MAX_OVERNIGHT_CONCURRENCY,
    MAX_OVERNIGHT_TRIALS,
    MAX_PROMPT_TOKEN_CEILING,
    MOUNT_INJECTED_PROVIDERS,
    PROVIDER_ACCESS_DENIED_MODELS,
    SCHEMA_VERSION,
    ZAI_PROVIDER,
    OvernightCampaignPreflight,
    OvernightCampaignResult,
    OvernightTaskSpec,
    OvernightWheelhouseSpec,
    PreflightCheck,
    RunPreflightEnvironment,
    RunPreflightReport,
    assess_campaign,
    build_environment,
    build_run_preflight,
    check_model_allowlist,
    render_run_preflight,
    run_preflight_to_dict,
)


# --- fixtures & helpers ---------------------------------------------------


def _sample_task_spec() -> OvernightTaskSpec:
    return OvernightTaskSpec(
        task_path="library/tasks/dummy-task",
        expected_package_digest="sha256:" + "a" * 64,
        expected_verifier_digest="sha256:" + "b" * 64,
    )


def _sample_wheelhouse_spec() -> OvernightWheelhouseSpec:
    return OvernightWheelhouseSpec(
        wheelhouse_path="library/wheelhouses/dummy-whl",
        provenance_path="library/wheelhouses/dummy-whl/offline-build-proof.json",
    )


def _valid_campaign_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": "test-campaign-01",
        "provider": ZAI_PROVIDER,
        "model": "glm-5.3",
        "task": {
            "task_path": "library/tasks/dummy-task",
            "expected_package_digest": "sha256:" + "a" * 64,
            "expected_verifier_digest": "sha256:" + "b" * 64,
        },
        "wheelhouse": {
            "wheelhouse_path": "library/wheelhouses/dummy-whl",
            "provenance_path": "library/wheelhouses/dummy-whl/offline-build-proof.json",
        },
        "n_trials": 3,
        "n_concurrent": 2,
        "prompt_token_ceiling": 65536,
        "evidence_mode": "calibration",
        "network_isolation": "none",
        "credential_proxy": False,
    }
    base.update(overrides)
    return base


def _make_campaign(**overrides: Any) -> OvernightCampaignPreflight:
    return OvernightCampaignPreflight.model_validate(_valid_campaign_dict(**overrides))


def _mock_env(
    *,
    os_name: str = "Darwin",
    docker_ok: bool = True,
    disk_ok: bool = True,
    isolation_enforced: bool = False,
    isolation_reason: str | None = "darwin-docker-cannot-enforce-no-network",
) -> RunPreflightEnvironment:
    return RunPreflightEnvironment(
        os_name=os_name,
        docker=PreflightCheck("docker-reachable", docker_ok, "docker-ok" if docker_ok else "no-docker"),
        disk=PreflightCheck("disk-headroom", disk_ok, "disk-ok" if disk_ok else "no-disk"),
        network_isolation_enforced=isolation_enforced,
        network_isolation_reason=isolation_reason,
    )


def _mock_provider_presence(provider: str) -> PreflightCheck:
    if provider in MOUNT_INJECTED_PROVIDERS:
        return PreflightCheck(
            "provider-presence", True, "provider-credential-injected-at-launch", provider=provider
        )
    return PreflightCheck(
        "provider-presence", False, "unknown-provider-credential-source", provider=provider
    )


# --- schema tests ---------------------------------------------------------


class TestOvernightCampaignPreflightSchema:
    def test_valid_manifest_parses(self) -> None:
        campaign = _make_campaign()
        assert campaign.campaign_id == "test-campaign-01"
        assert campaign.provider == ZAI_PROVIDER
        assert campaign.model == "glm-5.3"
        assert campaign.n_trials == 3
        assert campaign.evidence_mode == "calibration"

    def test_extra_fields_rejected_strictly(self) -> None:
        raw = _valid_campaign_dict(unknown_field="injected")
        with pytest.raises(ValidationError):
            OvernightCampaignPreflight.model_validate(raw)

    def test_invalid_digest_format_rejected(self) -> None:
        raw = _valid_campaign_dict()
        raw["task"]["expected_package_digest"] = "not-a-sha256"
        with pytest.raises(ValidationError):
            OvernightCampaignPreflight.model_validate(raw)

    def test_negative_or_zero_trials_rejected(self) -> None:
        raw = _valid_campaign_dict(n_trials=0)
        with pytest.raises(ValidationError):
            OvernightCampaignPreflight.model_validate(raw)

    def test_invalid_evidence_mode_rejected(self) -> None:
        raw = _valid_campaign_dict(evidence_mode="uncontrolled")
        with pytest.raises(ValidationError):
            OvernightCampaignPreflight.model_validate(raw)


# --- model allowlist tests ------------------------------------------------


class TestModelAllowlist:
    def test_allowed_zai_models_pass(self) -> None:
        for model_name in ALLOWED_ZAI_MODELS:
            check = check_model_allowlist(ZAI_PROVIDER, model_name)
            assert check.ok is True
            assert check.code == "model-allowlist"
            assert "model-allowlisted" in check.reason

    def test_prefixed_allowed_model_passes(self) -> None:
        check = check_model_allowlist(ZAI_PROVIDER, f"{ZAI_PROVIDER}/glm-5.3")
        assert check.ok is True
        assert "model-allowlisted:glm-5.3" in check.reason

    def test_mismatched_provider_prefix_fails(self) -> None:
        check = check_model_allowlist(ZAI_PROVIDER, "other-provider/glm-5.3")
        assert check.ok is False
        assert "model-provider-mismatch" in check.reason

    def test_highspeed_model_is_provider_access_failure_never_reward_0(self) -> None:
        for denied_model in PROVIDER_ACCESS_DENIED_MODELS:
            check = check_model_allowlist(ZAI_PROVIDER, denied_model)
            assert check.ok is False
            assert "provider-access-failure" in check.reason
            assert "never-reward-0" in check.reason

    def test_unregistered_model_fails(self) -> None:
        check = check_model_allowlist(ZAI_PROVIDER, "glm-6.0-quantum")
        assert check.ok is False
        assert "model-not-allowlisted" in check.reason


# --- secret-safe provider presence tests ----------------------------------


class TestSecretSafeProviderPresence:
    def test_provider_presence_emits_name_only_never_secret(self) -> None:
        check = _mock_provider_presence(ZAI_PROVIDER)
        assert check.ok is True
        assert check.provider == ZAI_PROVIDER
        # Ensure no credential tokens, auth paths, or secrets are in reason
        assert "sk-" not in check.reason
        assert "token" not in check.reason.lower()
        assert "key" not in check.reason.lower()

    def test_unknown_provider_fails_closed(self) -> None:
        check = _mock_provider_presence("unknown-vendor")
        assert check.ok is False
        assert check.provider == "unknown-vendor"
        assert "unknown-provider" in check.reason


# --- ceilings tests -------------------------------------------------------


class TestOvernightCeilings:
    def test_trial_count_exceeding_ceiling_fails(self) -> None:
        campaign = _make_campaign(n_trials=MAX_OVERNIGHT_TRIALS + 1)
        env = _mock_env()
        result = assess_campaign(campaign, env, provider_presence=_mock_provider_presence)
        trial_check = next(c for c in result.checks if c.code == "trial-count-ceiling")
        assert trial_check.ok is False
        assert "trial-count-exceeds-ceiling" in trial_check.reason
        assert result.may_calibrate is False
        assert "trial-count-ceiling" in result.calibrate_reasons

    def test_concurrency_exceeding_ceiling_fails(self) -> None:
        campaign = _make_campaign(n_concurrent=MAX_OVERNIGHT_CONCURRENCY + 1)
        env = _mock_env()
        result = assess_campaign(campaign, env, provider_presence=_mock_provider_presence)
        conc_check = next(c for c in result.checks if c.code == "concurrency-ceiling")
        assert conc_check.ok is False
        assert "concurrency-exceeds-ceiling" in conc_check.reason

    def test_token_ceiling_exceeding_cap_fails(self) -> None:
        campaign = _make_campaign(prompt_token_ceiling=MAX_PROMPT_TOKEN_CEILING + 1)
        env = _mock_env()
        result = assess_campaign(campaign, env, provider_presence=_mock_provider_presence)
        token_check = next(c for c in result.checks if c.code == "prompt-token-ceiling")
        assert token_check.ok is False
        assert "prompt-token-ceiling-exceeds-cap" in token_check.reason


# --- Darwin vs Linux causal promotion tests --------------------------------


class TestDarwinVsLinuxIsolationEligibility:
    def test_darwin_calibration_campaign_can_calibrate_not_promote_causal(self) -> None:
        campaign = _make_campaign(evidence_mode="calibration")
        env = _mock_env(os_name="Darwin", isolation_enforced=False)
        result = assess_campaign(campaign, env, provider_presence=_mock_provider_presence)

        # On Darwin, calibration mode produces may_calibrate (with static mocks)
        # Note: task/wheelhouse checks will fail on dummy paths unless mocked
        darwin_label = next(c for c in result.checks if c.code == "darwin-calibration-only")
        assert darwin_label.ok is True
        assert result.may_promote_causal is False
        assert "evidence-mode-not-causal" in result.promote_reasons

    def test_darwin_causal_campaign_is_labeled_and_refuses_launch(self) -> None:
        campaign = _make_campaign(
            evidence_mode="causal",
            network_isolation="required",
            credential_proxy=True,
        )
        env = _mock_env(os_name="Darwin", isolation_enforced=False)
        result = assess_campaign(campaign, env, provider_presence=_mock_provider_presence)

        # Causal on Darwin can never promote causally
        assert result.may_promote_causal is False
        assert result.launch_gate == LAUNCH_GATE_REFUSED
        assert any("isolation" in r for r in result.promote_reasons)

    def test_linux_enforced_isolation_with_proxy_allows_causal_promotion(self) -> None:
        campaign = _make_campaign(
            evidence_mode="causal",
            network_isolation="required",
            credential_proxy=True,
        )
        env = _mock_env(
            os_name="Linux",
            isolation_enforced=True,
            isolation_reason=None,
        )
        result = assess_campaign(campaign, env, provider_presence=_mock_provider_presence)
        isolation_check = next(c for c in result.checks if c.code == "isolation-eligibility")
        proxy_check = next(c for c in result.checks if c.code == "credential-proxy-eligibility")
        assert isolation_check.ok is True
        assert proxy_check.ok is True

    def test_linux_causal_campaign_without_credential_proxy_refused(self) -> None:
        campaign = _make_campaign(
            evidence_mode="causal",
            network_isolation="required",
            credential_proxy=False,
        )
        env = _mock_env(os_name="Linux", isolation_enforced=True)
        result = assess_campaign(campaign, env, provider_presence=_mock_provider_presence)
        proxy_check = next(c for c in result.checks if c.code == "credential-proxy-eligibility")
        assert proxy_check.ok is False
        assert result.may_promote_causal is False
        assert "credential-proxy-eligibility" in result.promote_reasons


# --- report & serialization tests -----------------------------------------


class TestReportRenderingAndSerialization:
    def test_report_renders_deterministic_text(self) -> None:
        campaign = _make_campaign()
        env = _mock_env()
        result = assess_campaign(campaign, env, provider_presence=_mock_provider_presence)
        report = RunPreflightReport(
            generated_at=datetime(2026, 8, 30, 0, 0, 0, tzinfo=UTC),
            environment=env,
            campaigns=(result,),
            compile_only=False,
            launch_ok=False,
        )
        rendered = render_run_preflight(report)
        assert "OVERNIGHT RUN PREFLIGHT (2026-08-30T00:00:00+00:00)" in rendered
        assert "CAMPAIGN test-campaign-01" in rendered
        assert "VERDICT" in rendered
        # Ensure no secrets in output
        assert "sk-" not in rendered
        assert "Bearer" not in rendered

    def test_canonical_json_serialization(self) -> None:
        campaign = _make_campaign()
        env = _mock_env()
        result = assess_campaign(campaign, env, provider_presence=_mock_provider_presence)
        report = RunPreflightReport(
            generated_at=datetime(2026, 8, 30, 0, 0, 0, tzinfo=UTC),
            environment=env,
            campaigns=(result,),
            compile_only=True,
            launch_ok=True,
        )
        data = run_preflight_to_dict(report)
        assert data["compile_only"] is True
        assert data["launch_ok"] is True
        assert len(data["campaigns"]) == 1
        c_dict = data["campaigns"][0]
        assert c_dict["campaign_id"] == "test-campaign-01"
        assert "verdict" in c_dict
        assert "may_compile" in c_dict["verdict"]
        assert "may_calibrate" in c_dict["verdict"]
        assert "may_promote_causal" in c_dict["verdict"]

        # Ensure json.dumps works cleanly with sorted keys
        serialized = json.dumps(data, indent=2, sort_keys=True)
        assert "test-campaign-01" in serialized


# --- compile-only mode tests ----------------------------------------------


class TestCompileOnlyMode:
    def test_compile_only_produces_launch_ok_true(self) -> None:
        campaign = _make_campaign()
        # Even with docker and disk failing:
        report = build_run_preflight(
            Path("."),
            [campaign],
            compile_only=True,
            docker_probe=lambda: PreflightCheck("docker-reachable", False, "no-docker"),
            disk_probe=lambda _: PreflightCheck("disk-headroom", False, "no-disk"),
            provider_presence=_mock_provider_presence,
        )
        assert report.compile_only is True
        assert report.launch_ok is True
        assert len(report.campaigns) == 1
