"""Tests for workflow friction synthesis artifacts."""

from __future__ import annotations

import json
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent


def test_manifest_integrity():
    manifest_path = EXP_DIR / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert "friction_raw.json" in manifest
    assert "taxonomy_summary.json" in manifest
    assert "ranked_workflows.json" in manifest
    assert "review_summary.json" in manifest
    assert "incident_summary.json" in manifest


def test_taxonomy_summary():
    tax_path = EXP_DIR / "taxonomy_summary.json"
    assert tax_path.exists()
    tax = json.loads(tax_path.read_text())
    assert len(tax) == 12  # exactly 12 categories
    total_bullets = sum(t["bullet_count"] for t in tax)
    assert total_bullets == 372


def test_ranked_workflows():
    ranked_path = EXP_DIR / "ranked_workflows.json"
    assert ranked_path.exists()
    ranked = json.loads(ranked_path.read_text())
    assert len(ranked) <= 8
    assert ranked[0]["name"].startswith("Merge Train")
    assert ranked[1]["name"].startswith("Automated Review-Fix-Review")
