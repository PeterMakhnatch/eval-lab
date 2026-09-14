"""Feedback builder for GEPA prompt optimizer and reflection.

Builds bounded, real, agent-visible feedback from task instructions,
trial trajectories (via established Lab readers), logs, and emitted
verifier diagnostics. Enforces path jails, central redaction semantics,
and character limits.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evallab.explorer import redact_text
from evallab.traj import TrajectoryOutline, _extract_command_string, outline_trajectory

__all__ = ["build_feedback"]

_FORBIDDEN_TASK_PARTS = frozenset({"tests", "solution"})
_MAX_OBSERVATION_CHARS = 2000


def _collect_all_known_secrets() -> frozenset[str]:
    """Collect known host secret values from existing Lab credential handlers."""
    secrets: set[str] = set()
    try:
        from evallab.execution_contracts import collected_secret_values

        secrets.update(s for s in collected_secret_values() if s)
    except Exception:
        pass
    try:
        from evallab.harbor_zai_opencode import collected_zai_secret_values

        secrets.update(s for s in collected_zai_secret_values() if s)
    except Exception:
        pass
    return frozenset(secrets)


def redact_string(text: str, extra_secrets: frozenset[str] | None = None) -> str:
    """Redact secret tokens, passwords, bearer headers, and known API keys from text."""
    if not text:
        return ""
    # 1. Standard pattern redaction from explorer (Bearer, sk-..., rk-..., password, etc.)
    redacted = redact_text(text)
    # 2. Known secret string replacement
    known = _collect_all_known_secrets() if extra_secrets is None else extra_secrets
    for secret in known:
        if secret and len(secret) >= 4 and secret in redacted:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def redact_payload(data: Any, extra_secrets: frozenset[str] | None = None) -> Any:
    """Recursively redact strings and mappings."""
    if isinstance(data, str):
        return redact_string(data, extra_secrets=extra_secrets)
    if isinstance(data, dict):
        clean: dict[str, Any] = {}
        for key, val in data.items():
            key_str = str(key)
            if any(
                marker in key_str.upper()
                for marker in ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "AUTH")
            ):
                clean[key_str] = "[redacted]"
            else:
                clean[key_str] = redact_payload(val, extra_secrets=extra_secrets)
        return clean
    if isinstance(data, (list, tuple)):
        return [redact_payload(item, extra_secrets=extra_secrets) for item in data]
    return data


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
) -> dict[str, Any]:
    """Build bounded, real agent-visible feedback from task and trial artifacts.

    Args:
        repo_root: Base repository root.
        task_path: Path to the task directory (relative or absolute, under repo_root).
        trial_path: Path to the trial directory (relative or absolute, under repo_root).
        max_chars: Upper character bound on total returned textual feedback.

    Returns:
        Structured feedback dictionary containing bounded text, action/observation traces,
        outcome metrics, sources, and coverage notices.

    Raises:
        ValueError: If paths escape the repo root, target forbidden directories, or symlink out.
        FileNotFoundError: If task_path or trial_path directory does not exist.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")

    if not repo_root.exists() or not repo_root.is_dir():
        raise FileNotFoundError(f"repo_root '{repo_root}' does not exist or is not a directory")

    resolved_repo_root = repo_root.resolve()

    # Reject hidden verification / solution directories in task_path
    _check_task_path_safety(Path(task_path))

    # Path jail enforcement for task_path and trial_path
    resolved_task_path = _validate_path_jail(resolved_repo_root, task_path, label="task_path")
    if not resolved_task_path.exists() or not resolved_task_path.is_dir():
        raise FileNotFoundError(f"task_path '{task_path}' does not exist or is not a directory")

    resolved_trial_path = _validate_path_jail(resolved_repo_root, trial_path, label="trial_path")
    if not resolved_trial_path.exists() or not resolved_trial_path.is_dir():
        raise FileNotFoundError(f"trial_path '{trial_path}' does not exist or is not a directory")

    task_rel_path = resolved_task_path.relative_to(resolved_repo_root).as_posix()
    trial_rel_path = resolved_trial_path.relative_to(resolved_repo_root).as_posix()

    sources: dict[str, str] = {}
    coverage_notices: list[str] = []

    # -------------------------------------------------------------------------
    # 1. Declared Task Instruction (instruction.md only, never tests or solution)
    # -------------------------------------------------------------------------
    task_instruction: str | None = None
    inst_file = _safe_child_file(resolved_task_path, "instruction.md", label="task instruction")
    if inst_file is not None and inst_file.is_file():
        try:
            raw_inst = inst_file.read_text(encoding="utf-8")
            task_instruction = redact_string(raw_inst)
            sources["task_instruction"] = inst_file.relative_to(resolved_repo_root).as_posix()
        except Exception as exc:
            coverage_notices.append(f"task_instruction: read_error ({exc})")
    else:
        coverage_notices.append(
            "task_instruction: absent (instruction.md not found in task directory)"
        )

    # -------------------------------------------------------------------------
    # 2. Trial Result & Outcome
    # -------------------------------------------------------------------------
    outcome: dict[str, Any] = {
        "status": "unknown",
        "primary_reward": None,
        "rewards": {},
        "exit_code": None,
        "error": None,
        "verifier_diagnostics": None,
    }
    task_id: str | None = None
    agent_name: str | None = None
    model_name: str | None = None

    result_file = _safe_child_file(resolved_trial_path, "result.json", label="trial result")
    result_data: dict[str, Any] = {}
    if result_file is not None and result_file.is_file():
        sources["trial_result"] = result_file.relative_to(resolved_repo_root).as_posix()
        try:
            loaded_res = json.loads(result_file.read_text(encoding="utf-8"))
            if isinstance(loaded_res, dict):
                result_data = loaded_res
        except Exception as exc:
            coverage_notices.append(f"trial_result: parse_error ({exc})")

    if result_data:
        raw_tid = result_data.get("task_name") or result_data.get("task_id")
        if isinstance(raw_tid, dict):
            task_id = str(raw_tid.get("path") or raw_tid.get("name") or resolved_task_path.name)
        elif raw_tid:
            task_id = str(raw_tid)
        else:
            task_id = resolved_task_path.name

        agent_info = (
            result_data.get("agent_info")
            if isinstance(result_data.get("agent_info"), dict)
            else {}
        )
        cfg_agent = (
            (result_data.get("config") or {}).get("agent")
            if isinstance(result_data.get("config"), dict)
            else {}
        )
        agent_name = agent_info.get("name") or (
            cfg_agent.get("name") if isinstance(cfg_agent, dict) else None
        )
        model_info = (
            agent_info.get("model_info") if isinstance(agent_info.get("model_info"), dict) else {}
        )
        model_name = model_info.get("name") or (
            cfg_agent.get("model") if isinstance(cfg_agent, dict) else None
        )

        verifier_result = (
            result_data.get("verifier_result")
            if isinstance(result_data.get("verifier_result"), dict)
            else {}
        )
        raw_rewards = (
            verifier_result.get("rewards")
            if isinstance(verifier_result.get("rewards"), dict)
            else {}
        )
        outcome["rewards"] = redact_payload(raw_rewards)

        if "reward" in raw_rewards and isinstance(raw_rewards["reward"], (int, float)):
            outcome["primary_reward"] = float(raw_rewards["reward"])

        raw_exc = result_data.get("exception_info") or result_data.get("error")
        if raw_exc:
            outcome["error"] = redact_payload(raw_exc)
            outcome["status"] = "error"
        elif outcome["primary_reward"] is not None:
            outcome["status"] = "completed"

        agent_result = (
            result_data.get("agent_result")
            if isinstance(result_data.get("agent_result"), dict)
            else {}
        )
        outcome["exit_code"] = agent_result.get("exit_code") or result_data.get("exit_code")
    else:
        task_id = resolved_task_path.name

    # Emitted verifier diagnostics (checks.json, test-stdout.txt)
    checks_file = _safe_child_file(
        resolved_trial_path, "verifier/checks.json", label="verifier checks"
    )
    if checks_file is not None and checks_file.is_file():
        sources["verifier_checks"] = checks_file.relative_to(resolved_repo_root).as_posix()
        try:
            checks_data = json.loads(checks_file.read_text(encoding="utf-8"))
            if isinstance(checks_data, dict):
                outcome["verifier_diagnostics"] = redact_payload(checks_data)
        except Exception:
            pass

    if outcome["verifier_diagnostics"] is None:
        stdout_file = _safe_child_file(
            resolved_trial_path, "verifier/test-stdout.txt", label="verifier stdout"
        )
        if stdout_file is not None and stdout_file.is_file():
            sources["verifier_stdout"] = stdout_file.relative_to(resolved_repo_root).as_posix()
            try:
                raw_stdout = stdout_file.read_text(encoding="utf-8").strip()
                if raw_stdout:
                    excerpt = raw_stdout[:1000]
                    outcome["verifier_diagnostics"] = redact_string(excerpt)
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # 3. Established Trajectory Reader (outline_trajectory)
    # -------------------------------------------------------------------------
    outline: TrajectoryOutline | None = None
    try:
        outline = outline_trajectory(resolved_trial_path, repo_root=resolved_repo_root)
    except Exception as exc:
        coverage_notices.append(f"outline_trajectory_notice: {exc}")

    if outline is not None:
        if agent_name is None and outline.agent_name != "unknown":
            agent_name = outline.agent_name
        if model_name is None and outline.model_name != "unknown":
            model_name = outline.model_name
        if outcome["primary_reward"] is None and outline.primary_reward is not None:
            outcome["primary_reward"] = outline.primary_reward
            if outcome["status"] == "unknown":
                outcome["status"] = "completed"
        if outcome["error"] is None and outline.exception_class:
            outcome["error"] = outline.exception_class

    traj_file = _safe_child_file(resolved_trial_path, "agent/trajectory.json", label="trajectory")
    if traj_file is None:
        traj_file = _safe_child_file(resolved_trial_path, "trajectory.json", label="trajectory")

    actions: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    final_response: str | None = None
    trace_status: str

    if (
        traj_file is None
        or not traj_file.is_file()
        or (outline is not None and outline.status == "accounted_unavailable")
    ):
        trace_status = "absent"
        reason = (
            outline.unavailable_reason
            if (outline and outline.unavailable_reason)
            else "missing_trajectory_file"
        )
        if agent_name in ("nop", "oracle"):
            coverage_notices.append(
                f"trajectory: absent (control agent '{agent_name}' has no model trace)"
            )
        else:
            coverage_notices.append(f"trajectory: absent ({reason})")
    else:
        trace_status = "present"
        sources["trial_trajectory"] = traj_file.relative_to(resolved_repo_root).as_posix()
        traj_data: dict[str, Any] = {}
        try:
            loaded_traj = json.loads(traj_file.read_text(encoding="utf-8"))
            if isinstance(loaded_traj, dict):
                traj_data = loaded_traj
            else:
                trace_status = "corrupt"
                coverage_notices.append(
                    "trajectory: invalid_shape (top-level JSON is not an object)"
                )
        except Exception as exc:
            trace_status = "corrupt"
            coverage_notices.append(f"trajectory: unparseable_json ({exc})")

        raw_steps = traj_data.get("steps")
        if isinstance(raw_steps, list):
            for raw_step in raw_steps:
                if not isinstance(raw_step, dict):
                    continue
                step_id = raw_step.get("step_id")
                source = str(raw_step.get("source") or "agent")
                raw_reasoning = raw_step.get("reasoning_content") or raw_step.get("thought")
                captured_reasoning = (
                    redact_string(str(raw_reasoning)) if raw_reasoning is not None else None
                )

                tool_calls = raw_step.get("tool_calls")
                calls = [c for c in tool_calls if isinstance(c, dict)] if isinstance(tool_calls, list) else []

                obs_obj = raw_step.get("observation")
                obs_results_raw = (
                    obs_obj.get("results")
                    if isinstance(obs_obj, dict)
                    else raw_step.get("observation_results") or raw_step.get("observations")
                )
                obs_results = (
                    [item for item in obs_results_raw if isinstance(item, dict)]
                    if isinstance(obs_results_raw, list)
                    else []
                )

                matched_call_ids: set[str] = set()

                for call in calls:
                    call_id = str(call.get("tool_call_id") or call.get("id") or "")
                    if call_id:
                        matched_call_ids.add(call_id)
                    fn_name = str(
                        (call.get("function") or {}).get("name")
                        if isinstance(call.get("function"), dict)
                        else call.get("function_name") or call.get("name") or "unknown"
                    )
                    args = (
                        (call.get("function") or {}).get("arguments")
                        if isinstance(call.get("function"), dict)
                        else call.get("arguments")
                    )
                    cmd_str = _extract_command_string(args)

                    matching_obs = None
                    if call_id:
                        for obs in obs_results:
                            if obs.get("source_call_id") == call_id:
                                matching_obs = obs
                                break

                    exit_code = None
                    obs_content = None
                    if matching_obs is not None:
                        extra = (
                            matching_obs.get("extra")
                            if isinstance(matching_obs.get("extra"), dict)
                            else {}
                        )
                        raw_exit = extra.get("exit_code") or matching_obs.get("command_exit_code")
                        if isinstance(raw_exit, int):
                            exit_code = raw_exit
                        raw_content = matching_obs.get("content")
                        if raw_content is not None:
                            text_repr = str(raw_content)
                            if len(text_repr) > _MAX_OBSERVATION_CHARS:
                                obs_content = (
                                    redact_string(text_repr[:_MAX_OBSERVATION_CHARS])
                                    + f"\n... [observation truncated to {_MAX_OBSERVATION_CHARS} chars]"
                                )
                            else:
                                obs_content = redact_string(text_repr)

                    is_error = bool(exit_code is not None and exit_code != 0)
                    action_entry = {
                        "step_id": step_id,
                        "source": source,
                        "tool_name": fn_name,
                        "tool_command": cmd_str,
                        "arguments": redact_payload(args),
                        "exit_code": exit_code,
                        "is_error": is_error,
                        "status": "error" if is_error else "ok",
                    }
                    if captured_reasoning is not None:
                        action_entry["reasoning_content"] = captured_reasoning
                    actions.append(action_entry)

                    if obs_content is not None:
                        observations.append(
                            {
                                "step_id": step_id,
                                "source_call_id": call_id or None,
                                "exit_code": exit_code,
                                "content": obs_content,
                            }
                        )

                # Unmatched observations
                for obs in obs_results:
                    call_ref = obs.get("source_call_id")
                    if call_ref and call_ref in matched_call_ids:
                        continue
                    raw_content = obs.get("content")
                    if raw_content is not None:
                        text_repr = str(raw_content)
                        if len(text_repr) > _MAX_OBSERVATION_CHARS:
                            bounded_content = (
                                redact_string(text_repr[:_MAX_OBSERVATION_CHARS])
                                + f"\n... [observation truncated to {_MAX_OBSERVATION_CHARS} chars]"
                            )
                        else:
                            bounded_content = redact_string(text_repr)
                        extra = obs.get("extra") if isinstance(obs.get("extra"), dict) else {}
                        raw_exit = extra.get("exit_code") or obs.get("command_exit_code")
                        observations.append(
                            {
                                "step_id": step_id,
                                "source_call_id": call_ref,
                                "exit_code": raw_exit if isinstance(raw_exit, int) else None,
                                "content": bounded_content,
                            }
                        )

            # Final response: last non-empty agent message
            for step in reversed(raw_steps):
                if isinstance(step, dict) and step.get("source") == "agent":
                    msg = step.get("message")
                    if msg and isinstance(msg, str) and msg.strip():
                        # Do not pick placeholder redaction markers as final response
                        if not msg.startswith("<<evallab-redacted:"):
                            final_response = redact_string(msg.strip())
                            break

    # -------------------------------------------------------------------------
    # 4. Render Structured Human/Model-Readable Feedback Text
    # -------------------------------------------------------------------------
    lines: list[str] = []
    lines.append(f"# Evaluation Feedback: {task_id or 'unknown-task'}")
    lines.append("")

    lines.append("## Task Instruction")
    if task_instruction:
        lines.append(task_instruction.strip())
    else:
        lines.append("Task instruction absent (instruction.md not found in declared task directory).")
    lines.append("")

    lines.append("## Outcome Feedback")
    lines.append(f"- Status: {outcome['status']}")
    lines.append(
        f"- Primary Reward: {f'{outcome[\"primary_reward\"]:.4f}' if isinstance(outcome['primary_reward'], float) else outcome['primary_reward']}"
    )
    if outcome["rewards"]:
        lines.append(f"- Reward Dimensions: {json.dumps(outcome['rewards'], sort_keys=True)}")
    if outcome["exit_code"] is not None:
        lines.append(f"- Process Exit Code: {outcome['exit_code']}")
    if outcome["error"]:
        lines.append(f"- Error: {outcome['error']}")
    if outcome["verifier_diagnostics"] is not None:
        diag_str = (
            json.dumps(outcome["verifier_diagnostics"], sort_keys=True)
            if isinstance(outcome["verifier_diagnostics"], (dict, list))
            else str(outcome["verifier_diagnostics"])
        )
        lines.append(f"- Verifier Diagnostics: {diag_str}")
    lines.append("")

    lines.append("## Agent Execution Trace")
    if trace_status == "absent":
        lines.append(f"Trace Status: absent ({'; '.join(coverage_notices) or 'no trajectory recorded'})")
        lines.append("No agent model trajectory was recorded for this trial.")
    else:
        lines.append(
            f"Trace Status: {trace_status} (actions={len(actions)}, observations={len(observations)})"
        )
        lines.append("")
        for act in actions:
            step_id = act["step_id"]
            t_name = act["tool_name"]
            cmd = act["tool_command"] or (
                json.dumps(act["arguments"], sort_keys=True) if act["arguments"] else ""
            )
            exit_txt = f" [exit={act['exit_code']}]" if act["exit_code"] is not None else ""
            lines.append(f"- Step {step_id}: {t_name}: {cmd}{exit_txt}".strip())
            if act.get("reasoning_content"):
                lines.append(f"  Reasoning: {act['reasoning_content']}")
            matching_obs = [o for o in observations if o["step_id"] == step_id]
            for o in matching_obs:
                lines.append(f"  Observation: {o['content']}")
        if final_response:
            lines.append("")
            lines.append(f"Final Response:\n{final_response}")
    lines.append("")

    lines.append("## Coverage & Provenance")
    for src_name, src_path in sorted(sources.items()):
        lines.append(f"- Source [{src_name}]: {src_path}")
    if coverage_notices:
        lines.append("- Coverage Notices:")
        for notice in coverage_notices:
            lines.append(f"  * {notice}")

    full_text = "\n".join(lines).strip() + "\n"

    # -------------------------------------------------------------------------
    # 5. Honest Budget Truncation against max_chars
    # -------------------------------------------------------------------------
    truncated = False
    if len(full_text) <= max_chars:
        final_text = full_text
    else:
        truncated = True
        trunc_notice = (
            f"\n\n[Truncated: feedback exceeded max_chars budget of {max_chars} characters]"
        )
        coverage_notices.append(f"feedback_text truncated to max_chars={max_chars} budget")
        if max_chars <= len(trunc_notice):
            final_text = full_text[:max_chars]
        else:
            allowed_len = max_chars - len(trunc_notice)
            final_text = full_text[:allowed_len] + trunc_notice

    return {
        "feedback": final_text,
        "feedback_text": final_text,
        "text": final_text,
        "task_id": task_id,
        "task_path": task_rel_path,
        "trial_path": trial_rel_path,
        "task_instruction": task_instruction,
        "trace_status": trace_status,
        "actions": actions,
        "observations": observations,
        "final_response": final_response,
        "outcome": outcome,
        "sources": sources,
        "coverage_notices": coverage_notices,
        "truncated": truncated,
        "char_count": len(final_text),
        "max_chars": max_chars,
    }
