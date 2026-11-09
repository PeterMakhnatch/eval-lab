from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from evallab.execution_contracts import load_policy

pytestmark = pytest.mark.docs_consumer

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


def repository_files(root: Path = ROOT) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in IGNORED_PARTS for part in path.relative_to(root).parts)
    ]


def contains_high_confidence_secret(data: bytes) -> bool:
    # Fixed-prefix prefilter: no pattern can match without its leading literal.
    if not any(
        prefix in data
        for prefix in (b"ghp_", b"github_pat_", b"sk-", b"-----BEGIN ")
    ):
        return False
    return any(pattern.search(data) for pattern in SECRET_PATTERNS)


def _git_paths_for(
    root: Path, *args: str, ok_codes: tuple[int, ...] = (0,)
) -> list[str] | None:
    """Run a git command that prints NUL-separated paths; None when git fails."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode not in ok_codes:
        return None
    return [os.fsdecode(part) for part in result.stdout.split(b"\0") if part]


def _git_tracked_sets(root: Path) -> tuple[set[str], set[str]]:
    """Return (all tracked paths, worktree-dirty tracked paths).

    ``git grep --cached`` scans the index's blobs (sequential pack I/O, far
    faster than ~80k individual file opens); files whose worktree content may
    differ from their index blob are reported separately so the caller reads
    them from disk.
    """
    tracked = _git_paths_for(root, "ls-files", "-z")
    if tracked is None:
        raise GitUnavailable
    dirty: set[str] = set()
    for args in (("diff", "-z", "--name-only"), ("diff", "-z", "--cached", "--name-only")):
        changed = _git_paths_for(root, *args)
        if changed is None:
            raise GitUnavailable
        dirty.update(changed)
    return set(tracked), dirty


def _git_secret_candidates(root: Path) -> list[str] | None:
    """Tracked paths whose index blob matches a token shape; None on git failure."""
    return _git_paths_for(
        root,
        "grep",
        "--cached",
        "-l",
        "-z",
        "-P",
        "-e",
        "ghp_[A-Za-z0-9]{30,}",
        "-e",
        "github_pat_[A-Za-z0-9_]{40,}",
        "-e",
        "sk-[A-Za-z0-9_-]{30,}",
        "-e",
        "-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
        ok_codes=(0, 1),  # 1 = no matches
    )


class GitUnavailable(Exception):
    """Raised when the git fast scan cannot run and the test must fall back."""


def _secret_findings(root: Path) -> list[str]:
    """Flag tracked files containing a high-confidence secret.

    ``git grep --cached`` scans every tracked blob from the object store
    (sequential pack I/O, far faster than ~80k individual file opens). The
    Python detector re-checks each candidate for the exact boundary rules and
    worktree-dirty files are read from disk. Falls back to a filesystem walk
    when git is unavailable.
    """
    try:
        tracked, dirty = _git_tracked_sets(root)
    except GitUnavailable:
        return sorted(
            path.relative_to(root).as_posix()
            for path in repository_files(root)
            if contains_high_confidence_secret(path.read_bytes())
        )

    candidates = _git_secret_candidates(root)
    if candidates is None:
        candidates = list(tracked)

    findings = {
        rel
        for rel in candidates
        if (path := root / rel).is_file()
        and contains_high_confidence_secret(path.read_bytes())
    }
    for rel in dirty:
        path = root / rel
        if path.is_file() and contains_high_confidence_secret(path.read_bytes()):
            findings.add(rel)
    return sorted(findings)


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
    assert _secret_findings(ROOT) == []


def test_secret_scanner_ignores_embedded_task_name_fragment() -> None:
    source_path = b"runs/eventdesk-belief-revision-nop-baseline-2/result.json"
    assert not contains_high_confidence_secret(source_path)


def test_secret_scanner_detects_standalone_api_key_shape() -> None:
    token = b"sk-" + (b"x" * 40)
    assert contains_high_confidence_secret(b'OPENAI_API_KEY="' + token + b'"')


def test_secret_scanner_git_fast_path_only_reports_tracked_files(tmp_path: Path) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    token = b"sk-" + (b"x" * 40)
    secret = b'OPENAI_API_KEY="' + token + b'"\n'
    (tmp_path / "tracked.env").write_bytes(secret)
    (tmp_path / "binary.dat").write_bytes(b"\0" + secret)
    (tmp_path / "embedded.txt").write_bytes(b"prefix_" + token)
    (tmp_path / "untracked.env").write_bytes(secret)
    (tmp_path / "dirty.txt").write_bytes(b"clean\n")
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "add",
            "--",
            "tracked.env",
            "binary.dat",
            "embedded.txt",
            "dirty.txt",
        ],
        check=True,
    )
    # Worktree content diverges from the index blob; the worktree bytes win.
    (tmp_path / "dirty.txt").write_bytes(secret)

    assert _secret_findings(tmp_path) == ["binary.dat", "dirty.txt", "tracked.env"]


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


def test_standing_policy_keeps_conservative_defaults() -> None:
    policy = load_policy(ROOT / "policy/standing-approvals.yaml")

    assert policy.daily_cost_ceiling_usd == 20
    assert policy.per_job_cost_ceiling_usd == 3
    assert policy.quiet_failure_rule == 3
    assert [rule.agents for rule in policy.auto_run if rule.name == "local-controls"] == [
        ["oracle", "nop"]
    ]


def test_subscription_helpers_never_alias_oauth_to_model_api_key() -> None:
    for relative in ("scripts/harbor-auth-env.sh", "scripts/auth-status.sh"):
        helper = (ROOT / relative).read_text()
        assert "export ANTHROPIC_API_KEY" not in helper
        assert "${ANTHROPIC_API_KEY" not in helper
