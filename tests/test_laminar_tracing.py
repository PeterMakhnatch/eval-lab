from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab import laminar_tracing as tracing

MODULE = Path(tracing.__file__)


def _readable_span(secret, host_path):
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import Link, SpanContext, Status, StatusCode, TraceFlags, TraceState

    escaped_state = (
        '"' + "".join(f"\\u{ord(character):04x}" for character in "Bearer state-token") + '"'
    )
    context = SpanContext(
        123, 456, False, TraceFlags(1), TraceState([("safe", "value"), ("escaped", escaped_state)])
    )
    parent = SpanContext(
        123, 789, True, TraceFlags(1), TraceState([("linked", "Bearer state-token")])
    )
    attributes = {
        "input": json.dumps({"nested": [secret, host_path + "/config"]}),
        "authorization": "Bearer other-token",
        "sandbox_path": "/testbed/task.py",
        "ordinary": 12,
        "secret-key-" + secret: "value",
        "code.filepath": "/opt/trusted-host/site-packages/client.py",
        "code.paths": json.dumps({"sdk": ["/opt/Trusted Host SDK/native.py", "/testbed/task.py"]}),
    }
    return ReadableSpan(
        name="operation " + secret,
        context=context,
        parent=parent,
        attributes=attributes,
        events=[
            Event(
                "event " + secret,
                {"stack": f'File "{host_path}/source.py", line 12', "message": secret},
            )
        ],
        links=[Link(parent, {"linked": secret})],
        status=Status(StatusCode.ERROR, "error " + secret),
        resource=Resource(
            {"machine": host_path, "credential": secret}, schema_url="https://example.com/" + secret
        ),
        instrumentation_scope=InstrumentationScope(
            "scope " + secret, "1", schema_url=host_path, attributes={"nested": secret}
        ),
        start_time=100,
        end_time=200,
    )


def _encoded_json(spans):
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

    return json.dumps(MessageToDict(encode_spans(spans)), ensure_ascii=False)


def test_all_span_surfaces_are_private_before_delegate_and_originals_unchanged():
    from opentelemetry.sdk.trace.export import SpanExportResult

    secret = 'proxy-credential-"résumé"'
    host = "/Users/operator/Developer/private-worktree"
    span = _readable_span(secret, host)
    original = _encoded_json((span,))
    exported = []

    class Delegate:
        def export(self, spans):
            exported.extend(spans)
            return SpanExportResult.SUCCESS

    runtime = tracing.TraceRuntime(sanitizer=tracing._Sanitizer((secret,), (host,)))
    exporter = tracing._SafeExporter(runtime, delegate=Delegate())
    assert exporter.export((span,)) == SpanExportResult.SUCCESS
    safe = _encoded_json(exported)
    assert secret not in safe
    assert "proxy-credential" not in safe
    assert host not in safe
    assert "other-token" not in safe
    assert "/opt/trusted-host" not in safe
    assert "Trusted Host SDK" not in safe
    assert "[REDACTED]" in safe
    assert "[HOST_PATH]" in safe
    assert "/testbed/task.py" in safe
    assert exported[0].context.trace_id == 123
    assert exported[0].context.span_id == 456
    assert exported[0].parent.span_id == 789
    assert exported[0].context.trace_state["safe"] == "value"
    assert json.loads(exported[0].context.trace_state["escaped"]) == "Bearer [REDACTED]"
    assert exported[0].links[0].context.trace_state["linked"] == "Bearer [REDACTED]"
    assert exported[0].start_time == 100 and exported[0].end_time == 200
    assert _encoded_json((span,)) == original
    with pytest.raises(TypeError):
        exported[0].attributes["ordinary"] = 999


def test_nested_unicode_escaped_secret_and_canonical_bearer_are_redacted_without_mutation():
    secret = "token-sensitive-123"
    escaped = "".join(f"\\u{ord(character):04x}" for character in secret)
    original = {
        "payload": '{"outer": "{\\"token\\":\\"' + escaped.replace("\\", "\\\\") + '\\"}"}',
        "header": "Authorization: Bearer header-secret",
        "nested": [secret, {secret: "/home/operator/credentials"}],
        "unknown": None,
    }
    before = json.dumps(original)
    sanitizer = tracing._Sanitizer((secret,), (), r"(?i)authorization\s*:\s*bearer\s+[^\s]+")
    safe = sanitizer.value(original)
    result = json.dumps(safe)
    assert secret not in result
    assert "header-secret" not in result
    assert "credentials" not in result
    assert safe["unknown"] is None
    assert json.dumps(original) == before


def test_failed_export_drops_telemetry_without_raw_diagnostic():
    from opentelemetry.sdk.trace.export import SpanExportResult

    class Broken:
        def export(self, _spans):
            raise RuntimeError("credential /Users/operator/private")

    runtime = tracing.TraceRuntime()
    exporter = tracing._SafeExporter(runtime, delegate=Broken())
    assert (
        exporter.export((_readable_span("credential", "/Users/operator/private"),))
        == SpanExportResult.FAILURE
    )
    assert runtime.error_type == "RuntimeError"


def test_sanitization_failure_never_reaches_delegate():
    from opentelemetry.sdk.trace.export import SpanExportResult

    class Unserializable:
        def __str__(self):
            raise RuntimeError("private value")

    class RejectRaw:
        def export(self, _spans):
            pytest.fail("exporter received an unsanitized span")

    span = _readable_span("credential", "/Users/operator/private")
    span._attributes["unserializable"] = Unserializable()
    runtime = tracing.TraceRuntime()
    assert (
        tracing._SafeExporter(runtime, delegate=RejectRaw()).export((span,))
        == SpanExportResult.FAILURE
    )
    assert runtime.error_type == "RuntimeError"


@pytest.mark.parametrize(
    "original",
    [
        ValueError("original"),
        asyncio.CancelledError("original"),
        KeyboardInterrupt(),
        SystemExit(17),
    ],
)
def test_original_exception_identity_survives_all_telemetry_failures(original):
    class BrokenSpan:
        def set_status(self, *_args):
            raise RuntimeError("status failed")

        def set_attribute(self, *_args):
            raise RuntimeError("attributes failed")

        def end(self):
            raise RuntimeError("export failed")

    class BrokenContext:
        @staticmethod
        def activate(_span):
            return object()

        @staticmethod
        def deactivate(_tokens):
            raise RuntimeError("cleanup failed")

    runtime = tracing.TraceRuntime()
    runtime._sdk = BrokenContext()
    with pytest.raises(type(original)) as caught, tracing.TraceSpan(runtime, BrokenSpan()):
        raise original
    assert caught.value is original
    assert runtime.error_type == "RuntimeError"


def test_disabled_runtime_does_not_fabricate_context_or_swallow_cancellation(monkeypatch):
    monkeypatch.delenv("LMNR_PROJECT_API_KEY", raising=False)
    runtime = tracing.initialize_tracing()
    assert runtime.enabled is False
    assert runtime.error_type == "Unconfigured"
    original = asyncio.CancelledError()
    with (
        pytest.raises(asyncio.CancelledError) as caught,
        runtime.start_span("disabled", parent_context="not a context") as span,
    ):
        assert span.trace_id is None
        assert span.serialized_context() is None
        span.set_output({"value": "unchanged"})
        raise original
    assert caught.value is original
    runtime.flush_native()
    runtime.flush_async()
    runtime.export_native("invalid")
    assert runtime.error_type == "Unconfigured"


@pytest.mark.parametrize("original", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit(2)])
def test_initialization_preserves_process_control_exceptions(monkeypatch, original):
    for variable in (
        "LMNR_DISABLE_TRACING",
        "OTEL_SDK_DISABLED",
        "LMNR_DEBUG",
        "LMNR_SPAN_CONTEXT",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "fixture-laminar-key")
    monkeypatch.setattr(tracing, "_runtime", None)

    def interrupted_adapter(*_args):
        raise original

    monkeypatch.setattr(tracing, "_SdkAdapter", interrupted_adapter)
    with pytest.raises(type(original)) as caught:
        tracing.initialize_tracing()
    assert caught.value is original


def _sdk_process(tmp_path, body, *, automatic_openai=False, before_init=""):
    sink = tmp_path / "native_logs" / "laminar-spans.jsonl"
    prelude = f"""
import importlib.util, json, os, pathlib, sys
spec = importlib.util.spec_from_file_location('trusted_laminar', {str(MODULE)!r})
tracing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracing)
# An actual native sink must not open any network connection, including SDK
# initialization and atexit. MockTransport model calls need none either.
def no_network(event, args):
    if event == 'socket.connect':
        # Exceptions in atexit/background threads would not fail the process.
        # A forbidden connection therefore terminates this isolated fixture.
        os._exit(89)
sys.addaudithook(no_network)
sink = {str(sink)!r}
{textwrap.dedent(before_init)}
runtime = tracing.initialize_tracing(
    automatic_openai={automatic_openai!r}, native_sink=sink,
    secrets=('model-capability',), host_paths=('/tmp/private-native-host',),
)
assert runtime.enabled, runtime.error_type
"""
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("LMNR_", "OTEL_"))
    }
    env["LMNR_PROJECT_API_KEY"] = "fixture-laminar-key"
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    result = subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent(prelude) + textwrap.dedent(body)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return json.loads(result.stdout), sink


def _sink_spans(sink):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    spans = []
    for line in sink.read_text().splitlines():
        request = ExportTraceServiceRequest.FromString(
            base64.b64decode(json.loads(line)["otlp"], validate=True)
        )
        for resource in request.resource_spans:
            for scope in resource.scope_spans:
                spans.extend(scope.spans)
    return spans


def _attribute(span, key):
    return next(attribute.value for attribute in span.attributes if attribute.key == key)


def test_actual_sdk_auto_openai_and_manual_native_spans_share_real_parent_session_and_metadata(
    tmp_path,
):
    result, sink = _sdk_process(
        tmp_path,
        """
import concurrent.futures, httpx
from openai import OpenAI
from opentelemetry import trace
from lmnr.opentelemetry_lib.tracing.context import get_current_context
calls = []
message = 'model-capability /tmp/private-native-host/config /testbed/task.py'
def respond(request):
    body = json.loads(request.content)
    calls.append({'body': body, 'headers': dict(request.headers)})
    return httpx.Response(200, json={
        'id': 'fixture-call', 'object': 'chat.completion', 'created': 1,
        'model': 'fixture-model', 'choices': [{'index': 0,
        'message': {'role': 'assistant', 'content': message}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10},
    })
client = OpenAI(api_key='model-capability', max_retries=0,
    http_client=httpx.Client(transport=httpx.MockTransport(respond)))
# Populate a reused thread before any trial span; parent context must be
# explicitly supplied rather than accidentally inherited at thread creation.
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
executor.submit(lambda: None).result()
root = runtime.start_span('trial', session_id='actual-job',
    metadata={'trial_id': 'actual-trial', 'unknown': None})
other = runtime.start_span('other-trial', session_id='actual-job')
assert root.trace_id != other.trace_id
other.end()
with root:
    remote_parent = root.serialized_context()
    def run_tool():
        with runtime.start_span('agent', span_type='TOOL', parent_context=remote_parent) as tool:
            with runtime.start_span('child-agent') as child:
                reply = client.chat.completions.create(model='fixture-model',
                    messages=[{'role': 'user', 'content': message}], temperature=0.25)
                assert reply.choices[0].message.content == message
                assert trace.get_current_span(get_current_context()).get_span_context().span_id == child._span.get_span_context().span_id
                child.set_output(reply.choices[0].message.content)
            tool.set_output({'exit_code': None, 'host': '/tmp/private-native-host/config'})
    executor.submit(run_tool).result()
    # Ended children must reach the local sink before the trial root closes.
    runtime.flush_native()
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    import base64
    partial_spans = []
    for line in pathlib.Path(sink).read_text().splitlines():
        request = ExportTraceServiceRequest.FromString(base64.b64decode(json.loads(line)['otlp']))
        partial_spans.extend(span for resource in request.resource_spans
            for scope in resource.scope_spans for span in scope.spans)
    assert {'agent', 'child-agent'} <= {span.name for span in partial_spans}
    assert 'trial' not in {span.name for span in partial_spans}
    late = runtime.start_span('late', parent_context=remote_parent)
    assert late.trace_id == root.trace_id
    late.end()
executor.shutdown()
runtime.flush_native()
assert [call['body'] for call in calls] == [{'messages': [{'role': 'user', 'content': message}], 'model': 'fixture-model', 'temperature': 0.25}]
assert not any('traceparent' in call['headers'] or 'tracestate' in call['headers'] for call in calls)
print(json.dumps({'root_id': root.trace_id, 'context': json.loads(remote_parent),
    'model_calls': len(calls), 'error': runtime.error_type}))
""",
        automatic_openai=True,
    )
    spans = _sink_spans(sink)
    root = next(span for span in spans if span.name == "trial")
    tool = next(span for span in spans if span.name == "agent")
    child = next(span for span in spans if span.name == "child-agent")
    llm = next(span for span in spans if _attribute_or_none(span, "lmnr.span.type") == "LLM")
    late = next(span for span in spans if span.name == "late")
    assert tool.parent_span_id == root.span_id
    assert child.parent_span_id == tool.span_id
    assert llm.parent_span_id == child.span_id
    assert late.parent_span_id == root.span_id
    assert all(span.trace_id == root.trace_id for span in (tool, child, llm, late))
    assert result["context"]["session_id"] == "actual-job"
    assert result["context"]["metadata"]["trial_id"] == "actual-trial"
    assert result["context"]["metadata"]["unknown"] is None
    assert result["model_calls"] == 1
    assert result["error"] is None
    assert _attribute(llm, "lmnr.association.properties.session_id").string_value == "actual-job"
    assert (
        _attribute(llm, "lmnr.association.properties.metadata.trial_id").string_value
        == "actual-trial"
    )
    from google.protobuf.json_format import MessageToDict

    exported = json.dumps([MessageToDict(span) for span in spans])
    assert "model-capability" not in exported
    assert "private-native-host" not in exported
    assert "fixture-laminar-key" not in exported
    assert "/testbed/task.py" in exported


def _attribute_or_none(span, key):
    for attribute in span.attributes:
        if attribute.key == key:
            return attribute.value.string_value
    return None


def test_bad_parent_context_drops_span_instead_of_silently_making_a_new_root(tmp_path):
    result, sink = _sdk_process(
        tmp_path,
        """
root = runtime.start_span('trial')
with root:
    invalid_parents = [
        {'trace_id': 'invalid', 'span_id': 'invalid'},
        {'trace_id': '00000000-0000-0000-0000-000000000001', 'span_id': '00000000-0000-0000-0000-000000000000'},
        {'trace_id': '00000000-0000-0000-0000-000000000000', 'span_id': '00000000-0000-0000-0000-000000000001'},
        {'trace_id': '00000000-0000-0000-0000-000000000001', 'span_id': '00000000-0000-0001-0000-000000000000'},
    ]
    for data in invalid_parents:
        broken = runtime.start_span('false-root', parent_context=json.dumps(data))
        assert broken.trace_id is None
        assert broken.serialized_context() is None
    good = runtime.start_span('good-child')
    assert good.trace_id == root.trace_id
    good.end()
runtime.flush_native()
print(json.dumps({'error': runtime.error_type}))
""",
    )
    spans = _sink_spans(sink)
    assert {span.name for span in spans} == {"trial", "good-child"}
    root = next(span for span in spans if span.name == "trial")
    child = next(span for span in spans if span.name == "good-child")
    assert child.parent_span_id == root.span_id
    assert result["error"] == "ValueError"


def test_local_drain_wait_is_bounded_even_if_processor_ignores_timeout():
    import threading
    import time

    waiting = threading.Event()
    release = threading.Event()

    class StuckLocalProcessor:
        @staticmethod
        def force_flush(**_kwargs):
            waiting.set()
            release.wait(5)
            return True

    runtime = tracing.TraceRuntime(native_sink="local-only.jsonl")
    runtime.enabled = True
    runtime._sdk = SimpleNamespace(processor=StuckLocalProcessor())
    started = time.monotonic()
    try:
        runtime.flush_native()
        elapsed = time.monotonic() - started
        assert waiting.is_set()
        assert elapsed < 1.5
        assert runtime.error_type == "LocalFlushTimeout"
    finally:
        release.set()
        runtime._flush_thread.join(2)


def test_cloud_flush_never_blocks_or_resets_live_context():
    import threading
    import time

    waiting = threading.Event()
    release = threading.Event()

    class OfflineProcessor:
        @staticmethod
        def force_flush(**_kwargs):
            waiting.set()
            release.wait(5)
            raise OSError("offline with credential /Users/operator/private")

    runtime = tracing.TraceRuntime()
    runtime.enabled = True
    runtime._sdk = SimpleNamespace(processor=OfflineProcessor())
    started = time.monotonic()
    try:
        runtime.flush_async()
        assert time.monotonic() - started < 0.5
        assert waiting.wait(1)
    finally:
        release.set()
        runtime._flush_thread.join(2)
    assert runtime.error_type == "OSError"


def test_large_native_frame_is_explicitly_truncated_without_losing_trace_identity(tmp_path):
    from opentelemetry.sdk.trace.export import SpanExportResult

    secret = "native-capability"
    span = _readable_span(secret, "/Users/operator/private")
    span._attributes["large"] = tuple((secret + "x" * 65536) for _ in range(65))
    runtime = tracing.TraceRuntime(
        sanitizer=tracing._Sanitizer((secret,), ("/Users/operator/private",))
    )
    sink = tmp_path / "native.jsonl"
    assert (
        tracing._SafeExporter(runtime, sink=str(sink)).export((span,)) == SpanExportResult.SUCCESS
    )
    frame = base64.b64decode(json.loads(sink.read_text())["otlp"])
    assert len(frame) <= tracing._MAX_FRAME
    (encoded,) = _sink_spans(sink)
    assert int.from_bytes(encoded.trace_id, "big") == 123
    assert int.from_bytes(encoded.span_id, "big") == 456
    assert int.from_bytes(encoded.parent_span_id, "big") == 789
    assert encoded.start_time_unix_nano == 100 and encoded.end_time_unix_nano == 200
    assert _attribute(encoded, "evallab.telemetry.truncated").bool_value is True
    assert secret.encode() not in frame
    assert span.attributes["large"][0].startswith(secret)


@pytest.fixture
def laminar_http_endpoint(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    records = []
    received = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            records.append(
                (
                    self.path,
                    dict(self.headers),
                    self.rfile.read(int(self.headers["Content-Length"])),
                )
            )
            self.send_response(200)
            self.end_headers()
            received.set()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(tracing, "_CLOUD_BASE", f"http://127.0.0.1:{server.server_port}")
    try:
        yield records, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_native_forwarding_delivers_authenticated_safe_protobuf_on_background_transport(
    laminar_http_endpoint,
):
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    secret = "forwarded-model-capability"
    span = _readable_span(secret, "/Users/operator/private")
    unsafe = encode_spans((span,))
    unsafe.resource_spans[0].scope_spans[0].spans[0].links[
        0
    ].trace_state = "linked=Bearer state-token"
    # Include an AnyValue byte payload, which automatic exporters can emit.
    raw_attribute = unsafe.resource_spans[0].scope_spans[0].spans[0].attributes.add()
    raw_attribute.key = "byte_payload"
    raw_attribute.value.bytes_value = json.dumps({"secret": secret}).encode()
    raw_wire = unsafe.SerializeToString()
    runtime = tracing.TraceRuntime(
        sanitizer=tracing._Sanitizer((secret, "fixture-laminar-key"), ("/Users/operator/private",))
    )
    runtime.enabled = True
    runtime._key = "fixture-laminar-key"
    records, received = laminar_http_endpoint
    runtime.export_native(base64.b64encode(raw_wire).decode())
    assert received.wait(3)
    path, headers, wire = records[0]
    assert path == "/v1/traces"
    assert headers["Authorization"] == "Bearer fixture-laminar-key"
    assert headers["Content-Type"] == "application/x-protobuf"
    forwarded = ExportTraceServiceRequest.FromString(wire)
    safe_span = forwarded.resource_spans[0].scope_spans[0].spans[0]
    states = dict(part.split("=", 1) for part in safe_span.trace_state.split(","))
    assert json.loads(states["escaped"]) == "Bearer [REDACTED]"
    assert safe_span.links[0].trace_state == "linked=Bearer [REDACTED]"
    assert safe_span.trace_id == unsafe.resource_spans[0].scope_spans[0].spans[0].trace_id
    assert safe_span.span_id == unsafe.resource_spans[0].scope_spans[0].spans[0].span_id
    assert (
        safe_span.parent_span_id == unsafe.resource_spans[0].scope_spans[0].spans[0].parent_span_id
    )
    exported = json.dumps(MessageToDict(forwarded))
    assert secret not in exported
    assert "/Users/operator/private" not in exported
    assert "/opt/trusted-host" not in exported
    assert "Trusted Host SDK" not in exported
    assert "[REDACTED]" in exported
    assert json.loads(_attribute(safe_span, "byte_payload").bytes_value)["secret"] == "[REDACTED]"
    assert unsafe.SerializeToString() == raw_wire


def test_typed_context_identity_is_not_corrupted_by_secret_matching_uuid_characters():
    context = {
        "trace_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "span_id": "00000000-0000-0000-aaaa-aaaaaaaaaaaa",
        "span_ids_path": ["00000000-0000-0000-aaaa-aaaaaaaaaaaa"],
        "span_path": ["sensitive-operation"],
        "metadata": {"known": "sensitive-value"},
        "debug": {"replay": True},
    }
    sanitizer = tracing._Sanitizer(("a", "sensitive-value"))
    safe = sanitizer.context_data(context)
    assert safe["trace_id"] == context["trace_id"]
    assert safe["span_id"] == context["span_id"]
    assert safe["span_ids_path"] == context["span_ids_path"]
    assert "debug" not in safe
    assert "sensitive-value" not in json.dumps(safe)
    assert context["debug"] == {"replay": True}


@pytest.mark.parametrize(
    ("name", "value", "error"),
    [
        ("LMNR_DISABLE_TRACING", "true", "Disabled"),
        ("OTEL_SDK_DISABLED", "true", "Disabled"),
        ("LMNR_DEBUG", "1", "DebugModeUnsupported"),
        ("LMNR_SPAN_CONTEXT", '{"debug":{"replay":true}}', "AmbientContextUnsupported"),
    ],
)
def test_configuration_that_could_change_execution_remains_an_explicit_noop(
    monkeypatch, name, value, error
):
    for variable in (
        "LMNR_DISABLE_TRACING",
        "OTEL_SDK_DISABLED",
        "LMNR_DEBUG",
        "LMNR_SPAN_CONTEXT",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "fixture-laminar-key")
    monkeypatch.setenv(name, value)
    runtime = tracing.initialize_tracing()
    assert runtime.enabled is False
    assert runtime.error_type == error
    with runtime.start_span("trial") as span:
        assert span.trace_id is None
        assert span.serialized_context() is None
    assert runtime.error_type == error


def test_real_auto_sdk_preserves_existing_openai_client_entire_wire_request_and_response(tmp_path):
    result, sink = _sdk_process(
        tmp_path,
        """
with runtime.start_span('trial', session_id='actual-job'):
    after = client.chat.completions.create(**kwargs)
runtime.flush_native()
assert calls[0] == calls[1]
assert after.model_dump() == before.model_dump()
assert kwargs == original_kwargs
print(json.dumps({'requests_identical': calls[0] == calls[1], 'response_identical': after.model_dump() == before.model_dump()}))
""",
        automatic_openai=True,
        before_init="""
import httpx
from openai import OpenAI
calls = []
def respond(request):
    calls.append((request.method, str(request.url), dict(request.headers), request.content.decode()))
    return httpx.Response(200, json={
        'id': 'same-response', 'object': 'chat.completion', 'created': 1,
        'model': 'fixture-model', 'choices': [{'index': 0,
        'message': {'role': 'assistant', 'content': 'unchanged-output'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10},
    })
client = OpenAI(api_key='model-capability', max_retries=0,
    http_client=httpx.Client(transport=httpx.MockTransport(respond)))
kwargs = {'model': 'fixture-model', 'messages': [{'role': 'user', 'content': 'unchanged-input'}],
    'temperature': 0.2, 'max_tokens': 9, 'stop': ['finished'],
    'extra_headers': {'Idempotency-Key': 'same-idempotency-key', 'X-Caller': 'original'}}
original_kwargs = json.loads(json.dumps(kwargs))
before = client.chat.completions.create(**kwargs)
""",
    )
    assert result == {"requests_identical": True, "response_identical": True}
    spans = _sink_spans(sink)
    root = next(span for span in spans if span.name == "trial")
    llm = next(span for span in spans if _attribute_or_none(span, "lmnr.span.type") == "LLM")
    assert llm.parent_span_id == root.span_id
    assert llm.trace_id == root.trace_id


def test_harbor_process_exits_normally_while_real_sdk_cloud_exporter_is_stalled(tmp_path):
    # This is a unit-level offline exporter reproduction, not a model/cloud run.
    script = f"""
import importlib.util, json, os, sys, threading
spec = importlib.util.spec_from_file_location('trusted_laminar', {str(MODULE)!r})
tracing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracing)
def no_network(event, args):
    if event == 'socket.connect':
        os._exit(89)
sys.addaudithook(no_network)
from lmnr.opentelemetry_lib.tracing.exporter import LaminarSpanExporter
blocked = threading.Event()
never_released = threading.Event()
def stalled_export(self, spans):
    blocked.set()
    never_released.wait(60)
LaminarSpanExporter.export = stalled_export
runtime = tracing.initialize_tracing(secrets=('model-capability',))
assert runtime.enabled, runtime.error_type
with runtime.start_span('first-trial', session_id='actual-job') as first:
    first_id = first.trace_id
runtime.flush_async()
assert blocked.wait(3), 'SDK exporter did not actually stall'
# A live second trial must still work while the first export holds its locks.
with runtime.start_span('second-trial', session_id='actual-job') as second:
    assert second.trace_id != first_id
    with runtime.start_span('child') as child:
        assert child.trace_id == second.trace_id
    runtime.flush_async()
print(json.dumps({{'blocked': blocked.is_set(), 'distinct_trials': second.trace_id != first_id,
    'status': runtime.error_type}}))
"""
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("LMNR_", "OTEL_"))
    }
    env["LMNR_PROJECT_API_KEY"] = "fixture-laminar-key"
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    result = subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(result.stdout) == {"blocked": True, "distinct_trials": True, "status": None}


def test_sdk_sampling_omission_never_fabricates_a_recorded_trial_identity(tmp_path):
    result, sink = _sdk_process(
        tmp_path,
        """
with runtime.start_span('unrecorded-trial') as span:
    assert span.trace_id is None
    assert span.serialized_context() is None
runtime.flush_native()
print(json.dumps({'error': runtime.error_type}))
""",
        before_init="os.environ['OTEL_TRACES_SAMPLER'] = 'always_off'",
    )
    assert result == {"error": "NonRecordingSpan"}
    assert not sink.exists()


def test_real_auto_openai_preserves_failure_and_cause_while_exporting_only_private_errors(tmp_path):
    result, sink = _sdk_process(
        tmp_path,
        """
try:
    with runtime.start_span('failed-trial') as root:
        client.chat.completions.create(**kwargs)
except Exception as error:
    assert type(error) is type(before_error)
    assert str(error) == str(before_error)
    assert error.__cause__ is original_failure
    after_error = error
else:
    raise AssertionError('model exception was suppressed')
runtime.flush_native()
assert calls == 2
print(json.dumps({'error_type': type(after_error).__name__, 'same_cause': after_error.__cause__ is original_failure}))
""",
        automatic_openai=True,
        before_init="""
import httpx
from openai import OpenAI
calls = 0
original_failure = RuntimeError('model-capability /tmp/private-native-host/private-code')
def respond(request):
    global calls
    calls += 1
    raise original_failure
client = OpenAI(api_key='model-capability', max_retries=0,
    http_client=httpx.Client(transport=httpx.MockTransport(respond)))
kwargs = {'model': 'fixture-model', 'messages': [{'role': 'user', 'content': 'input'}]}
try:
    client.chat.completions.create(**kwargs)
except Exception as error:
    before_error = error
    assert error.__cause__ is original_failure
else:
    raise AssertionError('baseline did not fail')
""",
    )
    assert result == {"error_type": "APIConnectionError", "same_cause": True}
    spans = _sink_spans(sink)
    root = next(span for span in spans if span.name == "failed-trial")
    llm = next(span for span in spans if _attribute_or_none(span, "lmnr.span.type") == "LLM")
    assert llm.parent_span_id == root.span_id
    assert root.status.code == 2 and llm.status.code == 2
    assert root.status.message == "APIConnectionError"
    from google.protobuf.json_format import MessageToDict

    exported = json.dumps([MessageToDict(span) for span in spans])
    assert "model-capability" not in exported
    assert "private-native-host" not in exported
