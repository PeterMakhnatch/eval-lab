"""Credential relay readiness, redirect isolation, and native ingress failure contracts."""

from __future__ import annotations

import gzip
import json
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToJson
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from evallab import credential_bridge as bridge


@pytest.fixture
def config():
    return bridge.BridgeConfig(
        model_id="XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
        model_upstream="https://model.invalid",
        model_api_key="private-model-key",
        chat_capability="chat-only-capability",
        telemetry_capability="native-ingest-only-capability",
        laminar_api_key="private-laminar-key",
    )


def _native_metric(*, bound=True):
    request = ExportMetricsServiceRequest()
    resource = request.resource_metrics.add()
    attributes = {"service.instance.id": "3f5367b7-f813-417e-bd43-b26ca6bcbea8"}
    if bound:
        attributes.update(
            {
                "evallab.trace_id": "00112233445566778899aabbccddeeff",
                "evallab.parent_span_id": "0123456789abcdef",
                "evallab.session_id": "native-job",
                "trial_id": "bb7aebe3-0000-4000-8000-000000000001",
            }
        )
    for key, value in attributes.items():
        resource.resource.attributes.add(key=key).value.string_value = value
    scope = resource.scope_metrics.add()
    scope.scope.name = "daytona-native"
    metric = scope.metrics.add(name="daytona.sandbox.cpu.utilization", unit="%")
    metric.gauge.data_points.add(time_unix_nano=1770000000000000000, as_double=12.5)
    return request


def _post(client, config, payload, **headers):
    return client.post(
        "/v1/metrics",
        content=payload,
        headers={
            "Authorization": "Bearer " + config.telemetry_capability,
            "Content-Type": "application/x-protobuf",
            **headers,
        },
    )


def test_ingest_capability_cannot_generate_model_answers_and_chat_cannot_ingest(config):
    def forbidden(*args, **kwargs):
        pytest.fail("Unauthorized requests must not reach either upstream")

    with TestClient(bridge.create_bridge_app(config, exchange=forbidden)) as client:
        native = client.post(
            "/v1/metrics",
            content=b"malformed",
            headers={"Authorization": "Bearer " + config.chat_capability},
        )
        chat = client.post(
            "/v1/chat/completions",
            json={"model": config.model_id, "messages": []},
            headers={"Authorization": "Bearer " + config.telemetry_capability},
        )
    assert native.status_code == 401
    assert chat.status_code == 401
    with pytest.raises(ValueError, match="separate capabilities"):
        replace(config, telemetry_capability=config.chat_capability)


@pytest.mark.parametrize(
    ("body", "headers", "expected"),
    [
        (b"\xff", {}, 400),
        (b"not-json", {"Content-Type": "application/json"}, 400),
        (b"", {"Content-Type": "text/plain"}, 415),
        (b"\x1f\x8b\x08", {"Content-Encoding": "gzip"}, 400),
        (b"", {"Content-Encoding": "deflate"}, 400),
    ],
)
def test_malformed_native_input_is_rejected_before_export(config, body, headers, expected):
    def forbidden(*args, **kwargs):
        pytest.fail("Malformed frames cannot be acknowledged by a downstream success")

    with TestClient(bridge.create_bridge_app(config, exchange=forbidden)) as client:
        response = _post(client, config, body, **headers)
    assert response.status_code == expected


def test_compressed_native_frame_cannot_bypass_uncompressed_limit(config, monkeypatch):
    monkeypatch.setattr(bridge, "_MAX_FRAME", 128)
    with TestClient(bridge.create_bridge_app(config)) as client:
        response = _post(
            client,
            config,
            gzip.compress(b" " * 129),
            **{"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )
    assert response.status_code == 413


@pytest.mark.parametrize("outcome", [503, "timeout"])
def test_downstream_failure_is_not_a_native_metrics_success_ack(config, outcome):
    def exchange(*args, **kwargs):
        if outcome == "timeout":
            raise TimeoutError("upstream did not accept")
        return outcome, b"unavailable", "text/plain"

    with TestClient(bridge.create_bridge_app(config, exchange=exchange)) as client:
        response = _post(client, config, _native_metric().SerializeToString())
    assert response.status_code == 503


def test_unbound_metric_has_explicit_point_rejection_not_an_orphan_trace(config):
    def forbidden(*args, **kwargs):
        pytest.fail("An unbound observation must not create a cloud trace")

    with TestClient(bridge.create_bridge_app(config, exchange=forbidden)) as client:
        response = _post(client, config, _native_metric(bound=False).SerializeToString())
    acknowledgment = ExportMetricsServiceResponse.FromString(response.content)
    assert response.status_code == 200
    assert acknowledgment.partial_success.rejected_data_points == 1


@pytest.mark.parametrize("as_json", [False, True])
def test_downstream_partial_rejection_is_propagated_as_metrics_rejection(config, as_json):
    upstream = ExportTraceServiceResponse()
    upstream.partial_success.rejected_spans = 1
    upstream.partial_success.error_message = "rejected " + config.laminar_api_key

    def exchange(*args, **kwargs):
        return (
            200,
            MessageToJson(upstream).encode() if as_json else upstream.SerializeToString(),
            "application/json" if as_json else "application/x-protobuf",
        )

    with TestClient(bridge.create_bridge_app(config, exchange=exchange)) as client:
        response = _post(client, config, _native_metric().SerializeToString())
    acknowledgment = ExportMetricsServiceResponse.FromString(response.content)
    assert response.status_code == 200
    assert acknowledgment.partial_success.rejected_data_points == 1
    assert config.laminar_api_key not in acknowledgment.partial_success.error_message
    assert "rejected" in acknowledgment.partial_success.error_message


def test_original_model_failure_does_not_trigger_paid_answer_retry(config):
    attempts = 0

    def exchange(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return 503, b'{"error":"model unavailable"}', "application/json"

    with TestClient(bridge.create_bridge_app(config, exchange=exchange)) as client:
        response = client.post(
            "/v1/chat/completions",
            content=json.dumps({"model": config.model_id, "stream": False, "messages": []}),
            headers={"Authorization": "Bearer " + config.chat_capability},
        )
    assert response.status_code == 503
    assert attempts == 1


def test_valid_large_metric_batch_preserves_every_observation(config):
    request = _native_metric()
    points = request.resource_metrics[0].scope_metrics[0].metrics[0].gauge.data_points
    points.clear()
    expected = {}
    for index in range(3000):
        timestamp = 1770000000000000000 + index
        expected[str(timestamp)] = index / 100
        points.add(time_unix_nano=timestamp, as_double=index / 100)
    received = {}

    def accept(frame, **_kwargs):
        exported = bridge.decode_otlp(
            frame.data, "application/x-protobuf", bridge.ExportTraceServiceRequest
        )
        for resource in exported.resource_spans:
            for scope in resource.scope_spans:
                for span in scope.spans:
                    attributes = {item.key: item.value.string_value for item in span.attributes}
                    value = json.loads(attributes["lmnr.span.output"])
                    timestamp = value["point"]["timeUnixNano"]
                    assert timestamp not in received
                    received[timestamp] = value["point"]["asDouble"]
                    assert value["metric"]["unit"] == "%"
                    assert span.parent_span_id.hex() == "0123456789abcdef"
        return 200, b"", "application/x-protobuf"

    with TestClient(bridge.create_bridge_app(config, exchange=accept)) as client:
        reply = _post(client, config, request.SerializeToString())
    assert reply.status_code == 200
    acknowledgment = bridge.decode_otlp(
        reply.content, "application/x-protobuf", ExportMetricsServiceResponse
    )
    assert acknowledgment.partial_success.rejected_data_points == 0
    assert received == expected


def test_empty_valid_native_frame_cannot_create_an_orphan_root(config):
    def forbid_export(_request, **_kwargs):
        pytest.fail("An empty native frame must not export a synthetic span")

    with TestClient(bridge.create_bridge_app(config, exchange=forbid_export)) as client:
        reply = _post(client, config, b"")
    assert reply.status_code == 200
    acknowledgment = bridge.decode_otlp(
        reply.content, "application/x-protobuf", ExportMetricsServiceResponse
    )
    assert acknowledgment.partial_success.rejected_data_points == 0


@pytest.mark.parametrize("invalid_timestamp", [True, 1.5, "1770000000000000000"])
def test_binding_window_cannot_silently_truncate_json_values(invalid_timestamp):
    binding = {
        "app_id": "ap-protocol-smoke",
        "parent": {
            "trace_id": "00112233445566778899aabbccddeeff",
            "span_id": "0123456789abcdef",
            "session_id": "shared-model-session",
        },
        "started_at_ns": invalid_timestamp,
    }
    with pytest.raises(ValueError):
        bridge.model_session_bindings(json.dumps([binding]))


class _ReadinessClock:
    def __init__(self, *, max_sleeps):
        self.started = 43210.25
        self.now = self.started
        self.max_sleeps = max_sleeps
        self.reads = 0
        self.sleeps = []

    def __call__(self):
        self.reads += 1
        assert self.reads <= 2 * self.max_sleeps + 8, "Readiness clock polling did not terminate"
        return self.now

    def sleep(self, seconds):
        assert seconds == 2
        assert len(self.sleeps) < self.max_sleeps, "Readiness exceeded its sleep limit"
        self.sleeps.append(seconds)
        self.now += seconds


class _ReadinessUpstream:
    def __init__(self, config, clock, outcomes, *, poll_elapsed=0):
        self.config = config
        self.clock = clock
        self.outcomes = outcomes
        self.poll_elapsed = poll_elapsed
        self.calls = 0

    def __call__(self, request, *, timeout, native):
        assert request.full_url == self.config.model_upstream.rstrip("/") + "/health"
        assert request.get_method() == "GET"
        assert request.data is None
        assert request.get_header("Authorization") == "Bearer " + self.config.model_api_key
        assert timeout == 10
        assert native is False
        assert self.calls < len(self.outcomes), "Readiness exceeded its upstream poll limit"
        outcome = self.outcomes[self.calls]
        self.calls += 1
        self.clock.now += self.poll_elapsed
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, b"health response", "text/plain"


def test_ready_succeeds_on_first_health_poll_without_model_generation(config):
    config = replace(config, model_upstream=config.model_upstream + "/")
    clock = _ReadinessClock(max_sleeps=0)
    upstream = _ReadinessUpstream(config, clock, [200], poll_elapsed=0.75)
    with TestClient(
        bridge.create_bridge_app(config, exchange=upstream, clock=clock, sleep=clock.sleep)
    ) as client:
        response = client.get(
            "/ready", headers={"Authorization": "Bearer " + config.chat_capability}
        )
    assert response.status_code == 200
    assert response.json() == {
        "ready": True,
        "elapsed_s": 0.75,
        "health_failures": {},
        "model_requests": 0,
    }
    assert upstream.calls == 1
    assert clock.sleeps == []


@pytest.mark.parametrize(
    ("outcomes", "expected_failures"),
    [
        ([503, 503, 429, 503, 429, 200], {"503": 3, "429": 2}),
        (
            [OSError("offline"), TimeoutError("slow"), OSError("offline"), TimeoutError("slow"), 200],
            {"OSError": 2, "TimeoutError": 2},
        ),
        (
            [503, OSError("offline"), 429, TimeoutError("slow"), 503, 200],
            {"503": 2, "OSError": 1, "429": 1, "TimeoutError": 1},
        ),
    ],
    ids=["repeated-statuses", "distinct-exception-classes", "mixed-failures"],
)
def test_ready_preserves_every_health_failure_until_eventual_success(
    config, outcomes, expected_failures
):
    failures = len(outcomes) - 1
    clock = _ReadinessClock(max_sleeps=failures)
    upstream = _ReadinessUpstream(config, clock, outcomes)
    with TestClient(
        bridge.create_bridge_app(config, exchange=upstream, clock=clock, sleep=clock.sleep)
    ) as client:
        response = client.get(
            "/ready", headers={"Authorization": "Bearer " + config.chat_capability}
        )
    assert response.status_code == 200
    assert response.json() == {
        "ready": True,
        "elapsed_s": failures * 2,
        "health_failures": expected_failures,
        "model_requests": 0,
    }
    assert upstream.calls == len(outcomes)
    assert clock.sleeps == [2] * failures


def test_ready_stops_at_exact_1200_second_deadline_without_an_extra_poll(config):
    clock = _ReadinessClock(max_sleeps=600)
    upstream = _ReadinessUpstream(config, clock, [503] * 600)
    with TestClient(
        bridge.create_bridge_app(config, exchange=upstream, clock=clock, sleep=clock.sleep)
    ) as client:
        response = client.get(
            "/ready", headers={"Authorization": "Bearer " + config.chat_capability}
        )
    assert response.status_code == 200
    assert response.json() == {
        "ready": False,
        "elapsed_s": 1200,
        "health_failures": {"503": 600},
        "model_requests": 0,
    }
    assert upstream.calls == 600
    assert clock.sleeps == [2] * 600
    assert clock.now == clock.started + 1200


@pytest.mark.parametrize("credential", ["missing", "wrong", "telemetry"])
def test_ready_rejects_non_chat_capabilities_before_any_collaborator_call(config, credential):
    def forbidden(*args, **kwargs):
        pytest.fail("Unauthorized readiness must not poll, read the clock, or sleep")

    headers = {
        "missing": {},
        "wrong": {"Authorization": "Bearer wrong-capability"},
        "telemetry": {"Authorization": "Bearer " + config.telemetry_capability},
    }[credential]
    with TestClient(
        bridge.create_bridge_app(config, exchange=forbidden, clock=forbidden, sleep=forbidden)
    ) as client:
        response = client.get("/ready", headers=headers)
    assert response.status_code == 401


@pytest.fixture
def exchange_http_server(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    requests = []
    responses = {
        "/redirect": (302, b"original redirect body", "text/plain; charset=utf-8"),
        "/destination": (201, b'{"accepted":true}', "application/json"),
        "/unavailable": (503, b"upstream unavailable", "text/plain; charset=utf-8"),
    }

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(2)

        def _respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(
                (self.command, self.path, self.headers.get("Authorization"), body)
            )
            status, response_body, content_type = responses[self.path]
            self.send_response(status)
            if self.path == "/redirect":
                self.send_header("Location", "/destination")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def do_POST(self):
            self._respond()

        def do_GET(self):
            self._respond()

        def log_message(self, _format, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive(), "Local exchange server did not stop"


def test_native_exchange_preserves_redirect_without_forwarding_credential(config, exchange_http_server):
    base_url, requests = exchange_http_server
    credential = "Bearer " + config.laminar_api_key
    request = urllib.request.Request(
        base_url + "/redirect",
        data=b"native export payload",
        headers={"Authorization": credential},
        method="POST",
    )

    response = bridge._exchange(request, timeout=2, native=True)

    assert response == (302, b"original redirect body", "text/plain; charset=utf-8")
    assert requests == [("POST", "/redirect", credential, b"native export payload")]


def test_non_native_exchange_follows_post_redirect(config, exchange_http_server):
    base_url, requests = exchange_http_server
    credential = "Bearer " + config.model_api_key
    request = urllib.request.Request(
        base_url + "/redirect",
        data=b"model request payload",
        headers={"Authorization": credential},
        method="POST",
    )

    response = bridge._exchange(request, timeout=2, native=False)

    assert response == (201, b'{"accepted":true}', "application/json")
    assert requests == [
        ("POST", "/redirect", credential, b"model request payload"),
        ("GET", "/destination", credential, b""),
    ]


@pytest.mark.parametrize("native", [True, False])
def test_exchange_returns_http_error_body_status_and_type(config, exchange_http_server, native):
    base_url, requests = exchange_http_server
    credential = "Bearer " + config.laminar_api_key
    request = urllib.request.Request(
        base_url + "/unavailable",
        data=b"export payload",
        headers={"Authorization": credential},
        method="POST",
    )

    response = bridge._exchange(request, timeout=2, native=native)

    assert response == (503, b"upstream unavailable", "text/plain; charset=utf-8")
    assert requests == [("POST", "/unavailable", credential, b"export payload")]


_TRACE_ID = "00112233445566778899aabbccddeeff"
_SPAN_ID = "0123456789abcdef"


def _forbidden_exchange(*args, **kwargs):
    pytest.fail("A refused request must not reach the model or Laminar")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"model_api_key": ""}, "Model credentials are required"),
        ({"chat_capability": ""}, "Model credentials are required"),
        ({"laminar_api_key": None}, "both its capability and Laminar key"),
        ({"telemetry_capability": None}, "both its capability and Laminar key"),
    ],
    ids=["no-model-key", "no-chat-capability", "ingest-without-laminar-key", "laminar-key-without-ingest"],
)
def test_bridge_config_refuses_incomplete_credentials(config, changes, message):
    with pytest.raises(ValueError, match=message):
        replace(config, **changes)


def test_chat_only_relay_refuses_native_ingest_with_empty_bearer(config):
    chat_only = replace(config, telemetry_capability=None, laminar_api_key=None)
    with TestClient(bridge.create_bridge_app(chat_only, exchange=_forbidden_exchange)) as client:
        response = client.post(
            "/v1/traces",
            content=b"",
            headers={"Authorization": "Bearer ", "Content-Type": "application/x-protobuf"},
        )
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"app_id": "ap-protocol-smoke"}', "must be a JSON array"),
        ('["ap-protocol-smoke"]', "needs its actual parent context"),
        ('[{"app_id": "ap-protocol-smoke", "started_at_ns": 0}]', "needs its actual parent context"),
        (
            '[{"app_id": "ap-protocol-smoke", "parent": "' + _SPAN_ID + '", "started_at_ns": 0}]',
            "needs its actual parent context",
        ),
    ],
    ids=["object-not-array", "string-binding", "missing-parent", "string-parent"],
)
def test_model_session_bindings_refuse_bindings_without_parent_context(raw, message):
    with pytest.raises(ValueError, match=message):
        bridge.model_session_bindings(raw)


def test_model_session_bindings_load_operator_supplied_windows():
    raw = json.dumps(
        [
            {
                "app_id": " ap-protocol-smoke ",
                "parent": {
                    "trace_id": _TRACE_ID.upper(),
                    "span_id": _SPAN_ID,
                    "session_id": "shared-model-session",
                },
                "started_at_ns": 10,
                "ended_at_ns": 20,
            },
            {
                "app_id": "ap-open-window",
                "parent": {
                    "trace_id": _TRACE_ID,
                    "span_id": _SPAN_ID,
                    "session_id": "second-model-session",
                },
                "started_at_ns": 30,
            },
        ]
    )

    first, second = bridge.model_session_bindings(raw)

    assert first.app_id == "ap-protocol-smoke"
    assert first.parent.trace_id == _TRACE_ID
    assert (first.started_at_ns, first.ended_at_ns) == (10, 20)
    assert second.parent.session_id == "second-model-session"
    assert second.ended_at_ns is None


def test_health_reports_transport_without_auth_or_upstream_call(config):
    with TestClient(bridge.create_bridge_app(config, exchange=_forbidden_exchange)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "purpose": "credential_transport_not_model_health"}


@pytest.mark.parametrize(
    "body",
    [
        b"\xff",
        b"not-json",
        b"[]",
        json.dumps({"model": "other/model", "messages": []}).encode(),
        json.dumps({"messages": []}).encode(),
        json.dumps({"model": "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B", "stream": True}).encode(),
        json.dumps({"model": "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B", "stream": None}).encode(),
    ],
    ids=["not-utf8", "not-json", "not-object", "wrong-model", "no-model", "stream-true", "stream-null"],
)
def test_chat_rejects_unrelayable_bodies_before_model_call(config, body):
    with TestClient(bridge.create_bridge_app(config, exchange=_forbidden_exchange)) as client:
        response = client.post(
            "/v1/chat/completions",
            content=body,
            headers={"Authorization": "Bearer " + config.chat_capability},
        )
    assert response.status_code == 400


def test_streamed_native_body_over_frame_limit_is_refused(config, monkeypatch):
    payload = _native_metric().SerializeToString()
    monkeypatch.setattr(bridge, "_MAX_FRAME", len(payload) - 1)
    with TestClient(bridge.create_bridge_app(config, exchange=_forbidden_exchange)) as client:
        response = _post(client, config, payload)
    assert response.status_code == 413
    assert response.text == "native OTLP frame too large"


@pytest.mark.parametrize("encoding", ["br", "x-gzip"])
def test_unsupported_content_encoding_is_refused(config, encoding):
    payload = gzip.compress(_native_metric().SerializeToString())
    with TestClient(bridge.create_bridge_app(config, exchange=_forbidden_exchange)) as client:
        response = _post(client, config, payload, **{"Content-Encoding": encoding})
    assert response.status_code == 400


def test_gzip_native_metric_is_decompressed_and_exported(config):
    exported = []

    def accept(request, **_kwargs):
        exported.append(
            bridge.decode_otlp(request.data, "application/x-protobuf", ExportTraceServiceRequest)
        )
        return 200, b"", "application/x-protobuf"

    payload = gzip.compress(_native_metric().SerializeToString())
    with TestClient(bridge.create_bridge_app(config, exchange=accept)) as client:
        response = _post(client, config, payload, **{"Content-Encoding": "GZIP"})
    assert response.status_code == 200
    assert len(exported) == 1
    spans = exported[0].resource_spans[0].scope_spans[0].spans
    assert len(spans) == 1
    assert spans[0].parent_span_id.hex() == _SPAN_ID


def test_metric_without_data_is_skipped_not_exported(config):
    request = _native_metric()
    scope = request.resource_metrics[0].scope_metrics[0]
    scope.metrics.clear()
    scope.metrics.add(name="daytona.sandbox.descriptor-only", unit="%")
    with TestClient(bridge.create_bridge_app(config, exchange=_forbidden_exchange)) as client:
        response = _post(client, config, request.SerializeToString())
    assert response.status_code == 200
    acknowledgment = ExportMetricsServiceResponse.FromString(response.content)
    assert not acknowledgment.HasField("partial_success")


def test_converted_frame_over_limit_is_refused_before_laminar(config, monkeypatch):
    payload = _native_metric().SerializeToString()
    # The input fits exactly; the converted span frame is larger than the input.
    monkeypatch.setattr(bridge, "_MAX_FRAME", len(payload))
    with TestClient(bridge.create_bridge_app(config, exchange=_forbidden_exchange)) as client:
        response = _post(client, config, payload)
    assert response.status_code == 413
    assert response.text == "converted native OTLP frame too large"


def _native_trace_json():
    span = {
        "traceId": _TRACE_ID,
        "spanId": _SPAN_ID,
        "name": "agent.step",
        "startTimeUnixNano": "1770000000000000000",
        "endTimeUnixNano": "1770000000000000001",
    }
    return json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}).encode()


def _post_traces(client, config, payload):
    return client.post(
        "/v1/traces",
        content=payload,
        headers={
            "Authorization": "Bearer " + config.telemetry_capability,
            "Content-Type": "application/json",
        },
    )


@pytest.mark.parametrize(
    "reply",
    [
        (200, b"", "application/x-protobuf"),
        (202, b"{}", "application/json; charset=utf-8"),
        (200, b'{"status":"accepted"}', "application/json"),
    ],
    ids=["empty-protobuf", "empty-json-otlp", "plain-json-http"],
)
def test_native_json_traces_are_forwarded_and_answered_as_json(config, reply):
    exported = []

    def accept(request, *, timeout):
        assert timeout == 2
        exported.append(request)
        return reply

    with TestClient(bridge.create_bridge_app(config, exchange=accept)) as client:
        response = _post_traces(client, config, _native_trace_json())
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {}
    (request,) = exported
    assert request.full_url == bridge._CLOUD_BASE.rstrip("/") + "/v1/traces"
    assert request.get_header("Authorization") == "Bearer " + config.laminar_api_key
    assert request.get_header("Content-type") == "application/x-protobuf"
    forwarded = bridge.decode_otlp(request.data, "application/x-protobuf", ExportTraceServiceRequest)
    span = forwarded.resource_spans[0].scope_spans[0].spans[0]
    assert (span.name, span.trace_id.hex(), span.span_id.hex()) == ("agent.step", _TRACE_ID, _SPAN_ID)


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ((200, b"{not-json", "application/json"), "invalid Laminar acknowledgment"),
        ((200, b"\xff\xfe", "application/json"), "invalid Laminar acknowledgment"),
        ((200, b"\xff", "application/x-protobuf"), "invalid Laminar OTLP acknowledgment"),
        (
            (200, b'{"partialSuccess": {"unknownField": 1}}', "application/json"),
            "invalid Laminar OTLP acknowledgment",
        ),
    ],
    ids=["json-unparseable", "json-not-utf8", "protobuf-undecodable", "json-not-otlp-schema"],
)
@pytest.mark.parametrize("route", ["traces", "metrics"])
def test_bad_laminar_acknowledgment_is_not_relayed_as_success(config, reply, message, route):
    def exchange(*args, **kwargs):
        return reply

    with TestClient(bridge.create_bridge_app(config, exchange=exchange)) as client:
        if route == "traces":
            response = _post_traces(client, config, _native_trace_json())
        else:
            response = _post(client, config, _native_metric().SerializeToString())
    assert response.status_code == 502
    assert response.text == message


def _native_log(text):
    request = ExportLogsServiceRequest()
    record = request.resource_logs.add().scope_logs.add().log_records.add()
    record.time_unix_nano = 1770000000000000000
    record.body.string_value = text
    return request


def test_native_logs_copy_sanitized_laminar_partial_success(config):
    exported = []
    upstream = ExportLogsServiceResponse()
    upstream.partial_success.rejected_log_records = 2
    upstream.partial_success.error_message = "dropped for " + config.laminar_api_key

    def exchange(request, **_kwargs):
        exported.append(request)
        return 200, upstream.SerializeToString(), "application/x-protobuf"

    payload = _native_log("agent saw " + config.model_api_key).SerializeToString()
    with TestClient(bridge.create_bridge_app(config, exchange=exchange)) as client:
        response = client.post(
            "/v1/logs",
            content=payload,
            headers={
                "Authorization": "Bearer " + config.telemetry_capability,
                "Content-Type": "application/x-protobuf",
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-protobuf"
    acknowledgment = ExportLogsServiceResponse.FromString(response.content)
    assert acknowledgment.partial_success.rejected_log_records == 2
    assert "dropped for" in acknowledgment.partial_success.error_message
    assert config.laminar_api_key not in acknowledgment.partial_success.error_message
    (request,) = exported
    assert request.full_url.endswith("/v1/logs")
    forwarded = bridge.decode_otlp(request.data, "application/x-protobuf", ExportLogsServiceRequest)
    body = forwarded.resource_logs[0].scope_logs[0].log_records[0].body.string_value
    assert body.startswith("agent saw ")
    assert config.model_api_key not in body
