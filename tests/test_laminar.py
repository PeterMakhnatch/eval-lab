"""Behavioral tests for evallab.laminar: live trial export, alert spans, Signals as code."""

from __future__ import annotations

import argparse
import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from evallab import laminar as laminar_module
from evallab.laminar import (
    LAMINAR_ENDPOINT,
    LAMINAR_ENDPOINT_ENV,
    LAMINAR_KEY_ENV,
    MAX_VALUE_CHARS,
    SIGNALS,
    WITHHELD,
    ZAI_BASE_URL,
    ZAI_KEY_ENV,
    ZAI_PROFILE_NAME,
    ZAI_SIGNAL_MODEL,
    LaminarExporter,
    _json_call,
    _laminar_command,
    _Sanitizer,
    apply_signals,
    build_laminar_parser,
    compare_signals,
    encode_spans,
    ensure_zai_profile,
    exporter_from_env,
    format_comparison,
    our_verdicts,
    step_spans,
    trial_trace_id,
)
from evallab.live_watch import run_watch

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


class Recorder:
    def __init__(self) -> None:
        self.posts: list[list[Any]] = []
        self.fail = False

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        assert headers["authorization"] == "Bearer k-123456789"
        if self.fail:
            raise OSError("unreachable")
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        self.posts.append(
            [s for r in request.resource_spans for ss in r.scope_spans for s in ss.spans]
        )
        return 200


def _pass(runs: Path, out: Path, recorder: Recorder, secrets: list[str] | None = None) -> dict:
    exporter = LaminarExporter(
        api_key="k-123456789",
        out_dir=out,
        endpoint="http://x",
        transport=recorder,
        secrets=secrets or [],
    )
    return run_watch(runs_dirs=[runs], out_dir=out, laminar=exporter)


def _write(runs: Path, steps: list[dict], result: dict | None = None) -> Path:
    trial = runs / "job" / TRIAL
    (trial / "agent").mkdir(parents=True, exist_ok=True)
    (trial / "agent" / "trajectory.json").write_text(json.dumps({"steps": steps}))
    if result is not None:
        (trial / "result.json").write_text(json.dumps(result))
    return trial


@pytest.mark.parametrize("switch", [None, "", "off", "0", "false", "yes", "on accidentally"])
def test_key_alone_does_not_enable_live_export(tmp_path: Path, switch: str | None) -> None:
    environ = {"LMNR_PROJECT_API_KEY": "fixture-key"}
    if switch is not None:
        environ["EVALLAB_LAMINAR"] = switch
    assert exporter_from_env(tmp_path, environ=environ) is None


@pytest.mark.parametrize("switch", ["1", "true", "TRUE", "on", " On "])
def test_explicit_environment_opt_in_enables_live_export(tmp_path: Path, switch: str) -> None:
    exporter = exporter_from_env(
        tmp_path,
        environ={"LMNR_PROJECT_API_KEY": "fixture-key", "EVALLAB_LAMINAR": switch},
        secrets=[],
    )
    assert isinstance(exporter, LaminarExporter)
    assert exporter._url == "https://api.lmnr.ai/v1/traces"


@pytest.mark.parametrize("switch", [None, "off"])
def test_explicit_cli_opt_in_does_not_require_environment_switch(
    tmp_path: Path, switch: str | None
) -> None:
    environ = {"LMNR_PROJECT_API_KEY": "fixture-key"}
    if switch is not None:
        environ["EVALLAB_LAMINAR"] = switch
    assert isinstance(exporter_from_env(tmp_path, environ=environ, enabled=True), LaminarExporter)


def test_explicit_opt_in_still_requires_a_key(tmp_path: Path) -> None:
    assert exporter_from_env(tmp_path, environ={"EVALLAB_LAMINAR": "on"}) is None
    assert exporter_from_env(tmp_path, environ={}, enabled=True) is None


def test_live_trial_exports_finished_steps_then_root_once(tmp_path: Path) -> None:


    runs, out, rec = tmp_path / "runs", tmp_path / "out", Recorder()
    _write(runs, STEPS[:3])
    _pass(runs, out, rec)
    first = {s.name for s in rec.posts[-1]}
    # The last step's observation may still change, and the root waits for result.json.
    assert "turn 2" in first and "turn 3" not in first and "harbor.trial" not in first

    _write(
        runs,
        STEPS,
        {
            "finished_at": "2026-10-05T10:01:00+00:00",
            "verifier_result": {"rewards": {"reward": 1.0}},
        },
    )
    _pass(runs, out, rec)
    second = [s for s in rec.posts[-1]]
    names = {s.name for s in second}
    assert {"turn 3", "turn 4", "harbor.trial"} <= names and "turn 2" not in names
    assert all(s.trace_id.hex() == trial_trace_id(TRIAL) for s in second)

    posted = len(rec.posts)
    _pass(runs, out, rec)
    assert len(rec.posts) == posted, "an unchanged finished trial must not be re-exported"


def test_alert_becomes_span_event_under_cited_step(tmp_path: Path) -> None:
    runs, out, rec = tmp_path / "runs", tmp_path / "out", Recorder()
    _write(runs, STEPS)
    summary = _pass(runs, out, rec)
    fetch = next(a for a in summary["statuses"][0]["open_alerts"] if a["rule"] == "fetch_attempt")
    spans = {s.span_id.hex(): s for post in rec.posts for s in post}
    alert = next(s for s in spans.values() if s.name == "evallab.alert.fetch_attempt")
    parent = spans[alert.parent_span_id.hex()]
    step = {a.key: a.value.string_value for a in parent.attributes}["evallab.step_id"]
    assert f"head#{step}" == fetch["step_ref"] == "head#3"
    assert [e.name for e in alert.events] == ["fetch_attempt"]


def test_failed_export_is_retried_and_secrets_never_leave(tmp_path: Path) -> None:
    runs, out, rec = tmp_path / "runs", tmp_path / "out", Recorder()
    secret = "sk-live-0123456789abcdef"
    steps = [*STEPS[:2], _step(3, f"echo {secret}\n", secret), STEPS[3]]
    _write(runs, steps, {"finished_at": "2026-10-05T10:01:00+00:00"})
    rec.fail = True
    failed = _pass(runs, out, rec, secrets=[secret])
    assert failed["laminar"]["ok"] is False and not rec.posts
    rec.fail = False
    _pass(runs, out, rec, secrets=[secret])
    assert {"turn 2", "turn 3", "harbor.trial"} <= {s.name for s in rec.posts[-1]}
    assert secret.encode() not in b"".join(s.SerializeToString() for s in rec.posts[-1])


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


def test_sdk_trial_uses_actual_trace_without_projected_root_or_llm_duplicates(
    tmp_path: Path,
) -> None:
    runs, out, rec = tmp_path / "runs", tmp_path / "out", Recorder()
    secret = "sk-private-watch-0123456789"
    steps = [
        *STEPS[:2],
        _step(
            3,
            COPY.rstrip("\n")
            + f" # {secret} Authorization: Bearer WATCH_AUTH_CANARY /Users/private-owner/config\n",
            "Successfully downloaded pkg\n  inflating: /tmp/p/pkg/core.py\n",
        ),
        STEPS[3],
    ]
    trial = _write(runs, steps, {"finished_at": "2026-10-05T10:01:00+00:00"})
    trace = "c12b7448-5585-42d9-918b-2c3fe3b373f0"
    parent = "a12b7448558542d9"
    (trial / "laminar-trace.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "trace_id": trace,
                "root_span_id": parent,
                "trial_name": TRIAL,
                "status": "closed",
            }
        )
    )
    summary = _pass(runs, out, rec, secrets=[secret])
    assert summary["statuses"][0]["laminar_trace_id"] == trace
    spans = [span for post in rec.posts for span in post]
    assert any(span.name == "evallab.alert.fetch_attempt" for span in spans)
    assert all(
        {attribute.key: attribute.value.string_value for attribute in span.attributes}[
            "lmnr.span.type"
        ]
        == "DEFAULT"
        for span in spans
    )
    assert all(
        span.trace_id.hex() == trace.replace("-", "") and span.parent_span_id.hex() == parent
        for span in spans
    )
    payload = b"".join(span.SerializeToString() for span in spans)
    assert all(
        value not in payload
        for value in (secret.encode(), b"WATCH_AUTH_CANARY", b"/Users/private-owner/config")
    )
    assert b"Bearer [REDACTED]" in payload
    posted = len(rec.posts)
    _pass(runs, out, rec, secrets=[secret])
    assert len(rec.posts) == posted


def test_unavailable_sdk_identity_never_falls_back_to_synthetic_cloud_trace(tmp_path: Path) -> None:
    runs, out, rec = tmp_path / "runs", tmp_path / "out", Recorder()
    trial = _write(runs, STEPS, {"finished_at": "2026-10-05T10:01:00+00:00"})
    (trial / "laminar-trace.json").write_text('{"trace_id": null}')
    summary = _pass(runs, out, rec)
    assert summary["statuses"][0]["laminar_trace_id"] is None
    assert any(alert["rule"] == "fetch_attempt" for alert in summary["statuses"][0]["open_alerts"])
    assert not rec.posts


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


def test_infra_death_is_an_error_span_in_the_trace(tmp_path: Path) -> None:
    runs, out, rec = tmp_path / "runs", tmp_path / "out", Recorder()
    death = {"exception_type": "DaytonaNotFoundError", "exception_message": "sandbox not found"}
    _write(runs, STEPS, {"finished_at": "2026-10-05T10:01:00+00:00", "exception_info": death})
    _pass(runs, out, rec)
    spans = rec.posts[-1]
    names = [s.name for s in spans]
    exc = spans[names.index("harbor.exception")]
    assert exc.status.code == exc.status.STATUS_CODE_ERROR
    assert "DaytonaNotFoundError: sandbox not found" in exc.status.message
    assert names.index("harbor.exception") < names.index("harbor.trial")


# --- Command path, comparison report, and workspace profile --------------------------------
#
# Everything below injects its HTTP seam (openers, monkeypatched module functions, or a
# localhost-only HTTP server) and clears the Laminar/Z.ai variables, so it never reads
# credentials from Peter's environment and never calls the real API.


@pytest.fixture
def no_laminar_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (LAMINAR_KEY_ENV, LAMINAR_ENDPOINT_ENV, ZAI_KEY_ENV):
        monkeypatch.delenv(name, raising=False)


@contextmanager
def _localhost(status: int, received: list[bytes], headers: list[dict[str, str]]) -> Iterator[str]:
    """A one-route HTTP server on 127.0.0.1, so the default transport is exercised for real."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            received.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            headers.append({key.lower(): value for key, value in self.headers.items()})
            self.send_response(status)
            self.end_headers()

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "marker",
    [
        "directory",  # the marker cannot be read at all (OSError, not FileNotFoundError)
        json.dumps({"trace_id": "00000000-0000-0000-0000-000000000000", "trial_name": TRIAL}),
        json.dumps({"trace_id": "not-a-uuid", "trial_name": TRIAL}),
    ],
)
def test_unreadable_or_null_sdk_identity_is_unknown_not_projected(
    tmp_path: Path, marker: str
) -> None:
    runs, out, rec = tmp_path / "runs", tmp_path / "out", Recorder()
    trial = _write(runs, STEPS, {"finished_at": "2026-10-05T10:01:00+00:00"})
    if marker == "directory":
        (trial / "laminar-trace.json").mkdir()
    else:
        (trial / "laminar-trace.json").write_text(marker)
    summary = _pass(runs, out, rec)
    assert summary["statuses"][0]["laminar_trace_id"] is None
    assert not rec.posts, "an unknown SDK identity never falls back to a synthetic trace"


def test_invalid_step_timestamp_reuses_the_previous_step_time() -> None:
    steps = [
        {
            "step_id": "1",
            "timestamp": "2026-10-05T10:00:00+00:00",
            "source": "user",
            "message": "fix it",
        },
        {"step_id": "2", "timestamp": "not-a-timestamp", "source": "user", "message": "later"},
    ]
    spans = step_spans(TRIAL, steps, final=True, clean=str, fallback_ns=1_000)
    first, second = spans
    assert second["start_ns"] == first["start_ns"] == 1_791_194_400 * 10**9


def test_sanitizer_withholds_pattern_secrets_and_clips_long_values() -> None:
    clean = _Sanitizer([])
    assert clean("ghp_" + "A" * 24) == WITHHELD
    assert clean.withheld == 1
    clipped = clean("z" * (MAX_VALUE_CHARS + 10))
    assert clipped.endswith("<<evallab: clipped 10 chars>>")
    assert len(clipped) < MAX_VALUE_CHARS + 100
    assert clean({"flag": True}) == '{"flag": true}'


def test_tool_output_ignores_foreign_and_non_dict_observation_results() -> None:
    step = _step(2, "ls\n", "ignored")
    step["tool_calls"] = [
        {
            "tool_call_id": "c2",
            "function_name": "bash_command",
            "arguments": {"keystrokes": "ls\n"},
        },
        {
            "tool_call_id": "c3",
            "function_name": "bash_command",
            "arguments": {"keystrokes": "cat\n"},
        },
    ]
    step["observation"] = {
        "results": [
            None,
            {"source_call_id": "c-other", "content": "FOREIGN"},
            {"content": "CALLED"},
        ]
    }
    spans = step_spans(TRIAL, [step], final=True, clean=str, fallback_ns=1_000)
    tools = [span for span in spans if span["attributes"].get("lmnr.span.type") == "TOOL"]
    assert [span["attributes"]["lmnr.span.output"] for span in tools] == ["CALLED", "CALLED"]


def test_non_dict_observation_text_is_empty() -> None:
    step = _step(2, "ls\n", "")
    step["observation"] = "raw text"
    spans = step_spans(TRIAL, [step], final=True, clean=str, fallback_ns=1_000)
    tool = next(span for span in spans if span["attributes"].get("lmnr.span.type") == "TOOL")
    assert tool["attributes"]["lmnr.span.output"] == ""


def test_attribute_values_keep_bool_int_float_and_string_arrays() -> None:
    encoded = encode_spans(
        [
            {
                "trace_id": "ab" * 16,
                "span_id": "cd" * 8,
                "parent_id": None,
                "name": "values",
                "start_ns": 1,
                "end_ns": 2,
                "attributes": {"flag": True, "count": 3, "ratio": 0.5, "tags": ["a", "b"]},
                "events": [],
                "error": None,
            }
        ]
    )
    request = ExportTraceServiceRequest.FromString(encoded)
    span = next(
        span
        for resource in request.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    )
    values = {attribute.key: attribute.value for attribute in span.attributes}
    assert values["flag"].WhichOneof("value") == "bool_value" and values["flag"].bool_value is True
    assert values["count"].int_value == 3
    assert values["ratio"].double_value == 0.5
    assert [item.string_value for item in values["tags"].array_value.values] == ["a", "b"]


def test_default_transport_posts_otlp_to_the_configured_endpoint(tmp_path: Path) -> None:
    runs, received, headers = tmp_path / "runs", [], []
    _write(runs, STEPS, {"finished_at": "2026-10-05T10:01:00+00:00"})
    with _localhost(200, received, headers) as url:
        exporter = LaminarExporter(
            api_key="k-123456789", out_dir=tmp_path / "out", endpoint=url, secrets=[]
        )
        summary = run_watch(runs_dirs=[runs], out_dir=tmp_path / "out", laminar=exporter)
    assert summary["laminar"]["ok"] is True
    assert headers[0]["authorization"] == "Bearer k-123456789"
    assert headers[0]["content-type"] == "application/x-protobuf"
    request = ExportTraceServiceRequest.FromString(b"".join(received))
    names = {
        span.name
        for resource in request.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    }
    assert {"turn 2", "turn 3", "turn 4", "harbor.trial"} <= names


def test_default_transport_reports_an_http_error_without_raising(tmp_path: Path) -> None:
    runs, received, headers = tmp_path / "runs", [], []
    trial = _write(runs, STEPS, {"finished_at": "2026-10-05T10:01:00+00:00"})
    with _localhost(503, received, headers) as url:
        exporter = LaminarExporter(
            api_key="k-123456789", out_dir=tmp_path / "out", endpoint=url, secrets=[]
        )
        exporter.sync_trial(runs / "job", trial, finished=True)
        outcome = exporter.flush()
    assert outcome["ok"] is False and outcome["error"] == "HTTP 503"
    assert received, "the transport must send the encoded spans before reporting the failure"


def test_malformed_result_json_still_exports_the_root_span(tmp_path: Path) -> None:
    runs, out, rec = tmp_path / "runs", tmp_path / "out", Recorder()
    trial = _write(runs, STEPS)
    (trial / "result.json").write_text("not json")
    summary = _pass(runs, out, rec)
    assert summary["laminar"]["ok"] is True
    assert {"harbor.trial", "turn 4"} <= {span.name for span in rec.posts[-1]}


def test_fleet_and_unscoped_alerts_never_become_spans(tmp_path: Path) -> None:
    rec = Recorder()
    exporter = LaminarExporter(
        api_key="k-123456789",
        out_dir=tmp_path / "out",
        endpoint="http://x",
        transport=rec,
        secrets=[],
    )
    exporter.sync_alerts(
        [
            {"rule": "fleet_spike", "scope": "fleet", "trial": TRIAL},
            {"rule": "no_trial", "scope": "trial"},
        ]
    )
    outcome = exporter.flush()
    assert outcome["spans"] == 0 and not rec.posts


def test_state_write_failure_never_raises(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    rec = Recorder()
    exporter = LaminarExporter(
        api_key="k-123456789", out_dir=blocked, endpoint="http://x", transport=rec, secrets=[]
    )
    exporter.sync_alerts([{"rule": "fetch_attempt", "trial": TRIAL}])
    assert exporter.flush()["ok"] is True and rec.posts


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


def test_laminar_parser_registers_signals_and_compare() -> None:
    parser = argparse.ArgumentParser()
    build_laminar_parser(parser.add_subparsers(dest="command"))
    signals = parser.parse_args(["laminar", "signals", "--byok-zai"])
    assert signals.laminar_action == "signals" and signals.byok_zai is True
    assert signals.func is _laminar_command
    compare = parser.parse_args(
        ["laminar", "compare", "--runs-dir", "a", "--runs-dir", "b", "--out", "o"]
    )
    assert compare.laminar_action == "compare"
    assert compare.runs_dir == [Path("a"), Path("b")]
    assert compare.out == Path("o") and compare.func is _laminar_command
    with pytest.raises(SystemExit):
        parser.parse_args(["laminar"])  # an action is required
