"""Read-only trace query interface over evaluated Harbor trials (HAR-131).

Provides in-memory, transient DuckDB querying across all recorded cohorts
(HAR-81, HAR-104, HAR-110, HAR-116, etc.) using existing Parquet selectors,
canonical ATIF projections, processed run reports, and frozen rater labels.

Public callable:
    connect_trace_query(*, repo_root: Path, results_home: Path | None = None,
                        derived_root: Path | None = None,
                        job_dirs: Sequence[Path] | None = None
    ) -> tuple[duckdb.DuckDBPyConnection, dict[str, Any]]

Exposes transient views:
    - v_trace_trials: Exactly one row per native job_id/trial_id.
    - v_trace_steps: Step grain with joined trial fields and explicit markers.
    - 10 canonical analytical views defined in sql/trace_queries.sql.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from evallab.results_home import (
    _agent_model,
    _published_jobs,
    card_from,
    is_agent_run,
    results_root,
)
from evallab.storage.paths import derived_root_from_environment

# --------------------------------------------------------------------------- #
# Arrow Schemas for in-memory registration
# --------------------------------------------------------------------------- #

TRIALS_ARROW_SCHEMA = pa.schema(
    [
        pa.field("job_id", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("job_name", pa.string(), nullable=False),
        pa.field("trial_name", pa.string(), nullable=False),
        pa.field("task_name", pa.string(), nullable=False),
        pa.field("card", pa.string(), nullable=True),
        pa.field("model_name", pa.string(), nullable=True),
        pa.field("arm", pa.string(), nullable=True),
        pa.field("split", pa.string(), nullable=True),
        pa.field("task_package_digest", pa.string(), nullable=True),
        pa.field("raw_reward", pa.float64(), nullable=True),
        pa.field("scored", pa.bool_(), nullable=True),
        pa.field("counts_verdict", pa.string(), nullable=True),
        pa.field("counts_reasons_json", pa.string(), nullable=False),
        pa.field("counts_evidence_json", pa.string(), nullable=False),
        pa.field("stop_reason", pa.string(), nullable=True),
        pa.field("input_tokens", pa.int64(), nullable=True),
        pa.field("output_tokens", pa.int64(), nullable=True),
        pa.field("agent_steps", pa.int64(), nullable=True),
        pa.field("decision_schema", pa.string(), nullable=True),
        pa.field("decision_facts_json", pa.string(), nullable=True),
        pa.field("decision_judgments_json", pa.string(), nullable=True),
        pa.field("token_flow_json", pa.string(), nullable=True),
        pa.field("taint_json", pa.string(), nullable=True),
        pa.field("diagnosis_json", pa.string(), nullable=True),
        pa.field("outline_json", pa.string(), nullable=True),
        pa.field("loop_kind", pa.string(), nullable=True),
        pa.field("first_failure_ref", pa.string(), nullable=True),
        pa.field("outcome_rule", pa.string(), nullable=True),
        pa.field("outcome_attribution", pa.string(), nullable=True),
        pa.field("last_edit_step", pa.int64(), nullable=True),
        pa.field("first_edit_step", pa.int64(), nullable=True),
        pa.field("tokens_after_last_edit_input", pa.int64(), nullable=True),
        pa.field("tokens_after_last_edit_output", pa.int64(), nullable=True),
        pa.field("source_job_dir", pa.string(), nullable=False),
        pa.field("source_trial_dir", pa.string(), nullable=False),
        pa.field("published_job_dir", pa.string(), nullable=True),
        pa.field("report_path", pa.string(), nullable=True),
        pa.field("trajectory_available", pa.bool_(), nullable=False),
        pa.field("processed_available", pa.bool_(), nullable=False),
        pa.field("counts_available", pa.bool_(), nullable=False),
        pa.field("step_evidence_source", pa.string(), nullable=False),
        pa.field("labels_json", pa.string(), nullable=False),
    ]
)

STEPS_ARROW_SCHEMA = pa.schema(
    [
        pa.field("job_id", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("document_id", pa.string(), nullable=False),
        pa.field("step_id", pa.int64(), nullable=False),
        pa.field("source_path", pa.string(), nullable=False),
        pa.field("source_sha256", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("timestamp", pa.string(), nullable=True),
        pa.field("is_copied_context", pa.bool_(), nullable=False),
        pa.field("prompt_tokens", pa.int64(), nullable=True),
        pa.field("completion_tokens", pa.int64(), nullable=True),
        pa.field("tool_call_count", pa.int64(), nullable=False),
        pa.field("command_text", pa.string(), nullable=True),
        pa.field("command_provenance", pa.string(), nullable=True),
        pa.field("observation_excerpt", pa.string(), nullable=True),
        pa.field("step_ref", pa.string(), nullable=False),
    ]
)


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Manifest and Label Loaders
# --------------------------------------------------------------------------- #


def _load_experiment_manifests(repo_root: Path) -> dict[str, dict[str, Any]]:
    """Load authoritative experiment manifests mapping job/trial to arm & split."""
    manifests: dict[str, dict[str, Any]] = {}

    # 1. HAR-110 Python GEPA split-v2
    v2_path = (
        repo_root
        / "research"
        / "experiments"
        / "har110-python-gepa"
        / "results-v2-trials.jsonl"
    )
    if v2_path.is_file():
        try:
            for line in v2_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                job_name = row.get("job")
                if job_name:
                    manifests[str(job_name)] = {
                        "arm": row.get("arm"),
                        "split": row.get("split"),
                        "task": row.get("task"),
                    }
        except Exception:
            pass

    # 2. HAR-116 loopfix-leak
    har116_path = (
        repo_root
        / "research"
        / "experiments"
        / "har116-loopfix-leak"
        / "results.jsonl"
    )
    if har116_path.is_file():
        try:
            for line in har116_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                job_name = row.get("job")
                if job_name and str(job_name) not in manifests:
                    manifests[str(job_name)] = {
                        "arm": row.get("arm"),
                        "split": "heldout" if row.get("part") == "A" else "leak_study",
                        "task": row.get("task"),
                    }
        except Exception:
            pass

    # 3. HAR-114 tokenflow
    har114_path = (
        repo_root
        / "research"
        / "experiments"
        / "har114-tokenflow"
        / "runs.jsonl"
    )
    if har114_path.is_file():
        try:
            for line in har114_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                job_name = row.get("job")
                if job_name and str(job_name) not in manifests:
                    manifests[str(job_name)] = {
                        "arm": row.get("arm"),
                        "split": None,
                        "task": row.get("task"),
                    }
        except Exception:
            pass

    return manifests


def _load_frozen_labels(
    repo_root: Path, derived_root: Path | None
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Load frozen rater and hand labels. Never fill unobserved with guesses."""
    labels_by_trial: dict[str, list[dict[str, Any]]] = {}
    manifest_hashes: dict[str, str] = {}

    # 1. HAR-119 Part 2 rater labels
    har119_dir = repo_root / "research" / "explorations" / "trace-lab" / "har119"
    har119_manifest = har119_dir / "labels" / "MANIFEST.sha256"
    if har119_manifest.is_file():
        manifest_hashes["har119"] = _file_sha256(har119_manifest)
        for rater in ("rater_a", "rater_b"):
            rater_dir = har119_dir / "labels" / rater
            if not rater_dir.is_dir():
                continue
            for label_file in sorted(rater_dir.glob("*.json")):
                trial_name = label_file.stem
                data = _safe_read_json(label_file)
                if not data:
                    continue
                entry = {
                    "cohort": "har119",
                    "rater": rater,
                    "trial_name": trial_name,
                    "blame": data.get("blame"),
                    "blame_confidence": data.get("blame_confidence"),
                    "first_failure": data.get("first_failure"),
                    "loop_kind": data.get("loop_kind"),
                    "stop_reason": data.get("stop_reason"),
                    "pass_copied": data.get("pass_copied"),
                    "provenance": "agent_rater",
                    "source_file": str(label_file.relative_to(repo_root)),
                    "source_sha256": _file_sha256(label_file),
                }
                labels_by_trial.setdefault(trial_name, []).append(entry)

    # 2. HAR-109 hand labels
    har109_dir = repo_root / "research" / "explorations" / "trace-lab" / "har109"
    har109_manifest = har109_dir / "hand_labels.sha256"
    errata_file = har109_dir / "hand_errata.json"
    errata_by_trial: dict[str, dict[str, Any]] = {}
    if errata_file.is_file():
        try:
            err_list = json.loads(errata_file.read_text(encoding="utf-8"))
            if isinstance(err_list, list):
                for item in err_list:
                    if isinstance(item, dict) and "trial_id" in item:
                        errata_by_trial[str(item["trial_id"])] = item
        except Exception:
            pass

    if har109_manifest.is_file():
        manifest_hashes["har109"] = _file_sha256(har109_manifest)
        hand_dir = har109_dir / "hand"
        if hand_dir.is_dir():
            for label_file in sorted(hand_dir.glob("*.json")):
                trial_name = label_file.stem
                data = _safe_read_json(label_file)
                if not data:
                    continue
                erratum = errata_by_trial.get(trial_name)
                completion_confirmed = data.get("completion_confirmed")
                if erratum and erratum.get("field") == "completion_confirmed":
                    completion_confirmed = erratum.get("corrected_value", completion_confirmed)
                entry = {
                    "cohort": "har109",
                    "rater": "hand",
                    "trial_name": trial_name,
                    "attribution": data.get("attribution"),
                    "completion_confirmed": completion_confirmed,
                    "first_failure_step": data.get("first_failure_step"),
                    "first_failure_what": data.get("first_failure_what"),
                    "loop": data.get("loop"),
                    "stop_reason": data.get("stop_reason"),
                    "task_verdict": data.get("task_verdict"),
                    "upstream_fetch": data.get("upstream_fetch"),
                    "provenance": "human_hand",
                    "has_erratum": bool(erratum),
                    "source_file": str(label_file.relative_to(repo_root)),
                    "source_sha256": _file_sha256(label_file),
                }
                labels_by_trial.setdefault(trial_name, []).append(entry)

    # 3. Parquet behavior_labels if present
    if derived_root:
        parquet_path = derived_root / "behavior_labels" / "behavior_labels.parquet"
        if parquet_path.is_file():
            try:
                import pyarrow.parquet as pq

                table = pq.read_table(
                    parquet_path,
                    columns=["trial_name", "label", "taxonomy", "provenance", "author"],
                )
                for row in table.to_pylist():
                    tname = row.get("trial_name")
                    if tname:
                        labels_by_trial.setdefault(str(tname), []).append(
                            {
                                "cohort": "behavior_labels_parquet",
                                "rater": row.get("author") or "heuristic",
                                "trial_name": tname,
                                "label": row.get("label"),
                                "taxonomy": row.get("taxonomy"),
                                "provenance": row.get("provenance") or "derived_parquet",
                            }
                        )
            except Exception:
                pass

    return labels_by_trial, manifest_hashes


# --------------------------------------------------------------------------- #
# Extraction Helpers
# --------------------------------------------------------------------------- #


def _extract_commands_from_step(step: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract (command_text, command_provenance) for one step."""
    native_calls = step.get("tool_calls")
    if isinstance(native_calls, list) and native_calls:
        cmds: list[str] = []
        for call in native_calls:
            if not isinstance(call, dict):
                continue
            fname = call.get("function_name")
            args = call.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if fname in ("bash_command", "bash", "execute_command"):
                cmd = args.get("keystrokes") or args.get("command")
                if cmd:
                    cmds.append(str(cmd).strip())
            elif fname == "mark_task_complete":
                cmds.append("mark_task_complete")
            elif fname:
                cmds.append(str(fname))
        if cmds:
            return "; ".join(cmds), "recorded"

    # Check synthesized layers in extra
    extra = step.get("extra")
    if isinstance(extra, dict) and "step_layers" in extra:
        layers = extra.get("step_layers") or {}
        accepted = layers.get("accepted") or {}
        prov = layers.get("provenance") or "reconstructed"
        calls = accepted.get("calls") or []
        cmds = []
        for call in calls:
            if isinstance(call, dict):
                if call.get("task_complete"):
                    cmds.append("mark_task_complete")
                elif "keystrokes" in call:
                    cmds.append(str(call["keystrokes"]).strip())
        if cmds:
            return "; ".join(cmds), prov

    return None, None


def _extract_observation_excerpt(step: dict[str, Any]) -> str | None:
    obs = step.get("observation")
    if not isinstance(obs, dict):
        return None
    results = obs.get("results")
    if isinstance(results, list) and results:
        contents = []
        for res in results:
            if isinstance(res, dict) and "content" in res:
                c = res["content"]
                if isinstance(c, str):
                    contents.append(c)
                elif isinstance(c, bytes):
                    contents.append(c.decode("utf-8", errors="replace"))
        if contents:
            full = "\n".join(contents).strip()
            return full[:300] if len(full) > 300 else full
    return None


def _resolve_arm_and_split(
    job_name: str,
    manifest_info: dict[str, Any] | None,
    spec_data: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    """Resolve arm and split from recorded manifests, never guessing."""
    if manifest_info:
        arm = manifest_info.get("arm")
        split = manifest_info.get("split")
        if arm is not None:
            return str(arm), str(split) if split else None

    if spec_data:
        # Check hypothesis e.g. "arm=original" or "arm=loopfix"
        hypothesis = spec_data.get("hypothesis")
        if isinstance(hypothesis, str):
            for part in hypothesis.split(","):
                part = part.strip()
                if part.startswith("arm="):
                    arm_val = part.split("=")[1].strip()
                    arm_token = arm_val.split(":")[0].split(" ")[0].strip()
                    if arm_token:
                        return arm_token, None
        arm_val = spec_data.get("arm")
        if arm_val:
            return str(arm_val), None

    return None, None


# --------------------------------------------------------------------------- #
# Core Builder
# --------------------------------------------------------------------------- #


def _build_trial_row(
    job_dir: Path,
    trial_dir: Path,
    published_dir: Path | None,
    job_data: dict[str, Any],
    provenance_data: dict[str, Any],
    spec_data: dict[str, Any] | None,
    manifest_info: dict[str, Any] | None,
    labels: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build one v_trace_trials row and its associated v_trace_steps rows."""
    trial_name = trial_dir.name
    job_name = job_dir.name
    card = (
        provenance_data.get("card")
        or card_from(job_name)
        or card_from(job_dir.as_posix())
    )

    # 1. Harbor trial result.json
    trial_result = _safe_read_json(trial_dir / "result.json") or {}
    job_id = str(job_data.get("id") or trial_result.get("job_id") or job_name)
    trial_id = str(trial_result.get("id") or trial_name)
    task_name = str(trial_result.get("task_name") or "unknown")

    # Model resolution
    agent_info = trial_result.get("agent_info") or {}
    model_info = agent_info.get("model_info") or {}
    model_name = (
        model_info.get("name")
        or provenance_data.get("model")
        or (spec_data or {}).get("model")
    )
    if model_name:
        model_name = str(model_name)

    # Arm and split
    arm, split = _resolve_arm_and_split(job_name, manifest_info, spec_data)

    # Task package digest
    task_package_digest = None
    if spec_data:
        task_package_digest = spec_data.get("task_package_digest")
    if not task_package_digest and provenance_data.get("tasks"):
        first_t = provenance_data["tasks"][0]
        if isinstance(first_t, dict):
            task_package_digest = first_t.get("package_digest")
    if not task_package_digest:
        task_package_digest = trial_result.get("task_checksum")
    if task_package_digest:
        task_package_digest = str(task_package_digest)

    # Raw reward & scored
    verifier_result = trial_result.get("verifier_result") or {}
    raw_rewards = verifier_result.get("rewards") or {}
    reward_val = raw_rewards.get("reward")
    raw_reward = (
        float(reward_val)
        if isinstance(reward_val, (int, float)) and not isinstance(reward_val, bool)
        else None
    )
    scored = bool(verifier_result and raw_reward is not None)

    # Stop reason fallback
    exception_info = trial_result.get("exception_info") or {}
    stop_reason = (
        exception_info.get("exception_type")
        or exception_info.get("type")
        or ("completed" if raw_reward is not None else "unknown")
    )

    # Token initialization:
    # Per HAR-131 contract and Research-Harbor rule:
    # input_tokens and output_tokens represent true proxy-settled totals.
    # Missing/invalid ledger or multi-trial means numeric None (never native fallback).
    input_tokens: int | None = None
    output_tokens: int | None = None
    # 2. Processed run report
    # Look in published dir first, then trial dir / job dir
    processed_file = None
    search_dirs = [d for d in [published_dir, job_dir] if d is not None]
    for base in search_dirs:
        cand = base / "processed" / f"trial-{trial_name}.json"
        if cand.is_file():
            processed_file = cand
            break

    processed_data = _safe_read_json(processed_file) if processed_file else None
    processed_available = processed_data is not None
    report_path = str(processed_file) if processed_file else None

    counts_verdict = None
    counts_reasons_json = "[]"
    counts_evidence_json = "[]"
    counts_available = False
    agent_steps = None
    decision_schema = None
    decision_facts_json = None
    decision_judgments_json = None
    token_flow_json = None
    taint_json = None
    diagnosis_json = None
    outline_json = None
    loop_kind = None
    first_failure_ref = None
    outcome_rule = None
    outcome_attribution = None
    last_edit_step = None
    first_edit_step = None
    tokens_after_last_edit_input = None
    tokens_after_last_edit_output = None

    if processed_data:
        if processed_data.get("reward") is not None and raw_reward is None:
            raw_reward = float(processed_data["reward"])
            scored = bool(processed_data.get("scored", True))
        if processed_data.get("stop_reason"):
            stop_reason = str(processed_data["stop_reason"])
        if isinstance(processed_data.get("agent_steps"), int):
            agent_steps = processed_data["agent_steps"]

        # Tokens resolution per Research-Harbor priority:
        # Stable tokens_proxy fields: source='proxy_settled_ledger', attribution='single_trial'.
        # Missing/invalid ledger or multi-trial means numeric None; never native fallback/equal division.
        t_proxy = processed_data.get("tokens_proxy")
        if isinstance(t_proxy, dict) and t_proxy.get("source") == "proxy_settled_ledger":
            if t_proxy.get("attribution") == "single_trial":
                p_in = t_proxy.get("input_tokens")
                p_out = t_proxy.get("output_tokens")
                input_tokens = int(p_in) if isinstance(p_in, int) else None
                output_tokens = int(p_out) if isinstance(p_out, int) else None
        else:
            # Check single-trial job settled ledger
            n_tot = job_data.get("n_total_trials")
            is_single = int(n_tot) == 1 if isinstance(n_tot, int) else False
            if not is_single:
                stats = job_data.get("stats") or {}
                is_single = int(stats.get("n_completed_trials") or 0) == 1
            if is_single:
                # Read from processed job.json or lab-metadata.json
                job_proc_file = None
                for base in search_dirs:
                    cand = base / "processed" / "job.json"
                    if cand.is_file():
                        job_proc_file = cand
                        break
                job_proc = _safe_read_json(job_proc_file) if job_proc_file else {}
                ledger_used = (job_proc.get("ledger") or {}).get("totals", {}).get("used") or {}
                if not ledger_used:
                    lab_meta_file = None
                    for base in search_dirs:
                        cand = base / "lab-metadata.json"
                        if cand.is_file():
                            lab_meta_file = cand
                            break
                    lab_meta = _safe_read_json(lab_meta_file) if lab_meta_file else {}
                    ledger_used = (lab_meta.get("provider_usage") or {}).get("totals") or {}
                p_in = ledger_used.get("input_tokens")
                p_out = ledger_used.get("output_tokens")
                input_tokens = int(p_in) if isinstance(p_in, int) else None
                output_tokens = int(p_out) if isinstance(p_out, int) else None
        # Counts
        counts = processed_data.get("counts")
        if isinstance(counts, dict) and counts.get("verdict"):
            counts_verdict = str(counts["verdict"])
            counts_available = True
            reasons = counts.get("reasons") or []
            counts_reasons_json = json.dumps(reasons)
            evidence = counts.get("evidence") or []
            counts_evidence_json = json.dumps(evidence)

        # Decision
        decision = processed_data.get("decision")
        if isinstance(decision, dict):
            decision_schema = decision.get("schema")
            facts = decision.get("facts")
            if facts:
                decision_facts_json = json.dumps(facts)
            judgments = decision.get("judgments")
            if judgments:
                decision_judgments_json = json.dumps(judgments)
                loop_obj = judgments.get("loop_kind")
                if isinstance(loop_obj, dict):
                    loop_kind = loop_obj.get("kind")
                ff = judgments.get("first_failure")
                if isinstance(ff, dict):
                    first_failure_ref = ff.get("step")

            outcome = decision.get("did") or {}
            outcome_rule = decision.get("rule_id")
            outcome_attribution = decision.get("attribution")
            if not loop_kind and decision.get("loop_kind"):
                lk = decision["loop_kind"]
                loop_kind = lk.get("kind") if isinstance(lk, dict) else str(lk)

        # First failure & outcome failure direct
        if not first_failure_ref:
            ff_direct = processed_data.get("first_failure")
            if isinstance(ff_direct, dict):
                first_failure_ref = ff_direct.get("step_ref") or ff_direct.get("ref")

        if not outcome_rule:
            of_direct = processed_data.get("outcome_failure")
            if isinstance(of_direct, dict):
                outcome_rule = of_direct.get("rule_id")
                outcome_attribution = of_direct.get("attribution")

        # Token flow
        tf = processed_data.get("token_flow")
        if isinstance(tf, dict):
            token_flow_json = json.dumps(tf)
            last_edit = tf.get("last_useful_edit")
            if isinstance(last_edit, dict) and isinstance(last_edit.get("step_id"), int):
                last_edit_step = last_edit["step_id"]
            after_edit = tf.get("tokens_after_last_edit")
            if isinstance(after_edit, dict):
                tokens_after_last_edit_input = after_edit.get("input_tokens")
                tokens_after_last_edit_output = after_edit.get("output_tokens")

        # Taint & Diagnosis & Outline
        if processed_data.get("taint"):
            taint_json = json.dumps(processed_data["taint"])
        if processed_data.get("diagnosis"):
            diagnosis_json = json.dumps(processed_data["diagnosis"])
        if processed_data.get("outline"):
            outline_json = json.dumps(processed_data["outline"])
            ol = processed_data["outline"]
            if isinstance(ol, dict) and isinstance(ol.get("step_to_first_edit"), int):
                first_edit_step = ol["step_to_first_edit"]

    # 3. Trajectory and steps extraction
    steps_rows: list[dict[str, Any]] = []
    agent_dir = trial_dir / "agent"
    traj_path = agent_dir / "trajectory.json"
    trajectory_available = traj_path.is_file()
    step_evidence_source = "atif_projection" if trajectory_available else "none"

    if trajectory_available:
        # Collect head and all continuations
        part_paths = [traj_path]
        for cont_path in sorted(agent_dir.glob("trajectory.cont-*.json")):
            part_paths.append(cont_path)

        for ppath in part_paths:
            p_data = _safe_read_json(ppath)
            if not p_data:
                continue
            doc_name = ppath.name
            doc_sha = _file_sha256(ppath)
            doc_id = hashlib.sha256(
                f"{trial_id}\0{doc_name}".encode()
            ).hexdigest()

            steps_list = p_data.get("steps") or []
            for raw_step in steps_list:
                if not isinstance(raw_step, dict):
                    continue
                step_id = raw_step.get("step_id")
                if not isinstance(step_id, int):
                    continue
                source = str(raw_step.get("source") or "")
                timestamp = raw_step.get("timestamp")
                is_copied = bool(raw_step.get("is_copied_context", False))
                metrics = raw_step.get("metrics") or {}
                prompt_toks = metrics.get("prompt_tokens")
                comp_toks = metrics.get("completion_tokens")

                cmd_text, cmd_prov = _extract_commands_from_step(raw_step)
                obs_snippet = _extract_observation_excerpt(raw_step)
                step_ref = f"{'head' if doc_name == 'trajectory.json' else doc_name}#{step_id}"

                tool_count = len(raw_step.get("tool_calls") or [])

                steps_rows.append(
                    {
                        "job_id": job_id,
                        "trial_id": trial_id,
                        "document_id": doc_id,
                        "step_id": step_id,
                        "source_path": f"agent/{doc_name}",
                        "source_sha256": f"sha256:{doc_sha}",
                        "source": source,
                        "timestamp": str(timestamp) if timestamp else None,
                        "is_copied_context": is_copied,
                        "prompt_tokens": prompt_toks if isinstance(prompt_toks, int) else None,
                        "completion_tokens": comp_toks if isinstance(comp_toks, int) else None,
                        "tool_call_count": tool_count,
                        "command_text": cmd_text,
                        "command_provenance": cmd_prov,
                        "observation_excerpt": obs_snippet,
                        "step_ref": step_ref,
                    }
                )
    else:
        # Sentinel trial-only no-step marker row
        steps_rows.append(
            {
                "job_id": job_id,
                "trial_id": trial_id,
                "document_id": "no_steps",
                "step_id": -1,
                "source_path": "none",
                "source_sha256": "",
                "source": "trial_no_steps",
                "timestamp": None,
                "is_copied_context": False,
                "prompt_tokens": None,
                "completion_tokens": None,
                "tool_call_count": 0,
                "command_text": None,
                "command_provenance": None,
                "observation_excerpt": None,
                "step_ref": "no_steps",
            }
        )

    # Source job dir from provenance if available
    source_job_dir = provenance_data.get("source_path") or str(job_dir)
    source_trial_dir = str(trial_dir)
    published_job_dir = str(published_dir) if published_dir else None

    trial_row = {
        "job_id": job_id,
        "trial_id": trial_id,
        "job_name": job_name,
        "trial_name": trial_name,
        "task_name": task_name,
        "card": card,
        "model_name": model_name,
        "arm": arm,
        "split": split,
        "task_package_digest": task_package_digest,
        "raw_reward": raw_reward,
        "scored": scored,
        "counts_verdict": counts_verdict,
        "counts_reasons_json": counts_reasons_json,
        "counts_evidence_json": counts_evidence_json,
        "stop_reason": stop_reason,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "agent_steps": agent_steps,
        "decision_schema": decision_schema,
        "decision_facts_json": decision_facts_json,
        "decision_judgments_json": decision_judgments_json,
        "token_flow_json": token_flow_json,
        "taint_json": taint_json,
        "diagnosis_json": diagnosis_json,
        "outline_json": outline_json,
        "loop_kind": loop_kind,
        "first_failure_ref": first_failure_ref,
        "outcome_rule": outcome_rule,
        "outcome_attribution": outcome_attribution,
        "last_edit_step": last_edit_step,
        "first_edit_step": first_edit_step,
        "tokens_after_last_edit_input": tokens_after_last_edit_input,
        "tokens_after_last_edit_output": tokens_after_last_edit_output,
        "source_job_dir": source_job_dir,
        "source_trial_dir": source_trial_dir,
        "published_job_dir": published_job_dir,
        "report_path": report_path,
        "trajectory_available": trajectory_available,
        "processed_available": processed_available,
        "counts_available": counts_available,
        "step_evidence_source": step_evidence_source,
        "labels_json": json.dumps(labels),
    }

    return trial_row, steps_rows


# --------------------------------------------------------------------------- #
# Public Entry Point
# --------------------------------------------------------------------------- #


def connect_trace_query(
    *,
    repo_root: Path,
    results_home: Path | None = None,
    derived_root: Path | None = None,
    job_dirs: Sequence[Path] | None = None,
) -> tuple[duckdb.DuckDBPyConnection, dict[str, Any]]:
    """Return in-memory DuckDB connection and coverage dictionary.

    Caller closes connection. Transient and read-only.
    """
    root = repo_root.resolve()
    home = (results_home or results_root()).resolve()
    droot = (
        derived_root.resolve()
        if derived_root
        else derived_root_from_environment(root)
    )

    manifest_map = _load_experiment_manifests(root)
    labels_map, manifest_hashes = _load_frozen_labels(root, droot)

    # Discovery
    discovered_jobs: list[tuple[Path, dict[str, Any], Path | None]] = []

    if job_dirs is not None:
        for jdir in job_dirs:
            p = jdir.resolve()
            if not p.is_dir():
                continue
            prov = _safe_read_json(p / "provenance.json") or {}
            discovered_jobs.append((p, prov, None))
    else:
        # Bounded published agent-job discovery
        for pub_dir, prov in _published_jobs(home):
            agent, _ = _agent_model(pub_dir, prov)
            if is_agent_run(agent):
                discovered_jobs.append((pub_dir, prov, pub_dir))

    # Build trial & step rows with deduplication
    trials_by_id: dict[tuple[str, str], dict[str, Any]] = {}
    steps_by_trial_id: dict[tuple[str, str], list[dict[str, Any]]] = {}

    missing_processed = 0
    missing_counts = 0
    missing_atif = 0
    projections_skipped: dict[str, str] = {}
    by_card: dict[str, int] = {}
    by_arm: dict[str, int] = {}

    corpus_hasher = hashlib.sha256()

    for job_path, prov, pub_path in discovered_jobs:
        job_result = _safe_read_json(job_path / "result.json") or {}
        spec_data = _safe_read_json(job_path / "experiment-spec.json")
        job_name = job_path.name
        manifest_info = manifest_map.get(job_name)

        # Iterate trial directories
        trial_candidates: list[Path] = []
        try:
            for child in sorted(job_path.iterdir()):
                if child.is_dir() and (child / "result.json").is_file():
                    trial_candidates.append(child)
        except OSError:
            continue

        if not trial_candidates:
            projections_skipped[job_name] = "no trial directories with result.json"
            continue

        for t_dir in trial_candidates:
            trial_name = t_dir.name
            trial_labels = labels_map.get(trial_name) or []
            t_row, s_rows = _build_trial_row(
                job_path,
                t_dir,
                pub_path,
                job_result,
                prov,
                spec_data,
                manifest_info,
                trial_labels,
            )

            key = (t_row["job_id"], t_row["trial_id"])

            # Deduplication: prefer record with processed report, then trajectory, then counts
            if key in trials_by_id:
                prev = trials_by_id[key]
                prev_score = (
                    int(prev["processed_available"]) * 4
                    + int(prev["counts_available"]) * 2
                    + int(prev["trajectory_available"])
                )
                curr_score = (
                    int(t_row["processed_available"]) * 4
                    + int(t_row["counts_available"]) * 2
                    + int(t_row["trajectory_available"])
                )
                if curr_score <= prev_score:
                    continue

            trials_by_id[key] = t_row
            steps_by_trial_id[key] = s_rows

    # Coverage metrics calculation over deduplicated corpus
    all_trial_rows = list(trials_by_id.values())
    all_step_rows: list[dict[str, Any]] = []

    for key, s_list in steps_by_trial_id.items():
        all_step_rows.extend(s_list)

    for r in all_trial_rows:
        if not r["processed_available"]:
            missing_processed += 1
        if not r["counts_available"]:
            missing_counts += 1
        if not r["trajectory_available"]:
            missing_atif += 1

        card_key = r["card"] or "unknown"
        by_card[card_key] = by_card.get(card_key, 0) + 1

        arm_key = r["arm"] or "unknown"
        by_arm[arm_key] = by_arm.get(arm_key, 0) + 1

        corpus_hasher.update(
            f"{r['job_id']}:{r['trial_id']}:{r['raw_reward']}:{r['stop_reason']}\n".encode()
        )

    coverage = {
        "discovered_jobs": len(discovered_jobs),
        "discovered_trials": len(all_trial_rows),
        "discovered_steps": len(all_step_rows),
        "missing_processed": missing_processed,
        "missing_counts": missing_counts,
        "missing_atif": missing_atif,
        "projections_skipped": projections_skipped,
        "label_manifest_hashes": manifest_hashes,
        "corpus_digest": f"sha256:{corpus_hasher.hexdigest()}",
        "by_card": by_card,
        "by_arm": by_arm,
    }

    # Register in DuckDB
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA IF NOT EXISTS z3")
    conn.execute("CREATE SCHEMA IF NOT EXISTS z4")

    # Attach Z3 parquet tables if derived root exists
    from evallab.storage.attach import _attach_z3

    try:
        _attach_z3(conn, droot)
    except Exception:
        pass

    trials_arrow = pa.Table.from_pylist(all_trial_rows, schema=TRIALS_ARROW_SCHEMA)
    steps_arrow = pa.Table.from_pylist(all_step_rows, schema=STEPS_ARROW_SCHEMA)

    conn.register("_trace_trials_data", trials_arrow)
    conn.register("_trace_steps_data", steps_arrow)

    # Create views
    conn.execute("CREATE OR REPLACE VIEW v_trace_trials AS SELECT * FROM _trace_trials_data")
    conn.execute(
        """
        CREATE OR REPLACE VIEW v_trace_steps AS
        SELECT
            s.*,
            t.job_name,
            t.trial_name,
            t.task_name,
            t.card,
            t.model_name,
            t.arm,
            t.split,
            t.task_package_digest,
            t.raw_reward,
            t.scored,
            t.counts_verdict,
            t.counts_reasons_json,
            t.stop_reason,
            t.loop_kind,
            t.first_failure_ref,
            t.outcome_rule,
            t.outcome_attribution
        FROM _trace_steps_data s
        LEFT JOIN _trace_trials_data t
          ON s.job_id = t.job_id AND s.trial_id = t.trial_id
        """
    )

    # Load canonical queries into views
    queries_sql_path = root / "sql" / "trace_queries.sql"
    if queries_sql_path.is_file():
        try:
            conn.execute(queries_sql_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return conn, coverage
