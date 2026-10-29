from __future__ import annotations

import io
import json
from typing import Any

import pytest

from evallab.laminar import SIGNAL_OPT_IN_FILTER, SIGNAL_OPT_IN_TAG, TAGS_ATTRIBUTE
from evallab.trace_store.core import ALLOWED_DATASETS, GOODFIRE_DATASET, TracePayload
from evallab.trace_store.laminar import PROJECT_ID, LaminarTarget, trace_spans
from evallab.trace_store.secrets import SecretScanRejected


def trace(dataset: str = "mimo-defect-map-v1") -> TracePayload:
    return TracePayload("own", dataset, {
        "model": "m", "run_id": "own", "source": dataset,
        "benchmark": "terminal-bench", "task_id": "terminal-bench/task",
        "outcome": "unknown", "split": "DEV",
    }, [
        {"step_index": 0, "role": "assistant", "content": "hello", "reasoning": "reasoning",
         "tool_calls": [], "tool_call_id": None},
    ])


class Recorder:
    def __init__(
        self, *, disabled: bool = True, project: str = PROJECT_ID, reply: bytes = b"",
        filters: list[dict[str, Any]] | None = None,
    ):
        self.disabled, self.project, self.reply, self.filters = disabled, project, reply, filters
        self.posts: list[bytes] = []

    def __call__(self, request: Any, timeout: int):
        if request.get_method() == "GET":
            signal = {
                "name": "preset", "projectId": self.project, "disabled": self.disabled,
                "filters": self.filters or [],
            }
            return io.BytesIO(json.dumps({"signals": [signal]}).encode())
        self.posts.append(request.data)
        return io.BytesIO(self.reply)


@pytest.mark.parametrize("disabled,project", [(False, PROJECT_ID), (True, "other")])
def test_upload_refuses_live_signals_or_wrong_project(disabled: bool, project: str) -> None:
    recorder = Recorder(disabled=disabled, project=project)
    with pytest.raises(ValueError):
        LaminarTarget(api_key="test", opener=recorder).upload(trace())
    assert not recorder.posts


def opted_in_recorder(*, filters: list[dict[str, Any]] | None = None) -> Recorder:
    return Recorder(disabled=False, filters=[SIGNAL_OPT_IN_FILTER] if filters is None else filters)


def test_opt_in_tags_the_root_span_with_an_array_attribute() -> None:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    recorder = opted_in_recorder()
    LaminarTarget(api_key="test", opener=recorder, signals_opt_in=True).upload(trace(GOODFIRE_DATASET))
    request = ExportTraceServiceRequest.FromString(b"".join(recorder.posts))
    tagged = [
        [item.string_value for item in attribute.value.array_value.values]
        for resource in request.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
        for attribute in span.attributes
        if attribute.key == TAGS_ATTRIBUTE
    ]
    assert tagged == [[SIGNAL_OPT_IN_TAG]]


def test_default_upload_never_adds_the_opt_in_tag() -> None:
    assert all(TAGS_ATTRIBUTE not in span["attributes"] for span in trace_spans(trace()))


@pytest.mark.parametrize("dataset", sorted(ALLOWED_DATASETS - {GOODFIRE_DATASET}))
def test_opt_in_refuses_non_goodfire_datasets_before_any_request(dataset: str) -> None:
    recorder = opted_in_recorder()
    with pytest.raises(ValueError, match="public Goodfire"):
        LaminarTarget(api_key="test", opener=recorder, signals_opt_in=True).upload(trace(dataset))
    assert not recorder.posts


def test_opt_in_refuses_a_live_signal_without_the_opt_in_filter() -> None:
    recorder = opted_in_recorder(filters=[])
    with pytest.raises(ValueError, match="opt-in filter"):
        LaminarTarget(api_key="test", opener=recorder, signals_opt_in=True).upload(trace(GOODFIRE_DATASET))
    assert not recorder.posts


@pytest.mark.parametrize("dataset", ["hackbench", "cheatbench", "HackBench", "unknown-dataset"])
def test_upload_rejects_unadmitted_dataset_before_any_span(
    dataset: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("span built for an unadmitted dataset")

    monkeypatch.setattr("evallab.trace_store.laminar.trace_spans", forbidden)
    recorder = Recorder()
    with pytest.raises(ValueError, match="not admitted"):
        LaminarTarget(api_key="test", opener=recorder).upload(trace(dataset))
    assert not recorder.posts


@pytest.mark.parametrize("dataset", sorted(ALLOWED_DATASETS))
def test_upload_accepts_every_docent_admitted_dataset(dataset: str) -> None:
    recorder = Recorder()
    LaminarTarget(api_key="test", opener=recorder).upload(trace(dataset))
    assert recorder.posts


def test_wire_byte_budget_is_checked_before_any_post() -> None:
    recorder = Recorder()
    target = LaminarTarget(api_key="test", opener=recorder)
    target.max_bytes = 1
    with pytest.raises(ValueError, match="allowance"):
        target.upload(trace())
    assert not recorder.posts
    assert target.sent_bytes == 0


def test_partial_otlp_rejection_cannot_be_marked_successful() -> None:
    recorder = Recorder(reply=b'{"partialSuccess":{"rejectedSpans":"1"}}')
    target = LaminarTarget(api_key="test", opener=recorder)
    with pytest.raises(ValueError, match="partially rejected"):
        target.upload(trace())
    assert target.sent_bytes == sum(map(len, recorder.posts)) > 0


def test_each_wire_attempt_reserves_exact_encoded_bytes() -> None:
    recorder = Recorder()
    target = LaminarTarget(api_key="test", opener=recorder)
    reservations: list[int] = []
    target.reserve_bytes = reservations.append
    result = target.upload(trace())
    assert reservations == list(map(len, recorder.posts))
    assert result["payload_bytes"] == sum(reservations) == target.sent_bytes


def test_json_encoded_newlines_do_not_create_credential_assignments() -> None:
    sample = trace()
    content = "TOKEN_EQ=0,\nTOKEN_GT=1,\nTOKEN_LT=2,\n"
    sample.messages[0]["content"] = content
    recorder = Recorder()
    result = LaminarTarget(api_key="test", opener=recorder).upload(sample)
    assert result["payload_bytes"] == sum(map(len, recorder.posts)) > 0
    assert b"TOKEN_EQ=0,\\nTOKEN_GT=1" in recorder.posts[0]


def test_real_credential_in_encoded_actor_text_is_rejected_before_post() -> None:
    sample = trace()
    sample.messages[0]["content"] = "credential: " + "sk-" + "proj-" + "A7b" * 16
    recorder = Recorder()
    with pytest.raises(SecretScanRejected) as exc:
        LaminarTarget(api_key="test", opener=recorder).upload(sample)
    assert exc.value.trace_id == sample.trace_id
    assert recorder.posts == []
