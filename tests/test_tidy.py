"""Tests for E16: evallab tidy working tree sweep."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evallab.cli import run_cli
from evallab.tidy import (
    classify_junk,
    collect_tidy_report,
    format_tidy_report,
    is_never_touch,
    run_tidy,
    sweep_branches,
    sweep_untracked_strays,
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


def test_cli_tidy_invocations(tidy_fixture_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
