from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from evallab import mimo_runtime_repair as producer
from evallab.runner import load_matrix, staged_matrix_request
from evallab.task_variants import derive_task
from test_mimo_training_split import driver as driver
from test_mimo_training_split import native_control_fixture
from test_mimo_training_split import successor_case as successor_case

FIXTURE = Path(__file__).parent / "fixtures/mimo_runtime_repair_001246.patch"


def test_missing_application_import_is_fourteen_real_test_failures_not_a_collection_error(tmp_path: Path) -> None:
    """Dependency doubles permit collection, never implement a BurnMan API."""
    original, _ = producer.test_module(FIXTURE.read_bytes())
    repaired, _ = producer.test_module(producer.collection_patch(FIXTURE.read_bytes()))
    conftest = '''
import builtins
import json
import sys
import types
from pathlib import Path

# Only collection-time array literals are needed; no application implementation
# or numerical oracle is provided. Every real body must fail its BurnMan import.
np = types.ModuleType("numpy")
np.array = lambda values: values
ag = types.ModuleType("autograd")
ag.numpy = np
sys.modules.update({"numpy": np, "autograd": ag, "autograd.numpy": np})
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "burnman" or name.startswith("burnman."):
        raise ImportError("cannot import name '_ideal_activities_fct'")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
observed = {"items": [], "calls": [], "collection_errors": []}
def pytest_collection_finish(session):
    for item in session.items:
        params = getattr(getattr(item, "callspec", None), "params", {})
        observed["items"].append({"name": item.originalname, "nodeid": item.nodeid,
            "factory": getattr(params.get("model"), "__name__", None),
            "fractions": params.get("molar_fractions")})
def pytest_runtest_logreport(report):
    if report.when == "call":
        observed["calls"].append({"nodeid": report.nodeid, "outcome": report.outcome,
                                 "error": str(report.longrepr)})
def pytest_collectreport(report):
    if report.failed:
        observed["collection_errors"].append(str(report.longrepr))
def pytest_sessionfinish(session):
    Path("observed.json").write_text(json.dumps(observed))
'''
    results = []
    for label, source in (("original", original), ("repaired", repaired)):
        directory = tmp_path / label
        directory.mkdir()
        (directory / "conftest.py").write_text(conftest)
        (directory / "pytest.ini").write_text("[pytest]\n")
        (directory / "test_solution_activities.py").write_text(source)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-c", "pytest.ini", "-q", "test_solution_activities.py"],
            cwd=directory, env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS=""),
            capture_output=True, text=True, check=False,
        )
        results.append((result.returncode, json.loads((directory / "observed.json").read_text())))
    assert results[0][0] == 2 and results[0][1]["items"] == []
    assert "_ideal_activities_fct" in results[0][1]["collection_errors"][0]
    rc, observed = results[1]
    assert rc == 1 and observed["collection_errors"] == []
    assert len(observed["items"]) == len(observed["calls"]) == 14
    assert len({item["name"] for item in observed["items"]}) == 11
    assert all(call["outcome"] == "failed" and "_ideal_activities_fct" in call["error"] for call in observed["calls"])
    parameters = [item for item in observed["items"] if item["name"] == producer.PARAMETER_TEST]
    assert [(item["factory"], item["fractions"]) for item in parameters] == [
        ("_regular_opx_model", [0.0, 1.0]), ("_subregular_model", [0.2, 0.3, 0.5]),
        ("_polynomial_model", [0.2, 0.3, 0.5]), ("_function_model", [0.2, 0.3, 0.5]),
    ]


def causal_fixture(directory: Path) -> dict:
    directory.mkdir()
    source = {
        "burnman/classes/solution.py": b"class Solution:\n    pass\n",
        "burnman/classes/solutionmodel.py": b"class IdealSolution:\n    pass\n",
        "burnman/classes/elasticsolutionmodel.py": b"from .solutionmodel import _ideal_activities_fct\n",
        "burnman/classes/__init__.py": b"from . import elasticsolutionmodel\n",
    }
    archive = directory / "workspace.tgz"
    with tarfile.open(archive, "w:gz") as handle:
        for name, data in source.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            handle.addfile(info, io.BytesIO(data))
    preimages, patch = [], ""
    for name in ("burnman/classes/solution.py", "burnman/classes/solutionmodel.py"):
        data = source[name]
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        preimages.append({"path": name, "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                          "git_blob": blob, "reference_preimage": blob[:8]})
        patch += f"diff --git a/{name} b/{name}\nindex {blob[:8]}..12345678 100644\n"
    patch += "+def _ideal_activities_fct():\n"
    (directory / "solution.patch").write_text(patch)
    (directory / "test_output.log").write_text("collected 0 items / 1 error\nImportError: _ideal_activities_fct\n")
    (directory / "setup.log").write_text("pre-import FAIL _ideal_activities_fct; matched 0 copies, changed 0\n")
    return {
        "native_execution_id": "bf0d13f9-ff9a-446f-87a1-7cd1231edd6e",
        "snapshot": producer.file_binding(archive), "reference_patch": producer.file_binding(directory / "solution.patch"),
        "test_output": producer.file_binding(directory / "test_output.log"), "setup_log": producer.file_binding(directory / "setup.log"),
        "source_preimages": preimages,
    }


@pytest.fixture
def runtime_case(successor_case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request):
    case = successor_case
    repo = case["repo"]
    (repo / "pyproject.toml").write_text("[project]\nname = 'fixture'\nversion = '0.0.0'\n")
    parent = case["packages"][producer.TASK_ID]
    identity = producer.package_identity(parent)
    # Synthetic packages exercise the real byte/digest/replay boundaries; only
    # the fixed production endpoint constants are substituted for this fixture.
    for name, key in (("PARENT_PACKAGE", "package_digest"), ("PARENT_HARBOR", "harbor_digest"), ("PARENT_VERIFIER", "verifier_digest")):
        monkeypatch.setattr(producer, name, identity[key])
    include_wrong = getattr(request, "param", False)
    inventory_path = repo / "effective-inventory.json"
    source_path = repo / "authoritative-source.json"
    rows, wrongs, corrects, corrections = [], [], [], []
    for label, role in [("correct", "prompt-valid-alternate")] + ([("wrong", "known-wrong")] if include_wrong else []):
        patch = repo / f"{label}.patch"
        patch.write_text(f"diff --git a/code.py b/code.py\n--- a/code.py\n+++ b/code.py\n@@ -1 +1 @@\n-0\n+{label}\n")
        row = {
            "task_id": producer.TASK_NAME, "finding_id": f"finding-{label}", "probe_id": label,
            "reproducer_patch": str(patch), "reproducer_sha256": producer.file_binding(patch)["sha256"],
            "v6_package_digest": identity["package_digest"],
            "effective_verdict": "wrong" if role == "known-wrong" else "equivalent",
        }
        (wrongs if role == "known-wrong" else corrects).append(row)
        rows.append(row | {"adjudication": {"verdict": "wrong"}})
        if role == "prompt-valid-alternate":
            corrections.append({"task_id": producer.TASK_NAME, "reproducer_id": label, "authoritative_verdict": "equivalent"})
    source_path.write_text(json.dumps({"verified_reproducers": rows, "authoritative_adjudication_review": {
        "reclassified_from_wrong_to_equivalent_or_unspecified": corrections,
    }}))
    inventory = {
        "schema": "envcheck.v7.input-inventory/v3", "training_allowed": False,
        "rows": wrongs, "adjudicated_correct_controls": corrects, "unspecified_controls": [],
        "source_inputs": [producer.file_binding(source_path)],
    }
    inventory_path.write_text(json.dumps(inventory))
    handoff_path = repo / "runtime-handoff.json"
    handoff = producer.derive_runtime_repair(
        parent=parent, inventory=inventory_path, evidence=causal_fixture(repo / "causal"),
        repo_root=repo, output_dir=repo / "derived/runtime-plan", variants_root=repo / "derived/runtime-variants",
        handoff_path=handoff_path, created_by="pytest",
    )
    return case | {"handoff": handoff, "handoff_path": handoff_path, "inventory_data": inventory,
                   "inventory_path": inventory_path, "source_path": source_path, "parent": parent}


def save_handoff(case) -> None:
    case["handoff_path"].write_text(json.dumps(case["handoff"]))


def record_controls(case, *, mutation: str | None = None) -> None:
    handoff, repo = case["handoff"], case["repo"]
    matrix = load_matrix(Path(handoff["matrix"]["path"]))
    controls = {item["run"]: item for item in handoff["controls"]}
    checks = []
    for index, run in enumerate(matrix.runs):
        role = "reference" if index == 0 else "nop" if index == 1 else controls[run.name]["role"]
        repeated = role != "known-wrong"
        job = repo / "native" / run.name
        with staged_matrix_request(matrix, run, repo_root=repo) as (request, provenance):
            package = case["parent"] if mutation == f"parent_{role}" else request.task
            reward = 0.0 if mutation == "correct_rejected" and role == "prompt-valid-alternate" else run.expect_reward
            case_evidence = mutation != "no_case_evidence" and not (
                mutation == "wrong_no_case_evidence" and role == "known-wrong"
            )
            native_control_fixture(job, package, producer.TASK_ID, run.agent, reward,
                                   repeats=3 if repeated else 1, case_evidence=case_evidence)
        if mutation == "repeat_alias" and index == 0:
            path = job / "trial/verifier/stability.json"
            stability = json.loads(path.read_text())
            for item in stability["runs"]:
                item["index"] = 0
            path.write_text(json.dumps(stability))
        if mutation == "model_control" and index == 0:
            path = job / "trial/result.json"
            result = json.loads(path.read_text())
            result["config"]["agent"]["model_name"] = "unapproved-model"
            path.write_text(json.dumps(result))
        if mutation == "unfinished_oracle" and index == 0:
            path = job / "trial/result.json"
            result = json.loads(path.read_text())
            result["agent_execution"]["finished_at"] = None
            path.write_text(json.dumps(result))
        checks.append({
            "run": run.name, "role": role, "status": "ok", "expected_reward": run.expect_reward,
            "matrix_sha256": handoff["matrix"]["sha256"], "job_dir": str(job),
            "repeats": [run.expect_reward] * 3 if repeated else None, **provenance,
        })
    if mutation == "unbound_payload":
        checks[2]["solution_sha256"] = "0" * 64
    receipt = {
        "schema": "mimo-v7-runtime-controls/v1", "task_id": producer.TASK_ID,
        **{key: handoff["selected"][key] for key in ("package_digest", "harbor_digest", "verifier_digest")},
        "matrix_sha256": handoff["matrix"]["sha256"], "inventory_sha256": handoff["inventory"]["sha256"], "checks": checks,
    }
    if mutation == "extra_receipt_field":
        receipt["qualified"] = True
    path = repo / "runtime-controls.json"
    path.write_text(json.dumps(receipt))
    handoff["qualification"] = {"status": "recorded", "receipt": producer.file_binding(path)}
    save_handoff(case)


def selected_row(payload):
    return next(row for row in payload["tasks"] if row["task_id"] == producer.TASK_ID)


def test_runtime_pending_candidate_preserves_frozen33_and_never_inherits_parent_controls(driver, runtime_case, tmp_path: Path) -> None:
    case = runtime_case
    before = {path.name: path.read_bytes() for path in case["frozen"].iterdir()}
    payload = driver["assemble_successor"](case["frozen"], tmp_path / "successor.json",
        runtime_repair_handoff=case["handoff_path"], repo_root=case["repo"])
    row = selected_row(payload)
    assert row["successor_selection"] == "runtime-repair"
    assert row["evaluation_target"]["package_digest"] != row["frozen_parent"]["package_digest"]
    assert row["evaluation_target"]["lineage"]["full_transform_chain_newest_first"] == [
        producer.TRANSFORM, "agent-network-none@1", "leak-close-pypi@1",
    ]
    selected = row["native_controls"]["selected_package"]
    assert selected["observation_indexes"] == []
    assert selected["measured_reference_rewards"] == selected["measured_nop_rewards"] == []
    assert selected["measurement_source"] == "reported_runtime_repair_controls"
    assert "native_reference_1_1_1" in selected["missing"] and "native_nop_0_0_0" in selected["missing"]
    assert row["native_controls"]["retained_frozen_v6"]["status"] == "retained"
    assert payload["heldout"] == case["membership"] and payload["counts"]["selected_runtime_repairs"] == 1
    assert payload["scoring_allowed"] is payload["freeze_ready"] is False
    assert row["evaluation_target"]["qualified_for_v7"] is False
    driver["validate_successor"](payload, case["frozen"], runtime_repair_handoff=case["handoff_path"], repo_root=case["repo"])
    assert before == {path.name: path.read_bytes() for path in case["frozen"].iterdir()}
    without = driver["successor_payload"](case["frozen"], repo_root=case["repo"])
    assert selected_row(without)["successor_selection"] == "unchanged-v6"


@pytest.mark.parametrize("runtime_case", [False, True], indirect=True)
def test_runtime_intake_requires_selected_native_controls_for_complete_wrong_and_correct_inventory(driver, runtime_case) -> None:
    case = runtime_case
    record_controls(case)
    payload = driver["successor_payload"](case["frozen"], runtime_repair_handoff=case["handoff_path"], repo_root=case["repo"])
    row = selected_row(payload)
    controls = row["native_controls"]["reported_runtime_repair_controls"]
    assert controls["measured_rewards"] == {"oracle": [1, 1, 1], "nop": [0, 0, 0]}
    assert {item["run"] for item in controls["observations"][2:]} == {item["run"] for item in case["handoff"]["controls"]}
    for item in controls["observations"]:
        expected = 0 if item["role"] in {"nop", "known-wrong"} else 1
        assert item["native"]["rewards"] == [expected] * (1 if item["role"] == "known-wrong" else 3)
    assert row["native_controls"]["selected_package"]["observation_indexes"] == []
    assert row["evaluation_target"]["qualified_for_v7"] is payload["scoring_allowed"] is False
    assert payload["independent_verifier"]["status"] == "pending"


@pytest.mark.parametrize("mutation", [
    "parent_reference", "parent_prompt-valid-alternate", "correct_rejected", "no_case_evidence",
    "repeat_alias", "unbound_payload", "model_control", "unfinished_oracle", "extra_receipt_field",
])
def test_runtime_controls_reject_parent_outcomes_zero_cases_false_negatives_and_bad_provenance(driver, runtime_case, mutation) -> None:
    record_controls(runtime_case, mutation=mutation)
    with pytest.raises(ValueError):
        driver["successor_payload"](runtime_case["frozen"], runtime_repair_handoff=runtime_case["handoff_path"], repo_root=runtime_case["repo"])


@pytest.mark.parametrize("mutation", [
    "unknown_field", "qualification_claim", "numeric_false", "different_task", "missing_correct", "added_wrong",
    "inventory_drift", "authority_drift", "script_drift", "wrong_parent", "local_parent", "extra_delta", "weakened_assertion",
])
def test_runtime_candidate_rejects_open_schemas_inventory_drift_and_nonbounded_variants(driver, runtime_case, mutation) -> None:
    case, handoff = runtime_case, runtime_case["handoff"]
    if mutation == "unknown_field":
        handoff["accepted"] = True
    elif mutation == "qualification_claim":
        handoff["qualified_for_v7"] = True
    elif mutation == "numeric_false":
        handoff["scoring_allowed"] = 0
    elif mutation == "different_task":
        handoff["task_id"] = "format-code-task-001948"
    elif mutation == "missing_correct":
        handoff["controls"] = []
    elif mutation == "added_wrong":
        row = copy.deepcopy(case["inventory_data"]["adjudicated_correct_controls"][0])
        row.update(finding_id="newly-admitted-wrong", probe_id="new-wrong", effective_verdict="wrong")
        case["inventory_data"]["rows"].append(row)
        case["inventory_path"].write_text(json.dumps(case["inventory_data"]))
        handoff["inventory"] = producer.file_binding(case["inventory_path"])
    elif mutation == "inventory_drift":
        case["inventory_path"].write_text(case["inventory_path"].read_text() + "\n")
    elif mutation == "authority_drift":
        case["source_path"].write_text(case["source_path"].read_text() + "\n")
    elif mutation == "script_drift":
        path = Path(handoff["controls"][0]["script"]["path"])
        path.write_text("#!/bin/sh\nexit 0\n")
        handoff["controls"][0]["script"] = producer.file_binding(path)
    elif mutation == "wrong_parent":
        handoff["parent"]["package_digest"] = "sha256:" + "0" * 64
    elif mutation == "local_parent":
        path = Path(handoff["native_record"]["path"])
        record = json.loads(path.read_text())
        record["parent"]["source"] = {"kind": "local", "path": str(case["parent"])}
        path.write_text(json.dumps(record))
        handoff["native_record"] = producer.file_binding(path)
    else:
        patch = producer.collection_patch(FIXTURE.read_bytes())
        changes = {"tests/test.patch": patch}
        if mutation == "extra_delta":
            changes["tests/test_command.sh"] = b"#!/bin/sh\nexit 0\n"
        else:
            changes["tests/test.patch"] = patch.replace(
                b'+    assert str(excinfo.value) == "Activity coefficients not defined at 0 K."', b"+    assert True",
            )
        record = derive_task(case["parent"], changes=changes, transform=producer.TRANSFORM,
            rationale="deliberately invalid boundary fixture", created_by="pytest", inputs=producer.TRANSFORM_INPUTS,
            parent_source={"kind": "variant", "record": str(case["records"][producer.TASK_ID].record_relpath())},
            repo_root=case["repo"], variants_root=case["repo"] / "derived/invalid-variants")
        package = case["repo"] / "derived/invalid-variants" / record.task_slug / record.digest12
        handoff["selected"] = producer.package_identity(package)
        handoff["native_record"] = producer.file_binding(case["repo"] / record.record_relpath())
    save_handoff(case)
    with pytest.raises(ValueError):
        driver["successor_payload"](case["frozen"], runtime_repair_handoff=case["handoff_path"], repo_root=case["repo"])


@pytest.mark.parametrize("runtime_case", [True], indirect=True)
def test_runtime_known_wrong_zero_requires_actual_case_execution(driver, runtime_case) -> None:
    record_controls(runtime_case, mutation="wrong_no_case_evidence")
    with pytest.raises(ValueError):
        driver["successor_payload"](runtime_case["frozen"], runtime_repair_handoff=runtime_case["handoff_path"], repo_root=runtime_case["repo"])


def test_runtime_handoff_is_not_an_envcheck_semantic_repair(driver, runtime_case) -> None:
    with pytest.raises(ValueError, match="schema"):
        driver["successor_payload"](runtime_case["frozen"], repairs_handoff=runtime_case["handoff_path"], repo_root=runtime_case["repo"])


def test_runtime_cli_forwards_optional_intake_without_releasing_scoring(driver, runtime_case, tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "cli-successor.json"
    monkeypatch.setattr(sys, "argv", [
        "freeze.py", "--assemble-successor", "--frozen-source-dir", str(runtime_case["frozen"]),
        "--successor-destination", str(destination), "--repo-root", str(runtime_case["repo"]),
        "--runtime-repair-handoff", str(runtime_case["handoff_path"]),
    ])
    driver["main"]()
    payload = json.loads(destination.read_text())
    assert selected_row(payload)["successor_selection"] == "runtime-repair"
    assert payload["scoring_allowed"] is False
