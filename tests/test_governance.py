from pathlib import Path

from evallab.governance import collect_issues, declared_roots

DOCUMENTS = {
    "agents/missions/ACTIVE.md": "# Mission board\n\n## Now\n\n## Missions\n",
    "agents/missions/TEMPLATE.md": (
        "# Mission template\n\n| Exclusive paths | x |\n| State | ready |\n"
    ),
    "agents/STRUCTURE.md": """# Repository structure

## The map

```
eval-lab/
├── agents/ governance
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
}


def seed_governance(
    root: Path,
    *,
    header: str | None = None,
    register_handoff: bool = True,
) -> None:
    for relative, content in DOCUMENTS.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    handoffs = root / "agents/handoffs"
    handoffs.mkdir(parents=True)
    if header is not None:
        (handoffs / "m001-example.md").write_text(header)
        if register_handoff:
            board = root / "agents/missions/ACTIVE.md"
            board.write_text(
                board.read_text()
                + "\n| ID | Handoff | Status | Next |\n"
                + "|---|---|---|---|\n"
                + "| M001 | `agents/handoffs/m001-example.md` | ready | execute |\n"
            )


def test_declared_roots_reads_only_top_level_tree_entries() -> None:
    text = DOCUMENTS["agents/STRUCTURE.md"].replace(
        "├── agents/ governance", "├── agents/ governance\n│   └── handoffs/ nested"
    )
    assert declared_roots(text) == frozenset({"agents", "src", "tests"})


def test_governance_contract_accepts_declared_roots_and_canonical_live_header(
    tmp_path: Path,
) -> None:
    seed_governance(
        tmp_path,
        header="Status: ready\nLast: registered\nNext: execute\nBlockers: none\n",
    )
    assert collect_issues(tmp_path, ("agents/WORKFLOW.md", "src/x.py", "tests/test_x.py")) == []


def test_root_freeze_rejects_a_tracked_undeclared_bucket(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    issues = collect_issues(tmp_path, ("agents/WORKFLOW.md", "containers/new/Dockerfile"))
    assert issues == ["undeclared tracked root entries: containers"]


def test_live_handoff_rejects_noncanonical_or_completed_status(tmp_path: Path) -> None:
    seed_governance(
        tmp_path,
        header="Status: done\nLast: merged\nNext: none\nBlockers: none\n",
    )
    issues = collect_issues(tmp_path, ("agents/WORKFLOW.md",))
    assert any("invalid live status 'done'" in issue for issue in issues)


def test_live_handoff_must_have_a_durable_board_reference(tmp_path: Path) -> None:
    seed_governance(
        tmp_path,
        header="Status: ready\nLast: registered\nNext: execute\nBlockers: none\n",
        register_handoff=False,
    )
    issues = collect_issues(tmp_path, ("agents/WORKFLOW.md",))
    assert (
        "agents/handoffs/m001-example.md: live handoff is not referenced by a mission board row"
        in issues
    )


def test_live_handoff_status_must_agree_with_board_row(tmp_path: Path) -> None:
    seed_governance(
        tmp_path,
        header="Status: ready\nLast: registered\nNext: execute\nBlockers: none\n",
    )
    board = tmp_path / "agents/missions/ACTIVE.md"
    board.write_text(board.read_text().replace("| ready | execute |", "| active | execute |"))
    issues = collect_issues(tmp_path, ("agents/WORKFLOW.md",))
    assert (
        "agents/handoffs/m001-example.md: handoff status 'ready' contradicts board status 'active'"
        in issues
    )


def test_live_board_row_requires_an_existing_handoff(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    board = tmp_path / "agents/missions/ACTIVE.md"
    board.write_text(
        board.read_text()
        + "\n| ID | Handoff | Status |\n"
        + "|---|---|---|\n"
        + "| M002 | `agents/handoffs/missing.md` | ready |\n"
    )
    issues = collect_issues(tmp_path, ("agents/WORKFLOW.md",))
    assert any(
        "live handoff does not exist: agents/handoffs/missing.md" in issue
        for issue in issues
    )


def test_live_board_row_rejects_archive_reference(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    board = tmp_path / "agents/missions/ACTIVE.md"
    board.write_text(
        board.read_text()
        + "\n| ID | Handoff | State |\n"
        + "|---|---|---|\n"
        + "| M002 | `agents/archive/2026-08-23-handoffs/m002.md` | `blocked` |\n"
    )
    issues = collect_issues(tmp_path, ("agents/WORKFLOW.md",))
    assert any("live blocked row references archived handoff" in issue for issue in issues)


def test_live_board_path_must_be_its_own_exact_cell(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    board = tmp_path / "agents/missions/ACTIVE.md"
    board.write_text(
        board.read_text()
        + "\n| ID | Evidence | Status |\n"
        + "|---|---|---|\n"
        + "| M002 | source agents/handoffs/missing.md | review |\n"
    )
    issues = collect_issues(tmp_path, ("agents/WORKFLOW.md",))
    assert any("must reference exactly one live handoff path; found 0" in issue for issue in issues)


def test_unknown_board_status_is_not_exempted(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    board = tmp_path / "agents/missions/ACTIVE.md"
    board.write_text(
        board.read_text()
        + "\n| ID | Handoff | Status |\n"
        + "|---|---|---|\n"
        + "| M002 | — | dispatched |\n"
    )
    issues = collect_issues(tmp_path, ("agents/WORKFLOW.md",))
    assert any("unknown board status 'dispatched'" in issue for issue in issues)


def test_required_governance_marker_cannot_disappear(tmp_path: Path) -> None:
    seed_governance(tmp_path)
    (tmp_path / "agents/CHECKS.md").write_text("# Definition of Green\n")
    issues = collect_issues(tmp_path, ("agents/WORKFLOW.md",))
    assert "agents/CHECKS.md: missing required marker '## CI contract'" in issues
    assert "agents/CHECKS.md: missing required marker '## Merge rule'" in issues
