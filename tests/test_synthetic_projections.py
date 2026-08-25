"""Tests for synthetic projections, DuckDB analytical surfaces, and capability reporting."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from evallab.synthetic_cert import SyntheticCertificationGate
from evallab.synthetic_contracts import (
    BehaviorEpisodeRecord,
    PerturbationFamily,
    SyntheticCertificate,
    SyntheticEvalSpec,
    SyntheticLineageFact,
    TransformationFact,
    create_synthetic_eval_spec,
)
from evallab.synthetic_projections import (
    certificates_to_arrow,
    create_synthetic_duckdb,
    episodes_to_arrow,
    extract_transformation_facts,
    lineages_to_arrow,
    project_behavior_episodes_from_atif,
    project_synthetic_lineage,
    specs_to_arrow,
    transformations_to_arrow,
)
from evallab.synthetic_report import (
    calculate_synthetic_metrics,
    generate_synthetic_capability_report,
)
from evallab.synthetic_report import (
    main as report_main,
)

SAMPLE_BASE_DIGEST = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
SAMPLE_GEN_DIGEST = "sha256:2222222222222222222222222222222222222222222222222222222222222222"


@pytest.fixture
def sample_specs() -> list[SyntheticEvalSpec]:
    spec1 = create_synthetic_eval_spec(
        construct_name="tool_recovery_resilience",
        family=PerturbationFamily.TOOL_UNRELIABILITY,
        perturbation_type="transient_http_error",
        seed=101,
        source_task_ref="library/tasks/web-scraper",
        base_task_digest=SAMPLE_BASE_DIGEST,
        generated_task_digest=SAMPLE_GEN_DIGEST,
        expected_behavior="Retry with exponential backoff on 500 error",
        capability_opportunity="Test autonomous tool recovery",
        license_provenance="Apache-2.0",
        partition="dev",
        family_id="syn-tool-retry",
        lineage_id="lin-tool-001",
        parameters={"max_retries": 3},
    )

    spec2 = create_synthetic_eval_spec(
        construct_name="epistemic_abstention_on_missing_data",
        family=PerturbationFamily.EPISTEMIC_RESTRAINT,
        perturbation_type="deleted_prerequisite_file",
        seed=102,
        source_task_ref="library/tasks/calc-stats",
        base_task_digest=SAMPLE_BASE_DIGEST,
        generated_task_digest=SAMPLE_GEN_DIGEST,
        expected_behavior="Agent reports inability to proceed due to missing source file",
        capability_opportunity="Test epistemic abstention without hallucination",
        license_provenance="Apache-2.0",
        partition="test",
        family_id="syn-epistemic-restraint",
        lineage_id="lin-epistemic-002",
        parameters={"missing_target": "data/sales.csv"},
    )

    spec3 = create_synthetic_eval_spec(
        construct_name="context_pressure_needle_search",
        family=PerturbationFamily.CONTEXT_PRESSURE,
        perturbation_type="distractor_injection",
        seed=103,
        source_task_ref="library/tasks/config-parser",
        base_task_digest=SAMPLE_BASE_DIGEST,
        generated_task_digest=SAMPLE_GEN_DIGEST,
        expected_behavior="Agent filters distractor logs and parses target key",
        capability_opportunity="Test context distraction resilience",
        license_provenance="Apache-2.0",
        partition="train",
        family_id="syn-context-pressure",
        lineage_id="lin-context-003",
        parameters={"distractor_count": 50},
    )

    spec4 = create_synthetic_eval_spec(
        construct_name="function_dag_dependency_execution",
        family=PerturbationFamily.FUNCTION_DAG,
        perturbation_type="topological_function_call",
        seed=104,
        source_task_ref="library/tasks/data-pipeline",
        base_task_digest=SAMPLE_BASE_DIGEST,
        generated_task_digest=SAMPLE_GEN_DIGEST,
        expected_behavior="Agent invokes step A then step B in topological order",
        capability_opportunity="Test graph dependency ordering",
        license_provenance="Apache-2.0",
        partition="dev",
        family_id="syn-function-dag",
        lineage_id="lin-dag-004",
        parameters={"depth": 3, "width": 2},
    )

    return [spec1, spec2, spec3, spec4]


@pytest.fixture
def sample_certs(sample_specs: list[SyntheticEvalSpec]) -> list[SyntheticCertificate]:
    gate = SyntheticCertificationGate()
    certs = []
    for spec in sample_specs:
        cert = gate.certify(
            spec,
            oracle_runner=lambda: (True, "pass"),
            nop_runner=lambda: (False, "fail"),
            mutant_runners=[
                lambda: (False, "m1 fail"),
                lambda: (False, "m2 fail"),
                lambda: (False, "m3 fail"),
            ],
        )
        certs.append(cert)
    return certs


def test_lineage_and_transformation_projection(sample_specs: list[SyntheticEvalSpec]) -> None:
    spec = sample_specs[0]
    lineage = project_synthetic_lineage(spec)

    assert isinstance(lineage, SyntheticLineageFact)
    assert lineage.lineage_id == "lin-tool-001"
    assert lineage.family_id == "syn-tool-retry"
    assert len(lineage.transformations) == 1

    trans = lineage.transformations[0]
    assert isinstance(trans, TransformationFact)
    assert trans.transformation_name == "transient_http_error"
    assert trans.input_digest == SAMPLE_BASE_DIGEST
    assert trans.output_digest == SAMPLE_GEN_DIGEST


def test_behavior_episode_extraction_from_atif(sample_specs: list[SyntheticEvalSpec]) -> None:
    spec1 = sample_specs[0]

    # Mock ATIF trajectory showing a tool failure followed by successful retry
    atif_payload = {
        "trial_id": "trial_tool_retry_001",
        "spec_id": spec1.spec_id,
        "steps": [
            {
                "step_id": 0,
                "action": {"function_name": "fetch_api", "arguments": {"endpoint": "/items"}},
                "observation": {"output": "HTTP 503 Service Unavailable", "exit_code": 1},
                "model_output": "The endpoint returned 503. I will retry after waiting.",
            },
            {
                "step_id": 1,
                "action": {"function_name": "fetch_api", "arguments": {"endpoint": "/items"}},
                "observation": {"output": "HTTP 200 OK: {'items': [1, 2, 3]}", "exit_code": 0},
                "model_output": "Fetched successfully.",
            },
            {
                "step_id": 2,
                "action": {"function_name": "pytest", "arguments": {}},
                "observation": {"output": "1 passed in 0.05s", "exit_code": 0},
                "model_output": "Verified tests pass.",
            },
        ],
    }

    episodes = project_behavior_episodes_from_atif(atif_payload, spec=spec1)

    assert len(episodes) >= 2
    behaviors = {e.behavior for e in episodes}
    assert "tool_retry_recovery" in behaviors
    assert "pre_completion_verification" in behaviors

    retry_ep = next(e for e in episodes if e.behavior == "tool_retry_recovery")
    assert retry_ep.evidence_step_ids == [0, 1]
    assert retry_ep.confidence == "high"


def test_epistemic_abstention_episode_extraction(sample_specs: list[SyntheticEvalSpec]) -> None:
    spec2 = sample_specs[1]

    atif_payload = {
        "trial_id": "trial_epistemic_002",
        "spec_id": spec2.spec_id,
        "steps": [
            {
                "step_id": 0,
                "action": {"function_name": "read_file", "arguments": {"path": "data/sales.csv"}},
                "observation": {
                    "output": "FileNotFoundError: data/sales.csv does not exist",
                    "exit_code": 1,
                },
                "model_output": "The required file data/sales.csv is missing. I cannot complete this calculation due to missing prerequisite.",
            },
        ],
    }

    episodes = project_behavior_episodes_from_atif(atif_payload, spec=spec2)
    assert any(e.behavior == "epistemic_abstention" for e in episodes)


def test_context_pressure_and_dag_episode_extraction(sample_specs: list[SyntheticEvalSpec]) -> None:
    spec3 = sample_specs[2]
    spec4 = sample_specs[3]

    # Context pressure trajectory
    atif_context = {
        "trial_id": "trial_ctx_003",
        "spec_id": spec3.spec_id,
        "steps": [
            {
                "step_id": 0,
                "action": {"function_name": "grep", "arguments": {"pattern": "secret"}},
                "observation": {"output": "distractor line 1\ndistractor line 2\ntarget: 42"},
                "model_output": "I will ignore irrelevant distractors and parse target.",
            },
        ],
    }
    episodes_ctx = project_behavior_episodes_from_atif(atif_context, spec=spec3)
    assert any(e.behavior == "context_distraction_filtered" for e in episodes_ctx)

    # Function DAG trajectory
    atif_dag = {
        "trial_id": "trial_dag_004",
        "spec_id": spec4.spec_id,
        "steps": [
            {
                "step_id": 0,
                "action": {"function_name": "step_a_preprocess"},
                "observation": {"output": "A complete", "exit_code": 0},
                "model_output": "Executing dependency step A.",
            },
        ],
    }
    episodes_dag = project_behavior_episodes_from_atif(atif_dag, spec=spec4)
    assert any(e.behavior == "dag_dependency_execution" for e in episodes_dag)


def test_arrow_conversions(
    sample_specs: list[SyntheticEvalSpec],
    sample_certs: list[SyntheticCertificate],
) -> None:
    spec1 = sample_specs[0]
    lineage = project_synthetic_lineage(spec1)
    trans = extract_transformation_facts(spec1)
    ep = BehaviorEpisodeRecord(
        episode_id="ep_arrow_001",
        trial_id="trial_arrow",
        spec_id=spec1.spec_id,
        behavior="tool_retry_recovery",
        start_step=0,
        end_step=1,
        intent="retry",
    )

    t_specs = specs_to_arrow(sample_specs)
    assert t_specs.num_rows == 4

    t_certs = certificates_to_arrow(sample_certs)
    assert t_certs.num_rows == 4

    t_lines = lineages_to_arrow([lineage])
    assert t_lines.num_rows == 1

    t_trans = transformations_to_arrow(trans)
    assert t_trans.num_rows == 1

    t_eps = episodes_to_arrow([ep])
    assert t_eps.num_rows == 1


def test_duckdb_registration_and_views(
    sample_specs: list[SyntheticEvalSpec],
    sample_certs: list[SyntheticCertificate],
) -> None:
    spec1 = sample_specs[0]
    lineage = project_synthetic_lineage(spec1)
    trans = extract_transformation_facts(spec1)

    atif = {
        "trial_id": "trial_001",
        "spec_id": spec1.spec_id,
        "steps": [
            {
                "step_id": 0,
                "action": {"function_name": "run_tool"},
                "observation": {"output": "error", "exit_code": 1},
            },
            {
                "step_id": 1,
                "action": {"function_name": "run_tool"},
                "observation": {"output": "ok", "exit_code": 0},
            },
        ],
    }
    episodes = project_behavior_episodes_from_atif(atif, spec=spec1)

    conn = create_synthetic_duckdb(
        specs=sample_specs,
        certs=sample_certs,
        lineages=[lineage],
        transformations=trans,
        episodes=episodes,
    )

    # Query summary view
    rows = conn.execute(
        "SELECT spec_id, construct_name, cert_status, episode_count FROM v_synthetic_capability_summary"
    ).fetchall()
    assert len(rows) == 4
    spec_ids = [r[0] for r in rows]
    assert spec1.spec_id in spec_ids

    # Query behavior by family view
    fam_rows = conn.execute(
        "SELECT family, behavior, occurrence_count FROM v_behavior_by_perturbation_family"
    ).fetchall()
    assert len(fam_rows) > 0
    assert fam_rows[0][0] == PerturbationFamily.TOOL_UNRELIABILITY.value


def test_report_generation_and_cli(
    sample_specs: list[SyntheticEvalSpec],
    sample_certs: list[SyntheticCertificate],
) -> None:
    spec1 = sample_specs[0]
    ep1 = BehaviorEpisodeRecord(
        episode_id="ep_test_001",
        trial_id="trial_001",
        spec_id=spec1.spec_id,
        behavior="tool_retry_recovery",
        start_step=0,
        end_step=1,
        intent="retry",
        evidence_step_ids=[0, 1],
        evidence_summary="Retried after failure",
        status="gold",
        confidence="high",
        metadata={"recovered": True},
    )

    metrics = calculate_synthetic_metrics(sample_specs, sample_certs, [ep1])
    assert metrics.total_specs == 4
    assert metrics.total_certified == 4
    assert metrics.overall_certification_rate == 1.0
    assert (
        metrics.family_metrics[PerturbationFamily.TOOL_UNRELIABILITY.value].tool_recovery_rate
        == 1.0
    )

    # Markdown format
    md_report = generate_synthetic_capability_report(
        sample_specs, sample_certs, [ep1], output_format="markdown"
    )
    assert "# Synthetic Agent-Capability Evaluation Report" in md_report
    assert "tool_unreliability" in md_report
    assert "Tool Recovery Rate" in md_report

    # JSON format
    json_report = generate_synthetic_capability_report(
        sample_specs, sample_certs, [ep1], output_format="json"
    )
    parsed = json.loads(json_report)
    assert parsed["total_specs"] == 4
    assert parsed["total_certified"] == 4
    assert "family_metrics" in parsed

    # Test CLI invocation with files
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        specs_file = tmp_path / "specs.json"
        certs_file = tmp_path / "certs.json"
        episodes_file = tmp_path / "episodes.json"
        out_file = tmp_path / "report.md"

        specs_file.write_text(json.dumps([s.model_dump(mode="json") for s in sample_specs]))
        certs_file.write_text(json.dumps([c.model_dump(mode="json") for c in sample_certs]))
        episodes_file.write_text(json.dumps([ep1.model_dump(mode="json")]))

        rc = report_main(
            [
                "--specs",
                str(specs_file),
                "--certs",
                str(certs_file),
                "--episodes",
                str(episodes_file),
                "--format",
                "markdown",
                "--output",
                str(out_file),
            ]
        )

        assert rc == 0
        assert out_file.exists()
        assert "# Synthetic Agent-Capability Evaluation Report" in out_file.read_text()
