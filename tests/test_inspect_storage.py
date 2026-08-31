from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from evallab.evidence_store import restore_evidence
from evallab.inspect_adapter import (
    INSPECT_SCHEMAS,
    ingest_inspect_eval_log,
    project_inspect_eval_log,
    write_inspect_projection,
)
from evallab.storage.attach import attach
from evallab.storage.inspect_storage import (
    InspectSourceManifestV1,
)
from evallab.storage.paths import discover_parquet_partitions


def _sample_inspect_log() -> dict:
    return {
        "version": 2,
        "status": "success",
        "eval": {
            "eval_id": "eval_storage_001",
            "task": "storage-test-suite",
            "model": "openai/gpt-4o-mini",
            "run_id": "eval-storage-999",
            "created": "2026-08-31T01:00:00Z",
            "revision": "git-sha-storage123",
            "solver": "agent",
        },
        "stats": {
            "started_at": "2026-08-31T01:00:00Z",
            "completed_at": "2026-08-31T01:03:00Z",
        },
        "attachments": {
            "global_spec": "Global test specification content",
        },
        "samples": [
            {
                "id": "s-101",
                "epoch": 1,
                "uuid": "11111111-2222-3333-4444-555555555555",
                "started_at": "2026-08-31T01:00:01Z",
                "completed_at": "2026-08-31T01:01:00Z",
                "total_time": 59.0,
                "working_time": 50.0,
                "error_retries": [
                    {
                        "type": "RateLimitError",
                        "message": "Too many requests",
                    }
                ],
                "scores": {
                    "accuracy": {"value": 1.0, "answer": "correct", "explanation": "all passed"},
                },
                "messages": [
                    {"role": "user", "content": "Run tests."},
                    {
                        "role": "assistant",
                        "content": "Running test command.",
                        "tool_calls": [
                            {"id": "call_1", "function": "pytest", "arguments": {"args": "-q"}}
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_1",
                        "content": "1 passed",
                    },
                    {"role": "assistant", "content": "Done."},
                ],
                "events": [
                    {"event": "sample_init", "timestamp": "2026-08-31T01:00:01Z"},
                    {"event": "model", "timestamp": "2026-08-31T01:00:05Z"},
                ],
            }
        ],
    }


def test_write_inspect_projection_writes_only_source_tables_in_job_partition(
    tmp_path: Path,
) -> None:
    payload = _sample_inspect_log()
    projection = project_inspect_eval_log(payload, source_path="test_log.json")
    output_root = tmp_path / "derived"

    table_paths = write_inspect_projection(
        projection,
        output_root,
        write_manifest=True,
        source_file="test_log.json",
        source_bytes_size=1024,
        raw_cas_uri="cas://sha256/1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
    )

    # Verify partition folder is discoverable root/job_id=<job_id>
    partition_dir = output_root / f"job_id={projection.run.job_id}"
    assert partition_dir.is_dir()

    # Verify only inspect_* tables are written, not canonical tables
    assert set(table_paths) == {
        "inspect_runs",
        "inspect_attempts",
        "inspect_scores",
        "inspect_events",
        "inspect_attachments",
    }

    manifest_path = partition_dir / "source-manifest.json"
    assert manifest_path.is_file()

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = InspectSourceManifestV1.model_validate(manifest_data)
    assert manifest.evidence_only is True
    assert manifest.projector_identity == "evallab.inspect_adapter"
    assert manifest.projector_version == "1.0.0"
    assert manifest.job_id == projection.run.job_id
    assert manifest.eval_id == "eval_storage_001"
    assert manifest.source_revision == "git-sha-storage123"
    assert manifest.source_file == "test_log.json"
    assert manifest.rebuild_digest == projection.rebuild_digest
    assert (
        manifest.raw_cas_uri
        == "cas://sha256/1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
    )
    assert manifest.sample_count == 1
    assert manifest.attempt_count == 2  # 1 retry + 1 terminal attempt
    assert manifest.score_count == 1
    assert manifest.event_count == 2
    assert manifest.attachment_count == 1

    # Verify Parquet schemas
    for table_name, file_path in table_paths.items():
        schema = pq.read_schema(file_path)
        expected_schema = INSPECT_SCHEMAS[table_name]
        assert schema.names == expected_schema.names

    # Test discovery and unified attach
    discovery = discover_parquet_partitions(output_root)
    assert len(discovery.partitions) == 5
    partition_tables = {p.table for p in discovery.partitions}
    assert partition_tables == {
        "inspect_runs",
        "inspect_attempts",
        "inspect_scores",
        "inspect_events",
        "inspect_attachments",
    }

    # Test attach views
    attach_res = attach(explicit_derived=output_root)
    conn = attach_res.connection
    runs_rows = conn.execute("SELECT count(*) FROM inspect_runs").fetchone()[0]
    assert runs_rows == 1
    attempts_rows = conn.execute("SELECT count(*) FROM inspect_attempts").fetchone()[0]
    assert attempts_rows == 2
    conn.close()


def test_ingest_inspect_eval_log_with_official_eval_and_cas_restoration(tmp_path: Path) -> None:
    pytest.importorskip("inspect_ai", reason="optional Inspect dependency group is not installed")
    import inspect_ai.log as inspect_log
    from inspect_ai.model import ChatMessageAssistant, ChatMessageUser
    from inspect_ai.scorer import Score

    eval_spec = inspect_log.EvalSpec(
        eval_id="eval_storage_live_01",
        task="storage-smoke-task",
        model="openai/gpt-4o",
        created="2026-08-31T00:00:00Z",
        dataset=inspect_log.EvalDataset(name="live_data", location="loc", samples=1),
        config=inspect_log.EvalConfig(),
        revision=inspect_log.EvalRevision(
            type="git", origin="https://github.com/repo", commit="livecommit123"
        ),
        solver="live_solver",
    )
    sample = inspect_log.EvalSample(
        id="sample_live_1",
        epoch=1,
        uuid="88888888-8888-8888-8888-888888888888",
        input="Live test input",
        target="Live test target",
        messages=[
            ChatMessageUser(content="User input"),
            ChatMessageAssistant(content="Assistant output"),
        ],
        scores={"accuracy": Score(value=1.0, answer="Assistant output", explanation="Correct")},
    )
    eval_log_obj = inspect_log.EvalLog(
        version=3,
        status="success",
        eval=eval_spec,
        samples=[sample],
        stats=inspect_log.EvalStats(
            started_at="2026-08-31T00:00:00Z", completed_at="2026-08-31T00:01:00Z"
        ),
    )

    eval_file = tmp_path / "live_sample.eval"
    inspect_log.write_eval_log(eval_log_obj, eval_file)

    derived_dir = tmp_path / "derived"
    store_dir = tmp_path / "store"

    result = ingest_inspect_eval_log(
        eval_file,
        output_root=derived_dir,
        store_root=store_dir,
    )

    assert result.raw_cas_uri.startswith("cas://sha256/")
    assert result.source_manifest is not None
    assert result.source_manifest.evidence_only is True
    assert result.source_manifest.raw_cas_uri == result.raw_cas_uri
    assert result.source_manifest.raw_cas_uri != "pending"

    # Restore CAS archive and verify raw source bytes ONLY (no pending manifest inside CAS)
    with tempfile.TemporaryDirectory() as unpack_temp:
        unpack_target = Path(unpack_temp)
        restore_evidence(store_dir, result.raw_cas_uri, unpack_target)
        assert (unpack_target / "live_sample.eval").is_file()
        assert not (unpack_target / "source-manifest.json").exists()

    # Rejection test for non-.eval
    fake_json = tmp_path / "fake.json"
    fake_json.write_text("{}", encoding="utf-8")
    with pytest.raises(
        ValueError, match="Production Inspect ingest accepts official .eval files only"
    ):
        ingest_inspect_eval_log(fake_json, output_root=derived_dir, store_root=store_dir)
