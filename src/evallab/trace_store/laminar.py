"""Lossless DB-message projection through Eval Lab's existing OTLP encoders."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Any
from urllib.request import Request, urlopen

from evallab import laminar
from evallab.trace_store.core import (
    ALLOWED_DATASETS,
    GOODFIRE_DATASET,
    METADATA_FIELDS,
    TracePayload,
    trace_uuid,
)
from evallab.trace_store.secrets import SecretScanRejected, secret_hits

PROJECT_ID = "a5586b6d-55e8-466f-9718-c38255c2ae94"
BATCH_BYTES = 4 * 1024 * 1024
# Pure projection default. Uploads pass their import time; these are explicitly
# synthetic sequence positions, never measured action timestamps/durations.
PROJECTION_NS = 1_790_000_000_000_000_000


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def trace_spans(
    trace: TracePayload, *, at_ns: int = PROJECTION_NS, signals_opt_in: bool = False,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    calls: dict[str, dict[str, Any]] = {}
    assistant_messages: dict[str, dict[str, Any]] = {}
    for message in trace.messages:
        if message["role"] == "tool" and message.get("tool_call_id") in calls:
            step = calls[message["tool_call_id"]]
            step.setdefault("observation", {"results": []})["results"].append({
                "source_call_id": message["tool_call_id"], "content": message["content"],
            })
            continue
        step_id = str(message["step_index"])
        source = "agent" if message["role"] == "assistant" else message["role"]
        step: dict[str, Any] = {
            "step_id": step_id, "source": source, "message": message["content"],
            "model_name": trace.metadata["model"],
        }
        if message.get("reasoning") is not None:
            step["reasoning_content"] = message["reasoning"]
        if source == "agent":
            step["tool_calls"] = [
                {"tool_call_id": c["id"], "function_name": c["function"], "arguments": c["arguments"]}
                for c in message.get("tool_calls", [])
            ]
            for call in step["tool_calls"]:
                calls[call["tool_call_id"]] = step
            assistant_messages[step_id] = message
        steps.append(step)
    spans = laminar.step_spans(
        trace.trace_id, steps, final=True, clean=_text, fallback_ns=PROJECTION_NS,
    )
    for span in spans:
        attributes = span["attributes"]
        message = assistant_messages.get(attributes["evallab.step_id"])
        if attributes["lmnr.span.type"] == "LLM" and message is not None:
            reply: dict[str, Any] = {"role": "assistant", "content": message["content"]}
            if message.get("reasoning") is not None:
                reply["reasoning"] = message["reasoning"]
            if message.get("tool_calls"):
                reply["tool_calls"] = [
                    {"id": c["id"], "name": c["function"], "arguments": c["arguments"]}
                    for c in message["tool_calls"]
                ]
            attributes["lmnr.span.output"] = _text([reply])
        elif attributes["evallab.source"] == "tool":
            attributes["lmnr.span.type"] = "TOOL"
            attributes["lmnr.span.output"] = attributes.pop("lmnr.span.input")
        attributes["evallab.timing"] = "synthetic_sequence"
    root = laminar.root_span(
        trace.dataset_id, trace.trace_id, steps, {}, clean=_text, fallback_ns=PROJECTION_NS,
    )
    root["name"] = "evallab.trace-store"
    root["attributes"] = {
        "lmnr.span.type": "DEFAULT",
        "lmnr.span.input": root["attributes"]["lmnr.span.input"],
        "lmnr.span.output": _text({"messages": len(trace.messages)}),
        "evallab.timing": "synthetic_sequence",
        **{laminar.METADATA_PREFIX + k: v for k, v in trace.metadata.items()},
        **({laminar.TAGS_ATTRIBUTE: [laminar.SIGNAL_OPT_IN_TAG]} if signals_opt_in else {}),
    }
    # Root goes last: all child payloads must arrive before root-finished triggers.
    spans.append(root)
    remote_id = uuid.UUID(trace_uuid(trace.trace_id)).hex
    for index, span in enumerate(spans):
        span["trace_id"] = remote_id
        span["start_ns"] = at_ns + index * 1_000_000
        span["end_ns"] = span["start_ns"] + 1_000_000
        span["attributes"]["evallab.timing"] = "import_sequence_not_event_time"
    root["start_ns"] = at_ns
    root["end_ns"] = at_ns + len(spans) * 1_000_000
    return spans


def _span_secret_hits(spans: list[dict[str, Any]]) -> bool:
    for span in spans:
        attributes = dict(span["attributes"])
        # These fields are JSON containers built by the encoder. Scan their
        # logical values: escaped newlines must not turn a short TOKEN_EQ enum
        # into an apparent eight-character credential assignment.
        for name in ("lmnr.span.input", "lmnr.span.output"):
            value = attributes.get(name)
            if isinstance(value, str):
                with suppress(ValueError):
                    attributes[name] = json.loads(value)
        if secret_hits({**span, "attributes": attributes}):
            return True
    return False


class LaminarTarget:
    def __init__(
        self, *, api_key: str | None = None, endpoint: str | None = None,
        opener: Callable[..., Any] = urlopen, signals_opt_in: bool = False,
    ) -> None:
        """``signals_opt_in`` tags every trace for the opt-in Laminar Signals.

        It is a target-level switch so one sync can never mix tagged and untagged
        traces. Only the public Goodfire dataset may be tagged: Laminar's Privacy
        Mode is unconfirmed, so our own runs never reach a Signal.
        """
        self.signals_opt_in = signals_opt_in
        self.key = api_key if api_key is not None else os.environ.get(laminar.LAMINAR_KEY_ENV, "")
        if not self.key:
            raise ValueError("LMNR_PROJECT_API_KEY is required (use keys run --)")
        self.endpoint = endpoint or os.environ.get(laminar.LAMINAR_ENDPOINT_ENV) or laminar.LAMINAR_ENDPOINT
        self.opener = opener
        self.safe = False
        self.sent_bytes = 0
        self.requests = 0
        self.reserve_bytes: Callable[[int], None] | None = None
        self.max_bytes = 2_500_000_000

    def check_signals(self) -> None:
        data = laminar._json_call(
            "GET", "/v1/signals", api_key=self.key, endpoint=self.endpoint, opener=self.opener,
        )
        signals = data.get("signals", []) if isinstance(data, dict) else []
        if not signals or any(signal.get("projectId") != PROJECT_ID for signal in signals):
            raise ValueError("Cannot verify the existing private Laminar project")
        for signal in signals:
            if self.signals_opt_in:
                # Live Signals are acceptable only while each one still requires the tag.
                if laminar.SIGNAL_OPT_IN_FILTER not in (signal.get("filters") or []):
                    raise ValueError(f"Laminar Signal lacks the opt-in filter: {signal.get('name')}")
            elif signal.get("disabled") is not True:
                raise ValueError(f"Pause Laminar Signal before sync: {signal.get('name')}")
        self.safe = True

    def upload(self, trace: TracePayload) -> dict[str, Any]:
        if trace.dataset_id not in ALLOWED_DATASETS:
            raise ValueError("Dataset is not admitted to the private trace store")
        if self.signals_opt_in and trace.dataset_id != GOODFIRE_DATASET:
            raise ValueError("Laminar Signals may read only the public Goodfire dataset")
        if (
            set(trace.metadata) != METADATA_FIELDS
            or trace.metadata["run_id"] != trace.trace_id
            or trace.metadata["source"] != trace.dataset_id
            or trace.metadata["split"] != "DEV"
            or any(value is not None and not isinstance(value, str) for value in trace.metadata.values())
        ):
            raise ValueError("Laminar metadata must contain exactly the admitted scalar fields")
        if not self.safe:
            self.check_signals()
        spans = trace_spans(trace, at_ns=time.time_ns(), signals_opt_in=self.signals_opt_in)
        if _span_secret_hits(spans):
            raise SecretScanRejected(trace.trace_id)
        # A small span batch stays below the documented 5 MiB HTTP default.
        # Oversized single spans are sent intact, never clipped. A provider 413
        # is recorded by sync and stops the batch for a resumable size review.
        bodies: list[bytes] = []
        batch: list[dict[str, Any]] = []
        estimated = 0
        for span in spans:
            size = len(laminar.encode_spans([span]))
            if batch and estimated + size > BATCH_BYTES:
                bodies.append(laminar.encode_spans(batch))
                batch, estimated = [], 0
            batch.append(span)
            estimated += size
        if batch:
            bodies.append(laminar.encode_spans(batch))
        total = 0
        for body in bodies:
            if self.sent_bytes + len(body) > self.max_bytes:
                raise ValueError("Monthly payload-byte allowance reached")
            if self.reserve_bytes is not None:
                self.reserve_bytes(len(body))
            request = Request(
                self.endpoint.rstrip("/") + "/v1/traces", data=body, method="POST",
                headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/x-protobuf"},
            )
            self.sent_bytes += len(body)
            self.requests += 1
            with self.opener(request, timeout=120) as response:
                raw = response.read()
                # OTLP may acknowledge a partial rejection with HTTP 200.
                if raw:
                    if raw.lstrip().startswith(b"{"):
                        partial = json.loads(raw).get("partialSuccess", {})
                        rejected = partial.get("rejectedSpans", 0)
                    elif raw == b"null":
                        rejected = 0
                    else:
                        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
                            ExportTraceServiceResponse,
                        )
                        result = ExportTraceServiceResponse()
                        result.ParseFromString(raw)
                        rejected = result.partial_success.rejected_spans
                    if int(rejected):
                        raise ValueError("Laminar partially rejected OTLP spans")
            total += len(body)
        return {"remote_id": trace_uuid(trace.trace_id), "payload_bytes": total}
