"""Providerless admission distinguishes retained controls from ledger validation."""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from evallab.task_variants import derive_task, lineage_chain

MODULE_PATH = Path(__file__).resolve().parents[1] / "research/experiments/repaired-data-sft-v2/control_admission.py"
spec = importlib.util.spec_from_file_location("repaired_sft_v2_control_admission", MODULE_PATH)
assert spec is not None and spec.loader is not None
admission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admission)


def _scope_inputs():
    heldout = [f"format-code-task-{index:06d}" for index in range(900000, 900033)]
    request = {
        "candidates": [{"task": admission.NAMESPACE + task} for task in admission.SELECTED_TASK_IDS],
        "heldout_exclusion": {"all_parent_and_descendant_ids": ["mimo-v2.6-rl/" + task for task in heldout]},
        "selection": {"explicit_training_exclusions": [{"task": admission.NAMESPACE + "001478"}]},
    }
    handoff = {"selected_task_ids": list(admission.SELECTED_TASK_IDS),
               "tasks": [{"task_id": task} for task in admission.SELECTED_TASK_IDS]}
    cohort = {"task_ids": heldout[:26], "training_exclusion": {"parent_task_ids": heldout}}
    return request, handoff, cohort, {"heldout": heldout}


def test_scope_uses_all33_exclusions_not_just26_evaluation_ids():
    scope = admission.validate_scope(*_scope_inputs())
    assert scope["strict_task_ids"] == list(admission.STRICT_TASK_IDS)
    assert len(scope["frozen33_task_ids"]) == 33
    assert "002486" not in scope["strict_task_ids"]
    assert scope["excluded_source_task_ids"] == ["002486"]
    assert scope["fresh_attempts_requested"] == 76
    assert scope["fresh_attempts_executed"] == 0


@pytest.mark.parametrize("change", ["drop_candidate", "duplicate_candidate", "missing_1478", "partial_heldout"])
def test_scope_refuses_narrowing_or_omitting_exclusions(change):
    request, handoff, cohort, frozen = _scope_inputs()
    if change == "drop_candidate":
        request["candidates"].pop()
    elif change == "duplicate_candidate":
        request["candidates"][-1] = copy.deepcopy(request["candidates"][0])
    elif change == "missing_1478":
        request["selection"]["explicit_training_exclusions"] = []
    else:
        cohort["training_exclusion"]["parent_task_ids"] = cohort["task_ids"]
    with pytest.raises(ValueError):
        admission.validate_scope(request, handoff, cohort, frozen)


def test_original_task_id_excludes_every_descendant():
    request, handoff, cohort, frozen = _scope_inputs()
    blocked = "format-code-task-000089"
    for values in (cohort["training_exclusion"]["parent_task_ids"], frozen["heldout"]):
        values[-1] = blocked
    request["heldout_exclusion"]["all_parent_and_descendant_ids"][-1] = "mimo-v2.6-rl/" + blocked
    with pytest.raises(ValueError, match="overlap"):
        admission.validate_scope(request, handoff, cohort, frozen)


def test_actual_inventory_hashes_all_bytes_and_reports_failures(tmp_path):
    good, bad, absent = (tmp_path / name for name in ("good", "bad", "absent"))
    good.write_bytes(b"retained\n")
    bad.write_bytes(b"changed\n")
    binding = admission.file_binding(good)
    rows = [binding, {**admission.file_binding(bad), "sha256": "sha256:" + "0" * 64},
            {"path": str(absent), "sha256": "sha256:" + "1" * 64, "bytes": 10}]
    actual, failures = admission.audit_inventory(rows)
    assert len(actual) == 3
    assert [row["path"] for row in failures] == sorted([str(absent), str(bad)])
    assert next(row for row in actual if row["path"] == str(good))["qualified"]
    assert all(not row["qualified"] for row in failures)


def test_manifest_binding_must_match_actual_bytes_even_if_row_matches(tmp_path):
    path = tmp_path / "native-result.json"
    path.write_bytes(b"{}\n")
    row = admission.file_binding(path)
    row["retained_bindings"] = [{"scope": "retained manifest", "expected_sha256": "sha256:" + "0" * 64,
                                  "expected_bytes": row["bytes"]}]
    _, failures = admission.audit_inventory([row])
    assert failures[0]["failures"] == ["retained manifest binding mismatch"]


def test_handoff_hash_mismatch_refuses_before_reading_any_json(tmp_path):
    path = tmp_path / "handoff.json"
    path.write_text("not trusted JSON")
    with pytest.raises(ValueError, match="source SHA256 mismatch"):
        admission.require_binding(path, admission.HANDOFF_SHA256)


def test_retained_recipe_is_data_not_executable_code(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    recipe = (f"raise RuntimeError('never execute')\n"
              f"open({str(sentinel)!r}, 'w').write('never execute')\n"
              "solve.write_text('#!/bin/bash\\n' f'echo {payload}\\n')\n").encode()
    patch = b"retained diff\n"
    assert admission.literal_override_script(recipe, patch) == b"#!/bin/bash\necho " + base64.b64encode(patch) + b"\n"
    assert not sentinel.exists()


def test_retained_recipe_refuses_calls_in_script_expression():
    with pytest.raises(ValueError, match="literal/payload-only"):
        admission.literal_override_script(b"solve.write_text(read_secret())", b"diff")


def test_scoped_alternatives_and_one_observation_wrong_are_not_mislabeled():
    assert admission.expected_vector("001269", "scope-limited-alternate", {}) == [1.0, 1.0, 1.0]
    assert admission.expected_vector("001269", "wrong-lenient", {}) == [0.0]
    raw = {"expected_vector": [1.0, 1.0, 1.0], "semantic_label": "QA_adjudicated_correct_alternative"}
    assert admission.expected_vector("002391", "002391-sc2", raw) == [1.0, 1.0, 1.0]
    assert admission.expected_vector("002486", "HAR161-unadjudicated", {}) == [1.0, 1.0, 1.0]


def test_open_nop_and_unreached_real_ledger_cannot_validate_candidate():
    blockers = admission.canonical_blockers("candidate", [{"effective": "open", "mode": "open"}],
                                            "sha256:real-ledger", ["sha256:frozen-v6"])
    assert len(blockers) == 3
    assert any("locked-nop" in reason for reason in blockers)
    assert any("real task-ledger" in reason for reason in blockers)
    missing = admission.canonical_blockers("candidate", [], None, [])
    assert any("no real task-ledger" in reason for reason in missing)


def test_byte_copy_preserves_candidate_history_and_local_lineage(tmp_path):
    source_root, destination = tmp_path / "source-repo", tmp_path / "canonical-repo"
    parent = tmp_path / "parent"
    (parent / "environment").mkdir(parents=True)
    (parent / "tests").mkdir()
    (parent / "task.toml").write_text('schema_version = "1.4"\n[task]\nname = "mimo-v2.6-rl/format-code-task-000089"\n')
    (parent / "instruction.md").write_text("Repair the named behavior.\n")
    (parent / "environment/Dockerfile").write_text("FROM scratch\n")
    (parent / "tests/test.sh").write_text("#!/bin/bash\nexit 0\n")
    record = derive_task(parent, changes={"tests/test.sh": b"#!/bin/bash\nexit 1\n"},
                         transform="verifier-repair@1", rationale="retained test repair", created_by="test",
                         repo_root=source_root, variants_root=tmp_path / "source-store")
    path = source_root / record.record_relpath()
    original = path.read_bytes()
    copied = admission.copy_candidate(record, path, destination)
    canonical = Path(copied["path"])
    assert canonical.read_bytes() == original == path.read_bytes()
    assert copied["sha256"] == "sha256:" + hashlib.sha256(original).hexdigest()
    assert not copied["status_evidence_appended"]
    payload = json.loads(canonical.read_bytes())
    assert payload["status"] == "candidate"
    assert payload["evidence"] == []
    chain = lineage_chain(canonical, repo_root=destination)
    assert len(chain) == 1
    assert chain[0].record.parent.source.kind == "local"
    assert chain[0].record.parent.digest == record.parent.digest
    # Copying to canonical storage must not invent a variant link to a ledger root.
    blockers = admission.canonical_blockers(payload["status"], [{"effective": "open", "mode": "open"}],
                                            "sha256:unreached-ledger", [record.parent.digest])
    assert any("ancestry terminates" in reason for reason in blockers)


def test_native_custody_keeps_original_projection_and_publisher_bytes_distinct(tmp_path):
    job = tmp_path / "published"
    (job / "processed").mkdir(parents=True)
    native = job / "lab-metadata.json"
    native.write_text('{"exit_code":0}\n')
    current_projection = job / "processed/job.json"
    current_projection.write_text('{"publisher":"current"}\n')
    original_projection = tmp_path / "native-supplements/processed/job.json"
    original_projection.parent.mkdir(parents=True)
    original_projection.write_text('{"native":"original"}\n')
    expected = {str(native), str(original_projection)}
    custody = {"route": "automatic-native-process-job-plus-any-byte-verified-supplements",
               "native_files": [{"retained_path": str(original_projection)}]}
    result = admission.native_file_custody(job, expected, custody, expected, set())
    assert result["failures"] == []
    assert result["relocated_source_paths"] == [str(original_projection)]
    assert result["publication_artifacts_not_native_proof"] == [admission.file_binding(current_projection)]
    assert admission.file_binding(original_projection)["sha256"] != result["publication_artifacts_not_native_proof"][0]["sha256"]
    # Added verifier bytes never receive the publisher-metadata exception.
    unbound = job / "verifier/reward.txt"
    unbound.parent.mkdir()
    unbound.write_text("1\n")
    result = admission.native_file_custody(job, expected, custody, expected, set())
    assert result["unexpected_files"] == [str(unbound)]
    assert result["failures"]


def test_native_custody_refuses_missing_source_or_changed_bound_file(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    retained = job / "result.json"
    retained.write_text("{}\n")
    expected = {str(retained)}
    assert admission.native_file_custody(job, expected, None, expected, expected)["failures"]
    retained.unlink()
    assert "missing" in " ".join(admission.native_file_custody(job, expected, None, expected, set())["failures"])
