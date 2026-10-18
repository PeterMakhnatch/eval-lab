"""Native evidence consumer regressions; fixture construction never starts Harbor."""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from evallab import regrade, runner
from evallab.execution_contracts import MODAL_ENVIRONMENT_IMPORT_PATH
from evallab.qa import counterfactual as cf

NativeJobConfig: Any
NativeTrialConfig: Any
native: Any


@pytest.fixture(scope="module", autouse=True)
def native_runtime() -> None:
    global NativeJobConfig, NativeTrialConfig, native
    pytest.importorskip("harbor.job_diff", reason="requires the pinned Harbor native profile")
    from harbor.models.job.config import JobConfig
    from harbor.models.trial.config import TrialConfig

    NativeJobConfig, NativeTrialConfig = JobConfig, TrialConfig
    path = Path(__file__).resolve().parents[1] / "research/experiments/heldout-v7/native.py"
    spec = importlib.util.spec_from_file_location("heldout_v7_native", path)
    assert spec and spec.loader
    native = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = native
    spec.loader.exec_module(native)


TASK = "mimo-v2.6-rl/format-code-task-000001"
PATCH = b"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
SOURCE_JOB = "00000000-0000-0000-0000-000000000001"
SOURCE_TRIAL = "00000000-0000-0000-0000-000000000002"
NEW_JOB = "00000000-0000-0000-0000-000000000003"
NEW_TRIAL = "00000000-0000-0000-0000-000000000004"
FINISHED = "2026-10-17T06:00:02Z"


def document(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def source(
    tmp: Path, *, patch: bytes = PATCH, raw: Any = 1, outcome: bool | None = True,
    error: Any = None,
) -> dict[str, Any]:
    trial = tmp / "retained/job/trial"
    config = {"job_id": SOURCE_JOB, "task": {"path": "/retained/original"}, "agent": {"name": "mimoagent"}}
    document(trial.parent / "result.json", {"id": SOURCE_JOB})
    document(trial / "result.json", {
        "id": SOURCE_TRIAL, "trial_name": trial.name, "task_name": TASK,
        "config": config, "exception_info": error,
        "verifier_result": {"rewards": {"reward": raw}} if raw is not None else None,
    })
    document(trial / "config.json", config)
    diff = trial / "verifier/agent.diff"
    diff.parent.mkdir()
    diff.write_bytes(patch)
    return {
        "source_uid": [SOURCE_JOB, SOURCE_TRIAL], "source_trial_dir": str(trial),
        "arm": "B", "task_id": TASK, "attempt_index": 1, "population": "official",
        "v6_outcome": outcome, "v6_error": error, "source_digest": cf.source_digest(trial),
        "diff": {"path": str(diff), "sha256": digest(patch)},
    }


def grader(tmp: Path) -> cf.Grader:
    package = tmp / "repo/variants/admitted-v7"
    (package / "tests").mkdir(parents=True)
    (package / "solution").mkdir()
    (package / "tests/test.sh").write_text("#!/bin/bash\nexit 0\n")
    (package / "solution/solve.sh").write_text("#!/bin/bash\nexit 0\n")
    (package / "instruction.md").write_text("Replace old with new.\n")
    (package / "task.toml").write_text(
        f'[task]\nname = "{TASK}"\n[environment]\nworkdir = "/testbed"\n'
        '[verifier]\nenvironment_mode = "separate"\n'
    )
    return cf.Grader(TASK, "heldout-v7", cf._digest(package), package, "externally-admitted.json")


def evidence_runner(score: Any = 1, *, mutate=None):
    """Write native receipt/config/lock/results using the existing staging APIs.

    This is the same retained-native-job fixture pattern as v2's qualification
    tests, extended with the real job/trial configs and invocation receipt. The
    consumer still calls actual cf.replay and independently reloads every job.
    """
    def execute(matrix, *, root):
        run = matrix.runs[0]
        job = root / "runs" / run.name
        trial = job / (run.name + "__fixture")
        with runner.staged_matrix_request(matrix, run, repo_root=root) as (request, provenance):
            staged, adaptation = runner.stage_request_task(request, root / "runs/.exec-stage" / run.name)
            staging = runner._task_staging_provenance(request.task, staged, adaptation)
        agent = {"name": "oracle", "model_name": None, "import_path": None}
        environment = {"type": None, "import_path": MODAL_ENVIRONMENT_IMPORT_PATH}
        task = {"path": str(staged)}
        trial_config = {
            "job_id": NEW_JOB, "trial_name": trial.name, "task": task,
            "agent": agent, "environment": environment, "verifier": {"disable": False},
        }
        trial_model = NativeTrialConfig.model_validate(trial_config)
        trial_config = trial_model.model_dump(mode="json", exclude_defaults=True)
        trial_lock = {
            "schema_version": 2, "task": {"name": run.name, "type": "local", "path": str(staged), "digest": staging["staged_harbor_digest"]},
            "agent": agent, "environment": environment, "verifier": {"disable": False},
        }
        matrix_result = {"name": run.name, "expect_reward": None, **provenance, "status": "ok", "rewards": [score], "error": None}
        job_result = {"id": NEW_JOB, "n_total_trials": 1, "stats": {}, "finished_at": FINISHED, "exception_info": None}
        job_config = {
            "job_name": run.name, "n_attempts": 1, "n_concurrent_trials": 1,
            "agents": [agent], "environment": environment, "tasks": [task], "verifier": {"disable": False},
        }
        job_config = NativeJobConfig.model_validate(job_config).model_dump(mode="json", exclude_defaults=True)
        job_lock = {"schema_version": 3, "harbor": {"version": "0.24.0"}, "trials": [trial_lock]}
        metadata = {"exit_code": 0, "timed_out": False, "task_staging": staging}
        trial_result = {
            "id": NEW_TRIAL, "trial_name": trial.name, "task_name": staging["declared_task_name"] or run.name, "task_id": task,
            "config": trial_model.model_dump(mode="json"), "agent_info": {"name": "oracle", "model_info": None},
            "finished_at": FINISHED, "exception_info": None,
            "agent_execution": {"started_at": "2026-10-17T06:00:00Z", "finished_at": "2026-10-17T06:00:01Z"},
            "verifier": {"started_at": "2026-10-17T06:00:01Z", "finished_at": FINISHED},
            "verifier_result": {"rewards": {"reward": score}},
        }
        context = {
            "matrix": matrix.model_dump(mode="json"), "matrix_result": matrix_result,
            "job_result": job_result, "job_config": job_config, "job_lock": job_lock,
            "metadata": metadata, "trial_result": trial_result, "trial_config": trial_config,
            "trial_lock": trial_lock, "script": root / run.solution, "job": job, "trial": trial,
            "omit": set(), "omit_job": False, "oracle_present": True, "oracle_exit": None,
            "reward_artifacts": {"reward.json": json.dumps({"reward": score})},
        }
        if mutate:
            mutate(context)
        documents = {
            job / "result.json": job_result, job / "config.json": job_config,
            job / "lock.json": job_lock, job / "lab-metadata.json": metadata,
            trial / "result.json": trial_result, trial / "config.json": trial_config,
            trial / "lock.json": trial_lock,
        }
        if not context["omit_job"]:
            for path, value in documents.items():
                if path not in context["omit"]:
                    document(path, value)
            for name, text in context["reward_artifacts"].items():
                path = trial / "verifier" / name
                if path not in context["omit"]:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
            (trial / "agent").mkdir(parents=True, exist_ok=True)
            if context["oracle_present"]:
                (trial / "agent/oracle.txt").write_text("")
            if context["oracle_exit"] is not None:
                (trial / "agent/exit-code.txt").write_text(str(context["oracle_exit"]))
        receipt = root / "runs/.executor" / f"{matrix.matrix_id}.matrix.json"
        document(receipt, {"matrix": context["matrix"], "results": [matrix_result]})
        invocation = receipt.with_suffix(".invocations.jsonl")
        invocation.write_text(json.dumps({"recorded_at": FINISHED, "matrix": context["matrix"], "reuse_existing": False, "result": matrix_result}) + "\n")
        return runner.MatrixExecution(exit_code=0, results=[matrix_result], completed=[], receipt_path=receipt, invocation_path=invocation)
    return execute


@pytest.fixture(autouse=True)
def no_harbor_probe(monkeypatch):
    monkeypatch.setattr(regrade, "lab_harbor_problem", lambda: None)


def collect(tmp: Path, row: dict[str, Any], target: cf.Grader, *, score: Any = 1, mutate=None, gate=None):
    gate = gate or regrade.CloudSpendGate("v7 approved native replay", 0.03, 0.01)
    observation = native.replay_and_collect(
        row, target, repo_root=tmp / "repo", work=tmp / "repo/runs/v7/item",
        spend_gate=gate, matrix_runner=evidence_runner(score, mutate=mutate),
    )
    return observation, gate


@pytest.mark.parametrize("score", [0, 1])
@pytest.mark.parametrize("original", ["null", "stale_error", "false", "pass"])
def test_new_native_measurement_is_independent_of_original_comparison(tmp_path, original, score):
    old = {
        "null": {"raw": None, "outcome": None},
        "stale_error": {"raw": 1, "outcome": None, "error": {}},
        "false": {"raw": 0, "outcome": False},
        "pass": {"raw": 1, "outcome": True},
    }[original]
    row, target = source(tmp_path, **old), grader(tmp_path)
    trial = Path(row["source_trial_dir"])
    before, original_row = files(trial), copy.deepcopy(row)
    observation, gate = collect(tmp_path, row, target, score=score)
    assert observation["v7_outcome"] is bool(score), observation
    assert observation["v7_error"] is None
    assert observation["replayed"] is True
    assert observation["native_evidence"]["dispatched"] is True
    if original == "null":
        assert observation["native_evidence"]["cf_comparison"] == "refused"
    assert observation["native_evidence"]["canonical_v6_outcome"] is row["v6_outcome"]
    assert row == original_row and files(trial) == before
    assert not (trial / "artifacts").exists()
    assert gate.executions == 1
    evidence = observation["native_evidence"]
    assert evidence["native_job_id"] == NEW_JOB and evidence["native_trial_id"] == NEW_TRIAL
    assert evidence["script"]["sha256"] == evidence["original_script_sha256"] == evidence["effective_script_sha256"]
    assert evidence["original_diff_sha256"] == evidence["effective_diff_sha256"] == row["diff"]["sha256"]
    assert all(digest(Path(ref["path"]).read_bytes()) == ref["sha256"] for ref in evidence["files"].values())


def test_native_sparse_configs_are_rehydrated_not_compared_raw(tmp_path):
    row, target = source(tmp_path), grader(tmp_path)
    observation, _ = collect(tmp_path, row, target)
    assert observation["v7_outcome"] is True, observation
    job = Path(observation["native_evidence"]["job_dir"])
    job_config = json.loads((job / "config.json").read_bytes())
    result_path = next(path for path in job.glob("*/result.json"))
    result = json.loads(result_path.read_bytes())
    saved_config = json.loads((result_path.parent / "config.json").read_bytes())
    assert "n_attempts" not in job_config
    assert "install_only" not in saved_config and result["config"]["install_only"] is False
    assert saved_config != result["config"]


def fail_native(context, failure):
    if failure == "matrix_infra":
        context["matrix_result"].update(status="infra", error="sandbox failed", rewards=[1])
    elif failure == "matrix_error":
        context["matrix_result"]["error"] = "stale infrastructure error"
    elif failure == "job_incomplete":
        context["job_result"]["finished_at"] = None
    elif failure == "job_exception":
        context["job_result"]["exception_info"] = {}
    elif failure == "trial_exception":
        context["trial_result"]["exception_info"] = {}
    elif failure == "phase_exception":
        context["trial_result"]["step_results"] = [{"exception_info": {}, "agent_execution": context["trial_result"]["agent_execution"]}]
    elif failure == "agent_incomplete":
        context["trial_result"]["agent_execution"]["finished_at"] = None
    elif failure == "oracle_missing":
        context["oracle_present"] = False
    elif failure == "oracle_exit":
        context["oracle_exit"] = 1
    elif failure == "job_timed_out":
        context["metadata"]["timed_out"] = True
    elif failure == "stale_result_reward":
        context["trial_result"]["verifier_result"]["rewards"]["reward"] = 0
    elif failure == "string_native_reward":
        context["trial_result"]["verifier_result"]["rewards"]["reward"] = "1"
    elif failure == "missing_config":
        context["omit"].add(context["job"] / "config.json")
    elif failure == "no_native_job":
        context["omit_job"] = True
    else:
        raise AssertionError(failure)


@pytest.mark.parametrize("failure", [
    "matrix_infra", "matrix_error", "job_incomplete", "job_exception", "trial_exception",
    "phase_exception", "agent_incomplete", "oracle_missing", "oracle_exit", "job_timed_out",
    "stale_result_reward", "string_native_reward", "missing_config", "no_native_job",
])
def test_scalar_success_never_overrides_failed_or_missing_native_evidence(tmp_path, failure):
    row, target = source(tmp_path), grader(tmp_path)
    observation, gate = collect(tmp_path, row, target, mutate=lambda context: fail_native(context, failure))
    assert observation["v7_outcome"] is None
    assert observation["v7_error"]
    assert observation["native_evidence"]["refusals"]
    assert observation["replayed"] is (failure != "no_native_job")
    assert gate.executions == 1


@pytest.mark.parametrize("score", [True, "1", float("nan"), float("inf"), 0.5, 2, None])
def test_only_exact_numeric_finite_binary_new_rewards_are_measurements(tmp_path, score):
    row, target = source(tmp_path), grader(tmp_path)
    observation, _ = collect(tmp_path, row, target, score=score)
    assert observation["v7_outcome"] is None and observation["v7_error"]


@pytest.mark.parametrize("score", [0, 1])
@pytest.mark.parametrize("timing", [
    "missing", None, {},
    {"started_at": "2026-10-17T06:00:01Z"},
    {"finished_at": FINISHED},
    {"started_at": None, "finished_at": FINISHED},
    {"started_at": "2026-10-17T06:00:01Z", "finished_at": None},
    {"started_at": "not-a-timestamp", "finished_at": FINISHED},
])
def test_binary_native_result_requires_completed_verifier(tmp_path, score, timing):
    row, target = source(tmp_path), grader(tmp_path)
    def mutate(context):
        if timing == "missing":
            context["trial_result"].pop("verifier")
        else:
            context["trial_result"]["verifier"] = copy.deepcopy(timing)
    observation, _ = collect(tmp_path, row, target, score=score, mutate=mutate)
    assert observation["v7_outcome"] is None
    assert observation["v7_error"] and observation["native_evidence"]["refusals"]


def test_completed_top_level_verifier_does_not_hide_an_incomplete_step(tmp_path):
    row, target = source(tmp_path), grader(tmp_path)
    def mutate(context):
        result = context["trial_result"]
        result["step_results"] = [{
            "step_name": "edit", "exception_info": None,
            "agent_execution": copy.deepcopy(result["agent_execution"]),
            "verifier": {"started_at": "2026-10-17T06:00:01Z", "finished_at": None},
        }]
    observation, _ = collect(tmp_path, row, target, mutate=mutate)
    assert observation["v7_outcome"] is None
    assert observation["v7_error"] == "native_verifier_execution_incomplete"


@pytest.mark.parametrize("score", [0, 1])
def test_binary_native_result_requires_a_consumed_reward_artifact(tmp_path, score):
    row, target = source(tmp_path), grader(tmp_path)
    observation, _ = collect(
        tmp_path, row, target, score=score,
        mutate=lambda context: context["reward_artifacts"].clear(),
    )
    assert observation["v7_outcome"] is None
    assert observation["v7_error"] == "native_reward_artifact_missing"


@pytest.mark.parametrize(("text", "score"), [
    ("+1", 1), ("1.", 1), ("00", 0), ("+0", 0), ("-0", 0),
    (" \n1e0\t", 1), ("0.", 0),
])
def test_harbor_float_text_binary_rewards_qualify(tmp_path, text, score):
    row, target = source(tmp_path), grader(tmp_path)
    observation, _ = collect(
        tmp_path, row, target, score=score,
        mutate=lambda context: context.update(reward_artifacts={"reward.txt": text}),
    )
    assert observation["v7_outcome"] is bool(score), observation
    assert observation["v7_error"] is None
    assert any(path.endswith("/verifier/reward.txt") for path in observation["native_evidence"]["files"])


@pytest.mark.parametrize(("text", "score"), [
    ("0.5", 1), ("-1", 0), ("2", 1), ("NaN", 1), ("+inf", 1),
    ("-Infinity", 0), ("1e309", 1), ("0", 1), ("1", 0),
    ("", 1), ('{"reward": 1}', 1), ("true", 1),
])
def test_invalid_or_disagreeing_text_reward_is_null(tmp_path, text, score):
    row, target = source(tmp_path), grader(tmp_path)
    observation, _ = collect(
        tmp_path, row, target, score=score,
        mutate=lambda context: context.update(reward_artifacts={"reward.txt": text}),
    )
    assert observation["v7_outcome"] is None
    assert observation["v7_error"] and observation["native_evidence"]["refusals"]


@pytest.mark.parametrize(("score", "stale_text"), [
    (1, "0"), (0, "1"), (1, "NaN"), (0, "not-a-number"), (1, ""),
])
def test_harbor_json_precedence_ignores_unconsumed_text(tmp_path, score, stale_text):
    row, target = source(tmp_path), grader(tmp_path)
    observation, _ = collect(
        tmp_path, row, target, score=score,
        mutate=lambda context: context["reward_artifacts"].update({"reward.txt": stale_text}),
    )
    assert observation["v7_outcome"] is bool(score), observation
    assert observation["v7_error"] is None


@pytest.mark.parametrize("raw_json", [
    "", "not JSON", "1", "[]", '{"reward": "1"}', '{"reward": 2}',
    '{"reward": NaN}', '{"reward": 1, "aux": Infinity}',
    '{"reward": 1, "aux": "1"}', '{"reward": 0}', '{"aux": 1}',
])
def test_invalid_or_disagreeing_json_never_falls_back_to_text(tmp_path, raw_json):
    row, target = source(tmp_path), grader(tmp_path)
    observation, _ = collect(
        tmp_path, row, target,
        mutate=lambda context: context.update(
            reward_artifacts={"reward.json": raw_json, "reward.txt": "1"},
        ),
    )
    assert observation["v7_outcome"] is None
    assert observation["v7_error"] and observation["native_evidence"]["refusals"]


@pytest.mark.parametrize(("rewards", "score"), [
    ({"reward": 1, "aux": 0.5}, 1), ({"reward": 0.0, "aux": -2}, 0),
    ({"reward": True}, 1), ({"reward": False}, 0),
])
def test_json_artifact_uses_harbor_numeric_mapping_and_schema(tmp_path, rewards, score):
    row, target = source(tmp_path), grader(tmp_path)
    observation, _ = collect(
        tmp_path, row, target, score=score,
        mutate=lambda context: context["reward_artifacts"].update({"reward.json": json.dumps(rewards)}),
    )
    # Harbor accepts bool JSON values as numbers and normalizes them through
    # VerifierResult; the retained native result itself must still be numeric.
    assert observation["v7_outcome"] is bool(score), observation
    assert observation["v7_error"] is None


@pytest.mark.parametrize("tamper", [
    "source", "diff", "script", "grader", "matrix_grader", "staged_digest", "staging_metadata",
    "trial_task_config", "trial_job_identity", "task_lock", "model_agent", "source_job_reused", "source_trial_reused",
])
def test_native_measurement_requires_original_script_grader_and_input_custody(tmp_path, tamper):
    row, target = source(tmp_path), grader(tmp_path)
    def mutate(context):
        if tamper == "source":
            (Path(row["source_trial_dir"]) / "config.json").write_text('{"job_id":"changed"}')
        elif tamper == "diff":
            Path(row["diff"]["path"]).write_bytes(PATCH + b"\n")
        elif tamper == "script":
            context["script"].write_text("#!/bin/bash\nexit 0\n")
        elif tamper == "grader":
            (target.package / "tests/test.sh").write_text("#!/bin/bash\nexit 1\n")
        elif tamper == "matrix_grader":
            context["matrix"]["task_package_digest"] = "sha256:" + "e" * 64
        elif tamper == "staged_digest":
            context["matrix_result"]["staged_task_digest"] = "sha256:" + "e" * 64
        elif tamper == "staging_metadata":
            context["metadata"]["task_staging"]["source_package_digest"] = "sha256:" + "e" * 64
        elif tamper == "trial_task_config":
            context["trial_config"]["task"] = {"path": "/different-task"}
        elif tamper == "trial_job_identity":
            context["trial_config"]["job_id"] = SOURCE_JOB
        elif tamper == "task_lock":
            context["trial_lock"]["task"]["digest"] = "sha256:" + "e" * 64
        elif tamper == "model_agent":
            context["trial_result"]["agent_info"] = {"name": "mimoagent", "model_info": {"name": "not-an-oracle"}}
        elif tamper == "source_job_reused":
            context["job_result"]["id"] = SOURCE_JOB
            context["trial_config"]["job_id"] = SOURCE_JOB
            context["trial_result"]["config"]["job_id"] = SOURCE_JOB
        elif tamper == "source_trial_reused":
            context["trial_result"]["id"] = SOURCE_TRIAL
    observation, _ = collect(tmp_path, row, target, mutate=mutate)
    assert observation["v7_outcome"] is None
    assert observation["v7_error"] and observation["native_evidence"]["refusals"]


@pytest.mark.parametrize("patch", [b"", PATCH.replace(b"\n", b"\r\n")])
def test_existing_empty_and_crlf_retained_diffs_are_not_rewritten(tmp_path, patch):
    row, target = source(tmp_path, patch=patch, raw=None, outcome=None), grader(tmp_path)
    before = files(Path(row["source_trial_dir"]))
    observation, gate = collect(tmp_path, row, target, score=0)
    assert observation["v7_outcome"] is False, observation
    script = Path(observation["native_evidence"]["script"]["path"]).read_bytes()
    assert script == cf._patch_script(patch.decode("utf-8"), "/testbed").encode("utf-8")
    if patch:
        assert base64.b64encode(patch) in script
    else:
        assert b"git apply" not in script
    assert files(Path(row["source_trial_dir"])) == before and gate.executions == 1


@pytest.mark.parametrize("problem", [
    "stripped_binary", "binary_placeholder", "invalid_utf8", "missing_diff", "missing_bytes",
    "stale_diff", "stale_source", "wrong_uid", "gpu",
])
def test_unreplayable_or_unbound_input_refuses_before_any_spend(tmp_path, problem):
    patch = PATCH
    if problem == "stripped_binary":
        patch += b"diff --git a/pkg/__pycache__/x.pyc b/pkg/__pycache__/x.pyc\nGIT binary patch\nliteral 1\n"
    elif problem == "binary_placeholder":
        patch += b"diff --git a/data.bin b/data.bin\nBinary files a/data.bin and b/data.bin differ\n"
    elif problem == "invalid_utf8":
        patch += b"\xff"
    row, target = source(tmp_path, patch=patch), grader(tmp_path)
    if problem == "missing_diff":
        row["diff"] = None
    elif problem == "missing_bytes":
        row["diff"]["path"] = str(tmp_path / "absent.diff")
    elif problem == "stale_diff":
        row["diff"]["sha256"] = "sha256:" + "f" * 64
    elif problem == "stale_source":
        row["source_digest"] = "0" * 64
    elif problem == "wrong_uid":
        row["source_uid"][1] = NEW_TRIAL
    elif problem == "gpu":
        config = target.package / "task.toml"
        config.write_text(config.read_text().replace('[environment]\n', '[environment]\ngpus = 1\n'))
        target = cf.Grader(TASK, target.transform, cf._digest(target.package), target.package, target.record)
    before = files(Path(row["source_trial_dir"]))
    observation, gate = collect(tmp_path, row, target)
    assert observation["v7_outcome"] is None and observation["v7_error"]
    assert not observation["replayed"] and not observation["native_evidence"]["dispatched"]
    assert gate.executions == 0
    assert files(Path(row["source_trial_dir"])) == before
    if problem == "stripped_binary":
        assert observation["v7_error"] == "retained_patch_would_be_modified"
        assert observation["native_evidence"]["original_diff_sha256"] != observation["native_evidence"]["effective_diff_sha256"]


def test_spend_cap_refusal_is_retained_without_a_fake_native_zero(tmp_path):
    row, target = source(tmp_path), grader(tmp_path)
    gate = regrade.CloudSpendGate("approved v7 replay", 0.01, 0.01, executions=1)
    observation, _ = collect(tmp_path, row, target, gate=gate)
    assert observation["v7_outcome"] is None and observation["v7_error"]
    assert observation["native_evidence"]["cf_refusals"] == ["spend_cap_reached"]
    assert not observation["replayed"] and not observation["native_evidence"]["dispatched"]
    assert gate.executions == 1


def test_backend_preflight_refusal_does_not_reserve_cloud_spend(tmp_path, monkeypatch):
    row, target = source(tmp_path), grader(tmp_path)
    monkeypatch.setattr(regrade, "lab_harbor_problem", lambda: "wrong Harbor revision")
    observation, gate = collect(tmp_path, row, target)
    assert observation["v7_outcome"] is None
    assert "wrong Harbor revision" in observation["v7_error"]
    assert not observation["replayed"] and gate.executions == 0


def test_parent_can_apply_existing_modal_policy_fields_without_losing_native_custody(tmp_path):
    row, target = source(tmp_path), grader(tmp_path)
    gate = regrade.CloudSpendGate("approved v7 replay", 0.01, 0.01)
    policies = {
        "modal_app_name": "mimo-clean-census-v7",
        "modal_resource_policy": "limit",
        "modal_sandbox_timeout_seconds": 900,
    }
    def bounded(matrix, *, root):
        return evidence_runner(0)(matrix.model_copy(update=policies), root=root)
    observation = native.replay_and_collect(
        row, target, repo_root=tmp_path / "repo", work=tmp_path / "repo/runs/v7/item",
        spend_gate=gate, matrix_runner=bounded,
    )
    assert observation["v7_outcome"] is False, observation
    assert observation["v7_error"] is None and gate.executions == 1
    receipt_ref = observation["native_evidence"]["matrix_receipt"]
    receipt = json.loads(Path(receipt_ref["path"]).read_bytes())
    assert all(receipt["matrix"][key] == value for key, value in policies.items())
