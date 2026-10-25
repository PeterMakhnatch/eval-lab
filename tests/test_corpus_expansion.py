"""Local, inert intake fixtures: no datasets, registry, CAS, network, or solvers."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from evallab.production import corpus_expansion as expansion
from evallab.production.cloud_trace_db import CloudTraceDatabase

USES = ("operator_raw", "actor_retrieval", "scorer", "derivatives", "training", "redistribution")


def _bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _record(number: int = 1) -> dict[str, Any]:
    authority = {"ref": "synthetic-authority:fixture", "sha256": "a" * 64}
    return {
        "source_dataset": "synthetic-source",
        "source_revision": "fixture-revision-1",
        "native_id": f"item-{number}",
        "aliases": [],
        "canonical_identity": {
            "dataset_id": "synthetic-canonical", "item_id": f"canonical-item-{number}",
            "task_id": f"synthetic/task-{number}",
        },
        "family_id": f"synthetic-family-{number}",
        "effective_split": "dev",
        "split_authority": authority,
        "rights": {
            use: {
                "decision": "allow" if use == "operator_raw" else "deny",
                "authority": authority if use == "operator_raw" else None,
            }
            for use in USES
        },
        "reasons": ["Inert fixture authority only; no real source is cleared."],
    }


def _bundle(number: int = 1, *, step_count: int = 2) -> dict[str, Any]:
    task_id = f"synthetic/task-{number}"
    trace_id = f"synthetic/trace-{number}"
    return {
        "task": {
            "task_id": task_id, "dataset_id": "synthetic-canonical", "split": "held",
            "instruction_text": "Inert fixture: describe a colored square.",
            "original_grader_digest": None, "repaired_grader_digest": None, "cas_uri": None,
            "metadata_json": {
                "publisher_split": "DEV", "publisher_label": "positive",
                "source_receipt": {"native_id": f"item-{number}", "notes": ["red", "blue"]},
            },
        },
        "traces": [{
            "trace": {
                "trace_id": trace_id, "run_id": "synthetic/run", "task_id": task_id,
                "model_id": "source-reported:synthetic/model", "outcome": "source_label:unreviewed",
                "reward": 1, "is_exploit": False, "is_task_defect_fail": False,
                "step_count": step_count, "raw_cas_uri": None,
                "verified_receipt_json": {
                    "status": "unverified",
                    "source": {
                        "dataset": "synthetic-source", "revision": "fixture-revision-1",
                        "locator": "https://source.invalid/inert-fixture",
                    },
                    "labels": {"publisher_claim": "positive", "votes": [True, False]},
                },
            },
            "steps": [{
                "trace_id": trace_id, "step_index": index, "role": "assistant",
                "tool_name": None, "command_or_action": f"describe square {index}",
                "content_text": f"Inert note {index}: blue square.",
                "observation_preview": f"Square {index} is blue.", "observation_cas_uri": None,
                "flags_json": {"publisher_note": {"index": index, "source_only": True}},
            } for index in range(step_count)],
        }],
    }


def _stage_pack(
    root: Path, bundles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Also usable by a parent-owned CLI smoke, without invoking pytest."""
    if bundles is None:
        bundles = [_bundle()]
    root.mkdir(parents=True, exist_ok=True)
    objects = []
    records = []
    for number, bundle in enumerate(bundles, 1):
        raw = _bytes(bundle)
        (root / f"bundle-{number}.json").write_bytes(raw)
        record = _record(number)
        records.append(record)
        objects.append({
            **{field: record[field] for field in ("source_dataset", "source_revision", "native_id")},
            "path": f"bundle-{number}.json", "sha256": hashlib.sha256(raw).hexdigest(),
        })
    pack = root / "pack.json"
    clearance = root / "clearance.json"
    pack.write_bytes(_bytes({"schema": expansion.PACK_SCHEMA, "objects": objects}))
    clearance.write_bytes(_bytes({"schema": "evallab.corpus_clearance/v1", "records": records}))
    return {
        "database": root / "staged.sqlite", "pack": pack, "clearance": clearance,
        "clearance_sha256": hashlib.sha256(clearance.read_bytes()).hexdigest(),
        "output": root / "receipt.json",
    }


def _replace_clearance(arguments: dict[str, Any], records: list[dict[str, Any]]) -> None:
    path = arguments["clearance"]
    raw = _bytes({"schema": "evallab.corpus_clearance/v1", "records": records})
    path.write_bytes(raw)
    arguments["clearance_sha256"] = hashlib.sha256(raw).hexdigest()


def _assert_no_import(arguments: dict[str, Any], receipt: dict[str, Any]) -> None:
    assert receipt["status"] in {"denied", "rejected"}
    assert receipt["row_counts_added"] == {
        "corpus_tasks": 0, "task_issues": 0, "agent_traces": 0, "trace_steps": 0,
    }
    assert receipt["admissions"] == dict.fromkeys((*USES, "cloud"), 0)
    assert not arguments["database"].exists()


def test_dry_run_does_not_read_bundles_or_open_database(tmp_path: Path) -> None:
    arguments = _stage_pack(tmp_path)
    (tmp_path / "bundle-1.json").unlink()
    arguments["database"] = tmp_path / "nonexistent-directory" / "staged.sqlite"

    receipt = expansion.import_pack(**arguments)

    assert receipt["status"] == "dry_run"
    assert (receipt["selected"], receipt["cleared"], receipt["denied"]) == (1, 1, 0)
    assert receipt["body_validation"] == "not_performed"
    assert receipt["bundle_bytes_hashed"] == 0
    assert receipt["row_counts_before"] is None
    assert receipt["admissions"] == dict.fromkeys((*USES, "cloud"), 0)
    assert not arguments["database"].parent.exists()
    for field, path in (
        ("pack_sha256", arguments["pack"]),
        ("clearance_sha256", arguments["clearance"]),
        ("importer_sha256", Path(expansion.__file__)),
    ):
        assert receipt[field] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_one_denial_prevents_reads_of_every_selected_object(tmp_path: Path) -> None:
    arguments = _stage_pack(tmp_path, [_bundle(1), _bundle(2)])
    records = [_record(1), _record(2)]
    records[1]["rights"]["operator_raw"] = {"decision": "unknown", "authority": None}
    _replace_clearance(arguments, records)
    # The first identity is allowed. Opening it before gating the second would
    # fail instead of producing the complete, identity-only denial receipt.
    (tmp_path / "bundle-1.json").unlink()
    (tmp_path / "bundle-2.json").unlink()

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)
    assert receipt["status"] == "denied"
    assert (receipt["selected"], receipt["cleared"], receipt["denied"]) == (2, 1, 1)
    assert [entry["decision"] for entry in receipt["objects"]] == ["allow", "deny"]
    assert receipt["bundle_bytes_hashed"] == 0
    assert receipt["body_validation"] == "not_performed"


@pytest.mark.parametrize("missing_authority", [
    "canonical_identity", "family_id", "split_authority", "effective_split", "operator_raw",
])
def test_source_identity_and_publisher_dev_are_not_clearance(
    tmp_path: Path, missing_authority: str,
) -> None:
    arguments = _stage_pack(tmp_path)
    record = _record()
    if missing_authority == "operator_raw":
        record["rights"]["operator_raw"] = {"decision": "allow", "authority": None}
    elif missing_authority == "effective_split":
        record[missing_authority] = "test"
    else:
        record[missing_authority] = None
    _replace_clearance(arguments, [record])
    (tmp_path / "bundle-1.json").unlink()

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)
    assert receipt["bundle_bytes_hashed"] == 0
    assert receipt["body_validation"] == "not_performed"


def test_manifest_pin_failure_precedes_bundle_and_database_access(tmp_path: Path) -> None:
    arguments = _stage_pack(tmp_path)
    arguments["clearance_sha256"] = "0" * 64
    (tmp_path / "bundle-1.json").unlink()

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)
    assert receipt["clearance_sha256"] is None
    assert receipt["bundle_bytes_hashed"] == 0


def test_clearance_must_be_a_regular_file_not_a_symlink(tmp_path: Path) -> None:
    arguments = _stage_pack(tmp_path)
    original = tmp_path / "original-clearance.json"
    arguments["clearance"].rename(original)
    arguments["clearance"].symlink_to(original)

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)
    assert receipt["bundle_bytes_hashed"] == 0
    assert receipt["body_validation"] == "not_performed"


def test_aliases_cannot_select_the_same_canonical_task_twice(tmp_path: Path) -> None:
    arguments = _stage_pack(tmp_path)
    record = _record()
    record["aliases"] = ["source-alias"]
    _replace_clearance(arguments, [record])
    document = json.loads(arguments["pack"].read_bytes())
    document["objects"].append({
        **document["objects"][0], "native_id": "source-alias", "path": "absent-alias.json",
    })
    arguments["pack"].write_bytes(_bytes(document))
    (tmp_path / "bundle-1.json").unlink()

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)
    assert receipt["status"] == "denied"
    assert receipt["denied"] == 1
    assert receipt["bundle_bytes_hashed"] == 0


def test_execute_retains_every_step_source_receipt_and_long_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(step_count=607)
    long_text = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=" * 4000
    bundle["task"]["instruction_text"] += "\n" + long_text
    bundle["traces"][0]["steps"][0].update(
        content_text=long_text, command_or_action=long_text, observation_preview=long_text,
    )
    bundle["traces"][0]["trace"]["verified_receipt_json"]["source"]["refs"] = [
        {"path": "/not-opened/synthetic-source.json", "sha256": "b" * 64},
        {"url": "https://source.invalid/not-fetched", "notes": ["κίτρινο", "青"]},
    ]
    arguments = _stage_pack(tmp_path, [bundle])
    monkeypatch.setenv("EVALLAB_CLOUD_DATABASE_URL", "postgresql://must-not-be-used.invalid/db")

    receipt = expansion.import_pack(**arguments, execute=True)

    assert receipt["status"] == "imported"
    assert receipt["row_counts_added"] == {
        "corpus_tasks": 1, "task_issues": 0, "agent_traces": 1, "trace_steps": 607,
    }
    assert receipt["admissions"] == {**dict.fromkeys((*USES, "cloud"), 0), "operator_raw": 1}
    with CloudTraceDatabase(arguments["database"]) as database:
        assert database.get_task(bundle["task"]["task_id"]) == bundle["task"]
        trace = database.get_trace(bundle["traces"][0]["trace"]["trace_id"])
        assert trace is not None
        assert trace["issues"] == []
        assert trace["steps"] == bundle["traces"][0]["steps"]
        assert {field: trace[field] for field in bundle["traces"][0]["trace"]} == bundle["traces"][0]["trace"]
        assert database.row_counts() == receipt["row_counts_after"]


@pytest.mark.parametrize(("section", "field", "value"), [
    ("task", "task_id", "synthetic/other-task"),
    ("task", "dataset_id", "synthetic-other-dataset"),
    ("task", "split", "DEV"),
    ("task", "extra_source_field", {"must_not_disappear": True}),
    ("trace", "task_id", "synthetic/other-task"),
    ("trace", "model_id", "synthetic/unattributed-model"),
    ("trace", "model_id", "source-reported:unknown"),
    ("trace", "is_exploit", True),
    ("trace", "is_exploit", 0),
    ("trace", "is_task_defect_fail", True),
    ("trace", "step_count", 1),
    ("trace", "reward", 2**53 + 1),
    ("trace", "verified_receipt_json", {"status": "confirmed"}),
    ("trace", "verified_receipt_json", {"status": "unverified", "verified": True}),
    ("trace", "verified_receipt_json", {"status": ["unverified"]}),
    ("step", "trace_id", "synthetic/other-trace"),
    ("step", "content_text", ["must not become a string"]),
    ("step", "step_index", True),
    ("step", "unknown_source_field", "must not disappear"),
])
def test_invalid_or_promoting_rows_reject_the_entire_bundle(
    tmp_path: Path, section: str, field: str, value: Any,
) -> None:
    bundle = _bundle()
    row = {
        "task": bundle["task"], "trace": bundle["traces"][0]["trace"],
        "step": bundle["traces"][0]["steps"][0],
    }[section]
    row[field] = value
    arguments = _stage_pack(tmp_path, [bundle])

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)
    assert receipt["body_validation"] == "failed"


@pytest.mark.parametrize("location", [
    "task", "trace", "step", "nested_metadata", "nested_receipt", "encoded_flags", "text",
])
def test_cas_references_are_rejected_not_dropped_or_followed(tmp_path: Path, location: str) -> None:
    bundle = _bundle()
    reference = "cas://sha256/" + "c" * 64
    if location == "task":
        bundle["task"]["cas_uri"] = "https://source.invalid/not-a-local-cas-object"
    elif location == "trace":
        bundle["traces"][0]["trace"]["raw_cas_uri"] = reference
    elif location == "step":
        bundle["traces"][0]["steps"][0]["observation_cas_uri"] = reference
    elif location == "nested_metadata":
        bundle["task"]["metadata_json"]["source"] = {"raw_cas_uri": "/do-not-open/local-object"}
    elif location == "nested_receipt":
        bundle["traces"][0]["trace"]["verified_receipt_json"]["source"]["refs"] = [reference]
    elif location == "encoded_flags":
        bundle["traces"][0]["steps"][0]["flags_json"] = json.dumps({"ref": reference})
    else:
        bundle["traces"][0]["steps"][0]["content_text"] = "external attachment " + reference
    arguments = _stage_pack(tmp_path, [bundle])

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)
    assert receipt["body_validation"] == "failed"


@pytest.mark.parametrize("duplicate", ["trace", "step"])
def test_duplicate_trace_or_step_id_cannot_replace_source_rows(tmp_path: Path, duplicate: str) -> None:
    bundle = _bundle()
    if duplicate == "trace":
        other = copy.deepcopy(bundle["traces"][0])
        other["steps"][0]["content_text"] = "Different source content for the same trace."
        bundle["traces"].append(other)
    else:
        bundle["traces"][0]["steps"][1]["step_index"] = 0
    arguments = _stage_pack(tmp_path, [bundle])

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)


def test_issue_payload_cannot_be_smuggled_in_as_source_metadata(tmp_path: Path) -> None:
    bundle = _bundle()
    bundle["issues"] = [{"status": "provisional", "publisher_label": "positive"}]
    arguments = _stage_pack(tmp_path, [bundle])

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)


@pytest.mark.parametrize("bad_pack", [
    "schema", "extra_field", "url", "absolute", "traversal", "bad_digest", "duplicate_identity",
])
def test_pack_metadata_rejected_without_reading_bodies(tmp_path: Path, bad_pack: str) -> None:
    arguments = _stage_pack(tmp_path)
    document = json.loads(arguments["pack"].read_bytes())
    item = document["objects"][0]
    if bad_pack == "schema":
        document["schema"] = "unsupported"
    elif bad_pack == "extra_field":
        item["publisher_grants"] = "DEV"
    elif bad_pack in {"url", "absolute", "traversal"}:
        item["path"] = {
            "url": "https://source.invalid/bundle.json", "absolute": "/outside/bundle.json",
            "traversal": "nested/../bundle.json",
        }[bad_pack]
    elif bad_pack == "bad_digest":
        item["sha256"] = "not-a-digest"
    else:
        document["objects"].append({**item, "native_id": "ITEM-1", "path": "other.json"})
    arguments["pack"].write_bytes(_bytes(document))
    (tmp_path / "bundle-1.json").unlink()

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)
    assert receipt["bundle_bytes_hashed"] == 0
    assert receipt["body_validation"] == "not_performed"


def test_bundle_hash_mismatch_rejects_batch_before_database_creation(tmp_path: Path) -> None:
    arguments = _stage_pack(tmp_path, [_bundle(1), _bundle(2)])
    with (tmp_path / "bundle-2.json").open("ab") as handle:
        handle.write(b" ")

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)
    assert receipt["body_validation"] == "failed"


def test_duplicate_json_members_are_not_silently_collapsed(tmp_path: Path) -> None:
    arguments = _stage_pack(tmp_path)
    raw = _bytes(_bundle()).replace(b'"is_exploit": false', b'"is_exploit": true, "is_exploit": false')
    (tmp_path / "bundle-1.json").write_bytes(raw)
    document = json.loads(arguments["pack"].read_bytes())
    document["objects"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
    arguments["pack"].write_bytes(_bytes(document))

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)


@pytest.mark.parametrize("column", ["metadata_json", "verified_receipt_json", "flags_json"])
def test_encoded_json_columns_cannot_hide_duplicate_source_fields(
    tmp_path: Path, column: str,
) -> None:
    bundle = _bundle()
    row = {
        "metadata_json": bundle["task"],
        "verified_receipt_json": bundle["traces"][0]["trace"],
        "flags_json": bundle["traces"][0]["steps"][0],
    }[column]
    row[column] = '{"status":"verified","status":"unverified"}'
    arguments = _stage_pack(tmp_path, [bundle])

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)


def test_oversized_bundle_is_rejected_without_clipping(tmp_path: Path) -> None:
    arguments = _stage_pack(tmp_path)
    os.truncate(tmp_path / "bundle-1.json", expansion.MAX_BUNDLE_BYTES + 1)

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)
    assert receipt["bundle_bytes_hashed"] == 0


@pytest.mark.parametrize("directory_link", [False, True])
def test_listed_bundle_cannot_reach_through_a_symlink(tmp_path: Path, directory_link: bool) -> None:
    arguments = _stage_pack(tmp_path / "pack")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.json").write_bytes(_bytes(_bundle()))
    root = arguments["pack"].parent
    if directory_link:
        (root / "linked").symlink_to(outside, target_is_directory=True)
        relative = "linked/source.json"
    else:
        (root / "linked.json").symlink_to(outside / "source.json")
        relative = "linked.json"
    document = json.loads(arguments["pack"].read_bytes())
    document["objects"][0]["path"] = relative
    arguments["pack"].write_bytes(_bytes(document))

    receipt = expansion.import_pack(**arguments, execute=True)

    _assert_no_import(arguments, receipt)


def test_existing_original_task_is_not_modified_or_extended(tmp_path: Path) -> None:
    bundle = _bundle()
    arguments = _stage_pack(tmp_path, [bundle])
    original = {**bundle["task"], "split": "DEV", "instruction_text": "Original local row."}
    with CloudTraceDatabase(arguments["database"]) as database:
        database.upsert_task(original)
        before = database.row_counts()

    receipt = expansion.import_pack(**arguments, execute=True)

    assert receipt["status"] == "rejected"
    with CloudTraceDatabase(arguments["database"]) as database:
        assert database.get_task(original["task_id"]) == original
        assert database.row_counts() == before
        assert database.get_trace(bundle["traces"][0]["trace"]["trace_id"]) is None


def test_existing_trace_rejects_all_new_tasks_without_overwrite(tmp_path: Path) -> None:
    arguments = _stage_pack(tmp_path, [_bundle(1), _bundle(2)])
    original = _bundle(3)
    original["traces"][0]["trace"]["trace_id"] = "synthetic/trace-2"
    for step in original["traces"][0]["steps"]:
        step["trace_id"] = "synthetic/trace-2"
    with CloudTraceDatabase(arguments["database"]) as database:
        database.upsert_task(original["task"])
        database.upsert_trace(original["traces"][0]["trace"], steps=original["traces"][0]["steps"])
        before = database.row_counts()
        original_trace = database.get_trace("synthetic/trace-2")

    receipt = expansion.import_pack(**arguments, execute=True)

    assert receipt["status"] == "rejected"
    with CloudTraceDatabase(arguments["database"]) as database:
        assert database.row_counts() == before
        assert database.get_task("synthetic/task-1") is None
        assert database.get_task("synthetic/task-2") is None
        assert database.get_trace("synthetic/trace-2") == original_trace


def test_database_write_failure_rolls_back_the_complete_batch(tmp_path: Path) -> None:
    arguments = _stage_pack(tmp_path, [_bundle(1), _bundle(2)])
    with CloudTraceDatabase(arguments["database"]) as database:
        database._execute(
            "CREATE TRIGGER refuse_fixture BEFORE INSERT ON agent_traces "
            "WHEN NEW.trace_id = 'synthetic/trace-2' "
            "BEGIN SELECT RAISE(ABORT, 'inert fixture rejection'); END",
        )
        before = database.row_counts()

    receipt = expansion.import_pack(**arguments, execute=True)

    assert receipt["status"] == "rejected"
    assert receipt["row_counts_added"] == dict.fromkeys(before, 0)
    with CloudTraceDatabase(arguments["database"]) as database:
        assert database.row_counts() == before


def test_receipt_is_not_overwritten_even_for_an_execute_request(tmp_path: Path) -> None:
    arguments = _stage_pack(tmp_path)
    expansion.import_pack(**arguments)
    original_receipt = arguments["output"].read_bytes()
    (tmp_path / "bundle-1.json").unlink()

    with pytest.raises(FileExistsError):
        expansion.import_pack(**arguments, execute=True)

    assert arguments["output"].read_bytes() == original_receipt
    assert not arguments["database"].exists()


@pytest.mark.parametrize("database", ["postgresql://db.invalid/store", "sqlite:///local.sqlite", ":memory:"])
def test_cli_refuses_remote_or_implicit_database_targets(tmp_path: Path, database: str) -> None:
    arguments = _stage_pack(tmp_path)
    arguments["database"] = database

    result = expansion.main(_cli_arguments(arguments))

    assert result == 2
    assert not arguments["output"].exists()


def _cli_arguments(arguments: dict[str, Any]) -> list[str]:
    return [
        part for key, value in arguments.items()
        for part in ("--" + key.replace("_", "-"), str(value))
    ]


def test_cli_defaults_to_metadata_and_requires_execute_for_rows(tmp_path: Path) -> None:
    arguments = _stage_pack(tmp_path)

    assert expansion.main(_cli_arguments(arguments)) == 0
    assert not arguments["database"].exists()
    assert json.loads(arguments["output"].read_bytes())["status"] == "dry_run"
    arguments["output"] = tmp_path / "executed-receipt.json"
    assert expansion.main([*_cli_arguments(arguments), "--execute"]) == 0

    with CloudTraceDatabase(arguments["database"]) as database:
        assert database.get_task("synthetic/task-1") == _bundle()["task"]
        trace = database.get_trace("synthetic/trace-1")
        assert trace is not None
        assert trace["steps"] == _bundle()["traces"][0]["steps"]
    assert json.loads(arguments["output"].read_bytes())["status"] == "imported"
