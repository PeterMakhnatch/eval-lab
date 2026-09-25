"""Local model qualification must never become a remote call or a weight pull."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from evallab.execution_contracts import RunRequest
from evallab.runner import preflight_request
from evallab.terminus_local import local_ollama_endpoint, resolve_ollama_binding

MODEL = "ollama_chat/qwen2.5:7b"
DIGEST = "a" * 64


@contextmanager
def _inventory(models: list[dict[str, Any]]) -> Iterator[tuple[str, list[str]]]:
    requested: list[str] = []
    payload = json.dumps({"models": models}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requested
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _installed() -> dict[str, Any]:
    return {"name": "qwen2.5:7b", "digest": DIGEST, "size": 1234,
            "details": {"format": "gguf"}}


def test_tagged_local_model_passes_inventory_preflight_without_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _inventory([_installed()]) as (endpoint, requested):
        monkeypatch.setenv("EVALLAB_TERMINUS_OLLAMA_URL", endpoint)
        request = RunRequest(
            task=tmp_path, agent="terminus-2", model=MODEL, name="local-preflight",
            jobs_dir=tmp_path / "runs", environment="docker", allow_billable=True,
        )
        decision = preflight_request(request)
        binding = resolve_ollama_binding(MODEL)
        assert decision.proceed and decision.reason is None
        assert binding.model_digest == "sha256:" + DIGEST
        assert binding.api_cost_usd == 0.0
        assert requested == ["/api/tags", "/api/tags"]
        assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("models", [[], [_installed() | {"remote_host": "https://ollama.com"}]])
def test_uninstalled_and_cloud_models_never_qualify(models: list[dict[str, Any]]) -> None:
    with _inventory(models) as (endpoint, requested):
        with pytest.raises(ValueError):
            resolve_ollama_binding(MODEL, environment={"EVALLAB_TERMINUS_OLLAMA_URL": endpoint})
        assert requested == ["/api/tags"]


@pytest.mark.parametrize("endpoint", [
    "https://api.example.com", "http://localhost:11434", "http://127.0.0.1:11434/api",
    "http://user:password@127.0.0.1:11434", "http://127.0.0.1:11434?remote=true",
])
def test_local_binding_refuses_nonliteral_or_decorated_endpoints(endpoint: str) -> None:
    with pytest.raises(ValueError):
        local_ollama_endpoint({"EVALLAB_TERMINUS_OLLAMA_URL": endpoint})
