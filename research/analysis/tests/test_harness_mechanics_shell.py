"""Tests for harness_mechanics.shell diagnostic module.

Verifies:
- Exact inspect(ir) contract matching contract.json (mechanism, observations, unknowns).
- Separation of reset request (SHELL_RESET_REQUESTED) from reset completion (SHELL_RESET_COMPLETED).
- Separation of real explicit timeout (SHELL_TIMEOUT_EXPLICIT, observed) from bare exit 124 (SHELL_TIMEOUT_HYPOTHESIS, hypothesis).
- Strict RFC6901 JSON pointer locators with zero-based array indexing into original raw steps.
- Strict source_call_id linkage without fallback pairing across mismatching or ambiguous IDs.
- Repeated identical failed command review hypothesis without command text leakage.
- Strict exclusion of non-shell tools (Python, RLM, MCP, file operations).
- Differentiation between environment/transport infrastructure errors and agent command failures.
- No inference of persistence from agent or environment names.
- No naive substring parsing for shell semantics or cwd dependence.
- No execution of stored commands or leakage of private command text.
"""

from __future__ import annotations

import json
import os
from typing import Any

from harness_mechanics.shell import inspect

from evallab.trajectory_ir import (
    ObservationResultRecord,
    StepRecord,
    ToolCallRecord,
    TrajectoryIR,
    build_trajectory_ir,
)


def _make_trajectory(
    steps: list[StepRecord],
    agent_name: str = "test-agent",
    agent_extra: dict[str, Any] | None = None,
    ir_extra: dict[str, Any] | None = None,
) -> TrajectoryIR:
    """Helper to construct a TrajectoryIR with minimal boilerplate."""
    return TrajectoryIR(
        schema_version="ATIF-v1.7",
        session_id="test-session-123",
        trajectory_id="traj-test-456",
        agent_name=agent_name,
        agent_extra=agent_extra or {},
        steps=tuple(steps),
        extra=ir_extra or {},
    )


def resolve_json_pointer(doc: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 JSON pointer against a JSON document."""
    if not pointer.startswith("/"):
        raise ValueError(f"Invalid JSON pointer (must start with '/'): {pointer}")
    tokens = pointer[1:].split("/")
    curr = doc
    for token in tokens:
        unescaped = token.replace("~1", "/").replace("~0", "~")
        if isinstance(curr, list):
            idx = int(unescaped)
            curr = curr[idx]
        elif isinstance(curr, dict):
            curr = curr[unescaped]
        else:
            raise KeyError(f"Cannot traverse segment '{unescaped}' in {type(curr).__name__}")
    return curr


def test_inspect_contract_structure():
    """Verify the top-level return structure matches contract.json exactly."""
    ir = _make_trajectory([])
    result = inspect(ir)

    assert isinstance(result, dict)
    assert result["mechanism"] == "shell"
    assert isinstance(result["observations"], list)
    assert isinstance(result["unknowns"], list)
    assert len(result["observations"]) == 0
    # Absence of shell tools should be noted in unknowns
    assert any("shell_activity_absent" in u for u in result["unknowns"])


def test_exit124_from_normal_program_emits_hypothesis_not_explicit_timeout():
    """Verify bare exit code 124 from a normal program emits conditional hypothesis, not observed timeout."""
    tc = ToolCallRecord(
        tool_call_id="call_t1",
        function_name="bash",
        arguments={"command": "python -c 'import sys; sys.exit(124)'"},
    )
    obs = ObservationResultRecord(
        source_call_id="call_t1",
        content="",
        extra={"exit_code": 124},
    )
    step = StepRecord(
        step_id=1,
        source="agent",
        tool_calls=(tc,),
        observation_results=(obs,),
    )
    ir = _make_trajectory([step])
    result = inspect(ir)

    # Must NOT assert observed timeout
    assert not any(o["code"] == "SHELL_TIMEOUT_EXPLICIT" for o in result["observations"])

    # Must record conditional hypothesis
    hyp_obs = [o for o in result["observations"] if o["code"] == "SHELL_TIMEOUT_HYPOTHESIS"]
    assert len(hyp_obs) == 1
    o = hyp_obs[0]
    assert o["evidence_kind"] == "hypothesis"
    assert o["step_id"] == 1
    assert o["tool_call_id"] == "call_t1"
    assert o["locator"] == "/steps/0/observation/results/0/extra/exit_code"
    assert "sys.exit" not in o["summary"]


def test_real_explicit_timeout_detected_from_metadata_flag():
    """Verify explicit timed_out metadata flag produces SHELL_TIMEOUT_EXPLICIT observation."""
    tc = ToolCallRecord(
        tool_call_id="call_t2",
        function_name="bash",
        arguments={"cmd": "pytest"},
    )
    obs = ObservationResultRecord(
        source_call_id="call_t2",
        content="timeout reached",
        extra={"timed_out": True, "status": "timed_out", "exit_code": 124},
    )
    step = StepRecord(
        step_id=2,
        source="agent",
        tool_calls=(tc,),
        observation_results=(obs,),
    )
    ir = _make_trajectory([step])
    result = inspect(ir)

    timeout_obs = [o for o in result["observations"] if o["code"] == "SHELL_TIMEOUT_EXPLICIT"]
    assert len(timeout_obs) == 1
    obs_res = timeout_obs[0]
    assert obs_res["evidence_kind"] == "observed"
    assert obs_res["locator"] == "/steps/0/observation/results/0/extra/timed_out"
    assert "pytest" not in obs_res["summary"]


def test_reset_request_separated_from_reset_completed():
    """Verify explicit shell reset requests in arguments and completed resets in observation metadata are distinct."""
    tc1 = ToolCallRecord(
        tool_call_id="call_r1",
        function_name="bash",
        arguments={"command": "clear", "reset": True},
    )
    tc2 = ToolCallRecord(
        tool_call_id="call_r2",
        function_name="sh",
        arguments={"command": "ls"},
    )
    obs2 = ObservationResultRecord(
        source_call_id="call_r2",
        content="",
        extra={"shell_reset": True, "exit_code": 0},
    )

    step1 = StepRecord(step_id=1, source="agent", tool_calls=(tc1,))
    step2 = StepRecord(step_id=2, source="agent", tool_calls=(tc2,), observation_results=(obs2,))
    ir = _make_trajectory([step1, step2])
    result = inspect(ir)

    # Step 1 requested reset
    requested_obs = [o for o in result["observations"] if o["code"] == "SHELL_RESET_REQUESTED"]
    assert len(requested_obs) == 1
    req = requested_obs[0]
    assert req["evidence_kind"] == "observed"
    assert req["step_id"] == 1
    assert req["locator"] == "/steps/0/tool_calls/0/arguments/reset"
    assert "clear" not in req["summary"]

    # Step 2 completed reset
    completed_obs = [o for o in result["observations"] if o["code"] == "SHELL_RESET_COMPLETED"]
    assert len(completed_obs) == 1
    comp = completed_obs[0]
    assert comp["evidence_kind"] == "observed"
    assert comp["step_id"] == 2
    assert comp["locator"] == "/steps/1/observation/results/0/extra/shell_reset"
    assert "ls" not in comp["summary"]


def test_failed_reset_request_does_not_assert_completed():
    """Verify that a tool call requesting reset which fails does not emit SHELL_RESET_COMPLETED."""
    tc = ToolCallRecord(
        tool_call_id="call_fail_reset",
        function_name="bash",
        arguments={"command": "bad_reset_cmd", "reset": True},
    )
    obs = ObservationResultRecord(
        source_call_id="call_fail_reset",
        content="Permission denied",
        extra={"exit_code": 1, "reset": True},
    )
    step = StepRecord(step_id=10, source="agent", tool_calls=(tc,), observation_results=(obs,))
    ir = _make_trajectory([step])
    result = inspect(ir)

    # Request is observed
    req_obs = [o for o in result["observations"] if o["code"] == "SHELL_RESET_REQUESTED"]
    assert len(req_obs) == 1
    assert req_obs[0]["locator"] == "/steps/0/tool_calls/0/arguments/reset"

    # Completion must NOT be claimed when command exited with non-zero code
    assert not any(o["code"] == "SHELL_RESET_COMPLETED" for o in result["observations"])


def test_conflicting_and_ambiguous_tool_call_ids():
    """Verify that mismatching or ambiguous IDs are not fallback-paired by position."""
    # Step has tool call 'call_alpha', but observation points to 'call_beta' (conflict / mismatch)
    tc1 = ToolCallRecord(
        tool_call_id="call_alpha",
        function_name="bash",
        arguments={"command": "ls"},
    )
    obs_mismatch = ObservationResultRecord(
        source_call_id="call_beta",
        content="",
        extra={"exit_code": 124, "timed_out": True},
    )
    step1 = StepRecord(
        step_id=1,
        source="agent",
        tool_calls=(tc1,),
        observation_results=(obs_mismatch,),
    )
    ir = _make_trajectory([step1])
    result = inspect(ir)

    # Tool call 'call_alpha' must NOT be paired with 'call_beta' observation
    assert len(result["observations"]) == 0

    # Test ambiguous duplicate tool call IDs
    tc_dup1 = ToolCallRecord(
        tool_call_id="dup_id", function_name="bash", arguments={"command": "pwd"}
    )
    tc_dup2 = ToolCallRecord(
        tool_call_id="dup_id", function_name="bash", arguments={"command": "pwd"}
    )
    obs_dup = ObservationResultRecord(source_call_id="dup_id", extra={"exit_code": 0})
    step2 = StepRecord(
        step_id=2,
        source="agent",
        tool_calls=(tc_dup1, tc_dup2),
        observation_results=(obs_dup,),
    )
    ir_ambig = _make_trajectory([step2])
    result_ambig = inspect(ir_ambig)

    # Linkage must remain unknown; cannot pair ambiguous IDs
    assert any("ambiguous" in u for u in result_ambig["unknowns"])


def test_nonconsecutive_step_ids_and_locator_indexing():
    """Verify that locators use zero-based array positions while step_id preserves recorded value."""
    tc = ToolCallRecord(
        tool_call_id="c_nonconsec",
        function_name="bash",
        arguments={"command": "echo test", "reset": True},
    )
    obs = ObservationResultRecord(
        source_call_id="c_nonconsec",
        content="test",
        extra={"exit_code": 124},
    )
    step42 = StepRecord(
        step_id=42,
        source="agent",
        tool_calls=(tc,),
        observation_results=(obs,),
    )
    ir = _make_trajectory([step42])
    result = inspect(ir)

    for o in result["observations"]:
        # Preserves recorded step_id 42
        assert o["step_id"] == 42
        # But locator points to 0-based array index /steps/0/...
        assert o["locator"].startswith("/steps/0/")
        assert "42" not in o["locator"]


def test_parent_reproduction_negative_probe():
    """Verify exact negative probe case from negative-shell-124-reset-request.json does not assert timeout/reset."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=42,
                source="agent",
                tool_calls=(
                    ToolCallRecord(
                        tool_call_id="a",
                        function_name="bash",
                        arguments={"command": "exit 124", "reset": True},
                    ),
                ),
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="a",
                        content="",
                        extra={"exit_code": 124},
                    ),
                ),
            ),
        ),
    )
    result = inspect(ir)

    # 1. Must NOT assert timeout happened
    assert not any(o["code"] == "SHELL_TIMEOUT_EXPLICIT" for o in result["observations"])
    # 2. Must NOT assert reset completed/happened
    assert not any(o["code"] == "SHELL_RESET_EXPLICIT" for o in result["observations"])
    assert not any(o["code"] == "SHELL_RESET_COMPLETED" for o in result["observations"])

    # 3. Must report requested reset and conditional timeout hypothesis with exact RFC6901 locators
    req_obs = [o for o in result["observations"] if o["code"] == "SHELL_RESET_REQUESTED"]
    assert len(req_obs) == 1
    assert req_obs[0]["step_id"] == 42
    assert req_obs[0]["locator"] == "/steps/0/tool_calls/0/arguments/reset"
    assert req_obs[0]["evidence_kind"] == "observed"

    hyp_obs = [o for o in result["observations"] if o["code"] == "SHELL_TIMEOUT_HYPOTHESIS"]
    assert len(hyp_obs) == 1
    assert hyp_obs[0]["step_id"] == 42
    assert hyp_obs[0]["locator"] == "/steps/0/observation/results/0/extra/exit_code"
    assert hyp_obs[0]["evidence_kind"] == "hypothesis"


def test_raw_locators_reopen_correctly():
    """Verify that all RFC 6901 locator pointers can be reopened and resolved against raw trajectory JSON."""
    raw_trajectory = {
        "schema_version": "ATIF-v1.7",
        "session_id": "raw-test-session",
        "agent": {"name": "test-agent"},
        "steps": [
            {
                "step_id": 99,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_call_id": "tc-1",
                        "function_name": "bash",
                        "arguments": {"command": "sleep 10", "reset": True},
                        "extra": {},
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "tc-1",
                            "content": "",
                            "extra": {"exit_code": 124, "timed_out": True},
                        }
                    ]
                },
            }
        ],
    }
    ir = build_trajectory_ir(raw_trajectory)
    result = inspect(ir)

    assert len(result["observations"]) > 0
    for obs in result["observations"]:
        locator = obs["locator"]
        # Must be valid RFC 6901 pointer
        assert locator.startswith("/")
        val = resolve_json_pointer(raw_trajectory, locator)
        assert val is not None


def test_repeated_identical_failed_command_hypothesis():
    """Verify repeated identical failed command triggers review hypothesis without leaking command."""
    secret_cmd = "make --jobs=8 test_target_internal_xyz"

    tc1 = ToolCallRecord(
        tool_call_id="call_f1",
        function_name="bash",
        arguments={"command": secret_cmd},
    )
    obs1 = ObservationResultRecord(
        source_call_id="call_f1",
        content="Target failed",
        extra={"exit_code": 2},
    )

    # Second call with identical command (with extra whitespace to test normalization)
    tc2 = ToolCallRecord(
        tool_call_id="call_f2",
        function_name="bash",
        arguments={"command": f"  {secret_cmd}  \n"},
    )
    obs2 = ObservationResultRecord(
        source_call_id="call_f2",
        content="Target failed again",
        extra={"exit_code": 2},
    )

    step1 = StepRecord(step_id=1, source="agent", tool_calls=(tc1,), observation_results=(obs1,))
    step2 = StepRecord(step_id=2, source="agent", tool_calls=(tc2,), observation_results=(obs2,))
    ir = _make_trajectory([step1, step2])
    result = inspect(ir)

    hypotheses = [
        o for o in result["observations"] if o["code"] == "SHELL_REPEATED_FAILED_COMMAND_HYPOTHESIS"
    ]
    assert len(hypotheses) == 1
    hyp = hypotheses[0]
    assert hyp["evidence_kind"] == "hypothesis"
    assert hyp["step_id"] == 2
    assert hyp["tool_call_id"] == "call_f2"
    assert hyp["locator"] == "/steps/1/tool_calls/0/arguments"

    # CRITICAL: Confirm secret command text is NOT anywhere in the result
    dumped = json.dumps(result)
    assert secret_cmd not in dumped
    assert "test_target_internal_xyz" not in dumped


def test_non_shell_tools_strictly_excluded():
    """Verify Python, RLM, MCP, and file editing tools are never misclassified as shell."""
    non_shell_calls = [
        ToolCallRecord(
            tool_call_id="c_py",
            function_name="python",
            arguments={"command": "import os; os.system('exit 1')"},
        ),
        ToolCallRecord(
            tool_call_id="c_ipy",
            function_name="ipython",
            arguments={"cmd": "run_cell()"},
        ),
        ToolCallRecord(
            tool_call_id="c_rlm",
            function_name="rlm_eval",
            arguments={"input": "rlm step"},
        ),
        ToolCallRecord(
            tool_call_id="c_mcp",
            function_name="mcp-server_exec",
            arguments={"command": "ls"},
        ),
        ToolCallRecord(
            tool_call_id="c_read",
            function_name="read",
            arguments={"path": "/app/code.py"},
        ),
    ]
    obs = [
        ObservationResultRecord(source_call_id=c.tool_call_id, extra={"exit_code": 1})
        for c in non_shell_calls
    ]
    step = StepRecord(
        step_id=1, source="agent", tool_calls=tuple(non_shell_calls), observation_results=tuple(obs)
    )
    ir = _make_trajectory([step])
    result = inspect(ir)

    # No shell observations should be produced
    assert len(result["observations"]) == 0
    assert any("shell_activity_absent" in u for u in result["unknowns"])


def test_distinguish_environment_error_from_agent_failure():
    """Verify infrastructure/harness errors are not counted as agent command failure loops."""
    cmd = "curl http://localhost:8080/api"

    tc1 = ToolCallRecord(tool_call_id="c1", function_name="bash", arguments={"command": cmd})
    obs1 = ObservationResultRecord(
        source_call_id="c1",
        extra={
            "error_type": "SandboxUnavailableError",
            "infrastructure_error": True,
        },
    )

    tc2 = ToolCallRecord(tool_call_id="c2", function_name="bash", arguments={"command": cmd})
    obs2 = ObservationResultRecord(
        source_call_id="c2",
        extra={"error_type": "DockerError", "harness_error": True},
    )

    step1 = StepRecord(step_id=1, source="agent", tool_calls=(tc1,), observation_results=(obs1,))
    step2 = StepRecord(step_id=2, source="agent", tool_calls=(tc2,), observation_results=(obs2,))
    ir = _make_trajectory([step1, step2])
    result = inspect(ir)

    # Should NOT emit repeated failed command hypothesis because these were environment errors
    hypotheses = [
        o for o in result["observations"] if o["code"] == "SHELL_REPEATED_FAILED_COMMAND_HYPOTHESIS"
    ]
    assert len(hypotheses) == 0


def test_ordinary_command_error_not_conflated_with_infrastructure():
    """Verify ordinary non-zero exit codes (e.g. exit 1 or exit 2) are not falsely diagnosed as infrastructure."""
    cmd = "curl http://example.com/missing"
    tc1 = ToolCallRecord(tool_call_id="c1", function_name="bash", arguments={"command": cmd})
    obs1 = ObservationResultRecord(
        source_call_id="c1",
        content="curl: (35) handshake failure",
        extra={"exit_code": 35},
    )
    tc2 = ToolCallRecord(tool_call_id="c2", function_name="bash", arguments={"command": cmd})
    obs2 = ObservationResultRecord(
        source_call_id="c2",
        content="curl: (35) handshake failure",
        extra={"exit_code": 35},
    )
    step1 = StepRecord(step_id=1, source="agent", tool_calls=(tc1,), observation_results=(obs1,))
    step2 = StepRecord(step_id=2, source="agent", tool_calls=(tc2,), observation_results=(obs2,))
    ir = _make_trajectory([step1, step2])
    result = inspect(ir)

    # These ordinary command errors should be detected as repeated command failures, not ignored as harness errors
    hypotheses = [
        o for o in result["observations"] if o["code"] == "SHELL_REPEATED_FAILED_COMMAND_HYPOTHESIS"
    ]
    assert len(hypotheses) == 1


def test_no_persistence_inferred_from_agent_name():
    """Verify that agent or model names do not cause false claims about persistence."""
    tc = ToolCallRecord(tool_call_id="c1", function_name="bash", arguments={"command": "echo hi"})
    obs = ObservationResultRecord(source_call_id="c1", extra={"exit_code": 0})
    step = StepRecord(step_id=1, source="agent", tool_calls=(tc,), observation_results=(obs,))

    # Agent named 'persistent_shell_runner'
    ir = _make_trajectory([step], agent_name="persistent_shell_runner")
    result = inspect(ir)

    # Must still register session persistence as unknown
    assert any("shell_session_persistence" in u for u in result["unknowns"])


def test_no_naive_substring_cwd_overclaiming():
    """Verify that commands containing 'cd' do not cause overclaiming of cwd state."""
    tc = ToolCallRecord(
        tool_call_id="c1",
        function_name="bash",
        arguments={"command": "cd /tmp && make build"},
    )
    obs = ObservationResultRecord(source_call_id="c1", extra={"exit_code": 0})
    step = StepRecord(step_id=1, source="agent", tool_calls=(tc,), observation_results=(obs,))
    ir = _make_trajectory([step])
    result = inspect(ir)

    # Substring parsing must NOT create a cwd observation; it must be recorded as an unknown
    cwd_obs = [o for o in result["observations"] if "cwd" in o.get("code", "").lower()]
    assert len(cwd_obs) == 0
    assert any("shell_cross_call_cwd" in u for u in result["unknowns"])


def test_never_execute_stored_commands():
    """Verify that command arguments are never executed (safety / non-execution invariant)."""
    canary_file = "/tmp/evallab_test_canary_should_not_exist_12345.txt"
    dangerous_cmd = f"touch {canary_file}"

    tc = ToolCallRecord(
        tool_call_id="c_dang",
        function_name="bash",
        arguments={"command": dangerous_cmd},
    )
    obs = ObservationResultRecord(source_call_id="c_dang", extra={"exit_code": 0})
    step = StepRecord(step_id=1, source="agent", tool_calls=(tc,), observation_results=(obs,))
    ir = _make_trajectory([step])

    result = inspect(ir)
    assert result["mechanism"] == "shell"

    # Confirm the canary file was NEVER created
    assert not os.path.exists(canary_file)


def test_commandline_spelling_recognized_without_command_leakage():
    """Verify Antigravity CommandLine argument spelling is digested without leaking command text."""
    secret_antigravity_cmd = "python3 -c 'import secret_pkg; secret_pkg.run()'"
    tc1 = ToolCallRecord(
        tool_call_id="c_ag1",
        function_name="run_command",
        arguments={"CommandLine": secret_antigravity_cmd},
    )
    obs1 = ObservationResultRecord(source_call_id="c_ag1", extra={"exit_code": 1})

    tc2 = ToolCallRecord(
        tool_call_id="c_ag2",
        function_name="run_command",
        arguments={"CommandLine": f"  {secret_antigravity_cmd}  \n"},
    )
    obs2 = ObservationResultRecord(source_call_id="c_ag2", extra={"exit_code": 1})

    step1 = StepRecord(step_id=1, source="agent", tool_calls=(tc1,), observation_results=(obs1,))
    step2 = StepRecord(step_id=2, source="agent", tool_calls=(tc2,), observation_results=(obs2,))
    ir = _make_trajectory([step1, step2])
    result = inspect(ir)

    hyp = [
        o for o in result["observations"] if o["code"] == "SHELL_REPEATED_FAILED_COMMAND_HYPOTHESIS"
    ]
    assert len(hyp) == 1
    assert hyp[0]["locator"] == "/steps/1/tool_calls/0/arguments"
    dumped = json.dumps(result)
    assert "secret_pkg" not in dumped


def test_generic_exec_tool_recorded_as_scope_unknown():
    """Verify generic 'exec' tool calls record scope unknown and are not treated as shell."""
    tc = ToolCallRecord(
        tool_call_id="c_exec",
        function_name="exec",
        arguments={"input": "const r = await tools.apply_patch(...);"},
    )
    obs = ObservationResultRecord(source_call_id="c_exec", extra={"exit_code": 0})
    step = StepRecord(step_id=1, source="agent", tool_calls=(tc,), observation_results=(obs,))
    ir = _make_trajectory([step])
    result = inspect(ir)

    # exec is NOT a shell tool
    assert len(result["observations"]) == 0
    assert any("shell_exec_tool_scope" in u for u in result["unknowns"])
    assert any("shell_activity_absent" in u for u in result["unknowns"])
