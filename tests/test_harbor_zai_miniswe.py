"""Focused tests for the mini-SWE-agent Z.ai OpenAPI provider lane.

Tests cover:
1. SecretSafeZaiMiniSweAgent adapter (accept/reject, env scrubbing, proxy rewrite, secret isolation).
2. containers/zai_openapi_secret_proxy.py (endpoints, auth, model identity, redaction, TrialBudget, pricing).
3. execution_contracts.py (resolve_harbor_agent, build_command, subscription_command, MAX_TRIAL_TIMEOUT_SECONDS=28800).
4. profiles.py & credentials.py (profile resolution, secret_source validator, credential probes).
5. runner.py (lane selection, preflight, profile_for_request).
6. campaigns.py (billable gate for ZAI_OPENAPI_MODEL_SELECTOR).
7. gepa optimizer (model-identity expectation for zai family).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from evallab.benchmark_program_contracts import SyntheticFamilyType
from evallab.campaigns import CampaignDefinitionAttempt, TrialLimits
from evallab.credentials import (
    ZAI_OPENAPI_API_CREDENTIAL,
    available_credentials,
    missing_credential_for,
    probe_zai_openapi_api,
)
from evallab.execution_contracts import (
    DEEPSEEK_MODEL_SELECTOR,
    MAX_TRIAL_TIMEOUT_SECONDS,
    PRIVATE_PERSIST_MODE,
    ZAI_MINISWE_AGENT_IMPORT_PATH,
    ZAI_OPENAPI_MODEL_SELECTOR,
    ZAI_OPENAPI_PROXY_TOKEN,
    ZAI_OPENAPI_PROXY_URL,
    RunRequest,
    build_command,
    resolve_harbor_agent,
    subscription_command,
    validate_request,
)
from evallab.gepa_optimizer.evaluator import DEEPSEEK_TARGET_AGENT
from evallab.profiles import (
    AgentProfile,
    builtin_profiles,
)
from evallab.runner import preflight_request, profile_for_request
from evallab.schemas import ExperimentSpec

SECRET_SENTINEL = "test-zai-openapi-secret-key-12345"


@dataclass(frozen=True)
class _Connection:
    provider: str | None = None
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    configured_base_url: str | None = None
    env: dict[str, str] = field(default_factory=dict, repr=False)
    model: str | None = None
    model_name: str | None = None


class _MiniSweAgent:
    def __init__(self, connection: _Connection, **kwargs: Any) -> None:
        self.connection = connection
        self.exec_calls: list[tuple[str, dict[str, str] | None]] = []
        self.logs_dir = Path(".")
        self.model_name = kwargs.get("model_name") or getattr(connection, "model", None)

    @property
    def model_connection(self) -> _Connection:
        return self.connection

    async def exec_as_agent(
        self,
        environment: Any,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> str:
        del environment, cwd, timeout_sec
        self.exec_calls.append((command, env))
        return "ok"

    def populate_context_post_run(self, context: Any) -> None:
        del context
        (self.logs_dir / "trajectory.json").write_text(
            json.dumps({"authorization": SECRET_SENTINEL, "ok": True}) + "\n"
        )


def _module(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _package(name: str) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    return module


@pytest.fixture
def wrapper_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    for name in ("harbor", "harbor.agents", "harbor.agents.installed", "harbor.environments"):
        monkeypatch.setitem(sys.modules, name, _package(name))
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.installed.mini_swe_agent",
        _module("harbor.agents.installed.mini_swe_agent", MiniSweAgent=_MiniSweAgent),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.model_connection",
        _module("harbor.agents.model_connection", ResolvedModelConnection=_Connection),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.environments.base",
        _module("harbor.environments.base", BaseEnvironment=object),
    )
    sys.modules.pop("evallab.harbor_zai_miniswe", None)
    try:
        yield importlib.import_module("evallab.harbor_zai_miniswe")
    finally:
        sys.modules.pop("evallab.harbor_zai_miniswe", None)


# ---------------------------------------------------------------------------
# 1. Adapter unit tests
# ---------------------------------------------------------------------------


def test_wrapper_accepts_zai_glm_flash(wrapper_module: ModuleType) -> None:
    agent = wrapper_module.SecretSafeZaiMiniSweAgent(
        _Connection(
            provider="zai",
            model="zai/glm-5.3-flash",
            api_key=SECRET_SENTINEL,
            env={"OTHER_VAR": "keep-me"},
        )
    )
    conn = agent.model_connection
    assert conn.provider == "zai"
    assert conn.base_url == ZAI_OPENAPI_PROXY_URL
    assert conn.configured_base_url == ZAI_OPENAPI_PROXY_URL
    assert conn.api_key == ZAI_OPENAPI_PROXY_TOKEN
    assert conn.env["MSWEA_API_KEY"] == ZAI_OPENAPI_PROXY_TOKEN
    assert conn.env["OPENAI_BASE_URL"] == ZAI_OPENAPI_PROXY_URL
    assert conn.env["OPENAI_API_BASE"] == ZAI_OPENAPI_PROXY_URL
    assert conn.env["OTHER_VAR"] == "keep-me"
    assert "ZAI_OPENAPI_API_KEY" not in conn.env


def test_wrapper_rejects_non_zai_provider(wrapper_module: ModuleType) -> None:
    agent = wrapper_module.SecretSafeZaiMiniSweAgent(
        _Connection(provider="openai", model="zai/glm-5.3-flash")
    )
    with pytest.raises(ValueError, match="requires a zai/\\* model"):
        _ = agent.model_connection


def test_wrapper_rejects_unallowed_zai_model(wrapper_module: ModuleType) -> None:
    with pytest.raises(ValueError, match="requires model in"):
        wrapper_module.SecretSafeZaiMiniSweAgent(
            _Connection(provider="zai", model="zai/glm-4")
        )


def test_wrapper_blocks_zai_secret_in_exec_env(wrapper_module: ModuleType) -> None:
    agent = wrapper_module.SecretSafeZaiMiniSweAgent(
        _Connection(provider="zai", model="zai/glm-5.3-flash", api_key=SECRET_SENTINEL)
    )
    with pytest.raises(ValueError, match="cannot enter the task exec environment"):
        asyncio.run(
            agent.exec_as_agent(
                object(),
                "echo hello",
                env={"ZAI_OPENAPI_API_KEY": SECRET_SENTINEL},
            )
        )


def test_wrapper_blocks_cat_secrets_command(wrapper_module: ModuleType) -> None:
    agent = wrapper_module.SecretSafeZaiMiniSweAgent(
        _Connection(provider="zai", model="zai/glm-5.3-flash", api_key=SECRET_SENTINEL)
    )
    with pytest.raises(ValueError, match="cannot enter the task exec command"):
        asyncio.run(
            agent.exec_as_agent(
                object(),
                "cat /run/secrets/evallab_zai_openapi_api_key",
                env={},
            )
        )


def test_wrapper_sanitizes_trajectories(
    wrapper_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_OPENAPI_API_KEY", SECRET_SENTINEL)
    agent = wrapper_module.SecretSafeZaiMiniSweAgent(
        _Connection(provider="zai", model="zai/glm-5.3-flash", api_key=SECRET_SENTINEL)
    )
    agent.logs_dir = tmp_path
    native = tmp_path / "mini-swe-agent.trajectory.json"
    native.write_text(
        json.dumps(
            {
                "authorization": f"Bearer {SECRET_SENTINEL}",
                "content": f"data {SECRET_SENTINEL}",
            }
        )
    )
    agent.populate_context_post_run(object())
    native_data = json.loads(native.read_text())
    atif_data = json.loads((tmp_path / "trajectory.json").read_text())
    assert SECRET_SENTINEL not in native.read_text()
    assert SECRET_SENTINEL not in (tmp_path / "trajectory.json").read_text()
    assert native_data["authorization"] == "<redacted>"
    assert native_data["content"] == "<redacted>"
    assert atif_data["authorization"] == "<redacted>"
    assert native.stat().st_mode & 0o777 == PRIVATE_PERSIST_MODE


# ---------------------------------------------------------------------------
# 2. Proxy unit tests
# ---------------------------------------------------------------------------


class _MockZaiUpstream(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/paas/v4/chat/completions":
            self.send_response(404)
            self.end_headers()
            return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self.send_response(401)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        stream = body.get("stream", False)
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            event = {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "model": "glm-5.3-flash",
                "choices": [{"delta": {"content": "hello"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
            self.wfile.write(f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode())
        else:
            response = {
                "id": "cmpl-1",
                "object": "chat.completion",
                "model": "glm-5.3-flash",
                "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def test_proxy_lifecycle_and_accounting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Start mock upstream
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _MockZaiUpstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    u_host, u_port = upstream.server_address

    secret_file = tmp_path / "secret_key"
    secret_file.write_text(f"{SECRET_SENTINEL}\n")
    secret_file.chmod(0o600)
    usage_file = tmp_path / "zai-openapi-usage.json"
    capability = "test-trial-cap-token"

    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_SECRET_PATH", str(secret_file))
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_UPSTREAM", f"http://127.0.0.1:{u_port}")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_PROXY_CAPABILITY", capability)
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_ATTEMPT_ID", "trial-01")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_USAGE_FILE", str(usage_file))
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_REQUESTS", "5")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_INPUT_TOKENS", "10000")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_OUTPUT_TOKENS", "10000")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_TOTAL_TOKENS", "20000")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_COST_MICROS", "1000000")

    from containers.zai_openapi_secret_proxy import serve

    proxy = serve(host="127.0.0.1", port=0)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    p_host, p_port = proxy.server_address
    proxy_url = f"http://127.0.0.1:{p_port}"

    try:
        # Healthz
        with urllib.request.urlopen(f"{proxy_url}/healthz", timeout=2) as resp:
            assert resp.status == 200
            assert resp.read() == b"ok\n"

        # Unauthorized request
        req = urllib.request.Request(
            f"{proxy_url}/api/paas/v4/chat/completions",
            data=json.dumps({"model": "zai/glm-5.3-flash"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 401

        # Unallowed model
        req = urllib.request.Request(
            f"{proxy_url}/api/paas/v4/chat/completions",
            data=json.dumps({"model": "zai/glm-4", "messages": [{"content": "hi"}]}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {capability}",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 403

        # Successful non-streaming request
        req = urllib.request.Request(
            f"{proxy_url}/api/paas/v4/chat/completions",
            data=json.dumps(
                {
                    "model": "zai/glm-5.3-flash",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 100,
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {capability}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["model"] == "glm-5.3-flash"
            assert data["usage"]["prompt_tokens"] == 10

        # Successful streaming request
        req_stream = urllib.request.Request(
            f"{proxy_url}/chat/completions",
            data=json.dumps(
                {
                    "model": "zai/glm-5.3-flash",
                    "messages": [{"role": "user", "content": "stream"}],
                    "stream": True,
                    "max_tokens": 100,
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {capability}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req_stream, timeout=5) as resp:
            assert resp.status == 200
            content = resp.read().decode()
            assert "glm-5.3-flash" in content
            assert "[DONE]" in content

        # Check usage report
        assert usage_file.is_file()
        usage = json.loads(usage_file.read_text())
        assert usage["totals"]["requests"] == 2
        assert usage["totals"]["input_tokens"] == 20
        assert usage["totals"]["output_tokens"] == 10
        assert usage["unresolved_requests"] == 0
    finally:
        proxy.shutdown()
        upstream.shutdown()


# ---------------------------------------------------------------------------
# 3. execution_contracts.py tests
# ---------------------------------------------------------------------------


def test_resolve_harbor_agent_both_families() -> None:
    # Backward compat default: no model -> DeepSeek wrapper
    assert (
        resolve_harbor_agent("mini-swe-agent")
        == "evallab.harbor_deepseek:SecretSafeDeepSeekMiniSweAgent"
    )
    # DeepSeek family -> DeepSeek wrapper
    assert (
        resolve_harbor_agent("mini-swe-agent", "deepseek/deepseek-flash")
        == "evallab.harbor_deepseek:SecretSafeDeepSeekMiniSweAgent"
    )
    # ZAI family -> SecretSafeZaiMiniSweAgent
    assert (
        resolve_harbor_agent("mini-swe-agent", "zai/glm-5.3-flash")
        == ZAI_MINISWE_AGENT_IMPORT_PATH
    )
    # Other agents unchanged
    assert resolve_harbor_agent("codex") == "evallab.harbor_codex:PinnedCodex"


def test_build_command_splits_by_model_family(tmp_path: Path) -> None:
    task_dir = tmp_path / "tasks" / "sample"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text("[agent]\ntimeout_sec = 60\n")

    # DeepSeek branch
    ds_req = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model="deepseek/deepseek-flash",
        name="run-deepseek",
        jobs_dir=tmp_path / "jobs",
        max_requests=10,
        max_input_tokens=1000,
        max_output_tokens=1000,
        max_total_tokens=2000,
        cost_limit_usd=2.5,
    )
    ds_cmd = build_command(ds_req)
    assert "--agent" in ds_cmd
    assert ds_cmd[ds_cmd.index("--agent") + 1] == (
        "evallab.harbor_deepseek:SecretSafeDeepSeekMiniSweAgent"
    )
    assert ds_cmd[ds_cmd.index("--model") + 1] == "deepseek/deepseek-flash"
    assert "cost_limit=2.5" in ds_cmd

    # ZAI branch
    zai_req = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model="zai/glm-5.3-flash",
        name="run-zai",
        jobs_dir=tmp_path / "jobs",
        max_requests=10,
        max_input_tokens=1000,
        max_output_tokens=1000,
        max_total_tokens=2000,
        cost_limit_usd=1.75,
    )
    zai_cmd = build_command(zai_req)
    assert "--agent" in zai_cmd
    assert zai_cmd[zai_cmd.index("--agent") + 1] == ZAI_MINISWE_AGENT_IMPORT_PATH
    assert zai_cmd[zai_cmd.index("--model") + 1] == "zai/glm-5.3-flash"
    assert "cost_limit=1.75" in zai_cmd

    # Unsupported model raises
    bad_req = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model="openai/gpt-4",
        name="run-bad",
        jobs_dir=tmp_path / "jobs",
        max_requests=10,
        max_input_tokens=1000,
        max_output_tokens=1000,
        max_total_tokens=2000,
        cost_limit_usd=1.0,
    )
    with pytest.raises(ValueError, match="mini-swe-agent requires model"):
        build_command(bad_req)


def test_subscription_command_injects_zai_openapi_overlay(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("[agent]\ntimeout_sec = 60\n")
    req = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model="zai/glm-5.3-flash",
        name="run-zai-sub",
        jobs_dir=tmp_path / "jobs",
    )
    cmd = ["harbor", "run"]
    repo_root = Path(__file__).resolve().parent.parent
    sub_cmd = subscription_command(req, cmd, repo_root=repo_root)
    assert "--extra-docker-compose" in sub_cmd
    overlay_path = sub_cmd[sub_cmd.index("--extra-docker-compose") + 1]
    assert overlay_path.endswith("zai-openapi-secret.compose.yaml")
    assert Path(overlay_path).is_file()


def test_max_trial_timeout_is_28800(tmp_path: Path) -> None:
    assert MAX_TRIAL_TIMEOUT_SECONDS == 28_800
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("[agent]\ntimeout_sec = 60\n")

    # 28800 is accepted
    req_ok = RunRequest(
        task=task_dir,
        agent="oracle",
        name="run-long",
        jobs_dir=tmp_path / "jobs",
        timeout_seconds=28_800,
    )
    validate_request(req_ok)

    # 28801 is rejected
    req_bad = RunRequest(
        task=task_dir,
        agent="oracle",
        name="run-too-long",
        jobs_dir=tmp_path / "jobs",
        timeout_seconds=28_801,
    )
    with pytest.raises(ValueError, match="timeout must be 1-28800"):
        validate_request(req_bad)


# ---------------------------------------------------------------------------
# 4. profiles.py & credentials.py tests
# ---------------------------------------------------------------------------


def test_builtin_profile_and_secret_source_validator() -> None:
    profiles = builtin_profiles()
    assert "mini-swe-agent-glm-5.3-flash" in profiles
    profile = profiles["mini-swe-agent-glm-5.3-flash"]
    assert profile.adapter == "mini-swe-agent"
    assert profile.model == "zai/glm-5.3-flash"
    assert profile.auth_mode == "api-key-environment"
    assert profile.secret_source == "env:ZAI_OPENAPI_API_KEY"

    # Validator allows admitted ZAI_OPENAPI_API_KEY
    p_ok = AgentProfile(
        profile_id="custom-zai-profile",
        adapter="mini-swe-agent",
        model="zai/glm-5.3-flash",
        auth_mode="api-key-environment",
        secret_source="env:ZAI_OPENAPI_API_KEY",
    )
    assert p_ok.secret_source == "env:ZAI_OPENAPI_API_KEY"

    # Validator rejects other environment names
    with pytest.raises(ValueError, match="only the admitted DeepSeek or Z.ai"):
        AgentProfile(
            profile_id="bad-zai-profile",
            adapter="mini-swe-agent",
            model="zai/glm-5.3-flash",
            auth_mode="api-key-environment",
            secret_source="env:UNAUTHORIZED_API_KEY",
        )


def test_credential_probing_and_requirements(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZAI_OPENAPI_API_KEY", raising=False)
    assert not probe_zai_openapi_api()
    assert ZAI_OPENAPI_API_CREDENTIAL not in available_credentials()
    assert (
        missing_credential_for(
            "mini-swe-agent",
            available_credentials(),
            model="zai/glm-5.3-flash",
        )
        == ZAI_OPENAPI_API_CREDENTIAL
    )

    monkeypatch.setenv("ZAI_OPENAPI_API_KEY", SECRET_SENTINEL)
    assert probe_zai_openapi_api()
    assert ZAI_OPENAPI_API_CREDENTIAL in available_credentials()
    assert (
        missing_credential_for(
            "mini-swe-agent",
            available_credentials(),
            model="zai/glm-5.3-flash",
        )
        is None
    )


# ---------------------------------------------------------------------------
# 5. runner.py tests
# ---------------------------------------------------------------------------


def test_runner_profile_for_request(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("[agent]\ntimeout_sec = 60\n")

    # Resolves new profile
    req_zai = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model="zai/glm-5.3-flash",
        name="run-zai",
        jobs_dir=tmp_path / "jobs",
    )
    p_zai = profile_for_request(req_zai)
    assert p_zai.profile_id == "mini-swe-agent-glm-5.3-flash"
    assert p_zai.model == "zai/glm-5.3-flash"

    # DeepSeek profile resolved when requested
    req_ds = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model="deepseek/deepseek-flash",
        name="run-ds",
        jobs_dir=tmp_path / "jobs",
    )
    p_ds = profile_for_request(req_ds)
    assert p_ds.profile_id == "mini-swe-agent-deepseek-v4-flash"

    # Default resolves DeepSeek profile when model is None
    req_default = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        name="run-def",
        jobs_dir=tmp_path / "jobs",
    )
    assert profile_for_request(req_default).profile_id == "mini-swe-agent-deepseek-v4-flash"


def test_runner_preflight_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("[agent]\ntimeout_sec = 60\n")
    req_zai = RunRequest(
        task=task_dir,
        agent="mini-swe-agent",
        model="zai/glm-5.3-flash",
        name="run-preflight",
        jobs_dir=tmp_path / "jobs",
    )

    monkeypatch.delenv("ZAI_OPENAPI_API_KEY", raising=False)
    decision = preflight_request(req_zai)
    assert not decision.proceed
    assert "ZAI_OPENAPI_API_KEY" in (decision.reason or "")

    monkeypatch.setenv("ZAI_OPENAPI_API_KEY", SECRET_SENTINEL)
    decision_ok = preflight_request(req_zai)
    assert decision_ok.proceed


# ---------------------------------------------------------------------------
# 6. campaigns.py tests
# ---------------------------------------------------------------------------


def test_campaign_admits_zai_openapi_model() -> None:
    limits = TrialLimits(
        max_requests=10,
        max_cost_usd=2.0,
        max_input_tokens=5000,
        max_output_tokens=5000,
        max_total_tokens=10000,
        max_wall_clock_seconds=1200,
    )
    spec = ExperimentSpec(
        name="test-zai-spec",
        hypothesis="Test Z.ai OpenAPI lane",
        purpose="baseline",
        task="tasks/task-one",
        task_id="task-one",
        agent="mini-swe-agent",
        model=ZAI_OPENAPI_MODEL_SELECTOR,
        jobs_dir="runs",
        task_family=SyntheticFamilyType.FAMILY_A_STATE_INVERSION.value,
        attempts=1,
        concurrency=1,
        timeout_seconds=600,
        submitted_by="test-user",
        est_cost_usd=1.0,
        requires=["fresh-zai-key"],
    )
    attempt = CampaignDefinitionAttempt(
        cell_id="cell-01",
        task_id="task-one",
        attempt=1,
        spec=spec,
        limits=limits,
    )
    assert attempt.spec.model == ZAI_OPENAPI_MODEL_SELECTOR


# ---------------------------------------------------------------------------
# 7. gepa optimizer tests
def test_gepa_evaluator_expected_model_for_zai() -> None:
    from evallab.gepa_optimizer.evaluator import (
        DEEPSEEK_ALLOWED_MODEL,
    )

    # Helper function matching evaluator.py lines 723-736
    def _compute_expected_model(agent: str, model: str | None) -> str:
        return (
            str(model).rsplit("/", 1)[-1]
            if str(model).startswith("zai/")
            else (
                DEEPSEEK_ALLOWED_MODEL
                if agent == DEEPSEEK_TARGET_AGENT
                else str(model).rsplit("/", 1)[-1]
            )
        )

    # DeepSeek selector maps to DEEPSEEK_ALLOWED_MODEL ('deepseek-flash')
    assert (
        _compute_expected_model(DEEPSEEK_TARGET_AGENT, DEEPSEEK_MODEL_SELECTOR)
        == DEEPSEEK_ALLOWED_MODEL
    )
    assert _compute_expected_model(DEEPSEEK_TARGET_AGENT, None) == DEEPSEEK_ALLOWED_MODEL

    # Z.ai selector maps to 'glm-5.3-flash'
    assert (
        _compute_expected_model(DEEPSEEK_TARGET_AGENT, ZAI_OPENAPI_MODEL_SELECTOR)
        == "glm-5.3-flash"
    )
    assert _compute_expected_model("zai-opencode", "zai-coding-plan/glm-5.3") == "glm-5.3"
