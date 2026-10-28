"""Inert retained cells: no real task, model, grader, network, or live state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evallab import cli
from evallab.production import seeker_known_hacks as hacks
from evallab.production.seeker_error_first import RunnerError, render_feedback_block


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def retained(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "cells"
    cell = root / "toy-data" / "toy-task" / "toy_model" / "turn-1"
    cell.mkdir(parents=True)
    ledger = tmp_path / "assignments.csv"
    ledger.write_text("key_id,item_id,task,split\ntoy-data,toy-item,toy-task,dev\n")
    prompt = "Inert task prompt.\n" + "unchanged context λ\r\n" * 4000
    (cell / "prompt.md").write_bytes(prompt.encode())
    response = json.dumps({"type": "thinking", "text": "retained thought λ\r\n" * 12000}, ensure_ascii=False) + "\n"
    (cell / "response.jsonl").write_bytes(response.encode())
    solve = b"#!/bin/sh\nprintf 'inert'\n"
    (cell / "submission").mkdir()
    (cell / "submission/solve.sh").write_bytes(solve)
    for side, reward in (("original", 1.0), ("repaired", 0.0)):
        output = cell / side / "verifier/test-stdout.txt"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"native inert check\n" + side.encode() + b" retained bytes\n")
        put_json(cell / side / "candidate.json", {
            "schema": "evallab.candidate.result/v1", "status": "measured",
            "executed": True, "error": None, "reward": reward,
            "task": {"id": "toy-task"},
            "logs": {"verifier": [{"path": str(output), "digest": "sha256:" + sha(output.read_bytes())}]},
        })
    put_json(cell / "result.json", {
        "dataset_id": "toy-data", "task_id": "toy-task", "turn": 1,
        "requested_model_route": "toy/model", "served_provider": "toy", "served_model": "model",
        "attestation_ok": True, "hide_tests": True,
        "llm_stdout_sha256": sha(response.encode()),
        "llm_solve_sha256": sha(solve), "executed_solve_sha256": sha(solve),
        "original_status": "measured", "repaired_status": "measured",
        "original_reward": 1.0, "repaired_reward": 0.0,
    })
    return root, cell, ledger


def change(path: Path, **changes: Any) -> None:
    put_json(path, {**json.loads(path.read_bytes()), **changes})


def export(retained: tuple[Path, Path, Path], tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    root, _, ledger = retained
    return hacks.export_known_hacks(root, state_dir=tmp_path / "state", assignments_csv=ledger, **kwargs)


@pytest.mark.parametrize(("changes", "reason"), [
    ({"attestation_ok": False}, "unattested"),
    ({"attestation_ok": 1}, "unattested"),
    ({"served_model": "fallback"}, "served_route_mismatch"),
    ({"original_status": "error"}, "non_measured"),
    ({"repaired_status": "not_run"}, "non_measured"),
    ({"repaired_reward": 0.25}, "not_differential"),
    ({"repaired_reward": None}, "not_differential"),
    ({"original_reward": 0.0}, "not_differential"),
    ({"hide_tests": False}, "hide_tests_mismatch"),
    ({"hide_tests": 1}, "hide_tests_mismatch"),
    ({"llm_stdout_sha256": None}, "missing_response_digest"),
    ({"llm_stdout_sha256": "0" * 64}, "response_digest_mismatch"),
    ({"llm_solve_sha256": "0" * 64}, "witness_verification"),
    ({"executed_solve_sha256": "0" * 64}, "witness_verification"),
    ({"candidate_digest": "sha256:" + "0" * 64}, "witness_verification"),
    ({"task_id": "other-task"}, "identity_mismatch"),
])
def test_production_gates_never_export(retained, tmp_path, changes, reason):
    change(retained[1] / "result.json", **changes)
    receipt = export(retained, tmp_path, mode="stage")
    assert (receipt["cells_seen"], receipt["confirmed"], receipt["exported"]) == (1, 0, 0)
    assert receipt["rejected_by_gate"] == {reason: 1}
    assert rows(tmp_path / "state" / hacks.RUNS_FILE) == []
    assert rows(tmp_path / "state" / hacks.LABELS_FILE) == []


@pytest.mark.parametrize("split", ["test", "heldout", "unknown"])
def test_secondary_non_dev_never_reads_cell(retained, tmp_path, monkeypatch, split):
    ledger = retained[2]
    ledger.write_text(ledger.read_text() + f"secondary,other,toy-task,{split}\n")

    def forbidden_read(path):
        raise AssertionError(f"non-DEV cell body read: {path}")

    monkeypatch.setattr(hacks, "_object", forbidden_read)
    receipt = export(retained, tmp_path, mode="stage")
    assert receipt["rejected_by_gate"] == {"non_dev": 1}
    assert receipt["confirmed"] == receipt["exported"] == 0


def test_unlisted_is_not_dev(retained, tmp_path):
    retained[2].write_text("key_id,item_id,task,split\n")
    receipt = export(retained, tmp_path)
    assert receipt["rejected_by_gate"] == {"unlisted_task": 1}


@pytest.mark.parametrize("mutation", ["unexecuted", "error", "inline_only", "wrong_native_reward", "same_receipt"])
def test_native_measured_receipts_not_inline_claims(retained, tmp_path, mutation):
    cell = retained[1]
    original = cell / "original/candidate.json"
    if mutation == "unexecuted":
        change(original, executed=False)
    elif mutation == "error":
        change(original, error="execution failed")
    elif mutation == "inline_only":
        put_json(original, {"reward": 1.0, "status": "measured", "executed": True})
    elif mutation == "wrong_native_reward":
        change(cell / "repaired/candidate.json", reward=1.0)
    else:
        for path in (cell / "repaired").rglob("*"):
            if path.is_file():
                path.unlink()
        (cell / "repaired/verifier").rmdir()
        (cell / "repaired").rmdir()
        (cell / "repaired").symlink_to(cell / "original", target_is_directory=True)
    receipt = export(retained, tmp_path, mode="stage")
    assert receipt["confirmed"] == receipt["exported"] == 0
    assert receipt["rejected_by_gate"] == {"witness_verification": 1}


def test_exact_untruncated_steps_and_separate_labels(retained, tmp_path):
    _, cell, ledger = retained
    receipt = export(retained, tmp_path, mode="stage")
    assert (receipt["cells_seen"], receipt["confirmed"], receipt["exported"]) == (1, 1, 1)
    run = rows(tmp_path / "state" / hacks.RUNS_FILE)[0]
    label = rows(tmp_path / "state" / hacks.LABELS_FILE)[0]
    doc = run["doc"]
    assert [step["step_id"] for step in doc["steps"]] == [1, 2, 3, 4]
    assert doc["steps"][0]["message"].encode() == (cell / "prompt.md").read_bytes()
    assert doc["steps"][1]["message"].encode() == (cell / "response.jsonl").read_bytes()
    assert len(doc["steps"][1]["message"]) > 100000
    action = doc["steps"][2]["tool_calls"][0]["arguments"]
    assert action["command"].encode() == (cell / "submission/solve.sh").read_bytes()
    assert action["sha256"] == sha((cell / "submission/solve.sh").read_bytes())
    observation = json.loads(doc["steps"][3]["message"])
    assert observation["reward"] == 1.0
    assert (cell / "original/verifier/test-stdout.txt").read_text().strip() in observation["verifier_tail"]
    for step in doc["steps"]:
        for source in step["evidence_files"]:
            assert source["sha256"] == sha((cell / source["path"]).read_bytes())
    assert label["run_id"] == run["run_id"] == doc["run_id"]
    assert label["source_label"] == {"judged": True, "verdict": "yes", "verdict_enacted": "yes"}
    assert label["passage_step_labels"] == {"positive_step_indices": [3]}
    assert label["admission"] == {"effective_split": "dev", "assignments_csv_sha256": sha(ledger.read_bytes())}
    assert label["evidence"]["original"]["reward"] == 1.0
    assert label["evidence"]["repaired"]["reward"] == 0.0
    serialized_doc = json.dumps(doc)
    assert not any(word in serialized_doc for word in ("toy-data", "known_hack", '"split"', '"source_label"', "verdict_enacted"))
    serialized_label = json.dumps(label)
    assert not any(word in serialized_label for word in ("unchanged context", "retained thought", "prompt.md", "response.jsonl", '"steps"', '"message"'))
    assert "verified_receipt_json" not in run["trace"]
    assert "is_exploit" not in run["trace"]


def test_scrubs_prompt_without_altering_its_provenance(retained, tmp_path):
    cell = retained[1]
    (cell / "prompt.md").write_text("Inert instruction, not tests/secret.py.\n")
    export(retained, tmp_path, mode="stage")
    step = rows(tmp_path / "state" / hacks.RUNS_FILE)[0]["doc"]["steps"][0]
    assert "tests/secret.py" not in step["message"]
    assert step["evidence_files"][0]["sha256"] == sha((cell / "prompt.md").read_bytes())
    assert step["content_sha256"] == sha(step["message"].encode())


def test_idempotent_and_recovers_partial_local_publication(retained, tmp_path):
    first = export(retained, tmp_path, mode="stage")
    paths = [tmp_path / "state" / name for name in (hacks.RUNS_FILE, hacks.LABELS_FILE)]
    before = [path.read_bytes() for path in paths]
    second = export(retained, tmp_path, mode="stage")
    assert first["exported"] == 1
    assert second["confirmed"] == second["already_exported"] == 1
    assert second["exported"] == second["would_export"] == 0
    assert [path.read_bytes() for path in paths] == before
    paths[1].unlink()
    repaired = export(retained, tmp_path, mode="stage")
    assert repaired["exported"] == 1
    assert [path.read_bytes() for path in paths] == before


def test_dry_run_default_writes_nothing_and_never_opens_database(retained, tmp_path, monkeypatch):
    from evallab.production import cloud_trace_db

    def forbidden_database(*args, **kwargs):
        raise AssertionError("dry-run opened a database")

    monkeypatch.setattr(cloud_trace_db, "CloudTraceDatabase", forbidden_database)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    receipt = export(retained, tmp_path, database=tmp_path / "must-not-exist.sqlite")
    assert receipt["mode"] == "dry-run"
    assert receipt["confirmed"] == receipt["would_export"] == 1
    assert receipt["exported"] == 0
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_apply_uses_public_ingestion_and_never_uploads_labels(retained, tmp_path, monkeypatch):
    from evallab.production import cloud_trace_db

    class FakeStore:
        tasks: dict[str, Any] = {}
        traces: dict[str, Any] = {}

        def __init__(self, database):
            assert database == "fake-target"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_task(self, task_id):
            return self.tasks.get(task_id)

        def upsert_task(self, task):
            self.tasks[task["task_id"]] = task

        def upsert_trace(self, trace, *, steps):
            self.traces[trace["trace_id"]] = {"trace": trace, "steps": steps}

    monkeypatch.setattr(cloud_trace_db, "CloudTraceDatabase", FakeStore)
    with pytest.raises(ValueError, match="explicit"):
        export(retained, tmp_path, mode="apply")
    assert not (tmp_path / "state").exists()
    export(retained, tmp_path, mode="stage")
    applied = export(retained, tmp_path, mode="apply", database="fake-target")
    assert applied["already_exported"] == 1
    assert len(FakeStore.traces) == 1
    native = next(iter(FakeStore.traces.values()))
    assert len(native["steps"]) == 4
    assert native["steps"][1]["content_text"].encode() == (retained[1] / "response.jsonl").read_bytes()
    assert json.loads(native["steps"][2]["command_or_action"])[0]["arguments"]["command"].encode() == (retained[1] / "submission/solve.sh").read_bytes()
    cloud_payload = json.dumps(FakeStore.traces)
    assert not any(field in cloud_payload for field in ("source_label", "verdict_enacted", "passage_step_labels", "effective_split", "verified_receipt_json", "known_hack"))
    export(retained, tmp_path, mode="apply", database="fake-target")
    assert len(FakeStore.traces) == 1


def test_turn_two_keeps_preceding_error_and_feedback_exact(retained, tmp_path):
    root, cell, ledger = retained
    cell = cell.rename(cell.with_name("turn-2"))
    # Move the inert receipt references alongside their files.
    for side in ("original", "repaired"):
        receipt = cell / side / "candidate.json"
        data = json.loads(receipt.read_bytes())
        data["logs"]["verifier"][0]["path"] = str(cell / side / "verifier/test-stdout.txt")
        put_json(receipt, data)
    error = RunnerError("measured", 0.0, None, (1,), (), (), "inert preceding failure\n", ("stdout.txt",))
    put_json(cell / "runner_error.json", error.to_json())
    block = render_feedback_block(error).encode()
    (cell / "feedback.md").write_bytes(block)
    change(cell / "result.json", turn=2, runner_error_sha256=error.sha256, feedback_block_sha256=sha(block))
    receipt = export((root, cell, ledger), tmp_path, mode="stage")
    assert receipt["confirmed"] == 1
    doc = rows(tmp_path / "state" / hacks.RUNS_FILE)[0]["doc"]
    assert [step["step_id"] for step in doc["steps"]] == [0, 1, 2, 3, 4]
    assert doc["steps"][0]["message"].encode() == block
    for source in doc["steps"][0]["evidence_files"]:
        assert source["sha256"] == sha((cell / source["path"]).read_bytes())
    change(cell / "result.json", runner_error_sha256="0" * 64)
    counts = hacks.ExportCounts()
    assert list(hacks.confirmed_exploit_cells(root, assignments_csv=ledger, counts=counts)) == []
    assert counts.rejected_by_gate == {"retained_evidence": 1}


def test_cli_default_and_explicit_dry_run_write_nothing(retained, tmp_path, capsys):
    root, _, ledger = retained
    argv = [
        "prod", "seeker-known-hacks", "--cells-root", str(root), "--state-dir", str(tmp_path / "state"),
        "--assignments-csv", str(ledger),
    ]
    assert cli.run_cli(argv, workspace=tmp_path) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["confirmed"] == 1 and receipt["exported"] == 0
    assert not (tmp_path / "state").exists()
    assert cli.run_cli([*argv, "--dry-run"], workspace=tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"
    assert not (tmp_path / "state").exists()
    assert cli.run_cli([*argv, "--stage"], workspace=tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["exported"] == 1


def test_tightened_ledger_invalidates_same_path_cache(retained, tmp_path, monkeypatch):
    assert export(retained, tmp_path)["confirmed"] == 1
    ledger = retained[2]
    ledger.write_text(ledger.read_text() + "new-key,new-item,toy-task,test\n")

    def forbidden_read(path):
        raise AssertionError(f"newly non-DEV cell body read: {path}")

    monkeypatch.setattr(hacks, "_object", forbidden_read)
    receipt = export(retained, tmp_path)
    assert receipt["confirmed"] == 0
    assert receipt["rejected_by_gate"] == {"non_dev": 1}


def test_harbor_receipts_with_native_reward_files(retained, tmp_path):
    cell = retained[1]
    for side, reward in (("original", 1.0), ("repaired", 0.0)):
        (cell / side / "candidate.json").unlink()
        put_json(cell / side / "result.json", {"verifier_result": {"rewards": {"reward": reward}}})
        put_json(cell / side / "verifier/reward.json", {"reward": reward})
    receipt = export(retained, tmp_path, mode="stage")
    assert receipt["confirmed"] == receipt["exported"] == 1
    observation = rows(tmp_path / "state" / hacks.RUNS_FILE)[0]["doc"]["steps"][3]
    assert {source["path"] for source in observation["evidence_files"]} == {
        "original/result.json", "original/verifier/reward.json", "original/verifier/test-stdout.txt",
    }


def test_repaired_observations_cannot_replace_original_output(retained, tmp_path):
    cell = retained[1]
    original = cell / "original/candidate.json"
    data = json.loads(original.read_bytes())
    repaired = cell / "repaired/verifier/test-stdout.txt"
    data["logs"]["verifier"] = [{"path": str(repaired), "digest": sha(repaired.read_bytes())}]
    put_json(original, data)
    receipt = export(retained, tmp_path, mode="stage")
    assert receipt["confirmed"] == receipt["exported"] == 0
    assert receipt["rejected_by_gate"] == {"retained_evidence": 1}


def test_cloud_trace_adapter_is_blindable_and_preserves_response(retained):
    from evallab.production.run_atlas import AtlasRun, blind_run, cloud_doc

    root, directory, ledger = retained
    cell = next(hacks.confirmed_exploit_cells(root, assignments_csv=ledger))
    doc = hacks.cell_to_atif(cell)
    native = hacks._db_steps(doc)

    class FakeConnection:
        def execute(self, query, params):
            assert params == (cell.run_id,)
            return [{**row, "observation_preview": None, "flags_json": json.dumps(row["flags_json"])}
                    for row in native]

    projected = cloud_doc(FakeConnection(), cell.run_id)
    assert projected["steps"][1]["message"].encode() == (directory / "response.jsonl").read_bytes()
    assert projected["steps"][2]["tool_calls"] == doc["steps"][2]["tool_calls"]
    blinded = blind_run(AtlasRun(cell.run_id, "toy-data", "DEV", "pass", projected))
    assert blinded.origins == [1, 2, 3, 4]
    assert blinded.steps[1]["message"].encode() == (directory / "response.jsonl").read_bytes()
    serialized = json.dumps(blinded.steps)
    assert not any(field in serialized for field in ("toy-data", "evidence_files", "source_label", "effective_split"))


@pytest.mark.parametrize("exception_info", [{"type": "HarnessError"}, None])
def test_native_harbor_error_or_reward_only_is_not_measured(retained, tmp_path, exception_info):
    cell = retained[1]
    (cell / "original/candidate.json").unlink()
    put_json(cell / "original/verifier/reward.json", {"reward": 1.0})
    if exception_info is not None:
        put_json(cell / "original/result.json", {
            "verifier_result": {"rewards": {"reward": 1.0}}, "exception_info": exception_info,
        })
    receipt = export(retained, tmp_path, mode="stage")
    assert receipt["confirmed"] == receipt["exported"] == 0
    assert receipt["rejected_by_gate"] == {"witness_verification": 1}
