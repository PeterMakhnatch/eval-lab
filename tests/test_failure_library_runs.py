"""Consumer-visible run ingestion over tiny offline, source-shaped native fixtures."""

from __future__ import annotations

from pathlib import Path

from evallab.failure_library.runs.common import agent_text
from evallab.failure_library.runs.io import reference


def test_agent_text_excludes_user_and_tool_messages():
    native = {"steps": [{"source": "user", "message": "You are being monitored."},
                        {"source": "agent", "message": "Run the unit tests."},
                        {"source": "tool", "message": "The grader is strict."}]}
    assert list(agent_text(native)) == ["Run the unit tests."]


def test_native_jsonl_reference_preserves_physical_line(tmp_path: Path):
    path = tmp_path / "raw.jsonl"
    path.write_text('\n{"id":"second line"}\n')
    assert reference(path, 1, jsonl=True).endswith("#line=2")


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
