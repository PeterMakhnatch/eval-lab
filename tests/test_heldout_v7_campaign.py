from __future__ import annotations

import csv
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
