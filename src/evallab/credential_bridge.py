"""Existing model credential relay with native OTLP adoption into Laminar.

Metric observations are converted to child spans, not sent to Laminar's no-op
metrics endpoint. Ingest acknowledgments follow synchronous upstream acceptance;
there is no second collector, store, durable queue, or model-answer retry.
"""

from __future__ import annotations

import asyncio
import gzip
import hmac
import io
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, Response
from google.protobuf.json_format import MessageToJson
from google.protobuf.message import Message
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

from evallab.laminar_tracing import _CLOUD_BASE, _MAX_FRAME, _Sanitizer
from evallab.native_telemetry import (
    ModelSessionBinding,
    TelemetryParent,
    decode_otlp,
    metrics_to_spans,
)


@dataclass(frozen=True)
class BridgeConfig:
    """Capabilities stay in the trusted relay, never in model request metadata."""

    model_id: str
    model_upstream: str
    model_api_key: str = field(repr=False)
    chat_capability: str = field(repr=False)
    telemetry_capability: str | None = field(default=None, repr=False)
    laminar_api_key: str | None = field(default=None, repr=False)
    model_sessions: tuple[ModelSessionBinding, ...] = ()
    laminar_base_url: str = _CLOUD_BASE

    def __post_init__(self) -> None:
        if not self.model_api_key or not self.chat_capability:
            raise ValueError("Model credentials are required")
        if bool(self.telemetry_capability) != bool(self.laminar_api_key):
            raise ValueError("Native ingestion needs both its capability and Laminar key")
        if self.telemetry_capability == self.chat_capability:
            raise ValueError("Native ingest and model chat must have separate capabilities")


def model_session_bindings(raw: str) -> tuple[ModelSessionBinding, ...]:
    """Load real app/SDK context bindings supplied by the trusted operator."""
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("Model-session bindings must be a JSON array")
    result = []
    for item in payload:
        if not isinstance(item, Mapping) or not isinstance(item.get("parent"), Mapping):
            raise ValueError("Each model-session binding needs its actual parent context")
        result.append(
            ModelSessionBinding(
                app_id=item["app_id"],
                parent=TelemetryParent(**item["parent"]),
                started_at_ns=item["started_at_ns"],
                ended_at_ns=item.get("ended_at_ns"),
            )
        )
    return tuple(result)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _exchange(
    request: urllib.request.Request, *, timeout: float, native: bool = True
) -> tuple[int, bytes, str]:
    """One request; native export cannot redirect the Laminar credential."""
    open_request = (
        urllib.request.build_opener(_NoRedirect).open if native else urllib.request.urlopen
    )
    try:
        with open_request(request, timeout=timeout) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        try:
            return error.code, error.read(), error.headers.get("Content-Type", "")
        finally:
            error.close()


def _otlp_response(message: Message, *, as_json: bool) -> Response:
    if as_json:
        return Response(MessageToJson(message), media_type="application/json")
    return Response(message.SerializeToString(), media_type="application/x-protobuf")


async def _native_body(request: Request) -> bytes:
    # Bound both the received stream and decompressed body before protobuf/JSON parsing.
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > _MAX_FRAME:
            raise OverflowError("Native OTLP request exceeds the bridge frame limit")
        chunks.extend(chunk)
    encoding = request.headers.get("content-encoding", "identity").lower()
    if encoding == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(chunks)) as compressed:
            body = compressed.read(_MAX_FRAME + 1)
        if len(body) > _MAX_FRAME:
            raise OverflowError("Decompressed native OTLP exceeds the bridge frame limit")
        return body
    if encoding != "identity":
        raise ValueError("Unsupported Content-Encoding")
    return bytes(chunks)


def _metric_batches(request: ExportMetricsServiceRequest):
    """Bound conversion expansion without copying unrelated source points."""
    for resource in request.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                kind = metric.WhichOneof("data")
                if kind is None:
                    continue
                descriptor = type(metric)()
                descriptor.CopyFrom(metric)
                getattr(descriptor, kind).ClearField("data_points")
                points = getattr(metric, kind).data_points
                for offset in range(0, len(points), 64):
                    batch = ExportMetricsServiceRequest()
                    out_resource = batch.resource_metrics.add(schema_url=resource.schema_url)
                    out_resource.resource.CopyFrom(resource.resource)
                    out_scope = out_resource.scope_metrics.add(schema_url=scope.schema_url)
                    if scope.HasField("scope"):
                        out_scope.scope.CopyFrom(scope.scope)
                    out_metric = out_scope.metrics.add()
                    out_metric.CopyFrom(descriptor)
                    getattr(out_metric, kind).data_points.extend(points[offset : offset + 64])
                    yield batch


def create_bridge_app(
    config: BridgeConfig,
    *,
    exchange: Callable[..., tuple[int, bytes, str]] = _exchange,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> FastAPI:
    """Adopt the existing /ready + exactly-once credential relay and add OTLP."""
    app = FastAPI()
    sanitizer = _Sanitizer(
        secrets=(
            config.model_api_key,
            config.chat_capability,
            config.telemetry_capability,
            config.laminar_api_key,
        )
    )

    def authorized(request: Request, capability: str | None) -> bool:
        return bool(capability) and hmac.compare_digest(
            request.headers.get("authorization", "").encode(),
            ("Bearer " + (capability or "")).encode(),
        )

    @app.get("/health")
    async def health():
        return {"ok": True, "purpose": "credential_transport_not_model_health"}

    @app.get("/ready")
    async def ready(request: Request):
        if not authorized(request, config.chat_capability):
            return Response(status_code=401)

        def wait():
            started = clock()
            failures: dict[str, int] = {}
            while clock() - started < 1200:
                req = urllib.request.Request(
                    config.model_upstream.rstrip("/") + "/health",
                    headers={"Authorization": "Bearer " + config.model_api_key},
                )
                try:
                    status, _, _ = exchange(req, timeout=10, native=False)
                    if status == 200:
                        return {
                            "ready": True,
                            "elapsed_s": clock() - started,
                            "health_failures": failures,
                            "model_requests": 0,
                        }
                    cause = str(status)
                except (OSError, TimeoutError) as error:
                    cause = type(error).__name__
                failures[cause] = failures.get(cause, 0) + 1
                sleep(2)
            return {
                "ready": False,
                "elapsed_s": clock() - started,
                "health_failures": failures,
                "model_requests": 0,
            }

        return await asyncio.to_thread(wait)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        if not authorized(request, config.chat_capability):
            return Response(status_code=401)
        body = await request.body()
        try:
            parsed = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return Response(status_code=400)
        if (
            not isinstance(parsed, dict)
            or parsed.get("model") != config.model_id
            or parsed.get("stream", False) is not False
        ):
            return Response(status_code=400)

        def forward():
            req = urllib.request.Request(
                config.model_upstream.rstrip("/") + "/v1/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + config.model_api_key,
                },
                method="POST",
            )
            status, content, _ = exchange(req, timeout=3600, native=False)
            return Response(content=content, status_code=status, media_type="application/json")

        return await asyncio.to_thread(forward)

    async def native(request: Request, signal: str):
        if not authorized(request, config.telemetry_capability):
            return Response(status_code=401)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"application/x-protobuf", "application/json"}:
            return Response("unsupported OTLP content type", status_code=415)
        request_type = {
            "metrics": ExportMetricsServiceRequest,
            "traces": ExportTraceServiceRequest,
            "logs": ExportLogsServiceRequest,
        }[signal]
        try:
            body = await _native_body(request)
            payload = decode_otlp(body, content_type, request_type)
        except OverflowError:
            return Response("native OTLP frame too large", status_code=413)
        except (ValueError, OSError, EOFError):
            return Response("invalid OTLP payload or encoding", status_code=400)
        response_type = {
            "metrics": ExportMetricsServiceResponse,
            "traces": ExportTraceServiceResponse,
            "logs": ExportLogsServiceResponse,
        }[signal]
        result = response_type()
        frames = (
            _metric_batches(payload)
            if isinstance(payload, ExportMetricsServiceRequest)
            else (payload,)
        )
        for frame in frames:
            destination = signal
            if isinstance(frame, ExportMetricsServiceRequest) and isinstance(
                result, ExportMetricsServiceResponse
            ):
                conversion = metrics_to_spans(frame, model_sessions=config.model_sessions)
                frame = conversion.request
                destination = "traces"
                if conversion.rejected_data_points:
                    result.partial_success.rejected_data_points += conversion.rejected_data_points
                    result.partial_success.error_message = sanitizer.text(
                        "; ".join(
                            text
                            for text in (
                                result.partial_success.error_message,
                                conversion.error_message,
                            )
                            if text
                        )
                    )
            if not frame.ListFields():
                continue
            sanitizer.protobuf(frame)
            outbound = frame.SerializeToString()
            if len(outbound) > _MAX_FRAME:
                return Response("converted native OTLP frame too large", status_code=413)
            req = urllib.request.Request(
                config.laminar_base_url.rstrip("/") + "/v1/" + destination,
                data=outbound,
                headers={
                    "Content-Type": "application/x-protobuf",
                    "Authorization": "Bearer " + (config.laminar_api_key or ""),
                },
                method="POST",
            )
            try:
                status, reply_body, reply_type = await asyncio.to_thread(exchange, req, timeout=2)
            except (OSError, TimeoutError):
                return Response("Laminar native export unavailable", status_code=503)
            if not 200 <= status < 300:
                # No success ACK before Laminar accepts every converted frame.
                return Response("Laminar native export unavailable", status_code=503)
            reply_type = reply_type.split(";", 1)[0].strip().lower()
            otlp_acknowledgment = reply_type == "application/x-protobuf"
            if reply_body and reply_type == "application/json":
                try:
                    acknowledgment = json.loads(reply_body)
                except (ValueError, UnicodeDecodeError):
                    return Response("invalid Laminar acknowledgment", status_code=502)
                # Laminar also returns ordinary JSON HTTP acknowledgments.
                otlp_acknowledgment = isinstance(acknowledgment, dict) and (
                    not acknowledgment
                    or "partialSuccess" in acknowledgment
                    or "partial_success" in acknowledgment
                )
            if reply_body and otlp_acknowledgment:
                upstream_response_type = (
                    ExportTraceServiceResponse
                    if destination == "traces"
                    else ExportLogsServiceResponse
                )
                try:
                    upstream = decode_otlp(reply_body, reply_type, upstream_response_type)
                except ValueError:
                    return Response("invalid Laminar OTLP acknowledgment", status_code=502)
                if upstream.HasField("partial_success"):
                    if isinstance(result, ExportMetricsServiceResponse) and isinstance(
                        upstream, ExportTraceServiceResponse
                    ):
                        result.partial_success.rejected_data_points += (
                            upstream.partial_success.rejected_spans
                        )
                        result.partial_success.error_message = sanitizer.text(
                            "; ".join(
                                message
                                for message in (
                                    result.partial_success.error_message,
                                    upstream.partial_success.error_message,
                                )
                                if message
                            )
                        )
                    else:
                        result.partial_success.CopyFrom(upstream.partial_success)
                        result.partial_success.error_message = sanitizer.text(
                            result.partial_success.error_message
                        )
        return _otlp_response(result, as_json=content_type == "application/json")

    @app.post("/v1/metrics")
    async def metrics(request: Request):
        return await native(request, "metrics")

    @app.post("/v1/traces")
    async def traces(request: Request):
        return await native(request, "traces")

    @app.post("/v1/logs")
    async def logs(request: Request):
        return await native(request, "logs")

    return app
