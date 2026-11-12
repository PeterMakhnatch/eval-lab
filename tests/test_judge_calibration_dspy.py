"""Source-bound calibration evidence packs and integrity checks.

These checks read local files without importing DSPy or invoking a model.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from evallab.calibrate import (
    EvidencePackError,
    evidence_root,
    load_evidence_pack,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FAMILY = "checkout-pool-exhaustion"


def _copy_pack(tmp_path: Path, family: str) -> Path:
    """A repo root whose only content is a copy of one family's evidence pack."""
    root = tmp_path / "repo"
    shutil.copytree(evidence_root(REPO_ROOT) / family, evidence_root(root) / family)
    return root


def test_evidence_pack_is_source_bound_and_contains_service_config() -> None:
    pack = load_evidence_pack(REPO_ROOT, FAMILY)
    assert pack.source_repository.endswith("/harbor-practice")
    assert re.fullmatch(r"[0-9a-f]{40}", pack.source_revision)
    assert pack.mount == "/app/evidence"
    assert pack.digest == load_evidence_pack(REPO_ROOT, FAMILY).digest

    config = next(file for file in pack.files if file.name == "service-config.yaml")
    assert "max_connections: 10" in config.text


def test_evidence_pack_refuses_tampered_missing_or_unlisted_files(tmp_path: Path) -> None:
    root = _copy_pack(tmp_path, FAMILY)
    pack_dir = evidence_root(root) / FAMILY
    original = load_evidence_pack(root, FAMILY).digest
    assert original == load_evidence_pack(REPO_ROOT, FAMILY).digest

    config = pack_dir / "service-config.yaml"
    config.write_text(config.read_text().replace("max_connections: 10", "max_connections: 32"))
    with pytest.raises(EvidencePackError, match="service-config.yaml does not match"):
        load_evidence_pack(root, FAMILY)

    config.unlink()
    with pytest.raises(EvidencePackError, match="evidence file missing: service-config.yaml"):
        load_evidence_pack(root, FAMILY)

    shutil.copy(evidence_root(REPO_ROOT) / FAMILY / "service-config.yaml", config)
    (pack_dir / "answer-key.json").write_text("{}")
    with pytest.raises(EvidencePackError, match="unlisted files.*answer-key.json"):
        load_evidence_pack(root, FAMILY)

