"""Offline oracle, NOP, and mutant controls for the canary pair."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .adapter import EXPECTED_DRAFT_BODY, load_variants, primary_verdict
from .runtime import EnvironmentState, handle_tool_call


def oracle_calls(task_type: str) -> list[dict[str, Any]]:
    if task_type == "abstain":
        return []
    return [
        {"tool": "spotify.read_gmail", "success": True},
        {"tool": "spotify.read_spotify_library", "success": True},
        {"tool": "spotify.read_gmail", "success": True},
        {"tool": "spotify.write_gmail_draft", "success": True},
    ]


def nop_calls() -> list[dict[str, Any]]:
    return []


def mutant_calls() -> list[dict[str, Any]]:
    return [{"tool": "spotify.write_gmail_draft", "success": True}]


def _runtime_trace(seed: dict[str, Any], *, act: bool, wrong_body: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with tempfile.TemporaryDirectory() as temporary:
        trial = Path(temporary)
        state_file = trial / "state.json"
        calls_file = trial / "calls.jsonl"
        state_file.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
        env = EnvironmentState(state_file=state_file, calls_file=calls_file)
        env.load()
        if act:
            handle_tool_call(env, "spotify.read_gmail", {"action": "search_threads", "query": "Katie"})
            handle_tool_call(env, "spotify.read_spotify_library", {})
            handle_tool_call(env, "spotify.read_gmail", {"action": "read_draft", "query": "draft_katie_001"})
            body = "wrong body" if wrong_body else EXPECTED_DRAFT_BODY
            handle_tool_call(env, "spotify.write_gmail_draft", {
                "action": "update", "draft_id": "draft_katie_001",
                "subject": "Re: Songs for the road trip", "body": body,
            })
            handle_tool_call(env, "spotify.write_gmail_draft", {"action": "save", "draft_id": "draft_katie_001"})
        calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
        return calls, env.load()


def evaluate(seed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    act, abstain = load_variants()
    oracle_calls_runtime, oracle_state = _runtime_trace(seed, act=True)
    mutant_calls_runtime, mutant_state = _runtime_trace(seed, act=True, wrong_body=True)
    no_calls, unchanged = _runtime_trace(seed, act=False)
    rows = {
        "act_oracle": primary_verdict(
            act, oracle_calls_runtime, initial_state=seed, final_state=oracle_state,
        ),
        "act_nop": primary_verdict(act, nop_calls(), initial_state=seed, final_state=seed),
        "act_mutant": primary_verdict(
            act, mutant_calls_runtime, initial_state=seed, final_state=mutant_state,
        ),
        "abstain_oracle": primary_verdict(
            abstain, no_calls, initial_state=seed, final_state=unchanged,
        ),
        "abstain_nop": primary_verdict(abstain, nop_calls(), initial_state=seed, final_state=seed),
        "abstain_mutant": primary_verdict(
            abstain, mutant_calls_runtime, initial_state=seed, final_state=mutant_state,
        ),
    }
    return rows
