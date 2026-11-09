from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

from evallab.tracing import (
    SpanSummary,
    TraceBatch,
    TraceError,
    TrialTraceResult,
    convert_atif,
    convert_source,
    format_batch,
    instrument_openinference,
    is_control_trial,
    is_job_dir,
    is_trial_dir,
    iter_trial_dirs,
    leaked_values,
    load_trajectory,
    root_session_id,
    ship_resource_spans,
    span_attribute,
    span_ids_by_step,
    summarize_otel,
    trace_completed_jobs,
    trace_identity,
    trace_path,
    traces_endpoint,
    trial_agent_name,
    validate_atif,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "research/explorations/harbor-021/fixtures/trajectory.json"
)


def test_fixture_exists() -> None:
    assert FIXTURE.is_file(), "RECON atif2otel fixture is required"


def test_convert_fixture_has_root_agent_span() -> None:
    trajectory = json.loads(FIXTURE.read_text())
    _resource_spans, payload = convert_atif(trajectory)
    summary = summarize_otel(payload)
    assert validate_atif(trajectory) == []
    assert summary.n_spans > 0
    assert summary.has_root_agent
    assert "AGENT" in summary.span_kinds
    assert summary.n_root_spans == 1
    assert "codex" in summary.root_names


def test_cli_dry_run_on_fixture_does_not_need_phoenix() -> None:
    batch = trace_path(FIXTURE, dry_run=True, include_controls=True)
    assert batch.failed == 0
    assert batch.shipped == 1
    assert batch.results[0].summary is not None
    assert batch.results[0].summary.has_root_agent
    assert batch.results[0].message == "dry-run"


def test_missing_trajectory_is_clear_error(tmp_path: Path) -> None:
    trial = tmp_path / "empty-trial"
    trial.mkdir()
    (trial / "trial.log").write_text("started\n")
    with pytest.raises(TraceError, match="no ATIF trajectory") as exc:
        load_trajectory(trial / "agent" / "trajectory.json")
    assert "Traceback" not in str(exc.value)
    batch = trace_path(trial, dry_run=True, include_controls=True)
    assert batch.shipped == 0
    assert batch.skipped == 1
    assert "no ATIF trajectory" in batch.results[0].message


def test_invalid_trajectory_is_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "trajectory.json"
    path.write_text("{}\n")
    with pytest.raises(TraceError, match="invalid ATIF trajectory") as exc:
        convert_source(path)
    message = str(exc.value)
    assert "schema_version" in message or "missing" in message
    assert "Traceback" not in message


def test_malformed_json_is_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "trajectory.json"
    path.write_text("{not-json")
    with pytest.raises(TraceError, match="not valid JSON"):
        load_trajectory(path)


def test_oracle_control_is_skipped_unless_flagged(tmp_path: Path) -> None:
    trial = tmp_path / "event-summary__abc"
    agent = trial / "agent"
    agent.mkdir(parents=True)
    (trial / "trial.log").write_text("ok\n")
    (trial / "result.json").write_text(
        json.dumps({"agent_info": {"name": "oracle"}, "id": "1", "trial_name": "t"})
    )
    (agent / "trajectory.json").write_text(FIXTURE.read_text())
    assert is_control_trial(trial)
    skipped = trace_path(trial, dry_run=True, include_controls=False)
    assert skipped.skipped == 1
    assert skipped.shipped == 0
    included = trace_path(trial, dry_run=True, include_controls=True)
    assert included.shipped == 1


def test_instrument_openinference_with_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class LiteLLMInstrumentor:
        def instrument(self) -> None:
            calls.append("litellm")

    class DSPyInstrumentor:
        def instrument(self) -> None:
            calls.append("dspy")

    import sys

    for name in list(sys.modules):
        if name == "openinference" or name.startswith("openinference."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "openinference.instrumentation.litellm",
        SimpleNamespace(LiteLLMInstrumentor=LiteLLMInstrumentor),
    )
    monkeypatch.setitem(
        sys.modules,
        "openinference.instrumentation.dspy",
        SimpleNamespace(DSPyInstrumentor=DSPyInstrumentor),
    )
    monkeypatch.setitem(sys.modules, "openinference.instrumentation", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "openinference", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "dspy", SimpleNamespace())

    wired = instrument_openinference()
    assert wired == {"litellm": True, "dspy": True}
    assert calls == ["litellm", "dspy"]
    assert instrument_openinference(enabled=False) == {"litellm": False, "dspy": False}


def test_instrument_openinference_ignores_unavailable_instrumentors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing package is a normal installation, not an export failure."""
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "dspy", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "openinference", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "openinference.instrumentation", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.litellm", None)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.dspy", None)

    assert instrument_openinference() == {"litellm": False, "dspy": False}


# --------------------------------------------------------------------------
# Directory discovery: CLI accepts a trajectory file, a trial, or a job.
# --------------------------------------------------------------------------


def test_trial_and_job_directory_recognition(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert not is_trial_dir(missing)
    assert not is_job_dir(missing)

    logged_job = tmp_path / "logged-job"
    logged_job.mkdir()
    (logged_job / "job.log").write_text("job\n")
    assert is_job_dir(logged_job)
    assert not is_trial_dir(logged_job)

    trial = tmp_path / "task__abc"
    (trial / "agent").mkdir(parents=True)
    assert is_trial_dir(trial)
    assert not is_job_dir(trial)

    configured_job = tmp_path / "configured-job"
    configured_job.mkdir()
    (configured_job / "config.json").write_text("{}")
    assert not is_job_dir(configured_job)
    (configured_job / "task__abc" / "agent").mkdir(parents=True)
    assert is_job_dir(configured_job)


def test_iter_trial_dirs_accepts_trajectory_trial_and_job_paths(tmp_path: Path) -> None:
    trial = tmp_path / "runs" / "job" / "task__abc"
    agent = trial / "agent"
    agent.mkdir(parents=True)
    (trial / "trial.log").write_text("ok\n")
    (agent / "trajectory.json").write_text("{}\n")

    assert iter_trial_dirs(agent / "trajectory.json") == [trial]
    assert iter_trial_dirs(trial) == [trial]

    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "trajectory.json").write_text("{}\n")
    assert iter_trial_dirs(loose / "trajectory.json") == [loose]

    job = trial.parent
    (job / "job.log").write_text("job\n")
    assert iter_trial_dirs(job) == [trial]

    sibling = job / "task__def"
    (sibling / "agent").mkdir(parents=True)
    assert iter_trial_dirs(job) == [trial, sibling]


def test_iter_trial_dirs_reports_each_unusable_shape(tmp_path: Path) -> None:
    with pytest.raises(TraceError, match="path does not exist"):
        iter_trial_dirs(tmp_path / "absent")

    empty_job = tmp_path / "empty-job"
    empty_job.mkdir()
    (empty_job / "job.log").write_text("job\n")
    with pytest.raises(TraceError, match="job directory contains no trials"):
        iter_trial_dirs(empty_job)

    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(TraceError, match="not a Harbor trial or job directory"):
        iter_trial_dirs(other)


def test_iter_trial_dirs_falls_back_when_the_second_probe_sees_a_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last branch covers a trial marker written between the two probes."""
    trial = tmp_path / "late-trial"
    trial.mkdir()
    probes = iter([False, True])
    monkeypatch.setattr("evallab.tracing.is_trial_dir", lambda path: next(probes))
    monkeypatch.setattr("evallab.tracing.is_job_dir", lambda path: False)

    assert iter_trial_dirs(trial) == [trial]


def test_trajectory_must_be_a_json_object(tmp_path: Path) -> None:
    path = tmp_path / "trajectory.json"
    path.write_text("[1, 2]\n")
    with pytest.raises(TraceError, match="must be a JSON object"):
        load_trajectory(path)


def test_missing_atif2otel_dependency_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "harbor_atif2otel", None)
    with pytest.raises(TraceError, match="harbor-atif2otel is not installed"):
        validate_atif({})


def test_missing_atif2otel_ids_dependency_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "harbor_atif2otel.ids", None)
    with pytest.raises(TraceError, match="harbor-atif2otel is not installed"):
        trace_identity({"session_id": "abc"})


# --------------------------------------------------------------------------
# Summary and endpoint helpers.
# --------------------------------------------------------------------------


def test_summarize_otel_without_span_kinds_reports_no_agent_root() -> None:
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "workflow",
                                "attributes": [{"key": "other", "value": {"stringValue": "x"}}],
                            }
                        ]
                    }
                ]
            }
        ]
    }

    summary = summarize_otel(payload)

    assert summary.n_spans == 1
    assert summary.n_root_spans == 1
    assert summary.root_names == ("workflow",)
    assert summary.root_kinds == ()
    assert summary.span_kinds == ()
    assert not summary.has_root_agent


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://127.0.0.1:6006", "http://127.0.0.1:6006/v1/traces"),
        ("http://127.0.0.1:6006/", "http://127.0.0.1:6006/v1/traces"),
        ("http://phoenix/v1/traces", "http://phoenix/v1/traces"),
        ("http://phoenix/v1/traces/", "http://phoenix/v1/traces"),
    ],
)
def test_traces_endpoint_is_idempotent(endpoint: str, expected: str) -> None:
    assert traces_endpoint(endpoint) == expected


# --------------------------------------------------------------------------
# OTLP shipping: protobuf body, POST shape, and every failure the operator sees.
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fixture_resource_spans() -> Any:
    resource_spans, _payload = convert_atif(json.loads(FIXTURE.read_text()))
    return resource_spans


def test_ship_resource_spans_posts_protobuf_without_a_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opentelemetry.proto.trace.v1.trace_pb2 import TracesData

    seen: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        seen["request"] = request
        seen["timeout"] = timeout
        return _FakeResponse(200)

    monkeypatch.setattr("evallab.tracing.urlopen", fake_urlopen)

    ship_resource_spans(_fixture_resource_spans(), endpoint="http://collector.test:6006")

    request = seen["request"]
    assert request.full_url == "http://collector.test:6006/v1/traces"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/x-protobuf"
    assert seen["timeout"] == 15
    decoded = TracesData.FromString(request.data)
    assert len(decoded.resource_spans) == 1


def test_ship_resource_spans_reports_an_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evallab.tracing.urlopen", lambda request, timeout: _FakeResponse(503))
    with pytest.raises(TraceError, match="returned HTTP 503"):
        ship_resource_spans(_fixture_resource_spans(), endpoint="http://collector.test:6006")


def test_ship_resource_spans_reports_an_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        raise HTTPError(request.full_url, 500, "server error", None, None)

    monkeypatch.setattr("evallab.tracing.urlopen", fake_urlopen)
    with pytest.raises(TraceError, match="returned HTTP 500"):
        ship_resource_spans(_fixture_resource_spans(), endpoint="http://collector.test:6006")


def test_ship_resource_spans_reports_a_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        raise URLError("connection refused")

    monkeypatch.setattr("evallab.tracing.urlopen", fake_urlopen)
    with pytest.raises(TraceError, match="could not reach Phoenix OTLP") as exc:
        ship_resource_spans(_fixture_resource_spans(), endpoint="http://collector.test:6006")
    assert "connection refused" in str(exc.value)
    assert "docker compose up -d phoenix" in str(exc.value)


def test_ship_resource_spans_requires_the_protobuf_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Convert first: harbor_atif2otel needs the same protobuf module.
    resource_spans = _fixture_resource_spans()
    monkeypatch.setitem(sys.modules, "opentelemetry.proto.trace.v1.trace_pb2", None)
    with pytest.raises(TraceError, match="opentelemetry-proto is not installed"):
        ship_resource_spans(resource_spans, endpoint="http://collector.test:6006")


def test_trace_path_ships_when_not_a_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    posts: list[str] = []

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        posts.append(request.full_url)
        return _FakeResponse(200)

    monkeypatch.setattr("evallab.tracing.urlopen", fake_urlopen)

    batch = trace_path(
        FIXTURE,
        endpoint="http://collector.test:6006",
        dry_run=False,
        include_controls=True,
    )

    assert batch.shipped == 1
    assert batch.results[0].message == "shipped to http://collector.test:6006/v1/traces"
    assert posts == ["http://collector.test:6006/v1/traces"]


# --------------------------------------------------------------------------
# Trial naming, completion scanning, and batch rendering.
# --------------------------------------------------------------------------


def test_trial_agent_name_falls_back_to_config_and_tolerates_bad_json(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "task__abc"
    trial.mkdir()
    (trial / "result.json").write_text("{not-json")
    (trial / "config.json").write_text(json.dumps({"agent": {"name": "codex"}}))
    assert trial_agent_name(trial) == "codex"

    (trial / "config.json").write_text("{not-json")
    assert trial_agent_name(trial) == ""


def test_convert_source_requires_a_root_agent_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "resourceSpans": [
            {"scopeSpans": [{"spans": [{"name": "workflow", "spanId": "aa", "attributes": []}]}]}
        ]
    }
    monkeypatch.setattr(
        "evallab.tracing.convert_atif",
        lambda trajectory, *, service_name: (SimpleNamespace(), payload),
    )

    with pytest.raises(TraceError, match="no root AGENT span") as exc:
        convert_source(FIXTURE)
    assert "kinds=()" in str(exc.value)


def _write_trial(job: Path, name: str, *, trajectory: bool = True) -> Path:
    trial = job / name
    (trial / "agent").mkdir(parents=True)
    (trial / "trial.log").write_text("ok\n")
    (trial / "result.json").write_text(json.dumps({"agent_info": {"name": "codex"}}))
    if trajectory:
        (trial / "agent" / "trajectory.json").write_text(FIXTURE.read_text())
    return trial


def test_trace_completed_jobs_scans_only_job_directories(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "notes.txt").write_text("not a job\n")
    (runs / "scratch").mkdir()

    ok_job = runs / "ok-job"
    ok_job.mkdir()
    (ok_job / "job.log").write_text("job\n")
    _write_trial(ok_job, "task__abc")

    broken_job = runs / "broken-job"
    broken_job.mkdir()
    (broken_job / "job.log").write_text("job\n")
    _write_trial(broken_job, "task__def", trajectory=False)

    batch = trace_completed_jobs(runs, dry_run=True)

    assert batch.shipped == 1
    assert batch.skipped == 1
    assert batch.failed == 0
    assert {item.trial_dir.name for item in batch.results} == {"task__abc", "task__def"}


def test_trace_completed_jobs_on_a_missing_runs_dir_is_empty(tmp_path: Path) -> None:
    assert trace_completed_jobs(tmp_path / "absent", dry_run=True).results == []


def test_format_batch_summarizes_counts_spans_and_messages() -> None:
    batch = TraceBatch(
        results=[
            TrialTraceResult(
                trial_dir=Path("/runs/job/task__ok"),
                status="ok",
                summary=SpanSummary(
                    n_spans=4,
                    n_root_spans=1,
                    root_kinds=("AGENT",),
                    root_names=("codex",),
                    span_kinds=("AGENT", "LLM"),
                ),
            ),
            TrialTraceResult(
                trial_dir=Path("/runs/job/task__bare"),
                status="ok",
                summary=SpanSummary(0, 0, (), (), ()),
            ),
            TrialTraceResult(
                trial_dir=Path("/runs/job/task__skip"),
                status="skipped",
                message="control agent (oracle/nop)",
            ),
            TrialTraceResult(
                trial_dir=Path("/runs/job/task__bad"),
                status="failed",
                message="invalid ATIF trajectory",
            ),
        ]
    )

    lines = format_batch(batch).splitlines()

    assert lines[0] == "traced 2  skipped 1  failed 1"
    assert lines[1] == "  ok      task__ok  spans=4 root=codex kinds=AGENT,LLM"
    assert lines[2] == "  ok      task__bare  spans=0 root=- kinds="
    assert lines[3] == "  skipped task__skip  control agent (oracle/nop)"
    assert lines[4] == "  failed  task__bad  invalid ATIF trajectory"


# --------------------------------------------------------------------------
# Span helpers: step mapping, attribute lookup, root session, secret scan.
# --------------------------------------------------------------------------


def _otlp_payload(*spans: dict[str, Any]) -> dict[str, Any]:
    return {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}


def _attr(key: str, value: Any) -> dict[str, Any]:
    return {"key": key, "value": value}


def test_span_ids_by_step_reads_every_attribute_alias() -> None:
    payload = _otlp_payload(
        {"spanId": "s1", "attributes": [_attr("step_id", {"stringValue": "1"})]},
        {"span_id": "s2", "attributes": [_attr("atif.step_id", {"intValue": 2})]},
        {"spanID": "s3", "attributes": [_attr("evallab.step_id", 3)]},
        {
            "spanId": "s4",
            "attributes": [
                _attr("harbor.step_id", {"stringValue": "4"}),
                _attr("step.id", {"stringValue": 4}),
            ],
        },
    )

    assert span_ids_by_step(payload) == {1: "s1", 2: "s2", 3: "s3", 4: "s4"}


def test_span_ids_by_step_ignores_unmappable_spans() -> None:
    payload = _otlp_payload(
        {"attributes": [_attr("step_id", {"stringValue": "1"})]},
        {"spanId": "", "attributes": [_attr("step_id", {"stringValue": "1"})]},
        {
            "spanId": "not-an-attribute",
            "attributes": ["junk", _attr("other", {"stringValue": "1"})],
        },
        {"spanId": "boolean", "attributes": [_attr("step_id", True)]},
        {"spanId": "null", "attributes": [_attr("step_id", None)]},
        {"spanId": "not-a-number", "attributes": [_attr("step_id", {"stringValue": "nope"})]},
        {"spanId": "no-step", "attributes": []},
    )

    assert span_ids_by_step(payload) == {}


def test_span_ids_by_step_rejects_conflicting_step_identities() -> None:
    payload = _otlp_payload(
        {
            "spanId": "s1",
            "attributes": [
                _attr("step_id", {"stringValue": "1"}),
                _attr("atif.step_id", {"stringValue": "2"}),
            ],
        }
    )

    with pytest.raises(TraceError, match="ambiguous step identity on span 's1'"):
        span_ids_by_step(payload)


def test_span_ids_by_step_rejects_two_spans_for_one_step() -> None:
    payload = _otlp_payload(
        {"spanId": "s1", "attributes": [_attr("step_id", {"stringValue": "1"})]},
        {"spanId": "s2", "attributes": [_attr("step_id", {"stringValue": "1"})]},
    )

    with pytest.raises(TraceError, match="step 1 maps to multiple OTLP spans"):
        span_ids_by_step(payload)


def test_span_ids_by_step_allows_the_same_span_twice() -> None:
    span = {"spanId": "s1", "attributes": [_attr("step_id", {"stringValue": "1"})]}
    assert span_ids_by_step(_otlp_payload(span, span)) == {1: "s1"}


def test_span_attribute_absent_or_non_string_values_are_none() -> None:
    assert span_attribute({}, "session.id") is None
    assert (
        span_attribute({"attributes": [_attr("other", {"stringValue": "x"})]}, "session.id") is None
    )
    assert (
        span_attribute({"attributes": [_attr("session.id", {"intValue": 1})]}, "session.id") is None
    )


def test_root_session_id_requires_a_root_span_that_carries_a_session() -> None:
    child = {
        "spanId": "c",
        "parentSpanId": "p",
        "attributes": [_attr("session.id", {"stringValue": "child"})],
    }
    assert root_session_id(_otlp_payload(child)) is None
    assert root_session_id(_otlp_payload({"spanId": "r", "attributes": []})) is None
    assert (
        root_session_id(
            _otlp_payload({"spanId": "r", "attributes": [_attr("session.id", {"stringValue": ""})]})
        )
        is None
    )


def test_leaked_values_with_no_secrets_is_empty() -> None:
    payload = _otlp_payload({"spanId": "s1", "attributes": [_attr("input", {"stringValue": "x"})]})
    assert leaked_values(payload, []) == ()
    assert leaked_values(payload, ["", ""]) == ()
