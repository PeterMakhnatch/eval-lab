"""Deterministic capture-authority and concordance contract at the ATIF boundary.

ATIF records direct tool calls issued by the agent runtime. Benchmark events
record the MCP service's observed calls. When an agent shells out to curl/HTTP
child processes (e.g. within bash), those calls appear in benchmark events but
not as direct ATIF tool calls. ATIF is not buggy in that case; it cannot observe
out-of-band child execution.

When an agent uses direct batch or range retrieval tools (such as
``get_context_chunks`` with explicit ``chunk_ids``), those direct tool calls
can be deterministically expanded into covered handles, representing valid batch
representation rather than indirect child execution.

This module establishes a deterministic capture-authority and concordance contract:
- Identifies direct vs indirect child execution.
- Distinguishes valid expanded batch/range calls from indirect capture loss.
- Compares expanded retrieval handles against benchmark retrieval handles while
  separately comparing non-retrieval call sequences/counts (e.g. list, mutate).
- Binds benchmark-event authority when benchmark events exist.
- Reason-codes incomplete trajectory capture so downstream trajectory-only ordering
  analysis can refuse while benchmark-event analysis remains admissible.
- Never synthesizes fake ATIF tool calls from verifier or benchmark evidence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

JsonObject = dict[str, Any]

CaptureAuthorityName = Literal["benchmark_events", "atif_trajectory", "unresolved", "none"]
CaptureConcordanceName = Literal[
    "concordant",
    "discordant_indirect_execution",
    "discordant_batch_unexpandable",
    "discordant_tool_omission",
    "no_trajectory",
    "no_benchmark_events",
]


class CaptureAuthority(StrEnum):
    BENCHMARK_EVENTS = "benchmark_events"
    ATIF_TRAJECTORY = "atif_trajectory"
    UNRESOLVED = "unresolved"
    NONE = "none"


class CaptureConcordanceStatus(StrEnum):
    CONCORDANT = "concordant"
    DISCORDANT_INDIRECT_EXECUTION = "discordant_indirect_execution"
    DISCORDANT_BATCH_UNEXPANDABLE = "discordant_batch_unexpandable"
    DISCORDANT_TOOL_OMISSION = "discordant_tool_omission"
    NO_TRAJECTORY = "no_trajectory"
    NO_BENCHMARK_EVENTS = "no_benchmark_events"


class CaptureReasonCode(StrEnum):
    CONCORDANT_DIRECT_CAPTURE = "CONCORDANT_DIRECT_CAPTURE"
    CONCORDANT_BATCH_CAPTURE = "CONCORDANT_BATCH_CAPTURE"
    INDIRECT_CHILD_EXECUTION = "INDIRECT_CHILD_EXECUTION"
    BATCH_TOOL_REPRESENTATION = "BATCH_TOOL_REPRESENTATION"
    TOOL_CALL_OMISSION = "TOOL_CALL_OMISSION"
    MISSING_BENCHMARK_EVENTS = "MISSING_BENCHMARK_EVENTS"
    MISSING_ATIF_TRAJECTORY = "MISSING_ATIF_TRAJECTORY"
    BENCHMARK_EVENT_SCHEMA_INVALID = "BENCHMARK_EVENT_SCHEMA_INVALID"


_SHELL_FUNCTION_NAMES = frozenset(
    {
        "bash",
        "shell",
        "sh",
        "zsh",
        "fish",
        "cmd",
        "command",
        "powershell",
        "pwsh",
        "terminal",
        "exec",
        "execute",
        "run_terminal_cmd",
        "run_command",
        "bash_tool",
        "shell_command",
        "bash_command",
    }
)

_BATCH_TOOL_FUNCTION_NAMES = frozenset(
    {
        "get_context_chunks",
        "read_context_chunks",
        "read_chunks",
        "get_chunks",
        "batch_read",
        "batch_get_context_chunk",
        "batch_get_context_chunks",
        "range_batch",
    }
)

_RETRIEVAL_TOOL_NAMES = frozenset(
    {
        "get_context_chunk",
        "get_context_chunks",
        "read_chunk",
        "read_chunks",
        "read_context_chunk",
        "read_context_chunks",
        "batch_read",
        "batch_get_context_chunk",
        "batch_get_context_chunks",
        "range_batch",
        "get_chunks",
    }
)

_TOOL_REQUEST_EVENT_TYPES = frozenset(
    {
        "mcp_call",
        "tool_call",
        "request",
        "tool_invoked",
        "tool_call_requested",
        "tool_call_success",
        "tool_call_error",
        "tool_call_execution",
        "read_chunk",
        "execute_mutation",
        "tools/call",
        "call_tool",
    }
)

_TRAJECTORY_CANDIDATES = ("agent/trajectory.json", "trajectory.json", "agent/trajectory.jsonl")
_EVENTS_CANDIDATES = (
    "benchmark-events.jsonl",
    "benchmark_events.jsonl",
    "events.jsonl",
    "artifacts/app/output/benchmark-events.jsonl",
    "artifacts/app/output/benchmark_events.jsonl",
    "artifacts/app/output/events.jsonl",
)


@dataclass(frozen=True)
class CaptureAuthorityAssessment:
    trial_id: str
    atif_tool_call_count: int | None
    benchmark_event_count: int | None
    benchmark_tool_call_count: int | None
    has_indirect_child_execution: bool
    has_batch_tool_representation: bool
    is_concordant: bool
    retrieval_authority: CaptureAuthorityName
    concordance_status: CaptureConcordanceName
    reason_codes: tuple[str, ...]
    trajectory_ordering_admissible: bool
    benchmark_events_admissible: bool
    disposition_summary: str
    assessment_digest: str


def extract_direct_atif_tool_calls(payload: Mapping[str, Any] | None) -> list[JsonObject]:
    """Return tool calls recorded on ATIF steps.

    Never synthesize calls from verifier output or benchmark events.
    """
    if not isinstance(payload, Mapping):
        return []
    recorded: list[JsonObject] = []
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return recorded
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        calls = step.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if isinstance(call, dict):
                recorded.append(call)
    return recorded


def extract_direct_atif_handles(payload: Mapping[str, Any] | None) -> list[str]:
    """Extract context retrieval handles requested directly in ATIF tool calls.

    Supports both singular handle calls (``get_context_chunk``) and deterministically
    expanded batch/range calls (``get_context_chunks``). Never invents handles.
    """
    tool_calls = extract_direct_atif_tool_calls(payload)
    handles: list[str] = []
    for call in tool_calls:
        expanded, _ = expand_tool_call_handles(call)
        handles.extend(expanded)
    return handles


def expand_tool_call_handles(
    call: Mapping[str, Any],
    observation: Mapping[str, Any] | None = None,
) -> tuple[list[str], bool]:
    """Deterministically expand handles from a tool call and optional observation.

    Parses stringified JSON arguments when present.
    Returns ``(handles, is_batch)`` where ``is_batch`` indicates whether the tool
    call represents a batch/range operation.
    """
    func_name = _function_name(call)
    norm_name = _normalize_tool_name(func_name)
    is_batch = norm_name in _BATCH_TOOL_FUNCTION_NAMES

    raw_args = call.get("arguments")
    if raw_args is None and isinstance(call.get("payload"), Mapping):
        raw_args = call["payload"].get("arguments")

    args: Mapping[str, Any] = {}
    if isinstance(raw_args, str):
        stripped = raw_args.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed_args = json.loads(stripped)
                if isinstance(parsed_args, Mapping):
                    args = parsed_args
            except json.JSONDecodeError:
                pass
    elif isinstance(raw_args, Mapping):
        args = raw_args

    handles: list[str] = []

    # Check for plural chunk_ids list in arguments (e.g. get_context_chunks)
    chunk_ids = args.get("chunk_ids") or args.get("handles") or args.get("ids")
    if isinstance(chunk_ids, list):
        is_batch = True
        for item in chunk_ids:
            if isinstance(item, str) and item.strip():
                handles.append(item.strip())
        return handles, is_batch

    # Check for singular chunk_id in arguments
    chunk_id = args.get("chunk_id") or args.get("handle") or args.get("id")
    if isinstance(chunk_id, str) and chunk_id.strip():
        handles.append(chunk_id.strip())
        return handles, is_batch

    # Check observation if arguments lacked explicit handles
    if isinstance(observation, Mapping):
        results = observation.get("results")
        if isinstance(results, list):
            for res in results:
                if isinstance(res, Mapping):
                    content = res.get("content")
                    if isinstance(content, str) and content.startswith("{"):
                        try:
                            parsed = json.loads(content)
                            if isinstance(parsed, dict):
                                val = parsed.get("value", parsed)
                                if isinstance(val, dict):
                                    if isinstance(val.get("chunk_ids"), list):
                                        is_batch = True
                                        for cid in val["chunk_ids"]:
                                            if isinstance(cid, str) and cid.strip():
                                                handles.append(cid.strip())
                                    elif isinstance(val.get("chunk_id"), str):
                                        handles.append(val["chunk_id"].strip())
                        except json.JSONDecodeError:
                            pass

    return handles, is_batch


def evaluate_capture_authority_from_dir(
    trial_dir: Path | str,
    *,
    trial_id: str | None = None,
) -> CaptureAuthorityAssessment:
    """Evaluate capture authority and concordance for a trial directory."""
    path = Path(trial_dir)
    tid = trial_id or path.name
    trajectory_path = _first_regular_file(path, _TRAJECTORY_CANDIDATES)
    events_path = _first_regular_file(path, _EVENTS_CANDIDATES)

    atif_calls: Sequence[Any] | None
    if trajectory_path is None:
        atif_calls = None
    else:
        payload, atif_ok = _load_trajectory_payload(trajectory_path)
        atif_calls = extract_direct_atif_tool_calls(payload) if atif_ok else None

    events: Sequence[Any] | None
    events_invalid = False
    if events_path is None:
        events = None
    else:
        parsed, events_invalid = _load_benchmark_event_records(events_path)
        events = None if events_invalid else parsed

    return assess_capture_concordance(
        atif_calls,
        events,
        trial_id=tid,
        benchmark_events_invalid=events_invalid,
    )


def assess_capture_concordance(
    atif_tool_calls: Sequence[Any] | None,
    benchmark_events: Sequence[Any] | None,
    trial_id: str = "",
    *,
    benchmark_events_invalid: bool = False,
) -> CaptureAuthorityAssessment:
    """Assess capture concordance between direct ATIF tool calls and benchmark events.

    Deterministic:
    - Identifies direct calls vs indirect shell/HTTP execution.
    - Partitions retrieval handles and non-retrieval tool call sequences.
    - Distinguishes valid batch expansion from unexpandable batch representation.
    - Binds retrieval_authority to benchmark_events when valid events exist.
    - Reason-codes indirect execution and sets trajectory_ordering_admissible=False.
    """
    atif_missing = atif_tool_calls is None
    events_missing = benchmark_events is None and not benchmark_events_invalid
    atif_seq = () if atif_missing else tuple(atif_tool_calls)
    event_seq = () if benchmark_events is None else tuple(benchmark_events)

    atif_count = None if atif_missing else len(atif_seq)
    event_count = None if (events_missing or benchmark_events_invalid) else len(event_seq)
    event_calls = (
        ()
        if (events_missing or benchmark_events_invalid)
        else tuple(_benchmark_tool_calls(event_seq))
    )
    event_tool_count = None if (events_missing or benchmark_events_invalid) else len(event_calls)

    reason_codes: list[str] = []
    if benchmark_events_invalid:
        reason_codes.append(CaptureReasonCode.BENCHMARK_EVENT_SCHEMA_INVALID)
    if events_missing:
        reason_codes.append(CaptureReasonCode.MISSING_BENCHMARK_EVENTS)
    if atif_missing:
        reason_codes.append(CaptureReasonCode.MISSING_ATIF_TRAJECTORY)

    events_admissible = not events_missing and not benchmark_events_invalid
    if events_admissible:
        retrieval: CaptureAuthorityName = CaptureAuthority.BENCHMARK_EVENTS
    elif benchmark_events_invalid:
        retrieval = CaptureAuthority.UNRESOLVED
    elif not atif_missing:
        retrieval = CaptureAuthority.ATIF_TRAJECTORY
    else:
        retrieval = CaptureAuthority.NONE

    indirect = False
    batch_representation = False
    batch_concordant = False
    batch_unexpandable = False
    omission = False

    if events_admissible and not atif_missing:
        # Partition ATIF direct calls into retrieval vs non-retrieval vs shell
        atif_retrieval_handles: list[str] = []
        atif_non_retrieval_names: list[str] = []
        atif_shell_names: list[str] = []
        has_any_batch = False
        has_unexpandable_batch = False

        for call in atif_seq:
            name = _function_name(call)
            if _is_shell_tool(name):
                atif_shell_names.append(name)
            elif _is_retrieval_tool(name):
                handles, is_batch = expand_tool_call_handles(call)
                if is_batch:
                    has_any_batch = True
                    if not handles:
                        has_unexpandable_batch = True
                    else:
                        atif_retrieval_handles.extend(handles)
                elif handles:
                    atif_retrieval_handles.extend(handles)
                else:
                    # Singular retrieval tool without handle
                    atif_retrieval_handles.append(_normalize_tool_name(name))
            else:
                atif_non_retrieval_names.append(name)

        if has_any_batch:
            batch_representation = True

        # Partition benchmark events into retrieval vs non-retrieval
        bench_retrieval_handles: list[str] = []
        bench_non_retrieval_names: list[str] = []

        for call in event_calls:
            name = _function_name(call)
            if _is_retrieval_tool(name):
                handles, _ = expand_tool_call_handles(call)
                if handles:
                    bench_retrieval_handles.extend(handles)
                else:
                    bench_retrieval_handles.append(_normalize_tool_name(name))
            else:
                bench_non_retrieval_names.append(name)

        shell_calls_present = bool(atif_shell_names)
        atif_has_no_direct_tools = not atif_retrieval_handles and not atif_non_retrieval_names

        # Evaluate concordance across retrieval handles and non-retrieval tool sequences
        if event_tool_count and event_tool_count > 0:
            if (
                atif_has_no_direct_tools
                and shell_calls_present
                or shell_calls_present
                and len(bench_retrieval_handles) > len(atif_retrieval_handles)
                or not batch_representation
                and (event_tool_count or 0)
                > (len(atif_retrieval_handles) + len(atif_non_retrieval_names))
            ):
                indirect = True
            elif batch_representation:
                if has_unexpandable_batch:
                    batch_unexpandable = True
                elif len(atif_retrieval_handles) >= len(
                    bench_retrieval_handles
                ) and _sequences_concordant(atif_non_retrieval_names, bench_non_retrieval_names):
                    batch_concordant = True
                elif shell_calls_present and len(bench_retrieval_handles) > len(
                    atif_retrieval_handles
                ):
                    indirect = True
                else:
                    batch_unexpandable = True
            elif _sequences_concordant(atif_non_retrieval_names, bench_non_retrieval_names) and (
                len(atif_retrieval_handles) == len(bench_retrieval_handles)
                and _retrieval_handles_concordant(atif_retrieval_handles, bench_retrieval_handles)
            ):
                pass
            elif (len(atif_retrieval_handles) + len(atif_non_retrieval_names)) > (
                event_tool_count or 0
            ):
                omission = True
            else:
                indirect = True
        elif (len(atif_retrieval_handles) + len(atif_non_retrieval_names)) > 0:
            omission = True

    if events_missing or benchmark_events_invalid:
        status: CaptureConcordanceName = CaptureConcordanceStatus.NO_BENCHMARK_EVENTS
    elif atif_missing:
        status = CaptureConcordanceStatus.NO_TRAJECTORY
    elif indirect:
        status = CaptureConcordanceStatus.DISCORDANT_INDIRECT_EXECUTION
        reason_codes.append(CaptureReasonCode.INDIRECT_CHILD_EXECUTION)
    elif batch_unexpandable:
        status = CaptureConcordanceStatus.DISCORDANT_BATCH_UNEXPANDABLE
        reason_codes.append(CaptureReasonCode.BATCH_TOOL_REPRESENTATION)
    elif omission:
        status = CaptureConcordanceStatus.DISCORDANT_TOOL_OMISSION
        reason_codes.append(CaptureReasonCode.TOOL_CALL_OMISSION)
    elif batch_concordant:
        status = CaptureConcordanceStatus.CONCORDANT
        reason_codes.append(CaptureReasonCode.CONCORDANT_BATCH_CAPTURE)
    else:
        status = CaptureConcordanceStatus.CONCORDANT
        reason_codes.append(CaptureReasonCode.CONCORDANT_DIRECT_CAPTURE)

    is_concordant = status == CaptureConcordanceStatus.CONCORDANT
    has_indirect = status == CaptureConcordanceStatus.DISCORDANT_INDIRECT_EXECUTION
    trajectory_admissible = (
        is_concordant and not has_indirect and not atif_missing and not batch_unexpandable
    )
    summary = _disposition_summary(
        status=status,
        atif_count=atif_count,
        event_tool_count=event_tool_count,
        events_invalid=benchmark_events_invalid,
        has_batch=batch_representation,
    )
    assessment = CaptureAuthorityAssessment(
        trial_id=trial_id,
        atif_tool_call_count=atif_count,
        benchmark_event_count=event_count,
        benchmark_tool_call_count=event_tool_count,
        has_indirect_child_execution=has_indirect,
        has_batch_tool_representation=batch_representation,
        is_concordant=is_concordant,
        retrieval_authority=retrieval,
        concordance_status=status,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        trajectory_ordering_admissible=trajectory_admissible,
        benchmark_events_admissible=events_admissible,
        disposition_summary=summary,
        assessment_digest="",
    )
    return replace(assessment, assessment_digest=_assessment_digest(assessment))


def _is_retrieval_tool(name: str) -> bool:
    norm = _normalize_tool_name(name)
    return norm in _RETRIEVAL_TOOL_NAMES or norm in _BATCH_TOOL_FUNCTION_NAMES


def _retrieval_handles_concordant(
    atif_handles: Sequence[str], bench_handles: Sequence[str]
) -> bool:
    if len(atif_handles) != len(bench_handles):
        return False
    return all(
        a == b or _tool_names_match(a, b) for a, b in zip(atif_handles, bench_handles, strict=True)
    )


def _sequences_concordant(atif_names: Sequence[str], event_names: Sequence[str]) -> bool:
    if len(atif_names) != len(event_names):
        return False
    return all(
        _tool_names_match(atif_name, event_name)
        for atif_name, event_name in zip(atif_names, event_names, strict=True)
    )


def _disposition_summary(
    *,
    status: str,
    atif_count: int | None,
    event_tool_count: int | None,
    events_invalid: bool,
    has_batch: bool = False,
) -> str:
    if events_invalid:
        return "Benchmark events are present but schema-invalid; retrieval authority is unresolved."
    if status == CaptureConcordanceStatus.CONCORDANT:
        count = atif_count if atif_count is not None else 0
        batch_note = " (with deterministic batch expansion)" if has_batch else ""
        return (
            f"ATIF direct tool calls match benchmark events in sequence and content "
            f"({count} calls{batch_note}); trajectory ordering is admissible."
        )
    if status == CaptureConcordanceStatus.DISCORDANT_INDIRECT_EXECUTION:
        return (
            "Benchmark events contain tool calls executed via child processes, shell, "
            f"or HTTP not captured as direct ATIF tool calls "
            f"(benchmark={event_tool_count}, atif={atif_count}); "
            "trajectory-only ordering analysis is inadmissible."
        )
    if status == CaptureConcordanceStatus.DISCORDANT_BATCH_UNEXPANDABLE:
        return (
            "ATIF records batch/range tool calls whose covered handles cannot be "
            f"deterministically expanded to reconcile benchmark events "
            f"(benchmark={event_tool_count}, atif={atif_count}); "
            "trajectory ordering is inadmissible."
        )
    if status == CaptureConcordanceStatus.DISCORDANT_TOOL_OMISSION:
        return (
            "ATIF records direct tool calls that are missing from benchmark events "
            f"(atif={atif_count}, benchmark={event_tool_count})."
        )
    if status == CaptureConcordanceStatus.NO_TRAJECTORY:
        return "ATIF trajectory is absent; retrieval authority remains benchmark events."
    return "Benchmark events are absent; capture concordance cannot be assessed."


def _assessment_digest(assessment: CaptureAuthorityAssessment) -> str:
    facts = {key: value for key, value in asdict(assessment).items() if key != "assessment_digest"}
    canonical = json.dumps(facts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{sha256(canonical.encode()).hexdigest()}"


def _function_name(call: Any) -> str:
    if isinstance(call, str):
        return call
    if isinstance(call, Mapping):
        for key in ("function_name", "tool_name", "name", "tool", "method"):
            value = call.get(key)
            if isinstance(value, str) and value:
                return value
        payload = call.get("payload")
        if isinstance(payload, Mapping):
            return _function_name(payload)
        return ""
    for attr in ("function_name", "tool_name", "name"):
        value = getattr(call, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _normalize_tool_name(name: str) -> str:
    normalized = name.strip().lower()
    for prefix in ("memory_mcp_", "mcp_", "functions."):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
    if "." in normalized:
        normalized = normalized.rsplit(".", 1)[-1]
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    return normalized


def _is_shell_tool(name: str) -> bool:
    raw = name.strip().lower()
    return raw in _SHELL_FUNCTION_NAMES or _normalize_tool_name(name) in _SHELL_FUNCTION_NAMES


def _tool_names_match(left: str, right: str) -> bool:
    if not left or not right:
        return not left and not right
    a = _normalize_tool_name(left)
    b = _normalize_tool_name(right)
    return a == b or a.endswith(b) or b.endswith(a)


def _benchmark_tool_calls(events: Sequence[Any]) -> list[Any]:
    if not events:
        return []
    first = events[0]
    if _looks_like_correlated_call(first):
        return list(events)
    calls: list[Any] = []
    seen_ids: set[str] = set()
    unnamed = 0
    for item in events:
        event_type, payload, name, call_id = _event_fields(item)
        if event_type in _TOOL_REQUEST_EVENT_TYPES or name or _looks_like_call_mapping(item):
            if not call_id:
                unnamed += 1
                call_id = f"call_{unnamed}"
            if call_id in seen_ids:
                continue
            seen_ids.add(call_id)
            calls.append({"function_name": name, "tool_call_id": call_id, "payload": payload})
    return calls


def _looks_like_correlated_call(item: Any) -> bool:
    return (
        hasattr(item, "tool_name") and hasattr(item, "call_id") and hasattr(item, "request_event")
    )


def _looks_like_call_mapping(item: Any) -> bool:
    if not isinstance(item, Mapping):
        return bool(getattr(item, "function_name", None) or getattr(item, "tool_name", None))
    return any(
        isinstance(item.get(key), str) and item.get(key)
        for key in ("function_name", "tool_name", "name", "tool", "method")
    )


def _event_fields(item: Any) -> tuple[str, Mapping[str, Any], str, str]:
    if isinstance(item, Mapping):
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else item
        event_type = str(item.get("event_type") or "")
        name = _function_name(item)
        if not name and isinstance(payload, Mapping):
            name = _function_name(payload)
        call_id = ""
        for key in ("tool_call_id", "call_id", "id", "request_id", "event_ordinal", "event_index"):
            value = item.get(key)
            if value is None and isinstance(payload, Mapping):
                value = payload.get(key)
            if value is not None and str(value):
                call_id = str(value)
                break
        return event_type, payload if isinstance(payload, Mapping) else {}, name, call_id
    event_type = str(getattr(item, "event_type", "") or "")
    payload_obj = getattr(item, "payload", {})
    payload = payload_obj if isinstance(payload_obj, Mapping) else {}
    name = _function_name(item)
    call_id = ""
    getter = getattr(item, "get_tool_call_id", None)
    if callable(getter):
        value = getter()
        if value is not None:
            call_id = str(value)
    if not call_id:
        for attr in ("call_id", "tool_call_id", "event_ordinal", "event_index"):
            val = getattr(item, attr, None)
            if val is not None and str(val):
                call_id = str(val)
                break
    return event_type, payload, name, call_id


def _first_regular_file(root: Path, candidates: Sequence[str]) -> Path | None:
    if not root.is_dir():
        return None
    for relative in candidates:
        candidate = root / relative
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def _load_trajectory_payload(path: Path) -> tuple[JsonObject | None, bool]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, False
    if path.suffix == ".jsonl":
        steps: list[JsonObject] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                return None, False
            if isinstance(payload, dict):
                if "steps" in payload and isinstance(payload["steps"], list):
                    return payload, True
                steps.append(payload)
        if steps:
            return {"steps": steps}, True
        return None, False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, False
    return (payload, True) if isinstance(payload, dict) else (None, False)


def _load_benchmark_event_records(path: Path) -> tuple[list[JsonObject], bool]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], True
    records: list[JsonObject] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return [], True
        if not isinstance(payload, dict):
            return [], True
        if "event_index" not in payload and "event_ordinal" not in payload:
            return [], True
        if "event_type" not in payload:
            return [], True
        index = payload.get("event_index", payload.get("event_ordinal"))
        if not isinstance(index, int) or isinstance(index, bool):
            return [], True
        records.append(payload)
        if line_number and records[-1] is payload:
            continue
    return records, False
