"""Tests for the docs/INDEX.md generator and archive sweep (WS-E item 7)."""

from __future__ import annotations

from pathlib import Path

from evallab.contextpack import DocMetadata, repo_root
from evallab.docindex import (
    GENERATED_BY_MARKER,
    check_index,
    generate_index,
    main,
    render_index,
    write_index,
)


def _write_doc(
    docs_dir: Path,
    name: str,
    *,
    status: str,
    audience: list[str],
    title: str,
    extra: str = "",
) -> Path:
    audience_yaml = "\n".join(f"  - {role}" for role in audience)
    path = docs_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstatus: {status}\naudience:\n{audience_yaml}\n---\n\n# {title}\n\n{extra}",
        encoding="utf-8",
    )
    return path


def _sample_tree(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    _write_doc(
        docs,
        "builder-live.md",
        status="living",
        audience=["builder"],
        title="Builder Living",
    )
    _write_doc(
        docs,
        "shared-live.md",
        status="living",
        audience=["builder", "analyst"],
        title="Shared Living",
    )
    _write_doc(
        docs,
        "operator-archive.md",
        status="historical",
        audience=["operator"],
        title="Operator Archive",
    )
    _write_doc(
        docs,
        "research/survey.md",
        status="historical",
        audience=["builder", "analyst"],
        title="Research Survey",
    )
    return docs


def test_generation_is_deterministic(tmp_path: Path) -> None:
    docs = _sample_tree(tmp_path)
    first = generate_index(docs_dir=docs, root=tmp_path)
    second = generate_index(docs_dir=docs, root=tmp_path)
    assert first == second
    assert GENERATED_BY_MARKER in first
    assert "generated_at" not in first
    assert "timestamp" not in first.lower()


def test_grouping_by_audience_then_status(tmp_path: Path) -> None:
    docs = _sample_tree(tmp_path)
    text = generate_index(docs_dir=docs, root=tmp_path)

    builder = text.index("## builder")
    analyst = text.index("## analyst")
    runner = text.index("## runner")
    operator = text.index("## operator")
    archive = text.index("## Archive")
    assert builder < analyst < runner < operator < archive

    builder_living = text.index("### living", builder)
    builder_historical = text.index("### historical", builder_living)
    analyst_living = text.index("### living", analyst)
    assert builder_living < builder_historical < analyst_living

    builder_block = text[builder:analyst]
    assert "`docs/builder-live.md`" in builder_block
    assert "`docs/shared-live.md`" in builder_block
    assert "`docs/research/survey.md`" in builder_block
    assert "`docs/operator-archive.md`" not in builder_block

    operator_block = text[operator:archive]
    assert "`docs/operator-archive.md`" in operator_block
    assert "### historical" in operator_block

    archive_block = text[archive:]
    assert "`docs/operator-archive.md`" in archive_block
    assert "`docs/research/survey.md`" in archive_block
    assert "`docs/builder-live.md`" not in archive_block


def test_check_fails_on_missing_front_matter(tmp_path: Path) -> None:
    docs = _sample_tree(tmp_path)
    (docs / "orphan.md").write_text("# Orphan\n\nNo front-matter.\n", encoding="utf-8")
    index_path = tmp_path / "docs" / "INDEX.md"
    write_index(index_path, docs_dir=docs, root=tmp_path)

    issues = check_index(docs_dir=docs, index_path=index_path, root=tmp_path)
    assert any(
        issue.path == "docs/orphan.md" and "missing YAML front-matter" in issue.message
        for issue in issues
    )
    assert main(["check", "--docs-dir", str(docs), "--index", str(index_path)]) == 1


def test_check_fails_on_invalid_status(tmp_path: Path) -> None:
    docs = _sample_tree(tmp_path)
    _write_doc(
        docs,
        "draft.md",
        status="draft",
        audience=["builder"],
        title="Draft Doc",
    )
    index_path = tmp_path / "docs" / "INDEX.md"
    write_index(index_path, docs_dir=docs, root=tmp_path)

    issues = check_index(docs_dir=docs, index_path=index_path, root=tmp_path)
    assert any(
        issue.path == "docs/draft.md" and "status 'draft'" in issue.message
        for issue in issues
    )


def test_check_fails_on_stale_committed_index(tmp_path: Path) -> None:
    docs = _sample_tree(tmp_path)
    index_path = tmp_path / "docs" / "INDEX.md"
    write_index(index_path, docs_dir=docs, root=tmp_path)
    assert check_index(docs_dir=docs, index_path=index_path, root=tmp_path) == []

    index_path.write_text(
        index_path.read_text(encoding="utf-8") + "\n<!-- hand edit -->\n",
        encoding="utf-8",
    )
    issues = check_index(docs_dir=docs, index_path=index_path, root=tmp_path)
    assert any("stale" in issue.message for issue in issues)
    assert main(["check", "--docs-dir", str(docs), "--index", str(index_path)]) == 1


def test_check_passes_on_real_repo_docs_tree() -> None:
    root = repo_root()
    docs = root / "docs"
    index_path = docs / "INDEX.md"
    assert index_path.is_file(), "docs/INDEX.md must be generated and committed"
    issues = check_index(docs_dir=docs, index_path=index_path, root=root)
    assert issues == []
    assert main(["check"]) == 0


def test_render_index_includes_required_marker_and_front_matter() -> None:
    text = render_index(
        [
            DocMetadata(
                path="docs/example.md",
                title="Example",
                status="living",
                audience=("runner",),
                body="body",
                raw_content="raw",
                content_digest="sha256:0",
            )
        ]
    )
    assert text.startswith("---\nstatus: living\n")
    assert "  - builder\n  - analyst\n  - runner\n  - operator\n" in text
    assert GENERATED_BY_MARKER in text
    assert text.endswith("\n")
