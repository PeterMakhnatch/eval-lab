"""Focused behavioral tests for the OpenRouter Terminus routes.

Covers the consumer-visible boundaries of HAR-104's route table
(``openrouter-metered/xiaomi/mimo-v2.6-flash`` and
``openrouter-metered/openai/gpt-oss-120b``):

- Selector grammar: exactly the table's selectors are accepted, anything else
  fails closed (near-misses included).
- Upstream pinning: the default https URL resolves to
  ``https://openrouter.ai:443/api/v1/chat/completions``; evil hosts, ports,
  paths and userinfo refuse; loopback http stays for tests.
- The generic metered proxy under the ``openrouter`` provider profile, run as
  a real subprocess against a loopback stub: the trial's own selector is the
  only one admitted, per-model provider/reasoning shaping with caller pins
  stripped, the per-model pinned ledger prices, and acceptance of
  OpenRouter's keep-alive padding (leading whitespace) on non-stream JSON.
- The proxy's standalone OPENROUTER_ROUTES mirror matches the contract table.
- Adapter binding: runtime-bound context/prices, stock parser (no
  MimoToolCallParser), capability confined to the controller environment.
- Runner proxy env for the ``openrouter`` provider.
- Treatment key: the route pins land in the key fields and a changed pin
  splits the key; the harness digest records the ATIF trajectory defaults.

Everything runs against scripted loopback fakes: no paid provider calls.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from evallab import runner as runner_module
from evallab.execution_contracts import (
    OPENROUTER_PROXY_CAPABILITY_ENV,
    OPENROUTER_PROXY_PROVIDER,
    OPENROUTER_ROUTES,
    OpenRouterRoute,
    ProxyTrialLimits,
    parse_openrouter_model,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

SECRET_SENTINEL = "test-openrouter-provider-key-13579"
CAPABILITY_SENTINEL = "test-openrouter-capability-token-abc"
UPSTREAM_USAGE = {"prompt_tokens": 30, "completion_tokens": 7}
MIMO = OPENROUTER_ROUTES["xiaomi/mimo-v2.6-flash"]
GPT_OSS = OPENROUTER_ROUTES["openai/gpt-oss-120b"]


def _proxy_module() -> Any:
    source = REPO_ROOT / "containers" / "zai_openapi_secret_proxy.py"
    spec = importlib.util.spec_from_file_location("openrouter_secret_proxy", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. Selector grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("selector", "native"),
    [
        ("openrouter-metered/xiaomi/mimo-v2.6-flash", "xiaomi/mimo-v2.6-flash"),
        ("openrouter-metered/openai/gpt-oss-120b", "openai/gpt-oss-120b"),
    ],
)
def test_parse_openrouter_accepts_exact_selectors(selector: str, native: str) -> None:
    assert parse_openrouter_model(selector) == native


@pytest.mark.parametrize(
    "selector",
    [
        "openrouter-metered/xiaomi/mimo-v2.6-flash ",
        " openrouter-metered/xiaomi/mimo-v2.6-flash",
        "openrouter-metered/xiaomi/mimo-v2.6-flash:fp8",
        "openrouter-metered/xiaomi/mimo-v2.6-flash@checkpoint",
        "openrouter/xiaomi/mimo-v2.6-flash",
        "openrouter-metered/xiaomi/mimo-v2.6-pro",
        "openrouter-metered/openai/gpt-oss-120b:free",
        "openrouter-metered/openai/gpt-oss-20b",
        "openrouter-metered/deepinfra/bf16",
        "openrouter-metered/",
        "openrouter-metered",
        "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
        "tinker/Qwen/Qwen3.5-9B",
        "zai/glm-5.3-flash",
        None,
        "",
    ],
)
def test_parse_openrouter_rejects_anything_else(selector: Any) -> None:
    with pytest.raises(ValueError, match="must be exactly"):
        parse_openrouter_model(selector)


# ---------------------------------------------------------------------------
# 2. Upstream pinning
# ---------------------------------------------------------------------------


def test_openrouter_default_upstream_pins_openrouter_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _proxy_module()
    monkeypatch.setenv("EVALLAB_PROXY_PROVIDER", "openrouter")
    monkeypatch.delenv("EVALLAB_OPENROUTER_UPSTREAM", raising=False)
    assert module._pinned_upstream_url() == (
        "https://openrouter.ai:443/api/v1/chat/completions"
    )


@pytest.mark.parametrize(
    "upstream",
    [
        "https://openrouter.ai",
        "https://openrouter.ai:443",
        "http://127.0.0.1:8471",
    ],
)
def test_openrouter_pinning_accepts_host_and_loopback(
    monkeypatch: pytest.MonkeyPatch, upstream: str
) -> None:
    module = _proxy_module()
    monkeypatch.setenv("EVALLAB_PROXY_PROVIDER", "openrouter")
    monkeypatch.setenv("EVALLAB_OPENROUTER_UPSTREAM", upstream)
    assert module._pinned_upstream_url() == (
        "https://openrouter.ai:443/api/v1/chat/completions"
        if upstream.startswith("https")
        else f"{upstream}/api/v1/chat/completions"
    )


@pytest.mark.parametrize(
    "upstream",
    [
        "https://evil.example.com",
        "https://openrouter.ai.evil.com",
        "https://api.openrouter.ai",
        "https://openrouter.ai:8080",
        "https://openrouter.ai/api/v1/extra",
        "https://user@openrouter.ai",
        "https://openrouter.ai?x=1",
        "http://10.0.0.1:8471",
        "http://127.0.0.1",
        "ftp://openrouter.ai",
    ],
)
def test_openrouter_pinning_refuses_non_pinned(
    monkeypatch: pytest.MonkeyPatch, upstream: str
) -> None:
    module = _proxy_module()
    monkeypatch.setenv("EVALLAB_PROXY_PROVIDER", "openrouter")
    monkeypatch.setenv("EVALLAB_OPENROUTER_UPSTREAM", upstream)
    with pytest.raises(RuntimeError, match="not pinned"):
        module._pinned_upstream_url()


# ---------------------------------------------------------------------------
# 3. Real-subprocess proxy: admission, shaping, prices, padded bodies
# ---------------------------------------------------------------------------


class _OpenRouterUpstream(BaseHTTPRequestHandler):
    """Local stand-in for OpenRouter's chat-completions endpoint."""

    protocol_version = "HTTP/1.1"
    seen: list[dict[str, Any]] = []
    #: Prefix written before the reply body (keep-alive padding stand-in).
    padding = b""

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/v1/chat/completions":
            self._reply(404, b"nope")
            return
        if self.headers.get("Authorization") != f"Bearer {SECRET_SENTINEL}":
            self._reply(401, b"nope")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        type(self).seen.append(body)
        response = {
            "id": "gen-local",
            "object": "chat.completion",
            "model": body.get("model"),
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": dict(UPSTREAM_USAGE),
        }
        self._reply(
            200,
            type(self).padding + json.dumps(response).encode(),
            content_type="application/json; charset=utf-8",
        )

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
def openrouter_upstream() -> Any:
    _OpenRouterUpstream.seen = []
    _OpenRouterUpstream.padding = b""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenRouterUpstream)
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


def _launch_openrouter_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    openrouter_upstream: ThreadingHTTPServer,
    capability: str,
    limits: ProxyTrialLimits,
    route: OpenRouterRoute = MIMO,
) -> tuple[Any, str, Path]:
    secret_file = tmp_path / "provider-key"
    secret_file.write_text(f"{SECRET_SENTINEL}\n")
    secret_file.chmod(0o600)
    usage_path = tmp_path / "usage.json"
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    host, port = openrouter_upstream.server_address
    monkeypatch.setenv("EVALLAB_OPENROUTER_UPSTREAM", f"http://{host}:{port}")
    process, url = runner_module._start_terminus_proxy(
        provider=OPENROUTER_PROXY_PROVIDER,
        secret_path=secret_file,
        capability=capability,
        attempt_id="openrouter-test-attempt",
        usage_path=usage_path,
        limits=limits,
        timeout_seconds=60.0,
        work_dir=work_dir,
        openrouter_spec=route,
    )
    return process, url, usage_path


def _post(
    url: str,
    payload: dict[str, Any],
    capability: str | None = None,
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {capability}"} if capability else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.mark.parametrize(
    ("route", "provider_pin", "reasoning_pin", "prices"),
    [
        (
            MIMO,
            {"order": ["xiaomi"], "allow_fallbacks": False},
            {"enabled": True},
            (140_000, 280_000),
        ),
        (
            GPT_OSS,
            {"order": ["deepinfra/bf16"], "allow_fallbacks": False},
            {"effort": "medium"},
            (37_000, 170_000),
        ),
    ],
)
def test_openrouter_proxy_shapes_request_and_ledger_prices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    openrouter_upstream: Any,
    route: OpenRouterRoute,
    provider_pin: dict[str, Any],
    reasoning_pin: dict[str, Any],
    prices: tuple[int, int],
) -> None:
    process, url, usage_path = _launch_openrouter_proxy(
        tmp_path, monkeypatch, openrouter_upstream, CAPABILITY_SENTINEL, _proxy_limits(), route
    )
    try:
        status, body = _post(
            f"{url}/api/v1/chat/completions",
            {
                "model": route.selector,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1000,
                "temperature": 0.6,
                "top_p": 0.95,
                # Caller pins that must be stripped and replaced:
                "provider": {"order": ["evil"], "allow_fallbacks": True},
                "reasoning": {"enabled": False},
                "reasoning_effort": "none",
            },
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 200, body
        assert len(_OpenRouterUpstream.seen) == 1
        forwarded = _OpenRouterUpstream.seen[0]
        # The selector is rewritten to the native id; sampling passes through.
        assert forwarded["model"] == route.native_model
        assert forwarded["temperature"] == 0.6
        assert forwarded["top_p"] == 0.95
        # The route's pins replace the caller's provider/reasoning control.
        assert forwarded["provider"] == provider_pin
        assert forwarded["reasoning"] == reasoning_pin
        assert "reasoning_effort" not in forwarded

        usage = json.loads(usage_path.read_text())
        assert usage["calls"][0]["requested_model"] == route.selector
        assert usage["calls"][0]["shaping_applied"] is True
        assert usage["pricing"] == {
            "input_cost_micros_per_million": prices[0],
            "output_cost_micros_per_million": prices[1],
        }
        # Ledger cost uses the pinned prices on the reconciled usage.
        expected_cost = -(-(
            UPSTREAM_USAGE["prompt_tokens"] * prices[0]
            + UPSTREAM_USAGE["completion_tokens"] * prices[1]
        ) // 1_000_000)
        assert usage["totals"]["cost_micros"] == expected_cost
        # The provider key never appears in the response or the ledger.
        assert SECRET_SENTINEL not in body.decode()
        assert SECRET_SENTINEL not in usage_path.read_text()
    finally:
        process.terminate()
        process.wait(10)


def test_openrouter_proxy_accepts_padded_non_stream_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openrouter_upstream: Any
) -> None:
    # OpenRouter may pad keep-alive whitespace before a non-stream JSON body.
    _OpenRouterUpstream.padding = b"      \n"
    process, url, usage_path = _launch_openrouter_proxy(
        tmp_path, monkeypatch, openrouter_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    try:
        status, _ = _post(
            f"{url}/api/v1/chat/completions",
            {
                "model": MIMO.selector,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 100,
            },
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 200
        usage = json.loads(usage_path.read_text())
        assert usage["unresolved_requests"] == 0
        assert usage["calls"][0]["state"] == "reconciled"
        assert usage["totals"]["input_tokens"] == UPSTREAM_USAGE["prompt_tokens"]
    finally:
        process.terminate()
        process.wait(10)


def test_openrouter_proxy_admits_only_the_trials_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openrouter_upstream: Any
) -> None:
    process, url, usage_path = _launch_openrouter_proxy(
        tmp_path, monkeypatch, openrouter_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    try:
        endpoint = f"{url}/api/v1/chat/completions"
        for bad in (
            "openrouter-metered/xiaomi/mimo-v2.6-flash:fp8",
            "openrouter-metered/xiaomi/other-model",
            "openrouter/xiaomi/mimo-v2.6-flash",
            # Another admitted route is still refused: the trial binds one.
            GPT_OSS.selector,
            "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
        ):
            status, _ = _post(
                endpoint, {"model": bad, "messages": []}, capability=CAPABILITY_SENTINEL
            )
            assert status == 403, bad
        assert _OpenRouterUpstream.seen == []
        usage = json.loads(usage_path.read_text())
        assert usage["calls"] == []
        assert usage["pricing"] is None
    finally:
        process.terminate()
        process.wait(10)


def test_proxy_route_table_mirrors_contract_table() -> None:
    """The standalone proxy cannot import the contracts; its copy must agree."""
    proxy_routes = _proxy_module().OPENROUTER_ROUTES
    assert set(proxy_routes) == set(OPENROUTER_ROUTES)
    for native, route in OPENROUTER_ROUTES.items():
        mirrored = proxy_routes[native]
        assert mirrored["endpoint"] == route.endpoint
        assert mirrored["provider"] == {
            "order": list(route.provider_pin["order"]),
            "allow_fallbacks": route.provider_pin["allow_fallbacks"],
        }
        assert mirrored["reasoning"] == dict(route.reasoning_pin)
        assert mirrored["prices"] == (
            route.input_cost_micros_per_million,
            route.output_cost_micros_per_million,
        )
        assert mirrored["context_input_tokens"] == route.context_input_tokens


# ---------------------------------------------------------------------------
# 4. Adapter binding (Harbor surface stubbed; adapter logic is real)
# ---------------------------------------------------------------------------


class _FakeTerminus2Options:
    """Upstream Terminus2Options stand-in (Harbor 0.24 options_model base)."""


class _FakeTerminus2:
    """Upstream stand-in recording transport and kwargs."""

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
        self.model_name = model_name
        self.api_base = api_base
        self._llm_kwargs = llm_kwargs
        self._llm_call_kwargs = dict(llm_call_kwargs) if llm_call_kwargs else {}
        self._extra_env = dict(extra_env) if extra_env else {}
        self.extra_kwargs = kwargs


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
        sys.modules,
        "harbor.llms.lite_llm",
        _module("harbor.llms.lite_llm", LiteLLM=type("LiteLLM", (), {})),
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
def openrouter_transport(monkeypatch: pytest.MonkeyPatch, terminus_module: Any) -> Any:
    monkeypatch.setenv("EVALLAB_TERMINUS_PROXY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv(OPENROUTER_PROXY_CAPABILITY_ENV, CAPABILITY_SENTINEL)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-original-openai-key")
    return terminus_module


@pytest.mark.parametrize(
    ("route", "context", "max_output", "input_per_token", "output_per_token"),
    [
        (MIMO, 1_048_576, 131_072, 0.14e-6, 0.28e-6),
        (GPT_OSS, 131_072, 117_964, 0.037e-6, 0.17e-6),
    ],
)
def test_adapter_binds_openrouter_context_prices_and_capability(
    openrouter_transport: Any,
    tmp_path: Path,
    route: OpenRouterRoute,
    context: int,
    max_output: int,
    input_per_token: float,
    output_per_token: float,
) -> None:
    agent = openrouter_transport.SecretSafeTerminus2(logs_dir=tmp_path, model_name=route.selector)
    assert agent.model_name == route.selector
    assert agent.api_base == "http://127.0.0.1:9"
    model_info = agent.extra_kwargs["model_info"]
    assert model_info["max_input_tokens"] == context
    assert model_info["max_output_tokens"] == max_output
    assert model_info["input_cost_per_token"] == pytest.approx(input_per_token)
    assert model_info["output_cost_per_token"] == pytest.approx(output_per_token)
    assert model_info["litellm_provider"] == "openai"
    # Stock parser: the route never wraps the Terminus JSON parser.
    assert agent._mimo_selfhosted is False
    # The capability reaches litellm's OpenAI-compatible lookup only in the
    # controller process environment, never in persisted kwargs.
    assert os.environ["OPENAI_API_KEY"] == CAPABILITY_SENTINEL
    assert CAPABILITY_SENTINEL not in json.dumps(agent._llm_kwargs or {})
    assert CAPABILITY_SENTINEL not in json.dumps(agent._llm_call_kwargs)
    assert CAPABILITY_SENTINEL not in json.dumps(agent._extra_env)


def test_adapter_rejects_openrouter_model_info_override(
    openrouter_transport: Any, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="runtime-bound"):
        openrouter_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name=MIMO.selector,
            model_info={"max_input_tokens": 1},
        )


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "openrouter-metered/xiaomi/mimo-v2.6-flash:fp8",
        "openrouter-metered/",
        "openrouter/xiaomi/mimo-v2.6-flash",
    ],
)
def test_adapter_rejects_non_exact_openrouter_models(
    openrouter_transport: Any, tmp_path: Path, bad: Any
) -> None:
    with pytest.raises(ValueError, match="exact metered model|must be exactly"):
        openrouter_transport.SecretSafeTerminus2(logs_dir=tmp_path, model_name=bad)


# ---------------------------------------------------------------------------
# 5. Runner proxy env
# ---------------------------------------------------------------------------


def test_runner_openrouter_proxy_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "EVALLAB_OPENROUTER_UPSTREAM", "https://openrouter.ai/example-stripped"
    )
    env = runner_module._terminus_proxy_env(
        provider=OPENROUTER_PROXY_PROVIDER,
        secret_path=tmp_path / "key",
        capability=CAPABILITY_SENTINEL,
        attempt_id="attempt-1",
        usage_path=tmp_path / "usage.json",
        limits=_proxy_limits(),
        timeout_seconds=900.0,
        openrouter_spec=GPT_OSS,
    )
    assert env["EVALLAB_PROXY_PROVIDER"] == "openrouter"
    assert env["EVALLAB_OPENROUTER_EXPECTED_MODEL"] == "openai/gpt-oss-120b"
    assert env["EVALLAB_OPENROUTER_SECRET_PATH"] == str(tmp_path / "key")
    assert env["EVALLAB_OPENROUTER_UPSTREAM"] == "https://openrouter.ai/example-stripped"
    assert env[OPENROUTER_PROXY_CAPABILITY_ENV] == CAPABILITY_SENTINEL
    assert env["EVALLAB_OPENROUTER_ATTEMPT_ID"] == "attempt-1"
    assert env["EVALLAB_OPENROUTER_USAGE_FILE"] == str(tmp_path / "usage.json")
    assert env["EVALLAB_OPENROUTER_MAX_REQUESTS"] == "50"
    assert env["EVALLAB_OPENROUTER_MAX_INPUT_TOKENS"] == "100000"
    assert env["EVALLAB_OPENROUTER_MAX_OUTPUT_TOKENS"] == "10000"
    assert env["EVALLAB_OPENROUTER_MAX_TOTAL_TOKENS"] == "110000"
    assert env["EVALLAB_OPENROUTER_MAX_COST_MICROS"] == "10000000"
    assert float(env["EVALLAB_OPENROUTER_CAPABILITY_EXPIRES_AT"]) > 0
    # Budget identity only: no provider key value ever rides the env.
    assert SECRET_SENTINEL not in json.dumps(env)


def test_runner_accepted_returned_models_for_openrouter() -> None:
    accepted = runner_module._accepted_returned_models(MIMO.selector)
    assert "xiaomi/mimo-v2.6-flash" in accepted
    assert "xiaomi/mimo-v2.6-flash:fp8" in accepted
    assert "xiaomi/other-model" not in accepted
    assert "openai/gpt-oss-120b" not in accepted
    gpt_oss = runner_module._accepted_returned_models(GPT_OSS.selector)
    assert "openai/gpt-oss-120b" in gpt_oss
    assert "xiaomi/mimo-v2.6-flash" not in gpt_oss


def test_runner_reads_openrouter_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openrouter_upstream: Any
) -> None:
    process, url, usage_path = _launch_openrouter_proxy(
        tmp_path, monkeypatch, openrouter_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    try:
        status, _ = _post(
            f"{url}/api/v1/chat/completions",
            {
                "model": MIMO.selector,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 100,
            },
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 200
    finally:
        process.terminate()
        process.wait(10)

    usage = runner_module._read_proxy_usage(
        usage_path,
        capability_id="sha256:" + hashlib.sha256(CAPABILITY_SENTINEL.encode()).hexdigest(),
        attempt_id="openrouter-test-attempt",
        limits=_proxy_limits(),
        provider_label="OpenRouter",
        expected_pricing={
            "input_cost_micros_per_million": 140_000,
            "output_cost_micros_per_million": 280_000,
        },
    )
    assert usage["unresolved_requests"] == 0
    assert usage["totals"]["requests"] == 1
    assert usage["totals"]["input_tokens"] == UPSTREAM_USAGE["prompt_tokens"]
    assert usage["totals"]["output_tokens"] == UPSTREAM_USAGE["completion_tokens"]
