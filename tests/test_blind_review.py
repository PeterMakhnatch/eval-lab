"""Behavioral tests for the blind trace-review workflow (`evallab review`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.cli import run_cli
from evallab.interpretation.blind_review import (
    leak_scan_pack,
    review_freeze,
    review_join,
    review_prepare,
)


def _make_mock_trial(
    job_dir: Path,
    trial_name: str,
    task_id: str,
    first_prompt: str,
    reward: float = 0.0,
    exception_type: str | None = None,
    stop_reason: str = "token_ceiling",
    extra_content: str = "",
) -> Path:
    """Create a minimal synthetic Harbor trial structure."""
    trial_dir = job_dir / trial_name
    trial_dir.mkdir(parents=True, exist_ok=True)
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    verifier_dir = trial_dir / "verifier"
    verifier_dir.mkdir(parents=True, exist_ok=True)

    traj = {
        "schema_version": "atif/v1",
        "steps": [
            {
                "step_id": 1,
                "source": "user",
                "message": first_prompt,
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": extra_content,
                "reasoning_content": "planning fix",
                "tool_calls": [
                    {
                        "tool_call_id": "c1",
                        "function_name": "bash",
                        "arguments": {"keystrokes": "cat file\n"},
                    }
                ],
                "observation": {
                    "results": [{"content": "file output"}]
                },
            },
        ],
    }
    (agent_dir / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")

    result = {
        "task_name": f"mimo-v2.6-rl/format-code-task-{task_id}",
        "task_id": task_id,
        "trial_name": trial_name,
        "verifier_result": {"rewards": {"reward": reward}},
        "agent_result": {
            "n_input_tokens": 1000,
            "n_output_tokens": 100,
            "metadata": {
                "n_episodes": 2,
                "stop_reason": stop_reason,
            },
        },
        "exception_info": {
            "exception_type": exception_type,
            "exception_message": "error" if exception_type else None,
        },
    }
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (verifier_dir / "reward.txt").write_text(f"{reward}\n", encoding="utf-8")
    return trial_dir


def test_leak_scan_catches_arm_token_hidden_in_json_escaped_text(tmp_path: Path) -> None:
    """The leak scan catches an arm token hidden in JSON-escaped text."""
    pack_dir = tmp_path / "packs" / "b-01"
    pack_dir.mkdir(parents=True, exist_ok=True)

    # In JSON, an escaped string containing 'tuned'
    escaped_obj = {"note": "some prefix \"tuned\" suffix\nwith newline"}
    (pack_dir / "trajectory.json").write_text(json.dumps(escaped_obj), encoding="utf-8")

    # Scanning for arm token 'tuned'
    leaks = leak_scan_pack(pack_dir, forbidden_tokens=["tuned"])
    assert len(leaks) >= 1
    assert any("tuned" in leak["token"] for leak in leaks)
    assert leaks[0]["file"] == "b-01/trajectory.json"


def test_prompt_identity_fails_on_unmasked_addendum_and_passes_once_masked(tmp_path: Path) -> None:
    """Prompt-identity check fails on an unmasked addendum and passes once masked."""
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()

    base_prompt = "You are solving a bug in root@12345678-1234-1234-1234-123456789abc:/repo#\nFix it."
    addendum = "CRITICAL ADVICE: change your diagnostic before retrying.\n"

    # Stock job
    stock_job = jobs_root / "job-stock"
    _make_mock_trial(stock_job, "trial-000100-stock__aaa", "000100", base_prompt)

    # GEPA job with unmasked addendum
    gepa_prompt = "You are solving a bug in root@abcdef01-abcd-abcd-abcd-abcdef012345:/repo#\n" + addendum + "Fix it."
    gepa_job = jobs_root / "job-gepa"
    _make_mock_trial(gepa_job, "trial-000100-gepa__bbb", "000100", gepa_prompt)

    out_dir = tmp_path / "review_unmasked"

    # 1. Unmasked: must fail hard
    with pytest.raises(ValueError, match="differ across arms"):
        review_prepare(
            job_dirs=[stock_job, gepa_job],
            arm_regex=r"-(?P<arm>stock|gepa)__",
            out_dir=out_dir,
        )

    # 2. Masked: write addendum to mask file and retry
    mask_file = tmp_path / "addendum.txt"
    mask_file.write_text(addendum, encoding="utf-8")

    out_masked = tmp_path / "review_masked"
    rc = review_prepare(
        job_dirs=[stock_job, gepa_job],
        arm_regex=r"-(?P<arm>stock|gepa)__",
        mask_text_files=[mask_file],
        out_dir=out_masked,
    )
    assert rc == 0
    assert (out_masked / "SEALED_arm_map.json").is_file()
    assert (out_masked / "metrics_blind.jsonl").is_file()
    assert (out_masked / "leak_scan.json").is_file()


def test_freeze_refuses_missing_rater_files_and_detects_tampering(tmp_path: Path) -> None:
    """Freeze refuses missing rater files, refuses overwrite, and join detects tampered label."""
    jobs_root = tmp_path / "jobs"
    job = jobs_root / "job-stock"
    _make_mock_trial(job, "trial-000100-stock__111", "000100", "Prompt root@11111111-1111-1111-1111-111111111111:/#")

    review_dir = tmp_path / "review"
    review_prepare(
        job_dirs=[job],
        arm_regex=r"-(?P<arm>stock)__",
        out_dir=review_dir,
    )

    labels_dir = tmp_path / "labels"
    rater_a_dir = labels_dir / "rater_a"
    rater_b_dir = labels_dir / "rater_b"
    rater_a_dir.mkdir(parents=True)
    rater_b_dir.mkdir(parents=True)

    # Read the opaque ID
    map_data = json.loads((review_dir / "SEALED_arm_map.json").read_text())
    (oid,) = map_data.keys()

    valid_label = {
        "trial": oid,
        "stop_reason": "token_ceiling",
        "first_failure": None,
        "blame": "none",
        "loop_kind": "none",
        "pass_copied": False,
        "evidence": [{"ref": "head#1", "quote": "solved"}],
    }

    # 1. Missing rater_b label -> must fail
    (rater_a_dir / f"{oid}.json").write_text(json.dumps(valid_label), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="Missing label file for rater rater_b"):
        review_freeze(review_dir, labels_dir)

    # 2. Add rater_b label -> freeze succeeds
    (rater_b_dir / f"{oid}.json").write_text(json.dumps(valid_label), encoding="utf-8")
    rc = review_freeze(review_dir, labels_dir)
    assert rc == 0
    assert (review_dir / "MANIFEST.sha256").is_file()
    assert (review_dir / "FROZEN_AT").is_file()

    # 3. Refuses overwrite
    with pytest.raises(FileExistsError, match="already frozen"):
        review_freeze(review_dir, labels_dir)

    # 4. Tampered label -> join detects tampering
    frozen_label_path = review_dir / "labels" / "rater_a" / f"{oid}.json"
    tampered_label = dict(valid_label, blame="model")
    frozen_label_path.write_text(json.dumps(tampered_label), encoding="utf-8")

    with pytest.raises(ValueError, match="changed after freeze"):
        review_join(review_dir)


def test_join_treats_excluded_cells_as_missing(tmp_path: Path) -> None:
    """Join treats excluded cells as missing, not zero."""
    review_dir = tmp_path / "review_excluded"
    review_dir.mkdir()
    labels_dir = review_dir / "labels"
    (labels_dir / "rater_a").mkdir(parents=True)
    (labels_dir / "rater_b").mkdir(parents=True)

    arm_map = {
        "b-01": {"arm": "stock", "task": "000100", "trial": "t-stock", "job_path": "/tmp", "seed": "1"},
        "b-02": {"arm": "tuned", "task": "000100", "trial": "t-tuned", "job_path": "/tmp", "seed": "1"},
        "b-03": {"arm": "gepa", "task": "000100", "trial": "t-gepa", "job_path": "/tmp", "seed": "1"},
    }
    sealed_map = review_dir / "SEALED_arm_map.json"
    sealed_map.write_text(json.dumps(arm_map), encoding="utf-8")
    sealed_map.chmod(0o400)

    # b-01 passed, b-02 is excluded (infra error), b-03 failed
    metrics_rows = [
        {
            "id": "b-01",
            "task": "000100",
            "verdict": "counted_pass",
            "reward": 1.0,
            "stop_reason": "model_finished",
            "agent_steps": 10,
            "first_edit_step": 3,
        },
        {
            "id": "b-02",
            "task": "000100",
            "verdict": "excluded",
            "reward": None,
            "stop_reason": "infra_error",
            "agent_steps": 2,
            "first_edit_step": None,
        },
        {
            "id": "b-03",
            "task": "000100",
            "verdict": "counted_fail",
            "reward": 0.0,
            "stop_reason": "token_ceiling",
            "agent_steps": 20,
            "first_edit_step": 5,
        },
    ]
    (review_dir / "metrics_blind.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in metrics_rows), encoding="utf-8"
    )

    for oid, arm in [("b-01", "stock"), ("b-02", "tuned"), ("b-03", "gepa")]:
        label = {
            "trial": oid,
            "stop_reason": "model_finished" if arm == "stock" else ("token_ceiling" if arm == "gepa" else "infra_error"),
            "first_failure": None if arm == "stock" else {"ref": "head#1", "what": "fail", "quote": "q"},
            "blame": "none" if arm == "stock" else ("model" if arm == "gepa" else "infra"),
            "loop_kind": "none",
            "pass_copied": False if arm == "stock" else None,
            "evidence": [],
        }
        (labels_dir / "rater_a" / f"{oid}.json").write_text(json.dumps(label), encoding="utf-8")
        (labels_dir / "rater_b" / f"{oid}.json").write_text(json.dumps(label), encoding="utf-8")

    # Freeze
    review_freeze(review_dir, labels_dir)

    # Join
    review_join(review_dir)

    tables_text = (review_dir / "TABLES.md").read_text()
    assert "| scored cells | 1 | 0 | 1 |" in tables_text or "| scored cells | 1 | 1 | 0 |" in tables_text
    # b-02 must be marked missing, not fail
    assert "missing" in tables_text


def test_report_pass_table_shows_counts_vs_rater_judgement(tmp_path: Path) -> None:
    """The REPORT pass table shows counts vs rater judgement disagreement (e.g. copied pass)."""
    review_dir = tmp_path / "review_report"
    review_dir.mkdir()
    labels_dir = review_dir / "labels"
    (labels_dir / "rater_a").mkdir(parents=True)
    (labels_dir / "rater_b").mkdir(parents=True)

    arm_map = {
        "b-01": {"arm": "stock", "task": "000100", "trial": "t-stock", "job_path": "/tmp", "seed": "1"},
        "b-02": {"arm": "tuned", "task": "000100", "trial": "t-tuned", "job_path": "/tmp", "seed": "1"},
    }
    sealed_map = review_dir / "SEALED_arm_map.json"
    sealed_map.write_text(json.dumps(arm_map), encoding="utf-8")
    sealed_map.chmod(0o400)

    # Both count as passed in verdict
    metrics_rows = [
        {"id": "b-01", "task": "000100", "verdict": "counted_pass", "reward": 1.0, "agent_steps": 10},
        {"id": "b-02", "task": "000100", "verdict": "counted_pass", "reward": 1.0, "agent_steps": 20},
    ]
    (review_dir / "metrics_blind.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in metrics_rows), encoding="utf-8"
    )

    # Stock is genuine pass (pass_copied: False)
    label_stock = {
        "trial": "b-01",
        "stop_reason": "model_finished",
        "first_failure": None,
        "blame": "none",
        "loop_kind": "none",
        "pass_copied": False,
        "evidence": [{"ref": "head#5", "quote": "clean fix"}],
    }
    (labels_dir / "rater_a" / "b-01.json").write_text(json.dumps(label_stock), encoding="utf-8")
    (labels_dir / "rater_b" / "b-01.json").write_text(json.dumps(label_stock), encoding="utf-8")

    # Tuned is copied pass (pass_copied: True)
    label_tuned = {
        "trial": "b-02",
        "stop_reason": "model_finished",
        "first_failure": {"ref": "head#3", "what": "pip download upstream", "quote": "pip download"},
        "blame": "model",
        "loop_kind": "none",
        "pass_copied": True,
        "evidence": [{"ref": "head#3", "quote": "pip download"}],
    }
    (labels_dir / "rater_a" / "b-02.json").write_text(json.dumps(label_tuned), encoding="utf-8")
    (labels_dir / "rater_b" / "b-02.json").write_text(json.dumps(label_tuned), encoding="utf-8")

    review_freeze(review_dir, labels_dir)
    review_join(review_dir)

    report_text = (review_dir / "REPORT.md").read_text()
    assert "| stock | 1 | 1 | 0 | 1/1 | 1/1 |" in report_text
    # Tuned had 1/1 in counts verdict, but 0/1 in raters' genuine passes:
    assert "| tuned | 1 | 1 | 0 | 1/1 | 0/1 |" in report_text


def test_cli_end_to_end(tmp_path: Path) -> None:
    """End-to-end execution of evallab review prepare, freeze, join via run_cli."""
    jobs_dir = tmp_path / "jobs"
    j1 = jobs_dir / "job-stock"
    j2 = jobs_dir / "job-gepa"
    _make_mock_trial(j1, "trial-000100-stock__aaa", "000100", "Prompt root@11111111-1111-1111-1111-111111111111:/#")
    _make_mock_trial(j2, "trial-000100-gepa__bbb", "000100", "Prompt root@22222222-2222-2222-2222-222222222222:/#")

    review_dir = tmp_path / "review_cli"

    # 1. Prepare
    prepare_rc = run_cli([
        "review",
        "prepare",
        "--job-dir", str(j1),
        "--job-dir", str(j2),
        "--arm-regex=-(?P<arm>stock|gepa)__",
        "--out", str(review_dir),
    ])
    assert prepare_rc == 0
    assert (review_dir / "SEALED_arm_map.json").is_file()

    # Create dummy labels
    labels_dir = tmp_path / "dummy_labels"
    (labels_dir / "rater_a").mkdir(parents=True)
    (labels_dir / "rater_b").mkdir(parents=True)

    arm_map = json.loads((review_dir / "SEALED_arm_map.json").read_text())
    for oid in arm_map:
        lbl = {
            "trial": oid,
            "stop_reason": "token_ceiling",
            "first_failure": None,
            "blame": "none",
            "loop_kind": "none",
            "pass_copied": False,
            "evidence": [],
        }
        (labels_dir / "rater_a" / f"{oid}.json").write_text(json.dumps(lbl), encoding="utf-8")
        (labels_dir / "rater_b" / f"{oid}.json").write_text(json.dumps(lbl), encoding="utf-8")

    # 2. Freeze
    freeze_rc = run_cli([
        "review",
        "freeze",
        str(review_dir),
        "--labels", str(labels_dir),
    ])
    assert freeze_rc == 0
    assert (review_dir / "MANIFEST.sha256").is_file()

    # 3. Join
    join_rc = run_cli([
        "review",
        "join",
        str(review_dir),
    ])
    assert join_rc == 0
    assert (review_dir / "TABLES.md").is_file()
    assert (review_dir / "tables.json").is_file()
    assert (review_dir / "REPORT.md").is_file()
