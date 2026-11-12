"""Canonical citation coordinates and immutable CAS evidence contracts."""

from pathlib import Path

from evallab.evidence_store import archive_evidence, restore_evidence
from evallab.interpretation.trajectory_hydration import create_citation_handle


def test_citation_archive_roundtrip(tmp_path: Path) -> None:
    source_dir = tmp_path / "source_trial"
    cas_store = tmp_path / "cas_store"
    (source_dir / "agent").mkdir(parents=True)
    raw = b'{"schema_version": "ATIF-v1.4", "steps": []}'
    (source_dir / "agent/trajectory.json").write_bytes(raw)
    (source_dir / "result.json").write_text('{"trial_name": "t1"}')
    archive = archive_evidence(source_dir, cas_store, record_id="t1", kind="trial")
    restored = tmp_path / "restored"
    restore_evidence(cas_store, archive.uri, restored)
    assert (restored / "agent/trajectory.json").read_bytes() == raw
    assert (source_dir / "agent/trajectory.json").read_bytes() == raw


def test_observation_citation_binds_source_call_id() -> None:
    citation = create_citation_handle(
        source_path="agent/trajectory.json",
        step_id=7,
        source_call_id="call-b",
        observation_index=1,
        target_type="observation",
    )
    other = create_citation_handle(
        source_path="agent/trajectory.json",
        step_id=7,
        source_call_id="call-a",
        observation_index=0,
        target_type="observation",
    )
    assert citation.format_citation() == "agent/trajectory.json#step=7:call=call-b:obs=1"
    assert citation.citation_id != other.citation_id
