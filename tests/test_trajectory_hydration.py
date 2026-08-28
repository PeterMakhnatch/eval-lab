"""Tests for CAS/raw-ATIF-backed redacted hydration API."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from evallab.interpretation.trajectory_hydration import (
    CitationPathJailError,
    CitationTarget,
    RedactionPolicy,
    apply_redaction,
    create_citation_handle,
    hydrate_citation,
    hydrate_error_observations,
)
from evallab.traj import LoopSuspicion, StepOutline, TrajectoryOutline


def test_apply_redaction_masks_secrets() -> None:
    """Secrets like API keys and bearer tokens are masked on-read with deterministic digest markers."""
    mock_key = "sk-" + "proj-" + "123456789012345678901234567890"
    text = f"Authorization: Bearer my_secret_token_1234567890abcdef and key {mock_key}"
    policy = RedactionPolicy(redact_secrets=True)

    redacted, is_redacted, meta = apply_redaction(text, policy)

    assert is_redacted is True
    assert meta["secrets_masked"] >= 1
    assert "sk-proj-" not in redacted
    assert "<<evallab-redacted:" in redacted
    assert "sha256:" in redacted


def test_apply_redaction_truncation() -> None:
    """Truncation beyond max_display_bytes includes omitted bytes and full content digest."""
    long_text = "A" * 500
    policy = RedactionPolicy(max_display_bytes=100)

    redacted, is_redacted, meta = apply_redaction(long_text, policy)

    assert is_redacted is True
    assert len(redacted) < 300
    assert meta["truncated_bytes"] == 400
    assert "<<evallab-truncated: 400 bytes omitted, full sha256:" in redacted


def test_hydrate_citation_preserves_raw_file_immutability() -> None:
    """Hydrating evidence leaves raw files on disk strictly untouched and identical in bytes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        trial_dir = Path(tmpdir)
        traj_file = trial_dir / "trajectory.json"
        mock_secret = "sk-" + "ant-" + "123456789012345678901234567890"
        raw_payload = {
            "schema_version": "ATIF-v1.4",
            "session_id": "sess-1",
            "steps": [
                {
                    "step_id": 1,
                    "actor": "agent",
                    "tool_calls": [
                        {
                            "name": "bash",
                            "arguments": {"command": f"echo '{mock_secret}'"},
                        }
                    ],
                    "observations": [
                        {
                            "content": f"Secret: {mock_secret}",
                            "extra": {"exit_code": 0},
                        }
                    ],
                }
            ],
        }
        original_bytes = json.dumps(raw_payload, indent=2).encode("utf-8")
        traj_file.write_bytes(original_bytes)

        citation = CitationTarget(
            trial_id="trial-1",
            source_path="trajectory.json",
            step_index=1,
            observation_index=0,
            target_type="observation",
        )

        hydrated = hydrate_citation(citation, trial_dir=trial_dir)

        # Invariant 1: file on disk was NOT modified
        assert traj_file.read_bytes() == original_bytes

        # Invariant 2: raw_content holds exact unredacted text
        assert "sk-ant-" in hydrated.raw_content

        # Invariant 3: redacted_content masks secrets
        assert hydrated.is_redacted is True
        assert "sk-ant-" not in hydrated.redacted_content
        assert "<<evallab-redacted:" in hydrated.redacted_content

        # Invariant 4: source citation is preserved
        assert hydrated.citation.step_index == 1
        assert hydrated.content_bytes > 0
        assert hydrated.content_sha256.startswith("sha256:")


def test_hydrate_citation_rejects_absolute_path_and_traversal() -> None:
    """Citation source_path is strictly jailed; absolute paths and .. escapes raise CitationPathJailError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        trial_dir = Path(tmpdir)

        # Test 1: Absolute path rejected
        abs_citation = CitationTarget(
            trial_id="trial-1",
            source_path="/etc/passwd",
        )
        with pytest.raises(CitationPathJailError, match="must be relative, got absolute path"):
            hydrate_citation(abs_citation, trial_dir=trial_dir)

        # Test 2: Traversal escaping trial_dir rejected
        traversal_citation = CitationTarget(
            trial_id="trial-1",
            source_path="../outside.json",
        )
        with pytest.raises(CitationPathJailError, match="escapes trial directory"):
            hydrate_citation(traversal_citation, trial_dir=trial_dir)


def test_hydrate_citation_cas_member_path_jailing() -> None:
    """Citation source_path inside CAS archive is strictly jailed; ../ escapes raise CitationPathJailError."""
    from evallab.evidence_store import archive_evidence

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir)
        source_dir = temp_path / "source_trial"
        cas_store = temp_path / "cas_store"
        source_dir.mkdir(parents=True)
        cas_store.mkdir(parents=True)

        (source_dir / "agent").mkdir()
        (source_dir / "agent" / "trajectory.json").write_text(
            json.dumps({"schema_version": "ATIF-v1.4", "steps": []})
        )
        (source_dir / "result.json").write_text(json.dumps({"trial_name": "t1"}))

        archive = archive_evidence(source_dir, cas_store, record_id="t1", kind="trial")

        # Test 1: Absolute path in CAS citation raises CitationPathJailError
        abs_cit = CitationTarget(
            source_path="/etc/shadow",
            raw_cas_uri=archive.uri,
        )
        with pytest.raises(CitationPathJailError, match="must be relative, got absolute path"):
            hydrate_citation(abs_cit, repo_root=cas_store)

        # Test 2: Traversal escaping CAS archive root raises CitationPathJailError
        traversal_cit = CitationTarget(
            source_path="../outside.json",
            raw_cas_uri=archive.uri,
        )
        with pytest.raises(CitationPathJailError, match="escapes CAS archive root"):
            hydrate_citation(traversal_cit, repo_root=cas_store)


def test_hydrate_citation_surfaces_typed_cas_limitations() -> None:
    """Missing or corrupted CAS archives surface typed evidence limitations with reason codes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_repo = Path(tmpdir)

        # Test: CAS archive not found
        missing_cas_citation = CitationTarget(
            trial_id="trial-cas-1",
            source_path="trajectory.json",
            cas_uri="cas://sha256/0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )
        hydrated = hydrate_citation(missing_cas_citation, repo_root=fake_repo)

        assert hydrated.redaction_metadata.get("limitation_reason") == "cas_archive_not_found"
        assert "[EvidenceLimitation: cas_archive_not_found" in hydrated.raw_content


def test_hydrate_error_observations_from_outline() -> None:
    """hydrate_error_observations loads untruncated stderr for all failing steps."""
    with tempfile.TemporaryDirectory() as tmpdir:
        trial_dir = Path(tmpdir)
        agent_dir = trial_dir / "agent"
        agent_dir.mkdir(parents=True)
        traj_file = agent_dir / "trajectory.json"

        raw_payload = {
            "schema_version": "ATIF-v1.4",
            "session_id": "sess-1",
            "steps": [
                {
                    "step_id": 1,
                    "actor": "agent",
                    "tool_calls": [{"name": "bash", "arguments": {"command": "cat missing.txt"}}],
                    "observations": [
                        {
                            "content": "cat: missing.txt: No such file or directory",
                            "extra": {
                                "exit_code": 1,
                                "stderr": "cat: missing.txt: No such file or directory",
                            },
                        }
                    ],
                }
            ],
        }
        traj_file.write_text(json.dumps(raw_payload))

        steps = [
            StepOutline(
                step_id=1,
                source="agent",
                timestamp="2026-08-25T12:00:00Z",
                model_name="model",
                tool_name="bash",
                tool_command="cat missing.txt",
                exit_code=1,
                is_error=True,
                error_message="cat: missing.txt: No such file or directory",
                prompt_tokens=100,
                completion_tokens=20,
                cached_tokens=0,
                cost_usd=0.001,
                thought_snippet="cat missing.txt",
            )
        ]

        outline = TrajectoryOutline(
            trial_id="err-trial",
            job_id="job-1",
            trial_name="trial_err",
            job_name="job_err",
            task_name="task",
            agent_name="agent",
            agent_version="1.0",
            model_name="model",
            status="featured",
            unavailable_reason=None,
            source_path="agent/trajectory.json",
            source_sha256="sha_err",
            duration_seconds=1.0,
            primary_reward=0.0,
            exception_class=None,
            total_steps=1,
            agent_steps=1,
            system_steps=0,
            user_steps=0,
            total_tool_calls=1,
            total_errors=1,
            recovery_count=0,
            step_to_first_tool=1,
            step_to_first_edit=None,
            time_to_first_tool_seconds=1.0,
            time_to_first_edit_seconds=None,
            total_prompt_tokens=100,
            total_completion_tokens=20,
            total_cached_tokens=0,
            total_cost_usd=0.001,
            phases=(),
            loop_suspicion=LoopSuspicion(0.0, False, (), 0, 0, 0),
            steps=tuple(steps),
            citations=(),
            tool_mix={"bash": 1},
        )

        evidences = hydrate_error_observations(trial_dir, outline)

        assert len(evidences) == 1
        ev = evidences[0]
        assert ev.citation.step_index == 1
        assert "No such file or directory" in ev.raw_content
        assert ev.citation.format_citation().startswith("agent/trajectory.json#step=1")


def test_hydrate_v17_observation_results_by_source_call_id(tmp_path: Path) -> None:
    """ATIF-v1.7 observation.results resolves only the cited source_call_id."""
    trial_dir = tmp_path / "v17"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "steps": [
                    {
                        "step_id": 7,
                        "tool_calls": [
                            {"tool_call_id": "call-a", "function_name": "exec"},
                            {"tool_call_id": "call-b", "function_name": "exec"},
                        ],
                        "observation": {
                            "results": [
                                {"source_call_id": "call-a", "content": "only-a"},
                                {"source_call_id": "call-b", "content": "only-b"},
                            ]
                        },
                    }
                ],
            }
        )
    )
    citation = create_citation_handle(
        source_path="agent/trajectory.json",
        step_id=7,
        source_call_id="call-b",
        observation_index=1,
        target_type="observation",
    )
    hydrated = hydrate_citation(citation, trial_dir=trial_dir, repo_root=tmp_path)
    assert hydrated.redacted_content == "only-b"
    assert "only-a" not in hydrated.redacted_content

    missing = create_citation_handle(
        source_path="agent/trajectory.json",
        step_id=99,
        source_call_id="missing",
        target_type="observation",
    )
    absent = hydrate_citation(missing, trial_dir=trial_dir, repo_root=tmp_path)
    assert "EvidenceLimitation: cited_element_not_found" in absent.redacted_content
