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
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
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
