"""Lifecycle regression tests for HAR-165 Harbor Laminar tracing.

Behavioral boundaries only: one trace across phases, per-trial isolation,
early-failure/missing-END closure, fail-open telemetry, actual reward
handling, cancellation preservation, mimo-only targeting, and safe
sidechannels. Public phase cases exercise Harbor's actual agent-phase method;
the exporter is a fake runtime. No network or Docker dependency.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab import harbor_laminar as hl
from evallab.execution_contracts import MIMO_AGENT_IMPORT_PATH


def _reset_laminar_state() -> None:
    with hl._RUNTIME_LOCK:
        hl._RUNTIME = None
    hl._JOB_SESSION = None
    hl._PARENT_METADATA = {}
    hl._WRAPPERS_INSTALLED = False
    with hl._TRACES_LOCK:
        hl._TRACES.clear()
    hl._ENVIRONMENT_PHASE.set(None)
    hl._AGENT_PHASE.set(None)
    hl._VERIFIER_PHASE.set(None)


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
        agent=SimpleNamespace(name=MIMO_AGENT_IMPORT_PATH, import_path=None),
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
    _reset_laminar_state()
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


@pytest.mark.parametrize(
    ("status", "stop_reason", "error", "span_error"),
    [
        (None, None, None, None),
        ("InfraError", None, RuntimeError("boom"), "InfraError"),
        ("LimitsExceeded", "trial_budget_exhausted", None, None),
        ("HarborCancelled", None, asyncio.CancelledError(), "CancelledError"),
    ],
)
def test_public_agent_phase_preserves_native_stop_and_termination(
    fake_runtime, tmp_path, status, stop_reason, error, span_error
):
    AgentContext = pytest.importorskip("harbor.models.agent.context").AgentContext
    Trial = pytest.importorskip("harbor.trial.trial").Trial
    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    event = _event(config, trial_id)
    plugin = hl.LaminarTrialPlugin()
    target = event.result

    class Agent:
        extra_env = {}

        async def run(self, *, context, **kwargs):
            assert isinstance(context, AgentContext)
            if status is not None:
                _persist_like_native(target, exit_status=status, stop_reason=stop_reason)
            if error is not None:
                raise error

    @asynccontextmanager
    async def network_scope(*args, **kwargs):
        yield

    async def emit(phase):
        if phase.value == "agent-start":
            await plugin._on_agent_started(event)
        elif phase.value == "agent-end":
            await plugin._on_agent_ended(event)

    environment = SimpleNamespace(
        with_default_user=lambda user: nullcontext(),
        scoped_exec_env=lambda env: nullcontext(),
        stream_enabled=False,
    )
    trial = SimpleNamespace(
        _emit=emit,
        _now=lambda: datetime.now(UTC),
        agent=Agent(),
        user_agent=None,
        agent_environment=environment,
        _network_plan=lambda step: SimpleNamespace(agent_env_baseline=None, agent_phase=None),
        _phase_network_policy=network_scope,
        _log_context=lambda *args: nullcontext(),
        paths=SimpleNamespace(agent_dir=tmp_path),
    )

    async def exercise():
        call = Trial._run_agent_phase(
            trial, target=target, instruction="fixture", timeout_sec=None, user=None
        )
        if error is None:
            await call
        else:
            with pytest.raises(type(error)) as raised:
                await call
            assert raised.value is error

    asyncio.run(exercise())
    span = next(span for span in fake_runtime.spans if span.name == "harbor.agent_run")
    assert span.ended
    assert span.metadata["native_exit_status"] == status
    assert span.metadata["stop_reason"] == stop_reason
    assert span.error_type == span_error
    assert "native_exit_result" not in span.metadata
    assert hl._TRACES[str(trial_id)].native_stop == {
        "native_exit_status": status,
        "stop_reason": stop_reason,
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
            await hl._observed_stop(trial, trace, hl._RUNTIME, boom, (), {})
        trace.closed = False
        with pytest.raises(asyncio.CancelledError):
            await hl._observed_stop(trial, trace, hl._RUNTIME, cancelled, (), {})

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
    pytest.importorskip("harbor.trial.hooks")
    _reset_laminar_state()
    monkeypatch.delenv(hl.KEY_ENV, raising=False)
    plugin = hl.LaminarTrialPlugin()

    class Job:
        config = SimpleNamespace(job_name="job-x")
        _id = "job-1"

        def add_hook(self, event, callback):
            setattr(self, event.value.replace("-", "_"), callback)
            return self

    job = Job()
    asyncio.run(plugin.on_job_start(job))
    # Disabled runtime: no root even for mimo.
    trial_id = uuid.uuid4()
    asyncio.run(job.start(_event(_mimo_config(tmp_path), trial_id, event_value="start")))
    assert hl.native_worker_context(str(trial_id)) == {}
    assert hl.trace_runtime().enabled is False


def test_mimo_gating_and_metadata_unknowns(fake_runtime):
    pytest.importorskip("harbor.agents.factory")
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


def test_canonical_cli_agent_identity_opens_sdk_trace_but_builtin_precedence_does_not(
    fake_runtime, tmp_path
):
    AgentConfig = pytest.importorskip("harbor.models.trial.config").AgentConfig
    plugin = hl.LaminarTrialPlugin()
    config = _mimo_config(tmp_path)
    config.agent = AgentConfig(name=MIMO_AGENT_IMPORT_PATH)
    trial_id = uuid.uuid4()
    event = _event(config, trial_id, event_value="start")
    asyncio.run(plugin._on_trial_started(event))
    trace = hl._TRACES[str(trial_id)]
    assert hl.native_worker_context(str(trial_id))["trace_id"] == trace.trace_id
    sidechannel = json.loads((tmp_path / config.trial_name / hl.TRACE_FILENAME).read_text())
    assert sidechannel["trace_id"] == trace.trace_id

    other = _other_config(tmp_path)
    other.agent = AgentConfig(name="nop", import_path=MIMO_AGENT_IMPORT_PATH)
    other_id = uuid.uuid4()
    asyncio.run(plugin._on_trial_started(_event(other, other_id, event_value="start")))
    assert str(other_id) not in hl._TRACES
    assert not (tmp_path / other.trial_name / hl.TRACE_FILENAME).exists()


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


# ---------------------------------------------------------------------------
# HAR-348: Harbor wrapper boundaries driven directly against the fake runtime.
# Each wrapper must annotate and end its span, and must hand the original's
# result, exception or cancellation back unchanged.
# ---------------------------------------------------------------------------


class _RaisingEnabledRuntime(FakeRuntime):
    """A runtime whose enabled probe itself fails (telemetry must fail open)."""

    @property
    def enabled(self):
        raise RuntimeError("runtime probe failed")

    @enabled.setter
    def enabled(self, value):
        pass


def _spans_named(runtime, name):
    return [span for span in runtime.spans if span.name == name]


def _wrapped_trial_class(original, *, kind, span_name="harbor.phase"):
    """Throwaway trial class whose ``method`` is wrapped like a Harbor method."""

    class FakeTrial:
        method = original

        def __init__(self, trial_id, result=None):
            self._id = trial_id
            self._result = result

    hl._wrap_trial_method(FakeTrial, "method", span_name=span_name, kind=kind)
    return FakeTrial


def _failing(error, value=None):
    calls = []

    async def original(self, *args, **kwargs):
        calls.append((self, args, kwargs))
        if error is not None:
            raise error
        return value

    return original, calls


async def _await_outcome(call, error, value):
    if error is None:
        assert await call is value
        return
    with pytest.raises(type(error)) as raised:
        await call
    assert raised.value is error


_OUTCOMES = [None, ValueError("original failed"), asyncio.CancelledError()]


def _error_name(error):
    return None if error is None else type(error).__name__


def test_trial_method_wrapper_installs_once_and_skips_missing_methods():
    original, _ = _failing(None)
    cls = _wrapped_trial_class(original, kind="setup")
    wrapped = cls.method
    assert wrapped is not original
    assert getattr(wrapped, "__evallab_laminar_wrapped__", False) is True
    hl._wrap_trial_method(cls, "method", span_name="harbor.phase", kind="setup")
    assert cls.method is wrapped
    hl._wrap_trial_method(cls, "missing", span_name="harbor.phase", kind="setup")
    assert not hasattr(cls, "missing")


@pytest.mark.parametrize("kind", ["environment", "setup", "stop", "shared_verifier"])
@pytest.mark.parametrize("state", ["absent", "closed", "disabled", "runtime-error"])
def test_trial_method_wrappers_pass_through_without_live_enabled_trace(
    fake_runtime, tmp_path, monkeypatch, kind, state
):
    trial_id, _, trace = _register_trace(fake_runtime, tmp_path)
    span_count = len(fake_runtime.spans)
    sentinel = object()
    original, calls = _failing(None, sentinel)
    cls = _wrapped_trial_class(original, kind=kind)
    trial = cls(uuid.uuid4() if state == "absent" else trial_id)
    if state == "closed":
        trace.closed = True
    elif state == "disabled":
        fake_runtime.enabled = False
    elif state == "runtime-error":
        monkeypatch.setattr(hl, "_RUNTIME", _RaisingEnabledRuntime())

    assert asyncio.run(trial.method(1, flag=True)) is sentinel
    assert calls == [(trial, (1,), {"flag": True})]
    assert len(fake_runtime.spans) == span_count
    assert trace.phase_spans == {}
    assert trace.native_stop is None and trace.latest_rewards is None


@pytest.mark.parametrize("error", _OUTCOMES)
def test_environment_wrapper_ends_sandbox_phase_with_error_type(fake_runtime, tmp_path, error):
    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    plugin = hl.LaminarTrialPlugin()
    sentinel = object()
    original, calls = _failing(error, sentinel)
    trial = _wrapped_trial_class(original, kind="environment", span_name="harbor.sandbox_setup")(
        trial_id
    )

    async def exercise():
        await plugin._on_environment_started(_event(config, trial_id))
        assert len(trace.phase_spans) == 1
        await _await_outcome(trial.method(), error, sentinel)
        assert hl._ENVIRONMENT_PHASE.get() is None

    asyncio.run(exercise())
    assert calls == [(trial, (), {})]
    (span,) = _spans_named(fake_runtime, "harbor.sandbox_setup")
    assert span.ended
    assert span.error_type == _error_name(error)
    assert trace.phase_spans == {}


@pytest.mark.parametrize("error", _OUTCOMES)
def test_setup_wrapper_opens_and_ends_agent_setup_span(fake_runtime, tmp_path, error):
    trial_id, _, trace = _register_trace(fake_runtime, tmp_path)
    sentinel = object()
    original, calls = _failing(error, sentinel)
    trial = _wrapped_trial_class(original, kind="setup", span_name="harbor.agent_setup")(trial_id)

    asyncio.run(_await_outcome(trial.method("arg", key="value"), error, sentinel))
    assert calls == [(trial, ("arg",), {"key": "value"})]
    (span,) = _spans_named(fake_runtime, "harbor.agent_setup")
    assert span.ended
    assert span.error_type == _error_name(error)
    assert span.metadata == {"trial_id": str(trial_id), "trial_name": trace.trial_name}


@pytest.mark.parametrize("error", [None, RuntimeError("stop failed"), asyncio.CancelledError()])
def test_stop_wrapper_attaches_native_stop_even_when_original_raises(
    fake_runtime, tmp_path, error
):
    trial_id, _, trace = _register_trace(fake_runtime, tmp_path)
    result = SimpleNamespace(agent_result=SimpleNamespace(metadata=None))
    _persist_like_native(result, exit_status="LimitsExceeded", stop_reason="trial_budget_exhausted")
    sentinel = object()
    original, calls = _failing(error, sentinel)
    trial = _wrapped_trial_class(original, kind="stop", span_name="harbor.stop")(trial_id, result)

    asyncio.run(_await_outcome(trial.method(), error, sentinel))
    assert calls == [(trial, (), {})]
    (span,) = _spans_named(fake_runtime, "harbor.stop")
    assert span.ended
    assert span.error_type == _error_name(error)
    assert span.metadata["native_exit_status"] == "LimitsExceeded"
    assert span.metadata["stop_reason"] == "trial_budget_exhausted"
    assert "native_exit_result" not in span.metadata
    assert trace.native_stop == {
        "native_exit_status": "LimitsExceeded",
        "stop_reason": "trial_budget_exhausted",
    }


@pytest.mark.parametrize("kind", ["shared_verifier", "separate_verifier"])
def test_verifier_wrapper_records_rewards_and_reward_child(fake_runtime, tmp_path, kind):
    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    plugin = hl.LaminarTrialPlugin()
    verifier_result = SimpleNamespace(rewards={"reward": 1, "label": "pass", "flag": True})
    original, calls = _failing(None, verifier_result)
    trial = _wrapped_trial_class(original, kind=kind, span_name="harbor.verifier")(trial_id)

    async def exercise():
        await plugin._on_verification_started(_event(config, trial_id))
        assert await trial.method() is verifier_result
        assert hl._VERIFIER_PHASE.get() is None

    asyncio.run(exercise())
    assert calls == [(trial, (), {})]
    expected = {"rewards": {"reward": 1.0}}
    (span,) = _spans_named(fake_runtime, "harbor.verifier")
    assert span.ended and span.error_type is None
    assert span.output == expected
    assert span.metadata["rewards"] == {"reward": 1.0}
    (reward,) = _spans_named(fake_runtime, "harbor.reward")
    assert reward.ended and reward.error_type is None
    assert reward.output == expected
    assert reward.metadata == {"trial_id": str(trial_id), "trial_name": trace.trial_name}
    assert trace.latest_rewards == {"reward": 1.0}
    assert trace.phase_spans == {}


@pytest.mark.parametrize("error", _OUTCOMES[1:])
def test_verifier_wrapper_ends_phase_with_error_type_and_no_reward(fake_runtime, tmp_path, error):
    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    plugin = hl.LaminarTrialPlugin()
    original, _ = _failing(error)
    trial = _wrapped_trial_class(original, kind="shared_verifier")(trial_id)

    async def exercise():
        await plugin._on_verification_started(_event(config, trial_id))
        await _await_outcome(trial.method(), error, None)

    asyncio.run(exercise())
    (span,) = _spans_named(fake_runtime, "harbor.verifier")
    assert span.ended
    assert span.error_type == type(error).__name__
    assert span.output is None
    assert _spans_named(fake_runtime, "harbor.reward") == []
    assert trace.latest_rewards is None
    assert trace.phase_spans == {}


class _UnreadableRewards:
    @property
    def rewards(self):
        raise RuntimeError("rewards unreadable")


@pytest.mark.parametrize(
    "verifier_result", [SimpleNamespace(rewards=None), _UnreadableRewards()], ids=["none", "raises"]
)
def test_verifier_wrapper_without_usable_rewards_returns_result_unchanged(
    fake_runtime, tmp_path, verifier_result
):
    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    plugin = hl.LaminarTrialPlugin()
    original, _ = _failing(None, verifier_result)
    trial = _wrapped_trial_class(original, kind="separate_verifier")(trial_id)

    async def exercise():
        await plugin._on_verification_started(_event(config, trial_id))
        assert await trial.method() is verifier_result

    asyncio.run(exercise())
    (span,) = _spans_named(fake_runtime, "harbor.verifier")
    assert span.ended and span.error_type is None
    assert span.output is None
    assert _spans_named(fake_runtime, "harbor.reward") == []
    assert trace.latest_rewards is None
    assert trace.phase_spans == {}


def _install_run_wrapper(monkeypatch, original):
    Trial = pytest.importorskip("harbor.trial.trial").Trial
    # Snapshot the class attribute so monkeypatch restores the real Trial.run.
    monkeypatch.setattr(Trial, "run", original)
    hl._wrap_trial_run()
    wrapped = Trial.run
    assert wrapped is not original
    assert getattr(wrapped, "__evallab_laminar_wrapped__", False) is True
    hl._wrap_trial_run()
    assert Trial.run is wrapped
    return wrapped


def _finished_trial(trial_id, trial_dir):
    result = SimpleNamespace(
        verifier_result=SimpleNamespace(rewards={"reward": 0}),
        exception_info=SimpleNamespace(exception_type="AgentTimeoutError"),
        agent_result=SimpleNamespace(metadata=None),
    )
    _persist_like_native(result, exit_status="InfraError")
    return SimpleNamespace(_id=trial_id, _result=result, paths=SimpleNamespace(trial_dir=trial_dir))


@pytest.mark.parametrize("error", [None, RuntimeError("persist failed"), asyncio.CancelledError()])
def test_run_finally_closes_root_and_dangling_phases_without_changing_outcome(
    fake_runtime, tmp_path, monkeypatch, error
):
    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    plugin = hl.LaminarTrialPlugin()
    sentinel = object()
    original, calls = _failing(error, sentinel)
    run = _install_run_wrapper(monkeypatch, original)
    trial_dir = tmp_path / config.trial_name
    trial = _finished_trial(trial_id, trial_dir)

    async def exercise():
        # END never fires; the agent phase is still open when run unwinds.
        await plugin._on_agent_started(_event(config, trial_id))
        await _await_outcome(run(trial), error, sentinel)

    asyncio.run(exercise())
    assert calls == [(trial, (), {})]
    assert trace.closed and trace.close_reason == "run-finally"
    assert trace.native_stop == {"native_exit_status": "InfraError", "stop_reason": None}
    (agent,) = _spans_named(fake_runtime, "harbor.agent_run")
    assert agent.ended and agent.error_type == "AgentTimeoutError"
    root = trace.root
    assert root.ended and root.error_type == "AgentTimeoutError"
    assert root.output == {"rewards": {"reward": 0.0}}
    assert root.metadata["close_reason"] == "run-finally"
    assert root.metadata["exception_type"] == "AgentTimeoutError"
    sidechannel = json.loads((trial_dir / hl.TRACE_FILENAME).read_text())
    assert sidechannel["status"] == "closed"
    assert sidechannel["close_reason"] == "run-finally"
    assert sidechannel["rewards"] == {"reward": 0.0}
    assert sidechannel["native_exit_status"] == "InfraError"


@pytest.mark.parametrize("state", ["absent", "closed", "disabled"])
def test_run_finally_passes_through_without_live_trace(fake_runtime, tmp_path, monkeypatch, state):
    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    trace.close_reason = "end" if state == "closed" else None
    trace.closed = state == "closed"
    if state == "absent":
        trial_id = uuid.uuid4()
    elif state == "disabled":
        # A disabled runtime never opens a root, so run has nothing to close.
        fake_runtime.enabled = False
        trial_id = uuid.uuid4()
        assert hl._open_trial_root(_event(config, trial_id), session_id="job-x") is None
    span_count = len(fake_runtime.spans)
    sentinel = object()
    original, calls = _failing(None, sentinel)
    run = _install_run_wrapper(monkeypatch, original)
    trial = _finished_trial(trial_id, tmp_path / "unused")

    assert asyncio.run(run(trial)) is sentinel
    assert calls == [(trial, (), {})]
    assert len(fake_runtime.spans) == span_count
    assert trace.closed is (state == "closed")
    assert trace.close_reason == ("end" if state == "closed" else None)
    assert not trace.root.ended
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("error", [None, ValueError("original failed")])
def test_run_finally_telemetry_failure_cannot_change_trial_outcome(
    fake_runtime, tmp_path, monkeypatch, error
):
    trial_id, config, _ = _register_trace(fake_runtime, tmp_path)
    sentinel = object()
    original, _ = _failing(error, sentinel)
    run = _install_run_wrapper(monkeypatch, original)

    def explode(*args, **kwargs):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(hl, "_close_trial_root", explode)
    asyncio.run(_await_outcome(run(_finished_trial(trial_id, tmp_path)), error, sentinel))


_WRAPPED_TRIAL_METHODS = (
    "_setup_agent_environment",
    "_setup_agent",
    "_run_shared_verifier",
    "_run_separate_verifier",
    "_stop_agent_environment",
    "run",
)


def _snapshot_plugin_globals(monkeypatch):
    """Restore every class and module global that on_job_start mutates."""
    Trial = pytest.importorskip("harbor.trial.trial").Trial

    for name in _WRAPPED_TRIAL_METHODS:
        monkeypatch.setattr(Trial, name, getattr(Trial, name))
    for name in ("_WRAPPERS_INSTALLED", "_JOB_SESSION", "_PARENT_METADATA"):
        monkeypatch.setattr(hl, name, getattr(hl, name))
    return Trial


class _RecordingJob:
    config = SimpleNamespace(job_name="job-config")
    _id = "job-1"

    def __init__(self):
        self.hooks = {}

    def add_hook(self, event, callback):
        self.hooks[event] = callback
        return self


def test_plugin_hooks_drive_one_trace_through_every_phase(fake_runtime, tmp_path, monkeypatch):
    TrialEvent = pytest.importorskip("harbor.trial.hooks").TrialEvent
    Trial = _snapshot_plugin_globals(monkeypatch)
    monkeypatch.setenv(hl.METADATA_ENV, json.dumps({"job_name": "job-hooks"}))
    flushed = []
    monkeypatch.setattr(fake_runtime, "flush_async", lambda: flushed.append(True))
    plugin = hl.LaminarTrialPlugin()
    job = _RecordingJob()
    config = _mimo_config(tmp_path)
    other = _other_config(tmp_path)
    trial_id, other_id = uuid.uuid4(), uuid.uuid4()
    agent_end = _event(config, trial_id, event_value="agent-end")
    agent_end.result.agent_result = SimpleNamespace(metadata=None)
    _persist_like_native(agent_end.result, exit_status="HarborCancelled")

    async def exercise():
        await plugin.on_job_start(job)
        assert set(job.hooks) == set(TrialEvent)
        hooks = job.hooks
        await hooks[TrialEvent.START](_event(other, other_id, event_value="start"))
        await hooks[TrialEvent.START](_event(config, trial_id, event_value="start"))
        await hooks[TrialEvent.ENVIRONMENT_START](_event(config, trial_id))
        await hooks[TrialEvent.AGENT_START](_event(config, trial_id))
        await hooks[TrialEvent.AGENT_END](agent_end)
        await hooks[TrialEvent.VERIFICATION_START](_event(config, trial_id))
        await hooks[TrialEvent.CANCEL](_event(other, other_id, event_value="cancel"))
        await hooks[TrialEvent.CANCEL](_event(config, trial_id, event_value="cancel"))
        assert not hl._TRACES[str(trial_id)].closed
        await hooks[TrialEvent.END](_event(other, other_id))
        # Harbor's END event carries the same live trial result, stop included.
        end = _event(config, trial_id, rewards={"reward": 1})
        end.result.agent_result = agent_end.result.agent_result
        await hooks[TrialEvent.END](end)
        await plugin.on_job_end(None)

    asyncio.run(exercise())
    assert str(other_id) not in hl._TRACES
    trace = hl._TRACES[str(trial_id)]
    assert trace.session_id == "job-hooks"
    assert trace.closed and trace.close_reason == "end"
    assert trace.native_stop == {"native_exit_status": "HarborCancelled", "stop_reason": None}
    root = trace.root
    assert root.ended and root.error_type is None
    assert root.metadata["cancelled"] is True
    assert root.output == {"rewards": {"reward": 1.0}}
    (agent,) = _spans_named(fake_runtime, "harbor.agent_run")
    assert agent.ended and agent.error_type == "CancelledError"
    assert agent.metadata["native_exit_status"] == "HarborCancelled"
    # END closes the phases whose completion wrappers never ran.
    for name in ("harbor.sandbox_setup", "harbor.verifier"):
        (span,) = _spans_named(fake_runtime, name)
        assert span.ended and span.error_type is None
    assert flushed == [True]
    # on_job_start installed every Harbor wrapper exactly once.
    installed = {name: getattr(Trial, name) for name in _WRAPPED_TRIAL_METHODS}
    assert all(getattr(fn, "__evallab_laminar_wrapped__", False) for fn in installed.values())
    hl._ensure_wrappers_installed()
    assert {name: getattr(Trial, name) for name in _WRAPPED_TRIAL_METHODS} == installed


def test_phase_hooks_ignore_unknown_closed_and_malformed_events(fake_runtime, tmp_path):
    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    plugin = hl.LaminarTrialPlugin()
    phase_hooks = (
        plugin._on_environment_started,
        plugin._on_agent_started,
        plugin._on_agent_ended,
        plugin._on_verification_started,
    )
    span_count = len(fake_runtime.spans)

    async def exercise():
        for hook in phase_hooks:
            assert await hook(SimpleNamespace()) is None
            assert await hook(_event(config, uuid.uuid4())) is None
        trace.closed = True
        for hook in phase_hooks:
            assert await hook(_event(config, trial_id)) is None

    asyncio.run(exercise())
    assert len(fake_runtime.spans) == span_count
    assert trace.phase_spans == {} and trace.native_stop is None


@pytest.mark.parametrize("error", [RuntimeError("telemetry down"), asyncio.CancelledError()])
def test_lifecycle_hooks_swallow_errors_but_propagate_cancellation(
    fake_runtime, tmp_path, monkeypatch, error
):
    pytest.importorskip("harbor.trial.hooks")
    _snapshot_plugin_globals(monkeypatch)

    def explode(*args, **kwargs):
        raise error

    for name in ("_ensure_runtime", "trace_runtime", "_open_trial_root"):
        monkeypatch.setattr(hl, name, explode)
    monkeypatch.setattr(hl, "_close_trial_root_from_result", explode)
    plugin = hl.LaminarTrialPlugin()
    config = _mimo_config(tmp_path)
    trial_id = uuid.uuid4()
    job = _RecordingJob()
    calls = (
        lambda: plugin.on_job_start(job),
        lambda: plugin.on_job_end(None),
        lambda: plugin._on_trial_started(_event(config, trial_id, event_value="start")),
        lambda: plugin._on_trial_ended(_event(config, trial_id)),
        lambda: plugin._on_trial_cancelled(_event(config, trial_id, event_value="cancel")),
    )

    async def exercise():
        for call in calls:
            if isinstance(error, asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError) as raised:
                    await call()
                assert raised.value is error
            else:
                assert await call() is None

    asyncio.run(exercise())
    assert job.hooks == {}
    assert str(trial_id) not in hl._TRACES


@pytest.mark.parametrize("fails", [False, True], ids=["initialized", "init-raises"])
def test_ensure_runtime_passes_redaction_inputs_and_fails_open(monkeypatch, fails):
    _reset_laminar_state()
    monkeypatch.setenv(hl.KEY_ENV, "test-key")
    monkeypatch.setattr(hl, "_PARENT_METADATA", {"bearer_pattern": "  parent-pattern  "})
    assert hl.trace_runtime() is hl._DISABLED
    initialized = FakeRuntime()
    calls = []

    def fake_initialize(**kwargs):
        calls.append(kwargs)
        if fails:
            raise RuntimeError("sdk init failed")
        return initialized

    monkeypatch.setattr(hl, "initialize_tracing", fake_initialize)
    runtime = hl._ensure_runtime()
    assert hl._ensure_runtime() is runtime
    assert hl.trace_runtime() is runtime
    (kwargs,) = calls
    assert kwargs["automatic_openai"] is False
    assert isinstance(kwargs["secrets"], tuple)
    assert kwargs["host_paths"] == (str(Path.cwd()), str(Path.home()))
    assert kwargs["bearer_pattern"] == "parent-pattern"
    if fails:
        assert runtime.enabled is False
        assert runtime.error_type == "RuntimeError"
    else:
        assert runtime is initialized
    _reset_laminar_state()


def test_native_worker_context_is_empty_unless_trial_is_live_and_enabled(fake_runtime, tmp_path):
    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    plugin = hl.LaminarTrialPlugin()
    root_context = hl.native_worker_context(str(trial_id))
    assert root_context["parent_context"] == trace.root_context
    assert root_context["session_id"] == "job-x"
    assert root_context["trial_id"] == str(trial_id)

    async def during_agent_phase():
        await plugin._on_agent_started(_event(config, trial_id))
        return hl.native_worker_context(str(trial_id))

    agent_context = asyncio.run(during_agent_phase())
    (agent,) = _spans_named(fake_runtime, "harbor.agent_run")
    assert agent_context["parent_context"] == agent.serialized_context()
    assert hl.native_worker_context(None) == {}
    assert hl.native_worker_context(str(uuid.uuid4())) == {}
    fake_runtime.enabled = False
    assert hl.native_worker_context(str(trial_id)) == {}
    fake_runtime.enabled = True
    trace.root_context = None
    assert hl.native_worker_context(str(trial_id)) == {}


def test_trial_root_opens_once_and_closes_once(fake_runtime, tmp_path):
    trial_id, config, trace = _register_trace(fake_runtime, tmp_path)
    again = hl._open_trial_root(_event(config, trial_id, event_value="start"), session_id="job-y")
    assert again is trace
    (duplicate,) = [
        span for span in _spans_named(fake_runtime, "harbor.trial") if span is not trace.root
    ]
    assert duplicate.ended and not trace.root.ended
    hl._close_trial_root(str(trial_id), reason="end", rewards={"reward": 1})
    hl._close_trial_root(str(trial_id), reason="run-finally", rewards={"reward": 0})
    assert trace.close_reason == "end"
    assert trace.latest_rewards == {"reward": 1}
    assert trace.root.metadata["close_reason"] == "end"
    assert trace.root.output == {"rewards": {"reward": 1.0}}


def test_native_stop_falls_back_to_latest_attributed_step():
    attributed = SimpleNamespace(agent_result=SimpleNamespace(metadata=None))
    _persist_like_native(
        attributed, exit_status="LimitsExceeded", stop_reason="trial_budget_exhausted"
    )
    silent = SimpleNamespace(agent_result=None)
    unknown = {"native_exit_status": None, "stop_reason": None}
    assert hl._native_stop_from_result(
        SimpleNamespace(agent_result=None, step_results=[attributed, silent])
    ) == {"native_exit_status": "LimitsExceeded", "stop_reason": "trial_budget_exhausted"}
    assert (
        hl._native_stop_from_result(SimpleNamespace(agent_result=None, step_results=[silent]))
        == unknown
    )
    assert hl._native_stop_from_result(SimpleNamespace(agent_result=None, step_results=5)) == unknown


def test_session_resolution_falls_back_to_job_identity():
    assert hl._resolve_session_from_job(SimpleNamespace(config=None, _id="job-7"), {}) == "job-7"
    no_identity = SimpleNamespace(config=SimpleNamespace(job_name=hl._UNKNOWN, job_id=None))
    assert hl._resolve_session_from_job(no_identity, {}) == hl._UNKNOWN


def test_parent_metadata_coerces_structured_and_blank_values(monkeypatch):
    monkeypatch.setenv(
        hl.METADATA_ENV,
        json.dumps({"card": {"b": 1, "a": [2]}, "arm": 3, "task": "  "}),
    )
    meta = hl._load_parent_metadata()
    assert meta["card"] == '{"a":[2],"b":1}'
    assert meta["arm"] == "3"
    assert meta["task"] is None
