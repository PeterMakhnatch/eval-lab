"""Canonical citations preserve tool-call identity without reopening raw evidence."""

from evallab.interpretation.trajectory_hydration import create_citation_handle


def test_multi_call_citation_handle_identity() -> None:
    cit_a = create_citation_handle(
        source_path="agent/trajectory.json",
        step_id=1,
        call_index=0,
        tool_call_id="call_a",
        target_type="tool_call",
    )
    cit_b = create_citation_handle(
        source_path="agent/trajectory.json",
        step_id=1,
        call_index=1,
        tool_call_id="call_b",
        target_type="tool_call",
    )
    assert cit_a.format_citation() == "agent/trajectory.json#step=1:call=call_a"
    assert cit_b.format_citation() == "agent/trajectory.json#step=1:call=call_b"
    assert cit_a.citation_id != cit_b.citation_id
