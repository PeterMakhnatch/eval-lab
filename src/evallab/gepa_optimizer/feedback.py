"""Feedback builder for GEPA prompt optimizer and reflection.

Builds bounded, real, agent-visible feedback from task instructions,
trial trajectories (via normalized TrajectoryIR steps), logs, and emitted
verifier diagnostics. Enforces path jails, central redaction semantics,
and character limits without unbounded duplicates.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from evallab.explorer import redact_text
from evallab.registry import task_directory_digest
from evallab.trajectory_ir import build_trajectory_ir

__all__ = ["build_feedback"]

_FORBIDDEN_TASK_PARTS = frozenset({"tests", "solution"})
_MAX_OBSERVATION_CHARS = 1000


def _collect_secrets() -> frozenset[str]:
    """Collect known host secret values from existing Lab credential handlers once."""
    secrets: set[str] = set()
    try:
        from evallab.execution_contracts import collected_secret_values

        secrets.update(s for s in collected_secret_values() if s)
    except ImportError:
        pass
    try:
        from evallab.harbor_zai_opencode import collected_zai_secret_values

        secrets.update(s for s in collected_zai_secret_values() if s)
    except ImportError:
        pass
    return frozenset(secrets)


def _redact_full_text(text: str, secrets: frozenset[str]) -> str:
    """Redact secret tokens, passwords, bearer headers, and known API keys."""
    if not text:
        return ""
    redacted = redact_text(text)
    for s in secrets:
        if len(s) >= 4 and s in redacted:
            redacted = redacted.replace(s, "[redacted]")
    return redacted


def _extract_command(args: Any) -> str | None:
    """Extract a representative command string from tool call arguments."""
    if isinstance(args, str):
        return args.strip()
    if isinstance(args, dict):
        for key in ("cmd", "command", "input", "script", "code"):
            val = args.get(key)
            if isinstance(val, str):
                return val.strip()
        try:
            return json.dumps(args, sort_keys=True)
        except Exception:
            return None
    return None

_ERROR_PATTERNS = (
    "Traceback (most recent call last)",
    "command not found",
    "No such file or directory",
    "Permission denied",
    "SyntaxError:",
    "ModuleNotFoundError:",
    "ImportError:",
    "FileNotFoundError:",
    "PermissionError:",
    "ZeroDivisionError:",
    "TypeError:",
    "ValueError:",
    "KeyError:",
    "AttributeError:",
    "IndexError:",
    "Error:",
    "ERROR:",
    "fatal:",
)


def _find_error_indicator(obs_text: str) -> str | None:
    """Extract a concise one-line error indicator from observation text if present."""
    if not obs_text:
        return None
    for line in obs_text.splitlines():
        s = line.strip()
        if not s:
            continue
        for pat in _ERROR_PATTERNS:
            if pat in s:
                return s[:160]
        s_lower = s.lower()
        if "exit code " in s_lower and "exit code 0" not in s_lower:
            return s[:160]
        if "returncode: " in s_lower and "returncode: 0" not in s_lower:
            return s[:160]
    return None


def _validate_path_jail(root: Path, target: Path | str, *, label: str) -> Path:
    """Resolve target strictly within root; reject path traversal and escaping symlinks."""
    resolved_root = root.resolve()
    target_path = Path(target)
    candidate = target_path if target_path.is_absolute() else (resolved_root / target_path)
    resolved_target = candidate.resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"{label} '{target}' escapes allowed root '{root}' via traversal or symlink"
        ) from exc
    return resolved_target


def _safe_child_file(parent_dir: Path, rel_path: str, *, label: str) -> Path | None:
    """Resolve child file strictly within parent_dir; reject symlink/path traversal escapes."""
    target = parent_dir / rel_path
    current = target
    while current != parent_dir:
        if current.is_symlink():
            raise ValueError(f"{label} escapes allowed root through an untrusted symlink")
        current = current.parent
    if not target.exists():
        return None
    resolved = target.resolve()
    try:
        resolved.relative_to(parent_dir.resolve())
    except ValueError as exc:
        raise ValueError(
            f"{label} '{rel_path}' under '{parent_dir}' escapes allowed root via symlink or traversal"
        ) from exc
    return resolved


def _check_task_path_safety(task_path: Path) -> None:
    for part in task_path.parts:
        if part.lower() in _FORBIDDEN_TASK_PARTS:
            raise ValueError(
                f"task_path '{task_path}' accesses forbidden hidden verification directory: '{part}'"
            )


def build_feedback(
    *,
    repo_root: Path,
    task_path: Path,
    trial_path: Path,
    max_chars: int = 24000,
    oracle_reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build bounded, real agent-visible feedback from task and trial artifacts.

    Reads only declared task instruction.md and jailed allowlisted trial artifacts.
    Reuses evallab.trajectory_ir.build_trajectory_ir for normalized steps.
    Redacts full strings before truncating to guarantee a strictly bounded return.

    Args:
        repo_root: Base repository root.
        task_path: Path to the task directory (under repo_root).
        trial_path: Path to the trial directory (under repo_root).
        max_chars: Upper character bound on returned textual feedback.
        oracle_reference: Optional dict with exact keys trial_path, result_sha256,
            and task_package_digest pointing to a completed, successful Oracle run.

    Returns:
        Dictionary with single bounded 'feedback' string, sources, coverage_notices,
        truncated flag, char_count, and max_chars.

    Raises:
        ValueError: If paths escape the repo root, target forbidden directories,
            symlink out, or if oracle_reference violates validation rules.
        FileNotFoundError: If task_path, trial_path, or oracle trial directory does not exist.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")

    if not repo_root.exists() or not repo_root.is_dir():
        raise FileNotFoundError(f"repo_root '{repo_root}' does not exist or is not a directory")

    resolved_repo_root = repo_root.resolve()

    _check_task_path_safety(Path(task_path))

    resolved_task_path = _validate_path_jail(resolved_repo_root, task_path, label="task_path")
    _check_task_path_safety(resolved_task_path)
    if not resolved_task_path.exists() or not resolved_task_path.is_dir():
        raise FileNotFoundError(f"task_path '{task_path}' does not exist or is not a directory")

    resolved_trial_path = _validate_path_jail(resolved_repo_root, trial_path, label="trial_path")
    if not resolved_trial_path.exists() or not resolved_trial_path.is_dir():
        raise FileNotFoundError(f"trial_path '{trial_path}' does not exist or is not a directory")

    sources: dict[str, str] = {}
    coverage_notices: list[str] = []
    secrets = _collect_secrets()

    # Optional Oracle Reference Validation and Extraction
    oracle_data: dict[str, Any] | None = None
    if oracle_reference is not None:
        if not isinstance(oracle_reference, dict):
            raise ValueError("oracle_reference must be a dictionary")
        expected_keys = {"trial_path", "result_sha256", "task_package_digest"}
        if set(oracle_reference.keys()) != expected_keys:
            raise ValueError(
                f"oracle_reference must contain exact keys {sorted(expected_keys)}, got {sorted(oracle_reference.keys())}"
            )
        for k in expected_keys:
            v = oracle_reference[k]
            if not isinstance(v, (str, Path)) or not str(v).strip():
                raise ValueError(f"oracle_reference key '{k}' must be a non-empty string or Path")

        raw_oracle_trial = Path(oracle_reference["trial_path"])
        _check_task_path_safety(raw_oracle_trial)
        resolved_oracle_trial = _validate_path_jail(
            resolved_repo_root, raw_oracle_trial, label="oracle_reference trial_path"
        )
        for part in resolved_oracle_trial.relative_to(resolved_repo_root).parts:
            if part.lower() in _FORBIDDEN_TASK_PARTS:
                raise ValueError(
                    f"oracle_reference trial_path accesses forbidden hidden directory: '{part}'"
                )
        if not resolved_oracle_trial.exists() or not resolved_oracle_trial.is_dir():
            raise FileNotFoundError(
                f"oracle_reference trial_path '{raw_oracle_trial}' does not exist or is not a directory"
            )

        # 1. Oracle Result & Result SHA256
        oracle_res_file = _safe_child_file(
            resolved_oracle_trial, "result.json", label="oracle trial result"
        )
        if oracle_res_file is None or not oracle_res_file.is_file():
            raise FileNotFoundError(
                f"oracle result.json not found under '{resolved_oracle_trial}'"
            )
        oracle_res_bytes = oracle_res_file.read_bytes()
        actual_res_sha = hashlib.sha256(oracle_res_bytes).hexdigest()
        expected_res_sha = str(oracle_reference["result_sha256"]).strip()
        expected_res_hex = (
            expected_res_sha[7:] if expected_res_sha.startswith("sha256:") else expected_res_sha
        )
        if actual_res_sha.lower() != expected_res_hex.lower():
            raise ValueError(
                f"oracle_reference result_sha256 mismatch: expected {expected_res_sha}, "
                f"computed {actual_res_sha}"
            )
        sources["oracle_trial_result"] = oracle_res_file.relative_to(resolved_repo_root).as_posix()

        try:
            oracle_res_data = json.loads(oracle_res_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"oracle trial result.json is unparseable: {exc}") from exc
        if not isinstance(oracle_res_data, dict):
            raise ValueError("oracle trial result.json is not a valid JSON object")

        # 2. Oracle completion, errors, agent identity, no-model, and reward 1.0
        if not oracle_res_data.get("finished_at"):
            raise ValueError("oracle trial did not finish successfully (missing finished_at)")
        if oracle_res_data.get("status") and str(oracle_res_data["status"]) != "completed":
            raise ValueError(
                f"oracle trial status is {oracle_res_data['status']!r}, expected 'completed'"
            )
        raw_oracle_err = oracle_res_data.get("exception_info") or oracle_res_data.get("error")
        if raw_oracle_err:
            raise ValueError(f"oracle trial encountered error: {raw_oracle_err}")

        oracle_agent_info = oracle_res_data.get("agent_info")
        if not isinstance(oracle_agent_info, dict):
            oracle_agent_info = {}
        oracle_cfg = oracle_res_data.get("config")
        oracle_cfg_agent = (
            oracle_cfg.get("agent")
            if isinstance(oracle_cfg, dict) and isinstance(oracle_cfg.get("agent"), dict)
            else {}
        )
        oracle_agent_name = oracle_agent_info.get("name") or oracle_cfg_agent.get("name")
        if not oracle_agent_name or not str(oracle_agent_name).startswith("oracle"):
            raise ValueError(
                f"oracle trial agent is {oracle_agent_name!r}, expected 'oracle'"
            )

        oracle_model = (
            oracle_cfg_agent.get("model_name")
            or oracle_agent_info.get("model_info")
            or oracle_res_data.get("model")
        )
        if oracle_model is not None:
            raise ValueError(
                f"oracle trial has model {oracle_model!r}, expected control agent with no model"
            )

        oracle_v_res = oracle_res_data.get("verifier_result")
        if not isinstance(oracle_v_res, dict):
            oracle_v_res = {}
        oracle_rewards = oracle_v_res.get("rewards")
        if not isinstance(oracle_rewards, dict):
            oracle_rewards = {}
        oracle_reward = oracle_rewards.get("reward")
        if oracle_reward is None:
            raise ValueError("oracle trial verifier_result has no primary reward")
        try:
            if float(oracle_reward) != 1.0:
                raise ValueError(
                    f"oracle trial primary reward is {oracle_reward!r}, expected 1.0"
                )
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"oracle trial primary reward is not 1.0: {oracle_reward!r}"
            ) from exc

        # 3. Task package binding and native parent lab-metadata.json
        expected_pkg_digest = str(oracle_reference["task_package_digest"]).strip()
        computed_task_pkg_digest = task_directory_digest(resolved_task_path)
        if computed_task_pkg_digest != expected_pkg_digest:
            raise ValueError(
                f"oracle_reference task_package_digest mismatch: expected {expected_pkg_digest}, "
                f"task directory computed {computed_task_pkg_digest}"
            )

        oracle_parent = resolved_oracle_trial.parent
        _validate_path_jail(resolved_repo_root, oracle_parent, label="oracle job parent")
        oracle_meta_file = _safe_child_file(
            oracle_parent, "lab-metadata.json", label="oracle lab-metadata"
        )
        if oracle_meta_file is None or not oracle_meta_file.is_file():
            oracle_meta_file = _safe_child_file(
                resolved_oracle_trial, "lab-metadata.json", label="oracle lab-metadata"
            )
        if oracle_meta_file is None or not oracle_meta_file.is_file():
            raise FileNotFoundError(
                f"oracle lab-metadata.json not found under '{oracle_parent}' or '{resolved_oracle_trial}'"
            )
        sources["oracle_lab_metadata"] = oracle_meta_file.relative_to(resolved_repo_root).as_posix()

        try:
            oracle_meta_json = json.loads(oracle_meta_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"oracle lab-metadata.json is unparseable: {exc}") from exc
        if not isinstance(oracle_meta_json, dict):
            raise ValueError("oracle lab-metadata.json is not a valid JSON object")

        meta_exp = oracle_meta_json.get("experiment")
        if not isinstance(meta_exp, dict):
            raise ValueError("oracle lab-metadata.json missing 'experiment' section")
        meta_pkg_digest = meta_exp.get("package_digest")
        if not meta_pkg_digest or meta_pkg_digest != expected_pkg_digest:
            raise ValueError(
                f"oracle lab-metadata experiment.package_digest {meta_pkg_digest!r} "
                f"does not match expected {expected_pkg_digest!r}"
            )

        # 4. Oracle Trajectory / Command Log
        oracle_actions: list[str] = []
        oracle_traj_file = _safe_child_file(
            resolved_oracle_trial, "agent/trajectory.json", label="oracle trajectory"
        )
        if oracle_traj_file is None:
            oracle_traj_file = _safe_child_file(
                resolved_oracle_trial, "trajectory.json", label="oracle trajectory"
            )
        if oracle_traj_file is not None and oracle_traj_file.is_file():
            sources["oracle_trajectory"] = oracle_traj_file.relative_to(resolved_repo_root).as_posix()
            try:
                raw_oracle_traj = json.loads(oracle_traj_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw_oracle_traj = {}
            if isinstance(raw_oracle_traj, dict) and raw_oracle_traj.get("steps"):
                for step_dict in raw_oracle_traj.get("steps", []):
                    if (
                        isinstance(step_dict, dict)
                        and "observation_results" not in step_dict
                        and isinstance(step_dict.get("observation"), dict)
                    ):
                        res = step_dict["observation"].get("results")
                        if isinstance(res, list):
                            step_dict["observation_results"] = res
                oracle_ir = build_trajectory_ir(
                    raw_oracle_traj, source_path=sources["oracle_trajectory"]
                )
                for step in oracle_ir.steps:
                    if step.source == "agent" and step.tool_calls:
                        for tc in step.tool_calls:
                            cmd = _extract_command(tc.arguments) or json.dumps(
                                tc.arguments, sort_keys=True
                            )
                            oracle_actions.append(
                                f"- Step {step.step_id} Action [{tc.function_name}]: {cmd}"
                            )

        oracle_txt_file = _safe_child_file(
            resolved_oracle_trial, "agent/oracle.txt", label="oracle log"
        )
        if oracle_txt_file is None:
            oracle_txt_file = _safe_child_file(
                resolved_oracle_trial, "oracle.txt", label="oracle log"
            )
        oracle_txt_content = ""
        if oracle_txt_file is not None and oracle_txt_file.is_file():
            sources["oracle_log"] = oracle_txt_file.relative_to(resolved_repo_root).as_posix()
            oracle_txt_content = oracle_txt_file.read_text(encoding="utf-8").strip()

        if not oracle_actions and not oracle_txt_content:
            coverage_notices.append(
                "oracle_trace: absent (oracle agent recorded 0 command steps; agent/oracle.txt is empty)"
            )

        # 5. Oracle File Transitions from State-Journal
        oracle_transitions: list[str] = []
        oracle_transition_paths: set[str] = set()
        oracle_diff_file = _safe_child_file(
            resolved_oracle_trial, "state-journal/state-diff.json", label="oracle state diff"
        )
        if oracle_diff_file is not None and oracle_diff_file.is_file():
            sources["oracle_state_diff"] = oracle_diff_file.relative_to(resolved_repo_root).as_posix()
            try:
                diff_data = json.loads(oracle_diff_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                diff_data = {}
            if isinstance(diff_data, dict):
                for change in diff_data.get("changes", []):
                    if isinstance(change, dict) and change.get("path"):
                        ch_path = change["path"]
                        if any(part.lower() in _FORBIDDEN_TASK_PARTS for part in Path(ch_path).parts):
                            continue
                        oracle_transition_paths.add(ch_path)
                        ch_type = change.get("change_type", "modified")
                        node_info = change.get("after") or change.get("before") or {}
                        entry_type = node_info.get("type", "file")
                        size_bytes = node_info.get("size_bytes")
                        size_str = f", {size_bytes} bytes" if size_bytes is not None else ""
                        oracle_transitions.append(f"- [{ch_type}] {ch_path} ({entry_type}{size_str})")
        else:
            oracle_events_file = _safe_child_file(
                resolved_oracle_trial, "state-journal/state-events.jsonl", label="oracle state events"
            )
            if oracle_events_file is not None and oracle_events_file.is_file():
                sources["oracle_state_events"] = oracle_events_file.relative_to(resolved_repo_root).as_posix()
                for line in oracle_events_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ev, dict) and ev.get("path"):
                        ev_path = ev["path"]
                        if any(part.lower() in _FORBIDDEN_TASK_PARTS for part in Path(ev_path).parts):
                            continue
                        oracle_transition_paths.add(ev_path)
                        ops = ",".join(ev.get("operations", ["modified"]))
                        oracle_transitions.append(f"- [{ops}] {ev_path}")
            else:
                coverage_notices.append(
                    "oracle_state_journal: absent (no state-journal captured for oracle trial)"
                )

        oracle_data = {
            "task_package_digest": expected_pkg_digest,
            "result_sha256": expected_res_sha,
            "primary_reward": 1.0,
            "actions": oracle_actions,
            "log_content": oracle_txt_content,
            "transitions": oracle_transitions,
            "transition_paths": oracle_transition_paths,
        }


    # 1. Declared Task Instruction (instruction.md only)
    task_instruction: str | None = None
    inst_file = _safe_child_file(resolved_task_path, "instruction.md", label="task instruction")
    if inst_file is not None and inst_file.is_file():
        task_instruction = inst_file.read_text(encoding="utf-8")
        sources["task_instruction"] = inst_file.relative_to(resolved_repo_root).as_posix()
    else:
        coverage_notices.append(
            "task_instruction: absent (instruction.md not found in task directory)"
        )

    # 2. Trial Result & Outcome (jailed result.json)
    result_file = _safe_child_file(resolved_trial_path, "result.json", label="trial result")
    result_data: dict[str, Any] = {}
    if result_file is not None and result_file.is_file():
        sources["trial_result"] = result_file.relative_to(resolved_repo_root).as_posix()
        try:
            loaded_res = json.loads(result_file.read_text(encoding="utf-8"))
            if isinstance(loaded_res, dict):
                result_data = loaded_res
        except json.JSONDecodeError as exc:
            coverage_notices.append(f"trial_result: unparseable_json ({exc})")

    raw_tid = result_data.get("task_name") or result_data.get("task_id")
    if isinstance(raw_tid, dict):
        task_id = str(raw_tid.get("path") or raw_tid.get("name") or resolved_task_path.name)
    elif raw_tid:
        task_id = str(raw_tid)
    else:
        task_id = resolved_task_path.name

    agent_info = result_data.get("agent_info")
    if not isinstance(agent_info, dict):
        agent_info = {}
    config = result_data.get("config")
    cfg_agent = config.get("agent") if isinstance(config, dict) else None
    agent_name = agent_info.get("name") or (
        cfg_agent.get("name") if isinstance(cfg_agent, dict) else None
    )

    verifier_result = result_data.get("verifier_result")
    if not isinstance(verifier_result, dict):
        verifier_result = {}
    rewards = verifier_result.get("rewards")
    if not isinstance(rewards, dict):
        rewards = {}
    primary_reward: float | None = None
    if "reward" in rewards and isinstance(rewards["reward"], (int, float)):
        primary_reward = float(rewards["reward"])

    raw_error = result_data.get("exception_info") or result_data.get("error")
    error_str = str(raw_error) if raw_error else None

    # Status: reported by result; no status-inferred success
    if error_str:
        outcome_status = "error"
    elif "status" in result_data:
        outcome_status = str(result_data["status"])
    elif result_data:
        outcome_status = "completed"
    else:
        outcome_status = "unknown"

    agent_result = result_data.get("agent_result")
    if not isinstance(agent_result, dict):
        agent_result = {}
    exit_code = agent_result.get("exit_code", result_data.get("exit_code"))

    # Emitted verifier diagnostics (checks.json, test-stdout.txt)
    verifier_diag: str | None = None
    checks_file = _safe_child_file(
        resolved_trial_path, "verifier/checks.json", label="verifier checks"
    )
    if checks_file is not None and checks_file.is_file():
        sources["verifier_checks"] = checks_file.relative_to(resolved_repo_root).as_posix()
        try:
            checks_data = json.loads(checks_file.read_text(encoding="utf-8"))
            if isinstance(checks_data, dict):
                verifier_diag = json.dumps(checks_data, sort_keys=True)
        except json.JSONDecodeError:
            pass

    if verifier_diag is None:
        stdout_file = _safe_child_file(
            resolved_trial_path, "verifier/test-stdout.txt", label="verifier stdout"
        )
        if stdout_file is not None and stdout_file.is_file():
            sources["verifier_stdout"] = stdout_file.relative_to(resolved_repo_root).as_posix()
            raw_stdout = stdout_file.read_text(encoding="utf-8").strip()
            if raw_stdout:
                verifier_diag = _redact_full_text(raw_stdout, secrets)[:_MAX_OBSERVATION_CHARS]

    # 3. Jailed Allowlisted Trajectory via evallab.trajectory_ir.build_trajectory_ir
    traj_file = _safe_child_file(resolved_trial_path, "agent/trajectory.json", label="trajectory")
    if traj_file is None:
        traj_file = _safe_child_file(resolved_trial_path, "trajectory.json", label="trajectory")

    trace_status: str
    actions_summary: list[str] = []
    agent_trace_errors: list[str] = []
    agent_redundant_actions: list[str] = []
    seen_actions: dict[tuple[str, str], list[int]] = {}
    final_response: str | None = None
    if traj_file is None or not traj_file.is_file():
        trace_status = "absent"
        if agent_name in ("nop", "oracle"):
            coverage_notices.append(
                f"trajectory: absent (control agent '{agent_name}' has no model trace)"
            )
        else:
            coverage_notices.append("trajectory: absent (missing trajectory file)")
    else:
        trace_status = "present"
        rel_traj_path = traj_file.relative_to(resolved_repo_root).as_posix()
        sources["trial_trajectory"] = rel_traj_path
        try:
            raw_data = json.loads(traj_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            trace_status = "corrupt"
            coverage_notices.append(f"trajectory: unparseable_json ({exc})")
            raw_data = {}

        if isinstance(raw_data, dict) and raw_data.get("steps"):
            # Map ATIF step.observation.results into observation_results if needed
            for step_dict in raw_data.get("steps", []):
                if (
                    isinstance(step_dict, dict)
                    and "observation_results" not in step_dict
                    and isinstance(step_dict.get("observation"), dict)
                ):
                    res = step_dict["observation"].get("results")
                    if isinstance(res, list):
                        step_dict["observation_results"] = res

            ir = build_trajectory_ir(raw_data, source_path=rel_traj_path)

            for step in ir.steps:
                if step.source == "agent" and step.tool_calls:
                    for tc in step.tool_calls:
                        cmd = _extract_command(tc.arguments) or json.dumps(
                            tc.arguments, sort_keys=True
                        )
                        act_key = (tc.function_name, cmd)
                        if act_key in seen_actions:
                            prior_step = seen_actions[act_key][-1]
                            cmd_disp = cmd[:120] + "..." if len(cmd) > 120 else cmd
                            agent_redundant_actions.append(
                                f"Step {step.step_id} repeated action [{tc.function_name}] from Step {prior_step}: {cmd_disp}"
                            )
                            seen_actions[act_key].append(step.step_id)
                        else:
                            seen_actions[act_key] = [step.step_id]

                        actions_summary.append(
                            f"- Step {step.step_id} Action [{tc.function_name}]: {cmd}"
                        )
                        for obs in step.observation_results:
                            if obs.source_call_id == tc.tool_call_id or not obs.source_call_id:
                                obs_text = (
                                    _redact_full_text(str(obs.content), secrets)
                                    if obs.content is not None
                                    else ""
                                )
                                err_ind = _find_error_indicator(obs_text)
                                if err_ind:
                                    agent_trace_errors.append(
                                        f"Step {step.step_id} Action [{tc.function_name}] observed error: {err_ind}"
                                    )
                                if len(obs_text) > _MAX_OBSERVATION_CHARS:
                                    obs_text = (
                                        obs_text[:_MAX_OBSERVATION_CHARS]
                                        + f"\n... [observation truncated to {_MAX_OBSERVATION_CHARS} chars]"
                                    )
                                actions_summary.append(f"  Observation: {obs_text}")
                    actions_summary.append(f"  Reasoning: {step.reasoning_content}")

            for step in reversed(ir.steps):
                if step.source == "agent":
                    msg = str(step.message).strip() if step.message else ""
                    if msg and not msg.startswith("<<evallab-redacted:"):
                        final_response = msg
                        break


    # Agent state-journal file transitions (when oracle_reference provided)
    agent_transition_paths: set[str] = set()
    if oracle_data is not None:
        agent_diff_file = _safe_child_file(
            resolved_trial_path, "state-journal/state-diff.json", label="agent state diff"
        )
        if agent_diff_file is not None and agent_diff_file.is_file():
            sources["trial_state_diff"] = agent_diff_file.relative_to(resolved_repo_root).as_posix()
            try:
                agent_diff_data = json.loads(agent_diff_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                agent_diff_data = {}
            if isinstance(agent_diff_data, dict):
                for change in agent_diff_data.get("changes", []):
                    if isinstance(change, dict) and change.get("path"):
                        p = change["path"]
                        if not any(part.lower() in _FORBIDDEN_TASK_PARTS for part in Path(p).parts):
                            agent_transition_paths.add(p)
        else:
            agent_events_file = _safe_child_file(
                resolved_trial_path, "state-journal/state-events.jsonl", label="agent state events"
            )
            if agent_events_file is not None and agent_events_file.is_file():
                sources["trial_state_events"] = agent_events_file.relative_to(resolved_repo_root).as_posix()
                for line in agent_events_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ev, dict) and ev.get("path"):
                        ev_path = ev["path"]
                        if not any(part.lower() in _FORBIDDEN_TASK_PARTS for part in Path(ev_path).parts):
                            agent_transition_paths.add(ev_path)
    # 4. Format Single Full Text
    lines: list[str] = []
    lines.append(f"# Evaluation Feedback: {task_id}")
    lines.append("")

    lines.append("## Task Instruction")
    if task_instruction:
        lines.append(task_instruction.strip())
    else:
        lines.append(
            "Task instruction absent (instruction.md not found in declared task directory)."
        )
    lines.append("")

    lines.append("## Outcome")
    lines.append(f"- Status: {outcome_status}")
    if primary_reward is not None:
        lines.append(
            f"- Primary Reward: {primary_reward:.4f}"
            if isinstance(primary_reward, float)
            else f"- Primary Reward: {primary_reward}"
        )
    if rewards:
        lines.append(f"- Reward Dimensions: {json.dumps(rewards, sort_keys=True)}")
    if exit_code is not None:
        lines.append(f"- Exit Code: {exit_code}")
    if error_str:
        lines.append(f"- Error: {error_str}")
    if verifier_diag:
        lines.append(f"- Verifier Diagnostics: {verifier_diag}")
    lines.append("")

    lines.append("## Execution Trace")
    if trace_status == "absent":
        lines.append(f"Trace Status: absent ({'; '.join(coverage_notices)})")
        lines.append("No agent model trajectory was recorded for this trial.")
    else:
        lines.append(f"Trace Status: {trace_status} (actions={len(actions_summary)})")
        lines.append("")
        for act_line in actions_summary:
            lines.append(act_line)
        if final_response:
            lines.append("")
            lines.append(f"Final Response:\n{final_response}")
    lines.append("")
    if oracle_data is not None:
        lines.append("## Oracle Reference Contrast")
        lines.append(f"- Reference Package Digest: {oracle_data['task_package_digest']}")
        lines.append(f"- Reference Result Digest: {oracle_data['result_sha256']}")
        lines.append("- Reference Outcome: completed (Reward: 1.0000, Errors: none)")
        agent_rew = (
            f"{primary_reward:.4f}"
            if isinstance(primary_reward, float)
            else (str(primary_reward) if primary_reward is not None else "none")
        )
        lines.append(
            f"- Agent Outcome: {outcome_status} (Reward: {agent_rew}, Errors: {error_str or 'none'})"
        )
        lines.append("")

        lines.append("### Oracle Observed Action Trace")
        if oracle_data["actions"]:
            for act in oracle_data["actions"]:
                lines.append(act)
        elif oracle_data["log_content"]:
            for log_ln in oracle_data["log_content"].splitlines()[:10]:
                lines.append(f"- {log_ln}")
        else:
            lines.append(
                "Trace Status: absent (agent/oracle.txt is empty; registered oracle has no command trace)"
            )
        lines.append("")

        lines.append("### Oracle Observed File Transitions")
        if oracle_data["transitions"]:
            for tr in oracle_data["transitions"]:
                lines.append(tr)
        else:
            lines.append("File Transitions: absent (no state-journal recorded for oracle trial)")
        lines.append("")

        lines.append("### Agent Contrast & Trace Diagnostics")
        lines.append(f"- Total Agent Actions: {len(actions_summary)}")
        if agent_trace_errors:
            lines.append(f"- Agent Observed Errors ({len(agent_trace_errors)}):")
            for err_ln in agent_trace_errors:
                lines.append(f"  * {err_ln}")
        else:
            lines.append("- Agent Observed Errors: none observed in tool outputs")

        if agent_redundant_actions:
            lines.append(f"- Redundant / Repeated Agent Actions ({len(agent_redundant_actions)}):")
            for red_ln in agent_redundant_actions:
                lines.append(f"  * {red_ln}")
        else:
            lines.append("- Redundant / Repeated Agent Actions: none observed")

        missing_from_agent = sorted(oracle_data["transition_paths"] - agent_transition_paths)
        matched_transitions = sorted(oracle_data["transition_paths"] & agent_transition_paths)
        if missing_from_agent:
            lines.append(f"- Missing Expected Outputs ({len(missing_from_agent)}):")
            for mp in missing_from_agent:
                lines.append(f"  * {mp} (produced by Oracle reference, missing in agent output)")
        elif oracle_data["transition_paths"]:
            lines.append("- Output File Transitions: all Oracle-produced files observed in agent output")
        if matched_transitions:
            lines.append(f"- Matched Output Transitions ({len(matched_transitions)}):")
            for mp in matched_transitions:
                lines.append(f"  * {mp}")
        lines.append("")

    lines.append("## Sources & Coverage")
    for src_name, src_path in sorted(sources.items()):
        lines.append(f"- Source [{src_name}]: {src_path}")
    if coverage_notices:
        lines.append("- Notices:")
        for notice in coverage_notices:
            lines.append(f"  * {notice}")

    raw_text = "\n".join(lines).strip() + "\n"

    # 5. Redact Full String Before Truncating
    # All individual excerpts have already been redacted before their local bounds.
    redacted_text = _redact_full_text(raw_text, secrets)

    # 6. Honest Budget Truncation Against max_chars
    truncated = False
    if len(redacted_text) <= max_chars:
        final_text = redacted_text
    else:
        truncated = True
        coverage_notices.append(f"feedback truncated to max_chars={max_chars} budget")
        trunc_notice = (
            f"\n\n[Truncated: feedback exceeded max_chars budget of {max_chars} characters]"
        )
        if max_chars <= len(trunc_notice):
            final_text = redacted_text[:max_chars]
        else:
            allowed = max_chars - len(trunc_notice)
            final_text = redacted_text[:allowed] + trunc_notice

    return {
        "feedback": final_text,
        "sources": sources,
        "coverage_notices": coverage_notices,
        "truncated": truncated,
        "char_count": len(final_text),
        "max_chars": max_chars,
    }
