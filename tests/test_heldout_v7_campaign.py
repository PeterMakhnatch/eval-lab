from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import runpy
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.regrade import SpendCapReached
from evallab.task_variants import task_directory_digest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "research/experiments/heldout-v7"
FROZEN = ROOT / "research/experiments/mimo-training-20261016/heldout.json"
JOURNAL_APPROVAL = "Isolated journal unit fixture; no real paid execution authority"
BUDGET_NOW = datetime(2026, 10, 18, 9, tzinfo=UTC)


@pytest.fixture(scope="module")
def inventory_module():
    return runpy.run_path(str(HERE / "regrade.py"))


@pytest.fixture(scope="module")
def campaign():
    return runpy.run_path(str(HERE / "campaign.py"))


@pytest.fixture
def budget_envelope(tmp_path, campaign):
    journal = tmp_path / "classifiers-v7-reservations.jsonl"
    journal.write_text("")
    approval = tmp_path / "approval.txt"
    approval.write_text("Isolated combined-authority fixture, not a real approval.\n")
    value = {
        "schema": "mimo.phase2-envelope/v1",
        "admitted": True,
        "approval": JOURNAL_APPROVAL,
        "ledger_tag": "2026-10-17-v7",
        "expires_at": "2026-10-18T10:00:00+00:00",
        "approved_usd": 12.0,
        "require_envelope_sum_at_most_usd": 12.0,
        "serving": {"cap_usd": 2.5},
        "native": {"cap_usd": 6.0},
        "heldout_regrade": {
            "cap_usd": 3.3,
            "usd_per_execution": 0.0666,
            "durable_journal_path": str(journal),
            "reset_authorized": False,
            "canonical_reuses_exact_measurements_only": True,
        },
        "training_reservation_usd": 0.2,
        "sources": {"approval": campaign["file_ref"](approval)},
        "operator_note": {"reason": "Retain the complete original parent envelope."},
    }
    path = tmp_path / "budget-envelope.json"
    path.write_text(json.dumps(value))
    return path, value, journal


@pytest.fixture
def no_native_input(tmp_path, inventory_module):
    leaves = json.loads(FROZEN.read_bytes())["heldout"]
    task = leaves[0]
    package = tmp_path / "package"
    package.mkdir()
    (package / "task.toml").write_text(
        f'[task]\nname = "mimo-v2.6-rl/{task}"\n[metadata]\nsource_id = "{task}"\n'
    )
    digest = task_directory_digest(package)
    mapping = io.StringIO()
    writer = csv.DictWriter(mapping, fieldnames=["task_id", "v6_package_digest"])
    writer.writeheader()
    writer.writerows({"task_id": leaf, "v6_package_digest": digest if leaf == task else "sha256:" + "0" * 64} for leaf in leaves)
    pairs = tmp_path / "pairs.csv"
    pairs.write_text(mapping.getvalue())
    sources = {}
    for arm in ("base", "A", "B", "C"):
        path = tmp_path / f"{arm}.jsonl"
        path.write_text("")
        sources[arm] = path
    row = {"task": str(package), "task_id": task, "arm": "A", "name": "unstarted-A",
           "job_dir": None, "attempt_index": 1, "reward": None, "passed": None,
           "error": "never_dispatched"}
    config_path = tmp_path / "inputs.json"

    def write(rows):
        sources["A"].write_text("".join(json.dumps(item) + "\n" for item in rows))
        config = {"schema": "heldout-v7.source-inputs/v1",
                  "frozen": inventory_module["file_ref"](FROZEN),
                  "package_pairs": inventory_module["file_ref"](pairs),
                  "source_files": {arm: inventory_module["file_ref"](path) for arm, path in sources.items()}}
        config_path.write_text(json.dumps(config))
        return config_path

    return row, write


def test_scheduled_null_job_is_not_a_native_failure(no_native_input, inventory_module):
    row, write = no_native_input
    value = inventory_module["inventory"](write([row]))
    assert value["rows"] == []
    assert value["summary"]["A"] == {
        "recorded_schedule_cells": 1, "native_attempts": 0, "kept_diffs": 0,
        "empty_kept_diffs": 0, "v6_measured": 0, "v6_passes": 0,
        "canonical_unmeasured_with_diff": 0, "native_missing_diff": 0,
    }
    cell = value["non_native_cells"][0]
    assert cell["task_id"] == "mimo-v2.6-rl/" + row["task_id"]
    assert cell["reason"] == "never_dispatched"
    assert cell["native_job_dir"] is None
    assert "source_uid" not in cell
    assert row["task_id"] not in [task.rsplit("/", 1)[-1] for task in value["unscheduled_tasks_by_arm"]["A"]]


@pytest.mark.parametrize("change,match", [
    ({"attempt_index": 2}, "unauthorized raw source attempt"),
    ({"arm": "base"}, "arm identity"),
    ({"task_id": "format-code-task-999999"}, "task identity"),
    ({"reward": 1.0, "passed": True}, "unmeasured row carries"),
])
def test_invalid_source_cells_refuse_before_native_discovery(no_native_input, inventory_module, change, match):
    row, write = no_native_input
    with pytest.raises(ValueError, match=match):
        inventory_module["inventory"](write([row | change]))


def test_duplicate_unstarted_cell_cannot_hide_in_the_missing_population(no_native_input, inventory_module):
    row, write = no_native_input
    with pytest.raises(ValueError, match="duplicate official source cell"):
        inventory_module["inventory"](write([row, row]))


def test_corrected_canonical_null_does_not_inherit_a_raw_native_pass(inventory_module):
    value = {"reward": None, "passed": None, "error": "proxy_usage_reconciliation",
             "native_raw_reward": {"reward": 1.0}}
    assert inventory_module["canonical_outcome"](value) == (None, "proxy_usage_reconciliation")


@pytest.mark.parametrize("reward", [True, "1", 0.5, float("nan"), float("inf")])
def test_nonbinary_canonical_endpoints_are_invalid(inventory_module, reward):
    with pytest.raises(ValueError, match="nonbinary canonical endpoint"):
        inventory_module["canonical_outcome"]({"reward": reward, "passed": True})


def test_historical_scoring_allowed_is_not_v7_authorization(campaign, inventory_module, tmp_path):
    with pytest.raises(ValueError, match="producer release"):
        campaign["admit"]({}, FROZEN, inventory_module["file_ref"](FROZEN)["sha256"], tmp_path / "absent-receipt")


def test_declared_candidate_cannot_open_scoring(campaign, inventory_module, tmp_path):
    manifest = json.loads(FROZEN.read_bytes()) | {"successor_version": "v7", "scoring_allowed": False}
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="producer release"):
        campaign["admit"]({}, path, inventory_module["file_ref"](path)["sha256"], tmp_path / "absent-receipt")


def test_different_manifest_bytes_cannot_inherit_verifier_acceptance(campaign, tmp_path):
    with pytest.raises(ValueError, match="source digest changed"):
        campaign["admit"]({}, FROZEN, "0" * 64, tmp_path / "absent-receipt")


@pytest.mark.parametrize("training_present", [False, True])
def test_budget_allows_zero_inactive_lanes_without_inventing_spend(campaign, budget_envelope, training_present):
    path, value, journal = budget_envelope
    value["serving"]["cap_usd"] = value["native"]["cap_usd"] = 0
    value["approved_usd"] = value["require_envelope_sum_at_most_usd"] = 3.3
    if training_present:
        value["training_reservation_usd"] = 0
    else:
        del value["training_reservation_usd"]
    value["additional_work"] = {"cap_usd": 0}
    path.write_text(json.dumps(value))
    admitted, _ = campaign["checked_budget"](
        path, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW,
    )
    assert admitted == value
    assert journal.read_bytes() == b""


@pytest.mark.parametrize("sources_shape", ["absent", "mapping"])
def test_budget_reader_binds_the_original_parent_without_writing(campaign, budget_envelope, sources_shape):
    path, value, journal = budget_envelope
    if sources_shape == "absent":
        value.pop("sources")
    value["expires_at"] = "2026-10-18T06:00:00-04:00"
    path.write_text(json.dumps(value))
    original, prior = path.read_bytes(), journal.read_bytes()

    admitted, reference = campaign["checked_budget"](
        path, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW,
    )

    assert admitted == value
    assert reference == campaign["file_ref"](path)
    assert path.read_bytes() == original
    assert journal.read_bytes() == prior
    assert not journal.with_name("classifiers-v7-ledger.lock").exists()


def test_string_denominations_and_unknown_invoice_preserve_the_exact_aggregate_boundary(campaign, budget_envelope):
    path, value, journal = budget_envelope
    value.update(approved_usd="12.00", require_envelope_sum_at_most_usd="12.00",
                 training_reservation_usd="0.00")
    value["serving"].update(cap_usd="2.50", settled_invoice_usd=None)
    value["native"]["cap_usd"] = "0.00"
    value["heldout_regrade"].update(cap_usd="3.30", usd_per_execution="0.0666")
    value["additional_work"] = {"cap_usd": "6.20"}
    path.write_text(json.dumps(value))
    _, reference = campaign["checked_budget"](
        path, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW,
    )
    assert reference == campaign["file_ref"](path)
    assert journal.read_bytes() == b""

    value["additional_work"]["cap_usd"] = "6.2000000000000000001"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        campaign["checked_budget"](path, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW)
    assert journal.read_bytes() == b""


@pytest.mark.parametrize("field, replacement", [
    ("schema", "mimo.phase2-envelope/v2"),
    ("admitted", False),
    ("admitted", 1),
    ("approval", ""),
    ("approval", " \n"),
    ("approval", True),
    ("ledger_tag", "2026-10-18-v7"),
    ("expires_at", "2026-10-18T09:00:00+00:00"),
    ("expires_at", "2026-10-18T08:00:00-01:00"),
    ("expires_at", "2026-10-18T08:59:59+00:00"),
    ("expires_at", "2026-10-18T10:00:00"),
    ("expires_at", "not-an-instant"),
    ("approved_usd", 0),
    ("approved_usd", 12.01),
    ("require_envelope_sum_at_most_usd", 0),
    ("require_envelope_sum_at_most_usd", 12.01),
])
def test_budget_requires_current_exact_authority(campaign, budget_envelope, field, replacement):
    path, value, journal = budget_envelope
    value[field] = replacement
    path.write_text(json.dumps(value))
    prior = journal.read_bytes()
    with pytest.raises(ValueError):
        campaign["checked_budget"](path, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW)
    assert journal.read_bytes() == prior


@pytest.mark.parametrize("field", [
    ("approved_usd",),
    ("require_envelope_sum_at_most_usd",),
    ("training_reservation_usd",),
    ("serving", "cap_usd"),
    ("native", "cap_usd"),
    ("heldout_regrade", "cap_usd"),
    ("heldout_regrade", "usd_per_execution"),
    ("additional_work", "cap_usd"),
])
@pytest.mark.parametrize("replacement", [
    True, False, -0.01, float("nan"), float("inf"), -float("inf"), "not-money", "NaN", "Infinity", "-0.01", None,
])
def test_every_parent_money_input_rejects_nonmoney(campaign, budget_envelope, field, replacement):
    path, value, journal = budget_envelope
    target = value
    for name in field[:-1]:
        target = target.setdefault(name, {})
    target[field[-1]] = replacement
    path.write_text(json.dumps(value))
    prior = journal.read_bytes()
    with pytest.raises(ValueError):
        campaign["checked_budget"](path, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW)
    assert journal.read_bytes() == prior


@pytest.mark.parametrize("change", [
    {"additional_work": {"cap_usd": 0.01}},
    {"training_reservation_usd": 0.21},
    {"approved_usd": 11.99, "require_envelope_sum_at_most_usd": 11.99},
    {"require_envelope_sum_at_most_usd": 11.99},
])
def test_all_parent_caps_and_training_share_both_aggregate_bounds(campaign, budget_envelope, change):
    path, value, journal = budget_envelope
    value.update(change)
    path.write_text(json.dumps(value))
    prior = journal.read_bytes()
    with pytest.raises(ValueError):
        campaign["checked_budget"](path, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW)
    assert journal.read_bytes() == prior


@pytest.mark.parametrize("section", ["serving", "native", "heldout_regrade"])
def test_budget_cannot_omit_an_existing_shared_lane(campaign, budget_envelope, section):
    path, value, journal = budget_envelope
    del value[section]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        campaign["checked_budget"](path, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW)
    assert journal.read_bytes() == b""


@pytest.mark.parametrize("field, replacement", [
    ("cap_usd", 0),
    ("reset_authorized", True),
    ("reset_authorized", 0),
    ("reset_authorized", None),
    ("canonical_reuses_exact_measurements_only", False),
    ("canonical_reuses_exact_measurements_only", 1),
    ("canonical_reuses_exact_measurements_only", None),
])
def test_budget_cannot_reset_spend_or_relax_measurement_identity(campaign, budget_envelope, field, replacement):
    path, value, journal = budget_envelope
    value["heldout_regrade"][field] = replacement
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        campaign["checked_budget"](path, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW)
    assert journal.read_bytes() == b""


@pytest.mark.parametrize("quote", [
    0.0666000001, 0.0665, True, False, -0.01, float("nan"), float("inf"),
])
def test_budget_cannot_change_or_approximate_the_execution_quote(campaign, budget_envelope, quote):
    path, _, journal = budget_envelope
    with pytest.raises(ValueError):
        campaign["checked_budget"](path, journal=journal, usd_per_execution=quote, now=BUDGET_NOW)
    assert journal.read_bytes() == b""


@pytest.mark.parametrize("change", ["other_existing", "missing_declared", "missing_expected"])
def test_budget_cannot_repoint_or_reinitialize_the_existing_journal(campaign, budget_envelope, tmp_path, change):
    path, value, journal = budget_envelope
    other = tmp_path / "different-reservations.jsonl"
    if change == "other_existing":
        other.write_text("")
        value["heldout_regrade"]["durable_journal_path"] = str(other)
    elif change == "missing_declared":
        value["heldout_regrade"]["durable_journal_path"] = str(other)
    else:
        journal.unlink()
    path.write_text(json.dumps(value))
    with pytest.raises((ValueError, OSError)):
        campaign["checked_budget"](path, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW)
    assert journal.exists() is (change != "missing_expected")
    if journal.exists():
        assert journal.read_bytes() == b""
    assert other.exists() is (change == "other_existing")
    if other.exists():
        assert other.read_bytes() == b""


def test_budget_resolves_aliases_to_the_same_durable_journal(campaign, budget_envelope, tmp_path):
    path, value, journal = budget_envelope
    alias = tmp_path / "journal-alias.jsonl"
    alias.symlink_to(journal)
    value["heldout_regrade"]["durable_journal_path"] = str(alias)
    path.write_text(json.dumps(value))
    admitted, _ = campaign["checked_budget"](
        path, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW,
    )
    assert admitted == value
    assert journal.read_bytes() == b""


@pytest.mark.parametrize("sources_shape", ["list", "mapping"])
def test_budget_source_bytes_cannot_change_under_retained_authority(campaign, budget_envelope, sources_shape):
    path, value, journal = budget_envelope
    reference = value["sources"]["approval"]
    if sources_shape == "list":
        value["sources"] = [reference]
    path.write_text(json.dumps(value))
    Path(reference["path"]).write_text("Different approval bytes.\n")
    with pytest.raises(ValueError):
        campaign["checked_budget"](path, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW)
    assert journal.read_bytes() == b""


def test_failed_execution_reservation_survives_restart_without_refund(campaign, tmp_path):
    path = tmp_path / "reservations.jsonl"
    gate = campaign["JournalGate"](path, 1.5, cap_usd=3.0, approval=JOURNAL_APPROVAL)
    gate.context.source_uid = ["job-1", "trial-1"]
    gate.charge()
    restarted = campaign["JournalGate"](path, 1.5, cap_usd=3.0, approval=JOURNAL_APPROVAL)
    assert restarted.to_json()["reserved_usd"] == 1.5
    restarted.context.source_uid = ["job-1", "trial-1"]
    with pytest.raises(SpendCapReached, match="already reserved"):
        restarted.charge()
    restarted.context.source_uid = ["job-2", "trial-2"]
    restarted.charge()
    restarted.context.source_uid = ["job-3", "trial-3"]
    with pytest.raises(SpendCapReached, match="cap is exhausted"):
        restarted.charge()
    assert restarted.to_json()["reserved_usd"] == 3.0
    assert [entry["source_uid"] for entry in map(json.loads, path.read_text().splitlines())] == [["job-1", "trial-1"], ["job-2", "trial-2"]]


def test_parallel_reservations_share_the_same_cap(campaign, tmp_path):
    gate = campaign["JournalGate"](
        tmp_path / "reservations.jsonl", 1.0, cap_usd=3.0, approval=JOURNAL_APPROVAL,
    )

    def reserve(index):
        uid = [f"job-{index}", f"trial-{index}"]
        gate.context.source_uid = uid
        try:
            gate.charge()
            return tuple(uid)
        except SpendCapReached:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        charged = {uid for uid in pool.map(reserve, range(12)) if uid is not None}
    assert gate.to_json()["reserved_usd"] == 3.0
    assert gate.reserved_uids == charged
    assert len(charged) == 3
    journal = [json.loads(line) for line in gate.path.read_text().splitlines()]
    assert [entry["execution"] for entry in journal] == [1, 2, 3]
    assert {tuple(entry["source_uid"]) for entry in journal} == charged


def test_changed_or_truncated_journal_is_not_reset(campaign, tmp_path):
    path = tmp_path / "reservations.jsonl"
    gate = campaign["JournalGate"](path, 0.02, cap_usd=3.0, approval=JOURNAL_APPROVAL)
    gate.context.source_uid = ["job", "trial"]
    gate.charge()
    with pytest.raises(ValueError, match="invalid cumulative"):
        campaign["JournalGate"](path, 0.01, cap_usd=3.0, approval=JOURNAL_APPROVAL)
    with path.open("a") as stream:
        stream.write('{"source_uid":')
    with pytest.raises(ValueError):
        campaign["JournalGate"](path, 0.02, cap_usd=3.0, approval=JOURNAL_APPROVAL)


def test_increasing_cap_preserves_all45_reservations_and_admits_only_four_new_keys(campaign, tmp_path):
    path = tmp_path / "reservations.jsonl"
    old_digest, new_digest = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    old = campaign["JournalGate"](path, 0.0666, cap_usd=3.0, approval=JOURNAL_APPROVAL)
    for index in range(45):
        old.context.source_uid = (f"job-{index}", f"trial-{index}")
        old.context.grader_digest = old_digest
        old.charge()
    prefix = path.read_bytes()
    assert old.executions == 45
    assert old.to_json()["reserved_usd"] == pytest.approx(2.997)
    old.context.source_uid = ("job-45", "trial-45")
    with pytest.raises(SpendCapReached):
        old.charge()
    assert path.read_bytes() == prefix

    amended = campaign["JournalGate"](
        path, 0.0666, cap_usd=3.30, approval=JOURNAL_APPROVAL + "; amended allocation",
    )
    assert amended.executions == 45
    assert amended.to_json()["reserved_usd"] == pytest.approx(2.997)
    assert path.read_bytes() == prefix
    amended.context.source_uid = ("job-0", "trial-0")
    amended.context.grader_digest = old_digest
    with pytest.raises(SpendCapReached):
        amended.charge()
    assert path.read_bytes() == prefix
    with pytest.raises(ValueError):
        campaign["JournalGate"](path, 0.01, cap_usd=3.30, approval=JOURNAL_APPROVAL)
    assert path.read_bytes() == prefix

    new_keys = [
        (("job-0", "trial-0"), new_digest),
        (("job-1", "trial-1"), new_digest),
        (("job-45", "trial-45"), old_digest),
        (("job-46", "trial-46"), old_digest),
    ]
    for uid, digest in new_keys:
        assert not amended.is_reserved(uid, digest)
        amended.context.source_uid, amended.context.grader_digest = uid, digest
        amended.charge()
    after_four = path.read_bytes()
    assert after_four.startswith(prefix)
    assert amended.executions == 49
    assert amended.to_json()["reserved_usd"] == pytest.approx(3.2634)
    entries = [json.loads(line) for line in after_four.splitlines()]
    assert [entry["execution"] for entry in entries] == list(range(1, 50))
    assert [(tuple(entry["source_uid"]), entry["grader_digest"]) for entry in entries[45:]] == new_keys
    assert entries[-1]["reserved_usd"] == 3.2634

    amended.context.source_uid, amended.context.grader_digest = ("job-47", "trial-47"), old_digest
    with pytest.raises(SpendCapReached):
        amended.charge()
    assert path.read_bytes() == after_four
    restarted = campaign["JournalGate"](path, 0.0666, cap_usd=3.30, approval=JOURNAL_APPROVAL)
    assert restarted.executions == 49
    assert restarted.to_json()["reserved_usd"] == pytest.approx(3.2634)
    assert restarted.is_reserved(("job-0", "trial-0"), old_digest)
    assert restarted.is_reserved(("job-0", "trial-0"), new_digest)
    with pytest.raises(ValueError):
        campaign["JournalGate"](path, 0.0666, cap_usd=3.0, approval=JOURNAL_APPROVAL)
    assert path.read_bytes() == after_four


def test_cached_observation_and_native_artifacts_are_both_hash_bound(campaign, tmp_path):
    native = tmp_path / "native.json"
    native.write_text('{"reward":1}')
    saved = tmp_path / "observation.json"
    observation = {"source_uid": ["job", "trial"], "v7_outcome": True,
                   "native_evidence": {"files": {str(native): campaign["file_ref"](native)}}}
    reference = campaign["create_json"](saved, observation)
    campaign["create_json"](tmp_path / "observation-ref.json", reference)
    native.write_text('{"reward":0}')
    with pytest.raises(ValueError, match="source digest changed"):
        campaign["_retained_observation"](saved)
    native.write_text('{"reward":1}')
    observation["v7_outcome"] = False
    saved.write_text(json.dumps(observation))
    with pytest.raises(ValueError, match="source digest changed"):
        campaign["_retained_observation"](saved)


@pytest.fixture
def released_source(tmp_path, no_native_input, inventory_module):
    """Isolated declared-release inputs, never a real scientific approval."""
    from uuid import UUID

    from evallab.registry import harbor_task_digest
    from evallab.task_variants import derive_task

    _, write = no_native_input
    leaves = json.loads(FROZEN.read_bytes())["heldout"]
    repo, variants = tmp_path / "repo", tmp_path / "variants"
    repo.mkdir()
    rows, selected, pairs = [], [], []
    for index, leaf in enumerate(leaves):
        task = "mimo-v2.6-rl/" + leaf
        parent = tmp_path / "parents" / leaf
        (parent / "environment").mkdir(parents=True)
        (parent / "tests").mkdir()
        (parent / "task.toml").write_text(
            f'schema_version = "1.4"\n[task]\nname = "{task}"\n[metadata]\nsource_id = "{leaf}"\n'
        )
        (parent / "instruction.md").write_text("Isolated admission-boundary fixture.\n")
        (parent / "environment/Dockerfile").write_text("FROM ubuntu:24.04\n")
        (parent / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
        record = derive_task(
            parent, changes={"tests/test.sh": b"#!/bin/sh\nexit 1\n"},
            transform="verifier-repair@1", rationale="isolated admission-boundary test",
            created_by="pytest", repo_root=repo,
            records_dir=Path("library/task-variants"), variants_root=variants,
        )
        package = variants / record.task_slug / record.digest12
        record_path = repo / "library/task-variants" / record.task_slug / f"{record.digest12}.json"
        record_ref = inventory_module["file_ref"](record_path)
        selected.append({
            "task_id": leaf, "frozen_parent": {"package_digest": record.parent.digest},
            "evaluation_target": {
                "package_path": str(package), "package_digest": record.variant_digest,
                "harbor_digest": record.variant_harbor_digest,
                "lineage": {
                    "record_sha256": record_ref["sha256"],
                    "records_newest_first": [{
                        "record": record_ref, "package_digest": record.variant_digest,
                        "harbor_digest": record.variant_harbor_digest, "transform": record.transform,
                    }],
                },
            },
        })
        pairs.append({"task_id": leaf, "v6_package_digest": record.parent.digest})
        job = tmp_path / "jobs" / f"native-{leaf}"
        trial = job / "trial"
        (trial / "verifier").mkdir(parents=True)
        (trial / "verifier/agent.kept.diff").write_bytes(b"")
        job_id, trial_id = str(UUID(int=2 * index + 1)), str(UUID(int=2 * index + 2))
        (job / "result.json").write_text(json.dumps({"id": job_id}))
        (job / "lab-metadata.json").write_text(json.dumps({
            "task_staging": {"declared_task_name": task, "source_package_digest": record.parent.digest},
        }))
        (trial / "result.json").write_text(json.dumps({
            "id": trial_id, "task_name": task, "exception_info": None,
            "config": {"job_id": job_id, "agent": {"model_name": inventory_module["MODEL"] + ":rds1-a"}},
            "verifier_result": {"rewards": {"reward": 0.0}},
        }))
        (trial / "lock.json").write_text(json.dumps({
            "task": {"type": "local", "name": job.name, "path": str(parent),
                     "digest": harbor_task_digest(parent)},
        }))
        rows.append({"task": str(parent), "task_id": leaf, "arm": "A", "name": job.name,
                     "job_dir": str(job), "attempt_index": 1, "reward": 0.0, "passed": False, "error": None})
    with (tmp_path / "pairs.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=["task_id", "v6_package_digest"])
        writer.writeheader()
        writer.writerows(pairs)
    config_path = write(rows)
    inventory = inventory_module["inventory"](config_path)
    inventory_path = tmp_path / "inventory.json"
    inventory_module["create_json"](inventory_path, inventory)
    manifest = {
        "schema": "mimo.heldout/v1", "successor_version": "v7",
        "scoring_allowed": True, "freeze_ready": True, "pending_gates": [],
        "independent_verifier": {"status": "pass"}, "heldout": leaves,
        "frozen_sources": {"heldout.json": inventory_module["file_ref"](FROZEN)},
        "sources": [], "tasks": selected,
    }
    manifest_path = tmp_path / "declared-release.json"
    manifest_path.write_text(json.dumps(manifest))
    receipt = tmp_path / "unit-only-receipt.txt"
    receipt.write_text("Isolated unit fixture, not a real Verifier acceptance.\n")
    kwargs = {
        "inventory_path": inventory_path, "manifest_path": manifest_path,
        "accepted_sha": inventory_module["file_ref"](manifest_path)["sha256"],
        "verifier_receipt": receipt, "rate_evidence": receipt, "usd_per_execution": 0.02,
        "work": tmp_path / "campaign", "output": tmp_path / "report", "repo_root": ROOT,
    }
    return kwargs, inventory, manifest, config_path


def test_native_task_cannot_be_relabelled_by_a_canonical_row(released_source, inventory_module):
    _, inventory, _, config = released_source
    first, second = inventory["rows"][:2]
    result_path = Path(first["source_trial_dir"]) / "result.json"
    result = json.loads(result_path.read_bytes())
    result["task_name"] = second["task_id"]
    result_path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match="native source task identity"):
        inventory_module["inventory"](config)


def test_missing_staging_requires_the_real_native_locked_package(released_source, inventory_module):
    _, inventory, _, config = released_source
    row = inventory["rows"][0]
    trial = Path(row["source_trial_dir"])
    (trial.parent / "lab-metadata.json").unlink()
    lock_path = trial / "lock.json"
    lock = json.loads(lock_path.read_bytes())
    lock["task"]["digest"] = "sha256:" + "0" * 64
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="source native locked package"):
        inventory_module["inventory"](config)


def test_derived_inventory_cannot_override_its_canonical_grade(released_source, campaign):
    kwargs, inventory, _, _ = released_source
    inventory["rows"][0]["v6_outcome"] = True
    kwargs["inventory_path"].write_text(json.dumps(inventory))
    with pytest.raises(ValueError, match="inventory cells differ"):
        campaign["execute"](**kwargs)
    assert not kwargs["work"].exists()
    assert not kwargs["output"].exists()


def test_release_missing_one_selected_grader_refuses_after_real_record_validation(released_source, campaign):
    kwargs, inventory, manifest, _ = released_source
    manifest["tasks"].pop()
    kwargs["manifest_path"].write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="exactly one grader"):
        campaign["admit"](
            inventory, kwargs["manifest_path"], campaign["file_ref"](kwargs["manifest_path"])["sha256"],
            kwargs["verifier_receipt"],
        )


@pytest.mark.parametrize("field", ["task_name", "variant_digest", "variant_harbor_digest"])
def test_selected_record_must_bind_its_actual_task_and_both_digests(released_source, campaign, field):
    kwargs, inventory, manifest, _ = released_source
    lineage = manifest["tasks"][0]["evaluation_target"]["lineage"]
    record_path = Path(lineage["records_newest_first"][0]["record"]["path"])
    record = json.loads(record_path.read_bytes())
    record[field] = inventory["rows"][1]["task_id"] if field == "task_name" else "sha256:" + "0" * 64
    record_path.write_text(json.dumps(record))
    reference = campaign["file_ref"](record_path)
    lineage["record_sha256"] = reference["sha256"]
    lineage["records_newest_first"][0]["record"] = reference
    kwargs["manifest_path"].write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="record does not bind"):
        campaign["admit"](
            inventory, kwargs["manifest_path"], campaign["file_ref"](kwargs["manifest_path"])["sha256"],
            kwargs["verifier_receipt"],
        )


def test_campaign_cannot_write_inside_the_retained_original_package(released_source, campaign):
    kwargs, inventory, _, _ = released_source
    package = Path(inventory["rows"][0]["source_package"]["path"])
    kwargs["work"] = package / "forbidden"
    with pytest.raises(ValueError, match="overlaps immutable input"):
        campaign["execute"](**kwargs)
    assert not kwargs["work"].exists()
    assert not kwargs["output"].exists()


@pytest.mark.parametrize("relative", [".", "nested/report", ".."])
def test_campaign_outputs_cannot_overlap_its_work_directory(released_source, campaign, relative):
    kwargs, _, _, _ = released_source
    kwargs["output"] = kwargs["work"] / relative
    with pytest.raises(ValueError, match="disjoint from campaign work"):
        campaign["execute"](**kwargs)
    assert not kwargs["work"].exists()


@pytest.mark.parametrize("authority", ["missing", "overallocated", "wrong_journal", "expired"])
def test_canonical_execution_refuses_invalid_budget_before_runtime_or_writes(
    released_source, campaign, budget_envelope, monkeypatch, tmp_path, authority,
):
    kwargs, inventory, _, _ = released_source
    path, value, journal = budget_envelope
    value["heldout_regrade"]["usd_per_execution"] = kwargs["usd_per_execution"]
    if authority == "overallocated":
        value["additional_work"] = {"cap_usd": 0.01}
    elif authority == "wrong_journal":
        other = tmp_path / "replacement-journal.jsonl"
        other.write_text("")
        value["heldout_regrade"]["durable_journal_path"] = str(other)
    elif authority == "expired":
        value["expires_at"] = BUDGET_NOW.isoformat()
    path.write_text(json.dumps(value))
    if authority != "missing":
        kwargs["budget_envelope"] = path

    prior_gate = campaign["JournalGate"](
        journal, kwargs["usd_per_execution"], cap_usd=3.0, approval=JOURNAL_APPROVAL,
    )
    prior_gate.context.source_uid = inventory["rows"][0]["source_uid"]
    prior_gate.charge()
    prior = journal.read_bytes()
    checked_budget = campaign["checked_budget"]

    def fixed_clock_budget(path, *, journal, usd_per_execution):
        return checked_budget(path, journal=journal, usd_per_execution=usd_per_execution, now=BUDGET_NOW)

    monkeypatch.setitem(campaign["execute"].__globals__, "checked_budget", fixed_clock_budget)
    load = runpy.run_path

    def no_native_runtime(path, *args, **options):
        if Path(path).name == "native.py":
            raise AssertionError("Native runtime reached without valid financial authority")
        return load(path, *args, **options)

    monkeypatch.setattr(runpy, "run_path", no_native_runtime)
    with pytest.raises(ValueError):
        campaign["execute"](**kwargs)
    assert not kwargs["work"].exists()
    assert not kwargs["output"].exists()
    assert not journal.with_name("classifiers-v7-ledger.lock").exists()
    assert journal.read_bytes() == prior


GRANT_ID = "11111111-2222-4333-8444-abcdef555555"
GRANT_FOREIGN_ID = "99999999-8888-4777-8666-777777777777"
GRANT_LIVE = datetime(2026, 10, 18, 9, 30, tzinfo=UTC)
WINDOW2_AUTHORITY = "trainer-finish-data-20261018-window-2"


def _grant_clock() -> datetime:
    return GRANT_LIVE


@pytest.fixture
def granted_budget(tmp_path, campaign, monkeypatch):
    """Isolated raised-authority envelope and its bound 49-row journal; not a real approval."""
    journal = tmp_path / "classifiers-v7-reservations.jsonl"
    filler = "sha256:" + "c" * 64
    gate = campaign["JournalGate"](journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL)
    for index in range(45):
        gate.context.source_uid = (f"job-{index:03d}", f"trial-{index:03d}")
        gate.context.grader_digest = filler
        gate.charge()
    for job, trial, digest in sorted(campaign["RETRY_GRANT_CELLS"]):
        gate.context.source_uid = (job, trial)
        gate.context.grader_digest = digest
        gate.charge()
    prefix = journal.read_bytes()
    # Tests pin their own isolated journal prefix into the runpy module
    # globals; they never read the real user journal the production pin binds.
    isolated_prefix = {
        "rows": 49, "size_bytes": len(prefix),
        "sha256": "sha256:" + hashlib.sha256(prefix).hexdigest(), "reserved_usd": "3.2634",
    }
    monkeypatch.setitem(
        campaign["retry_grant"].__globals__, "RETRY_GRANT_PREFIX", isolated_prefix,
    )
    approval_file = tmp_path / "approval.txt"
    approval_file.write_text("Isolated combined-authority fixture, not a real approval.\n")
    declared_grant = {
        "schema": "heldout-v7.retry-grant/v1", "grant_id": GRANT_ID,
        "approval": "Isolated retry-grant fixture, not a real approval.",
        "expires_at": "2026-10-18T10:00:00+00:00",
        "journal_prefix": {**isolated_prefix, "reserved_usd": 3.2634},
        "entries": [{"task_id": campaign["RETRY_GRANT_TASK"], "source_uid": [job, trial],
                     "grader_digest": digest} for job, trial, digest in sorted(campaign["RETRY_GRANT_CELLS"])],
    }
    value = {
        "schema": "mimo.phase2-envelope/v1", "admitted": True, "approval": JOURNAL_APPROVAL,
        "ledger_tag": "2026-10-17-v7", "expires_at": "2026-10-18T10:00:00+00:00",
        "approved_usd": 14.5, "require_envelope_sum_at_most_usd": 14.4998,
        "aggregate_authority": {
            "schema": "mimo.phase2-authority/v1", "ledger_tag": "2026-10-17-v7",
            "authorization_id": WINDOW2_AUTHORITY,
            "text": "Isolated fixture naming the 2026-10-18 11AM ET window2 cumulative $14.50 approval.",
            "approved_usd_at_most": 14.5, "training_authorized": False,
        },
        "serving": {"cap_usd": 4.47}, "native": {"cap_usd": 6.50},
        "heldout_regrade": {
            "cap_usd": 3.5298, "usd_per_execution": 0.0666,
            "durable_journal_path": str(journal), "reset_authorized": False,
            "canonical_reuses_exact_measurements_only": True,
            "retry_grant": declared_grant,
        },
        "training_reservation_usd": 0,
        "sources": {"approval": campaign["file_ref"](approval_file)},
    }
    path = tmp_path / "budget-envelope.json"
    path.write_text(json.dumps(value))
    grant = campaign["retry_grant"](value, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW)
    return {"envelope": path, "value": value, "journal": journal, "prefix": prefix, "grant": grant}


def test_retry_grant_is_opt_in_and_off_by_default(campaign, budget_envelope):
    path, value, journal = budget_envelope
    assert campaign["retry_grant"](
        value, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW,
    ) is None
    admitted, _ = campaign["checked_budget"](
        path, journal=journal, usd_per_execution=0.0666, now=BUDGET_NOW,
    )
    assert "retry_grant" not in admitted["heldout_regrade"]


def test_window2_authority_admits_raised_aggregate_and_the_four_authorized_cells(campaign, granted_budget):
    admitted, reference = campaign["checked_budget"](
        granted_budget["envelope"], journal=granted_budget["journal"],
        usd_per_execution=0.0666, now=BUDGET_NOW,
    )
    assert admitted == granted_budget["value"]
    assert reference == campaign["file_ref"](granted_budget["envelope"])
    grant = granted_budget["grant"]
    assert grant["grant_id"] == GRANT_ID
    assert grant["cells"] == campaign["RETRY_GRANT_CELLS"]
    assert grant["prefix"]["reserved_usd"] == "3.2634"


@pytest.mark.parametrize("change", [
    {"approved_usd": 14.51},
    {"approved_usd": 14.5, "require_envelope_sum_at_most_usd": 14.51},
    {"approved_usd": 12.0, "require_envelope_sum_at_most_usd": 12.0},
    {"serving": {"cap_usd": 4.5}},
])
def test_raised_authority_still_bounds_the_parent_totals(campaign, granted_budget, change):
    value = copy.deepcopy(granted_budget["value"])
    value.update(change)
    granted_budget["envelope"].write_text(json.dumps(value))
    prior = granted_budget["journal"].read_bytes()
    with pytest.raises(ValueError):
        campaign["checked_budget"](
            granted_budget["envelope"], journal=granted_budget["journal"],
            usd_per_execution=0.0666, now=BUDGET_NOW,
        )
    assert granted_budget["journal"].read_bytes() == prior


def test_raised_authority_admits_no_training_reservation(campaign, granted_budget):
    value = copy.deepcopy(granted_budget["value"])
    value["serving"]["cap_usd"] = 4.4
    value["training_reservation_usd"] = 0.01
    granted_budget["envelope"].write_text(json.dumps(value))
    with pytest.raises(ValueError, match="no training"):
        campaign["checked_budget"](
            granted_budget["envelope"], journal=granted_budget["journal"],
            usd_per_execution=0.0666, now=BUDGET_NOW,
        )


@pytest.mark.parametrize("change", [
    {"schema": "mimo.phase2-authority/v2"},
    {"ledger_tag": "2026-10-18-v7"},
    {"authorization_id": "trainer-finish-data-20261018-window-1"},
    {"authorization_id": ""},
    {"authorization_id": None},
    {"authorization_id": True},
    {"text": " "},
    {"training_authorized": True},
    {"approved_usd_at_most": 14.51},
    {"approved_usd_at_most": 12.0},
    {"approved_usd_at_most": 0},
])
def test_raised_aggregate_requires_the_exact_current_authority(campaign, granted_budget, change):
    value = copy.deepcopy(granted_budget["value"])
    value["aggregate_authority"].update(change)
    granted_budget["envelope"].write_text(json.dumps(value))
    prior = granted_budget["journal"].read_bytes()
    with pytest.raises(ValueError):
        campaign["checked_budget"](
            granted_budget["envelope"], journal=granted_budget["journal"],
            usd_per_execution=0.0666, now=BUDGET_NOW,
        )
    assert granted_budget["journal"].read_bytes() == prior


@pytest.mark.parametrize("field", ["aggregate_authority", "authorization_id"])
def test_raised_approval_without_current_authority_refuses(campaign, granted_budget, field):
    value = copy.deepcopy(granted_budget["value"])
    if field == "aggregate_authority":
        del value["aggregate_authority"]
    else:
        del value["aggregate_authority"]["authorization_id"]
    granted_budget["envelope"].write_text(json.dumps(value))
    with pytest.raises(ValueError):
        campaign["checked_budget"](
            granted_budget["envelope"], journal=granted_budget["journal"],
            usd_per_execution=0.0666, now=BUDGET_NOW,
        )


def _mutated_grant(value: dict, name: str) -> None:
    grant = value["heldout_regrade"]["retry_grant"]
    if name == "wrong_uid":
        entry = dict(grant["entries"][0])
        entry["source_uid"] = ["00000000-0000-0000-0000-000000000000", entry["source_uid"][1]]
        grant["entries"][0] = entry
    elif name == "wrong_grader":
        grant["entries"][0]["grader_digest"] = "sha256:" + "0" * 64
    elif name == "wrong_task":
        grant["entries"][0]["task_id"] = "mimo-v2.6-rl/format-code-task-000001"
    elif name == "missing_entry":
        grant["entries"].pop()
    elif name == "duplicate_entry":
        grant["entries"].append(dict(grant["entries"][0]))
    elif name == "wrong_identity":
        grant["grant_id"] = "not-a-canonical-uuid"
    elif name == "noncanonical_identity":
        grant["grant_id"] = GRANT_ID.upper()
    elif name == "grant_after_parent":
        grant["expires_at"] = "2026-10-18T10:00:01+00:00"
    elif name == "expired_grant":
        grant["expires_at"] = "2026-10-18T09:00:00+00:00"
    elif name == "naive_deadline":
        grant["expires_at"] = "2026-10-18T09:59:00"
    elif name == "no_approval":
        grant["approval"] = " "
    elif name == "wrong_schema":
        grant["schema"] = "heldout-v7.retry-grant/v2"
    elif name == "wrong_prefix_sha":
        grant["journal_prefix"]["sha256"] = "sha256:" + "0" * 64
    elif name == "wrong_prefix_size":
        grant["journal_prefix"]["size_bytes"] += 1
    elif name == "wrong_prefix_rows":
        grant["journal_prefix"]["rows"] = 48
    elif name == "wrong_prefix_reserved":
        grant["journal_prefix"]["reserved_usd"] = 3.0
    else:
        raise AssertionError(name)


@pytest.mark.parametrize("mutation", [
    "wrong_uid", "wrong_grader", "wrong_task", "missing_entry", "duplicate_entry",
    "wrong_identity", "noncanonical_identity", "grant_after_parent", "expired_grant",
    "naive_deadline", "no_approval", "wrong_schema",
    "wrong_prefix_sha", "wrong_prefix_size", "wrong_prefix_rows", "wrong_prefix_reserved",
])
def test_retry_grant_fails_closed_on_any_changed_binding(campaign, granted_budget, mutation):
    value = copy.deepcopy(granted_budget["value"])
    _mutated_grant(value, mutation)
    granted_budget["envelope"].write_text(json.dumps(value))
    prior = granted_budget["journal"].read_bytes()
    with pytest.raises(ValueError):
        campaign["checked_budget"](
            granted_budget["envelope"], journal=granted_budget["journal"],
            usd_per_execution=0.0666, now=BUDGET_NOW,
        )
    assert granted_budget["journal"].read_bytes() == prior


def test_retry_grant_requires_the_actual_journal_prefix(campaign, granted_budget, tmp_path):
    value = copy.deepcopy(granted_budget["value"])
    other = tmp_path / "other-journal.jsonl"
    gate = campaign["JournalGate"](other, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL)
    for index in range(49):
        gate.context.source_uid = (f"job-{index:03d}", f"trial-{index:03d}")
        gate.context.grader_digest = "sha256:" + "c" * 64
        gate.charge()
    value["heldout_regrade"]["durable_journal_path"] = str(other)
    granted_budget["envelope"].write_text(json.dumps(value))
    with pytest.raises(ValueError):
        campaign["checked_budget"](
            granted_budget["envelope"], journal=other,
            usd_per_execution=0.0666, now=BUDGET_NOW,
        )


def test_grant_permits_exactly_one_retry_per_authorized_cell(campaign, granted_budget):
    journal, grant, prefix = (granted_budget[name] for name in ("journal", "grant", "prefix"))
    gate = campaign["JournalGate"](
        journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL, grant=grant, clock=_grant_clock,
    )
    assert gate.executions == 49
    for job, trial, digest in sorted(grant["cells"]):
        # The granted cell's original reservation no longer masks its one retry.
        assert not gate.is_reserved((job, trial), digest)
        gate.context.source_uid = (job, trial)
        gate.context.grader_digest = digest
        gate.charge()
    after = journal.read_bytes()
    assert after.startswith(prefix)
    assert gate.executions == 53
    assert gate.to_json()["reserved_usd"] == pytest.approx(3.5298)
    entries = [json.loads(line) for line in after.decode().splitlines()]
    assert len(entries) == 53
    assert [entry["execution"] for entry in entries] == list(range(1, 54))
    granted_rows = entries[49:]
    assert all(row["retry_grant"] == GRANT_ID for row in granted_rows)
    assert [row["reserved_usd"] for row in granted_rows] == [3.33, 3.3966, 3.4632, 3.5298]
    # The four retries total exactly $0.2664; a 54th reservation exceeds the lane cap.
    gate.context.source_uid = ("job-extra", "trial-extra")
    gate.context.grader_digest = "sha256:" + "c" * 64
    with pytest.raises(SpendCapReached, match="cap is exhausted"):
        gate.charge()
    # A granted cell can never take a third reservation; a higher synthetic
    # cap probe isolates the one-use refusal from the exhausted lane cap.
    job, trial, digest = sorted(grant["cells"])[0]
    probe = campaign["JournalGate"](
        journal, 0.0666, cap_usd=4.0, approval=JOURNAL_APPROVAL, grant=grant, clock=_grant_clock,
    )
    probe.context.source_uid = (job, trial)
    probe.context.grader_digest = digest
    with pytest.raises(SpendCapReached, match="already reserved"):
        probe.charge()
    assert journal.read_bytes() == after


def test_consumed_grant_retry_never_refreshes_across_restart(campaign, granted_budget):
    journal, grant, prefix = (granted_budget[name] for name in ("journal", "grant", "prefix"))
    job, trial, digest = sorted(grant["cells"])[0]
    gate = campaign["JournalGate"](
        journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL, grant=grant, clock=_grant_clock,
    )
    gate.context.source_uid = (job, trial)
    gate.context.grader_digest = digest
    gate.charge()
    after_one = journal.read_bytes()

    restarted = campaign["JournalGate"](
        journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL, grant=grant, clock=_grant_clock,
    )
    assert restarted.executions == 50
    # A consumed retry stays reserved: a crash after the reservation remains unknown.
    assert restarted.is_reserved((job, trial), digest)
    restarted.context.source_uid = (job, trial)
    restarted.context.grader_digest = digest
    with pytest.raises(SpendCapReached, match="already reserved"):
        restarted.charge()
    # The other three authorized cells remain eligible exactly once.
    for other_job, other_trial, other_digest in sorted(grant["cells"])[1:]:
        assert not restarted.is_reserved((other_job, other_trial), other_digest)
    assert journal.read_bytes() == after_one


def test_grant_deadline_is_rechecked_at_the_actual_charge(campaign, granted_budget):
    journal, grant, prefix = (granted_budget[name] for name in ("journal", "grant", "prefix"))
    expired = datetime(2026, 10, 18, 10, tzinfo=UTC)  # at the grant deadline
    gate = campaign["JournalGate"](
        journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL, grant=grant,
        clock=lambda: expired,
    )
    job, trial, digest = sorted(grant["cells"])[0]
    gate.context.source_uid = (job, trial)
    gate.context.grader_digest = digest
    with pytest.raises(SpendCapReached, match="expired"):
        gate.charge()
    assert journal.read_bytes() == prefix


def _forged_suffix_row(uid: tuple[str, str], digest: str, execution: int, provenance: str | None) -> bytes:
    entry = {
        "ledger_tag": "2026-10-17-v7", "source_uid": list(uid), "grader_digest": digest,
        "execution": execution, "usd_per_execution": 0.0666,
        "reserved_usd": round(execution * 0.0666, 8),
    }
    if provenance is not None:
        entry["retry_grant"] = provenance
    return (json.dumps(entry, sort_keys=True, allow_nan=False) + "\n").encode()


@pytest.mark.parametrize("forgery", [
    "missing_provenance", "wrong_identity", "provenance_on_new_key", "wrong_grader", "consumed_twice",
    "unmarked_fresh",
])
def test_grant_suffix_rows_require_exact_provenance(campaign, granted_budget, tmp_path, forgery):
    grant = granted_budget["grant"]
    journal = tmp_path / "journal.jsonl"
    journal.write_bytes(granted_budget["prefix"])
    job, trial, digest = sorted(grant["cells"])[0]
    if forgery == "missing_provenance":
        journal.write_bytes(granted_budget["prefix"] + _forged_suffix_row((job, trial), digest, 50, None))
    elif forgery == "wrong_identity":
        journal.write_bytes(
            granted_budget["prefix"] + _forged_suffix_row((job, trial), digest, 50, GRANT_FOREIGN_ID))
    elif forgery == "provenance_on_new_key":
        journal.write_bytes(
            granted_budget["prefix"] + _forged_suffix_row(("job-new", "trial-new"), "sha256:" + "c" * 64, 50, GRANT_ID))
    elif forgery == "wrong_grader":
        journal.write_bytes(
            granted_budget["prefix"] + _forged_suffix_row((job, trial), "sha256:" + "0" * 64, 50, GRANT_ID))
    elif forgery == "unmarked_fresh":
        journal.write_bytes(
            granted_budget["prefix"] + _forged_suffix_row(("job-new", "trial-new"), "sha256:" + "c" * 64, 50, None))
    else:
        journal.write_bytes(
            granted_budget["prefix"]
            + _forged_suffix_row((job, trial), digest, 50, GRANT_ID)
            + _forged_suffix_row((job, trial), digest, 51, GRANT_ID))
    with pytest.raises(ValueError, match="invalid cumulative reservation journal"):
        campaign["JournalGate"](
            journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL, grant=grant, clock=_grant_clock,
        )


@pytest.mark.parametrize("damage", ["truncate", "flip_byte"])
def test_changed_grant_prefix_fails_closed(campaign, granted_budget, tmp_path, damage):
    journal = tmp_path / "journal.jsonl"
    journal.write_bytes(granted_budget["prefix"])
    if damage == "truncate":
        journal.write_bytes(granted_budget["prefix"][:-2])
    else:
        journal.write_bytes(granted_budget["prefix"][:10] + b"XX" + granted_budget["prefix"][12:])
    with pytest.raises(ValueError):
        campaign["JournalGate"](
            journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL,
            grant=granted_budget["grant"], clock=_grant_clock,
        )


def test_provenance_history_replays_without_grant_but_stays_reserved(campaign, granted_budget, tmp_path):
    # After the grant window the journal must remain valid history: a grant-free
    # gate replays provenance-marked duplicates as consumed and never reuses them.
    journal = tmp_path / "journal.jsonl"
    job, trial, digest = sorted(granted_budget["grant"]["cells"])[0]
    journal.write_bytes(
        granted_budget["prefix"] + _forged_suffix_row((job, trial), digest, 50, GRANT_ID),
    )
    gate = campaign["JournalGate"](journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL)
    assert gate.executions == 50
    assert gate.is_reserved((job, trial), digest)
    gate.context.source_uid = (job, trial)
    gate.context.grader_digest = digest
    with pytest.raises(SpendCapReached, match="already reserved"):
        gate.charge()
    # An unmarked duplicate remains malformed history even without a grant.
    broken = tmp_path / "broken.jsonl"
    broken.write_bytes(
        granted_budget["prefix"] + _forged_suffix_row((job, trial), digest, 50, None),
    )
    with pytest.raises(ValueError, match="invalid cumulative reservation journal"):
        campaign["JournalGate"](broken, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL)


def test_grant_cells_must_be_original_prefix_reservations(campaign, granted_budget, tmp_path, monkeypatch):
    journal = tmp_path / "foreign.jsonl"
    gate = campaign["JournalGate"](journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL)
    for index in range(49):
        gate.context.source_uid = (f"job-{index:03d}", f"trial-{index:03d}")
        gate.context.grader_digest = "sha256:" + "c" * 64
        gate.charge()
    prefix = journal.read_bytes()
    # Repin the isolated constants onto a foreign journal that never reserved
    # the authorized cells; the granted cells must already be prefix history.
    foreign_prefix = {
        "rows": 49, "size_bytes": len(prefix),
        "sha256": "sha256:" + hashlib.sha256(prefix).hexdigest(), "reserved_usd": "3.2634",
    }
    monkeypatch.setitem(
        campaign["JournalGate"].__init__.__globals__, "RETRY_GRANT_PREFIX", foreign_prefix,
    )
    grant = {**granted_budget["grant"], "prefix": foreign_prefix}
    with pytest.raises(ValueError, match="original prefix reservations"):
        campaign["JournalGate"](
            journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL, grant=grant, clock=_grant_clock,
        )


def test_granted_retry_supersedes_only_unmeasured_original_observations(campaign, tmp_path):
    case = tmp_path / "case"
    case.mkdir()
    grant = {"cells": campaign["RETRY_GRANT_CELLS"], "grant_id": GRANT_ID}
    key = sorted(grant["cells"])[0]
    unmeasured = {"v7_outcome": None, "v7_error": "interrupted_after_reservation_requires_native_evidence_reconciliation",
                  "native_evidence": {"files": {}}}
    retained, fresh = campaign["_case_paths"](case, key, grant)
    assert retained == case / "observation.json" == fresh
    reference = campaign["create_json"](case / "observation.json", unmeasured)
    campaign["create_json"](case / "observation-ref.json", reference)
    retained, fresh = campaign["_case_paths"](case, key, grant)
    assert retained == case / "observation.json"
    assert fresh == case / f"observation.retry-{GRANT_ID}.json"
    assert campaign["_masked_by_grant"](unmeasured, key, grant)
    assert not campaign["_masked_by_grant"]({**unmeasured, "v7_outcome": False}, key, grant)
    retry_observation = {**unmeasured, "granted_retry": {"grant_id": GRANT_ID}}
    assert not campaign["_masked_by_grant"](retry_observation, key, grant)
    reference = campaign["create_json"](case / f"observation.retry-{GRANT_ID}.json", retry_observation)
    campaign["create_json"](case / f"observation.retry-{GRANT_ID}-ref.json", reference)
    retained, _ = campaign["_case_paths"](case, key, grant)
    assert retained.name == f"observation.retry-{GRANT_ID}.json"
    loaded = campaign["_retained_observation"](retained)
    assert loaded["granted_retry"]["grant_id"] == GRANT_ID
    # A cell outside the grant keeps the single original observation path.
    foreign = tmp_path / "foreign-case"
    foreign.mkdir()
    retained, fresh = campaign["_case_paths"](foreign, ("job", "trial", "sha256:" + "0" * 64), grant)
    assert retained == foreign / "observation.json" == fresh


def test_retry_pointer_companion_does_not_block_restart_retention(campaign, tmp_path):
    # The post-write case dir holds observation.retry-<grant_id>.json plus its
    # observation.retry-<grant_id>-ref.json hash pointer; a restart's _case_paths
    # must select the real retry observation, never refuse on two glob matches.
    case = tmp_path / "case"
    case.mkdir()
    grant = {"cells": campaign["RETRY_GRANT_CELLS"], "grant_id": GRANT_ID}
    key = sorted(grant["cells"])[0]
    original = {"source_uid": list(key[:2]), "v7_outcome": None,
                "v7_error": "interrupted_after_reservation_requires_native_evidence_reconciliation",
                "native_evidence": {"files": {}}}
    reference = campaign["create_json"](case / "observation.json", original)
    campaign["create_json"](case / "observation-ref.json", reference)
    retry_observation = {**original, "v7_outcome": True, "granted_retry": {"grant_id": GRANT_ID}}
    reference = campaign["create_json"](case / f"observation.retry-{GRANT_ID}.json", retry_observation)
    campaign["create_json"](case / f"observation.retry-{GRANT_ID}-ref.json", reference)
    retained, _ = campaign["_case_paths"](case, key, grant)
    assert retained == case / f"observation.retry-{GRANT_ID}.json"
    loaded = campaign["_retained_observation"](retained)
    assert loaded["v7_outcome"] is True
    assert loaded["granted_retry"]["grant_id"] == GRANT_ID
    # The pointer exclusion never hides a second genuine retry observation.
    foreign = case / f"observation.retry-{GRANT_FOREIGN_ID}.json"
    reference = campaign["create_json"](foreign, retry_observation)
    campaign["create_json"](case / f"observation.retry-{GRANT_FOREIGN_ID}-ref.json", reference)
    with pytest.raises(ValueError, match="multiple retained retry observations"):
        campaign["_case_paths"](case, key, grant)


def _granted_suffix(cells, provenance: str) -> bytes:
    return b"".join(
        _forged_suffix_row((job, trial), digest, 50 + index, provenance)
        for index, (job, trial, digest) in enumerate(sorted(cells))
    )


@pytest.mark.parametrize("identity", ["same", "new"])
def test_expanded_prefix_cannot_authorize_a_third_use(campaign, granted_budget, tmp_path, identity):
    """An internally consistent expanded 53-row prefix — even under a fresh
    grant identity — never reopens the pinned original49 grant."""
    grant, prefix = granted_budget["grant"], granted_budget["prefix"]
    journal = tmp_path / "journal.jsonl"
    journal.write_bytes(prefix + _granted_suffix(grant["cells"], GRANT_ID))
    prior = journal.read_bytes()
    value = copy.deepcopy(granted_budget["value"])
    declared = value["heldout_regrade"]["retry_grant"]
    declared["journal_prefix"] = {
        "rows": 53, "size_bytes": len(prior),
        "sha256": "sha256:" + hashlib.sha256(prior).hexdigest(), "reserved_usd": 3.5298,
    }
    if identity == "new":
        declared["grant_id"] = GRANT_FOREIGN_ID
    value["heldout_regrade"]["durable_journal_path"] = str(journal)
    granted_budget["envelope"].write_text(json.dumps(value))
    with pytest.raises(ValueError):
        campaign["checked_budget"](
            granted_budget["envelope"], journal=journal,
            usd_per_execution=0.0666, now=BUDGET_NOW,
        )
    assert journal.read_bytes() == prior


def test_changed_grant_identity_cannot_take_a_third_reservation(campaign, granted_budget, tmp_path):
    grant, prefix = granted_budget["grant"], granted_budget["prefix"]
    journal = tmp_path / "journal.jsonl"
    journal.write_bytes(prefix + _granted_suffix(grant["cells"], GRANT_ID))
    prior = journal.read_bytes()
    foreign = {**grant, "grant_id": GRANT_FOREIGN_ID}
    with pytest.raises(ValueError, match="invalid cumulative reservation journal"):
        campaign["JournalGate"](
            journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL,
            grant=foreign, clock=_grant_clock,
        )
    gate = campaign["JournalGate"](
        journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL,
        grant=grant, clock=_grant_clock,
    )
    assert gate.executions == 53
    job, trial, digest = sorted(grant["cells"])[0]
    gate.context.source_uid = (job, trial)
    gate.context.grader_digest = digest
    # The exact 3.5298 lane cap refuses any further reservation first.
    with pytest.raises(SpendCapReached, match="cap is exhausted"):
        gate.charge()
    # A higher synthetic cap isolates the one-use refusal: the consumed cell
    # stays reserved independent of the cap boundary.
    probe = campaign["JournalGate"](
        journal, 0.0666, cap_usd=4.0, approval=JOURNAL_APPROVAL,
        grant=grant, clock=_grant_clock,
    )
    probe.context.source_uid = (job, trial)
    probe.context.grader_digest = digest
    with pytest.raises(SpendCapReached, match="already reserved"):
        probe.charge()
    assert journal.read_bytes() == prior


def test_sealed_prefix_history_never_carries_retry_provenance(
    campaign, granted_budget, tmp_path, monkeypatch,
):
    grant = granted_budget["grant"]
    job, trial, digest = sorted(grant["cells"])[0]
    lines = granted_budget["prefix"].splitlines(keepends=True)
    tampered = b"".join(lines[:48]) + _forged_suffix_row((job, trial), digest, 49, GRANT_ID)
    journal = tmp_path / "journal.jsonl"
    journal.write_bytes(tampered)
    # Even when the declared pin is repointed onto this tampered head, a
    # provenance-marked duplicate inside the original49 stays malformed.
    sealed = {
        "rows": 49, "size_bytes": len(tampered),
        "sha256": "sha256:" + hashlib.sha256(tampered).hexdigest(), "reserved_usd": "3.2634",
    }
    monkeypatch.setitem(campaign["JournalGate"].__init__.__globals__, "RETRY_GRANT_PREFIX", sealed)
    with pytest.raises(ValueError, match="invalid cumulative reservation journal"):
        campaign["JournalGate"](
            journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL,
            grant={**grant, "prefix": sealed}, clock=_grant_clock,
        )


@pytest.mark.parametrize("forgery", [
    "foreign_prefix", "non_granted_key", "third_duplicate", "changed_identity", "inside_prefix",
])
def test_grant_free_replay_keeps_default_refusal_for_arbitrary_provenance(
    campaign, granted_budget, tmp_path, forgery,
):
    grant, prefix = granted_budget["grant"], granted_budget["prefix"]
    cells = sorted(grant["cells"])
    job, trial, digest = cells[0]
    journal = tmp_path / "journal.jsonl"
    if forgery == "foreign_prefix":
        foreign = tmp_path / "foreign.jsonl"
        gate = campaign["JournalGate"](foreign, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL)
        for index in range(48):
            gate.context.source_uid = (f"job-{index:03d}", f"trial-{index:03d}")
            gate.context.grader_digest = "sha256:" + "c" * 64
            gate.charge()
        gate.context.source_uid = (job, trial)
        gate.context.grader_digest = digest
        gate.charge()
        journal.write_bytes(foreign.read_bytes() + _forged_suffix_row((job, trial), digest, 50, GRANT_ID))
    elif forgery == "non_granted_key":
        journal.write_bytes(
            prefix + _forged_suffix_row(("job-000", "trial-000"), "sha256:" + "c" * 64, 50, GRANT_ID))
    elif forgery == "third_duplicate":
        journal.write_bytes(
            prefix + _forged_suffix_row((job, trial), digest, 50, GRANT_ID)
            + _forged_suffix_row((job, trial), digest, 51, GRANT_ID))
    elif forgery == "changed_identity":
        other = cells[1]
        journal.write_bytes(
            prefix + _forged_suffix_row((job, trial), digest, 50, GRANT_ID)
            + _forged_suffix_row((other[0], other[1]), other[2], 51, GRANT_FOREIGN_ID))
    else:
        lines = prefix.splitlines(keepends=True)
        journal.write_bytes(b"".join(lines[:48]) + _forged_suffix_row((job, trial), digest, 49, GRANT_ID))
    written = journal.read_bytes()
    with pytest.raises(ValueError, match="invalid cumulative reservation journal"):
        campaign["JournalGate"](journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL)
    assert journal.read_bytes() == written


def test_grant_backed_charge_refuses_unrelated_fresh_uids(campaign, granted_budget):
    journal, grant, prefix = (granted_budget[name] for name in ("journal", "grant", "prefix"))
    gate = campaign["JournalGate"](
        journal, 0.0666, cap_usd=3.5298, approval=JOURNAL_APPROVAL, grant=grant, clock=_grant_clock,
    )
    gate.context.source_uid = ("job-unrelated", "trial-unrelated")
    gate.context.grader_digest = "sha256:" + "c" * 64
    with pytest.raises(SpendCapReached, match="authorized"):
        gate.charge()
    gate.context.source_uid = ("job-unrelated", "trial-unrelated")
    gate.context.grader_digest = None
    with pytest.raises(SpendCapReached, match="authorized"):
        gate.charge()
    assert journal.read_bytes() == prefix
