"""Provider-free custody admission for the explicitly requested fresh population.

This is NOT Qualification.native_replay or the historical build.py population.
Fresh source artifacts stay agent.full.diff + agent.kept.diff, never agent.diff.
Actual repaired passes can become proof_kind=fresh_native_repaired only after
all native identity, ordered lock, source/control and lossless trace checks.
An absent/null/error endpoint never becomes a failing grade or a passing row.
The export retains original_v6_grade=not_requested_unmeasured, all native
conversations (including child tool/reasoning streams), exact exclusions and
hash-bound artifact references. It does not tokenize, train, relax minimum 51,
or replace the default B/C token-exposure gate with a B-only build.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

MINIMUM_FRESH = 51


def effective_agent_lock(result: dict[str, Any], document: dict[str, Any], g: Any) -> dict[str, Any]:
    """Use ordered effective applications, NOT the misleading top-level any-lock."""
    applications = document.get("applications")
    g.require(document.get("schema_version") == 2 and isinstance(applications, list) and applications, "ordered effective native lock applications are missing")
    phases = {key: result.get(key) for key in ("environment_setup", "agent_setup", "agent_execution", "verifier")}
    for name, interval in phases.items():
        g.require(isinstance(interval, dict), "native phase timing is absent: " + name)
        start, end = g.utc_instant(interval["started_at"]), g.utc_instant(interval["finished_at"])
        g.require(start <= end, "native phase interval is inverted: " + name)
    g.require(g.utc_instant(phases["environment_setup"]["finished_at"]) <= g.utc_instant(phases["agent_execution"]["started_at"]), "environment setup follows generation")
    g.require(g.utc_instant(phases["agent_setup"]["finished_at"]) <= g.utc_instant(phases["agent_execution"]["started_at"]), "agent setup follows generation")
    g.require(g.utc_instant(phases["agent_execution"]["finished_at"]) <= g.utc_instant(phases["verifier"]["started_at"]), "repaired verification precedes generation completion")
    g.require(g.utc_instant(phases["verifier"]["finished_at"]) <= g.utc_instant(result["finished_at"]), "trial finished before repaired verification")
    times = [g.utc_instant(row["recorded_at"]) for row in applications]
    g.same(times, sorted(times), "effective policy applications are not ordered")
    creations = [row for row in applications if row.get("source") == "creation"]
    g.same(len(creations), 2, "one-attempt native separate mode did not retain exactly two actual sandbox creations")
    g.require(all(row.get("sandbox_id") for row in creations), "actual sandbox IDs are missing")
    g.same(len({row["sandbox_id"] for row in creations}), 2, "agent/verifier sandbox identities are not independent")
    agent_id = creations[0]["sandbox_id"]
    start = g.utc_instant(phases["agent_execution"]["started_at"])
    end = g.utc_instant(phases["agent_execution"]["finished_at"])
    agent_apps = [row for row in applications if row["sandbox_id"] == agent_id]
    before = [row for row in agent_apps if g.utc_instant(row["recorded_at"]) <= start]
    g.require(before, "no effective native policy application predates the first model/tool phase")
    throughout = [before[-1], *[row for row in agent_apps if start < g.utc_instant(row["recorded_at"]) <= end]]
    for row in throughout:
        g.require(row.get("applied") is True and row.get("network_mode") in {"no-network", "allowlist"}, "an effective public/unapplied policy overlaps native generation")
        if row["network_mode"] == "allowlist":
            g.require(isinstance(row.get("allowed_hosts"), list), "actual allowlist hosts are not retained")
    return {"agent_sandbox_id": agent_id, "verifier_sandbox_id": creations[1]["sandbox_id"],
            "ordered_applications": applications, "phase_order": list(phases), "phase_intervals": phases,
            "agent_start_effective_application": before[-1], "generation_interval_locked": True,
            "setup_public_applications_preserved": [row for row in applications if row.get("network_mode") == "public"]}


def faithful_trace(row: dict[str, Any], g: Any) -> dict[str, Any]:
    from harbor.models.trajectories.trajectory import Trajectory

    from evallab.mimoagent_trajectory import native_to_atif

    trajectory = g.bound_json(row["trajectory"])
    native = g.bound_json(row["native_trajectory"])
    g.checked_ref(row["model_calls"])
    calls = [json.loads(line) for line in Path(row["model_calls"]["path"]).read_text().splitlines() if line.strip()]
    g.require(calls, "fresh native trajectory has no model-call evidence")
    calls = [{key: value for key, value in call.items() if key != "event"} for call in calls]
    # worker.save_traj retains the actual guard verdict list; its finished
    # event deliberately sends only that list's count to the ATIF publisher.
    # Keep the source file unchanged and normalize ONLY this known projection.
    info = dict(native.get("info") or {})
    if isinstance(info.get("antihack_blocks"), list):
        info["antihack_blocks"] = len(info["antihack_blocks"])
    expected = native_to_atif({**native, "info": info}, calls, trajectory_id=trajectory["trajectory_id"], model_name=g.BASE_MODEL)
    expected = Trajectory.model_validate(expected).model_dump(mode="json", exclude_none=True)
    # SDK serializer stats and the worker's finished-event model_stats have
    # different producers. Preserve both real artifacts rather than invent an
    # equality. Every step/tool/reasoning byte, definitions, IDs, child graph,
    # call-derived metrics and the remaining native metadata MUST still match.
    observed = {**trajectory, "extra": dict(trajectory.get("extra") or {})}
    expected.get("extra", {}).pop("native_model_stats", None)
    observed["extra"].pop("native_model_stats", None)
    g.same(expected, observed, "actual ATIF is not the complete native conversation/tool/reasoning projection")
    g.same((trajectory.get("agent") or {}).get("name"), "mimoagent", "fresh trajectory is not NativeMimoAgent")
    g.same((trajectory.get("agent") or {}).get("model_name"), g.BASE_MODEL, "actual trajectory changed the native BASE selector")
    g.same((native.get("info") or {}).get("exit_status"), "Idle", "interrupted/native budget/model/context stops cannot qualify as complete fresh rows")
    conversations = native.get("trajs")
    g.require(isinstance(conversations, dict) and "main" in conversations, "full native conversation registry is missing")
    count = 0
    for name, conversation in conversations.items():
        messages = conversation.get("messages")
        g.require(isinstance(messages, list) and messages, "empty or malformed native conversation: " + name)
        pending: set[str] = set()
        assistants = 0
        for message in messages:
            role = message.get("role")
            g.require(role in {"system", "user", "assistant", "tool"}, "unsupported native conversation role")
            if role == "assistant":
                assistants += 1
                g.require(message.get("content") or message.get("reasoning_content") or message.get("tool_calls"), "empty assistant target")
                for call in message.get("tool_calls") or []:
                    g.require(isinstance(call.get("id"), str) and call["id"] not in pending, "duplicate/missing native tool-call ID")
                    g.require(isinstance(call.get("function", {}).get("arguments"), str), "raw native tool argument bytes are missing")
                    pending.add(call["id"])
            elif role == "tool":
                g.require(message.get("tool_call_id") in pending, "native tool observation has no unique preceding call")
                pending.remove(message["tool_call_id"])
        g.require(not pending and assistants > 0, "native conversation has unresolved tools or no assistant target")
        count += assistants
    qa = g.load_module("phase2_fresh_faithful_converter", g.ROOT / "research/experiments/qa-trace/training-arms/build_arms.py")
    _, _, fidelity = qa.to_messages(trajectory)
    g.same(fidelity, "raw_completion", "parsed-action-only traces cannot qualify")
    g.require(not qa.network_fetch(trajectory), "observed network retrieval cannot qualify a fresh clean source")
    return {"conversations": conversations, "assistant_turns": count, "model_call_records": calls,
            "fidelity": "complete_native_messages_tools_reasoning", "trace_sha256": g.helpers.digest(g.helpers.canonical(conversations)),
            "auxiliary_stats_binding": "actual_raw_and_atif_retained_not_equalized",
            "projection_normalizations": {"antihack_blocks": "actual_worker_verdict_list_to_finished_event_count"},
            "projection_code": g.ref(g.ROOT / "src/evallab/mimoagent_trajectory.py")}


def qualify(plan: dict[str, Any], row: dict[str, Any], g: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    _, _, schemas = g.dependencies()
    cells = {cell["cell_id"]: cell for cell in plan["cells"]}
    g.require(row.get("cell_id") in cells, "observation is not an exact planned fresh cell")
    cell = cells[row["cell_id"]]
    for key in ("spec_id", "job_name", "task_id", "task", "task_index", "attempt_index"):
        g.same(row.get(key), cell[key], "observation/planned native identity mismatch: " + key)
    task = plan["tasks"][cell["task_index"]]
    g.same(row.get("package"), task, "observation changed the exact repaired package/ancestry")
    g.same(row.get("controls"), plan["controls"], "observation changed approved controls")
    g.require(row.get("dispatch_error") is None and row.get("reservation") is not None, "missing/failed admitted native dispatch")
    g.same(row.get("queue_state"), "done", "native public queue did not settle done")
    g.same(row.get("status"), "observed_repaired_pass", "no actual clean repaired reward-one endpoint")
    g.require(not row.get("problems"), "native observation retains evidence problems")
    result = g.bound_json(row["result"])
    g.same(str(UUID(result["id"])), row["execution_id"], "native execution UUID mismatch")
    g.same((result.get("config") or {}).get("job_id"), row["native_job_id"], "native job UUID mismatch")
    UUID(row["native_job_id"])
    g.same(result.get("exception_info"), None, "native result has a real exception")
    g.same(g.score(result), 1.0, "actual repaired native grade is not one")
    g.same(result.get("verifier_environment_mode"), "separate", "fresh source exposed its repaired tests to the agent")
    g.require(result.get("finished_at"), "native trial has no completion time")
    config = result["config"]
    agent = config["agent"]
    g.same(agent.get("model_name"), g.BASE_MODEL, "actual native request selected another model")
    g.require(agent.get("import_path") == "evallab.harbor_mimoagent:NativeMimoAgent", "actual source did not use the native MiMo adapter")
    g.require(config.get("source_trial") is None and not agent.get("resume_trajectory") and agent.get("load_trajectory") is None, "historical correction/replay/resume is not fresh native generation")
    g.require(not config.get("extra_instruction_paths") and not config.get("extra_instructions"), "fresh trial received an extra correction/history prompt")
    status = (result.get("agent_result") or {}).get("metadata") or {}
    g.same(status.get("native_exit_status"), "Idle", "native budget/infra/context stops cannot qualify")
    g.require(not any(status.get(key) for key in ("infra_error", "teacher", "copied_fix")), "native source has infra/teacher/copied-fix counterevidence")
    lock = g.bound_json(row["lock"])
    g.same(lock["task"]["digest"], task["harbor_digest"], "native content lock is not the exact repaired Harbor package")
    environment = config["environment"]
    kwargs = environment.get("kwargs") or {}
    for resource in ("cpu", "memory"):
        g.same(kwargs.get(resource + "_enforcement_policy", environment.get(resource + "_enforcement_policy")), "limit", "native hard resource limit was not requested")
    g.same(kwargs.get("sandbox_create_max_attempts"), 1, "actual native sandbox creation was not one-shot")
    g.same(kwargs.get("sandbox_timeout_secs"), plan["config"]["sandbox_timeout_seconds"], "actual native provider TTL differs from its exposure reserve")
    g.same(kwargs.get("app_name"), plan["config"]["modal_app_name"], "actual native app ownership changed")
    metadata = g.bound_json(row["metadata"])
    staging = metadata.get("task_staging") or {}
    for key, expected in {"source_package_digest": task["package_digest"], "source_harbor_digest": task["harbor_digest"],
                          "staged_harbor_digest": task["harbor_digest"], "declared_task_name": task["task"]}.items():
        g.same(staging.get(key), expected, "native package staging custody changed: " + key)
    actual_spec_ref = row["artifacts"].get("experiment-spec.json")
    g.require(actual_spec_ref, "actual job ExperimentSpec is missing")
    actual_spec = schemas.ExperimentSpec.model_validate(g.bound_json(actual_spec_ref))
    planned_spec = schemas.ExperimentSpec.model_validate(g.bound_json(cell["spec"]))
    g.same(actual_spec.model_dump(mode="json"), planned_spec.model_dump(mode="json"), "actual job input differs from planned fresh ExperimentSpec")
    g.same((actual_spec.attempts, actual_spec.concurrency, actual_spec.diff_sources), (1, 1, []), "fresh cell silently expanded or replayed a stale diff")
    identity = metadata.get("model_identity") or {}
    g.same(identity.get("requested"), g.BASE_MODEL, "native model identity requested another route")
    g.require(identity.get("matched") is True and identity.get("mismatch") is False, "actual returned model identity did not match")
    calls = (metadata.get("provider_usage") or {}).get("calls")
    g.require(isinstance(calls, list) and calls, "observed provider model bindings are absent (declared model is insufficient)")
    for call in calls:
        g.same(call.get("status"), 200, "a native provider call has a non-success/null status")
        g.same(call.get("returned_model"), g.RETURNED_MODEL, "native provider returned a non-base/null model")
    g.same(len(calls), status.get("model_requests"), "native/provider call accounting is incomplete")
    fingerprint = g.bound_json(row["fingerprint"])
    g.same(fingerprint["task"]["digest"], task["package_digest"], "observed setup fingerprint changed the repaired package")
    g.same(fingerprint["reference_profile"], plan["config"]["reference_profile"], "actual source used another canonical profile")
    lock_audit = effective_agent_lock(result, g.bound_json(row["effective_locks"]), g)
    full = Path(row["full_diff"]["path"])
    kept = Path(row["kept_diff"]["path"])
    for value in (row["full_diff"], row["kept_diff"], row["workspace_artifacts"], row["reward_file"]):
        g.checked_ref(value)
    workspace = [value for name, value in row["artifacts"].items() if Path(name).name == "workspace.tgz"]
    g.require(workspace, "actual separate-verifier workspace snapshot was not retained")
    for value in workspace:
        g.checked_ref(value)
    g.same(full.name, "agent.full.diff", "fresh source full diff was renamed to satisfy a historical replay contract")
    g.same(kept.name, "agent.kept.diff", "fresh kept diff was renamed")
    g.require(full.read_bytes().startswith(b"diff --git ") and kept.read_bytes().startswith(b"diff --git "), "fresh pass has no real full/kept implementation diff")
    g.same(Path(row["reward_file"]["path"]).read_text().strip(), "1", "actual repaired reward artifact disagrees with native result")
    grader = Path(task["grader_source"]["path"]).read_text()
    capture_full, capture_kept = grader.index('> "$V/agent.full.diff"'), grader.index('> "$V/agent.kept.diff"')
    final_hidden = grader.index("# 4. Hidden tests", capture_kept)
    g.require(capture_full < capture_kept < final_hidden, "source does not prove both native diffs precede final hidden-patch application")
    trace = faithful_trace(row, g)
    excluded = set(plan["excluded_tasks"]) | {plan["answer_visible_exclusion"]}
    g.require(task["task"] not in excluded and task["task_id"] in g.TASK_IDS and task["task_id"] != "002486", "heldout/answer-visible/held parent entered the fresh source population")
    for key in ("source_record", "record", "task_toml", "grader_source"):
        g.checked_ref(task[key])
    source = {"schema": "repaired-data-sft-v2.fresh-native-source/v1", "proof_kind": "fresh_native_repaired",
              "cell_id": cell["cell_id"], "task_index": cell["task_index"], "attempt_index": cell["attempt_index"], "task": task["task"],
              "execution_id": row["execution_id"], "trial_id": row["execution_id"], "job_id": row["native_job_id"],
              "result": row["result"], "trajectory": row["trajectory"], "native_trajectory": row["native_trajectory"], "metadata": row["metadata"],
              "full_diff": row["full_diff"], "kept_diff": row["kept_diff"], "artifact_manifest": row["workspace_artifacts"],
              "all_native_artifacts": row["artifacts"], "ancestry": [task["source_record"]], "package": task,
              "controls": plan["controls"], "control_admission": plan["control_admission"],
              "native_route_selector": g.BASE_MODEL, "actual_returned_model": g.RETURNED_MODEL,
              "observed_provider_calls": calls, "model_call_records": row["model_calls"], "effective_locks": row["effective_locks"],
              "lock_audit": lock_audit, "repaired_grade": {"reward": 1.0, "result": row["result"], "error": None, "grader_package_digest": task["package_digest"]},
              "original_v6_grade": "not_requested_unmeasured", "trace_sha256": trace["trace_sha256"], "trace_fidelity": trace["fidelity"],
              "training_admitted": False, "tokenization": "not_requested_unmeasured", "token_matched_C": "required_before_training_not_built"}
    conversation = {"schema": "repaired-data-sft-v2.fresh-native-conversations/v1", "execution_id": row["execution_id"], "task": task["task"],
                    "cell_id": cell["cell_id"], "proof_kind": "fresh_native_repaired", **trace,
                    "native_trajectory": row["native_trajectory"], "trajectory": row["trajectory"], "tools_and_reasoning_unchanged": True,
                    "training_admitted": False, "original_v6_grade": "not_requested_unmeasured"}
    return source, conversation


def collect(plan_path: Path, out: Path, g: Any) -> dict[str, Any]:
    plan = g.checked_plan(plan_path)
    executions_path = plan_path.parent / "executions.jsonl"
    g.require(executions_path.is_file(), "actual fresh native executions are required; historical controls are not fresh rows")
    g.require(not out.exists(), "fresh admission output must be NEW; no history mutation")
    observations = [json.loads(line) for line in executions_path.read_text().splitlines() if line.strip()]
    by_cell: dict[str, list[dict[str, Any]]] = {}
    for row in observations:
        by_cell.setdefault(row.get("cell_id"), []).append(row)
    g.require(set(by_cell) <= {cell["cell_id"] for cell in plan["cells"]}, "execution file contains an unplanned source cell")
    out.mkdir(parents=True)
    sources, exclusions, conversations = [], [], []
    seen_ids: set[str] = set()
    seen_traces: set[str] = set()
    for cell in plan["cells"]:
        rows = by_cell.get(cell["cell_id"], [])
        try:
            g.same(len(rows), 1, "fresh cell must have one truthful terminal/null observation, not aliases or retries")
            source, conversation = qualify(plan, rows[0], g)
            g.require(source["execution_id"] not in seen_ids, "duplicate actual native execution UUID")
            g.require(source["trace_sha256"] not in seen_traces, "duplicate complete conversation bytes cannot increase fresh qualifier count")
            seen_ids.add(source["execution_id"])
            seen_traces.add(source["trace_sha256"])
            sources.append(source)
            conversations.append(conversation)
        except Exception as exc:
            exclusions.append({"cell_id": cell["cell_id"], "task": cell["task"], "task_index": cell["task_index"], "attempt_index": cell["attempt_index"],
                               "execution_id": rows[0].get("execution_id") if len(rows) == 1 else None,
                               "native_status": rows[0].get("status") if len(rows) == 1 else None,
                               "repaired_reward": rows[0].get("repaired_reward") if len(rows) == 1 else None,
                               "excluded": g.failure(exc), "original_v6_grade": "not_requested_unmeasured"})
    # Empty exports are truthful measured outcomes, never a fabricated successful
    # B build or a promise of >=51. Every requested cell appears once in census.
    for filename, rows in (("fresh-repaired-sources.jsonl", sources), ("faithful-conversations.jsonl", conversations), ("exclusions.jsonl", exclusions)):
        with (out / filename).open("xb"):
            pass
        for row in rows:
            g.append_json(out / filename, row)
    receipt = {"schema": "repaired-data-sft-v2.fresh-native-admission/v1", "at": datetime.now(UTC).isoformat(), "proof_kind": "fresh_native_repaired",
               "plan": g.ref(plan_path), "executions": g.ref(executions_path), "controls": plan["controls"],
               "control_admission": plan["control_admission"], "planned_cells": g.CELLS,
               "qualifying_fresh_rows": len(sources), "excluded_cells": len(exclusions), "qualifying_tasks": len({row["task"] for row in sources}),
               "source_export": g.ref(out / "fresh-repaired-sources.jsonl"), "faithful_conversation_export": g.ref(out / "faithful-conversations.jsonl"),
               "exclusions": g.ref(out / "exclusions.jsonl"), "observed_status_counts": dict(Counter(row.get("status") for row in observations)),
               "minimum_fresh_rows": MINIMUM_FRESH, "minimum_met": len(sources) >= MINIMUM_FRESH,
               "original_v6_grade": "not_requested_unmeasured", "historical_B_v2_contract_changed": False,
               "token_matched_C": "required_before_training_not_built", "all_in_training_cost_admission": "not_requested_unmeasured", "training_admitted": False,
               "provider_calls": 0, "model_calls": 0, "native_executions_by_collector": 0}
    g.same(len(sources) + len(exclusions), g.CELLS, "fresh admission lost planned cells")
    g.write_json(out / "admission.json", receipt)
    return receipt
