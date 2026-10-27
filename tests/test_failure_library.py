from __future__ import annotations

import hashlib
import json
import random
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.cli import run_cli
from evallab.evidence_store import load_blob
from evallab.failure_library.records import (
    Evidence,
    Finding,
    RunLabel,
    RunRecord,
    TaskRef,
    make_finding_id,
)
from evallab.failure_library.store import LibraryStore

REVISION = "0123456789abcdef0123456789abcdef01234567"


def _task(task_id: str = "fix-git", revision: str | None = REVISION) -> TaskRef:
    return TaskRef(
        source_id="terminal-bench",
        task_id=task_id,
        upstream_repo="https://github.com/laude-institute/terminal-bench",
        upstream_revision=revision,
    )


def _finding(
    *,
    task_id: str = "fix-git",
    evidence: list[Evidence] | None = None,
    tier: str = "V2_upstream_fixed",
    locator: str = "commit-abc",
    collection: str = "tb-fixes",
    **overrides: object,
) -> Finding:
    evidence = evidence or [Evidence(kind="upstream_fix", fix_revision="abc123")]
    payload: dict[str, object] = {
        "finding_id": make_finding_id("terminal-bench", task_id, REVISION, "TS01", locator),
        "collection": collection,
        "locator": locator,
        "task": _task(task_id),
        "defect_class": "TS01",
        "direction": "wrong_pass",
        "summary": "Tests only check that the output file exists.",
        "evidence": evidence,
        "tier": tier,
        "finder": "upstream maintainers",
        "source_url": "https://github.com/laude-institute/terminal-bench/commit/abc123",
        "observed_at": date(2026, 10, 27),
    }
    payload.update(overrides)
    return Finding.model_validate(payload)


def test_tier_is_the_strongest_evidence_and_cannot_be_overstated() -> None:
    mixed = [
        Evidence(kind="judge_label", label_source="goodfire-3-vote"),
        Evidence(kind="upstream_fix", fix_revision="abc123"),
    ]
    assert _finding(evidence=mixed, tier="V2_upstream_fixed").tier == "V2_upstream_fixed"
    with pytest.raises(ValidationError, match="disagrees with evidence"):
        _finding(evidence=[Evidence(kind="judge_label", label_source="judge")], tier="V3_executed")


def test_finding_requires_a_pinned_revision_and_a_fix_pin_when_fixed() -> None:
    with pytest.raises(ValidationError, match="upstream_revision"):
        Finding.model_validate(
            {**_finding().model_dump(), "task": _task(revision=None).model_dump()}
        )
    with pytest.raises(ValidationError, match="fixed_in_revision"):
        _finding(status_now="fixed_upstream")
    fixed = _finding(status_now="fixed_upstream", fixed_in_revision="abc123")
    assert fixed.fixed_in_revision == "abc123"


def test_finding_id_is_deterministic_and_checked() -> None:
    with pytest.raises(ValidationError, match="deterministic id"):
        _finding(finding_id="fl-0000000000000000")
    assert make_finding_id("a", "b", "c", "TS01", "x") != make_finding_id(
        "a", "b", "d", "TS01", "x"
    )


def test_evidence_kinds_require_their_proof() -> None:
    with pytest.raises(ValidationError, match="receipt_ref or reproducer_ref"):
        Evidence(kind="executed")
    with pytest.raises(ValidationError, match="fix_revision"):
        Evidence(kind="upstream_fix")
    with pytest.raises(ValidationError, match="label_source"):
        Evidence(kind="human_label")


def test_findings_write_is_sorted_stable_and_refuses_conflicts(tmp_path: Path) -> None:
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    findings = [_finding(task_id=f"task-{index}", locator=f"loc-{index}") for index in range(5)]
    path = store.write_findings("tb-fixes", findings)
    first = path.read_bytes()
    shuffled = findings[:]
    random.Random(7).shuffle(shuffled)
    store.write_findings("tb-fixes", shuffled + findings[:2])
    assert path.read_bytes() == first
    ids = [json.loads(line)["finding_id"] for line in first.decode().splitlines()]
    assert ids == sorted(ids)

    conflicting = _finding(task_id="task-0", locator="loc-0", summary="A different claim.")
    with pytest.raises(ValueError, match="conflicting records"):
        store.write_findings("tb-fixes", [findings[0], conflicting])
    with pytest.raises(ValueError, match="cannot be written"):
        store.write_findings("other", findings)


def test_run_manifest_detects_changed_bytes(tmp_path: Path) -> None:
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    runs = [
        RunRecord(
            run_id=f"cb-{index}",
            collection="cheatbench",
            task=_task(revision=None),
            outcome="pass",
            labels=[RunLabel(label="cheating", rs_code="RS02", kind="human", source="CheatBench")],
            transcript_ref=f"hf://steinad/CheatBench@rev#{index}",
            split="cross_test",
        )
        for index in range(3)
    ]
    manifest = store.write_runs("cheatbench", runs, source_revision="461360b")
    assert (manifest.count, manifest.outcomes, manifest.label_kinds) == (
        3,
        {"pass": 3},
        {"human": 3},
    )
    assert store.verify_runs("cheatbench") == []
    store.runs_path("cheatbench").write_text("{}\n", encoding="utf-8")
    assert "differs from manifest" in store.verify_runs("cheatbench")[0]


def test_run_stats_separate_label_occurrences_from_runs(tmp_path: Path) -> None:
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    judged = RunRecord(
        run_id="gf-1",
        collection="goodfire",
        task=_task(revision=None),
        outcome="pass",
        labels=[
            RunLabel(label="test_edit", kind="judge", source="goodfire-vote-1"),
            RunLabel(label="test_edit", kind="judge", source="goodfire-vote-2"),
            RunLabel(label="hack", kind="human", source="audit"),
        ],
        transcript_ref="raw/goodfire#1",
    )
    plain = judged.model_copy(update={"run_id": "gf-2", "labels": []})
    store.write_runs("goodfire", [judged, plain], source_revision="bd8fb47")
    runs = store.stats()["runs"]
    assert runs["by_label_kind"] == {"human": 1, "judge": 2}
    assert runs["by_runs_with_label_kind"] == {"human": 1, "judge": 1}


def test_stats_command_reports_library_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    store.write_findings("tb-fixes", [_finding(), _finding(task_id="other", locator="loc-2")])
    code = run_cli(
        ["failures", "stats", "--json", "--state-root", str(tmp_path / "state")], workspace=tmp_path
    )
    assert code == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["findings"]["total"] == 2
    assert stats["findings"]["by_tier"] == {"V2_upstream_fixed": 2}
    assert stats["runs"]["total"] == 0


def test_collection_update_retains_observations_and_manifests(tmp_path: Path) -> None:
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    finding = _finding()
    finding_path = store.write_findings("tb-fixes", [finding])
    original_finding = finding_path.read_bytes()
    fixed = _finding(status_now="fixed_upstream", fixed_in_revision="abc123")
    store.write_findings("tb-fixes", [fixed])

    run = RunRecord(
        run_id="historical-attempt",
        collection="tb-fixes",
        task=_task(),
        outcome="fail",
        transcript_ref="file:///immutable/old-revision/trajectory.json",
    )
    store.write_runs("tb-fixes", [run], source_revision=REVISION)
    original_runs = store.runs_path("tb-fixes").read_bytes()
    original_manifest = store.manifest_path("tb-fixes").read_bytes()
    new_revision = "fedcba9876543210fedcba9876543210fedcba98"
    newer_run = run.model_copy(
        update={
            "run_id": "later-attempt",
            "task": _task(revision=new_revision),
            "outcome": "pass",
            "transcript_ref": "file:///immutable/new-revision/trajectory.json",
        }
    )
    store.write_runs("tb-fixes", [newer_run], source_revision=new_revision)

    retained = [
        original_finding,
        finding_path.read_bytes(),
        original_runs,
        original_manifest,
        store.runs_path("tb-fixes").read_bytes(),
        store.manifest_path("tb-fixes").read_bytes(),
    ]
    for observation in retained:
        uri = "cas://sha256/" + hashlib.sha256(observation).hexdigest()
        assert load_blob(store.history_root, uri) == observation
    assert json.loads(original_manifest)["source_revision"] == REVISION
    assert list(store.iter_runs("tb-fixes")) == [newer_run]
    assert store.verify_runs("tb-fixes") == []


@pytest.mark.parametrize("collection", ["../outside", "/absolute", "nested/task", "", ".."])
def test_collection_paths_cannot_escape_the_library(tmp_path: Path, collection: str) -> None:
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    for path in (store.findings_path, store.runs_path, store.manifest_path):
        with pytest.raises(ValueError, match="invalid collection"):
            path(collection)


def test_stats_distinguish_claims_task_versions_and_construction(tmp_path: Path) -> None:
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    records = [
        _finding(locator="audit-a"),
        _finding(locator="audit-b"),
        _finding(
            locator="constructed",
            evidence=[Evidence(kind="construction", note="Deliberately contradictory task.")],
            tier="V1_external_label",
        ),
    ]
    store.write_findings("tb-fixes", records)
    findings = store.stats()["findings"]
    assert findings["total"] == 3
    assert findings["unique_task_ids"] == 1
    assert findings["unique_task_versions"] == 1
    assert findings["by_record_basis"] == {"constructed": 1, "reported_failure": 2}
    assert findings["by_execution_support"] == {"no_execution_evidence": 3}
