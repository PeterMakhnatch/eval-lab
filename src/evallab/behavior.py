"""Behavioral analysis over the unified attach surface (trial_facts, steps, tool_calls).

Analyzes how agents work (steps, tools, LLMs, latency, sources, tokens, cost, struggle signals)
split by outcome (passed / scored-zero / never-measured). Degenerate efficiency ratios are
represented as undefined (None), sparse token columns are explicitly reported with coverage counts,
and underpowered comparisons render 'not distinguishable'.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_POWER_THRESHOLD = 5
SQL_BEHAVIOR_PATH = Path("sql/behavior.sql")


