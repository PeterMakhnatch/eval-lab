"""HAR-92 step layers: what the model proposed, what the harness accepted, what executed, what came back.

Field names (proposed for HAR-93 agreement; Data owns the catalog tables, this
module owns the recording):

- ``step.extra["step_layers"]`` on Terminus-2 agent steps::

      {"schema": "evallab.step_layers/v1",
       "provenance": "recorded" | "reconstructed" | "copied",
       "proposed": {"message": str | None, "reasoning": str | None,
                    "reason": str | None},
       "accepted": {"kind": "calls" | "parse_error" | "prose_completion" | "unknown",
                    "calls": [{"keystrokes": str, "duration_sec": float | None}
                              | {"task_complete": True}] | None,
                    "task_complete": bool | None,
                    "parse_error": str | None,
                    "prose_shaped": bool | None,
                    "reason": str | None},
       "executed": {"keystrokes_sent": [str] | None,
                    "durations_sec": [float | None] | None,
                    "sent_at": str | None,
                    "timeout": bool | None,
                    "reason": str | None},
       "observed": {"output": str | None, "truncated": bool | None,
                    "truncated_bytes": int | None,
                    "timeout_template": bool | None,
                    "reason": str | None}}

  ``provenance`` is per step-layers object: ``recorded`` (written at runtime by
  :class:`evallab.harbor_terminus.SecretSafeTerminus2`), ``reconstructed``
  or ``copied`` (an ``is_copied_context`` step whose evidence lives in the
  head segment). Anything not derivable is null with a ``reason``; missing
  coverage never reads as 0.

- Trial level (computed by consumers, never stored on the trial):

  - ``verifier_outcome``: ``pass`` | ``fail`` | ``none`` (reward present and
    >= 1.0, reward present but lower, no reward).
  - ``stop_reason``: ``task_complete`` | ``prose_completion`` |
    ``agent_timeout`` | ``trial_budget_exhausted`` | ``error`` | ``unknown``.
    A verifier-scored ``AgentTimeoutError`` is a scored trial whose stop
    reason is ``agent_timeout``, not an infra failure.
  - ``execution_problems``: counts of parse errors, prose completions,
    provider 400s without usage, unreconciled proxy requests, and whether
    proxy usage failed reconciliation (derived from the provider-usage ledger:
    ``None`` only when there is no ledger). Layer-derived counts are ``None``
    — never partial counts shown as totals — when any live agent step lacks
    layers, with the reason carried alongside.
  - ``trajectory_coverage``: ``trajectory_head``,
    ``continuation_indices`` / ``continuation_count`` /
    ``continuations_missing`` (same names and meanings as the HAR-93 capture
    record), per-part steps, ``duplicate_segments`` (whole-duplicate parts in
    the SFT exporter's ``duplicate_of:`` vocabulary), unique steps after
    dedupe, duplicated/copied/malformed counts, gaps, and notes.

Recording is additive: the raw ATIF message, observation, and rollout
details are untouched, so SFT/RL training fidelity cannot change. The MiMo
parser itself is untouched; reconstruction replays the same
:func:`evallab.mimo_tool_calls.normalize_mimo_tool_calls` plus the stock
Terminus parser through an injected ``parse`` callback, so this module stays
Harbor-free and tests can inject a fake parser.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from evallab.mimo_tool_calls import (
    HARBOR_FALLBACK_RESPONSE,
    executed_keystrokes,
    normalize_mimo_tool_calls,
    prose_completion,
)

__all__ = [
    "STEP_LAYERS_KEY",
    "STEP_LAYERS_SCHEMA",
    "TRUNCATION_MARKER_RE",
    "TIMEOUT_TEMPLATE_RE",
    "ReplayedCommand",
    "ReplayedParse",
    "ParserFn",
    "TrajectoryPart",
    "StitchStats",
    "accepted_layer",
    "attach_layers",
    "build_recorded_layers",
    "classify_stop_reason",
    "copied_layers",
    "coverage_record",
    "discover_trajectory_parts",
    "duplicate_segments",
    "executed_layer",
    "executed_output",
    "execution_problems",
    "feedback_error_text",
    "observed_layer",
    "parse_observed_output",
    "proposed_layer",
    "reconstruct_layers",
    "segment_fingerprint",
    "stitch_steps",
    "summarize_layers",
    "synthesize_atif_calls",
    "verifier_outcome",
    "wrap_layers",
]

#: The ``step.extra`` key carrying the four layers on agent steps.
STEP_LAYERS_KEY = "step_layers"
#: Version stamp inside every recorded/reconstructed layers object.
STEP_LAYERS_SCHEMA = "evallab.step_layers/v1"

Provenance = Literal["recorded", "reconstructed", "copied"]
AcceptedKind = Literal["calls", "parse_error", "prose_completion", "unknown"]
VerifierOutcome = Literal["pass", "fail", "none"]
StopReason = Literal[
    "task_complete",
    "prose_completion",
    "agent_timeout",
    "trial_budget_exhausted",
    "error",
    "unknown",
]

#: Harbor's mid-output omission marker (``Terminus2._limit_output_length``):
#: ``[... output limited to 10000 bytes; N interior bytes omitted ...]``.
TRUNCATION_MARKER_RE = re.compile(
    r"\[\.\.\. output limited to \d+ bytes; (\d+) interior bytes omitted \.\.\.\]"
)
#: Head of Harbor's command-timeout observation template
#: (``terminus_2/templates/timeout.txt``). Required at the start so a command
#: that merely prints similar text is not mistaken for a harness timeout.
TIMEOUT_TEMPLATE_RE = re.compile(
    r"^Previous command:\n.*The previous command timed out after .* seconds",
    re.DOTALL,
)
_CONTINUATION_RE = re.compile(r"^trajectory\.cont-(\d+)\.json$")


@dataclass(frozen=True)
class ReplayedCommand:
    """One parsed command from a parser replay: keystrokes plus wait."""

    keystrokes: str
    duration_sec: float | None = None


@dataclass(frozen=True)
class ReplayedParse:
    """The parser decision for one response text, replayed deterministically."""

    error: str | None
    commands: tuple[ReplayedCommand, ...] = ()
    task_complete: bool = False


class ParserFn(Protocol):
    """Stock Terminus parse of one response text (Harbor-backed in prod)."""

    def __call__(self, text: str) -> ReplayedParse: ...


@dataclass
class TrajectoryPart:
    """One trajectory document of a trial: the head or a continuation."""

    name: str
    kind: Literal["head", "continuation"]
    index: int
    path: Path
    sha256: str | None = None
    readable: bool = False
    reason: str | None = None
    steps: int | None = None
    copied_steps: int | None = None
    agent_steps: int | None = None
    session_id: str | None = None


@dataclass
class StitchStats:
    """Dedupe outcome for stitched continuation parts."""

    per_part_steps: list[int] = field(default_factory=list)
    per_part_copied: list[int] = field(default_factory=list)
    unique_steps: int = 0
    duplicated_steps: int = 0
    copied_context_steps: int = 0
    malformed_steps: int = 0


# --------------------------------------------------------------------------- #
# Layer builders (runtime recording and offline reconstruction share these)
# --------------------------------------------------------------------------- #


def proposed_layer(
    message: Any, reasoning: Any, *, reason: str | None = None
) -> dict[str, Any]:
    """The model emission as generated: raw text plus reasoning."""
    text = message if isinstance(message, str) else None
    think = reasoning if isinstance(reasoning, str) and reasoning else None
    if text is None and reason is None:
        reason = "step carries no message text"
    return {"message": text, "reasoning": think, "reason": reason}


def accepted_layer(
    kind: AcceptedKind,
    *,
    calls: Sequence[Mapping[str, Any]] | None = None,
    task_complete: bool | None = None,
    parse_error: str | None = None,
    prose_shaped: bool | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """The parser result: normalized calls, a parse error, or prose completion."""
    return {
        "kind": kind,
        "calls": [dict(call) for call in calls] if calls is not None else None,
        "task_complete": task_complete,
        "parse_error": parse_error,
        "prose_shaped": prose_shaped,
        "reason": reason,
    }


def executed_layer(
    keystrokes_sent: Sequence[str] | None,
    durations_sec: Sequence[float | None] | None = None,
    *,
    sent_at: str | None = None,
    timeout: bool | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """The keystrokes actually sent, with waits and the batch timestamp."""
    return {
        "keystrokes_sent": list(keystrokes_sent) if keystrokes_sent is not None else None,
        "durations_sec": list(durations_sec) if durations_sec is not None else None,
        "sent_at": sent_at,
        "timeout": timeout,
        "reason": reason,
    }


def parse_observed_output(output: Any) -> dict[str, Any]:
    """Decode one observation text: truncation and timeout-template flags."""
    if not isinstance(output, str):
        return {
            "output": None,
            "truncated": None,
            "truncated_bytes": None,
            "timeout_template": None,
            "reason": "no observation text recorded",
        }
    marker = TRUNCATION_MARKER_RE.search(output)
    return {
        "output": output,
        "truncated": marker is not None,
        "truncated_bytes": int(marker.group(1)) if marker else None,
        "timeout_template": bool(TIMEOUT_TEMPLATE_RE.search(output)),
        "reason": None,
    }


def observed_layer(output: Any) -> dict[str, Any]:
    """The terminal output returned, with truncation flags."""
    return parse_observed_output(output)


def wrap_layers(
    provenance: Provenance,
    proposed: Mapping[str, Any],
    accepted: Mapping[str, Any],
    executed: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble the four layers into one ``step.extra`` object."""
    return {
        "schema": STEP_LAYERS_SCHEMA,
        "provenance": provenance,
        "proposed": dict(proposed),
        "accepted": dict(accepted),
        "executed": dict(executed),
        "observed": dict(observed),
    }

def attach_layers(
    extra: Mapping[str, Any] | None, layers: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a copy of a step ``extra`` mapping carrying ``layers``."""
    merged = dict(extra) if isinstance(extra, Mapping) else {}
    merged[STEP_LAYERS_KEY] = dict(layers)
    return merged


def build_recorded_layers(
    *,
    message: Any,
    reasoning: Any,
    prose_mapped: bool,
    commands: Sequence[tuple[str, float | None]],
    task_complete: bool,
    parse_error: str | None,
    keystrokes_sent: Sequence[str] | None,
    durations_sec: Sequence[float | None] | None,
    sent_at: str | None,
    timeout: bool | None,
    output: Any,
    not_executed_reason: str | None = None,
) -> dict[str, Any]:
    """Assemble runtime-recorded layers for one agent step.

    ``commands`` are the parsed ``(keystrokes, duration_sec)`` pairs the
    harness acted on; ``keystrokes_sent`` is what actually went to the
    terminal (``None`` with ``not_executed_reason`` when a parse error meant
    nothing executed). Parser decisions come from the live parse the harness
    already ran, never a re-parse.
    """
    calls: list[dict[str, Any]] = [
        {"keystrokes": keys, "duration_sec": duration}
        for keys, duration in commands
    ]
    if task_complete:
        calls.append({"task_complete": True})
    if prose_mapped:
        accepted = accepted_layer(
            "prose_completion",
            calls=[{"task_complete": True}],
            task_complete=True,
            prose_shaped=True,
        )
    elif parse_error:
        accepted = accepted_layer(
            "parse_error",
            calls=calls or None,
            task_complete=task_complete or None,
            parse_error=parse_error or None,
            prose_shaped=False,
        )
    else:
        accepted = accepted_layer(
            "calls",
            calls=calls or None,
            task_complete=task_complete or None,
            prose_shaped=False,
        )
    if keystrokes_sent is None:
        executed = executed_layer(
            None, None, sent_at=None, timeout=None, reason=not_executed_reason
        )
    else:
        executed = executed_layer(
            keystrokes_sent, durations_sec, sent_at=sent_at, timeout=timeout
        )
    return wrap_layers(
        "recorded",
        proposed_layer(message, reasoning),
        accepted,
        executed,
        observed_layer(output),
    )


# --------------------------------------------------------------------------- #
# Offline reconstruction (trials recorded before HAR-92)
# --------------------------------------------------------------------------- #


def _freeze(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _observation_text(step: Mapping[str, Any]) -> str | None:
    observation = step.get("observation")
    results = observation.get("results") if isinstance(observation, dict) else None
    if not isinstance(results, list):
        return None
    texts = [
        result.get("content")
        for result in results
        if isinstance(result, dict) and isinstance(result.get("content"), str)
    ]
    if not texts:
        return None
    return "\n".join(texts)


_FEEDBACK_PREFIX = "Previous response had parsing errors:"


def feedback_error_text(observation_text: Any) -> str | None:
    """The harness's parse-error verdict recorded in an observation, if any.

    A turn whose observation is the parse-error feedback prompt was rejected
    under the parser treatment that ran: nothing executed. Returns the
    feedback message, else ``None``.
    """
    if not isinstance(observation_text, str):
        return None
    text = observation_text.strip()
    if not text.startswith(_FEEDBACK_PREFIX):
        return None
    return text[len(_FEEDBACK_PREFIX) :].strip() or _FEEDBACK_PREFIX


def executed_output(step: Mapping[str, Any]) -> str | None:
    """Terminal output of a turn that executed, or ``None``.

    Harbor-free observation verdict for consumers without a parser: a turn
    whose observation is parse-error feedback, Harbor's stand-in reply, or
    nothing at all did not execute. Anything else is the terminal output the
    harness read back after executing. Never guesses commands or counts.
    """
    text = _observation_text(step)
    if not isinstance(text, str) or not text.strip():
        return None
    if feedback_error_text(text) is not None:
        return None
    if HARBOR_FALLBACK_RESPONSE in text:
        return None
    return text


def copied_layers() -> dict[str, Any]:
    """Null layers for an ``is_copied_context`` step; evidence is in the head."""
    reason = "is_copied_context: evidence lives in the head segment"
    return wrap_layers(
        "copied",
        proposed_layer(None, None, reason=reason),
        accepted_layer("unknown", reason=reason),
        executed_layer(None, None, reason=reason),
        parse_observed_output(None),
    )


def reconstruct_layers(step: Mapping[str, Any], *, parse: ParserFn) -> dict[str, Any] | None:
    """Rebuild the four layers for one raw ATIF step, labeled ``reconstructed``.

    Returns ``None`` for non-agent steps (layers are per agent turn). Steps
    that already carry recorded layers are returned as-is; copied-context
    steps yield null layers (evidence is in the head segment).
    """
    if step.get("source") != "agent":
        return None
    extra = step.get("extra")
    extra = extra if isinstance(extra, Mapping) else {}
    stored = extra.get(STEP_LAYERS_KEY)
    if isinstance(stored, Mapping) and stored.get("schema") == STEP_LAYERS_SCHEMA:
        return dict(stored)
    if step.get("is_copied_context"):
        return copied_layers()
    message = step.get("message")
    text = message if isinstance(message, str) else ""
    proposed = proposed_layer(
        message,
        step.get("reasoning_content"),
        reason=None if isinstance(message, str) else "step carries no message text",
    )
    prose_flag = extra.get("prose_completion") is True
    if prose_flag:
        # The runtime prose rule fired and mapped this turn to task_complete;
        # the flag is the recorded evidence, so no finish_reason is assumed.
        accepted = accepted_layer(
            "prose_completion",
            calls=[{"task_complete": True}],
            task_complete=True,
            prose_shaped=True,
        )
        calls: list[dict[str, Any]] | None = []
    else:
        feedback = feedback_error_text(_observation_text(step))
        normalized = normalize_mimo_tool_calls(text)
        replayed = parse(normalized if normalized is not None else text)
        if feedback is not None:
            # The harness rejected this turn under the parser treatment that
            # ran (its verdict is recorded in the observation), so nothing
            # executed — whatever today's parser says about the shape. This
            # is treatment drift, not a replay disagreement: 0758-c's tail
            # shape predates the normalizer rule that now accepts it.
            calls = None
            accepted = accepted_layer(
                "parse_error",
                parse_error=feedback,
                prose_shaped=False,
                reason="observation carries the harness's parse-error feedback",
            )
        elif not replayed.error:
            calls = [
                {"keystrokes": cmd.keystrokes, "duration_sec": cmd.duration_sec}
                for cmd in replayed.commands
            ]
            if replayed.task_complete:
                calls.append({"task_complete": True})
            accepted = accepted_layer(
                "calls",
                calls=calls or None,
                task_complete=replayed.task_complete or None,
                prose_shaped=False,
            )
        else:
            calls = None
            shaped = prose_completion(text) is not None
            accepted = accepted_layer(
                "parse_error",
                parse_error=replayed.error or None,
                prose_shaped=shaped,
                reason=(
                    "finish_reason was not recorded pre-HAR-92: a prose-shaped "
                    "turn with finish_reason=stop would have mapped to "
                    "task_complete instead"
                    if shaped
                    else None
                ),
            )
    command_calls = [
        call for call in (calls or [])
        if isinstance(call.get("keystrokes"), str)
    ]
    if calls is None:
        executed = executed_layer(
            None, None, reason="parse_error: nothing executed"
        )
    else:
        sent: list[str] = []
        durations: list[float | None] = []
        for call in command_calls:
            text = call.get("keystrokes")
            if not isinstance(text, str):
                continue
            sent.append(executed_keystrokes(text))
            duration = call.get("duration_sec")
            durations.append(
                duration if isinstance(duration, (int, float)) else None
            )
        executed = executed_layer(
            sent,
            durations,
            reason="pre-HAR-92: batch timestamp and timeout flag not recorded"
            if command_calls
            else "task_complete turn sent no keystrokes",
        )
        if command_calls:
            executed["sent_at"] = None
            executed["timeout"] = None
        else:
            executed["timeout"] = False
    observed = parse_observed_output(_observation_text(step))
    if observed["output"] is None:
        observed["reason"] = "step carries no observation results"
    elif observed["timeout_template"]:
        executed = dict(executed)
        executed["timeout"] = True
        executed["reason"] = None
    return wrap_layers("reconstructed", proposed, accepted, executed, observed)


def synthesize_atif_calls(
    step: Mapping[str, Any], layers: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """ATIF-shaped ``tool_calls`` synthesized from step layers.

    Lets consumers built for parsed trajectories (run reports, diagnosis
    views) count executed MiMo calls on ``raw_content`` trials. Returns []
    when the step accepted nothing (parse errors) or carries no layers.
    """
    if not isinstance(layers, Mapping):
        return []
    accepted = layers.get("accepted")
    provenance = layers.get("provenance")
    if not isinstance(accepted, Mapping) or accepted.get("kind") not in (
        "calls",
        "prose_completion",
    ):
        return []
    synthesized: list[dict[str, Any]] = []
    for position, call in enumerate(accepted.get("calls") or []):
        if not isinstance(call, Mapping):
            continue
        if call.get("task_complete") is True:
            synthesized.append(
                {
                    "function_name": "task_complete",
                    "arguments": {},
                    "tool_call_id": f"har92-{provenance}-{position}",
                    "extra": {"provenance": provenance},
                }
            )
        elif isinstance(call.get("keystrokes"), str):
            synthesized.append(
                {
                    "function_name": "exec",
                    "arguments": {
                        "keystrokes": call["keystrokes"],
                        "duration": call.get("duration_sec"),
                    },
                    "tool_call_id": f"har92-{provenance}-{position}",
                    "extra": {"provenance": provenance},
                }
            )
    return synthesized


def summarize_layers(
    steps: Sequence[Any],
    layers_by_index: Mapping[int, Any],
    *,
    layers_missing_why: str | None = None,
) -> dict[str, Any]:
    """Count layer outcomes over raw steps for execution-problem reporting.

    Provenance counts (recorded, reconstructed, copied, missing) are always
    exact. The outcome counts (parse errors, prose completions, executed
    calls, task-complete turns) are only exact when every live agent step has
    layers: with any step missing, a partial count would read as a total, so
    those fields are ``None`` with ``layers_unknown_reason`` carrying why.
    Copied-context steps are context replay, not live turns: without layers
    they count as copied, never missing.
    """
    summary: dict[str, Any] = {
        "agent_steps": 0,
        "recorded": 0,
        "reconstructed": 0,
        "copied": 0,
        "missing": 0,
        "parse_errors": 0,
        "prose_completions": 0,
        "executed_calls": 0,
        "task_complete_turns": 0,
        "layers_unknown_reason": None,
    }
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping) or step.get("source") != "agent":
            continue
        summary["agent_steps"] += 1
        layers = layers_by_index.get(index)
        if not isinstance(layers, Mapping):
            if step.get("is_copied_context"):
                summary["copied"] += 1
            else:
                summary["missing"] += 1
            continue
        provenance = layers.get("provenance")
        if provenance in ("recorded", "reconstructed", "copied"):
            summary[provenance] += 1
        else:
            summary["missing"] += 1
        accepted = layers.get("accepted")
        if not isinstance(accepted, Mapping):
            continue
        if accepted.get("kind") == "parse_error":
            summary["parse_errors"] += 1
        if accepted.get("kind") == "prose_completion":
            summary["prose_completions"] += 1
        for call in accepted.get("calls") or []:
            if isinstance(call, Mapping) and "keystrokes" in call:
                summary["executed_calls"] += 1
        if accepted.get("task_complete"):
            summary["task_complete_turns"] += 1
    if summary["missing"]:
        why = layers_missing_why or "no recorded step_layers and no reconstructed layers"
        summary["layers_unknown_reason"] = (
            f"layers missing for {summary['missing']} of {summary['agent_steps']} "
            f"agent steps: {why}"
        )
        summary["parse_errors"] = None
        summary["prose_completions"] = None
        summary["executed_calls"] = None
        summary["task_complete_turns"] = None
    return summary


# --------------------------------------------------------------------------- #
# Continuation discovery, stitching, and coverage
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str | None:
    try:
        return __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _part_stats(payload: Any) -> tuple[bool, str | None, int | None, int | None, int | None, str | None]:
    if not isinstance(payload, dict):
        return False, "top-level JSON is not an object", None, None, None, None
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return False, "no steps list", None, None, None, None
    agent = sum(
        1
        for step in steps
        if isinstance(step, Mapping) and step.get("source") == "agent"
    )
    copied = sum(1 for step in steps if isinstance(step, Mapping) and step.get("is_copied_context"))
    session = payload.get("session_id")
    return True, None, len(steps), copied, agent, session if isinstance(session, str) else None


def discover_trajectory_parts(agent_dir: Path) -> list[TrajectoryPart]:
    """List the head plus every ``trajectory.cont-N.json`` continuation.

    Tolerates a missing head (only continuations retained) and unreadable
    files; both are reported in the part record, never silently dropped.
    """
    parts: list[TrajectoryPart] = []
    try:
        entries = sorted(agent_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return parts
    for entry in entries:
        if not entry.is_file():
            continue
        if entry.name == "trajectory.json":
            kind, index = "head", 0
        else:
            match = _CONTINUATION_RE.match(entry.name)
            if not match:
                continue
            kind, index = "continuation", int(match.group(1))
        try:
            payload = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parts.append(
                TrajectoryPart(
                    name=entry.name, kind=kind, index=index, path=entry,
                    readable=False, reason=f"{type(exc).__name__}",
                )
            )
            continue
        readable, reason, steps, copied, agent, session = _part_stats(payload)
        parts.append(
            TrajectoryPart(
                name=entry.name, kind=kind, index=index, path=entry,
                sha256=_sha256(entry), readable=readable, reason=reason,
                steps=steps, copied_steps=copied, agent_steps=agent,
                session_id=session,
            )
        )
    parts.sort(key=lambda part: (part.kind != "head", part.index))
    return parts


def _step_key(step: Mapping[str, Any]) -> str | None:
    """Identity for cross-part dedupe, or ``None`` when unmergeable.

    Steps without a timestamp cannot be told apart from a re-emitted turn:
    merging them would collapse genuine repeats (a loop of identical calls)
    into one. Only timestamped steps merge across parts; the rest are always
    kept. Every shared step observed so far carries a timestamp.
    """
    if step.get("timestamp") is None:
        return None
    return _freeze(
        (
            step.get("timestamp"),
            step.get("source"),
            step.get("message"),
            step.get("reasoning_content"),
            _observation_text(step),
        )
    )


def stitch_steps(
    docs_in_order: Sequence[Mapping[str, Any] | Any],
) -> tuple[list[Any], StitchStats]:
    """Merge continuation parts into unique steps without double-counting.

    Copied-context steps repeat history already present in an earlier part,
    so they are excluded from action views (but counted). Steps shared across
    parts — a sealed head re-dumped as a continuation (duplicate files), or a
    head prefix restated by a cumulative continuation — are identified by
    (timestamp, source, message, reasoning, observation) and kept once, with
    later parts superseding. Repeats *within* one part are always kept: a
    loop of identical calls is evidence, not a recording duplicate.
    Non-dict entries pass through untouched so malformed shapes still fail
    closed in strict consumers.
    """
    stats = StitchStats()
    first_seen_in: dict[str, int] = {}
    unique: list[Any] = []
    for part_index, doc in enumerate(docs_in_order):
        raw_steps = doc.get("steps") if isinstance(doc, Mapping) else None
        if not isinstance(raw_steps, list):
            continue
        part_steps = 0
        part_copied = 0
        for raw_step in raw_steps:
            if not isinstance(raw_step, Mapping):
                stats.malformed_steps += 1
                unique.append(raw_step)
                continue
            part_steps += 1
            if raw_step.get("is_copied_context"):
                part_copied += 1
                stats.copied_context_steps += 1
                continue
            key = _step_key(raw_step)
            if key is not None and first_seen_in.get(key, part_index) < part_index:
                stats.duplicated_steps += 1
                continue
            if key is not None:
                first_seen_in.setdefault(key, part_index)
            unique.append(raw_step)
        stats.per_part_steps.append(part_steps)
        stats.per_part_copied.append(part_copied)
    stats.unique_steps = len(unique)
    return unique, stats


def segment_fingerprint(steps: Any) -> str | None:
    """Whole-segment identity, shared with the SFT exporter.

    Same algorithm as ``sft_terminus.py`` (sha256 over the steps array dumped
    with sorted keys): a continuation whose steps equal an exported segment's
    — a summarization that failed without splitting the chat — is the same
    segment twice. Non-list steps never match. Step-level prefix overlap (a
    cumulative continuation restating the head) does NOT match here; that is
    counted once by :func:`stitch_steps` for reporting, while the SFT
    exporter keeps both segments as separate conversations.
    """
    if not isinstance(steps, list):
        return None
    return hashlib.sha256(
        json.dumps(steps, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def duplicate_segments(
    named_step_lists: Sequence[tuple[str, Any]],
) -> dict[str, str]:
    """Name later whole-duplicate segments ``duplicate_of:<earlier name>``.

    Mirrors the SFT exporter's per-trial loop: segments iterate main-first,
    the first fingerprint wins, and later matches are skipped and recorded
    under the same ``duplicate_of:`` vocabulary. Input order must be
    main-first (see :func:`discover_trajectory_parts`).
    """
    seen: dict[str, str] = {}
    duplicates: dict[str, str] = {}
    for name, steps in named_step_lists:
        digest = segment_fingerprint(steps)
        if digest is None:
            continue
        if digest in seen:
            duplicates[name] = f"duplicate_of:{seen[digest]}"
        else:
            seen[digest] = name
    return duplicates


def coverage_record(
    parts: Sequence[TrajectoryPart],
    stats: StitchStats,
    *,
    summarization_count: int | None = None,
    step_lists: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The capture-coverage record: which parts exist and what they hold.

    Continuation names align with the HAR-93 capture record
    (``trial_treatment.collect_capture``): ``trajectory_head``,
    ``continuation_indices``, ``summarization_count``, and
    ``continuations_missing`` (expected ``1..summarization_count`` minus
    present, ``None`` when the count is unknown) mean the same in both.
    Beyond the capture record this adds per-part steps, whole-duplicate
    segments in the SFT exporter's ``duplicate_of:`` vocabulary, and the
    unique/step-overlap counts from :func:`stitch_steps`.
    """
    head = next((part for part in parts if part.kind == "head"), None)
    continuations = [part for part in parts if part.kind == "continuation"]
    present = sorted(part.index for part in continuations if part.readable)
    missing = (
        sorted(set(range(1, summarization_count + 1)) - set(present))
        if isinstance(summarization_count, int) and summarization_count >= 0
        else None
    )
    duplicates = (
        duplicate_segments([(name, step_lists[name]) for name in step_lists])
        if step_lists is not None
        else {}
    )
    gaps: list[str] = []
    notes: list[str] = []
    if head is None and continuations:
        gaps.append("head trajectory.json absent: history starts at first continuation")
    if head is not None and not head.readable:
        gaps.append(f"head {head.name} unreadable: {head.reason}")
    if present and present != list(range(present[0], present[0] + len(present))):
        notes.append(f"continuation indices not contiguous: {present}")
    for part in continuations:
        if not part.readable:
            gaps.append(f"{part.name} unreadable: {part.reason}")
    if missing:
        notes.append(
            f"{len(missing)} summarization attempt(s) left no continuation file: {missing}"
        )
    for name, target in sorted(duplicates.items()):
        notes.append(f"{name} repeats an earlier segment ({target})")
    if stats.duplicated_steps:
        notes.append(
            f"{stats.duplicated_steps} step(s) shared across parts counted once"
        )
    return {
        "trajectory_head": head is not None and head.readable,
        "continuation_indices": present,
        "continuation_count": len(present),
        "summarization_count": summarization_count,
        "continuations_missing": missing,
        "parts": [
            {
                "path": part.name,
                "kind": part.kind,
                "index": part.index,
                "readable": part.readable,
                "steps": part.steps,
                "copied_steps": part.copied_steps,
                "session_id": part.session_id,
            }
            for part in parts
        ],
        "duplicate_segments": duplicates,
        "unique_steps": stats.unique_steps,
        "duplicated_steps": stats.duplicated_steps,
        "copied_context_steps": stats.copied_context_steps,
        "malformed_steps": stats.malformed_steps,
        "complete": not gaps,
        "gaps": gaps,
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# Outcome vs execution: verifier outcome, stop reason, execution problems
# --------------------------------------------------------------------------- #


def verifier_outcome(rewards: Mapping[str, Any]) -> VerifierOutcome:
    """pass (reward >= 1.0), fail (a reward that is not a pass), or none."""
    values = [
        float(value)
        for value in rewards.values()
        if not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    ]
    judged = values[0] if len(values) == 1 else None
    if not values:
        return "none"
    if judged is None:
        # Several metrics without a primary: all at 1 pass, all at 0 fail,
        # anything else is a non-pass with a real score.
        if all(value >= 1.0 for value in values):
            return "pass"
        return "fail"
    return "pass" if judged >= 1.0 else "fail"


def _exception_type(exception_info: Any) -> str | None:
    if not isinstance(exception_info, Mapping):
        return None
    value = exception_info.get("exception_type") or exception_info.get("type")
    return str(value) if value else None


def classify_stop_reason(
    *,
    agent_metadata: Mapping[str, Any] | None,
    exception_info: Mapping[str, Any] | None,
    last_task_complete: bool | None = None,
    last_prose_completion: bool | None = None,
) -> tuple[StopReason, str]:
    """Return (stop_reason, detail). Unknown stays unknown, never a default.

    ``last_task_complete`` / ``last_prose_completion`` describe the final
    accepted agent turn (from step layers) for trials that raised nothing.
    """
    metadata = agent_metadata if isinstance(agent_metadata, Mapping) else {}
    if metadata.get("stop_reason") == "trial_budget_exhausted":
        return "trial_budget_exhausted", "agent metadata stop_reason"
    exc_type = _exception_type(exception_info) or ""
    if "TrialBudgetExhausted" in exc_type:
        return "trial_budget_exhausted", f"exception {exc_type}"
    if "AgentTimeout" in exc_type or "Timeout" in exc_type:
        return "agent_timeout", f"exception {exc_type}"
    if exc_type:
        return "error", f"exception {exc_type}"
    if last_prose_completion:
        return "prose_completion", "final turn mapped prose_completion to task_complete"
    if last_task_complete:
        return "task_complete", "final turn accepted task_complete"
    if last_task_complete is False:
        return "unknown", "no exception but final turn did not complete"
    return "unknown", "no exception and no accepted final turn found"


def _provider_400s(provider_usage: Any) -> tuple[int | None, int | None]:
    if not isinstance(provider_usage, Mapping):
        return None, None
    calls = provider_usage.get("calls")
    if not isinstance(calls, list):
        return None, None
    bad = 0
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        status = call.get("status")
        state = call.get("state")
        error = call.get("error")
        reason = call.get("reason")
        unreconciled = state != "reconciled"
        text_400 = any(
            isinstance(value, str) and "400" in value for value in (error, reason)
        )
        if (status == 400 or text_400) and unreconciled:
            bad += 1
    unresolved = provider_usage.get("unresolved_requests")
    unresolved = (
        int(unresolved)
        if isinstance(unresolved, int) and not isinstance(unresolved, bool)
        else None
    )
    return bad, unresolved


def _ledger_unreconciled(provider_usage: Any) -> bool | None:
    """Whether the proxy ledger shows unreconciled work, or ``None`` without one.

    ``True`` when any call's ``state`` is not ``"reconciled"`` or
    ``unresolved_requests`` is positive; ``False`` when a ledger is present
    and every call reconciled with nothing unresolved.
    """
    if not isinstance(provider_usage, Mapping):
        return None
    calls = provider_usage.get("calls")
    if not isinstance(calls, list):
        return None
    for call in calls:
        if isinstance(call, Mapping) and call.get("state") != "reconciled":
            return True
    unresolved = provider_usage.get("unresolved_requests")
    return (
        isinstance(unresolved, int) and not isinstance(unresolved, bool) and unresolved > 0
    )


def execution_problems(
    *,
    layer_summary: Mapping[str, Any] | None = None,
    lab_metadata: Mapping[str, Any] | None = None,
    proxy_usage_unreconciled: bool | None = None,
) -> dict[str, Any]:
    """Execution problems, kept separate from the verifier outcome."""
    summary = layer_summary if isinstance(layer_summary, Mapping) else {}
    metadata = lab_metadata if isinstance(lab_metadata, Mapping) else {}
    http_400, unresolved = _provider_400s(metadata.get("provider_usage"))
    unreconciled = proxy_usage_unreconciled
    if unreconciled is None:
        failure = metadata.get("failure_reason") or metadata.get("proxy_failure")
        if isinstance(failure, str) and "unreconciled" in failure:
            unreconciled = True
    if unreconciled is None:
        # A present ledger answers the question itself: ``None`` then means
        # "no ledger", never "looked and found nothing".
        unreconciled = _ledger_unreconciled(metadata.get("provider_usage"))
    problems: dict[str, Any] = {
        "parse_errors": summary.get("parse_errors"),
        "prose_completions": summary.get("prose_completions"),
        "http_400_no_usage": http_400,
        "proxy_unresolved_requests": unresolved,
        "proxy_usage_unreconciled": unreconciled,
    }
    missing = sorted(key for key, value in problems.items() if value is None)
    problems["coverage"] = "complete" if not missing else f"unknown: {', '.join(missing)}"
    return problems
