from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from evallab.harness_compare import (
    HarnessCompareError,
    compile_pair,
    inspect_pair,
    load_analysis_product,
    load_manifest,
    load_pair_inputs,
    readiness_report,
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


def _factory_cohort(root: Path, task: str) -> Path:
    payload = {
        "schema_version": 1,
        "cohort_id": "harness-first-20260909",
        "linear_issue": "HAR-14",
        "frozen_at_commit": "477dcdd21939d9009332db9dd6ed4af2ae432dbb",
        "scientific_question": "same-root mini-swe vs authors-RLM",
        "primary_metric": "task-level Harbor reward",
        "canary_task_id": "event-summary",
        "remaining_task_ids": [],
        "members": [
            {
                "task_id": "event-summary",
                "registered_ref": task,
                "task_family": "event-summary",
                "version": "1.0.0",
                "task_path": task,
                "stage": "canary",
                "limits": {"timeout_seconds": 180},
                "digests": {
                    "verifier": "sha256:" + ("ab" * 32),
                    "package": "sha256:" + ("cd" * 32),
                },
            }
        ],
    }
    path = root / "cohort.json"
    path.write_text(json.dumps(payload))
    return path


def test_factory_cohort_is_not_a_paired_manifest(tmp_path: Path) -> None:
    path = _factory_cohort(tmp_path, _task_package(tmp_path))
    with pytest.raises(HarnessCompareError, match="Factory cohort.json"):
        load_manifest(path)


def test_compose_factory_cohort_binds_root_on_both_arms(tmp_path: Path) -> None:
    task = _task_package(tmp_path)
    cohort = _factory_cohort(tmp_path, task)
    manifest = load_pair_inputs(
        cohort_path=cohort,
        root_model="deepseek/deepseek-v4-flash",
    )
    report = compile_pair(tmp_path, manifest, submitted_by="har-11")
    assert report["comparison_id"] == "harness-first-20260909"
    assert {arm["role"] for arm in report["arms"]} == {"baseline", "candidate"}
    for arm in report["arms"]:
        assert arm["spec"]["model"] == "deepseek/deepseek-v4-flash"
        assert arm["root_model"]["configured_id"] == "deepseek/deepseek-v4-flash"
        assert arm["spec"]["grid_id"] == "harness-first-20260909"
        assert arm["spec"]["task_id"] == "event-summary"
    by_role = {arm["role"]: arm for arm in report["arms"]}
    assert by_role["baseline"]["runtime"]["status"] == "registered"
    assert by_role["baseline"]["runtime"]["adapter"] == "mini-swe-agent"
    assert by_role["candidate"]["runtime"]["status"] == "registered"
    assert by_role["candidate"]["runtime"]["import_path"] == ("evallab.harbor_rlm:AuthorsRlmAgent")


def test_root_model_mismatch_fails_closed(tmp_path: Path) -> None:
    path = _manifest(
        tmp_path,
        baseline={"profile_id": "mini-swe-agent-deepseek-v4-flash", "role": "baseline"},
        candidate={"profile_id": "authors-rlm-deepseek-v4-flash", "role": "candidate"},
        root_model={"configured_id": "fixture/requested-root", "revision_status": "unknown"},
    )
    with pytest.raises(HarnessCompareError, match="does not match profile"):
        compile_pair(tmp_path, load_manifest(path), submitted_by="har-11")


def test_cli_prepare_rejects_cohort_as_manifest(tmp_path: Path) -> None:
    cohort = _factory_cohort(tmp_path, _task_package(tmp_path))
    assert (
        run_cli(
            [
                "paired-compare",
                "prepare",
                "--repo-root",
                str(tmp_path),
                "--manifest",
                str(cohort),
                "--format",
                "text",
            ]
        )
        == 2
    )


def test_cli_prepare_composes_cohort(tmp_path: Path) -> None:
    cohort = _factory_cohort(tmp_path, _task_package(tmp_path))
    assert (
        run_cli(
            [
                "paired-compare",
                "prepare",
                "--repo-root",
                str(tmp_path),
                "--cohort",
                str(cohort),
                "--root-model",
                "deepseek/deepseek-v4-flash",
                "--format",
                "json",
            ]
        )
        == 0
    )


def test_analysis_directory_product_is_consumed(tmp_path: Path) -> None:
    product = tmp_path / "analysis"
    product.mkdir()
    payload = {
        "metadata": {"evidence_kind": "fixture", "spec_id": "harness-first-20260909"},
        "summary": {"warnings": ["descriptive only"]},
    }
    (product / "report.json").write_text(json.dumps(payload))
    (product / "report.md").write_text("# fixture analysis\n")
    loaded = load_analysis_product(product)
    assert loaded["evidence_kind"] == "fixture"
    assert "fixture analysis" in loaded["markdown"]


def test_inspect_does_not_emit_comparison_spec_without_jobs(tmp_path: Path) -> None:
    _policy(tmp_path)
    submitted = submit_pair(tmp_path, load_manifest(_manifest(tmp_path)), submitted_by="har-11")
    viewed = inspect_pair(tmp_path, comparison_id=submitted["comparison_id"])
    assert viewed["comparison_spec"] is None
    assert any("CohortComparisonSpec not emitted" in issue for issue in viewed["issues"])


def test_readiness_is_blocked_not_approval_only(tmp_path: Path) -> None:
    _policy(tmp_path)
    task = _task_package(tmp_path, "event-summary")
    cohort = _factory_cohort(tmp_path, task)
    manifest = load_pair_inputs(cohort_path=cohort, root_model="deepseek/deepseek-v4-flash")
    report = readiness_report(tmp_path, manifest, submitted_by="har-11")
    assert report["verdict"] == "BLOCKED"
    assert "READY_FOR_APPROVAL" not in report["verdict"]
    assert "approval-only" in report["verdict_reason"]
    by_name = {gate["name"]: gate for gate in report["gates"]}
    assert by_name["constructor_config"]["status"] == "BLOCKED"
    assert by_name["execution_authorization"]["status"] == "BLOCKED"
    assert by_name["source_runtime_pins"]["status"] == "PASS"
    assert {arm["spec"]["est_cost_usd"] for arm in report["arms"]} == {2.5}
    assert (
        run_cli(
            [
                "paired-compare",
                "readiness",
                "--repo-root",
                str(tmp_path),
                "--cohort",
                str(cohort),
                "--root-model",
                "deepseek/deepseek-v4-flash",
                "--format",
                "json",
            ]
        )
        == 0
    )
