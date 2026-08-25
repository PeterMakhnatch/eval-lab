from __future__ import annotations

import json
from pathlib import Path

from evallab import cli
from evallab.evidence_store import archive_evidence, restore_evidence


def _evidence(root: Path) -> Path:
    source = root / "job"
    (source / "trial/agent").mkdir(parents=True)
    (source / "result.json").write_text('{"finished_at":"2026-08-19T00:00:00Z"}\n')
    (source / "trial/agent/trajectory.json").write_text(
        '{"schema_version":"ATIF-v1.7","steps":[]}\n'
    )
    return source


def test_evidence_archive_is_content_addressed_deduplicated_and_restorable(
    tmp_path: Path,
) -> None:
    source = _evidence(tmp_path)
    store = tmp_path / "store"

    first = archive_evidence(source, store, record_id="job-1")
    second = archive_evidence(source, store, record_id="job-2")

    assert first.uri == second.uri
    assert first.blob_path == second.blob_path
    assert first.archive_digest == second.archive_digest
    assert len(list((store / "blobs").rglob("*.tar.gz"))) == 1
    assert first.blob_path.stat().st_mode & 0o777 == 0o600

    restored = restore_evidence(store, first.uri, tmp_path / "restored")
    assert (restored / "result.json").read_bytes() == (source / "result.json").read_bytes()
    assert (
        restored / "trial/agent/trajectory.json"
    ).read_bytes() == (source / "trial/agent/trajectory.json").read_bytes()


def test_changed_evidence_creates_a_new_blob(tmp_path: Path) -> None:
    source = _evidence(tmp_path)
    store = tmp_path / "store"
    first = archive_evidence(source, store, record_id="before")
    (source / "result.json").write_text('{"finished_at":"changed"}\n')

    second = archive_evidence(source, store, record_id="after")

    assert second.uri != first.uri
    assert len(list((store / "blobs").rglob("*.tar.gz"))) == 2


def test_evidence_archive_cli_returns_durable_uri(tmp_path: Path, capsys) -> None:
    source = _evidence(tmp_path)
    store = tmp_path / "store"

    rc = cli.run_cli([
        "evidence", "archive", str(source),
        "--store", str(store),
        "--record-id", "cli-job",
        "--json",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["uri"].startswith("cas://sha256/")
    assert Path(payload["manifest_path"]).is_file()
