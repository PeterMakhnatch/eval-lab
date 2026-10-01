"""HAR-127 G3: an exported row matches only its own trial's captured call."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

FIDELITY = Path("research/experiments/ovn-sft-v0/fidelity.py")


def _load():
    spec = importlib.util.spec_from_file_location("ovn_fidelity", FIDELITY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HISTORY = [
    {"role": "user", "content": "task"},
    {"role": "assistant", "content": "<tool_call>ls</tool_call>"},
    {"role": "user", "content": "a b"},
]
TARGET = {"role": "assistant", "content": "done", "reasoning_content": "check"}


def _call(messages, content="done", reasoning="check", *, status=200, error=None):
    return {
        "request_body": {"messages": messages},
        "response_status": status,
        "error": error,
        "response_body": {
            "choices": [{"message": {"content": content, "reasoning_content": reasoning}}]
        },
    }


def test_identical_call_passes_and_any_byte_difference_fails() -> None:
    verdict = _load().capture_verdict
    first = _call(HISTORY[:1], content="<tool_call>ls</tool_call>", reasoning="")
    assert verdict(HISTORY, TARGET, [first, _call(HISTORY)]) == "identical"
    changed = [*HISTORY[:2], {"role": "user", "content": "a  b"}]
    assert verdict(HISTORY, TARGET, [_call(changed)]) == "differs_at_message_2"
    assert verdict(HISTORY, TARGET, [_call(HISTORY, content="done.")]) == "differs_at_target"
    assert verdict(HISTORY, TARGET, [_call(HISTORY, reasoning="")]) == "differs_at_target"


def test_missing_or_retried_call_is_not_identical() -> None:
    verdict = _load().capture_verdict
    assert verdict(HISTORY, TARGET, [_call(HISTORY[:1])]) == "missing"
    # A retried call at the same position: the export cannot say which reply it trained on.
    assert verdict(HISTORY, TARGET, [_call(HISTORY), _call(HISTORY, content="x")]) == "ambiguous"


def test_only_a_delivered_call_can_match() -> None:
    # Cdx 3: identical bytes on a disconnected call must not count as delivered.
    verdict = _load().capture_verdict
    disconnected = _call(HISTORY, error={"kind": "client_disconnect"})
    assert verdict(HISTORY, TARGET, [disconnected]) == "undelivered"
    assert verdict(HISTORY, TARGET, [_call(HISTORY, status=502)]) == "undelivered"
    # A delivered retry after the disconnect is the call the agent saw.
    assert verdict(HISTORY, TARGET, [disconnected, _call(HISTORY)]) == "identical"


def test_two_attempts_with_the_same_prompt_each_match_their_own_capture() -> None:
    # Cdx 3's control: identical prompts, different one-token replies, one file per trial.
    verdict = _load().capture_verdict
    trial_a = [_call(HISTORY, content="done")]
    trial_b = [_call(HISTORY, content="fail")]
    assert verdict(HISTORY, TARGET, trial_a) == "identical"
    assert verdict(HISTORY, {**TARGET, "content": "fail"}, trial_b) == "identical"
    assert verdict(HISTORY, TARGET, trial_b) == "differs_at_target"
