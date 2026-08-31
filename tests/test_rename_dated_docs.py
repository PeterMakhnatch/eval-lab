"""Tests for dated document naming migration tool (scripts/rename_dated_docs.py)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Add project root and scripts directory to sys.path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import pytest  # noqa: E402
from rename_dated_docs import (  # noqa: E402
    audit_collisions,
    build_link_rewrite_plan,
    infer_date_for_undated_doc,
    inventory_repo_documents,
    is_valid_iso_date,
    parse_front_matter,
    run_migration,
    to_kebab_slug,
)


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repository fixture."""
    repo = tmp_path / "test_repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test Agent"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "agent@eval-lab.local"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def commit_file(repo: Path, rel_path: str, content: str, commit_msg: str = "Add file") -> Path:
    """Helper to write and commit a file in a git repo."""
    file_path = repo / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", str(rel_path)], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo, check=True, capture_output=True)
    return file_path


# --- Classification and Normalization Tests ---


def test_to_kebab_slug() -> None:
    assert to_kebab_slug("TUTOR_ADVERSARIAL_REVIEW") == "tutor-adversarial-review"
    assert to_kebab_slug("C1-MATCHED-CAUSAL-AUDIT") == "c1-matched-causal-audit"
    assert to_kebab_slug("feature analysis meta brief") == "feature-analysis-meta-brief"
    assert (
        to_kebab_slug("special @#$ characters & multiple   spaces")
        == "special-characters-multiple-spaces"
    )
    assert to_kebab_slug("--leading-and-trailing--") == "leading-and-trailing"


def test_is_valid_iso_date() -> None:
    assert is_valid_iso_date("2026-08-31") is True
    assert is_valid_iso_date("2026-02-29") is False  # 2026 is not a leap year
    assert is_valid_iso_date("2026-13-01") is False
    assert is_valid_iso_date("2026-08") is False
    assert is_valid_iso_date("invalid") is False


def test_parse_front_matter() -> None:
    text_with_fm = "---\ndate: 2026-08-29\nauthor: test\n---\n\n# Body content\n"
    fm, body = parse_front_matter(text_with_fm)
    assert fm == {"date": "2026-08-29", "author": "test"}
    assert body == "# Body content"

    text_no_fm = "# Plain Markdown\nWithout front matter."
    fm_none, body_none = parse_front_matter(text_no_fm)
    assert fm_none is None
    assert body_none == text_no_fm


# --- Date Inference Hierarchy Tests ---


def test_infer_date_hierarchy_front_matter(temp_git_repo: Path) -> None:
    # 1. Front matter date takes precedence
    file_path = temp_git_repo / "doc.md"
    fm = {"date": "2026-08-20", "reviewed": "2026-08-25"}
    dt, source, reasons = infer_date_for_undated_doc(file_path, Path("doc.md"), temp_git_repo, fm)
    assert dt == "2026-08-20"
    assert source == "front-matter (date)"
    assert not reasons

    # 2. Front matter reviewed if date is absent
    fm2 = {"reviewed": "2026-08-25", "retrieved": "2026-08-21"}
    dt2, source2, _ = infer_date_for_undated_doc(file_path, Path("doc.md"), temp_git_repo, fm2)
    assert dt2 == "2026-08-25"
    assert source2 == "front-matter (reviewed)"

    # 3. Front matter retrieved if date & reviewed absent
    fm3 = {"retrieved": "2026-08-21"}
    dt3, source3, _ = infer_date_for_undated_doc(file_path, Path("doc.md"), temp_git_repo, fm3)
    assert dt3 == "2026-08-21"
    assert source3 == "front-matter (retrieved)"


def test_infer_date_hierarchy_git_log_add_commit(temp_git_repo: Path) -> None:
    content = "# Git Commited Doc\nNo front matter date."
    file_path = commit_file(temp_git_repo, "research/analysis/my-doc.md", content, "Initial commit")

    dt, source, _ = infer_date_for_undated_doc(
        file_path, Path("research/analysis/my-doc.md"), temp_git_repo, None
    )
    assert is_valid_iso_date(dt)
    assert source == "git-add"


def test_infer_date_hierarchy_mtime_fallback(temp_git_repo: Path) -> None:
    untracked = temp_git_repo / "untracked.md"
    untracked.write_text("# Untracked Doc", encoding="utf-8")

    dt, source, _ = infer_date_for_undated_doc(untracked, Path("untracked.md"), temp_git_repo, None)
    assert is_valid_iso_date(dt)
    assert source == "mtime"


# --- Inventory and Classification Tests ---


def test_inventory_classification(temp_git_repo: Path) -> None:
    commit_file(temp_git_repo, "research/inbox/2026-08-26-already-good.md", "# Conformant")
    commit_file(temp_git_repo, "research/inbox/TUTOR_REVIEW_2026-08-27.md", "# Suffix")
    commit_file(
        temp_git_repo,
        "research/analysis/some-analysis.md",
        "---\ndate: 2026-08-15\n---\n# Analysis",
    )
    commit_file(temp_git_repo, "research/inbox/QUEUE.md", "# Exempt queue")

    items = inventory_repo_documents(
        repo_id="eval-lab",
        repo_root=temp_git_repo,
        target_dirs=["research/inbox", "research/analysis"],
    )

    names = {it.filename: it for it in items}
    assert "QUEUE.md" not in names  # Exempt
    assert "2026-08-26-already-good.md" in names
    assert names["2026-08-26-already-good.md"].classification == "already-conformant"
    assert names["2026-08-26-already-good.md"].needs_rename is False

    assert "TUTOR_REVIEW_2026-08-27.md" in names
    assert names["TUTOR_REVIEW_2026-08-27.md"].classification == "date-suffixed"
    assert names["TUTOR_REVIEW_2026-08-27.md"].proposed_filename == "2026-08-27-tutor-review.md"

    assert "some-analysis.md" in names
    assert names["some-analysis.md"].classification == "undated"
    assert names["some-analysis.md"].proposed_filename == "2026-08-15-some-analysis.md"


# --- Refusal Safety Audit Tests ---


def test_refused_target_collision(temp_git_repo: Path) -> None:
    # Two files that would rename to the exact same proposed filename
    commit_file(temp_git_repo, "research/analysis/doc-one-2026-08-26.md", "# One")
    commit_file(temp_git_repo, "research/analysis/DOC_ONE_2026-08-26.md", "# Two")

    items = inventory_repo_documents(
        repo_id="eval-lab",
        repo_root=temp_git_repo,
        target_dirs=["research/analysis"],
    )
    audit_collisions(items)

    assert len(items) == 2
    for it in items:
        assert it.refused is True
        assert any("Target collision" in r for r in it.refusal_reasons)


def test_refused_open_pr_diff(temp_git_repo: Path) -> None:
    commit_file(temp_git_repo, "research/analysis/pr-locked-doc.md", "# Locked")
    pr_modified = {"research/analysis/pr-locked-doc.md"}

    items = inventory_repo_documents(
        repo_id="eval-lab",
        repo_root=temp_git_repo,
        target_dirs=["research/analysis"],
        pr_modified_files=pr_modified,
    )
    assert len(items) == 1
    assert items[0].refused is True
    assert any("open PR diff" in r for r in items[0].refusal_reasons)


def test_refused_worktree_conflict(temp_git_repo: Path) -> None:
    commit_file(temp_git_repo, "research/analysis/worktree-dirty-doc.md", "# Dirty")
    wt_dirty = {"research/analysis/worktree-dirty-doc.md"}

    items = inventory_repo_documents(
        repo_id="eval-lab",
        repo_root=temp_git_repo,
        target_dirs=["research/analysis"],
        worktree_dirty_files=wt_dirty,
    )
    assert len(items) == 1
    assert items[0].refused is True
    assert any("another active worktree" in r for r in items[0].refusal_reasons)


def test_refused_inbox_conformance_risk(temp_git_repo: Path) -> None:
    # An inbox note with invalid source_type
    content = "---\nsource_type: invalid_type\n---\n# Unsafe Note"
    commit_file(temp_git_repo, "research/inbox/unsafe-note.md", content)

    items = inventory_repo_documents(
        repo_id="eval-lab",
        repo_root=temp_git_repo,
        target_dirs=["research/inbox"],
    )
    assert len(items) == 1
    assert items[0].refused is True
    assert any("invalid source_type" in r for r in items[0].refusal_reasons)


# --- Link Rewriting Tests ---


def test_inbound_link_rewriting(temp_git_repo: Path) -> None:
    # Source file to be renamed
    commit_file(temp_git_repo, "research/inbox/SPEC_CONTRACT_2026-08-20.md", "# Contract")
    # Referencing file
    commit_file(
        temp_git_repo,
        "research/analysis/2026-08-20-overview.md",
        "See [Spec](../inbox/SPEC_CONTRACT_2026-08-20.md#section) or [[SPEC_CONTRACT_2026-08-20]].",
    )

    items = inventory_repo_documents(
        repo_id="eval-lab",
        repo_root=temp_git_repo,
        target_dirs=["research/inbox", "research/analysis"],
    )
    audit_collisions(items)

    repos = [("eval-lab", temp_git_repo)]
    rewrites = build_link_rewrite_plan(items, repos)

    assert len(rewrites) == 2
    md_rw = next(r for r in rewrites if r.link_type == "markdown")
    wiki_rw = next(r for r in rewrites if r.link_type == "wikilink")

    assert md_rw.old_target == "../inbox/SPEC_CONTRACT_2026-08-20.md#section"
    assert md_rw.new_target == "../inbox/2026-08-20-spec-contract.md#section"

    assert wiki_rw.old_target == "SPEC_CONTRACT_2026-08-20"
    assert wiki_rw.new_target == "2026-08-20-spec-contract"


# --- Dry-run vs Apply Execution & Git History Preservation ---


def test_dry_run_leaves_repo_unmutated(temp_git_repo: Path, tmp_path: Path) -> None:
    commit_file(temp_git_repo, "research/inbox/DOC_2026-08-25.md", "# Doc content")
    report_file = tmp_path / "report.md"

    items, rewrites, report = run_migration(
        repo_a_path=temp_git_repo,
        repo_b_path=tmp_path / "non_existent",
        report_path=report_file,
        apply=False,
    )

    # Assert original file still exists and no new file was created
    assert (temp_git_repo / "research/inbox/DOC_2026-08-25.md").exists()
    assert not (temp_git_repo / "research/inbox/2026-08-25-doc.md").exists()
    assert report_file.exists()
    assert "Critical Preconditions" in report


def test_apply_uses_git_mv_and_preserves_history(temp_git_repo: Path, tmp_path: Path) -> None:
    # 1. Commit initial file
    orig_rel = "research/inbox/OLD_NOTE_2026-08-20.md"
    commit_file(temp_git_repo, orig_rel, "# Original Note Content\nImportant historical data.")

    # 2. Add a conformant document referencing it
    ref_rel = "research/analysis/2026-08-20-summary.md"
    commit_file(temp_git_repo, ref_rel, "Link: [Note](../inbox/OLD_NOTE_2026-08-20.md).")

    report_file = tmp_path / "report.md"

    # 3. Run apply migration
    run_migration(
        repo_a_path=temp_git_repo,
        repo_b_path=tmp_path / "non_existent",
        report_path=report_file,
        apply=True,
    )

    new_rel = "research/inbox/2026-08-20-old-note.md"
    new_abs = temp_git_repo / new_rel
    assert not (temp_git_repo / orig_rel).exists()
    assert new_abs.exists()

    # Commit the staged git mv renames to verify history preservation
    subprocess.run(
        ["git", "commit", "-m", "Apply migration renames"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    # 4. Verify git log --follow finds the original commit history
    log_res = subprocess.run(
        ["git", "log", "--follow", "--oneline", "--", new_rel],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Add file" in log_res.stdout

    # 5. Verify front-matter was enriched with required inbox fields
    fm, body = parse_front_matter(new_abs.read_text(encoding="utf-8"))
    assert fm is not None
    assert fm["date"] == "2026-08-20"
    assert fm["source_type"] in ("paper", "repo", "thread", "drive", "blog")
    assert fm["status"] in ("raw", "distilled", "superseded")
    assert is_valid_iso_date(str(fm["retrieved"]))
    assert fm["feeds"] == ["parked"]
    assert "Original Note Content" in body

    # 6. Verify link in referencing document was updated
    ref_content = (temp_git_repo / ref_rel).read_text(encoding="utf-8")
    assert "2026-08-20-old-note.md" in ref_content
    assert "OLD_NOTE_2026-08-20.md" not in ref_content
