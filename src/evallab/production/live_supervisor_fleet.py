"""Live Supervisor source ``fleet``: the OMP lanes (herdr panes), the queue keeper and provider quotas.

Everything here is read-only toward the subjects: it lists panes, reads the tail of each
*authorized* lane's session JSONL, reads the keeper's ``_queue.log`` / ``queue*.json`` and the
``omp usage`` snapshot, and keeps one small state file of its own (quota snapshots and cached
per-lane token baselines).

Which session history is read (``authorized_lanes``, ``may_read_session``)
    Only panes the bridge run's ``queue.json`` lists as lanes, plus the panes in the agent-created
    registry ``research-context/omp/fleet.md``; never a pinned tab. Every other pane is still a
    subject, but from metadata only (status, agent, queue counts) — another chat's transcript is
    not opened, scored or quoted, whichever owner it has. Being absent from ``agent-routing.json``
    never authorizes a pane. Cached paths for lanes no longer authorized are dropped from the
    state file on the next cycle.

How a pane is mapped to its OMP session
    1. ``herdr pane list`` carries ``agent_session.value`` (the session JSONL path) for every pane
       whose agent is ``omp``; that is the authoritative join.
    2. Otherwise ``~/.config/herdr/agent-routing.json`` ``agents[*].session_ref`` for the pane id.
    A pane with neither (a plain shell, another agent) is still a subject, with
    ``actor_inputs_available=False``, so it never produces a ``clear``. ``~/.config/herdr/session.json``
    is not used: it stores herdr's internal numeric pane ids, not ``w1C:pX`` ids, and carries no owner.

How an owner is decided (``classify_owner``)
    ``peter`` when the tab label starts with the pin emoji, or the pane id is a route in
    ``agent-routing.json`` (Peter's persistent chats) and not in the agent-created registry
    ``research-context/omp/fleet.md``; ``fleet`` otherwise. A lane is never killable.

Thresholds are fixed a priori (Run Health RH-3, ``research-context/bridge/.../run-health.md``);
do not tune them on label sets.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import subprocess
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evallab.production.live_supervisor import Event, Observation, Subject, TriggerHit

SOURCE_NAME = "fleet"
KIND_LANE = "omp_lane"
KIND_KEEPER = "queue_keeper"
KIND_QUOTA = "provider_quota"
PIN = "\N{PUSHPIN}"

#: ``Runner`` runs one command and returns its stdout; it raises on failure.
Runner = Callable[[Sequence[str]], str]

# --------------------------------------------------------------------------- thresholds
#: RH-3 (run-health.md): the Run Health latency gap is 15 min; a *working* lane with no session
#: event at all for twice that is stuck (RH-3 saw one 11 h 45 min wait and one 4 h sleep, both with an open call).
SILENCE_S = 30 * 60
#: RH-3 identical-call run: >= 3 consecutive assistant tool calls with the same tool and args.
REPEAT_CALLS = 3
#: RH-3 wait-polling loop: >= 3 consecutive ``wait`` returns.
WAIT_LOOP = 3
#: RH-3 failure storm: model errors and fallback switches of one lane <= 10 min apart; 5 is the
#: tool-error-streak threshold RH-3 uses (its worst episode was 16 events in 7 min).
ERROR_WINDOW_S = 10 * 60
ERROR_COUNT = 5
#: Burn: tokens in the trailing hour against the lane's own median active hour.
BURN_WINDOW_S = 60 * 60
BURN_RATIO = 3.0
BASELINE_DAYS = 7
#: A median of fewer active hours than this is not a baseline.
MIN_BASELINE_HOURS = 6
#: Quota: points of a weekly-or-longer limit consumed within 6 h (a normal week is 14 points a day).
QUOTA_RISE_POINTS = 20.0
QUOTA_WINDOW_S = 6 * 60 * 60
QUOTA_MIN_WINDOW_S = 7 * 24 * 60 * 60
QUOTA_FETCH_INTERVAL_S = 10 * 60
#: RH-3: 78 done items had a median of 33 min and p90 of 1 h 44 min; 3 h is past p90 twice over.
WALL_TIME_S = 3 * 60 * 60
#: A lane idle with queue items pending and a keeper that has not paged it for an hour.
LANE_IDLE_S = 60 * 60
KEEPER_SILENT_S = 60 * 60
#: LS-FREEZE. A lane whose pane lost its agent (herdr ``agent_status`` ``unknown``) with an item in flight
#: and a turn still open (no final assistant stop) is stalled after 60 min without a session event. The
#: 30-min ``SILENCE_S`` is for a *working* lane herdr still sees; with the agent undetected the evidence is
#: weaker, so the bar is the keeper's own idle bar. Replayed over 1.3 days of 11 lanes (4,558 in-flight
#: lane-cycles, see research/evidence/runs/ls-freeze-20261029), the rule made 4/3/1 episodes at 30/45/60 min
#: (a long ``eval`` or ``wait``, and one 6 h stall); open ``ask`` calls and ended turns are not counted.
HEARTBEAT_STALE_S = 60 * 60
#: A supervisor cycle this much later than the previous one means the host slept or the supervisor stalled:
#: lane heartbeats and wall times that span the gap are suspect and are counted from the wake. The 94
#: recorded cycles are 123-330 s apart (median 162 s) and a cycle takes up to 187 s.
SUPERVISOR_GAP_S = 15 * 60
#: ``ps`` columns the frozen-lane probe and the keeper lookup read (``parse_process_table``).
PS_ARGV = ("ps", "-axo", "pid=,ppid=,pgid=,stat=,command=")


EVENT_WINDOW = 20
TAIL_START_BYTES = 256 * 1024
TAIL_MAX_BYTES = 16 * 1024 * 1024
BASELINE_MAX_BYTES = 32 * 1024 * 1024
BASELINE_TTL_S = 6 * 60 * 60

_NO_ARTIFACT = ("## Still Running", "Nothing to wait for", "[Uneventful result elided]")
_WAITING_STATUSES = ("idle", "done")
_TZ_HOURS = {"EDT": -4, "EST": -5, "CDT": -5, "CST": -6, "PDT": -7, "PST": -8, "UTC": 0, "GMT": 0}
_LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ([A-Z]{2,4}) (.*)$")
_DISPATCH = re.compile(r"^dispatch (\S+) (\S+)(?: status=\S+| \(self-directed\))? -> ok$")
_OUTCOME = re.compile(r"^outcome (\S+) (\S+): (done|blocked)$")
_FLEET_PANE = re.compile(r"^\|\s*`([^`|]+:[^`|]+)`\s*\|")


# --------------------------------------------------------------------------- small helpers


def run_command(argv: Sequence[str], timeout: float = 90.0) -> str:
    """The default ``Runner``: one subprocess, stdout, raises on a non-zero exit or timeout."""
    done = subprocess.run(list(argv), capture_output=True, text=True, check=True, timeout=timeout)
    return done.stdout


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _text(content: Any, limit: int = 200) -> str:
    if isinstance(content, str):
        return content[:limit]
    parts = [str(c.get("text", "")) for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
    return " ".join(parts)[:limit]


def _args_digest(args: Mapping[str, Any]) -> tuple[str, str]:
    """(stable digest, one-line summary) of tool arguments; ``i``/``title`` are intent labels, not args."""
    kept = {k: v for k, v in args.items() if k not in ("i", "title")}
    digest = hashlib.sha256(json.dumps(kept, sort_keys=True, default=str).encode()).hexdigest()[:12]
    summary = ""
    for key in ("command", "path", "pattern", "query", "code", "input"):
        if kept.get(key):
            summary = str(kept[key])
            break
    if not summary and isinstance(kept.get("tasks"), list):
        summary = "tasks:" + ",".join(str(t.get("name", "")) for t in kept["tasks"][:6] if isinstance(t, dict))
    return digest, " ".join(summary.split())[:100]


def _timeout_s(args: Mapping[str, Any]) -> float | None:
    value = args.get("timeout")
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return float(value)


# --------------------------------------------------------------------------- session records


@dataclass(frozen=True)
class _Rec:
    at: datetime
    kind: str  # call | result | assistant | model_change | user | exit
    tool: str = ""
    call_id: str = ""
    digest: str = ""
    text: str = ""
    is_error: bool = False
    stop: str = ""
    tokens: int = 0
    fallback: bool = False
    timeout_s: float | None = None

    def line(self) -> str:
        if self.kind == "call":
            return f"call {self.tool} {self.digest} {self.text}".rstrip()
        if self.kind == "result":
            return f"result {self.tool} {'ERROR ' if self.is_error else ''}{self.text}".rstrip()
        if self.kind == "assistant":
            return f"assistant stop={self.stop} tokens={self.tokens} {self.text}".rstrip()
        if self.kind == "model_change":
            return f"model_change {self.text} fallback={self.fallback}"
        return f"{self.kind} {self.text}".rstrip()


def _records(events: Sequence[Mapping[str, Any]], now: datetime) -> list[_Rec]:
    """Salient session records at or before ``now`` in file order.

    Calls come from ``tool_execution_start`` events; a call that only appears as a toolCall block of an
    assistant message (no start event anywhere in ``events``) is taken from the message instead.
    """
    started = {
        str((e.get("data") or {}).get("toolCallId"))
        for e in events
        if e.get("type") == "custom" and e.get("customType") == "tool_execution_start"
    }
    out: list[_Rec] = []
    for event in events:
        at = _ts(event.get("timestamp"))
        if at is None or at > now:
            continue
        kind = event.get("type")
        if kind == "custom" and event.get("customType") == "tool_execution_start":
            data = event.get("data") or {}
            args = data.get("args") or {}
            digest, summary = _args_digest(args)
            out.append(_Rec(at, "call", tool=str(data.get("toolName", "")), call_id=str(data.get("toolCallId", "")),
                            digest=digest, text=summary or str(data.get("intent", ""))[:100],
                            timeout_s=_timeout_s(args)))
        elif kind == "custom" and event.get("customType") == "session_exit":
            out.append(_Rec(at, "exit", text=str((event.get("data") or {}).get("reason", ""))))
        elif kind == "model_change":
            out.append(_Rec(at, "model_change", text=str(event.get("model", "")),
                            fallback=bool(event.get("resolvedModelIsFallback"))))
        elif kind == "message":
            message = event.get("message") or {}
            role = message.get("role")
            if role == "assistant":
                usage = message.get("usage") or {}
                error = str(message.get("errorMessage") or "")[:100]
                out.append(_Rec(at, "assistant", stop=str(message.get("stopReason", "")), text=error,
                                tokens=int(usage.get("totalTokens") or 0)))
                for block in message.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "toolCall":
                        continue
                    call_id = str(block.get("id", ""))
                    if call_id in started:
                        continue
                    args = block.get("arguments") or {}
                    digest, summary = _args_digest(args)
                    out.append(_Rec(at, "call", tool=str(block.get("name", "")), call_id=call_id, digest=digest,
                                    text=summary, timeout_s=_timeout_s(args)))
            elif role == "toolResult":
                out.append(_Rec(at, "result", tool=str(message.get("toolName", "")),
                                call_id=str(message.get("toolCallId", "")), is_error=bool(message.get("isError")),
                                text=" ".join(_text(message.get("content")).split())))
            elif role == "user":
                out.append(_Rec(at, "user", text=" ".join(_text(message.get("content"), 100).split())))
    return out


def _trailing_calls(recs: Sequence[_Rec]) -> tuple[str, int, tuple[int, ...]]:
    """(tool, count, record indices) of the trailing run of identical non-``wait`` calls."""
    calls = [(i, r) for i, r in enumerate(recs) if r.kind == "call"]
    if not calls or calls[-1][1].tool == "wait":
        return "", 0, ()
    last = calls[-1][1]
    run = []
    for index, rec in reversed(calls):
        if (rec.tool, rec.digest) != (last.tool, last.digest):
            break
        run.append(index)
    return last.tool, len(run), tuple(reversed(run))


def _trailing_waits(recs: Sequence[_Rec]) -> tuple[int, tuple[int, ...]]:
    """Count and record indices of the trailing ``wait`` returns that brought nothing new."""
    run = []
    for index in range(len(recs) - 1, -1, -1):
        rec = recs[index]
        if rec.kind != "result":
            continue
        if rec.tool != "wait" or not rec.text.startswith(_NO_ARTIFACT):
            break
        run.append(index)
    return len(run), tuple(reversed(run))


def _open_calls(recs: Sequence[_Rec]) -> list[_Rec]:
    closed = {r.call_id for r in recs if r.kind == "result"}
    return [r for r in recs if r.kind == "call" and r.call_id not in closed]


def _median_active_hour(recs: Sequence[_Rec], now: datetime) -> tuple[float | None, int]:
    """Median token sum over the active hour-buckets before the trailing hour, within BASELINE_DAYS."""
    buckets: dict[int, int] = {}
    for rec in recs:
        age = (now - rec.at).total_seconds()
        if rec.kind != "assistant" or not rec.tokens or age <= BURN_WINDOW_S or age > BASELINE_DAYS * 86400:
            continue
        buckets[int(age // BURN_WINDOW_S)] = buckets.get(int(age // BURN_WINDOW_S), 0) + rec.tokens
    if len(buckets) < MIN_BASELINE_HOURS:
        return None, len(buckets)
    return float(statistics.median(buckets.values())), len(buckets)


# --------------------------------------------------------------------------- process table (frozen lanes)

_HEADLESS_FLAGS = frozenset({"-p", "--print"})


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    pgid: int
    state: str  # BSD ``ps`` stat: ``T`` is job-control stopped, a trailing ``+`` is the foreground group
    argv: tuple[str, ...]


def parse_process_table(text: str) -> dict[int, Proc]:
    """Rows of ``ps -axo pid=,ppid=,pgid=,stat=,command=`` by pid; malformed rows are skipped."""
    table: dict[int, Proc] = {}
    for line in text.splitlines():
        fields = line.split(None, 4)
        if len(fields) == 5 and all(f.isdigit() for f in fields[:3]):
            pid = int(fields[0])
            table[pid] = Proc(pid, int(fields[1]), int(fields[2]), fields[3], tuple(fields[4].split()))
    return table


def _interactive_omp(proc: Proc) -> bool:
    """An ``omp`` that owns a terminal; a headless ``omp -p`` worker is not a lane."""
    return bool(proc.argv) and Path(proc.argv[0]).name == "omp" and not _HEADLESS_FLAGS.intersection(proc.argv[1:])


@dataclass(frozen=True)
class LaneProcess:
    """What the process table and herdr's pane process info say about the ``omp`` of one pane."""

    probed: bool = False
    pids: tuple[int, ...] = ()
    #: ``"<name> pid <pid> state <stat>"`` of every job-control stopped process in the omp's tree.
    stopped: str | None = None
    #: ``omp`` is alive but not in the pane's foreground process group; ``None`` when that group is unknown.
    background: bool | None = None
    foreground: str = ""
    #: The session JSONL named by ``omp --resume``; a join key, never a metric.
    resume: Path | None = None

    def metrics(self) -> dict[str, Any]:
        return {"omp_probe": "ok" if self.probed else "unavailable", "omp_pids": list(self.pids),
                "omp_stopped": self.stopped, "omp_background": self.background, "pane_foreground": self.foreground}


def lane_process(shell_pid: int, foreground_pgid: int | None, foreground: Sequence[str],
                 table: Mapping[int, Proc]) -> LaneProcess:
    """The pane's interactive ``omp`` processes (descendants of the shell) and whether they are frozen (pure)."""
    children: dict[int, list[int]] = {}
    for proc in table.values():
        children.setdefault(proc.ppid, []).append(proc.pid)
    omps: list[Proc] = []
    seen: set[int] = set()
    pending = list(children.get(shell_pid, ()))
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if _interactive_omp(table[pid]):
            omps.append(table[pid])  # its own tree is searched below, not for more lanes
        else:
            pending.extend(children.get(pid, ()))
    stopped: list[str] = []
    for omp in sorted(omps, key=lambda p: p.pid):
        tree, walk = {omp.pid}, [omp.pid]
        while walk:
            for child in children.get(walk.pop(), ()):
                if child not in tree:
                    tree.add(child)
                    walk.append(child)
        stopped += [f"{Path(table[p].argv[0]).name if table[p].argv else '?'} pid {p} state {table[p].state}"
                    for p in sorted(tree) if table[p].state.startswith("T")]
    background = None if foreground_pgid is None and omps else any(p.pgid != foreground_pgid for p in omps)
    resume = next((Path(argv[i + 1]) for p in omps for argv in (p.argv,) for i, a in enumerate(argv[:-1])
                   if a == "--resume" and argv[i + 1].endswith(".jsonl")), None)
    return LaneProcess(True, tuple(sorted(p.pid for p in omps)), "; ".join(stopped) or None, background,
                       ",".join(foreground), resume)


# --------------------------------------------------------------------------- lane observation


@dataclass(frozen=True)
class LaneQueue:
    """What the keeper's files say about one lane."""

    pending: int = 0
    open_item: str | None = None
    open_item_dispatched_at: datetime | None = None


def lane_observation(events: Sequence[Mapping[str, Any]], *, subject: Subject, now: datetime,
                     agent_status: str = "working", median_hourly_tokens: float | None = None,
                     queue: LaneQueue | None = None, activity_at: datetime | None = None,
                     tail_truncated: bool = False, awake_since: datetime | None = None) -> Observation:
    """The lane as it looked at ``now``, from raw OMP session JSONL records (pure; no I/O).

    Records after ``now`` are ignored, so a historical session can be replayed at any simulated
    time. ``Event.index`` is the ordinal of the record among the salient records of ``events``
    (tool calls, tool results, assistant messages, model changes, user messages, session exit).
    ``median_hourly_tokens`` is the lane's baseline; when omitted it is the median active hour
    of ``events`` themselves before the trailing hour (``None`` below MIN_BASELINE_HOURS hours).
    ``activity_at`` is later activity that is not in ``events`` (a subagent's session file).
    ``awake_since`` is the last host wake or supervisor stall: a heartbeat or dispatch older than it is
    counted from it, because nothing ran in between (``SUPERVISOR_GAP_S``).
    """
    recs = _records(events, now)
    awake = awake_since if awake_since is not None and awake_since <= now else None
    last_at = recs[-1].at if recs else None
    suspect = awake is not None and (last_at is None or last_at < awake)
    counted_from = max(last_at, awake) if last_at and awake else last_at
    age = (now - counted_from).total_seconds() if counted_from else None
    since = now - timedelta(seconds=ERROR_WINDOW_S)
    errors = [i for i, r in enumerate(recs) if r.at >= since and r.kind == "assistant" and r.stop == "error"]
    fallbacks = [i for i, r in enumerate(recs) if r.at >= since and r.kind == "model_change" and r.fallback]
    hour = now - timedelta(seconds=BURN_WINDOW_S)
    tokens = sum(r.tokens for r in recs if r.kind == "assistant" and r.at > hour)
    baseline, baseline_hours = (median_hourly_tokens, -1) if median_hourly_tokens is not None \
        else _median_active_hour(recs, now)
    repeat_tool, repeat_n, repeat_steps = _trailing_calls(recs)
    wait_n, wait_steps = _trailing_waits(recs)
    open_calls = _open_calls(recs)
    excused = [c for c in open_calls
               if c.timeout_s is not None and (now - c.at).total_seconds() < c.timeout_s]
    activity_age = min(age, max(0.0, (now - activity_at).total_seconds())) \
        if age is not None and activity_at is not None and activity_at <= now else age
    metrics: dict[str, Any] = {
        "agent_status": agent_status,
        "last_event_age_s": age,
        "last_activity_age_s": activity_age,
        "last_event_index": len(recs) - 1 if recs else None,
        "open_calls": [f"{c.tool}@{c.at.isoformat()}" for c in open_calls],
        "silence_excused": bool(excused),
        "identical_call_tool": repeat_tool,
        "identical_call_run": repeat_n,
        "identical_call_steps": repeat_steps,
        "wait_no_artifact_run": wait_n,
        "wait_no_artifact_steps": wait_steps,
        "errors_10min": len(errors),
        "fallbacks_10min": len(fallbacks),
        "error_steps": tuple(errors + fallbacks),
        "tokens_last_hour": tokens,
        "median_hourly_tokens": baseline,
        "baseline_hours": baseline_hours,
        "tail_truncated": tail_truncated,
        "turn_open": bool(recs) and (recs[-1].kind in ("call", "result", "user")
                                     or (recs[-1].kind == "assistant" and recs[-1].stop == "toolUse")),
        "awaiting_human": any(c.tool == "ask" for c in open_calls),
        "heartbeat_suspect": suspect,
        "queue_pending": queue.pending if queue else 0,
        "queue_open_item": queue.open_item if queue else None,
        "queue_open_item_age_s": (now - (max(queue.open_item_dispatched_at, awake) if awake
                                         else queue.open_item_dispatched_at)).total_seconds()
        if queue and queue.open_item_dispatched_at else None,
    }
    window = recs[-EVENT_WINDOW:]
    base = len(recs) - len(window)
    return Observation(
        subject=subject, observed_at=now,
        events=tuple(Event(base + i, r.at, SOURCE_NAME, r.line()) for i, r in enumerate(window)),
        metrics=metrics, actor_inputs_available=bool(recs),
        terminal=bool(recs) and recs[-1].kind == "exit",
    )


# --------------------------------------------------------------------------- triggers


def _frozen_hits(m: Mapping[str, Any]) -> list[TriggerHit]:
    """LS-FREEZE ``frozen`` hits (kind ``infra``): (a) omp or a process in its tree is job-control stopped,
    (b) omp is alive outside its pane's foreground process group, (c) the session heartbeat of an item in
    flight is stale, (d) the supervisor's own previous cycle is far older than its cadence."""
    hits: list[TriggerHit] = []
    last = m.get("last_event_index")
    last_steps = (last,) if isinstance(last, int) else ()
    if m.get("omp_stopped") or m.get("omp_background"):
        why = []
        if m.get("omp_stopped"):
            why.append(f"job-control stopped ({m['omp_stopped']})")
        if m.get("omp_background"):
            why.append(f"alive outside the pane foreground (foreground: {m.get('pane_foreground') or 'unknown'})")
        pids = ",".join(str(p) for p in m.get("omp_pids") or ())
        hits.append(TriggerHit("frozen", "infra", 1.0, f"omp pid {pids} " + " and ".join(why), last_steps))
    activity = m.get("last_activity_age_s")
    if (m.get("agent_status") == "unknown" and m.get("queue_open_item") and m.get("turn_open")
            and not m.get("silence_excused") and not m.get("awaiting_human")
            and activity is not None and activity >= HEARTBEAT_STALE_S):
        hits.append(TriggerHit("frozen", "infra", activity / HEARTBEAT_STALE_S,
                               f"pane agent status unknown, item {m['queue_open_item']} in flight and the turn "
                               f"open, but no session event for {activity / 60:.0f} min", last_steps))
    gap = m.get("supervisor_gap_s")
    if gap is not None and gap >= SUPERVISOR_GAP_S and (m.get("queue_open_item") or m.get("agent_status") == "working"):
        hits.append(TriggerHit("frozen", "infra", gap / SUPERVISOR_GAP_S,
                               f"supervisor's previous cycle was {gap / 60:.0f} min ago (host sleep or stall): "
                               f"this lane's heartbeat and wall time are suspect and counted from now", ()))
    return hits


def _lane_hits(m: Mapping[str, Any]) -> list[TriggerHit]:
    hits: list[TriggerHit] = _frozen_hits(m)
    status = m.get("agent_status")
    age = m.get("last_event_age_s")
    activity = m.get("last_activity_age_s")
    last = m.get("last_event_index")
    last_steps = (last,) if isinstance(last, int) else ()
    if status == "working" and activity is not None and activity >= SILENCE_S and not m.get("silence_excused"):
        hits.append(TriggerHit("silence", "latency", activity / SILENCE_S,
                               f"agent working with no session event for {activity / 60:.0f} min "
                               f"(open calls: {', '.join(m.get('open_calls') or []) or 'none'})", last_steps))
    elif (status in _WAITING_STATUSES and m.get("queue_pending") and age is not None and age >= LANE_IDLE_S):
        hits.append(TriggerHit("silence", "other", age / LANE_IDLE_S,
                               f"pane {status} for {age / 60:.0f} min with {m['queue_pending']} queue item(s) "
                               f"pending", last_steps))
    if m.get("identical_call_run", 0) >= REPEAT_CALLS:
        n = m["identical_call_run"]
        hits.append(TriggerHit("repetition", "loop", n / REPEAT_CALLS,
                               f"{n} identical consecutive {m.get('identical_call_tool')} calls",
                               tuple(m.get("identical_call_steps") or ())))
    elif m.get("wait_no_artifact_run", 0) >= WAIT_LOOP:
        n = m["wait_no_artifact_run"]
        hits.append(TriggerHit("repetition", "loop", n / WAIT_LOOP,
                               f"{n} consecutive wait returns with no new artifact",
                               tuple(m.get("wait_no_artifact_steps") or ())))
    storm = m.get("errors_10min", 0) + m.get("fallbacks_10min", 0)
    if storm >= ERROR_COUNT:
        hits.append(TriggerHit("error_signature", "infra", storm / ERROR_COUNT,
                               f"{m.get('errors_10min', 0)} model errors and {m.get('fallbacks_10min', 0)} "
                               f"fallback switches within 10 min", tuple(m.get("error_steps") or ())))
    tokens, median = m.get("tokens_last_hour", 0), m.get("median_hourly_tokens")
    if median and tokens >= BURN_RATIO * median:
        hits.append(TriggerHit("burn", "runaway", tokens / (BURN_RATIO * median),
                               f"{tokens:,} tokens in the last hour vs lane median {median:,.0f}/h", ()))
    open_age = m.get("queue_open_item_age_s")
    if open_age is not None and open_age >= WALL_TIME_S:
        hits.append(TriggerHit("wall_time", "runaway", open_age / WALL_TIME_S,
                               f"queue item {m.get('queue_open_item')} dispatched {open_age / 3600:.1f} h ago "
                               f"with no outcome line", ()))
    return hits


def _keeper_hits(m: Mapping[str, Any]) -> list[TriggerHit]:
    if not m.get("pending_total"):
        return []
    age = m.get("log_age_s")
    if not m.get("process_running") and not m.get("stopped_cleanly"):
        score = max(1.0, (age or 0) / KEEPER_SILENT_S)
        return [TriggerHit("silence", "other", score,
                           f"no queue_keeper.py process for run {m.get('run')} with "
                           f"{m['pending_total']} item(s) pending", ())]
    if age is not None and age >= KEEPER_SILENT_S:
        return [TriggerHit("silence", "other", age / KEEPER_SILENT_S,
                           f"_queue.log silent for {age / 60:.0f} min with {m['pending_total']} item(s) pending", ())]
    return []


def _quota_hits(m: Mapping[str, Any]) -> list[TriggerHit]:
    rise = m.get("rise_points")
    if rise is not None and rise > QUOTA_RISE_POINTS:
        return [TriggerHit("burn", "runaway", rise / QUOTA_RISE_POINTS,
                           f"{m.get('window')} usage rose {rise:.1f} points in 6 h (now {m.get('used_points'):.1f}%)",
                           ())]
    return []


def fleet_triggers(observation: Observation) -> list[TriggerHit]:
    """Hits that fired for one fleet observation; pure function of ``observation.metrics``."""
    kind = observation.subject.kind
    if kind == KIND_LANE:
        return _lane_hits(observation.metrics)
    if kind == KIND_KEEPER:
        return _keeper_hits(observation.metrics)
    if kind == KIND_QUOTA:
        return _quota_hits(observation.metrics)
    return []


# --------------------------------------------------------------------------- session file reading


def read_session_tail(path: Path, *, since: datetime, min_records: int = 60,
                      max_bytes: int = TAIL_MAX_BYTES) -> tuple[list[dict[str, Any]], bool]:
    """Records of the end of a session JSONL, in file order, reading backwards from EOF.

    Reads a doubling window until it reaches ``since`` with at least ``min_records`` parsed lines,
    the start of the file, or ``max_bytes``. Returns (records, truncated) where ``truncated`` means
    the cap was hit before ``since``.
    """
    size = path.stat().st_size
    window = TAIL_START_BYTES
    with path.open("rb") as handle:
        while True:
            start = max(0, size - window)
            handle.seek(start)
            lines = handle.read(size - start).decode("utf-8", errors="replace").splitlines()
            if start > 0:
                lines = lines[1:]  # the first line is cut mid-record
            records = [rec for rec in map(_load_line, lines) if rec is not None]
            earliest = next((t for t in (_ts(r.get("timestamp")) for r in records) if t), None)
            reached = start == 0 or (earliest is not None and earliest <= since and len(records) >= min_records)
            if reached:
                return records, False
            if window >= max_bytes:
                return records, True
            window *= 2


def _load_line(line: str) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def hourly_baseline(path: Path, now: datetime) -> tuple[float | None, int]:
    """Median active-hour tokens of a session file over its last BASELINE_DAYS (reads <= 32 MiB)."""
    size = path.stat().st_size
    start = max(0, size - BASELINE_MAX_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        lines = handle.read().decode("utf-8", errors="replace").splitlines()
    if start > 0:
        lines = lines[1:]
    assistants = [r for r in (_load_line(line) for line in lines if '"role":"assistant"' in line) if r]
    return _median_active_hour(_records(assistants, now), now)


def latest_subagent_activity(session_path: Path) -> datetime | None:
    """Newest mtime of the subagent session files stored beside ``session_path`` (``<stem>/*.jsonl``)."""
    root = session_path.with_suffix("")
    newest = 0.0
    for candidate in [*root.glob("*.jsonl"), *root.glob("*/*.jsonl")]:
        try:
            newest = max(newest, candidate.stat().st_mtime)
        except OSError:  # the subagent file was removed between listing and stat
            continue
    return datetime.fromtimestamp(newest, UTC) if newest else None


# --------------------------------------------------------------------------- keeper files


@dataclass(frozen=True)
class KeeperLog:
    lines: tuple[tuple[datetime, str], ...]
    #: pane -> (item id, dispatched at) for the last dispatch with no later outcome or dispatch.
    open_items: Mapping[str, tuple[str, datetime]]
    stopped_cleanly: bool


def parse_queue_log(text: str) -> KeeperLog:
    lines: list[tuple[datetime, str]] = []
    open_items: dict[str, tuple[str, datetime]] = {}
    for raw in text.splitlines():
        match = _LOG_LINE.match(raw)
        hours = _TZ_HOURS.get(match.group(2)) if match else None
        if match is None or hours is None:
            continue
        at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=hours)))
        body = match.group(3)
        lines.append((at, body))
        if dispatched := _DISPATCH.match(body):
            open_items[dispatched.group(1)] = (dispatched.group(2), at)
        elif (outcome := _OUTCOME.match(body)) and open_items.get(outcome.group(1), ("",))[0] == outcome.group(2):
            del open_items[outcome.group(1)]
    stopped = bool(lines) and lines[-1][1].startswith("queue keeper stop")
    return KeeperLog(tuple(lines), open_items, stopped)


def pending_by_pane(queue: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, int]:
    """Queue items still to run per pane: the open current item plus the undispatched rest.

    Items whose ledger outcome is done or blocked are closed; a lane the keeper parked is skipped.
    """
    out: dict[str, int] = {}
    for pane, cfg in (queue.get("panes") or {}).items():
        s = state.get(pane) or {}
        if s.get("parked"):
            continue
        closed = {r.get("id") for r in (s.get("ledger") or {}).values() if r.get("outcome") in ("done", "blocked")}
        items = list(cfg.get("items") or []) + list(s.get("extra") or [])
        index = int(s.get("index", -1))
        out[pane] = sum(1 for item in items[max(index, 0):] if item.get("id") not in closed)
    return out


# --------------------------------------------------------------------------- ownership


def fleet_registry_panes(text: str) -> frozenset[str]:
    """Pane ids in the first column of ``research-context/omp/fleet.md`` (agent-created tabs)."""
    return frozenset(m.group(1) for line in text.splitlines() if (m := _FLEET_PANE.match(line)))


def classify_owner(pane_id: str, label: str, *, routed_panes: frozenset[str], fleet_panes: frozenset[str]) -> str:
    """``peter`` for pinned tabs and Peter's routed chats, ``fleet`` for everything else."""
    if label.startswith(PIN):
        return "peter"
    if pane_id in fleet_panes:
        return "fleet"
    return "peter" if pane_id in routed_panes else "fleet"


# --------------------------------------------------------------------------- read scope


def authorized_lanes(queue_panes: Iterable[str], fleet_panes: frozenset[str]) -> frozenset[str]:
    """Panes whose session history this source may read: the run's ``queue.json`` lanes plus the
    agent-created registry. Absence from ``agent-routing.json`` never authorizes a pane."""
    return frozenset(queue_panes) | fleet_panes


def may_read_session(pane_id: str, label: str, *, authorized: frozenset[str]) -> bool:
    """A lane's session is read only when it is an authorized fleet lane and not a pinned tab.

    Everything else is observed from metadata alone (status, agent, queue counts): another
    chat's transcript is never opened, scored or quoted, however it is owned.
    """
    return not label.startswith(PIN) and pane_id in authorized


# --------------------------------------------------------------------------- quota


def quota_windows(usage: Mapping[str, Any]) -> dict[str, tuple[str, float]]:
    """``{"<provider>|<limit id>": (window label, used points)}`` for weekly-or-longer limits."""
    out: dict[str, tuple[str, float]] = {}
    for report in usage.get("reports") or []:
        provider = str(report.get("provider", ""))
        for limit in report.get("limits") or []:
            window = limit.get("window") or {}
            duration = window.get("durationMs")
            label = str(limit.get("label") or window.get("label") or "")
            long_enough = (duration / 1000 >= QUOTA_MIN_WINDOW_S) if isinstance(duration, int | float) \
                else bool(re.search(r"week|month|7 day", label, re.IGNORECASE))
            fraction = (limit.get("amount") or {}).get("usedFraction")
            if provider and long_enough and isinstance(fraction, int | float):
                out[f"{provider}|{limit.get('id', label)}"] = (label, float(fraction) * 100)
    return out


def quota_rise(history: Sequence[tuple[datetime, float]], now: datetime, used: float) -> float | None:
    """Points ``used`` is above the lowest snapshot of the last QUOTA_WINDOW_S; ``None`` without history."""
    floor = [p for at, p in history if timedelta(0) <= now - at <= timedelta(seconds=QUOTA_WINDOW_S)]
    return used - min(floor) if floor else None


# --------------------------------------------------------------------------- the source


class FleetSource:
    """Source ``fleet``: one ``omp_lane`` subject per herdr pane, one ``queue_keeper``, one quota per provider.

    Every external probe goes through ``runner`` (``herdr``, ``ps``, ``omp usage``) or ``home``, so a
    test supplies both. ``FleetSource()`` is safe to construct anywhere and only touches the machine in
    ``observe``; ``triggers`` is pure.
    """

    name = SOURCE_NAME

    def __init__(self, *, runner: Runner = run_command, home: Path | None = None,
                 run_name: str = "2026-10-27-run-atlas", state_path: Path | None = None,
                 fetch_quota: bool = True) -> None:
        self.runner = runner
        self.home = home if home is not None else Path.home()
        self.run_name = run_name
        self.state_path = state_path or (
            self.home / ".local/state/evallab-prod-live/live-supervisor/fleet-state.json")
        self.fetch_quota = fetch_quota

    # -- inputs
    def _json(self, argv: Sequence[str]) -> Any:
        return json.loads(self.runner(argv))

    def _config_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def _state(self) -> dict[str, Any]:
        return self._config_json(self.state_path)

    def _save_state(self, state: Mapping[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)

    # -- observe
    def observe(self, now: datetime) -> list[Observation]:
        panes = self._json(["herdr", "pane", "list"])["result"]["panes"]
        tabs = {t["tab_id"]: t.get("label", "") for t in self._json(["herdr", "tab", "list"])["result"]["tabs"]}
        herdr = self.home / ".config/herdr"
        routing = (self._config_json(herdr / "agent-routing.json")).get("agents") or {}
        routed = frozenset(a["pane_id"] for a in routing.values() if isinstance(a, dict) and a.get("pane_id"))
        routed_session = {a["pane_id"]: a.get("session_ref") for a in routing.values()
                          if isinstance(a, dict) and a.get("pane_id")}
        fleet_doc = self.home / "Developer/research-context/omp/fleet.md"
        fleet_panes = fleet_registry_panes(fleet_doc.read_text(encoding="utf-8")) if fleet_doc.is_file() \
            else frozenset()

        table = parse_process_table(self.runner(PS_ARGV))
        keeper_process = self._keeper_run(table)
        run_name = keeper_process or self.run_name
        run_dir = self.home / "Developer/research-context/bridge" / run_name
        log = parse_queue_log((run_dir / "_queue.log").read_text(encoding="utf-8")) \
            if (run_dir / "_queue.log").is_file() else KeeperLog((), {}, False)
        pending = pending_by_pane(self._config_json(run_dir / "queue.json"),
                                  self._config_json(run_dir / "queue-state.json"))

        state = self._state()
        gap, awake_since = self._clock(state, now)
        baselines: dict[str, Any] = state.setdefault("baselines", {})
        authorized = authorized_lanes(pending, fleet_panes)
        seen: set[str] = set()
        out: list[Observation] = []
        for pane in panes:
            pane_id = pane["pane_id"]
            label = tabs.get(pane.get("tab_id"), "") or pane_id
            subject = Subject(KIND_LANE, pane_id,
                              classify_owner(pane_id, label, routed_panes=routed, fleet_panes=fleet_panes),
                              killable=False, label=label)
            status = str(pane.get("agent_status", "unknown"))
            queue = LaneQueue(pending.get(pane_id, 0), *(log.open_items.get(pane_id, (None, None))))
            proc = self._pane_process(pane_id, table)
            meta = {"agent_status": status, "agent": pane.get("agent"), "queue_pending": queue.pending,
                    "queue_open_item": queue.open_item, "supervisor_gap_s": gap, **proc.metrics(),
                    "authorized": pane_id in authorized, "session_read": False}
            may_read = may_read_session(pane_id, label, authorized=authorized)
            session = (pane.get("agent_session") or {}).get("value") if may_read else None
            session = session or (routed_session.get(pane_id) if may_read else None)
            session = session or (proc.resume if may_read else None)
            path = Path(session) if session and str(session).endswith(".jsonl") else None
            if not may_read or path is None or not path.is_file() or (pane.get("agent") != "omp" and not proc.pids):
                out.append(Observation(subject, now, (), meta, actor_inputs_available=False))
                continue
            records, truncated = read_session_tail(path, since=now - timedelta(seconds=BURN_WINDOW_S))
            median = self._baseline(baselines, path, now)
            observation = lane_observation(records, subject=subject, now=now, agent_status=status,
                                           median_hourly_tokens=median, queue=queue,
                                           activity_at=latest_subagent_activity(path), tail_truncated=truncated,
                                           awake_since=awake_since)
            seen.add(str(path))
            out.append(_with_metrics(observation, session_path=str(path), agent=pane.get("agent"),
                                     authorized=True, session_read=True, supervisor_gap_s=gap, **proc.metrics()))
        state["baselines"] = {k: v for k, v in baselines.items() if k in seen}  # never keep a path we may not read
        out.append(self._keeper_observation(now, run_name, log, pending, running=keeper_process is not None))
        if self.fetch_quota:
            out.extend(self._quota_observations(now, state))
        self._save_state(state)
        return out

    def triggers(self, observation: Observation) -> list[TriggerHit]:
        return fleet_triggers(observation)

    # -- pieces
    def _baseline(self, baselines: dict[str, Any], path: Path, now: datetime) -> float | None:
        cached = baselines.get(str(path))
        if cached and (at := _ts(cached.get("at"))) and (now - at).total_seconds() < BASELINE_TTL_S:
            return cached.get("median")
        median, hours = hourly_baseline(path, now)
        baselines[str(path)] = {"at": now.isoformat(), "median": median, "hours": hours}
        return median

    def _keeper_run(self, table: Mapping[int, Proc]) -> str | None:
        """The ``--run`` of a running ``queue_keeper.py`` (from the process table), else ``None``."""
        for proc in table.values():
            runs = [proc.argv[i + 1] for i, a in enumerate(proc.argv[:-1]) if a == "--run"]
            if len(proc.argv) > 1 and "python" in proc.argv[0].lower() and runs \
                    and any(a.endswith("queue_keeper.py") for a in proc.argv[1:]):
                return runs[0]
        return None

    def _clock(self, state: dict[str, Any], now: datetime) -> tuple[float | None, datetime | None]:
        """(gap, awake_since): the seconds since this source's previous observation when that is at least
        SUPERVISOR_GAP_S (the host slept or the supervisor stalled), else ``None``; and the persisted time
        of the last such gap. Heartbeats older than ``awake_since`` are counted from it."""
        previous = _ts(state.get("last_observed_at"))
        elapsed = (now - previous).total_seconds() if previous else 0.0
        gap = elapsed if elapsed >= SUPERVISOR_GAP_S else None
        if gap is not None:
            state["awake_since"] = now.isoformat()
        state["last_observed_at"] = now.isoformat()
        return gap, _ts(state.get("awake_since"))

    def _pane_process(self, pane_id: str, table: Mapping[int, Proc]) -> LaneProcess:
        """The pane's ``omp`` process state from herdr's process info plus the process table; a pane whose
        info cannot be read is unprobed (no frozen verdict), not an error."""
        try:
            info = self._json(["herdr", "pane", "process-info", "--pane", pane_id])["result"]["process_info"]
            shell = int(info["shell_pid"])
            group = info.get("foreground_process_group_id")
            names = [str(p.get("name", "?")) for p in info.get("foreground_processes") or ()]
            return lane_process(shell, int(group) if group is not None else None, names, table)
        except (subprocess.SubprocessError, OSError, ValueError, LookupError, TypeError):
            return LaneProcess()

    def _keeper_observation(self, now: datetime, run_name: str, log: KeeperLog, pending: Mapping[str, int],
                            *, running: bool) -> Observation:
        last_at = log.lines[-1][0] if log.lines else None
        metrics = {"run": run_name, "process_running": running,
                   "log_age_s": (now - last_at).total_seconds() if last_at else None,
                   "pending_total": sum(pending.values()), "stopped_cleanly": log.stopped_cleanly,
                   "open_items": {p: i[0] for p, i in log.open_items.items()}}
        events = tuple(Event(i, at, SOURCE_NAME, body) for i, (at, body) in enumerate(log.lines[-EVENT_WINDOW:]))
        return Observation(Subject(KIND_KEEPER, run_name, "fleet", killable=False, label="queue keeper"),
                           now, events, metrics, actor_inputs_available=bool(log.lines))

    def _quota_observations(self, now: datetime, state: dict[str, Any]) -> Iterator[Observation]:
        quota: dict[str, Any] = state.setdefault("quota", {})
        last_fetch = _ts(quota.get("fetched_at"))
        if last_fetch is None or (now - last_fetch).total_seconds() >= QUOTA_FETCH_INTERVAL_S:
            snapshot = quota_windows(json.loads(self.runner(["omp", "usage", "--json"])))
            history = quota.setdefault("history", {})
            for key, (label, used) in snapshot.items():
                entries = history.setdefault(key, {"label": label, "points": []})
                entries["label"] = label
                entries["points"].append([now.isoformat(), used])
            cutoff = now - timedelta(seconds=QUOTA_WINDOW_S)
            for entries in history.values():
                entries["points"] = [p for p in entries["points"] if (_ts(p[0]) or now) >= cutoff]
            quota["fetched_at"] = now.isoformat()
        worst: dict[str, tuple[float, str, float, int]] = {}  # provider -> (rise, window label, used, snapshots)
        for key, entries in (quota.get("history") or {}).items():
            points = [(at, float(used)) for at, used in ((_ts(p[0]), p[1]) for p in entries["points"]) if at]
            if not points:
                continue
            used = points[-1][1]
            rise = quota_rise(points[:-1], now, used)
            provider = key.split("|", 1)[0]
            candidate = (rise if rise is not None else float("-inf"), str(entries.get("label", "")), used, len(points))
            if provider not in worst or candidate[0] > worst[provider][0]:
                worst[provider] = candidate
        for provider, (rise, label, used, snapshots) in sorted(worst.items()):
            yield Observation(
                Subject(KIND_QUOTA, provider, "other", killable=False, label=label), now, (),
                {"window": label, "used_points": used, "rise_points": None if rise == float("-inf") else rise,
                 "snapshots": snapshots},
                actor_inputs_available=True)


def _with_metrics(observation: Observation, **extra: Any) -> Observation:
    return replace(observation, metrics={**observation.metrics, **extra})
