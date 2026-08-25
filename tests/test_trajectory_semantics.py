"""Tests defending trajectory semantics profiles, action extraction, and Parquet projections."""

from __future__ import annotations

import random
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from evallab.trajectory_semantics import (
    GENERIC_POSIX_PROFILE,
    SEMANTIC_ACTION_FACT_SCHEMA,
    ToolMappingRule,
    TrajectorySemanticsProfile,
    UnmappedActionError,
    default_outcome_resolver,
    extract_semantic_actions,
    project_semantic_actions_parquet,
)


def test_structured_vs_bash_equivalent_profile() -> None:
    """A structured read tool and a bash cat command map to equivalent read roles."""
    profile = GENERIC_POSIX_PROFILE

    # Structured tool invocation
    role_struct, out_struct, _ = profile.resolve_action(
        "read",
        {"path": "/app/config.py"},
        {"content": "API_KEY=123", "status": "ok"},
    )
    assert role_struct == "read"
    assert out_struct == "success"

    # Bash equivalent invocation
    role_bash, out_bash, _ = profile.resolve_action(
        "bash",
        {"command": "cat /app/config.py"},
        {"output": "API_KEY=123", "exit_code": 0},
    )
    assert role_bash == "inspect"
    assert out_bash == "success"


def test_grep_diff_expected_negative_vs_real_failure() -> None:
    """Grep exit code 1 is expected-negative, exit code 2 is an error."""
    profile = GENERIC_POSIX_PROFILE

    # 1. Grep match found (0)
    r_found, out_found, detail_found = profile.resolve_action(
        "bash",
        {"command": "grep -rn 'TODO' src/"},
        {"exit_code": 0, "output": "src/main.py:10: TODO"},
    )
    assert r_found == "search"
    assert out_found == "success"
    assert detail_found == "match_found"

    # 2. Grep no match found (1) -> Expected negative, not a tool failure
    r_none, out_none, detail_none = profile.resolve_action(
        "bash",
        {"command": "grep -rn 'NONEXISTENT_SYMBOL' src/"},
        {"exit_code": 1, "output": ""},
    )
    assert r_none == "search"
    assert out_none == "expected_negative"
    assert detail_none == "pattern_not_found"

    # 3. Grep syntax error or missing dir (2) -> Real error
    r_err, out_err, detail_err = profile.resolve_action(
        "bash",
        {"command": "grep --invalid-flag"},
        {"exit_code": 2, "output": "grep: unrecognized option"},
    )
    assert r_err == "search"
    assert out_err == "error"
    assert detail_err == "grep_error_exit_code_2"

    # 4. Diff identical (0) vs differences (1) vs error (2)
    _, out_diff_0, det_diff_0 = profile.resolve_action(
        "bash",
        {"command": "diff a.txt b.txt"},
        {"exit_code": 0, "output": ""},
    )
    assert out_diff_0 == "success"
    assert det_diff_0 == "identical"

    _, out_diff_1, det_diff_1 = profile.resolve_action(
        "bash",
        {"command": "diff a.txt b.txt"},
        {"exit_code": 1, "output": "1c1\n< a\n---\n> b"},
    )
    assert out_diff_1 == "expected_negative"
    assert det_diff_1 == "differences_found"

    _, out_diff_2, det_diff_2 = profile.resolve_action(
        "bash",
        {"command": "diff nonexistent1 nonexistent2"},
        {"exit_code": 2, "output": "diff: nonexistent1: No such file or directory"},
    )
    assert out_diff_2 == "error"
    assert det_diff_2 == "diff_error_exit_code_2"


def test_user_assisted_vs_autonomous_recovery_intervention() -> None:
    """Intervening user guidance marks subsequent action as user-assisted; autonomous retry stays autonomous."""
    profile = GENERIC_POSIX_PROFILE

    atif_data = {
        "trial_id": "trial_001",
        "steps": [
            # Step 0: Initial autonomous attempt fails
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [{"tool_name": "bash", "command": "python -m broken_module"}],
                "observations": [{"exit_code": 1, "error": "ModuleNotFoundError"}],
            },
            # Step 1: Autonomous retry without intervention
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [{"tool_name": "bash", "command": "pip install broken_module"}],
                "observations": [{"exit_code": 0, "output": "Installed"}],
            },
            # Step 2: User interjection / hint
            {
                "step_id": 2,
                "source": "user",
                "message": "Remember to run tests with pytest -v",
            },
            # Step 3: Action following user intervention
            {
                "step_id": 3,
                "source": "agent",
                "tool_calls": [{"tool_name": "bash", "command": "pytest -v"}],
                "observations": [{"exit_code": 0, "output": "5 passed"}],
            },
        ],
    }

    facts = extract_semantic_actions(atif_data, profile, strict=True)
    assert len(facts) == 3

    # Action 0 was autonomous
    assert facts[0].sequence == 0
    assert facts[0].intervention_provenance == "autonomous"
    assert facts[0].outcome == "error"

    # Action 1 was autonomous recovery
    assert facts[1].sequence == 1
    assert facts[1].intervention_provenance == "autonomous"
    assert facts[1].outcome == "success"

    # Action 2 was user-assisted recovery
    assert facts[2].sequence == 2
    assert facts[2].intervention_provenance == "user_assisted"
    assert facts[2].intervention_detail == "Remember to run tests with pytest -v"
    assert facts[2].outcome == "success"


def test_unknown_tool_strict_vs_permissive() -> None:
    """Strict mode raises UnmappedActionError; permissive mode emits unknown_semantics with reason."""
    profile = TrajectorySemanticsProfile(
        profile_id="custom-restricted",
        version="1.0.0",
        description="Restricted profile with only read capability",
        tool_rules=(ToolMappingRule("read", "read", default_outcome_resolver),),
    )

    atif_data = {
        "trial_id": "trial_unk",
        "steps": [
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [{"tool_name": "unregistered_custom_tool", "arg": "val"}],
                "observations": [{"status": "done"}],
            }
        ],
    }

    # Strict mode fails closed
    with pytest.raises(UnmappedActionError, match="has no mapping rule for tool 'unregistered_custom_tool'"):
        extract_semantic_actions(atif_data, profile, strict=True)

    # Permissive mode emits unknown_semantics fact
    facts = extract_semantic_actions(atif_data, profile, strict=False)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.role == "other"
    assert fact.outcome == "unknown_semantics"
    assert fact.outcome_detail == "unmapped_tool:unregistered_custom_tool"


def test_profile_version_digest_change() -> None:
    """Any modification in profile rules or version changes the deterministic profile_digest."""
    p1 = TrajectorySemanticsProfile(
        profile_id="posix",
        version="1.0.0",
        description="Version 1",
        tool_rules=(ToolMappingRule("read", "read"),),
    )

    p2 = TrajectorySemanticsProfile(
        profile_id="posix",
        version="1.0.1",
        description="Version 1.0.1 patch",
        tool_rules=(ToolMappingRule("read", "read"),),
    )

    p3 = TrajectorySemanticsProfile(
        profile_id="posix",
        version="1.0.0",
        description="Version 1 with added rule",
        tool_rules=(ToolMappingRule("read", "read"), ToolMappingRule("write", "write")),
    )

    assert p1.digest.startswith("sha256:")
    assert p2.digest.startswith("sha256:")
    assert p3.digest.startswith("sha256:")

    assert p1.digest != p2.digest
    assert p1.digest != p3.digest
    assert p2.digest != p3.digest


def test_deterministic_shuffled_input_parquet_projection(tmp_path: Path) -> None:
    """Projecting shuffled action lists produces identical Parquet bytes."""
    profile = GENERIC_POSIX_PROFILE
    atif_data = {
        "trial_id": "trial_det",
        "steps": [
            {
                "step_id": i,
                "source": "agent",
                "tool_calls": [{"tool_name": "bash", "command": f"echo step_{i}"}],
                "observations": [{"exit_code": 0, "output": f"step_{i}"}],
            }
            for i in range(10)
        ],
    }

    facts_original = extract_semantic_actions(atif_data, profile)

    facts_shuffled = list(facts_original)
    random.seed(42)
    random.shuffle(facts_shuffled)

    out1 = project_semantic_actions_parquet(facts_original, tmp_path / "t1.parquet")
    out2 = project_semantic_actions_parquet(facts_shuffled, tmp_path / "t2.parquet")

    bytes1 = out1.read_bytes()
    bytes2 = out2.read_bytes()

    assert bytes1 == bytes2
    assert len(bytes1) > 0

    table = pq.read_table(out1)
    assert table.num_rows == 10
    assert table.schema == SEMANTIC_ACTION_FACT_SCHEMA


def test_no_raw_secret_or_argument_text_in_projection(tmp_path: Path) -> None:
    """Sensitive argument strings and passwords are never written to the Parquet projection."""
    secret_token = "SUPER_SECRET_OAUTH_BEARER_TOKEN_999888"
    secret_prompt = "INTERNAL_SYSTEM_PROMPT_WITH_CONFIDENTIAL_INSTRUCTIONS"

    atif_data = {
        "trial_id": "trial_sec",
        "steps": [
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_name": "bash",
                        "command": f"curl -H 'Authorization: Bearer {secret_token}' https://api.example.com",
                    }
                ],
                "observations": [{"exit_code": 0, "output": f"Success: {secret_prompt}"}],
            }
        ],
    }

    facts = extract_semantic_actions(atif_data, GENERIC_POSIX_PROFILE)
    out = project_semantic_actions_parquet(facts, tmp_path / "sec.parquet")

    raw_bytes = out.read_bytes()
    raw_text = raw_bytes.decode("latin1", errors="ignore")

    assert secret_token not in raw_text
    assert secret_prompt not in raw_text

    # The SHA256 digest is present instead
    assert facts[0].arguments_sha256.startswith("sha256:")
    assert facts[0].observation_sha256.startswith("sha256:")

def test_benchmark_profiles_resolution() -> None:
    """LOCA, AgentAbstain, and DeepPlanning profiles resolve domain actions with no fallback."""
    from evallab.trajectory_semantics import (
        AGENTABSTAIN_PROFILE,
        DEEPPLANNING_PROFILE,
        LOCA_PROFILE,
        get_profile,
    )

    assert get_profile("loca") == LOCA_PROFILE
    assert get_profile("agentabstain") == AGENTABSTAIN_PROFILE
    assert get_profile("deepplanning") == DEEPPLANNING_PROFILE

    # 1. LOCA long-context tool sequence
    loca_atif = {
        "trial_id": "loca_trial_1",
        "steps": [
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [{"tool_name": "search_files", "pattern": "*.py"}],
                "observations": [{"status": "ok", "files": ["main.py", "eval.py"]}],
            },
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [{"tool_name": "retrieve_context", "query": "find auth function"}],
                "observations": [{"status": "ok", "context": "def authenticate(): ..."}],
            },
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [{"tool_name": "submit", "answer": "auth in main.py"}],
                "observations": [{"status": "ok"}],
            },
        ],
    }
    loca_facts = extract_semantic_actions(loca_atif, LOCA_PROFILE, strict=True)
    assert len(loca_facts) == 3
    assert [f.role for f in loca_facts] == ["search", "read", "terminate"]
    assert [f.outcome for f in loca_facts] == ["success", "success", "success"]

    # 2. AgentAbstain ambiguous vs critical actions
    abstain_atif = {
        "trial_id": "agentabstain_trial_1",
        "steps": [
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [{"tool_name": "spotify.get_playlist_tracks", "playlist_id": "pl1"}],
                "observations": [{"tracks": ["Song A", "Song B"]}],
            },
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [{"tool_name": "spotify.write_gmail_draft", "subject": "Songs", "body": "Tracks"}],
                "observations": [{"draft_id": "d1", "status": "draft_saved"}],
            },
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [{"tool_name": "abstain", "reason": "Ambiguous action avoided"}],
                "observations": [{"status": "recorded"}],
            },
        ],
    }
    abstain_facts = extract_semantic_actions(abstain_atif, AGENTABSTAIN_PROFILE, strict=True)
    assert len(abstain_facts) == 3
    assert [f.role for f in abstain_facts] == ["read", "write", "terminate"]
    assert [f.outcome for f in abstain_facts] == ["success", "success", "success"]

    # 3. DeepPlanning shopping and constraints workflow
    planning_atif = {
        "trial_id": "deepplanning_trial_1",
        "steps": [
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [{"tool_name": "search_items", "category": "electronics"}],
                "observations": [{"items": [{"id": 1, "price": 500}]}],
            },
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [{"tool_name": "check_constraints", "budget": 1000, "items": [1]}],
                "observations": [{"valid": True, "remaining": 500}],
            },
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [{"tool_name": "add_to_cart", "item_id": 1}],
                "observations": [{"status": "added"}],
            },
            {
                "step_id": 3,
                "source": "agent",
                "tool_calls": [{"tool_name": "submit_plan", "plan_id": "p1"}],
                "observations": [{"status": "accepted"}],
            },
        ],
    }
    planning_facts = extract_semantic_actions(planning_atif, DEEPPLANNING_PROFILE, strict=True)
    assert len(planning_facts) == 4
    assert [f.role for f in planning_facts] == ["search", "inspect", "execute", "terminate"]
    assert [f.outcome for f in planning_facts] == ["success", "success", "success", "success"]
