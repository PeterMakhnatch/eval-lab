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
  or ``copied`` (an ``is_copied_context`` step: context replayed into a
  continuation, never a turn taken there). Anything not derivable is null
  with a ``reason``; missing coverage never reads as 0.

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

This module is also the one shared stitching library for Terminus
continuation files: ``discover_trajectory_parts`` + ``stitch_steps`` (+
``coverage_record``, ``segment_fingerprint`` / ``duplicate_segments``) are
the only allowed way to merge ``trajectory.json`` with its
``trajectory.cont-N.json`` parts. Consumers: ``traj`` (chain stitching),
``trial_diagnosis``, ``trial_treatment`` (capture token sums), ``explorer``,
``interpretation/trajectory_ir``,
``sft_terminus`` (whole-segment identity), ``process_job``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from evallab.mimo_tool_calls import HARBOR_FALLBACK_RESPONSE

__all__ = [
    "CEILING_STOP_PREFIX",
    "STEP_LAYERS_KEY",
    "STEP_LAYERS_SCHEMA",
    "TRUNCATION_MARKER_RE",
    "TIMEOUT_TEMPLATE_RE",
    "ReplayedCommand",
    "ReplayedParse",
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
    "effective_tool_calls",
    "executed_layer",
    "executed_output",
    "feedback_error_text",
    "limit_hit_summary",
    "observed_layer",
    "parse_observed_output",
    "proposed_layer",
    "segment_fingerprint",
    "stitch_steps",
    "stop_category",
    "synthesize_atif_calls",
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
    "loop_break",
    "harness_step_limit",
    "context_exhausted",
    "error",
    "unknown",
]
#: A ``ceiling:<dimension>`` stop reason names the binding trial-budget
#: ceiling (``input_tokens``, ``output_tokens``, ``requests``, ``cost`` or
#: ``total_tokens``); the dimension suffix is dynamic, so these reasons are
#: plain ``str`` rather than members of the ``StopReason`` literal.
CEILING_STOP_PREFIX = "ceiling:"
#: Whose limit (if any) stopped the trial. ``our_limit`` is a lab-imposed
#: budget or loop stop; ``harness_step_limit`` is the agent harness's own
#: step cap; ``context_exhausted`` is the agent harness's own served context
#: filling (agent-side: not a lab ceiling, not infra); ``task_timeout`` is
#: Harbor's outer wall timeout (recorded as ``agent_timeout``); ``model_end``
#: is the model finishing on its own.
StopCategory = Literal[
    "our_limit",
    "harness_step_limit",
    "context_exhausted",
    "task_timeout",
    "model_end",
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


def proposed_layer(message: Any, reasoning: Any, *, reason: str | None = None) -> dict[str, Any]:
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


def attach_layers(extra: Mapping[str, Any] | None, layers: Mapping[str, Any]) -> dict[str, Any]:
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
        {"keystrokes": keys, "duration_sec": duration} for keys, duration in commands
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
        executed = executed_layer(keystrokes_sent, durations_sec, sent_at=sent_at, timeout=timeout)
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
    """Null layers for an ``is_copied_context`` step, which never ran here.

    Harbor's summarization split copies ``[system, question prompt, questions
    reply, handoff]`` into the continuation, so its one copied agent message
    is the questions subagent's reply. That reply is only ever text for the
    answers subagent: never parsed or executed, even when it holds commands.
    Its own step, with timestamp and usage, is in
    ``trajectory.summarization-N-questions.json``, not the head.
    """
    reason = (
        "is_copied_context: replayed context, never parsed or executed in this "
        "segment; the step it copies is in the part it came from (after a "
        "summarization split, trajectory.summarization-N-questions.json)"
    )
    return wrap_layers(
        "copied",
        proposed_layer(None, None, reason=reason),
        accepted_layer("unknown", reason=reason),
        executed_layer(None, None, reason=reason),
        parse_observed_output(None),
    )


def synthesize_atif_calls(
    step: Mapping[str, Any], layers: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Stock Terminus-2 ``tool_calls`` synthesized from step layers.

    ``raw_content`` trials store the raw model reply and no ``tool_calls``;
    this rebuilds the calls the harness accepted in the shape stock
    Terminus-2 writes (``bash_command`` with ``keystrokes``/``duration``,
    ``mark_task_complete``), so parsed-trajectory consumers read MiMo runs
    like any other Terminus-2 run. Returns [] when the step accepted nothing
    (parse errors) or carries no layers.
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
    step_id = step.get("step_id")
    synthesized: list[dict[str, Any]] = []
    for position, call in enumerate(accepted.get("calls") or []):
        if not isinstance(call, Mapping):
            continue
        call_id = f"call_{step_id}_{position + 1}"
        if call.get("task_complete") is True:
            synthesized.append(
                {
                    "tool_call_id": call_id,
                    "function_name": "mark_task_complete",
                    "arguments": {},
                    "extra": {"provenance": provenance},
                }
            )
        elif isinstance(call.get("keystrokes"), str):
            synthesized.append(
                {
                    "tool_call_id": call_id,
                    "function_name": "bash_command",
                    "arguments": {
                        "keystrokes": call["keystrokes"],
                        "duration": call.get("duration_sec"),
                    },
                    "extra": {"provenance": provenance},
                }
            )
    return synthesized


def effective_tool_calls(
    step: Mapping[str, Any], layers: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The step's tool calls: native ``tool_calls`` when present, else synthesized.

    The single read path for consumers that count or inspect tool calls.
    Native calls win; a step without them falls back to ``layers`` (the
    caller's recorded or reconstructed layers) or, when none are passed, the
    layers recorded in ``step.extra``.
    """
    native = step.get("tool_calls")
    if isinstance(native, list):
        calls = [call for call in native if isinstance(call, dict)]
        if calls:
            return calls
    if layers is None:
        extra = step.get("extra")
        stored = extra.get(STEP_LAYERS_KEY) if isinstance(extra, Mapping) else None
        layers = stored if isinstance(stored, Mapping) else None
    return synthesize_atif_calls(step, layers)


# --------------------------------------------------------------------------- #
# Continuation discovery, stitching, and coverage
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str | None:
    try:
        return __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _part_stats(
    payload: Any,
) -> tuple[bool, str | None, int | None, int | None, int | None, str | None]:
    if not isinstance(payload, dict):
        return False, "top-level JSON is not an object", None, None, None, None
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return False, "no steps list", None, None, None, None
    agent = sum(1 for step in steps if isinstance(step, Mapping) and step.get("source") == "agent")
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
                    name=entry.name,
                    kind=kind,
                    index=index,
                    path=entry,
                    readable=False,
                    reason=f"{type(exc).__name__}",
                )
            )
            continue
        readable, reason, steps, copied, agent, session = _part_stats(payload)
        parts.append(
            TrajectoryPart(
                name=entry.name,
                kind=kind,
                index=index,
                path=entry,
                sha256=_sha256(entry),
                readable=readable,
                reason=reason,
                steps=steps,
                copied_steps=copied,
                agent_steps=agent,
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
        notes.append(f"{stats.duplicated_steps} step(s) shared across parts counted once")
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
    native_exit_status: str | None = None,
) -> tuple[str, str]:
    """Return (stop_reason, detail). Unknown stays unknown, never a default.

    ``last_task_complete`` / ``last_prose_completion`` describe the final
    accepted agent turn (from step layers) for trials that raised nothing.
    Xiaomi's pinned ``BaseAgent`` returns ``Idle`` when the model issues no
    tool call and ``LimitsExceeded`` at its step cap (or for an empty model
    response, identified by ``native_exit_result``). Structured infrastructure
    stops retain ``infra_error``; a served-context refusal ends as
    ``ContextExhausted``/``context_exhausted`` (final state graded, never
    infra); other native failures remain errors, and a binding trial-budget
    ceiling reads ``ceiling:<dimension>``.
    """
    metadata = agent_metadata if isinstance(agent_metadata, Mapping) else {}
    recorded = metadata.get("stop_reason")
    if isinstance(recorded, str) and (
        recorded
        in (
            "trial_budget_exhausted",
            "loop_break",
            "harness_step_limit",
            "agent_timeout",
            "infra_error",
            "context_exhausted",
        )
        or recorded.startswith(CEILING_STOP_PREFIX)
    ):
        return recorded, "agent metadata stop_reason"
    # A nudge alone does not mean the loop break ended the trial.
    loop_break = metadata.get("loop_break")
    if isinstance(loop_break, Mapping) and loop_break.get("stop_call") is not None:
        return "loop_break", "agent metadata loop_break record stopped the trial"
    exc_type = _exception_type(exception_info) or ""
    if "TrialBudgetExhausted" in exc_type:
        return "trial_budget_exhausted", f"exception {exc_type}"
    if "LoopBreakStop" in exc_type:
        return "loop_break", f"exception {exc_type}"
    if "AgentTimeout" in exc_type or "Timeout" in exc_type:
        return "agent_timeout", f"exception {exc_type}"
    if exc_type:
        return "error", f"exception {exc_type}"
    if native_exit_status is None:
        value = metadata.get("native_exit_status")
        native_exit_status = value if isinstance(value, str) else None
    if native_exit_status == "LimitsExceeded":
        if metadata.get("native_exit_result") == "Empty assistant response":
            return "error", "native exit_status LimitsExceeded: empty assistant response"
        return "harness_step_limit", "native exit_status LimitsExceeded (native step cap)"
    if native_exit_status == "ContextExhausted":
        return "context_exhausted", "native exit_status ContextExhausted (served context filled)"
    if native_exit_status in ("ModelQueryError", "InfraError"):
        return "error", f"native exit_status {native_exit_status} with no recorded exception"
    if last_prose_completion:
        return "prose_completion", "final turn mapped prose_completion to task_complete"
    if native_exit_status == "Idle":
        return "task_complete", "native exit_status Idle (model issued no tool call)"
    if last_task_complete:
        return "task_complete", "final turn accepted task_complete"
    if last_task_complete is False:
        return "unknown", "no exception but final turn did not complete"
    return "unknown", "no exception and no accepted final turn found"


def stop_category(reason: str | None) -> StopCategory:
    """Whose limit (if any) stopped the trial, for setup-limit rollups.

    ``trial_budget_exhausted``, ``loop_break`` and any ``ceiling:<dimension>``
    reason are ``our_limit``; ``harness_step_limit`` is the agent harness's
    own step cap; ``context_exhausted`` is the agent harness's own served
    context filling (agent-side, never a lab ceiling or infra);
    ``agent_timeout`` is Harbor's outer wall timeout
    (``task_timeout``); ``task_complete``/``prose_completion`` are the model
    finishing on its own (``model_end``). Anything else — including
    ``error`` and unrecognized labels — reads ``error``, except a missing or
    ``unknown`` reason, which stays ``unknown``.
    """
    if reason is None or reason == "unknown":
        return "unknown"
    if not isinstance(reason, str):
        return "unknown"
    if (
        reason == "trial_budget_exhausted"
        or reason == "loop_break"
        or reason.startswith(CEILING_STOP_PREFIX)
    ):
        return "our_limit"
    if reason == "harness_step_limit":
        return "harness_step_limit"
    if reason == "context_exhausted":
        return "context_exhausted"
    if reason == "agent_timeout":
        return "task_timeout"
    if reason in ("task_complete", "task_complete_confirmed", "prose_completion"):
        return "model_end"
    return "error"


def limit_hit_summary(reasons: Iterable[str | None]) -> dict[str, Any]:
    """Roll stop reasons up to a setup-limit summary.

    Returns ``{"trials", "limit_hit_trials", "limit_hit_share",
    "setup_limited"}``: a trial counts as limit-hit when its
    :func:`stop_category` is ``our_limit``. ``limit_hit_share`` is ``None``
    with no trials; ``setup_limited`` is a strict ``> 0.05`` share so a
    lone limit stop in a large sweep does not flag the setup.
    """
    trials = hits = 0
    for reason in reasons:
        trials += 1
        hits += stop_category(reason) == "our_limit"
    share = (hits / trials) if trials else None
    return {
        "trials": trials,
        "limit_hit_trials": hits,
        "limit_hit_share": share,
        "setup_limited": share is not None and share > 0.05,
    }


