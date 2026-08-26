"""Tests for Trajectory Interpretation Card rendering and evallab traj card CLI command."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import pytest

from evallab.cli import run_cli
from evallab.traj_card import (
    build_traj_card_data,
    generate_traj_card,
    render_traj_card_markdown,
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def canary_trial_dir(repo_root: Path) -> Path:
    canary = repo_root / "research" / "evidence" / "runs" / "canary-transaction-reconciliation-codex-20260815" / "transaction-reconciliation__ba8ovxZ"
    if not canary.exists():
        canary = repo_root / "runs" / "canary-transaction-reconciliation-codex-20260815" / "transaction-reconciliation__ba8ovxZ"
    return canary


def test_render_traj_card_on_canary_trial(canary_trial_dir: Path, repo_root: Path) -> None:
    """Render a comprehensive Trajectory Interpretation Card for a real canary trial."""
    rendered, card = generate_traj_card(
        canary_trial_dir,
        repo_root=repo_root,
    )

    assert card.trial_name == "transaction-reconciliation__ba8ovxZ"
    assert card.status == "featured"
    assert card.final_verdict in ("PASS", "FAIL", "UNKNOWN")

    # Section assertions
    assert "# Trajectory Interpretation Card: transaction-reconciliation__ba8ovxZ" in rendered
    assert "## 1. Identity & Final Outcome" in rendered
    assert "## 2. Execution Phases" in rendered
    assert "## 3. Mechanical Baseline Metrics" in rendered
    assert "## 4. Cited Error Observations & Stderr" in rendered
    assert "## 5. Loop & Cascade Reason Codes" in rendered
    assert "## 6. Intervention Provenance" in rendered
    assert "## 7. Semantic Coverage" in rendered
    assert "## 8. Source Citations & Exact Provenance" in rendered

    # Invariant: Quality Status must report unknown when ledger is absent
    assert "- **Quality Status:** `unknown`" in rendered

    # Invariant: Mechanical metrics table includes provenance categories and screening flags
    assert "`screening_heuristic`" in rendered
    assert "`mechanical_fact`" in rendered


def test_quality_status_known_when_ledger_present(repo_root: Path) -> None:
    """When quality ledger output is present, quality status and findings are rendered."""
    with tempfile.TemporaryDirectory() as tmpdir:
        trial_dir = Path(tmpdir)
        (trial_dir / "agent").mkdir(parents=True)
        (trial_dir / "agent" / "trajectory.json").write_text(
            json.dumps({"schema_version": "ATIF-v1.4", "steps": []})
        )
        (trial_dir / "quality.json").write_text(
            json.dumps({"status": "warn", "reasons": ["excessive_tool_retries", "unverified_final_state"]})
        )

        rendered, card = generate_traj_card(trial_dir, repo_root=repo_root)

        assert card.quality.status == "warn"
        assert "excessive_tool_retries" in card.quality.reasons
        assert "- **Quality Status:** `warn` (excessive_tool_retries, unverified_final_state)" in rendered


def test_semantic_coverage_inspection_with_facts(repo_root: Path) -> None:
    """When semantic facts are present in facts/ directory, card detects analysis_ready status."""
    with tempfile.TemporaryDirectory() as tmpdir:
        trial_dir = Path(tmpdir)
        (trial_dir / "agent").mkdir(parents=True)
        (trial_dir / "agent" / "trajectory.json").write_text(
            json.dumps({"schema_version": "ATIF-v1.4", "steps": []})
        )
        facts_dir = trial_dir / "facts"
        facts_dir.mkdir()
        (facts_dir / "evidence_coverage.parquet").write_bytes(b"dummy")
        (facts_dir / "process_step_facts.parquet").write_bytes(b"dummy")

        rendered, card = generate_traj_card(trial_dir, repo_root=repo_root)

        assert card.semantic_coverage.status == "analysis_ready"
        assert "`evidence_coverage.parquet`" in rendered
        assert "`process_step_facts.parquet`" in rendered


def test_cli_traj_card_command(canary_trial_dir: Path, repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI evallab traj card renders card markdown and JSON accurately."""
    # Test Markdown rendering via CLI
    code = run_cli(["traj", "card", str(canary_trial_dir)], workspace=repo_root)
    assert code == 0
    out = capsys.readouterr().out
    assert "# Trajectory Interpretation Card:" in out
    assert "## 1. Identity & Final Outcome" in out

    # Test JSON output via CLI
    code_json = run_cli(["traj", "card", str(canary_trial_dir), "--json"], workspace=repo_root)
    assert code_json == 0
    out_json = capsys.readouterr().out
    parsed = json.loads(out_json)
    assert parsed["trial_name"] == "transaction-reconciliation__ba8ovxZ"
    assert "baseline_metrics" in parsed
    assert "quality" in parsed
    assert "semantic_coverage" in parsed
