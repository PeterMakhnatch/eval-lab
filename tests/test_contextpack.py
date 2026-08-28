"""Tests for Context Pack Compiler (WS-B).

Covers:
- Front-matter extraction and parsing.
- Document filtering by status (living vs historical) and audience.
- Deterministic assembly and SHA-256 content hashing.
- Task facet querying from craft.parquet and CRAFT pattern generation.
- Mission brief template rendering.
- CLI subcommands (build, list-docs, json, output file).
- Repository doc front-matter validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.contextpack import (
    CHARS_PER_TOKEN,
    CONTEXTPACK_VERSION,
    HEADER_PREFIX,
    VALID_AUDIENCES,
    VALID_MISSION_TYPES,
    VALID_STATUSES,
    DocMetadata,
    TaskFacetSummary,
    build_context_pack,
    doc_priority_key,
    estimate_tokens,
    extract_title,
    main,
    parse_doc,
    parse_front_matter,
    query_task_facets,
    render_mission_brief_template,
    render_task_facets_section,
    repo_root,
    select_docs,
)


class TestFrontMatterParsing:
    """Test front-matter extraction and validation."""

    def test_parse_valid_yaml_list(self) -> None:
        content = """---
status: living
audience:
  - builder
  - analyst
---

# Architecture

This is the body content.
"""
        fm, body = parse_front_matter(content)
        assert fm is not None
        assert fm["status"] == "living"
        assert fm["audience"] == ["builder", "analyst"]
        assert body == "# Architecture\n\nThis is the body content."

    def test_parse_valid_yaml_inline_list(self) -> None:
        content = """---
status: historical
audience: [operator, runner]
---

# Incident Report
"""
        fm, body = parse_front_matter(content)
        assert fm is not None
        assert fm["status"] == "historical"
        assert fm["audience"] == ["operator", "runner"]
        assert body == "# Incident Report"

    def test_parse_valid_single_string_audience(self) -> None:
        content = """---
status: living
audience: builder
---

# Single Audience Doc
"""
        fm, body = parse_front_matter(content)
        assert fm is not None
        assert fm["status"] == "living"
        assert fm["audience"] == "builder"

    def test_parse_no_front_matter(self) -> None:
        content = """# Plain Document

No front matter here.
"""
        fm, body = parse_front_matter(content)
        assert fm is None
        assert body == content.strip()

    def test_parse_malformed_yaml(self) -> None:
        content = """---
status: [broken yaml: {unclosed
---

# Title
"""
        fm, body = parse_front_matter(content)
        assert fm is None
        assert body == content.strip()

    def test_extract_title_from_h1(self) -> None:
        body = "\n\n# Real Title\n\nSome paragraph."
        assert extract_title(body, "Fallback") == "Real Title"

    def test_extract_title_fallback(self) -> None:
        body = "Paragraph without H1.\n## H2 Section"
        assert extract_title(body, "Fallback Title") == "Fallback Title"


class TestDocSelectionAndFiltering:
    """Test discovery and filtering of living documentation."""

    def test_parse_doc_living_match(self, tmp_path: Path) -> None:
        doc_file = tmp_path / "docs" / "test-doc.md"
        doc_file.parent.mkdir(parents=True, exist_ok=True)
        doc_file.write_text(
            """---
status: living
audience:
  - builder
  - operator
---

# Test Living Doc

Prose here.
""",
            encoding="utf-8",
        )
        doc = parse_doc(doc_file, root=tmp_path)
        assert doc.status == "living"
        assert doc.audience == ("builder", "operator")
        assert doc.title == "Test Living Doc"
        assert doc.matches_mission("builder") is True
        assert doc.matches_mission("operator") is True
        assert doc.matches_mission("analyst") is False

    def test_parse_doc_historical_filtered(self, tmp_path: Path) -> None:
        doc_file = tmp_path / "docs" / "test-historical.md"
        doc_file.parent.mkdir(parents=True, exist_ok=True)
        doc_file.write_text(
            """---
status: historical
audience:
  - builder
---

# Old Doc
""",
            encoding="utf-8",
        )
        doc = parse_doc(doc_file, root=tmp_path)
        assert doc.status == "historical"
        assert doc.matches_mission("builder") is False

    def test_select_docs_for_builder(self) -> None:
        root = repo_root()
        docs = select_docs(root / "docs", "builder", root=root)
        assert len(docs) >= 5
        paths = [d.path for d in docs]
        assert "docs/task-workbench.md" in paths
        assert "docs/task-registry.md" in paths
        assert "docs/architecture.md" in paths
        assert "docs/engineering.md" in paths

        # Historical docs must NEVER be selected
        assert "docs/architecture-review-2026-08-16.md" not in paths
        assert "docs/mentor-review-2026-08.md" not in paths
        assert "docs/design-additions.md" not in paths

    def test_select_docs_for_analyst(self) -> None:
        root = repo_root()
        docs = select_docs(root / "docs", "analyst", root=root)
        paths = [d.path for d in docs]
        assert "docs/analysis-loop.md" in paths
        assert "docs/analysis-worker.md" in paths
        assert "docs/run-explorer.md" in paths
        assert "docs/research-questions.md" in paths

    def test_select_docs_for_runner(self) -> None:
        root = repo_root()
        docs = select_docs(root / "docs", "runner", root=root)
        paths = [d.path for d in docs]
        assert "docs/agent-profiles.md" in paths
        assert "docs/canaries.md" in paths
        assert "docs/execution-tiers.md" in paths
        assert "docs/quota-accounting.md" in paths

    def test_select_docs_for_operator(self) -> None:
        root = repo_root()
        docs = select_docs(root / "docs", "operator", root=root)
        paths = [d.path for d in docs]
        assert "docs/operating-manual.md" in paths
        assert "docs/fleet-tracking.md" in paths
        assert "docs/operations.md" in paths


class TestDeterminismAndAssembly:
    """Test deterministic assembly and SHA-256 content hashing."""

    @pytest.mark.parametrize("mission_type", VALID_MISSION_TYPES)
    def test_consecutive_builds_identical(self, mission_type: str) -> None:
        res1 = build_context_pack(mission_type)
        res2 = build_context_pack(mission_type)

        assert res1.content_hash == res2.content_hash
        assert res1.markdown == res2.markdown
        assert res1.content_hash.startswith("sha256:")
        assert len(res1.content_hash) == 71  # sha256: + 64 hex chars

    def test_header_structure(self) -> None:
        res = build_context_pack("builder")
        assert res.markdown.startswith(HEADER_PREFIX)
        assert "<!-- mission-type: builder -->" in res.markdown
        assert f"<!-- content-sha256: {res.content_hash} -->" in res.markdown
        assert "# Context Pack: Builder Mission" in res.markdown
        assert "## Index of Living Documentation" in res.markdown

    def test_invalid_mission_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid mission type"):
            build_context_pack("invalid_mission_role")


class TestTaskFacetsAndPatterns:
    """Test querying task facets from craft.parquet and rendering CRAFT patterns."""

    def test_query_task_facets_registered(self) -> None:
        facets = query_task_facets("terminal-bench/atrx-vep-crispr")
        if facets is not None:
            assert facets.task_ref == "terminal-bench/atrx-vep-crispr"
            assert facets.verifier_type == "hybrid"
            assert "pytest" in facets.verifier_signals
            assert "golden_file" in facets.verifier_signals
            assert "hidden_tests" in facets.anti_cheat

    def test_render_task_facets_section(self) -> None:
        summary = TaskFacetSummary(
            task_ref="sample-benchmark/demo-task",
            source_repo="test-repo",
            task_digest="sha256:11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff",
            verifier_type="pytest",
            verifier_signals=("pytest",),
            anti_cheat=("hidden_tests", "answer_outside_image"),
            answer_hiding="separate_verifier_image",
            env_languages=("python",),
            env_services_n=1,
            env_multi_container=False,
            pinned_deps=True,
            base_image_pin="digest",
            human_minutes=120,
            instruction_chars=1500,
        )
        rendered = render_task_facets_section(summary)
        assert "## Task Design Facets & CRAFT Patterns: `sample-benchmark/demo-task`" in rendered
        assert "Pytest Verifier Pattern" in rendered
        assert "Clean-Room Anti-Cheat Pattern" in rendered
        assert "`python`" in rendered

    def test_build_pack_with_task_ref(self) -> None:
        res = build_context_pack("builder", task_ref="terminal-bench/atrx-vep-crispr")
        assert res.task_ref == "terminal-bench/atrx-vep-crispr"
        assert "Target Task Reference: `terminal-bench/atrx-vep-crispr`" in res.markdown
        if res.task_facets is not None:
            assert "Task Design Facets & CRAFT Patterns" in res.markdown
            assert res.task_facets.verifier_type == "hybrid"

    def test_build_pack_with_unregistered_task(self) -> None:
        res = build_context_pack("builder", task_ref="nonexistent/task-ref-999")
        assert res.task_ref == "nonexistent/task-ref-999"
        assert res.task_facets is None
        assert "## Target Task: `nonexistent/task-ref-999`" in res.markdown


class TestMissionBriefs:
    """Test mission brief rendering for each role."""

    def test_render_builder_brief(self) -> None:
        brief = render_mission_brief_template("builder", task_ref="test/task")
        assert "Mission Brief & Execution Guide: `builder`" in brief
        assert "Objective: Authoring & Quality Certification" in brief
        assert "Task Package Layout" in brief
        assert "Local Free Controls" in brief
        assert "Workbench Certification" in brief
        assert "Target Task Reference: `test/task`" in brief

    def test_render_analyst_brief(self) -> None:
        brief = render_mission_brief_template("analyst")
        assert "Mission Brief & Execution Guide: `analyst`" in brief
        assert "Objective: Evidence & Trajectory Analysis" in brief
        assert "ATIF Citation Grounding" in brief

    def test_render_runner_brief(self) -> None:
        brief = render_mission_brief_template("runner")
        assert "Mission Brief & Execution Guide: `runner`" in brief
        assert "Objective: Experiment Execution & Queue Management" in brief
        assert "Preflight Check" in brief
        assert "Required Purpose" in brief

    def test_render_operator_brief(self) -> None:
        brief = render_mission_brief_template("operator")
        assert "Mission Brief & Execution Guide: `operator`" in brief
        assert "Objective: Platform Operations & Fleet Health" in brief
        assert "System Health Check" in brief


class TestCLI:
    """Test command-line execution."""

    def test_cli_build_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["build", "builder", "--json"])
        assert code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["generator"] == CONTEXTPACK_VERSION
        assert data["mission_type"] == "builder"
        assert data["doc_count"] >= 5
        assert data["content_hash"].startswith("sha256:")

    def test_cli_build_out_file(self, tmp_path: Path) -> None:
        out_file = tmp_path / "out" / "compiled_pack.md"
        code = main(["build", "runner", "-o", str(out_file)])
        assert code == 0
        assert out_file.is_file()
        content = out_file.read_text(encoding="utf-8")
        assert content.startswith(HEADER_PREFIX)
        assert "Context Pack: Runner Mission" in content

    def test_cli_list_docs(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["list-docs"])
        assert code == 0
        captured = capsys.readouterr()
        assert "| Path | Status | Audience | Title |" in captured.out
        assert "docs/architecture.md" in captured.out
        assert "docs/task-workbench.md" in captured.out

class TestTokenBudgetAndTruncation:
    """Test token budget calculation, priority-based truncation, and determinism."""

    def test_now_doc_is_last_dropped_category(self) -> None:
        now = DocMetadata(
            path="docs/NOW.md",
            title="Where the lab is now",
            status="living",
            audience=("builder", "analyst", "runner", "operator"),
            body="now-body",
            raw_content="raw",
            content_digest="sha256:0",
        )
        research = DocMetadata(
            path="docs/research/example.md",
            title="Research note",
            status="living",
            audience=("builder", "analyst"),
            body="research-body",
            raw_content="raw",
            content_digest="sha256:0",
        )
        assert doc_priority_key(now, "builder")[0] == 4
        assert doc_priority_key(research, "builder")[0] == 0
        assert doc_priority_key(now, "builder") > doc_priority_key(research, "builder")

    def test_truncated_builder_pack_retains_now_doc(self) -> None:
        res = build_context_pack("builder", token_budget=12_000)
        assert "docs/NOW.md" in [doc.path for doc in res.docs]

    def test_token_estimator(self) -> None:
        assert estimate_tokens("") == 0
        assert estimate_tokens("a") == 1
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("abcde") == 2
        assert estimate_tokens("a" * 400) == 100
        assert CHARS_PER_TOKEN == 4

    def test_pack_under_budget_is_byte_identical_to_unbudgeted(self) -> None:
        res_unlimited = build_context_pack("builder", token_budget=None)
        res_large_budget = build_context_pack("builder", token_budget=200_000)

        assert res_large_budget.markdown == res_unlimited.markdown
        assert res_large_budget.content_hash == res_unlimited.content_hash
        assert res_large_budget.truncated is False
        assert res_large_budget.tokens_shed == 0
        assert res_large_budget.dropped_items == ()
        header_lines = res_large_budget.markdown.split("\n# ")[0]
        assert "<!-- truncated: true -->" not in header_lines
    @pytest.mark.parametrize("mission_type", VALID_MISSION_TYPES)
    def test_pack_over_budget_is_truncated_to_at_or_below_limit(self, mission_type: str) -> None:
        budget = 12_000
        res = build_context_pack(mission_type, token_budget=budget)
        assert res.truncated is True
        assert res.estimated_tokens <= budget
        assert estimate_tokens(res.markdown) <= budget
        assert res.tokens_shed > 0
        assert len(res.dropped_items) > 0
        assert len(res.docs) < 35
        assert len(res.docs) >= 1

    def test_truncation_follows_declared_priority(self, tmp_path: Path) -> None:
        # Create custom mock docs to verify exact priority shedding order
        docs_dir = tmp_path / "docs"
        research_dir = docs_dir / "research"
        research_dir.mkdir(parents=True, exist_ok=True)

        # 1. Supplemental research note (Category 0 - most expendable)
        (research_dir / "expendable-research.md").write_text(
            """---
status: living
audience: [builder]
---
# Expendable Research
"""
            + ("Research content line.\n" * 100),
            encoding="utf-8",
        )

        # 2. General broad doc (Category 1 - multi-audience)
        (docs_dir / "broad-platform.md").write_text(
            """---
status: living
audience: [builder, analyst, runner, operator]
---
# Broad Platform
"""
            + ("Platform contract line.\n" * 100),
            encoding="utf-8",
        )

        # 3. Essential role-specific workbench (Category 3 - single audience)
        (docs_dir / "essential-workbench.md").write_text(
            """---
status: living
audience: [builder]
---
# Essential Workbench

Essential builder instructions.
""",
            encoding="utf-8",
        )

        # Build with budget that only fits the essential workbench + brief (budget=800)
        res = build_context_pack(
            "builder",
            docs_dir=docs_dir,
            root=tmp_path,
            token_budget=800,
        )

        assert res.truncated is True
        # Expendable research and broad platform should be dropped in priority order
        assert "docs/research/expendable-research.md" in res.dropped_items
        assert "docs/broad-platform.md" in res.dropped_items
        # Check exact drop order: category 0 dropped before category 1
        idx_research = res.dropped_items.index("docs/research/expendable-research.md")
        idx_broad = res.dropped_items.index("docs/broad-platform.md")
        assert idx_research < idx_broad
        retained_paths = [d.path for d in res.docs]
        assert "docs/essential-workbench.md" in retained_paths
        assert "docs/research/expendable-research.md" not in retained_paths
        assert "docs/broad-platform.md" not in retained_paths
        # Mission brief is always intact
        assert "Mission Brief & Execution Guide: `builder`" in res.markdown
        assert "Objective: Authoring & Quality Certification" in res.markdown

    def test_truncation_notice_names_dropped_items_and_tokens_shed(self) -> None:
        res = build_context_pack("builder", token_budget=12_000)

        assert "### ⚠️ Context Pack Truncation Notice" in res.markdown
        assert "Configured Token Budget**: 12,000 tokens" in res.markdown
        assert "Tokens Shed**:" in res.markdown
        assert "- **Dropped Items (in order shed)**:" in res.markdown
        for item in res.dropped_items:
            assert f"`{item}`" in res.markdown
        assert "<!-- truncated: true -->" in res.markdown
        assert "<!-- token-budget: 12000 -->" in res.markdown
        assert f"<!-- tokens-shed: {res.tokens_shed} -->" in res.markdown
        assert "## Mission Brief & Execution Guide: `builder`" in res.markdown
        assert "Task Package Layout" in res.markdown
        assert "Workbench Certification" in res.markdown

    def test_two_consecutive_builds_over_budget_are_byte_identical(self) -> None:
        res1 = build_context_pack("builder", token_budget=12_000)
        res2 = build_context_pack("builder", token_budget=12_000)

        assert res1.markdown == res2.markdown
        assert res1.content_hash == res2.content_hash
        assert res1.content_hash.startswith("sha256:")
        assert res1.to_dict() == res2.to_dict()

    def test_extreme_budget_pressure_preserves_mission_brief(self) -> None:
        # Even with an extremely tight budget (e.g. 50 tokens), mission brief survives intact
        res = build_context_pack("builder", token_budget=50)
        assert res.truncated is True
        assert len(res.docs) == 0
        assert "Mission Brief & Execution Guide: `builder`" in res.markdown
        assert "Objective: Authoring & Quality Certification" in res.markdown

    def test_cli_budget_flags(self, capsys: pytest.CaptureFixture[str]) -> None:
        code_budget = main(["build", "builder", "--budget", "5000", "--json"])
        assert code_budget == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["truncated"] is True
        assert data["token_budget"] == 5000
        assert data["estimated_tokens"] <= 5000
        assert data["tokens_shed"] > 0

        code_unlimited = main(["build", "builder", "--budget", "0", "--json"])
        assert code_unlimited == 0
        captured_unlimited = capsys.readouterr()
        data_unlimited = json.loads(captured_unlimited.out)
        assert data_unlimited["truncated"] is False
        assert data_unlimited["token_budget"] is None
        assert data_unlimited["doc_count"] >= 25


class TestRepoDocIntegrity:
    """Validate that all docs in docs/*.md follow the front-matter specification."""

    def test_all_docs_have_valid_front_matter(self) -> None:
        root = repo_root()
        docs_dir = root / "docs"
        md_files = [p for p in docs_dir.glob("*.md") if p.is_file() and not p.name.startswith(".")]

        assert len(md_files) >= 20, f"Expected at least 20 docs, found {len(md_files)}"

        for path in md_files:
            doc = parse_doc(path, root=root)
            assert (
                doc.status in VALID_STATUSES
            ), f"Doc {path.name} has invalid status '{doc.status}'"
            assert len(doc.audience) > 0, f"Doc {path.name} has empty audience"
            for aud in doc.audience:
                assert (
                    aud in VALID_AUDIENCES
                ), f"Doc {path.name} has invalid audience member '{aud}'"
            assert len(doc.title) > 0, f"Doc {path.name} has empty title"
            assert len(doc.body) > 0, f"Doc {path.name} has empty body"
