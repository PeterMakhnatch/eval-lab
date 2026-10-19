"""Exact retained-diff replay and independent native v7 measurement qualification.

The caller admits the external grader and owns scheduling, spend journaling and
observation persistence. A counterfactual comparison with an unmeasured original
can say ``refused`` even when its NEW native job is a valid measurement.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import runpy
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from harbor.models.job.config import JobConfig as NativeJobConfig
from harbor.models.trial.config import TrialConfig as NativeTrialConfig
from harbor.models.trial.result import TimingInfo as NativeTimingInfo
from harbor.models.verifier.result import VerifierResult as NativeVerifierResult

from evallab import runner as native_runner
from evallab.execution_contracts import (
    HOST_ISOLATED_DOCKER_ENVIRONMENT_IMPORT_PATH,
    LOCKED_DOCKER_ENVIRONMENT_IMPORT_PATH,
    MODAL_ENVIRONMENT_IMPORT_PATH,
)
from evallab.heldout_verifier import scan_agent_diff
from evallab.qa import counterfactual as cf
from evallab.registry import compute_task_digests, harbor_task_digest
from evallab.regrade import CloudSpendGate, modal_resource_blocker
from evallab.results import load_job
from evallab.schemas import ExperimentMatrix

# The caller may apply these existing native policies through matrix_runner.
_MODAL_POLICY_FIELDS = {
    "modal_app_name", "modal_resource_policy", "modal_sandbox_timeout_seconds",
    "modal_sandbox_create_max_attempts",
}


class _Unmeasured(ValueError):
    pass


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise _Unmeasured(reason)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _ref(path: Path) -> dict[str, str]:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path.resolve()), "sha256": "sha256:" + digest}


def _object(path: Path, evidence: dict[str, Any]) -> dict[str, Any]:
    data = path.read_bytes()
    evidence["files"][str(path.resolve())] = {"path": str(path.resolve()), "sha256": _sha(data)}
    value = json.loads(data.decode("utf-8"))
    _require(isinstance(value, dict), f"native_json_not_object:{path}")
    return value


def _binary(value: Any) -> bool:
    _require(
        type(value) in {int, float} and math.isfinite(value) and value in (0, 1),
        "native_reward_not_exact_finite_binary",
    )
    return value == 1


def _oracle(agent: Any) -> None:
    _require(
        isinstance(agent, dict) and agent.get("name") == "oracle"
        and agent.get("model_name") is None and agent.get("import_path") is None,
        "native_agent_not_model_free_oracle",
    )


def _modal(environment: Any) -> None:
    _require(
        isinstance(environment, dict)
        and (
            environment.get("import_path") == MODAL_ENVIRONMENT_IMPORT_PATH
            and environment.get("type") in (None, "modal")
            or environment.get("import_path") is None and environment.get("type") == "modal"
        ),
        "native_backend_not_modal",
    )


def _environment(value: Any, backend: str) -> None:
    if backend == "modal":
        _modal(value)
        return
    _require(
        backend == "docker" and isinstance(value, dict)
        and value.get("type") in (None, "docker")
        and value.get("import_path") in {
            HOST_ISOLATED_DOCKER_ENVIRONMENT_IMPORT_PATH, LOCKED_DOCKER_ENVIRONMENT_IMPORT_PATH,
        },
        "native_backend_not_first_party_isolated_docker",
    )


def _matrix(
    document: dict[str, Any], grader: cf.Grader, root: Path, script: bytes, backend: str = "modal",
) -> ExperimentMatrix:
    matrix = ExperimentMatrix.model_validate(document)
    _require(
        backend in {"modal", "docker"} and matrix.environment == backend and matrix.jobs_dir == "runs"
        and matrix.concurrency == 1 and len(matrix.runs) == 1
        and matrix.task_id == grader.task_name.rsplit("/", 1)[-1]
        and (root / matrix.task).resolve() == grader.package.resolve()
        and matrix.task_package_digest == grader.variant_digest,
        "native_matrix_grader_backend_or_layout_mismatch",
    )
    run = matrix.runs[0]
    _require(
        matrix.name == run.name and Path(run.name).name == run.name
        and run.name not in {".", ".."} and run.agent == "oracle" and run.model is None
        and run.attempts == 1 and not run.allow_billable and run.expect_reward is None
        and bool(run.solution),
        "native_matrix_not_single_model_free_replay",
    )
    script_path = root / "runs/qa-trace-regrade/scripts" / f"{_sha(script)[7:]}.sh"
    _require(
        not Path(run.solution).is_absolute() and (root / run.solution).resolve() == script_path.resolve()
        and script_path.read_bytes() == script,
        "native_script_not_actual_retained_patch",
    )
    digests = compute_task_digests(grader.package)
    _require(
        digests.package == grader.variant_digest and matrix.verifier_digest == digests.verifier,
        "native_full_grader_changed",
    )
    return matrix


def _qualify(
    row: dict[str, Any], execution: Any, submitted: dict[str, Any], grader: cf.Grader,
    root: Path, work: Path, script: bytes, evidence: dict[str, Any], *,
    backend: str = "modal", require_case_execution: bool = False,
) -> bool:
    _require(
        row.get("input_kind") == "native_patch_replay" and row.get("environment") == backend
        and row.get("grader_identity") == grader.identity_json
        and not row.get("refusals") and row.get("execution_error") is None,
        "native_replay_refused_or_failed",
    )
    score = _binary(row.get("regraded"))
    expected_receipt = root / "runs/.executor" / f"{submitted['matrix_id']}.matrix.json"
    _require(
        Path(row.get("matrix_receipt", "")).resolve() == expected_receipt.resolve()
        and Path(execution.receipt_path).resolve() == expected_receipt.resolve(),
        "native_matrix_receipt_path_mismatch",
    )
    receipt = _object(expected_receipt, evidence)
    matrix_doc = receipt.get("matrix")
    _require(isinstance(matrix_doc, dict), "native_matrix_document_missing")
    _require(
        {k: v for k, v in matrix_doc.items() if k not in _MODAL_POLICY_FIELDS}
        == {k: v for k, v in submitted.items() if k not in _MODAL_POLICY_FIELDS},
        "native_matrix_changed_since_submission",
    )
    matrix = _matrix(matrix_doc, grader, root, script, backend)
    run = matrix.runs[0]
    outcomes = receipt.get("results")
    _require(
        isinstance(outcomes, list) and len(outcomes) == 1 and isinstance(outcomes[0], dict)
        and execution.results == outcomes and execution.exit_code == 0,
        "native_matrix_execution_receipt_mismatch",
    )
    result = outcomes[0]
    _require(
        result.get("name") == run.name and result.get("status") in {"ok", "mismatch"}
        and result.get("error") is None and isinstance(result.get("rewards"), list)
        and len(result["rewards"]) == 1 and _binary(result["rewards"][0]) == score,
        "native_matrix_result_failed_or_disagrees",
    )
    invocation_path = expected_receipt.with_suffix(".invocations.jsonl")
    _require(
        Path(execution.invocation_path).resolve() == invocation_path.resolve(),
        "native_invocation_path_mismatch",
    )
    invocations = [json.loads(line) for line in invocation_path.read_bytes().decode("utf-8").splitlines()]
    evidence["files"][str(invocation_path)] = _ref(invocation_path)
    _require(
        len(invocations) == 1 and invocations[0].get("matrix") == matrix_doc
        and invocations[0].get("reuse_existing") is False
        and invocations[0].get("result") == result and bool(invocations[0].get("recorded_at")),
        "native_invocation_not_fresh_matching_dispatch",
    )
    script_path = (root / run.solution).resolve()
    script_ref = _ref(script_path)
    evidence["files"][str(script_path)] = script_ref
    _require(
        row.get("script_sha256") == _sha(script)[7:]
        and result.get("solution_sha256") == _sha(script)[7:]
        and script_ref["sha256"] == _sha(script),
        "native_script_hash_mismatch",
    )
    # Reconstruct the canonical solution override AND the native host staging.
    # Neither temporary package is a fabricated retained source snapshot.
    with native_runner.staged_matrix_request(matrix, run, repo_root=root) as (request, provenance):
        source_harbor = harbor_task_digest(request.task)
        with tempfile.TemporaryDirectory(prefix="v7-qualify-", dir=work) as temporary:
            staged, _ = native_runner.stage_request_task(request, Path(temporary) / run.name)
            staged_harbor = harbor_task_digest(staged)
        task_table = tomllib.loads((request.task / "task.toml").read_text()).get("task") or {}
        expected_staging = {
            "source_task_basename": request.task.name,
            "declared_task_name": task_table.get("name"),
            "source_package_digest": provenance["staged_task_digest"],
            "source_harbor_digest": source_harbor,
            "staged_task_name": run.name,
            "staged_harbor_digest": staged_harbor,
        }
    _require(
        result.get("staged_task_digest") == provenance["staged_task_digest"],
        "native_solution_staged_digest_mismatch",
    )
    job_path = root / matrix.jobs_dir / run.name
    _require(Path(row.get("job_dir", "")).resolve() == job_path.resolve(), "native_job_path_mismatch")
    _require(not any(p.is_symlink() for p in job_path.rglob("*")), "native_job_contains_symlink")
    for path in job_path.rglob("*"):
        if path.is_file():
            evidence["files"][str(path.resolve())] = _ref(path)
    job = load_job(job_path)
    _require(
        job.result.get("exception_info") is None and len(job.trials) == 1
        and isinstance(job.result.get("id"), str) and bool(job.result["id"])
        and job.id != evidence["source_uid"][0]
        and type(job.metadata.get("exit_code")) is int and job.metadata["exit_code"] == 0
        and job.metadata.get("timed_out") is False,
        "native_job_failed_or_incomplete",
    )
    staging = job.metadata.get("task_staging")
    _require(
        isinstance(staging, dict) and staging.get("schema_version") == 1
        and all(staging.get(k) == v for k, v in expected_staging.items()),
        "native_job_staged_input_mismatch",
    )
    saved_config = _object(job_path / "config.json", evidence)
    lock = _object(job_path / "lock.json", evidence)
    _require(saved_config == job.config and lock == job.lock, "native_job_evidence_changed")
    # Harbor saves exclude_defaults=True configs, but embeds the complete trial
    # config in result.json. Rehydrate through its actual schemas, not guesses.
    config = NativeJobConfig.model_validate(saved_config).model_dump(mode="json")
    _require(
        config.get("job_name") == run.name
        and config.get("n_attempts") == 1 and config.get("n_concurrent_trials") == 1
        and len(config.get("agents", [])) == 1 and len(config.get("tasks", [])) == 1
        and not config.get("datasets") and not config.get("install_only") and config.get("user_agent") is None
        and not (config.get("verifier") or {}).get("disable"),
        "native_job_config_mismatch",
    )
    _oracle(config["agents"][0])
    _environment(config.get("environment"), backend)
    executed_path = root / matrix.jobs_dir / ".exec-stage" / run.name
    _require(config["tasks"][0].get("path") == str(executed_path), "native_job_task_path_mismatch")
    trial = job.trials[0]
    data = _object(trial.path / "result.json", evidence)
    saved_trial_config = _object(trial.path / "config.json", evidence)
    trial_lock = _object(trial.path / "lock.json", evidence)
    _require(saved_trial_config == trial.config and trial_lock == trial.lock, "native_trial_evidence_changed")
    trial_config = NativeTrialConfig.model_validate(saved_trial_config).model_dump(mode="json")
    result_config = NativeTrialConfig.model_validate(data.get("config")).model_dump(mode="json")
    _require(
        data == trial.result
        and isinstance(data.get("id"), str) and bool(data["id"])
        and data["id"] != evidence["source_uid"][1] and data["id"] != job.id
        and data.get("trial_name") == trial.path.name
        and data.get("task_name") == (expected_staging["declared_task_name"] or run.name)
        and data.get("finished_at") and data.get("exception_info") is None
        and result_config == trial_config and trial_config.get("job_id") == job.id
        and trial_config.get("trial_name") == trial.path.name
        and not trial_config.get("source_trial") and not trial_config.get("install_only")
        and trial_config.get("user_agent") is None
        and not (trial_config.get("verifier") or {}).get("disable")
        and (data.get("task_id") or {}).get("path") == str(executed_path)
        and (trial_config.get("task") or {}).get("path") == str(executed_path)
        and lock.get("trials") == [trial_lock],
        "native_trial_result_config_or_identity_mismatch",
    )
    _oracle(trial_config.get("agent"))
    _oracle(trial_lock.get("agent"))
    _environment(trial_config.get("environment"), backend)
    _environment(trial_lock.get("environment"), backend)
    _require(
        (data.get("agent_info") or {}).get("name") == "oracle"
        and (data.get("agent_info") or {}).get("model_info") is None
        and not (trial_lock.get("verifier") or {}).get("disable"),
        "native_trial_agent_or_verifier_mismatch",
    )
    task_lock = trial_lock.get("task") or {}
    _require(
        task_lock.get("type") == "local" and task_lock.get("name") == run.name
        and task_lock.get("path") == str(executed_path) and task_lock.get("digest") == staged_harbor,
        "native_trial_task_lock_mismatch",
    )
    phases = data.get("step_results") or [data]
    _require(
        all(isinstance(phase, dict) and phase.get("exception_info") is None
            and (phase.get("agent_execution") or {}).get("started_at")
            and (phase.get("agent_execution") or {}).get("finished_at") for phase in phases),
        "native_oracle_execution_failed_or_incomplete",
    )
    for phase in phases:
        timing = NativeTimingInfo.model_validate(phase.get("verifier"))
        _require(
            timing.started_at is not None and timing.finished_at is not None,
            "native_verifier_execution_incomplete",
        )
    native = native_runner.matrix_run_outcome(job, run)
    _require(
        native.get("status") == result["status"] and native.get("error") is None
        and native.get("rewards") == result["rewards"]
        and _binary(((data.get("verifier_result") or {}).get("rewards") or {}).get("reward")) == score,
        "native_trial_reward_or_execution_disagrees",
    )
    # Harbor consumes JSON when present; a stale lower-priority text file is
    # irrelevant. An invalid JSON file must not fall back to otherwise valid text.
    path = trial.path / "verifier/reward.json"
    if not path.exists():
        path = trial.path / "verifier/reward.txt"
    _require(path.is_file(), "native_reward_artifact_missing")
    if path.suffix == ".json":
        rewards = _object(path, evidence)
        _require(
            all(isinstance(value, (int, float))
                and (not isinstance(value, float) or math.isfinite(value))
                for value in rewards.values()),
            "native_reward_artifact_invalid",
        )
        value = (NativeVerifierResult(rewards=rewards).rewards or {}).get("reward")
    else:
        value = float(path.read_text())
    _require(_binary(value) == score, "native_reward_artifact_disagrees")
    if require_case_execution:
        census = runpy.run_path(str(Path(__file__).resolve().parents[1] / "mimo-v6-full-fleet/census.py"))
        proof = census["positive_case_execution"](trial.path)
        evidence["positive_case_execution"] = proof
        _require(proof is True, "native_positive_case_execution_unproven")
    evidence.update(
        matrix_receipt=_ref(expected_receipt), invocations=_ref(invocation_path),
        job_dir=str(job_path), native_job_id=job.id, native_trial_id=trial.id,
        script=script_ref, staged_input=expected_staging,
    )
    return score


def replay_and_collect(
    source_row: dict[str, Any], grader: cf.Grader, *, repo_root: Path, work: Path,
    spend_gate: CloudSpendGate | None = None, matrix_runner: Any = None,
    backend: str = "modal", require_case_execution: bool = False,
) -> dict[str, Any]:
    """Return one nullable observation; source failures never become new zeroes.

    An existing empty diff is replayable. Missing/changed/non-UTF8 bytes, any
    binary-artifact stripping, native refusals and incomplete evidence are not.
    Modal still requires the shared spend gate; Docker uses cf.replay's shared FIFO.
    """
    evidence: dict[str, Any] = {
        "schema": "heldout-v7.native/v1", "engine": cf.ENGINE_VERSION,
        "dispatched": False, "files": {}, "refusals": [],
        "backend": backend,
        "canonical_v6_outcome": source_row.get("v6_outcome"),
        "canonical_v6_error": source_row.get("v6_error"),
    }
    observation: dict[str, Any] = {
        "source_uid": source_row.get("source_uid"), "v7_outcome": None,
        "v7_error": None, "replayed": False, "native_evidence": evidence,
    }
    state: dict[str, Any] = {}
    source: Path | None = None
    try:
        uid = source_row.get("source_uid")
        _require(
            isinstance(uid, list) and len(uid) == 2 and all(isinstance(v, str) and v for v in uid),
            "source_native_uid_missing",
        )
        source = Path(source_row["source_trial_dir"])
        _require(source.is_absolute() and source.is_dir(), "source_trial_missing")
        source = source.resolve()
        evidence.update(source_uid=list(uid), source_trial_dir=str(source))
        root, work = repo_root.resolve(), work.resolve()
        _require(
            not work.is_relative_to(source.parent) and not source.parent.is_relative_to(work)
            and not work.is_relative_to(grader.package.resolve())
            and not grader.package.resolve().is_relative_to(work),
            "runtime_work_overlaps_immutable_input",
        )
        for output in (
            root / "runs", root / "runs/.executor",
            root / "runs/qa-trace-regrade", root / "runs/qa-trace-regrade/scripts",
        ):
            _require(
                output.resolve() == output and not output.is_relative_to(source.parent)
                and not output.is_relative_to(grader.package.resolve()),
                "native_output_not_owned_or_overlaps_immutable_input",
            )
        _require(cf.source_digest(source) == source_row.get("source_digest"), "source_digest_mismatch")
        evidence["source_digest_before"] = source_row["source_digest"]
        data = _object(source / "result.json", evidence)
        configs = [data.get("config")]
        if (source / "config.json").is_file():
            configs.append(_object(source / "config.json", evidence))
        _require(
            data.get("id") == uid[1] and any(isinstance(c, dict) and c.get("job_id") == uid[0] for c in configs)
            and all(not isinstance(c, dict) or c.get("job_id") in (None, uid[0]) for c in configs),
            "source_native_uid_mismatch",
        )
        if (source.parent / "result.json").is_file():
            job = _object(source.parent / "result.json", evidence)
            _require(job.get("id") == uid[0], "source_native_job_identity_mismatch")
        diff_ref = source_row.get("diff")
        _require(isinstance(diff_ref, dict), "source_patch_unavailable")
        diff_path = Path(diff_ref["path"])
        _require(diff_path.is_absolute() and diff_path.is_file(), "source_patch_unavailable")
        diff_bytes = diff_path.read_bytes()
        _require(
            isinstance(diff_ref.get("sha256"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", diff_ref["sha256"])
            and _sha(diff_bytes) == diff_ref["sha256"],
            "source_patch_digest_mismatch",
        )
        evidence["diff"] = {"path": str(diff_path.resolve()), "sha256": _sha(diff_bytes)}
        evidence["files"][str(diff_path.resolve())] = evidence["diff"]
        text = diff_bytes.decode("utf-8", errors="strict")
        effective, dropped = cf.strip_binary_artifacts(text)
        evidence.update(original_diff_sha256=_sha(diff_bytes), effective_diff_sha256=_sha(effective.encode("utf-8")), dropped_binary_artifacts=dropped)
        _require(effective.encode("utf-8") == diff_bytes and not dropped, "retained_patch_would_be_modified")
        problems, _ = scan_agent_diff(str(diff_path))
        evidence["diff_problems"] = problems
        _require(not problems, "diff_unreplayable:" + ";".join(problems))
        _require(source_row.get("task_id") == grader.task_name, "source_grader_task_mismatch")
        cf.canonical_graders([grader])
        _require(not any(p.is_symlink() for p in grader.package.rglob("*")), "grader_package_contains_symlink")
        _require(backend in {"modal", "docker"}, "native_backend_not_authorized")
        if backend == "modal":
            blocker = modal_resource_blocker(grader.package)
            _require(blocker is None, "modal_resource_ineligible:" + str(blocker))
        cf.check_backend(backend, spend_gate)
        config = tomllib.loads((grader.package / "task.toml").read_bytes().decode("utf-8"))
        evidence["files"][str((grader.package / "task.toml").resolve())] = _ref(grader.package / "task.toml")
        evidence["grader"] = {**grader.to_json(), "verifier_digest": compute_task_digests(grader.package).verifier}
        _require(isinstance(config.get("environment", {}).get("workdir"), str), "grader_workdir_missing")
        script = cf._patch_script(text, config["environment"]["workdir"]).encode("utf-8")
        evidence.update(
            original_script_sha256=_sha(script), effective_script_sha256=_sha(script),
        )
        work.mkdir(parents=True, exist_ok=True)
        native_path = work / "native-replay.json"
        _require(not native_path.exists(), "runtime_native_observation_already_exists")

        def execute(matrix: ExperimentMatrix, *, root: Path) -> Any:
            _require(not state, "multiple_native_dispatches_refused")
            _require(root.resolve() == repo_root.resolve(), "native_dispatch_root_mismatch")
            submitted = matrix.model_dump(mode="json")
            checked = _matrix(submitted, grader, root.resolve(), script, backend)
            job_path = root / checked.jobs_dir / checked.runs[0].name
            receipt = root / checked.jobs_dir / ".executor" / f"{checked.matrix_id}.matrix.json"
            _require(not job_path.exists() and not receipt.exists() and not receipt.with_suffix(".invocations.jsonl").exists(), "native_existing_job_or_receipt_refused")
            _require(cf.source_digest(source) == source_row["source_digest"], "source_changed_before_dispatch")
            _require(diff_path.read_bytes() == diff_bytes, "retained_patch_changed_before_dispatch")
            state.update(submitted=submitted, job_path=job_path, receipt=receipt)
            evidence["submitted_matrix"] = submitted
            evidence["dispatched"] = True
            state["execution"] = (matrix_runner or native_runner.run_matrix)(matrix, root=root)
            return state["execution"]

        row = cf.replay(source, grader, work / "replay", diff_text=text, repo_root=root,
                        matrix_runner=execute, environment=backend, spend_gate=spend_gate)
        with native_path.open("x", encoding="utf-8") as stream:
            json.dump(row, stream, indent=2)
            stream.write("\n")
        evidence["cf_result"] = _ref(native_path)
        evidence["files"][str(native_path)] = evidence["cf_result"]
        evidence["cf_comparison"] = row.get("outcome")
        evidence["cf_recorded"] = row.get("recorded")
        evidence["cf_refusals"] = row.get("refusals", [])
        _require("execution" in state, "native_not_dispatched:" + ";".join(row.get("refusals", [])))
        observation["v7_outcome"] = _qualify(
            row, state["execution"], state["submitted"], grader, root, work, script, evidence,
            backend=backend, require_case_execution=require_case_execution,
        )
    except Exception as exc:
        observation["v7_outcome"] = None
        observation["v7_error"] = str(exc) if isinstance(exc, _Unmeasured) else f"{type(exc).__name__}:{exc}"
        evidence["refusals"].append(observation["v7_error"])
    finally:
        try:
            job_path = state.get("job_path")
            if job_path is not None:
                observation["replayed"] = job_path.is_dir() and any(
                    (job_path / name).is_file() for name in ("result.json", "config.json")
                )
                evidence["job_dir"] = str(job_path)
                paths = (state["receipt"], state["receipt"].with_suffix(".invocations.jsonl"))
                for path in paths:
                    if path.is_file() and str(path.resolve()) not in evidence["files"]:
                        evidence["files"][str(path.resolve())] = _ref(path)
                for path in job_path.rglob("*"):
                    _require(not path.is_symlink(), "native_job_contains_symlink")
                    if path.is_file() and str(path.resolve()) not in evidence["files"]:
                        evidence["files"][str(path.resolve())] = _ref(path)
            if source is not None and "source_digest_before" in evidence:
                evidence["source_digest_after"] = cf.source_digest(source)
                _require(evidence["source_digest_after"] == evidence["source_digest_before"], "immutable_source_changed_during_replay")
            for reference in evidence["files"].values():
                _require(_ref(Path(reference["path"])) == reference, "bound_evidence_changed_during_qualification:" + reference["path"])
            if "grader" in evidence:
                _require(cf._digest(grader.package) == grader.variant_digest, "native_full_grader_changed")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, _Unmeasured) else f"{type(exc).__name__}:{exc}"
            observation["v7_outcome"] = None
            observation["v7_error"] = reason if observation["v7_error"] is None else observation["v7_error"] + ";" + reason
            evidence["refusals"].append(reason)
    return observation
