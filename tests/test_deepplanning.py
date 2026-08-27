from __future__ import annotations

import json
from pathlib import Path

from evallab.deepplanning import (
    derive_solution,
    load_cohort,
    oracle,
    projections,
    reset_state,
    sanitize_agent_task,
    to_atif,
    typed_facts,
    verify_plan,
)

COHORT_PATH = Path("library/external/deepplanning-v1/cohort.json")
PROVENANCE_PATH = Path("library/external/deepplanning-v1/PROVENANCE.json")
CANARY_DIR = Path("library/external/deepplanning-v1/tasks/travel-lisbon-002")


def test_provenance_manifest_and_license_pinning() -> None:
    assert PROVENANCE_PATH.exists()
    prov = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
    assert prov["license"] == "Apache-2.0"
    assert prov["provenance_zone"] == "01-external"
    assert prov["upstream"]["commit"] == "31a4d36d123688581a9e9744427272b33ce940e0"
    assert prov["upstream"]["dataset_revision"] == "213876cce679f993a476d01042e13d111c0e3648"
    assert prov["oracle_isolation"]["status"] == "enforced"


def test_balanced_cohort_and_executable_oracle_derivation() -> None:
    tasks = load_cohort(COHORT_PATH)
    assert len(tasks) == 6
    assert [task["domain"] for task in tasks].count("travel") == 3
    assert [task["domain"] for task in tasks].count("shopping") == 3

    # Ensure executable oracle derivation matches verification for all tasks
    for task in tasks:
        derived = derive_solution(task)
        assert "status" in derived
        assert "acquired_sources" in derived
        verif = verify_plan(task, derived)
        assert verif.reward == 1.0
        assert verif.status in {"success", "infeasible"}
        assert verif.analysis_ready


def test_agent_task_sanitization_zero_oracle_leak() -> None:
    tasks = load_cohort(COHORT_PATH)
    for task in tasks:
        sanitized = sanitize_agent_task(task)
        # Agent visible json must not leak any oracle answer or expected results
        assert "oracle" not in sanitized
        assert "expected_status" not in sanitized
        assert "refusal_reason" not in sanitized
        # But must preserve task definition
        assert sanitized["task_id"] == task["task_id"]
        assert sanitized["prompt"] == task["prompt"]
        assert len(sanitized["sources"]) == len(task["sources"])
        assert len(sanitized["constraints"]) == len(task["constraints"])


def test_missing_source_fails_verification() -> None:
    task = load_cohort(COHORT_PATH)[0]
    answer = oracle(task)
    answer["acquired_sources"] = answer["acquired_sources"][:-1]
    check = verify_plan(task, answer)
    assert check.status == "failure"
    assert check.reward == 0.0
    assert check.missing_evidence == ("temple-pass",)
    assert any(item.verdict == "unknown" for item in check.constraints)
    assert not check.analysis_ready


def test_reset_state_replaces_mutated_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    target = tmp_path / "run"
    snapshot.mkdir()
    (snapshot / "catalog.json").write_text(json.dumps({"version": 1}))
    target.mkdir()
    (target / "catalog.json").write_text(json.dumps({"version": 99}))
    reset_state(snapshot, target)
    assert json.loads((target / "catalog.json").read_text()) == {"version": 1}


def test_atif_and_semantic_facts_projections() -> None:
    task = load_cohort(COHORT_PATH)[1]  # travel-lisbon-002
    answer = oracle(task)
    verif = verify_plan(task, answer)
    atif = to_atif(task, answer, verif)
    assert atif["schema_version"] == "ATIF-v1.7"
    assert atif["metadata"]["benchmark"] == "deepplanning"
    assert atif["metadata"]["verification"]["reward"] == 1.0

    facts = typed_facts(task, verif, "trial-test-1")
    assert len(facts.capability_opportunities) == 1
    assert facts.capability_opportunities[0].construct == "proactive_information_acquisition"
    assert len(facts.constraint_facts) == len(task["constraints"])
    assert projections(task, verif, "trial-test-1") == facts
