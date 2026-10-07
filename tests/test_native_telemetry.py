"""Isolated behavioral tests for evallab.native_telemetry.

No network, no files, no clocks: every case builds OTLP messages in memory
with fixed IDs/timestamps and asserts converted span behavior or explicit
rejection accounting.
"""

from __future__ import annotations

import base64
import json

import pytest
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
from opentelemetry.proto.metrics.v1 import metrics_pb2
from opentelemetry.proto.resource.v1.resource_pb2 import Resource

from evallab.native_telemetry import (
    MetricConversion,
    ModelSessionBinding,
    TelemetryParent,
    UnsupportedContentTypeError,
    decode_otlp,
    metrics_to_spans,
)

TRACE = "1234567890abcdef1234567890abcdef"
PARENT_SPAN = "abcdef1234567890"
SESSION = "fixture-trial__env"
TRIAL = "fixture-trial"
SANDBOX = "550e8400-e29b-41d4-a716-446655440000"
APP = "app-01J9Z8X7Y6"
CONTAINER = "container-abc123"
T0 = 1728000000000000000
T_START = T0 - 600_000_000_000

CUMULATIVE = metrics_pb2.AggregationTemporality.Value("AGGREGATION_TEMPORALITY_CUMULATIVE")
DELTA = metrics_pb2.AggregationTemporality.Value("AGGREGATION_TEMPORALITY_DELTA")

META = "lmnr.association.properties.metadata."


def _str(key: str, text: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=text))


def _trial_resource(extra: dict[str, str] | None = None) -> Resource:
    attrs = {
        "service.instance.id": SANDBOX,
        "evallab.trace_id": TRACE,
        "evallab.parent_span_id": PARENT_SPAN,
        "evallab.session_id": SESSION,
        "trial_id": TRIAL,
    }
    attrs.update(extra or {})
    return Resource(attributes=[_str(k, v) for k, v in attrs.items()])


def _scope(name: str = "daytona-meter") -> InstrumentationScope:
    return InstrumentationScope(name=name, version="9")


def _gauge_metric(
    name: str,
    points: list,
    description: str = "a gauge",
    unit: str = "1",
) -> metrics_pb2.Metric:
    return metrics_pb2.Metric(
        name=name,
        description=description,
        unit=unit,
        gauge=metrics_pb2.Gauge(data_points=points),
    )


def _gauge_point(
    value: int,
    at: int = T0,
    start: int = T_START,
    extra_attrs: dict[str, str] | None = None,
    exemplars: list | None = None,
) -> metrics_pb2.NumberDataPoint:
    return metrics_pb2.NumberDataPoint(
        attributes=[_str(k, v) for k, v in (extra_attrs or {}).items()],
        start_time_unix_nano=start,
        time_unix_nano=at,
        as_int=value,
        exemplars=exemplars or [],
    )


def _convert(metric, resource=None, scope=None, **kwargs):
    request = ExportMetricsServiceRequest(
        resource_metrics=[
            metrics_pb2.ResourceMetrics(
                resource=resource or _trial_resource(),
                scope_metrics=[
                    metrics_pb2.ScopeMetrics(
                        scope=scope or _scope(),
                        metrics=[metric],
                    )
                ],
            )
        ]
    )
    conversion = metrics_to_spans(request, **kwargs)
    return conversion


def _only_span(conversion: MetricConversion):
    assert conversion.rejected_data_points == 0, conversion.error_message

    spans = conversion.request.resource_spans
    assert len(spans) == 1
    assert len(spans[0].scope_spans) == 1
    assert len(spans[0].scope_spans[0].spans) == 1
    return spans[0].scope_spans[0].spans[0]


def _attrs(span) -> dict[str, str]:
    return {kv.key: kv.value.string_value for kv in span.attributes}


def _binding(app_id: str = APP, start: int = T_START, end: int | None = None):
    return ModelSessionBinding(
        app_id=app_id,
        parent=TelemetryParent(
            trace_id=TRACE,
            span_id=PARENT_SPAN,
            session_id=SESSION,
            model_session="nightly-train",
        ),
        started_at_ns=start,
        ended_at_ns=end,
    )


# decode_otlp ---------------------------------------------------------------


def test_decode_json_camel_case_with_hex_exemplar():
    trace_hex = "0a" * 16
    span_hex = "bc" * 8
    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.instance.id", "value": {"stringValue": SANDBOX}},
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "m"},
                        "metrics": [
                            {
                                "name": "daytona.sandbox.cpu",
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": str(T0),
                                            "asInt": "42",
                                            "exemplars": [
                                                {
                                                    "filteredAttributes": [
                                                        {
                                                            "key": "k",
                                                            "value": {"stringValue": "v"},
                                                        }
                                                    ],
                                                    "timeUnixNano": str(T0),
                                                    "asDouble": 1.5,
                                                    "traceId": trace_hex,
                                                    "spanId": span_hex,
                                                }
                                            ],
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }
    decoded = decode_otlp(
        json.dumps(payload).encode(), "application/json", ExportMetricsServiceRequest
    )
    point = decoded.resource_metrics[0].scope_metrics[0].metrics[0].gauge.data_points[0]
    assert point.as_int == 42
    exemplar = point.exemplars[0]
    assert bytes(exemplar.trace_id).hex() == trace_hex
    assert bytes(exemplar.span_id).hex() == span_hex
    assert exemplar.filtered_attributes[0].key == "k"


def test_decode_json_snake_case_aliases_with_hex_ids():
    trace_hex = "0a" * 16
    span_hex = "bc" * 8
    payload = {
        "resource_metrics": [
            {
                "resource": {"attributes": []},
                "scope_metrics": [
                    {
                        "metrics": [
                            {
                                "name": "daytona.sandbox.cpu",
                                "gauge": {
                                    "data_points": [
                                        {
                                            "time_unix_nano": str(T0),
                                            "as_int": "9",
                                            "exemplars": [
                                                {
                                                    "time_unix_nano": str(T0),
                                                    "as_double": 2.5,
                                                    "trace_id": trace_hex,
                                                    "span_id": span_hex,
                                                }
                                            ],
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }
    decoded = decode_otlp(
        json.dumps(payload).encode(), "application/json", ExportMetricsServiceRequest
    )
    exemplar = (
        decoded.resource_metrics[0].scope_metrics[0].metrics[0].gauge.data_points[0].exemplars[0]
    )
    assert bytes(exemplar.trace_id).hex() == trace_hex
    assert bytes(exemplar.span_id).hex() == span_hex


def test_decode_json_preserves_bytes_value_base64():
    raw = b"hello-native"
    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "blob",
                            "value": {"bytesValue": base64.b64encode(raw).decode()},
                        }
                    ]
                },
                "scopeMetrics": [{"metrics": []}],
            }
        ]
    }
    decoded = decode_otlp(
        json.dumps(payload).encode(), "application/json", ExportMetricsServiceRequest
    )
    value = decoded.resource_metrics[0].resource.attributes[0].value
    assert value.WhichOneof("value") == "bytes_value"
    assert bytes(value.bytes_value) == raw


def test_decode_unsupported_content_type_is_value_error():
    with pytest.raises(UnsupportedContentTypeError):
        decode_otlp(b"{}", "text/plain", ExportMetricsServiceRequest)
    with pytest.raises(ValueError):
        decode_otlp(b"{}", "application/xml", ExportMetricsServiceRequest)


def test_decode_malformed_bodies_fail_as_value_error():
    with pytest.raises(ValueError):
        decode_otlp(
            b"\xff\xff\xff\xff\xff\xff\xff", "application/x-protobuf", ExportMetricsServiceRequest
        )
    with pytest.raises(ValueError):
        decode_otlp(b"{oops", "application/json", ExportMetricsServiceRequest)
    with pytest.raises(ValueError):
        decode_otlp(b"\xff\xfe", "application/json", ExportMetricsServiceRequest)
    with pytest.raises(ValueError):
        decode_otlp(b"[1, 2]", "application/json", ExportMetricsServiceRequest)
    with pytest.raises(ValueError):
        decode_otlp(b"   ", "application/json", ExportMetricsServiceRequest)


def test_decode_json_unknown_fields_are_not_swallowed():
    payload = {"resourceMetrics": [], "bogusField": 1}
    with pytest.raises(ValueError):
        decode_otlp(json.dumps(payload).encode(), "application/json", ExportMetricsServiceRequest)


def test_decode_trace_ids_stay_hex_not_base64_misdecoded():
    trace_hex = "abcdef1234567890abcdef1234567890"
    span_hex = "1234567890abcdef"
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_hex,
                                "spanId": span_hex,
                                "name": "n",
                            }
                        ]
                    }
                ]
            }
        ]
    }
    decoded = decode_otlp(
        json.dumps(payload).encode(),
        "application/json",
        ExportTraceServiceRequest,
    )
    span = decoded.resource_spans[0].scope_spans[0].spans[0]
    assert bytes(span.trace_id).hex() == trace_hex
    assert bytes(span.span_id).hex() == span_hex


# identity validation --------------------------------------------------------


def test_telemetry_parent_rejects_malformed_identity():
    with pytest.raises(ValueError):
        TelemetryParent(trace_id="xyz", span_id=PARENT_SPAN, session_id=SESSION)
    with pytest.raises(ValueError):
        TelemetryParent(trace_id="0" * 32, span_id=PARENT_SPAN, session_id=SESSION)
    with pytest.raises(ValueError):
        TelemetryParent(trace_id=TRACE, span_id="0" * 16, session_id=SESSION)
    with pytest.raises(ValueError):
        TelemetryParent(trace_id=TRACE, span_id="short", session_id=SESSION)
    with pytest.raises(ValueError):
        TelemetryParent(trace_id=TRACE, span_id=PARENT_SPAN, session_id="  ")


def test_binding_rejects_bad_window():
    parent = TelemetryParent(trace_id=TRACE, span_id=PARENT_SPAN, session_id=SESSION)
    with pytest.raises(ValueError):
        ModelSessionBinding(app_id=APP, parent=parent, started_at_ns=10, ended_at_ns=9)
    with pytest.raises(ValueError):
        ModelSessionBinding(app_id="  ", parent=parent, started_at_ns=0)
    assert _binding().covers(T_START)
    assert _binding(start=5, end=5).covers(5)
    assert not _binding(start=5, end=5).covers(6)


# per-trial Daytona conversion ------------------------------------------------


def test_daytona_gauge_integer_exactness_and_attribution():
    big = 2**62 + 123
    conversion = _convert(_gauge_metric("daytona.sandbox.cpu", [_gauge_point(big)]))
    span = _only_span(conversion)
    assert bytes(span.trace_id).hex() == TRACE
    assert bytes(span.parent_span_id).hex() == PARENT_SPAN
    assert span.name == "native.metric daytona.sandbox.cpu"
    assert span.start_time_unix_nano == span.end_time_unix_nano == T0
    attrs = _attrs(span)
    assert attrs["lmnr.span.type"] == "DEFAULT"
    assert attrs["lmnr.association.properties.session_id"] == SESSION
    assert attrs[META + "native_provider"] == "daytona"
    assert attrs[META + "trial_id"] == TRIAL
    assert attrs[META + "sandbox_id"] == SANDBOX
    assert attrs[META + "metric_name"] == "daytona.sandbox.cpu"
    assert (META + "app_id") not in attrs
    payload = json.loads(attrs["lmnr.span.output"])
    assert payload["point"]["asInt"] == str(big)
    assert payload["point"]["timeUnixNano"] == str(T0)
    assert payload["point"]["startTimeUnixNano"] == str(T_START)
    assert payload["metric"]["name"] == "daytona.sandbox.cpu"
    assert payload["metric"]["unit"] == "1"
    assert payload["resource_schema_url"] == ""
    assert len(span.events) == 1
    assert span.events[0].name == "native.metric"
    assert span.events[0].time_unix_nano == T0


def test_all_metric_types_convert():
    hist = metrics_pb2.Metric(
        name="daytona.sandbox.mem",
        unit="By",
        histogram=metrics_pb2.Histogram(
            data_points=[
                metrics_pb2.HistogramDataPoint(
                    time_unix_nano=T0,
                    start_time_unix_nano=T_START,
                    count=3,
                    sum=6.5,
                    bucket_counts=[1, 2, 0],
                    explicit_bounds=[1.0, 5.0],
                    min=0.5,
                    max=4.0,
                )
            ],
            aggregation_temporality=CUMULATIVE,
        ),
    )
    exp = metrics_pb2.Metric(
        name="daytona.sandbox.lat",
        exponential_histogram=metrics_pb2.ExponentialHistogram(
            data_points=[
                metrics_pb2.ExponentialHistogramDataPoint(
                    time_unix_nano=T0,
                    count=4,
                    sum=9.0,
                    scale=3,
                    zero_count=1,
                    positive=metrics_pb2.ExponentialHistogramDataPoint.Buckets(
                        offset=1, bucket_counts=[2, 1]
                    ),
                )
            ],
            aggregation_temporality=DELTA,
        ),
    )
    summ = metrics_pb2.Metric(
        name="daytona.sandbox.q",
        summary=metrics_pb2.Summary(
            data_points=[
                metrics_pb2.SummaryDataPoint(
                    time_unix_nano=T0,
                    start_time_unix_nano=T_START,
                    count=2,
                    sum=7.0,
                    quantile_values=[
                        metrics_pb2.SummaryDataPoint.ValueAtQuantile(quantile=0.9, value=5.0)
                    ],
                    flags=1,
                )
            ]
        ),
    )
    total = metrics_pb2.Metric(
        name="daytona.sandbox.req",
        sum=metrics_pb2.Sum(
            data_points=[_gauge_point(11)],
            aggregation_temporality=CUMULATIVE,
            is_monotonic=True,
        ),
    )
    request = ExportMetricsServiceRequest(
        resource_metrics=[
            metrics_pb2.ResourceMetrics(
                resource=_trial_resource(),
                scope_metrics=[
                    metrics_pb2.ScopeMetrics(
                        scope=_scope(),
                        metrics=[
                            _gauge_metric("daytona.sandbox.cpu", [_gauge_point(1)]),
                            total,
                            hist,
                            exp,
                            summ,
                        ],
                    )
                ],
            )
        ]
    )
    conversion = metrics_to_spans(request)
    assert conversion.rejected_data_points == 0
    got = {
        span.name: span
        for rs in conversion.request.resource_spans
        for ss in rs.scope_spans
        for span in ss.spans
    }
    assert set(got) == {
        "native.metric daytona.sandbox.cpu",
        "native.metric daytona.sandbox.req",
        "native.metric daytona.sandbox.mem",
        "native.metric daytona.sandbox.lat",
        "native.metric daytona.sandbox.q",
    }
    assert _attrs(got["native.metric daytona.sandbox.req"])[META + "monotonic"] == "true"
    assert (
        _attrs(got["native.metric daytona.sandbox.req"])[META + "aggregation_temporality"]
        == "AGGREGATION_TEMPORALITY_CUMULATIVE"
    )
    assert (
        _attrs(got["native.metric daytona.sandbox.lat"])[META + "aggregation_temporality"]
        == "AGGREGATION_TEMPORALITY_DELTA"
    )


def test_histogram_cumulative_vs_delta_semantics_preserved():
    def hist(name, temporality):
        return metrics_pb2.Metric(
            name=name,
            histogram=metrics_pb2.Histogram(
                data_points=[
                    metrics_pb2.HistogramDataPoint(
                        time_unix_nano=T0,
                        start_time_unix_nano=T_START,
                        count=3,
                        sum=6.0,
                        bucket_counts=[1, 1, 1],
                        explicit_bounds=[1.0, 2.0],
                    )
                ],
                aggregation_temporality=temporality,
            ),
        )

    request = ExportMetricsServiceRequest(
        resource_metrics=[
            metrics_pb2.ResourceMetrics(
                resource=_trial_resource(),
                scope_metrics=[
                    metrics_pb2.ScopeMetrics(
                        scope=_scope(),
                        metrics=[
                            hist("daytona.sandbox.cum", CUMULATIVE),
                            hist("daytona.sandbox.dlt", DELTA),
                        ],
                    )
                ],
            )
        ]
    )
    conversion = metrics_to_spans(request)
    assert conversion.rejected_data_points == 0
    spans = [
        span
        for rs in conversion.request.resource_spans
        for ss in rs.scope_spans
        for span in ss.spans
    ]
    assert len(spans) == 2
    by_name = {span.name: span for span in spans}
    cum_payload = json.loads(
        _attrs(by_name["native.metric daytona.sandbox.cum"])["lmnr.span.output"]
    )
    dlt_payload = json.loads(
        _attrs(by_name["native.metric daytona.sandbox.dlt"])["lmnr.span.output"]
    )
    assert cum_payload["aggregation_temporality"] == "AGGREGATION_TEMPORALITY_CUMULATIVE"
    assert dlt_payload["aggregation_temporality"] == "AGGREGATION_TEMPORALITY_DELTA"
    assert cum_payload["point"]["bucketCounts"] == ["1", "1", "1"]
    assert cum_payload["point"]["explicitBounds"] == [1.0, 2.0]
    assert cum_payload["point"]["startTimeUnixNano"] == str(T_START)


def test_shared_model_attribution_from_point_labels():
    metric = _gauge_metric(
        "modal.gpu.utilization",
        [_gauge_point(83, extra_attrs={"app_id": APP, "container_id": CONTAINER})],
    )
    resource = Resource(attributes=[_str("service.name", "modal-worker")])
    conversion = _convert(metric, resource=resource, model_sessions=[_binding()])
    span = _only_span(conversion)
    assert bytes(span.trace_id).hex() == TRACE
    assert bytes(span.parent_span_id).hex() == PARENT_SPAN
    attrs = _attrs(span)
    assert attrs[META + "native_provider"] == "modal"
    assert attrs[META + "app_id"] == APP
    assert attrs[META + "container_id"] == CONTAINER
    assert attrs[META + "model_session"] == "nightly-train"
    assert attrs[META + "shared_model_session"] == "true"
    assert (META + "trial_id") not in attrs
    assert (META + "sandbox_id") not in attrs


def test_shared_model_attribution_from_resource_labels():
    metric = _gauge_metric("modal.gpu.memory", [_gauge_point(5)])
    resource = Resource(attributes=[_str("app_id", APP), _str("container_id", CONTAINER)])
    conversion = _convert(metric, resource=resource, model_sessions=[_binding()])
    span = _only_span(conversion)
    assert _attrs(span)[META + "app_id"] == APP


def test_model_span_never_takes_stray_trial_label():
    metric = _gauge_metric("modal.gpu.memory", [_gauge_point(5)])
    resource = Resource(attributes=[_str("app_id", APP), _str("trial_id", "some-other-trial")])
    conversion = _convert(metric, resource=resource, model_sessions=[_binding()])
    assert (META + "trial_id") not in _attrs(_only_span(conversion))


def test_model_sample_never_duplicated_across_bindings():
    first = _binding(start=T_START, end=T0)
    second = ModelSessionBinding(
        app_id=APP,
        parent=TelemetryParent(trace_id=TRACE, span_id="0011223344556677", session_id="other__env"),
        started_at_ns=T_START,
        ended_at_ns=None,
    )
    metric = _gauge_metric("modal.gpu.utilization", [_gauge_point(1, extra_attrs={"app_id": APP})])
    conversion = _convert(
        metric,
        resource=Resource(attributes=[]),
        model_sessions=[first, second],
    )
    assert conversion.rejected_data_points == 1
    assert len(conversion.request.resource_spans) == 0


# rejection accounting ---------------------------------------------------------


def test_org_level_aggregate_without_sandbox_rejected():
    resource = Resource(
        attributes=[
            _str("evallab.trace_id", TRACE),
            _str("evallab.parent_span_id", PARENT_SPAN),
            _str("evallab.session_id", SESSION),
            _str("trial_id", TRIAL),
        ]
    )
    conversion = _convert(
        _gauge_metric("daytona.sandbox.cpu", [_gauge_point(1)]), resource=resource
    )
    assert conversion.rejected_data_points == 1
    assert len(conversion.request.resource_spans) == 0


def test_missing_trial_labels_rejected_without_orphan_root():
    resource = Resource(attributes=[_str("service.instance.id", SANDBOX)])
    conversion = _convert(
        _gauge_metric("daytona.sandbox.cpu", [_gauge_point(1)]), resource=resource
    )
    assert conversion.rejected_data_points == 1
    assert len(conversion.request.resource_spans) == 0


def test_malformed_trace_label_rejected():
    resource = _trial_resource({"evallab.trace_id": "not-hex"})
    conversion = _convert(
        _gauge_metric("daytona.sandbox.cpu", [_gauge_point(1)]), resource=resource
    )
    assert conversion.rejected_data_points == 1
    assert len(conversion.request.resource_spans) == 0


def test_unknown_app_rejected():
    metric = _gauge_metric(
        "modal.gpu.utilization", [_gauge_point(1, extra_attrs={"app_id": "app-nope"})]
    )
    conversion = _convert(metric, resource=Resource(attributes=[]), model_sessions=[_binding()])
    assert conversion.rejected_data_points == 1


def test_observation_outside_window_rejected():
    metric = _gauge_metric(
        "modal.gpu.utilization",
        [_gauge_point(1, at=T0, extra_attrs={"app_id": APP})],
    )
    binding = _binding(start=T0 + 1, end=None)
    conversion = _convert(metric, resource=Resource(attributes=[]), model_sessions=[binding])
    assert conversion.rejected_data_points == 1


def test_window_edges_inclusive():
    for at in (T_START, T0):
        metric = _gauge_metric(
            "modal.gpu.utilization",
            [_gauge_point(1, at=at, extra_attrs={"app_id": APP})],
        )
        conversion = _convert(
            metric,
            resource=Resource(attributes=[]),
            model_sessions=[_binding(start=T_START, end=T0)],
        )
        assert conversion.rejected_data_points == 0, conversion.error_message


def test_conflicting_app_ids_rejected():
    metric = _gauge_metric(
        "modal.gpu.utilization", [_gauge_point(1, extra_attrs={"app_id": "app-other"})]
    )
    resource = Resource(attributes=[_str("app_id", APP)])
    conversion = _convert(metric, resource=resource, model_sessions=[_binding()])
    assert conversion.rejected_data_points == 1


def test_cross_provider_identity_rejected():
    metric = _gauge_metric("daytona.sandbox.cpu", [_gauge_point(1, extra_attrs={"app_id": APP})])
    conversion = _convert(metric, model_sessions=[_binding()])
    assert conversion.rejected_data_points == 1


def test_missing_observation_time_rejected():
    conversion = _convert(_gauge_metric("daytona.sandbox.cpu", [_gauge_point(1, at=0)]))
    assert conversion.rejected_data_points == 1


def test_unbound_non_daytona_metric_rejected():
    conversion = _convert(
        _gauge_metric("system.cpu.time", [_gauge_point(1)]), model_sessions=[_binding()]
    )
    assert conversion.rejected_data_points == 1


def test_partial_rejection_keeps_good_spans_with_message():
    conversion = _convert(
        _gauge_metric("daytona.sandbox.cpu", [_gauge_point(1), _gauge_point(2, at=0)])
    )
    assert conversion.rejected_data_points == 1
    assert len(conversion.request.resource_spans) == 1


def test_empty_request_converts_cleanly():
    conversion = metrics_to_spans(ExportMetricsServiceRequest())
    assert isinstance(conversion.request, ExportTraceServiceRequest)
    assert conversion.rejected_data_points == 0

    assert len(conversion.request.resource_spans) == 0


# identity stability and lossless content ---------------------------------------


def test_repeat_identity_stable_distinct_point_differs():
    first = _convert(_gauge_metric("daytona.sandbox.cpu", [_gauge_point(4)]))
    again = _convert(_gauge_metric("daytona.sandbox.cpu", [_gauge_point(4)]))
    changed = _convert(_gauge_metric("daytona.sandbox.cpu", [_gauge_point(5)]))
    assert _only_span(first).span_id == _only_span(again).span_id
    assert _only_span(first).span_id != _only_span(changed).span_id
    assert bytes(_only_span(first).span_id) != b"\x00" * 8


def test_aggregation_start_preserved_not_faked_as_duration():
    conversion = _convert(_gauge_metric("daytona.sandbox.cpu", [_gauge_point(4)]))
    span = _only_span(conversion)
    assert span.start_time_unix_nano == span.end_time_unix_nano == T0
    payload = json.loads(_attrs(span)["lmnr.span.output"])
    assert payload["point"]["startTimeUnixNano"] == str(T_START)


def test_native_json_exemplar_survives_end_to_end():
    trace_hex = "cc" * 16
    span_hex = "dd" * 8
    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.instance.id",
                            "value": {"stringValue": SANDBOX},
                        },
                        {"key": "evallab.trace_id", "value": {"stringValue": TRACE}},
                        {
                            "key": "evallab.parent_span_id",
                            "value": {"stringValue": PARENT_SPAN},
                        },
                        {"key": "evallab.session_id", "value": {"stringValue": SESSION}},
                        {"key": "trial_id", "value": {"stringValue": TRIAL}},
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "s"},
                        "metrics": [
                            {
                                "name": "daytona.sandbox.cpu",
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": str(T0),
                                            "asInt": "6",
                                            "exemplars": [
                                                {
                                                    "timeUnixNano": str(T0),
                                                    "asInt": "6",
                                                    "traceId": trace_hex,
                                                    "spanId": span_hex,
                                                }
                                            ],
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }
    request = decode_otlp(
        json.dumps(payload).encode(), "application/json", ExportMetricsServiceRequest
    )
    span = _only_span(metrics_to_spans(request))
    out = json.loads(_attrs(span)["lmnr.span.output"])
    exemplar = out["point"]["exemplars"][0]
    # MessageToDict schema form renders bytes fields as base64 (lossless).
    assert exemplar["traceId"] == base64.b64encode(bytes.fromhex(trace_hex)).decode()
    assert exemplar["spanId"] == base64.b64encode(bytes.fromhex(span_hex)).decode()


def test_scope_and_resource_preserved_in_output():
    resource = _trial_resource()
    resource.dropped_attributes_count = 2
    scope = InstrumentationScope(name="daytona-meter", version="9", dropped_attributes_count=1)
    request = ExportMetricsServiceRequest(
        resource_metrics=[
            metrics_pb2.ResourceMetrics(
                resource=resource,
                scope_metrics=[
                    metrics_pb2.ScopeMetrics(
                        scope=scope,
                        schema_url="https://example.test/scope",
                        metrics=[_gauge_metric("daytona.sandbox.cpu", [_gauge_point(1)])],
                    )
                ],
                schema_url="https://example.test/resource",
            )
        ]
    )
    conversion = metrics_to_spans(request)
    span = _only_span(conversion)
    payload = json.loads(_attrs(span)["lmnr.span.output"])
    assert payload["resource_schema_url"] == "https://example.test/resource"
    assert payload["scope_schema_url"] == "https://example.test/scope"
    assert payload["scope"]["name"] == "daytona-meter"
    out_rs = conversion.request.resource_spans[0]
    assert out_rs.schema_url == "https://example.test/resource"
    assert out_rs.scope_spans[0].schema_url == "https://example.test/scope"
    assert out_rs.scope_spans[0].scope.name == "daytona-meter"


def test_rebatching_and_reordering_do_not_create_duplicate_observations():
    metric = _gauge_metric("daytona.sandbox.cpu", [_gauge_point(4), _gauge_point(7, at=T0 + 1)])
    batch = _convert(metric)
    original = batch.request.resource_spans[0].scope_spans[0].spans
    reordered = (
        _convert(
            _gauge_metric("daytona.sandbox.cpu", [_gauge_point(7, at=T0 + 1), _gauge_point(4)])
        )
        .request.resource_spans[0]
        .scope_spans[0]
        .spans
    )
    single = _only_span(_convert(_gauge_metric("daytona.sandbox.cpu", [_gauge_point(4)])))
    assert original[0].span_id == reordered[1].span_id == single.span_id
    assert original[1].span_id == reordered[0].span_id
    for span in original:
        payload = json.loads(_attrs(span)["lmnr.span.output"])
        assert "dataPoints" not in payload["metric"]["gauge"]
        assert payload["point"]["timeUnixNano"] == str(span.start_time_unix_nano)


def test_duplicate_trial_identity_is_rejected_without_choosing_last_value():
    resource = _trial_resource()
    resource.attributes.append(_str("trial_id", "other-trial"))
    result = _convert(_gauge_metric("daytona.sandbox.cpu", [_gauge_point(4)]), resource)
    assert result.rejected_data_points == 1
    assert not result.request.resource_spans


def test_standard_modal_service_instance_is_not_fabricated_sandbox_identity():
    resource = Resource(
        attributes=[_str("service.instance.id", "modal-container"), _str("app_id", APP)]
    )
    span = _only_span(
        _convert(
            _gauge_metric("modal.gpu.utilization", [_gauge_point(4)]),
            resource,
            model_sessions=[_binding()],
        )
    )
    attributes = _attrs(span)
    assert attributes["lmnr.association.properties.metadata.shared_model_session"] == "true"
    assert "lmnr.association.properties.metadata.trial_id" not in attributes
    assert "lmnr.association.properties.metadata.sandbox_id" not in attributes
    assert span.trace_id.hex() == TRACE
    assert span.parent_span_id.hex() == PARENT_SPAN


def test_unrecognized_metric_family_is_not_relabelled_as_modal():
    result = _convert(
        _gauge_metric("custom.queue", [_gauge_point(4, extra_attrs={"app_id": APP})]),
        Resource(),
        model_sessions=[_binding()],
    )
    assert result.rejected_data_points == 1
    assert not result.request.resource_spans


def test_invalid_otlp_json_hex_ids_cannot_be_decoded_as_base64():
    body = b'{"resourceSpans":[{"scopeSpans":[{"spans":[{"traceId":"not-hex-but-base64","spanId":"0000000000000001"}]}]}]}'
    with pytest.raises(ValueError):
        decode_otlp(body, "application/json", ExportTraceServiceRequest)


def test_shared_model_binding_cannot_claim_an_exclusive_trial_parent():
    parent = TelemetryParent(
        trace_id=TRACE, span_id=PARENT_SPAN, session_id=SESSION, trial_id=TRIAL
    )
    with pytest.raises(ValueError):
        ModelSessionBinding(app_id=APP, parent=parent, started_at_ns=T_START)


def test_conflicting_point_parent_cannot_override_actual_sandbox_root():
    point = _gauge_point(4, extra_attrs={"evallab.parent_span_id": "abcdef0123456789"})
    result = _convert(_gauge_metric("daytona.sandbox.cpu", [point]))
    assert result.rejected_data_points == 1
    assert not result.request.resource_spans
