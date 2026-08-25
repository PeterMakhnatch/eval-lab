from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
ASSET = ROOT / "library/benchmarks/tau-knowledge"
MANIFEST = ASSET / "cohort.manifest.json"


def test_cohort_is_bounded_and_source_pinned() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["immutable"] is True
    assert manifest["selection"]["count"] == 8
    assert manifest["selection"]["task_ids"] == [row["task_id"] for row in manifest["tasks"]]
    assert manifest["required_upstream"]["release_tag"] == "v1.0.1"
    assert manifest["required_upstream"]["commit"] == "fc0055dc4e0a316c3f83133267fbd6faaa770992"
    assert manifest["adapter_evidence"]["harbor_repository_commit"] == "636a2d0295d3ee233666bcd7d77fa81f7f090a19"


def test_generated_runtime_overlays_attest_exact_source() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    commit = manifest["required_upstream"]["commit"]
    tag = manifest["required_upstream"]["release_tag"]
    for row in manifest["tasks"]:
        task = ASSET / "generated" / f"tau3-banking_knowledge-{row['task_id'].replace('_', '-')}"
        dockerfile = (task / "environment/runtime-server/Dockerfile").read_text(encoding="utf-8")
        assert f'--branch "{tag}"' in dockerfile
        assert commit in dockerfile


def test_luna_gate_fails_closed_without_control_status() -> None:
    env = os.environ.copy()
    env["TAU2_BENCH_ROOT"] = "/tmp/tau2-bench-v101"
    proc = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(ROOT),
            "python",
            str(ROOT / "scripts/tau_knowledge/run_controls.py"),
            "--phase",
            "luna",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "Luna gate closed" in proc.stderr
