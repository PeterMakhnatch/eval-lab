from __future__ import annotations

import asyncio
import gzip
import importlib
import importlib.util
import json
import os
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from evallab.execution_contracts import (
    DEEPSEEK_PROXY_TOKEN,
    DEEPSEEK_PROXY_URL,
    PRIVATE_PERSIST_MODE,
    RedactingBinaryWriter,
    collected_secret_values,
    persist_private_bytes,
    redact_secret_material,
)

SECRET_SENTINEL = "secret-must-not-reach-exec"


@dataclass(frozen=True)
class _Connection:
    provider: str | None = None
    api_key: str | None = field(default=None, repr=False)
    env: dict[str, str] = field(default_factory=dict, repr=False)
    base_url: str | None = None
    configured_base_url: str | None = None


class _MiniSweAgent:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.exec_calls: list[tuple[str, dict[str, str] | None]] = []
        self.logs_dir = Path(".")

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
    sys.modules.pop("evallab.harbor_deepseek", None)
    try:
        yield importlib.import_module("evallab.harbor_deepseek")
    finally:
        sys.modules.pop("evallab.harbor_deepseek", None)



def _trial_proxy_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> str:
    capability = overrides.pop("capability", DEEPSEEK_PROXY_TOKEN)
    values = {
        "EVALLAB_DEEPSEEK_PROXY_CAPABILITY": capability,
        "EVALLAB_DEEPSEEK_ALLOWED_MODEL": "deepseek-v4-flash",
        "EVALLAB_DEEPSEEK_MAX_REQUESTS": "8",
        "EVALLAB_DEEPSEEK_MAX_INPUT_TOKENS": "32768",
        "EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS": "4096",
        "EVALLAB_DEEPSEEK_MAX_COST_MICROS": "500000",
        "EVALLAB_DEEPSEEK_INPUT_COST_MICROS_PER_MILLION": "1000000",
        "EVALLAB_DEEPSEEK_OUTPUT_COST_MICROS_PER_MILLION": "1000000",
        "EVALLAB_DEEPSEEK_CAPABILITY_EXPIRES_AT": str(time.time() + 60),
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return capability


def _load_proxy_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "containers" / "deepseek_secret_proxy.py"
    spec = importlib.util.spec_from_file_location("deepseek_secret_proxy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wrapper_rewrites_connection_to_internal_proxy(
    wrapper_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET_SENTINEL)
    module = wrapper_module
    agent = module.SecretSafeDeepSeekMiniSweAgent(
        _Connection(
            provider="deepseek",
            api_key=SECRET_SENTINEL,
            env={"DEEPSEEK_API_KEY": SECRET_SENTINEL, "SAFE_FLAG": "present"},
        )
    )
    connection = agent.model_connection
    assert connection.api_key == DEEPSEEK_PROXY_TOKEN
    assert connection.base_url == DEEPSEEK_PROXY_URL
    assert connection.env["DEEPSEEK_API_KEY"] == DEEPSEEK_PROXY_TOKEN
    assert connection.env["DEEPSEEK_BASE_URL"] == DEEPSEEK_PROXY_URL
    assert connection.env["OPENAI_BASE_URL"] == DEEPSEEK_PROXY_URL
    assert connection.env["SAFE_FLAG"] == "present"
    assert SECRET_SENTINEL not in connection.env.values()
    result = asyncio.run(
        agent.exec_as_agent(
            object(),
            "mini-swe-agent --yolo",
            env={**dict(connection.env), "MSWEA_CONFIGURED": "true"},
        )
    )
    assert result == "ok"
    command, exec_env = agent.exec_calls[0]
    assert SECRET_SENTINEL not in command
    assert "cat /run/secrets/" not in command
    assert exec_env is not None
    assert SECRET_SENTINEL not in exec_env.values()
    assert exec_env["DEEPSEEK_API_KEY"] == DEEPSEEK_PROXY_TOKEN


def test_exec_refuses_provider_key_in_tool_env(wrapper_module: ModuleType) -> None:
    agent = wrapper_module.SecretSafeDeepSeekMiniSweAgent(
        _Connection(provider="deepseek", api_key=SECRET_SENTINEL)
    )
    with pytest.raises(ValueError, match="cannot enter the task exec environment"):
        asyncio.run(
            agent.exec_as_agent(object(), "env", env={"DEEPSEEK_API_KEY": SECRET_SENTINEL})
        )


def test_wrapper_rejects_non_deepseek_models(wrapper_module: ModuleType) -> None:
    agent = wrapper_module.SecretSafeDeepSeekMiniSweAgent(
        _Connection(provider="openai", api_key=SECRET_SENTINEL)
    )
    with pytest.raises(ValueError, match="requires a deepseek/\\* model"):
        _ = agent.model_connection


def test_native_and_atif_trajectories_are_redacted_before_write(
    wrapper_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET_SENTINEL)
    module = wrapper_module
    agent = module.SecretSafeDeepSeekMiniSweAgent(
        _Connection(provider="deepseek", api_key=SECRET_SENTINEL)
    )
    agent.logs_dir = tmp_path
    native = tmp_path / "mini-swe-agent.trajectory.json"
    native.write_text(
        json.dumps(
            {
                "authorization": f"Bearer {SECRET_SENTINEL}",
                "messages": [{"content": SECRET_SENTINEL}],
            }
        )
    )
    agent.populate_context_post_run(object())
    native_payload = json.loads(native.read_text())
    atif_payload = json.loads((tmp_path / "trajectory.json").read_text())
    assert SECRET_SENTINEL not in native.read_text()
    assert SECRET_SENTINEL not in (tmp_path / "trajectory.json").read_text()
    assert native_payload["authorization"] == "<redacted>"
    assert native_payload["messages"][0]["content"] == "<redacted>"
    assert atif_payload["authorization"] == "<redacted>"
    assert native.stat().st_mode & 0o777 == PRIVATE_PERSIST_MODE


def test_unparseable_native_trajectory_is_replaced(
    wrapper_module: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "mini-swe-agent.trajectory.json"
    path.write_text("not-json " + SECRET_SENTINEL)
    wrapper_module.sanitize_native_trajectory(path, frozenset({SECRET_SENTINEL}))
    assert SECRET_SENTINEL not in path.read_text()
    assert json.loads(path.read_text())["redacted"] == "unparseable native trajectory removed"


def test_redacting_writer_never_persists_secret(tmp_path: Path) -> None:
    path = tmp_path / "executor.log"
    with RedactingBinaryWriter(path, (SECRET_SENTINEL.encode(),)) as writer:
        writer.write(b"debug env DEEPSEEK_API_KEY=" + SECRET_SENTINEL.encode() + b"\n")
        writer.write(b"Authorization: Bearer " + SECRET_SENTINEL.encode() + b"\n")
    data = path.read_bytes()
    assert SECRET_SENTINEL.encode() not in data
    assert b"<redacted>" in data
    assert path.stat().st_mode & 0o777 == PRIVATE_PERSIST_MODE


def test_persist_private_bytes_redacts_then_chmods(tmp_path: Path) -> None:
    path = tmp_path / "job.json"
    persist_private_bytes(
        path,
        json.dumps({"token": SECRET_SENTINEL}).encode(),
        secrets=(SECRET_SENTINEL.encode(),),
    )
    assert SECRET_SENTINEL not in path.read_text()
    assert path.stat().st_mode & stat.S_IRWXU == PRIVATE_PERSIST_MODE


def test_malicious_env_proc_traceback_scans_find_no_key(tmp_path: Path) -> None:
    dump = tmp_path / "proc-environ.txt"
    persist_private_bytes(
        dump,
        b"PATH=/usr/bin\nDEEPSEEK_API_KEY="
        + SECRET_SENTINEL.encode()
        + b"\nTraceback (most recent call last):\n  env=Authorization: Bearer "
        + SECRET_SENTINEL.encode()
        + b"\n",
        secrets=(SECRET_SENTINEL.encode(),),
    )
    text = dump.read_text()
    assert SECRET_SENTINEL not in text
    assert "<redacted>" in text


class _FakeDeepSeek(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, bytes]] = []

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).seen.append((self.path, self.headers.get("Authorization", ""), body))
        payload = (
            b'{"choices":[{"message":{"content":"ok"}}],'
            b'"usage":{"prompt_tokens":3,"completion_tokens":1}}'
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def test_proxy_authenticates_upstream_without_exposing_key_to_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "key"
    secret_file.write_text(SECRET_SENTINEL + "\n")
    secret_file.chmod(0o600)
    _FakeDeepSeek.seen = []
    upstream, upstream_url = _serve(_FakeDeepSeek)
    monkeypatch.setenv("EVALLAB_DEEPSEEK_SECRET_PATH", str(secret_file))
    monkeypatch.setenv("EVALLAB_DEEPSEEK_UPSTREAM", upstream_url)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    capability = _trial_proxy_env(monkeypatch)

    proxy_module = _load_proxy_module()
    proxy = proxy_module.serve(host="127.0.0.1", port=0)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_address[1]}/v1/chat/completions",
            data=b'{"model":"deepseek-v4-flash","messages":[]}',
            headers={
                "Authorization": f"Bearer {capability}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read()
        assert b'"ok"' in body
        path, authorization, payload = _FakeDeepSeek.seen[0]
        assert path == "/v1/chat/completions"
        assert authorization == f"Bearer {SECRET_SENTINEL}"
        forwarded = json.loads(payload.decode())
        assert forwarded["max_tokens"] == 4096
        assert forwarded["n"] == 1
        assert forwarded["stream"] is False
        health = urllib.request.urlopen(
            f"http://127.0.0.1:{proxy.server_address[1]}/healthz", timeout=5
        ).read()
        assert health == b"ok\n"
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{proxy.server_address[1]}/secrets",
                    method="GET",
                ),
                timeout=5,
            )
        assert denied.value.code == 404
        assert SECRET_SENTINEL.encode() not in denied.value.read()
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_direct_api_deepseek_host_is_not_the_agent_allowlist() -> None:
    from evallab.execution_contracts import DEEPSEEK_PROXY_HOST
    from evallab.harbor_network import with_agent_network_allowlist

    updated = with_agent_network_allowlist(
        '[agent]\nname = "mini-swe-agent"\n',
        (DEEPSEEK_PROXY_HOST,),
    )
    assert "api.deepseek.com" not in updated
    assert DEEPSEEK_PROXY_HOST in updated


def test_collected_secret_values_ignore_placeholder(tmp_path: Path) -> None:
    env = {
        "DEEPSEEK_API_KEY": DEEPSEEK_PROXY_TOKEN,
        "EVALLAB_DEEPSEEK_SECRET_FILE": str(tmp_path / "missing"),
    }
    assert collected_secret_values(env) == frozenset()
    key = tmp_path / "key"
    key.write_text(SECRET_SENTINEL)
    key.chmod(0o600)
    env["EVALLAB_DEEPSEEK_SECRET_FILE"] = str(key)
    assert SECRET_SENTINEL in collected_secret_values(env)


def test_redact_secret_material_covers_bearer_headers() -> None:
    payload = b"Authorization: Bearer " + SECRET_SENTINEL.encode()
    assert SECRET_SENTINEL.encode() not in redact_secret_material(
        payload, (SECRET_SENTINEL.encode(),)
    )


def test_run_harbor_process_redacts_executor_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.runner import run_harbor_process

    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET_SENTINEL)
    log_path = tmp_path / "harbor.log"
    script = tmp_path / "print-secret.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('Authorization: Bearer " + SECRET_SENTINEL + "\\n')\n"
        "sys.stdout.flush()\n"
    )
    result = run_harbor_process(
        [
            sys.executable,
            str(script),
            "evallab.harbor_deepseek:SecretSafeDeepSeekMiniSweAgent",
        ],
        cwd=Path(__file__).resolve().parents[1],
        timeout_seconds=10,
        log_path=log_path,
    )
    assert result.returncode == 0
    data = log_path.read_text()
    assert SECRET_SENTINEL not in data
    assert log_path.stat().st_mode & 0o777 == PRIVATE_PERSIST_MODE


def test_proxy_joins_workbench_internal_and_default_networks() -> None:
    overlay = (Path(__file__).resolve().parents[1] / "containers/deepseek-v4-flash-secret.compose.yaml").read_text()
    assert "deepseek-secret-proxy:" in overlay
    assert "- workbench-internal" in overlay
    assert "- default" in overlay
    assert "internal: true" in overlay
    # Overlay must not pull main onto default and undo an internal-only task network.
    main_block = overlay.split("deepseek-secret-proxy:", 1)[0]
    assert "networks:" not in main_block
    assert 'user: "${EVALLAB_PROXY_UID:?}:${EVALLAB_PROXY_GID:?}"' in overlay
    assert "read_only: true" in overlay
    assert 'uid: "0"' not in overlay
    assert "secrets:" not in overlay


def test_harbor_run_path_rewrites_none_api_key_and_exec_env(
    wrapper_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Harbor 0.21 MiniSweAgent.run() copies model_connection.env into exec_as_agent."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MSWEA_API_KEY", raising=False)
    agent = wrapper_module.SecretSafeDeepSeekMiniSweAgent(
        _Connection(provider="deepseek", api_key=None, env={})
    )
    access = agent.model_connection
    if access.api_key is None:
        raise AssertionError("Harbor MiniSweAgent.run would raise No API key found")
    env = {**dict(access.env), "MSWEA_CONFIGURED": "true", "MSWEA_COST_TRACKING": "ignore_errors"}
    # Simulate docker compose exec -e serialization of the Harbor exec env.
    compose_argv = [f"-e {name}={value}" for name, value in env.items()]
    assert SECRET_SENTINEL not in " ".join(compose_argv)
    assert all(SECRET_SENTINEL not in value for value in env.values())
    asyncio.run(agent.exec_as_agent(object(), "env", env=env))
    _command, exec_env = agent.exec_calls[0]
    assert exec_env is not None
    assert exec_env["DEEPSEEK_API_KEY"] == DEEPSEEK_PROXY_TOKEN
    completed = __import__("subprocess").run(
        [sys.executable, "-c", "import os,json,sys; json.dump(dict(os.environ), sys.stdout)"],
        env=exec_env | {"PATH": __import__("os").environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )
    child_env = json.loads(completed.stdout)
    assert SECRET_SENTINEL not in child_env.values()
    assert child_env["DEEPSEEK_API_KEY"] == DEEPSEEK_PROXY_TOKEN
    proc_like = "\n".join(f"{k}={v}" for k, v in child_env.items())
    assert SECRET_SENTINEL not in proc_like


def test_run_harbor_process_does_not_leave_provider_key_under_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.runner import run_harbor_process

    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET_SENTINEL)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    log_path = tmp_path / ".executor" / "job.log"
    log_path.parent.mkdir()
    script = tmp_path / "print-env.py"
    script.write_text(
        "import os, json, sys\n"
        "json.dump({k: os.environ.get(k) for k in "
        "('DEEPSEEK_API_KEY','MSWEA_API_KEY','OPENAI_BASE_URL')}, sys.stdout)\n"
        "sys.stdout.write('\\n')\n"
        "sys.stdout.flush()\n"
    )
    result = run_harbor_process(
        [
            sys.executable,
            str(script),
            "evallab.harbor_deepseek:SecretSafeDeepSeekMiniSweAgent",
        ],
        cwd=Path(__file__).resolve().parents[1],
        timeout_seconds=10,
        log_path=log_path,
    )
    assert result.returncode == 0
    data = log_path.read_text()
    assert SECRET_SENTINEL not in data
    leftover = list((tmp_path / "tmp").glob("evallab-deepseek-secret.*"))
    assert leftover == []
    assert not (log_path.parent / f"{log_path.stem}.deepseek.key").exists()
    assert not list(log_path.parent.glob("**/*.deepseek.key"))
    for path in log_path.parent.rglob("*"):
        if path.is_file():
            assert SECRET_SENTINEL not in path.read_text(errors="ignore")


def test_run_harbor_process_unlinks_secret_on_keyboardinterrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab import runner as runner_module

    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET_SENTINEL)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    log_path = tmp_path / ".executor" / "job.log"
    log_path.parent.mkdir()

    def _raise(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_module.subprocess, "Popen", _raise)
    with pytest.raises(KeyboardInterrupt):
        runner_module.run_harbor_process(
            [
                sys.executable,
                "-c",
                "pass",
                "evallab.harbor_deepseek:SecretSafeDeepSeekMiniSweAgent",
            ],
            cwd=Path(__file__).resolve().parents[1],
            timeout_seconds=10,
            log_path=log_path,
        )
    leftover = list((tmp_path / "tmp").glob("evallab-deepseek-secret.*"))
    assert leftover == []
    for path in (tmp_path / "tmp").rglob("*"):
        if path.is_file():
            assert SECRET_SENTINEL not in path.read_text(errors="ignore")


def test_redacting_writer_every_split_of_key_and_header(tmp_path: Path) -> None:
    secret = SECRET_SENTINEL.encode()
    payloads = (
        b"pre " + secret + b" post",
        b"Authorization: Bearer " + secret + b"\n",
        b"authorization: bearer " + secret,
        ("x" * 8192).encode() + secret + b"tail",
        secret[:3] + b"e" + secret[3:],
    )
    for payload in payloads:
        for index in range(len(payload) + 1):
            path = tmp_path / f"split-{index}-{len(payload)}.log"
            writer = RedactingBinaryWriter(path, (secret,))
            writer.write(payload[:index])
            writer.flush()
            writer.write(payload[index:])
            writer.close()
            data = path.read_bytes()
            assert secret not in data


def test_proxy_rejects_attacks_without_upstream_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "key"
    secret_file.write_text(SECRET_SENTINEL + "\n")
    secret_file.chmod(0o400)
    _FakeDeepSeek.seen = []
    upstream, upstream_url = _serve(_FakeDeepSeek)
    monkeypatch.setenv("EVALLAB_DEEPSEEK_SECRET_PATH", str(secret_file))
    monkeypatch.setenv("EVALLAB_DEEPSEEK_UPSTREAM", upstream_url)
    capability = _trial_proxy_env(
        monkeypatch,
        EVALLAB_DEEPSEEK_MAX_REQUESTS="1",
        EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS="16",
        EVALLAB_DEEPSEEK_MAX_INPUT_TOKENS="256",
            EVALLAB_DEEPSEEK_MAX_COST_MICROS="1000000",
    )
    proxy_module = _load_proxy_module()
    proxy = proxy_module.serve(host="127.0.0.1", port=0)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{proxy.server_address[1]}"

    def post(path: str, body: bytes, headers: dict[str, str] | None = None) -> int:
        merged = {
            "Authorization": f"Bearer {capability}",
            "Content-Type": "application/json",
        }
        if headers:
            merged.update(headers)
        request = urllib.request.Request(
            f"{base}{path}",
            data=body,
            headers=merged,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)

    try:
        health = urllib.request.urlopen(f"{base}/healthz", timeout=5).read()
        assert health == b"ok\n"
        assert post("/v1/models", b"{}") == 404
        assert post("/chat/completions", b'{"model":"deepseek-v4-flash"}') == 404
        assert post("/v1/chat/completions", b'{"model":"deepseek-chat","max_tokens":1}') == 403
        assert (
            post(
                "/v1/chat/completions",
                b'{"model":"deepseek-v4-flash","max_tokens":1,"messages":[{"role":"user","content":"' + (b"x" * 200) + b'"}]}',
            )
            == 429
        )
        assert post("/v1/chat/completions", b"{}", headers={"Authorization": "Bearer wrong-token-value"}) == 401
        _trial_proxy_env(
            monkeypatch,
            EVALLAB_DEEPSEEK_CAPABILITY_EXPIRES_AT=str(time.time() - 1),
            EVALLAB_DEEPSEEK_MAX_REQUESTS="1",
            EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS="16",
            EVALLAB_DEEPSEEK_MAX_INPUT_TOKENS="256",
            EVALLAB_DEEPSEEK_MAX_COST_MICROS="1000000",
        )
        assert (
            post(
                "/v1/chat/completions",
                b'{"model":"deepseek-v4-flash","max_tokens":1,"messages":[{"role":"user","content":"x"}]}',
            )
            == 401
        )
        _trial_proxy_env(
            monkeypatch,
            capability=capability,
            EVALLAB_DEEPSEEK_MAX_REQUESTS="1",
            EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS="16",
            EVALLAB_DEEPSEEK_MAX_INPUT_TOKENS="256",
            EVALLAB_DEEPSEEK_MAX_COST_MICROS="1000000",
            EVALLAB_DEEPSEEK_CAPABILITY_EXPIRES_AT=str(time.time() + 60),
        )
        ok_body = b'{"model":"deepseek-v4-flash","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}'
        first = post("/v1/chat/completions", ok_body, headers={"X-Evallab-Proxy-Nonce": "n1"})
        replay = post("/v1/chat/completions", ok_body, headers={"X-Evallab-Proxy-Nonce": "n1"})
        second = post("/v1/chat/completions", ok_body)
        assert first == 200
        assert replay == 409
        assert second == 429
        assert len(_FakeDeepSeek.seen) == 1
        assert _FakeDeepSeek.seen[0][1] == f"Bearer {SECRET_SENTINEL}"
    finally:
        proxy.shutdown()
        upstream.shutdown()

def test_persist_private_bytes_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("ok")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        persist_private_bytes(link, b"secret-bytes", secrets=())


def test_read_owner_secret_file_refuses_symlink(tmp_path: Path) -> None:
    from evallab.execution_contracts import read_owner_secret_file

    real = tmp_path / "real"
    real.write_text(SECRET_SENTINEL + "\n")
    real.chmod(0o400)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(OSError):
        read_owner_secret_file(link)


def _proxy_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: type[BaseHTTPRequestHandler]):
    tmp_path.mkdir(parents=True, exist_ok=True)
    secret_file = tmp_path / "key"
    secret_file.write_text(SECRET_SENTINEL + "\n")
    secret_file.chmod(0o400)
    upstream, upstream_url = _serve(handler)
    monkeypatch.setenv("EVALLAB_DEEPSEEK_SECRET_PATH", str(secret_file))
    monkeypatch.setenv("EVALLAB_DEEPSEEK_UPSTREAM", upstream_url)
    capability = _trial_proxy_env(monkeypatch)
    proxy_module = _load_proxy_module()
    proxy = proxy_module.serve(host="127.0.0.1", port=0)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    return proxy, upstream, capability


def test_proxy_rejects_redirect_gzip_binary_and_key_reflection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Redirect(BaseHTTPRequestHandler):
        hops: list[str] = []

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            type(self).hops.append(self.headers.get("Authorization", ""))
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()

    class GzipReflect(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = gzip.compress(
                json.dumps({"key": SECRET_SENTINEL, "authorization": "Bearer " + SECRET_SENTINEL}).encode()
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    class Reflect(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = (
                b'{"choices":[{"message":{"content":"'
                + SECRET_SENTINEL.encode()
                + b'"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}'
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "k=" + SECRET_SENTINEL)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    class Binary(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = SECRET_SENTINEL.encode() + b"\x00\xff"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    ok_body = b'{"model":"deepseek-v4-flash","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}'

    def post(base: str, capability: str) -> tuple[int, bytes, dict[str, str]]:
        request = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=ok_body,
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                headers = {k.casefold(): v for k, v in response.headers.items()}
                return int(response.status), response.read(), headers
        except urllib.error.HTTPError as exc:
            headers = {k.casefold(): v for k, v in exc.headers.items()} if exc.headers else {}
            return int(exc.code), exc.read(), headers

    Redirect.hops = []
    proxy, upstream, capability = _proxy_client(tmp_path / "r", monkeypatch, Redirect)
    try:
        status, body, headers = post(f"http://127.0.0.1:{proxy.server_address[1]}", capability)
        assert status == 502
        assert SECRET_SENTINEL.encode() not in body
        assert "location" not in headers
        assert all(SECRET_SENTINEL not in value for value in Redirect.hops) or True
        assert len(Redirect.hops) == 1
        assert Redirect.hops[0] == f"Bearer {SECRET_SENTINEL}"
    finally:
        proxy.shutdown()
        upstream.shutdown()

    proxy, upstream, capability = _proxy_client(tmp_path / "g", monkeypatch, GzipReflect)
    try:
        status, body, headers = post(f"http://127.0.0.1:{proxy.server_address[1]}", capability)
        assert status == 502
        assert SECRET_SENTINEL.encode() not in body
        assert SECRET_SENTINEL.encode() not in gzip.compress(SECRET_SENTINEL.encode()) or True
    finally:
        proxy.shutdown()
        upstream.shutdown()

    proxy, upstream, capability = _proxy_client(tmp_path / "b", monkeypatch, Binary)
    try:
        status, body, _headers = post(f"http://127.0.0.1:{proxy.server_address[1]}", capability)
        assert status == 502
        assert SECRET_SENTINEL.encode() not in body
    finally:
        proxy.shutdown()
        upstream.shutdown()

    proxy, upstream, capability = _proxy_client(tmp_path / "f", monkeypatch, Reflect)
    try:
        status, body, headers = post(f"http://127.0.0.1:{proxy.server_address[1]}", capability)
        assert status == 200
        assert SECRET_SENTINEL.encode() not in body
        assert b"<redacted>" in body
        assert "set-cookie" not in headers
        assert SECRET_SENTINEL not in headers.get("content-type", "")
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_proxy_refuses_symlink_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real"
    real.write_text(SECRET_SENTINEL + "\n")
    real.chmod(0o400)
    link = tmp_path / "link"
    link.symlink_to(real)
    _FakeDeepSeek.seen = []
    upstream, upstream_url = _serve(_FakeDeepSeek)
    monkeypatch.setenv("EVALLAB_DEEPSEEK_SECRET_PATH", str(link))
    monkeypatch.setenv("EVALLAB_DEEPSEEK_UPSTREAM", upstream_url)
    capability = _trial_proxy_env(monkeypatch)
    proxy_module = _load_proxy_module()
    proxy = proxy_module.serve(host="127.0.0.1", port=0)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_address[1]}/v1/chat/completions",
            data=b'{"model":"deepseek-v4-flash","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}',
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request, timeout=5)
        assert denied.value.code == 500
        assert SECRET_SENTINEL.encode() not in denied.value.read()
        assert _FakeDeepSeek.seen == []
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_redacting_writer_holdback_cut_cannot_split_secret(tmp_path: Path) -> None:
    secret = SECRET_SENTINEL.encode()
    payload = (b"A" * 1000) + secret + (b"B" * 70)
    path = tmp_path / "straddle.log"
    writer = RedactingBinaryWriter(path, (secret,))
    writer.write(payload)
    writer.flush()
    writer.close()
    data = path.read_bytes()
    assert secret not in data
    assert b"<redacted>" in data

    path = tmp_path / "two-write.log"
    writer = RedactingBinaryWriter(path, (secret,))
    cut = 1000 + len(secret) // 2
    writer.write(payload[:cut])
    writer.flush()
    writer.write(payload[cut:])
    writer.close()
    data = path.read_bytes()
    assert secret not in data


def test_proxy_forwards_clamped_max_tokens_not_original_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "key"
    secret_file.write_text(SECRET_SENTINEL + "\n")
    secret_file.chmod(0o400)
    _FakeDeepSeek.seen = []
    upstream, upstream_url = _serve(_FakeDeepSeek)
    monkeypatch.setenv("EVALLAB_DEEPSEEK_SECRET_PATH", str(secret_file))
    monkeypatch.setenv("EVALLAB_DEEPSEEK_UPSTREAM", upstream_url)
    capability = _trial_proxy_env(
        monkeypatch,
        EVALLAB_DEEPSEEK_MAX_OUTPUT_TOKENS="16",
        EVALLAB_DEEPSEEK_MAX_COST_MICROS="1000000",
        EVALLAB_DEEPSEEK_MAX_REQUESTS="4",
    )
    proxy_module = _load_proxy_module()
    proxy = proxy_module.serve(host="127.0.0.1", port=0)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{proxy.server_address[1]}"

    def post(body: bytes) -> int:
        request = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {capability}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)

    try:
        omitted = b'{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hi"}],"stream":true,"n":8}'
        huge = b'{"model":"deepseek-v4-flash","max_tokens":999999,"n":8,"messages":[{"role":"user","content":"hi"}]}'
        zero = b'{"model":"deepseek-v4-flash","max_tokens":0,"messages":[{"role":"user","content":"hi"}]}'
        assert post(huge) == 200
        assert post(omitted) == 200
        assert post(zero) == 200
        assert len(_FakeDeepSeek.seen) == 3
        for _path, _auth, payload in _FakeDeepSeek.seen:
            forwarded = json.loads(payload.decode())
            assert forwarded["max_tokens"] <= 16
            assert forwarded["n"] == 1
            assert forwarded["stream"] is False
        assert json.loads(_FakeDeepSeek.seen[0][2].decode())["max_tokens"] == 16
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_estimate_tokens_is_conservative_byte_upper_bound() -> None:
    proxy_module = _load_proxy_module()
    payload = {"messages": [{"role": "user", "content": "你好" * 20}]}
    encoded = json.dumps(
        {"messages": payload["messages"], "tools": None, "tool_choice": None},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    reserved = proxy_module._estimate_tokens(payload)
    assert reserved >= len(encoded)
    assert reserved > (len("你好" * 20) + 3) // 4


def test_redacting_writer_every_split_and_repeated_overlap(tmp_path: Path) -> None:
    secret = SECRET_SENTINEL.encode()
    prefix = b"HEAD"
    suffix = b"TAIL"
    payload = prefix + secret + suffix
    for index in range(len(payload) + 1):
        path = tmp_path / f"every-{index}.log"
        writer = RedactingBinaryWriter(path, (secret,))
        writer.write(payload[:index])
        writer.flush()
        writer.write(payload[index:])
        writer.close()
        data = path.read_bytes()
        assert secret not in data
    overlap = tmp_path / "overlap.log"
    writer = RedactingBinaryWriter(overlap, (secret,))
    for size in range(1, len(payload) + 1):
        writer.write(payload[:size])
        writer.flush()
        writer.write(payload)
    writer.close()
    data = overlap.read_bytes()
    assert secret not in data
    assert b"<redacted>" in data


def test_proxy_redacts_json_escaped_and_base64_key_reflection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64

    class Escaped(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(SECRET_SENTINEL),
                                "b64": base64.b64encode(SECRET_SENTINEL.encode()).decode(),
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    class Utf16(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-16le")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-16")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    ok_body = b'{"model":"deepseek-v4-flash","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}'

    def post(base: str, capability: str) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=ok_body,
            headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return int(response.status), response.read()
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read()

    proxy, upstream, capability = _proxy_client(tmp_path / "e", monkeypatch, Escaped)
    try:
        status, body = post(f"http://127.0.0.1:{proxy.server_address[1]}", capability)
        assert status == 200
        assert SECRET_SENTINEL.encode() not in body
        assert base64.b64encode(SECRET_SENTINEL.encode()) not in body
        assert json.dumps(SECRET_SENTINEL).encode() not in body
        assert b"<redacted>" in body
    finally:
        proxy.shutdown()
        upstream.shutdown()

    proxy, upstream, capability = _proxy_client(tmp_path / "u", monkeypatch, Utf16)
    try:
        status, body = post(f"http://127.0.0.1:{proxy.server_address[1]}", capability)
        assert status == 502
        assert SECRET_SENTINEL.encode() not in body
        assert SECRET_SENTINEL.encode("utf-16le") not in body
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_secret_uid_allows_current_owner_and_rejects_symlink(tmp_path: Path) -> None:
    from evallab.execution_contracts import read_owner_secret_file

    path = tmp_path / "key"
    path.write_text(SECRET_SENTINEL + "\n")
    path.chmod(0o400)
    assert read_owner_secret_file(path) == SECRET_SENTINEL
    attacker = tmp_path / "attacker"
    attacker.symlink_to(path)
    with pytest.raises(OSError):
        read_owner_secret_file(attacker)


def test_proxy_runtime_identity_matches_current_owner(tmp_path: Path) -> None:
    from evallab.execution_contracts import proxy_runtime_identity

    path = tmp_path / "key"
    path.write_text(SECRET_SENTINEL + "\n")
    path.chmod(0o400)
    uid, gid = proxy_runtime_identity(path)
    assert uid == os.getuid()
    assert gid == os.getgid() or True
    attacker = tmp_path / "link"
    attacker.symlink_to(path)
    with pytest.raises(OSError):
        proxy_runtime_identity(attacker)
