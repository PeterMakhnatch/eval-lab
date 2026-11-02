from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from evallab.cli import run_cli
from evallab.gold_certifier import TaskCertificationResult
from evallab.hack_drop import load_certified_tasks
from evallab.task_admission import AdmissionRecord, AdmissionStepRecord, default_steps
from evallab.task_status import build_task_status
from evallab.trace_anatomy import RefusedRun


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "assignments.csv").write_text(
        "item_id,task,split\nsealed,private-task,test\n", encoding="utf-8"
    )
    return tmp_path


def gold(task: str = "sample-task", **changes: Any) -> dict[str, Any]:
    row = asdict(
        TaskCertificationResult(
            task_id=task,
            kind="original",
            upgraded_package="packages/sample-task",
            test_lines=12,
            train_mutants_total=2,
            train_mutants_killed=2,
            eval_mutants_total=2,
            eval_mutants_killed=2,
            eval_mutants_base_accepted=2,
            eval_mutants_base_accepted_killed=2,
            gate1_reference_passed=True,
            gate2_nop_passed=True,
            gate3_witness_passed=True,
            gate4_exit0_pth_passed=True,
            reference_reward=1.0,
            nop_reward=0.0,
            witness_reward=0.0,
            exit0_pth_reward=0.0,
            train_rewards={"train-a": 0.0, "train-b": 0.0},
            eval_rewards={"eval-a": 0.0, "eval-b": 0.0},
        )
    )
    # Tests explicitly choose whether Gate 7 was measured. Keep synthetic
    # observations independent of newer dataclass empty-collection defaults.
    row.pop("alternative_solution_rewards", None)
    row.update(changes)
    return row


def save(root: Path, name: str, data: Any) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def build(root: Path, **kwargs: Any) -> dict[str, Any]:
    return build_task_status(
        repo_root=root, built_at="2026-11-02T12:00:00Z", code_rev="a" * 40, **kwargs
    )


def status(root: Path, row: dict[str, Any]) -> dict[str, Any]:
    return build(root, gold_results=[save(root, "gold.json", row)])["tasks"][0]


@pytest.mark.parametrize(
    ("total", "accepted", "expected", "code"),
    [
        (None, None, "provisional", "ALT_UNMEASURED"),
        (2, None, "provisional", "ALT_UNMEASURED"),
        (0, 0, "provisional", "ALT_TOO_FEW_0"),
        (1, 1, "provisional", "ALT_TOO_FEW_1"),
        (2, 2, "certified", "ALL_GATES_PASSED"),
        (3, 3, "certified", "ALL_GATES_PASSED"),
        (1, 0, "uncertified", "WRONG_REJECTION_1_OF_1"),
        (3, 1, "uncertified", "WRONG_REJECTION_2_OF_3"),
    ],
)
def test_alternative_gate(
    root: Path, total: int | None, accepted: int | None, expected: str, code: str
) -> None:
    row = status(
        root, gold(alternative_solutions_total=total, alternative_solutions_accepted=accepted)
    )
    assert row["status"] == expected
    assert row["reason"].startswith(code + ": ")
    assert row["reason"].endswith(".")


def test_old_receipt_without_optional_fields_stays_provisional(root: Path) -> None:
    row = gold()
    row.pop("alternative_solutions_total", None)
    row.pop("alternative_solutions_accepted", None)
    assert status(root, row)["status"] == "provisional"


def test_named_witness_receipts_and_empty_legacy_default(root: Path) -> None:
    assert status(root, gold(witness_rewards={"conftest": 0.0}))["status"] == "provisional"
    assert status(root, gold(witness_rewards={}))["status"] == "provisional"


@pytest.mark.parametrize(
    "changes",
    [
        {"nop_reward": 1.0},
        {"witness_reward": 1.0},
        {"exit0_pth_reward": 1.0},
        {"witness_rewards": [0.0, 1.0]},
        {"witness_rewards": {"conftest": 0.0, "operator": 1.0}},
        {"attack_rewards": {"cheat": 1.0}},
        {"eval_mutants_killed": 1, "eval_rewards": {"a": 0.0, "b": 1.0}},
    ],
)
def test_observed_weakness_overrides_alternative_rejection(
    root: Path, changes: dict[str, Any]
) -> None:
    assert (
        status(
            root, gold(alternative_solutions_total=2, alternative_solutions_accepted=1, **changes)
        )["status"]
        == "weak"
    )


def test_reference_failure_has_first_precedence(root: Path) -> None:
    row = status(
        root,
        gold(
            reference_reward=0.0,
            nop_reward=1.0,
            alternative_solutions_total=2,
            alternative_solutions_accepted=0,
        ),
    )
    assert row["status"] == "uncertified"
    assert row["reason"].startswith("REFERENCE_FAILED:")


@pytest.mark.parametrize(
    "changes",
    [
        {"reference_reward": None},
        {"nop_reward": None},
        {"witness_reward": None},
        {"exit0_pth_reward": None},
        {"witness_rewards": []},
        {"train_mutants_killed": 1, "train_rewards": {"a": 0.0, "b": 1.0}},
        {"eval_mutants_killed": 1, "eval_rewards": {"a": 0.0, "b": None}},
        {"eval_mutants_total": 0, "eval_mutants_killed": 0, "eval_rewards": {}},
        {"train_rewards": {}},
        {"gate4_exit0_pth_passed": False},
    ],
)
def test_missing_infra_or_partial_gates_never_certify(root: Path, changes: dict[str, Any]) -> None:
    row = status(
        root, gold(alternative_solutions_total=2, alternative_solutions_accepted=2, **changes)
    )
    assert row["status"] == "uncertified"
    assert row["reason"].startswith("INCOMPLETE_GATES:")


@pytest.mark.parametrize(("total", "accepted"), [(None, 1), (-1, 0), (1, 2), (True, 1), (2.0, 2)])
def test_invalid_alternative_counts_refused(root: Path, total: Any, accepted: Any) -> None:
    with pytest.raises(ValueError, match="alternative solution counts"):
        status(
            root, gold(alternative_solutions_total=total, alternative_solutions_accepted=accepted)
        )


def test_partial_alternatives_keep_known_wrong_rejections(root: Path) -> None:
    row = status(
        root,
        gold(
            alternative_solutions_total=2,
            alternative_solutions_accepted=None,
            alternative_solution_rewards={"one": 0.0, "two": None},
        ),
    )
    assert row["status"] == "uncertified"
    assert row["reason"].startswith("WRONG_REJECTION_1_OF_2:")


def test_new_unmeasured_empty_alternatives_remain_provisional(root: Path) -> None:
    row = status(
        root,
        gold(
            alternative_solutions_total=None,
            alternative_solutions_accepted=None,
            alternative_solution_rewards={},
        ),
    )
    assert row["status"] == "provisional"


def admission_record(**changes: Any) -> dict[str, Any]:
    record = AdmissionRecord(
        task="/packages/sample-task",
        package_digest="sha256:" + "b" * 64,
        created_by="test",
        created_at="2026-11-02T12:00:00Z",
        verdict="admitted",
        duration_s=1,
        steps=[
            AdmissionStepRecord(
                name=step.name,
                kind=step.kind,
                duration_s=0,
                status="pass",
                rewards=[step.expect if step.expect is not None else 0.0]
                if step.kind in {"control", "cheat"}
                else [],
            )
            for step in default_steps()
        ],
    ).model_dump(by_alias=True, mode="json")
    record.update(changes)
    return record


def test_admission_directory_and_sha_provenance(root: Path) -> None:
    record = admission_record()
    file = save(root, "admissions/admission-bbbb.json", record)
    manifest = build(root, admission=[file.parent, file])
    assert len(manifest["tasks"]) == len(manifest["inputs"]) == 1
    row = manifest["tasks"][0]
    assert row["task_id"] == "sample-task"
    assert row["status"] == "provisional"
    assert row["package_digest"] == record["package_digest"]
    assert row["evidence"] == manifest["inputs"]
    assert row["evidence"][0] == {
        "kind": "admission",
        "path": str(file),
        "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
    }


def test_incomplete_admission_does_not_trust_verdict(root: Path) -> None:
    record = admission_record()
    record["steps"] = record["steps"][:-1]
    file = save(root, "admission.json", record)
    row = build(root, admission=[file])["tasks"][0]
    assert row["status"] == "uncertified"


def test_admission_attack_and_survivor_are_weak(root: Path) -> None:
    record = admission_record(survivor_patches=["mutant.patch"])
    file = save(root, "admission.json", record)
    assert build(root, admission=[file])["tasks"][0]["status"] == "weak"
    record["survivor_patches"] = []
    next(step for step in record["steps"] if step["kind"] == "cheat")["rewards"] = [1.0]
    save(root, "admission.json", record)
    assert build(root, admission=[file])["tasks"][0]["reason"].startswith("ATTACK_ACCEPTED:")


@pytest.mark.parametrize(
    "payload",
    [
        {"run_id": "r", "task_id": "sample", "label": "cheat"},
        {"tasks": [gold()], "metadata": {"raw_reward": 1}},
        {"tasks": [gold()], "metadata": {"nested": [{"is_cheat": True}]}},
        {"tasks": [gold()], "adjudications": []},
        {"tasks": [gold()], "real_submissions": []},
        {"tasks": [gold()], "runs": []},
        [{"task_id": "sample", "trajectory_path": "trace.json", "reward": 1}],
    ],
)
@pytest.mark.parametrize("argument", ["admission", "gold_results", "alt_replays"])
def test_circularity_guard_inspects_all_inputs(root: Path, payload: Any, argument: str) -> None:
    file = save(root, "innocent.json", payload)
    with pytest.raises(RefusedRun, match="circular"):
        build(root, **{argument: [file]})


@pytest.mark.parametrize("name", ["labels.json", "rollout-manifest.jsonl", "trajectory.json"])
def test_circularity_guard_refuses_filenames(root: Path, name: str) -> None:
    with pytest.raises(RefusedRun, match="filename"):
        build(root, gold_results=[save(root, name, gold())])


@pytest.mark.parametrize(
    "name", ["answer-keys/file.json", "held-out/file.json", "test-split/file.json"]
)
def test_dev_path_gate(root: Path, name: str) -> None:
    with pytest.raises(RefusedRun, match="forbidden"):
        build(root, gold_results=[save(root, name, gold())])


def test_dev_registry_and_nested_split_gates(root: Path) -> None:
    with pytest.raises(RefusedRun, match="assigned TEST"):
        status(root, gold(task="private-task"))
    with pytest.raises(RefusedRun, match="non-DEV"):
        status(root, gold(metadata={"split": "test"}))
    with pytest.raises(RefusedRun, match="forbidden"):
        status(root, gold(upgraded_package="/corpus/test/package"))


def test_manifest_loads_unchanged_in_hack_drop(root: Path) -> None:
    rows = [
        gold("a", alternative_solutions_total=2, alternative_solutions_accepted=2),
        gold("b"),
        gold("c", nop_reward=1.0),
        gold("d", reference_reward=0.0),
    ]
    manifest = build(root, gold_results=[save(root, "matrix.json", {"tasks": rows})])
    output = save(root, "status.json", manifest)
    loaded = load_certified_tasks(output)
    assert {key: value["status"] for key, value in loaded.items()} == {
        "a": "certified",
        "b": "provisional",
        "c": "weak",
        "d": "uncertified",
    }
    assert loaded == {
        row["task_id"]: {"status": row["status"], "reason": row["reason"]}
        for row in manifest["tasks"]
    }


def test_deterministic_input_order_and_conflicting_versions(root: Path) -> None:
    one = save(root, "one.json", gold("one"))
    two = save(root, "two.json", gold("two"))
    assert build(root, gold_results=[one, two]) == build(root, gold_results=[two, one])
    conflict = save(root, "conflict.json", gold("one", package_digest="sha256:" + "c" * 64))
    bound = save(root, "bound.json", gold("one", package_digest="sha256:" + "d" * 64))
    with pytest.raises(ValueError, match="conflicting package digests"):
        build(root, gold_results=[conflict, bound])
    with pytest.raises(ValueError, match="mixed bound and unbound"):
        build(root, gold_results=[one, bound])


def test_same_task_order_is_deterministic_and_alternatives_are_not_summed(root: Path) -> None:
    shared = {"package_digest": "sha256:" + "e" * 64}
    one = save(
        root,
        "one.json",
        gold(
            alternative_solutions_total=1,
            alternative_solutions_accepted=1,
            **shared,
        ),
    )
    two = save(
        root,
        "two.json",
        gold(
            alternative_solutions_total=1,
            alternative_solutions_accepted=1,
            **shared,
        ),
    )
    assert build(root, gold_results=[one, two])["tasks"][0]["status"] == "provisional"
    save(root, "one.json", gold(nop_reward=1.0, **shared))
    save(root, "two.json", gold(witness_reward=1.0, **shared))
    assert build(root, gold_results=[one, two]) == build(root, gold_results=[two, one])


def test_unbound_receipts_cannot_mix_batches_even_at_same_path(root: Path) -> None:
    one = save(root, "one.json", gold())
    two = save(
        root, "two.json", gold(alternative_solutions_total=2, alternative_solutions_accepted=2)
    )
    with pytest.raises(ValueError, match="unbound package versions"):
        build(root, gold_results=[one, two])


def test_separate_replay_reuses_gold_receipt_not_bare_counts(root: Path) -> None:
    receipt = save(
        root, "replay.json", gold(alternative_solutions_total=2, alternative_solutions_accepted=2)
    )
    assert build(root, alt_replays=[receipt])["tasks"][0]["status"] == "certified"
    save(
        root,
        "replay.json",
        {
            "task_id": "sample",
            "alternative_solutions_total": 2,
            "alternative_solutions_accepted": 2,
        },
    )
    with pytest.raises(ValueError, match="TaskCertificationResult"):
        build(root, alt_replays=[receipt])


def test_legacy_eight_task_matrix_ignores_claims(root: Path) -> None:
    row = {
        "task": "000123",
        "kind": "original",
        "reference_reward": 1.0,
        "nop_reward": 0.0,
        "witness_rewards": [0.0, 0.0],
        "exit0_pth_reward": 0.0,
        "gate1_ref": True,
        "gate2_nop": True,
        "gate3_wit": True,
        "gate4_exit0": True,
        "train_total": 2,
        "train_killed": 2,
        "eval_total": 2,
        "eval_killed": 1,
        "status": "gold",
        "is_weak": False,
    }
    file = save(root, "legacy.json", {"n_tasks": 1, "gate1_reference": "1/1", "tasks": [row]})
    result = build(root, gold_results=[file])["tasks"][0]
    assert result["task_id"] == "format-code-task-000123"
    assert result["status"] == "weak"


def test_legacy_failed_batch_reference_is_not_provisional(root: Path) -> None:
    file = save(
        root,
        "legacy.json",
        {
            "schema_version": "gold-v1/manifest/v1",
            "tasks": [
                {
                    "task_id": "sample",
                    "status": "certified",
                    "is_weak": True,
                    "gates": {"reference_passed": False},
                    "counts": {},
                }
            ],
        },
    )
    row = build(root, gold_results=[file])["tasks"][0]
    assert row["status"] == "uncertified"
    assert row["reason"].startswith("REFERENCE_FAILED:")


def test_filtered_gold_subset_is_not_complete_gate(root: Path) -> None:
    row = {
        "task_id": "sample",
        "rounds_executed": 1,
        "accepted_test": True,
        "status": "gold_heldout",
        "is_weak": False,
        "package_dir": "/packages/sample/r1",
        "upgraded_package_sha256": "f" * 64,
        "gates": {
            "reference_passed": True,
            "nop_passed": True,
            "witness_passed": True,
            "exit0_passed": True,
        },
        "counts": {
            "train_total": 2,
            "train_killed": 2,
            "probe_total": 2,
            "probe_killed": 2,
            "eval_total": 2,
        },
        "paired_eval_base_accepted": [{"arm2_killed": True}],
        "env": {"workdir": "/testbed"},
    }
    result = status(root, row)
    assert result["status"] == "uncertified"
    assert result["package_digest"] is None  # historical field hashes only the test file
    row["paired_eval_base_accepted"].append({"arm2_killed": False})
    assert status(root, row)["status"] == "weak"
    row["paired_eval_base_accepted"][1]["arm2_killed"] = True
    assert status(root, row)["status"] == "provisional"


def test_cli_build_and_no_output_on_rejection(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("evallab.task_status._code_revision", lambda _: "a" * 40)
    source = save(root, "gold.json", gold())
    command = [
        "tasks",
        "status",
        "build",
        "--gold-results",
        str(source),
        "--out",
        str(root / "status.json"),
    ]
    assert run_cli(command, workspace=root) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["counts"] == {"certified": 0, "provisional": 1, "weak": 0, "uncertified": 0}
    assert load_certified_tasks(root / "status.json")["sample-task"]["status"] == "provisional"
    assert run_cli(command, workspace=root) == 2
    capsys.readouterr()
    save(root, "gold.json", gold(metadata={"run_id": "r1"}))
    command[-1] = str(root / "refused.json")
    assert run_cli(command, workspace=root) == 2
    assert not (root / "refused.json").exists()


def test_cli_dev_output_refused_before_writing(root: Path) -> None:
    source = save(root, "gold.json", gold())
    out = root / "answer-keys/status.json"
    assert (
        run_cli(
            ["tasks", "status", "build", "--gold-results", str(source), "--out", str(out)],
            workspace=root,
        )
        == 2
    )
    assert not out.exists()
