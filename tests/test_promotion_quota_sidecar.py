"""Provenance checks for quota sidecars from the retired promotion script."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMOTED_RUNS = ROOT / "research/evidence/runs"


def test_every_promoted_codex_bundle_carries_its_quota_history() -> None:
    bundles = sorted(PROMOTED_RUNS.glob("canary-*-codex-20260815"))
    assert len(bundles) == 3
    for bundle in bundles:
        manifest = json.loads((bundle / "PROMOTION.json").read_text())
        sidecars = [e for e in manifest["files"] if e.get("rule") == "R4"]
        omitted = [e for e in manifest["files"] if e.get("action") == "omitted"]
        session_omissions = [
            e for e in omitted if "sessions" in Path(e["source_path"]).parts
        ]
        assert manifest["totals"]["quota_sidecars"] == len(sidecars) == 3
        assert len(session_omissions) == 3
        for entry in sidecars:
            path = bundle / entry["promoted_path"]
            assert path.is_file()
            assert "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() == entry["promoted_sha256"]
            document = json.loads(path.read_text())
            assert document["snapshot_count"] > 0
            assert document["source_sha256"] == entry["source_sha256"]
            assert any(e["source_sha256"] == entry["source_sha256"] for e in omitted)


def test_committed_evidence_holds_no_raw_session_file() -> None:
    sessions = [
        path.relative_to(PROMOTED_RUNS).as_posix()
        for path in PROMOTED_RUNS.rglob("*")
        if path.is_file() and "sessions" in path.parts
    ]
    assert sessions == []
