"""Lifecycle regression tests for HAR-165 Harbor Laminar tracing.

Behavioral boundaries only: one trace across phases, per-trial isolation,
early-failure/missing-END closure, fail-open telemetry, actual reward
handling, cancellation preservation, mimo-only targeting, and safe
sidechannels. Uses fake Harbor events and a fake runtime; native AgentContext
cases require the optional Harbor extra. No network or Docker dependency.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab import harbor_laminar as hl


class FakeSpan:
    def __init__(self, name, runtime, trace_id):
        self.name = name
        self.runtime = runtime
        self.trace_id = trace_id
        self.metadata: dict = {}
        self.output = None
        self.ended = False
        self.error_type = "not-ended"
        self._context = f"ctx-{name}-{trace_id}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_output(self, value):
        self.output = value

    def set_metadata(self, values):
        self.metadata.update(values)

    def set_attributes(self, values):
        self.metadata.update(values)

    def serialized_context(self):
        return self._context

    def end(self, *, error_type=None):
        self.ended = True
        self.error_type = error_type


class FakeRuntime:
    def __init__(self):
        self.enabled = True
        self.error_type = None
        self.spans: list[FakeSpan] = []
        self._trace_seq = 0

    def start_span(self, name, **kwargs):
        self._trace_seq += 1
        # Root spans mint a trace id; children reuse the root's trace id when a
        # parent context is supplied so one trial stays one trace.
        parent = kwargs.get("parent_context")
        if parent is None:
            trace_id = f"trace-{self._trace_seq}"
        else:
            trace_id = self.spans[0].trace_id if self.spans else f"trace-{self._trace_seq}"
        span = FakeSpan(name, self, trace_id)
        span.metadata.update(kwargs.get("metadata") or {})
        self.spans.append(span)
        return span

    def export_native(self, payload):
        pass

    def flush_async(self):
        pass

    def flush_native(self):
        pass


def _mimo_config(trials_dir, trial_name="trial-a"):
    return SimpleNamespace(
        trial_name=trial_name,
        trials_dir=Path(trials_dir),
        job_id="job-1",
        agent=SimpleNamespace(name="mimoagent", import_path=hl.MIMO_AGENT_IMPORT_PATH),
    )


def _other_config(trials_dir):
    return SimpleNamespace(
        trial_name="trial-other",
        trials_dir=Path(trials_dir),
        job_id="job-1",
        agent=SimpleNamespace(name="nop", import_path="harbor.agents:nop"),
    )


def _event(config, trial_id, rewards=None, exception_type=None, event_value="end"):
    verifier_result = SimpleNamespace(rewards=rewards)
    result = SimpleNamespace(
        id=trial_id,
        verifier_result=verifier_result,
        exception_info=None
        if exception_type is None
        else SimpleNamespace(exception_type=exception_type),
    )
    return SimpleNamespace(
        trial_id=trial_id,
        trial_name=config.trial_name,
        task_name="task-x",
        config=config,
        result=result,
        lock=SimpleNamespace(),
        event=SimpleNamespace(value=event_value),
    )


@pytest.fixture
def fake_runtime(monkeypatch):
    runtime = FakeRuntime()
    hl._reset_for_tests()
    monkeypatch.setattr(hl, "_RUNTIME", runtime)
    monkeypatch.setenv(hl.KEY_ENV, "test-key")
    return runtime


def _register_trace(fake_runtime, tmp_path, trial_name="trial-a"):
    trial_id = uuid.uuid4()
    config = _mimo_config(tmp_path, trial_name=trial_name)
    trace = hl._open_trial_root(_event(config, trial_id, event_value="start"), session_id="job-x")
    assert trace is not None
    return trial_id, config, trace


def _persist_like_native(target, *, exit_status, stop_reason=None):
    """Mirror ``NativeMimoAgent._persist``'s actual metadata contract."""
    target.agent_result.metadata = {
        "native_revision": "test-rev",
        "native_exit_status": exit_status,
        "model_requests": 3,
        "antihack": False,
    }
    if exit_status == "LimitsExceeded":
        target.agent_result.metadata["native_exit_result"] = "trial budget exhausted"
    if stop_reason is not None:
        target.agent_result.metadata["stop_reason"] = stop_reason


def test_agent_run_unknown_stop_when_persist_never_ran(fake_runtime, tmp_path):
    AgentContext = pytest.importorskip("harbor.models.agent.context").AgentContext

    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    target = SimpleNamespace(agent_result=None)

    async def original(trial, *, target, **kwargs):
        # ``_persist`` returns early without main messages: metadata stays None.
        target.agent_result = AgentContext()
        return None

    trial = SimpleNamespace(_id=trial_id)
    assert (
        asyncio.run(
            hl._observed_agent_run(trial, trace, fake_runtime, original, (), {"target": target})
        )
        is None
    )
    span = fake_runtime.spans[-1]
    assert span.metadata["native_exit_status"] is None
    assert span.metadata["stop_reason"] is None
    assert span.error_type is None


def test_agent_run_error_preserves_exception_and_stop(fake_runtime, tmp_path):
    AgentContext = pytest.importorskip("harbor.models.agent.context").AgentContext

    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    target = SimpleNamespace(agent_result=None)

    async def original(trial, *, target, **kwargs):
        target.agent_result = AgentContext()
        _persist_like_native(target, exit_status="InfraError")
        raise RuntimeError("boom")

    trial = SimpleNamespace(_id=trial_id)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(
            hl._observed_agent_run(trial, trace, fake_runtime, original, (), {"target": target})
        )
    span = fake_runtime.spans[-1]
    assert span.metadata["native_exit_status"] == "InfraError"
    assert span.metadata["stop_reason"] is None
    assert span.error_type == "RuntimeError"


def test_agent_run_limits_exceeded_is_attribution_not_exception(fake_runtime, tmp_path):
    AgentContext = pytest.importorskip("harbor.models.agent.context").AgentContext

    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    target = SimpleNamespace(agent_result=None)

    async def original(trial, *, target, **kwargs):
        target.agent_result = AgentContext()
        _persist_like_native(
            target, exit_status="LimitsExceeded", stop_reason="trial_budget_exhausted"
        )
        return None

    trial = SimpleNamespace(_id=trial_id)
    assert (
        asyncio.run(
            hl._observed_agent_run(trial, trace, fake_runtime, original, (), {"target": target})
        )
        is None
    )
    span = fake_runtime.spans[-1]
    assert span.error_type is None
    assert span.metadata["native_exit_status"] == "LimitsExceeded"
    assert span.metadata["stop_reason"] == "trial_budget_exhausted"
    assert "native_exit_result" not in span.metadata


def test_agent_run_cancel_preserves_cancellation_and_stop(fake_runtime, tmp_path):
    AgentContext = pytest.importorskip("harbor.models.agent.context").AgentContext

    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    target = SimpleNamespace(agent_result=None)

    async def original(trial, *, target, **kwargs):
        target.agent_result = AgentContext()
        _persist_like_native(target, exit_status="HarborCancelled")
        raise asyncio.CancelledError()

    trial = SimpleNamespace(_id=trial_id)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            hl._observed_agent_run(trial, trace, fake_runtime, original, (), {"target": target})
        )
    span = fake_runtime.spans[-1]
    assert span.error_type == "CancelledError"
    assert span.metadata["native_exit_status"] == "HarborCancelled"
    assert hl._TRACES[str(trial_id)].native_stop == {
        "native_exit_status": "HarborCancelled",
        "stop_reason": None,
    }


def test_native_stop_ignores_missing_or_foreign_metadata():
    assert hl._native_stop_from_target(SimpleNamespace(agent_result=None)) == {
        "native_exit_status": None,
        "stop_reason": None,
    }
    assert hl._native_stop_from_target(
        SimpleNamespace(agent_result=SimpleNamespace(metadata="oops"))
    ) == {"native_exit_status": None, "stop_reason": None}
    foreign = SimpleNamespace(
        agent_result=SimpleNamespace(metadata={"native_revision": "rev", "model_requests": 2})
    )
    assert hl._native_stop_from_target(foreign) == {"native_exit_status": None, "stop_reason": None}


def test_missing_end_closed_by_run_finally(fake_runtime, tmp_path):
    trial_id = uuid.uuid4()
    config = _mimo_config(tmp_path)
    trace = hl._open_trial_root(_event(config, trial_id, event_value="start"), session_id="job-x")
    assert trace is not None and not trace.closed
    # No END hook fires (e.g. result persistence failed); run-finally closes.
    hl._close_trial_root(
        str(trial_id), reason="run-finally", trial_dir=Path(tmp_path) / config.trial_name
    )
    assert hl._TRACES[str(trial_id)].closed
    assert hl._TRACES[str(trial_id)].close_reason == "run-finally"


def test_cancel_does_not_close_before_stop(fake_runtime, tmp_path):
    trial_id = uuid.uuid4()
    config = _mimo_config(tmp_path)
    hl._open_trial_root(_event(config, trial_id, event_value="start"), session_id="job-x")
    hl._close_trial_root_from_result(
        _event(config, trial_id, event_value="cancel"), reason="cancelled", close_on_cancel=False
    )
    assert not hl._TRACES[str(trial_id)].closed
    hl._close_trial_root(
        str(trial_id), reason="run-finally", trial_dir=Path(tmp_path) / config.trial_name
    )
    assert hl._TRACES[str(trial_id)].closed


def test_telemetry_failures_cannot_change_result_or_cancellation(fake_runtime):
    class ExplodingSpan(FakeSpan):
        def set_metadata(self, values):
            raise RuntimeError("telemetry down")

        def set_output(self, value):
            raise RuntimeError("telemetry down")

        def end(self, *, error_type=None):
            raise RuntimeError("telemetry down")

    class ExplodingRuntime(FakeRuntime):
        def start_span(self, name, **kwargs):
            span = ExplodingSpan(name, self, "trace-x")
            self.spans.append(span)
            return span

    hl._RUNTIME = ExplodingRuntime()
    trial_id = uuid.uuid4()
    # Close path must swallow telemetry explosions.
    hl._TRACES[str(trial_id)] = hl._TrialTrace(
        trial_id=str(trial_id),
        trial_name="t",
        session_id="s",
        root=ExplodingSpan("harbor.trial", hl._RUNTIME, "trace-x"),
        trace_id="trace-x",
        root_context="ctx",
        metadata={},
        latest_rewards={"reward": 1},
    )
    hl._close_trial_root(str(trial_id), reason="end", rewards={"reward": 1})
    assert hl._TRACES[str(trial_id)].closed

    # Wrapper semantics: original exception and cancellation propagate exactly.
    async def _check():
        async def boom(*a, **k):
            raise ValueError("original")

        async def cancelled(*a, **k):
            raise asyncio.CancelledError()

        trial = SimpleNamespace(_id=trial_id)
        trace = hl._TRACES[str(trial_id)]
        trace.closed = False
        with pytest.raises(ValueError, match="original"):
            await hl._observed_agent_run(trial, trace, hl._RUNTIME, boom, (), {})
        trace.closed = False
        with pytest.raises(asyncio.CancelledError):
            await hl._observed_agent_run(trial, trace, hl._RUNTIME, cancelled, (), {})

    asyncio.run(_check())


@pytest.mark.parametrize(
    "rewards,expected",
    [
        (None, None),
        ({"reward": 1}, {"reward": 1.0}),
        ({"a": 0, "b": 1}, {"a": 0.0, "b": 1.0}),
        ({"reward": 0}, {"reward": 0.0}),
    ],
)
def test_reward_boundaries_preserved(fake_runtime, tmp_path, rewards, expected):
    trial_id = uuid.uuid4()
    config = _mimo_config(tmp_path)
    hl._open_trial_root(_event(config, trial_id, event_value="start"), session_id="job-x")
    hl._close_trial_root_from_result(_event(config, trial_id, rewards=rewards), reason="end")
    assert hl._safe_rewards(hl._TRACES[str(trial_id)].latest_rewards) == expected


def test_non_mimo_and_disabled_are_noops(tmp_path, monkeypatch):
    hl._reset_for_tests()
    monkeypatch.delenv(hl.KEY_ENV, raising=False)
    plugin = hl.LaminarTrialPlugin()

    class Job:
        config = SimpleNamespace(job_name="job-x")
        _id = "job-1"

        def on_trial_started(self, cb):
            self.started = cb
            return self

        def on_trial_ended(self, cb):
            self.ended = cb
            return self

        def on_trial_cancelled(self, cb):
            self.cancelled = cb
            return self

    job = Job()
    asyncio.run(plugin.on_job_start(job))
    # Disabled runtime: no root even for mimo.
    trial_id = uuid.uuid4()
    asyncio.run(job.started(_event(_mimo_config(tmp_path), trial_id, event_value="start")))
    assert hl.native_worker_context(str(trial_id)) == {}
    assert hl.trace_runtime().enabled is False


def test_mimo_gating_and_metadata_unknowns(fake_runtime):
    assert hl._is_mimo_config(_mimo_config("/tmp")) is True
    assert hl._is_mimo_config(_other_config("/tmp")) is False
    assert hl._is_mimo_event(_event(_other_config("/tmp"), uuid.uuid4())) is False
    meta = hl._build_root_metadata(
        _event(_mimo_config("/tmp"), uuid.uuid4(), event_value="start"), {}
    )
    assert meta["card"] is None
    assert meta["model_revision_source"] is None
    assert meta["intended_lock_mode"] is None
    assert "model_revision_scope" not in meta
    assert meta["egress_lock_observed"] == "pending"
    assert "trial_uri" not in json.dumps(meta) and "capability" not in json.dumps(meta)


def test_sidechannel_never_leaks_paths_or_keys(fake_runtime, tmp_path, monkeypatch):
    monkeypatch.setenv(hl.KEY_ENV, "super-secret-key")
    trial_id = uuid.uuid4()
    config = _mimo_config(tmp_path)
    hl._open_trial_root(_event(config, trial_id, event_value="start"), session_id="job-x")
    hl._close_trial_root_from_result(_event(config, trial_id, rewards={"reward": 1}), reason="end")
    raw = (Path(tmp_path) / config.trial_name / hl.TRACE_FILENAME).read_text()
    assert "super-secret-key" not in raw
    assert str(tmp_path) not in raw
    assert "laminar.sh" not in raw and "trace_id" in raw


def test_journal_docker_monitoring_unchanged():
    from evallab.harbor_state_journal import (
        StateJournalPlugin,
        compose_project_name,
        monitor_command,
    )

    plugin = StateJournalPlugin()
    assert plugin.watch_root == "/app"
    assert plugin.monitors == {}
    assert plugin._laminar is not None
    assert compose_project_name("Sample.Task__abc") == "sample-task__abc__env"
    command = monitor_command(
        image="img",
        monitor_name="m",
        target_pid=123,
        output_dir=Path("/tmp/journal-test"),
        watch_root="/app",
        max_hash_bytes=8,
    )
    assert "--pid=host" in command
    assert not any(part == "/app" for part in command[1:3])


def test_parent_metadata_transport_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv(
        hl.METADATA_ENV,
        json.dumps(
            {
                "card": "HAR-165",
                "arm": "a",
                "task": "t",
                "job_name": "job-x",
                "intended_setup_fingerprint": "abc123",
                "model_revision": "rev-1",
                "model_revision_source": "serve.py MODEL_REVISION",
                "intended_lock_mode": "egress_lock=true",
                "bearer_pattern": "(?i)(authorization\\s*[:=]\\s*bearer\\s+)\\S+",
                "evil": "drop-me",
                "LMNR_PROJECT_API_KEY": "drop-me",
            }
        ),
    )
    meta = hl._load_parent_metadata()
    assert meta["card"] == "HAR-165"
    assert meta["model_revision_source"] == "serve.py MODEL_REVISION"
    assert meta["intended_lock_mode"] == "egress_lock=true"
    assert meta["bearer_pattern"] == "(?i)(authorization\\s*[:=]\\s*bearer\\s+)\\S+"
    assert "evil" not in meta and "LMNR_PROJECT_API_KEY" not in meta
    monkeypatch.setenv(hl.METADATA_ENV, json.dumps({"card": None}))
    assert hl._load_parent_metadata()["card"] is None
    monkeypatch.delenv(hl.METADATA_ENV)
    assert hl._load_parent_metadata()["card"] is None
