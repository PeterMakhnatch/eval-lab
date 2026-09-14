"""Feedback builder for GEPA prompt optimizer and reflection.

Builds bounded, real, agent-visible feedback from task instructions,
trial trajectories (via normalized TrajectoryIR steps), logs, and emitted
verifier diagnostics. Enforces path jails, central redaction semantics,
and character limits without unbounded duplicates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.explorer import redact_text
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

    Returns:
        Dictionary with single bounded 'feedback' string, sources, coverage_notices,
        truncated flag, char_count, and max_chars.

    Raises:
        ValueError: If paths escape the repo root, target forbidden directories, or symlink out.
        FileNotFoundError: If task_path or trial_path directory does not exist.
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

    agent_info = (
        result_data.get("agent_info") if isinstance(result_data.get("agent_info"), dict) else {}
    )
    cfg_agent = (
        (result_data.get("config") or {}).get("agent")
        if isinstance(result_data.get("config"), dict)
        else {}
    )
    agent_name = agent_info.get("name") or (
        cfg_agent.get("name") if isinstance(cfg_agent, dict) else None
    )

    verifier_result = (
        result_data.get("verifier_result")
        if isinstance(result_data.get("verifier_result"), dict)
        else {}
    )
    rewards = (
        verifier_result.get("rewards") if isinstance(verifier_result.get("rewards"), dict) else {}
    )
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

    agent_result = (
        result_data.get("agent_result") if isinstance(result_data.get("agent_result"), dict) else {}
    )
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
                                if len(obs_text) > _MAX_OBSERVATION_CHARS:
                                    obs_text = (
                                        obs_text[:_MAX_OBSERVATION_CHARS]
                                        + f"\n... [observation truncated to {_MAX_OBSERVATION_CHARS} chars]"
                                    )
                                actions_summary.append(f"  Observation: {obs_text}")
                if step.reasoning_content:
                    actions_summary.append(f"  Reasoning: {step.reasoning_content}")

            for step in reversed(ir.steps):
                if step.source == "agent":
                    msg = str(step.message).strip() if step.message else ""
                    if msg and not msg.startswith("<<evallab-redacted:"):
                        final_response = msg
                        break

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
