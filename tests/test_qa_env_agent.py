"""Adaptive review must not turn unavailable quota or a lost thread into a pass."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evallab.gepa_optimizer import codex_transport
from evallab.gepa_optimizer.codex_transport import CodexSession, CodexTransport, CodexTransportError
from evallab.qa.env_agent import (
    MAX_OBSERVATION_CHARS,
    AuditBudget,
    EnvAuditSession,
    QuotaUnavailable,
    _bounded,
    parse_action,
    weekly_codex_used,
)


@pytest.mark.parametrize("quota", [0.35, 0.8, None])
def test_closed_or_missing_quota_never_starts_a_model_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quota: float | None,
) -> None:
    root = tmp_path.resolve()
    task = root / "package"
    task.mkdir()
    (task / "instruction.md").write_text("Return 42.\n")

    def read_quota(_: float) -> float:
        if quota is None:
            raise QuotaUnavailable("weekly reading missing")
        return quota

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("model turn started without available subscription quota")

    monkeypatch.setattr(CodexSession, "turn", forbidden)
    session = EnvAuditSession(
        task_dir=task, out_dir=root / "audit", repo_root=root, model="codex/test",
        budget=AuditBudget(60, 100, 2, 0), quota_reader=read_quota,
    )
    result = session.run()
    assert result["status"] == "quota_stop"
    assert result["final"] is None
    assert result["session_id"] is None
    assert result["recorded_findings"] == []
    retained = json.loads(session.record_path.read_text())
    assert retained["status"] == "quota_stop"
    assert retained["used"]["tokens_known"] is True


@pytest.mark.parametrize("value", [None, True, "0.1", float("nan"), -0.1, 1.1])
def test_invalid_quota_readings_are_unavailable_not_zero(
    monkeypatch: pytest.MonkeyPatch, value: Any,
) -> None:
    body = {"reports": [{"provider": "openai-codex", "limits": [
        {"window": {"id": "7d"}, "amount": {"usedFraction": value}},
    ]}]}
    monkeypatch.setattr(
        "evallab.qa.env_agent.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps(body), ""),
    )
    with pytest.raises(QuotaUnavailable):
        weekly_codex_used(20)


@pytest.mark.parametrize("action", [
    {"calls": [{"tool": "bash", "args": {"command": "true"}}]},
    {"calls": [], "final": {"summary": "clean", "limitations": []}},
    {"calls": [{"tool": "task-info", "args": "not an object"}]},
    {"final": {"summary": "", "limitations": []}},
])
def test_invalid_actions_cannot_expand_capabilities_or_create_an_empty_final(action: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_action(json.dumps(action))


def test_large_observation_retains_identity_needed_to_record_cited_evidence() -> None:
    result = _bounded({
        "tool": "task-info", "task_id": "package", "task_digest": "sha256:bound",
        "result": {"source": "x" * (MAX_OBSERVATION_CHARS + 1)},
    })
    assert result["truncated"] is True
    assert result["task_id"] == "package"
    assert result["task_digest"] == "sha256:bound"
    assert result["tool"] == "task-info"


def test_resume_cannot_silently_switch_to_another_native_thread(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    session = CodexSession(CodexTransport(repo_root=root, model="codex/test"), directory=root / "session")
    events = root / "events.jsonl"
    first = "00000000-0000-4000-8000-000000000001"
    second = "00000000-0000-4000-8000-000000000002"
    events.write_text(json.dumps({"type": "thread.started", "thread_id": first}) + "\n")
    session._bind_thread(events, required=True)
    events.write_text(json.dumps({"type": "thread.started", "thread_id": second}) + "\n")
    with pytest.raises(CodexTransportError, match="different thread"):
        session._bind_thread(events, required=True)
    assert session.thread_id == first


def test_preflight_time_cannot_extend_the_model_turn_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    executable = root / "probe-only-cli"
    executable.write_text("unit-test executable identity; never executed")
    monkeypatch.setattr(codex_transport, "CODEX_EXECUTABLE", executable)
    clock = [100.0]
    monkeypatch.setattr(codex_transport.time, "monotonic", lambda: clock[0])
    transport = CodexTransport(repo_root=root, model="codex/test")

    def preflight(**kwargs: Any) -> dict[str, Any]:
        clock[0] += 2
        return {"text_only": {"ephemeral": True}}

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("model process started after preflight spent the entire time budget")

    monkeypatch.setattr(transport, "preflight", preflight)
    monkeypatch.setattr(codex_transport, "_execute", forbidden)
    session = CodexSession(transport, directory=root / "session")
    with pytest.raises(CodexTransportError, match="deadline"):
        session.turn("An ordinary review action", timeout_seconds=1)
    assert session.thread_id is None


@pytest.mark.parametrize("tokens,tool_action,expired,expected", [
    (100, False, False, "final"), (101, False, False, "token_budget_exhausted"),
    (101, True, False, "token_budget_exhausted"),
    (10, False, True, "time_budget_exhausted"),
    (10, True, True, "time_budget_exhausted"),
])
def test_completed_turn_budget_controls_final_and_tool_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokens: int, tool_action: bool,
    expired: bool, expected: str,
) -> None:
    root = tmp_path.resolve()
    task = root / "package"
    task.mkdir()
    (task / "instruction.md").write_text("Return 42.\n")
    action = {"calls": [{"tool": "task-info", "args": {}}]} if tool_action else {
        "final": {"summary": "Budget-boundary unit review.", "limitations": ["No runtime executed."]},
    }

    def completed_turn(self: CodexSession, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.invocations = 1
        self.thread_id = "00000000-0000-4000-8000-000000000001"
        self.cumulative = {"input_tokens": tokens - 1, "output_tokens": 1}
        if expired:
            session.deadline = 0.0
        return {
            "usage": self.cumulative, "invocation": 1, "thread_id": self.thread_id,
            "directory": "unit-turn", "cumulative_usage": self.cumulative,
            "text_selection": "last_agent_message", "response": json.dumps(action),
        }

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("an over-budget action reached a task tool")

    monkeypatch.setattr(CodexSession, "turn", completed_turn)
    monkeypatch.setattr(EnvAuditSession, "_run_call", forbidden)
    session = EnvAuditSession(
        task_dir=task, out_dir=root / "audit", repo_root=root, model="codex/test",
        budget=AuditBudget(60, 100, 2, 0), quota_reader=lambda _: 0.1,
    )
    result = session.run()
    assert result["status"] == expected
    assert result["used"]["tokens"] == tokens
    assert result["recorded_findings"] == []
    if expected != "final":
        assert result["final"] is None


def test_failed_model_turn_does_not_report_free_zero_token_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    task = root / "package"
    task.mkdir()
    (task / "instruction.md").write_text("Return 42.\n")

    def failed_turn(*args: Any, **kwargs: Any) -> None:
        raise CodexTransportError("transport failed before usable usage arrived")

    monkeypatch.setattr(CodexSession, "turn", failed_turn)
    session = EnvAuditSession(
        task_dir=task, out_dir=root / "audit", repo_root=root, model="codex/test",
        budget=AuditBudget(60, 100, 2, 0), quota_reader=lambda _: 0.1,
    )
    result = session.run()
    assert result["status"] == "model_error"
    assert result["used"]["tokens"] is None
    assert result["used"]["tokens_known"] is False
    assert result["final"] is None
