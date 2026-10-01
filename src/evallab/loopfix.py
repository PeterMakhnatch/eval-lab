"""HAR-116 loop break and output cap, shared by replay and the live agent.

The detector reuses the HAR-114 onset rule exactly
(:func:`evallab.token_flow.normalized_command` for commands,
``probe03.LOOP_MIN_RUN`` for messages, and the same edit-free guard on a
command run), so a run the offline replay would flag is the run the live
agent flags. What this module adds is the response to that detection:

One nudge, ``LOOP_NUDGE_MESSAGE``, on the call the run first reaches its
length; a stop ``grace_calls`` later, but only when the repetition never
broke. One call whose signature differs, or one edit-like call, ends it:
the agent keeps running and the nudge is recorded as broken. The run
lengths and the grace window are parameters (``command_run_min``,
``message_run_min``, ``grace_calls``) defaulting to the HAR-114/116
constants, so a replay sweep and the live agent share the exact rule.

The output cap is the other half of the variant: at most
``OUTPUT_CAP_CHARS`` characters of terminal output go back into the prompt,
head and tail, with a marker naming the sandbox file that holds the whole
output.
"""

from __future__ import annotations

from typing import Any

from evallab.probe03 import LOOP_MIN_RUN
from evallab.token_flow import COMMAND_RUN_MIN, _is_edit, _step_calls, normalized_command

#: Characters of terminal output fed back per step. The full output is
#: written beside it; the prompt keeps this much, split head and tail.
OUTPUT_CAP_CHARS = 2000

#: Sandbox path of one step's full output. ``{episode}`` is the 1-based
#: agent call, so the agent can grep earlier steps too.
OUTPUT_SPILL_PATH = "/logs/agent/evallab-output/step-{episode:04d}.txt"

#: The one nudge. Deliberately short and fixed: the detector's job is to
#: say the loop was noticed, not to coach.
LOOP_NUDGE_MESSAGE = "you are repeating; change approach or finish"

#: Calls after the nudge during which the repetition must break. Still
#: looping on the call this many later, the agent phase ends and the
#: verifier runs.
LOOP_GRACE_CALLS = 5

#: Trajectory/agent-metadata key for the loop-break record.
LOOP_BREAK_KEY = "loop_break"

#: The stop recorded when the grace window expires without a break.
LOOP_STOP_REASON = "loop_break"


def _message_text(step: dict[str, Any]) -> str:
    message = step.get("message")
    return message.strip() if isinstance(message, str) else ""


def _command_signature(step: dict[str, Any]) -> str:
    texts = [text for _, text in _step_calls(step) if text.strip()]
    return normalized_command(" ".join(texts)) if texts else ""


def step_features(step: dict[str, Any]) -> dict[str, Any]:
    """The detector's view of one agent step: signature, message, edit flag."""
    return {
        "signature": _command_signature(step),
        "message": _message_text(step),
        "edit": _is_edit(step)[0],
    }


def _run_length(values: list[str], end: int) -> int:
    """Length of the trailing run of ``values[end]`` ending at ``end``."""
    current = values[end]
    if not current:
        return 0
    start = end
    while start > 0 and values[start - 1] == current:
        start -= 1
    return end - start + 1


def _onset(
    features: list[dict[str, Any]],
    *,
    command_run_min: int = COMMAND_RUN_MIN,
    message_run_min: int = LOOP_MIN_RUN,
) -> dict[str, Any] | None:
    """First call at which a run reaches its length, or None.

    Command onset needs ``command_run_min`` identical normalized signatures
    with no edit-like step inside the run; message onset needs
    ``message_run_min`` identical stripped messages. The earlier wins; both at
    the same call are reported as ``both``.
    """
    signatures = [item["signature"] for item in features]
    messages = [item["message"] for item in features]
    edits = [item["edit"] for item in features]
    command_at: int | None = None
    message_at: int | None = None
    for index in range(len(features)):
        if command_at is None and signatures[index]:
            length = _run_length(signatures, index)
            start = index - length + 1
            if length >= command_run_min and not any(edits[start : index + 1]):
                command_at = index
        if (
            message_at is None
            and messages[index]
            and _run_length(messages, index) >= message_run_min
        ):
            message_at = index
        if command_at is not None and message_at is not None:
            break
    found = [item for item in (command_at, message_at) if item is not None]
    if not found:
        return None
    at = min(found)
    kinds = []
    if command_at == at:
        kinds.append("normalized_command_run")
    if message_at == at:
        kinds.append("identical_message_run")
    return {"call_index": at + 1, "detector": "both" if len(kinds) == 2 else kinds[0]}


def repetition_broken(features: list[dict[str, Any]], onset_index: int, at: int) -> bool:
    """Whether any call after the onset through ``at`` broke the repetition.

    A break is an edit-like call, or a call whose signature and message both
    differ from the repeated one. Empty signatures and messages never count
    as a break on their own: a parse-error turn has neither.
    """
    signature = features[onset_index]["signature"]
    message = features[onset_index]["message"]
    for item in features[onset_index + 1 : at + 1]:
        if item["edit"]:
            return True
        same_command = bool(signature) and item["signature"] == signature
        same_message = bool(message) and item["message"] == message
        if not same_command and not same_message:
            return True
    return False


def loop_decision(
    steps: list[dict[str, Any]],
    *,
    command_run_min: int = COMMAND_RUN_MIN,
    message_run_min: int = LOOP_MIN_RUN,
    grace_calls: int = LOOP_GRACE_CALLS,
) -> dict[str, Any]:
    """What the loop break does over a finished sequence of agent steps.

    ``nudge_call`` is the 1-based call the run first reached its length — the
    call that gets the nudge. ``stop_call`` is ``nudge_call +
    grace_calls`` when the repetition held through it, else None, and
    ``broke_at_call`` is the first call that broke it. A sequence shorter
    than the grace window reports neither: the decision is still open.
    """
    features = [step_features(step) for step in steps]
    onset = _onset(features, command_run_min=command_run_min, message_run_min=message_run_min)
    decision: dict[str, Any] = {
        "nudge_call": None,
        "detector": None,
        "stop_call": None,
        "broke_at_call": None,
    }
    if onset is None:
        return decision
    onset_index = onset["call_index"] - 1
    decision["nudge_call"] = onset["call_index"]
    decision["detector"] = onset["detector"]
    for index in range(onset_index + 1, len(features)):
        if repetition_broken(features, onset_index, index):
            decision["broke_at_call"] = index + 1
            return decision
        if index - onset_index == grace_calls:
            decision["stop_call"] = index + 1
            return decision
    return decision


def live_loop_action(
    steps: list[dict[str, Any]],
    *,
    command_run_min: int = COMMAND_RUN_MIN,
    message_run_min: int = LOOP_MIN_RUN,
    grace_calls: int = LOOP_GRACE_CALLS,
) -> str | None:
    """The live agent's action after the latest call: nudge, stop, or none.

    ``steps`` includes the call just taken. A nudge fires once, on the onset
    call; a stop fires on the grace-window call only while the repetition
    still holds. Anything else — no onset, an onset already nudged, a broken
    repetition — returns None and the loop continues untouched.
    """
    features = [step_features(step) for step in steps]
    if not features:
        return None
    onset = _onset(features, command_run_min=command_run_min, message_run_min=message_run_min)
    if onset is None:
        return None
    onset_index = onset["call_index"] - 1
    latest = len(features) - 1
    if latest == onset_index:
        return "nudge"
    if latest <= onset_index:
        return None
    if repetition_broken(features, onset_index, latest):
        return None
    if latest - onset_index == grace_calls:
        return "stop"
    return None


def cap_output(output: str, spill_path: str, *, limit: int = OUTPUT_CAP_CHARS) -> str:
    """Head and tail of ``output`` within ``limit`` characters, plus a marker.

    Under the limit the output is returned unchanged: the marker would only
    add prompt text the agent does not need. Over it, the kept characters
    split as evenly as possible, head first, and the marker names the file
    holding everything and how many characters were cut.
    """
    if limit < 2:
        raise ValueError("output cap must keep at least a head and a tail")
    if len(output) <= limit:
        return output
    head_chars = limit // 2
    tail_chars = limit - head_chars
    omitted = len(output) - head_chars - tail_chars
    return (
        output[:head_chars]
        + "\n[... output limited to "
        + f"{limit} characters; {omitted} characters omitted. "
        + f"Full output: {spill_path} — grep or read it there ...]\n"
        + output[-tail_chars:]
    )


def loop_break_record(
    *,
    fired: bool,
    nudge_call: int | None,
    detector: str | None,
    stop_call: int | None,
    model_next: str | None,
) -> dict[str, Any]:
    """The record written to trajectory and agent metadata."""
    return {
        "fired": fired,
        "nudge_call": nudge_call,
        "detector": detector,
        "stop_call": stop_call,
        "model_next": model_next,
    }
