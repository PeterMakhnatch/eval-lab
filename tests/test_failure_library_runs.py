"""Retained source custody digests over tiny offline native fixtures."""

from __future__ import annotations

from pathlib import Path


def test_retained_bundle_digest_changes_with_native_bytes(tmp_path: Path):
    from evallab.failure_library.runs.io import retained_bundle_digest
    native = tmp_path / "traces.jsonl"
    native.write_bytes(b'{"native":"fixture"}\n')
    pin, _ = retained_bundle_digest(tmp_path)
    native.write_bytes(b'{"native":"modified"}\n')
    assert retained_bundle_digest(tmp_path)[0] != pin


def test_retained_bundle_digest_binds_native_path(tmp_path: Path):
    from evallab.failure_library.runs.io import retained_bundle_digest
    native = tmp_path / "traces.jsonl"
    native.write_bytes(b'{"native":"fixture"}\n')
    pin, _ = retained_bundle_digest(tmp_path)
    native.rename(tmp_path / "other.jsonl")
    assert retained_bundle_digest(tmp_path)[0] != pin
