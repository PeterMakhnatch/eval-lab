#!/usr/bin/env python3
"""Targeted tests for C2-EXPORT-DRYRUN outputs, schema, and metrics."""

from __future__ import annotations

import json
from pathlib import Path

repo_root = Path(__file__).resolve().parents[3]
exp_dir = repo_root / "research/experiments/on-hackdrop-dryrun-20261102"


def test_schema_validity() -> None:
    schema_path = exp_dir / "export_manifest_schema.json"
    assert schema_path.is_file(), "export_manifest_schema.json missing"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema.get("title") == "HackDropSFTExportRow"
    assert "required" in schema
    for req in ("id", "task_id", "decision", "reasons", "evidence_steps", "source_record_sha256"):
        assert req in schema["required"]


def test_committable_manifests_conform_to_schema() -> None:
    schema_path = exp_dir / "export_manifest_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    allowed_decisions = set(schema["properties"]["decision"]["enum"])

    for corpus in ("c1c5", "sol_astra"):
        manifest_dir = exp_dir / "manifests" / corpus
        assert manifest_dir.is_dir(), f"missing manifest dir for {corpus}"
        for name in ("kept.jsonl", "dropped.jsonl", "review.jsonl"):
            p = manifest_dir / name
            assert p.is_file(), f"missing {p}"
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                assert isinstance(row["id"], str) and row["id"]
                assert isinstance(row["task_id"], str) and row["task_id"]
                assert row["decision"] in allowed_decisions
                assert isinstance(row["reasons"], list)
                assert isinstance(row["evidence_steps"], list)
                assert isinstance(row["source_record_sha256"], str)
                assert len(row["source_record_sha256"]) == 64
                # Strict Third-Party Data / Transcript Leaks Policy check
                assert "prompt" not in row
                assert "transcript" not in row
                assert "history" not in row
                assert "tools" not in row


def test_metrics_summary_contains_all_corpora() -> None:
    summary_path = exp_dir / "metrics_summary.json"
    assert summary_path.is_file(), "metrics_summary.json missing"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    expected_corpora = ("terminal_wrench", "cheatbench", "goodfire", "c1c5", "sol_astra", "pooled")
    for c in expected_corpora:
        assert c in summary, f"missing {c} in summary"
        for mode in ("l0_only", "l0_autoreview"):
            assert mode in summary[c]
            ev = summary[c][mode]["evaluation"]
            for m in ("drop_precision", "drop_recall", "retained_clean_fraction", "clean_false_alarm_rate"):
                assert m in ev
                assert "k" in ev[m]
                assert "n" in ev[m]
                assert "wilson_ci" in ev[m]
            counts = summary[c][mode]["counts"]
            assert counts["keep"] + counts["drop"] + counts["review"] == summary[c]["total_rollouts"]

    attestation = summary["overall_attestation"]
    assert attestation["requested"] == "google-antigravity/gemini-3.8-flash"
    assert attestation["fallbacks"] == 0
    assert attestation["served_match"] > 0
