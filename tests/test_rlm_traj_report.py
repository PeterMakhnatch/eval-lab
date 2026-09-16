"""Drift taxonomy contract for the RLM trajectory report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.rlm.traj_report import aggregate, classify_drift, load_bench


@pytest.mark.parametrize(
    ("head", "kind"),
    [
        (
            "Reasoning: parsed rows.\\n\\nCode:\\n```python\\nprint(1)\\n```",
            "mirrored-history-format",
        ),
        (
            "[[ ## reasoning ## ]]\\nBoth agree.\\nCode:\\n```python\\nSUBMIT('4')\\n```",
            "reasoning-marker-plus-code-label",
        ),
        ("Let me verify first.\\n```python\\nprint(x)\\n```", "preamble-plus-fenced-code"),
        (
            "The previous step executed successfully; let me do a final read-back.",
            "prose-without-code",
        ),
        (
            "Plan first.\\n[[ ## code ## ]]\\n```python\\nprint(1)\\n```",
            "code-marker-without-reasoning-marker",
        ),
    ],
)
def test_classify_drift_buckets_observed_shapes(head: str, kind: str) -> None:
    assert classify_drift(head) == kind


def test_bench_rows_aggregate_drift_salvage_and_tool_usage(tmp_path: Path) -> None:
    traj = tmp_path / "traj"
    traj.mkdir()
    steps = [
        {"reasoning": "", "code": "print(len(context))", "output": "120000"},
        {
            "reasoning": "",
            "code": "# (no code executed: previous response was not parseable)",
            "output": "[Error] Your previous response could not be parsed into the required fields. Unparsed response head: 'Reasoning: x\\n\\nCode:\\n```python\\nprint(1)\\n```'",
        },
        {
            "reasoning": "",
            "code": "labels = llm_query_batched(prompts)\nprint(labels[:2])",
            "output": "[Error] NameError: prompts",
        },
        {"reasoning": "", "code": "SUBMIT('4')", "output": "FINAL: {'answer': '4'}"},
    ]
    (traj / "t1.json").write_text(json.dumps({"trajectory": steps}))
    row = {
        "policy": "stock",
        "task_id": "t1",
        "family": "ledger-agg",
        "score": 1.0,
        "iterations": 4,
        "cost_usd": 0.05,
        "parse_failures": 1,
        "salvaged_actions": 0,
        "error": None,
        "trajectory_path": str(traj / "t1.json"),
    }
    (tmp_path / "stock-s1-r1.jsonl").write_text(json.dumps(row) + "\n")
    summary = aggregate(load_bench(tmp_path))["bench:stock"]
    assert summary["drift_by_kind"] == {"mirrored-history-format": 1}
    assert summary["repl_errors"] == 1 and summary["runs_with_drift"] == 1
    assert summary["tool_steps"]["llm_query_batched"] == 1 and summary["tool_steps"]["SUBMIT"] == 1
    assert "sandbox_open" not in summary["tool_steps"]
