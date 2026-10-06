"""Focused behavioral tests for the Tinker Terminus route and related knobs.

Covers the consumer-visible boundaries of HAR-81's Terminus additions:

- ``tinker/<base>[@checkpoint]`` selector grammar (accept/reject) and its
  admission through ``validate_request``/prepare-style validation.
- Adapter binding: 64K context/pricing model_info, capability in the
  controller environment, rejection of caller overrides.
- The generic metered proxy under the Tinker provider profile: native model
  rewrite (including checkpoints), ``reasoning_effort``/``top_p`` passthrough,
  pinned table pricing in the frozen ledger, and the Z.ai glm-5.3 price row.
    - Credential gating for the Tinker route.
    - Harness-tree opt-in ``trajectory_config`` (exactly ``raw_content`` and
      ``linear_history``) passed through to the native Terminus 2 kwarg.
    - Main's MiMo answer-leak blocklist setup step running unchanged on the
      Tinker branch, plus raw-content ATIF projection through ingestion readers.

Everything runs against scripted loopback fakes: no paid provider calls.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from evallab import runner as runner_module
from evallab.credentials import (
    TINKER_API_CREDENTIAL,
    missing_credential_for,
)
from evallab.execution_contracts import (
    TERMINUS_PROXY_URL_ENV,
    TINKER_PROXY_CAPABILITY_ENV,
    ProxyTrialLimits,
    RunRequest,
    is_tinker_terminus_model,
    parse_tinker_model,
    validate_request,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

SECRET_SENTINEL = "test-tinker-provider-key-13579"
CAPABILITY_SENTINEL = "test-tinker-capability-token-xyz"
UPSTREAM_USAGE = {"prompt_tokens": 10, "completion_tokens": 5}

TINKER_BASE = "Qwen/Qwen3.6-35B-A3B"
TINKER_BASE_SELECTOR = f"tinker/{TINKER_BASE}"
CHECKPOINT = "tinker://run-abc-123:train:0/sampler_weights/1000"
CHECKPOINT_SELECTOR = f"{TINKER_BASE_SELECTOR}@{CHECKPOINT}"

@pytest.mark.parametrize(
    ("selector", "base", "checkpoint", "rates"),
    [
        (TINKER_BASE_SELECTOR, TINKER_BASE, False, (540_000, 1_335_000)),
        ("tinker/Qwen/Qwen3.8-27B", "Qwen/Qwen3.8-27B", False, (1_860_000, 5_595_000)),
        ("tinker/Qwen/Qwen3.5-9B", "Qwen/Qwen3.5-9B", False, (660_000, 1_995_000)),
        (CHECKPOINT_SELECTOR, TINKER_BASE, True, (540_000, 1_335_000)),
    ],
)
def test_parse_tinker_model_accepts_admitted_selectors(
    selector: str, base: str, checkpoint: bool, rates: tuple[int, int]
) -> None:
    spec = parse_tinker_model(selector)
    assert spec.base_model == base
    assert spec.is_checkpoint is checkpoint
    if checkpoint:
        assert spec.native_model == CHECKPOINT
    else:
        assert spec.native_model == base
    # Pinned list rates (USD micros per 1M tokens).
    assert (
        spec.input_cost_micros_per_million,
        spec.output_cost_micros_per_million,
    ) == rates


@pytest.mark.parametrize(
    "selector",
    [
        "tinker/Qwen/Qwen3.6-35B",  # unknown base
        "tinker/Qwen/Qwen3.6-35B-A3B@tinker://run:train:0/sampler_weights/1/x",  # junk suffix
        "tinker/Qwen/Qwen3.6-35B-A3B@tinker://run:train:x/sampler_weights/1",  # non-numeric train idx
        "tinker/Qwen/Qwen3.6-35B-A3B@https://evil.example/w",  # not a tinker:// checkpoint
        "tinker/",  # empty base
        "tinker/Qwen/Qwen3.6-35B-A3B@",  # empty checkpoint
        "tinker/Qwen/Qwen3.6-35B-A3B@tinker://run:train:0/sampler_weights/1 extra",  # whitespace
        "openai/gpt-4",
        "zai/glm-5.3-flash",
    ],
)
def test_parse_tinker_model_rejects_unknown_or_malformed(selector: str) -> None:
    with pytest.raises(ValueError):
        parse_tinker_model(selector)


def test_is_tinker_terminus_model_prefix_check() -> None:
    assert is_tinker_terminus_model(TINKER_BASE_SELECTOR)
    assert not is_tinker_terminus_model("zai/glm-5.3-flash")
    assert not is_tinker_terminus_model(None)


def _terminus_request(tmp_path: Path, model: str, **overrides: Any) -> RunRequest:
    task_dir = tmp_path / "task"
    task_dir.mkdir(exist_ok=True)
    (task_dir / "task.toml").write_text("[agent]\ntimeout_sec = 60\n")
    values: dict[str, Any] = {
        "task": task_dir,
        "agent": "terminus-2",
        "model": model,
        "name": "tinker-run",
        "jobs_dir": tmp_path / "jobs",
        "attempts": 1,
        "allow_billable": True,
        "timeout_seconds": 60,
        "max_requests": 10,
        "max_input_tokens": 1000,
        "max_output_tokens": 1000,
        "max_total_tokens": 2000,
        "cost_limit_usd": 1.0,
    }
    values.update(overrides)
    return RunRequest(**values)


def test_validate_request_accepts_tinker_base_and_checkpoint(tmp_path: Path) -> None:
    for selector in (TINKER_BASE_SELECTOR, CHECKPOINT_SELECTOR):
        validate_request(_terminus_request(tmp_path, selector))


def test_validate_request_rejects_tinker_without_ceilings(tmp_path: Path) -> None:
    request = _terminus_request(
        tmp_path,
        TINKER_BASE_SELECTOR,
        max_requests=None,
        max_input_tokens=None,
        max_output_tokens=None,
        max_total_tokens=None,
    )
    with pytest.raises(ValueError, match="requires every provider ceiling"):
        validate_request(request)


def test_validate_request_rejects_unknown_tinker_base(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        validate_request(_terminus_request(tmp_path, "tinker/Qwen/Qwen9-99B"))

def test_validate_request_rejects_non_exact_terminus_model(tmp_path: Path) -> None:
    # Any non-admitted model fails closed: either as an unenforceable
    # metered route (ceilings present) or as a non-exact selector.
    with pytest.raises(ValueError):
        validate_request(_terminus_request(tmp_path, "zai/glm-5.3-pro"))


# ---------------------------------------------------------------------------
# 2. Adapter binding (Harbor surface stubbed; adapter logic is real)
# ---------------------------------------------------------------------------


class _FakeTerminus2Options:
    """Upstream Terminus2Options stand-in (Harbor 0.24 options_model base)."""


class _FakeTerminus2:
    """Upstream stand-in recording transport and running a no-op episode."""

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
        self.api_base = api_base
        self._llm_kwargs = llm_kwargs
        self._llm_call_kwargs = dict(llm_call_kwargs) if llm_call_kwargs else {}
        self._extra_env = dict(extra_env) if extra_env else {}
        self.extra_kwargs = kwargs
        self.ran = False
        self.setup_ran = False
        self.logger = logging.getLogger("fake-terminus2")

    async def setup(self, environment: Any) -> None:
        del environment
        self.setup_ran = True

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        del instruction, environment
        self.ran = True
        context.metadata = {"upstream": "ran"}

    def populate_context_post_run(self, context: Any) -> None:
        del context


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
def tinker_transport(monkeypatch: pytest.MonkeyPatch, terminus_module: Any) -> Any:
    monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(TINKER_PROXY_CAPABILITY_ENV, CAPABILITY_SENTINEL)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-original-openai-key")
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    return terminus_module


def test_adapter_binds_tinker_context_and_capability(
    tinker_transport: Any, tmp_path: Path
) -> None:
    agent = tinker_transport.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name=CHECKPOINT_SELECTOR
    )
    assert agent.model_name == CHECKPOINT_SELECTOR
    assert agent.api_base == "http://127.0.0.1:9"
    model_info = agent.extra_kwargs["model_info"]
    assert model_info["max_input_tokens"] == 65_536
    assert model_info["max_output_tokens"] == 65_536
    assert model_info["input_cost_per_token"] == pytest.approx(0.54e-6)
    assert model_info["output_cost_per_token"] == pytest.approx(1.335e-6)
    # The litellm openai-compatible lookup receives the capability only in
    assert agent.api_base == "http://127.0.0.1:9"
    assert os.environ["OPENAI_API_KEY"] == CAPABILITY_SENTINEL


def test_adapter_rejects_tinker_model_info_override(
    tinker_transport: Any, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="runtime-bound"):
        tinker_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name=TINKER_BASE_SELECTOR,
            model_info={"max_input_tokens": 1},
        )


def test_adapter_rejects_transport_overrides_on_tinker(
    tinker_transport: Any, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="api_base"):
        tinker_transport.SecretSafeTerminus2(
            logs_dir=tmp_path, model_name=TINKER_BASE_SELECTOR, api_base="http://x:1"
        )
    with pytest.raises(ValueError, match="transport override"):
        tinker_transport.SecretSafeTerminus2(
            logs_dir=tmp_path,
            model_name=TINKER_BASE_SELECTOR,
            llm_call_kwargs={"OPENAI_API_KEY": "nope"},
        )


# ---------------------------------------------------------------------------
# 3. trajectory_config harness knob (SFT export: raw content, linear segments)
# ---------------------------------------------------------------------------


def _tree(tmp_path: Path, config: dict[str, Any]) -> Path:
    from evallab.terminus_harness import CONFIG_PATH

    digest = sum(ord(ch) for ch in json.dumps(config, sort_keys=True))
    root = tmp_path / f"tree-{digest}"
    (root / CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / CONFIG_PATH).write_text(json.dumps(config))
    return root


def test_harness_knob_accepts_trajectory_config(tmp_path: Path) -> None:
    from evallab.terminus_harness import load_harness_tree

    for config in (
        {"trajectory_config": {"raw_content": True, "linear_history": True}},
        {"trajectory_config": {"raw_content": True}},
        {"trajectory_config": {"linear_history": False}},
        {"trajectory_config": {}},
    ):
        loaded = load_harness_tree(_tree(tmp_path, config))
        assert loaded.config["trajectory_config"] == config["trajectory_config"]
    # The knob is covered by the harness-tree digest: different values pin
    # different trees.
    first = load_harness_tree(
        _tree(tmp_path, {"trajectory_config": {"raw_content": True}})
    )
    second = load_harness_tree(
        _tree(tmp_path, {"trajectory_config": {"raw_content": False}})
    )
    assert first.sha256 != second.sha256


@pytest.mark.parametrize(
    "config",
    [
        {"trajectory_config": {"store_all_messages": True}},
        {"trajectory_config": {"raw_content": True, "bogus": False}},
        {"trajectory_config": ["raw_content"]},
        {"trajectory_config": {"raw_content": 1}},
        {"trajectory_config": {"linear_history": "yes"}},
        {"trajectory_config": {"raw_content": None}},
    ],
)
def test_harness_knob_rejects_trajectory_config(
    tmp_path: Path, config: dict[str, Any]
) -> None:
    from evallab.terminus_harness import load_harness_tree

    with pytest.raises(ValueError, match="trajectory_config"):
        load_harness_tree(_tree(tmp_path, config))


def test_trajectory_config_reaches_native_kwargs(tmp_path: Path) -> None:
    from evallab.execution_contracts import terminus_agent_kwargs
    from evallab.terminus_harness import load_harness_tree

    value = {"raw_content": True, "linear_history": True}
    root = _tree(tmp_path, {"trajectory_config": value})
    tree = load_harness_tree(root)
    request = _terminus_request(
        tmp_path,
        TINKER_BASE_SELECTOR,
        harness_tree_path=root,
        harness_tree_sha256=tree.sha256,
    )
    assert terminus_agent_kwargs(request)["trajectory_config"] == value


def test_adapter_forwards_trajectory_config_to_native_terminus(
    tinker_transport: Any, tmp_path: Path
) -> None:
    agent = tinker_transport.SecretSafeTerminus2(
        logs_dir=tmp_path,
        model_name=TINKER_BASE_SELECTOR,
        trajectory_config={"raw_content": True, "linear_history": False},
    )
    assert agent.extra_kwargs["trajectory_config"] == {
        "raw_content": True,
        "linear_history": False,
    }


@dataclass
class _BlocklistExecResult:
    stdout: str
    stderr: str
    return_code: int


class _BlocklistEnvironment:
    """Environment stand-in answering the answer-leak blocklist probe."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def exec(self, command: str, user: str | None = None) -> _BlocklistExecResult:
        self.calls.append({"command": command, "user": user})
        return _BlocklistExecResult(stdout="12\n", stderr="", return_code=0)


def test_setup_applies_mimo_blocklist_on_tinker_route(
    tinker_transport: Any, tmp_path: Path
) -> None:
    """Main's answer-leak blocklist step is route-agnostic: it runs on Tinker."""
    agent = tinker_transport.SecretSafeTerminus2(
        logs_dir=tmp_path, model_name=TINKER_BASE_SELECTOR
    )
    environment = _BlocklistEnvironment()
    asyncio.run(agent.setup(environment))
    assert agent.setup_ran is True  # type: ignore[attr-defined]
    assert len(environment.calls) == 1
    assert environment.calls[0]["user"] == "root"
    assert "/var/lib/mimo/blocklist" in environment.calls[0]["command"]


# A raw-content ATIF document: assistant steps carry the unparsed LLM text
# and no tool_calls key.
_RAW_DOCUMENT = {
    "schema_version": "ATIF-v1.7",
    "session_id": "raw-session",
    "agent": {
        "name": "terminus-2",
        "version": "2.0.0",
        "model_name": "tinker/Qwen/Qwen3.6-35B-A3B",
    },
    "steps": [
        {
            "step_id": 1,
            "timestamp": "2026-09-28T12:00:00+00:00",
            "source": "user",
            "message": "summarize the repository",
        },
        {
            "step_id": 2,
            "timestamp": "2026-09-28T12:00:05+00:00",
            "source": "agent",
            "model_name": "Qwen/Qwen3.6-35B-A3B",
            # Raw unparsed LLM text (fenced tool call, never parsed into
            # step tool_calls): this is what raw_content=True retains.
            "message": (
                '```json\n{"analysis": "list files", '
                '"plan": "run ls", "tool_calls": [{"name": "bash", '
                '"arguments": {"command": "ls"}}]}\n```'
            ),
            "metrics": {"prompt_tokens": 120, "completion_tokens": 60},
        },
    ],
    "final_metrics": {
        "total_prompt_tokens": 120,
        "total_completion_tokens": 60,
        "total_cached_tokens": 0,
    },
}


def _raw_content_job(root: Path) -> Path:
    job = root / "job-raw"
    trial_dir = job / "trial-raw"
    (trial_dir / "agent").mkdir(parents=True)
    (job / "result.json").write_text(
        json.dumps(
            {
                "id": "job-raw",
                "finished_at": "2026-09-28T12:30:00+00:00",
                "n_total_trials": 1,
                "stats": {"n_completed_trials": 1, "n_errored_trials": 0},
            }
        )
    )
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "id": "trial-raw",
                "trial_name": "trial-raw",
                "task_name": "raw-content-fixture-task",
            }
        )
    )
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps(_RAW_DOCUMENT))
    return job


def test_raw_content_trajectory_projects_and_outlines(tmp_path: Path) -> None:
    """Ingestion consumers complete on raw-content steps without tool_calls."""
    from evallab.evidence.atif import project_trial
    from evallab.results import load_job
    from evallab.traj import outline_trajectory

    job_dir = _raw_content_job(tmp_path)
    job = load_job(job_dir)
    trial = job.trials[0]
    projection = project_trial(job, trial)
    assert {t.validation_status for t in projection.trajectories} == {"valid"}
    assert projection.tool_calls == ()
    outline = outline_trajectory(trial.path, repo_root=tmp_path)
    assert outline.status == "featured"
    assert len(outline.steps) == 2
    # The unparsed fenced tool call stays message text, never a tool call.
    assert "```json" in (outline.steps[1].thought_snippet or "")


# ---------------------------------------------------------------------------
# 4. Generic metered proxy: Tinker provider profile
# ---------------------------------------------------------------------------


class _TinkerUpstream(BaseHTTPRequestHandler):
    """Local stand-in for the pinned Tinker chat-completions endpoint."""

    protocol_version = "HTTP/1.1"
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/services/tinker-prod/oai/api/v1/chat/completions":
            self._reply(404, b"nope")
            return
        if self.headers.get("Authorization") != f"Bearer {SECRET_SENTINEL}":
            self._reply(401, b"nope")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        type(self).seen.append(body)
        response = {
            "id": "cmpl-local",
            "object": "chat.completion",
            "model": body.get("model"),
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": dict(UPSTREAM_USAGE),
        }
        self._reply(200, json.dumps(response).encode(), content_type="application/json; charset=utf-8")

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
def tinker_upstream() -> Any:
    _TinkerUpstream.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TinkerUpstream)
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


def _launch_tinker_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tinker_upstream: ThreadingHTTPServer,
    capability: str,
    limits: ProxyTrialLimits,
    expected_base: str = TINKER_BASE,
    attempt_id: str = "tinker-test-attempt",
) -> tuple[subprocess.Popen[bytes], str, Path]:
    secret_file = tmp_path / "provider-key"
    secret_file.write_text(f"{SECRET_SENTINEL}\n")
    secret_file.chmod(0o600)
    usage_path = tmp_path / "usage.json"
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    host, port = tinker_upstream.server_address
    monkeypatch.setenv("EVALLAB_TINKER_UPSTREAM", f"http://{host}:{port}")
    spec = parse_tinker_model(f"tinker/{expected_base}")
    process, url = runner_module._start_terminus_proxy(
        provider="tinker",
        secret_path=secret_file,
        capability=capability,
        attempt_id=attempt_id,
        usage_path=usage_path,
        limits=limits,
        timeout_seconds=60.0,
        work_dir=work_dir,
        tinker_spec=spec,
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


def test_tinker_proxy_meters_checkpoint_call_with_table_pricing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tinker_upstream: Any
) -> None:
    limits = _proxy_limits()
    process, url, usage_path = _launch_tinker_proxy(
        tmp_path, monkeypatch, tinker_upstream, CAPABILITY_SENTINEL, limits
    )
    try:
        status, body = _post(
            f"{url}/services/tinker-prod/oai/api/v1/chat/completions",
            {
                "model": CHECKPOINT_SELECTOR,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 100,
                "reasoning_effort": "none",
                "top_p": 0.95,
                "top_k": 20,
            },
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 200, body
        # The proxy rewrote the selector to the native checkpoint id and
        # forwarded the harness protocol's sampling fields verbatim; fields
        # outside the Tinker profile (top_k) never reach the upstream.
        assert len(_TinkerUpstream.seen) == 1
        forwarded = _TinkerUpstream.seen[0]
        assert forwarded["model"] == CHECKPOINT
        assert forwarded["reasoning_effort"] == "none"
        assert forwarded["top_p"] == 0.95
        assert "top_k" not in forwarded

        usage = json.loads(usage_path.read_text())
        assert usage["calls"][0]["requested_model"] == CHECKPOINT_SELECTOR
        # Pinned list rates, frozen at the first reservation: every input
        # token is priced as uncached prefill.
        assert usage["pricing"] == {
            "input_cost_micros_per_million": 540_000,
            "output_cost_micros_per_million": 1_335_000,
        }
        expected_cost = (
            UPSTREAM_USAGE["prompt_tokens"] * 540_000
            + UPSTREAM_USAGE["completion_tokens"] * 1_335_000
            + 999_999
        ) // 1_000_000
        assert usage["totals"]["cost_micros"] == expected_cost
    finally:
        process.terminate()
        process.wait(10)


def test_tinker_proxy_rejects_wrong_base_and_unknown_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tinker_upstream: Any
) -> None:
    process, url, usage_path = _launch_tinker_proxy(
        tmp_path, monkeypatch, tinker_upstream, CAPABILITY_SENTINEL, _proxy_limits()
    )
    try:
        base, _ = url.rsplit(":", 1)
        endpoint = f"{url}/services/tinker-prod/oai/api/v1/chat/completions"
        # A different (still priced) base must not ride another trial's base.
        status, _ = _post(
            endpoint,
            {"model": "tinker/Qwen/Qwen3.8-27B", "messages": []},
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 403
        status, _ = _post(
            endpoint,
            {"model": "tinker/Qwen/Qwen3.6-35B-A3B@tinker://run:train:0/sampler_weights/1junk", "messages": []},
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 403
        usage = json.loads(usage_path.read_text())
        assert usage["calls"] == []
        # Zero-call ledger: no pricing is invented without a physical call.
        assert usage["pricing"] is None
    finally:
        process.terminate()
        process.wait(10)


def test_tinker_proxy_enforces_cost_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tinker_upstream: Any
) -> None:
    # 100k micros covers well under one priced call.
    limits = _proxy_limits(max_cost_micros=1)
    process, url, usage_path = _launch_tinker_proxy(
        tmp_path, monkeypatch, tinker_upstream, CAPABILITY_SENTINEL, limits
    )
    try:
        status, body = _post(
            f"{url}/services/tinker-prod/oai/api/v1/chat/completions",
            {"model": TINKER_BASE_SELECTOR, "messages": [{"role": "user", "content": "hi"}]},
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 429
        assert b"trial budget exhausted" in body
        usage = json.loads(usage_path.read_text())
        assert usage["totals"]["requests"] == 0
    finally:
        process.terminate()
        process.wait(10)


def test_zai_glm_53_price_row_is_pinned() -> None:
    from evallab.execution_contracts import zai_openapi_price_for

    assert zai_openapi_price_for("zai/glm-5.3") == (1_400_000, 4_400_000)
    assert zai_openapi_price_for("glm-5.3-flash") == (150_000, 500_000)
    with pytest.raises(ValueError):
        zai_openapi_price_for("glm-5.3-pro")


# ---------------------------------------------------------------------------
# 5. Credential gating
# ---------------------------------------------------------------------------


def test_tinker_route_requires_tinker_credential() -> None:
    empty = frozenset[str]()
    assert missing_credential_for("terminus-2", empty, TINKER_BASE_SELECTOR) == (
        TINKER_API_CREDENTIAL
    )
    assert missing_credential_for("terminus-2", empty, CHECKPOINT_SELECTOR) == (
        TINKER_API_CREDENTIAL
    )
    present = frozenset({TINKER_API_CREDENTIAL})
    assert missing_credential_for("terminus-2", present, CHECKPOINT_SELECTOR) is None


# ---------------------------------------------------------------------------
# 6. Scripted loopback smoke: real proxy subprocess + SecretSafeTerminus2
# ---------------------------------------------------------------------------


def test_loopback_smoke_through_real_proxy_and_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tinker_upstream: Any, terminus_module: Any
) -> None:
    """One episode-shaped call through every real transport component.

    The metered proxy runs as a real subprocess against the scripted
    upstream; the adapter binds to the proxy's loopback endpoint and
    authenticates with the trial capability. Only Terminus2's own upstream
    agent loop (unrelated to transport) is the fake.
    """
    monkeypatch.setenv(TINKER_PROXY_CAPABILITY_ENV, CAPABILITY_SENTINEL)
    limits = _proxy_limits()
    process, url, usage_path = _launch_tinker_proxy(
        tmp_path, monkeypatch, tinker_upstream, CAPABILITY_SENTINEL, limits
    )
    monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, url)
    try:
        agent = terminus_module.SecretSafeTerminus2(
            logs_dir=tmp_path, model_name=TINKER_BASE_SELECTOR
        )
        # Issue the model call the way the harness would: through the bound
        # proxy URL with the capability credential.
        status, body = _post(
            f"{url}/services/tinker-prod/oai/api/v1/chat/completions",
            {
                "model": TINKER_BASE_SELECTOR,
                "messages": [{"role": "user", "content": "solve the task"}],
                "max_tokens": 512,
                "reasoning_effort": False,
                "tools": [],
            },
            capability=CAPABILITY_SENTINEL,
        )
        assert status == 200, body
        response = json.loads(body)
        assert response["model"] == TINKER_BASE
        usage = json.loads(usage_path.read_text())
        assert usage["totals"]["requests"] == 1
        assert usage["calls"][0]["state"] == "reconciled"
        assert _TinkerUpstream.seen[-1]["model"] == TINKER_BASE
        assert agent.model_name == TINKER_BASE_SELECTOR
    finally:
        process.terminate()
        process.wait(10)


# ---------------------------------------------------------------------------
# 7. Capture chain: tinker secret proxy -> capture -> upstream (HAR-82 lane)
# ---------------------------------------------------------------------------


class _TinkerChainStub(BaseHTTPRequestHandler):
    """Stub Tinker endpoint answering behind the capture proxy."""

    protocol_version = "HTTP/1.1"
    received: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def _reply(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self) -> None:  # noqa: N802
        assert self.path == "/services/tinker-prod/oai/api/v1/chat/completions"
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length)) if length else {}
        type(self).received.append(
            {
                "auth": self.headers.get("Authorization"),
                "model": payload.get("model"),
                "reasoning_effort": payload.get("reasoning_effort"),
            }
        )
        self._reply(
            json.dumps(
                {
                    "id": "cmpl-tinker",
                    "object": "chat.completion",
                    "model": payload.get("model"),
                    "choices": [
                        {"message": {"role": "assistant", "content": "done"}}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ).encode()
        )


def test_tinker_secret_proxy_chain_records_chat_and_scrubs_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same HAR-82 placement as the z.ai lane: proxy -> capture -> upstream."""
    import importlib.util

    from evallab.model_capture import serve_capture

    _TinkerChainStub.received = []
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _TinkerChainStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    capture, recorder, _manifest = serve_capture(
        upstream=f"http://127.0.0.1:{stub.server_address[1]}",
        out_dir=tmp_path / "cap",
        bind="127.0.0.1",
        port=0,
    )
    capture_thread = threading.Thread(target=capture.serve_forever, daemon=True)
    capture_thread.start()
    provider_key = "tinker-provider-key-sentinel-24680"
    secret_file = tmp_path / "tinker-key"
    secret_file.write_text(provider_key + "\n")
    secret_file.chmod(0o600)
    capability = "tinker-chain-capability-token"
    monkeypatch.setenv("EVALLAB_PROXY_PROVIDER", "tinker")
    monkeypatch.setenv("EVALLAB_TINKER_SECRET_PATH", str(secret_file))
    monkeypatch.setenv(
        "EVALLAB_TINKER_UPSTREAM", f"http://127.0.0.1:{capture.server_address[1]}"
    )
    monkeypatch.setenv("EVALLAB_TINKER_PROXY_CAPABILITY", capability)
    monkeypatch.setenv("EVALLAB_TINKER_ATTEMPT_ID", "trial-01")
    monkeypatch.setenv("EVALLAB_TINKER_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("EVALLAB_TINKER_EXPECTED_BASE", TINKER_BASE)
    monkeypatch.setenv("EVALLAB_TINKER_MAX_REQUESTS", "5")
    monkeypatch.setenv("EVALLAB_TINKER_MAX_INPUT_TOKENS", "10000")
    monkeypatch.setenv("EVALLAB_TINKER_MAX_OUTPUT_TOKENS", "10000")
    monkeypatch.setenv("EVALLAB_TINKER_MAX_TOTAL_TOKENS", "20000")
    monkeypatch.setenv("EVALLAB_TINKER_MAX_COST_MICROS", "1000000")
    source = REPO_ROOT / "containers" / "zai_openapi_secret_proxy.py"
    spec = importlib.util.spec_from_file_location("chain_tinker_secret_proxy", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    proxy = module.serve(host="127.0.0.1", port=0)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_address[1]}"
            "/services/tinker-prod/oai/api/v1/chat/completions",
            data=json.dumps(
                {
                    "model": CHECKPOINT_SELECTOR,
                    "messages": [{"role": "user", "content": "hi"}],
                    "reasoning_effort": False,
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Evallab-Proxy-Capability": capability,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.status == 200
            body = json.loads(response.read())
            assert body["choices"][0]["message"]["content"] == "done"
    finally:
        proxy.shutdown()
        capture.shutdown()
        stub.shutdown()
        recorder.close()
    assert _TinkerChainStub.received
    seen = _TinkerChainStub.received[0]
    assert seen["auth"] == f"Bearer {provider_key}"
    assert seen["model"] == CHECKPOINT
    assert seen["reasoning_effort"] is False
    records = [
        json.loads(line)
        for line in (tmp_path / "cap" / "calls.jsonl").read_text().splitlines()
    ]
    assert len(records) == 1
    record = records[0]
    assert record["request_body"]["model"] == CHECKPOINT
    assert record["request_body"]["reasoning_effort"] is False
    assert record["assistant_texts"] == ["done"]
    assert "authorization" not in {k.lower() for k in record["request_headers"]}
    assert provider_key not in json.dumps(record)
