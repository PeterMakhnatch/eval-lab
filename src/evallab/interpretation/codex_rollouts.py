"""Codex native rollout reader: child agent threads Harbor's converter drops.

Harbor 0.21's codex converter (``harbor/agents/installed/codex.py``,
``_convert_events_to_trajectory``) converts exactly one rollout file -- the
lexically greatest ``rollout-*.jsonl`` in the most recent
``agent/sessions/<YYYY>/<MM>/<DD>/`` directory -- into the ATIF trajectory,
and it never emits ``subagent_trajectory_ref`` entries: a ``spawn_agent`` tool
call converts like any other function call. Child threads therefore vanish
from the trajectory, and the run report can only say ``delegations_only``.

This module re-reads the retained native rollouts so those children (and
their token spend) become visible again. Record shapes below are verified
against real retained rollouts (codex CLI 0.147.0, e.g.
``runs/canary-event-summary-codex-20260815/.../agent/sessions/2026/08/15/
rollout-*.jsonl``), Harbor 0.21's converter semantics, and the codex source
(openai/codex commit 25270df2615eb4da5b9d4a9a392226933fb096c5, 2026-09-26,
shallow clone at /Users/petermakhnatch/Developer/.sources/codex):

- one JSON object per line; line ``type`` is ``session_meta``,
  ``response_item``, ``event_msg``, ``turn_context`` (or ``world_state``);
- ``session_meta.payload`` carries the thread id (``id``; ``session_id`` is
  the *root* thread's id), ``cli_version``, ``originator``, ``source`` and
  ``thread_source``;
- ``source`` serializes codex's ``SessionSource`` enum
  (``codex-rs/protocol/src/protocol.rs``): top-level threads are plain
  strings (``"exec"``, ``"cli"``, ...); a spawned child is
  ``{"subagent": {"thread_spawn": {"parent_thread_id": "<uuid>",
  "depth": N, "agent_path": ..., "agent_nickname": ...,
  "agent_role": ...}}}`` (externally tagged serde; verified wire example in
  ``codex-rs/cli/src/doctor/thread_inventory.rs::source_category``).
  ``thread_source`` is ``"user"`` for top-level threads, ``"subagent"`` for
  spawned children (``ThreadSource``, same protocol file);
- a file counts as a child thread ONLY for a ``thread_spawn`` source with a
  ``parent_thread_id`` -- the real parent edge (``SessionSource::
  parent_thread_id``, used by ``core/src/hook_runtime.rs`` for
  SubagentStop). Any other extra rollout (retry, second ``codex exec``,
  resume, review/compact threads) is reported under ``other_threads``,
  never as delegated spend;
- tool calls are ``response_item`` payloads of type ``function_call`` /
  ``custom_tool_call`` paired with ``function_call_output`` /
  ``custom_tool_call_output`` by ``call_id``;
- every model API call is closed by an ``event_msg`` of type
  ``token_count`` whose ``info.last_token_usage`` meters that call and
  ``info.total_token_usage`` snapshots the thread's cumulative totals
  (``input_tokens``, ``cached_input_tokens``, ``output_tokens``,
  ``reasoning_output_tokens``); the payload carries no cost field;
- ``turn_context.payload.model`` names the thread's model;
- child threads persist as sibling ``rollout-<timestamp>-<thread-id>.jsonl``
  files in the same session date directory (the filename pattern is
  Harbor's ``_ROLLOUT_FILENAME_RE``).

Rules this reader keeps:

- Deterministic: sorted file order, no wall clock, no network, no model
  calls. Files stream line by line; only short spawn-related outputs are
  retained (capped), so large ``exec`` outputs never inflate memory.
- Missing data stays ``None`` with a reason -- never a fabricated zero. In
  particular ``cost_usd`` is always ``None``: codex ``token_count`` events
  carry no cost, and inventing one from a price table is another slice's
  job (HAR-76 item 3).
- Child token totals come from the cumulative ``total_token_usage``
  snapshots (maximum), never from summing per-call ``last_token_usage``:
  summing would double-count retried or re-reported calls. The caller
  (``run_report._subagents``) reports child tokens per child only; parent
  run totals are untouched, so nothing is double-counted into the run.
- Parent linkage is the ``parent_thread_id`` edge from the child's own
  ``session_meta``. The ``spawn_agent``-call linkage (``spawn_call_id``) is
  kept ONLY for the source-proven shape: V1 spawn outputs are
  ``{"agent_id": "<child thread id>", "nickname": ...}``
  (``core/src/tools/handlers/multi_agents/spawn.rs``); V2 outputs carry
  only ``task_name``/``nickname``
  (``core/src/tools/handlers/multi_agents_v2/spawn.rs``) and can never
  link, so such children keep ``spawn_call_id None`` rather than a guess.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Native codex rollout filename pattern (Harbor 0.21 ``_ROLLOUT_FILENAME_RE``).
ROLLOUT_FILENAME_RE = re.compile(r"^rollout-(\d{4})-(\d{2})-(\d{2})T\S+\.jsonl$")

#: Where Harbor retains codex rollouts inside a trial directory.
SESSIONS_PREFIX = ("agent", "sessions")

#: Cap on retained output text per tool call (spawn outputs are short status
#: payloads; this keeps large ``exec`` outputs out of memory).
_OUTPUT_RETAIN_CHARS = 4000


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


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class CodexChildThread:
    """One non-parent rollout file: a codex child agent thread."""

    thread_id: str
    model: str | None
    steps: int
    tool_calls: int
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: float | None = None
    location: str = ""
    parent_thread_id: str | None = None
    depth: int | None = None
    agent_role: str | None = None
    agent_nickname: str | None = None
    agent_path: str | None = None
    spawn_call_id: str | None = None


@dataclass(frozen=True)
class CodexRolloutRead:
    """Outcome of scanning a trial's retained codex rollouts."""

    status: str  # "children" | "no_children" | "absent" | "unreadable"
    reason: str | None = None
    parent: str | None = None
    files: tuple[dict[str, Any], ...] = ()
    children: tuple[CodexChildThread, ...] = ()
    other_threads: tuple[dict[str, Any], ...] = ()
    parent_spawns: tuple[str, ...] = ()


@dataclass
class _FileRead:
    path: Path
    relative: str
    session_id: str | None = None
    thread_id: str | None = None
    thread_source: str | None = None
    source_summary: str | None = None
    spawn_edge: dict[str, Any] | None = None
    agent_nickname: str | None = None
    agent_role: str | None = None
    agent_path: str | None = None
    model: str | None = None
    error: str | None = None
    token_totals: dict[str, int] = field(default_factory=dict)
    metered_calls: int = 0
    tool_calls: int = 0
    earliest: datetime | None = None
    latest: datetime | None = None
    stamp_count: int = 0
    spawn_call_ids: list[str] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)

def _iter_events(path: Path) -> Any:
    """Yield parsed JSON objects from a rollout file, skipping bad lines."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event

def _output_text(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    text = output if isinstance(output, str) else json.dumps(output, default=str)
    return text[:_OUTPUT_RETAIN_CHARS]


def _harvest_spawn_outputs(path: Path, wanted: set[str]) -> dict[str, str]:
    """Second streaming pass: output text for spawn calls seen before their call record."""
    found: dict[str, str] = {}
    for event in _iter_events(path):
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") not in ("function_call_output", "custom_tool_call_output"):
            continue
        call_id = payload.get("call_id")
        if isinstance(call_id, str) and call_id in wanted and call_id not in found:
            found[call_id] = _output_text(payload)
            if len(found) == len(wanted):
                break
    return found


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _classify_source(payload: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Split a session_meta source into a summary and a ThreadSpawn edge.

    Mirrors codex's ``SessionSource`` serde
    (``codex-rs/protocol/src/protocol.rs``): plain strings for top-level
    threads (``exec``, ``cli``, ...), ``{"subagent": <kind>}`` for the
    subagent family, ``{"subagent": {"thread_spawn": {...}}}`` for spawned
    children. Only the latter carries a parent edge.
    """
    source = payload.get("source")
    if isinstance(source, str):
        return (source or None), None
    if isinstance(source, dict) and set(source) == {"subagent"}:
        inner = source["subagent"]
        if isinstance(inner, str):
            return (f"subagent:{inner}" if inner else "subagent"), None
        if isinstance(inner, dict) and set(inner) == {"thread_spawn"}:
            spawn = inner["thread_spawn"]
            if isinstance(spawn, dict):
                edge = {
                    "parent_thread_id": _str(spawn.get("parent_thread_id")),
                    "depth": _int(spawn.get("depth")),
                    "agent_path": _str(spawn.get("agent_path")),
                    "agent_nickname": _str(spawn.get("agent_nickname")),
                    "agent_role": _str(spawn.get("agent_role")) or _str(spawn.get("agent_type")),
                }
                if edge["parent_thread_id"] is not None:
                    return "subagent:thread_spawn", edge
                return "subagent:thread_spawn_unlinked", None
            return "subagent:thread_spawn_malformed", None
        return "subagent:unknown", None
    if source is None:
        return None, None
    return "unparsable", None


def _scan_file(path: Path, relative: str) -> _FileRead:
    scanned = _FileRead(path=path, relative=relative)
    seen_lines = 0
    for event in _iter_events(path):
        seen_lines += 1
        etype = event.get("type")
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        stamp = _parse_ts(event.get("timestamp"))
        if stamp is not None:
            scanned.stamp_count += 1
            if scanned.earliest is None or stamp < scanned.earliest:
                scanned.earliest = stamp
            if scanned.latest is None or stamp > scanned.latest:
                scanned.latest = stamp
        if etype == "session_meta":
            thread_id = _str(payload.get("id"))
            if thread_id is not None:
                scanned.thread_id = thread_id
                scanned.session_id = thread_id
            thread_source = _str(payload.get("thread_source"))
            if thread_source is not None:
                scanned.thread_source = thread_source
            summary, edge = _classify_source(payload)
            if summary is not None:
                scanned.source_summary = summary
            if edge is not None:
                scanned.spawn_edge = edge
            for key, target in (
                ("agent_nickname", "agent_nickname"),
                ("agent_role", "agent_role"),
                ("agent_type", "agent_role"),
                ("agent_path", "agent_path"),
            ):
                value = _str(payload.get(key))
                if value is not None and getattr(scanned, target) is None:
                    setattr(scanned, target, value)
        elif etype == "turn_context":
            model = payload.get("model")
            if scanned.model is None and isinstance(model, str) and model:
                scanned.model = model
        elif etype == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            info = info if isinstance(info, dict) else {}
            last = info.get("last_token_usage")
            if isinstance(last, dict):
                scanned.metered_calls += 1
            total = info.get("total_token_usage")
            if isinstance(total, dict):
                for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
                    value = _int(total.get(key))
                    if value is not None:
                        scanned.token_totals[key] = max(value, scanned.token_totals.get(key, value))
        elif etype == "response_item":
            payload_type = payload.get("type")
            if payload_type in ("function_call", "custom_tool_call"):
                scanned.tool_calls += 1
                name = payload.get("name")
                call_id = payload.get("call_id")
                if (
                    name == "spawn_agent"
                    and isinstance(call_id, str)
                    and call_id
                    and call_id not in scanned.spawn_call_ids
                ):
                    scanned.spawn_call_ids.append(call_id)
            elif payload_type in ("function_call_output", "custom_tool_call_output"):
                call_id = payload.get("call_id")
                if (
                    isinstance(call_id, str)
                    and call_id in scanned.spawn_call_ids
                    and call_id not in scanned.outputs
                ):
                    scanned.outputs[call_id] = _output_text(payload)
    missing = {c for c in scanned.spawn_call_ids if c not in scanned.outputs}
    if missing:
        scanned.outputs.update(_harvest_spawn_outputs(path, missing))
    if seen_lines == 0:
        scanned.error = "no parseable JSONL lines"
    elif scanned.session_id is None:
        scanned.error = "no session_meta record"
    return scanned



def _summarize_child(scanned: _FileRead) -> CodexChildThread:
    assert scanned.thread_id is not None
    assert scanned.spawn_edge is not None
    edge = scanned.spawn_edge
    duration = None
    if scanned.stamp_count > 1 and scanned.earliest is not None and scanned.latest is not None:
        duration = round((scanned.latest - scanned.earliest).total_seconds(), 3)
    depth = edge.get("depth")
    return CodexChildThread(
        thread_id=scanned.thread_id,
        model=scanned.model,
        steps=scanned.metered_calls,
        tool_calls=scanned.tool_calls,
        input_tokens=scanned.token_totals.get("input_tokens"),
        output_tokens=scanned.token_totals.get("output_tokens"),
        cached_tokens=scanned.token_totals.get("cached_input_tokens"),
        reasoning_tokens=scanned.token_totals.get("reasoning_output_tokens"),
        started_at=scanned.earliest.isoformat() if scanned.earliest else None,
        ended_at=scanned.latest.isoformat() if scanned.latest else None,
        duration_seconds=duration,
        location=scanned.relative,
        parent_thread_id=_str(edge.get("parent_thread_id")),
        depth=depth if isinstance(depth, int) and not isinstance(depth, bool) else None,
        agent_role=_str(edge.get("agent_role")) or scanned.agent_role,
        agent_nickname=_str(edge.get("agent_nickname")) or scanned.agent_nickname,
        agent_path=_str(edge.get("agent_path")) or scanned.agent_path,
    )


def _summarize_other(scanned: _FileRead) -> dict[str, Any]:
    assert scanned.thread_id is not None
    return {
        "thread_id": scanned.thread_id,
        "location": scanned.relative,
        "source": scanned.source_summary,
        "thread_source": scanned.thread_source,
        "model": scanned.model,
        "steps": scanned.metered_calls,
        "tool_calls": scanned.tool_calls,
        "input_tokens": scanned.token_totals.get("input_tokens"),
        "output_tokens": scanned.token_totals.get("output_tokens"),
    }


def _spawn_agent_ids(outputs: dict[str, str]) -> dict[str, str]:
    """Map spawn call ids to child thread ids via the source-proven V1 shape.

    V1 ``spawn_agent`` outputs are ``{"agent_id": "<child thread id>",
    "nickname": ...}`` (``core/src/tools/handlers/multi_agents/spawn.rs``);
    V2 outputs carry only ``task_name``/``nickname`` and never link.
    """
    mapping: dict[str, str] = {}
    for call_id, text in outputs.items():
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            agent_id = _str(parsed.get("agent_id"))
            if agent_id is not None:
                mapping[call_id] = agent_id
    return mapping


def read_codex_rollouts(trial_dir: str | Path, *, parent_session_id: str | None = None) -> CodexRolloutRead:
    """Read retained codex rollouts under ``trial_dir`` and split parent/children.

    ``parent_session_id`` is the ATIF trajectory's session id; the rollout
    whose ``session_meta`` thread id matches it is the parent thread. Without
    a match, a lone rollout is the parent and otherwise the lexically greatest
    file is (mirroring Harbor 0.21's ``max(session_files)`` choice). Only
    ``thread_spawn`` rollouts whose ``parent_thread_id`` names the parent are
    children; every other extra rollout lands in ``other_threads``.
    """
    trial = Path(trial_dir)
    sessions = trial.joinpath(*SESSIONS_PREFIX)
    candidates = sorted(
        (p for p in sessions.rglob("rollout-*.jsonl") if p.is_file() and ROLLOUT_FILENAME_RE.match(p.name)),
        key=lambda p: p.relative_to(trial).as_posix(),
    )
    if not candidates:
        return CodexRolloutRead(status="absent", reason="no codex rollout files under agent/sessions")
    scanned = [_scan_file(p, p.relative_to(trial).as_posix()) for p in candidates]
    files = tuple(
        {
            "path": s.relative,
            "session_id": s.session_id,
            "source": s.source_summary,
            "readable": s.error is None,
            **({"error": s.error} if s.error is not None else {}),
        }
        for s in scanned
    )
    readable = [s for s in scanned if s.error is None]
    if not readable:
        names = ", ".join(s.relative for s in scanned)
        return CodexRolloutRead(
            status="unreadable",
            reason=f"{len(scanned)} rollout file(s) unreadable: {names}",
            files=files,
        )
    parent: _FileRead | None = None
    if parent_session_id:
        for s in readable:
            if s.thread_id == parent_session_id:
                parent = s
                break
    if parent is None:
        parent = readable[0] if len(readable) == 1 else max(readable, key=lambda s: s.relative)
    parent_id = parent.thread_id
    spawns = tuple(parent.spawn_call_ids)
    agent_ids = _spawn_agent_ids(parent.outputs)
    children: list[CodexChildThread] = []
    others: list[dict[str, Any]] = []
    for s in readable:
        if s is parent:
            continue
        if (
            s.spawn_edge is not None
            and parent_id is not None
            and _str(s.spawn_edge.get("parent_thread_id")) == parent_id
        ):
            child = _summarize_child(s)
            spawn_call_id = next(
                (call_id for call_id in spawns if agent_ids.get(call_id) == child.thread_id),
                None,
            )
            children.append(
                CodexChildThread(
                    thread_id=child.thread_id,
                    model=child.model,
                    steps=child.steps,
                    tool_calls=child.tool_calls,
                    input_tokens=child.input_tokens,
                    output_tokens=child.output_tokens,
                    cached_tokens=child.cached_tokens,
                    reasoning_tokens=child.reasoning_tokens,
                    started_at=child.started_at,
                    ended_at=child.ended_at,
                    duration_seconds=child.duration_seconds,
                    location=child.location,
                    parent_thread_id=child.parent_thread_id,
                    depth=child.depth,
                    agent_role=child.agent_role,
                    agent_nickname=child.agent_nickname,
                    agent_path=child.agent_path,
                    spawn_call_id=spawn_call_id,
                )
            )
        else:
            others.append(_summarize_other(s))
    if children:
        return CodexRolloutRead(
            status="children",
            parent=parent.relative,
            files=files,
            children=tuple(children),
            other_threads=tuple(others),
            parent_spawns=spawns,
        )
    if others:
        return CodexRolloutRead(
            status="no_children",
            reason="rollouts readable but no thread_spawn child of this parent; see other_threads",
            parent=parent.relative,
            files=files,
            other_threads=tuple(others),
            parent_spawns=spawns,
        )
    return CodexRolloutRead(
        status="no_children",
        reason="rollouts readable but hold a single thread; no child rollout files",
        parent=parent.relative,
        files=files,
        parent_spawns=spawns,
    )
