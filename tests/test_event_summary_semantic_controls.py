"""In-process semantic controls for the event-summary verifier.

Loads ``library/tasks/event-summary/tests/verify.py`` from its file path,
points its ``TRUSTED_INPUT``/``AGENT_INPUT``/``AGENT_OUTPUT``/``LOG_DIR``
globals at ``tmp_path`` locations, drives ``verify.main()``, and reads the
``reward`` float from ``LOG_DIR/reward.json``.

Arm list (labels fixed before observing any reward):

* Positive control (expect reward 1): ``positive_control`` — exact
  ``expected_summary()`` JSON with sorted ``counts`` and a trailing newline.
* Invalid arms (expect reward 0): ``missing_summary``, ``extra_key``,
  ``wrong_total_events``, ``input_bytes_changed``, ``extra_file``.
* Instruction-required representation arms (expect reward 1 from this
  verifier, recorded as false-accepts versus ``instruction.md`` because the
  verifier uses ``json.loads`` + ``dict ==`` and therefore cannot see key
  order or a missing trailing newline): ``counts_not_alphabetical``,
  ``no_trailing_newline``. These arms document verifier blindness; they are
  not a license to edit the verifier.

``semantic_control_summary`` reports ``false_accept_n`` /
``instruction_representation_n`` and ``false_reject_n`` / ``invalid_n``.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY_PATH = REPO_ROOT / "library" / "tasks" / "event-summary" / "tests" / "verify.py"
TRUSTED_SOURCE = (
    REPO_ROOT / "library" / "tasks" / "event-summary" / "environment" / "events.jsonl"
)

INVALID_ARMS: tuple[str, ...] = (
    "missing_summary",
    "extra_key",
    "wrong_total_events",
    "input_bytes_changed",
    "extra_file",
)
INSTRUCTION_REPRESENTATION_ARMS: tuple[str, ...] = (
    "counts_not_alphabetical",
    "no_trailing_newline",
)
POSITIVE_ARMS: tuple[str, ...] = ("positive_control",)

EXPECTED_REWARDS: dict[str, float] = {
    "positive_control": 1.0,
    "missing_summary": 0.0,
    "extra_key": 0.0,
    "wrong_total_events": 0.0,
    "input_bytes_changed": 0.0,
    "extra_file": 0.0,
    "counts_not_alphabetical": 1.0,
    "no_trailing_newline": 1.0,
}


def _load_verify() -> ModuleType:
    assert VERIFY_PATH.is_file(), f"verifier not found at {VERIFY_PATH}"
    assert TRUSTED_SOURCE.is_file(), f"trusted source not found at {TRUSTED_SOURCE}"
    spec = importlib.util.spec_from_file_location("event_summary_verify_inprocess", VERIFY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_arm(tmp_path: Path, arm: str) -> float:
    """Set up tmp input/output dirs for one arm, run verify.main(), return reward."""
    trusted_bytes = TRUSTED_SOURCE.read_bytes()
    trusted = tmp_path / "trusted" / "events.jsonl"
    trusted.parent.mkdir(parents=True, exist_ok=True)
    trusted.write_bytes(trusted_bytes)

    agent_input = tmp_path / "app" / "input" / "events.jsonl"
    agent_input.parent.mkdir(parents=True, exist_ok=True)
    output_dir = tmp_path / "app" / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = tmp_path / "logs"

    verify = _load_verify()
    verify.TRUSTED_INPUT = trusted  # type: ignore[attr-defined]
    verify.AGENT_INPUT = agent_input  # type: ignore[attr-defined]
    verify.AGENT_OUTPUT = output_dir / "summary.json"  # type: ignore[attr-defined]
    verify.LOG_DIR = log_dir  # type: ignore[attr-defined]
    expected = verify.expected_summary()  # type: ignore[attr-defined]

    # Default: agent input preserves the trusted bytes exactly.
    agent_input.write_bytes(trusted_bytes)
    candidate = output_dir / "summary.json"

    if arm == "positive_control":
        candidate.write_text(json.dumps(expected) + "\n", encoding="utf-8")
    elif arm == "missing_summary":
        if candidate.is_file():
            candidate.unlink()
    elif arm == "extra_key":
        bad = dict(expected)
        bad["extra"] = 1
        candidate.write_text(json.dumps(bad) + "\n", encoding="utf-8")
    elif arm == "wrong_total_events":
        bad = dict(expected)
        bad["total_events"] = int(expected["total_events"]) + 1  # type: ignore[arg-type]
        candidate.write_text(json.dumps(bad) + "\n", encoding="utf-8")
    elif arm == "input_bytes_changed":
        agent_input.write_bytes(
            trusted_bytes + b'{"event_id":"evt-009","kind":"request","duration_ms":1}\n'
        )
        candidate.write_text(json.dumps(expected) + "\n", encoding="utf-8")
    elif arm == "extra_file":
        candidate.write_text(json.dumps(expected) + "\n", encoding="utf-8")
        (output_dir / "notes.txt").write_text("extra file violates output hygiene\n")
    elif arm == "counts_not_alphabetical":
        counts = dict(expected["counts"])  # type: ignore[arg-type]
        reversed_names = sorted(counts, reverse=True)
        assert reversed_names != sorted(counts), "fixture needs >1 kind to reorder"
        payload = dict(expected)
        payload["counts"] = {name: counts[name] for name in reversed_names}
        candidate.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    elif arm == "no_trailing_newline":
        text = json.dumps(expected)
        assert not text.endswith("\n")
        candidate.write_text(text, encoding="utf-8")
    else:  # pragma: no cover - programming error
        raise AssertionError(f"unknown arm: {arm}")

    verify.main()  # type: ignore[attr-defined]
    reward_doc = json.loads((log_dir / "reward.json").read_text(encoding="utf-8"))
    return float(reward_doc["reward"])


def semantic_control_summary(rewards: Mapping[str, float]) -> dict[str, int]:
    """Summarise arm outcomes against independently labeled cases.

    False acceptance is an invalid or instruction-violating candidate with reward 1.
    False rejection is a known-valid positive control with reward != 1.
    """
    return {
        "false_accept_representation_n": sum(
            1 for arm in INSTRUCTION_REPRESENTATION_ARMS if rewards[arm] == 1.0
        ),
        "instruction_representation_n": len(INSTRUCTION_REPRESENTATION_ARMS),
        "false_accept_invalid_n": sum(1 for arm in INVALID_ARMS if rewards[arm] != 0.0),
        "invalid_n": len(INVALID_ARMS),
        "false_reject_valid_n": sum(1 for arm in POSITIVE_ARMS if rewards[arm] != 1.0),
        "valid_n": len(POSITIVE_ARMS),
    }


def test_positive_control_reward_1(tmp_path: Path) -> None:
    assert _run_arm(tmp_path, "positive_control") == 1.0


@pytest.mark.parametrize("arm", list(INVALID_ARMS))
def test_invalid_arms_reward_0(tmp_path: Path, arm: str) -> None:
    assert _run_arm(tmp_path, arm) == 0.0


@pytest.mark.parametrize("arm", list(INSTRUCTION_REPRESENTATION_ARMS))
def test_instruction_representation_false_accepts(tmp_path: Path, arm: str) -> None:
    """Verifier accepts these despite instruction.md requiring sorted keys/newline.

    ``json.loads`` + ``dict ==`` is blind to ``counts`` key order and to a
    missing trailing newline, so the reward stays 1.0. Recorded here as a
    false-accept versus the instruction, not as verifier approval to change.
    """
    assert _run_arm(tmp_path, arm) == 1.0


def test_semantic_control_summary_counts(tmp_path: Path) -> None:
    rewards = {arm: _run_arm(tmp_path / arm, arm) for arm in EXPECTED_REWARDS}
    assert rewards == EXPECTED_REWARDS
    assert semantic_control_summary(rewards) == {
        "false_accept_representation_n": 2,
        "instruction_representation_n": 2,
        "false_accept_invalid_n": 0,
        "invalid_n": 5,
        "false_reject_valid_n": 0,
        "valid_n": 1,
    }
