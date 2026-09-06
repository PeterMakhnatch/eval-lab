from pathlib import Path

import pytest

from evallab.governance import collect_issues, declared_roots, folder_documents

pytestmark = pytest.mark.docs_consumer

DOCUMENTS = {
    "agents/missions/ACTIVE.md": "# Mission board\n\n## Now\n\n## Missions\n",
    "research/inbox/board.md": "# Board — pull, don't push\n\n# house rules\n",
    "agents/missions/TEMPLATE.md": "# Mission template\n\n| Exclusive paths | x |\n| State | ready |\n",
    "agents/STRUCTURE.md": """# Repository structure

## The map

```
eval-lab/
├── agents/ governance
├── research/ research
├── src/ software
└── tests/ tests
```

## Placement guide
""",
    "agents/WORKFLOW.md": """# Agent workflow

## The handoff file
Status: ready | building | blocked | review-wanted | done
""",
    "agents/CHECKS.md": "# Definition of Green\n\n## CI contract\n\n## Merge rule\n",
    "agents/README.md": "# agents\n",
    "research/README.md": "# research\n",
    "src/README.md": "# src\n",
    "tests/README.md": "# tests\n",
}


def seed_governance(root: Path) -> None:
    for relative, content in DOCUMENTS.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (root / "agents/handoffs").mkdir()
    (root / "research/inbox/claims").mkdir()


def _claim(root: Path, name: str = "pane-review-1.md", extra: str = "") -> Path:
    path = root / "research/inbox/claims" / name
    path.write_text(
        "item: review #1\nrole: verify changes\nwhy-me: pane (model), independent reviewer\n"
        + extra
    )
    return path


def test_declared_roots_reads_only_top_level_tree_entries() -> None:
    text = DOCUMENTS["agents/STRUCTURE.md"].replace(
        "├── agents/ governance", "├── agents/ governance\n│   └── handoffs/ nested"
    )
    assert declared_roots(text) == frozenset({"agents", "research", "src", "tests"})


def test_claim_counter_accepts_current_three_field_protocol(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    _claim(tmp_path)
    assert collect_issues(tmp_path, ("agents/WORKFLOW.md", "src/x.py")) == []


def test_root_freeze_rejects_a_tracked_undeclared_bucket(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    issues = collect_issues(tmp_path, ("containers/new/Dockerfile",))
    assert any("undeclared" in issue and "containers" in issue for issue in issues)


def test_claim_requires_intent_and_one_open_claim_per_pane(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    first = _claim(tmp_path)
    first.write_text("item: review #1\nrole: verify\nwhy-me: \n")
    _claim(tmp_path, "pane-review-2.md")
    issues = collect_issues(tmp_path, ())
    assert any("why-me" in issue for issue in issues)
    assert any("multiple open claims" in issue and "pane" in issue for issue in issues)


def test_live_handoff_requires_claim_and_cannot_be_completed(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    handoff = tmp_path / "agents/handoffs/review.md"
    handoff.write_text("Status: building\nLast: started\nNext: review\nBlockers: none\n")
    assert any("no pickup-counter claim" in issue for issue in collect_issues(tmp_path, ()))
    _claim(tmp_path, extra="handoff: agents/handoffs/review.md\n")
    assert collect_issues(tmp_path, ()) == []
    handoff.write_text("Status: done\nLast: merged\nNext: none\nBlockers: none\n")
    assert any("invalid live status" in issue for issue in collect_issues(tmp_path, ()))


def test_claim_cannot_reference_absent_or_archived_handoff(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    claim = _claim(tmp_path, extra="handoff: agents/handoffs/missing.md\n")
    assert any("does not exist" in issue for issue in collect_issues(tmp_path, ()))
    claim.write_text(claim.read_text().replace("agents/handoffs/", "agents/archive/"))
    assert any("must name a live" in issue for issue in collect_issues(tmp_path, ()))


def test_missing_authoritative_board_cannot_fall_back_to_navigation(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    (tmp_path / "research/inbox/board.md").unlink()
    assert any(
        "missing governance document: research/inbox/board.md" in issue
        for issue in collect_issues(tmp_path, ())
    )


def test_folder_documents_conforming_tree_passes(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    subpkg = tmp_path / "src/evallab/subpkg"
    subpkg.mkdir(parents=True)
    (subpkg / "module.py").write_text("# module\n")
    (subpkg / "AGENTS.md").write_text("# Subpackage\n\n## Purpose\nScope.\n")
    (tmp_path / ".github").mkdir()
    (tmp_path / "derived").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/INDEX.md").write_text("# Index\n")
    assert folder_documents(tmp_path) == []


def test_folder_documents_reports_missing_documents(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    (tmp_path / "agents/README.md").unlink()
    subpkg = tmp_path / "src/evallab/pkg"
    subpkg.mkdir(parents=True)
    (subpkg / "algo.py").write_text("# code\n")
    issues = folder_documents(tmp_path)
    assert any("missing folder document: agents/README.md" in issue for issue in issues)
    assert any("missing folder document: src/evallab/pkg/AGENTS.md" in issue for issue in issues)


def test_folder_documents_reports_over_length_documents(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    over_length = "\n".join(f"line {i}" for i in range(61))
    (tmp_path / "agents/README.md").write_text(over_length)
    issues = folder_documents(tmp_path)
    assert any("agents/README.md: document exceeds 60 lines (61)" in issue for issue in issues)


def test_folder_documents_exempt_directories_are_not_reported(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    for exempt in (".github", ".omp", ".claude", "derived", "runs", "queue", "backups", "docs"):
        (tmp_path / exempt).mkdir(parents=True, exist_ok=True)
    assert folder_documents(tmp_path) == []
