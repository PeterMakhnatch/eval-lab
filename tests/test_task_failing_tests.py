"""Conservative same-failing-test evidence across physical trials."""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1] / "research/experiments/python-task-ledger/failing_tests.py"
)
_spec = importlib.util.spec_from_file_location("task_failing_tests", SCRIPT)
assert _spec is not None and _spec.loader is not None
_analysis = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_analysis)

TASK = "format-code-task-000001"


def _trial(
    root: Path,
    name: str,
    tests: list[dict] | None,
    *,
    model: str = "model-a",
    physical_id: str | None = None,
    instruction: str | None = "Implement the requested behavior.",
    nop: bool = False,
) -> str:
    package = root / "packages" / name / TASK
    package.mkdir(parents=True)
    if instruction is not None:
        (package / "instruction.md").write_text(instruction)
    relative = f"2026-10-02/{'HAR-146' if nop else 'HAR-126'}-{name}/{name}__trial"
    trial = root / relative
    (trial / "verifier").mkdir(parents=True)
    config = {
        "task": {"path": str(package)},
        "agent": {"name": "nop" if nop else "test-agent", "model_name": None if nop else model},
    }
    (trial / "config.json").write_text(json.dumps(config))
    (trial / "result.json").write_text(
        json.dumps({"id": physical_id or name, "task_name": TASK, "config": config})
    )
    if tests is not None:
        (trial / "verifier" / "ctrf.json").write_text(json.dumps({"results": {"tests": tests}}))
    if nop:
        (trial / "egress-lock.json").write_text(
            json.dumps({"applied": True, "network_block_all": True})
        )
    return relative


def _failure(name: str, literal: str = "required sentinel") -> dict:
    return {"name": name, "status": "failed", "trace": f">   assert actual == {literal!r}\n"}


def _analyze(root: Path, paths: list[str]) -> dict:
    history = root / "history.csv"
    with history.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["task_id", "runs", "trials"])
        writer.writeheader()
        writer.writerow({"task_id": TASK, "runs": len(paths), "trials": " ".join(paths)})
    manifest = root / "locked.csv"
    manifest.write_text(f"task_id,locked_nop\n{TASK},sound\n")
    return _analysis.analyze(history, manifest, root, root)["rows"][0]


def test_all_runs_intersection_retains_late_failures_and_nop_evidence(tmp_path: Path) -> None:
    first = [_failure(f"first-only-{index}") for index in range(9)] + [_failure("common")]
    a = _trial(tmp_path, "a", first)
    b = _trial(tmp_path, "b", [_failure("common"), _failure("second-only")], model="model-b")
    nop = _trial(tmp_path, "nop", [_failure("common")], nop=True)

    row = _analyze(tmp_path, [a, b])
    assert row["always_failing_tests"] == ["common"]
    assert (row["runs"], row["unique_runs"], row["models"]) == (2, 2, 2)
    assert row["candidate_tests"] == ["common"]
    evidence = next(item for item in row["test_evidence"] if item["test"] == "common")
    assert evidence["no_agent_fails"] == "yes"
    assert row["no_agent_trial_paths"] == [nop]


def test_missing_ctrf_does_not_turn_observed_intersection_into_every_run(tmp_path: Path) -> None:
    a = _trial(tmp_path, "a", [_failure("common")])
    b = _trial(tmp_path, "infra", None)
    row = _analyze(tmp_path, [a, b])
    assert row["observed_common_failing_tests"] == ["common"]
    assert row["always_failing_tests"] == []
    assert row["candidate_broken_test"] is False
    assert row["model_evidence_state"] == "partial"
    assert (row["runs"], row["ctrf_runs"]) == (2, 1)


def test_skipped_and_passing_tests_are_not_common_failures(tmp_path: Path) -> None:
    a = _trial(tmp_path, "a", [_failure("skip"), _failure("pass")])
    b = _trial(
        tmp_path,
        "b",
        [{"name": "skip", "status": "skipped"}, {"name": "pass", "status": "passed"}],
    )
    row = _analyze(tmp_path, [a, b])
    assert row["always_failing_tests"] == []
    assert row["candidate_tests"] == []


def test_duplicate_publications_do_not_inflate_independent_run_count(tmp_path: Path) -> None:
    a = _trial(tmp_path, "a", [_failure("common")], physical_id="one-physical-trial")
    copy = _trial(tmp_path, "copy", [_failure("common")], physical_id="one-physical-trial")
    row = _analyze(tmp_path, [a, copy])
    assert (row["runs"], row["unique_runs"], row["ctrf_runs"]) == (2, 1, 1)
    assert row["model_trial_paths"] == [a, copy]
    assert row["candidate_tests"] == ["common"]


def test_missing_or_stated_instruction_literal_cannot_be_candidate(tmp_path: Path) -> None:
    a = _trial(tmp_path, "unknown", [_failure("common")], instruction=None)
    b = _trial(tmp_path, "stated", [_failure("common")], instruction="Return required sentinel.")
    row = _analyze(tmp_path, [a, b])
    assert row["always_failing_tests"] == ["common"]
    assert row["candidate_tests"] == []
    evidence = next(item for item in row["test_evidence"] if item["test"] == "common")
    assert evidence["no_agent_fails"] == "unknown"
    assert {item["in_instruction"] for item in evidence["assertions"]} == {None, True}


def test_incomplete_duplicate_can_borrow_bound_instruction_but_conflicts_refuse(
    tmp_path: Path,
) -> None:
    a = _trial(tmp_path, "a", [_failure("common")], physical_id="same", instruction=None)
    b = _trial(tmp_path, "b", [_failure("common")], physical_id="same")
    assert _analyze(tmp_path, [a, b])["candidate_tests"] == ["common"]
    c = _trial(
        tmp_path,
        "c",
        [_failure("common")],
        physical_id="same",
        instruction="Return required sentinel.",
    )
    row = _analyze(tmp_path, [a, b, c])
    assert row["unique_runs"] == 1
    assert row["model_evidence_state"] == "missing"
    assert row["candidate_tests"] == []
    assert "conflicting_publications:same" in row["notes"]


def test_instruction_recovery_requires_matching_digest_and_unchanged_ancestry(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    package = snapshot / TASK
    package.mkdir(parents=True)
    (package / "instruction.md").write_text("Original instruction.")
    parent = _analysis.task_directory_digest(package)
    assert (
        _analysis.pinned_instruction(TASK, parent, snapshot, tmp_path, {})[0]
        == "Original instruction."
    )
    assert _analysis.pinned_instruction(TASK, None, snapshot, tmp_path, {}) == (None, None)
    variant = "sha256:" + "a" * 64
    record_dir = tmp_path / "library/task-variants" / f"mimo-v2.6-rl__{TASK}"
    record_dir.mkdir(parents=True)
    record_path = record_dir / f"{'a' * 12}.json"
    record = {
        "task_name": f"mimo-v2.6-rl/{TASK}",
        "variant_digest": variant,
        "parent": {"digest": parent},
        "components_changed": ["environment"],
        "files": [{"path": "environment/setup.sh"}],
    }
    record_path.write_text(json.dumps(record))
    assert (
        _analysis.pinned_instruction(TASK, variant, snapshot, tmp_path, {})[0]
        == "Original instruction."
    )
    record["files"].append({"path": "instruction.md"})
    record_path.write_text(json.dumps(record))
    assert _analysis.pinned_instruction(TASK, variant, snapshot, tmp_path, {}) == (None, None)


def test_no_agent_only_task_cannot_vacuously_satisfy_every_model_run(tmp_path: Path) -> None:
    _trial(tmp_path, "nop", [_failure("common")], nop=True)
    row = _analyze(tmp_path, [])
    assert row["no_agent_runs"] == 1
    assert row["model_evidence_state"] == "no_model_runs"
    assert row["always_failing_tests"] == []
    assert row["candidate_broken_test"] is False
