from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from evallab.trajectory_quality import (
    evaluate_trajectory_quality,
)


def _make_clean_atif():
    return {
        "schema_version": "ATIF-v1.7",
        "metadata": {
            "session_id": "sess-clean-01",
            "total_cost_usd": 0.042,
        },
        "steps": [
            {
                "step_id": 1,
                "role": "user",
                "content": "Hello, fix the file.",
            },
            {
                "step_id": 2,
                "role": "assistant",
                "content": "Running command.",
                "tool_calls": [
                    {
                        "tool_call_id": "call_001",
                        "function": "bash",
                        "arguments": {"command": "ls -la"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 50,
                    "cached_tokens": 800,
                },
            },
            {
                "step_id": 3,
                "role": "tool",
                "tool_results": [
                    {
                        "tool_call_id": "call_001",
                        "content": "total 0\n-rw-r--r-- 1 root root 0 main.py",
                    }
                ],
            },
        ],
    }


def test_valid_clean_trajectory_pass():
    atif = _make_clean_atif()
    trial_result = {
        "task_name": "fix-main-py",
        "task_checksum": "sha256:123456",
        "rewards": {"accuracy": 1.0},
    }
    projected = {
        "trajectory_steps": [
            {"step_id": 1},
            {"step_id": 2},
            {"step_id": 3},
        ],
        "tool_calls": [
            {"tool_call_id": "call_001"},
        ],
    }

    report = evaluate_trajectory_quality(
        raw_atif=atif,
        trial_result=trial_result,
        projected_facts=projected,
    )

    assert report.status == "pass"
    assert report.is_ingestable is True
    assert report.is_analysis_ready is True
    assert report.summary_counts["error"] == 0
    assert report.quarantine_reason is None


def test_malformed_step_sequence():
    atif = {
        "schema_version": "ATIF-v1.7",
        "steps": [
            {"step_id": 1, "role": "user"},
            {"step_id": "non-int", "role": "assistant"},
            {"step_id": 1, "role": "assistant"},
            {"step_id": 10, "role": "assistant"},
        ],
    }

    report = evaluate_trajectory_quality(raw_atif=atif)
    assert report.status == "fail"
    reasons = {f.reason_code for f in report.findings}
    assert "ATIF_STEP_ID_NON_INTEGER" in reasons
    assert "ATIF_STEP_ID_DUPLICATE" in reasons
    assert "ATIF_STEP_SEQUENCE_GAP" in reasons


def test_unlinked_tool_result():
    atif = {
        "schema_version": "ATIF-v1.7",
        "steps": [
            {
                "step_id": 1,
                "role": "assistant",
                "tool_calls": [{"tool_call_id": "call_valid_01"}],
            },
            {
                "step_id": 2,
                "role": "tool",
                "tool_results": [{"tool_call_id": "call_ORPHAN_99"}],
            },
        ],
    }

    report = evaluate_trajectory_quality(raw_atif=atif)
    assert report.status == "fail"
    reasons = {f.reason_code for f in report.findings}
    assert "ORPHAN_TOOL_RESULT" in reasons


def test_cached_tokens_exceed_prompt_tokens():
    atif = {
        "schema_version": "ATIF-v1.7",
        "steps": [
            {
                "step_id": 1,
                "role": "assistant",
                "usage": {
                    "prompt_tokens": 500,
                    "cached_tokens": 1200,
                },
            }
        ],
    }

    report = evaluate_trajectory_quality(raw_atif=atif)
    assert report.status == "fail"
    reasons = {f.reason_code for f in report.findings}
    assert "CACHE_TOKENS_EXCEED_PROMPT" in reasons


def test_atif_clean_with_harbor_exception_quarantined():
    atif = _make_clean_atif()
    trial_result = {
        "task_name": "sample-task",
        "task_checksum": "sha256:111222",
        "exception": "Docker daemon timeout error while running container.",
        "rewards": None,
    }

    report = evaluate_trajectory_quality(
        raw_atif=atif,
        trial_result=trial_result,
    )

    assert report.status == "quarantined"
    assert report.is_ingestable is False
    assert report.is_analysis_ready is False
    assert report.quarantine_reason is not None
    assert "infrastructure exception" in report.quarantine_reason.lower()


def test_reward_indeterminate_vs_explicit_zero():
    atif = _make_clean_atif()

    res_none = {"task_name": "t1", "task_checksum": "sha256:1"}
    rep_none = evaluate_trajectory_quality(raw_atif=atif, trial_result=res_none)
    assert any(f.reason_code == "REWARD_INDETERMINATE" for f in rep_none.findings)

    res_zero = {"task_name": "t1", "task_checksum": "sha256:1", "rewards": {"score": 0.0}}
    rep_zero = evaluate_trajectory_quality(raw_atif=atif, trial_result=res_zero)
    assert not any(f.reason_code == "REWARD_INDETERMINATE" for f in rep_zero.findings)


def test_artifact_missing_and_digest_mismatch():
    with TemporaryDirectory() as tmpdir:
        art_dir = Path(tmpdir)
        present_file = art_dir / "out.txt"
        present_file.write_text("hello world\n")

        trial_result = {
            "task_name": "art-task",
            "task_checksum": "sha256:1",
            "artifacts": [
                {"path": "out.txt", "sha256": "WRONG_DIGEST_HASH_ABC"},
                {"path": "missing.txt", "sha256": "some_hash"},
            ],
        }

        report = evaluate_trajectory_quality(
            raw_atif=_make_clean_atif(),
            trial_result=trial_result,
            artifacts_dir=art_dir,
        )

        assert report.status == "fail"
        reasons = {f.reason_code for f in report.findings}
        assert "ARTIFACT_FILE_MISSING" in reasons
        assert "ARTIFACT_DIGEST_MISMATCH" in reasons


def test_duplicate_tool_call_id():
    atif = {
        "schema_version": "ATIF-v1.7",
        "steps": [
            {
                "step_id": 1,
                "tool_calls": [
                    {"tool_call_id": "call_DUP_1"},
                    {"tool_call_id": "call_DUP_1"},
                ],
            }
        ],
    }

    report = evaluate_trajectory_quality(raw_atif=atif)
    reasons = {f.reason_code for f in report.findings}
    assert "DUPLICATE_TOOL_CALL_ID" in reasons


def test_unresolved_subagent_ref():
    atif = {
        "schema_version": "ATIF-v1.7",
        "steps": [
            {"step_id": 1, "parent_step_id": 99, "role": "assistant"},
        ],
    }

    report = evaluate_trajectory_quality(raw_atif=atif)
    reasons = {f.reason_code for f in report.findings}
    assert "UNRESOLVED_PARENT_STEP_REF" in reasons
    # Warnings allow ingestion if no fatal errors exist
    assert report.status == "warn"
    assert report.is_ingestable is True


def test_cost_negative_error():
    atif = {
        "schema_version": "ATIF-v1.7",
        "metadata": {"total_cost_usd": -0.50},
        "steps": [{"step_id": 1, "role": "user"}],
    }

    report = evaluate_trajectory_quality(raw_atif=atif)
    assert report.status == "fail"
    reasons = {f.reason_code for f in report.findings}
    assert "COST_NEGATIVE" in reasons


def test_projection_fidelity_step_count_mismatch_and_orphan():
    atif = _make_clean_atif()
    projected = {
        "trajectory_steps": [
            {"step_id": 1},
            {"step_id": 2},
            {"step_id": 999},
            {"step_id": 1000},
        ],
        "tool_calls": [],
    }

    report = evaluate_trajectory_quality(raw_atif=atif, projected_facts=projected)
    reasons = {f.reason_code for f in report.findings}
    assert "PROJECTION_STEP_COUNT_MISMATCH" in reasons
    assert "PROJECTION_ORPHAN_STEP" in reasons


def test_deterministic_digest_on_shuffled_input():
    atif_1 = _make_clean_atif()
    atif_2 = _make_clean_atif()

    atif_2["metadata"] = {"total_cost_usd": 0.042, "session_id": "sess-clean-01"}

    rep_1 = evaluate_trajectory_quality(raw_atif=atif_1)
    rep_2 = evaluate_trajectory_quality(raw_atif=atif_2)

    assert rep_1.check_digest == rep_2.check_digest
    assert len(rep_1.findings) == len(rep_2.findings)
