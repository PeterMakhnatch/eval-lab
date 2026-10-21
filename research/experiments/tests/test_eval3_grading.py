"""Consumer-visible custody, missingness and judgment boundaries for Eval3."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[1] / "eval3-harness-v1" / "grade.py"
spec = importlib.util.spec_from_file_location("eval3_experiment_grade", MODULE)
assert spec is not None and spec.loader is not None
grade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grade)


def inputs(tmp_path: Path) -> dict:
    view = tmp_path / "view"
    source = view / "tasks/example/instruction.md"
    source.parent.mkdir(parents=True)
    source.write_text("Return the sum.\nDo not return the product.\n")
    rows = [
        {
            "package": {"name": "example"},
            "split": "dev",
            "subject": {"task_digest": "sha256:" + "a" * 64},
        }
    ]
    identities = view / "inputs.jsonl"
    identities.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    bundle = {
        "schema": "tb4.audit.agent-view/v1",
        "split": "dev",
        "files": {
            "tasks/example/instruction.md": grade.digest(source.read_bytes()),
            "inputs.jsonl": grade.digest(identities.read_bytes()),
        },
    }
    (view / "bundle.json").write_text(json.dumps(bundle))
    report = tmp_path / "report.txt"
    report.write_text("The checker accepts products instead of the required sum.\n")
    return {
        "study_id": "study",
        "source_view": str(view),
        "cases": ["example"],
        "auditors": ["a", "b"],
        "trials": [
            {
                "run_id": "run-a",
                "trial_id": "trial-a",
                "auditor": "a",
                "case": "example",
                "state": "completed",
                "task_digest": "a" * 64,
                "authored_files": [str(report)],
                "trace_files": [],
                "source_only": True,
            }
        ],
    }


def test_missing_auditor_is_pending_not_an_observed_empty_set(tmp_path: Path) -> None:
    config = inputs(tmp_path)
    Path(config["trials"][0]["authored_files"][0]).write_text('{"findings": []}\n')
    study = grade.prepare(config, tmp_path / "prepared")
    rows = {row["auditor"]: row for row in study["trials"]}
    assert rows["a"]["state"] == "completed"
    assert rows["b"]["state"] == "pending"
    assert rows["b"]["workspace"] is None
    assert rows["b"]["files"] == {}


@pytest.mark.parametrize(
    "change",
    [
        "test_split",
        "changed_source",
        "changed_identity",
        "wrong_task_digest",
        "missing_task_digest",
    ],
)
def test_unqualified_inputs_are_refused_before_grading(tmp_path: Path, change: str) -> None:
    config = inputs(tmp_path)
    view = Path(config["source_view"])
    if change == "test_split":
        bundle = json.loads((view / "bundle.json").read_text())
        bundle["split"] = "test"
        (view / "bundle.json").write_text(json.dumps(bundle))
    elif change == "changed_source":
        (view / "tasks/example/instruction.md").write_text("Return the product.\n")
    elif change == "changed_identity":
        (view / "inputs.jsonl").write_text("{}\n")
    elif change == "wrong_task_digest":
        config["trials"][0]["task_digest"] = "b" * 64
    else:
        del config["trials"][0]["task_digest"]
    with pytest.raises(ValueError):
        grade.prepare(config, tmp_path / "prepared")


def test_authored_extraction_cannot_browse_source_and_support_requires_an_actual_read(
    tmp_path: Path,
) -> None:
    out = tmp_path / "prepared"
    study = grade.prepare(inputs(tmp_path), out)
    evidence = grade.Evidence(out, study["tasks"][0], study["trials"][0])
    args = {"path": "source/instruction.md", "start_line": 1, "end_line": 2}
    citation = {"scope": "source", "path": args["path"], "start_line": 1, "end_line": 2}
    with pytest.raises(ValueError, match="phase"):
        evidence.run("read", args, authored_only=True)
    with pytest.raises(ValueError, match="independently read"):
        evidence.citation(citation, authored=False)
    evidence.run("read", {**args, "end_line": 1}, authored_only=False)
    with pytest.raises(ValueError, match="independently read"):
        evidence.citation(citation, authored=False)
    evidence.run("read", {**args, "start_line": 2}, authored_only=False)
    evidence.citation(citation, authored=False)
    assert evidence.observed_lines[args["path"]] == {1, 2}


def test_modified_prepared_evidence_cannot_be_read_under_its_old_identity(tmp_path: Path) -> None:
    out = tmp_path / "prepared"
    study = grade.prepare(inputs(tmp_path), out)
    evidence = grade.Evidence(out, study["tasks"][0], study["trials"][0])
    evidence.path("source/instruction.md").write_text("different task\n")
    with pytest.raises(ValueError, match="evidence changed"):
        evidence.run("read", {"path": "source/instruction.md"}, authored_only=False)


@pytest.mark.parametrize("groups", [[["a"], ["a", "b"]], [["a"]], [["a", "b"], []], [["a", "c"]]])
def test_duplicate_missing_or_invented_issue_links_are_rejected(groups: list[list[str]]) -> None:
    with pytest.raises(ValueError, match="partition"):
        grade.partition(groups, {"a", "b"})


def test_importance_uses_all_five_ordinal_votes_not_an_average_or_partial_vote() -> None:
    votes = [{"importance": value} for value in ["high", "low", "medium", "high", "medium"]]
    assert grade.importance(votes) == "medium"
    with pytest.raises(ValueError, match="five"):
        grade.importance(votes[:4])


def test_schema_rejects_rewritten_allegation_fields_and_empty_required_text() -> None:
    schema = {
        "type": "object",
        "properties": {"reason": {"type": "string", "minLength": 1}},
        "required": ["reason"],
        "additionalProperties": False,
    }
    with pytest.raises(ValueError, match="unexpected"):
        grade.validate_schema(schema, {"reason": "valid", "new_allegation": "not frozen"})
    with pytest.raises(ValueError, match="empty"):
        grade.validate_schema(schema, {"reason": ""})
