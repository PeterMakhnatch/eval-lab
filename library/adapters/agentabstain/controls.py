"""Offline oracle, NOP, and mutant controls for the canary pair."""
from __future__ import annotations

import copy
from typing import Any

from .adapter import load_variants, primary_verdict


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


def evaluate(seed: dict[str, Any]) -> dict[str, dict[str, str]]:
    act, abstain = load_variants()
    changed = copy.deepcopy(seed)
    changed["gmail"]["drafts"]["draft_katie_001"]["body"] += "\nmutant"
    rows = {
        "act_oracle": primary_verdict(act, oracle_calls("act"), initial_state=seed, final_state=changed),
        "act_nop": primary_verdict(act, nop_calls(), initial_state=seed, final_state=seed),
        "abstain_oracle": primary_verdict(abstain, oracle_calls("abstain"), initial_state=seed, final_state=seed),
        "abstain_nop": primary_verdict(abstain, nop_calls(), initial_state=seed, final_state=seed),
        "abstain_mutant": primary_verdict(abstain, mutant_calls(), initial_state=seed, final_state=changed),
    }
    return {name: value for name, value in rows.items()}
