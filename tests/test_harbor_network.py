"""Focused tests for the execution-time Harbor network policy adapter.

These tests prove that the staging adapter preserves the canonical task package
and only changes ``network_mode`` entries to match the host's capabilities.
"""
import re
import tomllib

import pytest

from evallab.harbor_network import (
    HarborNetworkPolicy,
    NetworkAdaptation,
    _set_network_sentinel,
    adapt_task_toml_for_host,
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
