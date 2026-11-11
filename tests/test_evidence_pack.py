"""Native citation hydration preserves tool-call and observation identity."""

from pathlib import Path


def test_multi_call_citation_handle_hydration_identity(tmp_path: Path) -> None:
    """CitationHandle target_type='tool_call' hydrates specific tool call and sibling observation."""
    import json

    from evallab.interpretation.trajectory_hydration import create_citation_handle, hydrate_citation

    trial_dir = tmp_path / "t_multi_cit"
    (trial_dir / "agent").mkdir(parents=True)
    raw_atif = {
        "schema_version": "ATIF-v1.4",
        "session_id": "sess-cit-test",
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [
                    {
                        "name": "bash",
                        "arguments": {"command": "cat foo.txt"},
                        "tool_call_id": "call_a",
                    },
                    {"name": "edit", "arguments": {"file": "bar.txt"}, "tool_call_id": "call_b"},
                ],
                "observations": [
                    {"source_call_id": "call_a", "content": "hello foo"},
                    {"source_call_id": "call_b", "content": "edited bar"},
                ],
            }
        ],
    }
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps(raw_atif, indent=2))

    cit_a = create_citation_handle(
        source_path="agent/trajectory.json",
        step_id=1,
        call_index=0,
        tool_call_id="call_a",
        target_type="tool_call",
    )
    assert cit_a.tool_call_id == "call_a"
    assert cit_a.call_index == 0

    hydrated_a = hydrate_citation(cit_a, trial_dir=trial_dir, repo_root=tmp_path)
    parsed_a = json.loads(hydrated_a.redacted_content)
    assert "tool_call" in parsed_a
    assert parsed_a["tool_call"]["name"] == "bash"
    assert "observation" in parsed_a
    assert parsed_a["observation"]["content"] == "hello foo"
    # Does not duplicate the other tool call
    assert parsed_a["tool_call"]["tool_call_id"] == "call_a"
