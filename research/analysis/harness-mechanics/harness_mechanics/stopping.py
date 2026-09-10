"""Stopping mechanics diagnostic for Harbor and ATIF trajectories.

Observes explicitly retained termination and finish reasons, max-token/time/step limits,
final assistant structure, and submission markers.

Scientific and architectural principles:
- Conservative observational facts only (evidence_kind='observed'|'hypothesis').
- A no-tool final assistant turn is a neutral observed structure, NEVER proof of
  premature stopping or task failure.
- Command-level timeouts (individual tool execution) are strictly separated from
  episode wall-clock timeouts. Bare exit code 124 without explicit timeout flag
  is at most a command timeout hypothesis.
- Agent completion (submitting or concluding turns) is strictly separated from
  external verifier success or reward. Never claim task success from arbitrary metadata.
- Root agent scope and subagent scope are never conflated.
- Exact field locators are RFC6901 JSON pointers into original raw trajectory JSON
  (/steps/0/observation/results/0/extra/timed_out), using zero-based array positions,
  preserving recorded step_id separately.
- No raw prompt, reasoning, command, or output text is leaked into reports.
  Raw finish and termination values containing private content are sanitized and
  never echoed verbatim.
- Non-agent, empty, and captured-context structures are treated conservatively.
  Trajectories with only system/user steps are classified as STOPPING_NO_AGENT_STEPS_RECORDED
  (evidence availability/structure observation, not an agent decision or proof of zero model calls).
- Step counts and record counts are never conflated with model rounds. Inferred LIMIT_REACHED
  observations from record counts or token totals are prohibited; limits are observed as
  configured bounds neutrally, with actual exhaustion classified as unknown unless explicit
  termination metadata establishes it.
- Missing telemetry and outcome evidence are recorded as unknowns, never fabricated.
"""

from __future__ import annotations

from typing import Any

from evallab.trajectory_ir import TrajectoryIR

SUBMISSION_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "submit",
        "finish",
        "complete",
        "complete_task",
        "submit_answer",
        "submit_solution",
        "done",
    }
)

VERIFIER_OUTCOME_KEYS: tuple[str, ...] = (
    "reward",
    "score",
    "verifier_result",
    "passed",
    "success",
)

TERMINATION_REASON_KEYS: tuple[str, ...] = (
    "termination_reason",
    "stop_reason",
    "exit_reason",
    "exit_status",
    "finish_reason",
)

TIMEOUT_CONFIG_KEYS: tuple[str, ...] = (
    "timeout_seconds",
    "timeout_sec",
    "timeout",
)

STEP_LIMIT_KEYS: tuple[str, ...] = (
    "max_steps",
    "step_limit",
    "max_turns",
    "max_iterations",
)

VERIFIER_STANDARD_TOKENS: frozenset[str] = frozenset(
    {
        "passed",
        "failed",
        "pass",
        "fail",
        "success",
        "error",
        "timeout",
        "true",
        "false",
        "none",
    }
)

SUBMISSION_STANDARD_TOKENS: frozenset[str] = frozenset(
    {
        "submitted",
        "done",
        "completed",
        "complete",
        "finished",
        "success",
    }
)

TERMINATION_STANDARD_TOKENS: frozenset[str] = frozenset(
    {
        "timeout",
        "trial_wall_clock_timeout",
        "wall_clock_timeout",
        "max_steps",
        "step_limit",
        "max_iterations",
        "max_turns",
        "completed",
        "submitted",
        "done",
        "agent_stop",
        "stop",
        "complete",
        "max_tokens",
        "token_limit",
        "context_length_exceeded",
        "length",
        "error",
        "cancelled",
    }
)


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    """Safely extract field from dict or object attribute."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    val = getattr(obj, key, default)
    return val if val is not None else default


def inspect(ir: TrajectoryIR) -> dict[str, Any]:
    """Inspect a TrajectoryIR for stopping mechanics, limits, and completion structures.

    Parameters
    ----------
    ir : TrajectoryIR
        The canonical intermediate representation of a trajectory.

    Returns
    -------
    dict[str, Any]
        Diagnostic dictionary containing:
        - 'mechanism': 'stopping'
        - 'observations': list of observation dicts, each with keys:
          'code', 'step_id', 'tool_call_id', 'locator', 'evidence_kind', 'summary'
        - 'unknowns': list of strings detailing absent evidence or telemetry
    """
    observations: list[dict[str, Any]] = []
    unknowns: list[str] = []

    steps = ir.steps if ir.steps is not None else ()
    agent_extra = ir.agent_extra if isinstance(ir.agent_extra, dict) else {}
    extra = ir.extra if isinstance(ir.extra, dict) else {}
    final_metrics_extra = (
        ir.final_metrics.extra
        if ir.final_metrics and isinstance(ir.final_metrics.extra, dict)
        else {}
    )

    # -------------------------------------------------------------------------
    # 1. Trajectory step structure (empty vs non-agent vs final assistant structure)
    # -------------------------------------------------------------------------
    if len(steps) == 0:
        observations.append(
            {
                "code": "STOPPING_EMPTY_TRAJECTORY",
                "step_id": None,
                "tool_call_id": None,
                "locator": "/steps",
                "evidence_kind": "observed",
                "summary": (
                    "Trajectory contains zero recorded steps "
                    "(neutral observation; not proof of harness failure)."
                ),
            }
        )
        unknowns.append(
            "No trajectory steps available to observe stopping structure or tool calls."
        )
    else:
        has_agent_step = any(_get_field(s, "source", "") == "agent" for s in steps)
        if not has_agent_step:
            observations.append(
                {
                    "code": "STOPPING_NO_AGENT_STEPS_RECORDED",
                    "step_id": None,
                    "tool_call_id": None,
                    "locator": "/steps",
                    "evidence_kind": "observed",
                    "summary": (
                        "Trajectory retains only system/user/non-agent steps; no agent turns recorded "
                        "(evidence availability/structure observation, not an agent decision or proof "
                        "that zero model calls occurred)."
                    ),
                }
            )
            unknowns.append(
                "No agent steps recorded in trajectory (uninstrumented pre-execution boundary, "
                "aborted run, or unrecorded model interactions)."
            )

        last_idx = len(steps) - 1
        last_step = steps[last_idx]
        step_source = _get_field(last_step, "source", "unknown")
        step_tool_calls = _get_field(last_step, "tool_calls", ()) or ()
        step_id = _get_field(last_step, "step_id", None)

        if step_source == "agent":
            if len(step_tool_calls) == 0:
                observations.append(
                    {
                        "code": "STOPPING_FINAL_ASSISTANT_NO_TOOL",
                        "step_id": step_id,
                        "tool_call_id": None,
                        "locator": f"/steps/{last_idx}",
                        "evidence_kind": "observed",
                        "summary": (
                            "Final agent turn contains no tool calls (neutral observed structure; "
                            "does not indicate premature stopping or task failure)."
                        ),
                    }
                )
            else:
                first_tc = step_tool_calls[0]
                primary_call = _get_field(first_tc, "tool_call_id", None)
                observations.append(
                    {
                        "code": "STOPPING_FINAL_ASSISTANT_WITH_TOOL_CALLS",
                        "step_id": step_id,
                        "tool_call_id": primary_call,
                        "locator": f"/steps/{last_idx}/tool_calls",
                        "evidence_kind": "observed",
                        "summary": (
                            f"Final agent turn emitted {len(step_tool_calls)} tool call(s) "
                            "without subsequent observation in trajectory."
                        ),
                    }
                )
        elif has_agent_step:
            safe_source = step_source if step_source in ("user", "system", "tool") else "non-agent"
            observations.append(
                {
                    "code": "STOPPING_TRAILING_NON_AGENT_STEP",
                    "step_id": step_id,
                    "tool_call_id": None,
                    "locator": f"/steps/{last_idx}/source",
                    "evidence_kind": "observed",
                    "summary": (
                        f"Trajectory terminates on non-agent step source '{safe_source}' "
                        "without subsequent agent turn."
                    ),
                }
            )

    # -------------------------------------------------------------------------
    # 2. Step-level finish reasons, limits, timeouts, and submission tool calls
    # -------------------------------------------------------------------------
    step_finish_reason_observed = False
    max_tokens_configured_observed = False

    for s_idx, step in enumerate(steps):
        s_id = _get_field(step, "step_id", None)
        s_extra = _get_field(step, "extra", {}) or {}
        s_metrics = _get_field(step, "metrics", None)
        s_sampling = _get_field(step, "sampling_params", None)

        # 2a. Captured context boundary
        if _get_field(step, "is_copied_context", None) is True:
            observations.append(
                {
                    "code": "STOPPING_CAPTURED_CONTEXT_OBSERVED",
                    "step_id": s_id,
                    "tool_call_id": None,
                    "locator": f"/steps/{s_idx}/is_copied_context",
                    "evidence_kind": "observed",
                    "summary": f"Step {s_id} is marked as copied or captured context boundary.",
                }
            )

        # 2b. Finish reason
        finish_reason: Any = None
        finish_locator: str | None = None

        if isinstance(s_extra, dict) and "finish_reason" in s_extra:
            finish_reason = s_extra.get("finish_reason")
            finish_locator = f"/steps/{s_idx}/extra/finish_reason"
        elif isinstance(s_extra, dict) and "stop_reason" in s_extra:
            finish_reason = s_extra.get("stop_reason")
            finish_locator = f"/steps/{s_idx}/extra/stop_reason"
        elif s_metrics is not None:
            m_extra = _get_field(s_metrics, "extra", {}) or {}
            if isinstance(m_extra, dict) and "finish_reason" in m_extra:
                finish_reason = m_extra.get("finish_reason")
                finish_locator = f"/steps/{s_idx}/metrics/extra/finish_reason"
            elif isinstance(m_extra, dict) and "stop_reason" in m_extra:
                finish_reason = m_extra.get("stop_reason")
                finish_locator = f"/steps/{s_idx}/metrics/extra/stop_reason"
        elif s_sampling is not None:
            samp_extra = _get_field(s_sampling, "extra", {}) or {}
            if isinstance(samp_extra, dict) and "finish_reason" in samp_extra:
                finish_reason = samp_extra.get("finish_reason")
                finish_locator = f"/steps/{s_idx}/sampling_params/extra/finish_reason"

        if finish_reason is not None and finish_locator is not None:
            step_finish_reason_observed = True
            raw_str = str(finish_reason).strip()
            norm = raw_str.lower()

            if norm in ("length", "max_tokens", "token_limit"):
                code = "STOPPING_STEP_FINISH_REASON_LENGTH"
                summary = f"Step {s_id} LLM generation halted by token limit."
            elif norm in ("stop", "end_turn", "stop_sequence"):
                code = "STOPPING_STEP_FINISH_REASON_STOP"
                summary = f"Step {s_id} LLM generation completed with normal stop."
            elif norm in ("tool_calls", "function_call", "tool_use"):
                code = "STOPPING_STEP_FINISH_REASON_TOOL_CALLS"
                summary = f"Step {s_id} LLM generation concluded with tool invocation request."
            elif norm in ("content_filter", "safety"):
                code = "STOPPING_STEP_FINISH_REASON_CONTENT_FILTER"
                summary = f"Step {s_id} LLM generation halted by safety/content filter."
            else:
                code = "STOPPING_STEP_FINISH_REASON_EXPLICIT"
                summary = f"Step {s_id} LLM generation recorded explicit finish reason."

            observations.append(
                {
                    "code": code,
                    "step_id": s_id,
                    "tool_call_id": None,
                    "locator": finish_locator,
                    "evidence_kind": "observed",
                    "summary": summary,
                }
            )

        # 2c. Max tokens limit configuration
        step_max_tokens: Any = None
        max_tokens_locator: str | None = None
        if s_sampling is not None:
            samp_max_tok = _get_field(s_sampling, "max_tokens", None)
            if samp_max_tok is not None:
                step_max_tokens = samp_max_tok
                max_tokens_locator = f"/steps/{s_idx}/sampling_params/max_tokens"
        elif "max_tokens" in agent_extra:
            step_max_tokens = agent_extra.get("max_tokens")
            max_tokens_locator = "/agent/extra/max_tokens"
        elif "max_tokens" in extra:
            step_max_tokens = extra.get("max_tokens")
            max_tokens_locator = "/extra/max_tokens"

        if step_max_tokens is not None and max_tokens_locator is not None:
            if not max_tokens_configured_observed:
                max_tokens_configured_observed = True
                observations.append(
                    {
                        "code": "STOPPING_TOKEN_LIMIT_CONFIGURED",
                        "step_id": s_id if max_tokens_locator.startswith("/steps/") else None,
                        "tool_call_id": None,
                        "locator": max_tokens_locator,
                        "evidence_kind": "observed",
                        "summary": f"Sampling max_tokens limit configured at {step_max_tokens}.",
                    }
                )

            # Token totals meeting a bound are not proof of truncation/termination.
            # Truncation requires explicit finish_reason (e.g. 'length' / 'max_tokens').
            if s_metrics is not None:
                comp_tokens = _get_field(s_metrics, "completion_tokens", None)
                if (
                    comp_tokens is not None
                    and isinstance(step_max_tokens, (int, float))
                    and comp_tokens >= step_max_tokens
                    and not (
                        finish_reason
                        and str(finish_reason).strip().lower()
                        in ("length", "max_tokens", "token_limit")
                    )
                ):
                    unknowns.append(
                        f"Step {s_id} completion tokens ({comp_tokens}) met configured bound "
                        f"({step_max_tokens}), but token limit truncation is unconfirmed without explicit finish_reason."
                    )

        # 2d. Command timeout in tool observations (strictly separate from episode timeout)
        # Using actual observation owner step index for cross-step JSON pointer locator.
        obs_results = _get_field(step, "observation_results", ()) or ()
        for obs_idx, obs in enumerate(obs_results):
            obs_extra = _get_field(obs, "extra", {}) or {}
            obs_call_id = _get_field(obs, "source_call_id", None)
            cmd_timed_out = obs_extra.get("timed_out") is True or obs_extra.get("timeout") is True

            # Resolve command return/exit code field spellings consistently with shell.py:
            # Supports "exit_code" and "returncode". Contradictory code fields are left unknown.
            has_exit_code = "exit_code" in obs_extra
            has_returncode = "returncode" in obs_extra
            code_field: str | None = None
            raw_code_val: Any = None

            if has_exit_code and has_returncode:
                val_exit = obs_extra.get("exit_code")
                val_ret = obs_extra.get("returncode")
                if val_exit == val_ret:
                    code_field = "exit_code"
                    raw_code_val = val_exit
                else:
                    # Contradictory return/exit code fields: leave ambiguous/unknown
                    code_field = None
                    raw_code_val = None
                    unknowns.append(
                        f"Step {s_id} observation '{obs_call_id or 'unknown'}' has contradictory "
                        f"exit_code ({val_exit!r}) and returncode ({val_ret!r}) metadata; code outcome left unknown."
                    )
            elif has_exit_code:
                code_field = "exit_code"
                raw_code_val = obs_extra.get("exit_code")
            elif has_returncode:
                code_field = "returncode"
                raw_code_val = obs_extra.get("returncode")

            cmd_exit_124 = code_field is not None and raw_code_val == 124

            if cmd_timed_out:
                loc_key = "timed_out" if obs_extra.get("timed_out") is True else "timeout"
                observations.append(
                    {
                        "code": "STOPPING_COMMAND_TIMEOUT_OBSERVED",
                        "step_id": s_id,
                        "tool_call_id": obs_call_id,
                        "locator": f"/steps/{s_idx}/observation/results/{obs_idx}/extra/{loc_key}",
                        "evidence_kind": "observed",
                        "summary": (
                            f"Tool invocation '{obs_call_id or 'unknown'}' encountered a "
                            "command-level execution timeout. "
                            "Distinct from episode wall-clock timeout."
                        ),
                    }
                )
            elif cmd_exit_124:
                # Bare exit 124 is a recorded exit code, at most a timeout hypothesis
                observations.append(
                    {
                        "code": "STOPPING_COMMAND_TIMEOUT_HYPOTHESIS",
                        "step_id": s_id,
                        "tool_call_id": obs_call_id,
                        "locator": f"/steps/{s_idx}/observation/results/{obs_idx}/extra/{code_field}",
                        "evidence_kind": "hypothesis",
                        "summary": (
                            f"Tool invocation '{obs_call_id or 'unknown'}' recorded exit code 124 "
                            "(potential command timeout hypothesis; unconfirmed without explicit timeout metadata). "
                            "Distinct from episode wall-clock timeout."
                        ),
                    }
                )

            # Subagent reference in observation (isolated scope)
            sub_ref = _get_field(obs, "subagent_trajectory_ref", None)
            if sub_ref:
                observations.append(
                    {
                        "code": "STOPPING_OBSERVATION_SUBAGENT_REF",
                        "step_id": s_id,
                        "tool_call_id": obs_call_id,
                        "locator": f"/steps/{s_idx}/observation/results/{obs_idx}/subagent_trajectory_ref",
                        "evidence_kind": "observed",
                        "summary": (
                            f"Step {s_id} observation references subagent execution "
                            f"({len(sub_ref)} item(s)); scoped separately from root agent."
                        ),
                    }
                )

        # 2e. Submission tool calls (requesting submission, distinct from recorded completion)
        tool_calls = _get_field(step, "tool_calls", ()) or ()
        for tc_idx, tc in enumerate(tool_calls):
            fn_name = _get_field(tc, "function_name", "")
            tc_id = _get_field(tc, "tool_call_id", None)
            if fn_name and str(fn_name).strip().lower() in SUBMISSION_TOOL_NAMES:
                clean_fn = str(fn_name).strip().lower()
                observations.append(
                    {
                        "code": "STOPPING_SUBMISSION_TOOL_CALL",
                        "step_id": s_id,
                        "tool_call_id": tc_id,
                        "locator": f"/steps/{s_idx}/tool_calls/{tc_idx}/function_name",
                        "evidence_kind": "observed",
                        "summary": (
                            f"Agent emitted tool call requesting completion/submission via '{clean_fn}' "
                            "(request to submit, distinct from recorded episode completion or verifier success)."
                        ),
                    }
                )

    if len(steps) > 0 and has_agent_step and not step_finish_reason_observed:
        unknowns.append("Step-level LLM finish_reason is not retained in trajectory telemetry.")

    if not max_tokens_configured_observed:
        unknowns.append("Sampling max_tokens limit is not recorded in trajectory metadata.")

    # -------------------------------------------------------------------------
    # 3. Episode-level termination reason and episode wall-clock timeout
    # -------------------------------------------------------------------------
    episode_term_reason: Any = None
    episode_term_locator: str | None = None

    for key in TERMINATION_REASON_KEYS:
        if key in extra:
            episode_term_reason = extra.get(key)
            episode_term_locator = f"/extra/{key}"
            break
        elif key in agent_extra:
            episode_term_reason = agent_extra.get(key)
            episode_term_locator = f"/agent/extra/{key}"
            break
        elif key in final_metrics_extra:
            episode_term_reason = final_metrics_extra.get(key)
            episode_term_locator = f"/final_metrics/extra/{key}"
            break

    episode_timeout_reported = False
    episode_step_limit_terminated = False

    if episode_term_reason is not None and episode_term_locator is not None:
        raw_term_str = str(episode_term_reason).strip()
        lower_term = raw_term_str.lower()

        if "timeout" in lower_term:
            episode_timeout_reported = True
            observations.append(
                {
                    "code": "STOPPING_EPISODE_TIMEOUT_OBSERVED",
                    "step_id": None,
                    "tool_call_id": None,
                    "locator": episode_term_locator,
                    "evidence_kind": "observed",
                    "summary": (
                        "Episode execution terminated due to wall-clock timeout. "
                        "Distinct from command-level timeout."
                    ),
                }
            )
        elif (
            lower_term in ("max_steps", "step_limit", "max_iterations", "max_turns")
            or "step_limit" in lower_term
        ):
            episode_step_limit_terminated = True
            observations.append(
                {
                    "code": "STOPPING_STEP_LIMIT_TERMINATION",
                    "step_id": None,
                    "tool_call_id": None,
                    "locator": episode_term_locator,
                    "evidence_kind": "observed",
                    "summary": "Episode execution terminated due to step limit.",
                }
            )
        elif lower_term in ("completed", "submitted", "done", "agent_stop", "stop", "complete"):
            observations.append(
                {
                    "code": "STOPPING_EPISODE_COMPLETION_OBSERVED",
                    "step_id": None,
                    "tool_call_id": None,
                    "locator": episode_term_locator,
                    "evidence_kind": "observed",
                    "summary": (
                        "Episode execution recorded completion. "
                        "Completion does not imply verifier success."
                    ),
                }
            )
        elif lower_term in ("max_tokens", "token_limit", "context_length_exceeded"):
            observations.append(
                {
                    "code": "STOPPING_EPISODE_TOKEN_LIMIT_REACHED",
                    "step_id": None,
                    "tool_call_id": None,
                    "locator": episode_term_locator,
                    "evidence_kind": "observed",
                    "summary": "Episode execution terminated due to token limit.",
                }
            )
        else:
            safe_term = lower_term if lower_term in TERMINATION_STANDARD_TOKENS else None
            if safe_term:
                summary = f"Explicit episode termination reason recorded: '{safe_term}'."
            else:
                summary = "Explicit episode termination reason recorded."
            observations.append(
                {
                    "code": "STOPPING_EPISODE_TERMINATION_EXPLICIT",
                    "step_id": None,
                    "tool_call_id": None,
                    "locator": episode_term_locator,
                    "evidence_kind": "observed",
                    "summary": summary,
                }
            )
    else:
        unknowns.append(
            "Explicit episode-level termination_reason is not retained in trajectory metadata."
        )

    # Explicit timed_out boolean flags in root/agent metadata
    if not episode_timeout_reported:
        if extra.get("timed_out") is True:
            observations.append(
                {
                    "code": "STOPPING_EPISODE_TIMEOUT_OBSERVED",
                    "step_id": None,
                    "tool_call_id": None,
                    "locator": "/extra/timed_out",
                    "evidence_kind": "observed",
                    "summary": (
                        "Episode wall-clock timeout flag is True. "
                        "Distinct from command-level timeout."
                    ),
                }
            )
        elif agent_extra.get("timed_out") is True:
            observations.append(
                {
                    "code": "STOPPING_EPISODE_TIMEOUT_OBSERVED",
                    "step_id": None,
                    "tool_call_id": None,
                    "locator": "/agent/extra/timed_out",
                    "evidence_kind": "observed",
                    "summary": (
                        "Episode wall-clock timeout flag in agent extra is True. "
                        "Distinct from command-level timeout."
                    ),
                }
            )

    # -------------------------------------------------------------------------
    # 4. Configured limits (timeout & step limits)
    # Record configured bounds neutrally. Never infer LIMIT_REACHED from ATIF
    # record counts or step_id; actual exhaustion requires explicit termination metadata.
    # -------------------------------------------------------------------------
    timeout_limit: Any = None
    timeout_locator: str | None = None
    for key in TIMEOUT_CONFIG_KEYS:
        if key in agent_extra:
            timeout_limit = agent_extra.get(key)
            timeout_locator = f"/agent/extra/{key}"
            break
        elif key in extra:
            timeout_limit = extra.get(key)
            timeout_locator = f"/extra/{key}"
            break

    if timeout_limit is not None and timeout_locator is not None:
        observations.append(
            {
                "code": "STOPPING_TIMEOUT_LIMIT_CONFIGURED",
                "step_id": None,
                "tool_call_id": None,
                "locator": timeout_locator,
                "evidence_kind": "observed",
                "summary": f"Episode wall-clock timeout limit configured at {timeout_limit} seconds.",
            }
        )
    else:
        unknowns.append(
            "Configured wall-clock timeout limit is not recorded in trajectory metadata."
        )

    step_limit: Any = None
    step_limit_locator: str | None = None
    for key in STEP_LIMIT_KEYS:
        if key in extra:
            step_limit = extra.get(key)
            step_limit_locator = f"/extra/{key}"
            break
        elif key in agent_extra:
            step_limit = agent_extra.get(key)
            step_limit_locator = f"/agent/extra/{key}"
            break

    if (
        step_limit is not None
        and step_limit_locator is not None
        and isinstance(step_limit, (int, float))
    ):
        observations.append(
            {
                "code": "STOPPING_STEP_LIMIT_CONFIGURED",
                "step_id": None,
                "tool_call_id": None,
                "locator": step_limit_locator,
                "evidence_kind": "observed",
                "summary": (
                    f"Episode configured with step limit of {int(step_limit)} "
                    "(configuration parameter; actual exhaustion requires explicit termination metadata)."
                ),
            }
        )
        if not episode_step_limit_terminated:
            unknowns.append(
                "Whether configured step limit was exhausted or caused termination is not "
                "recorded in termination metadata."
            )
    else:
        unknowns.append("Max step limit is not recorded in trajectory metadata.")

    # -------------------------------------------------------------------------
    # 5. Submission metadata & Verifier outcome (completion vs verifier success)
    # Never claim task success from arbitrary metadata.
    # -------------------------------------------------------------------------
    if "submitted" in extra:
        observations.append(
            {
                "code": "STOPPING_SUBMISSION_MARKER_METADATA",
                "step_id": None,
                "tool_call_id": None,
                "locator": "/extra/submitted",
                "evidence_kind": "observed",
                "summary": f"Explicit submission flag recorded in root metadata (submitted={extra.get('submitted')}).",
            }
        )
    elif "submission_status" in extra:
        raw_sub_status = str(extra.get("submission_status", "")).strip().lower()
        safe_sub_status = (
            raw_sub_status if raw_sub_status in SUBMISSION_STANDARD_TOKENS else "recorded"
        )
        observations.append(
            {
                "code": "STOPPING_SUBMISSION_MARKER_METADATA",
                "step_id": None,
                "tool_call_id": None,
                "locator": "/extra/submission_status",
                "evidence_kind": "observed",
                "summary": f"Explicit submission status recorded in root metadata ('{safe_sub_status}').",
            }
        )

    verifier_found = False
    for vkey in VERIFIER_OUTCOME_KEYS:
        if vkey in extra:
            verifier_found = True
            vval = extra.get(vkey)
            if isinstance(vval, (bool, int, float)):
                val_repr = str(vval)
            elif isinstance(vval, str) and vval.strip().lower() in VERIFIER_STANDARD_TOKENS:
                val_repr = f"'{vval.strip().lower()}'"
            else:
                val_repr = "recorded"
            observations.append(
                {
                    "code": "STOPPING_VERIFIER_OUTCOME_RECORDED",
                    "step_id": None,
                    "tool_call_id": None,
                    "locator": f"/extra/{vkey}",
                    "evidence_kind": "observed",
                    "summary": (
                        f"Verifier evaluation outcome recorded in metadata: {vkey}={val_repr}. "
                        "Completion, verifier outcome, and task success are strictly distinct."
                    ),
                }
            )
            break
        elif vkey in final_metrics_extra:
            verifier_found = True
            vval = final_metrics_extra.get(vkey)
            if isinstance(vval, (bool, int, float)):
                val_repr = str(vval)
            elif isinstance(vval, str) and vval.strip().lower() in VERIFIER_STANDARD_TOKENS:
                val_repr = f"'{vval.strip().lower()}'"
            else:
                val_repr = "recorded"
            observations.append(
                {
                    "code": "STOPPING_VERIFIER_OUTCOME_RECORDED",
                    "step_id": None,
                    "tool_call_id": None,
                    "locator": f"/final_metrics/extra/{vkey}",
                    "evidence_kind": "observed",
                    "summary": (
                        f"Verifier evaluation outcome recorded in final metrics: {vkey}={val_repr}. "
                        "Completion, verifier outcome, and task success are strictly distinct."
                    ),
                }
            )
            break

    if not verifier_found:
        unknowns.append(
            "Verifier evaluation outcome and reward are not recorded in TrajectoryIR "
            "(completion does not imply task success)."
        )

    # -------------------------------------------------------------------------
    # 6. Subagent scope (strictly separated from root agent)
    # -------------------------------------------------------------------------
    subagents = ir.subagent_trajectories if ir.subagent_trajectories is not None else ()
    if len(subagents) > 0:
        observations.append(
            {
                "code": "STOPPING_SUBAGENT_TRAJECTORIES_RECORDED",
                "step_id": None,
                "tool_call_id": None,
                "locator": "/subagent_trajectories",
                "evidence_kind": "observed",
                "summary": (
                    f"Trajectory retains {len(subagents)} subagent trajectory reference(s); "
                    "subagent termination is scoped separately from root agent stopping."
                ),
            }
        )

        for sub_idx, sub in enumerate(subagents):
            sub_reason: Any = None
            sub_loc: str | None = None
            if isinstance(sub, TrajectoryIR):
                if isinstance(sub.extra, dict) and "termination_reason" in sub.extra:
                    sub_reason = sub.extra.get("termination_reason")
                    sub_loc = f"/subagent_trajectories/{sub_idx}/extra/termination_reason"
            elif isinstance(sub, dict):
                if "termination_reason" in sub:
                    sub_reason = sub.get("termination_reason")
                    sub_loc = f"/subagent_trajectories/{sub_idx}/termination_reason"
                elif isinstance(sub.get("extra"), dict) and "termination_reason" in sub["extra"]:
                    sub_reason = sub["extra"].get("termination_reason")
                    sub_loc = f"/subagent_trajectories/{sub_idx}/extra/termination_reason"

            if sub_reason is not None and sub_loc is not None:
                sub_str = str(sub_reason).strip().lower()
                safe_sub_reason = sub_str if sub_str in TERMINATION_STANDARD_TOKENS else None
                if safe_sub_reason:
                    sub_summary = (
                        f"Subagent [{sub_idx}] termination reason recorded: '{safe_sub_reason}'. "
                        "Scoped independently from root agent."
                    )
                else:
                    sub_summary = (
                        f"Subagent [{sub_idx}] termination reason recorded. "
                        "Scoped independently from root agent."
                    )
                observations.append(
                    {
                        "code": "STOPPING_SUBAGENT_TERMINATION_RECORDED",
                        "step_id": None,
                        "tool_call_id": None,
                        "locator": sub_loc,
                        "evidence_kind": "observed",
                        "summary": sub_summary,
                    }
                )

    return {
        "mechanism": "stopping",
        "observations": observations,
        "unknowns": unknowns,
    }
