"""Fail-open, sanitize-before-export Laminar tracing for trusted host processes.

Importable by file path in the isolated native worker: imports here are stdlib
only. The small private SDK adapter is deliberately pinned to lmnr 0.7.64.
Telemetry never owns execution, model transport, or process termination.
"""

from __future__ import annotations

import atexit
import base64
import importlib.metadata
import json
import logging
import os
import queue
import re
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

_SDK_VERSION = "0.7.64"
_CLOUD_BASE = "https://api.lmnr.ai"
_SCHEDULE_MS = 250
_EXPORT_TIMEOUT_SECONDS = 2
_LOCAL_FLUSH_MS = 500
_MAX_TEXT = 128 * 1024
_MAX_FRAME = 2 * 1024 * 1024
_REDACTED = "[REDACTED]"
_HOST_PATH = "[HOST_PATH]"
_TRUNCATED = "[TRUNCATED: telemetry size limit]"
# This fallback covers standalone Bearer values. The canonical execution-contract
# pattern, when supplied, additionally handles its established header spellings.
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_HOST_PREFIX = re.compile(r"/(?:Users|home)/[^\s\"'<>\\]+|/(?:private/)?var/folders/[^\s\"'<>\\]+")
_STACK_PATH = re.compile(r"(\bFile\s+[\"'])(/(?!testbed(?:/|[\"']))[^\"'\n]+)([\"'])")
_ABSOLUTE_HOST = re.compile(r"file://[^\s\"'<>]+|(?<![\w:/])/(?!testbed(?:/|$))[^\s\"'<>]+")
_runtime: TraceRuntime | None = None
_init_lock = threading.Lock()


class _Sanitizer:
    def __init__(self, secrets=(), host_paths=(), bearer_pattern=None):
        self._lock = threading.Lock()
        self._secrets: tuple[str, ...] = ()
        self._paths: tuple[str, ...] = ()
        self._patterns: tuple[re.Pattern, ...] = ()
        self.extend(secrets, host_paths, bearer_pattern)

    def extend(self, secrets=(), host_paths=(), bearer_pattern=None):
        with self._lock:
            self._secrets = tuple(
                sorted(set(self._secrets).union(s for s in secrets if s), key=len, reverse=True)
            )
            paths = set(self._paths)
            for path in host_paths:
                if path:
                    paths.add(str(path).rstrip("/"))
                    paths.add(str(Path(path).expanduser().absolute()).rstrip("/"))
            # Root is not a useful prefix and would erase all task/sandbox paths.
            self._paths = tuple(sorted((p for p in paths if p and p != "/"), key=len, reverse=True))
            if bearer_pattern:
                pattern = re.compile(bearer_pattern)
                if all(p.pattern != pattern.pattern for p in self._patterns):
                    self._patterns += (pattern,)

    def text(self, text: str, *, limit: int = _MAX_TEXT) -> str:
        with self._lock:
            secrets, paths, patterns = self._secrets, self._paths, self._patterns
        for secret in secrets:
            # Protect nested JSON strings, escaped quotes and Unicode escapes too.
            for form in {
                secret,
                json.dumps(secret)[1:-1],
                json.dumps(secret, ensure_ascii=False)[1:-1],
            }:
                text = text.replace(form, _REDACTED)
        for pattern in patterns:
            text = pattern.sub(_REDACTED, text)
        text = _BEARER.sub("Bearer " + _REDACTED, text)
        for path in paths:
            text = text.replace(path, _HOST_PATH)
            text = text.replace(json.dumps(path)[1:-1], _HOST_PATH)
        text = _HOST_PREFIX.sub(_HOST_PATH, text)
        text = _STACK_PATH.sub(lambda m: m[1] + _HOST_PATH + m[3], text)
        if len(text) > limit:
            text = text[: max(0, limit - len(_TRUNCATED))] + _TRUNCATED
        return text

    def value(self, value: Any, *, depth: int = 0) -> Any:
        if depth > 30:
            return _TRUNCATED
        if isinstance(value, str):
            # Rebuild embedded JSON before textual redaction; a token can be
            # represented with Unicode escapes rather than literal characters.
            if value.lstrip().startswith(("{", "[", '"')):
                try:
                    parsed = json.loads(value)
                except (ValueError, RecursionError):
                    pass
                else:
                    return self.text(
                        json.dumps(self.value(parsed, depth=depth + 1), ensure_ascii=False)
                    )
            return self.text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, Mapping):
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index == 10000:
                    result["evallab.telemetry.truncated"] = True
                    break
                result[self.value(str(key), depth=depth + 1)] = self.value(item, depth=depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            result = [self.value(item, depth=depth + 1) for item in value[:10000]]
            if len(value) > 10000:
                result.append(_TRUNCATED)
            return tuple(result) if isinstance(value, tuple) else result
        if isinstance(value, bytes):
            return self.value(value.decode("utf-8", errors="replace"), depth=depth + 1)
        # Never invoke a model/tool object's custom serializer or mutate it.
        return self.value(str(value), depth=depth + 1)

    def host_value(self, value, *, depth=0):
        if depth > 30:
            return _TRUNCATED
        if isinstance(value, str):
            if value.lstrip().startswith(("{", "[", '"')):
                try:
                    parsed = json.loads(value)
                except (ValueError, RecursionError):
                    pass
                else:
                    return json.dumps(self.host_value(parsed, depth=depth + 1), ensure_ascii=False)
            if value.startswith("/") and not (value == "/testbed" or value.startswith("/testbed/")):
                return _HOST_PATH
            return _ABSOLUTE_HOST.sub(_HOST_PATH, value)
        if isinstance(value, Mapping):
            return {
                self.host_value(key, depth=depth + 1): self.host_value(item, depth=depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return tuple(self.host_value(item, depth=depth + 1) for item in value)
        return value

    def attributes(self, attributes, *, host_surface=False) -> Mapping:
        result = self.value(attributes or {})
        for key, value in result.items():
            if host_surface or key.startswith(("code.", "process.executable", "process.command")):
                value = self.host_value(value)
            if (
                value is None
                or isinstance(value, Mapping)
                or (
                    isinstance(value, (list, tuple))
                    and any(not isinstance(item, (str, bool, int, float)) for item in value)
                )
            ):
                value = json.dumps(value, ensure_ascii=False)
            elif isinstance(value, list):
                value = tuple(value)
            result[key] = value
        return MappingProxyType(result)

    def context_data(self, data: dict) -> dict:
        # Trace/span IDs are typed identity, not model text. In particular a
        # short known secret must not corrupt hexadecimal UUID characters.
        identity = {}
        other = {}
        for key, value in data.items():
            if key in {"trace_id", "traceId", "span_id", "spanId"}:
                identity[key] = str(uuid.UUID(value))
            elif key in {"span_ids_path", "spanIdsPath"}:
                identity[key] = [str(uuid.UUID(item)) for item in value]
            elif key != "debug":
                other[key] = value
        return {**self.value(other), **identity}

    def span(self, span):
        """Create an independent immutable ReadableSpan, including all surfaces."""
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import Event, ReadableSpan
        from opentelemetry.sdk.util.instrumentation import InstrumentationScope
        from opentelemetry.trace import Link, SpanContext, Status, TraceState

        def context(original):
            if original is None:
                return None
            return SpanContext(
                trace_id=original.trace_id,
                span_id=original.span_id,
                is_remote=original.is_remote,
                trace_flags=original.trace_flags,
                trace_state=TraceState(
                    [(self.value(k), self.value(v)) for k, v in original.trace_state.items()]
                ),
            )

        scope = span.instrumentation_scope
        resource = span.resource
        return ReadableSpan(
            name=self.value(span.name),
            context=context(span.context),
            parent=context(span.parent),
            kind=span.kind,
            start_time=span.start_time,
            end_time=span.end_time,
            attributes=self.attributes(span.attributes),
            events=tuple(
                Event(self.value(e.name), self.attributes(e.attributes), e.timestamp)
                for e in span.events
            ),
            links=tuple(
                Link(context(link.context), self.attributes(link.attributes)) for link in span.links
            ),
            status=Status(
                span.status.status_code,
                self.value(span.status.description) if span.status.description else None,
            ),
            resource=Resource(
                self.attributes(resource.attributes, host_surface=True),
                schema_url=self.value(resource.schema_url or ""),
            ),
            instrumentation_scope=(
                InstrumentationScope(
                    self.value(scope.name),
                    self.value(scope.version or ""),
                    schema_url=self.value(scope.schema_url or ""),
                    attributes=self.attributes(scope.attributes, host_surface=True),
                )
                if scope is not None
                else None
            ),
        )

    def protobuf(self, message, *, host_surface=False):
        """Defense in depth for forwarded OTLP: keep wire IDs, redact all text."""
        key = getattr(message, "key", "")
        host_surface = host_surface or key.startswith(
            ("code.", "process.executable", "process.command")
        )

        def safe_text(text, field_name):
            if field_name == "trace_state":
                parts = []
                for item in text.split(","):
                    key, separator, value = item.partition("=")
                    parts.append(self.value(key) + separator + self.value(value))
                return ",".join(parts)
            result = self.value(text)
            if host_surface or field_name == "schema_url":
                result = self.host_value(result)
            return result

        for field, value in message.ListFields():
            if field.type == field.TYPE_MESSAGE:
                values = value if field.is_repeated else (value,)
                for child in values:
                    self.protobuf(
                        child, host_surface=host_surface or field.name in {"resource", "scope"}
                    )
            elif field.type == field.TYPE_STRING:
                if field.is_repeated:
                    for index, item in enumerate(value):
                        value[index] = safe_text(item, field.name)
                else:
                    setattr(message, field.name, safe_text(value, field.name))
            elif field.type == field.TYPE_BYTES and field.name == "bytes_value":
                setattr(
                    message,
                    field.name,
                    safe_text(value.decode("utf-8", errors="replace"), field.name).encode(),
                )


class _SafeExporter:
    def __init__(self, runtime, delegate=None, sink=None):
        self.runtime = runtime
        self.delegate: Any = delegate
        self.sink = sink
        self._file_lock = threading.Lock()

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            safe = tuple(self.runtime._sanitizer.span(span) for span in spans)
            if self.sink is not None:
                from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

                # One span per frame bounds the native transport and lets the
                # Harbor reader forward ended children while the root stays open.
                lines = []
                for span in safe:
                    request = encode_spans((span,))
                    data = request.SerializeToString()
                    if len(data) > _MAX_FRAME:
                        # Preserve IDs/timing, make any loss explicit, and reduce
                        # values (including automatic LLM attribute arrays).
                        self._shrink(request)
                        data = request.SerializeToString()
                    if len(data) > _MAX_FRAME:
                        raise ValueError("TelemetryFrameTooLarge")
                    lines.append(
                        json.dumps({"otlp": base64.b64encode(data).decode("ascii")}) + "\n"
                    )
                with self._file_lock, Path(self.sink).open("a", encoding="utf-8") as output:
                    output.writelines(lines)
                return SpanExportResult.SUCCESS
            result = self.delegate.export(safe)
            if result != SpanExportResult.SUCCESS:
                self.runtime.error_type = "ExportFailed"
            return result
        except BaseException as exc:
            self.runtime._failed(exc)
            return SpanExportResult.FAILURE

    @staticmethod
    def _shrink(request):
        def shorten(message):
            for field, value in message.ListFields():
                if field.type == field.TYPE_MESSAGE:
                    for child in value if field.is_repeated else (value,):
                        shorten(child)
                elif field.type == field.TYPE_STRING and not field.is_repeated and len(value) > 256:
                    setattr(message, field.name, value[:256] + _TRUNCATED)

        shorten(request)
        for resource_spans in request.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    marker = span.attributes.add()
                    marker.key = "evallab.telemetry.truncated"
                    marker.value.bool_value = True

    def force_flush(self, timeout_millis=30000):
        # No underlying Cloud flush is called by a local sink exporter.
        return True

    def shutdown(self):
        # Runtime/process lifecycle does not own a network shutdown handshake.
        return None


class _NoLogExporter:
    """Disable unrelated SDK log export, including at interpreter exit."""

    def __init__(self, **_kwargs):
        pass

    def export(self, _records):
        from opentelemetry.sdk._logs.export import LogRecordExportResult

        return LogRecordExportResult.SUCCESS

    def force_flush(self, timeout_millis=30000):
        return True

    def shutdown(self):
        return None


class _SdkAdapter:
    """Only private SDK dependency: 0.7.64 constructor exporter hook."""

    def __init__(self, runtime, key, automatic_openai, native_sink):
        if importlib.metadata.version("lmnr") != _SDK_VERSION:
            raise RuntimeError("UnsupportedLaminarVersion")
        from lmnr import Laminar
        from lmnr.opentelemetry_lib import tracing
        from lmnr.opentelemetry_lib.tracing import processor
        from lmnr.opentelemetry_lib.tracing.exporter import LaminarSpanExporter
        from lmnr.opentelemetry_lib.tracing.instruments import Instruments
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        if Laminar.is_initialized() or tracing.TracerWrapper.verify_initialized():
            # Adopting an unknown singleton could bypass the sanitizing boundary.
            raise RuntimeError("ForeignLaminarInitialization")
        instruments = {Instruments.OPENAI} if automatic_openai else set()
        delegate = (
            None
            if native_sink is not None
            else LaminarSpanExporter(
                base_url=_CLOUD_BASE,
                port=443,
                api_key=key,
                timeout_seconds=_EXPORT_TIMEOUT_SECONDS,
                force_http=True,
            )
        )
        exporter = _SafeExporter(runtime, delegate=delegate, sink=native_sink)
        if native_sink is not None:
            Path(native_sink).parent.mkdir(parents=True, exist_ok=True)

        class ShortBatchSpanProcessor(BatchSpanProcessor):
            def __init__(self, span_exporter, **kwargs):
                super().__init__(
                    span_exporter,
                    schedule_delay_millis=_SCHEDULE_MS,
                    export_timeout_millis=_EXPORT_TIMEOUT_SECONDS * 1000,
                    **kwargs,
                )

        # The SDK creates a log exporter unconditionally. Replacing only its
        # constructor binding prevents credential-bearing logs and native
        # termination network waits; no global logger/provider is installed.
        original_log = tracing.LaminarLogExporter
        original_batch = processor.BatchSpanProcessor
        cast(Any, tracing).LaminarLogExporter = _NoLogExporter
        cast(Any, processor).BatchSpanProcessor = ShortBatchSpanProcessor
        try:
            tracing.TracerWrapper.set_static_params({"service.name": "evallab"}, True)
            wrapper: Any = tracing.TracerWrapper(
                exporter=cast(Any, exporter),
                project_api_key=key,
                instruments=instruments,
                base_url=_CLOUD_BASE,
                port=8443,
                http_port=443,
                disable_batch=False,
                max_export_batch_size=64,
                flush_by_size=False,
                timeout_seconds=_EXPORT_TIMEOUT_SECONDS,
                force_http=True,
                set_global_tracer_provider=False,
                otel_logger_level=logging.CRITICAL,
            )
            Laminar.initialize(
                project_api_key=key,
                instruments=instruments,
                base_url=_CLOUD_BASE,
                http_port=443,
                grpc_port=8443,
                disable_batch=False,
                max_export_batch_size=64,
                flush_by_size=False,
                export_timeout_seconds=_EXPORT_TIMEOUT_SECONDS,
                force_http=True,
                set_global_tracer_provider=False,
                otel_logger_level=logging.CRITICAL,
            )
        finally:
            tracing.LaminarLogExporter = original_log
            processor.BatchSpanProcessor = original_batch
        self.laminar = Laminar
        # LaminarSpanProcessor.force_flush holds the same lock as on_start/end
        # while awaiting export. Drain the stable batch instance directly so an
        # offline/stalled exporter cannot stall model or lifecycle span events.
        # No processor reset/reinitialization is permitted during this runtime.
        self.processor = wrapper._span_processor.instance
        if automatic_openai:
            from lmnr.opentelemetry_lib.opentelemetry.instrumentation.openai import (
                OpenAIInstrumentor,
            )
            from lmnr.opentelemetry_lib.opentelemetry.instrumentation.openai.shared.config import (
                Config,
            )

            instrumentor = OpenAIInstrumentor._instance
            if instrumentor is None or not instrumentor.is_instrumented_by_opentelemetry:
                raise RuntimeError("OpenAIInstrumentationUnavailable")
            # SDK tracing is required; modifying model request headers is not.
            Config.enable_trace_context_propagation = False
        # Remove *all* SDK-owned synchronous exit waits in both modes. A stalled
        # Cloud exporter must not keep Harbor alive after execution completes.
        # Final Cloud delivery is best-effort through live batching/flush_async.
        atexit.unregister(wrapper.exit_handler)
        atexit.unregister(wrapper._tracer_provider.shutdown)
        atexit.unregister(wrapper._logger_provider.shutdown)
        if native_sink is not None:
            atexit.register(runtime.flush_native)

    def activate(self, span):
        from lmnr.opentelemetry_lib.tracing import context as isolated
        from opentelemetry import context, trace

        association = span.get_laminar_span_context()
        ctx = isolated.set_association_prop_context(
            user_id=association.user_id,
            session_id=association.session_id,
            trace_type=association.trace_type,
            metadata=association.metadata,
            context=isolated.get_current_context(),
            attach=False,
        )
        ctx = trace.set_span_in_context(span, ctx)
        isolated_token = isolated.attach_context(ctx)
        try:
            global_token = context.attach(ctx)
        except BaseException:
            isolated.detach_context(isolated_token)
            raise
        return isolated_token, global_token

    @staticmethod
    def deactivate(tokens):
        from lmnr.opentelemetry_lib.tracing import context as isolated
        from opentelemetry import context

        try:
            context.detach(tokens[1])
        finally:
            isolated.detach_context(tokens[0])


class TraceSpan:
    def __init__(self, runtime, span=None):
        self._runtime = runtime
        self._span = span
        self._tokens = None
        self._ended = False

    def __enter__(self):
        if self._span is not None and not self._ended:
            try:
                self._tokens = self._runtime._sdk.activate(self._span)
            except Exception as exc:
                self._runtime._failed(exc)
        return self

    def __exit__(self, exc_type, exc, traceback):
        # Do NOT pass the body exception into an SDK context manager: it can
        # record raw messages or replace/suppress it if its cleanup also fails.
        try:
            if self._tokens is not None:
                self._runtime._sdk.deactivate(self._tokens)
        except BaseException as failure:
            if exc_type is None and not isinstance(failure, Exception):
                raise
            self._runtime._failed(failure)
        finally:
            self._tokens = None
            try:
                self.end(error_type=exc_type.__name__ if exc_type is not None else None)
            except BaseException as failure:
                if exc_type is None and not isinstance(failure, Exception):
                    raise
                self._runtime._failed(failure)
        return False

    def _call(self, name, *args):
        if self._span is not None and not self._ended:
            try:
                getattr(self._span, name)(*args)
            except Exception as exc:
                self._runtime._failed(exc)

    def set_output(self, value):
        try:
            self._call("set_output", self._runtime._sanitizer.value(value))
        except Exception as exc:
            self._runtime._failed(exc)

    def set_metadata(self, metadata: dict):
        try:
            self._call("set_trace_metadata", self._runtime._sanitizer.value(metadata))
        except Exception as exc:
            self._runtime._failed(exc)

    def set_attributes(self, attributes: dict):
        try:
            self._call("set_attributes", self._runtime._sanitizer.attributes(attributes))
        except Exception as exc:
            self._runtime._failed(exc)

    def serialized_context(self) -> str | None:
        if self._span is None:
            return None
        try:
            raw = self._runtime._sdk.laminar.serialize_span_context(self._span)
            if raw is None:
                return None
            data = self._runtime._sanitizer.context_data(json.loads(raw))
            return json.dumps(data)
        except Exception as exc:
            self._runtime._failed(exc)
            return None

    @property
    def trace_id(self) -> str | None:
        if self._span is None:
            return None
        try:
            context = self._span.get_span_context()
            return str(uuid.UUID(int=context.trace_id)) if context.trace_id else None
        except Exception as exc:
            self._runtime._failed(exc)
            return None

    def end(self, *, error_type: str | None = None):
        if self._span is None or self._ended:
            return
        if error_type is not None:
            try:
                from opentelemetry.trace import Status, StatusCode

                self._call(
                    "set_status",
                    Status(StatusCode.ERROR, self._runtime._sanitizer.text(error_type)),
                )
                self._call("set_attribute", "error.type", self._runtime._sanitizer.text(error_type))
            except Exception as exc:
                self._runtime._failed(exc)
        try:
            self._span.end()
        except Exception as exc:
            self._runtime._failed(exc)
        finally:
            self._ended = True


class TraceRuntime:
    def __init__(self, *, error_type: str | None = None, sanitizer=None, native_sink=None):
        self.enabled = False
        self.error_type = error_type
        self._sanitizer = sanitizer or _Sanitizer()
        self._native_sink = native_sink
        self._sdk: _SdkAdapter | None = None
        self._automatic_openai = False
        self._native_queue: queue.Queue = queue.Queue(maxsize=32)
        self._forwarder = None
        self._forward_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._flush_thread = None
        self._key: str | None = None

    def _failed(self, exc):
        # Error messages and tracebacks often contain credentials/host paths.
        self.error_type = type(exc).__name__

    def start_span(
        self,
        name: str,
        *,
        span_type: Literal["DEFAULT", "LLM", "TOOL"] = "DEFAULT",
        input: Any = None,
        parent_context: str | None = None,
        session_id: str | None = None,
        metadata: dict | None = None,
        tags: list[str] | None = None,
    ) -> TraceSpan:
        sdk = self._sdk
        if not self.enabled or sdk is None:
            return TraceSpan(self)
        try:
            parent = None
            if parent_context is not None:
                data = self._sanitizer.context_data(json.loads(parent_context))
                # Validate BEFORE SDK start_span; SDK silently roots malformed
                # context. Dropping telemetry is preferable to false identity.
                parent = sdk.laminar.deserialize_span_context(data)
                if not (0 < parent.trace_id.int < 1 << 128 and 0 < parent.span_id.int < 1 << 64):
                    raise ValueError("InvalidTelemetryParent")
            span = sdk.laminar.start_span(
                self._sanitizer.value(name),
                span_type=span_type,
                input=self._sanitizer.value(input),
                parent_span_context=parent,
                session_id=self._sanitizer.value(session_id),
                metadata=self._sanitizer.value(metadata),
                tags=self._sanitizer.value(tags),
            )
            if not span.is_recording():
                self.error_type = "NonRecordingSpan"
                return TraceSpan(self)
            return TraceSpan(self, span)
        except Exception as exc:
            self._failed(exc)
            return TraceSpan(self)

    def flush_async(self) -> None:
        if not self.enabled:
            return
        try:
            with self._flush_lock:
                if self._flush_thread is not None and self._flush_thread.is_alive():
                    return
                self._flush_thread = threading.Thread(
                    target=self._flush, daemon=True, name="evallab-laminar-flush"
                )
                self._flush_thread.start()
        except Exception as exc:
            self._failed(exc)

    def _flush(self):
        sdk = self._sdk
        if sdk is None:
            return
        try:
            if not sdk.processor.force_flush(timeout_millis=_EXPORT_TIMEOUT_SECONDS * 1000):
                self.error_type = "FlushFailed"
        except BaseException as exc:
            self._failed(exc)

    def flush_native(self) -> None:
        if not self.enabled or self._native_sink is None:
            return
        try:
            # OTel versions can ignore force_flush's timeout while waiting for
            # the export lock. Bound our wait independently; work is local-only.
            self.flush_async()
            thread = self._flush_thread
            if thread is not None:
                thread.join(_LOCAL_FLUSH_MS / 1000)
                if thread.is_alive():
                    self.error_type = "LocalFlushTimeout"
        except Exception as exc:
            self._failed(exc)

    def export_native(self, payload: str) -> None:
        if not self.enabled or self._native_sink is not None:
            return
        try:
            if len(payload) > _MAX_FRAME * 2:
                raise ValueError("TelemetryFrameTooLarge")
            self._native_queue.put_nowait(payload)
            with self._forward_lock:
                if self._forwarder is None or not self._forwarder.is_alive():
                    self._forwarder = threading.Thread(
                        target=self._forward_native, daemon=True, name="evallab-laminar-forward"
                    )
                    self._forwarder.start()
        except Exception as exc:
            self._failed(exc)

    def _forward_native(self):
        key = self._key
        if key is None:
            return
        try:
            from urllib.error import HTTPError
            from urllib.request import HTTPRedirectHandler, Request, build_opener

            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
                ExportTraceServiceRequest,
            )

            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    raise HTTPError(req.full_url, code, "TelemetryRedirectRejected", headers, fp)

            opener = build_opener(NoRedirect())
        except BaseException as exc:
            self._failed(exc)
            return

        while True:
            payload = self._native_queue.get()
            try:
                request = ExportTraceServiceRequest.FromString(
                    base64.b64decode(payload, validate=True)
                )
                self._sanitizer.protobuf(request)
                body = request.SerializeToString()
                if len(body) > _MAX_FRAME:
                    raise ValueError("TelemetryFrameTooLarge")
                http_request = Request(
                    _CLOUD_BASE + "/v1/traces",
                    data=body,
                    method="POST",
                    headers={
                        "Authorization": "Bearer " + key,
                        "Content-Type": "application/x-protobuf",
                    },
                )
                with opener.open(http_request, timeout=_EXPORT_TIMEOUT_SECONDS) as response:
                    if not 200 <= response.status < 300:
                        self.error_type = "ExportFailed"
            except BaseException as exc:
                self._failed(exc)
            finally:
                self._native_queue.task_done()


def initialize_tracing(
    *,
    automatic_openai: bool = False,
    secrets: tuple[str, ...] = (),
    host_paths: tuple[str, ...] = (),
    native_sink: str | None = None,
    bearer_pattern: str | None = None,
) -> TraceRuntime:
    """Initialize once; absent key/disabled telemetry is an explicit no-op.

    The caller supplies the canonical known-secret values and bearer-header
    pattern. Only the centrally admitted LMNR key is added here. No model or
    sandbox environment is inspected or broadened.
    """
    global _runtime
    key = os.environ.get("LMNR_PROJECT_API_KEY")
    if not key:
        return TraceRuntime(error_type="Unconfigured")
    if os.environ.get("LMNR_DISABLE_TRACING", "").lower().strip() == "true":
        return TraceRuntime(error_type="Disabled")
    if os.environ.get("OTEL_SDK_DISABLED", "").lower().strip() == "true":
        return TraceRuntime(error_type="Disabled")
    if os.environ.get("LMNR_DEBUG", "").lower().strip() in {"true", "1", "yes", "on"}:
        # SDK debug replay changes provider request behavior and is not tracing.
        return TraceRuntime(error_type="DebugModeUnsupported")
    if os.environ.get("LMNR_SPAN_CONTEXT"):
        # Parentage is explicit in the contract. SDK ambient contexts can also
        # carry debug replay instructions, so do not initialize through them.
        return TraceRuntime(error_type="AmbientContextUnsupported")
    try:
        with _init_lock:
            if _runtime is not None:
                if _runtime._native_sink != native_sink:
                    return TraceRuntime(error_type="TracingModeConflict")
                if _runtime._automatic_openai != automatic_openai:
                    return TraceRuntime(error_type="TracingInstrumentConflict")
                _runtime._sanitizer.extend((*secrets, key), host_paths, bearer_pattern)
                return _runtime
            sanitizer = _Sanitizer((*secrets, key), host_paths, bearer_pattern)
            runtime = TraceRuntime(sanitizer=sanitizer, native_sink=native_sink)
            runtime._key = key
            runtime._automatic_openai = automatic_openai
            runtime._sdk = _SdkAdapter(runtime, key, automatic_openai, native_sink)
            runtime.enabled = True
            _runtime = runtime
            return runtime
    except Exception as exc:
        return TraceRuntime(error_type=type(exc).__name__)
