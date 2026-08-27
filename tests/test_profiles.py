"""AgentProfile contract tests (M003). Deterministic per agents/CHECKS.md:
injected home/clock/security-runner/environment; zero real credentials,
keychain, network, or wall-clock dependence.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.profiles import (
    DECLARED_UNAVAILABLE,
    AgentProfile,
    AuthFileProbe,
    CliSessionProbe,
    DeclaredUnavailableProbe,
    EnvironmentPresenceProbe,
    KeychainProbe,
    PreflightDecision,
    builtin_profiles,
    default_probe_for,
    preflight,
    scrub_environment,
    validate_model_pin,
)
from evallab.runner import _SUBSCRIPTION_ENVIRONMENT_KEYS, subscription_environment

FROZEN_NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
FAKE_SECRET = "sk-FAKE-SECRET-VALUE-000"


def codex_profile() -> AgentProfile:
    return builtin_profiles()["codex-gpt-5.6-terra"]


# ---- identity and digests ---------------------------------------------------


def test_digest_is_stable_and_deterministic():
    a, b = codex_profile(), codex_profile()
    assert a.digest == b.digest
    assert a.digest.startswith("sha256:")
    assert a.canonical_json() == b.canonical_json()
    # canonical form is key-sorted: byte-identical through a JSON round trip
    reloaded = json.dumps(json.loads(a.canonical_json()), sort_keys=True, separators=(",", ":"))
    assert reloaded == a.canonical_json()


def test_digest_changes_when_identity_changes():
    base = codex_profile()
    changed = base.model_copy(update={"model": "gpt-5.6-sol"})
    assert changed.digest != base.digest


def test_profiles_are_immutable():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        codex_profile().model = "other"  # type: ignore[misc]


def test_control_profiles_take_no_model_or_credential():
    with pytest.raises(ValueError, match="no credential and no model"):
        AgentProfile(
            profile_id="bad-oracle", adapter="oracle", auth_mode="none", model="gpt-5.6-terra"
        )
    oracle = builtin_profiles()["oracle"]
    assert oracle.auth_mode == "none" and oracle.model is None


def test_billable_profiles_require_pin_and_secret_source():
    with pytest.raises(ValueError, match="exact model pin"):
        AgentProfile(
            profile_id="bad",
            adapter="codex",
            auth_mode="subscription-auth-file",
            secret_source="file:.codex/auth.json",
        )
    with pytest.raises(ValueError, match="secret source"):
        AgentProfile(
            profile_id="bad2",
            adapter="codex",
            model="gpt-5.6-terra",
            auth_mode="subscription-auth-file",
        )


def test_api_key_shaped_names_are_rejected_everywhere():
    with pytest.raises(ValueError, match="subscriptions-only"):
        AgentProfile(
            profile_id="bad",
            adapter="codex",
            model="m",
            auth_mode="subscription-auth-file",
            secret_source="file:.codex/auth.json",
            required_files=("OPENAI_API_KEY",),
        )


def test_deepseek_profile_allows_only_admitted_environment_names(tmp_path: Path) -> None:
    profile = builtin_profiles()["mini-swe-agent-deepseek-v4-flash"]
    assert profile.adapter == "mini-swe-agent"
    assert profile.model == "deepseek/deepseek-v4-flash"
    assert profile.auth_mode == "api-key-environment"

    probe = default_probe_for(
        profile,
        home=tmp_path,
        security_runner=lambda argv: 1,
        keychain_account="nobody",
        environment={"MSWEA_API_KEY": FAKE_SECRET},
    )
    assert isinstance(probe, EnvironmentPresenceProbe)
    decision = preflight(profile, probe)
    assert decision.proceed
    assert FAKE_SECRET not in repr(decision)

    with pytest.raises(ValueError, match="only the admitted DeepSeek"):
        AgentProfile(
            profile_id="bad-env-profile",
            adapter="mini-swe-agent",
            model="deepseek/deepseek-v4-flash",
            auth_mode="api-key-environment",
            secret_source="env:OPENAI_API_KEY",
        )


# ---- model pin discipline ---------------------------------------------------


def test_model_mismatch_is_refused():
    with pytest.raises(ValueError, match="change profiles, not pins"):
        validate_model_pin(codex_profile(), "gpt-5.6-sol")
    validate_model_pin(codex_profile(), "gpt-5.6-terra")
    validate_model_pin(codex_profile(), None)


# ---- probes: injected seams, no secrets -------------------------------------


def test_keychain_probe_uses_exit_status_only():
    calls: list[list[str]] = []

    def fake_security(args: list[str]) -> int:
        calls.append(args)
        return 0

    probe = KeychainProbe(security_runner=fake_security, service="svc", account="acct")
    result = probe(builtin_profiles()["claude-code-fable-5"])
    assert result.ok and result.reason is None
    # existence flags only; never `-w` (print secret)
    assert calls and "-w" not in calls[0]


def test_keychain_probe_missing_item_gives_reason_not_secret():
    probe = KeychainProbe(security_runner=lambda a: 44, service="svc", account="acct")
    result = probe(builtin_profiles()["claude-code-fable-5"])
    assert not result.ok
    assert "absent" in (result.reason or "")


def test_keychain_probe_timeout_fails_closed():
    def timed_out(args: list[str]) -> int:
        raise subprocess.TimeoutExpired(cmd=args, timeout=10)

    probe = KeychainProbe(security_runner=timed_out, service="svc", account="acct")
    result = probe(builtin_profiles()["claude-code-fable-5"])
    assert not result.ok
    assert result.reason == "keychain probe failed: TimeoutExpired"


def test_auth_file_missing(tmp_path: Path):
    probe = AuthFileProbe(home=tmp_path, relative_path=".codex/auth.json", clock=lambda: FROZEN_NOW)
    result = probe(codex_profile())
    assert not result.ok and "missing" in (result.reason or "")


def test_auth_file_expired_via_injected_clock(tmp_path: Path):
    auth = tmp_path / ".codex/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps({"tokens": {"access_token": FAKE_SECRET, "expires_at": "2026-08-01T00:00:00Z"}})
    )
    probe = AuthFileProbe(home=tmp_path, relative_path=".codex/auth.json", clock=lambda: FROZEN_NOW)
    result = probe(codex_profile())
    assert not result.ok
    assert result.reason == "auth file expired"
    assert result.expires_at == datetime(2026, 8, 1, tzinfo=UTC)


def test_auth_file_valid_future_expiry(tmp_path: Path):
    auth = tmp_path / ".codex/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps({"tokens": {"access_token": FAKE_SECRET, "expires_at": "2026-12-01T00:00:00Z"}})
    )
    probe = AuthFileProbe(home=tmp_path, relative_path=".codex/auth.json", clock=lambda: FROZEN_NOW)
    result = probe(codex_profile())
    assert result.ok and result.expires_at is not None


def test_probe_results_never_carry_secret_material(tmp_path: Path):
    auth = tmp_path / ".codex/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"access_token": FAKE_SECRET, "expires_at": 1790000000}))
    probe = AuthFileProbe(home=tmp_path, relative_path=".codex/auth.json", clock=lambda: FROZEN_NOW)
    result = probe(codex_profile())
    assert FAKE_SECRET not in repr(result)
    # the profile's serialized identity never contains secrets either
    assert FAKE_SECRET not in codex_profile().canonical_json()


# ---- preflight: fail closed, never reward zero ------------------------------


def test_preflight_controls_pass_without_probe():
    decision = preflight(builtin_profiles()["oracle"], probe=None)
    assert decision.proceed


def test_preflight_billable_without_probe_fails_closed():
    decision = preflight(codex_profile(), probe=None)
    assert not decision.proceed
    assert "fail closed" in (decision.reason or "")


def test_preflight_auth_failure_stops_with_reason_and_no_reward_field():
    probe = DeclaredUnavailableProbe(reason="credential store empty")
    decision = preflight(codex_profile(), probe=probe)
    assert not decision.proceed
    assert decision.reason == "credential store empty"
    # A preflight stop is structurally incapable of becoming a trial score:
    # the decision carries no reward-like field at all.
    assert not any("reward" in f for f in PreflightDecision.__dataclass_fields__)


def test_gemini_and_grok_remain_declared_unavailable(tmp_path: Path):
    registry = builtin_profiles()
    assert frozenset({"gemini-cli-declared", "grok-cli-declared"}) == DECLARED_UNAVAILABLE
    for profile_id in sorted(DECLARED_UNAVAILABLE):
        probe = default_probe_for(
            registry[profile_id],
            home=tmp_path,
            security_runner=lambda a: 0,
            keychain_account="acct",
            clock=lambda: FROZEN_NOW,
        )
        decision = preflight(registry[profile_id], probe)
        assert not decision.proceed
        assert "not independently proven" in (decision.reason or "")


def test_claude_uses_real_keychain_probe_not_declared_block(tmp_path: Path):
    registry = builtin_profiles()
    probe = default_probe_for(
        registry["claude-code-fable-5"],
        home=tmp_path,
        security_runner=lambda a: 0,
        keychain_account="acct",
        clock=lambda: FROZEN_NOW,
    )
    assert isinstance(probe, KeychainProbe)
    assert preflight(registry["claude-code-fable-5"], probe).proceed


# ---- environment allowlist --------------------------------------------------


def test_subscription_environment_never_forwards_api_keys():
    poisoned = {
        "ANTHROPIC_API_KEY": FAKE_SECRET,
        "OPENAI_API_KEY": FAKE_SECRET,
        "XAI_API_KEY": FAKE_SECRET,
        "SOME_ACCESS_KEY": FAKE_SECRET,
        "HOME": "/home/x",
        "PATH": "/usr/bin",
    }
    env = subscription_environment(poisoned)
    assert FAKE_SECRET not in json.dumps(env)
    assert env["HOME"] == "/home/x"
    assert env["AGY_FORCE_AUTH_JSON"] == "1"
    assert env["CODEX_FORCE_AUTH_JSON"] == "1"
    assert env["CLAUDE_FORCE_OAUTH"] == "1"


def test_subscription_environment_forwards_agy_oauth_token_but_not_cursor_key():
    """The AGY lane authenticates with an OAuth token file, so it may cross into
    Harbor. Cursor's adapter wants an API key, which this lab forbids: the key
    must be dropped even when the caller has it set."""
    source = {
        "AGY_FORCE_AUTH_JSON": "1",
        "AGY_AUTH_JSON_PATH": "/tokens/agy-oauth-token",
        "CURSOR_API_KEY": FAKE_SECRET,
        "HOME": "/home/x",
    }
    env = subscription_environment(source)
    assert env["AGY_FORCE_AUTH_JSON"] == "1"
    assert env["AGY_AUTH_JSON_PATH"] == "/tokens/agy-oauth-token"
    assert "CURSOR_API_KEY" not in env
    assert FAKE_SECRET not in json.dumps(env)


def test_scrub_environment_drops_key_shaped_names_even_if_allowlisted():
    allow = frozenset({"HOME", "SNEAKY_API_KEY"})
    clean = scrub_environment({"HOME": "/h", "SNEAKY_API_KEY": FAKE_SECRET}, allow)
    assert clean == {"HOME": "/h"}


def test_runner_allowlist_itself_contains_no_key_shaped_names():
    for key in _SUBSCRIPTION_ENVIRONMENT_KEYS:
        assert "API_KEY" not in key and "SECRET" not in key


# ---- deterministic serialization -------------------------------------------


def test_registry_serialization_is_deterministic():
    first = {pid: p.canonical_json() for pid, p in builtin_profiles().items()}
    second = {pid: p.canonical_json() for pid, p in builtin_profiles().items()}
    assert first == second
    digests = {pid: p.digest for pid, p in builtin_profiles().items()}
    assert len(set(digests.values())) == len(digests)  # all distinct


# --- Cursor lane (subscription-cli-session auth) ---------------------------------


def _fake_cli(status: int, stdout: str):
    def runner(argv):
        return status, stdout

    return runner


def test_cursor_default_profile_pins_grok_4_6_high() -> None:
    """Peter's stated default is grok-4.6 **high**, explicitly not the -fast variant.

    Pinned here because several profiles share the cursor-cli adapter, so nothing
    but an assertion stops iteration order from choosing a different default.
    """
    from evallab.credentials import DEFAULT_AGENT_MODELS, DEFAULT_PROFILE_FOR_ADAPTER

    assert DEFAULT_PROFILE_FOR_ADAPTER["cursor-cli"] == "cursor-grok-4.6-high"
    assert DEFAULT_AGENT_MODELS["cursor-cli"] == "cursor-grok-4.6-high"
    assert not DEFAULT_AGENT_MODELS["cursor-cli"].endswith("-fast")


def test_cursor_profiles_are_pinned_and_cli_session_authed() -> None:
    profiles = builtin_profiles()
    cursor = [p for p in profiles.values() if p.adapter == "cursor-cli"]
    assert cursor, "the cursor lane must be present in the profile registry"
    for profile in cursor:
        assert profile.auth_mode == "subscription-cli-session"
        assert profile.secret_source == "cli:cursor-agent status"
        assert profile.model and profile.model != "auto", "models must be pinned exactly"


def test_cli_session_probe_reports_ok_on_logged_in_marker() -> None:
    profile = builtin_profiles()["cursor-grok-4.6-high"]
    probe = CliSessionProbe(
        argv=("cursor-agent", "status"),
        expect="logged in",
        runner=_fake_cli(0, "\u2713 Logged in as someone@example.com"),
    )
    assert probe(profile).ok is True


def test_cli_session_probe_fails_when_cli_reports_no_session() -> None:
    profile = builtin_profiles()["cursor-grok-4.6-high"]
    probe = CliSessionProbe(
        argv=("cursor-agent", "status"), expect="logged in", runner=_fake_cli(1, "")
    )
    result = probe(profile)
    assert result.ok is False
    assert "no session" in (result.reason or "")


def test_cli_session_probe_fails_when_marker_absent_despite_exit_zero() -> None:
    """Exit zero is not enough: a CLI can succeed while reporting 'not logged in'."""
    profile = builtin_profiles()["cursor-grok-4.6-high"]
    probe = CliSessionProbe(
        argv=("cursor-agent", "status"),
        expect="logged in",
        runner=_fake_cli(0, "You are not signed in."),
    )
    result = probe(profile)
    assert result.ok is False
    assert "did not match" in (result.reason or "")


def test_cli_session_auth_requires_a_cli_secret_source() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentProfile(
            profile_id="cursor-bad-source",
            adapter="cursor-cli",
            model="cursor-grok-4.6-high",
            auth_mode="subscription-cli-session",
            secret_source="file:.cursor/cli-config.json",
        )


def test_default_probe_for_cursor_is_a_cli_session_probe(tmp_path: Path) -> None:
    profile = builtin_profiles()["cursor-grok-4.6-high"]
    probe = default_probe_for(
        profile, home=tmp_path, security_runner=lambda argv: 1, keychain_account="nobody"
    )
    assert isinstance(probe, CliSessionProbe)
    assert probe.argv == ("cursor-agent", "status")


# --- Antigravity lane (subscription-cli-session auth) -------------------------


def test_antigravity_default_profile_pins_gemini_3_7_flash_high() -> None:
    """Peter's stated default is Gemini 3.7 Flash high.

    Pinned explicitly in DEFAULT_PROFILE_FOR_ADAPTER because several profiles share
    the antigravity-cli adapter.
    """
    from evallab.credentials import DEFAULT_AGENT_MODELS, DEFAULT_PROFILE_FOR_ADAPTER

    assert DEFAULT_PROFILE_FOR_ADAPTER["antigravity-cli"] == "antigravity-gemini-3.7-flash-high"
    assert DEFAULT_AGENT_MODELS["antigravity-cli"] == "gemini-3.7-flash-high"


def test_antigravity_profiles_are_pinned_and_cli_session_authed() -> None:
    profiles = builtin_profiles()
    antigravity = [p for p in profiles.values() if p.adapter == "antigravity-cli"]
    assert antigravity, "the antigravity lane must be present in the profile registry"
    for profile in antigravity:
        assert profile.auth_mode == "subscription-cli-session"
        assert profile.secret_source == "cli:agy models"
        assert profile.model and profile.model != "auto", "models must be pinned exactly"


def test_antigravity_cli_session_probe_reports_ok_on_gemini_marker() -> None:
    profile = builtin_profiles()["antigravity-gemini-3.7-flash-high"]
    probe = CliSessionProbe(
        argv=("agy", "models"),
        expect="gemini",
        runner=_fake_cli(
            0, "Fetching available models...\ngemini-3.7-flash-high\tGemini 3.7 Flash (High)"
        ),
    )
    assert probe(profile).ok is True


def test_antigravity_cli_session_probe_fails_when_cli_reports_no_session() -> None:
    profile = builtin_profiles()["antigravity-gemini-3.7-flash-high"]
    probe = CliSessionProbe(
        argv=("agy", "models"),
        expect="gemini",
        runner=_fake_cli(1, "Error: Please sign in to view available models."),
    )
    result = probe(profile)
    assert result.ok is False
    assert "no session" in (result.reason or "")


def test_antigravity_cli_session_probe_fails_when_marker_absent_despite_exit_zero() -> None:
    """Exit zero is not enough: a CLI can succeed with an unexpected message."""
    profile = builtin_profiles()["antigravity-gemini-3.7-flash-high"]
    probe = CliSessionProbe(
        argv=("agy", "models"),
        expect="gemini",
        runner=_fake_cli(0, "Fetching available models...\nNo models found."),
    )
    result = probe(profile)
    assert result.ok is False
    assert "did not match" in (result.reason or "")


def test_default_probe_for_antigravity_is_a_cli_session_probe(tmp_path: Path) -> None:
    profile = builtin_profiles()["antigravity-gemini-3.7-flash-high"]
    probe = default_probe_for(
        profile, home=tmp_path, security_runner=lambda argv: 1, keychain_account="nobody"
    )
    assert isinstance(probe, CliSessionProbe)
    assert probe.argv == ("agy", "models")
    assert probe.expect == "gemini"
