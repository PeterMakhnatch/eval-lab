from __future__ import annotations

from pathlib import Path

import pytest

from evallab.evidence_store import (
    archive_evidence,
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


def test_evidence_archive_is_content_addressed_and_deduplicated(
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


def test_changed_evidence_creates_a_new_blob(tmp_path: Path) -> None:
    source = _evidence(tmp_path)
    store = tmp_path / "store"
    first = archive_evidence(source, store, record_id="before")
    (source / "result.json").write_text('{"finished_at":"changed"}\n')

    second = archive_evidence(source, store, record_id="after")

    assert second.uri != first.uri
    assert len(list((store / "blobs").rglob("*.tar.gz"))) == 2


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


