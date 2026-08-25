from __future__ import annotations

import json
from pathlib import Path

from evallab.registry import compute_task_digests

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    REPO_ROOT / "library" / "frozen" / "terminal-bench-v3-screen" / "manifest.json"
)
SOURCE_COMMIT = "4e77c91dc523107eedd9440b659159d470209188"
EXPECTED_TASKS = {
    "bun-sourcemap-leak",
    "cargo-flight-dispatch",
    "embedding-drift-monitor",
    "foodstuff-beta-activity",
    "ico-path-patch",
}


def test_terminal_bench_v3_cohort_is_pinned_and_byte_exact() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["frozen"] is True
    assert manifest["benchmark"] == "terminal-bench"
    assert manifest["benchmark_version"] == "v3.0.0"
    assert manifest["source_commit"] == SOURCE_COMMIT
    assert manifest["source_ref"].endswith(f"@{SOURCE_COMMIT}")
    assert manifest["license"] == "Apache-2.0"
    assert manifest["task_count"] == len(manifest["tasks"]) == 5
    assert {entry["task_id"] for entry in manifest["tasks"]} == EXPECTED_TASKS

    for entry in manifest["tasks"]:
        task_path = (REPO_ROOT / entry["task_path"]).resolve()
        assert REPO_ROOT in task_path.parents
        digests = compute_task_digests(task_path)
        assert entry["task_digest"] == digests.package
        assert entry["verifier_digest"] == digests.verifier
