from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from evallab.harness_compare import (
    HarnessCompareError,
    compile_pair,
    inspect_pair,
    load_manifest,
    submit_pair,
)
from evallab.task_workbench import run_cli


def _task_package(root: Path, name: str = "demo-task") -> str:
    rel = f"library/tasks/{name}"
    task = root / rel
    task.mkdir(parents=True)
    (task / "task.toml").write_text(
        f'[task]\nname = {json.dumps(name)}\nversion = "1.0.0"\n\n[agent]\ntimeout_sec = 60\n'
    )
    return rel


def _policy(root: Path) -> None:
    dest = root / "policy/standing-approvals.yaml"
    dest.parent.mkdir(parents=True)
    dest.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "daily_cost_ceiling_usd": 20,
                "per_job_cost_ceiling_usd": 3,
                "quiet_failure_rule": 3,
                "refuse_billable_at_used_percent": None,
                "auto_run": [{"name": "local-controls", "agents": ["oracle", "nop"]}],
                "escalate_to_human": [
                    "any_billable_agent",
                    "new_task_registration",
                    "cloud_or_remote_environment",
                    "anything_exceeding_ceilings",
                    "subscription_quota_exhausted",
                ],
            }
        )
    )


def _manifest(root: Path, **fields: object) -> Path:
    task = _task_package(root)
    extra = _task_package(root, "second-task")
    payload = {
        "schema_version": 1,
        "comparison_id": "harness-first-canary",
        "question": "Does an RLM-derived agent change the same-root outcome versus mini-swe-agent?",
        "root_model": {"configured_id": "unspecified-held", "revision_status": "unknown"},
        "baseline": {"profile_id": "oracle", "role": "baseline"},
        "candidate": {"profile_id": "nop", "role": "candidate"},
        "tasks": [
            {"task": task, "canary": True},
            {"task": extra, "canary": False},
        ],
    }
    payload.update(fields)
    path = root / "pair.json"
    path.write_text(json.dumps(payload))
    return path


def test_canary_compile_emits_two_linked_control_specs(tmp_path: Path) -> None:
    path = _manifest(tmp_path)
    manifest = load_manifest(path)
    report = compile_pair(tmp_path, manifest, submitted_by="har-11")
    assert report["canary_only"] is True
    assert len(report["arms"]) == 2
    grid_ids = {arm["spec"]["grid_id"] for arm in report["arms"]}
    tasks = {arm["spec"]["task"] for arm in report["arms"]}
    roles = {arm["role"] for arm in report["arms"]}
    assert grid_ids == {"harness-first-canary"}
    assert len(tasks) == 1
    assert roles == {"baseline", "candidate"}
    assert all(arm["runtime"]["status"] == "control" for arm in report["arms"])


def test_submit_admits_controls_and_holds_billable_model(tmp_path: Path) -> None:
    _policy(tmp_path)
    controls = load_manifest(_manifest(tmp_path))
    admitted = submit_pair(tmp_path, controls, submitted_by="har-11")
    assert {arm["queue_state"] for arm in admitted["arms"]} == {"approved"}
    assert all(arm["admitted"] is True and arm["spec_id"] for arm in admitted["arms"])

    billed_root = tmp_path / "billable"
    billed_root.mkdir()
    billable_path = _manifest(
        billed_root,
        baseline={"profile_id": "oracle", "role": "baseline"},
        candidate={"profile_id": "mini-swe-agent-deepseek-v4-flash", "role": "candidate"},
        root_model={
            "configured_id": "deepseek/deepseek-v4-flash",
            "revision_status": "unknown",
        },
    )
    _policy(billed_root)
    held = submit_pair(billed_root, load_manifest(billable_path), submitted_by="har-11")
    by_role = {arm["role"]: arm for arm in held["arms"]}
    assert by_role["baseline"]["admitted"] is True
    assert by_role["candidate"]["admitted"] is False
    assert by_role["candidate"]["hold"]["reason_code"] == "paid_run_unauthorized"
    assert "uv run evallab approve" in by_role["candidate"]["hold"]["approval_command"]
    assert by_role["candidate"]["runtime"]["status"] == "registered"


def test_unknown_profile_and_unregistered_runtime_fail_closed(tmp_path: Path) -> None:
    path = _manifest(tmp_path, candidate={"profile_id": "not-a-profile", "role": "candidate"})
    with pytest.raises(HarnessCompareError, match="unknown agent profile"):
        compile_pair(tmp_path, load_manifest(path), submitted_by="har-11")


def test_inspect_reports_missing_jobs_without_inventing_them(tmp_path: Path) -> None:
    _policy(tmp_path)
    submitted = submit_pair(tmp_path, load_manifest(_manifest(tmp_path)), submitted_by="har-11")
    viewed = inspect_pair(tmp_path, comparison_id=submitted["comparison_id"])
    assert viewed["baseline_count"] == 1
    assert viewed["candidate_count"] == 1
    assert viewed["jobs"] == []
    assert {row["missing"] for row in viewed["missing_arms"]} == {"job"}


def test_cli_prepare_and_submit_do_not_tick(tmp_path: Path) -> None:
    _policy(tmp_path)
    path = _manifest(tmp_path)
    assert (
        run_cli(
            [
                "paired-compare",
                "prepare",
                "--repo-root",
                str(tmp_path),
                "--manifest",
                str(path),
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert (
        run_cli(
            [
                "paired-compare",
                "submit",
                "--repo-root",
                str(tmp_path),
                "--manifest",
                str(path),
                "--format",
                "text",
            ]
        )
        == 0
    )
    assert list((tmp_path / "queue/running").glob("*.json")) == []
