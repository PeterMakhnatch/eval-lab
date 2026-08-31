from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from evallab.evidence_store import restore_evidence
from evallab.inspect_adapter import (
    INSPECT_SCHEMAS,
    ingest_inspect_eval_log,
    project_inspect_eval_log,
    write_inspect_projection,
)
from evallab.storage.inspect_storage import (
    InspectSourceManifestV1,
)


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


def test_write_inspect_projection_writes_only_source_tables(tmp_path: Path) -> None:
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

    # Verify partition folder
    partition_dir = output_root / "source=inspect" / f"job_id={projection.run.job_id}"
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

    # DuckDB read test
    conn = duckdb.connect()
    attempts_count = conn.execute(
        f"SELECT count(*) FROM read_parquet('{table_paths['inspect_attempts']}')"
    ).fetchone()[0]
    assert attempts_count == 2


def test_ingest_inspect_eval_log_with_mandatory_cas_store(tmp_path: Path) -> None:
    log_file = tmp_path / "inspect-eval.json"
    raw_data = _sample_inspect_log()
    raw_bytes = json.dumps(raw_data, indent=2).encode("utf-8")
    log_file.write_bytes(raw_bytes)

    derived_dir = tmp_path / "derived"
    store_dir = tmp_path / "store"

    result = ingest_inspect_eval_log(
        log_file,
        output_root=derived_dir,
        store_root=store_dir,
    )

    assert result.raw_cas_uri is not None
    assert result.raw_cas_uri.startswith("cas://sha256/")
    assert result.source_manifest is not None
    assert result.source_manifest.evidence_only is True
    assert result.source_manifest.raw_cas_uri == result.raw_cas_uri
    assert result.source_manifest.source_bytes_size == len(raw_bytes)

    # Restore CAS archive and verify contents
    with tempfile.TemporaryDirectory() as unpack_temp:
        unpack_target = Path(unpack_temp)
        restore_evidence(store_dir, result.raw_cas_uri, unpack_target)
        assert (unpack_target / "inspect-eval.json").is_file()
        assert (unpack_target / "source-manifest.json").is_file()

        unpacked_manifest = json.loads((unpack_target / "source-manifest.json").read_text())
        assert unpacked_manifest["rebuild_digest"] == result.projection.rebuild_digest
        assert unpacked_manifest["source_digest"] == result.projection.run.source_digest
        assert unpacked_manifest["evidence_only"] is True
