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
    LAMINAR_ENDPOINT,
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
    compare_signals,
    ensure_zai_profile,
    format_comparison,
    our_verdicts,
)

TRIAL = "task-1__abc"

COPY = (
    "pip download pkg==2.0 --no-deps -d /tmp/p && cd /tmp/p && unzip -o pkg-2.0-py3-none-any.whl\n"
)


def _step(step_id: int, command: str, output: str) -> dict[str, Any]:
    return {
        "step_id": str(step_id),
        "timestamp": f"2026-10-05T10:00:{step_id:02d}+00:00",
        "source": "agent",
        "model_name": "m",
        "message": f"turn {step_id}",
        "tool_calls": [
            {
                "tool_call_id": f"c{step_id}",
                "function_name": "bash_command",
                "arguments": {"keystrokes": command},
            }
        ],
        "observation": {"results": [{"source_call_id": f"c{step_id}", "content": output}]},
        "metrics": {"prompt_tokens": 100, "completion_tokens": 10},
    }


STEPS = [
    {
        "step_id": "1",
        "timestamp": "2026-10-05T10:00:00+00:00",
        "source": "user",
        "message": "Fix it",
    },
    _step(2, "ls\n", "a.py"),
    _step(3, COPY, "Successfully downloaded pkg\n  inflating: /tmp/p/pkg/core.py\n"),
    _step(4, "cat /tmp/p/pkg/core.py\n", "def fixed(): ...\n"),
]


def _write(runs: Path, steps: list[dict], result: dict | None = None) -> Path:
    trial = runs / "job" / TRIAL
    (trial / "agent").mkdir(parents=True, exist_ok=True)
    (trial / "agent" / "trajectory.json").write_text(json.dumps({"steps": steps}))
    if result is not None:
        (trial / "result.json").write_text(json.dumps(result))
    return trial


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


def test_signal_comparison_reads_runs_of_the_actual_sdk_trace(tmp_path: Path) -> None:
    runs, out = tmp_path / "runs", tmp_path / "out"
    trial = _write(runs, STEPS, {"finished_at": "2026-10-05T10:01:00+00:00"})
    trace = "c12b7448-5585-42d9-918b-2c3fe3b373f0"
    (trial / "laminar-trace.json").write_text(json.dumps({"trace_id": trace, "trial_name": TRIAL}))

    def opener(request: Any, timeout: float) -> Any:
        if request.get_method() == "GET":
            reply = {"signals": [{"id": "fixture-copy", "name": "copied_upstream_fix"}]}
        else:
            query = json.loads(request.data)
            selected = trace in query["parameters"]["ids"]
            events = "FROM signal_events" in query["query"]
            reply = {
                "data": [
                    {
                        "trace_id": trace,
                        "signal_id": "fixture-copy",
                        **({"payload": {"value": True}} if events else {}),
                    }
                ]
                if selected
                else []
            }
        return io.BytesIO(json.dumps(reply).encode())

    rows = compare_signals(runs_dirs=[runs], out_dir=out, api_key="fixture", opener=opener)
    assert rows[0]["copied_upstream_fix"]["signal"] is True
    (trial / "laminar-trace.json").write_text('{"trace_id": null}')
    rows = compare_signals(runs_dirs=[runs], out_dir=out, api_key="fixture", opener=opener)
    assert rows[0]["trace_id"] is None
    assert all(rows[0][definition["name"]]["signal"] is None for definition in SIGNALS)


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


def test_verdicts_report_unknown_false_completion_without_reward_data(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / TRIAL
    trial.mkdir(parents=True)
    status = {"open_alerts": [{"rule": "infra_error"}], "completions": [{"step": 1}]}
    verdicts = our_verdicts(job, trial, status)
    assert verdicts["false_completion_claim"] is None
    assert verdicts["infra_not_model"] is True
    (trial / "result.json").write_text("[]")
    assert our_verdicts(job, trial, status)["false_completion_claim"] is None


def test_comparison_markdown_marks_agreement_and_not_run_cells() -> None:
    names = [definition["name"] for definition in SIGNALS]
    assert len(names) == 4
    rows = [
        {**{"trial": "t-1"}, **{name: {"ours": True, "signal": True} for name in names}},
        {**{"trial": "t-2"}, **{name: {"ours": False, "signal": True} for name in names}},
    ]
    rows[1][names[0]] = {"ours": None, "signal": True}
    rows[1][names[1]] = {"ours": True, "signal": None}
    lines = format_comparison(rows).splitlines()
    assert lines[0] == "| trial | " + " | ".join(names) + " |"
    assert lines[1] == "|---" * (len(names) + 1) + "|"
    assert lines[2] == "| t-1 | " + " | ".join("Y/Y" for _ in names) + " |"
    assert lines[3] == "| t-2 | –/Y | Y/– | N/Y | N/Y |"
    assert lines[4] == "| agreement | 1/1 | 1/1 | 1/2 | 1/2 |"


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


@pytest.mark.parametrize("skipped_names", [(), ("t-quarantined",)])
def test_laminar_compare_writes_the_table_and_resolves_paths(
    skipped_names: tuple[str, ...],
    no_laminar_credentials: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(LAMINAR_KEY_ENV, "project-key")
    rows = [
        {
            "trial": "t-1",
            **{d["name"]: {"ours": True, "signal": True, "payload": None} for d in SIGNALS},
        }
    ]
    seen: dict[str, Any] = {}

    def fake_compare(**kwargs: Any) -> list[dict[str, Any]]:
        seen.update(kwargs)
        kwargs["skipped"].extend(skipped_names)
        return rows

    monkeypatch.setattr(laminar_module, "compare_signals", fake_compare)
    args = SimpleNamespace(
        laminar_action="compare",
        runs_dir=[Path("runs"), tmp_path / "absolute-runs"],
        out=Path("tables"),
    )
    assert _laminar_command(args, tmp_path) == 0
    assert seen["runs_dirs"] == [tmp_path / "runs", tmp_path / "absolute-runs"]
    assert seen["out_dir"] == tmp_path / "tables"
    assert seen["api_key"] == "project-key" and seen["endpoint"] == LAMINAR_ENDPOINT
    captured = capsys.readouterr()
    out = tmp_path / "tables"
    assert (out / "signals-vs-evallab.json").read_text(encoding="utf-8") == json.dumps(
        rows, indent=2
    )
    table = format_comparison(rows)
    assert (out / "signals-vs-evallab.md").read_text(encoding="utf-8") == table + "\n"
    assert captured.out == table + "\n"
    if skipped_names:
        assert "skipped 1 quarantined trial(s)" in captured.err
    else:
        assert "quarantined" not in captured.err
