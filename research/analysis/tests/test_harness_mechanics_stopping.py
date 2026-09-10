"""Focused regression and boundary tests for stopping mechanics diagnostics.

Defends the scientific boundaries required by HAR-20:
- Conservative reporting of observed vs hypothesis facts.
- Final assistant no-tool turn is a neutral observed structure, NEVER proof of premature
  stopping or task failure.
- Command-level timeouts (individual tool executions) are strictly separated from
  episode-level wall-clock timeouts. Bare exit code 124 without timeout flag is
  at most a command timeout hypothesis.
- Agent completion (e.g. submit tool invocation or completed state) is strictly
  separated from external verifier success or reward. Never claim task success from arbitrary metadata.
- Subagent scopes and references are strictly isolated from root agent observations.
- All field locators are RFC6901 JSON pointers into original raw trajectory JSON
  (/steps/0/observation/results/0/extra/timed_out), preserving recorded step_id separately.
- Cross-step observation references and nonconsecutive step IDs are accurately resolved.
- Mismatched tool calls and observations are never paired by position.
- Step counts and record counts are never conflated with model rounds. Inferred LIMIT_REACHED
  from record counts or token totals is prohibited; configured bounds are observed neutrally,
  and actual exhaustion is unknown unless explicit termination metadata establishes it.
- Negative controls: trajectories with only system/user records (e.g. 5 records + max_steps=5)
  are classified as STOPPING_NO_AGENT_STEPS_RECORDED (evidence availability/structure, not
  agent decision or cutoff).
- Non-agent, empty, and captured-context structures are treated conservatively.
- No private text leaks (prompts, reasoning, commands, tool outputs, custom reasons) into summaries or locators.
- Missing telemetry and outcome evidence are defended as unknowns, never fabricated as observations.
- Every locator reopens retained raw trajectory data.
"""

from __future__ import annotations

from typing import Any

from harness_mechanics.stopping import inspect

from evallab.trajectory_ir import (
    MetricsRecord,
    ObservationResultRecord,
    SamplingParams,
    StepRecord,
    ToolCallRecord,
    TrajectoryIR,
)


def _resolve_rfc6901(doc: dict[str, Any], pointer: str) -> Any:
    """Resolve an RFC6901 JSON pointer against a document dictionary."""
    if not pointer or pointer == "/":
        return doc
    parts = pointer.lstrip("/").split("/")
    curr: Any = doc
    for p in parts:
        token = p.replace("~1", "/").replace("~0", "~")
        if isinstance(curr, dict):
            if token not in curr:
                raise KeyError(f"Pointer token {token!r} not in dict keys: {list(curr.keys())}")
            curr = curr[token]
        elif isinstance(curr, (list, tuple)):
            idx = int(token)
            if idx < 0 or idx >= len(curr):
                raise IndexError(f"Pointer index {idx} out of range (len={len(curr)})")
            curr = curr[idx]
        else:
            raise TypeError(
                f"Cannot traverse token {token!r} on non-container {type(curr).__name__}"
            )
    return curr


def test_contract_schema() -> None:
    """inspect(ir) must conform strictly to the diagnostic API schema with RFC6901 locators."""
    ir = TrajectoryIR(schema_version="ATIF-v1.7")
    res = inspect(ir)

    assert isinstance(res, dict)
    assert set(res.keys()) == {"mechanism", "observations", "unknowns"}
    assert res["mechanism"] == "stopping"
    assert isinstance(res["observations"], list)
    assert isinstance(res["unknowns"], list)

    for obs in res["observations"]:
        assert set(obs.keys()) == {
            "code",
            "step_id",
            "tool_call_id",
            "locator",
            "evidence_kind",
            "summary",
        }
        assert obs["evidence_kind"] in ("observed", "hypothesis")
        assert isinstance(obs["code"], str)
        assert isinstance(obs["locator"], str)
        assert obs["locator"].startswith("/"), (
            f"Locator must be an RFC6901 pointer: {obs['locator']}"
        )
        assert isinstance(obs["summary"], str)
        assert obs["step_id"] is None or isinstance(obs["step_id"], int)
        assert obs["tool_call_id"] is None or isinstance(obs["tool_call_id"], str)


def test_neutral_final_assistant_no_tool() -> None:
    """Final assistant prose without tool calls is a neutral structure, NOT failure or premature stop."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                tool_calls=(),
                message="I have resolved the issue and verified the test suite.",
            ),
        ),
    )
    res = inspect(ir)
    codes = [o["code"] for o in res["observations"]]

    assert "STOPPING_FINAL_ASSISTANT_NO_TOOL" in codes
    obs = next(o for o in res["observations"] if o["code"] == "STOPPING_FINAL_ASSISTANT_NO_TOOL")
    assert obs["step_id"] == 1
    assert obs["locator"] == "/steps/0"
    assert obs["evidence_kind"] == "observed"

    summary_lower = obs["summary"].lower()
    assert "neutral" in summary_lower
    assert "does not indicate premature stopping" in summary_lower


def test_final_assistant_with_tool_calls() -> None:
    """Final assistant step with tool calls emits trailing tool call observation."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                tool_calls=(
                    ToolCallRecord(tool_call_id="call_99", function_name="exec", arguments={}),
                ),
                message="Running build...",
            ),
        ),
    )
    res = inspect(ir)
    codes = [o["code"] for o in res["observations"]]

    assert "STOPPING_FINAL_ASSISTANT_WITH_TOOL_CALLS" in codes
    assert "STOPPING_FINAL_ASSISTANT_NO_TOOL" not in codes
    obs = next(
        o for o in res["observations"] if o["code"] == "STOPPING_FINAL_ASSISTANT_WITH_TOOL_CALLS"
    )
    assert obs["step_id"] == 1
    assert obs["tool_call_id"] == "call_99"
    assert obs["locator"] == "/steps/0/tool_calls"


def test_trailing_non_agent_step() -> None:
    """Trajectory ending on user or system turn after agent steps is flagged as trailing non-agent step."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(step_id=1, source="agent", tool_calls=(), message="Initial reply"),
            StepRecord(step_id=2, source="user", tool_calls=(), message="Follow-up request"),
        ),
    )
    res = inspect(ir)
    codes = [o["code"] for o in res["observations"]]

    assert "STOPPING_TRAILING_NON_AGENT_STEP" in codes
    assert "STOPPING_FINAL_ASSISTANT_NO_TOOL" not in codes
    obs = next(o for o in res["observations"] if o["code"] == "STOPPING_TRAILING_NON_AGENT_STEP")
    assert obs["step_id"] == 2
    assert obs["locator"] == "/steps/1/source"
    assert "user" in obs["summary"]


def test_empty_trajectory_not_failure() -> None:
    """Zero steps in a trajectory must be recorded neutrally without fabricating a failure."""
    ir = TrajectoryIR(schema_version="ATIF-v1.7", steps=())
    res = inspect(ir)
    codes = [o["code"] for o in res["observations"]]

    assert "STOPPING_EMPTY_TRAJECTORY" in codes
    obs = next(o for o in res["observations"] if o["code"] == "STOPPING_EMPTY_TRAJECTORY")
    assert obs["locator"] == "/steps"
    assert "not proof of harness failure" in obs["summary"]
    assert any("no trajectory steps available" in u.lower() for u in res["unknowns"])


def test_non_agent_and_captured_context_structure() -> None:
    """Non-agent-only trajectories and captured-context steps are observed conservatively."""
    # Case 1: Trajectory contains only non-agent turns
    ir_non_agent = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(step_id=1, source="system", message="System instructions"),
            StepRecord(step_id=2, source="user", message="User task prompt"),
        ),
    )
    res_na = inspect(ir_non_agent)
    codes_na = [o["code"] for o in res_na["observations"]]
    assert "STOPPING_NO_AGENT_STEPS_RECORDED" in codes_na
    assert "STOPPING_TRAILING_NON_AGENT_STEP" not in codes_na
    obs_na = next(
        o for o in res_na["observations"] if o["code"] == "STOPPING_NO_AGENT_STEPS_RECORDED"
    )
    assert obs_na["locator"] == "/steps"
    assert "availability/structure" in obs_na["summary"].lower()
    assert any("no agent steps recorded" in u.lower() for u in res_na["unknowns"])

    # Case 2: Step marked as copied / captured context boundary
    ir_captured = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="system",
                message="Pre-seeded context",
                is_copied_context=True,
            ),
            StepRecord(step_id=2, source="agent", message="Starting work"),
        ),
    )
    res_cap = inspect(ir_captured)
    codes_cap = [o["code"] for o in res_cap["observations"]]
    assert "STOPPING_CAPTURED_CONTEXT_OBSERVED" in codes_cap
    obs_cap = next(
        o for o in res_cap["observations"] if o["code"] == "STOPPING_CAPTURED_CONTEXT_OBSERVED"
    )
    assert obs_cap["locator"] == "/steps/0/is_copied_context"
    assert obs_cap["step_id"] == 1


def test_non_agent_steps_with_matching_max_steps_negative_control() -> None:
    """5 system/user records + max_steps=5 must NEVER infer cutoff, limit reached, or agent decision."""
    ir_5_steps = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(step_id=1, source="system", message="Prompt 1"),
            StepRecord(step_id=2, source="system", message="Prompt 2"),
            StepRecord(step_id=3, source="system", message="Prompt 3"),
            StepRecord(step_id=4, source="user", message="Task part 1"),
            StepRecord(step_id=5, source="user", message="Task part 2"),
        ),
        extra={"max_steps": 5},
    )
    res = inspect(ir_5_steps)
    codes = [o["code"] for o in res["observations"]]

    # Availability/structure observation only; NOT an agent decision or cutoff
    assert "STOPPING_NO_AGENT_STEPS_RECORDED" in codes
    assert "STOPPING_STEP_LIMIT_CONFIGURED" in codes

    # MUST NOT infer limit reached, cutoff, or trailing non-agent step
    assert "STOPPING_STEP_LIMIT_REACHED" not in codes
    assert "STOPPING_STEP_LIMIT_TERMINATION" not in codes
    assert "STOPPING_TRAILING_NON_AGENT_STEP" not in codes
    assert "STOPPING_FINAL_ASSISTANT_NO_TOOL" not in codes

    # Unknowns must record that actual exhaustion is unconfirmed
    assert any("no agent steps recorded" in u.lower() for u in res["unknowns"])
    assert any("step limit was exhausted" in u.lower() for u in res["unknowns"])


def test_command_timeout_distinct_from_episode_timeout() -> None:
    """Command-level timeout and episode-level timeout must never be conflated."""
    # Case 1: Command timeout only (tool timed out with explicit flag, episode completed)
    ir_cmd = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                tool_calls=(
                    ToolCallRecord(tool_call_id="call_cmd", function_name="bash", arguments={}),
                ),
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_cmd",
                        content="Process timed out after 30s",
                        extra={"timed_out": True, "exit_code": 124},
                    ),
                ),
            ),
            StepRecord(step_id=2, source="agent", tool_calls=(), message="Recovery"),
        ),
        extra={"termination_reason": "completed"},
    )
    res_cmd = inspect(ir_cmd)
    codes_cmd = [o["code"] for o in res_cmd["observations"]]

    assert "STOPPING_COMMAND_TIMEOUT_OBSERVED" in codes_cmd
    assert "STOPPING_EPISODE_TIMEOUT_OBSERVED" not in codes_cmd
    obs_cmd = next(
        o for o in res_cmd["observations"] if o["code"] == "STOPPING_COMMAND_TIMEOUT_OBSERVED"
    )
    assert obs_cmd["step_id"] == 1
    assert obs_cmd["tool_call_id"] == "call_cmd"
    assert obs_cmd["locator"] == "/steps/0/observation/results/0/extra/timed_out"
    assert obs_cmd["evidence_kind"] == "observed"
    assert "distinct from episode" in obs_cmd["summary"].lower()

    # Case 2: Episode timeout only (no tool timed out, wall clock expired)
    ir_ep = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(StepRecord(step_id=1, source="agent", tool_calls=(), message="In progress..."),),
        extra={"termination_reason": "trial_wall_clock_timeout"},
    )
    res_ep = inspect(ir_ep)
    codes_ep = [o["code"] for o in res_ep["observations"]]

    assert "STOPPING_EPISODE_TIMEOUT_OBSERVED" in codes_ep
    assert "STOPPING_COMMAND_TIMEOUT_OBSERVED" not in codes_ep
    obs_ep = next(
        o for o in res_ep["observations"] if o["code"] == "STOPPING_EPISODE_TIMEOUT_OBSERVED"
    )
    assert obs_ep["locator"] == "/extra/termination_reason"
    assert obs_ep["evidence_kind"] == "observed"
    assert "distinct from command-level" in obs_ep["summary"].lower()

    # Case 3: Bare exit 124 without timed_out flag is at most a timeout hypothesis
    ir_bare_124 = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                tool_calls=(
                    ToolCallRecord(tool_call_id="call_bare", function_name="bash", arguments={}),
                ),
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_bare",
                        extra={"exit_code": 124},
                    ),
                ),
            ),
        ),
    )
    res_bare = inspect(ir_bare_124)
    codes_bare = [o["code"] for o in res_bare["observations"]]
    assert "STOPPING_COMMAND_TIMEOUT_HYPOTHESIS" in codes_bare
    assert "STOPPING_COMMAND_TIMEOUT_OBSERVED" not in codes_bare
    obs_bare = next(
        o for o in res_bare["observations"] if o["code"] == "STOPPING_COMMAND_TIMEOUT_HYPOTHESIS"
    )
    assert obs_bare["evidence_kind"] == "hypothesis"
    assert obs_bare["locator"] == "/steps/0/observation/results/0/extra/exit_code"


def test_completion_distinct_from_verifier_success() -> None:
    """Agent completion (e.g. submit tool or termination_reason) is distinct from verifier success."""
    # Case 1: Completed agent, no verifier outcome recorded
    ir_comp = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                tool_calls=(
                    ToolCallRecord(tool_call_id="call_sub", function_name="submit", arguments={}),
                ),
                message="Submitting answer.",
            ),
        ),
        extra={"termination_reason": "completed", "submitted": True},
    )
    res_comp = inspect(ir_comp)
    codes = [o["code"] for o in res_comp["observations"]]

    assert "STOPPING_SUBMISSION_TOOL_CALL" in codes
    assert "STOPPING_EPISODE_COMPLETION_OBSERVED" in codes
    assert "STOPPING_SUBMISSION_MARKER_METADATA" in codes
    assert "STOPPING_VERIFIER_OUTCOME_RECORDED" not in codes

    obs_sub = next(
        o for o in res_comp["observations"] if o["code"] == "STOPPING_SUBMISSION_TOOL_CALL"
    )
    assert obs_sub["locator"] == "/steps/0/tool_calls/0/function_name"
    assert "request to submit" in obs_sub["summary"].lower()

    # Case 2: Verifier reward present (even if 0.0)
    ir_reward = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(StepRecord(step_id=1, source="agent", tool_calls=(), message="Finished"),),
        extra={"reward": 0.0, "termination_reason": "completed"},
    )
    res_reward = inspect(ir_reward)
    codes_reward = [o["code"] for o in res_reward["observations"]]

    assert "STOPPING_VERIFIER_OUTCOME_RECORDED" in codes_reward
    obs_v = next(
        o for o in res_reward["observations"] if o["code"] == "STOPPING_VERIFIER_OUTCOME_RECORDED"
    )
    assert obs_v["locator"] == "/extra/reward"
    assert "strictly distinct" in obs_v["summary"]


def test_step_level_finish_reasons() -> None:
    """All explicit finish_reason variants are detected with RFC6901 locators."""
    variants = [
        ("length", "STOPPING_STEP_FINISH_REASON_LENGTH"),
        ("max_tokens", "STOPPING_STEP_FINISH_REASON_LENGTH"),
        ("stop", "STOPPING_STEP_FINISH_REASON_STOP"),
        ("end_turn", "STOPPING_STEP_FINISH_REASON_STOP"),
        ("tool_calls", "STOPPING_STEP_FINISH_REASON_TOOL_CALLS"),
        ("content_filter", "STOPPING_STEP_FINISH_REASON_CONTENT_FILTER"),
        ("custom_reason", "STOPPING_STEP_FINISH_REASON_EXPLICIT"),
    ]

    for raw_reason, expected_code in variants:
        ir = TrajectoryIR(
            schema_version="ATIF-v1.7",
            steps=(
                StepRecord(
                    step_id=1,
                    source="agent",
                    tool_calls=(),
                    extra={"finish_reason": raw_reason},
                ),
            ),
        )
        res = inspect(ir)
        codes = [o["code"] for o in res["observations"]]
        assert expected_code in codes, f"Failed for {raw_reason}"
        obs = next(o for o in res["observations"] if o["code"] == expected_code)
        assert obs["step_id"] == 1
        assert obs["locator"] == "/steps/0/extra/finish_reason"

    # Finish reason in metrics.extra
    ir_metrics = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                tool_calls=(),
                metrics=MetricsRecord(extra={"finish_reason": "stop"}),
            ),
        ),
    )
    res_metrics = inspect(ir_metrics)
    obs_m = next(
        o for o in res_metrics["observations"] if o["code"] == "STOPPING_STEP_FINISH_REASON_STOP"
    )
    assert obs_m["locator"] == "/steps/0/metrics/extra/finish_reason"


def test_step_limits_configured_vs_explicit_termination() -> None:
    """Step limits are observed neutrally; termination is established only via explicit metadata."""
    # Case 1: Configured step limit without explicit termination metadata
    ir_configured = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(step_id=1, source="agent", tool_calls=(), message="1"),
            StepRecord(step_id=2, source="agent", tool_calls=(), message="2"),
        ),
        extra={"max_steps": 2},
    )
    res_configured = inspect(ir_configured)
    codes_configured = [o["code"] for o in res_configured["observations"]]
    assert "STOPPING_STEP_LIMIT_CONFIGURED" in codes_configured
    assert "STOPPING_STEP_LIMIT_REACHED" not in codes_configured
    assert "STOPPING_STEP_LIMIT_TERMINATION" not in codes_configured
    obs_cfg = next(
        o for o in res_configured["observations"] if o["code"] == "STOPPING_STEP_LIMIT_CONFIGURED"
    )
    assert obs_cfg["locator"] == "/extra/max_steps"
    assert any("step limit was exhausted" in u.lower() for u in res_configured["unknowns"])

    # Case 2: Explicit step limit termination in metadata
    ir_term = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(StepRecord(step_id=1, source="agent", tool_calls=(), message="1"),),
        extra={"max_steps": 10, "termination_reason": "max_steps"},
    )
    res_term = inspect(ir_term)
    codes_term = [o["code"] for o in res_term["observations"]]
    assert "STOPPING_STEP_LIMIT_CONFIGURED" in codes_term
    assert "STOPPING_STEP_LIMIT_TERMINATION" in codes_term
    obs_term = next(
        o for o in res_term["observations"] if o["code"] == "STOPPING_STEP_LIMIT_TERMINATION"
    )
    assert obs_term["locator"] == "/extra/termination_reason"


def test_no_step_id_inferred_round_limit() -> None:
    """Model-round limit must NEVER be inferred from the value of step_id."""
    # step_id is 100, but there is only 1 step; configured limit is 10
    ir_nonconsec = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(StepRecord(step_id=100, source="agent", tool_calls=(), message="Only step"),),
        extra={"max_steps": 10},
    )
    res = inspect(ir_nonconsec)
    codes = [o["code"] for o in res["observations"]]

    assert "STOPPING_STEP_LIMIT_CONFIGURED" in codes
    assert "STOPPING_STEP_LIMIT_REACHED" not in codes
    assert "STOPPING_STEP_LIMIT_TERMINATION" not in codes


def test_token_limits_reached_vs_configured() -> None:
    """Sampling max_tokens limit configuration and hit are detected with RFC6901 pointers."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                tool_calls=(),
                metrics=MetricsRecord(completion_tokens=2048),
                sampling_params=SamplingParams(max_tokens=2048),
                extra={"finish_reason": "length"},
            ),
        ),
    )
    res = inspect(ir)
    codes = [o["code"] for o in res["observations"]]

    assert "STOPPING_TOKEN_LIMIT_CONFIGURED" in codes
    assert "STOPPING_STEP_FINISH_REASON_LENGTH" in codes

    obs_cfg = next(o for o in res["observations"] if o["code"] == "STOPPING_TOKEN_LIMIT_CONFIGURED")
    assert obs_cfg["locator"] == "/steps/0/sampling_params/max_tokens"


def test_configured_wall_clock_timeout() -> None:
    """Configured episode wall-clock timeout is observed from agent_extra or extra."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(StepRecord(step_id=1, source="agent", tool_calls=(), message="ok"),),
        agent_extra={"timeout_seconds": 1800},
    )
    res = inspect(ir)
    codes = [o["code"] for o in res["observations"]]

    assert "STOPPING_TIMEOUT_LIMIT_CONFIGURED" in codes
    obs = next(o for o in res["observations"] if o["code"] == "STOPPING_TIMEOUT_LIMIT_CONFIGURED")
    assert obs["locator"] == "/agent/extra/timeout_seconds"
    assert "1800" in obs["summary"]


def test_subagent_scope_isolation() -> None:
    """Subagent trajectories and subagent observation references are scoped separately."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                tool_calls=(),
                message="delegating to worker",
                observation_results=(
                    ObservationResultRecord(
                        subagent_trajectory_ref=({"sub_session": "sub_987"},),
                    ),
                ),
            ),
        ),
        subagent_trajectories=({"termination_reason": "subagent_step_limit"},),
    )
    res = inspect(ir)
    codes = [o["code"] for o in res["observations"]]

    assert "STOPPING_SUBAGENT_TRAJECTORIES_RECORDED" in codes
    assert "STOPPING_SUBAGENT_TERMINATION_RECORDED" in codes
    assert "STOPPING_OBSERVATION_SUBAGENT_REF" in codes

    obs_sub = next(
        o for o in res["observations"] if o["code"] == "STOPPING_SUBAGENT_TERMINATION_RECORDED"
    )
    assert obs_sub["locator"] == "/subagent_trajectories/0/termination_reason"
    assert "independently from root agent" in obs_sub["summary"]

    obs_ref = next(
        o for o in res["observations"] if o["code"] == "STOPPING_OBSERVATION_SUBAGENT_REF"
    )
    assert obs_ref["locator"] == "/steps/0/observation/results/0/subagent_trajectory_ref"


def test_nonconsecutive_step_ids_and_cross_step_observations() -> None:
    """Zero-based array position must be used for locators while preserving nonconsecutive step_id."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=42,
                source="agent",
                tool_calls=(
                    ToolCallRecord(tool_call_id="call_alpha", function_name="exec", arguments={}),
                ),
                message="Initiating command",
            ),
            StepRecord(
                step_id=999,
                source="tool",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_alpha",
                        extra={"timed_out": True},
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)
    obs = next(o for o in res["observations"] if o["code"] == "STOPPING_COMMAND_TIMEOUT_OBSERVED")

    # Locator MUST use zero-based array index 1 of owning step, NOT step_id 999 or 42
    assert obs["locator"] == "/steps/1/observation/results/0/extra/timed_out"
    # Preserved step_id must be the actual recorded step_id 999
    assert obs["step_id"] == 999
    assert obs["tool_call_id"] == "call_alpha"


def test_mismatched_tool_observation_ids_not_paired_by_position() -> None:
    """Tool calls and observations with mismatched or missing IDs are never paired by position."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                tool_calls=(
                    ToolCallRecord(tool_call_id="call_first", function_name="exec", arguments={}),
                ),
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_different",
                        extra={"timed_out": True},
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)
    obs = next(o for o in res["observations"] if o["code"] == "STOPPING_COMMAND_TIMEOUT_OBSERVED")

    # The observation must record its own source_call_id, NOT steal call_first by index 0
    assert obs["tool_call_id"] == "call_different"
    assert obs["locator"] == "/steps/0/observation/results/0/extra/timed_out"


def test_submission_marker_metadata() -> None:
    """Explicit submission flags in root metadata are observed with RFC6901 pointers."""
    ir1 = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(StepRecord(step_id=1, source="agent", tool_calls=(), message="Done"),),
        extra={"submitted": True},
    )
    res1 = inspect(ir1)
    assert "STOPPING_SUBMISSION_MARKER_METADATA" in [o["code"] for o in res1["observations"]]
    obs1 = next(
        o for o in res1["observations"] if o["code"] == "STOPPING_SUBMISSION_MARKER_METADATA"
    )
    assert obs1["locator"] == "/extra/submitted"

    ir2 = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(StepRecord(step_id=1, source="agent", tool_calls=(), message="Done"),),
        extra={"submission_status": "submitted"},
    )
    res2 = inspect(ir2)
    assert "STOPPING_SUBMISSION_MARKER_METADATA" in [o["code"] for o in res2["observations"]]
    obs2 = next(
        o for o in res2["observations"] if o["code"] == "STOPPING_SUBMISSION_MARKER_METADATA"
    )
    assert obs2["locator"] == "/extra/submission_status"


def test_no_private_text_leaks() -> None:
    """Private prompts, commands, outputs, thoughts, and custom termination values must NEVER leak."""
    leak_prompt = "CONFIDENTIAL_SYSTEM_PROMPT_ABC"
    leak_command = "cat /etc/shadow && aws s3 cp s3://private-vault/keys ."
    leak_output = "SECRET_API_KEY_12345"
    leak_thought = "REASONING_CHAIN_SECRET_DO_NOT_EXPOSE"
    leak_finish = "HARNESS_PRIVATE_SENTINEL_FinishLeak"
    leak_term = "HARNESS_PRIVATE_SENTINEL_TermLeak"
    leak_sub_term = "HARNESS_PRIVATE_SENTINEL_SubTermLeak"
    leak_reward = "HARNESS_PRIVATE_SENTINEL_RewardLeak"

    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                tool_calls=(
                    ToolCallRecord(
                        tool_call_id="call_sec",
                        function_name="exec",
                        arguments={"cmd": leak_command},
                    ),
                ),
                message=leak_prompt,
                reasoning_content=leak_thought,
                extra={"finish_reason": leak_finish},
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_sec",
                        content=leak_output,
                        extra={"exit_code": 124, "timed_out": True},
                    ),
                ),
            ),
        ),
        extra={"termination_reason": leak_term, "reward": leak_reward},
        subagent_trajectories=({"termination_reason": leak_sub_term},),
    )
    res = inspect(ir)

    forbidden_tokens = (
        leak_prompt,
        leak_command,
        leak_output,
        leak_thought,
        leak_finish,
        leak_term,
        leak_sub_term,
        leak_reward,
        "shadow",
        "aws s3",
        "12345",
        "private-vault",
    )

    for obs in res["observations"]:
        for forbidden in forbidden_tokens:
            assert forbidden not in obs["summary"], f"Leak found in summary: {forbidden}"
            assert forbidden not in obs["locator"], f"Leak found in locator: {forbidden}"
            assert forbidden not in obs["code"], f"Leak found in code: {forbidden}"

    for u in res["unknowns"]:
        for forbidden in forbidden_tokens:
            assert forbidden not in u, f"Leak found in unknown: {forbidden}"


def test_unknowns_when_evidence_absent() -> None:
    """Defend unknown-versus-observed behavior without fragile wording pins."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(StepRecord(step_id=1, source="agent", tool_calls=(), message="No metadata"),),
    )
    res = inspect(ir)
    unknowns = res["unknowns"]
    obs_codes = {o["code"] for o in res["observations"]}

    # Unknowns must be populated for absent evidence
    assert len(unknowns) > 0
    # Defend semantic absence without fragile full-string equality
    assert any("finish_reason" in u.lower() for u in unknowns)
    assert any("termination_reason" in u.lower() for u in unknowns)
    assert any("timeout" in u.lower() for u in unknowns)
    assert any("max_tokens" in u.lower() or "token" in u.lower() for u in unknowns)
    assert any("step limit" in u.lower() or "step" in u.lower() for u in unknowns)
    assert any("verifier" in u.lower() or "reward" in u.lower() for u in unknowns)

    # Behavior: absent telemetry MUST NOT be fabricated as observations
    assert "STOPPING_STEP_FINISH_REASON_STOP" not in obs_codes
    assert "STOPPING_STEP_FINISH_REASON_LENGTH" not in obs_codes
    assert "STOPPING_EPISODE_TIMEOUT_OBSERVED" not in obs_codes
    assert "STOPPING_EPISODE_COMPLETION_OBSERVED" not in obs_codes
    assert "STOPPING_VERIFIER_OUTCOME_RECORDED" not in obs_codes
    assert "STOPPING_STEP_LIMIT_TERMINATION" not in obs_codes


def test_all_locators_reopen_retained_raw_data() -> None:
    """Every generated observation locator must successfully reopen fields in raw trajectory data."""
    raw_doc: dict[str, Any] = {
        "schema_version": "ATIF-v1.7",
        "agent": {
            "name": "test-agent",
            "extra": {
                "timeout_seconds": 1800,
                "max_tokens": 4096,
            },
        },
        "steps": [
            {
                "step_id": 10,
                "source": "agent",
                "message": "working",
                "is_copied_context": True,
                "tool_calls": [
                    {
                        "tool_call_id": "call_1",
                        "function_name": "submit",
                        "arguments": {},
                    }
                ],
                "sampling_params": {
                    "max_tokens": 4096,
                    "extra": {"finish_reason": "length"},
                },
                "metrics": {
                    "completion_tokens": 4096,
                    "extra": {"stop_reason": "stop"},
                },
                "extra": {
                    "finish_reason": "length",
                },
            },
            {
                "step_id": 20,
                "source": "tool",
                "observation": {
                    "results": [
                        {
                            "source_call_id": "call_1",
                            "extra": {"timed_out": True, "exit_code": 124},
                            "subagent_trajectory_ref": [{"session": "sub1"}],
                        }
                    ]
                },
            },
            {
                "step_id": 30,
                "source": "user",
                "message": "done",
            },
        ],
        "extra": {
            "termination_reason": "completed",
            "submitted": True,
            "reward": 1.0,
            "max_steps": 5,
        },
        "subagent_trajectories": [
            {"termination_reason": "completed"},
        ],
    }

    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        agent_extra=raw_doc["agent"]["extra"],
        steps=(
            StepRecord(
                step_id=10,
                source="agent",
                message="working",
                is_copied_context=True,
                tool_calls=(
                    ToolCallRecord(tool_call_id="call_1", function_name="submit", arguments={}),
                ),
                sampling_params=SamplingParams(
                    max_tokens=4096,
                    extra={"finish_reason": "length"},
                ),
                metrics=MetricsRecord(
                    completion_tokens=4096,
                    extra={"stop_reason": "stop"},
                ),
                extra={"finish_reason": "length"},
            ),
            StepRecord(
                step_id=20,
                source="tool",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_1",
                        subagent_trajectory_ref=({"session": "sub1"},),
                        extra={"timed_out": True, "exit_code": 124},
                    ),
                ),
            ),
            StepRecord(
                step_id=30,
                source="user",
                message="done",
            ),
        ),
        extra=raw_doc["extra"],
        subagent_trajectories=tuple(raw_doc["subagent_trajectories"]),
    )

    res = inspect(ir)
    assert len(res["observations"]) > 0

    for obs in res["observations"]:
        locator = obs["locator"]
        # Must resolve cleanly without KeyError, IndexError, or TypeError
        resolved = _resolve_rfc6901(raw_doc, locator)
        assert resolved is not None, f"Resolved value at {locator} was None"


def test_final_assistant_no_tool_reopens_raw_step_omitted_tool_calls() -> None:
    """Verify STOPPING_FINAL_ASSISTANT_NO_TOOL locator reopens actual enclosing step when tool_calls is omitted."""
    raw_doc = {
        "schema_version": "ATIF-v1.7",
        "session_id": "raw-omitted-tc",
        "steps": [
            {
                "step_id": 42,
                "source": "agent",
                "message": "Final answer without tool calls.",
            }
        ],
    }
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=42,
                source="agent",
                tool_calls=(),
                message="Final answer without tool calls.",
            ),
        ),
    )
    res = inspect(ir)
    obs = next(o for o in res["observations"] if o["code"] == "STOPPING_FINAL_ASSISTANT_NO_TOOL")
    assert obs["step_id"] == 42
    assert obs["locator"] == "/steps/0"

    # Reopening locator must resolve to actual enclosing raw step object
    resolved = _resolve_rfc6901(raw_doc, obs["locator"])
    assert resolved is raw_doc["steps"][0]
    assert "tool_calls" not in resolved


def test_returncode_124_hypothesis_and_raw_atif_pointer_reopening() -> None:
    """Native raw ATIF fixture verifying returncode=124 emits hypothesis, reopens via pointer, and handles contradictions."""
    # 1. Native raw ATIF trajectory document containing returncode=124
    raw_doc = {
        "schema_version": "ATIF-v1.7",
        "session_id": "session-raw-returncode-124",
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_call_id": "call_ret_124",
                        "function_name": "bash",
                        "arguments": {"command": "sleep 120"},
                    }
                ],
            },
            {
                "step_id": 2,
                "source": "tool",
                "observation": {
                    "results": [
                        {
                            "source_call_id": "call_ret_124",
                            "content": "",
                            "extra": {"returncode": 124},
                        }
                    ]
                },
            },
        ],
        "extra": {
            "termination_reason": "completed",
        },
    }

    ir_returncode = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                tool_calls=(
                    ToolCallRecord(
                        tool_call_id="call_ret_124",
                        function_name="bash",
                        arguments={"command": "sleep 120"},
                    ),
                ),
            ),
            StepRecord(
                step_id=2,
                source="tool",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_ret_124",
                        content="",
                        extra={"returncode": 124},
                    ),
                ),
            ),
        ),
        extra=raw_doc["extra"],
    )

    res = inspect(ir_returncode)
    codes = [o["code"] for o in res["observations"]]
    # Conservative hypothesis semantics: bare 124 is a hypothesis, NEVER observed timeout
    assert "STOPPING_COMMAND_TIMEOUT_HYPOTHESIS" in codes
    assert "STOPPING_COMMAND_TIMEOUT_OBSERVED" not in codes
    assert "STOPPING_EPISODE_TIMEOUT_OBSERVED" not in codes

    obs_hyp = next(
        o for o in res["observations"] if o["code"] == "STOPPING_COMMAND_TIMEOUT_HYPOTHESIS"
    )
    assert obs_hyp["evidence_kind"] == "hypothesis"
    assert obs_hyp["tool_call_id"] == "call_ret_124"
    # Preserves actual field spelling in RFC6901 pointer: /steps/1/observation/results/0/extra/returncode
    assert obs_hyp["locator"] == "/steps/1/observation/results/0/extra/returncode"

    # Verify pointer reopens the actual retained key in raw ATIF data
    resolved_val = _resolve_rfc6901(raw_doc, obs_hyp["locator"])
    assert resolved_val == 124

    # 2. Contradictory metadata: conflicting exit_code and returncode must NOT be falsely certified
    ir_contradictory = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_conflict",
                        extra={"exit_code": 124, "returncode": 0},
                    ),
                ),
            ),
        ),
    )
    res_conflict = inspect(ir_contradictory)
    codes_conflict = [o["code"] for o in res_conflict["observations"]]
    # Must leave ambiguous code outcome unknown rather than picking favorable 124
    assert "STOPPING_COMMAND_TIMEOUT_HYPOTHESIS" not in codes_conflict
    assert "STOPPING_COMMAND_TIMEOUT_OBSERVED" not in codes_conflict
    assert any("contradictory" in u.lower() for u in res_conflict["unknowns"])

    # 3. Concordant metadata: matching exit_code and returncode retain exit_code pointer
    ir_concordant = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_match",
                        extra={"exit_code": 124, "returncode": 124},
                    ),
                ),
            ),
        ),
    )
    res_match = inspect(ir_concordant)
    obs_match = next(
        o for o in res_match["observations"] if o["code"] == "STOPPING_COMMAND_TIMEOUT_HYPOTHESIS"
    )
    assert obs_match["locator"] == "/steps/0/observation/results/0/extra/exit_code"
