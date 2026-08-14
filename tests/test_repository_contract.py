from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks/event-summary"
IGNORED_PARTS = {".git", ".venv", ".pytest_cache", ".ruff_cache", ".worktrees", "runs"}
SECRET_PATTERNS = [
    re.compile(rb"ghp_[A-Za-z0-9]{30,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(rb"sk-[A-Za-z0-9_-]{30,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
FORBIDDEN_JVM_NAMES = {"build.gradle", "settings.gradle", "pom.xml"}
FORBIDDEN_JVM_SUFFIXES = {".gradle", ".jar", ".java", ".kt", ".kts"}


def repository_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not any(part in IGNORED_PARTS for part in path.parts)
    ]


def test_task_has_complete_harbor_contract() -> None:
    required = [
        "instruction.md",
        "task.toml",
        "README.md",
        "environment/Dockerfile",
        "solution/solve.sh",
        "tests/Dockerfile",
        "tests/test.sh",
    ]
    assert all((TASK / relative).is_file() for relative in required)

    config = tomllib.loads((TASK / "task.toml").read_text())
    assert config["verifier"]["environment_mode"] == "separate"
    assert config["environment"]["network_mode"] == "public"
    assert 3 <= len(config["task"]["keywords"]) <= 8
    assert "/app/output/summary.json" in config["artifacts"]


def test_agent_image_does_not_contain_solution_or_tests() -> None:
    dockerfile = (TASK / "environment/Dockerfile").read_text().lower()

    assert "solution" not in dockerfile
    assert "/tests" not in dockerfile


def test_verifier_fixture_matches_initial_input() -> None:
    assert (TASK / "environment/events.jsonl").read_bytes() == (
        TASK / "tests/fixtures/events.jsonl"
    ).read_bytes()


def test_repository_has_no_high_confidence_secrets() -> None:
    findings: list[str] = []
    for path in repository_files():
        data = path.read_bytes()
        for pattern in SECRET_PATTERNS:
            if pattern.search(data):
                findings.append(path.relative_to(ROOT).as_posix())
    assert findings == []


def test_repository_contains_no_jvm_source_or_build_tooling() -> None:
    forbidden = [
        path.relative_to(ROOT).as_posix()
        for path in repository_files()
        if path.name in FORBIDDEN_JVM_NAMES or path.suffix in FORBIDDEN_JVM_SUFFIXES
    ]

    assert forbidden == []


def test_quixbugs_adapter_manifest_is_python_only() -> None:
    manifest_path = ROOT / "adapters/quixbugs/generated/generation_manifest.json"
    manifest = json.loads(manifest_path.read_text())

    assert manifest["selection"]["language"] == "python"
    assert manifest["task_count"] == 40
    assert len(manifest["task_ids"]) == 40
    assert all(task_id.startswith("quixbugs-python-") for task_id in manifest["task_ids"])


def test_curated_evidence_files_remain_reviewable() -> None:
    evidence = ROOT / "evidence/runs"
    oversized = [
        path.relative_to(ROOT).as_posix()
        for path in evidence.rglob("*")
        if path.is_file() and path.stat().st_size > 2 * 1024 * 1024
    ]
    assert oversized == []


def test_standing_policy_keeps_conservative_defaults() -> None:
    policy = (ROOT / "policy/standing-approvals.yaml").read_text()

    assert "daily_cost_ceiling_usd: 20" in policy
    assert "per_job_cost_ceiling_usd: 3" in policy
    assert "quiet_failure_rule: 3" in policy
    assert "agents: [oracle, nop]" in policy


def test_only_queue_executor_imports_the_harbor_runner() -> None:
    importers = []
    for path in (ROOT / "src/harbor_lab").glob("*.py"):
        if path.name == "runner.py":
            continue
        if "run_experiment" in path.read_text():
            importers.append(path.name)

    assert importers == ["queue.py"]

    cli = (ROOT / "src/harbor_lab/cli.py").read_text()
    assert 'tool_version("harbor")' not in cli
    assert 'tool_version("docker")' not in cli
    assert '"docker",\n' not in cli
