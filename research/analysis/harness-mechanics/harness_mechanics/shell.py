"""Shell mechanics diagnostic module for Harness Mechanics Lab.

Inspects TrajectoryIR for explicit shell reset/timeout evidence and
conservative review hypotheses around repeated identical failed commands or
cross-call cwd assumptions.

Diagnostic constraints:
- Detect explicit shell-reset/timeout evidence.
- Repeated identical commands and cross-call cwd dependence are at most review
  hypotheses absent state instrumentation.
- Never execute command text or use substring presence to assert shell semantics.
- Do not treat Python/RLM calls as bash.
- Non-shell tools are not shell evidence.
- No permanent claim that an environment is persistent/stateless from its name.
- Count identical commands by secure digest internally and report no command text.
- If robust cwd-dependence identification needs unsupported shell parsing,
  leave it unknown instead of overclaiming.
- Distinguish tool/environment errors from agent incapability.
"""

from __future__ import annotations

import hashlib
from typing import Any

from evallab.trajectory_ir import ObservationResultRecord, ToolCallRecord, TrajectoryIR

# Recognized shell tool names (case-insensitive).
# Tools that run Python, RLM, MCP, browser, or file operations are strictly excluded.
_SHELL_TOOL_NAMES = frozenset(
    {
        "bash",
        "sh",
        "shell",
        "terminal",
        "exec_command",
        "execute_command",
        "run_bash",
        "run_command",
    }
)

# Explicitly excluded non-shell tool prefixes/names to avoid misclassification.
_NON_SHELL_EXCLUSIONS = frozenset(
    {
        "python",
        "python3",
        "ipython",
        "py_eval",
        "eval",
        "run_python",
        "execute_python",
        "rlm",
        "rlm_eval",
        "read",
        "write",
        "edit",
        "view",
        "str_replace_editor",
    }
)


def _is_shell_tool(function_name: str) -> bool:
    """Determine whether a tool function name corresponds to a shell tool."""
    name = function_name.strip().lower()
    if not name:
        return False
    if name in _NON_SHELL_EXCLUSIONS or name.startswith("mcp-") or name.startswith("mcp_"):
        return False
    return name in _SHELL_TOOL_NAMES


def _rfc6901_escape(segment: str | int) -> str:
    """Escape a JSON pointer token according to RFC 6901."""
    return str(segment).replace("~", "~0").replace("/", "~1")


def _extract_command_digest(arguments: dict[str, Any]) -> str | None:
    """Compute a SHA256 digest of the command argument without exposing command text."""
    cmd: Any = None
    for key in (
        "command",
        "cmd",
        "input",
        "CommandLine",
        "command_line",
        "commandline",
        "script",
    ):
        if key in arguments and isinstance(arguments[key], str):
            cmd = arguments[key]
            break

    if cmd is None:
        arg_lower = {k.lower(): v for k, v in arguments.items() if isinstance(v, str)}
        for key in ("command", "cmd", "input", "commandline", "command_line", "script"):
            if key in arg_lower:
                cmd = arg_lower[key]
                break

    if cmd is None:
        return None

    cmd_str = cmd.strip()
    if not cmd_str:
        return None
    return hashlib.sha256(cmd_str.encode("utf-8")).hexdigest()


def _is_environment_error(obs_extra: dict[str, Any], content: Any) -> bool:
    """Distinguish harness/environment infrastructure errors from agent command failures.

    Do not conflate ordinary command exit errors (like non-zero exits or tool-level network
    errors from inside the container) with infrastructure diagnosis.
    """
    if obs_extra.get("harness_error") is True:
        return True
    if obs_extra.get("infrastructure_error") is True:
        return True
    if obs_extra.get("runner_error") is True:
        return True
    if obs_extra.get("sandbox_error") is True:
        return True

    err_type = str(obs_extra.get("error_type") or "").strip().lower()
    known_infra_errors = (
        "sandboxunavailableerror",
        "containerdiederror",
        "harnesserror",
        "dockererror",
        "runnerconnectionerror",
        "runnererror",
    )
    if err_type in known_infra_errors:
        return True

    if isinstance(content, dict):
        c_err_type = str(content.get("error_type") or "").strip().lower()
        if c_err_type in known_infra_errors:
            return True
        if content.get("harness_error") is True or content.get("infrastructure_error") is True:
            return True

    return False


def _check_timeout(
    tc: ToolCallRecord,
    obs: ObservationResultRecord | None,
    step_id: int,
    step_idx: int,
    tc_idx: int,
    obs_step_idx: int | None,
    obs_idx: int | None,
) -> dict[str, Any] | None:
    """Detect explicit timeout markers or conditional exit code 124 timeout hypotheses.

    Actual explicit timeout requires retained runtime metadata.
    Bare exit code 124 without runtime confirmation is reported as a conditional hypothesis.
    """
    # 1. Explicit timeout in observation extra metadata
    if (
        obs is not None
        and isinstance(obs.extra, dict)
        and obs_step_idx is not None
        and obs_idx is not None
    ):
        if obs.extra.get("timed_out") is True or obs.extra.get("timeout") is True:
            field_name = "timed_out" if "timed_out" in obs.extra else "timeout"
            return {
                "code": "SHELL_TIMEOUT_EXPLICIT",
                "step_id": step_id,
                "tool_call_id": tc.tool_call_id or None,
                "locator": f"/steps/{obs_step_idx}/observation/results/{obs_idx}/extra/{field_name}",
                "evidence_kind": "observed",
                "summary": "Shell command execution timed out according to explicit observation metadata.",
            }

        status = str(obs.extra.get("status") or "").strip().lower()
        if status in ("timeout", "timed_out"):
            return {
                "code": "SHELL_TIMEOUT_EXPLICIT",
                "step_id": step_id,
                "tool_call_id": tc.tool_call_id or None,
                "locator": f"/steps/{obs_step_idx}/observation/results/{obs_idx}/extra/status",
                "evidence_kind": "observed",
                "summary": "Shell tool status recorded as timeout in observation metadata.",
            }

    # 2. Explicit timeout in tool call extra metadata
    if isinstance(tc.extra, dict):
        status = str(tc.extra.get("status") or "").strip().lower()
        if status in ("timeout", "timed_out"):
            return {
                "code": "SHELL_TIMEOUT_EXPLICIT",
                "step_id": step_id,
                "tool_call_id": tc.tool_call_id or None,
                "locator": f"/steps/{step_idx}/tool_calls/{tc_idx}/extra/status",
                "evidence_kind": "observed",
                "summary": "Tool call extra metadata explicitly recorded timeout status.",
            }

    # 3. Bare exit code 124 without explicit runtime metadata -> conditional hypothesis
    if (
        obs is not None
        and isinstance(obs.extra, dict)
        and obs_step_idx is not None
        and obs_idx is not None
    ):
        exit_code = obs.extra.get("exit_code")
        exit_field = "exit_code"
        if exit_code is None and "returncode" in obs.extra:
            exit_code = obs.extra.get("returncode")
            exit_field = "returncode"

        if exit_code == 124:
            return {
                "code": "SHELL_TIMEOUT_HYPOTHESIS",
                "step_id": step_id,
                "tool_call_id": tc.tool_call_id or None,
                "locator": f"/steps/{obs_step_idx}/observation/results/{obs_idx}/extra/{exit_field}",
                "evidence_kind": "hypothesis",
                "summary": "Shell command returned exit code 124; conditional hypothesis for timeout without explicit runtime confirmation.",
            }

    return None


def _check_reset(
    tc: ToolCallRecord,
    obs: ObservationResultRecord | None,
    step_id: int,
    step_idx: int,
    tc_idx: int,
    obs_step_idx: int | None,
    obs_idx: int | None,
) -> list[dict[str, Any]]:
    """Detect explicit reset requests in tool call arguments and completed resets in observation metadata.

    Tool call arguments record requested resets (SHELL_RESET_REQUESTED).
    Observation metadata records completed resets (SHELL_RESET_COMPLETED) when not a command error.
    """
    results: list[dict[str, Any]] = []

    # A. Reset requested in tool call arguments
    if isinstance(tc.arguments, dict):
        for reset_key in ("reset", "restart", "session_reset"):
            if tc.arguments.get(reset_key) is True:
                results.append(
                    {
                        "code": "SHELL_RESET_REQUESTED",
                        "step_id": step_id,
                        "tool_call_id": tc.tool_call_id or None,
                        "locator": f"/steps/{step_idx}/tool_calls/{tc_idx}/arguments/{_rfc6901_escape(reset_key)}",
                        "evidence_kind": "observed",
                        "summary": f"Tool call requested shell reset via '{reset_key}' parameter.",
                    }
                )
                break

    # B. Reset completed in observation extra metadata
    if (
        obs is not None
        and isinstance(obs.extra, dict)
        and obs_step_idx is not None
        and obs_idx is not None
    ):
        for reset_key in ("shell_reset", "session_reset", "restarted", "reset"):
            if obs.extra.get(reset_key) is True:
                # If the command exited with an error code, reset request failed rather than completed
                exit_c = obs.extra.get("exit_code")
                if exit_c is None:
                    exit_c = obs.extra.get("returncode")
                if isinstance(exit_c, int) and exit_c != 0:
                    continue
                results.append(
                    {
                        "code": "SHELL_RESET_COMPLETED",
                        "step_id": step_id,
                        "tool_call_id": tc.tool_call_id or None,
                        "locator": f"/steps/{obs_step_idx}/observation/results/{obs_idx}/extra/{_rfc6901_escape(reset_key)}",
                        "evidence_kind": "observed",
                        "summary": f"Observation metadata recorded explicit shell reset completion in '{reset_key}'.",
                    }
                )
                break

    return results


def inspect(ir: TrajectoryIR) -> dict[str, Any]:
    """Inspect TrajectoryIR for shell reset, timeout, and failure mechanics.

    Returns:
        dict with keys:
            - mechanism: 'shell'
            - observations: list of observation dicts (code, step_id, tool_call_id,
              locator, evidence_kind, summary)
            - unknowns: list of str explaining unobservable or missing state
    """
    observations: list[dict[str, Any]] = []
    unknowns: list[str] = []

    # Index observations by source_call_id across all steps: call_id -> list of (obs, step_idx, obs_idx)
    obs_by_call_id: dict[str, list[tuple[ObservationResultRecord, int, int]]] = {}
    for s_idx, step in enumerate(ir.steps):
        for o_idx, obs in enumerate(step.observation_results):
            if obs.source_call_id:
                obs_by_call_id.setdefault(obs.source_call_id, []).append((obs, s_idx, o_idx))

    # Track tool call id frequencies to detect ambiguous tool calls
    tc_id_counts: dict[str, int] = {}
    for step in ir.steps:
        for tc in step.tool_calls:
            if tc.tool_call_id:
                tc_id_counts[tc.tool_call_id] = tc_id_counts.get(tc.tool_call_id, 0) + 1

    shell_tool_call_count = 0
    missing_exit_codes = 0
    tracked_cwd_count = 0

    # Track command failures by secure digest: digest -> list of (step_id, tc_id, tc_idx)
    failed_command_history: dict[str, list[tuple[int, str | None, int]]] = {}

    for step_idx, step in enumerate(ir.steps):
        for tc_idx, tc in enumerate(step.tool_calls):
            if not _is_shell_tool(tc.function_name):
                continue

            shell_tool_call_count += 1

            # Match observation strictly via source_call_id; never fallback-pair mismatching IDs
            obs: ObservationResultRecord | None = None
            obs_step_idx: int | None = None
            obs_idx: int | None = None

            if tc.tool_call_id:
                if tc_id_counts.get(tc.tool_call_id, 0) > 1:
                    unknowns.append(
                        f"shell_ambiguous_tool_call_id: Multiple tool calls share tool_call_id '{tc.tool_call_id}'; linkage ambiguous."
                    )
                else:
                    candidates = obs_by_call_id.get(tc.tool_call_id, [])
                    if len(candidates) == 1:
                        obs, obs_step_idx, obs_idx = candidates[0]
                    elif len(candidates) > 1:
                        unknowns.append(
                            f"shell_ambiguous_source_call_id: Multiple observations share source_call_id '{tc.tool_call_id}'; linkage ambiguous."
                        )
                    # If 0 candidates, obs remains None (strictly no positional fallback across mismatching IDs)
            else:
                # Positional pairing permitted ONLY if both tool call and observation omit call ID
                # and exactly 1 tool call and 1 observation exist in the current step
                if len(step.tool_calls) == 1 and len(step.observation_results) == 1:
                    cand = step.observation_results[0]
                    if not cand.source_call_id:
                        obs = cand
                        obs_step_idx = step_idx
                        obs_idx = 0

            # 1. Check timeout (explicit observed or exit 124 hypothesis)
            timeout_obs = _check_timeout(
                tc, obs, step.step_id, step_idx, tc_idx, obs_step_idx, obs_idx
            )
            if timeout_obs is not None:
                observations.append(timeout_obs)

            # 2. Check reset (requested in arguments and/or completed in observation metadata)
            reset_obs_list = _check_reset(
                tc, obs, step.step_id, step_idx, tc_idx, obs_step_idx, obs_idx
            )
            observations.extend(reset_obs_list)

            has_cwd = False
            if isinstance(tc.arguments, dict) and any(
                k.lower() in ("cwd", "workdir", "working_dir", "workingdirectory")
                for k in tc.arguments
            ):
                has_cwd = True
            if (
                obs is not None
                and isinstance(obs.extra, dict)
                and any(
                    k.lower() in ("cwd", "workdir", "working_dir", "workingdirectory")
                    for k in obs.extra
                )
            ):
                has_cwd = True
            if has_cwd:
                tracked_cwd_count += 1

            # 4. Check command execution failure vs environment error
            cmd_digest = (
                _extract_command_digest(tc.arguments) if isinstance(tc.arguments, dict) else None
            )

            if obs is None:
                missing_exit_codes += 1
                continue

            obs_extra = obs.extra if isinstance(obs.extra, dict) else {}
            if "exit_code" not in obs_extra and "returncode" not in obs_extra:
                missing_exit_codes += 1
                continue

            exit_code = obs_extra.get("exit_code")
            if exit_code is None:
                exit_code = obs_extra.get("returncode")

            # Distinguish tool/environment error from command failure
            if _is_environment_error(obs_extra, obs.content):
                continue

            # Check if this command failed (non-zero exit code)
            # Skip if explicitly observed timeout, otherwise record failed command
            is_explicit_timeout = (
                timeout_obs is not None and timeout_obs["code"] == "SHELL_TIMEOUT_EXPLICIT"
            )
            if (
                isinstance(exit_code, int)
                and exit_code != 0
                and not is_explicit_timeout
                and cmd_digest is not None
            ):
                history = failed_command_history.setdefault(cmd_digest, [])
                history.append((step.step_id, tc.tool_call_id, tc_idx))

                # If this identical command failed previously, record a review hypothesis
                if len(history) >= 2:
                    observations.append(
                        {
                            "code": "SHELL_REPEATED_FAILED_COMMAND_HYPOTHESIS",
                            "step_id": step.step_id,
                            "tool_call_id": tc.tool_call_id or None,
                            "locator": f"/steps/{step_idx}/tool_calls/{tc_idx}/arguments",
                            "evidence_kind": "hypothesis",
                            "summary": (
                                f"Identical shell command digest failed repeatedly ({len(history)} attempts "
                                f"with non-zero exit code); conservative review hypothesis for unadapted failure loop."
                            ),
                        }
                    )
    if shell_tool_call_count == 0:
        unknowns.append(
            "shell_activity_absent: No shell tool invocations (e.g. bash, sh, terminal) were present in this trajectory."
        )
    else:
        # Check environment persistence
        # Never infer persistence from agent name or environment name
        agent_extra = ir.agent_extra if isinstance(ir.agent_extra, dict) else {}
        ir_extra = ir.extra if isinstance(ir.extra, dict) else {}
        has_persistence_meta = (
            "shell_persistence" in agent_extra
            or "shell_persistence" in ir_extra
            or "persistent_shell" in agent_extra
            or "persistent_shell" in ir_extra
        )
        if not has_persistence_meta:
            unknowns.append(
                "shell_session_persistence: Shell state persistence across tool calls is uninstrumented in retained trajectory metadata; persistence cannot be inferred from agent name."
            )

        # Cross-call CWD state unknown
        if tracked_cwd_count < shell_tool_call_count:
            unknowns.append(
                "shell_cross_call_cwd: Cross-call working directory persistence cannot be determined without environment state instrumentation; arbitrary shell command parsing is intentionally omitted."
            )

        # Missing exit code fidelity
        if missing_exit_codes > 0:
            unknowns.append(
                f"shell_exit_code_fidelity: {missing_exit_codes} of {shell_tool_call_count} shell tool observation(s) lack retained exit codes or execution status; cannot distinguish command failure from tool/environment error."
            )

        # Environment configuration
        unknowns.append(
            "shell_environment_state: Shell environment variables, ulimits, and execution shell flags (-e, -u, pipefail) are not recorded in trajectory metadata."
        )

    has_generic_exec_call = any(
        tc.function_name.strip().lower() == "exec" for step in ir.steps for tc in step.tool_calls
    )
    if has_generic_exec_call:
        unknowns.append(
            "shell_exec_tool_scope: Trajectory contains generic 'exec' tool calls with unverified script or evaluator shapes; execution invocations are not assumed to have shell semantics without source-supported shell schemas."
        )
    return {
        "mechanism": "shell",
        "observations": observations,
        "unknowns": unknowns,
    }
