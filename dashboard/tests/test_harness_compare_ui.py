from __future__ import annotations

import json
from pathlib import Path

import yaml

from dashboard.harness_compare import run_operator_action, sanitize_readiness_report
from evallab.harness_compare import render_pair_text


def test_submit_button_path_holds_unapproved_model(tmp_path: Path) -> None:
    task = tmp_path / "library/tasks/demo"
    task.mkdir(parents=True)
    (task / "task.toml").write_text('[task]\nname = "demo"\nversion = "1.0.0"\n')
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy/standing-approvals.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "daily_cost_ceiling_usd": 20,
                "per_job_cost_ceiling_usd": 3,
                "quiet_failure_rule": 3,
                "refuse_billable_at_used_percent": None,
                "auto_run": [{"name": "local-controls", "agents": ["oracle", "nop"]}],
                "escalate_to_human": ["any_billable_agent"],
            }
        )
    )
    manifest = tmp_path / "pair.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "comparison_id": "ui-canary",
                "question": "operator journey",
                "root_model": {
                    "configured_id": "deepseek/deepseek-v4-flash",
                    "revision_status": "unknown",
                },
                "baseline": {"profile_id": "oracle", "role": "baseline"},
                "candidate": {
                    "profile_id": "mini-swe-agent-deepseek-v4-flash",
                    "role": "candidate",
                },
                "tasks": [{"task": "library/tasks/demo", "canary": True}],
            }
        )
    )
    report = run_operator_action(
        tmp_path, action="submit", manifest_path=manifest, submitted_by="ui-test"
    )
    by_role = {arm["role"]: arm for arm in report["arms"]}
    assert by_role["baseline"]["admitted"] is True
    assert by_role["candidate"]["hold"]["reason_code"] == "paid_run_unauthorized"
    assert by_role["candidate"]["queue_state"] == "waiting"


def test_har24_fail_verdict_with_present_pair_suppresses_approval() -> None:
    report = {
        "kind": "harness_paired_readiness",
        "verdict": "FAIL",
        "current_pair": {
            "status": "present",
            "baseline_spec_id": "b",
            "candidate_spec_id": "c",
        },
        "arms": [
            {
                "role": "candidate",
                "hold": {"approval_command": "uv run evallab approve C --actor you"},
            }
        ],
        "spec_ids": [
            {
                "spec_id": "c",
                "freshness": "current",
                "stale": False,
                "approval_command": "uv run evallab approve C --actor you",
            }
        ],
    }
    sanitized = sanitize_readiness_report(report)
    assert sanitized["arms"][0]["hold"]["approval_command"] is None
    assert sanitized["spec_ids"][0]["approval_command"] is None
    assert "uv run evallab approve" not in render_pair_text(sanitized)

