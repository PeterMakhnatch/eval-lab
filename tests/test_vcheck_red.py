"""Offline tests for the VerifierCheck red agent (no models, no Docker)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from evallab import vcheck_red as red_mod
from evallab.vcheck_red import (
    RED_SYSTEM,
    RED_TOOL_NAMES,
    TaskContext,
    build_red_system,
    coerce_task_ctx,
    run_red,
)

# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


class FakeGrade:
    """Package-bound ``(submission) -> GradeResult`` fake with a call ledger."""

    def __init__(self, reward: float = 1.0, status: str = "ok") -> None:
        self.calls: list[Any] = []
        self.reward = reward
        self.status = status

    def __call__(self, submission: Any) -> Any:
        self.calls.append(submission)
        return SimpleNamespace(
            reward=self.reward,
            status=self.status,
            verifier_outputs={"reward": self.reward},
            job_dir=None,
            script="fake-grade",
        )


class FakeProbe:
    """``(hint) -> outcome`` fake recording hints verbatim."""

    def __init__(self, reward: float = 0.0) -> None:
        self.hints: list[str] = []
        self.reward = reward

    def __call__(self, hint: str) -> dict[str, Any]:
        self.hints.append(hint)
        return {"transcript": [{"role": "blue", "hint": hint}], "reward": self.reward}


class ScriptedLoopClient:
    """Fake CLIENT exposing the real ``run_tool_loop`` signature; runs a script."""

    def __init__(self, script: Any, on_messages: Any = None) -> None:
        self.script = script
        self.on_messages = on_messages
        self.seen: list[dict[str, Any]] = []

    def run_tool_loop(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: dict[str, Any],
        budget: Any = None,
        run_dir: Any = None,
        records: Any = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        max_steps: int = 30,
        key_provider: Any = None,
        reasoning_effort: str = "low",
        timeout_s: float = 120.0,
    ) -> tuple[list[Any], list[Any]]:
        self.seen.append({"model": model, "max_steps": max_steps, "tools": sorted(tools)})
        if self.on_messages is not None:
            self.on_messages(messages)
        return self.script(tools)


def _standard_script(grade_payload: Any = None, probe: bool = True, finish: bool = True) -> Any:
    """Simulate: probe -> grade(wrong, accepted) -> record -> finish."""

    def _run(tools: dict[str, Any]) -> tuple[list[Any], list[Any]]:
        if probe:
            tools["probe_blue"][1]({"hint": "checker only looks at exit code"})
        grade_out = tools["grade"][1](
            {"submission": grade_payload or {"kind": "file", "path": "out.txt", "content": "wrong"}}
        )
        assert grade_out["ok"], grade_out
        rec_out = tools["record_hypothesis"][1](
            {
                "statement": "Empty output accepted because the checker only asserts exit code (grade.py:12).",
                "requirement_ids": ["REQ-1"],
                "submission_ref": grade_out["submission_ref"],
                "defect_family": "insufficient-checking",
                "defect_class": "missing-behavioral-assertion",
            }
        )
        assert rec_out["filed"], rec_out
        if finish:
            tools["finish"][1]({"summary": "filed one finding"})
        return [], []

    return _run


# ---------------------------------------------------------------------------
# System prompt + schemas.
# ---------------------------------------------------------------------------


def test_broadcast_hints_appear_verbatim_in_system_message() -> None:
    hints = ["verifiers of family X fail to check Y", "second theory: narrow inputs"]
    system = build_red_system({"task": "demo-task"}, hints)
    for hint in hints:
        assert hint in system
    assert "demo-task" in system


def test_system_prompt_encodes_audit_rules() -> None:
    assert "honest" in RED_SYSTEM
    assert "file:line" in RED_SYSTEM
    assert "without a graded submission" in " ".join(RED_SYSTEM.split())
    assert "Universe theories to test" in RED_SYSTEM
    assert "missing-behavioral-assertion" in RED_SYSTEM


def test_tool_set_exact_and_schemas_valid() -> None:
    assert set(RED_TOOL_NAMES) == {
        "read_file",
        "list_files",
        "grade",
        "probe_blue",
        "record_hypothesis",
        "finish",
    }
    ctx = TaskContext(task="t")
    session = red_mod._RedSession(ctx, grade_fn=FakeGrade(), probe_fn=FakeProbe())
    tools = red_mod.build_red_tools(session)
    assert set(tools) == set(RED_TOOL_NAMES)
    for name, (schema, fn) in tools.items():
        assert callable(fn)
        assert schema["name"] == name
        assert isinstance(schema["description"], str) and schema["description"]
        params = schema["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        for req in params["required"]:
            assert req in params["properties"]
    _, record_fn = tools["record_hypothesis"]
    schema = tools["record_hypothesis"][0]
    assert set(schema["parameters"]["required"]) == {"statement", "submission_ref"}
    assert record_fn({"statement": "x", "submission_ref": "sub-999"})["filed"] is False


def test_coerce_task_ctx_shapes() -> None:
    assert coerce_task_ctx("t1").task == "t1"
    assert coerce_task_ctx("t1").package == "t1"
    ctx = coerce_task_ctx({"task": "t2", "package": "pkg", "files": {"a.py": "x"}})
    assert (ctx.task, ctx.package, ctx.files) == ("t2", "pkg", {"a.py": "x"})
    assert coerce_task_ctx(SimpleNamespace(task="t3")).task == "t3"


# ---------------------------------------------------------------------------
# Grade gating + budgets.
# ---------------------------------------------------------------------------


def test_hypothesis_without_grade_never_filed() -> None:
    ctx = TaskContext(task="t")
    session = red_mod._RedSession(ctx, grade_fn=FakeGrade(), probe_fn=FakeProbe())
    tools = red_mod.build_red_tools(session)
    out = tools["record_hypothesis"][1]({"statement": "no grade yet", "submission_ref": "sub-001"})
    assert out["filed"] is False
    assert session.filed == []
    assert len(session.unfiled) == 1


def test_grade_then_record_files_candidate() -> None:
    grade_fn = FakeGrade(reward=1.0)
    probe_fn = FakeProbe()
    ctx = TaskContext(task="t")
    session = red_mod._RedSession(ctx, grade_fn=grade_fn, probe_fn=probe_fn)
    tools = red_mod.build_red_tools(session)
    tools["probe_blue"][1]({"hint": "try empty output"})
    graded = tools["grade"][1]({"submission": {"kind": "json", "path": "s", "content": "{}"}})
    assert graded["ok"] and graded["reward"] == 1.0
    out = tools["record_hypothesis"][1](
        {
            "statement": "Wrong but accepted (v.py:3).",
            "requirement_ids": ["REQ-9"],
            "submission_ref": graded["submission_ref"],
            "defect_family": "checker-logic",
            "defect_class": "answer-matching-logic-error",
        }
    )
    assert out == {
        "ok": True,
        "filed": True,
        "hypothesis_id": out["hypothesis_id"],
        "status": "candidate",
    }
    (hyp,) = session.filed
    assert hyp.status == "candidate" if not isinstance(hyp, dict) else hyp["status"]
    submissions = grade_fn.calls
    assert len(submissions) == 1 and submissions[0].kind == "json"
    kinds = {(e["blue_involvement"], e["condition"]) for e in hyp.evidence}
    assert ("blue", "target_hinted") in kinds
    assert ("auditor_authored", "auditor_authored") in kinds
    assert probe_fn.hints == ["try empty output"]


def test_grade_accepts_json_string_submission() -> None:
    session = red_mod._RedSession(TaskContext(task="t"), grade_fn=FakeGrade(), probe_fn=FakeProbe())
    tools = red_mod.build_red_tools(session)
    out = tools["grade"][1](
        {"submission": json.dumps({"kind": "file", "path": "a", "content": "z"})}
    )
    assert out["ok"] and out["submission_ref"] == "sub-001"


def test_two_arg_grade_fn_protocol_supported() -> None:
    seen: list[tuple[Any, Any]] = []

    def two_arg(package: Any, submission: Any) -> Any:
        seen.append((package, submission))
        return SimpleNamespace(
            reward=0.0, status="ok", verifier_outputs={}, job_dir=None, script="s"
        )

    session = red_mod._RedSession(
        TaskContext(task="t", package="pkg"), grade_fn=two_arg, probe_fn=FakeProbe()
    )
    out = red_mod.build_red_tools(session)["grade"][1]({"submission": {"content": "x"}})
    assert out["ok"] and seen and seen[0][0] == "pkg"


def test_grade_and_probe_budgets_enforced() -> None:
    grade_fn = FakeGrade()
    probe_fn = FakeProbe()
    session = red_mod._RedSession(
        TaskContext(task="t"), grade_fn=grade_fn, probe_fn=probe_fn, max_grades=2, max_probes=1
    )
    tools = red_mod.build_red_tools(session)
    for _ in range(5):
        tools["grade"][1]({"submission": {"content": "x"}})
    assert len(grade_fn.calls) == 2
    assert session.grades_used == 2
    assert tools["grade"][1]({"submission": {"content": "x"}})["ok"] is False
    for _ in range(3):
        tools["probe_blue"][1]({"hint": "h"})
    assert len(probe_fn.hints) == 1
    assert tools["probe_blue"][1]({"hint": "h"})["ok"] is False


def test_max_steps_enforced_on_manual_driver() -> None:
    calls: list[Any] = []

    def chat_fn(
        model: str, messages: list[Any], tools: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        calls.append((model, len(messages)))
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": f"c{len(calls)}",
                    "name": "grade",
                    "arguments": {"submission": {"content": "x"}},
                }
            ],
        }

    grade_fn = FakeGrade()
    hyps = run_red(
        {"task": "t"},
        client=SimpleNamespace(chat_completion=chat_fn),
        budget=None,
        broadcast_hints=[],
        grade_fn=grade_fn,
        probe_fn=FakeProbe(),
        max_steps=3,
        max_grades=25,
    )
    assert hyps == []
    assert len(calls) == 3
    assert len(grade_fn.calls) == 3


# ---------------------------------------------------------------------------
# File tools.
# ---------------------------------------------------------------------------


def test_file_tools_constrained_to_task(tmp_path: Any) -> None:
    root = tmp_path / "task"
    (root / "grade").mkdir(parents=True)
    (root / "grade" / "check.py").write_text("line1\n", encoding="utf-8")
    session = red_mod._RedSession(
        TaskContext(task="t", task_dir=root, files={"mem.txt": "hello"}),
        grade_fn=FakeGrade(),
        probe_fn=FakeProbe(),
    )
    tools = red_mod.build_red_tools(session)
    assert tools["read_file"][1]({"path": "mem.txt"})["content"] == "hello"
    assert tools["read_file"][1]({"path": "grade/check.py"})["content"] == "line1\n"
    assert tools["read_file"][1]({"path": "../escape.py"})["ok"] is False
    assert tools["read_file"][1]({"path": "missing.py"})["ok"] is False
    listed = tools["list_files"][1]({})
    assert listed["ok"] and listed["files"] == ["grade/check.py"]
    assert tools["list_files"][1]({"root": "../"})["ok"] is False


# ---------------------------------------------------------------------------
# End to end.
# ---------------------------------------------------------------------------


def test_full_red_run_files_hypothesis_with_labels() -> None:
    grade_fn = FakeGrade(reward=1.0)
    probe_fn = FakeProbe(reward=0.0)
    client = ScriptedLoopClient(_standard_script())
    records: list[dict[str, Any]] = []
    hyps = run_red(
        {"task": "demo", "package": "demo-pkg"},
        client=client,
        budget=None,
        broadcast_hints=["in this universe, verifiers of family F fail to check X"],
        grade_fn=grade_fn,
        probe_fn=probe_fn,
        records=records,
    )
    assert client.seen and client.seen[0]["tools"] == sorted(RED_TOOL_NAMES)
    assert len(hyps) == 1
    (hyp,) = hyps
    assert (hyp.status if not isinstance(hyp, dict) else hyp["status"]) == "candidate"
    ev = hyp.evidence if not isinstance(hyp, dict) else hyp["evidence"]
    grade_ev = [e for e in ev if e["kind"] == "grade"][0]
    assert grade_ev["blue_involvement"] == "auditor_authored"
    assert grade_ev["condition"] == "auditor_authored"
    assert grade_ev["family"] == "insufficient-checking"
    assert grade_ev["class"] == "missing-behavioral-assertion"
    assert grade_ev["assertion"] == "demonstrated_exploitation"
    probe_ev = [e for e in ev if e["kind"] == "probe"][0]
    assert probe_ev["hint"] == "checker only looks at exit code"
    assert probe_fn.hints == ["checker only looks at exit code"]
    system_messages = client.seen  # loop invocation observed; prompt asserted below
    assert system_messages[0]["max_steps"] == 30


def test_system_prompt_sent_to_loop() -> None:
    captured: dict[str, Any] = {}

    def script(tools: dict[str, Any]) -> tuple[list[Any], list[Any]]:
        tools["finish"][1]({"summary": "nothing demonstrated"})
        return [], []

    client = ScriptedLoopClient(
        script, on_messages=lambda messages: captured.update(system=messages[0]["content"])
    )
    hyps = run_red(
        {"task": "demo"},
        client=client,
        budget=None,
        broadcast_hints=["theory: exit-code-only checking"],
        grade_fn=FakeGrade(),
        probe_fn=FakeProbe(),
    )
    assert hyps == []
    assert "theory: exit-code-only checking" in captured["system"]
