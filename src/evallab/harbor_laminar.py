"""Live Laminar tracing lifecycle for Harbor mimoagent trials (HAR-165).

One live Laminar trace per actual Harbor trial. The root opens on Harbor's
public lifecycle hooks. ``END`` closes it after Harbor's existing cleanup;
a narrow ``Trial.run`` finally closes early failures and a missing ``END``.
Public phase hooks open sandbox, agent and verifier spans. Narrow completion
wrappers cover only boundaries Harbor does not expose: setup/verifier end,
agent setup, provider-acknowledged egress lock and environment stop.

Fail-open by construction: every telemetry call is guarded, ``Exception``
never escapes into Harbor execution, and ``CancelledError``/``KeyboardInterrupt``
always propagate unchanged. No network calls, no reordered locks, no changed
await/cancellation/error/cleanup semantics. Non-mimo trials and runs without
``LMNR_PROJECT_API_KEY`` are no-ops. No Cloud URLs are fabricated; the
``trial/laminar-trace.json`` sidechannel stores only trace/session/trial
identity and safe status.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from evallab.execution_contracts import _BEARER_HEADER, collected_secret_values
from evallab.laminar_tracing import TraceRuntime, initialize_tracing

METADATA_ENV = "EVALLAB_LAMINAR_METADATA"
KEY_ENV = "LMNR_PROJECT_API_KEY"
TRACE_FILENAME = "laminar-trace.json"
SIDECHANNEL_SCHEMA_VERSION = 1

MIMO_AGENT_IMPORT_PATH = "evallab.harbor_mimoagent:NativeMimoAgent"

_METADATA_KEYS = (
    "card",
    "arm",
    "task",
    "job_name",
    "model_session",
    "intended_setup_fingerprint",
    "model_revision",
    "model_revision_source",
    "intended_lock_mode",
    "bearer_pattern",
)

_UNKNOWN = "unknown"


_DISABLED = TraceRuntime(error_type="Unconfigured")

_RUNTIME: Any = None
_RUNTIME_LOCK = threading.Lock()
_JOB_SESSION: str | None = None
_PARENT_METADATA: dict[str, Any] = {}
_WRAPPERS_INSTALLED = False


@dataclass
class _TrialTrace:
    trial_id: str
    trial_name: str
    session_id: str
    root: Any = None
    trace_id: str | None = None
    root_context: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    trials_dir: str | None = None
    latest_rewards: Any = None
    native_stop: dict[str, Any] | None = None
    phase_spans: dict[int, Any] = field(default_factory=dict)
    closed: bool = False
    close_reason: str | None = None


_TRACES: dict[str, _TrialTrace] = {}
_TRACES_LOCK = threading.Lock()

_ENVIRONMENT_PHASE: ContextVar[tuple[str, Any] | None] = ContextVar(
    "evallab_laminar_environment_phase", default=None
)
_AGENT_PHASE: ContextVar[tuple[str, Any] | None] = ContextVar(
    "evallab_laminar_agent_phase", default=None
)
_VERIFIER_PHASE: ContextVar[tuple[str, Any] | None] = ContextVar(
    "evallab_laminar_verifier_phase", default=None
)


# ---------------------------------------------------------------------------
# Shared APIs consumed by the native host wrapper
# ---------------------------------------------------------------------------


def trace_runtime() -> Any:
    """Initialized host runtime for forwarding sanitized native payloads.

    Returns a disabled safe runtime when no key is configured or
    initialization failed. Never raises.
    """
    runtime = _RUNTIME
    if runtime is None:
        return _DISABLED
    return runtime


def native_worker_context(context_id: str | None) -> dict[str, Any]:
    """Safe tracing context for the native worker of one actual trial.

    Returns ``{}`` when disabled or unknown. Never raises, never includes
    secrets, paths, or Cloud URLs.
    """
    try:
        if context_id is None:
            return {}
        runtime = trace_runtime()
        if not getattr(runtime, "enabled", False):
            return {}
        key = str(context_id)
        with _TRACES_LOCK:
            trace = _TRACES.get(key)
            snapshot = (
                None
                if trace is None
                else (
                    _active_parent(trace),
                    trace.session_id,
                    dict(trace.metadata),
                    trace.trace_id,
                    trace.trial_id,
                    trace.trial_name,
                )
            )
        if snapshot is None:
            return {}
        parent_context, session_id, metadata, trace_id, trial_id, trial_name = snapshot
        if parent_context is None:
            return {}
        return {
            "parent_context": parent_context,
            "session_id": session_id,
            "metadata": metadata,
            "bearer_pattern": _bearer_pattern_str(),
            "trace_id": trace_id,
            "trial_id": trial_id,
            "trial_name": trial_name,
        }
    except Exception:
        return {}


def daytona_telemetry_labels(trial_dir: Path) -> dict[str, str]:
    """Bind native sandbox observations to the actual open trial root.

    Harbor's environment session ID is ``trial_name__env``, not the trial UUID.
    Match the real trial directory, never an active agent-phase span or a name
    shared by another job. Only plain non-secret startup labels leave the host.
    """
    try:
        if not getattr(trace_runtime(), "enabled", False):
            return {}
        target = Path(trial_dir).absolute()
        with _TRACES_LOCK:
            matches = [
                trace
                for trace in _TRACES.values()
                if not trace.closed
                and trace.trials_dir
                and (Path(trace.trials_dir) / trace.trial_name).absolute() == target
            ]
            if len(matches) != 1:
                return {}
            trace = matches[0]
            context = json.loads(trace.root_context or "")
            trace_id = uuid.UUID(context["trace_id"]).hex
            parent_id = uuid.UUID(context["span_id"]).int
            if trace_id == "0" * 32 or not 0 < parent_id < 1 << 64:
                return {}
            labels = {
                "evallab.trace_id": trace_id,
                "evallab.parent_span_id": f"{parent_id:016x}",
                "evallab.session_id": trace.session_id,
                "trial_id": trace.trial_id,
            }
            if model_session := trace.metadata.get("model_session"):
                labels["model_session"] = str(model_session)
        # Daytona's documented startup format is comma-delimited key=value.
        # Unsupported identities must stay unbound, not silently change scope.
        if any(not value or "," in value or "=" in value for value in labels.values()):
            return {}
        return labels
    except Exception:
        # Native observability cannot break environment creation or grading.
        return {}


# ---------------------------------------------------------------------------
# Metadata / identity helpers (pure, testable)
# ---------------------------------------------------------------------------


def _safe_text(value: Any, *, limit: int = 256) -> str:
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return _UNKNOWN
    text = text.strip()
    if not text:
        return _UNKNOWN
    if len(text) > limit:
        text = text[:limit]
    return text


def _load_parent_metadata() -> dict[str, Any]:
    """Allowlisted parent transport; unknown values stay ``None`` (null)."""
    meta: dict[str, Any] = {}
    raw = os.environ.get(METADATA_ENV)
    parsed: dict[str, Any] = {}
    if raw:
        with contextlib.suppress(Exception):
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                parsed = loaded
    for key in _METADATA_KEYS:
        if key not in parsed or parsed.get(key) is None:
            meta[key] = None
            continue
        value = parsed[key]
        if isinstance(value, (dict, list)):
            with contextlib.suppress(Exception):
                value = json.dumps(value, sort_keys=True, separators=(",", ":"))[:512]
        if not isinstance(value, str):
            with contextlib.suppress(Exception):
                value = str(value)
        if not isinstance(value, str) or not value.strip():
            meta[key] = None
            continue
        meta[key] = value.strip()[:512]
    return meta


def _bearer_pattern_str() -> str | None:
    # Parent transports the canonical execution_contracts._BEARER_HEADER.pattern
    # (decoded) in EVALLAB_LAMINAR_METADATA; prefer it, fall back to the same
    # single convention on direct import. No competing pattern is defined here.
    try:
        supplied = _PARENT_METADATA.get("bearer_pattern")
        if isinstance(supplied, str) and supplied.strip():
            return supplied.strip()[:512]
    except Exception:
        pass
    try:
        return _BEARER_HEADER.pattern.decode("ascii")
    except Exception:
        return None


def _collect_secrets() -> tuple[str, ...]:
    return tuple(collected_secret_values(os.environ))


def _host_path_prefixes() -> tuple[str, ...]:
    prefixes: list[str] = []
    with contextlib.suppress(Exception):
        prefixes.append(str(Path.cwd()))
    with contextlib.suppress(Exception):
        home = str(Path.home())
        if home and home not in prefixes:
            prefixes.append(home)
    return tuple(prefixes)


def _agent_ref(config: Any) -> str:
    try:
        agent = getattr(config, "agent", None)
        name = getattr(agent, "name", None) or _UNKNOWN
        import_path = getattr(agent, "import_path", None) or ""
        if import_path:
            return f"{name} ({import_path})"
        return str(name)
    except Exception:
        return _UNKNOWN


def _is_mimo_config(config: Any) -> bool:
    try:
        from harbor.agents.factory import AgentFactory  # ty: ignore[unresolved-import]

        from evallab.harbor_mimoagent import NativeMimoAgent

        agent = getattr(config, "agent", None)
        if agent is None:
            return False
        # Use the same resolution as Harbor: --agent stores an import path in
        # name, and built-in names take precedence over an explicit import path.
        return AgentFactory.get_agent_class_from_config(agent) is NativeMimoAgent
    except Exception:
        return False


def _is_mimo_event(event: Any) -> bool:
    try:
        return _is_mimo_config(getattr(event, "config", None))
    except Exception:
        return False


def _resolve_session_from_job(job: Any, parent_meta: dict[str, Any]) -> str:
    for candidate in (
        parent_meta.get("job_name"),
        getattr(getattr(job, "config", None), "job_name", None),
    ):
        if candidate and str(candidate).strip() and str(candidate) != _UNKNOWN:
            return str(candidate).strip()
    with contextlib.suppress(Exception):
        job_id = getattr(job, "_id", None) or getattr(getattr(job, "config", None), "job_id", None)
        if job_id is not None and str(job_id).strip():
            return str(job_id).strip()
    return _UNKNOWN


def _build_root_metadata(event: Any, parent_meta: dict[str, Any]) -> dict[str, Any]:
    try:
        trial_id = str(getattr(event, "trial_id", _UNKNOWN))
    except Exception:
        trial_id = _UNKNOWN
    try:
        trial_name = str(getattr(event, "trial_name", _UNKNOWN))
    except Exception:
        trial_name = _UNKNOWN
    try:
        event_task = getattr(event, "task_name", None)
        task: Any = (
            str(event_task).strip()
            if event_task is not None and str(event_task).strip()
            else parent_meta.get("task")
        )
    except Exception:
        task = parent_meta.get("task")
    return {
        "trial_id": _safe_text(trial_id),
        "trial_name": _safe_text(trial_name),
        "task": task,
        "card": parent_meta.get("card"),
        "arm": parent_meta.get("arm"),
        "job_name": parent_meta.get("job_name"),
        "model_session": parent_meta.get("model_session"),
        "intended_setup_fingerprint": parent_meta.get("intended_setup_fingerprint"),
        "model_revision": parent_meta.get("model_revision"),
        "model_revision_source": parent_meta.get("model_revision_source"),
        "intended_lock_mode": parent_meta.get("intended_lock_mode"),
        "agent": _safe_text(_agent_ref(getattr(event, "config", None)), limit=128),
        "egress_lock_observed": "pending",
        "evidence_scope": "source-evidence-only",
    }


def _safe_rewards(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    safe: dict[str, Any] = {}
    for key, item in value.items():
        try:
            name = str(key)
        except Exception:
            continue
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            continue
        with contextlib.suppress(Exception):
            safe[name] = float(item)
    return safe


def _clean_stop_value(value: Any) -> str | None:
    """Short safe status string, or ``None`` when unknown."""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return None
    text = text.strip()
    if not text:
        return None
    return text[:128]


def _native_stop_from_target(target: Any) -> dict[str, Any]:
    """Actual ``NativeMimoAgent._persist`` stop attribution, never invented.

    ``_run_agent_phase`` returns ``None`` and populates
    ``target.agent_result.metadata``; ``_persist`` leaves metadata ``None``
    when no main messages exist, so unknown stays ``None``. The native
    result string is deliberately NOT read here: a ``LimitsExceeded`` result
    is a stop attribution, not an exception and not a guessed success.
    """
    stop: dict[str, Any] = {"native_exit_status": None, "stop_reason": None}
    try:
        context = getattr(target, "agent_result", None)
        metadata = getattr(context, "metadata", None)
        if not isinstance(metadata, dict):
            return stop
        if "native_exit_status" in metadata:
            stop["native_exit_status"] = _clean_stop_value(metadata.get("native_exit_status"))
        if "stop_reason" in metadata:
            stop["stop_reason"] = _clean_stop_value(metadata.get("stop_reason"))
    except Exception:
        pass
    return stop


def _native_stop_from_result(result: Any) -> dict[str, Any]:
    """Stop attribution from a trial/step result, with multistep fallback."""
    stop = _native_stop_from_target(result)
    if stop["native_exit_status"] is not None or stop["stop_reason"] is not None:
        return stop
    try:
        steps = getattr(result, "step_results", None) or []
        for step in reversed(list(steps)):
            candidate = _native_stop_from_target(step)
            if candidate["native_exit_status"] is not None or candidate["stop_reason"] is not None:
                return candidate
    except Exception:
        pass
    return stop


def _native_stop_from_trial(trial: Any) -> dict[str, Any]:
    try:
        result = getattr(trial, "_result", None)
        if result is None:
            with contextlib.suppress(Exception):
                result = getattr(trial, "result", None)
        if result is None:
            return {"native_exit_status": None, "stop_reason": None}
        return _native_stop_from_result(result)
    except Exception:
        return {"native_exit_status": None, "stop_reason": None}


def _attach_native_stop(span: Any, stop: dict[str, Any] | None) -> None:
    if stop is None:
        return
    with contextlib.suppress(Exception):
        _set_span_metadata(
            span,
            {
                "native_exit_status": stop.get("native_exit_status"),
                "stop_reason": stop.get("stop_reason"),
            },
        )


def _stash_native_stop(trace: _TrialTrace, stop: dict[str, Any] | None) -> None:
    if stop is None:
        return
    try:
        with _TRACES_LOCK:
            live = _TRACES.get(trace.trial_id)
            if live is not None and not live.closed:
                live.native_stop = {
                    "native_exit_status": stop.get("native_exit_status"),
                    "stop_reason": stop.get("stop_reason"),
                }
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Telemetry-safe small wrappers
# ---------------------------------------------------------------------------


def _start_child(
    runtime: Any,
    name: str,
    *,
    parent_context: str | None,
    session_id: str | None,
    metadata: dict[str, Any] | None = None,
    span_type: str = "DEFAULT",
    tags: list[str] | None = None,
) -> Any | None:
    try:
        if not getattr(runtime, "enabled", False):
            return None
        kwargs: dict[str, Any] = {"span_type": span_type}
        if parent_context is not None:
            kwargs["parent_context"] = parent_context
        if session_id is not None:
            kwargs["session_id"] = session_id
        if metadata is not None:
            kwargs["metadata"] = metadata
        if tags is not None:
            kwargs["tags"] = tags
        return runtime.start_span(name, **kwargs)
    except Exception:
        return None


def _serialized(span: Any) -> str | None:
    try:
        if span is None:
            return None
        context = span.serialized_context()
        return context if isinstance(context, str) else None
    except Exception:
        return None


def _set_span_metadata(span: Any, values: dict[str, Any]) -> None:
    try:
        if span is not None:
            span.set_metadata(values)
    except Exception:
        pass


def _set_span_output(span: Any, value: Any) -> None:
    try:
        if span is not None:
            span.set_output(value)
    except Exception:
        pass


def _end_span(span: Any, *, error_type: str | None = None) -> None:
    try:
        if span is not None:
            span.end(error_type=error_type)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Sidechannel persistence (safe identity/status only)
# ---------------------------------------------------------------------------


def _trial_dir_for_event(event: Any) -> Path | None:
    try:
        config = getattr(event, "config", None)
        trials_dir = getattr(config, "trials_dir", None)
        trial_name = getattr(config, "trial_name", None) or getattr(event, "trial_name", None)
        if trials_dir is None or trial_name is None:
            return None
        return Path(trials_dir) / str(trial_name)
    except Exception:
        return None


def _write_sidechannel(trace: _TrialTrace, directory: Path | None) -> None:
    try:
        if directory is None:
            return
        payload: dict[str, Any] = {
            "schema_version": SIDECHANNEL_SCHEMA_VERSION,
            "trace_id": trace.trace_id,
            "session_id": trace.session_id,
            "trial_id": trace.trial_id,
            "trial_name": trace.trial_name,
            "status": "closed" if trace.closed else "open",
        }
        if trace.root_context is not None:
            with contextlib.suppress(ValueError, TypeError, KeyError, AttributeError):
                root_id = uuid.UUID(json.loads(trace.root_context)["span_id"]).int
                if 0 < root_id < 1 << 64:
                    payload["root_span_id"] = f"{root_id:016x}"
        if trace.closed:
            payload["close_reason"] = trace.close_reason or _UNKNOWN
            rewards = _safe_rewards(trace.latest_rewards)
            payload["rewards"] = rewards
            stop = trace.native_stop or {}
            payload["native_exit_status"] = stop.get("native_exit_status")
            payload["stop_reason"] = stop.get("stop_reason")
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (TRACE_FILENAME + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(directory / TRACE_FILENAME)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Root lifecycle
# ---------------------------------------------------------------------------


def _ensure_runtime() -> Any:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is not None:
            return _RUNTIME
        key = os.environ.get(KEY_ENV)
        if not key or not key.strip():
            _RUNTIME = TraceRuntime(error_type="Unconfigured")
            return _RUNTIME
        try:
            _RUNTIME = initialize_tracing(
                automatic_openai=False,
                secrets=_collect_secrets(),
                host_paths=_host_path_prefixes(),
                bearer_pattern=_bearer_pattern_str(),
            )
        except Exception as exc:
            _RUNTIME = TraceRuntime(error_type=type(exc).__name__)
        return _RUNTIME


def _open_trial_root(event: Any, *, session_id: str) -> _TrialTrace | None:
    try:
        trial_id = str(event.trial_id)
        trial_name = str(event.trial_name)
    except Exception:
        return None
    runtime = _ensure_runtime()
    try:
        if not getattr(runtime, "enabled", False):
            return None
    except Exception:
        return None
    metadata = _build_root_metadata(event, _PARENT_METADATA)
    root = _start_child(
        runtime,
        "harbor.trial",
        parent_context=None,
        session_id=session_id,
        metadata=metadata,
        tags=["harbor", "mimoagent"],
    )
    if root is None:
        return None
    trace_id: str | None = None
    with contextlib.suppress(Exception):
        raw_trace = getattr(root, "trace_id", None)
        trace_id = str(raw_trace) if raw_trace is not None else None
    trace = _TrialTrace(
        trial_id=trial_id,
        trial_name=trial_name,
        session_id=session_id,
        root=root,
        trace_id=trace_id,
        root_context=_serialized(root),
        metadata=metadata,
        trials_dir=str(getattr(getattr(event, "config", None), "trials_dir", "") or ""),
    )
    with _TRACES_LOCK:
        existing = _TRACES.get(trial_id)
        if existing is not None and not existing.closed:
            # Never orphan a live root: keep the first opener's trace.
            _end_span(root)
            return existing
        _TRACES[trial_id] = trace
    _write_sidechannel(trace, _trial_dir_for_event(event))
    return trace


def _lookup_trial(trial_id: Any) -> _TrialTrace | None:
    try:
        with _TRACES_LOCK:
            return _TRACES.get(str(trial_id))
    except Exception:
        return None


def _close_trial_root(
    trial_id: Any,
    *,
    reason: str,
    rewards: Any = None,
    exception_type: str | None = None,
    trial_dir: Path | None = None,
    native_stop: dict[str, Any] | None = None,
) -> None:
    try:
        key = str(trial_id)
    except Exception:
        return
    with _TRACES_LOCK:
        trace = _TRACES.get(key)
        if trace is None or trace.closed:
            return
        trace.closed = True
        trace.close_reason = reason
        if rewards is not None:
            trace.latest_rewards = rewards
        if native_stop is not None:
            trace.native_stop = {
                "native_exit_status": native_stop.get("native_exit_status"),
                "stop_reason": native_stop.get("stop_reason"),
            }
        root = trace.root
        phases = tuple(trace.phase_spans.values())
        trace.phase_spans.clear()
    for span in phases:
        _end_span(span, error_type=exception_type)
    try:
        final_metadata: dict[str, Any] = {"close_reason": _safe_text(reason, limit=64)}
        safe_rewards = _safe_rewards(rewards if rewards is not None else trace.latest_rewards)
        if safe_rewards is not None:
            final_metadata["rewards"] = safe_rewards
            _set_span_output(root, {"rewards": safe_rewards})
        if exception_type is not None:
            final_metadata["exception_type"] = _safe_text(exception_type, limit=128)
        stop = trace.native_stop or {"native_exit_status": None, "stop_reason": None}
        final_metadata["native_exit_status"] = stop.get("native_exit_status")
        final_metadata["stop_reason"] = stop.get("stop_reason")
        _set_span_metadata(root, final_metadata)
    except Exception:
        pass
    _end_span(root, error_type=exception_type)
    try:
        with _TRACES_LOCK:
            snapshot = _TRACES.get(key)
        if snapshot is not None:
            _write_sidechannel(snapshot, trial_dir)
    except Exception:
        pass


def _close_trial_root_from_result(
    event: Any, *, reason: str, close_on_cancel: bool = False
) -> None:
    """Close from a hook event carrying the live ``result``.

    ``CANCEL`` never closes: recovery and shielded stop still follow.
    """
    try:
        result = getattr(event, "result", None)
        rewards = getattr(getattr(result, "verifier_result", None), "rewards", None)
        exc_info = getattr(result, "exception_info", None)
        exception_type = getattr(exc_info, "exception_type", None)
        if getattr(event, "event", None) is not None:
            try:
                event_name = str(getattr(event.event, "value", event.event)).lower()
                if "cancel" in event_name and not close_on_cancel:
                    trace = _lookup_trial(event.trial_id)
                    if trace is not None and not trace.closed:
                        with contextlib.suppress(Exception):
                            _set_span_metadata(
                                trace.root, {"cancelled": True, "close_reason": reason}
                            )
                    return
            except Exception:
                pass
        _close_trial_root(
            event.trial_id,
            reason=reason,
            rewards=rewards,
            exception_type=str(exception_type) if exception_type is not None else None,
            trial_dir=_trial_dir_for_event(event),
            native_stop=_native_stop_from_result(result),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Narrow Harbor wrappers (installed once per process)
# ---------------------------------------------------------------------------


def _trial_uuid_of(instance: Any) -> str | None:
    for attr in ("_id", "id"):
        with contextlib.suppress(Exception):
            value = getattr(instance, attr, None)
            if value is None:
                continue
            text = str(value() if callable(value) else value)
            if text.strip():
                return text
    with contextlib.suppress(Exception):
        result = getattr(instance, "result", None)
        if result is not None and getattr(result, "id", None) is not None:
            return str(result.id)
    return None


def _active_parent(trace: _TrialTrace) -> str | None:
    phase = _AGENT_PHASE.get()
    if phase is not None and phase[0] == trace.trial_id:
        return _serialized(phase[1]) or trace.root_context
    return trace.root_context


def _open_phase(
    trace: _TrialTrace,
    scope: ContextVar[tuple[str, Any] | None],
    name: str,
    *,
    span_type: str = "DEFAULT",
) -> None:
    span = _start_child(
        trace_runtime(),
        name,
        parent_context=trace.root_context,
        session_id=trace.session_id,
        metadata={"trial_id": trace.trial_id, "trial_name": trace.trial_name},
        span_type=span_type,
    )
    if span is None:
        scope.set(None)
        return
    with _TRACES_LOCK:
        trace.phase_spans[id(span)] = span
    scope.set((trace.trial_id, span))


def _phase_for(trace: _TrialTrace, scope: ContextVar[tuple[str, Any] | None]) -> Any:
    phase = scope.get()
    return phase[1] if phase is not None and phase[0] == trace.trial_id else None


def _finish_phase(trace: _TrialTrace, span: Any, *, error_type: str | None = None) -> None:
    if span is None:
        return
    with _TRACES_LOCK:
        trace.phase_spans.pop(id(span), None)
    _end_span(span, error_type=error_type)


def _wrap_trial_method(cls: Any, name: str, *, span_name: str, kind: str) -> None:
    original = getattr(cls, name, None)
    if original is None or getattr(original, "__evallab_laminar_wrapped__", False):
        return

    @functools.wraps(original)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            trial_id = _trial_uuid_of(self)
            trace = _lookup_trial(trial_id) if trial_id is not None else None
            runtime = trace_runtime()
            enabled = getattr(runtime, "enabled", False)
        except Exception:
            trace, runtime, enabled = None, None, False
        if trace is None or trace.closed or not enabled:
            return await original(self, *args, **kwargs)
        if kind == "environment":
            error_type = None
            try:
                return await original(self, *args, **kwargs)
            except asyncio.CancelledError:
                error_type = "CancelledError"
                raise
            except Exception as exc:
                error_type = type(exc).__name__
                raise
            finally:
                with contextlib.suppress(Exception):
                    span = _phase_for(trace, _ENVIRONMENT_PHASE)
                    _ENVIRONMENT_PHASE.set(None)
                    _finish_phase(trace, span, error_type=error_type)
        if kind == "stop":
            return await _observed_stop(self, trace, runtime, original, args, kwargs)
        if kind in ("shared_verifier", "separate_verifier"):
            return await _observed_verifier(self, trace, runtime, original, args, kwargs)
        span = _start_child(
            runtime,
            span_name,
            parent_context=_active_parent(trace),
            session_id=trace.session_id,
            metadata={"trial_id": trace.trial_id, "trial_name": trace.trial_name},
        )
        try:
            result = await original(self, *args, **kwargs)
        except asyncio.CancelledError:
            _end_span(span, error_type="CancelledError")
            raise
        except Exception as exc:
            _end_span(span, error_type=type(exc).__name__)
            raise
        _end_span(span)
        return result

    cast(Any, wrapped).__evallab_laminar_wrapped__ = True
    setattr(cls, name, wrapped)


async def _observed_stop(
    trial: Any,
    trace: _TrialTrace,
    runtime: Any,
    original: Any,
    args: Any,
    kwargs: Any,
) -> Any:
    span = _start_child(
        runtime,
        "harbor.stop",
        parent_context=trace.root_context,
        session_id=trace.session_id,
        metadata={"trial_id": trace.trial_id, "trial_name": trace.trial_name},
    )

    def _observe() -> None:
        stop = _native_stop_from_trial(trial)
        _attach_native_stop(span, stop)
        _stash_native_stop(trace, stop)

    try:
        result = await original(trial, *args, **kwargs)
    except asyncio.CancelledError:
        _observe()
        _end_span(span, error_type="CancelledError")
        raise
    except Exception as exc:
        _observe()
        _end_span(span, error_type=type(exc).__name__)
        raise
    _observe()
    _end_span(span)
    return result


async def _observed_verifier(
    trial: Any,
    trace: _TrialTrace,
    runtime: Any,
    original: Any,
    args: Any,
    kwargs: Any,
) -> Any:
    span = _phase_for(trace, _VERIFIER_PHASE)
    _VERIFIER_PHASE.set(None)
    try:
        result = await original(trial, *args, **kwargs)
    except asyncio.CancelledError:
        _finish_phase(trace, span, error_type="CancelledError")
        raise
    except Exception as exc:
        _finish_phase(trace, span, error_type=type(exc).__name__)
        raise
    try:
        rewards = _safe_rewards(getattr(result, "rewards", None))
        if rewards is not None:
            with _TRACES_LOCK:
                live = _TRACES.get(trace.trial_id)
                if live is not None:
                    live.latest_rewards = rewards
            _set_span_output(span, {"rewards": rewards})
            _set_span_metadata(span, {"rewards": rewards})
            reward_span = _start_child(
                runtime,
                "harbor.reward",
                parent_context=_serialized(span) or trace.root_context,
                session_id=trace.session_id,
                metadata={"trial_id": trace.trial_id, "trial_name": trace.trial_name},
            )
            _set_span_output(reward_span, {"rewards": rewards})
            _end_span(reward_span)
    except Exception:
        pass
    finally:
        _finish_phase(trace, span)
    return result


def _wrap_egress_lock() -> None:
    try:
        from evallab.harbor_daytona import BoundedDaytonaEnvironment
    except Exception:
        return
    if getattr(
        BoundedDaytonaEnvironment._lock_egress,
        "__evallab_laminar_wrapped__",
        False,
    ):
        return
    original = BoundedDaytonaEnvironment._lock_egress

    @functools.wraps(original)
    async def wrapped(self: Any) -> None:
        context_id = getattr(self, "context_id", None)
        trace = _lookup_trial(context_id) if context_id is not None else None
        runtime = trace_runtime()
        if trace is None or trace.closed or not getattr(runtime, "enabled", False):
            await original(self)
            return
        span = _start_child(
            runtime,
            "harbor.egress_lock",
            parent_context=_active_parent(trace),
            session_id=trace.session_id,
            metadata={
                "trial_id": trace.trial_id,
                "mechanism": "daytona update_network_settings(network_block_all=True)",
            },
        )
        try:
            await original(self)
        except asyncio.CancelledError:
            # Remote state is ambiguous when cancelled; never fabricate it.
            _end_span(span, error_type="CancelledError")
            raise
        except Exception as exc:
            with contextlib.suppress(Exception):
                sandbox = getattr(self, "_sandbox", None)
                _set_span_metadata(
                    span,
                    {
                        "applied": False,
                        "network_block_all": getattr(sandbox, "network_block_all", None),
                    },
                )
            _end_span(span, error_type=type(exc).__name__)
            raise
        # Provider acknowledgement: the original already required the cached
        # ``network_block_all is True`` response. Read the cache only.
        with contextlib.suppress(Exception):
            sandbox = getattr(self, "_sandbox", None)
            observed = {
                "applied": True,
                "network_block_all": getattr(sandbox, "network_block_all", None),
                "sandbox_id": getattr(sandbox, "id", None),
            }
            _set_span_metadata(span, observed)
            with _TRACES_LOCK:
                live = _TRACES.get(trace.trial_id)
                if live is not None and not live.closed:
                    lock_metadata = {
                        "egress_lock_observed": "applied",
                        "network_block_all": observed["network_block_all"],
                    }
                    # Native spans inherit this snapshot later. Keep it in sync
                    # so delayed exports cannot restore a stale "pending" state.
                    live.metadata.update(lock_metadata)
                    with contextlib.suppress(Exception):
                        live.root.set_metadata(lock_metadata)
        _end_span(span)

    cast(Any, wrapped).__evallab_laminar_wrapped__ = True
    cast(Any, BoundedDaytonaEnvironment)._lock_egress = wrapped


def _wrap_trial_run() -> None:
    try:
        from harbor.trial.trial import Trial  # ty: ignore[unresolved-import]
    except Exception:
        return
    if getattr(Trial.run, "__evallab_laminar_wrapped__", False):
        return
    original = Trial.run

    @functools.wraps(original)
    async def wrapped(self: Any) -> Any:
        try:
            return await original(self)
        finally:
            # True run finally: after existing cleanup, even when END was
            # never emitted because result persistence failed. Never changes
            # the trial result or cancellation.
            try:
                trial_id = _trial_uuid_of(self)
                trace = _lookup_trial(trial_id) if trial_id is not None else None
                if trace is not None and not trace.closed:
                    rewards: Any = None
                    exception_type: str | None = None
                    native_stop: dict[str, Any] | None = None
                    with contextlib.suppress(Exception):
                        result = getattr(self, "_result", None) or getattr(self, "result", None)
                        if result is not None:
                            rewards = getattr(
                                getattr(result, "verifier_result", None),
                                "rewards",
                                None,
                            )
                            exc_info = getattr(result, "exception_info", None)
                            raw_type = getattr(exc_info, "exception_type", None)
                            exception_type = str(raw_type) if raw_type is not None else None
                            native_stop = _native_stop_from_result(result)
                    trial_dir: Path | None = None
                    with contextlib.suppress(Exception):
                        trial_dir = getattr(getattr(self, "paths", None), "trial_dir", None)
                    _close_trial_root(
                        trace.trial_id,
                        reason="run-finally",
                        rewards=rewards,
                        exception_type=exception_type,
                        trial_dir=trial_dir,
                        native_stop=native_stop,
                    )
            except Exception:
                pass

    cast(Any, wrapped).__evallab_laminar_wrapped__ = True
    cast(Any, Trial).run = wrapped


def _ensure_wrappers_installed() -> None:
    global _WRAPPERS_INSTALLED
    if _WRAPPERS_INSTALLED:
        return
    with contextlib.suppress(Exception):
        from harbor.trial.trial import Trial  # ty: ignore[unresolved-import]

        _wrap_trial_method(
            Trial,
            "_setup_agent_environment",
            span_name="harbor.sandbox_setup",
            kind="environment",
        )
        _wrap_trial_method(Trial, "_setup_agent", span_name="harbor.agent_setup", kind="setup")
        _wrap_trial_method(
            Trial,
            "_run_shared_verifier",
            span_name="harbor.verifier",
            kind="shared_verifier",
        )
        _wrap_trial_method(
            Trial,
            "_run_separate_verifier",
            span_name="harbor.verifier",
            kind="separate_verifier",
        )
        _wrap_trial_method(Trial, "_stop_agent_environment", span_name="harbor.stop", kind="stop")
    with contextlib.suppress(Exception):
        _wrap_trial_run()
    with contextlib.suppress(Exception):
        _wrap_egress_lock()
    _WRAPPERS_INSTALLED = True


# ---------------------------------------------------------------------------
# Job plugin (wired through the single existing CLI plugin)
# ---------------------------------------------------------------------------


class LaminarTrialPlugin:
    """Fail-open per-trial Laminar roots for mimoagent trials only."""

    def __init__(self) -> None:
        self._session_id: str | None = None

    async def on_job_start(self, job: Any) -> None:
        try:
            global _JOB_SESSION, _PARENT_METADATA
            _PARENT_METADATA = _load_parent_metadata()
            self._session_id = _resolve_session_from_job(job, _PARENT_METADATA)
            _JOB_SESSION = self._session_id
            _ensure_runtime()
            _ensure_wrappers_installed()
            from harbor.trial.hooks import TrialEvent  # ty: ignore[unresolved-import]

            job.add_hook(TrialEvent.START, self._on_trial_started)
            job.add_hook(TrialEvent.ENVIRONMENT_START, self._on_environment_started)
            job.add_hook(TrialEvent.AGENT_START, self._on_agent_started)
            job.add_hook(TrialEvent.AGENT_END, self._on_agent_ended)
            job.add_hook(TrialEvent.VERIFICATION_START, self._on_verification_started)
            job.add_hook(TrialEvent.END, self._on_trial_ended)
            job.add_hook(TrialEvent.CANCEL, self._on_trial_cancelled)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def on_job_end(self, _job_result: Any) -> None:
        try:
            runtime = trace_runtime()
            with contextlib.suppress(Exception):
                runtime.flush_async()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _on_trial_started(self, event: Any) -> None:
        try:
            if not _is_mimo_event(event):
                return
            session_id = (
                self._session_id
                or _JOB_SESSION
                or _safe_text(getattr(getattr(event, "config", None), "job_id", _UNKNOWN))
            )
            _open_trial_root(event, session_id=session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _on_environment_started(self, event: Any) -> None:
        try:
            trace = _lookup_trial(event.trial_id)
            if trace is not None and not trace.closed:
                _open_phase(trace, _ENVIRONMENT_PHASE, "harbor.sandbox_setup")
        except Exception:
            pass

    async def _on_agent_started(self, event: Any) -> None:
        try:
            trace = _lookup_trial(event.trial_id)
            if trace is not None and not trace.closed:
                _open_phase(trace, _AGENT_PHASE, "harbor.agent_run", span_type="TOOL")
        except Exception:
            pass

    async def _on_agent_ended(self, event: Any) -> None:
        try:
            trace = _lookup_trial(event.trial_id)
            if trace is None or trace.closed:
                return
            span = _phase_for(trace, _AGENT_PHASE)
            _AGENT_PHASE.set(None)
            stop = _native_stop_from_result(event.result)
            _attach_native_stop(span, stop)
            _stash_native_stop(trace, stop)
            status = stop.get("native_exit_status")
            error_type = (
                "CancelledError"
                if status == "HarborCancelled"
                else status if status in {"ModelQueryError", "InfraError"} else None
            )
            _finish_phase(trace, span, error_type=error_type)
        except Exception:
            pass

    async def _on_verification_started(self, event: Any) -> None:
        try:
            trace = _lookup_trial(event.trial_id)
            if trace is not None and not trace.closed:
                _open_phase(trace, _VERIFIER_PHASE, "harbor.verifier")
        except Exception:
            pass

    async def _on_trial_ended(self, event: Any) -> None:
        try:
            if not _is_mimo_event(event):
                return
            _close_trial_root_from_result(event, reason="end")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _on_trial_cancelled(self, event: Any) -> None:
        try:
            if not _is_mimo_event(event):
                return
            # CANCEL precedes recovery and shielded stop: annotate, do not close.
            _close_trial_root_from_result(event, reason="cancelled", close_on_cancel=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def _reset_for_tests() -> None:
    """Test-only reset of module globals (never used at runtime)."""
    global _RUNTIME, _JOB_SESSION, _PARENT_METADATA, _WRAPPERS_INSTALLED
    with _RUNTIME_LOCK:
        _RUNTIME = None
    _JOB_SESSION = None
    _PARENT_METADATA = {}
    _WRAPPERS_INSTALLED = False
    with _TRACES_LOCK:
        _TRACES.clear()
    _ENVIRONMENT_PHASE.set(None)
    _AGENT_PHASE.set(None)
    _VERIFIER_PHASE.set(None)
