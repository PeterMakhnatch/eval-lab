from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
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
    render_pair_text,
    submit_pair,
)
from evallab.registry import compute_task_digests, harbor_task_digest
from evallab.schemas import (
    ControlEvidenceRef,
    TaskControlEvidence,
    TaskLimits,
    TaskRegistryRecord,
)
from evallab.task_workbench import run_cli


def _task_package(root: Path, name: str = "demo-task") -> str:
    rel = f"library/tasks/{name}"
    task = root / rel
    task.mkdir(parents=True, exist_ok=True)
    (task / "task.toml").write_text(
        f'[task]\nname = {json.dumps(name)}\nversion = "1.0.0"\n\n[agent]\ntimeout_sec = 60\n'
    )
    return rel


def _make_registered_task(
    root: Path,
    name: str = "event-summary",
    rel_path: str = "library/tasks/event-summary",
) -> Path:
    task_dir = root / rel_path
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(
        f'schema_version = "1.4"\n[task]\nname = {json.dumps(name)}\nfamily = "event-summary"\nversion = "1.0.0"\n\n[agent]\ntimeout_sec = 60\n'
    )
    (task_dir / "instruction.md").write_text("Summarize events.")
    env_dir = task_dir / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_task.py").write_text("def test_summary(): assert True\n")

    digests = compute_task_digests(task_dir)
    harbor_digest = harbor_task_digest(task_dir)

    runs_dir = root / "research/evidence/runs"

    def make_ref(agent: str, reward: float) -> ControlEvidenceRef:
        job_name = f"{name}-{agent}-evidence"
        trial_name = f"{name}__{agent}"
        trial_dir = runs_dir / job_name / trial_name
        trial_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": f"{agent}-trial",
            "task_name": name,
            "trial_name": trial_name,
            "task_id": {"path": str(task_dir)},
            "config": {"task": {"path": str(task_dir)}, "agent": {"name": agent}},
            "agent_info": {"name": agent, "version": "1.0.0"},
            "verifier_result": {"rewards": {"reward": reward}},
            "finished_at": "2026-08-15T12:00:00Z",
        }
        lock = {
            "schema_version": 2,
            "task": {
                "name": name,
                "version": "1.0.0",
                "type": "local",
                "digest": harbor_digest,
                "path": str(task_dir),
            },
            "agent": {"name": agent},
        }
        result_file = trial_dir / "result.json"
        lock_file = trial_dir / "lock.json"
        result_file.write_text(json.dumps(payload, indent=2))
        lock_file.write_text(json.dumps(lock, indent=2))
        return ControlEvidenceRef(
            job_name=job_name,
            trial_name=trial_name,
            reward=reward,
            evidence_path=result_file.relative_to(root).as_posix(),
            evidence_digest=f"sha256:{hashlib.sha256(result_file.read_bytes()).hexdigest()}",
            lock_digest=f"sha256:{hashlib.sha256(lock_file.read_bytes()).hexdigest()}",
            observed_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
            task_id=name,
            task_version="1.0.0",
            task_digests=digests,
            harbor_task_digest=harbor_digest,
        )

    record = TaskRegistryRecord(
        schema_version=2,
        task_id=name,
        task_family="event-summary",
        version="1.0.0",
        task_path=rel_path,
        digests=digests,
        source_uri=f"local/{name}@1.0.0",
        source_ref="main",
        license="MIT",
        provenance_zone="02-local-evidence",
        is_synthetic=False,
        limits=TaskLimits(timeout_seconds=1800),
        control_evidence=TaskControlEvidence(
            oracle=make_ref("oracle", 1.0),
            nop=make_ref("nop", 0.0),
        ),
        state="registered",
        allowed_uses=["measurement", "training"],
        approved_by="Peter Makhnatch",
        approved_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
    )
    reg_dir = root / "library/registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / f"{name}.json").write_text(record.model_dump_json(indent=2))
    return task_dir


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
    by_name = {gate["name"]: gate for gate in report["gates"]}
    assert by_name["constructor_config"]["status"] == "PASS"
    assert by_name["constructor_config"]["evidence_kind"] == "declared"
    assert by_name["execution_authorization"]["status"] == "BLOCKED"
    assert by_name["source_runtime_pins"]["status"] == "PASS"
    assert report["estimates"]["coverage"] == "unknown"
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


def test_task_equality_for_registered_and_relative_references(tmp_path: Path) -> None:
    """Registered reference and repository-relative task reference resolve to the same task."""
    _policy(tmp_path)
    _make_registered_task(tmp_path, "event-summary")
    rel_path = "library/tasks/event-summary"

    manifest_registered = load_manifest(
        _manifest(
            tmp_path,
            comparison_id="reg-ref-check",
            tasks=[{"task": rel_path, "canary": True, "registered_ref": "registered/event-summary"}],
        )
    )
    manifest_relative = load_manifest(
        _manifest(
            tmp_path,
            comparison_id="rel-ref-check",
            tasks=[{"task": rel_path, "canary": True}],
        )
    )

    report_reg = readiness_report(tmp_path, manifest_registered, submitted_by="har-11")
    report_rel = readiness_report(tmp_path, manifest_relative, submitted_by="har-11")

    gates_reg = {g["name"]: g for g in report_reg["gates"]}
    gates_rel = {g["name"]: g for g in report_rel["gates"]}

    assert gates_reg["task_verifier_identity"]["status"] == "PASS"
    assert gates_reg["task_verifier_identity"]["evidence_kind"] == "resolved"
    assert gates_rel["task_verifier_identity"]["status"] == "PASS"
    assert gates_rel["task_verifier_identity"]["evidence_kind"] == "resolved"


def test_canary_flag_cannot_bless_unregistered_or_mismatched_task(tmp_path: Path) -> None:
    """canary=True must not produce task_verifier_identity PASS for unregistered/mismatched tasks."""
    _policy(tmp_path)
    unregistered = _task_package(tmp_path, "unregistered-task")
    manifest = load_manifest(
        _manifest(
            tmp_path,
            comparison_id="unregistered-canary",
            tasks=[{"task": unregistered, "canary": True}],
        )
    )
    report = readiness_report(tmp_path, manifest, submitted_by="har-11")
    gates = {g["name"]: g for g in report["gates"]}
    assert gates["task_verifier_identity"]["status"] != "PASS"
    assert gates["task_verifier_identity"]["status"] in {"FAIL", "UNKNOWN"}


def test_readiness_queue_projections(tmp_path: Path) -> None:
    """Readiness projections distinguish omitted queue, single current pair, stale metadata,
    missing provenance, incomplete arms, and ambiguous pairs."""
    _policy(tmp_path)
    _make_registered_task(tmp_path, "event-summary")
    manifest = load_manifest(
        _manifest(
            tmp_path,
            comparison_id="queue-test",
            tasks=[{"task": "library/tasks/event-summary", "canary": True}],
        )
    )

    # 1. No queue root: does not scan production queue, reports issue, status absent
    report_no_q = readiness_report(tmp_path, manifest, submitted_by="har-11", queue_root=None)
    assert report_no_q["spec_ids"] == []
    assert report_no_q["current_pair"]["status"] == "absent"
    assert report_no_q["current_pair"]["baseline_spec_id"] is None
    assert report_no_q["current_pair"]["candidate_spec_id"] is None
    assert any("queue_root omitted" in issue for issue in report_no_q["inspect"].get("issues", []))

    # 2. Compile current pair metadata to build synthetic queue specs
    compiled = compile_pair(tmp_path, manifest, submitted_by="har-11")
    arms_by_role = {arm["role"]: arm for arm in compiled["arms"]}
    base_spec = arms_by_role["baseline"]["spec"]
    cand_spec = arms_by_role["candidate"]["spec"]

    queue_dir = tmp_path / "synthetic_queue"
    waiting_dir = queue_dir / "waiting"
    waiting_dir.mkdir(parents=True, exist_ok=True)

    (waiting_dir / f"{base_spec['agent']}-{base_spec['spec_id']}.json").write_text(
        json.dumps(base_spec)
    )
    (waiting_dir / f"{cand_spec['agent']}-{cand_spec['spec_id']}.json").write_text(
        json.dumps(cand_spec)
    )

    report_current = readiness_report(
        tmp_path, manifest, submitted_by="har-11", queue_root=queue_dir
    )
    assert report_current["current_pair"]["status"] == "present"
    assert report_current["current_pair"]["baseline_spec_id"] == base_spec["spec_id"]
    assert report_current["current_pair"]["candidate_spec_id"] == cand_spec["spec_id"]
    spec_rows = {row["spec_id"]: row for row in report_current["spec_ids"]}
    assert spec_rows[base_spec["spec_id"]]["freshness"] == "current"
    assert spec_rows[base_spec["spec_id"]]["stale"] is False
    assert spec_rows[cand_spec["spec_id"]]["freshness"] == "current"
    assert spec_rows[cand_spec["spec_id"]]["stale"] is False

    # 3. Stale metadata: mutate profile_digest in readiness_binding
    stale_queue_dir = tmp_path / "stale_queue"
    stale_waiting = stale_queue_dir / "waiting"
    stale_waiting.mkdir(parents=True, exist_ok=True)
    stale_cand = json.loads(json.dumps(cand_spec))
    if "readiness_binding" in stale_cand.get("grid_point", {}):
        stale_cand["grid_point"]["readiness_binding"]["profile_digest"] = "sha256:different"
    else:
        stale_cand["grid_point"]["manifest_digest"] = "sha256:different"
    stale_cand["spec_id"] = "stale-cand-spec-id"
    (stale_waiting / "cand-stale.json").write_text(json.dumps(stale_cand))

    report_stale = readiness_report(
        tmp_path, manifest, submitted_by="har-11", queue_root=stale_queue_dir
    )
    assert report_stale["current_pair"]["status"] in {"absent", "incomplete"}
    assert report_stale["current_pair"]["baseline_spec_id"] is None
    assert report_stale["current_pair"]["candidate_spec_id"] is None
    stale_rows = {row["spec_id"]: row for row in report_stale["spec_ids"]}
    assert stale_rows["stale-cand-spec-id"]["freshness"] == "stale"
    assert stale_rows["stale-cand-spec-id"]["stale"] is True

    # 4. Missing provenance: strip readiness_binding
    missing_prov_dir = tmp_path / "missing_prov_queue"
    missing_waiting = missing_prov_dir / "waiting"
    missing_waiting.mkdir(parents=True, exist_ok=True)
    no_prov_cand = json.loads(json.dumps(cand_spec))
    no_prov_cand["grid_point"].pop("readiness_binding", None)
    no_prov_cand["spec_id"] = "no-prov-cand-id"
    (missing_waiting / "cand-no-prov.json").write_text(json.dumps(no_prov_cand))

    report_missing = readiness_report(
        tmp_path, manifest, submitted_by="har-11", queue_root=missing_prov_dir
    )
    assert report_missing["current_pair"]["status"] in {"absent", "incomplete", "unverifiable"}
    missing_rows = {row["spec_id"]: row for row in report_missing["spec_ids"]}
    assert missing_rows["no-prov-cand-id"]["freshness"] == "unknown"
    assert missing_rows["no-prov-cand-id"]["stale"] is None

    # 5. Missing arm: only baseline present, candidate absent
    inc_queue_dir = tmp_path / "inc_queue"
    inc_waiting = inc_queue_dir / "waiting"
    inc_waiting.mkdir(parents=True, exist_ok=True)
    (inc_waiting / "base.json").write_text(json.dumps(base_spec))

    report_inc = readiness_report(
        tmp_path, manifest, submitted_by="har-11", queue_root=inc_queue_dir
    )
    assert report_inc["current_pair"]["status"] == "incomplete"
    assert report_inc["current_pair"]["baseline_spec_id"] is None
    assert report_inc["current_pair"]["candidate_spec_id"] is None

    # 6. Duplicate current arm: two baseline specs matching current metadata
    amb_queue_dir = tmp_path / "amb_queue"
    amb_waiting = amb_queue_dir / "waiting"
    amb_waiting.mkdir(parents=True, exist_ok=True)
    dup_base = json.loads(json.dumps(base_spec))
    dup_base["spec_id"] = "base-spec-dup-2"
    (amb_waiting / "base1.json").write_text(json.dumps(base_spec))
    (amb_waiting / "base2.json").write_text(json.dumps(dup_base))
    (amb_waiting / "cand.json").write_text(json.dumps(cand_spec))

    report_amb = readiness_report(
        tmp_path, manifest, submitted_by="har-11", queue_root=amb_queue_dir
    )
    assert report_amb["current_pair"]["status"] == "ambiguous"
    assert report_amb["current_pair"]["baseline_spec_id"] is None
    assert report_amb["current_pair"]["candidate_spec_id"] is None


def test_current_metadata_pair_never_clears_runtime_or_approval_blocks(tmp_path: Path) -> None:
    """A metadata-current synthetic pair remains BLOCKED by runtime and human gates."""
    _policy(tmp_path)
    _make_registered_task(tmp_path, "event-summary")
    manifest = load_manifest(
        _manifest(
            tmp_path,
            comparison_id="blocks-test",
            tasks=[{"task": "library/tasks/event-summary", "canary": True}],
        )
    )
    compiled = compile_pair(tmp_path, manifest, submitted_by="har-11")
    arms = {arm["role"]: arm["spec"] for arm in compiled["arms"]}

    queue_dir = tmp_path / "queue"
    waiting = queue_dir / "waiting"
    waiting.mkdir(parents=True, exist_ok=True)
    (waiting / "base.json").write_text(json.dumps(arms["baseline"]))
    (waiting / "cand.json").write_text(json.dumps(arms["candidate"]))

    report = readiness_report(tmp_path, manifest, submitted_by="har-11", queue_root=queue_dir)
    assert report["current_pair"]["status"] == "present"
    assert report["verdict"] == "BLOCKED"

    gates = {g["name"]: g for g in report["gates"]}
    assert gates["execution_authorization"]["status"] == "BLOCKED"
    assert gates["constructor_config"]["status"] == "PASS"
    assert gates["constructor_config"]["evidence_kind"] == "declared"
    if "worker_route" in gates:
        assert gates["worker_route"]["status"] in {"BLOCKED", "UNKNOWN"}
        assert gates["worker_route"]["evidence_kind"] != "runtime"
    if "runtime_enforcement" in gates:
        assert gates["runtime_enforcement"]["status"] in {"BLOCKED", "UNKNOWN"}
        assert gates["runtime_enforcement"]["evidence_kind"] != "runtime"


def test_estimates_disclose_codex_mismatch(tmp_path: Path) -> None:
    """Readiness discloses historical Codex estimate mismatch rather than treating 2.5 as paired cost."""
    _policy(tmp_path)
    (tmp_path / "policy/canary-suite.yaml").write_text(
        "version: 1\nattempts: 3\nagents: [codex]\n"
        "members:\n"
        "  - name: event-summary\n"
        "    task_path: library/tasks/event-summary\n"
        "    task_version: 1.0.0\n"
        "    est_cost_usd: 2.5\n"
    )
    _make_registered_task(tmp_path, "event-summary")
    manifest = load_manifest(
        _manifest(
            tmp_path,
            tasks=[{"task": "library/tasks/event-summary", "canary": True}],
        )
    )
    report = readiness_report(tmp_path, manifest, submitted_by="har-11")
    estimates = report["estimates"]
    assert estimates["coverage"] == "unknown"
    assert estimates["baseline_usd"] is None
    assert estimates["candidate_usd"] is None
    assert estimates["source"] is not None
    assert len(estimates["reason"]) > 0

    gates = {g["name"]: g for g in report["gates"]}
    assert gates["estimates"]["status"] != "PASS"


def test_approval_commands_suppressed_when_blocked(tmp_path: Path) -> None:
    """Approval commands must be null in spec_ids and absent from rendered text while blocked."""
    _policy(tmp_path)
    _make_registered_task(tmp_path, "event-summary")
    manifest = load_manifest(
        _manifest(
            tmp_path,
            comparison_id="suppress-test",
            tasks=[{"task": "library/tasks/event-summary", "canary": True}],
        )
    )
    compiled = compile_pair(tmp_path, manifest, submitted_by="har-11")
    arms = {arm["role"]: arm["spec"] for arm in compiled["arms"]}

    queue_dir = tmp_path / "queue"
    waiting = queue_dir / "waiting"
    waiting.mkdir(parents=True, exist_ok=True)
    (waiting / "base.json").write_text(json.dumps(arms["baseline"]))
    (waiting / "cand.json").write_text(json.dumps(arms["candidate"]))

    report = readiness_report(tmp_path, manifest, submitted_by="har-11", queue_root=queue_dir)
    assert report["verdict"] == "BLOCKED"
    for row in report["spec_ids"]:
        assert row["approval_command"] is None

    rendered = render_pair_text(report)
    assert "uv run evallab approve" not in rendered
    assert "next (copy, do not auto-run):" not in rendered


def test_readiness_no_backend_imports_or_queue_mutations(tmp_path: Path) -> None:
    """Readiness report must not import the blocked backend or mutate queue files."""
    _policy(tmp_path)
    _make_registered_task(tmp_path, "event-summary")
    manifest = load_manifest(
        _manifest(
            tmp_path,
            comparison_id="immutable-queue-test",
            tasks=[{"task": "library/tasks/event-summary", "canary": True}],
        )
    )
    compiled = compile_pair(tmp_path, manifest, submitted_by="har-11")
    arms = {arm["role"]: arm["spec"] for arm in compiled["arms"]}

    queue_dir = tmp_path / "queue"
    waiting = queue_dir / "waiting"
    waiting.mkdir(parents=True, exist_ok=True)
    f1 = waiting / "base.json"
    f2 = waiting / "cand.json"
    f1.write_text(json.dumps(arms["baseline"]))
    f2.write_text(json.dumps(arms["candidate"]))

    # Snapshot before
    snapshot_before = {
        p.relative_to(queue_dir): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in queue_dir.rglob("*")
        if p.is_file()
    }

    # Ensure rlm_runtime is not imported before or during
    sys.modules.pop("evallab.rlm_runtime", None)

    report = readiness_report(tmp_path, manifest, submitted_by="har-11", queue_root=queue_dir)
    assert report["kind"] == "harness_paired_readiness"

    # Backend was not imported
    assert "evallab.rlm_runtime" not in sys.modules

    # Queue was completely unmutated
    snapshot_after = {
        p.relative_to(queue_dir): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in queue_dir.rglob("*")
        if p.is_file()
    }
    assert snapshot_before == snapshot_after
