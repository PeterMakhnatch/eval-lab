"""Native ingress rejects unsafe/malformed input and never falsely acknowledges loss."""

from __future__ import annotations

import gzip
import json
from dataclasses import replace

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
