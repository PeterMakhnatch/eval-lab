"""Read-only admission of actual native correction outputs; never execute a job.

Native repaired-package observations are not counterfactual/regrade receipts. For
an evaluation request the same native reward belongs ONLY to the original grader.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import partition as p

EXECUTION_SCHEMA = "envcheck.native-correction-execution/v1"
NATIVE_MEASURED_BASIS = "native_execution_on_pinned_repaired_package"


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _same(actual: Any, expected: Any, message: str) -> None:
    _require(actual == expected, message)


def _checked_ref(ref: dict[str, Any]) -> Path:
    path = Path(ref["path"])
    _same(p.file_ref(path), {key: ref[key] for key in ("path", "sha256")},
          f"native evidence bytes changed: {path}")
    return path


def _jsonl_snapshot(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = path.resolve()
    data = path.read_bytes()
    rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    _require(all(isinstance(row, dict) for row in rows), "execution JSONL must contain objects")
    return {"path": str(path), "sha256": p.sha256(data)}, rows


def execution_binding(path: Path, execution_id: str) -> dict[str, Any]:
    """Bind one untouched native terminal-trial observation, including failed stops."""
    execution_id = str(UUID(execution_id))
    ref, rows = _jsonl_snapshot(path)
    selected = [(index, row) for index, row in enumerate(rows) if row.get("execution_id") == execution_id]
    _require(len(selected) == 1, "execution UUID must select exactly one untouched native row")
    index, row = selected[0]
    _same(row.get("schema"), EXECUTION_SCHEMA, "not an actual native correction execution row")
    _require(row.get("native_result") and row.get("source_trial"), "native execution has no actual trial result")
    return {**ref, "row_index": index, "row_sha256": p.sha256(p.canonical_bytes(row))}


def _bound_execution(binding: dict[str, Any]) -> dict[str, Any]:
    ref, rows = _jsonl_snapshot(Path(binding["path"]))
    _same(ref, {key: binding[key] for key in ("path", "sha256")}, "native execution JSONL changed")
    index = binding["row_index"]
    _require(type(index) is int and 0 <= index < len(rows), "native execution row selector is outside its input")
    row = rows[index]
    _same(p.sha256(p.canonical_bytes(row)), binding["row_sha256"], "native execution row changed")
    _same(row.get("schema"), EXECUTION_SCHEMA, "unsupported native execution evidence schema")
    return row


def _checked_request(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    import correction_data as c
    import evaluation_corrections as e

    from evallab.task_variants import task_directory_digest

    _same(request.get("schema"), "envcheck.correction-request/v1", "unsupported native correction request")
    heldout, heldout_ref = p.frozen_heldout(Path(request["heldout_manifest"]["path"]))
    if request.get("purpose") == "evaluation":
        e.checked_request(request)
    else:
        c.checked_request(request, heldout, heldout_ref)
        _same(request.get("heldout"), False, "training correction must explicitly exclude all33")
        _require(request.get("training_allowed") is not False, "request explicitly forbids training")
        _require(p.parent_task(request["task"]) not in heldout, "heldout parent cannot become training")
        row = request["source_partition"]
        _require(row.get("partition") == "false_pass" and row.get("status") == "measured"
                 and row.get("original_reward") == 1.0 and row.get("repaired_reward") == 0.0,
                 "training source is not an observed original1/repaired0 false pass")
        evidence = row["provenance"]["selected_receipt"]
        c.checked_evidence(evidence)
        _require(not p.validate_receipt(row, row["variant"], evidence["row"]), "source repaired-failure receipt no longer validates")
        manifest = row["variant"]["manifest_row"]
        _require(not any(manifest.get(key) for key in ("training_excluded", "training_exclusion", "excluded_from_training"))
                 and manifest.get("training_eligible") is not False, "manifest excludes this task from training")
    original = c.checked_source(request["source_partition"])
    for key in ("teacher", "copied_fix", "alias_conflicts", "attempt_conflict"):
        _require(not (request["source_partition"].get("flags") or {}).get(key)
                 and not original["flags"].get(key), f"ineligible original source flag: {key}")
    _same((original["execution_id"], original["task"], original["model"]),
          (request["source_execution_id"], request["task"], request["model"]), "correction changed source UUID/task/model")
    pin = request["source_partition"]["variant"]
    _require(p.mechanical_grader_admitted(pin), "repaired grader lacks mechanical admission")
    meta = request["provenance"]["correction"]
    _same(meta.get("allowed_model_inputs"), ["task_prompt", "source_patch", "repaired_failure_stdout"], "unrecognized model inputs")
    for key in ("hidden_test_source_included", "reference_source_included", "full_historical_context_included"):
        _same(meta.get(key), False, "hidden/reference source or historical context is forbidden")
    executed = meta["task_package_snapshot"]
    repaired = meta["repaired_task_package_snapshot"] if request["purpose"] == "evaluation" else executed
    for package in (executed, repaired):
        path = Path(package["path"]).resolve()
        _require(path.is_relative_to(p.REPO / "runs") and not any(item.is_symlink() for item in path.rglob("*")),
                 "native request snapshot is outside runs/ or contains symlinks")
        _same(task_directory_digest(path), package["sha256"], "native request package snapshot changed")
    _same(repaired["sha256"], pin["repaired_digest"], "repaired snapshot differs from the admitted grader pin")
    _same((Path(executed["path"]) / "instruction.md").read_bytes(), Path(meta["task_prompt"]["path"]).read_bytes(),
          "executed snapshot changed the actual task instruction")
    return original, executed


def checked_completion(request: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    """Recheck native identity, bytes, setup, actual input and pre-hidden patch.

    A return value is evidence, not success admission: native stop/infra/missing
    reward flags and the actual native status remain intact for every consumer.
    """
    import correction_data as c
    import evaluation_corrections as e

    from evallab.registry import harbor_task_digest
    from evallab.regrade import verifier_identity
    from evallab.schemas import ExperimentSpec
    from evallab.task_variants import task_directory_digest

    execution = _bound_execution(binding)
    original, executed = _checked_request(request)
    for key in ("request_id", "request", "source_execution_id", "task", "model", "purpose", "heldout",
                "heldout_manifest", "protocol", "partition_scope"):
        _same(execution.get(key), request[key], f"native execution changed request binding: {key}")
    _same(execution.get("training_allowed"), not request["heldout"], "native execution changed heldout isolation")
    _same(execution.get("source_original_reward"), original["original_reward"], "native execution changed original source reward")
    _same(execution.get("source_flags"), request["source_partition"].get("flags"), "native execution changed original source flags")
    _same(execution.get("repaired_digest"), request["source_partition"]["variant"]["repaired_digest"], "native execution changed repaired grader")
    _same(execution["provenance"]["request"], request["request"], "native execution changed request provenance")
    _same(execution["provenance"]["correction"], request["provenance"]["correction"], "native execution changed feedback provenance")
    input_path = _checked_ref(execution["input_file"])
    inputs = p.read_jsonl(input_path) if input_path.suffix != ".json" else [p.read_json(input_path)]
    if input_path.suffix == ".json" and "request" not in inputs[0]:
        inputs[0] = {**inputs[0], "request": execution["input_file"]}
    matches = [row for row in inputs if p.sha256(p.canonical_bytes(row)) == execution["input_row_sha256"]]
    _same(matches, [request], "native execution does not bind one exact retained request input row")
    _same(execution.get("baseline_runtime_matched"), False, "reduced correction runtime cannot be labeled baseline-matched")
    _require(isinstance(execution.get("notice"), str) and execution["notice"], "native runtime confound notice is missing")
    _same(execution.get("native_authorization_recorded"), True, "native queue authorization was not recorded")

    trial = Path(execution["source_trial"]).resolve()
    current = p.source_record(trial)
    alias = current["alias"]
    files = alias["files"]
    execution_id = str(UUID(execution["execution_id"]))
    _same((current["execution_id"], current["task"], current["model"]),
          (execution_id, request["task"], request["model"]), "actual native UUID/task/model changed")
    _require(execution_id != request["source_execution_id"], "correction cannot reuse its original source UUID")
    _same(execution.get("native_trial_id"), execution_id, "native trial identity changed")
    _same(execution.get("native_provenance"), alias, "native source alias/lock/checksum/trajectory bytes changed")
    _same(execution.get("flags"), current["flags"], "native clean/stop/source flags changed")
    _same(execution.get("native_result"), files["result"], "native result binding changed")
    for key in ("teacher", "copied_fix", "alias_conflicts", "attempt_conflict"):
        _require(not current["flags"].get(key) and not execution.get(key), f"ineligible native completion flag: {key}")
    _same(execution.get("teacher"), current["flags"]["teacher"], "native teacher flag changed")
    _same(execution.get("copied_fix"), current["flags"]["copied_fix"], "native copied-fix flag changed")
    _require(p.digest(alias.get("task_checksum")) is not None, "native task checksum is missing or malformed")

    native_files = execution["native_files"]
    for ref in native_files.values():
        _checked_ref(ref)
    for key, field in (("native_trajectory", "native"), ("trajectory", "trajectory"), ("lock", "lock"), ("fingerprint", "fingerprint")):
        if field in files or key in native_files:
            _same(native_files.get(key), files.get(field), f"native {key} reference differs from its actual source alias")
    _same(execution.get("native_trajectory"), files["native"], "actual native conversation binding changed")
    _same(execution["provenance"].get("native_trajectory"), files["native"], "native conversation provenance changed")
    raw = p.read_json(Path(files["result"]["path"]))
    config = raw["config"]
    agent = config["agent"]
    _same(str(UUID(config["job_id"])), execution["native_job_id"], "native job UUID changed")
    _same(config.get("source_trial"), None, "a replay is not a student model completion")
    _require(not agent.get("resume_trajectory") and not agent.get("load_trajectory"), "restored native history is forbidden")
    extra = request["provenance"]["correction"]["extra_instruction"]
    _same([str(Path(path).resolve()) for path in config.get("extra_instruction_paths") or []],
          [str(Path(extra["path"]).resolve())], "native extra-instruction input changed")
    _require(not config.get("extra_instructions"), "native execution contains unbound inline instructions")
    messages, tools = p.native_messages(Path(files["native"]["path"]))
    matched, boundary = c.match_request(current, [request], messages)
    _same(matched, request, "actual task+feedback input no longer matches its request")
    _same(execution.get("initial_user_message_index"), boundary, "native reset-context boundary changed")

    spec_path = _checked_ref(native_files["experiment-spec.json"])
    spec = ExperimentSpec.model_validate_json(spec_path.read_text())
    queued_path = _checked_ref(execution["queued_spec"])
    queued = ExperimentSpec.model_validate_json(queued_path.read_text())
    _same(spec.model_dump(mode="json"), queued.model_dump(mode="json"), "actual native spec differs from its queued spec")
    _same((spec.spec_id, spec.name, spec.task, spec.model, spec.agent),
          (execution["spec_id"], execution["job_name"], request["task"], request["model"], "mimoagent"), "native spec identity changed")
    _same(spec.question_ref, request["request"]["path"], "native spec points at another request")
    _same((spec.attempts, spec.concurrency, spec.diff_sources), (1, 1, []), "native correction is not a single fresh attempt")
    _same((spec.task_path, spec.task_package_digest, spec.extra_instruction_path, spec.extra_instruction_sha256),
          (request["native_spec_fields"]["task_path"], executed["sha256"],
           request["native_spec_fields"]["extra_instruction_path"], extra["sha256"]), "native spec lost package/input-byte custody")
    _same((p.REPO / spec.task_path).resolve(), Path(executed["path"]).resolve(), "native spec task path changed")
    _same((p.REPO / spec.extra_instruction_path).resolve(), Path(extra["path"]).resolve(), "native spec extra-instruction path changed")
    _same(spec.egress_lock, True, "native correction spec no longer declares locked egress")
    job_dir = (p.REPO / spec.jobs_dir / spec.name).resolve()
    _same(Path(execution["job_dir"]).resolve(), job_dir, "native job directory changed")
    _same(trial.parent, job_dir, "native result is not this spec's actual trial")
    _same(spec_path.resolve(), job_dir / "experiment-spec.json", "native spec is outside its actual job")
    _same(execution.get("queue_state"), queued_path.parent.name, "native queue state binding changed")
    for change in execution["declared_runtime_changes"]:
        _same(change["request_value"], request["native_spec_fields"].get(change["field"]), "native runtime change lost its original value")
        _same(change["correction_value"], getattr(spec, change["field"]), "native runtime change differs from the actual spec")

    lock = p.read_json(Path(files["lock"]["path"]))
    # Harbor lock digest and Eval Lab package digest deliberately use different algorithms.
    harbor_digest = harbor_task_digest(Path(executed["path"]))
    _same(lock["task"]["digest"], harbor_digest, "native lock does not bind the requested package's Harbor digest")
    _same(alias["executed_task_digest"], harbor_digest, "native executed-task identity changed")
    _same(lock["agent"]["model_name"], request["model"], "native lock changed the exact student selector")
    _same(lock["agent"]["name"], agent["name"], "native lock changed the agent")
    _require(not lock["agent"].get("resume_trajectory") and not lock["agent"].get("load_trajectory"), "native lock restores history")
    task_path = Path(lock["task"]["path"]).resolve()
    _same(Path(config["task"]["path"]).resolve(), task_path, "native result/lock task paths disagree")
    _same(Path(raw["task_id"]["path"]).resolve(), task_path, "native task identity path disagrees with its lock")
    _same(task_path, (p.REPO / spec.jobs_dir / ".exec-stage" / spec.name).resolve(), "native task escaped the spec's private staging path")
    lab_path = _checked_ref(native_files["lab-metadata.json"])
    _same(lab_path.resolve(), job_dir / "lab-metadata.json", "native job metadata path changed")
    lab = p.read_json(lab_path)
    staging = lab["task_staging"]
    _same((staging["source_package_digest"], staging["source_harbor_digest"], staging["staged_harbor_digest"]),
          (executed["sha256"], harbor_digest, harbor_digest), "native task staging changed the requested source package")
    _same(staging.get("network_adaptation"), None, "native task staging changed the frozen package")
    _require(not lab.get("model_identity_mismatch") and not (lab.get("model_identity") or {}).get("mismatch"),
             "native provider returned a different model identity")
    _same(lab["experiment_spec"]["sha256"], native_files["experiment-spec.json"]["sha256"], "native metadata spec digest changed")

    actual_setup = p.read_json(Path(files["fingerprint"]["path"]))
    job_setup = p.read_json(_checked_ref(native_files["setup-fingerprint.json"]))
    for key in ("schema", "subject", "reference_profile", "task", "environment", "sampling", "server", "harness", "budgets", "deviations"):
        _same(actual_setup.get(key), job_setup.get(key), f"native trial/job fingerprint differs: {key}")
    _same(actual_setup["subject"].get("model"), request["model"], "native fingerprint changed student model")
    _same(actual_setup["subject"].get("spec"), spec.name, "native fingerprint changed spec")
    _same(actual_setup.get("reference_profile"), spec.reference_profile, "native fingerprint changed the job's declared reference profile")
    _same(actual_setup["task"]["digest"], executed["sha256"], "native fingerprint changed exact package bytes")
    _same(actual_setup["task"]["declared_digest"], spec.task_package_digest, "native fingerprint changed its declared package pin")
    _same(actual_setup["environment"]["backend"], spec.environment, "native fingerprint changed environment")
    for key in ("timeout_seconds", "max_requests", "max_input_tokens", "max_output_tokens", "max_total_tokens", "cost_limit_usd"):
        _same(actual_setup["budgets"].get(key), getattr(spec, key), f"native fingerprint changed runtime bound: {key}")
    _same((raw.get("agent_info") or {}).get("version"), actual_setup["harness"]["version"], "native harness revision differs from the actual agent")
    _same(actual_setup["harness"]["additions"].get("extra_instruction"), spec.extra_instruction_path, "native fingerprint changed extra instruction")
    locked_setup = json.loads(lock["agent"]["env"]["EVALLAB_SETUP_FINGERPRINT"])
    excluded = {"source", "sources", "version_source", "step_limit_source", "observed_applied", "observed_source"}
    for section in ("environment", "harness", "server", "sampling", "budgets", "lock"):
        expected = {key: value for key, value in actual_setup[section].items() if key not in excluded}
        if section == "harness":
            expected["additions"] = {key: value for key, value in expected["additions"].items()
                                     if key not in {"rules", "skills", "extra_instruction", "extra_skills"}}
        _same(locked_setup.get(section), expected, f"lock-covered setup differs from actual native fingerprint: {section}")
    _same(actual_setup["lock"].get("effective"), "locked", "native execution did not retain effective egress locking")
    _same(actual_setup["lock"].get("observed_applied"), True, "native egress lock was not observed applied")

    metadata = ((raw.get("agent_result") or {}).get("metadata") or {})
    exception = raw.get("exception_info") or {}
    for key, value in {
        "native_exit_status": metadata.get("native_exit_status"), "native_stop_reason": metadata.get("stop_reason"),
        "native_exception_type": exception.get("exception_type"), "native_infra_error": bool(metadata.get("infra_error")),
        "native_finished_at": raw.get("finished_at"), "observed_model_requests": metadata.get("model_requests"),
        "original_reward": p.score(raw),
    }.items():
        _same(execution.get(key), value, f"native final result/stop observation changed: {key}")
    reward = p.score(raw) if p.score(raw) in (0.0, 1.0) else None
    budget_stop = exception.get("exception_type") == "TrialBudgetExhaustedError" or metadata.get("stop_reason") == "trial_budget_exhausted"
    context_stop = metadata.get("stop_reason") == "context_exhausted" or metadata.get("native_exit_status") == "ContextExhausted"
    clean = not exception and not metadata.get("infra_error") and metadata.get("native_exit_status") == "Idle" and bool(raw.get("finished_at"))
    if budget_stop:
        status = "native_model_budget_stop"
    elif context_stop:
        status = "native_context_budget_stop"
    elif execution.get("problems"):
        status = "native_evidence_incomplete"
    elif not clean:
        status = "native_infra_or_incomplete"
    elif execution["queue_state"] != "done":
        status = "native_settlement_failed"
    elif reward is None:
        status = "native_grade_missing_or_nonbinary"
    elif request["purpose"] == "evaluation":
        status = "completed_pending_separate_repaired_evaluation"
    else:
        status = "completed_repaired_pass" if reward == 1.0 else "completed_repaired_fail"
    _same(execution.get("status"), status, "native execution status disagrees with its actual final result")
    if request["purpose"] == "evaluation":
        _same(execution.get("repaired_reward"), None, "weak original evaluation reward cannot be a repaired grade")
        _same(execution.get("final_repaired_result"), None, "native original evaluation cannot supply a final repaired result")
    elif status in {"completed_repaired_pass", "completed_repaired_fail"}:
        _same(execution.get("repaired_reward"), reward, "native repaired reward differs from actual final result")
        final = execution["final_repaired_result"]
        for key, value in {"result": files["result"], "execution_id": execution_id, "job_id": config["job_id"],
                           "reward": reward, "fingerprint": files["fingerprint"], "lock": files["lock"],
                           "grader": execution["repaired_grader"]}.items():
            _same(final.get(key), value, f"native final repaired result binding changed: {key}")
    _same(execution["repaired_grader"]["package"], request["provenance"]["correction"].get("repaired_task_package_snapshot", executed),
          "native final repaired grader package changed")
    _same(execution["repaired_grader"]["variant"], request["source_partition"]["variant"], "native repaired grader admission changed")
    repaired_package = execution["repaired_grader"]["package"]
    _same(execution["repaired_grader"]["verifier"],
          verifier_identity(Path(repaired_package["path"])).model_dump(mode="json"),
          "native repaired verifier identity changed")
    original_alias = original["alias"]
    original_grader = {
        "source_execution_id": original["execution_id"], "source_trial": original["source_trial"],
        "package_digest": original_alias.get("executed_task_digest"),
        "task_checksum": original_alias.get("task_checksum"), "native_files": original_alias["files"],
        "identity_basis": "retained native original-grader evidence",
    }
    if request["purpose"] == "evaluation":
        original_grader.update(package=executed, verifier=verifier_identity(Path(executed["path"])).model_dump(mode="json"))
        if status == "completed_pending_separate_repaired_evaluation":
            _same(execution["original_grader_observation"],
                  {"result": files["result"], "reward": reward, "package": executed, "is_final_repaired_evaluation": False},
                  "native original-grader observation was relabeled as repaired evaluation")
    _same(execution["original_grader"], original_grader, "native original source grader binding changed")

    patch = files.get("patch")
    custody = None
    if patch:
        _same(execution.get("corrected_patch"), patch, "native legacy corrected patch binding changed")
        _same(native_files.get("corrected_patch"), patch, "native legacy patch file reference changed")
        _checked_ref(patch)
        _require(Path(patch["path"]).read_text().strip(), "native corrected legacy patch is empty")
    else:
        patch, custody = e.checked_retained_patch(current, Path(executed["path"]))
        _same(native_files.get("kept_patch"), patch, "native kept patch is not the untouched execution's actual retained patch")
        custody = {**custody, "native_execution": binding, "request": request["request"]}
    _same(_bound_execution(binding), execution, "native execution evidence changed while reading its artifacts")
    _same(p.source_record(trial), current, "native source bytes changed during completion admission")
    _same(task_directory_digest(Path(executed["path"])), executed["sha256"], "executed package changed during completion admission")
    source = {key: value for key, value in current.items() if key != "alias"}
    source["provenance"] = {"trial_uuid": execution_id, "job_uuid": alias["job_uuid"],
                            "source_result_sha256": files["result"]["sha256"], "aliases": [alias]}
    return {"execution": execution, "execution_binding": binding, "source": source, "patch": patch,
            "patch_custody": custody, "messages": messages, "tools": tools, "native_reward": reward,
            "executed_package": executed}
