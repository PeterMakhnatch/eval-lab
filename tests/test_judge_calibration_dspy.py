"""Typed DSPy judge contract: evidence input, scoring, bundle construction, record identity.

No language model and no ``dspy`` import are needed: the evidence pack, metric and
bundle builder only look at files and at the typed ``judgments`` attribute of a
prediction object.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.calibrate import (
    EvidencePackError,
    check_dspy_program_state,
    evidence_root,
    load_dspy_examples,
    load_evidence_pack,
    render_evidence_pack,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FAMILY = "checkout-pool-exhaustion"


def _copy_pack(tmp_path: Path, family: str) -> Path:
    """A repo root whose only content is a copy of one family's evidence pack."""
    root = tmp_path / "repo"
    shutil.copytree(evidence_root(REPO_ROOT) / family, evidence_root(root) / family)
    return root


def test_evidence_pack_is_source_bound_and_reaches_the_judge_input() -> None:
    pack = load_evidence_pack(REPO_ROOT, FAMILY)
    assert pack.source_repository.endswith("/harbor-practice")
    assert re.fullmatch(r"[0-9a-f]{40}", pack.source_revision)
    assert pack.mount == "/app/evidence"
    assert pack.digest == load_evidence_pack(REPO_ROOT, FAMILY).digest

    example = load_dspy_examples(REPO_ROOT, FAMILY)[9]  # 10-correct-timeline-dense
    assert example.evidence == render_evidence_pack(pack)
    # The document cites service-config.yaml's pool size; the judge sees that file.
    assert "service-config.yaml" in example.document
    assert "### /app/evidence/service-config.yaml" in example.evidence
    assert "max_connections: 10" in example.evidence


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


def test_saved_program_state_from_another_input_contract_is_refused(tmp_path: Path) -> None:
    live_prefixes = [
        "Family:",
        "Rubric Json:",
        "Evidence:",
        "Document:",
        "Reasoning:",
        "Judgments:",
    ]
    program = SimpleNamespace(
        named_predictors=lambda: [
            (
                "judge.predict",
                SimpleNamespace(
                    signature=SimpleNamespace(
                        fields={
                            p: SimpleNamespace(json_schema_extra={"prefix": p})
                            for p in live_prefixes
                        }
                    )
                ),
            )
        ]
    )

    def save(prefixes: list[str]) -> Path:
        path = tmp_path / "program.json"
        state = {
            "judge.predict": {
                "signature": {"instructions": "x", "fields": [{"prefix": p} for p in prefixes]}
            }
        }
        path.write_text(json.dumps(state))
        return path

    check_dspy_program_state(program, save(live_prefixes))
    pre_evidence = [p for p in live_prefixes if p != "Evidence:"]
    with pytest.raises(ValueError, match="different input contract"):
        check_dspy_program_state(program, save(pre_evidence))


