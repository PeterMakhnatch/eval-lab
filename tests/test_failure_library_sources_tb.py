from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from evallab.cli import run_cli
from evallab.failure_library.commands import ingest
from evallab.failure_library.sources import collection_builders, json_rows, mimo, normalize_split
from evallab.failure_library.sources.mimo import (
    KEY as MIMO_KEY,
)
from evallab.failure_library.sources.mimo import (
    MIMO_REVISION,
    build_mimo_weak,
    defect_map_records,
)
from evallab.failure_library.sources.tb_family import (
    KEYS,
    PORT_REVISION,
    TB4_REVISION,
    build_pm_tb4,
    build_tb2_fixes,
    build_tb21_weak,
    envcheck_records,
)
from evallab.failure_library.store import LibraryStore

ROOT = Path(__file__).resolve().parents[1]


def _write_rows(root: Path, relative: Path, rows: list[dict[str, Any]]) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_pm_preserves_source_claims_benchmark_scope_and_ten_dev_receipts() -> None:
    findings = build_pm_tb4(ROOT)
    originals = {row["item_id"]: row for row in json_rows(ROOT / KEYS / "tb4-audit-v1/items.jsonl")}
    assert len(findings) == 241
    assert Counter(f.tier for f in findings) == {"V1_external_label": 231, "V3_executed": 10}
    for finding in findings:
        item = originals[finding.locator]
        assert finding.split == normalize_split(item["split"])
        assert finding.task.upstream_revision == TB4_REVISION
        assert finding.evidence[0].label_source == "Preference Model"
        assert finding.status_now == "unknown"  # Karotte port != Harbor upstream.
        assert any(PORT_REVISION in (e.note or "") for e in finding.evidence)
        if finding.tier == "V3_executed":
            assert finding.split == "DEV"
            receipt = next(e for e in finding.evidence if e.kind == "executed")
            assert "witnesses.json#" in (receipt.receipt_ref or "")
        if item["scope"] == "benchmark":
            assert finding.task.task_id == "__benchmark__/terminal-bench-4"
            assert finding.task.task_version_digest is None
            assert finding.task.task_path is None


def _envcheck_report() -> dict[str, Any]:
    # Exact public EnvCheck record shape and actual receipt identity, reduced to
    # fields consumed by intake; no reproducer code is copied into the fixture.
    return {
        "post": "2026-10-grader-defects",
        "slug": "anonymization-large-input-determinism-unchecked",
        "files": "posts/2026-10-grader-defects/findings/anonymization-large-input-determinism-unchecked/",
        "discovered_by": {"date": "2026-09-29"},
        "defect": {"family": "insufficient-checking"},
        "cases": [
            {
                "label": "urandom-noise-on-full-input",
                "task": "data-anonymization",
                "shows": "incorrect submission, accepted",
                "grader_verdict": {"reward": 1.0, "status": "graded"},
            }
        ],
        "evidence": [
            {
                "artifact_kind": "grader_output",
                "type": "reproduction",
                "subject": "urandom-noise-on-full-input",
                "file": "evidence/urandom-noise-on-full-input/urandom-noise-on-full-input-grader-output.json",
                "sha256": "a8180c3f7a6b071a6a5ba59e19c1181c25ec75566744924b8285e650fcb429f2",
            }
        ],
    }


def test_envcheck_admits_only_receipt_matched_defects_and_issue_read_epoch_flags() -> None:
    all_items = json_rows(ROOT / KEYS / "envcheck-tb4-39/items.jsonl")
    item = copy.deepcopy(
        next(row for row in all_items if row["package"]["name"] == "data-anonymization")
    )
    item["root_cause"] = [
        next(c for c in item["root_cause"] if c["id"] == _envcheck_report()["slug"])
    ]
    epoch = next(row for row in all_items if row["package"]["name"] == "nextjs-performance")
    negative = next(row for row in all_items if row["expected_verdict"] == "pass")
    findings = envcheck_records([item, epoch, negative], [_envcheck_report()])
    assert len(findings) == 2
    reported, flagged = findings
    assert reported.tier == "V3_executed"
    assert reported.task.upstream_revision == TB4_REVISION
    assert reported.split == normalize_split(item["split"])
    assert reported.defect_class == "TS01"
    assert "/evidence/" in (reported.evidence[0].receipt_ref or "")
    assert flagged.tier == "V1_external_label"
    assert flagged.evidence[0].label_source == "Epoch AI"
    assert flagged.direction == "unfair_fail"
    assert flagged.split == normalize_split(epoch["split"])
    unbound = _envcheck_report()
    unbound["cases"][0]["task"] = "another-task"
    assert envcheck_records([item], [unbound])[0].tier == "V0_unverified"


def test_tb21_four_native_controls_support_fourteen_witnesses_on_five_tasks() -> None:
    findings = build_tb21_weak(ROOT)
    items = {
        row["subject"]["task_id"].removeprefix("tb21/"): row
        for row in json_rows(ROOT / KEYS / "tb21-weak-tests-v1/items.jsonl")
    }
    assert len(findings) == 14
    assert len({f.task.task_id for f in findings}) == 5
    assert all(f.tier == "V3_executed" for f in findings)
    for finding in findings:
        item = items[finding.task.task_id]
        assert finding.split == normalize_split(item["split"])
        assert finding.task.upstream_revision == item["source_pin"]
        assert (ROOT / str(finding.evidence[0].receipt_ref)).is_file()


def test_tb21_missing_receipts_do_not_inherit_executed_strength(tmp_path: Path) -> None:
    key = KEYS / "tb21-weak-tests-v1"
    reported = json_rows(ROOT / key / "findings.jsonl")[0]
    item = next(
        row
        for row in json_rows(ROOT / key / "items.jsonl")
        if row["subject"] == reported["subject"]
    )
    atlas = next(
        row
        for row in json_rows(ROOT / key / "atlas.jsonl")
        if row["task_id"] == "cobol-modernization"
    )
    _write_rows(tmp_path, key / "findings.jsonl", [reported])
    _write_rows(tmp_path, key / "items.jsonl", [item])
    _write_rows(tmp_path, key / "atlas.jsonl", [atlas])
    finding = build_tb21_weak(tmp_path)[0]
    assert finding.tier == "V0_unverified"
    assert finding.split == normalize_split(item["split"])


def test_tb2_version_diff_keeps_both_revisions_and_does_not_guess_resource_defects() -> None:
    findings = build_tb2_fixes(ROOT)
    items = {row["item_id"]: row for row in json_rows(ROOT / KEYS / "tb2-to-tb21-v1/items.jsonl")}
    assert len(findings) == 26
    for finding in findings:
        assert finding.tier == "V2_upstream_fixed"
        assert finding.task.upstream_revision == "69671fbaac6d67a7ef0dfec016cc38a64ef7a77c"
        assert finding.fixed_in_revision == "7131e4375048a0e408a8fb404b5f499d726b695b"
        assert finding.split == normalize_split(items[finding.locator]["split"])
    by_task = {f.task.task_id: f for f in findings}
    assert by_task["polyglot-c-py"].defect_class == "TS07"
    assert by_task["fix-git"].defect_class == "TS03"
    assert by_task["torch-pipeline-parallelism"].defect_class == "unclassified"
    assert by_task["torch-pipeline-parallelism"].direction == "other"


def _mimo_fixture(tmp_path: Path, *, archived_log: bool) -> tuple[Path, Path]:
    # Retain a real atlas row, complete committed patch and key split. Only the
    # large historical log is replaced with its three real-format grade headers.
    row = copy.deepcopy(
        next(
            r
            for r in json_rows(ROOT / MIMO_KEY / "atlas.jsonl")
            if r["task_id"] == "format-code-task-000030"
        )
    )
    item = next(
        r
        for r in json_rows(ROOT / MIMO_KEY / "items.jsonl")
        if r["package"]["name"] == row["task_id"]
    )
    row["witnesses"] = row["witnesses"][:1]
    witness = row["witnesses"][0]
    log_path = tmp_path / "source/grades.log"
    log_data = (
        b"== full reward=1.0 rc=0 scored 11.0s\n"
        b"\r    \x1b[32m  passing progress update\x1b[0m\n"
        b"== nop reward=0.0 rc=0 scored 10.8s\n"
        b"== confirm reward=1.0 rc=0 scored 9.3s\n"
    )
    if archived_log:
        log_path.parent.mkdir(parents=True)
        log_path.write_bytes(log_data)
    descriptor = {"path": str(log_path), "sha256": hashlib.sha256(log_data).hexdigest()}
    row["primary"]["grades_log"] = descriptor
    row["line"]["scans"] = []
    witness["evidence_refs"] = [f"{log_path}:{index}" for index in (1, 3, 4)]
    for label, index in (("full", 1), ("nop", 3), ("witness", 4)):
        witness["controls"][label]["line"] = index
    _write_rows(tmp_path, MIMO_KEY / "atlas.jsonl", [row])
    _write_rows(tmp_path, MIMO_KEY / "items.jsonl", [item])
    patch = tmp_path / MIMO_KEY / witness["path"]
    patch.parent.mkdir(parents=True)
    patch.write_bytes((ROOT / MIMO_KEY / witness["path"]).read_bytes())
    return log_path, tmp_path / "state"


def test_mimo_archived_logs_not_patches_determine_executed_tier(tmp_path: Path) -> None:
    log, state = _mimo_fixture(tmp_path, archived_log=True)
    findings = build_mimo_weak(tmp_path, state_root=state)
    assert findings[0].tier == "V3_executed"
    assert findings[0].task.upstream_revision == MIMO_REVISION
    assert findings[0].split == "DEV"
    assert findings[0].locator == "mimo-weak-tests-v2.1:format-code-task-000030:block"
    log.unlink()  # Durable content-addressed custody outlives the source locator.
    assert build_mimo_weak(tmp_path, state_root=state) == findings


def test_mimo_without_raw_grade_logs_is_static_not_executed(tmp_path: Path) -> None:
    _, state = _mimo_fixture(tmp_path, archived_log=False)
    finding = build_mimo_weak(tmp_path, state_root=state)[0]
    assert finding.tier == "V0_unverified"
    assert finding.evidence[0].kind == "static_finding"
    assert finding.split == "DEV"


def _issue(trust: str, split: str) -> dict[str, Any]:
    # Retained task_issues + corpus_tasks shape: the source is a read-only
    # projection, never a connection to the developer's live database in tests.
    return {
        "issue_id": "retained-issue:7eba0f18a9d8101f48f2aaa292d78d3b",
        "task_id": "format-code-task-001431",
        "defect_class": "reference_validated_weak_test",
        "direction": "wrong_pass",
        "status": "confirmed",
        "trust_tier": trust,
        "witness_digest": "sha256:55554db2d126b4110ba0d7e61a3864df287da48375dc04f4b5a7f3059f43402d",
        "original_reward": 1.0,
        "repaired_reward": 0.0,
        "task_split": split,
        "cas_uri": "cas://sha256/25be293493885609fbe1b497d9e462e2cb2d879e161fea38011faf48447e8f5a",
        "metadata_json": json.dumps(
            {
                "variants": [
                    {
                        "source": "mimo30",
                        "original_package_digest": "sha256:cb9d3d85293fdfb9d55e78a3024060ff69389a523632be653c6b66d0597fef36",
                        "confirmation_scope": {
                            "reference_validated_weak_test": True,
                            "witnessed_model_exploit_confirmation": False,
                        },
                        "source_record": {
                            "cas_uri": "cas://sha256/d18e1a7cbfc290a3f1ebcca558f81d02d0f38ef666fc29f42ffbc341901da1da",
                            "json_pointer": "/tasks/0",
                            "source_file_digest": "sha256:d5c938e88d92e61c0ef519b3e9af1b9750b07ee9cb54715ca99751a8921eec95",
                        },
                    }
                ]
            }
        ),
    }


@pytest.mark.parametrize(
    ("trust", "tier", "kind", "split"),
    [
        ("tier1_container_verified", "V3_executed", "executed", "DEV"),
        ("tier2_consensus_labeled", "V0_unverified", "judge_label", "TEST"),
        ("tier3_provisional", "V0_unverified", "static_finding", "heldout"),
    ],
)
def test_mimo_db_trust_mapping_and_original_split(
    trust: str, tier: str, kind: str, split: str
) -> None:
    finding = defect_map_records([_issue(trust, split)])[0]
    assert (finding.tier, finding.evidence[0].kind, finding.split) == (tier, kind, split)
    assert finding.task.upstream_revision == MIMO_REVISION
    assert finding.status_now == "unknown"
    assert (
        finding.task.task_version_digest
        == "sha256:cb9d3d85293fdfb9d55e78a3024060ff69389a523632be653c6b66d0597fef36"
    )


def test_mimo_db_rejects_unqualified_tier1_instead_of_laundering_execution() -> None:
    row = _issue("tier1_container_verified", "DEV")
    row["repaired_reward"] = None
    with pytest.raises(ValueError, match="qualified differential"):
        defect_map_records([row])


def test_registry_discovers_source_modules_and_cli_writes_reproducibly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = {
        "pm-tb4-findings",
        "envcheck-tb4",
        "tb21-weak-tests",
        "tb2-to-tb21-fixes",
        "mimo-weak-tests",
        "mimo-defect-map",
    }
    assert expected <= collection_builders().keys()
    finding = defect_map_records([_issue("tier3_provisional", "heldout")])[0]
    monkeypatch.setattr(
        ingest,
        "collection_builders",
        lambda: {finding.collection: lambda root, state_root=None: [finding]},
    )
    command = ["failures", "ingest", finding.collection, "--state-root", str(tmp_path / "state")]
    assert run_cli(command, workspace=tmp_path) == 0
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    original = store.findings_path(finding.collection).read_bytes()
    assert run_cli(command, workspace=tmp_path) == 0
    assert store.findings_path(finding.collection).read_bytes() == original
    assert store.stats()["findings"]["by_collection"] == {finding.collection: 1}


def test_mimo_db_snapshot_is_read_only_pinned_and_survives_source_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "source.sqlite"
    issue = _issue("tier1_container_verified", "DEV")
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE task_issues(issue_id TEXT, task_id TEXT, trust_tier TEXT)")
        connection.execute(
            "CREATE TABLE corpus_tasks(task_id TEXT, dataset_id TEXT, split TEXT, metadata_json TEXT, cas_uri TEXT)"
        )
        connection.execute(
            "INSERT INTO task_issues VALUES (?, ?, ?)",
            (issue["issue_id"], issue["task_id"], issue["trust_tier"]),
        )
        connection.execute(
            "INSERT INTO corpus_tasks VALUES (?, ?, ?, ?, ?)",
            (
                issue["task_id"],
                "mimo-defect-map-v1",
                "DEV",
                issue["metadata_json"],
                issue["cas_uri"],
            ),
        )
    exported = {
        "issue_id": issue["issue_id"],
        "task_id": issue["task_id"],
        "trust_tier": issue["trust_tier"],
        "task_split": "DEV",
        "metadata_json": issue["metadata_json"],
        "task_cas_uri": issue["cas_uri"],
    }
    data = (
        json.dumps(exported, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(mimo, "DB_EXPORT_SHA256", digest)
    original = db.read_bytes()
    rows = mimo._db_snapshot(tmp_path / "state", db_path=db)
    assert rows == [exported]
    assert db.read_bytes() == original
    cached = tmp_path / "state/raw/mimo-defect-map" / digest / "issues.jsonl"
    assert cached.read_bytes() == data
    assert cached.stat().st_mode & 0o222 == 0
    db.unlink()
    assert mimo._db_snapshot(tmp_path / "state", db_path=db) == rows
    monkeypatch.setattr(mimo, "DB_EXPORT_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="cannot read pinned MiMo"):
        mimo._db_snapshot(tmp_path / "state", db_path=db)
