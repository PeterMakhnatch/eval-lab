"""Typed DSPy judge contract: evidence input, scoring, bundle construction, record identity.

No language model and no ``dspy`` import are needed: the evidence pack, metric and
bundle builder only look at files and at the typed ``judgments`` attribute of a
prediction object.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.calibrate import (
    DSPY_INPUT_FIELDS,
    RUBRICS,
    EvidencePackError,
    IncompleteJudgeOutputError,
    _record_id,
    check_dspy_program_state,
    dspy_metric,
    dspy_prediction_bundle,
    evaluate_predictions,
    evidence_root,
    load_corpus,
    load_dspy_examples,
    load_evidence_pack,
    make_stub_bundle,
    render_evidence_pack,
    stage_agent_judge_task,
    validate_prediction_bundle,
)
from evallab.schemas import JudgeCalibrationRecord, JudgeCriterionVerdict

REPO_ROOT = Path(__file__).resolve().parents[1]
FAMILY = "checkout-pool-exhaustion"


def _cells(family: str, verdict: str) -> dict[str, dict[str, JudgeCriterionVerdict]]:
    return {
        dimension: {name: JudgeCriterionVerdict(verdict=verdict, rationale="r") for name in block}
        for dimension, block in RUBRICS[family]["criteria"].items()
    }


def _complete_predictions(family: str) -> list[tuple[str, SimpleNamespace]]:
    full = _cells(family, "yes")
    return [
        (d.document_id, SimpleNamespace(judgments=full)) for d in load_corpus(REPO_ROOT, family)
    ]


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
    assert "evidence" in DSPY_INPUT_FIELDS


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


def test_dspy_metric_scores_exact_cells_and_treats_missing_or_invalid_as_disagreement() -> None:
    example = load_dspy_examples(REPO_ROOT, FAMILY)[9]
    expected = json.loads(example.expected_json)
    total = sum(len(block) for block in expected.values())

    perfect = {
        dimension: {
            name: JudgeCriterionVerdict(verdict=v, rationale="r") for name, v in block.items()
        }
        for dimension, block in expected.items()
    }
    assert dspy_metric(example, SimpleNamespace(judgments=perfect)) == 1.0

    # Drop one criterion and hand another an unparseable cell: both count against the judge.
    damaged = {d: dict(b) for d, b in perfect.items()}
    del damaged["causal_reasoning"]["identifies_the_mechanism"]
    damaged["evidence_fidelity"]["invents_evidence"] = {"verdict": "maybe", "rationale": "r"}
    assert dspy_metric(example, SimpleNamespace(judgments=damaged)) == pytest.approx(
        (total - 2) / total
    )

    assert dspy_metric(example, SimpleNamespace(judgments=None)) == 0.0
    assert dspy_metric(example, SimpleNamespace()) == 0.0


def test_dspy_prediction_bundle_refuses_omitted_criteria_by_name() -> None:
    predictions = _complete_predictions(FAMILY)
    partial = {"causal_reasoning": dict(_cells(FAMILY, "yes")["causal_reasoning"])}
    first_id = predictions[0][0]
    predictions[0] = (first_id, SimpleNamespace(judgments=partial))
    pack = load_evidence_pack(REPO_ROOT, FAMILY)

    with pytest.raises(IncompleteJudgeOutputError) as caught:
        dspy_prediction_bundle(
            REPO_ROOT,
            FAMILY,
            predictions,
            evidence=pack,
            judge_backend="dspy-test",
            judge_model="m",
        )

    criteria = RUBRICS[FAMILY]["criteria"]
    assert set(caught.value.missing) == {first_id}
    assert caught.value.missing[first_id] == [
        f"{dimension}.{name}"
        for dimension in ("action_quality", "evidence_fidelity")
        for name in criteria[dimension]
    ]
    assert first_id in str(caught.value) and "evidence_fidelity.invents_evidence" in str(
        caught.value
    )


def test_complete_dspy_output_becomes_an_evidence_bound_bundle_and_record() -> None:
    pack = load_evidence_pack(REPO_ROOT, FAMILY)
    bundle = dspy_prediction_bundle(
        REPO_ROOT,
        FAMILY,
        _complete_predictions(FAMILY),
        evidence=pack,
        judge_backend="dspy-test",
        judge_model="fake-model",
    )
    assert bundle.evidence_digest == pack.digest
    assert [p.document_id for p in bundle.predictions] == [
        d.document_id for d in load_corpus(REPO_ROOT, FAMILY)
    ]

    record = evaluate_predictions(REPO_ROOT, bundle, prediction_artifact="test://bundle")
    assert isinstance(record, JudgeCalibrationRecord)
    assert record.evidence_digest == pack.digest
    assert record.judge_backend == "dspy-test"

    other = load_evidence_pack(REPO_ROOT, "retry-storm-backlog")
    with pytest.raises(ValueError, match="evidence pack is for 'retry-storm-backlog'"):
        dspy_prediction_bundle(
            REPO_ROOT,
            FAMILY,
            _complete_predictions(FAMILY),
            evidence=other,
            judge_backend="dspy-test",
            judge_model="fake-model",
        )
    stale = bundle.model_copy(update={"evidence_digest": other.digest})
    with pytest.raises(ValueError, match="evidence digest does not match"):
        validate_prediction_bundle(REPO_ROOT, stale)


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


def test_staged_agent_judge_task_ships_the_evidence_directory(tmp_path: Path) -> None:
    task_root = stage_agent_judge_task(
        REPO_ROOT,
        FAMILY,
        backend="harbor-codex-agent",
        judge_model="m",
        task_relative=Path("queue/staged") / tmp_path.name,
    )
    try:
        pack = load_evidence_pack(REPO_ROOT, FAMILY)
        staged = {p.name: p.read_text() for p in (task_root / "environment/evidence").iterdir()}
        assert staged == {f.name: f.text for f in pack.files}
        rubric = json.loads((task_root / "environment/rubric.json").read_text())
        assert rubric["evidence_digest"] == pack.digest
        assert (
            "COPY evidence/ /app/input/evidence/"
            in (task_root / "environment/Dockerfile").read_text()
        )
        assert "/app/input/evidence/" in (task_root / "instruction.md").read_text()
    finally:
        shutil.rmtree(task_root)


def test_record_id_distinguishes_programs_and_input_contracts_on_the_same_day() -> None:
    stub = make_stub_bundle(REPO_ROOT, FAMILY)
    unoptimized = stub.model_copy(
        update={"judge_backend": "dspy-cot-unoptimized", "judge_model": "glm-5.3-flash"}
    )
    compiled = stub.model_copy(
        update={"judge_backend": "dspy-gepa-checkout", "judge_model": "glm-5.3-flash"}
    )
    pack = load_evidence_pack(REPO_ROOT, FAMILY)
    bound = unoptimized.model_copy(update={"evidence_digest": pack.digest})
    day = date(2026, 9, 16)

    first, second, third = (_record_id(b, day) for b in (unoptimized, compiled, bound))

    assert len({first, second, third}) == 3
    assert first.startswith(f"{FAMILY}-20260916-dspy-cot-unoptimized-glm-5-3-flash-")
    assert third == first + "-e" + pack.digest.removeprefix("sha256:")[:8]
    pattern = JudgeCalibrationRecord.model_fields["record_id"].metadata[0].pattern
    assert all(re.fullmatch(pattern, value) for value in (first, second, third))
