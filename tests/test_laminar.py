"""Behavioral tests for evallab.laminar: trace identity and Signals as code."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError

import pytest

from evallab import laminar as laminar_module
from evallab.laminar import (
    LAMINAR_ENDPOINT_ENV,
    LAMINAR_KEY_ENV,
    SIGNALS,
    ZAI_BASE_URL,
    ZAI_KEY_ENV,
    ZAI_PROFILE_NAME,
    ZAI_SIGNAL_MODEL,
    _json_call,
    _laminar_command,
    apply_signals,
    ensure_zai_profile,
)


def test_apply_signals_creates_missing_and_patches_existing() -> None:
    calls: list[tuple[str, str, Any]] = []
    existing = {"signals": [{"id": "s-1", "name": "stuck_loop"}, {"id": "s-9", "name": "other"}]}

    def opener(request: Any, timeout: float) -> Any:
        body = json.loads(request.data) if request.data else None
        path = request.full_url.split("api.lmnr.ai")[1]
        if path == "/v1/sql/query":  # the quarantine guard: no quarantined copy is selectable
            return io.BytesIO(b'{"data": []}')
        calls.append((request.get_method(), path, body))
        reply = (
            existing
            if request.get_method() == "GET"
            else {**(body or {}), "id": "new", "version": 1}
        )
        return io.BytesIO(json.dumps(reply).encode())

    apply_signals(api_key="k", opener=opener)
    writes = {(method, path, (body or {}).get("name")) for method, path, body in calls[1:]}
    assert ("PATCH", "/v1/signals/s-1", "stuck_loop") in writes
    created = {name for method, _, name in writes if method == "POST"}
    assert created == {d["name"] for d in SIGNALS} - {"stuck_loop"}
    expected_filter = {
        "column": "tags", "operator": "includes", "value": ["evallab.signals=on"]
    }
    assert all(body["filters"] == [expected_filter] for _, _, body in calls[1:])
    assert all(body["disabled"] is True for _, _, body in calls[1:]), (
        "applying definitions must not reactivate per-token judges"
    )
    for _, _, body in calls[1:]:
        schema = body["structuredOutput"]
        assert schema["required"] == list(schema["properties"]), (
            "the API rejects schemas without required"
        )



def test_byok_route_is_sent_with_every_signal() -> None:
    bodies: list[Any] = []

    def opener(request: Any, timeout: float) -> Any:
        if request.full_url.endswith("/v1/sql/query"):
            return io.BytesIO(b'{"data": []}')
        if request.get_method() == "GET":
            return io.BytesIO(b'{"signals": []}')
        bodies.append(json.loads(request.data))
        return io.BytesIO(json.dumps({**bodies[-1], "id": "x", "version": 1}).encode())

    apply_signals(api_key="k", opener=opener, llm_profile_id="p-1", model="glm-5.3-flash")
    assert len(bodies) == len(SIGNALS)
    assert all((b["llmProfileId"], b["model"]) == ("p-1", "glm-5.3-flash") for b in bodies)


# --- Command path, comparison report, and workspace profile --------------------------------
#
# Everything below injects its HTTP seam (openers, monkeypatched module functions, or a
# localhost-only HTTP server) and clears the Laminar/Z.ai variables, so it never reads
# credentials from Peter's environment and never calls the real API.


@pytest.fixture
def no_laminar_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (LAMINAR_KEY_ENV, LAMINAR_ENDPOINT_ENV, ZAI_KEY_ENV):
        monkeypatch.delenv(name, raising=False)


def test_json_call_maps_http_errors_to_a_runtime_error() -> None:
    def opener(request: Any, timeout: float) -> Any:
        raise HTTPError(request.full_url, 500, "server error", {}, io.BytesIO(b'{"error":"boom"}'))

    with pytest.raises(RuntimeError) as excinfo:
        _json_call("POST", "/v1/signals", api_key="k", endpoint="http://x", opener=opener)
    message = str(excinfo.value)
    assert "Laminar POST /v1/signals -> HTTP 500" in message and "boom" in message


def test_ensure_zai_profile_reuses_an_existing_profile() -> None:
    calls: list[tuple[str, str]] = []

    def opener(request: Any, timeout: float) -> Any:
        calls.append((request.get_method(), request.full_url))
        reply = {"llmProfiles": [{"id": "p-9", "name": ZAI_PROFILE_NAME}]}
        return io.BytesIO(json.dumps(reply).encode())

    assert ensure_zai_profile(api_key="k", zai_key="z", endpoint="http://x", opener=opener) == "p-9"
    assert calls == [("GET", "http://x/v1/llm-profiles")]


def test_ensure_zai_profile_creates_the_custom_zai_route_once() -> None:
    bodies: list[dict[str, Any]] = []

    def opener(request: Any, timeout: float) -> Any:
        if request.get_method() == "GET":
            return io.BytesIO(b'{"llmProfiles": [{"id": "p-1", "name": "other"}]}')
        bodies.append(json.loads(request.data))
        return io.BytesIO(b'{"id": "p-new"}')

    assert (
        ensure_zai_profile(api_key="k", zai_key="zai-secret", endpoint="http://x", opener=opener)
        == "p-new"
    )
    assert bodies == [
        {
            "name": ZAI_PROFILE_NAME,
            "provider": "custom",
            "config": {"baseUrl": ZAI_BASE_URL, "auth": {"type": "api_key"}},
            "secrets": {"apiKey": "zai-secret"},
            "models": [ZAI_SIGNAL_MODEL],
        }
    ]



def test_laminar_command_requires_the_project_key(
    no_laminar_credentials: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = SimpleNamespace(laminar_action="signals", byok_zai=False)
    assert _laminar_command(args, tmp_path) == 2
    assert "LMNR_PROJECT_API_KEY is not set" in capsys.readouterr().err


def test_laminar_signals_byok_requires_the_zai_key(
    no_laminar_credentials: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(LAMINAR_KEY_ENV, "project-key")
    monkeypatch.setattr(
        laminar_module, "ensure_zai_profile", lambda **_: pytest.fail("no profile without a key")
    )
    args = SimpleNamespace(laminar_action="signals", byok_zai=True)
    assert _laminar_command(args, tmp_path) == 2
    assert ZAI_KEY_ENV in capsys.readouterr().err


def test_laminar_signals_reports_api_errors(
    no_laminar_credentials: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(LAMINAR_KEY_ENV, "project-key")

    def refuse(**_: Any) -> list[dict[str, Any]]:
        raise RuntimeError("Laminar POST /v1/signals -> HTTP 403: forbidden")

    monkeypatch.setattr(laminar_module, "apply_signals", refuse)
    monkeypatch.setattr(
        laminar_module, "ensure_zai_profile", lambda **_: pytest.fail("BYOK flag not set")
    )
    args = SimpleNamespace(laminar_action="signals", byok_zai=False)
    assert _laminar_command(args, tmp_path) == 1
    assert "HTTP 403" in capsys.readouterr().err


def test_laminar_signals_applies_with_the_endpoint_and_byok_profile(
    no_laminar_credentials: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(LAMINAR_KEY_ENV, "project-key")
    monkeypatch.setenv(LAMINAR_ENDPOINT_ENV, "http://laminar.test")
    monkeypatch.setenv(ZAI_KEY_ENV, "zai-key")
    seen: dict[str, Any] = {}

    def fake_profile(**kwargs: Any) -> str:
        seen["profile"] = kwargs
        return "zai-profile"

    def fake_apply(**kwargs: Any) -> list[dict[str, Any]]:
        seen["apply"] = kwargs
        return [{"name": "stuck_loop", "id": "s-1", "version": 2}]

    monkeypatch.setattr(laminar_module, "ensure_zai_profile", fake_profile)
    monkeypatch.setattr(laminar_module, "apply_signals", fake_apply)
    args = SimpleNamespace(laminar_action="signals", byok_zai=True)
    assert _laminar_command(args, tmp_path) == 0
    assert seen["profile"] == {
        "api_key": "project-key",
        "zai_key": "zai-key",
        "endpoint": "http://laminar.test",
    }
    assert seen["apply"] == {
        "api_key": "project-key",
        "endpoint": "http://laminar.test",
        "llm_profile_id": "zai-profile",
    }
    assert capsys.readouterr().out == "signal stuck_loop: id s-1 version 2\n"


