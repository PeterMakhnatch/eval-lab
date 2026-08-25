"""Offline oracle/no-op controls for every AgentAbstain pair."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .agentabstain import TaskVariant, load_variants, primary_verdict, validate_corpus


def oracle_calls(variant: TaskVariant) -> list[dict[str, Any]]:
    """Reference should-act trace: one successful call per critical action."""
    if variant.task_type == "abstain":
        return []
    return [{"call_id": f"oracle-{index}", "tool": tool, "success": True, "arguments": {}}
             for index, tool in enumerate(variant.critical_actions, start=1)]


def nop_calls(_: TaskVariant) -> list[dict[str, Any]]:
    """No-op control: no tool calls and therefore no state mutation."""
    return []


def run_controls(corpus: str | Path) -> dict[str, Any]:
    variants = load_variants(corpus)
    validate_corpus(variants)
    rows = []
    for variant in variants:
        calls = oracle_calls(variant) if variant.task_type == "act" else nop_calls(variant)
        verdict = primary_verdict(variant, calls, initial_state={}, final_state={})
        rows.append({"pair_id": variant.pair_id, "task_type": variant.task_type,
                     "control": "oracle" if variant.task_type == "act" else "nop",
                     "primary_verdict": verdict.verdict})
    return {"schema_version": "agentabstain-controls/v1", "rows": rows}


if __name__ == "__main__":
    import argparse, json
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", type=Path)
    args = parser.parse_args()
    print(json.dumps(run_controls(args.corpus), indent=2, sort_keys=True))
