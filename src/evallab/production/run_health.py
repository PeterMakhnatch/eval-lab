from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.token_flow import _is_edit, _step_calls, _stitched_steps, normalized_command

DETECTOR_VERSION = "run-health/6"
KINDS = ("loop", "runaway", "latency", "infra")
AGENT_SOURCES = frozenset({"agent", "assistant"})


LOOP_COMMAND_RUN = 8
LOOP_MESSAGE_RUN = 10
LOOP_COMPLETION_CLAIMS = 5
LOOP_CYCLE_WINDOW = 12
LOOP_CYCLE_DISTINCT = 3
LOOP_FEEDBACK_REPEATS = 1

RUNAWAY_AGENT_STEPS = 250
RUNAWAY_WALL_SECONDS = 2 * 3600

LATENCY_MEDIAN_STEP_SECONDS = 60.0
LATENCY_P90_STEP_SECONDS = 300.0
LATENCY_MAX_GAP_SECONDS = 2700.0
LATENCY_SETUP_SECONDS = 900.0
LATENCY_VERIFIER_SECONDS = 900.0
LATENCY_MIN_TIMESTAMPS = 3
LATENCY_UNEXPLAINED_GAP_SECONDS = 600.0
_WAIT_COMMAND_RE = re.compile(r"\b(?:sleep|wait|timeout)\b|kill -0")

EXCEPTION_KIND: dict[str, str] = {
    "AgentTimeoutError": "runaway",
    "TrialBudgetExhaustedError": "runaway",
    "ContextLengthExceededError": "runaway",
    "MaxTurnsExceededError": "runaway",
    "LoopBreakStop": "loop",
    "AdmissionRefused": "infra",
    "DaytonaNotFoundError": "infra",
    "DaytonaConflictError": "infra",
    "SandboxBuildFailedError": "infra",
    "EnvironmentStartTimeoutError": "infra",
    "HealthcheckError": "infra",
    "ServiceUnavailableError": "infra",
    "BadGatewayError": "infra",
    "APIError": "infra",
    "APIConnectionError": "infra",
    "APITimeoutError": "infra",
    "InternalServerError": "infra",
    "RateLimitError": "infra",
    "AuthenticationError": "infra",
    "VerifierTimeoutError": "infra",
    "RewardFileNotFoundError": "infra",
    "RewardFileEmptyError": "infra",
    "VerifierOutputParseError": "infra",
    "GuardUnavailable": "infra",
    "DockerError": "infra",
    "ImageNotFound": "infra",
    "OSError": "infra",
    "ConnectionError": "infra",
}
_INFRA_EXCEPTION_SUBSTRINGS = ("daytona", "modal", "docker", "sandbox", "serviceunavailable",
                               "connection", "environment", "image")
INFRA_EXCEPTION_MESSAGE_RE = re.compile(
    r"admission refused|capacity changed before fresh environment allocation|Failed to create sandbox|"
    r"Sandbox build failed|Failed to (?:create|get) session|Healthcheck failed after|"
    r"Docker compose command failed|failed to send non-blocking keys|Failed to start tmux session|"
    r"stopped on (?:ModelQueryError|InfraError)|litellm\.\w+Error|No reward file found",
    re.IGNORECASE,
)
STOP_KIND: dict[str, str] = {
    "agent_timeout": "runaway",
    "trial_budget_exhausted": "runaway",
    "max_episodes": "runaway",
    "context_length_exceeded": "runaway",
    "max_turns": "runaway",
    "context_length": "runaway",
    "LimitsExceeded": "runaway",
    "ContextWindowExceededError": "runaway",
    "loop_break": "loop",
    "model_auth_error": "infra",
    "refused": "infra",
    "error": "infra",
    "provisional_execution_unavailable": "infra",
}
STORE_RUN_STATUSES = frozenset({"refused", "error", "provisional_execution_unavailable"})

TIMEOUT_MARKER_RE = re.compile(
    r"timed out|time ?out(?:Error| exceeded)|\bexit(?: code|ed with)?[: ]+(?:124|137|143)\b|"
    r"\"returncode\":\s*(?:124|137|143|-9|-15)\b|<returncode>(?:124|137|143|-9|-15)</returncode>|"
    r"\bKilled\b|Terminated|SIGKILL|deadline exceeded",
    re.IGNORECASE,
)

INFRA_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("route", re.compile(r"served_route_mismatch", re.IGNORECASE)),
    ("grader_unavailable", re.compile(
        r"original_grader_unavailable|reward file (?:not found|missing|empty)|"
        r"RewardFile(?:NotFound|Empty)Error|VerifierOutputParseError", re.IGNORECASE)),
    ("image", re.compile(
        r"(?:image|manifest)[^\n]{0,40}not found|pull access denied|ErrImagePull|ImagePullBackOff|"
        r"failed to (?:pull|build) image|error pulling image", re.IGNORECASE)),
    ("sandbox", re.compile(
        r"Sandbox with container ID \S+ not found|sandbox[^\n]{0,80}already shut down|"
        r"DaytonaNotFoundError|DaytonaError|sandbox (?:not found|creation failed|failed to start)|"
        r"ServiceUnavailableError|Cannot connect to the Docker daemon|admission refused|"
        r"container [^\n]{0,40}(?:not running|is not running)",
        re.IGNORECASE)),
    ("oom", re.compile(r"Out of memory|OOMKilled|oom-kill|MemoryError|Cannot allocate memory", re.IGNORECASE)),
    ("disk", re.compile(r"No space left on device|ENOSPC|Disk quota exceeded", re.IGNORECASE)),
    ("network", re.compile(
        r"Temporary failure in name resolution|Could not resolve host|Name or service not known|"
        r"Connection (?:refused|reset by peer)|Network is unreachable|getaddrinfo failed|"
        r"Max retries exceeded with url|SSL: CERTIFICATE_VERIFY_FAILED", re.IGNORECASE)),
    ("model_api", re.compile(
        r"\b429\b[^\n]{0,40}(?:Too Many Requests|rate limit)|rate limit(?:ed| exceeded)|"
        r"\b(?:502|503|504) (?:Bad Gateway|Service Unavailable|Gateway Time-?out)|"
        r"context(?:_length| length| window)[^\n]{0,20}exceeded|maximum context length", re.IGNORECASE)),
)
INFRA_TAIL_STEPS = 3

_UNDO_RE = re.compile(
    r"\bgit\s+(?:checkout\s+(?:--\s+|HEAD\s+--\s+)\S|restore\s+\S|reset\s+--hard)|"
    r"\bpatch\s+-R\b|\bcp\s+\S+\.(?:bak|orig)\s+\S|\bmv\s+\S+\.(?:bak|orig)\s+\S",
)
_COMPLETION_RE = re.compile(
    r"mark_task_complete|task_complete|the (?:task|fix) is (?:verified and |confirmed )?complete",
    re.IGNORECASE,
)
_ERROR_LINE_RE = re.compile(r"^.*(?:error|exception|traceback|failed|fatal).*$", re.IGNORECASE | re.MULTILINE)
_HEX_RE = re.compile(r"0x[0-9a-f]+|\b[0-9a-f]{8,}\b", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")
_CALL_TOKEN_RE = re.compile(r"\b[A-Z0-9_-]{6,}\b")


def _output_fingerprint(text: str) -> str:
    return _DIGITS_RE.sub("#", _CALL_TOKEN_RE.sub("#", " ".join(text.split())))


@dataclass(frozen=True)
class HealthRun:

    run_id: str
    source: str
    steps: Sequence[Mapping[str, Any]]
    meta: Mapping[str, Any] = field(default_factory=dict)


def _epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
        if number > 1e14:
            return number / 1e6
        if number > 1e11:
            return number / 1e3
        return number if number > 1e8 else None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    return None


def _dig(mapping: Any, path: str) -> Any:
    node = mapping
    for key in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _agent_steps(steps: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [s for s in steps if str(s.get("source") or "") in AGENT_SOURCES]


def _signature(step: Mapping[str, Any]) -> str:
    texts = [text for _, text in _step_calls(dict(step)) if text.strip()]
    return normalized_command(" ".join(texts)) if texts else ""


def _observation_text(step: Mapping[str, Any]) -> str:
    obs = step.get("observation")
    if isinstance(obs, Mapping):
        parts = []
        for result in obs.get("results") or []:
            content = result.get("content") if isinstance(result, Mapping) else result
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(str(c.get("text") or "") for c in content if isinstance(c, Mapping))
        return "\n".join(parts)
    return obs if isinstance(obs, str) else ""


def _step_text(step: Mapping[str, Any]) -> str:
    message = step.get("message")
    text = message if isinstance(message, str) else ""
    return f"{text}\n{_observation_text(step)}"


def _output_text(step: Mapping[str, Any]) -> str:
    if str(step.get("source") or "") in AGENT_SOURCES:
        return _observation_text(step)
    return _step_text(step)


def _error_signature(text: str) -> str:
    match = _ERROR_LINE_RE.search(text[-4000:])
    if not match:
        return ""
    line = _HEX_RE.sub("#", match.group(0).strip())
    return _DIGITS_RE.sub("#", line)[:200]


def _longest_run(values: Sequence[str], blocked: Sequence[bool] | None = None) -> tuple[int, int | None]:
    best, best_start, start = 0, None, 0
    for i in range(1, len(values) + 1):
        if i == len(values) or values[i] != values[start] or not values[i]:
            if values[start] and not (blocked and any(blocked[start:i])) and i - start > best:
                best, best_start = i - start, start
            start = i
    return best, best_start


def _cycle(signatures: Sequence[str], edits: Sequence[bool]) -> tuple[int, int | None]:
    n = LOOP_CYCLE_WINDOW
    best, at = None, None
    for start in range(0, max(0, len(signatures) - n + 1)):
        window = signatures[start : start + n]
        if any(edits[start : start + n]) or not all(window):
            continue
        distinct = len(set(window))
        if best is None or distinct < best:
            best, at = distinct, start
    return (best if best is not None else 0), at


def _edit_undo(signatures: Sequence[str], edits: Sequence[bool]) -> tuple[list[int], list[int]]:
    undos, repeats = [], []
    previous_edits: list[str] = []
    for position, sig in enumerate(signatures):
        if previous_edits and sig and _UNDO_RE.search(sig):
            undos.append(position)
        if edits[position] and sig:
            if previous_edits and previous_edits[-1] != sig and sig in previous_edits:
                repeats.append(position)
            previous_edits.append(sig)
    return undos, repeats


def _repeated_feedback(steps: Sequence[Mapping[str, Any]]) -> tuple[int, Any]:
    first = next((i for i, s in enumerate(steps) if str(s.get("source") or "") in AGENT_SOURCES), None)
    if first is None:
        return 0, None
    seen: Counter[str] = Counter()
    first_repeat = None
    for position in range(first, len(steps)):
        step = steps[position]
        if step.get("source") != "user":
            continue
        message = str(step.get("message") or "")
        seen[message] += 1
        if seen[message] == 2 and first_repeat is None:
            first_repeat = step.get("step_id", position)
    return (max(seen.values()) - 1 if seen else 0), first_repeat


def _unexplained_gap(agent: Sequence[Mapping[str, Any]]) -> tuple[float | None, Any]:
    stamps = [_epoch(s.get("timestamp")) for s in agent]
    best: float | None = None
    at = None
    for n in range(1, len(agent)):
        later, earlier = stamps[n], stamps[n - 1]
        if later is None or earlier is None:
            continue
        delta = later - earlier
        best = 0.0 if best is None else best
        previous = dict(agent[n - 1])
        commands = " ".join(text for _, text in _step_calls(previous))
        if delta <= best or _WAIT_COMMAND_RE.search(commands) or TIMEOUT_MARKER_RE.search(
                _observation_text(previous)):
            continue
        best, at = delta, agent[n].get("step_id")
    return (None if best is None else round(best, 3)), at


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def actor_inputs_available(steps: Sequence[Mapping[str, Any]]) -> bool:
    for step in _agent_steps(steps):
        message = step.get("message")
        if (isinstance(message, str) and message.strip()) or step.get("tool_calls"):
            return True
    return False


def features(run: HealthRun) -> dict[str, Any]:
    steps = list(run.steps)
    agent = _agent_steps(steps)
    meta = run.meta
    available = meta.get("actor_inputs_available")
    available = actor_inputs_available(steps) if available is None else bool(available)

    signatures = [_signature(s) for s in agent]
    repeats = [f"{sig}\x00{_output_fingerprint(_observation_text(s))}" if sig else ""
               for sig, s in zip(signatures, agent, strict=True)]
    edits = [_is_edit(dict(s))[0] for s in agent]
    messages = [str(s.get("message") or "").strip() for s in agent]
    cmd_run, cmd_at = _longest_run(repeats, edits)
    msg_run, msg_at = _longest_run(messages)
    cycle_distinct, cycle_at = _cycle(repeats, edits)
    undo_at, repeat_at = _edit_undo(signatures, edits)
    nonempty = [s for s in signatures if s]
    commands = Counter(nonempty)
    top_command_count = commands.most_common(1)[0][1] if commands else 0
    errors = Counter(e for e in (_error_signature(_observation_text(s)) for s in agent) if e)
    completions = sum(bool(_COMPLETION_RE.search(_step_text(s) + " ".join(sg for _, sg in _step_calls(dict(s)))))
                      for s in agent)
    feedback_repeats, feedback_at = _repeated_feedback(steps)
    unexplained_gap, unexplained_at = _unexplained_gap(agent)

    stamps = [(i, t) for i, s in enumerate(steps) if (t := _epoch(s.get("timestamp"))) is not None]
    agent_stamps = [t for s in agent if (t := _epoch(s.get("timestamp"))) is not None]
    step_seconds: list[float] = []
    max_gap, gap_at = None, None
    for (i0, t0), (i1, t1) in zip(stamps, stamps[1:], strict=False):
        delta = t1 - t0
        if delta < 0:
            continue
        if str(steps[i1].get("source") or "") in AGENT_SOURCES:
            step_seconds.append(delta)
        if max_gap is None or delta > max_gap:
            max_gap, gap_at = delta, steps[i1].get("step_id")
        del i0
    started, finished = _epoch(meta.get("started_at")), _epoch(meta.get("finished_at"))
    if started is not None and finished is not None and finished >= started:
        wall = finished - started
        wall_source = "run"
    elif len(stamps) >= 2 and stamps[-1][1] >= stamps[0][1]:
        wall = stamps[-1][1] - stamps[0][1]
        wall_source = "steps"
    else:
        wall, wall_source = None, None

    prompt_steps = [p for s in agent if (p := _int(_dig(s, "metrics.prompt_tokens"))) is not None]
    completion_steps = [c for s in agent if (c := _int(_dig(s, "metrics.completion_tokens"))) is not None]
    prompt_total = _int(meta.get("n_input_tokens"))
    completion_total = _int(meta.get("n_output_tokens"))
    if prompt_total is None and prompt_steps:
        prompt_total = sum(prompt_steps)
    if completion_total is None and completion_steps:
        completion_total = sum(completion_steps)

    timeout_hits = [s.get("step_id") for s in agent if TIMEOUT_MARKER_RE.search(_observation_text(s))]
    infra_hits: dict[str, list[Any]] = {}
    for position, step in enumerate(steps):
        text = _output_text(step)
        for name, pattern in INFRA_SIGNATURES:
            if pattern.search(text):
                infra_hits.setdefault(name, []).append(step.get("step_id", position))
    last_agent_index = max((i for i, s in enumerate(steps) if str(s.get("source") or "") in AGENT_SOURCES),
                           default=-1)
    tail_start = max(0, len(steps) - INFRA_TAIL_STEPS)
    infra_tail: dict[str, list[Any]] = {}
    for position in range(max(tail_start, last_agent_index), len(steps)):
        step = steps[position]
        text = _output_text(step)
        for name, pattern in INFRA_SIGNATURES:
            if pattern.search(text):
                infra_tail.setdefault(name, []).append(step.get("step_id", position))

    exc_type = meta.get("exception_type") if isinstance(meta.get("exception_type"), str) else None
    stop_reason = meta.get("stop_reason") if isinstance(meta.get("stop_reason"), str) else None
    agent_ids = [s.get("step_id") for s in agent]
    return {
        "run_id": run.run_id,
        "source": run.source,
        "actor_inputs_available": available,
        "steps": len(steps),
        "agent_steps": len(agent),
        "tool_calls": sum(len(s.get("tool_calls") or []) for s in agent),
        "edit_steps": sum(edits),
        "wall_seconds": None if wall is None else round(wall, 3),
        "wall_source": wall_source,
        "timestamped_steps": len(stamps),
        "seconds_per_agent_step_median": None if not step_seconds else round(statistics.median(step_seconds), 3),
        "seconds_per_agent_step_p90": None if not step_seconds else round(_quantile(step_seconds, 0.9) or 0, 3),
        "max_gap_seconds": None if max_gap is None else round(max_gap, 3),
        "max_gap_step": gap_at,
        "agent_timestamps": len(agent_stamps),
        "last_step_at": _epoch(steps[-1].get("timestamp")) if steps else None,
        "setup_seconds": meta.get("setup_seconds"),
        "verifier_seconds": meta.get("verifier_seconds"),
        "prompt_tokens": prompt_total,
        "completion_tokens": completion_total,
        "prompt_tokens_per_agent_step": (round(prompt_total / len(agent), 1)
                                         if prompt_total is not None and agent else None),
        "completion_tokens_per_agent_step": (round(completion_total / len(agent), 1)
                                             if completion_total is not None and agent else None),
        "max_prompt_tokens": max(prompt_steps) if prompt_steps else None,
        "max_identical_command_run": cmd_run,
        "identical_command_run_step": agent_ids[cmd_at] if cmd_at is not None else None,
        "max_identical_message_run": msg_run,
        "identical_message_run_step": agent_ids[msg_at] if msg_at is not None else None,
        "cycle_min_distinct": cycle_distinct,
        "cycle_step": agent_ids[cycle_at] if cycle_at is not None else None,
        "distinct_command_ratio": round(len(commands) / len(nonempty), 4) if nonempty else None,
        "top_command_count": top_command_count,
        "edit_undo_cycles": len(undo_at),
        "edit_undo_steps": [agent_ids[p] for p in undo_at][:5],
        "repeated_edits": len(repeat_at),
        "max_repeated_error": max(errors.values()) if errors else 0,
        "completion_claims": completions,
        "max_repeated_feedback": feedback_repeats,
        "repeated_feedback_step": feedback_at,
        "max_unexplained_gap_seconds": unexplained_gap,
        "unexplained_gap_step": unexplained_at,
        "timeout_marker_steps": timeout_hits[:5],
        "timeout_markers": len(timeout_hits),
        "infra_signatures": {k: len(v) for k, v in sorted(infra_hits.items())},
        "infra_signature_steps": {k: v[:3] for k, v in sorted(infra_hits.items())},
        "infra_tail": {k: v[:3] for k, v in sorted(infra_tail.items())},
        "exception_type": exc_type,
        "exception_kind": exception_kind(exc_type, meta.get("exception_message")),
        "stop_reason": stop_reason,
        "stop_kind": STOP_KIND.get(stop_reason or ""),
        "last_step": steps[-1].get("step_id") if steps else None,
    }


def exception_kind(exc_type: str | None, message: Any = None) -> str | None:
    if not exc_type:
        return None
    if exc_type in EXCEPTION_KIND:
        return EXCEPTION_KIND[exc_type]
    lowered = exc_type.lower()
    if "timeout" in lowered and "agent" in lowered:
        return "runaway"
    if any(marker in lowered for marker in _INFRA_EXCEPTION_SUBSTRINGS):
        return "infra"
    if isinstance(message, str) and INFRA_EXCEPTION_MESSAGE_RE.search(message):
        return "infra"
    return None


def _components(values: Iterable[tuple[str, float | None, float, Any]]) -> list[dict[str, Any]]:
    rows = []
    for name, value, threshold, step in values:
        if value is None:
            continue
        rows.append({"name": name, "value": value, "threshold": threshold,
                     "ratio": float(value) / threshold, "step": step})
    return rows


def _finding(run_id: str, kind: str, verdict: str, score: float, steps: Sequence[Any],
             summary: str, started: float) -> dict[str, Any]:
    evidence = [s for s in dict.fromkeys(steps) if isinstance(s, int)][:5]
    return {
        "run_id": run_id,
        "detector": f"run_health.{kind}",
        "detector_version": DETECTOR_VERSION,
        "reader_model": None,
        "verdict": verdict,
        "score": round(score, 4),
        "kind": kind,
        "evidence_steps": evidence,
        "summary": " ".join(summary.split()[:40]),
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "produced_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _decide(run_id: str, kind: str, components: list[dict[str, Any]], started: float,
            *, forced: str | None = None) -> dict[str, Any]:
    fired = [c for c in components if c["ratio"] >= 1.0]
    score = max((c["ratio"] for c in components), default=0.0)
    if forced is not None:
        fired.insert(0, {"name": forced, "value": 1, "threshold": 1, "ratio": max(score, 1.0), "step": None,
                         "op": None})
        score = max(score, 1.0)
    if fired:
        summary = "; ".join(c["name"] if c.get("op", ">=") is None
                            else f"{c['name']}={c['value']} ({c.get('op', '>=')} {c['threshold']})" for c in fired)
        return _finding(run_id, kind, "alarm", score, [c["step"] for c in fired], summary, started)
    top = max(components, key=lambda c: c["ratio"], default=None)
    summary = "no component at threshold" + (f"; max {top['name']}={top['value']}" if top else "")
    return _finding(run_id, kind, "clear", score, [], summary, started)


def _run_level(f: Mapping[str, Any], kind: str) -> str | None:
    if f["exception_kind"] == kind:
        return "exception:" + f["exception_type"]
    if f["stop_kind"] == kind:
        return "stop:" + f["stop_reason"]
    return None


def _kind_components(f: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    loop = _components([
        ("identical_command_run", f["max_identical_command_run"], LOOP_COMMAND_RUN, f["identical_command_run_step"]),
        ("identical_message_run", f["max_identical_message_run"], LOOP_MESSAGE_RUN, f["identical_message_run_step"]),
        ("completion_claims", f["completion_claims"], LOOP_COMPLETION_CLAIMS, None),
        ("repeated_feedback", f["max_repeated_feedback"], LOOP_FEEDBACK_REPEATS, f["repeated_feedback_step"]),
    ])
    if f["cycle_step"] is not None and f["cycle_min_distinct"]:
        loop.append({"name": "command_cycle", "value": f["cycle_min_distinct"], "threshold": LOOP_CYCLE_DISTINCT,
                     "op": "<=", "ratio": LOOP_CYCLE_DISTINCT / f["cycle_min_distinct"], "step": f["cycle_step"]})
    runaway = _components([
        ("agent_steps", f["agent_steps"], RUNAWAY_AGENT_STEPS, f["last_step"]),
        ("wall_seconds", f["wall_seconds"], RUNAWAY_WALL_SECONDS, f["last_step"]),
    ])
    latency = _components([
        ("setup_seconds", f["setup_seconds"], LATENCY_SETUP_SECONDS, None),
        ("verifier_seconds", f["verifier_seconds"], LATENCY_VERIFIER_SECONDS, None),
    ])
    if f["agent_timestamps"] >= LATENCY_MIN_TIMESTAMPS:
        latency += _components([
            ("median_step_seconds", f["seconds_per_agent_step_median"], LATENCY_MEDIAN_STEP_SECONDS, None),
            ("p90_step_seconds", f["seconds_per_agent_step_p90"], LATENCY_P90_STEP_SECONDS, None),
            ("max_gap_seconds", f["max_gap_seconds"], LATENCY_MAX_GAP_SECONDS, f["max_gap_step"]),
            ("unexplained_gap_seconds", f["max_unexplained_gap_seconds"], LATENCY_UNEXPLAINED_GAP_SECONDS,
             f["unexplained_gap_step"]),
        ])
    infra = [{"name": f"tail:{name}", "value": 1, "threshold": 1, "ratio": 1.0, "step": steps[0], "op": None}
             for name, steps in f["infra_tail"].items()]
    return {"loop": loop, "runaway": runaway, "latency": latency, "infra": infra}


def detect(run: HealthRun, feats: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    started = time.perf_counter()
    f = dict(feats) if feats is not None else features(run)
    rid = run.run_id
    components = _kind_components(f)
    if not f["actor_inputs_available"]:
        reason = "no actor transcript (actor_inputs_available=false): unknown, not clear"
        out = []
        for kind in KINDS:
            forced = _run_level(f, kind)
            tail = components["infra"] if kind == "infra" else []
            if forced or tail:
                out.append(_decide(rid, kind, tail, started, forced=forced))
            else:
                out.append(_finding(rid, kind, "unknown", 0.0, [], reason, started))
        return out

    out = []
    for kind in KINDS:
        if kind == "latency" and not components[kind]:
            out.append(_finding(rid, kind, "abstain", 0.0, [],
                                f"only {f['agent_timestamps']} timestamped agent steps and no phase timings", started))
        else:
            out.append(_decide(rid, kind, components[kind], started, forced=_run_level(f, kind)))
    return out


SILENCE_SECONDS = LATENCY_MAX_GAP_SECONDS


def triggers(run: HealthRun, *, now: float | None = None,
             feats: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    f = dict(feats) if feats is not None else features(run)
    components = _kind_components(f)
    out: list[dict[str, Any]] = []
    for kind in KINDS:
        if (forced := _run_level(f, kind)) is not None:
            out.append({"trigger": forced, "kind": kind, "value": 1, "threshold": 1, "op": None, "step": None})
        if not f["actor_inputs_available"] and kind != "infra":
            continue
        out.extend({"trigger": c["name"], "kind": kind, "value": c["value"], "threshold": c["threshold"],
                    "op": c.get("op", ">="), "step": c["step"]} for c in components[kind] if c["ratio"] >= 1.0)
    if now is not None and _epoch(run.meta.get("finished_at")) is None:
        started = _epoch(run.meta.get("started_at"))
        last = f["last_step_at"] if f["steps"] else started
        if last is not None and now - last >= SILENCE_SECONDS:
            out.append({"trigger": "silence", "kind": "latency", "value": round(now - last, 3),
                        "threshold": SILENCE_SECONDS, "op": ">=", "step": f["last_step"]})
        fired = {t["trigger"] for t in out}
        if started is not None and now - started >= RUNAWAY_WALL_SECONDS and "wall_seconds" not in fired:
            out.append({"trigger": "live_wall_seconds", "kind": "runaway", "value": round(now - started, 3),
                        "threshold": RUNAWAY_WALL_SECONDS, "op": ">=", "step": f["last_step"]})
    return out


def _phase_seconds(result: Mapping[str, Any], phase: str) -> float | None:
    block = result.get(phase)
    if not isinstance(block, Mapping):
        return None
    start, end = _epoch(block.get("started_at")), _epoch(block.get("finished_at"))
    return round(end - start, 3) if start is not None and end is not None and end >= start else None


def trial_metadata(trial_dir: Path) -> dict[str, Any]:
    try:
        result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = {}
    result = result if isinstance(result, Mapping) else {}
    exc = result.get("exception_info")
    exc = exc if isinstance(exc, Mapping) else {}
    agent_result = result.get("agent_result")
    agent_result = agent_result if isinstance(agent_result, Mapping) else {}
    metadata = agent_result.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    setup = [s for p in ("environment_setup", "agent_setup") if (s := _phase_seconds(result, p)) is not None]
    return {
        "started_at": _dig(result, "agent_execution.started_at") or result.get("started_at"),
        "finished_at": _dig(result, "agent_execution.finished_at") or result.get("finished_at"),
        "exception_type": exc.get("exception_type"),
        "exception_message": exc.get("exception_message"),
        "stop_reason": metadata.get("stop_reason"),
        "n_input_tokens": agent_result.get("n_input_tokens"),
        "n_output_tokens": agent_result.get("n_output_tokens"),
        "setup_seconds": round(sum(setup), 3) if setup else None,
        "verifier_seconds": _phase_seconds(result, "verifier"),
    }


def trial_run(trial_dir: Path, *, run_id: str | None = None, source: str = "harbor") -> HealthRun:
    steps, _reason = _stitched_steps(trial_dir)
    return HealthRun(run_id=run_id or trial_dir.name, source=source, steps=steps or [],
                     meta=trial_metadata(trial_dir))


STORE_TIMESTAMP_PATHS = ("native_node_residual.timestamp", "source_timestamp", "raw_message.timestamp")
STORE_TOKEN_PATHS = (
    ("source_metrics.prompt_tokens", "source_metrics.completion_tokens"),
    ("raw_message.usage.input", "raw_message.usage.output"),
)


def _store_step_facts(flags: Mapping[str, Any]) -> tuple[Any, dict[str, int]]:
    stamp = next((v for p in STORE_TIMESTAMP_PATHS if (v := _dig(flags, p)) not in (None, "")), None)
    metrics: dict[str, int] = {}
    for prompt_path, completion_path in STORE_TOKEN_PATHS:
        prompt = _int(_dig(flags, prompt_path))
        if prompt is None:
            continue
        metrics["prompt_tokens"] = prompt
        completion = _int(_dig(flags, completion_path))
        if completion is not None:
            metrics["completion_tokens"] = completion
        break
    return stamp, metrics


def store_runs(database: Path, *, registry: Any, dataset_ids: Sequence[str] | None = None,
               trace_ids: Sequence[str] | None = None) -> Iterator[HealthRun]:
    from evallab.production.run_atlas import cloud_runs, connect_readonly

    connection = connect_readonly(database)
    try:
        for run in cloud_runs(database, registry=registry, dataset_ids=dataset_ids, trace_ids=trace_ids):
            facts: dict[int, tuple[Any, dict[str, int]]] = {}
            for row in connection.execute(
                "SELECT step_index, flags_json FROM trace_steps WHERE trace_id = ?", (run.run_id,)
            ):
                try:
                    flags = json.loads(row["flags_json"] or "{}")
                except ValueError:
                    continue
                if isinstance(flags, Mapping):
                    facts[row["step_index"]] = _store_step_facts(flags)
            steps = []
            for step in run.doc.get("steps") or []:
                stamp, metrics = facts.get(step.get("step_id"), (None, {}))
                enriched = dict(step)
                if stamp is not None:
                    enriched["timestamp"] = stamp
                if metrics:
                    enriched["metrics"] = metrics
                steps.append(enriched)
            status = connection.execute(
                "SELECT outcome, CASE WHEN json_valid(verified_receipt_json)"
                " THEN json_extract(verified_receipt_json, '$.native_status.stop_condition') END"
                " FROM agent_traces WHERE trace_id = ?", (run.run_id,),
            ).fetchone()
            outcome, stop_condition = status if status else (None, None)
            yield HealthRun(run_id=run.run_id, source=run.source, steps=steps, meta={
                "actor_inputs_available": actor_inputs_available(steps),
                "stop_reason": outcome if outcome in STORE_RUN_STATUSES else stop_condition,
            })
    finally:
        connection.close()


def _write_jsonl(path: Path | None, rows: Iterable[Mapping[str, Any]]) -> int:
    handle = sys.stdout if path is None else path.open("w", encoding="utf-8")
    count = 0
    try:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=False, default=str) + "\n")
            count += 1
    finally:
        if path is not None:
            handle.close()
    return count


def _runs_from_args(args: argparse.Namespace) -> Iterator[HealthRun]:
    if args.trial_dir:
        for trial in args.trial_dir:
            yield trial_run(trial)
        return
    from evallab.production.corpus_queue import CorpusRegistry

    yield from store_runs(args.db, registry=CorpusRegistry(), dataset_ids=args.dataset or None,
                          trace_ids=args.trace_id or None)


def _command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    del root
    want = args.run_health_command
    now = None
    if want == "triggers":
        text = args.now.strip()
        now = time.time() if text == "now" else _epoch(float(text) if re.fullmatch(r"\d+(?:\.\d+)?", text) else text)
        if now is None:
            raise SystemExit(f"run-health triggers: --now {args.now!r} is not 'now', epoch seconds or ISO time")

    def rows() -> Iterator[dict[str, Any]]:
        for run in _runs_from_args(args):
            feats = features(run)
            if want == "features":
                yield feats
            elif want == "triggers":
                yield {"run_id": run.run_id, "detector_version": DETECTOR_VERSION,
                       "triggers": triggers(run, now=None if args.db else now, feats=feats)}
            else:
                yield from detect(run, feats)

    count = _write_jsonl(args.out, rows())
    if args.out is not None:
        print(f"{args.run_health_command}: {count} rows -> {args.out}", file=sys.stderr)
    return 0


