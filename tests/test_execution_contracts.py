"""Focused unit tests for extracted execution contracts and DTOs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evallab.execution_contracts import (
    MAX_TRIAL_TIMEOUT_SECONDS,
    DispatchCapacity,
    PaidRunAuthorization,
    RunRequest,
    new_ulid,
    redact_environment,
    subscription_environment,
    transient_provider_exception,
    transient_provider_reason,
    validate_request,
)
from evallab.harbor_network import (
    HarborNetworkPolicy,
    adapt_task_toml_for_host,
    with_agent_network_allowlist,
)


def _task_dir(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    task.mkdir(parents=True, exist_ok=True)
    (task / "task.toml").write_text("[environment]\nnetwork_mode = 'no-network'\n")
    return task


def test_run_request_immutability_and_job_timeout(tmp_path: Path) -> None:
    """RunRequest is a frozen dataclass with conservative aggregate timeout calculation."""
    task = _task_dir(tmp_path)
    req = RunRequest(
        task=task,
        agent="oracle",
        name="test-run",
        jobs_dir=tmp_path / "jobs",
        concurrency=2,
        attempts=3,
        timeout_seconds=300,
    )
    assert req.job_timeout_seconds == 900
    with pytest.raises(AttributeError):
        req.name = "new-name"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda r, p: RunRequest(
                task=p / "nonexistent", agent="oracle", name="valid-name", jobs_dir=p / "jobs"
            ),
            "Task directory does not exist",
        ),
        (
            lambda r, p: RunRequest(
                task=p / "notaskdir", agent="oracle", name="valid-name", jobs_dir=p / "jobs"
            ),
            "Task directory does not exist",
        ),
        (
            lambda r, p: RunRequest(
                task=_task_dir(p), agent="oracle", name="Bad_Name", jobs_dir=p / "jobs"
            ),
            "Job names must be 3-80 lowercase",
        ),
        (
            lambda r, p: RunRequest(
                task=_task_dir(p), agent="oracle", name="sh", jobs_dir=p / "jobs"
            ),
            "Job names must be 3-80 lowercase",
        ),
        (
            lambda r, p: RunRequest(
                task=_task_dir(p),
                agent="oracle",
                name="valid-name",
                jobs_dir=p / "jobs",
                concurrency=0,
            ),
            "Concurrency and attempts must be positive",
        ),
        (
            lambda r, p: RunRequest(
                task=_task_dir(p),
                agent="oracle",
                name="valid-name",
                jobs_dir=p / "jobs",
                attempts=0,
            ),
            "Concurrency and attempts must be positive",
        ),
        (
            lambda r, p: RunRequest(
                task=_task_dir(p),
                agent="oracle",
                name="valid-name",
                jobs_dir=p / "jobs",
                timeout_seconds=0,
            ),
            "timeout must be 1-21600",
        ),
        (
            lambda r, p: RunRequest(
                task=_task_dir(p),
                agent="oracle",
                name="valid-name",
                jobs_dir=p / "jobs",
                timeout_seconds=MAX_TRIAL_TIMEOUT_SECONDS + 1,
            ),
            "timeout must be 1-21600",
        ),
        (
            lambda r, p: RunRequest(
                task=_task_dir(p),
                agent="codex",
                name="valid-name",
                jobs_dir=p / "jobs",
                allow_billable=False,
            ),
            "Pass --allow-billable",
        ),
        (
            lambda r, p: RunRequest(
                task=_task_dir(p),
                agent="oracle",
                name="valid-name",
                jobs_dir=p / "jobs",
                model="some-model",
                allow_billable=True,
            ),
            "control does not accept a model",
        ),
        (
            lambda r, p: RunRequest(
                task=_task_dir(p),
                agent="codex",
                name="valid-name",
                jobs_dir=p / "jobs",
                model="some-model",
                allow_billable=False,
            ),
            "Pass --allow-billable",
        ),
    ],
)
def test_validate_request_invariants(tmp_path: Path, mutate: Any, match: str) -> None:
    """validate_request enforces directory, name, timeout, and billable bounds."""
    req = mutate(None, tmp_path)
    with pytest.raises(ValueError, match=match):
        validate_request(req)


def test_paid_run_authorization_dataclass() -> None:
    """PaidRunAuthorization is a frozen dataclass holding human grant metadata."""
    now = datetime.now(UTC)
    auth = PaidRunAuthorization(
        spec_id="01TESTSPEC0000000000000000",
        actor="peter",
        authorized_at=now,
        quota_override=True,
    )
    assert auth.spec_id == "01TESTSPEC0000000000000000"
    assert auth.actor == "peter"
    assert auth.authorized_at == now
    assert auth.quota_override is True
    with pytest.raises(AttributeError):
        auth.actor = "someone_else"  # type: ignore[misc]


def test_dispatch_capacity_validation() -> None:
    """DispatchCapacity validates positive limit values and rejects non-positive values."""
    cap = DispatchCapacity(
        max_specs_per_tick=5,
        max_active_trials=10,
        per_agent_active_trials={"codex": 2, "oracle": 4},
    )
    assert cap.max_specs_per_tick == 5
    assert cap.max_active_trials == 10

    with pytest.raises(ValueError, match="dispatch capacity values must be positive"):
        DispatchCapacity(max_specs_per_tick=0)

    with pytest.raises(ValueError, match="dispatch capacity values must be positive"):
        DispatchCapacity(per_agent_active_trials={"codex": 0})


def test_subscription_environment_forwarding_and_forcing() -> None:
    """subscription_environment forwards allowlisted keys and injects non-secret routing switches."""
    mock_env = {
        "HOME": "/home/user",
        "PATH": "/bin:/usr/bin",
        "SECRET_API_KEY": "sk-secret-123",
        "OPENAI_API_KEY": "sk-openai-456",
        "ANTHROPIC_API_KEY": "sk-ant-789",
    }
    sanitized = subscription_environment(mock_env)
    assert sanitized["HOME"] == "/home/user"
    assert sanitized["PATH"] == "/bin:/usr/bin"
    assert "SECRET_API_KEY" not in sanitized
    assert "OPENAI_API_KEY" not in sanitized
    assert "ANTHROPIC_API_KEY" not in sanitized
    assert sanitized["AGY_FORCE_AUTH_JSON"] == "1"
    assert sanitized["CODEX_FORCE_AUTH_JSON"] == "1"
    assert sanitized["CLAUDE_FORCE_OAUTH"] == "1"
    assert sanitized["REWARDKIT_FORCE_OAUTH"] == "1"


def test_deepseek_credentials_are_opt_in_and_log_redacted() -> None:
    secret = "fresh-secret-never-log"
    source = {
        "HOME": "/home/user",
        "MSWEA_API_KEY": secret,
        "OPENAI_API_KEY": "never-forward",
        "EVALLAB_DEEPSEEK_SECRET_FILE": "/tmp/evallab-deepseek.key",
        "EVALLAB_DEEPSEEK_PROXY_SCRIPT": "/tmp/deepseek_secret_proxy.py",
    }

    assert "MSWEA_API_KEY" not in subscription_environment(source)
    admitted = subscription_environment(source, include_deepseek_credentials=True)
    assert "DEEPSEEK_API_KEY" not in admitted
    assert "MSWEA_API_KEY" not in admitted
    assert admitted["EVALLAB_DEEPSEEK_SECRET_FILE"] == "/tmp/evallab-deepseek.key"
    assert admitted["EVALLAB_DEEPSEEK_PROXY_SCRIPT"].endswith("deepseek_secret_proxy.py")
    assert "OPENAI_API_KEY" not in admitted
    assert secret not in admitted.values()

    redacted = redact_environment({"DEEPSEEK_API_KEY": secret, "HOME": "/home/user"})
    assert redacted["DEEPSEEK_API_KEY"] == "<redacted>"
    assert secret not in repr(redacted)


def test_new_ulid_format_and_monotonicity() -> None:
    """new_ulid generates 26-character sortable Crockford Base32 identifiers."""
    u1 = new_ulid(timestamp_ms=1000, randomness=1)
    u2 = new_ulid(timestamp_ms=2000, randomness=1)
    assert len(u1) == 26
    assert len(u2) == 26
    assert u1 < u2


def test_transient_provider_classification() -> None:
    """transient_provider_reason and transient_provider_exception classify 429 and 5xx errors."""
    assert (
        transient_provider_reason("HTTP 429: Too Many Requests")
        == "transient_harness:provider_http_429"
    )
    assert (
        transient_provider_reason("HTTP 502 Bad Gateway from upstream provider")
        == "transient_harness:provider_http_5xx"
    )
    assert transient_provider_reason("syntax error in python file") is None

    res_429 = {
        "exception_info": {"exception_type": "ApiRateLimitError", "message": "rate limit reached"}
    }
    assert transient_provider_exception(res_429) == "transient_harness:provider_http_429"

    res_500 = {
        "exception_info": {"exception_type": "ApiInternalServerError", "message": "server error"}
    }
    assert transient_provider_exception(res_500) == "transient_harness:provider_http_5xx"


def test_harbor_network_adaptation_typed_policy_input() -> None:
    """adapt_task_toml_for_host accepts explicit host_policy and adapts appropriately."""
    task_toml = (
        "[environment]\n"
        "network_mode = 'no-network'\n"
        "\n"
        "[verifier.environment]\n"
        "network_mode = 'no-network'\n"
    )

    # 1. Linux policy -> no-network preserved with isolation enforced
    linux_policy = HarborNetworkPolicy(
        network_mode="no-network",
        network_isolation_enforced=True,
        network_isolation_reason=None,
    )
    text_linux, adapt_linux = adapt_task_toml_for_host(task_toml, host_policy=linux_policy)
    assert text_linux == task_toml
    assert adapt_linux is None

    # 2. Non-Linux (e.g. Darwin) -> adapted to public with reason documented
    darwin_policy = HarborNetworkPolicy(
        network_mode="public",
        network_isolation_enforced=False,
        network_isolation_reason="darwin-docker-cannot-enforce-no-network",
    )
    text_darwin, adapt_darwin = adapt_task_toml_for_host(task_toml, host_policy=darwin_policy)
    assert adapt_darwin is not None
    assert adapt_darwin.effective_agent_network == "public"
    assert adapt_darwin.network_isolation_enforced is False
    assert adapt_darwin.network_isolation_reason == "darwin-docker-cannot-enforce-no-network"
    assert "network_mode = 'public'" in text_darwin


def test_agent_allowlist_is_execution_only_and_exact() -> None:
    task_toml = '[agent]\ntimeout_sec = 60.0\n\n[environment]\nnetwork_mode = "public"\n'

    updated = with_agent_network_allowlist(task_toml, ("api.deepseek.com",))

    assert task_toml == ('[agent]\ntimeout_sec = 60.0\n\n[environment]\nnetwork_mode = "public"\n')
    assert 'network_mode = "allowlist"' in updated
    assert 'allowed_hosts = ["api.deepseek.com"]' in updated
