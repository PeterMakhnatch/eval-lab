from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "library/tasks/event-summary"
IGNORED_PARTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".worktrees",
    "__pycache__",
    "backups",
    "derived",
    "exports",
    "logs",
    "queue",
    "runs",
}
SECRET_PATTERNS = [
    re.compile(rb"(?<![A-Za-z0-9_])ghp_[A-Za-z0-9]{30,}"),
    re.compile(rb"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(rb"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{30,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def repository_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(part in IGNORED_PARTS for part in path.relative_to(ROOT).parts)
    ]


def contains_high_confidence_secret(data: bytes) -> bool:
    return any(pattern.search(data) for pattern in SECRET_PATTERNS)


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
    findings = [
        path.relative_to(ROOT).as_posix()
        for path in repository_files()
        if contains_high_confidence_secret(path.read_bytes())
    ]
    assert findings == []


def test_secret_scanner_ignores_embedded_task_name_fragment() -> None:
    source_path = b"runs/eventdesk-belief-revision-nop-baseline-2/result.json"
    assert not contains_high_confidence_secret(source_path)


def test_secret_scanner_detects_standalone_api_key_shape() -> None:
    token = b"sk-" + (b"x" * 40)
    assert contains_high_confidence_secret(b'OPENAI_API_KEY="' + token + b'"')


def test_quixbugs_adapter_manifest_is_python_only() -> None:
    manifest_path = ROOT / "library/adapters/quixbugs/generated/generation_manifest.json"
    manifest = json.loads(manifest_path.read_text())

    assert manifest["selection"]["language"] == "python"
    assert manifest["task_count"] == 40
    assert len(manifest["task_ids"]) == 40
    assert all(task_id.startswith("quixbugs-python-") for task_id in manifest["task_ids"])


def test_curated_evidence_files_remain_reviewable() -> None:
    evidence = ROOT / "research/evidence/runs"
    oversized = [
        path.relative_to(ROOT).as_posix()
        for path in evidence.rglob("*")
        if path.is_file() and path.stat().st_size > 2 * 1024 * 1024
    ]
    assert oversized == []


def test_subscription_helpers_never_alias_oauth_to_model_api_key() -> None:
    for relative in ("scripts/harbor-auth-env.sh", "scripts/auth-status.sh"):
        helper = (ROOT / relative).read_text()
        assert "export ANTHROPIC_API_KEY" not in helper
        assert "${ANTHROPIC_API_KEY" not in helper
