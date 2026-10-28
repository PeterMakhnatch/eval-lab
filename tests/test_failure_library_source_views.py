from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.failure_library.loop.source_views import extract_documents
from evallab.failure_library.records import Evidence, Finding, TaskRef, make_finding_id
from evallab.failure_library.runs.published import olmo
from evallab.failure_library.store import LibraryStore
from evallab.registry import task_directory_digest


@pytest.fixture
def task_view(tmp_path: Path) -> tuple[dict[str, Any], Finding, Path]:
    package = tmp_path / "Developer/eval-lab/derived/kev/raw/terminal-bench-v4.0.0/tasks/sum"
    package.mkdir(parents=True)
    instruction = package / "instruction.md"
    instruction.write_text("Compute the sum of the supplied integers.\n")
    task = TaskRef(
        source_id="terminal-bench",
        task_id="sum",
        upstream_repo="https://example.invalid/tasks",
        upstream_revision="a" * 40,
        task_version_digest=task_directory_digest(package),
    )
    finding = Finding(
        finding_id=make_finding_id(task.source_id, task.task_id, "a" * 40, "TS01", "audit"),
        collection="pm-tb4-findings",
        locator="audit",
        task=task,
        defect_class="TS01",
        direction="wrong_pass",
        summary="Incomplete validation coverage.",
        evidence=[Evidence(kind="static_finding", note="Source-only fixture.")],
        tier="V0_unverified",
        finder="fixture",
        source_url="https://example.invalid/audit",
        observed_at=date(2026, 10, 27),
        split="DEV",
    )
    return (
        {
            "view_extractor": "task-files/v1",
            "package_path": str(package),
            "sources": [{"path": str(instruction)}],
        },
        finding,
        instruction,
    )


def test_task_view_rejects_changed_package_even_with_new_file_checksum(task_view) -> None:
    subject, finding, instruction = task_view
    instruction.write_text("A different task revision.\n")
    with pytest.raises(ValueError, match="pinned finding version"):
        extract_documents(subject, finding)


def test_task_view_cannot_substitute_an_unrelated_source_file(task_view, tmp_path: Path) -> None:
    subject, finding, _ = task_view
    unrelated = tmp_path / "different-instruction.md"
    unrelated.write_text("A different problem, with authentic bytes.\n")
    subject["sources"] = [{"path": str(unrelated)}]
    with pytest.raises(ValueError, match="outside the pinned package"):
        extract_documents(subject, finding)


@pytest.fixture
def trace_view(tmp_path: Path):
    rows = [
        {
            "source_eval_file": "fixture",
            "sample_id": index,
            "epoch": 1,
            "problem_id": f"problem-{index}",
            "training_passed": index,
            "passed": index,
            "actually_solved": index,
            "reward_hacked": 0,
            "messages": [{"role": "assistant", "content": "I computed the requested result."}],
        }
        for index in (0, 1)
    ]
    path = tmp_path / "source.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path)
    reference = path.as_uri() + "#row=0&field=messages"
    record = olmo(rows[0], reference).model_copy(update={"split": "DEV"})
    content_length = len(rows[0]["messages"][0]["content"])
    subject = {
        "view_extractor": "olmo-messages/v1",
        "sources": [{"path": str(path)}],
        "spans": [
            {"message": 0, "start": 0, "end": content_length, "original_chars": content_length}
        ],
    }
    return subject, record


def test_trace_view_cannot_attach_one_rows_labels_to_another_row(trace_view) -> None:
    subject, record = trace_view
    record = record.model_copy(
        update={"transcript_ref": record.transcript_ref.replace("row=0", "row=1")}
    )
    with pytest.raises(ValueError, match="disagree with the native row"):
        extract_documents(subject, record)


def test_trace_view_rejects_out_of_bounds_or_stale_message_spans(trace_view) -> None:
    subject, record = trace_view
    subject["spans"][0]["end"] = 1000
    with pytest.raises(ValueError, match="span no longer matches"):
        extract_documents(subject, record)


@pytest.mark.parametrize(
    "mutation", ["documents", "source-label", "heldout", "missing", "duplicate"]
)
def test_pilot_rejects_unbound_views_claims_and_split_laundering(
    task_view, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from evallab.failure_library.loop.readonly import diagnostic_pilot, source_view
    from evallab.failure_library.loop.selection import digest

    monkeypatch.setenv("HOME", str(tmp_path))
    subject, finding, instruction = task_view
    canonical = (
        finding.model_copy(update={"split": "heldout"}) if mutation == "heldout" else finding
    )
    if mutation != "missing":
        LibraryStore.for_repo(tmp_path).write_findings(finding.collection, [canonical])
    subject["sources"][0]["sha256"] = hashlib.sha256(instruction.read_bytes()).hexdigest()
    subject.update(
        id="S1",
        kind="task",
        record=finding.model_dump(mode="json"),
        record_digest=digest(finding.model_dump(mode="json")),
        documents=["Unrelated view."] if mutation == "documents" else [instruction.read_text()],
    )
    if mutation == "source-label":
        subject["record"]["finder"] = "unattested-local-claim"
        subject["record_digest"] = digest(subject["record"])
    subject["view_sha256"] = source_view(subject["documents"])["sha256"]
    subjects = [subject, {**subject, "id": "S2"}] if mutation == "duplicate" else [subject]
    body = {"schema": "failure-library.readonly-pilot/v1", "subjects": subjects}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({**body, "digest": digest(body)}))
    pack = tmp_path / "packet"
    pack.mkdir()
    guidance = "Check the stated requirement against the independent oracle."
    (pack / "pack.md").write_text(guidance)
    (pack / "pack.manifest.json").write_text(
        json.dumps(
            {
                "status": "ready",
                "retrieval_mode": "curated_only",
                "example_ids": [],
                "card_ids": [],
                "curated_guidance_ids": ["FG-TS01"],
                "files": {"pack.md": {"sha256": hashlib.sha256(guidance.encode()).hexdigest()}},
            }
        )
    )
    message = "documents disagree" if mutation == "documents" else "canonical"
    with pytest.raises(ValueError, match=message):
        diagnostic_pilot(
            manifest,
            pack,
            tmp_path / "output",
            repo_root=tmp_path,
            dry_run=True,
            allow_curated=True,
        )
    assert not (tmp_path / "output").exists()
