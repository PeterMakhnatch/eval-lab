"""Secure structural projection for Goose ``llm_request.*.jsonl`` records."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.interpretation.trajectory_hydration import RedactionPolicy
from evallab.results import JobRecord, TrialRecord, sha256_file

_NAME = re.compile(r"^llm_request\.(\d+)\.jsonl$")
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_MISSING = re.compile(r"Missing credentials|Please pass an [`'\"]?api_key|OPENAI_API_KEY", re.I)
_INTERNAL = re.compile(r"litellm\.InternalServerError", re.I)
_RUNTIME = re.compile(r"runtime.{0,40}(?:error|failed)", re.I | re.S)
_USAGE = ("prompt_tokens", "completion_tokens", "cached_tokens")


class LlmRequestProjectionError(ValueError):
    pass


@dataclass(frozen=True)
class _Record:
    index: int
    path: str
    digest: str
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...]
    model: str | None
    timestamp: str | None
    usage_status: str
    tokens: tuple[int | None, int | None, int | None]

    @property
    def ordinal(self) -> int:
        return sum(row.get("role") == "assistant" for row in self.messages) + 1


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _bytes(value)
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _id(*parts: object) -> str:
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()


def _safe(value: str, label: str) -> str:
    if not _SAFE.fullmatch(value) or any(
        pattern.search(value) for pattern in RedactionPolicy().secret_patterns
    ):
        raise LlmRequestProjectionError(f"unsafe {label} in llm_request evidence")
    return value


def _usage(
    events: list[Mapping[str, Any]],
) -> tuple[str, tuple[int | None, int | None, int | None]]:
    found: Mapping[str, Any] | None = None
    for event in events:
        for candidate in (event.get("usage"), event.get("data")):
            if not isinstance(candidate, Mapping):
                continue
            nested = candidate.get("usage")
            if isinstance(nested, Mapping):
                found = nested
            elif any(key in candidate for key in _USAGE):
                found = candidate
    if found is None:
        return "unavailable", (None, None, None)
    values = tuple(found.get(key) for key in _USAGE)
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
        return "unavailable", (None, None, None)
    if not any(values):
        return "unavailable_zero", (None, None, None)
    return "reported", values  # type: ignore[return-value]


def _record(path: Path, trial: TrialRecord, index: int) -> _Record:
    events: list[Mapping[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise LlmRequestProjectionError(f"cannot read {path.name}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LlmRequestProjectionError(f"malformed {path.name} line {line_number}") from exc
        if not isinstance(row, Mapping):
            raise LlmRequestProjectionError(f"unsupported {path.name} line {line_number}")
        events.append(row)
    inputs = [row for row in events if isinstance(row.get("input"), Mapping)]
    if len(inputs) != 1:
        raise LlmRequestProjectionError(f"{path.name} must contain one input")
    request, raw = inputs[0], inputs[0]["input"]
    messages, tools = raw.get("messages"), raw.get("tools", [])
    if not isinstance(messages, list) or not all(isinstance(row, Mapping) for row in messages):
        raise LlmRequestProjectionError("unsupported input.messages")
    if not isinstance(tools, list) or not all(isinstance(row, Mapping) for row in tools):
        raise LlmRequestProjectionError("unsupported input.tools")
    if any(
        row.get("role") not in {"system", "developer", "user", "assistant", "tool"}
        for row in messages
    ):
        raise LlmRequestProjectionError("unsupported message role")
    model = raw.get("model")
    if model is None and isinstance(request.get("model_config"), Mapping):
        model = request["model_config"].get("model_name")
    model = _safe(model, "model") if isinstance(model, str) else None
    created = next(
        (
            data.get("created")
            for row in reversed(events)
            if isinstance((data := row.get("data")), Mapping) and data.get("created") is not None
        ),
        None,
    )
    timestamp = None
    if isinstance(created, int | float) and not isinstance(created, bool):
        with suppress(OSError, OverflowError, ValueError):
            timestamp = datetime.fromtimestamp(float(created), tz=UTC).isoformat()
    status, tokens = _usage(events)
    return _Record(
        index,
        path.relative_to(trial.path).as_posix(),
        f"sha256:{sha256_file(path)}",
        tuple(messages),
        tuple(tools),
        model,
        timestamp,
        status,
        tokens,
    )


def _records(trial: TrialRecord) -> tuple[_Record, ...]:
    root = trial.path / "agent"
    if not root.is_dir():
        return ()
    indexed: list[tuple[int, Path]] = []
    parents: set[Path] = set()
    for path in root.rglob("llm_request.*.jsonl"):
        match = _NAME.fullmatch(path.name)
        if match is None:
            raise LlmRequestProjectionError("unsupported llm_request filename")
        indexed.append((int(match.group(1)), path))
        parents.add(path.parent.resolve())
    if not indexed:
        return ()
    indices = [index for index, _path in indexed]
    if (
        len(parents) != 1
        or min(indices) != 0
        or len(indices) != len(set(indices))
        or len(indices) > 10
    ):
        raise LlmRequestProjectionError("unsupported llm_request ring layout")
    return tuple(_record(path, trial, index) for index, path in sorted(indexed))


def _tool_name(tool: Mapping[str, Any]) -> str:
    function = tool.get("function")
    name = function.get("name") if isinstance(function, Mapping) else tool.get("name")
    if not isinstance(name, str):
        raise LlmRequestProjectionError("offered tool has no name")
    return _safe(name, "tool name")


def _call(row: Mapping[str, Any]) -> tuple[str, str, Any]:
    call_id = row.get("id") or row.get("tool_call_id")
    function = row.get("function")
    if (
        not isinstance(call_id, str)
        or not isinstance(function, Mapping)
        or not isinstance(function.get("name"), str)
    ):
        raise LlmRequestProjectionError("unsupported tool call")
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise LlmRequestProjectionError("invalid tool arguments JSON") from exc
    return (
        _safe(call_id, "call id"),
        _safe(function["name"], "tool name"),
        arguments,
    )


def _error(value: Any) -> str | None:
    text = value if isinstance(value, str) else _bytes(value).decode(errors="replace")
    if _MISSING.search(text):
        return "missing_credentials"
    if _INTERNAL.search(text):
        return "litellm_internal_server_error"
    if _RUNTIME.search(text):
        return "runtime_status_error"
    if text.lstrip().lower().startswith(("error", "failed")):
        return "tool_error"
    return None


def project_llm_requests(job: JobRecord, trial: TrialRecord):
    """Project one request ring into existing trajectory mechanical facts."""
    from evallab.evidence.atif import (
        ObservationFact,
        StepFact,
        ToolCallFact,
        TrajectoryFact,
        TrialTrajectoryProjection,
    )

    records = _records(trial)
    if not records:
        return None
    latest = records[0]
    assistants = [row for row in latest.messages if row.get("role") == "assistant"]
    lower_bound = max(
        len(assistants) + 1,
        len(records),
        *(row.ordinal for row in records),
    )
    metadata: dict[int, _Record] = {}
    for row in records:
        metadata.setdefault(row.ordinal, row)
    results: dict[str, Any] = {}
    for row in latest.messages:
        if row.get("role") != "tool":
            continue
        call_id = row.get("tool_call_id")
        if not isinstance(call_id, str):
            raise LlmRequestProjectionError("tool result has no call id")
        call_id = _safe(call_id, "call id")
        if call_id in results:
            raise LlmRequestProjectionError("duplicate tool result")
        results[call_id] = row.get("content")

    document_id = _id(trial.id, latest.path, "goose-llm-request-v1")
    steps: list[StepFact] = []
    calls: list[ToolCallFact] = []
    observations: list[ObservationFact] = []
    seen: set[str] = set()
    sequence = 0
    first_fault: str | None = None
    for ordinal in range(1, lower_bound + 1):
        message = assistants[ordinal - 1] if ordinal <= len(assistants) else None
        raw_calls = message.get("tool_calls", []) if message else []
        raw_calls = [] if raw_calls is None else raw_calls
        if not isinstance(raw_calls, list) or not all(
            isinstance(row, Mapping) for row in raw_calls
        ):
            raise LlmRequestProjectionError("unsupported assistant tool_calls")
        observed = 0
        for raw_call in raw_calls:
            call_id, name, arguments = _call(raw_call)
            if call_id in seen:
                raise LlmRequestProjectionError("duplicate tool call")
            seen.add(call_id)
            classification = _error(results[call_id]) if call_id in results else None
            if first_fault is None and classification in {
                "missing_credentials",
                "litellm_internal_server_error",
                "runtime_status_error",
            }:
                first_fault = classification
            calls.append(
                ToolCallFact(
                    job_id=job.id,
                    trial_id=trial.id,
                    document_id=document_id,
                    source_path=latest.path,
                    source_sha256=latest.digest,
                    step_id=ordinal,
                    tool_call_id=call_id,
                    function_name=name,
                    arguments_sha256=_digest(arguments),
                    call_index=sequence,
                    result_error_flag=(classification is not None if call_id in results else None),
                )
            )
            sequence += 1
            if call_id in results:
                result = results[call_id]
                content = result.encode() if isinstance(result, str) else _bytes(result)
                observations.append(
                    ObservationFact(
                        job_id=job.id,
                        trial_id=trial.id,
                        document_id=document_id,
                        source_path=latest.path,
                        source_sha256=latest.digest,
                        step_id=ordinal,
                        observation_index=observed,
                        source_call_id=call_id,
                        content_size_bytes=len(content),
                        content_sha256=_digest(content),
                        subagent_ref_count=0,
                        subagent_refs_sha256=None,
                        command_exit_code=None,
                        error_classification=classification,
                    )
                )
                observed += 1
        record = metadata.get(ordinal)
        tokens = record.tokens if record else (None, None, None)
        steps.append(
            StepFact(
                job_id=job.id,
                trial_id=trial.id,
                document_id=document_id,
                source_path=latest.path,
                source_sha256=latest.digest,
                step_id=ordinal,
                source="llm_request",
                timestamp=record.timestamp if record else None,
                model_name=latest.model,
                is_copied_context=False,
                llm_call_count=1,
                prompt_tokens=tokens[0],
                completion_tokens=tokens[1],
                cached_tokens=tokens[2],
                cost_usd=None,
                tool_call_count=len(raw_calls),
                observation_count=observed,
                llm_source_path=record.path if record else None,
                llm_source_sha256=record.digest if record else None,
                llm_metadata_available=record is not None,
                usage_status=(record.usage_status if record else "unavailable_unknown_prefix"),
            )
        )

    offered = tuple(sorted({_tool_name(row) for row in latest.tools}))
    unknown_prefix = lower_bound > len(records)
    unavailable = {"cost_usd"}
    if any(row.usage_status != "reported" for row in records):
        unavailable.update(_USAGE)
    if unknown_prefix:
        unavailable.update({"timestamp", "source_path", "source_sha256"})
    trajectory = TrajectoryFact(
        job_id=job.id,
        trial_id=trial.id,
        document_id=document_id,
        source_path=latest.path,
        source_sha256=latest.digest,
        embedded_path=None,
        schema_version="GOOSE-LLM-REQUEST-v1",
        session_id=None,
        trajectory_id=document_id,
        validation_status="valid",
        validator="goose-llm-request-v1",
        validation_error=None,
        agent_name="goose",
        agent_version=None,
        model_name=latest.model,
        continued_trajectory_ref=None,
        step_count=len(steps),
        llm_call_count=lower_bound,
        prompt_tokens=None,
        completion_tokens=None,
        cached_tokens=None,
        cost_usd=None,
        capture_source="llm_request_ring",
        retained_request_count=len(records),
        inferred_total_call_lower_bound=lower_bound,
        assistant_turn_lower_bound=len(assistants),
        ring_buffer_truncated=unknown_prefix,
        unknown_prefix=unknown_prefix,
        per_call_metadata_complete=not unavailable,
        unavailable_call_metadata=tuple(sorted(unavailable)),
        retained_request_paths=tuple(row.path for row in records),
        retained_request_sha256=tuple(row.digest for row in records),
        tools_offered=offered,
        tools_offered_sha256=_digest(offered),
        harness_fault_signature=first_fault,
    )
    return TrialTrajectoryProjection(
        trajectories=(trajectory,),
        steps=tuple(steps),
        tool_calls=tuple(calls),
        observations=tuple(observations),
    )
