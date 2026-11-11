from __future__ import annotations

import json
from pathlib import Path


def test_audit_evidence_consumes_synthetic_and_binds_provenance(
    tmp_path: Path,
) -> None:
    """Verify audit-evidence loads runs, computes provenance binding, and reports arms."""
    import hashlib

    from evallab.task_lint import load_quality_audit_evidence

    task_dir = tmp_path / "mock-task"
    task_dir.mkdir()
    test_file = task_dir / "test.py"
    test_file.write_text("print('hello')\n")
    test_sha = hashlib.sha256(test_file.read_bytes()).hexdigest()
    test_verifier = task_dir / "tests/test_state.py"
    test_verifier.parent.mkdir(parents=True)
    test_verifier.write_text("def test_v(): pass\n")
    v_sha = hashlib.sha256(test_verifier.read_bytes()).hexdigest()

    audit_dir = tmp_path / "mock-audit"
    audit_dir.mkdir()
    (audit_dir / "run_meta.json").write_text(
        json.dumps({
            "run_id": "test-run-123",
            "meta": {
                "task_dir": str(task_dir),
                "image": "test-image:latest",
                "verifier_sha256": v_sha,
            },
        })
    )
    (audit_dir / "audit-rollup.json").write_text(
        json.dumps({
            "hashes": {
                "mock-task": {
                    "test.py": test_sha,
                    "tests/test_state.py": v_sha,
                }
            }
        })
    )
    (audit_dir / "summary.json").write_text(
        json.dumps({
            "run_id": "test-run-123",
            "findings": {
                "status": "pass",
                "per_arm": {
                    "oracle": {
                        "label": "positive reference",
                        "expected_reward": "1.0",
                    }
                },
            },
            "arms": {
                "oracle": {
                    "reward": "1.0",
                    "ctrf_summary": {"passed": 5, "tests": 5},
                }
            },
        })
    )
    oracle_arm = audit_dir / "oracle"
    oracle_arm.mkdir()
    (oracle_arm / "result.json").write_text(
        json.dumps({
            "reward": "1.0",
            "action_exit": 0,
            "verifier_exit": 0,
            "ctrf_summary": {"passed": 5, "tests": 5},
        })
    )

    evidence = load_quality_audit_evidence(audit_dir)
    assert evidence["run_id"] == "test-run-123"
    assert evidence["provenance_binding"]["status"] == "verified"
    assert evidence["provenance_binding"]["matched_files"] == 2
    assert evidence["provenance_binding"]["executed_verifier_status"] == "verified"
    assert evidence["arms"]["oracle"]["observed_reward"] == 1.0
    assert evidence["arms"]["oracle"]["execution_status"] == "completed"

    # Changed bytes must invalidate provenance binding
    test_file.write_text("print('tampered')\n")
    tampered = load_quality_audit_evidence(audit_dir)
    assert tampered["provenance_binding"]["status"] == "mismatched"
    assert "test.py" in tampered["provenance_binding"]["mismatched_files"]


def test_audit_evidence_retains_declared_arms_without_result_json(tmp_path: Path) -> None:
    """Declared arms missing result.json must be retained as missing/unassessed, never dropped."""
    from evallab.task_lint import load_quality_audit_evidence

    audit_dir = tmp_path / "mock-missing-arm"
    audit_dir.mkdir()
    (audit_dir / "summary.json").write_text(
        json.dumps({
            "run_id": "test-missing-1",
            "arms": {
                "declared_missing": {
                    "reward": "1.0",
                }
            },
            "findings": {
                "per_arm": {
                    "declared_missing": {
                        "label": "should be preserved",
                        "expected_reward": "1.0",
                    }
                }
            }
        })
    )
    # Companion verifier directories are not arms. An undeclared execution
    # receipt is an arm, while an explicitly declared missing arm stays visible.
    for name in ("tests", "original-tests"):
        (audit_dir / name).mkdir()
        (audit_dir / name / "test_state.py").write_text("def test_v(): pass\n")
    (audit_dir / "undeclared").mkdir()
    (audit_dir / "undeclared/result.json").write_text(
        json.dumps({"reward": "0", "action_exit": 0, "verifier_exit": 0})
    )

    ev = load_quality_audit_evidence(audit_dir)
    assert set(ev["arms"]) == {"declared_missing", "undeclared"}
    assert ev["arms"]["declared_missing"]["execution_status"] == "missing"
    assert ev["arms"]["declared_missing"]["observed_reward"] == 1.0
    assert ev["arms"]["declared_missing"]["declared_label"] == "should be preserved"
    assert ev["execution_summary"]["arms_discovered"] == 2
    assert ev["execution_summary"]["completed_arms"] == 1


def test_audit_evidence_requires_complete_coverage_and_checks_inputs(tmp_path: Path) -> None:
    """Missing manifest files or input digest mismatch must prevent verified provenance."""
    import hashlib

    from evallab.task_lint import load_quality_audit_evidence

    task_dir = tmp_path / "coverage-task"
    task_dir.mkdir()
    (task_dir / "f1.txt").write_text("one\n")
    (task_dir / "input").mkdir()
    (task_dir / "input" / "in.json").write_text("input_data\n")

    audit_dir = tmp_path / "coverage-audit"
    audit_dir.mkdir()
    (audit_dir / "summary.json").write_text(json.dumps({"run_id": "cov-1"}))
    (audit_dir / "run_meta.json").write_text(
        json.dumps({
            "run_id": "cov-1",
            "meta": {
                "task_dir": str(task_dir),
                "input_digests": {
                    "in.json": hashlib.sha256(b"input_data\n").hexdigest(),
                },
            },
        })
    )
    # Manifest requires f1.txt AND f2.txt, but f2.txt is missing on disk
    (audit_dir / "task-manifest.json").write_text(
        json.dumps({
            "f1.txt": hashlib.sha256(b"one\n").hexdigest(),
            "f2.txt": "deadbeef" * 8,
        })
    )

    ev = load_quality_audit_evidence(audit_dir)
    assert ev["provenance_binding"]["status"] == "mismatched"
    assert "f2.txt" in ev["provenance_binding"]["missing_files"]

    # Add f2.txt with correct hash
    (task_dir / "f2.txt").write_text("two\n")
    (audit_dir / "task-manifest.json").write_text(
        json.dumps({
            "f1.txt": hashlib.sha256(b"one\n").hexdigest(),
            "f2.txt": hashlib.sha256(b"two\n").hexdigest(),
        })
    )
    ev_ok = load_quality_audit_evidence(audit_dir)
    assert ev_ok["provenance_binding"]["package_snapshot_status"] == "verified"
    assert ev_ok["provenance_binding"]["executed_verifier_status"] == "unbound"
    assert ev_ok["provenance_binding"]["status"] == "partial"

    # Tamper with input file
    (task_dir / "input" / "in.json").write_text("corrupted_input\n")
    ev_tampered_input = load_quality_audit_evidence(audit_dir)
    assert ev_tampered_input["provenance_binding"]["status"] == "mismatched"
    assert "in.json" in ev_tampered_input["provenance_binding"]["input_mismatches"]



def test_audit_evidence_omitted_verifier_identity_stays_partial_not_verified(tmp_path: Path) -> None:
    """When package snapshot is verified but recorded verifier identity is absent, overall status is partial, not verified."""
    import hashlib

    from evallab.task_lint import load_quality_audit_evidence

    task_dir = tmp_path / "pkg-only-task"
    task_dir.mkdir()
    (task_dir / "code.py").write_text("print('ok')\n")
    c_sha = hashlib.sha256(b"print('ok')\n").hexdigest()

    audit_dir = tmp_path / "pkg-only-audit"
    audit_dir.mkdir()
    (audit_dir / "summary.json").write_text(json.dumps({"run_id": "pkg-only-1"}))
    (audit_dir / "task-manifest.json").write_text(json.dumps({"code.py": c_sha}))
    # run_meta has NO verifier_sha256
    (audit_dir / "run_meta.json").write_text(
        json.dumps({
            "run_id": "pkg-only-1",
            "meta": {
                "task_dir": str(task_dir),
            },
        })
    )

    ev = load_quality_audit_evidence(audit_dir)
    assert ev["provenance_binding"]["package_snapshot_status"] == "verified"
    assert ev["provenance_binding"]["executed_verifier_status"] == "unbound"
    assert ev["provenance_binding"]["status"] == "partial"


def test_audit_evidence_separates_package_snapshot_from_executed_verifier(tmp_path: Path) -> None:
    """Two conditions sharing a task snapshot but executing different verifiers must not borrow or falsely verify."""
    import hashlib

    from evallab.task_lint import load_quality_audit_evidence

    task_dir = tmp_path / "shared-task-snapshot"
    task_dir.mkdir()
    (task_dir / "tests").mkdir()
    (task_dir / "task.toml").write_text("name = 'test/shared'\n")
    repaired_verifier_text = "def test_repaired(): assert True\n"
    (task_dir / "tests" / "test_state.py").write_text(repaired_verifier_text)
    repaired_sha = hashlib.sha256(repaired_verifier_text.encode()).hexdigest()
    manifest = {
        "task.toml": hashlib.sha256(b"name = 'test/shared'\n").hexdigest(),
        "tests/test_state.py": repaired_sha,
    }
    (tmp_path / "task-manifest.json").write_text(json.dumps(manifest))

    # Condition A: executed baseline/original verifier (different from snapshot)
    orig_verifier_text = "def test_original(): assert False\n"
    orig_sha = hashlib.sha256(orig_verifier_text.encode()).hexdigest()
    (tmp_path / "original-tests").mkdir()
    (tmp_path / "original-tests" / "test_state.py").write_text(orig_verifier_text)

    cond_a = tmp_path / "condition-a-before"
    cond_a.mkdir()
    (cond_a / "summary.json").write_text(json.dumps({"run_id": "run-a"}))
    (cond_a / "run_meta.json").write_text(
        json.dumps({
            "run_id": "run-a",
            "meta": {
                "task_dir": str(task_dir),
                "verifier_sha256": orig_sha,
            },
        })
    )
    arm_a = cond_a / "oracle"
    arm_a.mkdir()
    (arm_a / "result.json").write_text(json.dumps({"reward": "1.0", "action_exit": 0, "verifier_exit": 0}))

    ev_a = load_quality_audit_evidence(cond_a)
    assert ev_a["provenance_binding"]["package_snapshot_status"] == "verified"
    assert ev_a["provenance_binding"]["executed_verifier_status"] == "verified"
    assert ev_a["provenance_binding"]["executed_verifier_sha256"] == orig_sha
    assert ev_a["provenance_binding"]["status"] == "verified"

    # Condition B: executed repaired verifier (matches snapshot)
    cond_b = tmp_path / "condition-b-after"
    cond_b.mkdir()
    (cond_b / "summary.json").write_text(json.dumps({"run_id": "run-b"}))
    (cond_b / "run_meta.json").write_text(
        json.dumps({
            "run_id": "run-b",
            "meta": {
                "task_dir": str(task_dir),
                "verifier_sha256": repaired_sha,
            },
        })
    )
    arm_b = cond_b / "oracle"
    arm_b.mkdir()
    (arm_b / "result.json").write_text(json.dumps({"reward": "1.0", "action_exit": 0, "verifier_exit": 0}))

    ev_b = load_quality_audit_evidence(cond_b)
    assert ev_b["provenance_binding"]["package_snapshot_status"] == "verified"
    assert ev_b["provenance_binding"]["executed_verifier_status"] == "verified"
    assert ev_b["provenance_binding"]["executed_verifier_sha256"] == repaired_sha
    assert ev_b["provenance_binding"]["status"] == "verified"

    # Condition C: recorded verifier is missing from retained candidates -> mismatched
    cond_c = tmp_path / "condition-c-unretained"
    cond_c.mkdir()
    (cond_c / "summary.json").write_text(json.dumps({"run_id": "run-c"}))
    (cond_c / "run_meta.json").write_text(
        json.dumps({
            "run_id": "run-c",
            "meta": {
                "task_dir": str(task_dir),
                "verifier_sha256": "cafebabe" * 8,
            },
        })
    )
    ev_c = load_quality_audit_evidence(cond_c)
    assert ev_c["provenance_binding"]["package_snapshot_status"] == "verified"
    assert ev_c["provenance_binding"]["executed_verifier_status"] == "mismatched"
    assert ev_c["provenance_binding"]["status"] == "mismatched"
