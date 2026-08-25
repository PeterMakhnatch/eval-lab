"""Focused LOCA Harbor state-sharing acceptance tests.

These checks do not invoke Harbor, Docker, or model trials.  They verify
that:

1. ``adapter.materialize()`` never places the golden/expected record in the
   agent-visible state tree.
2. ``oracle.solve()`` computes the record from the clickstream and the
   verifier-only golden, and ``verify()`` returns reward ``1.0``.
3. The generated packages declare the correct artifacts, keep the verifier
   separate, and always write ``/logs/verifier/reward.txt``.
4. The generator is the sole source for all per-package copies.
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import tomllib
from pathlib import Path

import adapter
import oracle
import verify
import reset_isolation

ROOT = Path(__file__).resolve().parent
TASKS = ROOT / "tasks"
SEEDS = (42, 123, 456)
SIZES = ("8k", "64k", "128k")


def _expected_in_tree(path: Path) -> list[Path]:
    return [p for p in path.rglob("*") if p.is_file() and "expected_record" in p.name]


def test_materialize_does_not_leak_expected_record():
    for seed in SEEDS:
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task"
            golden = Path(tmp) / "golden"
            adapter.materialize(task, "8k", seed, golden_dir=golden)
            bad = _expected_in_tree(task)
            assert not bad, f"leaked expected files: {bad}"
            assert (golden / "expected_record.csv").exists()
            assert (golden / "manifest.json").exists()


def test_fresh_workspace_rewards_zero():
    with tempfile.TemporaryDirectory() as tmp:
        task = Path(tmp) / "task"
        golden = Path(tmp) / "golden"
        adapter.materialize(task, "8k", 42, golden_dir=golden)
        result = verify.verify(task_dir=task, golden_dir=golden)
        assert result["reward"] == 0.0
        assert result["assertions"]["record_exists"]
        assert not result["assertions"]["record_matches_upstream_oracle"]


def test_oracle_solve_rewards_one():
    with tempfile.TemporaryDirectory() as tmp:
        task = Path(tmp) / "task"
        golden = Path(tmp) / "golden"
        adapter.materialize(task, "8k", 42, golden_dir=golden)
        oracle.solve(task, task / "agent_workspace")
        result = verify.verify(task_dir=task, golden_dir=golden)
        assert result["reward"] == 1.0
        assert result["assertions"]["record_matches_upstream_oracle"]
        assert result["assertions"]["all_assertions"]


def test_reset_isolation_still_holds():
    result = reset_isolation.prove("8k", 42)
    assert result["isolation_pass"]


def test_generated_task_toml_and_test_sh():
    for seed in SEEDS:
        for size in SIZES:
            pkg = TASKS / f"ab-testing-seed-{seed}-{size}"
            assert pkg.exists()

            data = tomllib.loads((pkg / "task.toml").read_text())
            assert data["verifier"]["environment_mode"] == "separate"
            assert data["environment"]["network_mode"] == "public"
            artifacts = data["artifacts"]
            assert "/app/task_state/agent_workspace/record.csv" in artifacts
            assert "/app/task_state/agent_workspace/promo-assets-for-b.marker" in artifacts
            for a in artifacts:
                assert a.startswith("/app/task_state/agent_workspace/")
                assert "expected" not in a
                assert a != "/app/task_state"

            test_sh = (pkg / "tests" / "test.sh").read_text()
            assert "mkdir -p /logs/verifier" in test_sh
            assert "/logs/verifier/reward.txt" in test_sh
            assert "python3 /tests/verify.py" in test_sh

            assert (pkg / "tests" / "golden" / "expected_record.csv").exists()
            assert (pkg / "tests" / "golden" / "manifest.json").exists()


def test_generated_copies_match_source():
    """The packages are the generator's output and match the top-level source."""
    for seed in SEEDS:
        for size in SIZES:
            pkg = TASKS / f"ab-testing-seed-{seed}-{size}"
            assert pkg.exists()

            assert (pkg / "environment" / "adapter.py").read_text() == (ROOT / "adapter.py").read_text()
            assert (pkg / "environment" / "oracle.py").read_text() == (ROOT / "oracle.py").read_text()
            assert (pkg / "environment" / "emit_atif.py").read_text() == (ROOT / "emit_atif.py").read_text()
            assert (pkg / "environment" / "runtime_evidence.py").read_text() == (ROOT / "runtime_evidence.py").read_text()
            assert (pkg / "tests" / "verify.py").read_text() == (ROOT / "verify.py").read_text()

            # Verifier code must not be present in the agent image.
            assert not (pkg / "environment" / "verify.py").exists()


def test_realized_size_evidence_matches_generated_manifests():
    evidence = json.loads((ROOT / "REALIZED_SIZE_EVIDENCE.json").read_text())
    by_key = {(r["official_seed"], r["size"]): r for r in evidence["rows"]}
    assert len(by_key) == 9
    for seed in SEEDS:
        for size in SIZES:
            pkg = TASKS / f"ab-testing-seed-{seed}-{size}"
            golden_manifest = json.loads((pkg / "tests" / "golden" / "manifest.json").read_text())
            row = by_key[(seed, size)]
            assert row["row_count"] == golden_manifest["row_count"]
            assert row["state_digest"] == golden_manifest["state_digest"]
            assert row["padding_only"] is False


def test_source_manifest_includes_generator_and_hashes_match():
    manifest = json.loads((ROOT / "SOURCE_MANIFEST.json").read_text())
    for name, digest in manifest["adapter_files"].items():
        path = ROOT / name
        assert path.exists()
        sha = "sha256:" + __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        assert sha == digest, f"{name} hash mismatch"


def test_harbor_loca_config_lists_all_packages():
    config = json.loads((ROOT / "harbor-loca-config.json").read_text())
    tasks = {t["task"] for t in config["tasks"]}
    expected = {f"tasks/ab-testing-seed-{seed}-{size}" for seed in SEEDS for size in SIZES}
    assert tasks == expected
