"""Run report: one comprehensive, cited account of a Harbor trial or job.

``build_run_report(trial_dir)`` reads a trial's ``result.json`` and its ATIF
trajectory (following continuation segments and subagent references) and
returns a JSON-ready dictionary with schema ``evallab.run_report/v1``.
``render_run_report_markdown`` renders exactly that dictionary, so a person and
a model read the same facts. ``build_job_report`` rolls a job's trials up.

Rules the report keeps:

- Every total names its source. A missing input is ``None`` with a reason in
  ``data_quality``, never a silent zero.
- Tool status is tri-state. ``ok``/``error`` need a harness signal (exit code,
  error flag, envelope, code-mode script status, rejection message); strong
  output-text signals mark ``error`` with ``evidence: "output_text"``; anything
  else is ``unknown``.
- A *revisit* is an action whose exact signature (tool plus normalized
  command/arguments) already occurred in the run. An *exact revisit* also got
  the same normalized result back: the agent was at the same spot and nothing
  had changed. Polls (empty keystrokes, wait tools) are excluded.
- Excerpts pass through ``sanitize_excerpt`` (hidden verifier paths and
  secret-like tokens are redacted).
- Deterministic: no wall clock, network, or model calls.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from evallab.interpretation.price_table import estimate_cost_usd, lookup_price
from evallab.traj import (
    CONTROL_AGENTS,
    TrajectoryError,
    TrajectoryOutline,
    _chain_action_steps,
    _resolve_chain_segments,
    outline_trajectory,
    resolve_trial_target,
)
from evallab.trajectory_error_taxonomy import classify_step_error, split_envelope
from evallab.trajectory_ir import _extract_reasoning_tokens
from evallab.trial_diagnosis import sanitize_excerpt

RUN_REPORT_SCHEMA = "evallab.run_report/v1"
JOB_REPORT_SCHEMA = "evallab.job_report/v1"
DEFAULT_TIMELINE_LIMIT = 60
TOP_N = 5
TARGET_CHARS = 160
WINDOW_COUNT = 10
MIN_STEPS_FOR_WINDOWS = 20
MIN_REPEATED_OUTPUT_CHARS = 32
PASS_REWARD = 1.0

# Argument keys that carry the command text, compared case-insensitively.
_COMMAND_KEYS = ("command", "cmd", "keystrokes", "commandline", "script", "code", "input")
_SHELL_KEYS = frozenset({"command", "cmd", "keystrokes", "commandline"})
_DELEGATION_TOOLS = frozenset(
    {"task", "agent", "spawn_agent", "delegate", "subagent", "dispatch_agent", "new_task"}
)
_POLL_TOOLS = frozenset({"wait", "sleep", "poll", "wait_agent"})
_CODE_MODE_CALL_RE = re.compile(r"\btools\.([A-Za-z_]\w*)\s*\(")
_PATCH_FILE_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: ([^\n\\\"']+)")
_CODE_MODE_STATUS_RE = re.compile(r"^\s*Script (completed|failed)\b")
_WALL_TIME_RE = re.compile(r"Wall time:?\s*[\d.]+\s*(?:seconds|s)\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_EXIT_PREFIX_RE = re.compile(r"^\s*exit (-?\d+)\b")
_STRONG_ERROR_TEXT_RE = re.compile(
    r"Traceback \(most recent call last\)|command not found|No such file or directory"
    r"|Permission denied|SyntaxError:|ModuleNotFoundError:"
)
_CACHE_WRITE_KEYS = (
    "cache_write_input_tokens",
    "cache_creation_input_tokens",
    "cache_write_tokens",
)
_HARBOR_PHASES = ("environment_setup", "agent_setup", "agent_execution", "verifier")
_COMPACTION_MIN_PROMPT = 8_000
_COMPACTION_DROP_RATIO = 0.6
_CONTROL_REASON = "control agent (oracle/nop) does not emit a trajectory"


# --------------------------------------------------------------------------- #
# Normalized views
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Output:
    text: str
    exit_code: int | None
    failed: bool | None
    result_type: str | None
    result_status: str | None
    signal: str | None = None


@dataclass(frozen=True)
class _Call:
    tool: str
    wrapper: str | None
    target: str
    key: str
    inner_tools: tuple[str, ...]
    shell: bool = False


@dataclass(frozen=True)
class _Action:
    index: int
    step: int
    timestamp: datetime | None
    tool: str
    wrapper: str | None
    target: str
    signature: str
    is_poll: bool
    output_chars: int | None
    output_hash: str | None
    status: str
    evidence: str | None
    category: str
    excerpt: str | None
    inner_tools: tuple[str, ...]
    shell: bool = False
    call_count: int = 1


@dataclass(frozen=True)
class _Step:
    step: int
    native_step_id: int | None
    segment: int
    source: str
    timestamp: datetime | None
    model: str | None
    message: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    is_sidechain: bool
    agent_id: str | None
    context_event: str | None
    subagent_refs: tuple[dict[str, Any], ...]
    actions: tuple[_Action, ...]
    notices: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 3)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _clip(text: str | None, limit: int = TARGET_CHARS) -> str | None:
    if text is None:
        return None
    return sanitize_excerpt(text, limit)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return json.dumps(content, sort_keys=True, default=str)


def _code_mode_parts(text: str) -> list[str] | None:
    """Codex code-mode results are a Python-repr list of ``input_text`` parts."""
    stripped = text.lstrip()
    if not stripped.startswith("[{'type': 'input_text'"):
        return None
    try:
        parts = ast.literal_eval(stripped)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    if not isinstance(parts, list):
        return None
    return [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]


def _normalized_output(text: str) -> str:
    return _WS_RE.sub(" ", _WALL_TIME_RE.sub("", text)).strip()


def _target_of(args: Any) -> str:
    """Command text of a call (first non-blank command field) or its canonical arguments.

    A call whose only command fields are blank (terminal keystrokes ``""``) has an
    empty target: it is a wait, not an action.
    """
    if isinstance(args, str):
        return args.strip()
    if isinstance(args, dict):
        lowered = {str(key).lower(): value for key, value in args.items()}
        present = [lowered[key] for key in _COMMAND_KEYS if isinstance(lowered.get(key), str)]
        for value in present:
            if value.strip():
                return value.strip()
        if present:
            return ""
        return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return "" if args is None else json.dumps(args, sort_keys=True, default=str)


_WRAPPER_PROGRAMS = frozenset({"sudo", "doas", "env", "nice", "nohup", "time", "timeout", "stdbuf"})


_PROGRAM_NAME_RE = re.compile(r"^[A-Za-z_][\w.+-]*$")


def _program_of(command: str) -> str | None:
    """First program of a shell command, skipping ``cd x &&``, env assignments, and wrappers.

    A segment that only assigns a command substitution (``tmp=$(mktemp)``) names no
    program of its own; a head that is not a plausible program name is skipped.
    """
    for segment in re.split(r"&&|\|\||;|\n", command):
        tokens = segment.strip().split()
        if tokens and re.match(r"^[A-Za-z_]\w*=(\$\(|`)", tokens[0]):
            continue
        while tokens and (
            ("=" in tokens[0] and not tokens[0].startswith(("-", "/", ".")))
            or tokens[0] in _WRAPPER_PROGRAMS
            or (tokens[0].startswith("-") and len(tokens) > 1)
            or tokens[0].replace(".", "").isdigit()
        ):
            tokens = tokens[1:]
        if not tokens or tokens[0] in {"cd", "set", "export", "source", "."}:
            continue
        head = tokens[0].rsplit("/", 1)[-1]
        if _PROGRAM_NAME_RE.match(head):
            return head[:40]
    return None


def _is_shell_args(args: dict[str, Any]) -> bool:
    """Arguments that carry a shell command line (not code or structured fields)."""
    return any(
        str(key).lower() in _SHELL_KEYS and isinstance(value, str) for key, value in args.items()
    )


# --------------------------------------------------------------------------- #
# Calls, outputs, and status
# --------------------------------------------------------------------------- #


def _unwrap_code_mode(name: str, args: Any) -> _Call | None:
    """Codex code-mode wraps real tools in JavaScript: ``tools.exec_command({...})``."""
    if name != "exec" or not isinstance(args, dict):
        return None
    source = args.get("input")
    if not isinstance(source, str):
        return None
    matches = list(_CODE_MODE_CALL_RE.finditer(source))
    if not matches:
        return None
    decoder = json.JSONDecoder()
    inner: list[tuple[str, str | None]] = []
    shell = False
    for match in matches:
        start = match.end()
        while start < len(source) and source[start].isspace():
            start += 1
        target: str | None = None
        if source.startswith("{", start):
            try:
                obj, _ = decoder.raw_decode(source, start)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                target = _target_of(obj)
                shell = shell or _is_shell_args(obj)
        inner.append((match.group(1), target))
    if len(inner) == 1 and inner[0][1] is not None:
        display = inner[0][1] or ""
        key = display
    else:
        files = list(dict.fromkeys(f.strip() for f in _PATCH_FILE_RE.findall(source)))
        pieces = [t if t is not None else tool for tool, t in inner]
        display = f"files: {', '.join(files)}" if files else " ; ".join(pieces)
        key = _WS_RE.sub(" ", source).strip()
    return _Call(
        tool=inner[0][0],
        wrapper=name,
        target=display,
        key=key,
        inner_tools=tuple(tool for tool, _ in inner),
        shell=shell and len(inner) == 1,
    )


def _call_from_raw(raw_call: dict[str, Any]) -> _Call:
    name = str(raw_call.get("function_name") or "unknown")
    args = raw_call.get("arguments")
    unwrapped = _unwrap_code_mode(name, args)
    if unwrapped is not None:
        return unwrapped
    target = _target_of(args)
    return _Call(
        tool=name, wrapper=None, target=target, key=target, inner_tools=(),
        shell=isinstance(args, dict) and _is_shell_args(args),
    )


_PAYLOAD_OK = frozenset({"ok", "success", "succeeded", "completed"})
_PAYLOAD_ERROR = frozenset({"error", "failed", "failure"})


def _payload_failed(text: str) -> bool | None:
    """Status of a structured tool payload (MCP ``isError``, ``{"status": ...}``).

    A transport-level success that wraps an error value (``{"status": "ok",
    "value": {"error": "not_found"}}``) is a failed call for the agent.
    """
    stripped = text.strip()
    if not stripped.startswith("{") or len(stripped) > 1_000_000:
        return None
    try:
        payload = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("isError", "is_error"):
        if isinstance(payload.get(key), bool):
            return payload[key]
    value = payload.get("value")
    wrapped_error = isinstance(value, dict) and bool(value.get("error"))
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in _PAYLOAD_OK | _PAYLOAD_ERROR:
        return status.lower() in _PAYLOAD_ERROR or wrapped_error
    if isinstance(payload.get("ok"), bool):
        return not payload["ok"] or wrapped_error
    if payload.get("error") or wrapped_error:
        return True
    return None


def _envelope_note(raw_text: str) -> str | None:
    try:
        payload = json.loads(raw_text)
    except ValueError:
        return None
    note = payload.get("exception_info") if isinstance(payload, dict) else None
    return str(note) if note else None


def _decode_result(result: dict[str, Any], call_extra: dict[str, Any]) -> _Output:
    """Decode one observation result, keeping the strongest status signal and its channel."""
    text = _content_text(result.get("content"))
    extra = _dict(result.get("extra"))
    exit_code: int | None = None
    for key in ("exit_code", "returncode", "return_code"):
        exit_code = _int(extra.get(key))
        if exit_code is not None:
            break
    signal = "exit_code" if exit_code is not None else None
    raw_text = text
    envelope_code, envelope_text = split_envelope(text)
    if envelope_code is not None:
        text = envelope_text
        if exit_code is None:
            exit_code, signal = envelope_code, "envelope"
        if envelope_code == -1 and not text.strip() and _envelope_note(raw_text):
            # The harness intercepted the action (for example mini-swe-agent's submit
            # sentinel): nothing ran, so there is no success or failure to report.
            note = _envelope_note(raw_text) or ""
            return _Output(note, None, None, None, None, "not_executed")
    if exit_code is None:
        # REPL-style harnesses lead the output with ``exit N``.
        prefix = _EXIT_PREFIX_RE.match(text)
        if prefix:
            exit_code, signal = int(prefix.group(1)), "exit_prefix"
    failed: bool | None = None if exit_code is None else exit_code != 0
    parts = _code_mode_parts(text)
    if parts is not None:
        status = _CODE_MODE_STATUS_RE.match(parts[0]) if parts else None
        if status:
            parts = parts[1:]
            if failed is None:
                failed, signal = status.group(1) == "failed", "script_status"
        # The script can complete while a tool inside it returned an error payload.
        if failed is not True and any(_payload_failed(part) for part in parts):
            failed, signal = True, "result_payload"
        text = "\n".join(parts)
    for flags in (extra, call_extra):
        for key in ("tool_result_is_error", "is_error"):
            if flags.get(key) is True:
                failed, signal = True, "error_flag"
            elif flags.get(key) is False and failed is None:
                failed, signal = False, "error_flag"
    result_type = str(result.get("type") or "").lower() or None
    result_status = str(result.get("status") or "").lower() or None
    if result_type in {"error", "tool_error"} or result_status in {"error", "failed"}:
        failed, signal = True, "error_flag"
    if failed is None:
        payload = _payload_failed(text)
        if payload is not None:
            failed, signal = payload, "result_payload"
    return _Output(text, exit_code, failed, result_type, result_status, signal)


def _merge_outputs(outputs: Sequence[_Output], call_extras: Sequence[dict[str, Any]]) -> _Output:
    """One observation for several calls: the decisive output names the code and channel.

    Any failure fails the batch; otherwise any success makes it ok; otherwise unknown.
    A per-call error flag fails the batch even when the shared observation is silent.
    """
    decisive = next((o for o in outputs if o.failed is True), None) or next(
        (o for o in outputs if o.failed is False), None
    )
    flagged = any(
        extra.get(key) is True for extra in call_extras for key in ("tool_result_is_error", "is_error")
    )
    return _Output(
        text="\n".join(o.text for o in outputs),
        exit_code=decisive.exit_code if decisive else None,
        failed=True if flagged else (decisive.failed if decisive else None),
        result_type=decisive.result_type if decisive else None,
        result_status=decisive.result_status if decisive else None,
        signal="error_flag" if flagged and not (decisive and decisive.failed) else (
            decisive.signal if decisive else None
        ),
    )


def _status(call: _Call, output: _Output | None) -> tuple[str, str | None, str, str | None]:
    """Return (status, evidence, error_category, excerpt); evidence names the status channel."""
    if output is None:
        return "unknown", "no_result", "none", None
    if output.signal == "not_executed":
        return "unknown", "not_executed", "not_executed", _clip(output.text, 120)
    # Only a real exit code can make a call an expected probe miss (grep exiting 1);
    # an explicit error flag without a code stays an error.
    classification = classify_step_error(
        tool_name=call.tool,
        tool_command=call.target,
        exit_code=output.exit_code,
        output_content=output.text,
        result_type=output.result_type,
        result_status=output.result_status
        or ("failed" if output.failed and output.exit_code is None else None),
    )
    if classification.is_expected_probe:
        return "ok", output.signal, "expected_probe_miss", None
    if output.failed is True or classification.is_error:
        return (
            "error",
            output.signal if output.failed else "harness_rejection",
            classification.category.value,
            _clip(output.text or classification.error_message or "", 240),
        )
    if output.failed is False:
        return "ok", output.signal, "none", None
    strong = _STRONG_ERROR_TEXT_RE.search(output.text)
    if strong:
        start = max(0, strong.start() - 80)
        return "error", "output_text", "inferred_from_output", _clip(output.text[start:], 240)
    return "unknown", None, "none", None


def _make_action(
    index: int,
    step: int,
    timestamp: datetime | None,
    call: _Call,
    output: _Output | None,
    call_count: int = 1,
) -> _Action:
    status, evidence, category, excerpt = _status(call, output)
    normalized = _normalized_output(output.text) if output is not None else None
    output_hash = _digest(normalized) if normalized is not None else None
    # Polls are wait tools and blank terminal/command input, never no-argument calls.
    is_poll = call.tool.lower() in _POLL_TOOLS or (call.shell and not call.key.strip())
    return _Action(
        index=index,
        step=step,
        timestamp=timestamp,
        tool=call.tool,
        wrapper=call.wrapper,
        target=call.target,
        signature=_digest(f"{call.tool}\0{_WS_RE.sub(' ', call.key).strip()}"),
        is_poll=is_poll,
        output_chars=len(output.text) if output is not None else None,
        output_hash=output_hash,
        status=status,
        evidence=evidence,
        category=category,
        excerpt=excerpt,
        inner_tools=call.inner_tools,
        shell=call.shell,
        call_count=call_count,
    )


def _observation_results(raw_step: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    observation = raw_step.get("observation")
    if isinstance(observation, dict) and isinstance(observation.get("results"), list):
        results.extend(r for r in observation["results"] if isinstance(r, dict))
    if isinstance(raw_step.get("observation_results"), list):
        results.extend(r for r in raw_step["observation_results"] if isinstance(r, dict))
    return results


def _step_actions(
    raw_step: dict[str, Any], step: int, timestamp: datetime | None, start_index: int
) -> tuple[list[_Action], list[str]]:
    """Pair a step's tool calls with its results; return (actions, unpaired harness notices)."""
    raw_calls = [c for c in raw_step.get("tool_calls") or [] if isinstance(c, dict)]
    if not raw_calls:
        return [], []
    calls = [_call_from_raw(c) for c in raw_calls]
    call_extras = [_dict(c.get("extra")) for c in raw_calls]
    results = _observation_results(raw_step)
    by_id = {r["source_call_id"]: r for r in results if isinstance(r.get("source_call_id"), str)}
    call_ids = [c.get("tool_call_id") for c in raw_calls]
    paired: list[dict[str, Any] | None]
    if by_id and all(isinstance(cid, str) for cid in call_ids):
        paired = [by_id.get(str(cid)) for cid in call_ids]
        extras = [r for r in results if r.get("source_call_id") not in set(call_ids)]
    elif len(results) >= len(calls):
        # Positional pairing; trailing results are harness notices (for example a
        # length-limit warning appended after the tool outputs).
        paired = [result for result in results[: len(calls)]]
        extras = results[len(calls) :]
    else:
        # Fewer observations than calls (terminal harnesses send several keystroke
        # batches and read one screen): the step is the action.
        merged = (
            _merge_outputs([_decode_result(r, {}) for r in results], call_extras) if results else None
        )
        joined = _Call(
            tool=calls[0].tool if len({c.tool for c in calls}) == 1 else "+".join(
                dict.fromkeys(c.tool for c in calls)
            ),
            wrapper=calls[0].wrapper,
            target=" ; ".join(c.target for c in calls if c.target),
            key="\n".join(c.key for c in calls),
            inner_tools=tuple(t for c in calls for t in c.inner_tools),
            shell=any(c.shell for c in calls),
        )
        return [_make_action(start_index, step, timestamp, joined, merged, len(calls))], []
    actions = [
        _make_action(
            start_index + offset,
            step,
            timestamp,
            call,
            _decode_result(result, call_extras[offset]) if result is not None else None,
        )
        for offset, (call, result) in enumerate(zip(calls, paired, strict=True))
    ]
    return actions, [_content_text(r.get("content")) for r in extras]


def _cache_write(metrics: dict[str, Any]) -> int | None:
    extra = _dict(metrics.get("extra"))
    for key in _CACHE_WRITE_KEYS:
        value = _int(extra.get(key))
        if value is not None:
            return value
    return None


def _build_steps(
    positioned: Sequence[tuple[int, Any]],
) -> list[_Step]:
    steps: list[_Step] = []
    action_index = 0
    # Number only well-formed steps so ordinals stay contiguous.
    well_formed = [(segment, raw) for segment, raw in positioned if isinstance(raw, dict)]
    for ordinal, (segment, raw) in enumerate(well_formed, start=1):
        timestamp = _parse_ts(raw.get("timestamp"))
        metrics = _dict(raw.get("metrics"))
        extra = _dict(raw.get("extra"))
        actions, notices = _step_actions(raw, ordinal, timestamp, action_index)
        action_index += len(actions)
        refs: list[dict[str, Any]] = []
        for result in _observation_results(raw):
            refs_value = result.get("subagent_trajectory_ref")
            for ref in [refs_value] if isinstance(refs_value, dict) else refs_value or []:
                if isinstance(ref, dict):
                    refs.append(ref)
        context = _dict(extra.get("context_management"))
        steps.append(
            _Step(
                step=ordinal,
                native_step_id=_int(raw.get("step_id")),
                segment=segment,
                source=str(raw.get("source") or "agent"),
                timestamp=timestamp,
                model=raw.get("model_name") if isinstance(raw.get("model_name"), str) else None,
                message=_content_text(raw.get("message")),
                prompt_tokens=_int(metrics.get("prompt_tokens")),
                completion_tokens=_int(metrics.get("completion_tokens")),
                cached_tokens=_int(metrics.get("cached_tokens")),
                reasoning_tokens=_extract_reasoning_tokens(raw, metrics) if metrics else None,
                cache_write_tokens=_cache_write(metrics),
                cost_usd=_float(metrics.get("cost_usd")),
                is_sidechain=extra.get("is_sidechain") is True,
                agent_id=extra.get("agent_id") if isinstance(extra.get("agent_id"), str) else None,
                context_event=str(context.get("type")) if context.get("type") else None,
                subagent_refs=tuple(refs),
                actions=tuple(actions),
                notices=tuple(notices),
            )
        )
    return steps


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


def _phase_timing(result: dict[str, Any]) -> tuple[dict[str, Any], datetime | None]:
    started = _parse_ts(result.get("started_at"))
    finished = _parse_ts(result.get("finished_at"))
    phases: dict[str, Any] = {}
    known = 0.0
    for name in _HARBOR_PHASES:
        span = _dict(result.get(name))
        start = _parse_ts(span.get("started_at"))
        end = _parse_ts(span.get("finished_at"))
        seconds = _seconds(start, end)
        if seconds is not None:
            known += seconds
        phases[name] = {
            "seconds": seconds,
            "starts_at_offset_seconds": _seconds(started, start),
        }
    total = _seconds(started, finished)
    return {
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "total_seconds": total,
        "phases": phases,
        "unaccounted_seconds": round(max(total - known, 0.0), 3) if total is not None else None,
        "phases_overlap": total is not None and known > total + 1.0,
    }, started


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "median": None, "p90": None, "max": None, "total": None}
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]
    return {
        "count": len(ordered),
        "median": round(median(ordered), 3),
        "p90": round(p90, 3),
        "max": round(ordered[-1], 3),
        "total": round(sum(ordered), 3),
    }


def _action_preview(actions: Sequence[_Action]) -> str | None:
    if not actions:
        return None
    first = actions[0]
    more = f" (+{len(actions) - 1} more)" if len(actions) > 1 else ""
    target = _clip(first.target, 100) if first.target else "(wait, no input)"
    return f"{first.tool}: {target}{more}"


def _timing(result: dict[str, Any], steps: Sequence[_Step]) -> tuple[dict[str, Any], datetime | None]:
    section, trial_start = _phase_timing(result)
    agent_start = _parse_ts(_dict(result.get("agent_execution")).get("started_at"))
    stamped = [s for s in steps if s.timestamp is not None]
    origin = agent_start or (stamped[0].timestamp if stamped else None) or trial_start
    # Gaps between consecutive agent steps: the model's turn plus the tool time of the
    # previous action. Time spent before the first agent step is reported separately.
    agent_steps = [s for s in stamped if s.source == "agent"]
    gaps: list[tuple[float, _Step]] = []
    for previous, step in zip(agent_steps, agent_steps[1:], strict=False):
        if previous.timestamp and step.timestamp:
            gaps.append(((step.timestamp - previous.timestamp).total_seconds(), step))
    section["first_agent_step_offset_seconds"] = (
        _seconds(origin, agent_steps[0].timestamp) if agent_steps else None
    )
    section["offset_origin"] = "agent_execution.started_at" if agent_start else (
        "first_step_timestamp" if stamped else "trial.started_at"
    )
    section["steps_with_timestamps"] = len(stamped)
    section["agent_span_seconds"] = (
        _seconds(stamped[0].timestamp, stamped[-1].timestamp) if len(stamped) > 1 else None
    )
    section["step_gap_seconds"] = _distribution([g for g, _ in gaps if g >= 0])
    section["slowest_steps"] = [
        {
            "step": step.step,
            "seconds": round(gap, 3),
            "action": _action_preview(step.actions),
        }
        for gap, step in sorted(gaps, key=lambda item: (-item[0], item[1].step))[:TOP_N]
    ]
    return section, origin


def _sum(values: Iterable[int | float | None]) -> int | float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _trial_model(result: dict[str, Any]) -> str | None:
    """Resolve the trial model id for price-table matching.

    Same precedence as ``_identity`` for the sources visible here
    (``result.json`` only): ``agent_info.model_info.name`` first, then
    ``config.agent.model_name``. Returns the raw id; normalization to a
    table key happens in ``price_table.lookup_price``.
    """
    agent_info = _dict(result.get("agent_info"))
    model_info = _dict(agent_info.get("model_info"))
    agent_cfg = _dict(_dict(result.get("config")).get("agent"))
    model = model_info.get("name") or agent_cfg.get("model_name")
    return str(model) if isinstance(model, str) and model.strip() else None


def _tokens_and_cost(
    result: dict[str, Any], terminal: dict[str, Any], steps: Sequence[_Step]
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    agent_result = _dict(result.get("agent_result"))
    final = _dict(terminal.get("final_metrics"))
    final_extra = _dict(final.get("extra"))
    llm_steps = [s for s in steps if s.source == "agent"]
    metered = [s for s in llm_steps if s.prompt_tokens is not None or s.completion_tokens is not None]
    step_sums = {
        "input": _sum(s.prompt_tokens for s in steps),
        "cached_input": _sum(s.cached_tokens for s in steps),
        "output": _sum(s.completion_tokens for s in steps),
        "reasoning": _sum(s.reasoning_tokens for s in steps),
        "cache_write": _sum(s.cache_write_tokens for s in steps),
        "cost_usd": _sum(s.cost_usd for s in steps),
    }
    declared = {
        "result_json": {
            "input": _int(agent_result.get("n_input_tokens")),
            "cached_input": _int(agent_result.get("n_cache_tokens")),
            "output": _int(agent_result.get("n_output_tokens")),
            "cost_usd": _float(agent_result.get("cost_usd")),
        },
        "trajectory_final_metrics": {
            "input": _int(final.get("total_prompt_tokens")),
            "cached_input": _int(final.get("total_cached_tokens")),
            "output": _int(final.get("total_completion_tokens")),
            "cost_usd": _float(final.get("total_cost_usd")),
        },
        "step_sum": step_sums,
    }
    warnings: list[str] = []

    def pick(field: str) -> tuple[Any, str | None]:
        for source in ("result_json", "trajectory_final_metrics", "step_sum"):
            value = declared[source].get(field)
            if value is not None:
                return value, source
        return None, None

    chosen: dict[str, Any] = {}
    sources: dict[str, str | None] = {}
    for field in ("input", "cached_input", "output"):
        chosen[field], sources[field] = pick(field)
    reasoning_total = _first(
        _int(final_extra.get("total_reasoning_tokens")), _int(final_extra.get("reasoning_output_tokens"))
    )
    chosen["reasoning"] = reasoning_total if reasoning_total is not None else step_sums["reasoning"]
    sources["reasoning"] = (
        "trajectory_final_metrics" if reasoning_total is not None
        else ("step_sum" if step_sums["reasoning"] is not None else None)
    )
    chosen["cache_write"] = step_sums["cache_write"]
    uncached = (
        chosen["input"] - chosen["cached_input"]
        if chosen["input"] is not None and chosen["cached_input"] is not None
        else None
    )
    total = (
        chosen["input"] + chosen["output"]
        if chosen["input"] is not None and chosen["output"] is not None
        else None
    )
    for field in ("input", "output"):
        a = declared["result_json"][field]
        b = declared["trajectory_final_metrics"][field]
        if a is not None and b is not None and max(a, b) > 0 and abs(a - b) / max(a, b) > 0.01:
            warnings.append(
                f"{field} tokens disagree: result.json {a:,} vs trajectory final_metrics {b:,}"
            )
    prompts = [(s.step, s.prompt_tokens) for s in steps if s.prompt_tokens is not None]
    peak = max(prompts, key=lambda item: item[1]) if prompts else None
    by_step = sorted(
        (s for s in steps if s.prompt_tokens is not None or s.completion_tokens is not None),
        key=lambda s: (-(s.cost_usd or 0.0), -((s.prompt_tokens or 0) + (s.completion_tokens or 0)), s.step),
    )
    tokens = {
        "input": chosen["input"],
        "cached_input": chosen["cached_input"],
        "uncached_input": uncached,
        "output": chosen["output"],
        "reasoning": chosen["reasoning"],
        "cache_write": chosen["cache_write"],
        "total": total,
        "sources": sources,
        "declared": declared,
        "llm_steps": len(llm_steps),
        "llm_steps_with_usage": len(metered),
        "context": {
            "first_prompt_tokens": prompts[0][1] if prompts else None,
            "peak_prompt_tokens": peak[1] if peak else None,
            "peak_step": peak[0] if peak else None,
            "last_prompt_tokens": prompts[-1][1] if prompts else None,
        },
        "top_steps": [
            {
                "step": s.step,
                "input": s.prompt_tokens,
                "output": s.completion_tokens,
                "cost_usd": s.cost_usd,
                "action": _action_preview(s.actions) or (_clip(s.message, 100) if s.message.strip() else None),
            }
            for s in by_step[:TOP_N]
        ],
    }
    if metered and len(metered) < len(llm_steps):
        warnings.append(
            f"token usage recorded on {len(metered)} of {len(llm_steps)} agent steps"
        )
    for field in ("input", "output"):
        total_value, step_value = chosen[field], step_sums[field]
        if total_value and step_value is not None and sources[field] != "step_sum":
            gap = total_value - step_value
            if abs(gap) > 0.05 * total_value:
                warnings.append(
                    f"{gap:,} {field} tokens ({gap / total_value:.1%}) of the run total are not "
                    "attributed to any step; per-step and per-window figures undercount"
                    if gap > 0
                    else f"per-step {field} tokens exceed the run total by {-gap:,}"
                )
    if not metered and chosen["input"] is None:
        warnings.append("no token usage recorded anywhere for this run")

    cost_value, cost_source = None, None
    for source in ("result_json", "trajectory_final_metrics"):
        value = declared[source]["cost_usd"]
        if value is not None:
            cost_value, cost_source = value, source
            break
    steps_with_cost = [s for s in llm_steps if s.cost_usd is not None]
    lower_bound = False
    if cost_value is None and steps_with_cost:
        cost_value, cost_source = step_sums["cost_usd"], "step_sum"
        lower_bound = len(steps_with_cost) < len(llm_steps)
    price_entry = None
    price_model: str | None = None
    if cost_value is None:
        # No harness-reported cost anywhere: an estimate from the pinned
        # provider price table is the only figure allowed, and only for a
        # model with a verified list price. It is never added to harness
        # spend because there is none on this path by construction.
        price_model = _trial_model(result)
        price_entry = lookup_price(price_model)
        if price_entry is None:
            warnings.append(
                "cost unavailable: the harness recorded no cost for this run; "
                f"model {price_model!r} has no pinned price-table entry"
            )
        else:
            estimate = estimate_cost_usd(
                chosen["input"], chosen["cached_input"], chosen["output"], price_entry
            )
            if estimate is None:
                warnings.append(
                    "cost unavailable: the harness recorded no cost for this run; "
                    f"model {price_model!r} matches price-table key "
                    f"{price_entry.key!r} but input/output token counts are missing"
                )
            else:
                cost_value, cost_source = estimate, "price_table_estimate"
    estimated = cost_source == "price_table_estimate" and price_entry is not None
    cost = {
        "total_usd": round(cost_value, 6) if cost_value is not None else None,
        "source": cost_source,
        "harness_cost_source": final_extra.get("cost_source")
        if isinstance(final_extra.get("cost_source"), str)
        else None,
        "is_lower_bound": lower_bound,
        "steps_with_cost": len(steps_with_cost),
        "per_1k_output_tokens_usd": (
            round(cost_value / chosen["output"] * 1000, 6)
            if cost_value is not None and chosen["output"]
            else None
        ),
        "note": (
            "Estimate from pinned provider list prices; not a metered charge or provider invoice."
            if estimated
            else "Harness-reported native ledger; not a provider invoice."
        ),
        "price_table": (
            {
                "model": price_model,
                "matched_key": price_entry.key,
                "source_url": price_entry.source_url,
                "retrieved_on": price_entry.retrieved_on,
                "usd_per_mtok": {
                    "input": price_entry.input_usd_per_mtok,
                    "cached_input": price_entry.cached_input_usd_per_mtok,
                    "output": price_entry.output_usd_per_mtok,
                },
            }
            if estimated
            else None
        ),
    }
    return tokens, cost, warnings


def _tools(actions: Sequence[_Action]) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    programs: Counter[str] = Counter()
    wrappers: Counter[str] = Counter()
    for action in actions:
        row = rows.setdefault(
            action.tool,
            {
                "tool": action.tool,
                "calls": 0,
                "ok": 0,
                "errors": 0,
                "unknown": 0,
                "output_chars": 0,
                "first_step": action.step,
                "last_step": action.step,
            },
        )
        # A batch of calls sharing one observation fails once, not once per call.
        row["calls"] += action.call_count
        if action.status == "error":
            row["errors"] += 1
        elif action.status == "ok":
            row["ok"] += action.call_count
        row["unknown"] = row["calls"] - row["ok"] - row["errors"]
        row["output_chars"] += action.output_chars or 0
        row["last_step"] = action.step
        if action.wrapper:
            wrappers[action.wrapper] += 1
        program = _program_of(action.target) if action.shell and action.target else None
        if program:
            programs[program] += 1
    table = sorted(rows.values(), key=lambda r: (-r["calls"], r["tool"]))
    for row in table:
        judged = row["ok"] + row["errors"]
        row["judged"] = judged
        row["error_rate"] = round(row["errors"] / judged, 4) if judged else None
    return {
        "total_calls": sum(a.call_count for a in actions),
        "actions": len(actions),
        "distinct_tools": len(rows),
        "polls": sum(1 for a in actions if a.is_poll),
        "by_tool": table,
        "top_programs": [{"program": p, "calls": n} for p, n in programs.most_common(10)],
        "wrapped_calls": dict(wrappers),
    }


def _window_of(step: int, total_steps: int) -> int:
    return min(WINDOW_COUNT - 1, (step - 1) * WINDOW_COUNT // max(total_steps, 1))


def _repeated_outputs(group: Sequence[_Action]) -> int:
    """Occurrences whose result equals an earlier occurrence's result (exact revisits)."""
    seen: set[str] = set()
    count = 0
    for action in group:
        if action.output_hash in seen:
            count += 1
        elif action.output_hash:
            seen.add(action.output_hash)
    return count


def _revisits(
    actions: Sequence[_Action], outline: TrajectoryOutline | None, total_steps: int
) -> dict[str, Any]:
    counted = [a for a in actions if not a.is_poll]
    seen: set[str] = set()
    states: set[tuple[str, str]] = set()
    outputs: dict[str, str] = {}
    groups: dict[str, list[_Action]] = defaultdict(list)
    repeats = returns = consecutive = exact = same_output = 0
    previous: str | None = None
    run: list[int] = []
    longest: list[int] = []
    revisit_steps: list[int] = []
    for action in counted:
        groups[action.signature].append(action)
        state = (action.signature, action.output_hash) if action.output_hash else None
        # Cross-action matches need substantial output: many unrelated commands print nothing.
        substantial = action.output_hash is not None and (
            (action.output_chars or 0) >= MIN_REPEATED_OUTPUT_CHARS
        )
        other_producer = substantial and outputs.get(action.output_hash or "") not in (
            None,
            action.signature,
        )
        if action.signature in seen:
            repeats += 1
            revisit_steps.append(action.step)
            if previous == action.signature:
                consecutive += 1
            else:
                returns += 1
            if state in states:
                exact += 1
            elif other_producer:
                same_output += 1
        else:
            seen.add(action.signature)
            if other_producer:
                same_output += 1
        if state is not None:
            states.add(state)
        if substantial and action.output_hash:
            outputs.setdefault(action.output_hash, action.signature)
        run = [*run, action.step] if previous == action.signature else [action.step]
        if len(run) > len(longest):
            longest = run
        previous = action.signature
    error_signatures: Counter[str] = Counter(
        f"{a.tool}\0{a.category}\0{_normalized_output(a.excerpt or '')[:80]}"
        for a in counted
        if a.status == "error"
    )
    repeated_groups = sorted(
        (g for g in groups.values() if len(g) > 1),
        key=lambda g: (-len(g), g[0].step),
    )
    loop = outline.loop_suspicion if outline is not None else None
    section: dict[str, Any] = {
        "actions_considered": len(counted),
        "distinct_actions": len(groups),
        "repeated_actions": repeats,
        "repeat_rate": round(repeats / len(counted), 4) if counted else None,
        "returns_to_earlier_action": returns,
        "consecutive_repeats": consecutive,
        "exact_revisits": exact,
        "same_result_from_different_action": same_output,
        "repeated_errors": sum(n - 1 for n in error_signatures.values() if n > 1),
        "longest_identical_run": {
            "length": len(longest),
            "steps": list(dict.fromkeys(longest))[:20],
        },
        "most_repeated": [
            {
                "tool": g[0].tool,
                "target": _clip(g[0].target),
                "count": len(g),
                "steps": list(dict.fromkeys(a.step for a in g))[:20],
                "identical_results": _repeated_outputs(g),
            }
            for g in repeated_groups[:TOP_N]
        ],
        "loop_suspicion": {
            "detected": loop.detected,
            "score": loop.score,
            "reasons": list(loop.reasons),
        }
        if loop is not None
        else None,
    }
    if total_steps >= MIN_STEPS_FOR_WINDOWS:
        windows = [0] * WINDOW_COUNT
        for step in revisit_steps:
            windows[_window_of(step, total_steps)] += 1
        section["repeats_by_window"] = windows
    return section


def _load_child(
    ref: dict[str, Any],
    embedded: dict[str, dict[str, Any]],
    segment_dir: Path,
    trial_root: Path,
) -> tuple[dict[str, Any] | None, str]:
    trajectory_id = ref.get("trajectory_id")
    if isinstance(trajectory_id, str) and trajectory_id in embedded:
        return embedded[trajectory_id], f"embedded:{trajectory_id}"
    path_value = ref.get("trajectory_path")
    if not isinstance(path_value, str) or not path_value:
        return None, "unresolved"
    candidate = Path(path_value)
    candidate = (
        trial_root / candidate.as_posix().lstrip("/") if candidate.is_absolute()
        else segment_dir / candidate
    )
    resolved = candidate.resolve()
    if resolved != trial_root and trial_root not in resolved.parents:
        return None, "escaping"
    if not resolved.is_file():
        return None, f"missing:{path_value}"
    try:
        loaded = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, f"unparseable:{path_value}"
    return (loaded if isinstance(loaded, dict) else None), str(resolved.relative_to(trial_root))


def _first(*values: Any) -> Any:
    """First value that is not None (0 and 0.0 are real measurements)."""
    return next((v for v in values if v is not None), None)


def _child_summary(doc: dict[str, Any]) -> dict[str, Any]:
    raw_steps = [s for s in doc.get("steps") or [] if isinstance(s, dict)]
    stamps = [t for t in (_parse_ts(s.get("timestamp")) for s in raw_steps) if t is not None]
    metrics = [_dict(s.get("metrics")) for s in raw_steps]
    final = _dict(doc.get("final_metrics"))
    agent = _dict(doc.get("agent"))
    children = [c for c in doc.get("subagent_trajectories") or [] if isinstance(c, dict)]
    return {
        "model": agent.get("model_name"),
        "steps": len(raw_steps),
        "tool_calls": sum(
            len(s["tool_calls"]) for s in raw_steps if isinstance(s.get("tool_calls"), list)
        ),
        "input_tokens": _first(
            _int(final.get("total_prompt_tokens")), _sum(_int(m.get("prompt_tokens")) for m in metrics)
        ),
        "output_tokens": _first(
            _int(final.get("total_completion_tokens")),
            _sum(_int(m.get("completion_tokens")) for m in metrics),
        ),
        "cost_usd": _first(
            _float(final.get("total_cost_usd")), _sum(_float(m.get("cost_usd")) for m in metrics)
        ),
        "started_at": _iso(min(stamps)) if stamps else None,
        "ended_at": _iso(max(stamps)) if stamps else None,
        "duration_seconds": _seconds(min(stamps), max(stamps)) if len(stamps) > 1 else None,
        "nested_subagents": len(children),
    }


def _subagents(
    steps: Sequence[_Step],
    segments: Sequence[tuple[Path, dict[str, Any], str]],
    trial_dir: Path,
    origin: datetime | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    trial_root = trial_dir.resolve()
    embedded: dict[str, dict[str, Any]] = {}
    for _, data, _ in segments:
        for child in data.get("subagent_trajectories") or []:
            if isinstance(child, dict) and isinstance(child.get("trajectory_id"), str):
                embedded.setdefault(child["trajectory_id"], child)
    items: list[dict[str, Any]] = []
    context_events: list[dict[str, Any]] = []
    referenced: set[str] = set()
    for step in steps:
        segment_dir = segments[step.segment][0].parent
        for ref in step.subagent_refs:
            doc, location = _load_child(ref, embedded, segment_dir, trial_root)
            if isinstance(ref.get("trajectory_id"), str):
                referenced.add(ref["trajectory_id"])
            label = str(ref.get("trajectory_path") or ref.get("trajectory_id") or "")
            kind = "context_summarization" if "summar" in label.lower() else "delegated"
            item = {
                "id": ref.get("trajectory_id") or ref.get("session_id") or label,
                "kind": kind,
                "evidence": "atif_subagent_ref",
                "location": location,
                "spawned_at_step": step.step,
                "spawned_at_offset_seconds": _seconds(origin, step.timestamp),
                **(_child_summary(doc) if doc is not None else {"captured": False}),
            }
            items.append(item)
            if kind == "context_summarization":
                context_events.append(
                    {"step": step.step, "kind": "summarization_subagent", "detail": label}
                )
    for trajectory_id, child in embedded.items():
        if trajectory_id not in referenced:
            items.append(
                {
                    "id": trajectory_id,
                    "kind": "delegated",
                    "evidence": "atif_embedded_unreferenced",
                    "location": f"embedded:{trajectory_id}",
                    "spawned_at_step": None,
                    "spawned_at_offset_seconds": None,
                    **_child_summary(child),
                }
            )
    sidechain_groups: dict[str, list[_Step]] = {}
    current_run = 0
    previous_side = False
    for step in steps:
        if step.is_sidechain:
            if not previous_side:
                current_run += 1
            key = step.agent_id or f"sidechain-run-{current_run}"
            sidechain_groups.setdefault(key, []).append(step)
        previous_side = step.is_sidechain
    for key, group in sidechain_groups.items():
        stamps = [s.timestamp for s in group if s.timestamp]
        items.append(
            {
                "id": key,
                "kind": "delegated",
                "evidence": "claude_code_sidechain",
                "location": "inline sidechain steps",
                "spawned_at_step": group[0].step,
                "spawned_at_offset_seconds": _seconds(origin, group[0].timestamp),
                "model": group[0].model,
                "steps": len(group),
                "tool_calls": sum(len(s.actions) for s in group),
                "input_tokens": _sum(s.prompt_tokens for s in group),
                "output_tokens": _sum(s.completion_tokens for s in group),
                "cost_usd": _sum(s.cost_usd for s in group),
                "started_at": _iso(min(stamps)) if stamps else None,
                "ended_at": _iso(max(stamps)) if stamps else None,
                "duration_seconds": _seconds(min(stamps), max(stamps)) if len(stamps) > 1 else None,
            }
        )
    delegations = [
        {
            "step": step.step,
            "offset_seconds": _seconds(origin, step.timestamp),
            "tool": action.tool,
            "target": _clip(action.target, 120),
        }
        for step in steps
        if not step.is_sidechain
        for action in step.actions
        if action.tool.lower() in _DELEGATION_TOOLS
        or any(t.lower() in _DELEGATION_TOOLS for t in action.inner_tools)
    ]
    items.sort(key=lambda i: (i["spawned_at_step"] is None, i["spawned_at_step"] or 0, str(i["id"])))
    if items:
        observability = "captured"
    elif delegations:
        observability = "delegations_only"
    else:
        observability = "none_observed"
    return {
        "observability": observability,
        "count": len(items),
        "delegated_count": sum(1 for i in items if i["kind"] == "delegated"),
        "summarization_count": sum(1 for i in items if i["kind"] == "context_summarization"),
        "delegation_calls": delegations,
        "items": items,
    }, context_events


def _context(
    steps: Sequence[_Step],
    segments: Sequence[tuple[Path, dict[str, Any], str]],
    subagent_events: list[dict[str, Any]],
    trial_dir: Path,
) -> dict[str, Any]:
    events = list(subagent_events)
    previous: _Step | None = None
    previous_segment: int | None = None
    for step in steps:
        if previous_segment is not None and step.segment != previous_segment:
            name = segments[step.segment][0].name
            events.append({"step": step.step, "kind": "continuation_segment", "detail": name})
        if step.context_event:
            events.append({"step": step.step, "kind": "context_management", "detail": step.context_event})
        for notice in step.notices:
            events.append({"step": step.step, "kind": "harness_notice", "detail": _clip(notice, 160)})
        if (
            previous is not None
            and previous.prompt_tokens is not None
            and step.prompt_tokens is not None
            and previous.prompt_tokens >= _COMPACTION_MIN_PROMPT
            and step.prompt_tokens < previous.prompt_tokens * _COMPACTION_DROP_RATIO
        ):
            events.append(
                {
                    "step": step.step,
                    "kind": "inferred_context_drop",
                    "detail": f"input tokens fell {previous.prompt_tokens:,} -> {step.prompt_tokens:,}",
                }
            )
        previous_segment = step.segment
        if step.source == "agent":
            previous = step
    copied = sum(
        1
        for _, data, _ in segments
        for raw in data.get("steps") or []
        if isinstance(raw, dict) and raw.get("is_copied_context")
    )
    root = trial_dir.resolve()
    return {
        "segments": [str(path.resolve().relative_to(root)) if root in path.resolve().parents else path.name for path, _, _ in segments],
        "copied_context_steps_excluded": copied,
        "events": sorted(events, key=lambda e: (e["step"], e["kind"])),
    }


def _errors(
    actions: Sequence[_Action], result: dict[str, Any], origin: datetime | None
) -> dict[str, Any]:
    errors = [a for a in actions if a.status == "error"]
    categories = Counter(a.category for a in errors)
    exception = _dict(result.get("exception_info"))
    message = exception.get("exception_message") or exception.get("message")
    last_judged = next((a for a in reversed(actions) if a.status != "unknown"), None)
    return {
        "tool_errors": len(errors),
        "harness_signalled": sum(1 for a in errors if a.evidence not in (None, "output_text")),
        "inferred_from_output": sum(1 for a in errors if a.evidence == "output_text"),
        "status_unknown_calls": sum(a.call_count for a in actions)
        - sum(a.call_count for a in actions if a.status == "ok")
        - len(errors),
        "calls_without_result": sum(a.call_count for a in actions if a.evidence == "no_result"),
        "not_executed_calls": sum(a.call_count for a in actions if a.evidence == "not_executed"),
        "expected_probe_misses": sum(1 for a in actions if a.category == "expected_probe_miss"),
        "by_category": dict(sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))),
        "first_error": {
            "step": errors[0].step,
            "offset_seconds": _seconds(origin, errors[0].timestamp),
        }
        if errors
        else None,
        "ended_in_error": bool(last_judged and last_judged.status == "error"),
        "examples": [
            {
                "step": a.step,
                "tool": a.tool,
                "target": _clip(a.target, 120),
                "category": a.category,
                "evidence": a.evidence,
                "excerpt": a.excerpt,
            }
            for a in errors[:TOP_N]
        ],
        "exception": {
            "type": exception.get("exception_type") or exception.get("type"),
            "message": _clip(str(message), 300) if message else None,
            "occurred_at": exception.get("occurred_at"),
        }
        if exception
        else None,
    }


def _outcome(result: dict[str, Any], steps: Sequence[_Step], trial_dir: Path) -> dict[str, Any]:
    verifier = _dict(result.get("verifier_result"))
    rewards = {
        str(k): float(v) for k, v in _dict(verifier.get("rewards")).items() if _float(v) is not None
    }
    # The primary reward is `reward` (or the only metric). Several metrics without a
    # primary still score the trial: all at 1 passes, all at 0 fails, anything else is partial.
    reward = rewards.get("reward") if "reward" in rewards else (
        next(iter(rewards.values())) if len(rewards) == 1 else None
    )
    judged = [reward] if reward is not None else list(rewards.values())
    exception = _dict(result.get("exception_info"))
    if not judged:
        verdict = "errored" if exception else "not_scored"
    elif all(value >= PASS_REWARD for value in judged):
        verdict = "passed"
    elif all(value <= 0 for value in judged):
        verdict = "failed"
    else:
        verdict = "partial"
    final_message = next(
        (s.message for s in reversed(steps) if s.source == "agent" and s.message.strip()), None
    )
    verifier_dir = trial_dir / "verifier"
    return {
        "verdict": verdict,
        "reward": reward,
        "rewards": rewards,
        "final_agent_message": _clip(final_message, 600) if final_message else None,
        "verifier_files": sorted(
            str(p.relative_to(trial_dir)) for p in verifier_dir.rglob("*") if p.is_file()
        )[:20]
        if verifier_dir.is_dir()
        else [],
    }


def _step_flags(
    step: _Step, revisit_steps: set[int], error_steps: set[int], spawn_steps: set[int],
    event_steps: set[int],
) -> list[str]:
    flags: list[str] = []
    if step.step in error_steps:
        flags.append("error")
    if step.step in revisit_steps:
        flags.append("revisit")
    if step.step in spawn_steps:
        flags.append("subagent")
    if step.step in event_steps:
        flags.append("context")
    if step.is_sidechain:
        flags.append("sidechain")
    return flags


def _timeline(
    steps: Sequence[_Step],
    actions: Sequence[_Action],
    subagents: dict[str, Any],
    context: dict[str, Any],
    origin: datetime | None,
    limit: int | None,
) -> dict[str, Any]:
    seen: set[str] = set()
    revisit_steps: set[int] = set()
    for action in actions:
        if action.is_poll:
            continue
        if action.signature in seen:
            revisit_steps.add(action.step)
        seen.add(action.signature)
    error_steps = {a.step for a in actions if a.status == "error"}
    spawn_steps = {i["spawned_at_step"] for i in subagents["items"] if i["spawned_at_step"]}
    spawn_steps |= {d["step"] for d in subagents["delegation_calls"]}
    event_steps = {e["step"] for e in context["events"]}
    notable = revisit_steps | error_steps | spawn_steps | event_steps

    def entry(step: _Step) -> dict[str, Any]:
        statuses = {a.status for a in step.actions}
        return {
            "step": step.step,
            "offset_seconds": _seconds(origin, step.timestamp),
            "source": step.source,
            "action": _action_preview(step.actions),
            "status": "error" if "error" in statuses else (
                "ok" if statuses == {"ok"} else ("unknown" if statuses else None)
            ),
            "input_tokens": step.prompt_tokens,
            "output_tokens": step.completion_tokens,
            "cost_usd": step.cost_usd,
            "message": _clip(step.message, 140 if step.source == "agent" else 80)
            if step.message.strip()
            else None,
            "flags": _step_flags(step, revisit_steps, error_steps, spawn_steps, event_steps),
        }

    if limit is None or len(steps) <= limit:
        chosen = list(steps)
    else:
        edge = max(1, limit // 6)
        keep = {s.step for s in steps[:edge]} | {s.step for s in steps[-edge:]}
        for step in steps:
            if len(keep) >= limit:
                break
            if step.step in notable:
                keep.add(step.step)
        chosen = [s for s in steps if s.step in keep]
    entries: list[dict[str, Any]] = []
    previous_step = 0
    for step in chosen:
        if step.step > previous_step + 1:
            entries.append({"omitted_steps": [previous_step + 1, step.step - 1]})
        entries.append(entry(step))
        previous_step = step.step
    if steps and previous_step < steps[-1].step:
        entries.append({"omitted_steps": [previous_step + 1, steps[-1].step]})
    windows: list[dict[str, Any]] = []
    if len(steps) >= MIN_STEPS_FOR_WINDOWS:
        for index in range(WINDOW_COUNT):
            members = [s for s in steps if _window_of(s.step, len(steps)) == index]
            if not members:
                continue
            member_actions = [a for s in members for a in s.actions]
            stamps = [s.timestamp for s in members if s.timestamp]
            windows.append(
                {
                    "steps": [members[0].step, members[-1].step],
                    "tool_calls": len(member_actions),
                    "errors": sum(1 for a in member_actions if a.status == "error"),
                    "revisits": sum(1 for s in members if s.step in revisit_steps),
                    "output_tokens": _sum(s.completion_tokens for s in members),
                    "cost_usd": _sum(s.cost_usd for s in members),
                    "seconds": _seconds(min(stamps), max(stamps)) if len(stamps) > 1 else None,
                }
            )
    return {
        "total_steps": len(steps),
        "shown_steps": len(chosen),
        "entries": entries,
        "windows": windows,
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def _identity(
    result: dict[str, Any],
    trial_dir: Path,
    root_doc: dict[str, Any],
    outline: TrajectoryOutline | None,
) -> dict[str, Any]:
    config = _dict(result.get("config"))
    agent_info = _dict(result.get("agent_info"))
    agent_cfg = _dict(config.get("agent"))
    model_info = _dict(agent_info.get("model_info"))
    agent_doc = _dict(root_doc.get("agent"))
    model = (
        model_info.get("name")
        or agent_cfg.get("model_name")
        or agent_doc.get("model_name")
        or (outline.model_name if outline and outline.model_name != "unknown" else None)
    )
    if model and model_info.get("provider") and "/" not in str(model):
        model = f"{model_info['provider']}/{model}"
    return {
        "trial_name": result.get("trial_name") or trial_dir.name,
        "trial_id": result.get("id"),
        "task": result.get("task_name") or (outline.task_name if outline else None),
        "job": trial_dir.parent.name,
        "agent": agent_info.get("name") or agent_cfg.get("name") or agent_doc.get("name"),
        "agent_version": agent_info.get("version") or agent_doc.get("version"),
        "model": model,
        "atif_schema": root_doc.get("schema_version"),
        "session_id": root_doc.get("session_id"),
        "trial_dir": str(trial_dir),
    }


def _summary_line(report: dict[str, Any]) -> str:
    identity = report["identity"]
    outcome = report["outcome"]
    timing = report["timing"]
    tokens = report["tokens"]
    cost = report["cost"]
    tools = report["tools"]
    revisits = report["revisits"]
    subagents = report["subagents"]
    who = f"{identity['agent'] or 'unknown agent'} ({identity['model'] or 'unknown model'})"
    reward = outcome["reward"]
    verdict = outcome["verdict"].replace("_", " ")
    result = f"{verdict}" + (f" (reward {reward:g})" if reward is not None else "")
    wall = _fmt_seconds(timing["total_seconds"])
    agent = _fmt_seconds(timing["phases"]["agent_execution"]["seconds"])
    parts = [
        f"{who} {result} on {identity['task'] or 'an unknown task'}",
        f"in {wall} wall ({agent} agent)",
    ]
    if report["availability"]["trajectory"] == "present":
        spend = _fmt_tokens(tokens["total"])
        money = f"${cost['total_usd']:.4f}" if cost["total_usd"] is not None else "cost unavailable"
        parts.append(
            f"{_plural(report['timeline']['total_steps'], 'step')}, "
            f"{_plural(tools['total_calls'], 'tool call')}, "
            f"{_plural(report['errors']['tool_errors'], 'error')}, {spend} tokens, {money}"
        )
        parts.append(
            f"{_plural(revisits['repeated_actions'], 'repeated action')} "
            f"({_plural(revisits['exact_revisits'], 'exact revisit')})"
        )
        parts.append(
            "no subagents observed"
            if subagents["observability"] == "none_observed"
            else f"{subagents['count']} subagents, {len(subagents['delegation_calls'])} delegation calls"
        )
    else:
        parts.append(f"no trajectory ({report['availability']['reason']})")
    return "; ".join(parts) + "."


def build_run_report(
    trial_dir: str | Path, *, timeline_limit: int | None = DEFAULT_TIMELINE_LIMIT
) -> dict[str, Any]:
    """Build the ``evallab.run_report/v1`` dictionary for one Harbor trial directory."""
    trial = Path(trial_dir).resolve()
    result_path = trial / "result.json"
    try:
        loaded = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    except (OSError, ValueError):
        loaded = {}
    result = _dict(loaded)
    quality: list[str] = []
    if not result:
        quality.append("result.json missing or unreadable: outcome and phase timing unavailable")

    outline: TrajectoryOutline | None
    try:
        outline = outline_trajectory(trial, repo_root=trial, explicit_runs_root=trial)
    except (TrajectoryError, ValueError, OSError):
        outline = None
    try:
        _, traj_path, _ = resolve_trial_target(trial, repo_root=trial, explicit_runs_root=trial)
    except (TrajectoryError, ValueError, OSError):
        traj_path = None

    segments: tuple[tuple[Path, dict[str, Any], str], ...] = ()
    availability: dict[str, Any] = {"trajectory": "present", "reason": None}
    if traj_path is None or not traj_path.is_file():
        agent_name = str(
            _dict(result.get("agent_info")).get("name")
            or _dict(_dict(result.get("config")).get("agent")).get("name")
            or ""
        )
        reason = (
            _CONTROL_REASON
            if agent_name in CONTROL_AGENTS
            else "trajectory file missing (agent may have crashed before writing it)"
        )
        availability = {"trajectory": "absent", "reason": reason}
    else:
        try:
            data = json.loads(traj_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            data = None
            availability = {"trajectory": "unreadable", "reason": f"{type(exc).__name__}"}
        if isinstance(data, dict):
            chain = _resolve_chain_segments(traj_path, data, trial)
            segments = chain.segments
            if not chain.complete:
                quality.append(
                    f"continuation chain incomplete: {chain.stopped_ref!r} is {chain.stop_cause}; "
                    "totals come from the last readable segment"
                )
        elif data is not None:
            availability = {"trajectory": "unreadable", "reason": "top-level JSON is not an object"}
    if availability["trajectory"] != "present":
        quality.append(f"trajectory {availability['trajectory']}: {availability['reason']}")

    positioned = _chain_action_steps(segments) if segments else []
    steps = _build_steps(positioned)
    if len(steps) < len(positioned):
        quality.append(f"{len(positioned) - len(steps)} malformed (non-object) steps skipped")
    actions = [a for s in steps for a in s.actions]
    root_doc = segments[0][1] if segments else {}
    terminal_doc = segments[-1][1] if segments else {}

    timing, origin = _timing(result, steps)
    if timing["phases_overlap"]:
        quality.append("Harbor phase timestamps overlap or exceed the trial wall time")
    tokens, cost, token_warnings = _tokens_and_cost(result, terminal_doc, steps)
    if availability["reason"] != _CONTROL_REASON:
        quality.extend(token_warnings)
    if steps and not any(s.timestamp for s in steps):
        quality.append("steps carry no timestamps: per-step timing unavailable")
    subagents, subagent_events = _subagents(steps, segments, trial, origin)
    context = _context(steps, segments, subagent_events, trial)
    errors = _errors(actions, result, origin)
    if actions and all(a.evidence in (None, "output_text", "no_result") for a in actions):
        quality.append(
            "harness reports no per-call status (no exit codes or error flags): tool errors "
            "are only inferred from output text"
        )
    report: dict[str, Any] = {
        "schema": RUN_REPORT_SCHEMA,
        "identity": _identity(result, trial, root_doc, outline),
        "availability": availability,
        "outcome": _outcome(result, steps, trial),
        "timing": timing,
        "tokens": tokens,
        "cost": cost,
        "tools": _tools(actions),
        "revisits": _revisits(actions, outline if outline and outline.status == "featured" else None, len(steps)),
        "subagents": subagents,
        "context": context,
        "errors": errors,
        "timeline": _timeline(steps, actions, subagents, context, origin, timeline_limit),
        "data_quality": quality,
        "sources": [
            {"path": str(path.relative_to(trial)) if trial in path.parents else str(path), "sha256": _sha256_file(path)}
            for path in ([result_path] if result_path.is_file() else []) + [p for p, _, _ in segments]
        ],
    }
    report["summary"] = _summary_line(report)
    return report


def is_trial_dir(path: Path) -> bool:
    """A Harbor trial directory holds a TrialResult (``result.json`` with ``trial_name``)."""
    try:
        data = json.loads((path / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "trial_name" in data


def trial_dirs(job_dir: str | Path) -> list[Path]:
    """Trial directories directly under a Harbor job directory, sorted by name."""
    job = Path(job_dir)
    return sorted(p for p in job.iterdir() if p.is_dir() and is_trial_dir(p))


def build_job_report(
    job_dir: str | Path, *, timeline_limit: int | None = DEFAULT_TIMELINE_LIMIT
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build every trial report under ``job_dir`` plus an ``evallab.job_report/v1`` rollup."""
    job = Path(job_dir).resolve()
    reports = [build_run_report(t, timeline_limit=timeline_limit) for t in trial_dirs(job)]
    rows = []
    for r in reports:
        rows.append(
            {
                "trial": r["identity"]["trial_name"],
                "task": r["identity"]["task"],
                "agent": r["identity"]["agent"],
                "model": r["identity"]["model"],
                "verdict": r["outcome"]["verdict"],
                "reward": r["outcome"]["reward"],
                "wall_seconds": r["timing"]["total_seconds"],
                "agent_seconds": r["timing"]["phases"]["agent_execution"]["seconds"],
                "steps": r["timeline"]["total_steps"],
                "tool_calls": r["tools"]["total_calls"],
                "tool_errors": r["errors"]["tool_errors"],
                "tokens": r["tokens"]["total"],
                "cost_usd": r["cost"]["total_usd"],
                "cost_source": r["cost"]["source"],
                "repeated_actions": r["revisits"]["repeated_actions"],
                "exact_revisits": r["revisits"]["exact_revisits"],
                "subagents": r["subagents"]["count"],
                "trajectory": r["availability"]["trajectory"],
            }
        )
    rewards = [row["reward"] for row in rows if row["reward"] is not None]
    # Price-table estimates are trial-level list-price figures, never metered
    # spend: they roll up separately and never enter the harness-cost sums.
    costs = [
        row["cost_usd"]
        for row in rows
        if row["cost_usd"] is not None and row["cost_source"] != "price_table_estimate"
    ]
    estimated = [
        row["cost_usd"]
        for row in rows
        if row["cost_usd"] is not None and row["cost_source"] == "price_table_estimate"
    ]
    walls = [row["wall_seconds"] for row in rows if row["wall_seconds"] is not None]
    agents = [row["agent_seconds"] for row in rows if row["agent_seconds"] is not None]
    passed = sum(1 for row in rows if row["verdict"] == "passed")
    scored = sum(1 for row in rows if row["verdict"] in {"passed", "failed", "partial"})
    job_report = {
        "schema": JOB_REPORT_SCHEMA,
        "job": job.name,
        "job_dir": str(job),
        "trials": len(rows),
        "scored": scored,
        "passed": passed,
        "pass_rate": round(passed / scored, 4) if scored else None,
        "mean_reward": round(sum(rewards) / len(rewards), 4) if rewards else None,
        "total_cost_usd": round(sum(costs), 6) if costs else None,
        "trials_without_cost": len(rows) - len(costs) - len(estimated),
        "trials_with_estimated_cost": len(estimated),
        "estimated_cost_usd": round(sum(estimated), 6) if estimated else None,
        "cost_per_pass_usd": round(sum(costs) / passed, 6) if costs and passed else None,
        "median_wall_seconds": round(median(walls), 3) if walls else None,
        "median_agent_seconds": round(median(agents), 3) if agents else None,
        "total_tokens": _sum(row["tokens"] for row in rows),
        "total_tool_calls": sum(row["tool_calls"] for row in rows),
        "total_repeated_actions": sum(row["repeated_actions"] for row in rows),
        "total_subagents": sum(row["subagents"] for row in rows),
        "verdicts": dict(Counter(row["verdict"] for row in rows)),
        "rows": rows,
    }
    return job_report, reports


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value < 60:
        return f"{value:.1f}s"
    minutes, seconds = divmod(int(round(value)), 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _fmt_tokens(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 10_000:
        return f"{value / 1000:.1f}k"
    return f"{int(value):,}"


def _fmt_usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _cell(text: Any) -> str:
    return _fmt(text).replace("|", "\\|").replace("\n", " ")


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return lines


def render_run_report_markdown(report: dict[str, Any]) -> str:
    """Render an ``evallab.run_report/v1`` dictionary as Markdown."""
    identity = report["identity"]
    outcome = report["outcome"]
    timing = report["timing"]
    tokens = report["tokens"]
    cost = report["cost"]
    tools = report["tools"]
    revisits = report["revisits"]
    subagents = report["subagents"]
    context = report["context"]
    errors = report["errors"]
    timeline = report["timeline"]
    lines = [
        f"# Run report: {identity['trial_name']}",
        "",
        f"> {report['summary']}",
        "",
        "## Identity",
        *_table(
            ["Field", "Value"],
            [
                ["Task", identity["task"]],
                ["Agent", f"{identity['agent']} {identity['agent_version'] or ''}".strip()],
                ["Model", identity["model"]],
                ["Job", identity["job"]],
                ["Trajectory", f"{report['availability']['trajectory']} ({identity['atif_schema'] or 'no schema'})"],
                ["Trial dir", f"`{identity['trial_dir']}`"],
            ],
        ),
        "",
        "## Outcome",
        f"- Verdict: **{outcome['verdict'].replace('_', ' ')}**"
        + (f" — reward {outcome['reward']:g}" if outcome["reward"] is not None else ""),
    ]
    if len(outcome["rewards"]) > 1:
        lines.append(
            "- Rewards: " + ", ".join(f"{k}={v:g}" for k, v in sorted(outcome["rewards"].items()))
        )
    if errors["exception"]:
        exc = errors["exception"]
        lines.append(f"- Exception: `{exc['type']}` — {exc['message'] or ''}".rstrip(" —"))
    if outcome["final_agent_message"]:
        lines.append(f"- Final agent message: {outcome['final_agent_message']}")
    lines += ["", "## Time"]
    phases = timing["phases"]
    lines += _table(
        ["Phase", "Duration", "Starts at"],
        [
            [name.replace("_", " "), _fmt_seconds(phases[name]["seconds"]),
             _fmt_seconds(phases[name]["starts_at_offset_seconds"])]
            for name in _HARBOR_PHASES
        ]
        + [["**total wall**", _fmt_seconds(timing["total_seconds"]), "0.0s"]],
    )
    gaps = timing["step_gap_seconds"]
    if timing["first_agent_step_offset_seconds"] is not None:
        lines += [
            "",
            f"First agent step {_fmt_seconds(timing['first_agent_step_offset_seconds'])} after "
            f"`{timing['offset_origin']}` (offsets below use the same origin).",
        ]
    if gaps["count"]:
        lines += [
            "",
            f"Time between consecutive agent steps (model turn + previous tool time): median "
            f"{_fmt_seconds(gaps['median'])}, p90 {_fmt_seconds(gaps['p90'])}, max "
            f"{_fmt_seconds(gaps['max'])} over {gaps['count']} gaps.",
        ]
        if timing["slowest_steps"]:
            lines += ["", "Slowest steps:"]
            lines += [
                f"- step {s['step']}: {_fmt_seconds(s['seconds'])} — {s['action'] or 'no tool call'}"
                for s in timing["slowest_steps"]
            ]
    lines += ["", "## Tokens and cost"]
    src = tokens["sources"]
    lines += _table(
        ["Measure", "Value", "Source"],
        [
            ["Input (incl. cached)", _fmt_tokens(tokens["input"]), src.get("input")],
            ["  cached", _fmt_tokens(tokens["cached_input"]), src.get("cached_input")],
            ["  uncached", _fmt_tokens(tokens["uncached_input"]), "input − cached"],
            ["Output", _fmt_tokens(tokens["output"]), src.get("output")],
            ["  reasoning", _fmt_tokens(tokens["reasoning"]), src.get("reasoning")],
            ["Cache writes", _fmt_tokens(tokens["cache_write"]), "step_sum"],
            ["**Total tokens**", _fmt_tokens(tokens["total"]), "input + output"],
            [
                "**Cost**",
                _fmt_usd(cost["total_usd"])
                + (" (lower bound)" if cost["is_lower_bound"] else "")
                + (" (estimate)" if cost["source"] == "price_table_estimate" else ""),
                (cost["source"] or "unavailable")
                + (f" / {cost['harness_cost_source']}" if cost["harness_cost_source"] else ""),
            ],
        ],
    )
    if cost["source"] == "price_table_estimate" and cost["price_table"]:
        table = cost["price_table"]
        rates = table["usd_per_mtok"]
        lines += [
            "",
            f"Cost is an estimate from pinned list prices for model {table['model']!r} "
            f"(matched {table['matched_key']!r}): ${rates['input']}/M input, "
            f"${rates['cached_input']}/M cached input, ${rates['output']}/M output. "
            f"Source: {table['source_url']} (retrieved {table['retrieved_on']}). "
            "Not a metered charge.",
        ]
    ctx = tokens["context"]
    lines += [
        "",
        f"Usage recorded on {tokens['llm_steps_with_usage']} of {tokens['llm_steps']} agent steps. "
        f"Context: first prompt {_fmt_tokens(ctx['first_prompt_tokens'])}, peak "
        f"{_fmt_tokens(ctx['peak_prompt_tokens'])} (step {_fmt(ctx['peak_step'])}), last "
        f"{_fmt_tokens(ctx['last_prompt_tokens'])}. {cost['note']}",
    ]
    if tokens["top_steps"]:
        lines += ["", "Most expensive steps:"]
        lines += _table(
            ["Step", "Input", "Output", "Cost", "Action"],
            [
                [s["step"], _fmt_tokens(s["input"]), _fmt_tokens(s["output"]), _fmt_usd(s["cost_usd"]), s["action"]]
                for s in tokens["top_steps"]
            ],
        )
    lines += ["", "## Tools"]
    if tools["total_calls"]:
        lines.append(
            f"{_plural(tools['total_calls'], 'call')} across {_plural(tools['distinct_tools'], 'tool')}"
            + (
                f" in {_plural(tools['actions'], 'action')} (calls that share one observation "
                "count as one action; revisits, errors, and polls count actions)"
                if tools["actions"] != tools["total_calls"]
                else ""
            )
            + (f"; {tools['polls']} polls" if tools["polls"] else "")
            + (
                "; wrapped: " + ", ".join(f"{k}×{v}" for k, v in tools["wrapped_calls"].items())
                if tools["wrapped_calls"]
                else ""
            )
            + "."
        )
        lines += _table(
            ["Tool", "Calls", "OK", "Errors", "Unknown", "Errors / judged", "Output chars", "Steps"],
            [
                [r["tool"], r["calls"], r["ok"], r["errors"], r["unknown"],
                 f"{_fmt_pct(r['error_rate'])} of {r['judged']}" if r["judged"] else "n/a",
                 f"{r['output_chars']:,}", f"{r['first_step']}–{r['last_step']}"]
                for r in tools["by_tool"]
            ],
        )
        if tools["top_programs"]:
            lines += [
                "",
                "Shell programs: "
                + ", ".join(f"`{p['program']}`×{p['calls']}" for p in tools["top_programs"]),
            ]
    else:
        lines.append("No tool calls recorded.")
    lines += ["", "## Revisits (did it circle back?)"]
    if revisits["actions_considered"]:
        lines += _table(
            ["Measure", "Value"],
            [
                ["Actions considered (polls excluded)", revisits["actions_considered"]],
                ["Distinct actions", revisits["distinct_actions"]],
                ["Repeated actions", f"{revisits['repeated_actions']} ({_fmt_pct(revisits['repeat_rate'])} of actions)"],
                ["  returned to an earlier action", revisits["returns_to_earlier_action"]],
                ["  immediate repeats", revisits["consecutive_repeats"]],
                ["**Exact revisits** (same action, same result)", revisits["exact_revisits"]],
                ["Same result from a different action", revisits["same_result_from_different_action"]],
                ["Repeated identical errors", revisits["repeated_errors"]],
                ["Longest identical run", f"{revisits['longest_identical_run']['length']} (steps {revisits['longest_identical_run']['steps']})"],
            ]
            + (
                [["Loop suspicion", f"{'detected' if revisits['loop_suspicion']['detected'] else 'not detected'} "
                  f"(score {revisits['loop_suspicion']['score']:.2f}; {', '.join(revisits['loop_suspicion']['reasons']) or 'no reasons'})"]]
                if revisits["loop_suspicion"]
                else []
            ),
        )
        if revisits["most_repeated"]:
            lines += ["", "Most repeated actions:"]
            lines += [
                f"- {g['count']}× `{g['tool']}` {g['target']} — steps {g['steps']}, "
                f"{g['identical_results']} with identical results"
                for g in revisits["most_repeated"]
            ]
        if any(revisits.get("repeats_by_window") or ()):
            lines += ["", f"Repeats by tenth of the run: {revisits['repeats_by_window']}"]
    else:
        lines.append("No actions to compare.")
    lines += ["", "## Subagents"]
    observability = {
        "captured": "Subagent activity captured in the trajectory.",
        "delegations_only": "Delegation calls observed, but the harness did not record the child trajectories.",
        "none_observed": "No subagents or delegation calls observed. (Harbor's codex converter drops subagent threads; claude-code records them as sidechain steps; terminus-2 records summarization subagents.)",
    }[subagents["observability"]]
    lines.append(observability)
    if subagents["items"]:
        lines += _table(
            ["Id", "Kind", "Spawned (step / offset)", "Duration", "Steps", "Tool calls", "Tokens in/out", "Cost", "Evidence"],
            [
                [
                    i["id"], i["kind"],
                    f"{_fmt(i['spawned_at_step'])} / {_fmt_seconds(i['spawned_at_offset_seconds'])}",
                    _fmt_seconds(i.get("duration_seconds")), _fmt(i.get("steps")), _fmt(i.get("tool_calls")),
                    f"{_fmt_tokens(i.get('input_tokens'))}/{_fmt_tokens(i.get('output_tokens'))}",
                    _fmt_usd(i.get("cost_usd")), i["evidence"],
                ]
                for i in subagents["items"]
            ],
        )
    if subagents["delegation_calls"]:
        lines += ["", "Delegation calls:"]
        lines += [
            f"- step {d['step']} (+{_fmt_seconds(d['offset_seconds'])}): `{d['tool']}` {d['target']}"
            for d in subagents["delegation_calls"]
        ]
    lines += ["", "## Context management"]
    lines.append(
        f"Segments: {len(context['segments'])}; copied-context steps excluded: "
        f"{context['copied_context_steps_excluded']}."
    )
    lines += [f"- step {e['step']}: {e['kind']} — {e['detail']}" for e in context["events"]]
    lines += ["", "## Errors"]
    unknown_detail = [
        f"{errors[key]} {label}"
        for key, label in (
            ("calls_without_result", "without a recorded result"),
            ("not_executed_calls", "intercepted by the harness (not executed)"),
        )
        if errors[key]
    ]
    lines.append(
        f"{errors['tool_errors']} tool errors ({errors['harness_signalled']} signalled by the harness, "
        f"{errors['inferred_from_output']} inferred from output text); "
        f"{errors['status_unknown_calls']} calls with no status signal"
        + (f" ({', '.join(unknown_detail)})" if unknown_detail else "")
        + "."
        + (
            f" {errors['expected_probe_misses']} expected probe misses (for example grep finding "
            "nothing) are counted as ok."
            if errors["expected_probe_misses"]
            else ""
        )
        + (f" First error at step {errors['first_error']['step']}." if errors["first_error"] else "")
        + (" The run ended on an error." if errors["ended_in_error"] else "")
    )
    if errors["by_category"]:
        lines.append("By category: " + ", ".join(f"{k}×{v}" for k, v in errors["by_category"].items()))
    lines += [
        f"- step {e['step']} `{e['tool']}` {e['target']} [{e['category']}]: {e['excerpt'] or ''}"
        for e in errors["examples"]
    ]
    lines += ["", "## Timeline"]
    if timeline["windows"]:
        lines += _table(
            ["Steps", "Tool calls", "Errors", "Revisits", "Output tokens", "Cost", "Duration"],
            [
                [f"{w['steps'][0]}–{w['steps'][1]}", w["tool_calls"], w["errors"], w["revisits"],
                 _fmt_tokens(w["output_tokens"]), _fmt_usd(w["cost_usd"]), _fmt_seconds(w["seconds"])]
                for w in timeline["windows"]
            ],
        )
        lines.append("")
    lines.append(f"Showing {timeline['shown_steps']} of {timeline['total_steps']} steps.")
    rows = []
    for e in timeline["entries"]:
        if "omitted_steps" in e:
            rows.append([f"… {e['omitted_steps'][0]}–{e['omitted_steps'][1]}", "", "", "", "", "", ""])
            continue
        rows.append(
            [
                e["step"], _fmt_seconds(e["offset_seconds"]), e["source"],
                e["action"] or e["message"] or "", e["status"] or "",
                "—" if e["input_tokens"] is None and e["output_tokens"] is None
                else f"{_fmt_tokens(e['input_tokens'])}/{_fmt_tokens(e['output_tokens'])}",
                ", ".join(e["flags"]),
            ]
        )
    lines += _table(["Step", "At", "Source", "Action / message", "Status", "Tokens in/out", "Flags"], rows)
    lines += ["", "## Data quality"]
    lines += [f"- {q}" for q in report["data_quality"]] or ["- No gaps detected."]
    lines += ["", "## Sources"]
    lines += [f"- `{s['path']}` sha256:{s['sha256'][:16]}…" for s in report["sources"]]
    return "\n".join(lines) + "\n"


def render_job_report_markdown(job_report: dict[str, Any]) -> str:
    """Render an ``evallab.job_report/v1`` dictionary as Markdown."""
    lines = [
        f"# Job report: {job_report['job']}",
        "",
        f"Trials: {job_report['trials']}; scored: {job_report['scored']}; passed: {job_report['passed']} "
        f"(pass rate {_fmt_pct(job_report['pass_rate'])}, mean reward {_fmt(job_report['mean_reward'])}). "
        f"Total cost {_fmt_usd(job_report['total_cost_usd'])}"
        + (f" ({job_report['trials_without_cost']} trials without cost)" if job_report["trials_without_cost"] else "")
        + f"; cost per pass {_fmt_usd(job_report['cost_per_pass_usd'])}. "
        + (
            f"Price-table estimates {_fmt_usd(job_report['estimated_cost_usd'])} "
            f"across {job_report['trials_with_estimated_cost']} trials (not metered). "
            if job_report.get("trials_with_estimated_cost")
            else ""
        )
        + f"Median wall {_fmt_seconds(job_report['median_wall_seconds'])}, median agent time "
        f"{_fmt_seconds(job_report['median_agent_seconds'])}. Total tokens {_fmt_tokens(job_report['total_tokens'])}, "
        f"tool calls {job_report['total_tool_calls']}, repeated actions {job_report['total_repeated_actions']}, "
        f"subagents {job_report['total_subagents']}.",
        "",
    ]
    lines += _table(
        ["Trial", "Verdict", "Reward", "Wall", "Agent", "Steps", "Tools", "Errors", "Tokens", "Cost", "Repeats (exact)", "Subagents"],
        [
            [
                r["trial"], r["verdict"], r["reward"], _fmt_seconds(r["wall_seconds"]),
                _fmt_seconds(r["agent_seconds"]), r["steps"], r["tool_calls"], r["tool_errors"],
                _fmt_tokens(r["tokens"]),
                _fmt_usd(r["cost_usd"])
                + (" (est)" if r.get("cost_source") == "price_table_estimate" else ""),
                f"{r['repeated_actions']} ({r['exact_revisits']})", r["subagents"],
            ]
            for r in job_report["rows"]
        ],
    )
    return "\n".join(lines) + "\n"


_UNSAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _file_stem(name: str) -> str:
    """A filename stem that cannot leave the output directory."""
    stem = _UNSAFE_STEM_RE.sub("_", name).lstrip(".")[:120]
    return stem or "trial"


def write_reports(
    reports: Sequence[dict[str, Any]],
    output_dir: Path,
    job_report: dict[str, Any] | None = None,
) -> list[Path]:
    """Write JSON and Markdown files for each report (and the job rollup)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for report in reports:
        stem = _file_stem(Path(str(report["identity"]["trial_dir"])).name)
        json_path = output_dir / f"{stem}.run_report.json"
        md_path = output_dir / f"{stem}.run_report.md"
        json_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        md_path.write_text(render_run_report_markdown(report), encoding="utf-8")
        written += [json_path, md_path]
    if job_report is not None:
        json_path = output_dir / "job.run_report.json"
        md_path = output_dir / "job.run_report.md"
        json_path.write_text(json.dumps(job_report, indent=2) + "\n", encoding="utf-8")
        md_path.write_text(render_job_report_markdown(job_report), encoding="utf-8")
        written += [json_path, md_path]
    return written
