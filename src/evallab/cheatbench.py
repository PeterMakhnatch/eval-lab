"""Fetch and convert steinad/CheatBench public cheating traces ($0, no models).

CheatBench (MIT, https://huggingface.co/datasets/steinad/CheatBench) holds
4,472 labelled agent traces from public benchmark runs (Terminal-Bench 2,
SWE-bench Verified, CyBench, SWE-smith, HAL-USACO, MLE-bench, BountyBench,
SWE-rebench, KernelBench). Labels are ``cheating`` (844), ``attempt`` (41)
and ``benign`` (3,587); every positive row carries a mutually exclusive
``cheating_mechanism`` plus line-level localization (``loc_*`` columns).

This module pins the download (revision + sha256), selects the MiMo-relevant
subset (``secret_discovery_in_environment``,
``secret_discovery_via_web_search``, ``verifier_exploitation`` — future git
history, upstream fetch, grader exploitation), and converts
``cheatbench.raw_trace.v1`` payloads to stock-shaped ATIF staged
trajectories for the Trace Lab Scout viewer. Stdlib + pyarrow only; Harbor
models validate ATIF when installed, so staged files are checked
structurally here (see :func:`validate_staged`) and validated for real by
the Scout insert path.

These are other people's runs from public benchmarks, not Eval Lab runs:
imported outcomes stay theirs, flagged external, never recomputed into ours.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

REPO_ID = "steinad/CheatBench"
REVISION = "461360b34e93644955563dcc9243c6cc1a53bd0f"
FILENAME = "data/processed/parquet/full.parquet"
SHA256 = "b4d2a1496e5f8de7b4c0e4791bd9f8fcbd0a3567f95d2e759b5da20ebb160e63"
LICENSE = "MIT"
CARD_URL = "https://huggingface.co/datasets/steinad/CheatBench"
RAW_SCHEMA = "cheatbench.raw_trace.v1"
USER_AGENT = "evallab-cheatbench/0.1"

SUBSET_LABELS = frozenset({"cheating", "attempt"})
#: Mechanisms matching our MiMo cheats (future git history, upstream fetch,
#: grader exploitation).
SUBSET_MECHANISMS = frozenset(
    {
        "secret_discovery_in_environment",
        "secret_discovery_via_web_search",
        "verifier_exploitation",
    }
)

ATIF_SOURCES = ("system", "user", "agent")
_STAGED_BY = "src/evallab/cheatbench.py"


class CheatbenchError(Exception):
    """Pinned-fetch or conversion failure for the CheatBench corpus."""


def source_url() -> str:
    """Pinned HTTPS URL of the full parquet (never a moving ref)."""
    return f"https://huggingface.co/datasets/{REPO_ID}/resolve/{REVISION}/{FILENAME}"


def fetch(dest: str | Path, *, downloader: Callable[[str], bytes] | None = None) -> Path:
    """Download the pinned parquet to ``dest`` and verify its sha256.

    ``downloader`` is injectable so tests never reach the network.
    """
    dest = Path(dest)
    if dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == SHA256:
        return dest
    payload = (downloader or _anonymous_download)(source_url())
    actual = hashlib.sha256(payload).hexdigest()
    if actual != SHA256:
        raise CheatbenchError(
            f"digest mismatch for {REPO_ID}@{REVISION}: expected {SHA256}, got {actual}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    return dest


def _anonymous_download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
        return response.read()


def load_rows(parquet_path: str | Path, *, columns: list[str] | None = None) -> list[dict]:
    """Read parquet rows as plain dicts (pyarrow is a root dependency)."""
    import pyarrow.parquet as pq

    return pq.read_table(parquet_path, columns=columns).to_pylist()


def is_subset_row(row: Mapping[str, Any]) -> bool:
    """Whether a row belongs to the MiMo-relevant viewer subset."""
    return row.get("label") in SUBSET_LABELS and row.get("cheating_mechanism") in SUBSET_MECHANISMS


def select_subset(rows: list[dict]) -> list[dict]:
    """Every ``cheating``/``attempt`` row with a MiMo-relevant mechanism."""
    return [row for row in rows if is_subset_row(row)]


def counts_table(rows: list[dict]) -> list[tuple[Any, Any, Any, int]]:
    """Sorted (benchmark, mechanism, label, n) over the subset rows."""
    from collections import Counter

    counts: Counter[tuple[Any, Any, Any]] = Counter(
        (row.get("benchmark"), row.get("cheating_mechanism"), row.get("label"))
        for row in rows
        if is_subset_row(row)
    )
    return [(b, m, label, counts[(b, m, label)]) for (b, m, label) in sorted(counts)]


_FUNCTION_MARKUP = re.compile(r"<function=([^>]+)>")
_PARAMETER_MARKUP = re.compile(r"<parameter=([^>]+)>(.*?)</parameter>", re.DOTALL)
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def resolve_field_path(obj: Any, path: str) -> Any:
    """Resolve ``messages[8].content``-style paths; raise LookupError."""
    if not path:
        raise LookupError("empty field path")
    current = obj
    for match in _PATH_TOKEN.finditer(path):
        name, index = match.group(1), match.group(2)
        try:
            current = current[name] if name is not None else current[int(index)]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LookupError(f"unresolvable field path {path!r}") from exc
    return current


def parse_raw_trace(payload: str) -> dict:
    """Parse a serialized trace; reject a mismatched declared schema."""
    try:
        trace = json.loads(payload)
    except ValueError as exc:
        raise CheatbenchError(f"trace is not JSON: {exc}") from exc
    if not isinstance(trace, dict):
        raise CheatbenchError("trace payload must be a JSON object")
    declared = trace.get("schema_version", trace.get("schema"))
    if declared is not None and declared != RAW_SCHEMA:
        raise CheatbenchError(f"unsupported trace schema {declared!r}")
    return trace


def text_of(content: Any) -> str:
    """Best-effort plain text of heterogeneous message content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                for key in ("text", "content", "output"):
                    value = part.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
        return "".join(parts)
    if isinstance(content, dict):
        return json.dumps(content)
    return str(content)


def parse_function_markup(message: str) -> list[dict]:
    """SWE-bench-style ``<function=f><parameter=k>v</parameter>`` calls."""
    calls = []
    for index, fn_match in enumerate(_FUNCTION_MARKUP.finditer(message), start=1):
        params = {key: value for key, value in _PARAMETER_MARKUP.findall(message[fn_match.end() :])}
        calls.append(
            {
                "tool_call_id": f"markup_{index}",
                "function_name": fn_match.group(1),
                "arguments": params,
            }
        )
    return calls


def parse_openai_tool_calls(tool_calls: Any) -> list[dict]:
    """OpenAI-style ``{id, function: {name, arguments}}`` to staged shape."""
    staged = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {"command": arguments}
        staged.append(
            {
                "tool_call_id": str(call.get("id") or f"call_{len(staged) + 1}"),
                "function_name": str(function.get("name") or "unknown"),
                "arguments": arguments if isinstance(arguments, dict) else {},
            }
        )
    return staged


def _new_step(
    step_id: int,
    source: str,
    message: str,
    *,
    field: str,
    line_start: int | None = None,
    line_end: int | None = None,
    note: str | None = None,
) -> dict:
    cheatbench: dict[str, Any] = {
        "source_ref": {"field": field, "line_start": line_start, "line_end": line_end}
    }
    if note is not None:
        cheatbench["note"] = note
    step: dict[str, Any] = {
        "step_id": step_id,
        "source": source,
        "message": message,
        "extra": {"cheatbench": cheatbench},
    }
    return step


def _attach_observation(
    steps: list[dict],
    content: str,
    *,
    field: str,
    line_start: int | None = None,
    line_end: int | None = None,
    source_call_id: str | None = None,
    warnings: list[str],
) -> int | None:
    """Attach tool output to the latest agent step; return its step_id."""
    text = content if isinstance(content, str) else text_of(content)
    result: dict[str, Any] = {"content": text}
    if source_call_id:
        result["source_call_id"] = source_call_id
    for step in reversed(steps):
        if step["source"] == "agent":
            observation = step.setdefault("observation", {"results": []})
            observation["results"].append(result)
            ref = step["extra"]["cheatbench"].setdefault("obs_refs", [])
            ref.append({"field": field, "line_start": line_start, "line_end": line_end})
            return step["step_id"]
    warnings.append(f"tool output at {field} has no preceding agent step; kept as user step")
    steps.append(_new_step(len(steps) + 1, "user", text, field=field, note="remapped-tool-output"))
    return steps[-1]["step_id"]


#: MLE-bench turn events use ``role: trace`` for agent narrations
#: (``LLM_OUTPUT: ...``); the swe-rebench single-log kind event never reaches
#: role mapping (its content is parsed into turns instead).
_ROLE_MAP = {"system": "system", "user": "user", "assistant": "agent", "trace": "agent"}


def _map_role(role: Any, *, field: str, warnings: list[str]) -> str:
    mapped = _ROLE_MAP.get(role)
    if mapped is None:
        warnings.append(f"unknown role {role!r} at {field}; kept as user step")
        return "user"
    return mapped


def convert_trace(trace: dict) -> tuple[list[dict], list[str]]:
    """Convert one parsed raw trace to ATIF steps (1-based step_id)."""
    warnings: list[str] = []
    if "messages" in trace:
        return _convert_messages(trace, warnings), warnings
    return _convert_events(trace, warnings), warnings


def _convert_messages(trace: dict, warnings: list[str]) -> list[dict]:
    steps = []
    for index, message in enumerate(trace.get("messages") or []):
        field = f"messages[{index}].content"
        role = message.get("role") if isinstance(message, dict) else None
        content = text_of(message.get("content") if isinstance(message, dict) else message)
        source = _map_role(role, field=field, warnings=warnings)
        calls = parse_function_markup(content) if source == "agent" else []
        step = _new_step(len(steps) + 1, source, content, field=field)
        if calls:
            step["tool_calls"] = calls
        steps.append(step)
    if not steps:
        warnings.append("trace has an empty messages[] list")
    return steps


def _convert_events(trace: dict, warnings: list[str]) -> list[dict]:
    events = trace.get("events") or []
    if isinstance(events, dict):
        events = [events]
    kinds = {tuple(sorted(e.keys())) for e in events if isinstance(e, dict)}
    if kinds == {("content", "kind", "role")} and len(events) == 1:
        content = events[0].get("content")
        if isinstance(content, str) and re.search(r"^\[\d+\]\s+role=", content, re.MULTILINE):
            return _convert_prefixed_log(content, "events[0].content", warnings)
    stdout_corpus = "\n".join(
        event.get("stdout")
        for event in events
        if isinstance(event, dict) and isinstance(event.get("stdout"), str)
    )
    steps: list[dict] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            warnings.append(f"events[{index}] is not an object; skipped")
            continue
        keys = set(event.keys())
        if {"msg", "step"} <= keys:
            _convert_step_event(event, index, steps, warnings)
        elif {"prompt", "response"} <= keys:
            _convert_episode_event(event, index, steps, warnings)
        elif "stdout" in keys or ("text" in keys and "command" in keys):
            _convert_forgecode_event(event, index, steps, warnings)
        elif "text" in keys and "content" not in keys:
            _convert_text_event(event, index, steps, warnings, stdout_corpus=stdout_corpus)
        elif "content" in keys:
            _convert_content_event(event, index, steps, warnings)
        else:
            warnings.append(f"events[{index}] has unhandled keys {sorted(keys)}; skipped")
    return steps


def _convert_content_event(event: dict, index: int, steps: list[dict], warnings: list[str]) -> None:
    field = f"events[{index}].content"
    role = event.get("role")
    content = text_of(event.get("content"))
    if role == "tool":
        ids = event.get("tool_call_ids") or []
        call_id = str(ids[0]) if ids else None
        lines = content.split("\n")
        _attach_observation(
            steps,
            content,
            field=field,
            line_start=1,
            line_end=len(lines) or 1,
            source_call_id=call_id,
            warnings=warnings,
        )
        return
    source = _map_role(role, field=field, warnings=warnings)
    message = content
    action = event.get("action") if isinstance(event.get("action"), str) else None
    if source == "agent" and action and action not in content:
        # The executed command lives in ``action`` (and in tool_calls
        # arguments); inline it so the cheat step stays text-searchable.
        message = f"{content}\n\n[action]\n{action}" if content else f"[action]\n{action}"
        step = _new_step(len(steps) + 1, source, message, field=field, note="action-inlined")
    else:
        step = _new_step(len(steps) + 1, source, content, field=field)
    if source == "agent":
        message_type = event.get("message_type")
        if message_type == "action" or action:
            calls = parse_openai_tool_calls(event.get("tool_calls"))
            if action and not calls:
                calls = [
                    {
                        "tool_call_id": f"action_{len(steps)}",
                        "function_name": "bash",
                        "arguments": {"command": action},
                    }
                ]
            if calls:
                step["tool_calls"] = calls
        elif event.get("tool_calls"):
            step["tool_calls"] = parse_openai_tool_calls(event.get("tool_calls"))
    steps.append(step)


def _convert_step_event(event: dict, index: int, steps: list[dict], warnings: list[str]) -> None:
    """Codex/mini-swe-agent style ``{msg, obs, src, step, tools?}`` events."""
    src = event.get("src", "agent")
    source = _map_role(
        {"agent": "assistant", "user": "user"}.get(src, src),
        field=f"events[{index}].src",
        warnings=warnings,
    )
    message = event.get("msg") or ""
    step = _new_step(len(steps) + 1, source, message, field=f"events[{index}].msg")
    tools = event.get("tools") or []
    calls = []
    for position, tool in enumerate(tools):
        if not isinstance(tool, dict):
            continue
        calls.append(
            {
                "tool_call_id": f"call_{event.get('step', index)}_{position}",
                "function_name": str(tool.get("fn") or "shell"),
                "arguments": {"cmd": tool.get("cmd")},
            }
        )
    if calls:
        step["tool_calls"] = calls
    obs = event.get("obs")
    if isinstance(obs, str) and obs:
        lines = obs.split("\n")
        step["observation"] = {"results": [{"content": obs}]}
        step["extra"]["cheatbench"]["obs_refs"] = [
            {"field": f"events[{index}].obs", "line_start": 1, "line_end": len(lines) or 1}
        ]
    steps.append(step)


def _convert_episode_event(event: dict, index: int, steps: list[dict], warnings: list[str]) -> None:
    """Meta-Harness style ``{prompt, response}`` episode events."""
    prompt = event.get("prompt") or ""
    response = event.get("response") or ""
    response_text = response if isinstance(response, str) else text_of(response)
    steps.append(_new_step(len(steps) + 1, "user", prompt, field=f"events[{index}].prompt"))
    step = _new_step(len(steps) + 1, "agent", response_text, field=f"events[{index}].response")
    calls = _parse_embedded_tool_calls(response_text)
    if calls:
        step["tool_calls"] = calls
    elif "Tool Calls" in response_text:
        warnings.append(f"events[{index}].response mentions tool calls that did not parse")
    steps.append(step)


_TOOL_CALLS_JSON = re.compile(r"Tool Calls:\s*(\[.*\])", re.DOTALL)


def _parse_embedded_tool_calls(response_text: str) -> list[dict]:
    match = _TOOL_CALLS_JSON.search(response_text)
    if not match:
        return []
    try:
        return parse_openai_tool_calls(json.loads(match.group(1)))
    except ValueError:
        return []


def _convert_forgecode_event(
    event: dict, index: int, steps: list[dict], warnings: list[str]
) -> None:
    """ForgeCode ``{command, stdout}`` + ``{text}`` events (Claude renderings).

    ``text`` is the same rendered transcript as ``stdout`` minus the shell
    banner, so when it is fully contained it is not emitted twice.
    """
    stdout = event.get("stdout") or ""
    command = event.get("command") or ""
    return_code = event.get("return_code")
    if "stdout" in event:
        for segment, start, end in _split_rendered_log(stdout):
            step = _new_step(
                len(steps) + 1,
                "agent",
                segment,
                field=f"events[{index}].stdout",
                line_start=start,
                line_end=end,
            )
            if start == 1 and command:
                step["extra"]["cheatbench"]["shell_command"] = command
                step["extra"]["cheatbench"]["return_code"] = return_code
            steps.append(step)
    if "text" in event:
        _convert_text_event(event, index, steps, warnings, stdout_corpus=stdout)


def _convert_text_event(
    event: dict, index: int, steps: list[dict], warnings: list[str], *, stdout_corpus: str
) -> None:
    """ForgeCode ``{id, text, type}`` events: skip when stdout already holds them.

    The skipped text is recorded as a line-offset alias on the first stdout
    step so localizations against ``events[i].text`` still map (cb-001975).
    """
    text = event.get("text")
    field = f"events[{index}].text"
    if not isinstance(text, str) or not text:
        warnings.append(f"{field} is empty; skipped")
        return
    if text in stdout_corpus:
        line_offset = stdout_corpus[: stdout_corpus.find(text)].count("\n")
        for step in steps:
            ref = step["extra"]["cheatbench"]["source_ref"]
            if ref["field"].endswith(".stdout"):
                step["extra"]["cheatbench"].setdefault("text_aliases", []).append(
                    {"field": field, "line_offset": line_offset}
                )
                warnings.append(f"{field} is contained in {ref['field']}; emitted once")
                return
        warnings.append(f"{field} is contained in stdout but no stdout step exists; emitting")
    for segment, start, end in _split_rendered_log(text):
        steps.append(
            _new_step(len(steps) + 1, "agent", segment, field=field, line_start=start, line_end=end)
        )


def _split_rendered_log(stdout: str) -> list[tuple[str, int, int]]:
    """Split a Claude rendered transcript on ``⏺`` markers (1-based lines)."""
    lines = stdout.split("\n")
    starts = [n for n, line in enumerate(lines) if line.startswith("⏺ ")]
    if not starts:
        return [(stdout, 1, len(lines) or 1)] if stdout else []
    boundaries = [0] + starts[1:] + [len(lines)]
    segments = []
    for start, end in zip(boundaries[:-1], boundaries[1:], strict=True):
        body = "\n".join(lines[start:end]).strip("\n")
        if body:
            segments.append((body, start + 1, end))
    return segments


_LOG_TURN = re.compile(r"^\[(\d+)\]\s+role=(system|assistant|tool)\b")
_LOG_CALL = re.compile(r"^TOOL_CALL\[(\d+)\]\s+(\w+):\s*(.*)$", re.DOTALL)
_LOG_PREFIX = re.compile(r"^\[\d+\]\s+")


def _convert_prefixed_log(content: str, field: str, warnings: list[str]) -> list[dict]:
    """SWE-rebench single-log ``[NNNN] role=X`` transcripts."""
    steps: list[dict] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        role = current["role"]
        text = "\n".join(current["lines"])
        if role == "tool":
            _attach_observation(
                steps,
                text,
                field=field,
                line_start=current["start"],
                line_end=current["end"],
                warnings=warnings,
            )
        else:
            step = _new_step(
                len(steps) + 1,
                _map_role(role, field=field, warnings=warnings),
                text,
                field=field,
                line_start=current["start"],
                line_end=current["end"],
            )
            if current["calls"]:
                step["tool_calls"] = current["calls"]
            steps.append(step)
        current = None

    for lineno, raw in enumerate(content.split("\n"), start=1):
        body = _LOG_PREFIX.sub("", raw)
        turn = _LOG_TURN.match(raw)
        if turn:
            flush()
            current = {
                "role": turn.group(2),
                "lines": [raw],
                "calls": [],
                "start": lineno,
                "end": lineno,
            }
            continue
        call = _LOG_CALL.match(body)
        if call and current is not None and current["role"] == "assistant":
            current["calls"].append(
                {
                    "tool_call_id": f"log_{current['start']}_{call.group(1)}",
                    "function_name": call.group(2),
                    "arguments": {"command": call.group(3)},
                }
            )
        if current is None:
            current = {
                "role": "assistant",
                "lines": [],
                "calls": [],
                "start": lineno,
                "end": lineno,
            }
        assert current is not None
        current["lines"].append(raw)
        current["end"] = lineno
    flush()
    if not steps:
        warnings.append(f"{field}: prefixed log produced no steps")
    return steps


def _step_line_span(step: dict) -> list[tuple[str, int, int]]:
    """(field, start, end) spans a step covers: own ref plus obs refs."""
    spans = []
    ref = step["extra"]["cheatbench"]["source_ref"]
    spans.append((ref["field"], ref.get("line_start"), ref.get("line_end")))
    for obs in step["extra"]["cheatbench"].get("obs_refs", []):
        spans.append((obs["field"], obs.get("line_start"), obs.get("line_end")))
    return spans


def locate_cheat(
    steps: list[dict], loc_field_path: str, loc_line_start: Any
) -> tuple[int | None, int | None, str | None]:
    """Map a CheatBench localization to (step_id, obs result idx|None, correction).

    ``correction`` is None on an exact match, else one of
    ``single-event-fallback`` (swe-rebench ``events[1]`` on 1-event traces),
    ``text-alias`` (skipped-duplicate ``events[i].text``), or
    ``event-index-fallback`` (subfields with no own span: ``tools[j].cmd``,
    ``action``).
    """
    if not loc_field_path:
        return None, None, None
    try:
        line = int(loc_line_start) if loc_line_start is not None else None
    except (TypeError, ValueError):
        line = None
    match = re.fullmatch(r"(messages|events)\[(\d+)\](\..+)?", loc_field_path)
    if match is None:
        return None, None, None
    kind, wanted, _rest = match.group(1), int(match.group(2)), match.group(3)
    correction: str | None = None
    fields = {span[0] for step in steps for span in _step_line_span(step)}
    if loc_field_path not in fields:
        aliased = _resolve_text_alias(steps, loc_field_path, line)
        if aliased is not None:
            loc_field_path, line = aliased
            correction = "text-alias"
        elif kind == "events" and wanted == 1 and f"events[0]{loc_field_path[9:]}" in fields:
            loc_field_path = f"events[0]{loc_field_path[9:]}"
            correction = "single-event-fallback"
        else:
            event_steps = [
                step["step_id"]
                for step in steps
                for field, _s, _e in _step_line_span(step)
                if field.startswith(f"events[{wanted}].")
            ]
            if kind == "events" and event_steps:
                return event_steps[0], None, "event-index-fallback"
            return None, None, None
    for step in steps:
        for position, (field, start, end) in enumerate(_step_line_span(step)):
            if field != loc_field_path:
                continue
            if line is None or start is None or end is None or start <= line <= end:
                return step["step_id"], (position - 1 if position > 0 else None), correction
    first = next(
        step["step_id"]
        for step in steps
        for field, _s, _e in _step_line_span(step)
        if field == loc_field_path
    )
    return first, None, correction


def _resolve_text_alias(
    steps: list[dict], loc_field_path: str, line: int | None
) -> tuple[str, int | None] | None:
    """Map a skipped-duplicate ``events[i].text`` line into its stdout step."""
    for step in steps:
        for alias in step["extra"]["cheatbench"].get("text_aliases", []):
            if alias["field"] != loc_field_path:
                continue
            for field, start, end in _step_line_span(step):
                if not field.endswith(".stdout"):
                    continue
                shifted = None if line is None else line + alias["line_offset"]
                if shifted is None or start is None or end is None or start <= shifted <= end:
                    return field, shifted
            for field, _s, _e in _step_line_span(step):
                if field.endswith(".stdout"):
                    return field, None
    return None


def _resolve_loc_content(trace: dict, loc_field_path: str) -> tuple[str | None, str | None]:
    """Resolve loc content, applying the single-event fallback (swe-rebench)."""
    try:
        content = resolve_field_path(trace, loc_field_path)
    except LookupError:
        content = None
    if isinstance(content, str):
        return content, loc_field_path
    match = re.fullmatch(r"events\[1\](\..+)", loc_field_path)
    if match is not None:
        fallback = f"events[0]{match.group(1)}"
        try:
            content = resolve_field_path(trace, fallback)
        except LookupError:
            content = None
        if isinstance(content, str):
            return content, fallback
    return None, None


def _anchor_line(content: str, loc_line_start: Any, loc_snippet: str | None) -> tuple[Any, bool]:
    """Calibrate the loc line by searching the snippet in the field content.

    Upstream line numbers drift when a field mixes ``\\r``/``\\n`` newlines,
    and some snippets normalize newlines to ``|`` or unescape JSON quotes;
    the snippet itself is ground truth when found verbatim under one of
    these exact-substring variants.
    """
    needle = (loc_snippet or "")[:200]
    for variant in _snippet_variants(needle):
        if variant and variant in content:
            line = content[: content.find(variant)].count("\n") + 1
            try:
                adjusted = loc_line_start is not None and int(loc_line_start) != line
            except (TypeError, ValueError):
                adjusted = True
            return line, adjusted
    return loc_line_start, False


def _snippet_variants(needle: str) -> list[str]:
    """Exact-substring search variants for upstream snippet normalizations."""
    variants = [needle]
    piped = needle.replace("|", "\n")
    if piped != needle:
        variants.append(piped)
    escaped = needle.replace('"', '\\"')
    if escaped != needle:
        variants.append(escaped)
    return variants


def message_number(steps: list[dict], step_id: int | None, result_index: int | None) -> int | None:
    """1-based transcript message number as Scout badges it.

    Scout numbers each step's own message plus one entry per structured
    ``tool_calls`` item; ``observation.results`` tool outputs render without
    a badge number, so a cheat inside one has no message number (step only).
    """
    if step_id is None or result_index is not None:
        return None
    count = 0
    for step in steps:
        if step["step_id"] == step_id:
            return count + 1
        count += 1 + len(step.get("tool_calls") or [])
    return None


_CORRECTION_NOTES = {
    "single-event-fallback": (
        "note: raw loc path events[1].content did not resolve (single-event trace); "
        "mapped to events[0].content"
    ),
    "text-alias": (
        "note: raw loc events[i].text is a duplicate of the shell stdout; "
        "mapped into the stdout steps"
    ),
    "event-index-fallback": (
        "note: raw loc subfield (tools[j].cmd/action) has no own text span; "
        "mapped to the owning event's step"
    ),
}


def build_annotation(
    row: Mapping[str, Any],
    *,
    cheat_step_id: int | None,
    cheat_message_no: int | None,
    correction: str | None,
    line_adjusted: bool,
    warnings: list[str],
    n_steps: int,
) -> str:
    """Clearly-marked locator card appended after the original trace."""
    snippet = (row.get("loc_snippet") or "")[:1000]
    lines = [
        "[CheatBench annotation — not part of the original trace]",
        f"trace {row.get('trace_id')} · benchmark {row.get('benchmark')} · "
        f"harness {row.get('harness')} · model {row.get('model')}",
        f"label: {row.get('label')} (type {row.get('cheating_type')}) · "
        f"mechanism: {row.get('cheating_mechanism')}",
    ]
    if cheat_step_id is None:
        lines.append(
            "cheat location: unlocalized — the curators record no in-trace span "
            f"(loc_kind={row.get('loc_kind')})"
        )
    else:
        lines.append(
            f"cheat location: ATIF step {cheat_step_id} of {n_steps}"
            + (f", transcript message M{cheat_message_no}" if cheat_message_no else "")
            + f" · raw loc: {row.get('loc_field_path')} "
            f"L{row.get('loc_line_start')}-L{row.get('loc_line_end')}"
        )
    if correction in _CORRECTION_NOTES:
        lines.append(_CORRECTION_NOTES[correction])
    if line_adjusted:
        lines.append(
            "note: raw loc line did not match the snippet position (mixed newlines); "
            "mapped by snippet search"
        )
    if row.get("rationale"):
        lines.append(f"rationale: {row.get('rationale')}")
    if row.get("note"):
        lines.append(f"note: {row.get('note')}")
    if snippet:
        lines.append(f"loc snippet: {snippet}")
    for warning in warnings:
        lines.append(f"converter warning: {warning}")
    return "\n".join(lines)


def locate_row(
    trace: dict, steps: list[dict], row: Mapping[str, Any]
) -> tuple[int | None, int | None, str | None, bool]:
    """Locate a row's cheat: (step_id, obs idx|None, correction|None, line_adjusted)."""
    raw_path = row.get("loc_field_path") or ""
    content, resolved_path = _resolve_loc_content(trace, raw_path)
    if content is not None:
        loc_line, line_adjusted = _anchor_line(
            content, row.get("loc_line_start"), row.get("loc_snippet")
        )
    else:
        resolved_path, loc_line, line_adjusted = raw_path, row.get("loc_line_start"), False
    cheat_step_id, result_index, correction = locate_cheat(steps, resolved_path or "", loc_line)
    if resolved_path != raw_path and correction is None and cheat_step_id is not None:
        correction = "single-event-fallback"
    return cheat_step_id, result_index, correction, line_adjusted


def convert_row(row: Mapping[str, Any]) -> tuple[dict, dict, list[str]]:
    """Convert one parquet row to (staged trajectory, scout metadata, warnings)."""
    trace = parse_raw_trace(row["trace"])
    steps, warnings = convert_trace(trace)
    cheat_step_id, result_index, correction, line_adjusted = locate_row(trace, steps, row)
    cheat_message_no = message_number(steps, cheat_step_id, result_index)
    annotation = build_annotation(
        row,
        cheat_step_id=cheat_step_id,
        cheat_message_no=cheat_message_no,
        correction=correction,
        line_adjusted=line_adjusted,
        warnings=list(warnings),
        n_steps=len(steps),
    )
    steps.append(_new_step(len(steps) + 1, "user", annotation, field="cheatbench.annotation"))
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "session_id": f"cheatbench-{row.get('trace_id')}",
        "agent": {
            "name": str(row.get("harness") or "cheatbench"),
            "version": "cheatbench-import/1",
            "model_name": row.get("model"),
        },
        "steps": steps,
        "extra": {
            "cheatbench": {
                "staged_by": _STAGED_BY,
                "trace_id": row.get("trace_id"),
                "benchmark": row.get("benchmark"),
                "label": row.get("label"),
                "cheating_type": row.get("cheating_type"),
                "cheating_mechanism": row.get("cheating_mechanism"),
                "source_uri": CARD_URL,
                "revision": REVISION,
            }
        },
    }
    label = row.get("label")
    reward = 1.0 if label == "cheating" else 0.0
    meta = {
        "task": row.get("benchmark"),
        "trial_name": row.get("trace_id"),
        "trial_id": row.get("trace_id"),
        "job": "cheatbench",
        "agent": row.get("harness"),
        "model": row.get("model"),
        "verdict": "passed" if label == "cheating" else "failed",
        "reward": reward,
        "rewards": {"reward": reward},
        "stop_reason": None,
        "source": "cheatbench:import_cheatbench.py",
        "cheatbench_label": label,
        "cheating_type": row.get("cheating_type"),
        "cheating_mechanism": row.get("cheating_mechanism"),
        "rationale": row.get("rationale"),
        "note": row.get("note"),
        "cheat_step_id": cheat_step_id,
        "cheat_message_no": cheat_message_no,
        "loc_corrected": correction is not None,
        "loc_correction": correction,
        "loc_line_adjusted": line_adjusted,
        "loc_kind": row.get("loc_kind"),
        "loc_pattern": row.get("loc_pattern"),
        "loc_blatancy": row.get("loc_blatancy"),
        "loc_field_path": row.get("loc_field_path"),
        "loc_line_start": row.get("loc_line_start"),
        "loc_line_end": row.get("loc_line_end"),
        "loc_snippet_context": row.get("loc_snippet_context"),
        "loc_evidence_source": row.get("loc_evidence_source"),
        "loc_confidence": row.get("loc_confidence"),
    }
    return trajectory, meta, warnings


def validate_staged(trajectory: Mapping[str, Any]) -> list[str]:
    """Structural check of a staged trajectory (Harbor validates for real)."""
    issues = []
    if not isinstance(trajectory, dict):
        return ["trajectory must be an object"]
    for key in ("schema_version", "session_id", "agent", "steps"):
        if key not in trajectory:
            issues.append(f"missing required key {key!r}")
    agent = trajectory.get("agent")
    if not isinstance(agent, dict) or not agent.get("name"):
        issues.append("agent.name is required")
    steps = trajectory.get("steps")
    if not isinstance(steps, list) or not steps:
        issues.append("steps must be a non-empty list")
        return issues
    for position, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            issues.append(f"step {position}: must be an object")
            continue
        if step.get("step_id") != position:
            issues.append(f"step {position}: step_id must be {position}")
        if step.get("source") not in ATIF_SOURCES:
            issues.append(f"step {position}: invalid source {step.get('source')!r}")
        if not isinstance(step.get("message"), str):
            issues.append(f"step {position}: message must be a string")
        for call in step.get("tool_calls") or []:
            for key in ("tool_call_id", "function_name", "arguments"):
                if key not in call:
                    issues.append(f"step {position}: tool_call missing {key!r}")
        results = (step.get("observation") or {}).get("results", [])
        if not isinstance(results, list):
            issues.append(f"step {position}: observation.results must be a list")
    return issues
