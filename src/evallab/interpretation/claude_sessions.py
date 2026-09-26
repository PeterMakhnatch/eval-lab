"""Claude Code native session lookup for run reports.

Harbor 0.21's claude-code converter drops the per-step subagent key: raw
session events carry camelCase ``agentId`` (collected only into
trajectory-level ``agent.extra.agent_ids``), while per-step ``extra`` is
written from snake_case ``event.get("agent_id")`` — absent on real sessions —
so sidechain grouping would degrade to contiguous ``is_sidechain`` runs,
conflating chronologically interleaved subagents. Two exact keys survive the
conversion: the raw ``tool_use`` block id (copied to ATIF
``tool_calls[].tool_call_id``) and the raw event timestamp (copied verbatim
to the step timestamp); the raw events holding them carry the ``agentId``.
The report rejoins sidechain steps through those keys, attributing by
timestamp only when it names exactly one agent.

Sessions are retained inside the trial (``<trial>/agent/sessions/projects/``,
mirroring the converter's ``_session_dirs`` roots). When the layout is
ambiguous (zero or several session directories — the converter itself
declines then), or a step carries no joined key, grouping falls back to
contiguous runs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def session_dirs(agent_dir: Path) -> list[Path]:
    """Session directories holding primary Claude session files.

    Mirrors Harbor 0.21 ``ClaudeCode._session_dirs``: directories under
    ``sessions/projects`` that directly contain ``*.jsonl`` (subagent-only
    ``subagents/`` parents excluded).
    """
    project_root = agent_dir / "sessions" / "projects"
    if not project_root.is_dir():
        return []
    return sorted(
        {
            found.parent
            for found in project_root.rglob("*.jsonl")
            if "subagents" not in found.parent.parts
        }
    )


def _session_events(session_dir: Path) -> Any:
    """Raw session events in converter read order (sorted files, line order)."""
    files = sorted(session_dir.glob("*.jsonl")) + sorted(
        session_dir.rglob("subagents/*.jsonl")
    )
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


def tool_agent_by_call_id(session_dir: Path) -> dict[str, str]:
    """Map raw ``tool_use`` block id to its event's ``agentId`` (first wins).

    Mirrors the converter's call key (``tool_block.get("id") or
    tool_block.get("tool_use_id")``): the same string lands in ATIF
    ``tool_calls[].tool_call_id``.
    """
    table: dict[str, str] = {}
    for event in _session_events(session_dir):
        agent = event.get("agentId")
        if not isinstance(agent, str) or not agent:
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            call_id = block.get("id") or block.get("tool_use_id")
            if isinstance(call_id, str) and call_id:
                table.setdefault(call_id, agent)
    return table


def sidechain_session_tables(
    trial_dir: Path,
) -> tuple[dict[str, str], dict[datetime, set[str]]]:
    """Both join tables for one trial, or empty tables when ambiguous.

    Exactly one session directory (mirroring the converter's own
    single-session rule) yields its tool-call → agentId and
    timestamp → {agentIds} tables; zero or several directories mean the
    report cannot attribute steps safely and callers fall back.
    """
    dirs = session_dirs(trial_dir / "agent")
    if len(dirs) != 1:
        return {}, {}
    return tool_agent_by_call_id(dirs[0]), sidechain_agents_by_timestamp(dirs[0])


def normalize_timestamp(value: datetime) -> datetime:
    """Canonical join key for a step timestamp: UTC, millisecond precision."""
    utc = value.astimezone(UTC)
    return utc.replace(microsecond=(utc.microsecond // 1000) * 1000)


def _parse_raw_timestamp(value: Any) -> datetime | None:
    """Parse a raw session timestamp the way the report parses ATIF ones."""
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


def sidechain_agents_by_timestamp(session_dir: Path) -> dict[datetime, set[str]]:
    """Map normalized sidechain-event timestamp to its agent ids.

    The converter copies the raw event timestamp verbatim to the step
    timestamp, so a step joins here when its timestamp names exactly one
    agent; shared timestamps stay ambiguous and the caller falls back.
    """
    table: dict[datetime, set[str]] = {}
    for event in _session_events(session_dir):
        if event.get("isSidechain") is not True:
            continue
        agent = event.get("agentId")
        if not isinstance(agent, str) or not agent:
            continue
        stamp = _parse_raw_timestamp(event.get("timestamp"))
        if stamp is None:
            continue
        table.setdefault(normalize_timestamp(stamp), set()).add(agent)
    return table
