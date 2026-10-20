"""Evidence identity and resource boundaries of the adaptive environment reviewer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.qa.env import EnvTools
from evallab.qa.models import load_findings_dir


def _tools(tmp_path: Path, *, max_seconds: float = 600) -> EnvTools:
    root = tmp_path.resolve()
    task = root / "package"
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text("Return exactly the integer 42.\n")
    (task / "tests" / "check.py").write_text("answer = 7\nassert isinstance(answer, int)\n")
    return EnvTools(task, root / "run", repo_root=root, max_seconds=max_seconds)


def _finding(tools: EnvTools, evidence: list[str] | None = None) -> dict[str, Any]:
    return {
        "schema": "qa.finding/v1",
        "subject": {"kind": "task", "task_id": tools.task_id, "task_digest": tools.task_digest},
        "stage": "source-review", "verdict": "fail", "category": "weak",
        "severity": "high", "fault": "eval",
        "summary": "The integer-only check accepts 7 although the instruction requires 42; runtime behaviour unverified.",
        "evidence_refs": evidence if evidence is not None else ["instruction.md:1", "tests/check.py:1-2"],
        "refutation": {"status": "unverified"},
        "reviewer": {"agent": "qa-env-test"},
        "measurement": "not_run", "root_cause": ["weak_check"],
    }


def _record(tools: EnvTools, finding: dict[str, Any]) -> dict[str, Any]:
    return tools.call("record-finding", {"defect_class": "weak_check", "finding": finding})["result"]


def test_recorded_source_finding_remains_suspected_in_shared_consumer(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    result = _record(tools, _finding(tools))
    retained, = load_findings_dir(tools.run_dir / "findings")
    assert retained.subject.task_digest == tools.task_digest
    assert retained.root_cause == ["weak_check"]
    assert retained.refutation.status == "unverified"
    assert retained.measurement == "not_run"
    assert result["status"] == "suspected"
    assert "executed_witness" not in result["evidence_basis"]


@pytest.mark.parametrize("evidence", [
    [], ["tests/check.py:0"], ["tests/check.py:3"], ["tests/check.py:2-1"],
    ["missing.py:1"], ["../private:1"], ["witness:" + "a" * 32],
])
def test_unresolvable_evidence_cannot_be_recorded(tmp_path: Path, evidence: list[str]) -> None:
    tools = _tools(tmp_path)
    with pytest.raises((ValueError, OSError)):
        _record(tools, _finding(tools, evidence))
    assert not (tools.run_dir / "findings").exists()


@pytest.mark.parametrize("change", ["task_id", "task_digest", "root_cause", "verified"])
def test_claim_cannot_change_subject_class_or_certify_itself(tmp_path: Path, change: str) -> None:
    tools = _tools(tmp_path)
    finding = _finding(tools)
    if change in {"task_id", "task_digest"}:
        finding["subject"][change] = "different-package"
    elif change == "root_cause":
        finding["root_cause"] = ["weak_check", "overstrict_oracle"]
    else:
        finding["refutation"]["status"] = "verified"
    with pytest.raises(ValueError):
        _record(tools, finding)
    assert not (tools.run_dir / "findings").exists()


def test_changing_package_invalidates_previously_pinned_evidence(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    (tools.task_dir / "instruction.md").write_text("Return any integer.\n")
    with pytest.raises(ValueError, match="changed"):
        _record(tools, _finding(tools))


@pytest.mark.parametrize("status", ["refused", "error", "timeout"])
def test_raw_reward_from_unusable_execution_is_not_a_witness(tmp_path: Path, status: str) -> None:
    tools = _tools(tmp_path)
    result = tools._observe("run-reference", {
        "status": status, "executed": True, "reward": None, "observed_native_rewards": {"reward": 1},
    })
    assert result["witness"] is None
    receipt = json.loads(Path(result["observation_path"]).read_text())
    with pytest.raises(ValueError, match="unmeasured"):
        _record(tools, _finding(tools, ["witness:" + receipt["id"]]))


def test_measured_witness_is_bound_to_its_task_and_runtime_tool(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    result = tools._observe("run-reference", {
        "status": "measured", "executed": True, "reward": 1,
        "task": {"digest": tools.task_digest},
    })
    receipt_path = Path(result["observation_path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["task_digest"] = "sha256:" + "0" * 64
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="bound"):
        _record(tools, _finding(tools, [result["witness"]]))
    receipt["task_digest"] = tools.task_digest
    receipt["tool"] = "diff"
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="bound"):
        _record(tools, _finding(tools, [result["witness"]]))


def test_native_result_for_another_task_is_rejected(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    with pytest.raises(ValueError, match="different task"):
        tools._observe("run-reference", {
            "status": "measured", "executed": True, "reward": 1,
            "task": {"digest": "sha256:" + "0" * 64},
        })
    assert not (tools.run_dir / "observations").exists()


def test_expired_budget_stops_source_and_recording_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _tools(tmp_path, max_seconds=1)
    monkeypatch.setattr("evallab.qa.env.time.monotonic", lambda: tools.deadline + 1)
    with pytest.raises(TimeoutError, match="budget"):
        tools.call("task-info", {})
    with pytest.raises(TimeoutError, match="budget"):
        _record(tools, _finding(tools))
    assert not (tools.run_dir / "findings").exists()


def test_package_symlink_cannot_read_outside_the_pinned_source(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    outside = tmp_path / "private"
    outside.write_text("not part of the package")
    (tools.task_dir / "extra.py").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        EnvTools(tools.task_dir, tmp_path.resolve() / "other-run", repo_root=tmp_path.resolve())


def test_evidence_subdirectory_symlink_cannot_write_outside_run(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    outside = tmp_path.resolve() / "unrelated"
    outside.mkdir()
    (tools.run_dir / "findings").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        _record(tools, _finding(tools))
    assert list(outside.iterdir()) == []


def test_rejected_output_inside_source_does_not_create_a_directory(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    output = tools.task_dir / "review-output"
    with pytest.raises(ValueError, match="disjoint"):
        EnvTools(tools.task_dir, output, repo_root=tmp_path.resolve())
    assert not output.exists()


@pytest.mark.parametrize("excluded", ["README.md", "cheat/solve.sh"])
def test_task_author_answer_material_is_not_a_reviewer_capability(tmp_path: Path, excluded: str) -> None:
    tools = _tools(tmp_path)
    material = tools.task_dir / excluded
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_text("Task-author answer material, not reviewer input.\n")
    fresh = EnvTools(tools.task_dir, tmp_path.resolve() / "fresh", repo_root=tmp_path.resolve())
    with pytest.raises(ValueError):
        fresh.call("task-info", {"path": excluded})


def test_structured_candidate_compilation_does_not_run_its_program_on_host(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    marker = tmp_path.resolve() / "host-marker"
    tools._candidate({"submission": {"kind": "command", "content": f"touch '{marker}'"}})
    assert not marker.exists()


@pytest.mark.parametrize("measurement", [None, "measured", "ran_without_usable_result"])
def test_source_citations_cannot_claim_native_execution(
    tmp_path: Path, measurement: str | None,
) -> None:
    tools = _tools(tmp_path)
    finding = _finding(tools)
    if measurement is None:
        finding.pop("measurement")
    else:
        finding["measurement"] = measurement
    with pytest.raises(ValueError, match="measurement"):
        _record(tools, finding)
    assert not (tools.run_dir / "findings").exists()


def test_rejected_output_inside_exported_view_does_not_change_the_view(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    view = tmp_path.resolve() / "exported"
    view.mkdir()
    output = view / "review-output"
    with pytest.raises(ValueError, match="disjoint"):
        EnvTools(tools.task_dir, output, repo_root=tmp_path.resolve(), view_dir=view)
    assert not output.exists()
