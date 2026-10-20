"""Deterministic provenance, immutable-cache and no-drop label-join contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from evallab import hub_trials as hub

EXPERIMENT = Path(__file__).resolve().parents[1] / "research/experiments/tb4-provenance-20261020"


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, EXPERIMENT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


acquire = _load("tb4_provenance_acquire", "acquire.py")
_previous_acquire = sys.modules.get("acquire")
sys.modules["acquire"] = acquire
try:
    stratify = _load("tb4_provenance_stratify", "stratify.py")
finally:
    if _previous_acquire is None:
        del sys.modules["acquire"]
    else:
        sys.modules["acquire"] = _previous_acquire

TRIAL = "11111111-1111-4111-8111-111111111111"
OTHER_TRIAL = "22222222-2222-4222-8222-222222222222"
ROW = "33333333-3333-4333-8333-333333333333"
JOB = "44444444-4444-4444-8444-444444444444"
ARCHIVE = "55555555-5555-4555-8555-555555555555"
TASK = "terminal-bench/example"
DIGEST = "sha256:" + "a" * 64


def _trial(source: str = hub.PACKAGE, **summary_changes: Any) -> dict:
    summary = {
        "id": TRIAL, "job_id": JOB, "task_name": TASK, "name": "example__abcdef",
        "status": "completed", "reward": 1, "error_type": None, **summary_changes,
    }
    detail = {
        "id": TRIAL, "job_id": JOB, "task_name": TASK, "trial_name": "example__abcdef",
        "status": summary["status"], "lock": {"task": {"source": source, "digest": DIGEST, "type": "local", "path": "/tmp/unavailable/task"}},
        "config": {"task": {"name": TASK}},
    }
    archive = {"status": "inventory_and_native_detail_linked", "archive_result_id": ARCHIVE, "hub_trial_id": TRIAL, "id_equal": False}
    return acquire.trial_record(TRIAL, [ROW], summary, detail, {"sha256": "evidence"}, {TASK: {"digest": DIGEST}}, {"dataset_matches": True, "dataset_refs": [{"name": hub.PACKAGE, "ref": "4"}]}, None, archive)


def _label(**changes: Any) -> dict:
    return {"trial_id": TRIAL, "row_id": ROW, "task": "example", "reward": 1, "side": "pass", "label": "known_exploit", **changes}


def _fingerprint(trial: dict, **changes: Any) -> dict:
    return {
        "key": trial["package_key"], "source": trial["lock_task"]["source"], "digest": DIGEST, "task_name": TASK,
        "binding_status": "bound", "status": "measured", "instruction_equal": True, "tests_equal": True,
        "environment_equal": False, "config_equal": False, "reference_equal": True,
        "comparison": {"metadata_changed_paths": ["tests/test.sh"], "changed_paths": ["environment/Dockerfile"]},
        **changes,
    }


def _fingerprints(*entries: dict) -> dict:
    return {"schema": stratify.FINGERPRINT_SCHEMA, "packages": list(entries)}


def test_key_binds_all_three_exact_lock_components() -> None:
    expected = hashlib.sha256(json.dumps([hub.PACKAGE, DIGEST, TASK], separators=(",", ":")).encode()).hexdigest()
    assert acquire.package_key(hub.PACKAGE, DIGEST, TASK) == expected
    assert acquire.package_key("different/source", DIGEST, TASK) != expected
    assert acquire.package_key(hub.PACKAGE, DIGEST, "terminal-bench/other") != expected


def test_native_cache_reuses_original_and_refuses_corruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    native = acquire.FrozenNative("must-not-run", {}, False)
    calls = []

    def response(args: Any) -> bytes:
        calls.append(tuple(args))
        return b'{"id":"original"}\n'

    monkeypatch.setattr(native, "json", response)
    cache = acquire.cache(tmp_path / "cache", native)
    assert acquire.fetch(cache, "hub", "read")[0] == {"id": "original"}
    assert acquire.fetch(cache, "hub", "read")[0] == {"id": "original"}
    assert len(calls) == 1
    path = next((tmp_path / "cache/metadata").iterdir())
    path.write_bytes(b"corrupt original")
    with pytest.raises(hub.IntegrityError, match="hash mismatch"):
        acquire.fetch(cache, "hub", "read")
    assert path.read_bytes() == b"corrupt original"
    assert len(calls) == 1


def test_native_cache_never_adopts_untracked_bytes(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    original = root / "unknown.json"
    original.write_bytes(b"preserve me")
    with pytest.raises(hub.IntegrityError, match="Untracked"):
        acquire.cache(root, acquire.FrozenNative("must-not-run", {}, False))
    assert original.read_bytes() == b"preserve me"


def test_existing_native_pagination_rejects_duplicate_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    native = acquire.FrozenNative("must-not-run", {}, False)

    def response(args: Any) -> bytes:
        page = int(args[-1])
        return json.dumps({"page": page, "page_size": 1, "total": 2, "total_pages": 2, "items": [{"id": TRIAL}]}).encode()

    monkeypatch.setattr(native, "json", response)
    cache = acquire.cache(tmp_path / "cache", native)
    with pytest.raises(hub.HubError, match="Duplicate native identity"):
        acquire.fetch_pages(cache, ["hub", "items"], "id")


def test_surviving_reward_on_noncompleted_trial_is_not_measured_success() -> None:
    trial = _trial(status="error", reward=1, error_type="VerifierError")
    assert trial["native_reward"] == 1
    assert not trial["reported_outcome_measured"]
    assert trial["validated_integrity"] == "not_adjudicated"


def test_hosted_error_blocks_measured_outcome_without_erasing_reward() -> None:
    trial = _trial(hosted_error_code="failed")
    assert trial["native_reward"] == 1
    assert not trial["reported_outcome_measured"]


def test_actual_lock_not_absent_config_ref_controls_native_identity() -> None:
    trial = _trial()
    assert trial["provenance_status"] == "canonical_exact_lock"
    assert trial["lock_task"]["type"] == "local"
    assert trial["lock_task"]["path"] == "/tmp/unavailable/task"


def test_source_mismatch_stays_separate_from_equal_static_tests() -> None:
    trial = _trial(source="ryanmarten/terminal-bench-4-no-prebuild")
    index = stratify.fingerprint_index(_fingerprints(_fingerprint(trial)))
    joined = stratify.join_labels([_label()], [trial], index)
    assert joined[0]["existing_label"]["label"] == "known_exploit"
    assert joined[0]["native_lock_stratum"] == "different_source"
    assert joined[0]["static_bytes"]["status"] == "bound_instruction_and_tests_equal"
    assert joined[0]["static_bytes"]["runtime_equivalence"] == "not_established"
    assert joined[0]["static_bytes"]["comparison"]["metadata_changed_paths"] == ["tests/test.sh"]
    assert not joined[0]["qualified_canonical_static_label"]


@pytest.mark.parametrize("changes,expected", [
    ({"binding_status": "unmeasured"}, "unmeasured_unbound_package"),
    ({"status": "unmeasured"}, "unmeasured_upstream_comparison"),
    ({"instruction_equal": None}, "unmeasured_instruction_or_tests"),
    ({"tests_equal": False}, "bound_instruction_or_tests_changed"),
])
def test_unbound_or_unmeasured_bytes_never_qualify(changes: dict, expected: str) -> None:
    trial = _trial()
    index = stratify.fingerprint_index(_fingerprints(_fingerprint(trial, **changes)))
    joined = stratify.join_labels([_label()], [trial], index)
    assert joined[0]["static_bytes"]["status"] == expected
    assert not joined[0]["qualified_canonical_static_label"]


def test_duplicate_fingerprints_are_ambiguous_not_last_writer_wins() -> None:
    trial = _trial()
    fingerprint = _fingerprint(trial)
    index = stratify.fingerprint_index(_fingerprints(fingerprint, fingerprint.copy()))
    assert stratify.static_stratum(trial, index)["status"] == "ambiguous_fingerprint"


def test_fingerprint_cannot_claim_another_locks_key() -> None:
    trial = _trial()
    with pytest.raises(hub.HubError, match="Fingerprint key"):
        stratify.fingerprint_index(_fingerprints(_fingerprint(trial, source="other/source")))


def test_every_unmatched_ambiguous_conflicting_and_duplicate_label_is_retained() -> None:
    trial = _trial()
    other = _trial() | {"hub_trial_id": OTHER_TRIAL}
    labels = [_label(), _label(), _label(trial_id=ARCHIVE), _label(trial_id=OTHER_TRIAL, row_id=JOB)]
    joined = stratify.join_labels(labels, [trial, other, other.copy()], {})
    assert len(joined) == len(labels)
    assert [entry["existing_label"] for entry in joined] == labels
    assert [entry["join_status"] for entry in joined] == ["conflict", "conflict", "unmatched", "ambiguous"]
    assert stratify.label_counts(joined)["label_cells"] == 4
    assert stratify.label_counts(joined)["labels"] == {"known_exploit": 4}


def test_join_does_not_guess_names_across_uuid_namespaces() -> None:
    trial = _trial()
    trial["archive_link"] = {"status": "unresolved", "archive_result_id": ARCHIVE}
    label = _label(trial_id=ARCHIVE, trial_name=trial["trial_name"])
    joined = stratify.join_labels([label], [trial], {})
    assert joined[0]["join_status"] == "unmatched"


def test_explicit_archive_link_preserves_different_native_and_original_ids() -> None:
    trial = _trial()
    trial["archive_link"] = {"status": "inventory_and_native_detail_linked", "archive_result_id": ARCHIVE, "id_equal": False}
    joined = stratify.join_labels([_label(trial_id=ARCHIVE)], [trial], {})
    assert joined[0]["join_status"] == "matched"
    assert joined[0]["join_basis"] == "explicit_archive_result_id"
    assert joined[0]["hub_trial_id"] == TRIAL
    assert joined[0]["existing_label"]["trial_id"] == ARCHIVE


def test_row_task_and_reward_conflicts_are_not_silently_joined() -> None:
    trial = _trial()
    joined = stratify.join_labels([_label(row_id=JOB, task="other", reward=0)], [trial], {})
    assert joined[0]["join_status"] == "conflict"
    assert set(joined[0]["issues"]) == {"row_membership_mismatch", "task_identity_mismatch", "reported_reward_mismatch", "existing_side_reward_mismatch"}
    assert not joined[0]["qualified_canonical_static_label"]


def test_board_counts_separate_unique_trials_and_memberships() -> None:
    trial = _trial() | {"row_ids": [ROW, JOB]}
    counts = stratify.board_counts([trial], {})
    assert counts["distinct_trials"] == 1
    assert counts["row_trial_memberships"] == 2


def test_package_lock_export_deduplicates_exact_keys_and_preserves_dataset_refs() -> None:
    first = _trial()
    second = _trial() | {"dataset_refs": [{"name": hub.PACKAGE, "ref": "v4.0.0"}]}
    exported = acquire.package_locks([first, second])
    assert len(exported["packages"]) == 1
    assert exported["packages"][0]["dataset_refs"] == [{"name": hub.PACKAGE, "ref": "4"}, {"name": hub.PACKAGE, "ref": "v4.0.0"}]


def test_derived_export_keeps_snapshots_and_never_repairs_corrupt_view(tmp_path: Path) -> None:
    first = acquire.publish(tmp_path, "view.json", {"cohort": "primary"})
    second = acquire.publish(tmp_path, "view.json", {"cohort": "board"})
    assert Path(first["immutable_snapshot"]["path"]).exists()
    assert Path(second["immutable_snapshot"]["path"]).exists()
    (tmp_path / "view.json").write_bytes(b"corrupt")
    with pytest.raises(hub.IntegrityError, match="changed"):
        acquire.publish(tmp_path, "view.json", {"cohort": "later"})
    assert (tmp_path / "view.json").read_bytes() == b"corrupt"


def test_frozen_labels_reject_changed_input_without_reading_any_other_sources(tmp_path: Path) -> None:
    label = tmp_path / "labels.jsonl"
    label.write_text(json.dumps(_label()) + "\n")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"schema": "evallab.tb4_provenance_experiment/v1", "label_inputs": [{"path": "labels.jsonl", "sha256": "0" * 64, "bytes": label.stat().st_size}], "primary_tasks": ["example"]}))
    with pytest.raises(hub.IntegrityError, match="Frozen existing-label input changed"):
        stratify.frozen_labels(spec, tmp_path)


def test_native_refusal_stops_future_live_reads_and_preserves_frozen_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = tmp_path / "prior.stdout"
    original.write_bytes(b'{"id":"frozen"}')
    calls = []

    def refuse(self: Any, args: Any) -> bytes:
        calls.append(tuple(args))
        raise hub.HubError("Native Harbor command failed (exit 23)")

    monkeypatch.setattr(hub.NativeHarbor, "json", refuse)
    refusal = tmp_path / "native-refusal.json"
    native = acquire.FrozenNative("must-not-run", {("cached",): original}, False, refusal)
    with pytest.raises(acquire.NativeRefusal, match="exit 23"):
        native.json(["first"])
    with pytest.raises(acquire.NativeRefusal, match="not_attempted"):
        native.json(["second"])
    assert len(calls) == 1
    assert native.json(["cached"]) == original.read_bytes()
    assert json.loads(refusal.read_bytes())["error"] == "Native Harbor command failed (exit 23)"


def test_missing_job_metadata_never_changes_frozen_membership(tmp_path: Path) -> None:
    native = acquire.FrozenNative("must-not-run", {}, True)
    cache = acquire.cache(tmp_path / "cache", native)
    membership = {TRIAL: [ROW], OTHER_TRIAL: [ROW]}
    jobs, summaries = acquire.census_jobs(cache, membership)
    assert jobs == []
    assert summaries == {}
    assert membership == {TRIAL: [ROW], OTHER_TRIAL: [ROW]}


def test_static_qualification_keeps_native_dataset_binding_gate() -> None:
    trial = _trial()
    index = stratify.fingerprint_index(_fingerprints(_fingerprint(trial)))
    assert stratify.join_labels([_label()], [trial], index)[0]["qualified_canonical_static_label"]
    trial["exact_source_eligible"] = False
    trial["job_dataset_matches"] = None
    joined = stratify.join_labels([_label()], [trial], index)[0]
    assert joined["native_lock_stratum"] == "canonical_exact_lock"
    assert not joined["qualified_canonical_static_label"]


def test_archive_receipts_preserve_original_id_without_assuming_hub_uuid_equality(tmp_path: Path) -> None:
    entry = {"trial_id": TRIAL, "task": "example", "trial_name": "example__abcdef"}
    directory = tmp_path / "trials/example/example__abcdef"
    directory.mkdir(parents=True)
    lock = {"source": hub.PACKAGE, "digest": DIGEST}
    (directory / "result.json").write_text(json.dumps({"id": ARCHIVE, "task_name": TASK, "trial_name": entry["trial_name"]}))
    (directory / "lock.json").write_text(json.dumps({"task": lock}))
    (directory / "config.json").write_text(json.dumps({"task": {"name": TASK}}))
    detail = {"task_name": TASK, "trial_name": entry["trial_name"], "lock": {"task": lock}}
    linked = acquire.archive_link(tmp_path, entry, detail)
    assert linked["status"] == "inventory_and_native_detail_linked"
    assert linked["archive_result_id"] == ARCHIVE
    assert linked["hub_trial_id"] == TRIAL
    assert linked["id_equal"] is False
    assert len(linked["evidence"]) == 3
    unlinked = acquire.archive_link(tmp_path, entry, None)
    assert unlinked["status"] == "unresolved"
    assert unlinked["archive_result_id"] == ARCHIVE
    assert unlinked["issues"] == ["native_detail_missing"]
    renamed = {**detail, "trial_name": "different-native-name"}
    assert acquire.archive_link(tmp_path, entry, renamed)["status"] == "unresolved"
    (directory / "result.json").write_text(json.dumps({"id": TRIAL, "task_name": TASK, "trial_name": entry["trial_name"]}))
    exact_id = acquire.archive_link(tmp_path, entry, renamed)
    assert exact_id["status"] == "inventory_and_native_detail_linked"
    assert exact_id["metadata_differences"] == ["archive_trial_name_mismatch"]
    assert exact_id["binding_issues"] == []


def test_cross_source_digest_bytes_do_not_transfer_canonical_membership() -> None:
    trial = _trial()
    trial.update({"provenance_status": "canonical_source_different_digest", "exact_source_eligible": False})
    fingerprint = _fingerprint(
        trial, binding_scope="native_packager_digest_bytes_not_source_membership",
        actual_export_source="other/dataset", actual_export_ref=DIGEST,
        declared_source_binding="unproven", canonical_dataset_eligible=False,
    )
    joined = stratify.join_labels([_label()], [trial], stratify.fingerprint_index(_fingerprints(fingerprint)))[0]
    assert joined["static_bytes"]["status"] == "bound_instruction_and_tests_equal"
    assert joined["static_bytes"]["declared_source_binding"] == "unproven"
    assert joined["static_bytes"]["actual_export_source"] == "other/dataset"
    assert joined["existing_label"] == _label()
    assert not joined["qualified_canonical_static_label"]


@pytest.mark.parametrize(("covered", "scope", "qualified"), [(False, "measured", False), (True, "unmeasured", False), (True, "measured", True)])
def test_current_export_equality_is_separate_from_historical_hash_covered_content(covered: bool, scope: str, qualified: bool) -> None:
    trial = _trial()
    coverage = {
        category: {
            "all_exported_content_paths_hashed": covered,
            "full_compared_content_paths_digest_covered": covered,
            "native_hash_included_paths": [category],
            "exported_content_paths_not_hashed": [] if covered else [".unhashed-support"],
            "unmeasured_exported_content_paths": [],
            "upstream_content_paths_missing_from_export": [],
        }
        for category in ("instruction", "tests")
    }
    fingerprint = _fingerprint(
        trial, content_hash_coverage=coverage,
        native_hash_scope={"status": scope, "included_paths": ["instruction", "tests"], "not_hashed_paths": [], "mode_and_symlink_metadata_hashed": False},
        historical_full_inventory_binding="unmeasured",
    )
    joined = stratify.join_labels([_label()], [trial], stratify.fingerprint_index(_fingerprints(fingerprint)))[0]
    assert joined["qualified_canonical_static_label"]
    assert joined["qualified_canonical_hash_covered_label"] is qualified
    assert joined["static_bytes"]["content_hash_coverage"]["tests"]["current_export_equal"] is True
    assert joined["static_bytes"]["historical_full_inventory_binding"] == "unmeasured"
    assert joined["static_bytes"]["native_hash_scope"]["mode_and_symlink_metadata_hashed"] is False
    assert joined["static_bytes"]["runtime_equivalence"] == "not_established"


def test_unresolved_archive_link_is_retained_without_qualifying_original_label() -> None:
    trial = _trial()
    trial["archive_link"] = {"status": "unresolved", "archive_result_id": ARCHIVE, "issues": ["archive_lock_mismatch"]}
    joined = stratify.join_labels([_label()], [trial], stratify.fingerprint_index(_fingerprints(_fingerprint(trial))))[0]
    assert joined["join_status"] == "matched"
    assert joined["existing_label"] == _label()
    assert joined["original_archive_binding"] == "unresolved"
    assert not joined["qualified_canonical_static_label"]


def test_parsed_input_receipt_hashes_the_same_single_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "input.json"
    original = b'{"stage":"original"}'
    path.write_bytes(original)
    read_bytes = Path.read_bytes
    calls = []

    def changed_after_read(current: Path) -> bytes:
        raw = read_bytes(current)
        if current == path:
            calls.append(current)
            current.write_bytes(b'{"stage":"newer"}')
        return raw

    monkeypatch.setattr(Path, "read_bytes", changed_after_read)
    parsed, evidence = acquire.read_receipted(path)
    assert parsed == {"stage": "original"}
    assert evidence["sha256"] == hashlib.sha256(original).hexdigest()
    assert evidence["bytes"] == len(original)
    assert calls == [path]


def test_archive_consumer_binds_original_result_to_its_single_read_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = {"trial_id": TRIAL, "task": "example", "trial_name": "example__abcdef"}
    directory = tmp_path / "trials/example/example__abcdef"
    directory.mkdir(parents=True)
    result_path = directory / "result.json"
    original = json.dumps({"id": ARCHIVE, "task_name": TASK, "trial_name": entry["trial_name"]}).encode()
    result_path.write_bytes(original)
    lock = {"source": hub.PACKAGE, "digest": DIGEST}
    (directory / "lock.json").write_text(json.dumps({"task": lock}))
    (directory / "config.json").write_text(json.dumps({"task": {"name": TASK}}))
    read_bytes = Path.read_bytes
    calls = []

    def changed_after_read(path: Path) -> bytes:
        raw = read_bytes(path)
        if path == result_path:
            calls.append(path)
            path.write_bytes(json.dumps({"id": OTHER_TRIAL, "task_name": TASK, "trial_name": entry["trial_name"]}).encode())
        return raw

    monkeypatch.setattr(Path, "read_bytes", changed_after_read)
    linked = acquire.archive_link(tmp_path, entry, {"task_name": TASK, "trial_name": entry["trial_name"], "lock": {"task": lock}})
    assert linked["archive_result_id"] == ARCHIVE
    assert linked["status"] == "inventory_and_native_detail_linked"
    result_receipt = next(evidence for evidence in linked["evidence"] if evidence["path"] == str(result_path))
    assert result_receipt["sha256"] == hashlib.sha256(original).hexdigest()
    assert result_receipt["bytes"] == len(original)
    assert calls == [result_path]


def test_frozen_label_consumer_never_parses_unhashed_replacement_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    label_path = tmp_path / "labels.jsonl"
    original = (json.dumps(_label()) + "\n").encode()
    label_path.write_bytes(original)
    census_path = tmp_path / "census.json"
    census_path.write_text(json.dumps({"trials": 1, "tasks": ["example"]}))
    inputs = [
        {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
        for path in (label_path, census_path)
    ]
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"schema": "evallab.tb4_provenance_experiment/v1", "label_inputs": inputs, "primary_tasks": ["example"]}))
    read_bytes = Path.read_bytes

    def changed_after_read(path: Path) -> bytes:
        raw = read_bytes(path)
        if path == label_path:
            path.write_bytes((json.dumps(_label(label="task_defect")) + "\n").encode())
        return raw

    monkeypatch.setattr(Path, "read_bytes", changed_after_read)
    _, labels, evidence = stratify.frozen_labels(spec_path, tmp_path)
    assert labels == [_label()]
    label_receipt = next(item for item in evidence if item["path"] == str(label_path))
    assert label_receipt["sha256"] == hashlib.sha256(original).hexdigest()


@pytest.mark.parametrize(("native_lock", "lock_status"), [(None, "null"), ({"task": None}, "task_null")])
def test_failed_native_trial_with_null_lock_is_preserved_unmeasured(native_lock: dict | None, lock_status: str) -> None:
    summary = {"id": TRIAL, "job_id": JOB, "task_name": TASK, "status": "failed", "reward": 1, "error_type": "TaskStartError"}
    detail = {**summary, "lock": native_lock, "verifier_result": None}
    record = acquire.trial_record(TRIAL, [ROW], summary, detail, {"sha256": "evidence"}, {TASK: {"digest": DIGEST}}, {"dataset_matches": True}, None, None)
    assert record["native_reward"] == 1
    assert record["native_status"] == "failed"
    assert record["detail_status"] == "complete"
    assert record["lock_task"] is None
    assert record["native_lock_status"] == lock_status
    assert record["provenance_status"] == "unmeasured"
    assert not record["reported_outcome_measured"]
    assert not record["exact_source_eligible"]
    assert record["validated_integrity"] == "not_adjudicated"
    assert acquire.package_locks([record])["packages"] == []
    assert stratify.static_stratum(record, {})["status"] == "unmeasured_no_task_lock"
    assert stratify.board_counts([record], {})["missing_task_lock_ids"] == [TRIAL]
