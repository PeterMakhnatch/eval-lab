"""Focused tests for the Terminus2 host-loopback runtime lane.

Covers, with real behavior throughout (no mock-echo or wiring tautologies):

1. ``evallab.harbor_terminus.SecretSafeTerminus2`` — exact-route admission,
   transport/credential override rejection, loopback-endpoint and capability
   binding, in-process provider-key binding without trajectory persistence,
   task-container env isolation, and trajectory redaction.
2. ``containers/zai_openapi_secret_proxy.py`` host entrypoint — subprocess
   launch with ``--host/--port/--ready-file``, loopback binding, capability
   auth, model gating, cap exhaustion (429), and ledger reconciliation
   through the runner's real ``_read_proxy_usage`` accounting.
3. ``evallab.runner`` — Terminus inclusion in ``_proxy_trial_limits`` /
   ``_proxy_attempt_id``, fail-closed lane setup without a bound capability,
   proxy stop lifecycle, and declared-network preservation at staging.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from evallab import runner as runner_module
from evallab.execution_contracts import (
    PRIVATE_PERSIST_MODE,
    TERMINUS_PROXY_URL_ENV,
    ZAI_OPENAPI_MODEL_SELECTOR,
    ProxyTrialLimits,
    RunRequest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROXY_SCRIPT = REPO_ROOT / "containers" / "zai_openapi_secret_proxy.py"

SECRET_SENTINEL = "test-terminus-provider-key-67890"
CAPABILITY_SENTINEL = "test-terminus-capability-token-abc"
UPSTREAM_USAGE = {"prompt_tokens": 10, "completion_tokens": 5}


# ---------------------------------------------------------------------------
# 1. Adapter tests (Harbor surface stubbed; adapter logic is real)
# ---------------------------------------------------------------------------


class _FakeTerminus2Options:
    """Upstream Terminus2Options stand-in (Harbor 0.24 options_model base)."""


class _FakeTerminus2:
    """Minimal upstream Terminus2 stand-in recording constructor transport."""

    def __init__(
        self,
        logs_dir: Path | str | None = None,
        model_name: str | None = None,
        *args: Any,
        api_base: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        llm_call_kwargs: dict[str, Any] | None = None,
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.logs_dir = Path(logs_dir) if logs_dir is not None else Path(".")
        self.model_name = model_name
        self.positional_args = args
        self.api_base = api_base
        self._llm_kwargs = llm_kwargs
        self._llm_call_kwargs = dict(llm_call_kwargs) if llm_call_kwargs else {}
        self._extra_env = dict(extra_env) if extra_env else {}
        self.extra_kwargs = kwargs

    run_error: BaseException | None = None

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        del instruction, environment, context
        if self.run_error is not None:
            raise self.run_error

    def populate_context_post_run(self, context: Any) -> None:
        del context

    def _dump_trajectory_with_continuation_index(self, continuation_index: int) -> None:
        del continuation_index

    def _dump_trajectory(self) -> None:
        # Mirrors Harbor 0.21.0: the plain dump delegates to the indexed dump.
        self._dump_trajectory_with_continuation_index(0)

    async def _execute_commands(self, commands: Any, session: Any) -> Any:
        del session
        return False, "stub terminal output\n"


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
def terminus_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in (
        "harbor",
        "harbor.agents",
        "harbor.agents.installed",
        "harbor.agents.terminus_2",
        "harbor.llms",
    ):
        monkeypatch.setitem(sys.modules, name, _package(name))
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.installed.base",
        _module(
            "harbor.agents.installed.base",
            NonZeroAgentExitCodeError=type("NonZeroAgentExitCodeError", (RuntimeError,), {}),
        ),
    )
    monkeypatch.setitem(
        sys.modules, "harbor.llms.lite_llm", _module("harbor.llms.lite_llm", LiteLLM=type("LiteLLM", (), {}))
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.terminus_2.terminus_2",
        _module(
            "harbor.agents.terminus_2.terminus_2",
            Terminus2=_FakeTerminus2,
            Terminus2Options=_FakeTerminus2Options,
        ),
    )
    sys.modules.pop("evallab.harbor_terminus", None)
    try:
        return importlib.import_module("evallab.harbor_terminus")
    finally:
        sys.modules.pop("evallab.harbor_terminus", None)


@pytest.fixture
def trial_transport(monkeypatch: pytest.MonkeyPatch, terminus_module: Any) -> Any:
    monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_PROXY_CAPABILITY", CAPABILITY_SENTINEL)
    monkeypatch.setenv("ZAI_API_KEY", "fixture-original-key")
    return terminus_module


def test_adapter_binds_exact_route_without_persisting_secrets(
    trial_transport: Any, tmp_path: Path
) -> None:
    agent = trial_transport.SecretSafeTerminus2(
        logs_dir=tmp_path,
        model_name="zai/glm-5.3-flash",
        llm_call_kwargs={"max_tokens": 100},
    )
    assert agent.model_name == ZAI_OPENAPI_MODEL_SELECTOR
    assert agent.api_base == "http://127.0.0.1:9"
    # Parent's real per-call kwarg passes through untouched.
    assert agent._llm_call_kwargs == {"max_tokens": 100}
    # Capability reaches litellm's zai provider lookup in-process only.
    assert os.environ["ZAI_API_KEY"] == CAPABILITY_SENTINEL
    stored = json.dumps(
        {
            "llm_kwargs": agent._llm_kwargs,
            "llm_call_kwargs": agent._llm_call_kwargs,
            "extra_env": agent._extra_env,
        }
    )
    assert CAPABILITY_SENTINEL not in stored


def test_adapter_rejects_non_exact_models(trial_transport: Any, tmp_path: Path) -> None:
    for bad in (None, "glm-5.3-flash", "zai/glm-4", "openai/gpt-4", "zai/glm-5.3-flash "):
        with pytest.raises(ValueError, match="exact metered model"):
            trial_transport.SecretSafeTerminus2(logs_dir=tmp_path, model_name=bad)


def test_adapter_rejects_transport_overrides(
    trial_transport: Any, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="api_base"):
        trial_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name="zai/glm-5.3-flash",
            api_base="http://127.0.0.1:9",
        )
    with pytest.raises(ValueError, match="transport override"):
        trial_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name="zai/glm-5.3-flash",
            llm_kwargs={"api_key": CAPABILITY_SENTINEL},
        )
    with pytest.raises(ValueError, match="transport override"):
        trial_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name="zai/glm-5.3-flash",
            llm_kwargs={"OPENAI_BASE_URL": "http://127.0.0.1:9"},
        )
    with pytest.raises(ValueError, match="transport override"):
        trial_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name="zai/glm-5.3-flash",
            llm_call_kwargs={"base_url": "http://127.0.0.1:9"},
        )
    with pytest.raises(ValueError, match="transport override"):
        trial_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name="zai/glm-5.3-flash",
            api_key=CAPABILITY_SENTINEL,
        )
    with pytest.raises(ValueError, match="litellm backend"):
        trial_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name="zai/glm-5.3-flash",
            llm_backend="tinker",
        )
    with pytest.raises(ValueError, match="at most logs_dir positionally"):
        trial_transport.SecretSafeTerminus2(
            tmp_path, "zai/glm-5.3-flash", model_name="zai/glm-5.3-flash"
        )


def test_adapter_requires_runner_loopback_endpoint(
    terminus_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_PROXY_CAPABILITY", CAPABILITY_SENTINEL)
    monkeypatch.setenv("ZAI_API_KEY", "fixture-original-key")
    for bad in (
        "",
        "http://0.0.0.0:8080",
        "http://localhost:8080",
        "https://127.0.0.1:8080",
        "http://127.0.0.1/",
        "http://127.0.0.1:0",
        "http://user:pass@127.0.0.1:8080",
        "http://127.0.0.1:8080/extra/path",
        "http://192.168.1.10:8080",
    ):
        monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, bad)
        with pytest.raises(ValueError, match="127.0.0.1"):
            terminus_module.SecretSafeTerminus2(
                logs_dir=tmp_path, model_name="zai/glm-5.3-flash"
            )


def test_adapter_requires_bound_capability(
    terminus_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv("ZAI_API_KEY", "fixture-original-key")
    for bad in ("", "evallab-proxy-placeholder"):
        monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_PROXY_CAPABILITY", bad)
        with pytest.raises(ValueError, match="bound trial capability"):
            terminus_module.SecretSafeTerminus2(
                logs_dir=tmp_path, model_name="zai/glm-5.3-flash"
            )


def test_adapter_rejects_secret_task_env(
    trial_transport: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_OPENAPI_API_KEY", SECRET_SENTINEL)
    with pytest.raises(ValueError, match="cannot enter the task environment"):
        trial_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name="zai/glm-5.3-flash",
            extra_env={"ZAI_OPENAPI_API_KEY": SECRET_SENTINEL},
        )
    with pytest.raises(ValueError, match="cannot enter the task environment"):
        trial_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name="zai/glm-5.3-flash",
            extra_env={"HELPER_TOKEN": CAPABILITY_SENTINEL},
        )
    with pytest.raises(ValueError, match="cannot enter the task environment"):
        trial_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name="zai/glm-5.3-flash",
            extra_env={"EVALLAB_TERMINUS_PROXY_URL": "http://127.0.0.1:9"},
        )


def test_adapter_sanitizes_trajectory_files(
    trial_transport: Any, tmp_path: Path
) -> None:
    agent = trial_transport.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name="zai/glm-5.3-flash"
    )
    trajectory = tmp_path / "trajectory.json"
    trajectory.write_text(
        json.dumps(
            {
                "authorization": f"Bearer {CAPABILITY_SENTINEL}",
                "content": f"evidence {CAPABILITY_SENTINEL}",
            }
        )
    )
    continuation = tmp_path / "trajectory.cont-1.json"
    continuation.write_text(json.dumps({"content": CAPABILITY_SENTINEL}))
    agent.populate_context_post_run(object())
    for path in (trajectory, continuation):
        text = path.read_text()
        assert CAPABILITY_SENTINEL not in text
        payload = json.loads(text)
        assert payload.get("authorization", "<redacted>") == "<redacted>"
        assert path.stat().st_mode & 0o777 == PRIVATE_PERSIST_MODE


# litellm's error when the proxy refuses a call at a trial ceiling, as the
# HAR-90 adapter smoke observed it through real Harbor.
CEILING_429 = "litellm.RateLimitError: RateLimitError: OpenAIException - trial budget exhausted"


def _wrapped(message: str) -> BaseException:
    try:
        raise RuntimeError(message)
    except RuntimeError as inner:
        try:
            raise ValueError("Terminus episode failed") from inner
        except ValueError as outer:
            return outer


@pytest.mark.parametrize(
    "error",
    [pytest.param(RuntimeError(CEILING_429), id="direct"), pytest.param(_wrapped(CEILING_429), id="wrapped")],
)
def test_ceiling_trip_ends_the_agent_as_an_exit_error_with_a_stop_reason(
    trial_transport: Any, tmp_path: Path, error: BaseException
) -> None:
    agent = trial_transport.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name=ZAI_OPENAPI_MODEL_SELECTOR
    )
    agent.run_error = error
    context = SimpleNamespace(metadata={"n_episodes": 3})

    # Harbor's trial catches NonZeroAgentExitCodeError and still runs the
    # verifier; any other agent exception skips it.
    with pytest.raises(trial_transport.NonZeroAgentExitCodeError) as caught:
        asyncio.run(agent.run("task", None, context))

    assert type(caught.value).__name__ == "TrialBudgetExhaustedError"
    assert caught.value.__cause__ is error
    assert context.metadata == {"n_episodes": 3, "stop_reason": "trial_budget_exhausted"}


def test_other_agent_errors_propagate_without_a_stop_reason(
    trial_transport: Any, tmp_path: Path
) -> None:
    agent = trial_transport.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name=ZAI_OPENAPI_MODEL_SELECTOR
    )
    # A provider's own rate limit is transient, not a trial ceiling.
    error = RuntimeError("litellm.RateLimitError: RateLimitError: OpenAIException - rate limit reached")
    agent.run_error = error
    context = SimpleNamespace(metadata=None)

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(agent.run("task", None, context))

    assert caught.value is error
    assert context.metadata is None


# ---------------------------------------------------------------------------
# 2. Host proxy entrypoint (real subprocess, local deterministic upstream)
# ---------------------------------------------------------------------------


class _DeterministicUpstream(BaseHTTPRequestHandler):
    """Local stand-in for the Z.ai OpenAPI endpoint with fixed usage."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/paas/v4/chat/completions":
            self._reply(404, b"nope")
            return
        if self.headers.get("Authorization") != f"Bearer {SECRET_SENTINEL}":
            self._reply(401, b"nope")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        response = {
            "id": "cmpl-local",
            "object": "chat.completion",
            "model": "glm-5.3-flash",
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": dict(UPSTREAM_USAGE),
        }
        encoded = json.dumps(response).encode()
        assert body.get("model") == "glm-5.3-flash"
        self._reply(200, encoded, content_type="application/json; charset=utf-8")

    def _reply(
        self, status: int, body: bytes, content_type: str = "text/plain"
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.fixture
def local_upstream() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DeterministicUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()


def _proxy_limits(**overrides: Any) -> ProxyTrialLimits:
    values: dict[str, Any] = {
        "max_requests": 50,
        "max_input_tokens": 100000,
        "max_output_tokens": 10000,
        "max_total_tokens": 110000,
        "max_cost_micros": 10000000,
    }
    values.update(overrides)
    return ProxyTrialLimits(**values)


def _launch_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_upstream: ThreadingHTTPServer,
    capability: str,
    limits: ProxyTrialLimits,
    attempt_id: str = "terminus-test-attempt",
    provider: str = "zai_openapi",
) -> tuple[subprocess.Popen[bytes], str, Path]:
    secret_file = tmp_path / "provider-key"
    secret_file.write_text(f"{SECRET_SENTINEL}\n")
    secret_file.chmod(0o600)
    usage_path = tmp_path / "usage.json"
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    host, port = local_upstream.server_address
    monkeypatch.setenv(
        "EVALLAB_ZAI_OPENAPI_UPSTREAM", f"http://{host}:{port}"
    )
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_INPUT_COST_MICROS_PER_MILLION", "150000")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_OUTPUT_COST_MICROS_PER_MILLION", "500000")
    process, url = runner_module._start_terminus_proxy(
        provider=provider,
        secret_path=secret_file,
        capability=capability,
        attempt_id=attempt_id,
        usage_path=usage_path,
        limits=limits,
        timeout_seconds=60.0,
        work_dir=work_dir,
    )
    return process, url, usage_path


def _post(
    url: str,
    payload: dict[str, Any],
    capability: str | None = None,
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        f"{url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            **(
                {"Authorization": f"Bearer {capability}"}
                if capability is not None
                else {}
            ),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_host_proxy_binds_loopback_and_meters_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_upstream: Any
) -> None:
    process, url, usage_path = _launch_proxy(
        tmp_path, monkeypatch, local_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    try:
        assert url.startswith("http://127.0.0.1:")
        ready = json.loads((tmp_path / "work" / "terminus-proxy-ready.json").read_text())
        assert ready["host"] == "127.0.0.1"
        assert url == f"http://127.0.0.1:{ready['port']}"

        status, _ = _post(
            url, {"model": "zai/glm-5.3-flash", "messages": []}
        )
        assert status == 401

        status, _ = _post(
            url,
            {"model": "zai/glm-4", "messages": [{"role": "user", "content": "hi"}]},
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 403

        status, body = _post(
            url,
            {
                "model": "zai/glm-5.3-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 100,
            },
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 200
        payload = json.loads(body.decode())
        assert payload["model"] == "glm-5.3-flash"
        assert payload["usage"] == UPSTREAM_USAGE

        status, body = _post(
            url,
            {
                "model": "zai/glm-5.3-flash",
                "messages": [{"role": "user", "content": "again"}],
                "max_tokens": 100,
                "stream": True,
            },
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 200
        assert "glm-5.3-flash" in body.decode()
    finally:
        runner_module._stop_terminus_proxy(process)
    assert process.poll() is not None
    # Stopping twice is safe (cancellation/timeout paths share the helper).
    runner_module._stop_terminus_proxy(process)
    runner_module._stop_terminus_proxy(None)

    ledger_text = usage_path.read_text()
    assert CAPABILITY_SENTINEL not in ledger_text
    assert SECRET_SENTINEL not in ledger_text
    ledger = json.loads(ledger_text)
    assert ledger["totals"]["requests"] == 2
    assert ledger["totals"]["input_tokens"] == 2 * UPSTREAM_USAGE["prompt_tokens"]
    assert ledger["totals"]["output_tokens"] == 2 * UPSTREAM_USAGE["completion_tokens"]
    assert ledger["unresolved_requests"] == 0
    assert ledger["schema_version"] == 2
    assert ledger["attempted"]["requests"] == 0

    # The runner's real reconciliation accepts the host-proxy ledger.
    usage = runner_module._read_proxy_usage(
        usage_path,
        capability_id="sha256:" + hashlib.sha256(CAPABILITY_SENTINEL.encode()).hexdigest(),
        attempt_id="terminus-test-attempt",
        limits=_proxy_limits(),
        provider_label="Z.ai OpenAPI",
        expected_pricing={
            "input_cost_micros_per_million": 150000,
            "output_cost_micros_per_million": 500000,
        },
    )
    assert usage["totals"]["cost_micros"] == (
        2 * UPSTREAM_USAGE["prompt_tokens"] * 150000
        + 2 * UPSTREAM_USAGE["completion_tokens"] * 500000
        + 999999
    ) // 1000000


def test_host_proxy_start_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_upstream: Any
) -> None:
    secret_file = tmp_path / "provider-key"
    secret_file.write_text(f"{SECRET_SENTINEL}\n")
    secret_file.chmod(0o600)
    # A regular file where the ledger directory must go: the proxy's own
    # budget init crashes, and the supervisor must fail before Harbor
    # launches rather than hang or run unmetered.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    host, port = local_upstream.server_address
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_UPSTREAM", f"http://{host}:{port}")
    with pytest.raises(RuntimeError, match="before becoming ready"):
        runner_module._start_terminus_proxy(
            provider="zai_openapi",
            secret_path=secret_file,
            capability=CAPABILITY_SENTINEL,
            attempt_id="terminus-test-attempt",
            usage_path=blocker / "usage.json",
            limits=_proxy_limits(),
            timeout_seconds=60.0,
            work_dir=work_dir,
        )


# ---------------------------------------------------------------------------
# 3. Runner lane bindings
# ---------------------------------------------------------------------------


def _terminus_request(tmp_path: Path, **overrides: Any) -> RunRequest:
    task_dir = tmp_path / "task"
    task_dir.mkdir(exist_ok=True)
    (task_dir / "task.toml").write_text("[agent]\ntimeout_sec = 60\n")
    values: dict[str, Any] = {
        "task": task_dir,
        "agent": "terminus-2",
        "model": "zai/glm-5.3-flash",
        "name": "terminus-run",
        "jobs_dir": tmp_path / "jobs",
        "max_requests": 10,
        "max_input_tokens": 1000,
        "max_output_tokens": 1000,
        "max_total_tokens": 2000,
        "cost_limit_usd": 1.5,
    }
    values.update(overrides)
    return RunRequest(**values)


def test_metered_terminus_requires_ceilings_but_controls_do_not(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="explicit provider ceilings"):
        runner_module._proxy_trial_limits(
            _terminus_request(tmp_path, max_requests=None)
        )
    oracle = _terminus_request(tmp_path, agent="oracle")
    assert runner_module._proxy_trial_limits(oracle) is None


def test_terminus_lane_requires_bound_capability(tmp_path: Path) -> None:
    command = [
        "harbor",
        "run",
        "--agent",
        "evallab.harbor_terminus:SecretSafeTerminus2",
        "--model",
        "zai/glm-5.3-flash",
        "--env",
        "docker",
    ]
    with pytest.raises(ValueError, match="Terminus execution requires"):
        runner_module.run_harbor_process(
            command,
            cwd=tmp_path,
            timeout_seconds=5,
            log_path=tmp_path / "harbor.log",
        )


def test_terminus_staging_preserves_declared_network(tmp_path: Path) -> None:
    source = tmp_path / "source-task"
    source.mkdir()
    (source / "task.toml").write_text(
        "[environment]\nnetwork_mode = \"no-network\"\n"
        "\n[agent]\ntimeout_sec = 60\n"
    )
    staged, adaptation = runner_module._stage_task_for_host(
        source,
        tmp_path / "staging" / "job",
        agent_allowed_hosts=(),
        preserve_declared_network=True,
    )
    assert adaptation is None
    assert (staged / "task.toml").read_text() == (source / "task.toml").read_text()
    manifest = json.loads((staged / "run_manifest.json").read_text())
    assert manifest["agent_allowed_hosts"] == []
    assert "network_adaptation" not in manifest
    assert "allowlist" not in (staged / "task.toml").read_text()


# ---------------------------------------------------------------------------
# 4. Step-layer annotation (HAR-92 first-step + sanitized-message fixes)
# ---------------------------------------------------------------------------


def _layer_record(message: str, keystrokes: str | None = None) -> dict[str, Any]:
    sent = keystrokes if keystrokes is not None else message
    return {
        "message": message,
        "reasoning": None,
        "prose_mapped": False,
        "commands": [(sent, 0.1)],
        "task_complete": False,
        "parse_error": None,
        "exec": {
            "keystrokes_sent": [sent],
            "durations_sec": [0.1],
            "sent_at": "2026-09-29T00:00:00+00:00",
            "timeout": False,
            "output": "ok\n",
        },
    }


def _agent_step(message: str) -> SimpleNamespace:
    return SimpleNamespace(source="agent", message=message, is_copied_context=False, extra=None)


def test_first_agent_step_gets_recorded_layers(trial_transport: Any, tmp_path: Path) -> None:
    """The first annotation must not treat its own list as a split (HAR-92)."""
    agent = trial_transport.SecretSafeTerminus2(logs_dir=tmp_path, model_name="zai/glm-5.3-flash")
    step = _agent_step("echo hi")
    agent._trajectory_steps = [step]
    agent._layer_queue = [_layer_record("echo hi")]
    agent._save_raw_content_in_trajectory = True
    assert agent._layer_steps_id is None
    agent._annotate_step_layers()
    layers = (step.extra or {}).get("step_layers")
    assert layers is not None, "first agent step lost its episode record"
    assert layers["provenance"] == "recorded"
    assert layers["executed"]["keystrokes_sent"] == ["echo hi"]


def test_non_matching_step_preserves_later_records(trial_transport: Any, tmp_path: Path) -> None:
    """A sanitized/redacted step leaves later episode records intact (HAR-92)."""
    agent = trial_transport.SecretSafeTerminus2(logs_dir=tmp_path, model_name="zai/glm-5.3-flash")
    redacted = _agent_step("REDACTED")
    agent._trajectory_steps = [redacted]
    agent._layer_queue = [_layer_record("echo later", keystrokes="echo later")]
    agent._save_raw_content_in_trajectory = True
    agent._annotate_step_layers()
    assert (redacted.extra or {}).get("step_layers") is None
    assert len(agent._layer_queue) == 1, "non-matching step dropped later records"
    later = _agent_step("echo later")
    agent._trajectory_steps.append(later)
    agent._annotate_step_layers()
    layers = (later.extra or {}).get("step_layers")
    assert layers is not None, "later step lost its record to the redacted step"
    assert layers["provenance"] == "recorded"
    assert layers["executed"]["keystrokes_sent"] == ["echo later"]


def test_list_swap_keeps_bridge_record_without_double_counting(
    trial_transport: Any, tmp_path: Path
) -> None:
    """A summarization-style list swap must not drop the bridging record (HAR-92)."""
    agent = trial_transport.SecretSafeTerminus2(logs_dir=tmp_path, model_name="zai/glm-5.3-flash")
    agent._save_raw_content_in_trajectory = True
    first = _agent_step("first turn")
    agent._trajectory_steps = [first]
    agent._layer_queue = [_layer_record("first turn")]
    agent._annotate_step_layers()
    assert (first.extra or {}).get("step_layers", {}).get("provenance") == "recorded"
    # Upstream seals the head and installs a fresh list: the converted copy
    # plus a step whose record was queued at the split's own dump. The
    # already-layered step object itself survives here to prove it never
    # consumes a second record.
    bridged = _agent_step("bridging turn")
    agent._trajectory_steps = [first, bridged]
    agent._layer_queue = [_layer_record("bridging turn")]
    agent._annotate_step_layers()
    first_layers = (first.extra or {}).get("step_layers")
    assert first_layers is not None
    assert first_layers["provenance"] == "recorded"
    assert first_layers["executed"]["keystrokes_sent"] == ["first turn"]
    layers = (bridged.extra or {}).get("step_layers")
    assert layers is not None, "list swap dropped the bridging record"
    assert layers["provenance"] == "recorded"
    assert layers["executed"]["keystrokes_sent"] == ["bridging turn"]
    assert agent._layer_queue == []


def _parsed_pending(message: str, keystrokes: str, *, parse_error: str | None = None) -> dict[str, Any]:
    """A ``_pending_layer`` as ``_handle_llm_interaction`` builds it after a parse."""
    return {
        "message": message,
        "reasoning": None,
        "prose_mapped": False,
        "commands": [(keystrokes, 0.5)],
        "task_complete": False,
        "parse_error": parse_error,
        "exec": None,
    }


def test_split_between_parse_and_execute_keeps_executed_layer(
    trial_transport: Any, tmp_path: Path
) -> None:
    """A split dump must not abandon the live turn (HAR-92 follow-up).

    Drives Harbor 0.21.0's real call order in ``linear_history`` mode: parse
    (``_handle_llm_interaction``) leaves ``_pending_layer`` set, the pending
    handoff splits (``_run_agent_loop``:1322 dumps via
    ``_dump_trajectory_with_continuation_index`` *before* ``_execute_commands``
    at :1399), then the turn executes, its step is appended (:1503), and the
    episode dump (:1529) annotates.
    """
    agent = trial_transport.SecretSafeTerminus2(logs_dir=tmp_path, model_name="zai/glm-5.3-flash")
    agent._save_raw_content_in_trajectory = True
    agent._trajectory_steps = []
    agent._pending_layer = _parsed_pending("run the migration", "run-migration\n")
    agent._dump_trajectory_with_continuation_index(0)
    assert agent._pending_layer is not None, "split dump abandoned a live turn"
    command = SimpleNamespace(keystrokes="run-migration\n", duration_sec=0.5)
    asyncio.run(agent._execute_commands([command], object()))
    agent._trajectory_steps.append(_agent_step("run the migration"))
    agent._dump_trajectory()
    layers = (agent._trajectory_steps[0].extra or {}).get("step_layers")
    assert layers is not None, "executed turn lost its episode record at the split"
    assert layers["provenance"] == "recorded"
    assert layers["accepted"]["kind"] == "calls"
    assert layers["executed"]["keystrokes_sent"] == ["run-migration\n"]
    assert layers["executed"]["reason"] is None
    assert layers["observed"]["output"] == "stub terminal output\n"


def test_cancelled_turn_before_execute_keeps_not_executed_reason(
    trial_transport: Any, tmp_path: Path
) -> None:
    """A turn parsed but cancelled before execute still records a reason (HAR-92)."""
    agent = trial_transport.SecretSafeTerminus2(logs_dir=tmp_path, model_name="zai/glm-5.3-flash")
    agent._save_raw_content_in_trajectory = True
    agent._trajectory_steps = []
    agent._pending_layer = _parsed_pending("run the migration", "run-migration\n")
    # Cancel/timeout: run()'s finally reaches _dump_trajectory (terminus_2.py:1643).
    agent._dump_trajectory()
    assert agent._pending_layer is None
    agent._trajectory_steps.append(_agent_step("run the migration"))
    agent._dump_trajectory()
    layers = (agent._trajectory_steps[0].extra or {}).get("step_layers")
    assert layers is not None, "cancelled turn lost its proposed/accepted layers"
    assert layers["provenance"] == "recorded"
    assert layers["executed"]["keystrokes_sent"] is None
    assert layers["executed"]["reason"] == "episode interrupted before execution"
    assert layers["observed"]["output"] is None
