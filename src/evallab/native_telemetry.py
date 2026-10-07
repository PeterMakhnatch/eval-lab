"""Native OTLP metric observations represented in the existing Laminar store.

Sandbox points bind to actual Harbor roots; model points bind only to a real,
time-bounded shared model session. Original descriptors, aggregation fields,
timestamps, resource/scope identities and point payloads remain in span output.
Observation spans have zero duration and are not grades, costs or allocations.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from google.protobuf.json_format import MessageToDict, Parse, ParseError
from google.protobuf.message import Message
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    InstrumentationScope,
    KeyValue,
)
from opentelemetry.proto.metrics.v1.metrics_pb2 import AggregationTemporality
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

__all__ = [
    "MetricConversion",
    "ModelSessionBinding",
    "TelemetryParent",
    "UnsupportedContentTypeError",
    "decode_otlp",
    "metrics_to_spans",
]


_CONVERTER_SCOPE = "evallab.native_telemetry"
_CONVERTER_VERSION = "1"

_DAYTONA_PREFIX = "daytona.sandbox."

LABEL_TRACE_ID = "evallab.trace_id"
LABEL_PARENT_SPAN_ID = "evallab.parent_span_id"
LABEL_SESSION_ID = "evallab.session_id"
LABEL_TRIAL_ID = "trial_id"
LABEL_MODEL_SESSION = "model_session"
LABEL_APP_ID = "app_id"
LABEL_CONTAINER_ID = "container_id"
LABEL_SANDBOX_ID = "service.instance.id"

_TRACE_RE = re.compile(r"[0-9a-fA-F]{32}")
_SPAN_RE = re.compile(r"[0-9a-fA-F]{16}")
_HEX_RUN = re.compile(r"[0-9a-fA-F]+")

_TRACE_JSON_KEYS = frozenset({"traceId", "trace_id"})
_SPAN_JSON_KEYS = frozenset({"spanId", "span_id", "parentSpanId", "parent_span_id"})

_METRIC_DATA_FIELDS = ("gauge", "sum", "histogram", "exponential_histogram", "summary")

ATTR_SPAN_TYPE = "lmnr.span.type"
ATTR_SESSION_ID = "lmnr.association.properties.session_id"
ATTR_OUTPUT = "lmnr.span.output"
META = "lmnr.association.properties.metadata."
EVENT_NATIVE_METRIC = "native.metric"


class UnsupportedContentTypeError(ValueError):
    """Raised when the OTLP content type is neither protobuf nor JSON."""


class _IdentityError(ValueError):
    """Internal: a data point carries missing/conflicting/unbound identity."""


def _valid_trace_id(value: str) -> bool:
    return bool(_TRACE_RE.fullmatch(value)) and set(value) != {"0"}


def _valid_span_id(value: str) -> bool:
    return bool(_SPAN_RE.fullmatch(value)) and set(value) != {"0"}


@dataclass(frozen=True)
class TelemetryParent:
    """ACTUAL parent context a converted observation span hangs under.

    Trace IDs are canonical 32-hex nonzero, span IDs 16-hex nonzero (case
    insensitive, normalized to lowercase). Malformed IDs raise rather than
    silently rooting a new trace.
    """

    trace_id: str
    span_id: str
    session_id: str
    trial_id: str | None = None
    model_session: str | None = None

    def __post_init__(self) -> None:
        trace_id = str(self.trace_id).strip().lower()
        span_id = str(self.span_id).strip().lower()
        session_id = str(self.session_id).strip() if self.session_id is not None else ""
        if not _valid_trace_id(trace_id):
            raise ValueError(f"malformed evallab.trace_id: {self.trace_id!r}")
        if not _valid_span_id(span_id):
            raise ValueError(f"malformed evallab.parent_span_id: {self.span_id!r}")
        if not session_id:
            raise ValueError("missing evallab.session_id")
        trial_id = self.trial_id.strip() if isinstance(self.trial_id, str) else self.trial_id
        if trial_id is not None and not str(trial_id).strip():
            raise ValueError("empty trial_id")
        model_session = (
            self.model_session.strip()
            if isinstance(self.model_session, str)
            else self.model_session
        )
        if model_session is not None and not str(model_session).strip():
            raise ValueError("empty model_session")
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "span_id", span_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "trial_id", trial_id)
        object.__setattr__(self, "model_session", model_session)


@dataclass(frozen=True)
class ModelSessionBinding:
    """Real app/session context for shared Modal model measurements.

    A binding covers observation times ``started_at_ns <= t <= ended_at_ns``
    (open-ended when ``ended_at_ns`` is None). The binding parent carries the
    model session identity; there is deliberately no model ``trial_id`` for
    shared measurements, and converted model spans never emit one.
    """

    app_id: str
    parent: TelemetryParent
    started_at_ns: int
    ended_at_ns: int | None = None

    def __post_init__(self) -> None:
        app_id = str(self.app_id).strip() if self.app_id is not None else ""
        if not app_id:
            raise ValueError("missing app_id")
        if not isinstance(self.parent, TelemetryParent):
            raise ValueError("parent must be a TelemetryParent")
        if self.parent.trial_id is not None:
            raise ValueError("Shared model observations cannot bind to an exclusive trial parent")
        started = self.started_at_ns
        if isinstance(started, bool) or not isinstance(started, int) or started < 0:
            raise ValueError(f"invalid started_at_ns: {started!r}")
        ended = self.ended_at_ns
        if ended is not None and (
            isinstance(ended, bool) or not isinstance(ended, int) or ended < started
        ):
            raise ValueError(f"invalid ended_at_ns: {ended!r}")
        object.__setattr__(self, "app_id", app_id)

    def covers(self, time_unix_nano: int) -> bool:
        """Return True when an observation time falls inside this window."""
        if time_unix_nano < self.started_at_ns:
            return False
        return self.ended_at_ns is None or time_unix_nano <= self.ended_at_ns


@dataclass
class MetricConversion:
    """Converted trace request plus explicit accounting for skipped points."""

    request: ExportTraceServiceRequest
    rejected_data_points: int = 0
    error_message: str = ""


def _hex_to_b64(value: str, raw_len: int) -> str:
    """Native OTLP JSON IDs are hexadecimal; AnyValue bytes remain base64."""
    if not value:
        return value
    if len(value) != raw_len or not _HEX_RUN.fullmatch(value):
        raise ValueError("Invalid hexadecimal OTLP trace or span ID")
    return base64.b64encode(bytes.fromhex(value)).decode("ascii")


def _normalize_otlp_json_ids(node: Any) -> Any:
    """Recursively rewrite hex trace/span ID strings to base64 in OTLP JSON."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, item in node.items():
            item = _normalize_otlp_json_ids(item)
            if isinstance(item, str):
                if key in _TRACE_JSON_KEYS:
                    item = _hex_to_b64(item, 32)
                elif key in _SPAN_JSON_KEYS:
                    item = _hex_to_b64(item, 16)
            out[key] = item
        return out
    if isinstance(node, list):
        return [_normalize_otlp_json_ids(item) for item in node]
    return node


def decode_otlp[M: Message](body: bytes, content_type: str, message_type: type[M]) -> M:
    """Decode an OTLP/HTTP body into an instance of ``message_type``.

    Accepts ``application/x-protobuf`` and ``application/json`` (optional
    ``;charset=...`` parameters allowed). Native OTLP JSON hex
    ``traceId``/``spanId`` spellings -- including exemplars and snake/camel
    field aliases -- are converted to the base64 form vanilla protobuf JSON
    parsing expects; other ``bytesValue`` payloads stay base64. Unknown fields
    are never swallowed and malformed bodies fail.
    """
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise TypeError(f"OTLP body must be bytes, got {type(body).__name__}")
    raw = bytes(body)
    if not isinstance(content_type, str):
        raise ValueError(f"invalid content type: {content_type!r}")
    base = content_type.split(";", 1)[0].strip().lower()

    if base == "application/x-protobuf":
        message = message_type()
        try:
            message.ParseFromString(raw)
        except Exception as exc:
            raise ValueError(f"malformed OTLP protobuf body: {exc}") from exc
        return message

    if base == "application/json":
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"OTLP JSON body is not UTF-8: {exc}") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed OTLP JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("malformed OTLP JSON body: top-level value must be an object")
        normalized = _normalize_otlp_json_ids(payload)
        message = message_type()
        try:
            Parse(json.dumps(normalized), message, ignore_unknown_fields=False)
        except ParseError as exc:
            raise ValueError(f"malformed OTLP JSON body: {exc}") from exc
        return message

    raise UnsupportedContentTypeError(f"unsupported OTLP content type: {content_type!r}")


def _scalar_str(value: AnyValue) -> str | None:
    """Render a scalar label value for identity matching; None if not scalar."""
    kind = value.WhichOneof("value")
    if kind == "string_value":
        return value.string_value
    if kind == "int_value":
        return str(value.int_value)
    if kind == "bool_value":
        return "true" if value.bool_value else "false"
    if kind == "double_value":
        return repr(value.double_value)
    if kind == "bytes_value":
        try:
            return bytes(value.bytes_value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _label_strings(kvs: Any) -> dict[str, str]:
    """Reject ambiguous identity labels rather than choosing the last value."""
    identity_keys = {
        LABEL_TRACE_ID,
        LABEL_PARENT_SPAN_ID,
        LABEL_SESSION_ID,
        LABEL_TRIAL_ID,
        LABEL_MODEL_SESSION,
        LABEL_APP_ID,
        LABEL_CONTAINER_ID,
        LABEL_SANDBOX_ID,
    }
    out: dict[str, str] = {}
    for kv in kvs:
        text = _scalar_str(kv.value)
        if kv.key in identity_keys and (text is None or (kv.key in out and out[kv.key] != text)):
            raise _IdentityError(f"conflicting or non-scalar identity label: {kv.key}")
        if text is not None:
            out[kv.key] = text
    return out


def _string_attr(key: str, text: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=text))


def _metric_data_field(metric: Any) -> str | None:
    for name in _METRIC_DATA_FIELDS:
        if metric.HasField(name):
            return name
    return None


def _temporality_name(data: Any) -> str | None:
    descriptor = data.DESCRIPTOR.fields_by_name.get("aggregation_temporality")
    if descriptor is None:
        return None
    try:
        return AggregationTemporality.Name(data.aggregation_temporality)
    except ValueError:
        return str(data.aggregation_temporality)


def _parent_from_resource_labels(labels: dict[str, str]) -> TelemetryParent:
    """Build the ACTUAL trial parent from trusted startup resource labels."""
    missing = [
        key
        for key in (LABEL_TRACE_ID, LABEL_PARENT_SPAN_ID, LABEL_SESSION_ID, LABEL_TRIAL_ID)
        if not labels.get(key)
    ]
    if missing:
        raise _IdentityError(f"missing trial context label(s): {', '.join(missing)}")
    try:
        return TelemetryParent(
            trace_id=labels[LABEL_TRACE_ID],
            span_id=labels[LABEL_PARENT_SPAN_ID],
            session_id=labels[LABEL_SESSION_ID],
            trial_id=labels[LABEL_TRIAL_ID],
            model_session=labels.get(LABEL_MODEL_SESSION) or None,
        )
    except ValueError as exc:
        raise _IdentityError(f"invalid trial context: {exc}") from exc


def _deterministic_span_id(
    parent: TelemetryParent,
    resource: Resource,
    resource_schema_url: str,
    scope: InstrumentationScope | None,
    scope_schema_url: str,
    metric: Any,
    metric_type: str,
    point: Message,
) -> str:
    """Deterministic nonzero 8-byte span ID over parent + complete source.

    Repeated export of the identical point under the same parent yields the
    same identity; any change to the point, descriptor, resource, scope, or
    parent yields a different one.
    """
    digest = hashlib.sha256()
    digest.update(b"evallab.native_telemetry/v1\x00")
    digest.update(parent.trace_id.encode("ascii"))
    digest.update(b"\x00")
    digest.update(parent.span_id.encode("ascii"))
    digest.update(b"\x00")
    digest.update(parent.session_id.encode("utf-8"))
    digest.update(b"\x00")
    digest.update((parent.trial_id or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update((parent.model_session or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update(resource.SerializeToString(deterministic=True))
    digest.update(b"\x00")
    digest.update(resource_schema_url.encode("utf-8"))
    digest.update(b"\x00")
    if scope is not None:
        digest.update(scope.SerializeToString(deterministic=True))
    digest.update(b"\x00")
    digest.update(scope_schema_url.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(metric.SerializeToString(deterministic=True))
    digest.update(b"\x00")
    digest.update(metric_type.encode("ascii"))
    digest.update(b"\x00")
    digest.update(point.SerializeToString(deterministic=True))
    candidate = bytearray(digest.digest()[:8])
    if all(byte == 0 for byte in candidate):
        candidate[-1] = 1
    return bytes(candidate).hex()


def _point_value_summary(metric_type: str, point: Any) -> str:
    """Compact exact value summary for the native.metric event."""
    if metric_type in ("gauge", "sum"):
        if point.HasField("as_int"):
            return str(point.as_int)
        if point.HasField("as_double"):
            return repr(point.as_double)
        return "unknown"
    if metric_type == "histogram":
        parts = [f"count={point.count}"]
        if point.HasField("sum"):
            parts.append(f"sum={repr(point.sum)}")
        return " ".join(parts)
    if metric_type == "exponential_histogram":
        parts = [f"count={point.count}"]
        if point.HasField("sum"):
            parts.append(f"sum={repr(point.sum)}")
        return " ".join(parts)
    if metric_type == "summary":
        return f"count={point.count} sum={repr(point.sum)}"
    return "unknown"


def _resolve_daytona(
    metric_name: str,
    resource_labels: dict[str, str],
    point_labels: dict[str, str],
) -> tuple[TelemetryParent, str]:
    """Resolve per-trial sandbox identity; raise _IdentityError when unbound."""
    app_id = resource_labels.get(LABEL_APP_ID) or point_labels.get(LABEL_APP_ID)
    container_id = resource_labels.get(LABEL_CONTAINER_ID) or point_labels.get(LABEL_CONTAINER_ID)
    if app_id is not None or container_id is not None:
        raise _IdentityError(
            f"conflicting provider identity on Daytona metric {metric_name!r}: "
            "Modal app/container labels present"
        )
    res_sandbox = resource_labels.get(LABEL_SANDBOX_ID)
    pt_sandbox = point_labels.get(LABEL_SANDBOX_ID)
    if res_sandbox and pt_sandbox and res_sandbox != pt_sandbox:
        raise _IdentityError(f"conflicting sandbox identity on Daytona metric {metric_name!r}")
    sandbox_id = pt_sandbox or res_sandbox
    if not sandbox_id:
        raise _IdentityError(
            f"unbound Daytona metric {metric_name!r}: "
            "org-level aggregate without service.instance.id"
        )
    for key in (
        LABEL_TRACE_ID,
        LABEL_PARENT_SPAN_ID,
        LABEL_SESSION_ID,
        LABEL_TRIAL_ID,
        LABEL_MODEL_SESSION,
    ):
        if key in point_labels and point_labels[key] != resource_labels.get(key):
            raise _IdentityError(f"conflicting point/resource identity: {key}")
    return _parent_from_resource_labels(resource_labels), sandbox_id


def _resolve_model(
    metric_name: str,
    resource_labels: dict[str, str],
    point_labels: dict[str, str],
    observation_time: int,
    model_sessions: Sequence[ModelSessionBinding],
) -> tuple[TelemetryParent, str, str | None]:
    """Resolve the single shared-model binding; raise _IdentityError if not."""
    if any(key.startswith("daytona_") for key in resource_labels):
        raise _IdentityError(f"conflicting Daytona resource on model metric {metric_name!r}")
    res_app = resource_labels.get(LABEL_APP_ID)
    pt_app = point_labels.get(LABEL_APP_ID)
    if res_app and pt_app and res_app != pt_app:
        raise _IdentityError(f"conflicting app identity on model metric {metric_name!r}")
    app_id = pt_app or res_app
    res_container = resource_labels.get(LABEL_CONTAINER_ID)
    pt_container = point_labels.get(LABEL_CONTAINER_ID)
    if res_container and pt_container and res_container != pt_container:
        raise _IdentityError(f"conflicting container identity on model metric {metric_name!r}")
    container_id = pt_container or res_container
    if not app_id:
        raise _IdentityError(
            f"unbound model metric {metric_name!r}: no app_id at resource or point"
        )
    same_app = [binding for binding in model_sessions if binding.app_id == app_id]
    if not same_app:
        raise _IdentityError(f"unknown app_id {app_id!r} on model metric {metric_name!r}")
    matches = [binding for binding in same_app if binding.covers(observation_time)]
    if not matches:
        raise _IdentityError(
            f"observation {observation_time} outside model session window(s) "
            f"for app_id {app_id!r} on model metric {metric_name!r}"
        )
    if len(matches) > 1:
        raise _IdentityError(
            f"ambiguous model session binding for app_id {app_id!r} "
            f"at {observation_time} on model metric {metric_name!r}"
        )
    return matches[0].parent, app_id, container_id


def metrics_to_spans(
    request: ExportMetricsServiceRequest,
    *,
    model_sessions: Sequence[ModelSessionBinding] = (),
) -> MetricConversion:
    """Convert each bound source point once, with explicit rejection counts."""
    resource_spans: list[ResourceSpans] = []
    rejected = 0
    reasons: dict[str, int] = {}

    def reject(detail: str) -> None:
        nonlocal rejected
        rejected += 1
        reason = detail[:240]
        reasons[reason] = reasons.get(reason, 0) + 1

    for rm in request.resource_metrics:
        resource = rm.resource
        resource_error = None
        try:
            resource_labels = _label_strings(resource.attributes)
        except _IdentityError as error:
            resource_labels = {}
            resource_error = str(error)
        out_resource = ResourceSpans(schema_url=rm.schema_url)
        out_resource.resource.CopyFrom(resource)
        resource_payload = MessageToDict(resource)
        for sm in rm.scope_metrics:
            scope = sm.scope if sm.HasField("scope") else None
            scope_payload = MessageToDict(scope) if scope is not None else None
            out_scope = ScopeSpans(schema_url=sm.schema_url)
            if scope is not None:
                out_scope.scope.CopyFrom(scope)
            else:
                out_scope.scope.CopyFrom(
                    InstrumentationScope(name=_CONVERTER_SCOPE, version=_CONVERTER_VERSION)
                )
            for metric in sm.metrics:
                metric_type = _metric_data_field(metric)
                if metric_type is None:
                    # A descriptor with no points cannot contribute a rejected point.
                    continue
                data = getattr(metric, metric_type)
                temporality = _temporality_name(data)
                monotonic = (
                    bool(data.is_monotonic)
                    if "is_monotonic" in data.DESCRIPTOR.fields_by_name
                    else None
                )
                descriptor = type(metric)()
                descriptor.CopyFrom(metric)
                getattr(descriptor, metric_type).ClearField("data_points")
                descriptor_payload = MessageToDict(descriptor)
                for point in data.data_points:
                    observation_time = int(point.time_unix_nano)
                    if observation_time <= 0:
                        reject(f"missing observation time: {metric.name}")
                        continue
                    try:
                        if resource_error is not None:
                            raise _IdentityError(resource_error)
                        point_labels = _label_strings(point.attributes)
                        if metric.name.startswith(_DAYTONA_PREFIX):
                            parent, sandbox_id = _resolve_daytona(
                                metric.name, resource_labels, point_labels
                            )
                            provider = "daytona"
                            app_id = container_id = None
                            trial_id = parent.trial_id
                        elif metric.name.startswith(("modal.", "input_events.")):
                            parent, app_id, container_id = _resolve_model(
                                metric.name,
                                resource_labels,
                                point_labels,
                                observation_time,
                                model_sessions,
                            )
                            provider = "modal"
                            sandbox_id = trial_id = None
                        else:
                            raise _IdentityError(f"unsupported native metric family: {metric.name}")
                    except _IdentityError as error:
                        reject(str(error))
                        continue
                    span_id = _deterministic_span_id(
                        parent,
                        resource,
                        rm.schema_url,
                        scope,
                        sm.schema_url,
                        descriptor,
                        metric_type,
                        point,
                    )
                    payload = {
                        "metric": descriptor_payload,
                        "metric_type": metric_type,
                        "point": MessageToDict(point),
                        "resource": resource_payload,
                        "resource_schema_url": rm.schema_url,
                        "scope": scope_payload,
                        "scope_schema_url": sm.schema_url,
                        "aggregation_temporality": temporality,
                        "is_monotonic": monotonic,
                    }
                    metadata = {
                        "native_provider": provider,
                        "measurement_source": "native-provider",
                        "metric_name": metric.name,
                        "metric_type": metric_type,
                        "metric_unit": metric.unit,
                    }
                    if temporality is not None:
                        metadata["aggregation_temporality"] = temporality
                    if monotonic is not None:
                        metadata["monotonic"] = "true" if monotonic else "false"
                    model_session = parent.model_session or app_id
                    if model_session:
                        metadata["model_session"] = model_session
                    if trial_id is not None:
                        metadata["trial_id"] = trial_id
                    if sandbox_id is not None:
                        metadata["sandbox_id"] = sandbox_id
                    if app_id is not None:
                        metadata["app_id"] = app_id
                    if container_id is not None:
                        metadata["container_id"] = container_id
                    if provider == "modal":
                        metadata["shared_model_session"] = "true"
                    out_scope.spans.add(
                        trace_id=bytes.fromhex(parent.trace_id),
                        span_id=bytes.fromhex(span_id),
                        parent_span_id=bytes.fromhex(parent.span_id),
                        name=f"native.metric {metric.name}",
                        kind=Span.SPAN_KIND_INTERNAL,
                        start_time_unix_nano=observation_time,
                        end_time_unix_nano=observation_time,
                        attributes=[
                            _string_attr(ATTR_SPAN_TYPE, "DEFAULT"),
                            _string_attr(ATTR_SESSION_ID, parent.session_id),
                            *(_string_attr(META + key, value) for key, value in metadata.items()),
                            _string_attr(
                                ATTR_OUTPUT, json.dumps(payload, ensure_ascii=False, sort_keys=True)
                            ),
                        ],
                        events=[
                            Span.Event(
                                time_unix_nano=observation_time,
                                name=EVENT_NATIVE_METRIC,
                                attributes=[
                                    _string_attr("metric_name", metric.name),
                                    _string_attr("metric_type", metric_type),
                                    _string_attr("value", _point_value_summary(metric_type, point)),
                                ],
                            )
                        ],
                    )
            if out_scope.spans:
                out_resource.scope_spans.append(out_scope)
        if out_resource.scope_spans:
            resource_spans.append(out_resource)
    return MetricConversion(
        request=ExportTraceServiceRequest(resource_spans=resource_spans),
        rejected_data_points=rejected,
        error_message="; ".join(f"{reason} ({count})" for reason, count in reasons.items()),
    )
