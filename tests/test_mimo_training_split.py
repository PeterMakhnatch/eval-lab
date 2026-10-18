from __future__ import annotations

import copy
import csv
import hashlib
import json
import runpy
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

DRIVER = (
    Path(__file__).resolve().parents[1]
    / "research/experiments/mimo-training-20261016/freeze.py"
)
TRAIN_ONLY = "format-code-task-009999"
PREFIX = "mimo-v2.6-rl/"


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    namespace = runpy.run_path(str(DRIVER))
    monkeypatch.setitem(
        namespace["release_scoring"].__globals__,
        "datetime",
        SimpleNamespace(now=lambda tz: datetime(2026, 10, 16, tzinfo=tz)),
    )
    return namespace


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_arms(directory: Path, task_ids: list[str]) -> None:
    directory.mkdir(parents=True)
    hashes = {}
    for arm in "ABC":
        rows = [{"task": PREFIX + task_id, "execution_id": f"fixture-{n}"} for n, task_id in enumerate(task_ids)] if arm == "A" else []
        data = "".join(json.dumps(row) + "\n" for row in rows).encode()
        (directory / f"{arm}.jsonl").write_bytes(data)
        hashes[arm] = sha(data)
    (directory / "manifest.json").write_text(json.dumps({
        "tasks": [PREFIX + task_id for task_id in task_ids], "arm_sha256": hashes,
    }), encoding="utf-8")


@pytest.fixture
def make_case(tmp_path: Path):
    def make(candidate_count: int = 36, extra_ids: tuple[str, ...] = ()) -> dict[str, Any]:
        repo = tmp_path / "repo"
        packages = tmp_path / "packages"
        training = tmp_path / "training"
        v6_path = repo / "research/experiments/mimo-clean-v6/manifest.csv"
        reference_path = repo / "research/experiments/mimo-reference-fixes/index.csv"
        ordinary_path = repo / "research/experiments/qa-trace/regrade/ordinary-passes.jsonl"
        findings_path = tmp_path / "findings.jsonl"
        ids = [f"format-code-task-{n:06d}" for n in range(1, candidate_count + 1)] + list(extra_ids)
        v6_rows = []
        reference_rows = []
        findings = []
        for task_id in ids + [TRAIN_ONLY]:
            package_path = f"variants/{task_id}"
            v6_rows.append({
                "task_id": task_id, "language": "python", "status": "built",
                "package_path": package_path, "final_digest": "sha256:" + sha(task_id.encode()),
            })
            if task_id == TRAIN_ONLY:
                continue
            solve = packages / package_path / "solution/solve.sh"
            solve.parent.mkdir(parents=True)
            solve.write_text(f"fixture reference {task_id}\n", encoding="utf-8")
            patch = tmp_path / f"{task_id}.patch"
            patch.write_text(f"fixture patch {task_id}\n", encoding="utf-8")
            reference_rows.append({
                "task_id": task_id, "label": "oracle:pass+nop:fail",
                "patch_path": str(patch), "patch_sha256": sha(patch.read_bytes()),
            })
            n = int(task_id.rsplit("-", 1)[1])
            findings.append({
                "subject": {"task_id": task_id, "task_digest": "sha256:" + "f" * 64},
                "verdict": "fail" if n in {2, 3, 5} else "unknown" if n == 4 else "pass",
                "refutation": {"status": "verified" if n == 2 else "refuted" if n == 5 else "unverified"},
                "stage": "S2.ref_subset",
            })
        write_csv(v6_path, list(v6_rows[0]), v6_rows)
        write_csv(reference_path, list(reference_rows[0]), reference_rows)
        ordinary_path.parent.mkdir(parents=True)
        ordinary_path.write_text(json.dumps({
            "task": PREFIX + TRAIN_ONLY, "execution_id": "ordinary-execution",
        }) + "\n", encoding="utf-8")
        findings_path.write_text("".join(json.dumps(row) + "\n" for row in findings), encoding="utf-8")
        write_arms(training / "actual-build", [TRAIN_ONLY, ids[0]])
        config = {
            "repo_root": str(repo), "package_root": str(packages),
            "ordinary": str(ordinary_path), "v6_manifest": str(v6_path),
            "reference_index": str(reference_path), "findings": str(findings_path),
            "training_root": str(training),
        }
        return {"config": config, "ids": ids, "v6_rows": v6_rows, "output": tmp_path / "frozen"}
    return make


def freeze_case(driver: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    split = driver["build_split"](case["config"])
    split_path = case["output"] / "split.json"
    driver["create_json"](split_path, split)
    driver["create_json"](case["output"] / "heldout.json", driver["heldout_payload"](split, split_path))
    return split


def pair_rows(driver: dict[str, Any], case: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for v6 in case["v6_rows"]:
        if v6["task_id"] == TRAIN_ONLY:
            continue
        row = dict.fromkeys(driver["PAIR_COLUMNS"], "")
        row.update(
            task_id=v6["task_id"],
            original_package_digest="sha256:" + sha(("v5:" + v6["task_id"]).encode()),
            original_package_path=f"original/{v6['task_id']}",
            v6_package_digest=v6["final_digest"], v6_package_path=v6["package_path"],
            repair_disposition="missing: no verified distinct test repair",
        )
        rows.append(row)
    return rows


def release_case(driver, case):
    split = freeze_case(driver, case)
    release_path = case["output"] / "validity-release.json"
    driver["create_json"](release_path, {
        "schema": "mimo.validity-release/v1",
        "split": driver["fingerprint"](case["output"] / "split.json"),
        "schema_agreement": {
            "status": "accepted",
            "evidence": [{"role": role} for role in ("QA Trace", "Trainer")],
        },
    })
    pairs_path = case["output"] / "package-pairs.csv"
    write_csv(pairs_path, list(driver["PAIR_COLUMNS"]), pair_rows(driver, case))
    driver["replace_json"](case["output"] / "heldout.json", driver["heldout_payload"](split, case["output"] / "split.json", release_path))
    driver["create_json"](case["output"] / "manifest.json", driver["manifest_payload"](split, case["output"], pairs_path, release_path=release_path))
    return split


@pytest.mark.parametrize("value", [
    "another/format-code-task-000001",
    "MIMO-v2.6-rl/format-code-task-000001",
    "mimo-v2.6-rl/nested/format-code-task-000001",
    "/format-code-task-000001", " format-code-task-000001", "../format-code-task-000001",
])
def test_namespace_mismatch_and_ambiguous_ids_are_refused(driver: dict[str, Any], value: str) -> None:
    with pytest.raises(ValueError, match="namespace or ambiguous"):
        driver["canonical_id"](value)


def test_exact_namespace_is_normalized_but_unknown_id_is_refused(driver: dict[str, Any]) -> None:
    task_id = "format-code-task-000001"
    assert driver["canonical_id"](PREFIX + task_id, {task_id}) == task_id
    with pytest.raises(ValueError, match="unknown task ID"):
        driver["canonical_id"]("format-code-task-999999", {task_id})


def test_all_actual_training_metadata_is_excluded_and_rank_is_deterministic(driver, make_case) -> None:
    case = make_case()
    split = driver["build_split"](case["config"])
    assert split["train"] == [case["ids"][0], TRAIN_ONLY]
    assert split["train_outside_reference_pool"] == [TRAIN_ONLY]
    assert split["counts"]["training_unique_tasks"] == 2
    assert split["counts"]["eligible_completed_pass_or_refuted"] == 32
    assert len(split["heldout"]) == 30
    assert not set(split["train"]) & set(split["heldout"])
    assert not set(case["ids"][1:4]) & set(split["heldout"])
    assert case["ids"][4] in split["selection_rule"]["eligible"]  # completed refutation
    expected = sorted(split["selection_rule"]["eligible"], key=lambda task_id: sha(f"20261016:{task_id}".encode()))[:30]
    assert split["heldout"] == expected
    assert driver["build_split"](case["config"]) == split
    assert all("/" not in task_id for task_id in split["train"] + split["heldout"])
    assert all(row["sha256"] and row["bytes"] >= 0 for row in split["training_inputs"])


def test_training_leakage_in_frozen_assignment_is_refused(driver, make_case) -> None:
    split = driver["build_split"](make_case()["config"])
    split["heldout"][0] = split["train"][0]
    with pytest.raises(ValueError, match="training leakage"):
        driver["validate_split"](split)


def test_minimum_size_reports_exact_gap_without_unknown_fill(driver, make_case) -> None:
    case = make_case(candidate_count=33)
    with pytest.raises(ValueError, match="only 29 eligible.*gap 1; no unknown fill"):
        driver["build_split"](case["config"])
    assert not case["output"].exists()


def test_frozen_input_byte_drift_is_refused_without_rewriting_outputs(driver, make_case) -> None:
    case = make_case()
    freeze_case(driver, case)
    split_path = case["output"] / "split.json"
    before = split_path.read_bytes()
    ordinary = Path(case["config"]["ordinary"])
    ordinary.write_bytes(ordinary.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="frozen input drift"):
        driver["check_frozen"](case["output"])
    assert split_path.read_bytes() == before


def test_later_training_dataset_addition_fails_closed(driver, make_case) -> None:
    case = make_case()
    split = freeze_case(driver, case)
    write_arms(Path(case["config"]["training_root"]) / "later-build", [split["heldout"][0]])
    with pytest.raises(ValueError, match="training inventory drift"):
        driver["check_frozen"](case["output"])


def test_frozen_outputs_cannot_be_overwritten(driver, make_case) -> None:
    case = make_case()
    split = freeze_case(driver, case)
    with pytest.raises(FileExistsError):
        driver["create_json"](case["output"] / "split.json", split)


def test_manifest_binds_pair_bytes_and_resolves_target_without_claiming_controls(driver, make_case) -> None:
    case = make_case()
    split = freeze_case(driver, case)
    pairs_path = case["output"] / "package-pairs.csv"
    write_csv(pairs_path, list(driver["PAIR_COLUMNS"]), pair_rows(driver, case))
    manifest = driver["manifest_payload"](split, case["output"], pairs_path)
    assert manifest["package_pairs"]["sha256"] == sha(pairs_path.read_bytes())
    assert manifest["train"] == split["train"]
    assert manifest["training_tasks_without_reference_pairs"] == [TRAIN_ONLY]
    assert manifest["scoring_allowed"] is False
    assert manifest["canonical_id_form"] == "bare-source-id"
    assert manifest["task_family"] == "mimo-v2.6-rl"
    assert json.loads((case["output"] / "heldout.json").read_text(encoding="utf-8"))["heldout"] == split["heldout"]
    assert manifest["counts"]["heldout_distinct_test_repaired_rows"] == 0
    assert manifest["evaluation_target_policy"]["status"] == "resolved"
    assert all(row["evaluation_target"]["package_digest"] == row["v6_package_digest"] for row in manifest["tasks"])
    assert all(row["evaluation_target"]["repair_scope"] == "global-v6-hardening-only" and not row["evaluation_target"]["test_repair_claim"] for row in manifest["tasks"])
    assert all(not row["test_repaired_package_digest"] for row in manifest["tasks"])
    assert all(control["status"] == "unproven" for row in manifest["tasks"] for control in row["source_controls"].values())


def test_unchanged_v6_is_never_exported_as_test_repaired(driver, make_case) -> None:
    case = make_case()
    split = freeze_case(driver, case)
    rows = pair_rows(driver, case)
    rows[0]["test_repaired_package_digest"] = rows[0]["v6_package_digest"]
    rows[0]["test_repaired_package_path"] = rows[0]["v6_package_path"]
    pairs_path = case["output"] / "package-pairs.csv"
    write_csv(pairs_path, list(driver["PAIR_COLUMNS"]), rows)
    with pytest.raises(ValueError, match="unchanged/unbound target is not a test repair"):
        driver["manifest_payload"](split, case["output"], pairs_path)


def test_non_code_training_only_identity_is_preserved(driver, make_case) -> None:
    case = make_case()
    task_id = "arvo_18737"
    ordinary = Path(case["config"]["ordinary"])
    ordinary.write_text(json.dumps({"task": PREFIX + task_id, "execution_id": "terminal-source"}) + "\n", encoding="utf-8")
    identity = Path(case["config"]["repo_root"]) / "library/task-variants" / f"mimo-v2.6-rl__{task_id}" / "record.json"
    identity.parent.mkdir(parents=True)
    identity.write_text(json.dumps({"task_name": PREFIX + task_id}), encoding="utf-8")
    split = driver["build_split"](case["config"])
    assert task_id in split["train"]
    assert task_id in split["train_outside_reference_pool"]
    assert task_id not in split["heldout"]
    assert any(row["path"] == str(identity) for row in split["sources"])


def test_unknown_training_identity_is_refused(driver, make_case) -> None:
    case = make_case()
    ordinary = Path(case["config"]["ordinary"])
    ordinary.write_text(json.dumps({"task": "unknown-source-task", "execution_id": "unknown-source"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown task ID .*no manifest or identity record"):
        driver["build_split"](case["config"])


def test_pair_csv_byte_drift_invalidates_frozen_manifest(driver, make_case) -> None:
    case = make_case()
    split = freeze_case(driver, case)
    pairs_path = case["output"] / "package-pairs.csv"
    write_csv(pairs_path, list(driver["PAIR_COLUMNS"]), pair_rows(driver, case))
    manifest = driver["manifest_payload"](split, case["output"], pairs_path)
    driver["create_json"](case["output"] / "manifest.json", manifest)
    pairs_path.write_bytes(pairs_path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="frozen package manifest/input drift"):
        driver["check_frozen"](case["output"])


def test_repair_record_must_bind_exact_v6_parent(driver, make_case) -> None:
    case = make_case()
    split = freeze_case(driver, case)
    rows = pair_rows(driver, case)
    row = rows[0]
    row["test_repaired_package_digest"] = "sha256:" + "e" * 64
    row["test_repaired_package_path"] = "distinct-repair"
    record = case["output"] / "repair-record.json"
    record.write_text(json.dumps({
        "task_name": PREFIX + row["task_id"],
        "variant_digest": row["test_repaired_package_digest"],
        "parent": {"digest": "sha256:" + "d" * 64},
        "transform": "verifier-repair@1",
    }), encoding="utf-8")
    row["repair_record_path"] = str(record)
    row["repair_record_sha256"] = "sha256:" + sha(record.read_bytes())
    pairs_path = case["output"] / "package-pairs.csv"
    write_csv(pairs_path, list(driver["PAIR_COLUMNS"]), rows)
    with pytest.raises(ValueError, match="repair is not derived from the exact v6 parent"):
        driver["manifest_payload"](split, case["output"], pairs_path)


def test_research_pins_preserve_base_and_broken_history_never_becomes_active(driver, make_case) -> None:
    case = make_case()
    base = driver["build_split"](case["config"])
    broken = "format-code-task-000838"
    identity = Path(case["config"]["repo_root"]) / "library/task-variants" / f"mimo-v2.6-rl__{broken}" / "identity.json"
    identity.parent.mkdir(parents=True)
    identity.write_text(json.dumps({"task_name": PREFIX + broken}), encoding="utf-8")
    pins = case["ids"][1:4]  # supported/unresolved/unmeasured fixtures, never automatic clean candidates
    revision = {
        "prior_heldout": base["heldout"], "prior_training_exposure": [broken],
        "added_heldout": pins, "provenance": {"authority": "deterministic fixture"},
    }
    split = driver["build_split"](case["config"], revision=revision)
    assert split["heldout"][:30] == base["heldout"]
    assert split["heldout"][30:] == pins
    assert len(split["heldout"]) == 33
    assert broken in split["training_exposure"] and broken not in split["train"]
    assert not set(split["training_exposure"]) & set(split["heldout"])
    assert all(row["readiness"] == "repair/control-pending; not certified clean" for row in split["heldout_task_metadata"] if row["task_id"] in pins)
    split["heldout"][0] = broken
    with pytest.raises(ValueError, match="training leakage"):
        driver["validate_split"](split)


@pytest.mark.parametrize("task_id,use_repair", [(None, True), ("format-code-task-001425", False)])
def test_repair_availability_does_not_override_research_target_disposition(driver, make_case, task_id, use_repair) -> None:
    case = make_case(extra_ids=(task_id,) if task_id is not None else ())
    split = freeze_case(driver, case)
    rows = pair_rows(driver, case)
    selected_id = task_id if task_id is not None else split["heldout"][0]
    assert selected_id in split["heldout"]
    row = next(row for row in rows if row["task_id"] == selected_id)
    row["test_repaired_package_digest"] = "sha256:" + "e" * 64
    row["test_repaired_package_path"] = "distinct-repair"
    record = case["output"] / "repair-record.json"
    record.write_text(json.dumps({
        "task_name": PREFIX + row["task_id"], "variant_digest": row["test_repaired_package_digest"],
        "parent": {"digest": row["v6_package_digest"]}, "transform": "verifier-repair@1",
    }), encoding="utf-8")
    row["repair_record_path"] = str(record)
    row["repair_record_sha256"] = "sha256:" + sha(record.read_bytes())
    pairs_path = case["output"] / "package-pairs.csv"
    write_csv(pairs_path, list(driver["PAIR_COLUMNS"]), rows)
    manifest = driver["manifest_payload"](split, case["output"], pairs_path)
    selected = next(task for task in manifest["tasks"] if task["task_id"] == row["task_id"])
    expected_column = "test_repaired" if use_repair else "v6"
    assert selected["evaluation_target"]["package_digest"] == row[f"{expected_column}_package_digest"]
    assert selected["evaluation_target"]["test_repair_claim"] is use_repair
    assert manifest["counts"]["heldout_distinct_test_repaired_rows"] == 1
    assert manifest["counts"]["heldout_evaluation_target_v6_rows"] == (29 if use_repair else 30)


def test_released_membership_survives_publication_metadata_and_same_pool_rebuild(driver, make_case) -> None:
    case = make_case()
    split = release_case(driver, case)
    original_split = (case["output"] / "split.json").read_bytes()
    findings = Path(case["config"]["findings"])
    rows = [json.loads(line) for line in findings.read_text().splitlines()]
    findings.write_text("".join(json.dumps(row | {"publication_note": "updated"}) + "\n" for row in rows))
    write_arms(Path(case["config"]["training_root"]) / "rebuilt", split["training_exposure"])
    checked = driver["check_frozen"](case["output"])
    assert checked["heldout"] == split["heldout"]
    assert (case["output"] / "split.json").read_bytes() == original_split
    heldout = json.loads((case["output"] / "heldout.json").read_text())
    assert heldout["freeze_ready"] is True
    assert heldout["validity_check_allowed"] is True
    assert heldout["scoring_allowed"] is False


@pytest.mark.parametrize("change", ["training_leakage", "heldout_evidence"])
def test_released_membership_refuses_material_changes_without_reshuffling(driver, make_case, change) -> None:
    case = make_case()
    split = release_case(driver, case)
    before = (case["output"] / "split.json").read_bytes()
    if change == "training_leakage":
        write_arms(Path(case["config"]["training_root"]) / "leaking", [split["heldout"][0]])
    else:
        findings = Path(case["config"]["findings"])
        rows = [json.loads(line) for line in findings.read_text().splitlines()]
        row = next(row for row in rows if row["subject"]["task_id"] == split["heldout"][0])
        row["verdict"] = "unknown"
        findings.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError):
        driver["check_frozen"](case["output"])
    assert (case["output"] / "split.json").read_bytes() == before


def test_scoring_release_opens_both_gates_without_changing_frozen_bindings(driver, make_case) -> None:
    case = make_case()
    release_case(driver, case)
    output = case["output"]
    before_heldout = json.loads((output / "heldout.json").read_text())
    before_manifest = json.loads((output / "manifest.json").read_text())
    before_split = (output / "split.json").read_bytes()
    before_pairs = (output / "package-pairs.csv").read_bytes()
    predecessor_heldout = driver["fingerprint"](output / "heldout.json")
    predecessor_manifest = driver["fingerprint"](output / "manifest.json")
    driver["release_scoring"](output, "research-harbor", "Validity gate confirmed; flags ruled.")
    heldout = json.loads((output / "heldout.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert heldout == before_heldout | {"scoring_allowed": True}
    assert manifest == before_manifest | {
        "scoring_allowed": True,
        "heldout_manifest": driver["fingerprint"](output / "heldout.json"),
    }
    assert (output / "split.json").read_bytes() == before_split
    assert (output / "package-pairs.csv").read_bytes() == before_pairs
    custody = json.loads((output / "scoring-release.json").read_text())
    assert custody["predecessor_heldout_manifest"] == predecessor_heldout
    assert custody["predecessor_package_manifest"] == predecessor_manifest
    assert custody["released_heldout_manifest"] == driver["fingerprint"](output / "heldout.json")
    assert custody["released_package_manifest"] == driver["fingerprint"](output / "manifest.json")
    checked = driver["check_frozen"](output)
    assert checked["heldout"] == heldout["heldout"]


@pytest.mark.parametrize("actor,approval", [
    ("trainer", "Validity gate confirmed."),
    ("research-harbor", ""),
    (None, "Validity gate confirmed."),
])
def test_scoring_release_requires_research_authority_before_writing(driver, make_case, actor, approval) -> None:
    case = make_case()
    release_case(driver, case)
    output = case["output"]
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(ValueError):
        driver["release_scoring"](output, actor, approval)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


@pytest.mark.parametrize("change", [
    "missing_custody", "predecessor_hash", "package_binding",
    "heldout_gate_type", "manifest_gate_type",
])
def test_scoring_release_still_refuses_unbound_or_changed_packages(driver, make_case, change) -> None:
    case = make_case()
    release_case(driver, case)
    output = case["output"]
    driver["release_scoring"](output, "research-harbor", "Validity gate confirmed; flags ruled.")
    custody_path = output / "scoring-release.json"
    if change == "missing_custody":
        custody_path.unlink()
    elif change == "predecessor_hash":
        custody = json.loads(custody_path.read_text())
        custody["predecessor_package_manifest"]["sha256"] = "0" * 64
        custody_path.write_text(json.dumps(custody))
    elif change.endswith("_gate_type"):
        path = output / ("heldout.json" if change == "heldout_gate_type" else "manifest.json")
        payload = json.loads(path.read_text())
        payload["scoring_allowed"] = 1
        driver["replace_json"](path, payload)
    else:
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["tasks"][0]["evaluation_target"]["package_digest"] = "sha256:" + "f" * 64
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        driver["check_frozen"](output)


def native_control_fixture(
    job: Path, package: Path, task_id: str, agent: str, reward: float, *,
    repeats: int = 1, failed: bool = False, case_evidence: bool = True,
) -> dict[str, Any]:
    """Persist native-shaped evidence; caller flags deliberately cannot prove case execution."""
    from evallab.registry import compute_task_digests
    from evallab.task_variants import harbor_task_digest

    identity, harbor = compute_task_digests(package), harbor_task_digest(package)
    raw = job / "trial"
    raw.mkdir(parents=True)
    config = {"task": {"path": str(package)}, "agent": {"name": agent}}
    finished = "2026-10-17T22:00:00+00:00"
    payloads = {
        job / "lab-metadata.json": {
            "exit_code": 1 if failed else 0, "timed_out": False,
            "task_staging": {
                "declared_task_name": PREFIX + task_id, "staged_task_name": package.name,
                "source_package_digest": identity.package, "source_harbor_digest": harbor,
                "staged_harbor_digest": harbor,
            },
        },
        job / "result.json": {"n_total_trials": 1, "stats": {}, "finished_at": finished},
        job / "config.json": {}, job / "lock.json": {},
        raw / "result.json": {
            "id": job.name, "task_name": PREFIX + task_id, "trial_name": "trial",
            "config": config, "task_id": {"path": str(package)}, "finished_at": finished,
            "agent_execution": {"started_at": finished, "finished_at": finished},
            "verifier_result": {"rewards": {"reward": reward}},
            "exception_info": {"exception_type": "SetupError"} if failed else None,
        },
        raw / "config.json": config,
        raw / "lock.json": {
            "task": {"path": str(package), "name": package.name, "type": "local", "digest": harbor},
        },
    }
    for path, payload in payloads.items():
        path.write_text(json.dumps(payload))
    if agent == "oracle":
        (raw / "agent").mkdir()
        (raw / "agent/oracle.txt").write_text("oracle submission completed\n")
    verifier = raw / "verifier"
    verifier.mkdir()
    for index in range(repeats):
        target = verifier if repeats == 1 else verifier / "repeat" / str(index)
        target.mkdir(parents=True, exist_ok=True)
        if not failed and case_evidence:
            (target / "runner.txt").write_text("RUNNER=pytest\n")
            (target / "test_output.log").write_text(
                "1 passed in 0.01s\n" if reward == 1.0 else "1 failed in 0.01s\n"
            )
    if repeats > 1:
        (verifier / "stability.json").write_text(json.dumps({
            "schema": "evallab.verifier_stability/v1", "repeat_n_requested": repeats,
            "n_runs": repeats, "first_reward": reward,
            "runs": [{"index": index, "reward": reward, "error": None} for index in range(repeats)],
        }))
    binding = {
        "source_package_digest": identity.package, "task_digest": harbor,
        "verified": True, "error": None, "via": "trial-lock",
    }
    observation = {
        "agent": agent, "execution_id": job.name, "task_id": task_id,
        "source_package_digest": identity.package, "source_harbor_digest": harbor,
        "staged_harbor_digest": harbor, "executed_harbor_digest": harbor, "native_identity_verified": True,
        "returncode": 1 if failed else 0, "timed_out": False,
        "process_ok": not failed, "process_failed": failed, "exception_present": failed,
        "scored": not failed, "rewards": [reward] * repeats, "repeat_scored": [not failed] * repeats,
        "test_execution": [not failed] * repeats, "run_error_present": [failed] * repeats,
        "run_errors": ["setup error" if failed else None] * repeats, "task_binding": binding,
    }
    for owner, names in ((observation, ("metadata", "result")), (binding, ("config", "lock"))):
        for name in names:
            path = job / "lab-metadata.json" if name == "metadata" else raw / f"{name}.json"
            owner[f"{name}_path"] = str(path)
            owner[f"{name}_sha256"] = sha(path.read_bytes())
    return observation


@pytest.fixture
def successor_case(driver, tmp_path: Path):
    """Real scratch packages/variant records; no runner, mocks, or shared store."""
    from evallab.registry import compute_task_digests
    from evallab.task_variants import derive_task

    repo, packages, frozen = tmp_path / "repo", tmp_path / "packages", tmp_path / "frozen33"
    variants = packages / "variants"
    frozen.mkdir()
    membership = json.loads(DRIVER.with_name("heldout.json").read_text())["heldout"]
    exposure = [f"historical-training-{index:03d}" for index in range(42)]
    split = {
        "schema": "mimo.training-split/v1", "heldout": membership,
        "train": exposure[:40], "training_exposure": exposure,
        "config": {"package_root": str(packages)}, "heldout_task_metadata": [],
    }
    manifest = {
        "schema": "mimo.training-manifest/v1", "heldout": membership,
        "train": split["train"], "training_exposure": exposure, "tasks": [],
    }
    controls = {
        "schema": "mimo.native-controls/v1", "backend": "modal", "scoring_allowed": False,
        "qualification_scope": "Retained exact-v6 oracle x1 and nop x1; not repeat3 qualification",
        "observations": [], "selected_pairs": [],
    }
    registration = {
        "schema": "mimo.frozen-lineage-registration/v1", "endpoints": [], "frozen_sources": [],
    }
    rows, records, package_paths = [], {}, {}
    for task_id in membership:
        base = tmp_path / "sources" / task_id
        for relative, content in {
            "task.toml": f'schema_version = "1.4"\n[task]\nname = "{PREFIX}{task_id}"\n',
            "instruction.md": f"Implement the requested behavior for {task_id}.\n",
            "environment/Dockerfile": "FROM ubuntu:24.04\n",
            "environment/setup/setup.sh": "#!/bin/sh\npython -V\n",
            "solution/solve.sh": "#!/bin/sh\nprintf 'reference implementation\\n'\n",
            "tests/test.sh": "#!/bin/sh\npython -c 'assert 2 + 2 == 4'\n",
            "tests/test.patch": (
                (Path(__file__).parent / "fixtures/mimo_runtime_repair_001246.patch").read_text()
                if task_id == "format-code-task-001246" else
                "diff --git a/test_existing.py b/test_existing.py\nnew file mode 100644\n"
                "--- /dev/null\n+++ b/test_existing.py\n@@ -0,0 +1,2 @@\n"
                "+def test_existing():\n+    assert 2 + 2 == 4\n"
            ),
            "tests/test_command.sh": "#!/bin/sh\npython -m pytest test_existing.py\n",
        }.items():
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        original = derive_task(
            base, changes={"environment/setup/setup.sh": b"#!/bin/sh\npython3 -V\n"},
            transform="leak-close-pypi@1", rationale="scratch ancestry", created_by="pytest",
            repo_root=repo, variants_root=variants,
        )
        original_path = variants / original.task_slug / original.digest12
        record = derive_task(
            original_path,
            changes={"task.toml": (original_path / "task.toml").read_bytes() + b"\n[environment]\nallow_network = false\nworkdir = '/testbed'\n"},
            transform="agent-network-none@1", rationale="scratch frozen endpoint", created_by="pytest",
            parent_source={"kind": "variant", "record": str(original.record_relpath())},
            repo_root=repo, variants_root=variants,
        )
        package = variants / record.task_slug / record.digest12
        records[task_id], package_paths[task_id] = record, package
        row = dict.fromkeys(driver["PAIR_COLUMNS"], "")
        row.update(
            task_id=task_id, original_package_digest=original.variant_digest,
            original_package_path=str(original_path), v6_package_digest=record.variant_digest,
            v6_package_path=str(package), repair_disposition="missing",
        )
        rows.append(row)
        split["heldout_task_metadata"].append({
            "task_id": task_id, "v6_package_digest": record.variant_digest,
            "v6_package_path": str(package.relative_to(packages)),
        })
        manifest["tasks"].append(row | {
            "split": "heldout", "exclusion_reason": None,
            "source_controls": {
                version: {"status": "unproven", "package_digest": row[f"{version}_package_digest"], "evidence": []}
                for version in ("original", "v6")
            },
            "evaluation_target": {
                "package_digest": record.variant_digest, "package_path": str(package),
                "version_column": "v6", "repair_scope": "global-v6-hardening-only", "test_repair_claim": False,
            },
        })
        pair = {"task_id": task_id, "package_digest": record.variant_digest}
        for agent, reward in (("oracle", 1.0), ("nop", 0.0)):
            metadata = tmp_path / "raw" / task_id / agent / "lab-metadata.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({"task_id": task_id, "agent": agent, "package_digest": record.variant_digest}))
            pair[f"{agent}_observation"] = len(controls["observations"])
            controls["observations"].append({
                "source_package_digest": record.variant_digest, "source_harbor_digest": record.variant_harbor_digest,
                "staged_harbor_digest": record.variant_harbor_digest,
                "native_metadata": driver["fingerprint"](metadata),
                "native_exit_code": 0, "timed_out": False,
                "receipt": {"task_id": task_id, "agent": agent, "package": str(package), "reward": reward, "error": None},
                "receipt_source": str(metadata.parent / "controls.jsonl"), "receipt_line": 1,
                "runner_repository": {"commit": "f" * 40, "dirty": True, "uncommitted_diff_sha256": "sha256:" + "e" * 64},
            })
        controls["selected_pairs"].append(pair)
        registration["endpoints"].append({
            "task_id": task_id, "role": "headline", "package_digest": record.variant_digest,
            "package_path": str(package), "harbor_digest": record.variant_harbor_digest,
            "record_path": str(record.record_relpath()),
            "record_sha256": driver["fingerprint"](repo / record.record_relpath())["sha256"],
            "full_transform_chain_newest_first": ["agent-network-none@1", "leak-close-pypi@1"],
        })
    write_csv(frozen / "package-pairs.csv", list(driver["PAIR_COLUMNS"]), rows)

    def refresh() -> None:
        (frozen / "split.json").write_text(json.dumps(split))
        heldout = {
            "schema": "mimo.heldout/v1", "heldout": split["heldout"],
            "split": driver["fingerprint"](frozen / "split.json"), "scoring_allowed": True,
        }
        (frozen / "heldout.json").write_text(json.dumps(heldout))
        manifest.update(
            split=heldout["split"], heldout_manifest=driver["fingerprint"](frozen / "heldout.json"),
            package_pairs=driver["fingerprint"](frozen / "package-pairs.csv"),
        )
        (frozen / "manifest.json").write_text(json.dumps(manifest))
        (frozen / "native-controls.json").write_text(json.dumps(controls))
        for endpoint in registration["endpoints"]:
            endpoint["control_source"] = driver["fingerprint"](frozen / "native-controls.json")
            pair = next(pair for pair in controls["selected_pairs"] if pair["task_id"] == endpoint["task_id"])
            endpoint["controls"] = {}
            for agent in ("oracle", "nop"):
                index = pair[f"{agent}_observation"]
                observation = controls["observations"][index]
                endpoint["controls"][agent] = {
                    "observation_index": index, "reward": observation["receipt"]["reward"],
                    "native_metadata": observation["native_metadata"],
                    "receipt_source": observation["receipt_source"], "receipt_line": observation["receipt_line"],
                }
        registration["frozen_sources"] = [
            driver["fingerprint"](frozen / name) for name in ("heldout.json", "manifest.json", "native-controls.json")
        ]
        (frozen / "lineage-registration.json").write_text(json.dumps(registration))

    refresh()
    inventory = tmp_path / "qa-adjudications.json"
    inventory.write_text(json.dumps({"adjudications": [{"task_id": membership[0], "verdict": "unspecified"}]}))
    repairs = tmp_path / "repairs-v7.json"
    repairs.write_text(json.dumps({
        "schema": "envcheck.repairs-v7/v1", "status": "in_progress", "repairs": [], "complete_count": 0,
        "required_task_count": 16, "recorded_reproducer_count": 25, "training_allowed": False,
        "source_inputs": [driver["fingerprint"](inventory)],
    }))

    def setup(extra_changes: dict[str, bytes] | None = None) -> Path:
        task_id = "format-code-task-001948"
        parent = package_paths[task_id]
        changes = {
            "environment/setup/setup.sh": b"#!/bin/sh\nPATH=/usr/bin:/bin python3 -V\n",
            "task.toml": (parent / "task.toml").read_bytes() + b"\n# Repacked setup payload\n",
        } | (extra_changes or {})
        record = derive_task(
            parent, changes=changes, transform="setup-python-path@1", rationale="scratch setup-only child",
            created_by="pytest", parent_source={"kind": "variant", "record": str(records[task_id].record_relpath())},
            repo_root=repo, variants_root=variants,
        )
        package = variants / record.task_slug / record.digest12
        handoff = tmp_path / "setup-001948-v7.json"
        handoff.write_text(json.dumps({
            "schema": "mimo-v7-setup-derivation/v1", "task_id": PREFIX + task_id, "scoring_allowed": False,
            "parent_package": str(parent), "parent_package_digest": records[task_id].variant_digest,
            "package": str(package), "package_digest": record.variant_digest, "harbor_digest": record.variant_harbor_digest,
            "record": str(record.record_relpath()),
            "record_sha256": driver["fingerprint"](repo / record.record_relpath())["sha256"],
            "transform": "setup-python-path@1", "changed_files": sorted(changes),
            "tests_byte_identical": True, "tests_digest": compute_task_digests(package).verifier,
            "controls": {"status": "pending", "evidence_path": str(tmp_path / "setup-001948-v7-controls.json")},
            "unchanged_files": {
                str(path.relative_to(parent)): sha(path.read_bytes())
                for path in parent.rglob("*") if path.is_file() and str(path.relative_to(parent)) not in changes
            },
        }))
        return handoff

    return {
        "frozen": frozen, "repo": repo, "membership": membership, "split": split, "manifest": manifest,
        "registration": registration, "controls": controls, "records": records, "packages": package_paths,
        "refresh": refresh, "setup": setup, "repairs": repairs, "inventory": inventory,
    }


def test_successor_retains_frozen33_packages_lineage_and_exact_v6_controls(driver, successor_case, tmp_path: Path) -> None:
    case = successor_case
    before = {path.name: path.read_bytes() for path in case["frozen"].iterdir()}
    destination = tmp_path / "successor" / "heldout-v7.json"
    payload = driver["assemble_successor"](
        case["frozen"], destination, repairs_handoff=case["repairs"], repo_root=case["repo"],
    )
    assert payload["schema"] == "mimo.heldout/v1"
    assert payload["heldout"] == case["membership"]
    assert len(payload["heldout"]) == len(set(payload["heldout"])) == 33
    assert payload["training_exposure"] == case["split"]["training_exposure"]
    assert not set(payload["heldout"]) & set(payload["training_exposure"])
    assert payload["scoring_allowed"] is False and payload["freeze_ready"] is False
    assert payload["independent_verifier"] == {"status": "pending", "evidence": None}
    for row, frozen_row, pair in zip(payload["tasks"], case["manifest"]["tasks"], case["controls"]["selected_pairs"], strict=True):
        assert row["task_id"] == frozen_row["task_id"]
        assert row["source_controls"] == frozen_row["source_controls"]
        assert row["evaluation_target"]["package_digest"] == frozen_row["v6_package_digest"]
        assert row["evaluation_target"]["package_path"] == frozen_row["v6_package_path"]
        assert row["successor_selection"] == "unchanged-v6"
        evidence = row["native_controls"]
        assert evidence["retained_frozen_v6"]["selected_pair"] == pair
        assert [item["observation"] for item in evidence["retained_frozen_v6"]["observations"]] == [
            case["controls"]["observations"][pair[f"{agent}_observation"]] for agent in ("oracle", "nop")
        ]
        selected = evidence["selected_package"]
        assert selected["measured_reference_rewards"] == [1.0] and selected["measured_nop_rewards"] == [0.0]
        assert "native_reference_1_1_1" in selected["missing"] and "native_nop_0_0_0" in selected["missing"]
        lineage = row["evaluation_target"]["lineage"]
        assert lineage["full_transform_chain_newest_first"] == ["agent-network-none@1", "leak-close-pypi@1"]
        assert len(lineage["records_newest_first"]) == 2
        for record in lineage["records_newest_first"]:
            assert record["record"] == driver["fingerprint"](case["repo"] / record["record_path"])
    assert payload["semantic_repairs"]["status"] == "pending"
    assert payload["semantic_repairs"]["source"] == driver["fingerprint"](case["repairs"])
    assert payload["counts"]["selected_semantic_repairs"] == 0
    assert destination.read_text() == driver["encoded"](payload)
    driver["validate_successor"](payload, case["frozen"], repairs_handoff=case["repairs"], repo_root=case["repo"])
    with pytest.raises(FileExistsError):
        driver["assemble_successor"](case["frozen"], destination, repairs_handoff=case["repairs"], repo_root=case["repo"])
    with pytest.raises(ValueError, match="outside the frozen"):
        driver["assemble_successor"](case["frozen"], case["frozen"] / "heldout-v7.json", repo_root=case["repo"])
    assert {path.name: path.read_bytes() for path in case["frozen"].iterdir()} == before


@pytest.mark.parametrize("change", ["duplicate", "substitute", "drop", "reorder", "rich_row", "open_gate", "numeric_gate"])
def test_successor_validator_refuses_membership_target_or_scoring_mutation(driver, successor_case, change) -> None:
    case = successor_case
    payload = driver["successor_payload"](case["frozen"], repo_root=case["repo"])
    if change == "duplicate":
        payload["heldout"][0] = payload["heldout"][1]
    elif change == "substitute":
        payload["heldout"][0] = "format-code-task-009998"
    elif change == "drop":
        payload["heldout"].pop()
    elif change == "reorder":
        payload["heldout"][0], payload["heldout"][1] = payload["heldout"][1], payload["heldout"][0]
    elif change == "rich_row":
        payload["tasks"][0]["evaluation_target"]["package_digest"] = "sha256:" + "0" * 64
    else:
        payload["scoring_allowed"] = True if change == "open_gate" else 0
    with pytest.raises(ValueError, match="successor manifest differs"):
        driver["validate_successor"](payload, case["frozen"], repo_root=case["repo"])


@pytest.mark.parametrize("change", ["substitute", "reorder"])
def test_successor_refuses_a_consistently_rebound_but_different_source_cohort(driver, successor_case, change) -> None:
    case = successor_case
    if change == "substitute":
        case["split"]["heldout"][0] = "format-code-task-009998"
    else:
        case["split"]["heldout"][:2] = list(reversed(case["split"]["heldout"][:2]))
    case["refresh"]()
    with pytest.raises(ValueError, match="exact frozen33"):
        driver["successor_payload"](case["frozen"], repo_root=case["repo"])


def test_successor_refuses_any_historical_training_exposure_overlap(driver, successor_case) -> None:
    case = successor_case
    case["split"]["training_exposure"].append(case["membership"][0])
    case["refresh"]()
    with pytest.raises(ValueError, match="training leakage"):
        driver["successor_payload"](case["frozen"], repo_root=case["repo"])


@pytest.mark.parametrize("change", ["package_bytes", "record_hash", "lineage_harbor", "frozen_parent", "native_digest"])
def test_successor_refuses_package_lineage_and_control_identity_mismatch(driver, successor_case, change) -> None:
    case = successor_case
    task_id = case["membership"][0]
    record = case["records"][task_id]
    path = case["repo"] / record.record_relpath()
    if change == "package_bytes":
        (case["packages"][task_id] / "tests/test.sh").write_text("assertions removed\n")
    elif change in {"record_hash", "lineage_harbor"}:
        payload = json.loads(path.read_text())
        if change == "record_hash":
            payload["rationale"] += " altered after custody"
        else:
            payload["parent"]["harbor_digest"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(payload))
        if change == "lineage_harbor":
            case["registration"]["endpoints"][0]["record_sha256"] = driver["fingerprint"](path)["sha256"]
            case["refresh"]()
    elif change == "frozen_parent":
        case["split"]["heldout_task_metadata"][0]["v6_package_digest"] = "sha256:" + "0" * 64
        case["refresh"]()
    else:
        case["controls"]["observations"][0]["source_package_digest"] = "sha256:" + "0" * 64
        case["refresh"]()
    with pytest.raises(ValueError, match="digest mismatch|identity mismatch"):
        driver["successor_payload"](case["frozen"], repo_root=case["repo"])


def test_successor_setup_child_is_not_a_test_repair_or_an_inheritor_of_parent_controls(driver, successor_case) -> None:
    case = successor_case
    handoff = case["setup"]()
    payload = driver["successor_payload"](
        case["frozen"], setup_handoff=handoff, repairs_handoff=case["repairs"], repo_root=case["repo"],
    )
    row = next(row for row in payload["tasks"] if row["task_id"] == "format-code-task-001948")
    selected, parent = row["evaluation_target"], row["frozen_parent"]
    assert row["successor_selection"] == "setup-only" and selected["version_column"] == "setup_only"
    assert selected["test_repair_claim"] is False and selected["setup_only"] is True
    assert selected["package_digest"] != parent["package_digest"] == row["v6_package_digest"]
    assert selected["harbor_digest"] != parent["harbor_digest"]
    assert selected["verifier_digest"] == parent["verifier_digest"]
    assert row["test_repaired_package_digest"] == ""
    assert selected["lineage"]["records_newest_first"][1:] == parent["lineage"]["records_newest_first"]
    evidence = row["native_controls"]
    assert evidence["retained_frozen_v6"]["status"] == "retained"
    assert evidence["selected_package"]["observation_indexes"] == []
    assert evidence["selected_package"]["measured_reference_rewards"] == []
    assert evidence["selected_package"]["measured_nop_rewards"] == []
    assert evidence["reported_setup_controls"]["status"] == "pending"
    assert "semantic_repairs" in payload["pending_gates"] and "independent_verifier_pass" in payload["pending_gates"]
    assert payload["scoring_allowed"] is False


@pytest.mark.parametrize("change", ["wrong_parent", "false_verifier_digest", "changed_tests"])
def test_successor_setup_requires_exact_frozen_parent_and_byte_unchanged_tests(driver, successor_case, change) -> None:
    case = successor_case
    path = case["setup"]({"tests/test.sh": b"#!/bin/sh\nexit 0\n"} if change == "changed_tests" else None)
    handoff = json.loads(path.read_text())
    if change == "wrong_parent":
        handoff["parent_package_digest"] = "sha256:" + "0" * 64
    elif change == "false_verifier_digest":
        handoff["tests_digest"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(handoff))
    with pytest.raises(ValueError, match="exact frozen|setup-only"):
        driver["successor_payload"](case["frozen"], setup_handoff=path, repo_root=case["repo"])


@pytest.mark.parametrize("reward", [None, 0.0])
def test_successor_preserves_setup_error_and_null_as_unmeasured_not_nop_rejection(driver, successor_case, reward) -> None:
    case = successor_case
    nop = case["controls"]["observations"][1]
    nop["native_exit_code"] = 1
    nop["receipt"].update(reward=reward, error="setup failed before verifier")
    Path(nop["native_metadata"]["path"]).unlink()
    case["refresh"]()
    payload = driver["successor_payload"](case["frozen"], repo_root=case["repo"])
    evidence = payload["tasks"][0]["native_controls"]
    retained = evidence["retained_frozen_v6"]["observations"][1]
    assert retained["observation"]["receipt"]["reward"] == reward
    assert retained["measured"] is False
    assert retained["raw_metadata_custody"]["status"] == "raw_metadata_unavailable"
    assert evidence["selected_package"]["measured_nop_rewards"] == []
    assert "native_nop_0_0_0" in evidence["selected_package"]["missing"]
    assert "adjudicated_wrong_inventory_and_exact_digest_zero_outcomes" in evidence["selected_package"]["missing"]
    assert "admitted_correct_inventory_and_exact_digest_accepted_outcomes" in evidence["selected_package"]["missing"]
    assert payload["scoring_allowed"] is False


@pytest.mark.parametrize("change", ["source_drift", "unbound_repair"])
def test_successor_does_not_guess_repairs_or_ignore_changed_adjudication_custody(driver, successor_case, change) -> None:
    case = successor_case
    if change == "source_drift":
        case["inventory"].write_text(json.dumps({"adjudications": [{"verdict": "wrong"}]}))
    else:
        handoff = json.loads(case["repairs"].read_text())
        handoff.update(repairs=[{"task_id": case["membership"][0]}], complete_count=1)
        case["repairs"].write_text(json.dumps(handoff))
    with pytest.raises(ValueError, match="source hash/digest mismatch|unrecognized nonempty EnvCheck"):
        driver["successor_payload"](case["frozen"], repairs_handoff=case["repairs"], repo_root=case["repo"])


def test_successor_does_not_promote_a_partial_envcheck_repair(driver, successor_case) -> None:
    case = successor_case
    handoff = json.loads(case["repairs"].read_text())
    handoff["repairs"] = [{
        "task_id": case["membership"][0], "status": "partial", "qualified_for_v7": False,
    }]
    case["repairs"].write_text(json.dumps(handoff))
    payload = driver["successor_payload"](case["frozen"], repairs_handoff=case["repairs"], repo_root=case["repo"])
    assert payload["tasks"][0]["evaluation_target"]["package_digest"] == case["records"][case["membership"][0]].variant_digest
    assert payload["counts"]["selected_semantic_repairs"] == 0
    assert payload["scoring_allowed"] is False


@pytest.mark.parametrize("change", [
    None, "remove_assertions", "skip_original", "missing_wrong", "false_negative",
    "no_case_evidence", "repeat_alias", "unbound_submission",
    "historical_wrapper", "historical_original_status", "historical_addition_status",
])
def test_successor_binds_nonempty_repairs_to_preserved_tests_and_all_native_submission_controls(
    driver, successor_case, tmp_path: Path, change,
) -> None:
    from evallab.registry import compute_task_digests
    from evallab.runner import load_matrix, staged_matrix_request
    from evallab.task_variants import derive_task
    from evallab.verifier_repair import build_command_wrapper

    case = successor_case
    repo = case["repo"]
    (repo / "pyproject.toml").write_text("[project]\nname = 'fixture'\nversion = '0.0.0'\n")
    task_id = case["membership"][0]
    parent, parent_record = case["packages"][task_id], case["records"][task_id]
    command = "python -m pytest test_extra.py"
    extra = (
        b"diff --git a/test_extra.py b/test_extra.py\nnew file mode 100644\n"
        b"--- /dev/null\n+++ b/test_extra.py\n@@ -0,0 +1,2 @@\n"
        b"+def test_extra():\n+    assert 3 + 3 == 6\n"
    )
    original = (parent / "tests/test.patch").read_bytes()
    wrapper = build_command_wrapper(
        (parent / "tests/test_command.sh").read_text(), command, workdir="/testbed",
    ).encode()
    if change in {"historical_wrapper", "historical_original_status", "historical_addition_status"}:
        base = (parent / "tests/test_command.sh").read_text()
        base = base if base.endswith("\n") else base + "\n"
        wrapper = (
            f"(\n{base})\n"
            "REPAIR_RC=$?\n"
            "cd /testbed || exit 1\n"
            f"{command}\n"
            "REPAIR_NEW_RC=$?\n"
            'if [ "$REPAIR_NEW_RC" -ne 0 ]; then exit 1; fi\n'
            'exit "$REPAIR_RC"\n'
        ).encode()
        if change == "historical_original_status":
            wrapper = wrapper.replace(b"REPAIR_RC=$?", b"REPAIR_RC=0")
        elif change == "historical_addition_status":
            wrapper = wrapper.replace(b"REPAIR_NEW_RC=$?", b"REPAIR_NEW_RC=0")
    record = derive_task(
        parent, transform="verifier-repair@1", rationale="scratch append-only repair",
        created_by="pytest", parent_source={"kind": "variant", "record": str(parent_record.record_relpath())},
        changes={
            "tests/test.patch": extra if change == "remove_assertions" else original + extra,
            "tests/test_command.sh": b"#!/bin/sh\nexit 0\n" if change == "skip_original" else wrapper,
        },
        inputs={"command": command}, repo_root=repo, variants_root=parent.parent.parent,
    )
    package = parent.parent.parent / record.task_slug / record.digest12
    controls_dir = repo / "controls"
    controls_dir.mkdir()
    wrong_patch, wrong_script, alternate_script = (controls_dir / name for name in (
        "wrong.patch", "wrong.sh", "alternate.sh",
    ))
    wrong_patch.write_text("diff --git a/code.py b/code.py\n--- a/code.py\n+++ b/code.py\n@@ -1 +1 @@\n-1\n+0\n")
    wrong_script.write_text("#!/bin/sh\nprintf 'wrong submission\\n'\n")
    alternate_script.write_text("#!/bin/sh\nprintf 'adjudicated-correct alternate\\n'\n")
    wrong = {
        "run": "wrong", "finding_id": "wrong-finding", "patch": str(wrong_patch),
        "patch_sha256": sha(wrong_patch.read_bytes()), "script": str(wrong_script),
        "script_sha256": sha(wrong_script.read_bytes()),
    }
    alternate = {"run": "alternate", "finding_id": "correct-finding", "script": str(alternate_script)}
    admitted = repo / "admitted.json"
    admitted.write_text(json.dumps({"rows": [{
        "task_id": task_id, "finding_id": wrong["finding_id"], "reproducer_sha256": wrong["patch_sha256"],
    }]}))
    identity = {
        "task_id": task_id, "package_path": str(package), "package_digest": record.variant_digest,
        "harbor_digest": record.variant_harbor_digest, "verifier_digest": compute_task_digests(package).verifier,
        "native_record": str(repo / record.record_relpath()),
        "native_record_sha256": sha((repo / record.record_relpath()).read_bytes()),
        "parent_digest": parent_record.variant_digest, "parent_package": str(parent),
    }
    matrix_path = repo / "matrix.json"
    matrix_path.write_text(json.dumps({
        "schema_version": 2, "matrix_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "name": "v7-native-custody",
        "hypothesis": "append-only controls", "benchmark_family": "mimo-clean-v7", "task_id": task_id,
        "task": str(package), "task_package_digest": record.variant_digest,
        "verifier_digest": identity["verifier_digest"], "jobs_dir": "runs",
        "environment": "modal", "concurrency": 1, "timeout_seconds": 600,
        "runs": [
            {"name": "reference", "agent": "oracle", "attempts": 1, "expect_reward": 1.0},
            {"name": "nop", "agent": "nop", "attempts": 1, "expect_reward": 0.0},
            {"name": "wrong", "agent": "oracle", "attempts": 1, "expect_reward": 0.0, "solution": "controls/wrong.sh"},
            {"name": "alternate", "agent": "oracle", "attempts": 1, "expect_reward": 1.0, "solution": "controls/alternate.sh"},
        ],
    }))
    matrix_sha = sha(matrix_path.read_bytes())
    matrix = load_matrix(matrix_path)
    checks = []
    for index, run in enumerate(matrix.runs):
        job = tmp_path / "repair-native" / run.name
        repeated = run.name != "wrong"
        actual_reward = 0.0 if change == "false_negative" and run.name == "alternate" else run.expect_reward
        with staged_matrix_request(matrix, run, repo_root=repo) as (request, provenance):
            native_control_fixture(
                job, request.task, task_id, run.agent, actual_reward, repeats=3 if repeated else 1,
                case_evidence=not (change == "no_case_evidence" and run.name == "wrong"),
            )
        if change == "repeat_alias" and run.name == "reference":
            stability_path = job / "trial/verifier/stability.json"
            stability = json.loads(stability_path.read_text())
            for item in stability["runs"]:
                item["index"] = 0
            stability_path.write_text(json.dumps(stability))
        checks.append({
            "run": run.name, "role": ("reference", "nop", "known-wrong", "prompt-valid-alternate")[index],
            "expected_reward": run.expect_reward, "matrix_sha256": matrix_sha, "status": "ok",
            "repeats": [run.expect_reward] * 3 if repeated else None, "job_dir": str(job),
            **provenance,
        })
    if change == "unbound_submission":
        checks[2]["solution_sha256"] = "0" * 64
    qualification_path = repo / "qualification.json"
    qualification_path.write_text(json.dumps(identity | {
        "qualified": True, "matrix_sha256": matrix_sha, "checks": checks,
    }))
    candidate_path = repo / "candidate.json"
    candidate_path.write_text(json.dumps(identity | {
        "schema": "envcheck.v7.candidate/v1", "matrix": str(matrix_path), "matrix_sha256": matrix_sha,
        "source_manifest": str(admitted), "source_sha256": sha(admitted.read_bytes()),
        "known_wrong_reproducers": [] if change == "missing_wrong" else [wrong],
        "adjudicated_correct_controls": [alternate],
    }))
    row = identity | {
        "status": "complete", "qualified_for_v7": True, "reference": [1, 1, 1], "do_nothing": [0, 0, 0],
        "task_control_bindings_unchanged": True, "no_original_assertion_weakened": True,
        "training_allowed": False, "unrepairable": [], "reproducers": [wrong],
        "recorded_reproducer_count": 1, "reproducers_killed": 1, "adjudicated_correct_controls": [alternate],
        "candidate": str(candidate_path.relative_to(repo)), "candidate_sha256": sha(candidate_path.read_bytes()),
        "qualification": str(qualification_path), "qualification_sha256": sha(qualification_path.read_bytes()),
    }
    handoff = json.loads(case["repairs"].read_text())
    handoff.update(repairs=[row], complete_count=1)
    case["repairs"].write_text(json.dumps(handoff))
    if change not in {None, "historical_wrapper"}:
        with pytest.raises(ValueError):
            driver["successor_payload"](case["frozen"], repairs_handoff=case["repairs"], repo_root=repo)
        return
    payload = driver["successor_payload"](case["frozen"], repairs_handoff=case["repairs"], repo_root=repo)
    selected = payload["tasks"][0]["evaluation_target"]
    assert selected["package_digest"] == record.variant_digest != parent_record.variant_digest
    assert selected["lineage"]["full_transform_chain_newest_first"] == [
        "verifier-repair@1", "agent-network-none@1", "leak-close-pypi@1",
    ]
    controls = payload["tasks"][0]["native_controls"]["selected_package"]
    assert controls["measured_reference_rewards"] == [1, 1, 1]
    assert controls["measured_nop_rewards"] == [0, 0, 0]
    assert "native_reference_1_1_1" not in controls["missing"]
    assert "native_nop_0_0_0" not in controls["missing"]
    assert payload["counts"]["selected_semantic_repairs"] == 1
    assert payload["independent_verifier"]["status"] == "pending" and payload["scoring_allowed"] is False
    driver["validate_successor"](payload, case["frozen"], repairs_handoff=case["repairs"], repo_root=repo)


@pytest.mark.parametrize("native_change", [
    None, "setup_error", "missing_case_evidence", "relabelled_execution", "false_reward", "wrong_config",
])
def test_setup_native_triad_is_separate_from_wrong_correct_and_independent_verifier_gates(
    driver, successor_case, tmp_path: Path, native_change,
):
    case = successor_case
    handoff_path = case["setup"]()
    handoff = json.loads(handoff_path.read_text())
    observations = []
    for agent, reward in (("oracle", 1.0), ("nop", 0.0)):
        for index in range(3):
            execution_id = f"{agent}-{index}"
            observations.append(native_control_fixture(
                tmp_path / "setup-native" / execution_id, Path(handoff["package"]),
                "format-code-task-001948", agent, reward,
                failed=native_change == "setup_error" and agent == "nop" and index == 2,
                case_evidence=not (native_change == "missing_case_evidence" and agent == "nop" and index == 2),
            ))
    if native_change == "relabelled_execution":
        observations[2] = copy.deepcopy(observations[0])
        observations[2]["execution_id"] = "oracle-2"
    elif native_change == "false_reward":
        observations[-1]["rewards"] = [1.0]
    elif native_change == "wrong_config":
        for key in ("config_path", "config_sha256"):
            observations[-1]["task_binding"][key] = observations[0]["task_binding"][key]
    evidence_path = Path(handoff["controls"]["evidence_path"])
    evidence_path.write_text(json.dumps({
        "schema": "mimo-v7-native-controls/v1", "task_id": handoff["task_id"],
        "package_digest": handoff["package_digest"], "harbor_digest": handoff["harbor_digest"],
        "scoring_allowed": False, "controls_complete": True,
        # Deliberately optimistic aggregates must not hide the failed cell.
        "oracle_rewards": [1, 1, 1], "nop_rewards": [0, 0, 0], "observations": observations,
        "wrong_and_valid_alternate_gate": "pending_comprehensive_QA_inventory",
    }))
    handoff["controls"]["evidence_sha256"] = sha(evidence_path.read_bytes())
    handoff_path.write_text(json.dumps(handoff))
    if native_change in {"relabelled_execution", "false_reward", "wrong_config"}:
        with pytest.raises(ValueError, match="actual physical execution identity or score"):
            driver["successor_payload"](case["frozen"], setup_handoff=handoff_path, repo_root=case["repo"])
        return
    payload = driver["successor_payload"](case["frozen"], setup_handoff=handoff_path, repo_root=case["repo"])
    row = next(row for row in payload["tasks"] if row["task_id"] == "format-code-task-001948")
    evidence = row["native_controls"]
    selected = evidence["selected_package"]
    assert evidence["reported_setup_controls"]["source"] == driver["fingerprint"](evidence_path)
    assert evidence["reported_setup_controls"]["handoff_hash_bound"] is True
    assert selected["measured_reference_rewards"] == [1, 1, 1]
    incomplete = native_change in {"setup_error", "missing_case_evidence"}
    assert selected["measured_nop_rewards"] == ([0, 0] if incomplete else [0, 0, 0])
    assert ("native_nop_0_0_0" in selected["missing"]) is incomplete
    assert selected["status"] == "pending"
    assert "adjudicated_wrong_inventory_and_exact_digest_zero_outcomes" in selected["missing"]
    assert "admitted_correct_inventory_and_exact_digest_accepted_outcomes" in selected["missing"]
    assert "independent_verifier_pass" in selected["missing"]
    assert payload["scoring_allowed"] is False and payload["independent_verifier"]["status"] == "pending"
    if native_change is None:
        case_log = Path(observations[0]["result_path"]).parent / "verifier/test_output.log"
        case_log.write_text("2 passed in 0.01s\n")
        with pytest.raises(ValueError, match="successor manifest differs"):
            driver["validate_successor"](
                payload, case["frozen"], setup_handoff=handoff_path, repo_root=case["repo"],
            )


@pytest.mark.parametrize("fault", [
    None, "single_run", "missing_cases", "oracle_exit", "unfinished_agent", "failed_process",
    "conflicting_primary_reward", "repeat_index", "partial_pair", "duplicate_uuid", "duplicate_role",
    "unselected_package", "outside_cohort", "repaired_selection",
])
def test_successor_fresh_baseline_pairs_require_native_repeat3_and_selected_frozen_identity(
    driver, successor_case, tmp_path: Path, fault,
) -> None:
    case = successor_case
    task_id = "format-code-task-001948" if fault == "repaired_selection" else case["membership"][0]
    package = case["packages"][task_id]
    if fault == "outside_cohort":
        task_id = "format-code-task-999999"
    elif fault == "unselected_package":
        package = Path(shutil.copytree(package, tmp_path / "different-submission"))
        (package / "solution/solve.sh").write_text("printf 'not the frozen reference submission\\n'\n")
    observations = [
        native_control_fixture(
            tmp_path / "baseline-native" / agent, package, task_id, agent, reward,
            repeats=1 if fault == "single_run" and agent == "oracle" else 3,
            failed=fault == "failed_process" and agent == "oracle",
            case_evidence=not (fault == "missing_cases" and agent == "oracle"),
        )
        for agent, reward in (("oracle", 1.0), ("nop", 0.0))
    ]
    oracle = Path(observations[0]["result_path"])
    if fault == "oracle_exit":
        (oracle.parent / "agent/exit-code.txt").write_text("1\n")
    elif fault in {"unfinished_agent", "conflicting_primary_reward"}:
        result = json.loads(oracle.read_text())
        if fault == "unfinished_agent":
            result["agent_execution"]["finished_at"] = None
        else:
            result["verifier_result"]["rewards"]["reward"] = 0.0
        oracle.write_text(json.dumps(result))
    elif fault == "repeat_index":
        path = oracle.parent / "verifier/stability.json"
        stability = json.loads(path.read_text())
        stability["runs"][2]["index"] = 1
        path.write_text(json.dumps(stability))
    elif fault == "partial_pair":
        observations.pop()
    elif fault == "duplicate_uuid":
        path = Path(observations[1]["result_path"])
        result = json.loads(path.read_text())
        result["id"] = observations[0]["execution_id"]
        path.write_text(json.dumps(result))
    elif fault == "duplicate_role":
        observations.append(native_control_fixture(
            tmp_path / "baseline-native/extra-oracle", package, task_id, "oracle", 1.0, repeats=3,
        ))
    metadata = tuple(Path(observation["metadata_path"]) for observation in observations)
    options = {"repo_root": case["repo"], "native_control_metadata": metadata}
    if fault == "repaired_selection":
        options["setup_handoff"] = case["setup"]()
    if fault is not None:
        with pytest.raises(ValueError):
            driver["successor_payload"](case["frozen"], **options)
        return
    original = driver["successor_payload"](case["frozen"], repo_root=case["repo"])
    before = {path.name: path.read_bytes() for path in case["frozen"].iterdir()}
    payload = driver["successor_payload"](case["frozen"], **options)
    row = next(row for row in payload["tasks"] if row["task_id"] == task_id)
    previous = next(row for row in original["tasks"] if row["task_id"] == task_id)
    evidence, selected = row["native_controls"], row["native_controls"]["selected_package"]
    assert row["evaluation_target"] == previous["evaluation_target"]
    assert evidence["retained_frozen_v6"] == previous["native_controls"]["retained_frozen_v6"]
    assert selected["observation_indexes"] == []
    assert selected["measurement_source"] == "reported_baseline_controls"
    assert selected["measured_reference_rewards"] == [1.0, 1.0, 1.0]
    assert selected["measured_nop_rewards"] == [0.0, 0.0, 0.0]
    assert selected["missing"] == [
        "adjudicated_wrong_inventory_and_exact_digest_zero_outcomes",
        "admitted_correct_inventory_and_exact_digest_accepted_outcomes",
        "independent_verifier_pass",
    ]
    assert payload["heldout"] == original["heldout"] and payload["scoring_allowed"] is False
    assert payload["freeze_ready"] is False and payload["independent_verifier"]["status"] == "pending"
    assert {path.name: path.read_bytes() for path in case["frozen"].iterdir()} == before
    driver["validate_successor"](payload, case["frozen"], **options)
    case_log = oracle.parent / "verifier/repeat/0/test_output.log"
    case_log.write_text("2 passed in 0.01s\n")
    with pytest.raises(ValueError, match="successor manifest differs"):
        driver["validate_successor"](payload, case["frozen"], **options)
