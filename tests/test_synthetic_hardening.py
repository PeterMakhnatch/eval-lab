"""Focused regression tests for synthetic admission hardening artifacts."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGN_PATH = ROOT / "research/synthetic/memory-tool-2x2-design-v1.json"


def _design() -> dict[str, object]:
    return json.loads(DESIGN_PATH.read_text(encoding="utf-8"))


def test_memory_tool_design_is_exact_abstract_2x2() -> None:
    design = _design()
    cells = design["cells"]

    assert isinstance(cells, list)
    assert {(cell["memory_continuity"], cell["tool_dependency"]) for cell in cells} == {
        ("absent", "absent"),
        ("absent", "present"),
        ("present", "absent"),
        ("present", "present"),
    }
    assert len({cell["cell_id"] for cell in cells}) == 4
    assert design["matched_axes"] == [
        "context_length",
        "tool_schema",
        "tool_inventory",
        "task_template",
        "verifier_implementation",
        "execution_profile",
        "generator_version",
        "seed",
    ]


def test_memory_tool_design_separates_generator_and_executing_agent_memory() -> None:
    calibration = _design()["generator_calibration"]

    assert calibration == {
        "designer_history_and_hint_regret_use": "generator_curriculum_only",
        "executing_agent_memory_factor_source": "runtime_condition_only",
        "executing_agent_memory_is_generator_memory": False,
        "heldout_measurement_failures_used": False,
    }


def test_memory_tool_design_holds_concrete_lineage_until_upstream_digests() -> None:
    design = _design()
    dependencies = design["required_upstream_identities"]
    lineage = design["lineage"]
    partition = design["partition"]

    assert {item["owner_lane"] for item in dependencies} == {
        "action-memory",
        "mcp-funcdag",
    }
    assert all(item["status"] == "pending" for item in dependencies)
    assert lineage == {
        "paired_lineage_spec_status": "not_created_dependency_hold",
        "source_task_digests": None,
        "generated_task_digests": None,
        "base_task_pair": None,
        "template_lineage_id": None,
        "oracle_owner": None,
        "verifier_owner": None,
        "mutation_coverage": None,
    }
    assert partition["status"] == "not_assigned_dependency_hold"
    assert partition["assignment_precedes_generation"] is True
    assert partition["algorithm"] is None
    assert partition["salt_digest"] is None
    assert partition["manifest_digest"] is None


def test_memory_tool_design_uses_only_canonical_admission_boundary() -> None:
    design = _design()

    assert design["canonical_admission"] == {
        "path": [
            "task_workbench certification packet",
            "TaskCertificationEnvelope",
            "registry.promote_task / TaskRegistryRecord",
        ],
        "minimum_adversarial_invalid_probes": 3,
        "synthetic_certificate_is_passing_direct_measurement_consumer": False,
        "new_promotion_schema_added": False,
    }
    assert design["execution"] == {
        "model_runs": 0,
        "certification_runs": 0,
        "registration_attempts": 0,
        "measurement_authorized": False,
        "replication_authorized": False,
    }
