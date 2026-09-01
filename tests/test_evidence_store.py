from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evallab import cli
from evallab.evidence_store import (
    archive_evidence,
    load_archive,
    load_blob,
    read_record,
    restore_evidence,
    store_blob,
)


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
    assert first.archive_digest == second.archive_digest
    assert not hasattr(first, "blob_path")
    assert not hasattr(first, "manifest_path")
    blobs = list((store / "blobs").rglob("*.tar.gz"))
    assert len(blobs) == 1
    assert blobs[0].stat().st_mode & 0o777 == 0o600

    restored = restore_evidence(store, first.uri, tmp_path / "restored")
    assert (restored / "result.json").read_bytes() == (source / "result.json").read_bytes()
    assert (restored / "trial/agent/trajectory.json").read_bytes() == (
        source / "trial/agent/trajectory.json"
    ).read_bytes()


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

    rc = cli.run_cli(
        [
            "evidence",
            "archive",
            str(source),
            "--store",
            str(store),
            "--record-id",
            "cli-job",
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["uri"].startswith("cas://sha256/")
    assert payload["record_digest"].startswith("sha256:")
    assert payload["content_digest"].startswith("sha256:")
    assert "manifest_path" not in payload
    assert "blob_path" not in payload


@pytest.mark.parametrize(
    "symlink_parts",
    [
        ("blobs",),
        ("blobs", "sha256"),
    ],
)
def test_store_blob_rejects_nested_cas_directory_symlink(
    tmp_path: Path,
    symlink_parts: tuple[str, ...],
) -> None:
    store = tmp_path / "store"
    outside = tmp_path / "outside"
    outside.mkdir()
    link = store.joinpath(*symlink_parts)
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink|non-directory"):
        store_blob(store, b"must-stay-confined")

    assert list(outside.iterdir()) == []


def test_archive_rejects_symlinked_record_kind_without_external_write(
    tmp_path: Path,
) -> None:
    source = _evidence(tmp_path)
    store = tmp_path / "store"
    outside = tmp_path / "outside"
    outside.mkdir()
    record_link = store / "records" / "job"
    record_link.parent.mkdir(parents=True)
    record_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink|non-directory"):
        archive_evidence(source, store, record_id="escape")

    assert not (outside / "escape.json").exists()


def test_cas_reads_reject_final_blob_symlinks(tmp_path: Path) -> None:
    store = tmp_path / "store"
    outside_blob = tmp_path / "outside.bin"
    payload = b"attacker-controlled-payload"
    outside_blob.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    blob_dir = store / "blobs" / "sha256" / digest[:2]
    blob_dir.mkdir(parents=True)
    (blob_dir / f"{digest}.bin").symlink_to(outside_blob)

    with pytest.raises(ValueError, match="unsafe"):
        load_blob(store, f"cas://sha256/{digest}")


def test_cas_archive_read_rejects_nested_symlink(tmp_path: Path) -> None:
    store = tmp_path / "store"
    outside = tmp_path / "outside"
    outside.mkdir()
    digest = "a" * 64
    prefix = store / "blobs" / "sha256" / digest[:2]
    prefix.parent.mkdir(parents=True)
    prefix.symlink_to(outside, target_is_directory=True)
    (outside / f"{digest}.tar.gz").write_bytes(b"not-a-trusted-archive")

    with pytest.raises(ValueError, match="symlink|non-directory"):
        load_archive(store, f"cas://sha256/{digest}")


def test_cas_record_read_rejects_final_symlink(tmp_path: Path) -> None:
    store = tmp_path / "store"
    outside = tmp_path / "outside.json"
    outside.write_text('{"record_id":"forged"}\n', encoding="utf-8")
    record_dir = store / "records" / "job"
    record_dir.mkdir(parents=True)
    (record_dir / "forged.json").symlink_to(outside)

    with pytest.raises(ValueError, match="unsafe"):
        read_record(store, kind="job", record_id="forged")
