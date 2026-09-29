"""Deterministic, no-LLM Scout scanners for HAR-81 + HAR-90 Harbor traces.

No scanner here calls a model. The file holds two families:

- Text scanners (message text only):
  - ``repeated_assistant_message`` (custom): longest run of consecutive
    assistant messages whose whitespace-normalized text is identical.
    Catches the 0036-e completion-summary loop (~50 identical messages).
  - ``harness_parse_errors`` (grep): Terminus parser notices in
    observation/tool text (``Missing required fields``,
    ``Extra text detected ...``, ``No valid JSON``).
  - ``native_tool_call_markup`` (grep): MiMo native-format tool proposals
    (``<tool_call>`` / ``<function=``) inside assistant message text.
  - ``restored_tool_calls`` (grep): Scout's rendering of STRUCTURED
    ``tool_calls`` (``Tool Call: bash_command`` /
    ``Tool Call: mark_task_complete``). Raw runs used Terminus-2
    ``raw_content`` mode, so structured ``tool_calls`` are empty and this
    scanner reads 0 everywhere; the normalized trajectories restore stock
    ``bash_command`` / ``mark_task_complete`` calls, so it reads positive.
    The raw-vs-normalized gap on this scanner IS the tool-call
    visibility comparison.
- Probe-03 label scanners (deterministic lookup, no model): each
  transcript's trial is looked up in ``probe03_lookup.json`` (built by
  ``build_probe03.py`` from the read-only probe-03 ``capabilities.jsonl``
  files) and the stored labels are re-emitted as results with message
  cites:
  - ``probe03_outcome``: outcome tag/attribution/rule + stop reason.
  - ``probe03_first_failure``: first-failure rule (``none`` when null).
  - ``probe03_wedge``: boolean stuck-terminal (True when probe-03
    records wedge stretches); validated against the blind
    ``har99-wedge`` hand key (see ``validation/wedge_stuck.json``).

Message cites (``[M1]``, ``[M2]``, ...) are 1-based positions over the
full loaded message list, the same convention as Scout's grep scanner,
so cites resolve in Scout View.

Step-ref mapping: Scout's ATIF importer does NOT carry ATIF ``extra``
into messages (verified: ``message.metadata`` is None on every imported
message; only ``schema_version``/``continued_trajectory_ref`` reach
transcript metadata). So probe-03 ``step_ref`` values (``head#12``,
``trajectory.cont-1.json#40``) are mapped to message positions by
re-walking the source trajectory file named in
``transcript.source_uri``: each step yields its own message plus one
message per ``observation.results`` entry, except ``system`` steps with
``extra.context_management`` which yield none (verified: the walk
predicts the stored ``message_count`` exactly, e.g. 206/206 on the
stitched new_session trial har81-p-d-arvo-41330). On normalized files
the walk keys come from ``extra.trace_lab.ref`` (identical to the
probe-03 refs by construction); on raw files the keys are
``<filename>#<step_id>`` with ``head`` aliasing ``trajectory.json``,
so only refs from the same fragment resolve and the rest are listed
in ``metadata.unmapped_refs``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from inspect_scout import Result, Scanner, Transcript, grep_scanner, scanner
from inspect_scout._scanner.result import Reference

_WS = re.compile(r"\s+")
_HERE = Path(__file__).resolve().parent
_LOOKUP_PATH = _HERE / "probe03_lookup.json"
_LOOKUP: dict | None = None
_REFMAP_CACHE: dict[str, dict[str, int]] = {}


def _normalize(text: str | None) -> str:
    """Collapse all whitespace; empty/None becomes empty string."""
    return _WS.sub(" ", text or "").strip()


@scanner(messages="all")
def repeated_assistant_message() -> Scanner[Transcript]:
    """Longest run of consecutive identical assistant messages.

    Value is the longest run length (0 when there are no assistant
    messages or no run of length >= 2). References point at every
    message in the longest run. Only non-empty normalized texts count:
    consecutive empty messages are not a loop.
    """

    async def scan(transcript: Transcript) -> Result:
        messages = transcript.messages or []
        assistants = [
            (index, message)
            for index, message in enumerate(messages, start=1)
            if message.role == "assistant"
        ]
        if not assistants:
            return Result(
                value=0,
                explanation="no assistant messages in transcript",
                references=[],
                metadata={"longest_run": 0, "n_assistant": 0},
            )

        normalized = [_normalize(message.text) for _, message in assistants]
        best_len, best_start = 1, 0
        cur_len, cur_start = 1, 0
        for i in range(1, len(normalized)):
            if normalized[i] and normalized[i] == normalized[i - 1]:
                cur_len += 1
            else:
                if cur_len > best_len:
                    best_len, best_start = cur_len, cur_start
                cur_len, cur_start = 1, i
        if cur_len > best_len:
            best_len, best_start = cur_len, cur_start

        if best_len < 2:
            return Result(
                value=0,
                explanation=(
                    f"no repeated consecutive assistant messages "
                    f"({len(assistants)} assistant messages checked)"
                ),
                references=[],
                metadata={"longest_run": 1, "n_assistant": len(assistants)},
            )

        run = assistants[best_start : best_start + best_len]
        start_cite, end_cite = f"[M{run[0][0]}]", f"[M{run[-1][0]}]"
        preview = (normalized[best_start] or "")[:160]
        return Result(
            value=best_len,
            explanation=(
                f"longest run of {best_len} identical consecutive assistant "
                f"messages from {start_cite} to {end_cite} "
                f"({len(assistants)} assistant messages checked): "
                f"{preview}..."
            ),
            references=[
                Reference(type="message", cite=f"[M{index}]", id=message.id)
                for index, message in run
                if message.id is not None
            ],
            metadata={
                "longest_run": best_len,
                "run_start_cite": start_cite,
                "run_end_cite": end_cite,
                "n_assistant": len(assistants),
            },
        )

    return scan


@scanner(messages="all")
def harness_parse_errors() -> Scanner[Transcript]:
    """Terminus parser notices in observation/tool/user text.

    One result per label; each value counts match occurrences (a message
    containing the phrase twice counts twice). The ``extra_text_before``
    label extends the required set: it is the dominant warning in 0036,
    where only the "after" variant was named.
    """
    return grep_scanner(
        {
            "missing_required_fields": ["Missing required fields"],
            "extra_text_after_json": ["Extra text detected after JSON object"],
            "extra_text_before_json": ["Extra text detected before JSON object"],
            "no_valid_json": ["No valid JSON"],
        }
    )


@scanner(messages=["assistant"])
def native_tool_call_markup() -> Scanner[Transcript]:
    """MiMo native-format tool proposals inside assistant message text.

    Terminus-2 ran in ``raw_content`` mode, so commands live in message
    text (``<tool_call><function=bash ...>``) with structured
    ``tool_calls`` empty. One result per label; values count match
    occurrences. Cites are 1-based over assistant messages only (this
    scanner's loaded message list).
    """
    return grep_scanner(
        {
            "tool_call_tag": ["<tool_call>"],
            "function_marker": ["<function="],
        }
    )

@scanner(messages="all")
def restored_tool_calls() -> Scanner[Transcript]:
    """Structured tool calls as Scout renders them in message text.

    Scout's message rendering appends ``Tool Call: <function>`` plus the
    arguments to any assistant message carrying structured ``tool_calls``.
    Raw Terminus-2 runs (``raw_content`` mode) have empty ``tool_calls``,
    so this reads 0 on the raw DB; the normalized trajectories restore
    stock ``bash_command`` / ``mark_task_complete`` calls, so it reads
    positive there. One result per label; values count occurrences.
    """
    return grep_scanner(
        {
            "bash_command": ["Tool Call: bash_command"],
            "mark_task_complete": ["Tool Call: mark_task_complete"],
        }
    )


def _lookup() -> dict:
    """Probe-03 labels by trial dir name (loaded once per worker)."""
    global _LOOKUP
    if _LOOKUP is None:
        _LOOKUP = json.loads(_LOOKUP_PATH.read_text())
    return _LOOKUP


def _trial_of(transcript: Transcript) -> str | None:
    """Trial dir name from the source trajectory path.

    Both DBs keep the Harbor layout ``.../<job>/<trial>/agent/<file>``,
    so the trial is the parent of the ``agent`` dir.
    """
    uri = transcript.source_uri or ""
    parts = Path(uri).parts
    if "agent" in parts:
        return parts[parts.index("agent") - 1]
    return None


def _ref_index_map(traj_path: str) -> dict[str, int]:
    """Probe-03 step ref -> 1-based message index for one trajectory file.

    Mirrors Scout's ATIF import: each step yields its own message plus one
    per ``observation.results`` entry, except ``system`` steps carrying
    ``extra.context_management`` (compaction markers), which yield none.
    Keys are ``extra.trace_lab.ref`` when present (normalized files) plus
    ``<filename>#<step_id>``, with ``head`` aliasing ``trajectory.json``
    (raw fragments).
    """
    cached = _REFMAP_CACHE.get(traj_path)
    if cached is not None:
        return cached
    data = json.loads(Path(traj_path).read_text())
    filename = Path(traj_path).name
    mapping: dict[str, int] = {}
    position = 0
    for step in data.get("steps") or []:
        extra = step.get("extra") or {}
        if step.get("source") == "system" and "context_management" in extra:
            continue
        position += 1
        step_id = step.get("step_id")
        keys = {f"{filename}#{step_id}"}
        if filename == "trajectory.json":
            keys.add(f"head#{step_id}")
        trace_lab = extra.get("trace_lab") if isinstance(extra, dict) else None
        if isinstance(trace_lab, dict) and trace_lab.get("ref"):
            keys.add(str(trace_lab["ref"]))
        for key in keys:
            mapping.setdefault(key, position)
        observation = step.get("observation")
        results = observation.get("results") if isinstance(observation, dict) else None
        position += len(results) if isinstance(results, list) else 0
    _REFMAP_CACHE[traj_path] = mapping
    return mapping


def _cite_refs(
    transcript: Transcript, ref_map: dict[str, int], refs: list[str | None]
) -> tuple[list[Reference], list[str]]:
    """Build message references for probe-03 refs; report unmapped ones."""
    references: list[Reference] = []
    unmapped: list[str] = []
    messages = transcript.messages or []
    for ref in refs:
        if not ref:
            continue
        index = ref_map.get(str(ref))
        if index is None or index < 1 or index > len(messages):
            unmapped.append(str(ref))
            continue
        message = messages[index - 1]
        if message.id is None:
            unmapped.append(str(ref))
            continue
        references.append(Reference(type="message", cite=f"[M{index}]", id=message.id))
    return references, unmapped

def _cite_text(
    transcript: Transcript, ref_map: dict[str, int], refs: list[str | None]
) -> tuple[str, list[Reference], list[str]]:
    """Human cite string plus references for probe-03 refs.

    Returns text like ``head#16 ([M30])`` per ref (``head#16 (other file,
    not in this transcript)`` when unmapped) together with the Reference
    list and the unmapped refs. The ``[Mn]`` cites in the text are what
    Scout View turns into clickable message links.
    """
    references, unmapped = _cite_refs(transcript, ref_map, refs)
    by_cite = {reference.cite for reference in references}
    index_by_ref = {str(ref): ref_map.get(str(ref)) for ref in refs if ref}
    parts: list[str] = []
    for ref in refs:
        if not ref:
            continue
        index = index_by_ref.get(str(ref))
        cite = f"[M{index}]" if index else None
        if cite and cite in by_cite:
            parts.append(f"{ref} ({cite})")
        else:
            parts.append(f"{ref} (not in this transcript)")
    return "; ".join(parts), references, unmapped


@scanner(messages="all")
def probe03_outcome() -> Scanner[Transcript]:
    """Probe-03 outcome-relevant failure + stop reason for this trial."""

    async def scan(transcript: Transcript) -> Result:
        trial = _trial_of(transcript)
        row = _lookup().get(trial) if trial else None
        if row is None:
            return Result(
                value="unknown",
                explanation=f"no probe-03 row for trial {trial!r}",
                references=[],
                metadata={"trial": trial},
            )
        outcome = row["outcome"] or {}
        stop = row.get("stop") or {}
        refs = list(outcome.get("evidence_step_refs") or [])
        if outcome.get("step_ref") and outcome["step_ref"] not in refs:
            refs.append(outcome["step_ref"])
        uri = transcript.source_uri or ""
        try:
            ref_map = _ref_index_map(uri)
        except (OSError, ValueError):
            ref_map = {}
        cite_text, references, unmapped = _cite_text(transcript, ref_map, refs)
        value = "/".join(
            str(outcome.get(k) or "none") for k in ("tag", "attribution", "rule_id")
        )
        return Result(
            value=value,
            explanation=(
                f"probe-03 {outcome.get('rule_id')}: {outcome.get('note')} "
                f"Evidence: {cite_text}. "
                f"Stop: {stop.get('reason')} "
                f"({stop.get('exception_type') or 'no exception'})."
            ),
            references=references,
            metadata={
                "trial": trial,
                "tag": outcome.get("tag"),
                "attribution": outcome.get("attribution"),
                "rule_id": outcome.get("rule_id"),
                "stop_reason": stop.get("reason"),
                "exception_type": stop.get("exception_type"),
                "natural_completion": stop.get("natural_completion"),
                "unmapped_refs": unmapped,
            },
        )

    return scan


@scanner(messages="all")
def probe03_first_failure() -> Scanner[Transcript]:
    """Probe-03 first-failure rule for this trial (``none`` when null)."""

    async def scan(transcript: Transcript) -> Result:
        trial = _trial_of(transcript)
        row = _lookup().get(trial) if trial else None
        if row is None:
            return Result(
                value="unknown",
                explanation=f"no probe-03 row for trial {trial!r}",
                references=[],
                metadata={"trial": trial},
            )
        first = row.get("first_failure")
        if not first:
            return Result(
                value="none",
                explanation=(
                    "probe-03 first_failure is null: the execution is "
                    "mechanically clean and the outcome explains the trial."
                ),
                references=[],
                metadata={"trial": trial, "recovered": None},
            )
        refs = list(first.get("evidence_step_refs") or [])
        if first.get("step_ref") and first["step_ref"] not in refs:
            refs.append(first["step_ref"])
        uri = transcript.source_uri or ""
        try:
            ref_map = _ref_index_map(uri)
        except (OSError, ValueError):
            ref_map = {}
        cite_text, references, unmapped = _cite_text(transcript, ref_map, refs)
        return Result(
            value=str(first.get("rule_id") or "none"),
            explanation=(
                f"probe-03 first failure {first.get('rule_id')} "
                f"({first.get('tag')}/{first.get('attribution')}) "
                f"at {first.get('step_ref')}: {first.get('note')} "
                f"Evidence: {cite_text}. "
                f"Recovered: {first.get('recovered')}."
            ),
            references=references,
            metadata={
                "trial": trial,
                "tag": first.get("tag"),
                "attribution": first.get("attribution"),
                "rule_id": first.get("rule_id"),
                "recovered": first.get("recovered"),
                "unmapped_refs": unmapped,
            },
        )

    return scan


@scanner(messages="all")
def probe03_wedge() -> Scanner[Transcript]:
    """Probe-03 wedge (stuck-terminal) flag for this trial.

    Boolean True when probe-03 records wedge stretches. Validated against
    the blind har99-wedge hand key at the trial level; note the in-sample
    caveat (probe-03 scored its wedge measurement against that key).
    """

    async def scan(transcript: Transcript) -> Result:
        trial = _trial_of(transcript)
        row = _lookup().get(trial) if trial else None
        if row is None:
            return Result(
                value="unknown",
                explanation=f"no probe-03 row for trial {trial!r}",
                references=[],
                metadata={"trial": trial},
            )
        stretches = (row.get("wedge") or {}).get("stretches") or []
        refs: list[str | None] = []
        for stretch in stretches:
            refs.extend(
                [
                    stretch.get("trigger_ref"),
                    stretch.get("start_ref"),
                    stretch.get("end_ref"),
                    stretch.get("interrupt_ref"),
                ]
            )
        uri = transcript.source_uri or ""
        try:
            ref_map = _ref_index_map(uri)
        except (OSError, ValueError):
            ref_map = {}
        if not stretches:
            return Result(
                value=False,
                explanation="probe-03 records no wedge (stuck-terminal) stretch.",
                references=[],
                metadata={"trial": trial, "n_stretches": 0},
            )
        cite_text, references, unmapped = _cite_text(transcript, ref_map, refs)
        summary = "; ".join(
            f"{s.get('cause')} {s.get('start_ref')}->{s.get('end_ref')}"
            f" ({s.get('turns')} turns)"
            for s in stretches
        )
        return Result(
            value=True,
            explanation=(
                f"probe-03 wedge stretch(es): {summary}. "
                f"Cites: {cite_text}."
            ),
            references=references,
            metadata={
                "trial": trial,
                "n_stretches": len(stretches),
                "stretches": stretches,
                "unmapped_refs": unmapped,
            },
        )

    return scan
