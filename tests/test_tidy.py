"""Tests for E16: evallab tidy working tree sweep."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import evallab.tidy as tidy_module
from evallab.cli import run_cli
from evallab.tidy import (
    TidyReport,
    apply_deletions,
    check_branch_merged_status,
    classify_junk,
    collect_tidy_report,
    format_tidy_report,
    is_never_touch,
    parse_worktree_porcelain,
    run_tidy,
    sweep_branches,
    sweep_untracked_strays,
    sweep_worktrees,
)


def init_git_repo(path: Path) -> None:
    """Initialize a git repository for testing."""
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tester"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "tester@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / ".gitignore").write_text(".git/\n", encoding="utf-8")
    (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def tree_digest(root: Path) -> dict[str, str]:
    """Compute sha256 digests for all files outside .git."""
    digests: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            rel = p.relative_to(root).as_posix()
            digests[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return digests


@pytest.fixture
def tidy_fixture_repo(tmp_path: Path) -> Path:
    """Create a realistic repository tree with all 5 classes of findings."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    # 1. Stale worktree setup (clean merged vs dirty)
    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()

    # Stale clean worktree (merged branch)
    clean_wt = worktrees_dir / "clean-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/clean-feat", str(clean_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    # Merge role/clean-feat into main
    (clean_wt / "clean_file.txt").write_text("clean work\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=clean_wt, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "clean feat"],
        cwd=clean_wt,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "merge", "role/clean-feat"], cwd=root, check=True, capture_output=True)

    # Dirty worktree (uncommitted file)
    dirty_wt = worktrees_dir / "dirty-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/dirty-feat", str(dirty_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (dirty_wt / "uncommitted_draft.py").write_text("wip code\n", encoding="utf-8")

    # 2. Merged local branch with no active worktree
    subprocess.run(
        ["git", "branch", "role/merged-orphan", "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    # 3. Unindexed documentation with front-matter issues
    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "valid.md").write_text(
        "---\nstatus: living\naudience:\n  - builder\n  - operator\n---\n# Valid Doc\n",
        encoding="utf-8",
    )
    (docs_dir / "orphan.md").write_text(
        "# Orphan Doc without front-matter\n",
        encoding="utf-8",
    )
    # Write a committed index (stale or missing orphan)
    (docs_dir / "INDEX.md").write_text(
        "---\nstatus: living\naudience:\n  - builder\n  - analyst\n  - runner\n  - operator\n---\n"
        "<!-- generated-by: docindex v1 -->\n\n# Documentation index\n",
        encoding="utf-8",
    )

    # 4. Untracked files: recognized junk vs draft work-in-progress
    (root / "temp_scratch.tmp").write_text("temporary junk\n", encoding="utf-8")
    (root / "test_output_run.log").write_text("test logs\n", encoding="utf-8")
    (root / "new_draft_feature.py").write_text("def new_feature(): pass\n", encoding="utf-8")

    # Protected paths (never touch)
    (root / "research/evidence/promoted_run").mkdir(parents=True, exist_ok=True)
    (root / "research/evidence/promoted_run/result.json").write_text("{}", encoding="utf-8")
    (root / "policy").mkdir(parents=True, exist_ok=True)
    (root / "policy/standing-approvals.yaml").write_text("rules: []\n", encoding="utf-8")
    (root / "agents/handoffs").mkdir(parents=True, exist_ok=True)
    (root / "agents/handoffs/e16-tidy.md").write_text("Status: done\n", encoding="utf-8")

    # 5. Retention violations: Z3 hot partition (>7d), unpromoted Z1 job (>14d), events.jsonl (>30d)
    fixed_now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)

    # Z3 partition (8 days old)
    derived_dir = root / "derived/parquet/trials"
    derived_dir.mkdir(parents=True, exist_ok=True)
    parquet_file = derived_dir / "trial_001.parquet"
    parquet_file.write_text("parquet_bytes", encoding="utf-8")
    old_time = (fixed_now - timedelta(days=8)).timestamp()
    os.utime(parquet_file, (old_time, old_time))

    # Z1 unpromoted job (16 days old)
    old_job_dir = root / "runs/unpromoted-job-001"
    old_job_dir.mkdir(parents=True, exist_ok=True)
    job_finished = (fixed_now - timedelta(days=16)).isoformat()
    (old_job_dir / "result.json").write_text(
        json.dumps({"id": "job_001", "finished_at": job_finished}),
        encoding="utf-8",
    )

    # queue/events.jsonl (35 days old event)
    queue_dir = root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    old_event_ts = (fixed_now - timedelta(days=35)).isoformat()
    (queue_dir / "events.jsonl").write_text(
        json.dumps({"event": "submitted", "timestamp": old_event_ts}) + "\n",
        encoding="utf-8",
    )

    return root


def test_tidy_fixture_findings(tidy_fixture_repo: Path) -> None:
    """Assert each finding is detected in the right category with reasons."""
    fixed_now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
    report = collect_tidy_report(tidy_fixture_repo, now=fixed_now)

    # 1. Stale worktrees
    wt_names = {w.path.name: w for w in report.worktrees}
    assert "clean-wt" in wt_names
    assert wt_names["clean-wt"].status == "clean_merged"
    assert wt_names["clean-wt"].actionable is True

    assert "dirty-wt" in wt_names
    assert wt_names["dirty-wt"].status == "dirty"
    assert "dirty — skipped" in wt_names["dirty-wt"].reason
    assert wt_names["dirty-wt"].file_count == 1
    assert wt_names["dirty-wt"].actionable is False

    # 2. Local branches
    branch_names = {b.branch: b for b in report.branches}
    assert "role/merged-orphan" in branch_names
    assert "role/clean-feat" in branch_names
    assert "role/dirty-feat" in branch_names

    # 3. Unindexed docs
    doc_paths = {d.path.name: d for d in report.docs}
    assert "orphan.md" in doc_paths
    assert "missing YAML front-matter" in doc_paths["orphan.md"].reason
    for doc in report.docs:
        assert doc.actionable is False

    # 4. Untracked strays
    stray_map = {s.path.name: s for s in report.strays}
    assert "temp_scratch.tmp" in stray_map
    assert stray_map["temp_scratch.tmp"].is_junk is True
    assert stray_map["temp_scratch.tmp"].actionable is True

    assert "test_output_run.log" in stray_map
    assert stray_map["test_output_run.log"].is_junk is True
    assert stray_map["test_output_run.log"].actionable is True

    assert "new_draft_feature.py" in stray_map
    assert stray_map["new_draft_feature.py"].is_junk is False
    assert stray_map["new_draft_feature.py"].actionable is False
    assert "preserved" in stray_map["new_draft_feature.py"].reason

    # 5. Retention violations
    retention_cats = {r.category: r for r in report.retention}
    assert "z3_hot_partition" in retention_cats
    assert retention_cats["z3_hot_partition"].actionable is False
    assert "Z3 hot partition older than 7d" in retention_cats["z3_hot_partition"].reason

    assert "z1_unpromoted_job" in retention_cats
    assert retention_cats["z1_unpromoted_job"].actionable is False
    assert "unpromoted Z1 job older than 14d" in retention_cats["z1_unpromoted_job"].reason

    assert "events_log" in retention_cats
    assert retention_cats["events_log"].actionable is False
    assert "events.jsonl contains entries older than 30d" in retention_cats["events_log"].reason


def test_dry_run_deletes_nothing(tidy_fixture_repo: Path) -> None:
    """Load-bearing test: assert --dry-run mutates zero bytes and exits non-zero."""
    fixed_now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
    before_digest = tree_digest(tidy_fixture_repo)

    exit_code = run_tidy(tidy_fixture_repo, apply=False, now=fixed_now)
    assert exit_code == 1

    after_digest = tree_digest(tidy_fixture_repo)
    assert before_digest == after_digest


def test_apply_preserves_dirty_worktrees_and_drafts(tidy_fixture_repo: Path) -> None:
    """Assert --apply cleans only safe junk and preserves dirty worktrees and drafts."""
    fixed_now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)

    # Stub gh checker so merged-orphan can be deleted
    def gh_stub(branch: str, root: Path) -> tuple[bool, int | None, str | None]:
        return (True, None, None)

    exit_code = run_tidy(tidy_fixture_repo, apply=True, gh_checker=gh_stub, now=fixed_now)
    assert exit_code == 0

    # 1. Clean stale worktree deleted
    clean_wt = tidy_fixture_repo / ".worktrees/clean-wt"
    assert not clean_wt.exists()

    # 2. Dirty worktree PRESERVED
    dirty_wt = tidy_fixture_repo / ".worktrees/dirty-wt"
    assert dirty_wt.exists()
    assert (dirty_wt / "uncommitted_draft.py").is_file()

    # 3. Junk stray deleted, draft PRESERVED
    assert not (tidy_fixture_repo / "temp_scratch.tmp").exists()
    assert not (tidy_fixture_repo / "test_output_run.log").exists()
    assert (tidy_fixture_repo / "new_draft_feature.py").is_file()

    # 4. Docs and retention items PRESERVED (report only)
    assert (tidy_fixture_repo / "docs/orphan.md").is_file()
    assert (tidy_fixture_repo / "derived/parquet/trials/trial_001.parquet").is_file()
    assert (tidy_fixture_repo / "runs/unpromoted-job-001/result.json").is_file()
    assert (tidy_fixture_repo / "queue/events.jsonl").is_file()


def test_never_touch_invariants(tmp_path: Path) -> None:
    """Assert promoted evidence, policy, and handoffs are never listed for deletion."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    # Untracked files inside never-touch paths
    (root / "research/evidence/untracked.tmp").parent.mkdir(parents=True, exist_ok=True)
    (root / "research/evidence/untracked.tmp").write_text("never delete\n", encoding="utf-8")

    (root / "policy/untracked.swp").parent.mkdir(parents=True, exist_ok=True)
    (root / "policy/untracked.swp").write_text("never delete\n", encoding="utf-8")

    (root / "agents/handoffs/untracked.bak").parent.mkdir(parents=True, exist_ok=True)
    (root / "agents/handoffs/untracked.bak").write_text("never delete\n", encoding="utf-8")

    strays = sweep_untracked_strays(root)
    stray_paths = [s.path.as_posix() for s in strays]

    for p in stray_paths:
        assert not is_never_touch(p)

    assert len(strays) == 0


def test_exit_codes_clean_vs_dirty(tmp_path: Path) -> None:
    """Assert exit code is 0 on a clean repository and 1 on a dirty repository in dry-run."""
    clean_root = tmp_path / "clean_repo"
    clean_root.mkdir()
    init_git_repo(clean_root)

    # Clean repo should return 0 in dry-run
    exit_clean = run_tidy(clean_root, apply=False)
    assert exit_clean == 0

    # Add an untracked junk file
    (clean_root / "scratch.tmp").write_text("junk", encoding="utf-8")
    exit_dirty = run_tidy(clean_root, apply=False)
    assert exit_dirty == 1


def test_byte_identical_output(tidy_fixture_repo: Path) -> None:
    """Assert format_tidy_report produces byte-identical output across repeated runs."""
    fixed_now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
    report1 = collect_tidy_report(tidy_fixture_repo, now=fixed_now)
    report2 = collect_tidy_report(tidy_fixture_repo, now=fixed_now)

    out1 = format_tidy_report(report1, tidy_fixture_repo)
    out2 = format_tidy_report(report2, tidy_fixture_repo)

    assert out1 == out2
    assert len(out1) > 0


def test_branch_sweep_gh_pr_checking(tmp_path: Path) -> None:
    """Test branch sweep behavior under different gh PR states."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    subprocess.run(
        ["git", "branch", "role/with-pr", "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "role/no-pr", "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    # 1. gh reports an open PR on role/with-pr
    def gh_checker_with_pr(branch: str, r: Path) -> tuple[bool, int | None, str | None]:
        if branch == "role/with-pr":
            return (True, 42, None)
        return (True, None, None)

    findings = sweep_branches(root, gh_checker=gh_checker_with_pr)
    f_map = {f.branch: f for f in findings}
    assert f_map["role/with-pr"].status == "open_pr"
    assert f_map["role/with-pr"].actionable is False
    assert "open PR #42" in f_map["role/with-pr"].reason

    assert f_map["role/no-pr"].status == "merged_no_pr"
    assert f_map["role/no-pr"].actionable is True

    # 2. gh unavailable -> both branches preserved
    def gh_checker_unavailable(branch: str, r: Path) -> tuple[bool, int | None, str | None]:
        return (False, None, "gh not installed")

    findings_unavail = sweep_branches(root, gh_checker=gh_checker_unavailable)
    f_map_unavail = {f.branch: f for f in findings_unavail}
    assert f_map_unavail["role/no-pr"].status == "gh_unavailable"
    assert f_map_unavail["role/no-pr"].actionable is False
    assert "gh unavailable" in f_map_unavail["role/no-pr"].reason


def test_classify_junk() -> None:
    """Unit test for classify_junk signature recognition."""
    assert classify_junk(Path("foo.tmp")) is not None
    assert classify_junk(Path("foo.bak")) is not None
    assert classify_junk(Path("foo.swp")) is not None
    assert classify_junk(Path("foo.pyc")) is not None
    assert classify_junk(Path(".DS_Store")) is not None
    assert classify_junk(Path("scratch.txt")) is not None
    assert classify_junk(Path("temp.json")) is not None
    assert classify_junk(Path("tmp_test.py")) is not None
    assert classify_junk(Path("__pycache__/module.pyc")) is not None

    # Non-junk drafts
    assert classify_junk(Path("feature.py")) is None
    assert classify_junk(Path("schema.sql")) is None
    assert classify_junk(Path("README.md")) is None
    assert classify_junk(Path("config.toml")) is None


def test_cli_tidy_invocations(
    tidy_fixture_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test CLI execution for tidy subcommand."""
    # Dry run
    code_dry = run_cli(["tidy", "--dry-run"], workspace=tidy_fixture_repo)
    assert code_dry == 1
    captured_dry = capsys.readouterr()
    assert "# evallab tidy report" in captured_dry.out
    assert "Dry-run mode" in captured_dry.out

    # Apply
    code_apply = run_cli(["tidy", "--apply"], workspace=tidy_fixture_repo)
    assert code_apply == 0
    captured_apply = capsys.readouterr()
    assert "Applied Actions" in captured_apply.out


def test_current_worktree_never_appears_as_stale_or_finding(tmp_path: Path) -> None:
    """Assert the current invoking worktree is excluded entirely and never reported."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()
    current_wt = worktrees_dir / "my-current-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/current-task", str(current_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    report = collect_tidy_report(root, current_worktree=current_wt)
    assert "my-current-wt" not in [w.path.name for w in report.worktrees]

    text = format_tidy_report(report, root)
    assert "my-current-wt" not in text
    assert "## 1. Stale worktrees (0 items, 0 B)" in text


def test_dirty_worktree_reported_in_active_not_swept_and_stale_count_excludes_it(
    tmp_path: Path,
) -> None:
    """Assert a dirty worktree is reported under active/not-swept section."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()
    dirty_wt = worktrees_dir / "live-worktree"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/live-task", str(dirty_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (dirty_wt / "wip.py").write_text("print('in progress')\n", encoding="utf-8")

    report = collect_tidy_report(root)
    stale_wts = [w for w in report.worktrees if w.actionable]
    active_wts = [w for w in report.worktrees if not w.actionable]

    assert len(stale_wts) == 0
    assert len(active_wts) == 1
    assert active_wts[0].path.name == "live-worktree"
    assert active_wts[0].status == "dirty"

    text = format_tidy_report(report, root)
    assert "## 1. Stale worktrees (0 items, 0 B)" in text
    assert "## Active worktrees (not swept) (1 items" in text
    assert "live-worktree" in text
    assert "dirty — skipped (1 uncommitted file)" in text


def test_branch_with_dirty_worktree_not_labelled_merged(tmp_path: Path) -> None:
    """Assert a branch with no commits of its own and dirty worktree is not labelled merged."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()
    fresh_wt = worktrees_dir / "fresh-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/fresh-task", str(fresh_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (fresh_wt / "uncommitted.txt").write_text("live work\n", encoding="utf-8")

    report = collect_tidy_report(root)
    b_map = {b.branch: b for b in report.branches}
    assert "role/fresh-task" in b_map
    branch_finding = b_map["role/fresh-task"]
    assert branch_finding.status == "active_worktree"
    assert branch_finding.actionable is False
    assert "no commits of its own" in branch_finding.reason
    assert "merged into" not in branch_finding.reason

    text = format_tidy_report(report, root)
    assert "## 2. Merged local branches (0 items)" in text
    # Assert role/fresh-task does not appear in the Merged local branches section
    merged_section = text.split("## 2. Merged local branches")[1].split("## 3.")[0]
    assert "role/fresh-task" not in merged_section


def test_exit_code_agreement_fixtures(tmp_path: Path) -> None:
    """Assert exit is non-zero when actionable items exist, and 0 when all are preserved."""
    # Fixture 1: Preserved-only findings (dirty worktree, draft stray, orphan doc)
    preserved_repo = tmp_path / "preserved_repo"
    preserved_repo.mkdir()
    init_git_repo(preserved_repo)

    # Dirty worktree
    wt_dir = preserved_repo / ".worktrees/active-wt"
    wt_dir.parent.mkdir()
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/active-task", str(wt_dir), "main"],
        cwd=preserved_repo,
        check=True,
        capture_output=True,
    )
    (wt_dir / "draft.txt").write_text("in progress\n", encoding="utf-8")

    # Draft file (unrecognized stray)
    (preserved_repo / "my_draft_feature.py").write_text("def draft(): pass\n", encoding="utf-8")

    # Unindexed doc (report only)
    docs_dir = preserved_repo / "docs"
    docs_dir.mkdir()
    (docs_dir / "orphan.md").write_text("# Orphan\n", encoding="utf-8")

    rep_preserved = collect_tidy_report(preserved_repo)
    assert rep_preserved.total_findings_count > 0
    assert rep_preserved.actionable_count == 0

    # Dry-run exit must be 0
    exit_preserved = run_tidy(preserved_repo, apply=False)
    assert exit_preserved == 0

    # Fixture 2: Actionable items exist (junk stray file added)
    (preserved_repo / "scratch.tmp").write_text("junk\n", encoding="utf-8")
    rep_actionable = collect_tidy_report(preserved_repo)
    assert rep_actionable.actionable_count == 1

    # Dry-run exit must now be 1
    exit_actionable = run_tidy(preserved_repo, apply=False)
    assert exit_actionable == 1


def test_squash_merged_worktree_is_detected_as_stale_and_actionable(tmp_path: Path) -> None:
    """Squash-merged clean worktree is actionable and reported under stale worktrees."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()
    squash_wt = worktrees_dir / "squash-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/squash-feat", str(squash_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (squash_wt / "feature.py").write_text("def feat(): return 42\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=squash_wt, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feature implementation"],
        cwd=squash_wt,
        check=True,
        capture_output=True,
    )

    # Squash merge into main
    subprocess.run(
        ["git", "merge", "--squash", "role/squash-feat"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "squash role/squash-feat into main"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    report = collect_tidy_report(root)
    wt_names = {w.path.name: w for w in report.worktrees}
    assert "squash-wt" in wt_names
    assert wt_names["squash-wt"].actionable is True
    assert wt_names["squash-wt"].status == "clean_merged"
    assert "branch merged into" in wt_names["squash-wt"].reason

    text = format_tidy_report(report, root)
    assert "## 1. Stale worktrees (1 items" in text
    assert "squash-wt" in text


def test_multi_commit_squash_merged_worktree_is_actionable(tmp_path: Path) -> None:
    """Worktree with multiple commits squash-merged into main is clean_merged and actionable."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()
    multi_wt = worktrees_dir / "multi-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/multi-feat", str(multi_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    # Commit 1
    (multi_wt / "step1.txt").write_text("step 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=multi_wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "step 1"], cwd=multi_wt, check=True, capture_output=True)

    # Commit 2
    (multi_wt / "step2.txt").write_text("step 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=multi_wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "step 2"], cwd=multi_wt, check=True, capture_output=True)

    # Squash merge all commits of role/multi-feat into main
    subprocess.run(
        ["git", "merge", "--squash", "role/multi-feat"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "squash multi-feat"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    report = collect_tidy_report(root)
    wt_names = {w.path.name: w for w in report.worktrees}
    assert "multi-wt" in wt_names
    assert wt_names["multi-wt"].actionable is True
    assert wt_names["multi-wt"].status == "clean_merged"


def test_branch_with_unmerged_commit_is_never_actionable(tmp_path: Path) -> None:
    """Branch with even one unmerged commit is NEVER actionable (content safety)."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "partial-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/partial-feat", str(wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (wt / "file1.txt").write_text("file 1 landed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "commit 1: file 1"],
        cwd=wt,
        check=True,
        capture_output=True,
    )

    # Main squash-merges commit 1
    subprocess.run(
        ["git", "merge", "--squash", "role/partial-feat"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "squash commit 1"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    # But branch adds an unmerged commit 2
    (wt / "file2.txt").write_text("file 2 NOT landed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "commit 2: unmerged change"],
        cwd=wt,
        check=True,
        capture_output=True,
    )

    report = collect_tidy_report(root)
    wt_names = {w.path.name: w for w in report.worktrees}
    assert "partial-wt" in wt_names
    assert wt_names["partial-wt"].actionable is False
    assert wt_names["partial-wt"].status == "active_clean"
    assert "not merged into" in wt_names["partial-wt"].reason

    text = format_tidy_report(report, root)
    assert "## 1. Stale worktrees (0 items, 0 B)" in text
    assert "## Active worktrees (not swept)" in text
    assert "partial-wt" in text


def test_detached_head_worktree_is_unproven_and_not_actionable(tmp_path: Path) -> None:
    """Detached HEAD worktree is classified as unproven and NEVER actionable."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()
    detached_wt = worktrees_dir / "detached-wt"
    # Create detached worktree pointing at HEAD
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(detached_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    report = collect_tidy_report(root)
    wt_names = {w.path.name: w for w in report.worktrees}
    assert "detached-wt" in wt_names
    assert wt_names["detached-wt"].actionable is False
    assert wt_names["detached-wt"].status == "unproven"
    assert "unproven" in wt_names["detached-wt"].reason
    assert "detached" in wt_names["detached-wt"].reason

    text = format_tidy_report(report, root)
    assert "## 1. Stale worktrees (0 items, 0 B)" in text
    assert "## Active worktrees (not swept) (1 items" in text
    assert "detached-wt" in text
    assert "unproven" in text


def test_missing_branch_worktree_is_unproven_and_not_actionable(tmp_path: Path) -> None:
    """Missing/vanished branch worktree is classified as unproven and NEVER actionable."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tester"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "tester@example.com"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()
    missing_wt = worktrees_dir / "missing-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/temporary-branch", str(missing_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    # Delete the branch from refs while keeping worktree
    subprocess.run(
        ["git", "update-ref", "-d", "refs/heads/role/temporary-branch"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    report = collect_tidy_report(root)
    wt_names = {w.path.name: w for w in report.worktrees}
    assert "missing-wt" in wt_names
    assert wt_names["missing-wt"].actionable is False
    assert wt_names["missing-wt"].status == "unproven"
    assert "unproven" in wt_names["missing-wt"].reason
    assert "does not exist in local refs" in wt_names["missing-wt"].reason

    text = format_tidy_report(report, root)
    assert "## 1. Stale worktrees (0 items, 0 B)" in text
    assert "## Active worktrees (not swept) (1 items" in text
    assert "missing-wt" in text
    assert "unproven" in text


def test_dirty_merged_worktree_is_not_actionable(tmp_path: Path) -> None:
    """Dirty worktree on a merged branch is NEVER actionable."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()
    dirty_merged_wt = worktrees_dir / "dirty-merged-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/dirty-merged", str(dirty_merged_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (dirty_merged_wt / "committed.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=dirty_merged_wt,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "commit"],
        cwd=dirty_merged_wt,
        check=True,
        capture_output=True,
    )

    # Squash merge into main
    subprocess.run(
        ["git", "merge", "--squash", "role/dirty-merged"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "squash merge"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    # Add uncommitted modification to worktree
    (dirty_merged_wt / "committed.py").write_text("x = 2\n", encoding="utf-8")

    report = collect_tidy_report(root)
    wt_names = {w.path.name: w for w in report.worktrees}
    assert "dirty-merged-wt" in wt_names
    assert wt_names["dirty-merged-wt"].actionable is False
    assert wt_names["dirty-merged-wt"].status == "dirty"
    assert "dirty — skipped" in wt_names["dirty-merged-wt"].reason


def test_git_failure_classifies_unproven_rather_than_merged(tmp_path: Path) -> None:
    """Git command failure defaults to unproven, NEVER merged."""
    non_repo = tmp_path / "non_repo"
    non_repo.mkdir()

    state, reason = check_branch_merged_status(non_repo, "role/some-branch", "main")
    assert state == "unproven"
    assert "unproven" in state


def test_broken_worktree_classifies_unproven(tmp_path: Path) -> None:
    """Corrupted/broken registered worktree classifies as unproven and NEVER actionable."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()
    broken_wt = worktrees_dir / "broken-wt"
    # Registered via git, then its .git pointer corrupted: the directory still
    # exists (so Git does not report the registration prunable) but git status
    # inside it fails.
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/broken", str(broken_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (broken_wt / ".git").write_text("gitdir: /nonexistent/path/to/gitdir\n", encoding="utf-8")

    report = collect_tidy_report(root)
    wt_names = {w.path.name: w for w in report.worktrees}
    assert "broken-wt" in wt_names
    assert wt_names["broken-wt"].actionable is False
    assert wt_names["broken-wt"].status == "unproven"
    assert "unproven" in wt_names["broken-wt"].reason


@settings(max_examples=12, deadline=None)
@given(
    branch_type=st.sampled_from([
        "ancestor_merged",
        "squash_merged",
        "unmerged_extra_commit",
        "unmerged_divergent",
        "detached",
        "missing_branch",
    ]),
    is_dirty=st.booleans(),
)
def test_property_actionable_implies_provably_merged_and_clean(
    tmp_path_factory: pytest.TempPathFactory,
    branch_type: str,
    is_dirty: bool,
) -> None:
    """Property: actionable is True iff worktree is provably merged and clean."""
    root = tmp_path_factory.mktemp("prop_repo")
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tester"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "tester@example.com"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()
    wt_dir = worktrees_dir / "test-wt"

    if branch_type == "detached":
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(wt_dir), "main"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    elif branch_type == "missing_branch":
        subprocess.run(
            ["git", "worktree", "add", "-b", "role/vanish", str(wt_dir), "main"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "update-ref", "-d", "refs/heads/role/vanish"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "worktree", "add", "-b", "role/prop-branch", str(wt_dir), "main"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        (wt_dir / "code.py").write_text("def run(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=wt_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add code"],
            cwd=wt_dir,
            check=True,
            capture_output=True,
        )

        if branch_type == "ancestor_merged":
            subprocess.run(
                ["git", "merge", "role/prop-branch"],
                cwd=root,
                check=True,
                capture_output=True,
            )
        elif branch_type == "squash_merged":
            subprocess.run(
                ["git", "merge", "--squash", "role/prop-branch"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "squash branch"],
                cwd=root,
                check=True,
                capture_output=True,
            )
        elif branch_type == "unmerged_extra_commit":
            # Squash merge first commit
            subprocess.run(
                ["git", "merge", "--squash", "role/prop-branch"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "squash commit 1"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            # Add second unmerged commit
            (wt_dir / "extra.py").write_text("extra unmerged code\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=wt_dir, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "unmerged commit"],
                cwd=wt_dir,
                check=True,
                capture_output=True,
            )
        elif branch_type == "unmerged_divergent":
            # No merge into main
            pass

    if is_dirty:
        (wt_dir / "dirty_file.txt").write_text("uncommitted wip\n", encoding="utf-8")

    findings = sweep_worktrees(root)
    assert len(findings) == 1
    finding = findings[0]

    # Invariants
    if finding.actionable:
        # Actionable must strictly imply provably merged and clean
        assert finding.status == "clean_merged"
        assert not is_dirty
        assert branch_type in ("ancestor_merged", "squash_merged")
    else:
        # Non-actionable must be dirty, unmerged, or unproven
        assert finding.status in ("dirty", "active_clean", "unproven")
        if is_dirty:
            assert finding.status == "dirty"
        elif branch_type in ("detached", "missing_branch"):
            assert finding.status == "unproven"
        elif branch_type in ("unmerged_extra_commit", "unmerged_divergent"):
            assert finding.status == "active_clean"


def test_parse_worktree_porcelain_synthetic_entries() -> None:
    """Synthetic porcelain: prunable/locked reasons parsed; headerless blocks skipped."""
    output = (
        "worktree /repo/main\n"
        "HEAD 0123456789abcdef0123456789abcdef01234567\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /elsewhere/gone-wt\n"
        "HEAD aaaaffffaaaaffffaaaaffffaaaaffffaaaaffff\n"
        "branch refs/heads/role/gone\n"
        "prunable gitdir file points to non-existent location\n"
        "\n"
        "worktree /elsewhere/locked-wt\n"
        "HEAD bbbbffffbbbbffffbbbbffffbbbbffffbbbbffff\n"
        "detached\n"
        "locked experimental checkout\n"
        "\n"
        "worktree /elsewhere/quietly-locked-wt\n"
        "HEAD ccccffffccccffffccccffffccccffffccccffff\n"
        "branch refs/heads/role/quiet\n"
        "locked\n"
        "prunable gitdir file does not exist\n"
        "\n"
        "worktree /repo/bare-main\n"
        "bare\n"
        "\n"
        # Malformed: no explicit worktree header -> skipped, path never inferred.
        "HEAD ddddeeeeddddeeeeddddeeeeddddeeeeddddeeee\n"
        "branch refs/heads/ghost\n"
        "\n"
    )
    regs = {str(r.path): r for r in parse_worktree_porcelain(output, root=Path("/repo"))}

    assert set(regs) == {
        "/repo/main",
        "/elsewhere/gone-wt",
        "/elsewhere/locked-wt",
        "/elsewhere/quietly-locked-wt",
        "/repo/bare-main",
    }
    main = regs["/repo/main"]
    assert main.branch == "main"
    assert main.prunable_reason is None and main.locked_reason is None
    gone = regs["/elsewhere/gone-wt"]
    assert gone.branch == "role/gone"
    assert gone.prunable_reason == "gitdir file points to non-existent location"
    assert gone.locked_reason is None
    locked = regs["/elsewhere/locked-wt"]
    assert locked.detached is True and locked.branch is None
    assert locked.locked_reason == "experimental checkout"
    quietly = regs["/elsewhere/quietly-locked-wt"]
    assert quietly.locked_reason == "" and quietly.prunable_reason == "gitdir file does not exist"
    assert regs["/repo/bare-main"].bare is True


def test_external_linked_worktree_is_inventoried(tmp_path: Path) -> None:
    """A registered linked worktree outside the repo root is inventoried and classified."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    external_wt = tmp_path / "external-wt"  # outside the repo, not under .worktrees/
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/external-feat", str(external_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (external_wt / "ext.txt").write_text("external work\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=external_wt, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "external feature"],
        cwd=external_wt,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "merge", "--squash", "role/external-feat"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "squash external feature"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    findings = sweep_worktrees(root)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.path.resolve() == external_wt.resolve()
    assert finding.status == "clean_merged"
    assert finding.actionable is True
    assert finding.size_bytes > 0  # actionable existing worktree is measured


def test_dirty_external_worktree_is_preserved(tmp_path: Path) -> None:
    """A dirty registered worktree outside the repo root is preserved and never removed."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    external_wt = tmp_path / "external-dirty-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/external-dirty", str(external_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (external_wt / "wip.txt").write_text("uncommitted\n", encoding="utf-8")

    report = collect_tidy_report(root)
    assert len(report.worktrees) == 1
    finding = report.worktrees[0]
    assert finding.status == "dirty"
    assert finding.actionable is False
    assert finding.size_bytes == 0  # active entries are not walked

    exit_code = run_tidy(root, apply=True, gh_checker=lambda b, r: (True, None, None))
    assert exit_code == 0
    assert (external_wt / "wip.txt").is_file()
    listing = subprocess.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert str(external_wt) in listing  # registration intact


def test_git_reported_prunable_registration_is_actionable_and_pruned(tmp_path: Path) -> None:
    """Git-reported prunable registration is actionable; apply prunes via Git only."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    external_wt = tmp_path / "vanished-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/vanish-wt", str(external_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    shutil.rmtree(external_wt)  # deleted outside git: registration becomes prunable

    findings = sweep_worktrees(root)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.path.resolve() == external_wt.resolve()
    assert finding.status == "prunable"
    assert finding.actionable is True
    assert finding.size_bytes == 0  # nothing left on disk to reclaim
    assert "prunable" in finding.reason

    report = TidyReport(worktrees=findings, apply=True)
    report = apply_deletions(report, root)
    assert report.deleted_worktrees == [str(external_wt)]

    listing = subprocess.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert str(external_wt) not in listing  # registration pruned
    # Prune never deletes branches.
    still_there = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "refs/heads/role/vanish-wt"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert still_there.returncode == 0


def test_synthetic_prunable_apply_uses_git_prune_and_never_touches_live_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply for prunable entries calls git worktree prune; no live dir is targeted."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    live_wt = root / ".worktrees" / "live-wt"
    live_wt.parent.mkdir()
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/live", str(live_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (live_wt / "keep.txt").write_text("live\n", encoding="utf-8")  # dirty: preserved

    synthetic = (
        f"worktree {root}\n"
        "HEAD 0123456789abcdef0123456789abcdef01234567\n"
        "branch refs/heads/main\n"
        "\n"
        f"worktree {live_wt}\n"
        "HEAD bbbbffffbbbbffffbbbbffffbbbbffffbbbbffff\n"
        "branch refs/heads/role/live\n"
        "\n"
        "worktree /nonexistent/vanished-wt\n"
        "HEAD ccccffffccccffffccccffffccccffffccccffff\n"
        "branch refs/heads/role/vanished\n"
        "prunable gitdir file points to non-existent location\n"
        "\n"
    )
    findings = sweep_worktrees(root, porcelain_output=synthetic)
    by_name = {w.path.name: w for w in findings}
    assert set(by_name) == {"live-wt", "vanished-wt"}
    assert by_name["live-wt"].status == "dirty"
    assert by_name["live-wt"].actionable is False
    assert by_name["vanished-wt"].status == "prunable"
    assert by_name["vanished-wt"].actionable is True

    recorded: list[list[str]] = []
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        recorded.append(list(cmd))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    report = apply_deletions(TidyReport(worktrees=findings, apply=True), root)

    # Registration-only removal: prune ran, and no `worktree remove` was issued.
    assert ["git", "-C", str(root), "worktree", "prune"] in recorded
    assert not any("remove" in argv for argv in recorded)
    assert "/nonexistent/vanished-wt" in report.deleted_worktrees

    # The live directory and its registration are untouched.
    assert (live_wt / "keep.txt").is_file()
    listing = subprocess.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert str(live_wt) in listing
    assert "/nonexistent/vanished-wt" not in listing


def test_current_worktree_excluded_before_expensive_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invoking worktree is excluded before sizing; sizing runs for actionable only."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()
    current_wt = worktrees_dir / "invoking-wt"
    merged_wt = worktrees_dir / "merged-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/invoking", str(current_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/merged-task", str(merged_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (current_wt / "wip.txt").write_text("in progress\n", encoding="utf-8")  # dirty current

    size_calls: list[Path] = []
    real_sizer = tidy_module.dir_size_bytes

    def spy_size(path: Path) -> int:
        size_calls.append(Path(path))
        return real_sizer(path)

    monkeypatch.setattr(tidy_module, "dir_size_bytes", spy_size)
    findings = sweep_worktrees(root, current_worktree=current_wt)
    # Current worktree excluded entirely: absent from findings, never sized.
    assert [f.path.name for f in findings] == ["merged-wt"]
    assert findings[0].status == "clean_merged"  # branch tip == main tip
    assert findings[0].actionable is True
    # Only the actionable worktree is measured; never the current one.
    assert len(size_calls) == 1
    assert size_calls[0].resolve() == merged_wt.resolve()

def test_active_worktrees_not_recursively_sized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active entries are not walked for sizes; the report says so instead of claiming bytes."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    worktrees_dir = root / ".worktrees"
    worktrees_dir.mkdir()
    dirty_wt = worktrees_dir / "dirty-wt"
    unmerged_wt = worktrees_dir / "unmerged-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/dirty-task", str(dirty_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "role/unmerged-task", str(unmerged_wt), "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (dirty_wt / "wip.txt").write_text("wip\n", encoding="utf-8")
    (unmerged_wt / "feature.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=unmerged_wt, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "unmerged feature"],
        cwd=unmerged_wt,
        check=True,
        capture_output=True,
    )

    size_calls: list[Path] = []
    real_sizer = tidy_module.dir_size_bytes

    def spy_size(path: Path) -> int:
        size_calls.append(Path(path))
        return real_sizer(path)

    monkeypatch.setattr(tidy_module, "dir_size_bytes", spy_size)
    report = collect_tidy_report(root)

    assert size_calls == []  # nothing actionable -> no recursive sizing at all
    assert all(w.size_bytes == 0 for w in report.worktrees)
    assert {w.status for w in report.worktrees} == {"dirty", "active_clean"}

    text = format_tidy_report(report, root)
    assert "## Active worktrees (not swept) (2 items, sizes not walked)" in text
    active_block = text.split("## Active worktrees (not swept)")[1].split("\n\n")[0]
    assert "0 B)" not in active_block  # no byte totals claimed for unsized entries


def test_unregistered_directory_is_never_inventoried(tmp_path: Path) -> None:
    """Porcelain is authoritative: unregistered directories under .worktrees/ are ignored."""
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root)

    ghost = root / ".worktrees" / "ghost-wt"
    ghost.mkdir(parents=True)
    (ghost / ".git").write_text("gitdir: /nonexistent/path/to/gitdir\n", encoding="utf-8")
    (ghost / "stray.txt").write_text("not a worktree\n", encoding="utf-8")

    findings = sweep_worktrees(root)
    assert findings == []  # never inferred from directory names
