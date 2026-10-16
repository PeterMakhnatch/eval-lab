from __future__ import annotations

import csv
import hashlib
import json
import runpy
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
