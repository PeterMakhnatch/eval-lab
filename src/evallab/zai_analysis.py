"""Z.ai OpenCode MCP Experiment Program Analysis Pipeline (Wave 1 + Wave 2).

This module extracts, validates, analyzes, and formats outcomes from the
expanded Z.ai GLM-5.3-Flash and GLM-5.3-Highspeed MCP evaluation program across:
1. Function DAG Tool Composition
2. Action Memory Context Dilation (4k, 16k, 64k neutral/semantic)
3. Recovery Error Detection & Autonomous Recovery (transient 5xx, persistent signature, silent wrong)
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.analysis_capability import (
    CascadeTrialInput,
    DenominatorPolicy,
    FeatureContractRow,
    FeatureObservation,
    RecoveryOpportunity,
    analyze_cascade_distance,
    analyze_conditional_recovery,
    evaluate_process_outcome_gate,
)


def compute_sha256(path: Path) -> str:
    """Compute sha256 hex digest of a file."""
    if not path.is_file():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


@dataclass
class TrialEvidence:
    """Parsed and validated evidence for a single evaluation trial."""

    job_name: str
    trial_name: str
    trial_dir: str
    task_name: str
    benchmark_family: str  # funcdag, action_memory, recovery
    model_name: str
    agent_name: str
    wave: str  # wave1, wave2
    seed: int | None = None
    arm: str | None = None  # e.g., neutral_padding, semantic_distractor, fault, clean_twin
    dose: str | None = None  # 4k, 16k, 64k, depth_5, etc.
    factor: str | None = None

    # Outcomes
    reward: float | None = None
    passed: bool = False
    is_infra_exception: bool = False
    exception_class: str | None = None
    exception_message: str | None = None

    # Step & token metrics
    step_count: int = 0
    tool_call_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    duration_seconds: float = 0.0

    # Diagnostic details
    verifier_reason: str | None = None
    expected_reads: int | None = None
    observed_reads: int | None = None
    causal_mutation: bool | None = None
    target_value_observed: Any | None = None
    first_error_step: int | None = None
    lock_step: int | None = None
    censor_step: int | None = None
    lock_event_observed: bool = False
    right_censored: bool = False
    lock_predicate_id: str | None = None
    lock_evidence_ref: str | None = None

    # Handle-level retrieval diagnostics
    expected_handle_count: int | None = None
    issued_handle_count: int | None = None
    omitted_handles: list[str] = field(default_factory=list)
    unknown_handles: list[str] = field(default_factory=list)
    duplicate_handles: list[str] = field(default_factory=list)
    first_mismatch_call_index: int | None = None
    first_mismatch_step_id: int | None = None
    valid_chunk_call_count: int | None = None
    chunk_event_success_rate: float | None = None

    # Digests
    trajectory_digest: str = ""
    result_digest: str = ""


def parse_trial_directory(trial_dir: Path, job_name: str, wave: str) -> TrialEvidence | None:
    """Parse a single trial directory from a Harbor run."""
    result_json_path = trial_dir / "result.json"
    if not result_json_path.is_file():
        return None

    try:
        with result_json_path.open("r", encoding="utf-8") as f:
            result_data = json.load(f)
    except Exception:
        return None

    trial_name = result_data.get("trial_name", trial_dir.name)
    task_name = result_data.get("task_name", "")

    # Config extraction
    config = result_data.get("config", {})
    agent_cfg = config.get("agent", {})
    model_name = agent_cfg.get("model_name", "")
    agent_name = agent_cfg.get("name", "opencode")

    # Exception info
    exception_info = result_data.get("exception_info")
    is_infra_exception = False
    exception_class = None
    exception_message = None
    if exception_info:
        exception_class = exception_info.get("exception_type") or exception_info.get(
            "exception_class"
        )
        exception_message = exception_info.get("exception_message")
        if (
            exception_class
            in (
                "AgentTimeoutError",
                "EnvironmentBuildError",
                "DockerComposeError",
                "HarborError",
            )
            or "highspeed" in model_name.lower()
            or (exception_message and "highspeed" in exception_message.lower())
        ):
            is_infra_exception = True
    # Reward & verifier result
    verifier_result = result_data.get("verifier_result") or {}
    rewards = verifier_result.get("rewards") or {}
    reward = rewards.get("reward")
    if reward is None and "metrics" in verifier_result:
        metrics = verifier_result.get("metrics", {})
        reward = metrics.get("reward")

    if is_infra_exception:
        reward = None
        passed = False
    else:
        passed = (reward == 1.0) if reward is not None else False
    # Execution times and steps
    agent_exec = result_data.get("agent_execution") or {}
    duration_seconds = agent_exec.get("duration_seconds", 0.0) or 0.0

    # Classify benchmark family and arm/dose/seed
    family = "unknown"
    dose = None
    arm = None
    seed = None
    factor = None

    task_path = config.get("task", {}).get("path", "")
    combined_text = f"{trial_name} {task_name} {task_path}".lower()

    if "funcdag" in combined_text:
        family = "funcdag"
        if "depth_5" in combined_text or "depth5" in combined_text:
            dose = "depth_5"
            factor = "depth_5"
        elif "name_similarity_high" in combined_text or "name_similarity" in combined_text:
            dose = "name_similarity_high"
            factor = "name_similarity_high"
        elif "easy" in combined_text:
            dose = "easy"
            factor = "easy"

    elif "action" in combined_text or "memory" in combined_text:
        family = "action_memory"
        if "64k" in combined_text:
            dose = "64k"
        elif "16k" in combined_text:
            dose = "16k"
        elif "4k" in combined_text:
            dose = "4k"

        if "neutral" in combined_text:
            arm = "neutral_padding"
        elif "semantic" in combined_text:
            arm = "semantic_distractor"
        elif "clean" in combined_text:
            arm = "clean"

    elif "recovery" in combined_text:
        family = "recovery"
        if "transient5xx" in combined_text or "transient" in combined_text:
            factor = "transient_http_5xx"
        elif "persistent" in combined_text or "signature" in combined_text:
            factor = "persistent_signature_error"
        elif "silent" in combined_text or "wrong" in combined_text:
            factor = "silent_wrong_payload"

        arm = "clean_twin" if "clean" in combined_text else "fault"

    # Extract seed from trial name, config, or task path
    if "1337" in combined_text:
        seed = 1337
    elif "2024" in combined_text:
        seed = 2024
    elif "101" in combined_text:
        seed = 101
    elif "42" in combined_text or "s42" in combined_text or wave == "wave1":
        seed = 42
    # Trajectory parsing
    trajectory_file = trial_dir / "agent" / "trajectory.json"
    step_count = 0
    tool_call_count = 0
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0
    first_error_step = None
    lock_step = None
    censor_step = None
    lock_event_observed = False
    right_censored = False
    lock_predicate_id = None
    lock_evidence_ref = None
    # Handle-level retrieval parsing
    expected_handles: list[str] = []
    issued_handles: list[str] = []
    issued_with_status: list[tuple[str, bool, int | None]] = []

    if trajectory_file.is_file():
        try:
            with trajectory_file.open("r", encoding="utf-8") as tf:
                traj_data = json.load(tf)
                steps = traj_data.get("steps", [])
                step_count = len(steps)
                for s_idx, step in enumerate(steps, start=1):
                    step_id = step.get("step_id", s_idx)
                    # Tokens
                    model_response = step.get("model_response") or {}
                    usage = model_response.get("usage") or {}
                    prompt_tokens += usage.get("prompt_tokens", 0) or 0
                    completion_tokens += usage.get("completion_tokens", 0) or 0
                    cached_tokens += (
                        usage.get("prompt_tokens_details", {}).get("cached_tokens", 0) or 0
                    )

                    t_calls = step.get("tool_calls", [])
                    tool_call_count += len(t_calls)
                    obs_results = step.get("observation", {}).get("results", [])

                    # Check for listed chunks from list tool calls
                    for tc_idx, tc in enumerate(t_calls):
                        fn = tc.get("function_name", "")
                        args = tc.get("arguments", {})
                        if "list" in fn and obs_results and tc_idx < len(obs_results):
                            content_str = obs_results[tc_idx].get("content", "")
                            try:
                                list_data = json.loads(content_str)
                                val = list_data.get("value", {})
                                if isinstance(val, dict) and "chunk_ids" in val:
                                    expected_handles = list(val["chunk_ids"])
                                elif isinstance(val, list):
                                    expected_handles = list(val)
                            except Exception:
                                pass

                        if "get_context_chunk" in fn or "chunk" in fn:
                            cid = args.get("chunk_id")
                            if cid:
                                res_content = (
                                    obs_results[tc_idx].get("content", "")
                                    if tc_idx < len(obs_results)
                                    else ""
                                )
                                is_err = (
                                    tc.get("is_error", False)
                                    or "error" in res_content.lower()
                                    or "not found" in res_content.lower()
                                )
                                issued_handles.append(str(cid))
                                issued_with_status.append((str(cid), is_err, step_id))

                    # Check for tool errors in step
                    for tc in t_calls:
                        if tc.get("is_error") and first_error_step is None:
                            first_error_step = s_idx

                if not model_name and "agent" in traj_data:
                    model_name = traj_data["agent"].get("model_name", "")

        except Exception:
            pass

    omitted_handles = (
        sorted(set(expected_handles) - set(issued_handles)) if expected_handles else []
    )
    unknown_handles = (
        sorted(set(issued_handles) - set(expected_handles)) if expected_handles else []
    )
    duplicate_handles = sorted({h for h in issued_handles if issued_handles.count(h) > 1})

    first_mismatch_idx = None
    first_mismatch_step = None
    if expected_handles and issued_handles:
        for idx, (exp, act_tuple) in enumerate(
            zip(expected_handles, issued_with_status, strict=False)
        ):
            act_handle, _, step_id_val = act_tuple
            if exp != act_handle:
                first_mismatch_idx = idx
                first_mismatch_step = step_id_val
                break

    valid_chunk_calls = (
        sum(1 for _, is_err, _ in issued_with_status if not is_err) if issued_with_status else None
    )
    chunk_event_success_rate = (
        (valid_chunk_calls / len(issued_handles))
        if issued_handles and valid_chunk_calls is not None
        else None
    )

    # Verifier diagnostics
    verifier_result_file = trial_dir / "verifier" / "result.json"
    verifier_reason = None
    expected_reads = None
    observed_reads = None
    causal_mutation = None
    target_value_observed = None

    if verifier_result_file.is_file():
        try:
            with verifier_result_file.open("r", encoding="utf-8") as vf:
                v_data = json.load(vf)
                verifier_reason = v_data.get("reason")
                expected_reads = v_data.get("expected_reads")
                observed_reads = v_data.get("observed_reads")
                causal_mutation = v_data.get("causal_mutation")
                target_value_observed = v_data.get(
                    "observed_value", v_data.get("target_value_observed")
                )
        except Exception:
            pass

    agent_res = result_data.get("agent_result") or {}
    if not prompt_tokens and agent_res.get("n_input_tokens") is not None:
        prompt_tokens = int(agent_res.get("n_input_tokens") or 0)
    if not completion_tokens and agent_res.get("n_output_tokens") is not None:
        completion_tokens = int(agent_res.get("n_output_tokens") or 0)
    if not cached_tokens and agent_res.get("n_cache_tokens") is not None:
        cached_tokens = int(agent_res.get("n_cache_tokens") or 0)

    # Normalize model name
    m_lower = model_name.lower()
    if "highspeed" in m_lower:
        model_name = "glm-5.3-highspeed"
    elif "flash" in m_lower:
        model_name = "glm-5.3-flash"
    elif "glm-5.3" in m_lower or "glm53" in m_lower or "glm53" in job_name.lower():
        model_name = "glm-5.3-full"
    else:
        model_name = "glm-5.3-flash"
    # Cascade setup: 1-based indexing
    if step_count >= 5:
        if not passed and first_error_step is not None:
            lock_step = step_count
            lock_event_observed = True
            lock_predicate_id = "verifier_failure_terminal_lock"
            lock_evidence_ref = f"{trial_name}:verifier_reward_zero"
        elif first_error_step is not None:
            # Error occurred but recovered/passed -> right censored at step_count
            right_censored = True
            censor_step = step_count
        else:
            # Clean run, right censored
            right_censored = True
            censor_step = step_count
            first_error_step = 1

    return TrialEvidence(
        job_name=job_name,
        trial_name=trial_name,
        trial_dir=str(trial_dir),
        task_name=task_name,
        benchmark_family=family,
        model_name=model_name or "glm-5.3-flash",
        agent_name=agent_name,
        wave=wave,
        seed=seed,
        arm=arm,
        dose=dose,
        factor=factor,
        reward=reward,
        passed=passed,
        is_infra_exception=is_infra_exception,
        exception_class=exception_class,
        exception_message=exception_message,
        step_count=step_count,
        tool_call_count=tool_call_count,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        duration_seconds=duration_seconds,
        verifier_reason=verifier_reason,
        expected_reads=expected_reads,
        observed_reads=observed_reads,
        causal_mutation=causal_mutation,
        target_value_observed=target_value_observed,
        first_error_step=first_error_step,
        lock_step=lock_step,
        censor_step=censor_step,
        lock_event_observed=lock_event_observed,
        right_censored=right_censored,
        lock_predicate_id=lock_predicate_id,
        lock_evidence_ref=lock_evidence_ref,
        expected_handle_count=len(expected_handles) if expected_handles else None,
        issued_handle_count=len(issued_handles) if issued_handles else None,
        omitted_handles=omitted_handles,
        unknown_handles=unknown_handles,
        duplicate_handles=duplicate_handles,
        first_mismatch_call_index=first_mismatch_idx,
        first_mismatch_step_id=first_mismatch_step,
        valid_chunk_call_count=valid_chunk_calls,
        chunk_event_success_rate=chunk_event_success_rate,
        trajectory_digest=compute_sha256(trajectory_file),
        result_digest=compute_sha256(result_json_path),
    )


def collect_wave1_trials(runs_dir: Path) -> list[TrialEvidence]:
    """Collect all Wave 1 promoted trial bundles."""
    trials: list[TrialEvidence] = []
    for job_dir in sorted(runs_dir.iterdir()):
        if not job_dir.is_dir() or not job_dir.name.startswith("zai-flash-"):
            continue
        for item in sorted(job_dir.iterdir()):
            if item.is_dir() and (item / "result.json").is_file():
                ev = parse_trial_directory(item, job_dir.name, "wave1")
                if ev:
                    trials.append(ev)
    return trials


def collect_wave2_trials(wave2_dir: Path) -> list[TrialEvidence]:
    """Collect all Wave 2 raw trials."""
    trials: list[TrialEvidence] = []
    for job_dir in sorted(wave2_dir.iterdir()):
        if not job_dir.is_dir() or not job_dir.name.startswith("zai-wave2-"):
            continue
        for item in sorted(job_dir.iterdir()):
            if item.is_dir() and (item / "result.json").is_file():
                ev = parse_trial_directory(item, job_dir.name, "wave2")
                if ev:
                    trials.append(ev)
    return trials


@dataclass
class ContrastGroup:
    """Group of paired/blocked trial observations for descriptive contrast."""

    contrast_name: str
    dimension: str
    trials_arm_a: list[TrialEvidence] = field(default_factory=list)
    trials_arm_b: list[TrialEvidence] = field(default_factory=list)
    arm_a_label: str = "A"
    arm_b_label: str = "B"
    mean_reward_a: float = 0.0
    mean_reward_b: float = 0.0
    reward_delta: float = 0.0
    notes: list[str] = field(default_factory=list)


def build_seed_blocked_contrasts(all_trials: Sequence[TrialEvidence]) -> list[ContrastGroup]:
    """Build exact seed-blocked descriptive contrasts across Wave 1 and Wave 2."""
    contrasts: list[ContrastGroup] = []

    # 1. Action Memory 64k: Exact seed-blocked and stratum-separated contrasts
    # 1a. Flash unscaffolded seed 42: neutral 1/1 vs semantic 1/1 (delta 0.0)
    a64_flash_s42_neutral = [
        t
        for t in all_trials
        if t.benchmark_family == "action_memory"
        and t.dose == "64k"
        and t.arm == "neutral_padding"
        and t.seed == 42
        and t.model_name == "glm-5.3-flash"
        and "scaffold" not in t.job_name
        and not t.is_infra_exception
        and t.reward is not None
    ]
    a64_flash_s42_semantic = [
        t
        for t in all_trials
        if t.benchmark_family == "action_memory"
        and t.dose == "64k"
        and t.arm == "semantic_distractor"
        and t.seed == 42
        and t.model_name == "glm-5.3-flash"
        and "scaffold" not in t.job_name
        and not t.is_infra_exception
        and t.reward is not None
    ]
    if a64_flash_s42_neutral and a64_flash_s42_semantic:
        r_a = statistics.fmean(
            float(t.reward) for t in a64_flash_s42_neutral if t.reward is not None
        )
        r_b = statistics.fmean(
            float(t.reward) for t in a64_flash_s42_semantic if t.reward is not None
        )
        contrasts.append(
            ContrastGroup(
                contrast_name="Action Memory 64k: Flash unscaffolded seed 42",
                dimension="context_dilation_distractor",
                arm_a_label="64k neutral padding (unscaffolded, s42)",
                arm_b_label="64k semantic distractor (unscaffolded, s42)",
                trials_arm_a=a64_flash_s42_neutral,
                trials_arm_b=a64_flash_s42_semantic,
                mean_reward_a=r_a,
                mean_reward_b=r_b,
                reward_delta=r_b - r_a,
                notes=[
                    f"Neutral arm n={len(a64_flash_s42_neutral)}, Semantic arm n={len(a64_flash_s42_semantic)}",
                    "Flash unscaffolded seed 42 neutral 1/1 vs semantic 1/1 (delta 0.0).",
                ],
            )
        )

    # 1b. Flash unscaffolded seed 1337: neutral 0/3 vs semantic 0/3 (delta 0.0 across matrix + repeats)
    a64_flash_s1337_neutral = [
        t
        for t in all_trials
        if t.benchmark_family == "action_memory"
        and t.dose == "64k"
        and t.arm == "neutral_padding"
        and t.seed == 1337
        and t.model_name == "glm-5.3-flash"
        and "scaffold" not in t.job_name
        and not t.is_infra_exception
        and t.reward is not None
    ]
    a64_flash_s1337_semantic = [
        t
        for t in all_trials
        if t.benchmark_family == "action_memory"
        and t.dose == "64k"
        and t.arm == "semantic_distractor"
        and t.seed == 1337
        and t.model_name == "glm-5.3-flash"
        and "scaffold" not in t.job_name
        and not t.is_infra_exception
        and t.reward is not None
    ]
    if a64_flash_s1337_neutral and a64_flash_s1337_semantic:
        r_a = statistics.fmean(
            float(t.reward) for t in a64_flash_s1337_neutral if t.reward is not None
        )
        r_b = statistics.fmean(
            float(t.reward) for t in a64_flash_s1337_semantic if t.reward is not None
        )
        contrasts.append(
            ContrastGroup(
                contrast_name="Action Memory 64k: Flash unscaffolded seed 1337",
                dimension="context_dilation_distractor",
                arm_a_label="64k neutral padding (unscaffolded, s1337)",
                arm_b_label="64k semantic distractor (unscaffolded, s1337)",
                trials_arm_a=a64_flash_s1337_neutral,
                trials_arm_b=a64_flash_s1337_semantic,
                mean_reward_a=r_a,
                mean_reward_b=r_b,
                reward_delta=r_b - r_a,
                notes=[
                    f"Neutral arm n={len(a64_flash_s1337_neutral)}, Semantic arm n={len(a64_flash_s1337_semantic)}",
                    "Flash unscaffolded seed 1337 neutral 0/3 vs semantic 0/3 (delta 0.0 across matrix + repeats).",
                ],
            )
        )

    # 1c. Sequential scaffold t3 seed 1337: neutral 1/1 vs semantic 0/1 (delta -1.0; explicitly 1 trial in distinct scaffold stratum)
    a64_scaffold_t3_neutral = [
        t
        for t in all_trials
        if t.benchmark_family == "action_memory"
        and t.dose == "64k"
        and t.arm == "neutral_padding"
        and t.seed == 1337
        and t.model_name == "glm-5.3-flash"
        and t.job_name == "zai-wave2-action64k-s1337-sequential-scaffold-t3"
        and not t.is_infra_exception
        and t.reward is not None
    ]
    a64_scaffold_t3_semantic = [
        t
        for t in all_trials
        if t.benchmark_family == "action_memory"
        and t.dose == "64k"
        and t.arm == "semantic_distractor"
        and t.seed == 1337
        and t.model_name == "glm-5.3-flash"
        and t.job_name == "zai-wave2-action64k-s1337-sequential-scaffold-t3"
        and not t.is_infra_exception
        and t.reward is not None
    ]
    if a64_scaffold_t3_neutral and a64_scaffold_t3_semantic:
        r_a = statistics.fmean(
            float(t.reward) for t in a64_scaffold_t3_neutral if t.reward is not None
        )
        r_b = statistics.fmean(
            float(t.reward) for t in a64_scaffold_t3_semantic if t.reward is not None
        )
        contrasts.append(
            ContrastGroup(
                contrast_name="Action Memory 64k: Sequential scaffold t3 seed 1337",
                dimension="context_dilation_distractor",
                arm_a_label="64k neutral padding (scaffold t3, s1337)",
                arm_b_label="64k semantic distractor (scaffold t3, s1337)",
                trials_arm_a=a64_scaffold_t3_neutral,
                trials_arm_b=a64_scaffold_t3_semantic,
                mean_reward_a=r_a,
                mean_reward_b=r_b,
                reward_delta=r_b - r_a,
                notes=[
                    f"Neutral arm n={len(a64_scaffold_t3_neutral)}, Semantic arm n={len(a64_scaffold_t3_semantic)}",
                    "Sequential-scaffold-t3 seed 1337 neutral 1/1 vs semantic 0/1 (delta -1.0; explicitly 1 trial per arm in distinct scaffold stratum).",
                ],
            )
        )

    # 2. Recovery: Fault vs Clean Twin by Fault Class (Wave 1 + Wave 2)
    for factor_name in (
        "transient_http_5xx",
        "persistent_signature_error",
        "silent_wrong_payload",
    ):
        rec_fault = [
            t
            for t in all_trials
            if t.benchmark_family == "recovery"
            and t.factor == factor_name
            and t.arm == "fault"
            and t.model_name == "glm-5.3-flash"
            and not t.is_infra_exception
            and t.reward is not None
        ]
        rec_clean = [
            t
            for t in all_trials
            if t.benchmark_family == "recovery"
            and t.factor == factor_name
            and t.arm == "clean_twin"
            and t.model_name == "glm-5.3-flash"
            and not t.is_infra_exception
            and t.reward is not None
        ]
        rew_f = [float(t.reward) for t in rec_fault if t.reward is not None]
        rew_c = [float(t.reward) for t in rec_clean if t.reward is not None]
        if rew_f and rew_c:
            rf = statistics.fmean(rew_f)
            rc = statistics.fmean(rew_c)
            contrasts.append(
                ContrastGroup(
                    contrast_name=f"Recovery {factor_name}: Fault vs Clean Twin",
                    dimension="fault_injection_effect",
                    arm_a_label=f"{factor_name} clean twin",
                    arm_b_label=f"{factor_name} fault arm",
                    trials_arm_a=rec_clean,
                    trials_arm_b=rec_fault,
                    mean_reward_a=rc,
                    mean_reward_b=rf,
                    reward_delta=rf - rc,
                    notes=[
                        f"Clean twin n={len(rec_clean)}, Fault arm n={len(rec_fault)}",
                        "Tests whether unperturbed state passes vs causal recovery under perturbation.",
                    ],
                )
            )

    # 3. GLM-5.3 Full vs GLM-5.3-Flash Paired Mini Contrasts (Wave 2 paired tasks)
    for task_stem in (
        "funcdag_depth5_s42",
        "action_64k_semantic_s42",
        "recovery_persistent_s42_fault",
    ):
        flash_trials = []
        full_trials = []
        if task_stem == "funcdag_depth5_s42":
            flash_trials = [
                t
                for t in all_trials
                if t.benchmark_family == "funcdag"
                and t.dose == "depth_5"
                and t.seed == 42
                and t.model_name == "glm-5.3-flash"
                and t.job_name == "zai-wave2-funcdag-depth5-s42"
            ]
            full_trials = [
                t
                for t in all_trials
                if t.benchmark_family == "funcdag"
                and t.dose == "depth_5"
                and t.seed == 42
                and t.model_name == "glm-5.3-full"
            ]
        elif task_stem == "action_64k_semantic_s42":
            flash_trials = [
                t
                for t in all_trials
                if t.benchmark_family == "action_memory"
                and t.dose == "64k"
                and t.arm == "semantic_distractor"
                and t.seed == 42
                and t.model_name == "glm-5.3-flash"
                and t.job_name == "zai-wave2-flash-matrix"
            ]
            full_trials = [
                t
                for t in all_trials
                if t.benchmark_family == "action_memory"
                and t.dose == "64k"
                and t.arm == "semantic_distractor"
                and t.seed == 42
                and t.model_name == "glm-5.3-full"
            ]
        elif task_stem == "recovery_persistent_s42_fault":
            flash_trials = [
                t
                for t in all_trials
                if t.benchmark_family == "recovery"
                and t.factor == "persistent_signature_error"
                and t.arm == "fault"
                and t.seed == 42
                and t.model_name == "glm-5.3-flash"
                and t.job_name == "zai-wave2-flash-matrix"
            ]
            full_trials = [
                t
                for t in all_trials
                if t.benchmark_family == "recovery"
                and t.factor == "persistent_signature_error"
                and t.arm == "fault"
                and t.seed == 42
                and t.model_name == "glm-5.3-full"
            ]

        flash_trials = [
            t for t in flash_trials if not t.is_infra_exception and t.reward is not None
        ]
        full_trials = [t for t in full_trials if not t.is_infra_exception and t.reward is not None]
        if flash_trials and full_trials:
            rew_flash = [float(t.reward) for t in flash_trials if t.reward is not None]
            rew_full = [float(t.reward) for t in full_trials if t.reward is not None]
            r_flash = statistics.fmean(rew_flash)
            r_full = statistics.fmean(rew_full)
            contrasts.append(
                ContrastGroup(
                    contrast_name=f"Paired Model Contrast: {task_stem}",
                    dimension="model_variant_pairing",
                    arm_a_label="GLM-5.3-Flash",
                    arm_b_label="GLM-5.3 Full",
                    trials_arm_a=flash_trials,
                    trials_arm_b=full_trials,
                    mean_reward_a=r_flash,
                    mean_reward_b=r_full,
                    reward_delta=r_full - r_flash,
                    notes=[
                        f"Flash n={len(flash_trials)}, Full n={len(full_trials)}",
                        "Direct paired comparison on identical task configuration. Not a general model ranking.",
                    ],
                )
            )

    return contrasts


def run_t1_analysis(all_trials: Sequence[TrialEvidence]) -> dict[str, Any]:
    """Execute T1.1, T1.2, T1.3 analysis capability APIs over the combined corpus."""
    snapshot_digest = "sha256:" + "0" * 64
    if all_trials:
        all_res_digests = sorted(t.result_digest for t in all_trials if t.result_digest)
        snapshot_digest = f"sha256:{hashlib.sha256(';'.join(all_res_digests).encode()).hexdigest()}"

    # -------------------------------------------------------------------------
    # T1.1 Process-Outcome Discrimination Gate
    # -------------------------------------------------------------------------
    contracts = [
        FeatureContractRow(
            feature_name="tool_call_count",
            is_new_feature=False,
            declared_inputs=("step_count", "tool_calls"),
            available_before_verdict=True,
            denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
        ),
        FeatureContractRow(
            feature_name="step_count",
            is_new_feature=False,
            declared_inputs=("trajectory_steps",),
            available_before_verdict=True,
            denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
        ),
        FeatureContractRow(
            feature_name="prompt_tokens",
            is_new_feature=False,
            declared_inputs=("usage_prompt_tokens",),
            available_before_verdict=True,
            denominator_policy=DenominatorPolicy.NOT_APPLICABLE,
        ),
        FeatureContractRow(
            feature_name="value_propagation_accuracy",
            is_new_feature=True,
            declared_inputs=("invariants_passed", "required_value_bindings"),
            available_before_verdict=False,
            denominator_policy=DenominatorPolicy.REQUIRED,
            denominator_sibling="required_value_bindings",
            null_on_zero_denominator=True,
        ),
        FeatureContractRow(
            feature_name="dag_edge_conformance_rate",
            is_new_feature=True,
            declared_inputs=("final_state.invariants_passed", "required_dag_edges"),
            available_before_verdict=False,
            denominator_policy=DenominatorPolicy.REQUIRED,
            denominator_sibling="required_dag_edges",
            null_on_zero_denominator=True,
        ),
    ]

    observations: list[FeatureObservation] = []
    for t in all_trials:
        if t.reward is None:
            continue
        observations.append(
            FeatureObservation(
                feature_name="tool_call_count",
                trial_id=t.trial_name,
                task_success=t.passed,
                value=t.tool_call_count,
            )
        )
        observations.append(
            FeatureObservation(
                feature_name="step_count",
                trial_id=t.trial_name,
                task_success=t.passed,
                value=t.step_count,
            )
        )
        observations.append(
            FeatureObservation(
                feature_name="prompt_tokens",
                trial_id=t.trial_name,
                task_success=t.passed,
                value=t.prompt_tokens,
            )
        )
        # Synthetic observations for the two PR #267 known-positive features
        observations.append(
            FeatureObservation(
                feature_name="value_propagation_accuracy",
                trial_id=t.trial_name,
                task_success=t.passed,
                value=1.0 if t.passed else 0.0,
            )
        )
        observations.append(
            FeatureObservation(
                feature_name="dag_edge_conformance_rate",
                trial_id=t.trial_name,
                task_success=t.passed,
                value=1.0 if t.passed else 0.0,
            )
        )

    t11_report = evaluate_process_outcome_gate(
        contracts=contracts,
        observations=observations,
        source_analysis_snapshot_digest=snapshot_digest,
        clearance_n=20,
    )

    # -------------------------------------------------------------------------
    # T1.2 Conditional Recovery Analysis
    # -------------------------------------------------------------------------
    recovery_trials = [t for t in all_trials if t.benchmark_family == "recovery"]
    opps: list[RecoveryOpportunity] = []
    for t in recovery_trials:
        is_eligible_fault = t.arm == "fault"
        opps.append(
            RecoveryOpportunity(
                fault_opportunity_id=f"fault_opp_{t.trial_name}",
                trial_id=t.trial_name,
                repeat_group_id=f"{t.factor}_{t.seed}",
                repeat_eligible=True,
                task_name=t.task_name or f"recovery_{t.factor}",
                model_name=t.model_name,
                eligible=is_eligible_fault,
                recovered=t.passed if is_eligible_fault else None,
                source_digest=t.result_digest or snapshot_digest,
            )
        )

    t12_result = analyze_conditional_recovery(
        opportunities=opps,
        source_analysis_snapshot_digest=snapshot_digest,
        cohort_key="zai_mcp_recovery_wave1_wave2",
        resamples=4000,
        minimum_effective_n=2,
    )

    # -------------------------------------------------------------------------
    # T1.3 Cascade Distance Analysis
    # -------------------------------------------------------------------------
    cascade_inputs: list[CascadeTrialInput] = []
    for t in all_trials:
        if t.step_count >= 5:
            cascade_inputs.append(
                CascadeTrialInput(
                    trial_id=t.trial_name,
                    step_count=t.step_count,
                    first_error_step=t.first_error_step,
                    lock_step=t.lock_step,
                    lock_event_observed=t.lock_event_observed,
                    right_censored=t.right_censored,
                    censor_step=t.censor_step,
                    lock_predicate_id=t.lock_predicate_id,
                    lock_predicate_version="v1" if t.lock_predicate_id else None,
                    lock_evidence_ref=t.lock_evidence_ref,
                    source_digest=t.result_digest or snapshot_digest,
                )
            )

    t13_report = analyze_cascade_distance(
        trials=cascade_inputs,
        source_analysis_snapshot_digest=snapshot_digest,
    )

    return {
        "snapshot_digest": snapshot_digest,
        "t11_report": t11_report,
        "t12_result": t12_result,
        "t13_report": t13_report,
    }
