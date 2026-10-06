"""Harbor 0.24 agent-kwarg audit: every kwarg we emit validates (HAR-167).

Harbor 0.24 validates ``--agent-kwarg`` against the resolved agent class's
``options_model`` with ``extra=forbid`` at preflight
(``harbor/agents/base.py`` ``parse_options``,
``harbor/agents/factory.py`` ``run_preflight``). The pinned allowlists below
mirror the 0.24 options models field-for-field, so CI (which has no Harbor
installed) still catches a new or renamed kwarg. Where Harbor 0.24 is
importable, the same emitted kwargs are additionally validated against the
real options models and our adapter subclasses.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evallab.execution_contracts import (
    AGENTS_WITH_MCP_SUPPORT,
    AGENTS_WITH_SKILLS_SUPPORT,
    TERMINUS_REASONING_EFFORT_VALUES,
    ProfileInferenceSettings,
    RunRequest,
    build_command,
    terminus_agent_kwargs,
)

# Harbor 0.24 MiniSweAgentOptions fields (harbor/agents/installed/mini_swe_agent.py),
# including the InstalledAgentOptions base (harbor/agents/options.py).
MINISWE_OPTION_FIELDS = frozenset(
    {
        "version",
        "prompt_template_path",
        "config",
        "cost_limit",
        "reasoning_effort",
        "max_tokens",
        "config_file",
        "litellm_model_registry_file",
        "litellm_model_registry",
        "session_id_headers",
    }
)

# Harbor 0.24 Terminus2Options fields (harbor/agents/terminus_2/terminus_2.py).
TERMINUS_OPTION_FIELDS = frozenset(
    {
        "max_turns",
        "episodes",
        "max_episodes",
        "parser_name",
        "api_base",
        "temperature",
        "reasoning_effort",
        "collect_rollout_details",
        "session_id",
        "enable_summarize",
        "proactive_summarization_threshold",
        "max_thinking_tokens",
        "model_info",
        "trajectory_config",
        "tmux_pane_width",
        "tmux_pane_height",
        "store_all_messages",
        "record_terminal_session",
        "interleaved_thinking",
        "suppress_max_turns_warning",
        "use_responses_api",
        "llm_backend",
        "llm_kwargs",
        "llm_call_kwargs",
        "session_id_headers",
    }
)

# Lab harness knobs SecretSafeTerminus2 consumes as named __init__ params
# (evallab/harbor_terminus.py SecretSafeTerminus2Options).
TERMINUS_LAB_KNOB_FIELDS = frozenset(
    {
        "loop_break",
        "output_cap_chars",
        "completion_fix",
        "loop_command_run_min",
        "loop_message_run_min",
        "loop_grace_calls",
    }
)

# Lab LabRlmOptions fields (evallab/harbor_rlm.py). The stock DspyRlmOptions
# knows none of these; the lane targets our adapter, not stock dspy-rlm.
RLM_OPTION_FIELDS = frozenset(
    {"policy", "cost_limit_usd", "tool_timeout_sec", "working_dir", "verbose"}
)


def _task(tmp_path: Path) -> Path:
    task_dir = tmp_path / "task"
    task_dir.mkdir(exist_ok=True)
    (task_dir / "task.toml").write_text('schema_version = "1.4"\n')
    return task_dir


def _agent_kwargs(command: list[str]) -> dict[str, str]:
    kwargs: dict[str, str] = {}
    for index, token in enumerate(command):
        if token == "--agent-kwarg":
            key, _, _ = command[index + 1].partition("=")
            kwargs[key] = command[index + 1]
    return kwargs


def _request(tmp_path: Path, agent: str, model: str | None, **overrides: Any) -> RunRequest:
    return RunRequest(
        task=_task(tmp_path),
        agent=agent,
        name=f"kwarg-audit-{agent}",
        jobs_dir=tmp_path / "runs",
        environment="docker",
        model=model,
        allow_billable=True,
        **overrides,
    )


def test_miniswe_lanes_emit_only_miniswe_options(tmp_path: Path) -> None:
    lanes = [
        ("mini-swe-agent", "deepseek/deepseek-flash", {}),
        ("mini-swe-agent", "zai/glm-5.3-flash", {}),
        (
            "mini-swe-agent",
            "glm-selfhosted/glm-5.3-flash",
            {"inference_settings": ProfileInferenceSettings(effort="max", max_tokens=8192)},
        ),
    ]
    for agent, model, overrides in lanes:
        command = build_command(_request(tmp_path, agent, model, **overrides))
        emitted = _agent_kwargs(command)
        assert emitted, f"{model} must forward cost/max-tokens kwargs"
        assert set(emitted) <= MINISWE_OPTION_FIELDS, (model, sorted(emitted))


def test_terminus_lane_emit_only_terminus_options(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        "terminus-2",
        "zai/glm-5.3",
        inference_settings=ProfileInferenceSettings(effort="max", max_tokens=4096),
        max_output_tokens=4096,
    )
    kwargs = terminus_agent_kwargs(request)
    assert set(kwargs) <= (TERMINUS_OPTION_FIELDS | TERMINUS_LAB_KNOB_FIELDS), sorted(kwargs)
    assert set(kwargs["llm_call_kwargs"]) == {"max_tokens"}
    command = build_command(request)
    assert set(_agent_kwargs(command)) <= (TERMINUS_OPTION_FIELDS | TERMINUS_LAB_KNOB_FIELDS)


def test_terminus_effort_outside_024_literal_is_refused(tmp_path: Path) -> None:
    for effort in (5, True, "ultra", "HIGH"):
        request = _request(
            tmp_path,
            "terminus-2",
            "zai/glm-5.3",
            inference_settings=ProfileInferenceSettings(effort=effort),  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="reasoning_effort"):
            terminus_agent_kwargs(request)
    for effort in sorted(TERMINUS_REASONING_EFFORT_VALUES):
        request = _request(
            tmp_path,
            "terminus-2",
            "zai/glm-5.3",
            inference_settings=ProfileInferenceSettings(effort=effort),
        )
        assert terminus_agent_kwargs(request)["reasoning_effort"] == effort


def test_rlm_lane_emits_only_lab_rlm_options(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        "rlm",
        "zai-coding-plan/glm-5.3-flash",
        harness_policy="stock",
        cost_limit_usd=1.0,
    )
    emitted = _agent_kwargs(build_command(request))
    assert set(emitted) == {"policy", "cost_limit_usd"}
    assert set(emitted) <= RLM_OPTION_FIELDS


def test_mimo_and_opencode_lanes_emit_no_agent_kwargs(tmp_path: Path) -> None:
    mimo = _request(tmp_path, "mimoagent", "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B")
    assert _agent_kwargs(build_command(mimo)) == {}
    opencode = _request(tmp_path, "zai-opencode", "zai-coding-plan/glm-5.3-flash")
    assert _agent_kwargs(build_command(opencode)) == {}


def test_capability_allowances_match_024_gate() -> None:
    # Mirrors harbor/trial/trial.py _validate_agent_capabilities capabilities:
    # MiniSwe atif+mcp, Terminus atif+skills+mcp, stock RLM mcp-only,
    # nop/oracle windows-only; our LabRlmAgent and NativeMimoAgent add
    # no skills/MCP support.
    assert "terminus-2" in AGENTS_WITH_SKILLS_SUPPORT
    assert "zai-opencode" in AGENTS_WITH_SKILLS_SUPPORT
    assert "mini-swe-agent" not in AGENTS_WITH_SKILLS_SUPPORT
    assert "rlm" not in AGENTS_WITH_SKILLS_SUPPORT
    assert "mimoagent" not in AGENTS_WITH_SKILLS_SUPPORT
    assert "mini-swe-agent" in AGENTS_WITH_MCP_SUPPORT
    assert "rlm" not in AGENTS_WITH_MCP_SUPPORT
    assert "mimoagent" not in AGENTS_WITH_MCP_SUPPORT


def test_skills_with_skill_less_lane_fail_preflight_early(tmp_path: Path) -> None:
    from evallab.execution_contracts import validate_request

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# skill\n")
    base = _request(
        tmp_path, "mimoagent", "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B", skill=skill_dir
    )
    request = replace(
        base,
        environment="daytona",
        egress_lock=True,
        max_requests=10,
        max_input_tokens=100000,
        max_output_tokens=8192,
        max_total_tokens=108192,
        cost_limit_usd=2.5,
    )
    with pytest.raises(ValueError, match="does not support skills under Harbor 0.24"):
        validate_request(request)


def test_task_mcp_with_mcp_less_lane_fails_preflight_early(tmp_path: Path) -> None:
    from evallab.execution_contracts import validate_request

    task_dir = _task(tmp_path)
    (task_dir / "task.toml").write_text(
        'schema_version = "1.4"\n[environment]\nmcp_servers = [{name = "s"}]\n'
    )
    request = RunRequest(
        task=task_dir,
        agent="rlm",
        name="kwarg-audit-rlm-mcp",
        jobs_dir=tmp_path / "runs",
        environment="docker",
        model="zai-coding-plan/glm-5.3-flash",
        allow_billable=True,
    )
    with pytest.raises(ValueError, match="does not support MCP servers under Harbor 0.24"):
        validate_request(request)


def test_miniswe_kwargs_validate_against_real_024_model(tmp_path: Path) -> None:
    module = pytest.importorskip(
        "harbor.agents.installed.mini_swe_agent", reason="Harbor 0.24 is not installed here"
    )
    request = _request(tmp_path, "mini-swe-agent", "zai/glm-5.3-flash")
    raw = {
        key: value.partition("=")[2] for key, value in _agent_kwargs(build_command(request)).items()
    }
    parsed = {key: json.loads(value) for key, value in raw.items()}
    module.MiniSweAgentOptions.model_validate(parsed)
    with pytest.raises(ValueError, match="Invalid kwargs"):
        module.MiniSweAgent.preflight({"not_a_024_option": True})


def test_terminus_kwargs_validate_against_real_024_model(tmp_path: Path) -> None:
    module = pytest.importorskip(
        "harbor.agents.terminus_2.terminus_2", reason="Harbor 0.24 is not installed here"
    )
    request = _request(
        tmp_path,
        "terminus-2",
        "zai/glm-5.3",
        inference_settings=ProfileInferenceSettings(effort="high", max_tokens=4096),
        max_output_tokens=4096,
    )
    module.Terminus2Options.model_validate(terminus_agent_kwargs(request))
    with pytest.raises(ValueError, match="Invalid kwargs"):
        module.Terminus2.preflight({"not_a_024_option": True})
