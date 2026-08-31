"""Unit and integration tests for Automated Self-Repairing Daily Digest Generator."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure scripts module is importable
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import daily_digest  # noqa: E402


@pytest.fixture
def test_env(tmp_path: Path):
    """Set up test repository environment with repo_a, repo_b, and output directories."""
    repo_a = tmp_path / "eval-lab"
    repo_b = tmp_path / "research-context"
    output_dir = repo_a / "digests"
    watermark_path = repo_a / "runs" / "digest-state.json"

    # Create directory structure
    (repo_a / "research" / "inbox").mkdir(parents=True, exist_ok=True)
    (repo_a / "research" / "analysis").mkdir(parents=True, exist_ok=True)
    (repo_a / "research" / "explorations").mkdir(parents=True, exist_ok=True)
    (repo_b / "trajectory-analysis").mkdir(parents=True, exist_ok=True)
    (repo_b / "benchmarks").mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    return {
        "repo_a": repo_a,
        "repo_b": repo_b,
        "output_dir": output_dir,
        "watermark": watermark_path,
    }


def test_frontmatter_parsing():
    content = (
        "---\n"
        "date: 2026-08-31\n"
        "author: alice\n"
        "summary: Important findings on task evaluation.\n"
        "status: published\n"
        "---\n"
        "# Document Title\n\nContent body here."
    )
    fm, body = daily_digest.parse_frontmatter(content)
    assert fm["date"] == "2026-08-31" or str(fm["date"]) == "2026-08-31"
    assert fm["author"] == "alice"
    assert fm["summary"] == "Important findings on task evaluation."
    assert fm["status"] == "published"
    assert "# Document Title" in body


def test_digest_sections_and_formatting(test_env):
    repo_a = test_env["repo_a"]
    repo_b = test_env["repo_b"]
    output_dir = test_env["output_dir"]
    target_date = "2026-08-31"

    # Create sample research document in repo_a
    doc_path = repo_a / "research" / "analysis" / "trajectory-proof.md"
    doc_path.write_text(
        "---\n"
        "date: 2026-08-31\n"
        "time: 14:30:00\n"
        "author: researcher\n"
        "title: trajectory-proof\n"
        "summary: Trajectory hop proof demonstrated zero leakage.\n"
        "status: verified\n"
        "---\n"
        "# Proof\n",
        encoding="utf-8",
    )

    sample_prs = [
        {
            "number": 101,
            "title": "Add self-repairing daily digest",
            "state": "MERGED",
            "mergedAt": "2026-08-31T10:00:00Z",
        },
        {
            "number": 102,
            "title": "Refactor runner queue",
            "state": "OPEN",
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
            "reviewDecision": "REVIEW_REQUIRED",
            "mergeStateStatus": "CLEAN",
        },
        {
            "number": 103,
            "title": "Fix flaky docker test",
            "state": "OPEN",
            "statusCheckRollup": [{"conclusion": "FAILURE", "status": "COMPLETED"}],
            "reviewDecision": "CHANGES_REQUESTED",
            "mergeStateStatus": "DIRTY",
        },
    ]

    with (
        patch("daily_digest.query_git_head_sha", return_value=("a1b2c3d", None)),
        patch("daily_digest.query_git_commits_in_window", side_effect=[(5, None), (2, None)]),
        patch("daily_digest.query_gh_prs", return_value=(sample_prs, None)),
    ):
        digest_file = daily_digest.generate_single_digest(
            target_date=target_date,
            repo_a=repo_a,
            repo_b=repo_b,
            output_dir=output_dir,
        )

    assert digest_file.exists()
    content = digest_file.read_text(encoding="utf-8")

    # Verify Frontmatter
    assert content.startswith("---\n")
    assert "date: 2026-08-31" in content
    assert "author: digest-automation" in content
    assert "summary: Daily eval lab digest for 2026-08-31." in content
    assert "status: distilled" in content

    # Verify Title
    assert "# Eval lab digest \u2014 2026-08-31" in content

    # Verify Section 1: Morning snapshot
    assert "## Morning snapshot" in content
    assert "- origin/main: a1b2c3d" in content
    assert "- Commits merged in window: 5 in eval-lab, 2 in research-context" in content
    assert "- Open PRs: 2 open PRs (1 green, 1 failing, 1 conflicted)" in content

    # Verify Section 2: What changed and what was learned
    assert "## What changed and what was learned" in content
    assert "- PR #101: Add self-repairing daily digest" in content
    assert "- trajectory-proof: Trajectory hop proof demonstrated zero leakage." in content

    # Verify Section 3: Landed versus in flight
    assert "## Landed versus in flight" in content
    assert "### Merged PRs" in content
    assert "- PR #101: Add self-repairing daily digest" in content
    assert "### Green-and-unreviewed PRs" in content
    assert "- PR #102: Refactor runner queue" in content
    assert "### Blocked or conflicted PRs" in content
    assert "- PR #103: Fix flaky docker test" in content

    # Verify Section 4: Documents created or changed
    assert "## Documents created or changed" in content
    assert "| Time | Author | Document | Summary |" in content
    assert "|---|---|---|---|" in content
    assert "| 00:00:00 | researcher | [trajectory-proof](../research/analysis/trajectory-proof.md) | Trajectory hop proof demonstrated zero leakage. |" in content


def test_idempotency_sha256(test_env):
    repo_a = test_env["repo_a"]
    repo_b = test_env["repo_b"]
    output_dir = test_env["output_dir"]
    target_date = "2026-08-31"

    doc_path = repo_a / "research" / "analysis" / "card.md"
    doc_path.write_text(
        "---\ndate: 2026-08-31\nauthor: dev\ntitle: card\nsummary: summary text\n---\n",
        encoding="utf-8",
    )

    with (
        patch("daily_digest.query_git_head_sha", return_value=("deadbeef", None)),
        patch("daily_digest.query_git_commits_in_window", return_value=(3, None)),
        patch("daily_digest.query_gh_prs", return_value=([], None)),
    ):
        file1 = daily_digest.generate_single_digest(target_date, repo_a, repo_b, output_dir)
        content1 = file1.read_text(encoding="utf-8")
        sha1 = hashlib.sha256(content1.encode("utf-8")).hexdigest()

        file2 = daily_digest.generate_single_digest(target_date, repo_a, repo_b, output_dir)
        content2 = file2.read_text(encoding="utf-8")
        sha2 = hashlib.sha256(content2.encode("utf-8")).hexdigest()

    assert sha1 == sha2
    assert content1 == content2


def test_vault_relative_links_and_disk_resolution(test_env):
    repo_a = test_env["repo_a"]
    repo_b = test_env["repo_b"]
    output_dir = test_env["output_dir"]
    target_date = "2026-08-31"

    # Valid doc in repo_a
    valid_doc_a = repo_a / "research" / "inbox" / "new-idea.md"
    valid_doc_a.write_text(
        "---\ndate: 2026-08-31\nauthor: bob\ntitle: new-idea\nsummary: A new idea.\n---\n",
        encoding="utf-8",
    )

    # Valid doc in repo_b
    valid_doc_b = repo_b / "trajectory-analysis" / "2026-08-31-report.md"
    valid_doc_b.write_text(
        "---\ndate: 2026-08-31\nauthor: charlie\ntitle: report\nsummary: Trajectory report.\n---\n",
        encoding="utf-8",
    )

    docs = daily_digest.scan_filesystem(repo_a, repo_b, target_date, output_dir)
    assert len(docs) == 2

    # Check relative links format
    links = {doc.name: doc.relative_link for doc in docs}
    assert links["new-idea"] == "../research/inbox/new-idea.md"
    assert links["report"] == "../../research-context/trajectory-analysis/2026-08-31-report.md"

    # Check each doc actually exists on disk
    for doc in docs:
        assert doc.path.exists()
        assert doc.path.is_file()


def test_graceful_degradation_when_gh_fails_or_repo_b_absent(test_env):
    repo_a = test_env["repo_a"]
    output_dir = test_env["output_dir"]
    target_date = "2026-08-31"

    with (
        patch("daily_digest.query_git_head_sha", return_value=("1234567", None)),
        patch("daily_digest.query_git_commits_in_window", return_value=(1, None)),
        patch("daily_digest.query_gh_prs", return_value=(None, "gh CLI unavailable or not authenticated")),
    ):
        digest_file = daily_digest.generate_single_digest(
            target_date=target_date,
            repo_a=repo_a,
            repo_b=None,  # repo_b absent
            output_dir=output_dir,
        )

    content = digest_file.read_text(encoding="utf-8")
    assert "- origin/main: 1234567" in content
    assert "- Commits merged in window: 1 in eval-lab, 0 in research-context" in content
    assert "- Open PRs: unavailable: gh CLI unavailable or not authenticated" in content
    assert "unavailable: gh CLI unavailable or not authenticated" in content


def test_self_repair_and_backfill_logic(test_env):
    repo_a = test_env["repo_a"]
    output_dir = test_env["output_dir"]
    watermark_path = test_env["watermark"]

    # Write an older watermark date
    watermark_path.parent.mkdir(parents=True, exist_ok=True)
    watermark_path.write_text(
        json.dumps({"last_successful_digest_date": "2026-08-28", "generated_dates": ["2026-08-28"]}),
        encoding="utf-8",
    )

    watermark_date = daily_digest.get_watermark(watermark_path, output_dir)
    assert watermark_date == "2026-08-28"

    dates = daily_digest.determine_date_range(watermark_date, "2026-08-31", backfill=False)
    assert dates == ["2026-08-29", "2026-08-30", "2026-08-31"]

    with (
        patch("daily_digest.query_git_head_sha", return_value=("abcdef0", None)),
        patch("daily_digest.query_git_commits_in_window", return_value=(0, None)),
        patch("daily_digest.query_gh_prs", return_value=([], None)),
    ):
        exit_code = daily_digest.main([
            "--date", "2026-08-31",
            "--repo-a", str(repo_a),
            "--watermark", str(watermark_path),
            "--output-dir", str(output_dir),
        ])

    assert exit_code == 0
    assert (output_dir / "2026-08-29.md").exists()
    assert (output_dir / "2026-08-30.md").exists()
    assert (output_dir / "2026-08-31.md").exists()

    updated_state = json.loads(watermark_path.read_text(encoding="utf-8"))
    assert updated_state["last_successful_digest_date"] == "2026-08-31"
    assert set(updated_state["generated_dates"]) >= {"2026-08-28", "2026-08-29", "2026-08-30", "2026-08-31"}


def test_discover_watermark_from_existing_digests(test_env):
    output_dir = test_env["output_dir"]
    watermark_path = test_env["watermark"]

    # Pre-populate some digest files without state file
    (output_dir / "2026-08-14.md").write_text("dummy", encoding="utf-8")
    (output_dir / "2026-08-16.md").write_text("dummy", encoding="utf-8")
    (output_dir / "2026-08-15.md").write_text("dummy", encoding="utf-8")

    discovered = daily_digest.get_watermark(watermark_path, output_dir)
    assert discovered == "2026-08-16"

    dates = daily_digest.determine_date_range(discovered, "2026-08-18")
    assert dates == ["2026-08-17", "2026-08-18"]


def test_cli_argument_handling(test_env):
    repo_a = test_env["repo_a"]
    output_dir = test_env["output_dir"]
    watermark_path = test_env["watermark"]

    # Test invalid date
    exit_code_invalid = daily_digest.main(["--date", "not-a-date"])
    assert exit_code_invalid == 1

    with (
        patch("daily_digest.query_git_head_sha", return_value=("abcdef0", None)),
        patch("daily_digest.query_git_commits_in_window", return_value=(0, None)),
        patch("daily_digest.query_gh_prs", return_value=([], None)),
    ):
        exit_code = daily_digest.main([
            "--date", "2026-09-01",
            "--repo-a", str(repo_a),
            "--watermark", str(watermark_path),
            "--output-dir", str(output_dir),
        ])

    assert exit_code == 0
    assert (output_dir / "2026-09-01.md").exists()
