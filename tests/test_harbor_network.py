"""Focused tests for the execution-time Harbor network policy adapter.

These tests prove that the staging adapter preserves the canonical task package
and only changes ``network_mode`` entries to match the host's capabilities.
"""

import platform
import re
import tomllib

import pytest

from evallab.harbor_network import (
    HarborNetworkPolicy,
    NetworkAdaptation,
    _replace_in_section,
    _set_network_sentinel,
    adapt_task_toml_for_host,
    host_harbor_network_policy,
    with_agent_network_allowlist,
)


def _set_system(monkeypatch: pytest.MonkeyPatch, system: str) -> None:
    """Make ``host_harbor_network_policy`` return a policy for ``system``."""
    if system == "Linux":
        policy = HarborNetworkPolicy(
            network_mode="no-network",
            network_isolation_enforced=True,
            network_isolation_reason=None,
        )
    else:
        policy = HarborNetworkPolicy(
            network_mode="public",
            network_isolation_enforced=False,
            network_isolation_reason="darwin-docker-cannot-enforce-no-network",
        )
    monkeypatch.setattr(
        "evallab.harbor_network.host_harbor_network_policy",
        lambda: policy,
    )


_SEQGEN_CANONICAL = """\
schema_version = "1.4"
artifacts = ["/app/data/orders.jsonl", "/app/output/result.jsonl"]

[task]
name = "local-lab/seqgen-s7-000"
version = "1.0.0"
description = "Process structured JSONL orders"
keywords = ["jsonl", "synthetic"]

[[task.authors]]
name = "Eval Lab"
email = "eval-lab@example.invalid"

[metadata]
difficulty = "unknown"
category = "data-processing"
tags = ["deterministic", "synthetic", "seqgen"]

[verifier]
timeout_sec = 60.0
environment_mode = "separate"
collect = []

[verifier.environment]
network_mode = "no-network"

[agent]
timeout_sec = 120.0

[environment]
# Docker Desktop on macOS cannot enforce Harbor's no-network policy.
network_mode = "public"
build_timeout_sec = 300.0
os = "linux"
cpus = 1
memory_mb = 512
storage_mb = 2048
mcp_servers = []
"""


def _assert_non_network_fields_unchanged(original_text: str, adapted_text: str) -> None:
    """Every parsed field except ``network_mode`` is identical after adaptation."""
    original = tomllib.loads(original_text)
    adapted = tomllib.loads(adapted_text)
    assert _set_network_sentinel(original, "<adapted>") == _set_network_sentinel(
        adapted,
        "<adapted>",
    )


def test_linux_runs_no_network_verifier_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux executes the canonical no-network verifier policy directly."""
    _set_system(monkeypatch, "Linux")

    new_text, adaptation = adapt_task_toml_for_host(_SEQGEN_CANONICAL)

    assert new_text is _SEQGEN_CANONICAL
    assert adaptation is None
    _assert_non_network_fields_unchanged(_SEQGEN_CANONICAL, new_text)


def test_darwin_adapts_no_network_verifier_to_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Darwin rewrites the verifier baseline to public and records the change."""
    _set_system(monkeypatch, "Darwin")

    new_text, adaptation = adapt_task_toml_for_host(_SEQGEN_CANONICAL)

    assert new_text is not _SEQGEN_CANONICAL
    assert isinstance(adaptation, NetworkAdaptation)
    assert adaptation.requested_agent_network == "public"
    assert adaptation.effective_agent_network == "public"
    assert adaptation.requested_verifier_network == "no-network"
    assert adaptation.effective_verifier_network == "public"
    assert adaptation.network_isolation_enforced is False
    assert adaptation.network_isolation_reason == "darwin-docker-cannot-enforce-no-network"
    assert re.search(
        r'^\[verifier\.environment\]\nnetwork_mode = "public"',
        new_text,
        re.MULTILINE,
    )
    assert re.search(
        r'^\[environment\]\n.*network_mode = "public"',
        new_text,
        re.MULTILINE | re.DOTALL,
    )
    _assert_non_network_fields_unchanged(_SEQGEN_CANONICAL, new_text)


def test_darwin_adapts_fully_no_network_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A package that asks for no-network everywhere becomes public on Darwin."""
    _set_system(monkeypatch, "Darwin")

    canonical = _SEQGEN_CANONICAL.replace(
        '[environment]\n# Docker Desktop on macOS cannot enforce Harbor\'s no-network policy.\nnetwork_mode = "public"',
        '[environment]\nnetwork_mode = "no-network"',
    )
    new_text, adaptation = adapt_task_toml_for_host(canonical)

    assert new_text is not canonical
    assert adaptation is not None
    assert adaptation.requested_agent_network == "no-network"
    assert adaptation.effective_agent_network == "public"
    assert adaptation.requested_verifier_network == "no-network"
    assert adaptation.effective_verifier_network == "public"
    _assert_non_network_fields_unchanged(canonical, new_text)


def test_darwin_public_package_needs_no_adaptation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A canonical public package is already runnable on Darwin."""
    _set_system(monkeypatch, "Darwin")

    canonical = _SEQGEN_CANONICAL.replace(
        '[verifier.environment]\nnetwork_mode = "no-network"',
        '[verifier.environment]\nnetwork_mode = "public"',
    )
    new_text, adaptation = adapt_task_toml_for_host(canonical)

    assert new_text is canonical
    assert adaptation is None
    _assert_non_network_fields_unchanged(canonical, new_text)


def test_adaptation_preserves_unparsed_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter must not drop comments outside the changed lines."""
    _set_system(monkeypatch, "Darwin")

    new_text, _ = adapt_task_toml_for_host(_SEQGEN_CANONICAL)

    assert "# Docker Desktop on macOS cannot enforce Harbor" in new_text
    _assert_non_network_fields_unchanged(_SEQGEN_CANONICAL, new_text)


def test_adaptation_preserves_inline_comments_and_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The value is replaced; leading whitespace and inline comments stay."""
    _set_system(monkeypatch, "Darwin")

    canonical = _SEQGEN_CANONICAL.replace(
        '[verifier.environment]\nnetwork_mode = "no-network"',
        '[verifier.environment]\n  network_mode = "no-network"  # keep this comment',
    )
    new_text, _ = adapt_task_toml_for_host(canonical)

    assert '  network_mode = "public"  # keep this comment' in new_text
    _assert_non_network_fields_unchanged(canonical, new_text)


def test_adaptation_preserves_single_quoted_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The quoting style of the original value is preserved."""
    _set_system(monkeypatch, "Darwin")

    canonical = _SEQGEN_CANONICAL.replace(
        '[verifier.environment]\nnetwork_mode = "no-network"',
        "[verifier.environment]\nnetwork_mode = 'no-network'  # single-quoted",
    )
    new_text, _ = adapt_task_toml_for_host(canonical)

    assert "network_mode = 'public'  # single-quoted" in new_text
    _assert_non_network_fields_unchanged(canonical, new_text)


def test_darwin_adapts_verifier_phase_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``[verifier].network_mode`` phase override is also adapted."""
    _set_system(monkeypatch, "Darwin")
    canonical = _SEQGEN_CANONICAL.replace(
        '[verifier]\ntimeout_sec = 60.0\nenvironment_mode = "separate"\ncollect = []',
        '[verifier]\ntimeout_sec = 60.0\nenvironment_mode = "separate"\nnetwork_mode = "no-network"\ncollect = []',
    )
    new_text, adaptation = adapt_task_toml_for_host(canonical)

    assert adaptation is not None
    assert adaptation.requested_verifier_phase_network == "no-network"
    assert adaptation.effective_verifier_phase_network == "public"
    assert re.search(
        r'^\[verifier\]\n.*network_mode = "public"',
        new_text,
        re.MULTILINE | re.DOTALL,
    )
    _assert_non_network_fields_unchanged(canonical, new_text)


@pytest.mark.parametrize(
    ("system", "mode", "enforced", "reason"),
    [
        ("Linux", "no-network", True, None),
        ("Darwin", "public", False, "darwin-docker-cannot-enforce-no-network"),
        ("FreeBSD", "public", False, "freebsd-docker-cannot-enforce-no-network"),
        ("", "public", False, "-docker-cannot-enforce-no-network"),
    ],
)
def test_host_policy_is_derived_from_platform_and_fails_open_only_with_reason(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    mode: str,
    enforced: bool,
    reason: str | None,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: system)

    assert host_harbor_network_policy() == HarborNetworkPolicy(mode, enforced, reason)


_AGENT_BASE = '[task]\nname = "x"\n\n[agent]\ntimeout_sec = 1.0\n\n[environment]\nos = "linux"\n'


def test_allowlist_empty_hosts_returns_input_unchanged() -> None:
    assert with_agent_network_allowlist(_AGENT_BASE, ()) is _AGENT_BASE


def test_allowlist_inserts_policy_under_agent_header() -> None:
    updated = with_agent_network_allowlist(_AGENT_BASE, ("api.example.com", "b-1.test"))

    agent = tomllib.loads(updated)["agent"]
    assert agent["network_mode"] == "allowlist"
    assert agent["allowed_hosts"] == ["api.example.com", "b-1.test"]
    assert agent["timeout_sec"] == 1.0
    assert tomllib.loads(updated)["environment"] == {"os": "linux"}


@pytest.mark.parametrize(
    "host", ["bad host", "evil.com;rm", "a/b", "ü.example", "", "host:443", "*.example.com"]
)
def test_allowlist_refuses_invalid_hostname(host: str) -> None:
    with pytest.raises(ValueError, match="invalid hostname"):
        with_agent_network_allowlist(_AGENT_BASE, ("ok.example.com", host))


@pytest.mark.parametrize("text", ['[task]\nname = "x"\n', 'agent = "scalar"\n'])
def test_allowlist_refuses_missing_agent_table(text: str) -> None:
    with pytest.raises(ValueError, match=r"\[agent\] not found"):
        with_agent_network_allowlist(text, ("a.example.com",))


def test_allowlist_refuses_agent_table_without_own_header() -> None:
    """An inline-table agent parses as a table but has no ``[agent]`` header to extend."""
    with pytest.raises(ValueError, match=r"\[agent\] not found"):
        with_agent_network_allowlist("agent = { timeout_sec = 1.0 }\n", ("a.example.com",))


def test_allowlist_identical_existing_policy_is_returned_unchanged() -> None:
    declared = _AGENT_BASE.replace(
        "[agent]\n", '[agent]\nnetwork_mode = "allowlist"\nallowed_hosts = ["a.example.com"]\n'
    )

    assert with_agent_network_allowlist(declared, ("a.example.com",)) is declared


@pytest.mark.parametrize(
    "extra",
    [
        'network_mode = "public"\n',
        'network_mode = "allowlist"\n',
        'allowed_hosts = ["a.example.com"]\n',
        'network_mode = "allowlist"\nallowed_hosts = ["other.example.com"]\n',
        'network_mode = "no-network"\nallowed_hosts = ["a.example.com"]\n',
    ],
)
def test_allowlist_refuses_conflicting_existing_policy(extra: str) -> None:
    declared = _AGENT_BASE.replace("[agent]\n", "[agent]\n" + extra)

    with pytest.raises(ValueError, match="different agent network policy"):
        with_agent_network_allowlist(declared, ("a.example.com",))


def test_allowlist_handles_agent_header_at_end_of_file() -> None:
    updated = with_agent_network_allowlist("[agent]", ("a.example.com",))

    assert tomllib.loads(updated) == {
        "agent": {"network_mode": "allowlist", "allowed_hosts": ["a.example.com"]}
    }


def _loads_with_override(monkeypatch: pytest.MonkeyPatch, override: object) -> None:
    """Return ``override`` from the second ``tomllib.loads`` (the post-insertion parse)."""
    real = tomllib.loads
    calls = []

    def loads(text: str) -> object:
        calls.append(text)
        return real(text) if len(calls) == 1 else override

    monkeypatch.setattr("evallab.harbor_network.tomllib.loads", loads)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"agent": "scalar"}, "did not parse"),
        ({"agent": {"allowed_hosts": ["a.example.com"]}}, "network_mode was not set"),
        (
            {"agent": {"network_mode": "allowlist", "allowed_hosts": []}},
            "allowed_hosts was not set",
        ),
    ],
)
def test_allowlist_refuses_when_inserted_policy_does_not_round_trip(
    monkeypatch: pytest.MonkeyPatch, override: dict, message: str
) -> None:
    _loads_with_override(monkeypatch, override)

    with pytest.raises(ValueError, match=message):
        with_agent_network_allowlist(_AGENT_BASE, ("a.example.com",))


def test_replace_in_section_replaces_only_requested_section_key() -> None:
    text = '[a]\nk = "old"  # c\n[b]\nk = "other"\n'

    assert _replace_in_section(text, "a", "k", "new") == '[a]\nk = "new"  # c\n[b]\nk = "other"\n'


def test_replace_in_section_refuses_missing_section() -> None:
    with pytest.raises(ValueError, match=r"\[missing\] not found"):
        _replace_in_section('[a]\nk = "v"\n', "missing", "k", "new")


def test_replace_in_section_refuses_ambiguous_key() -> None:
    with pytest.raises(ValueError, match="ambiguous 'k'"):
        _replace_in_section('[a]\nk = "1"\nk = "2"\n', "a", "k", "new")


@pytest.mark.parametrize("body", ['other = "v"\n', "k = 3\n", ""])
def test_replace_in_section_refuses_missing_or_unquoted_key(body: str) -> None:
    with pytest.raises(ValueError, match="'k' not found in"):
        _replace_in_section(f'[a]\n{body}[b]\nk = "v"\n', "a", "k", "new")


_DARWIN = HarborNetworkPolicy("public", False, "darwin-docker-cannot-enforce-no-network")


def test_adapt_refuses_when_rewrite_changes_non_network_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = _replace_in_section

    def tampering(text: str, section: str, key: str, value: str) -> str:
        return real(text, section, key, value).replace('os = "linux"', 'os = "windows"')

    monkeypatch.setattr("evallab.harbor_network._replace_in_section", tampering)

    with pytest.raises(ValueError, match="changed non-network parsed fields"):
        adapt_task_toml_for_host(_SEQGEN_CANONICAL, host_policy=_DARWIN)


_NO_NETWORK_EVERYWHERE = _SEQGEN_CANONICAL.replace(
    '[environment]\n# Docker Desktop on macOS cannot enforce Harbor\'s no-network policy.\nnetwork_mode = "public"',
    '[environment]\nnetwork_mode = "no-network"',
).replace(
    '[verifier]\ntimeout_sec = 60.0\nenvironment_mode = "separate"\ncollect = []',
    '[verifier]\ntimeout_sec = 60.0\nenvironment_mode = "separate"\nnetwork_mode = "no-network"\ncollect = []',
)


def _skip_rewrite_of(monkeypatch: pytest.MonkeyPatch, skipped_section: str) -> None:
    """Make ``_replace_in_section`` a no-op for one section; others are rewritten."""
    real = _replace_in_section

    def replace(text: str, section: str, key: str, value: str) -> str:
        if section == skipped_section:
            return text
        return real(text, section, key, value)

    monkeypatch.setattr("evallab.harbor_network._replace_in_section", replace)


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("environment", r"environment\.network_mode not set"),
        ("verifier.environment", r"verifier\.environment\.network_mode not set"),
        ("verifier", r"verifier\.network_mode not set"),
    ],
)
def test_adapt_refuses_when_effective_value_did_not_land(
    monkeypatch: pytest.MonkeyPatch, section: str, message: str
) -> None:
    assert _NO_NETWORK_EVERYWHERE.count("no-network") == 3
    _skip_rewrite_of(monkeypatch, section)

    with pytest.raises(ValueError, match=message):
        adapt_task_toml_for_host(_NO_NETWORK_EVERYWHERE, host_policy=_DARWIN)


def test_adapt_full_rewrite_lands_all_three_values() -> None:
    new_text, adaptation = adapt_task_toml_for_host(_NO_NETWORK_EVERYWHERE, host_policy=_DARWIN)

    config = tomllib.loads(new_text)
    assert config["environment"]["network_mode"] == "public"
    assert config["verifier"]["environment"]["network_mode"] == "public"
    assert config["verifier"]["network_mode"] == "public"
    assert adaptation is not None
    assert adaptation.network_isolation_enforced is False
