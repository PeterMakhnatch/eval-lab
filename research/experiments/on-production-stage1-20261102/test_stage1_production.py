"""Unit tests for D9 production stage 1 analysis and runner."""
import json
from pathlib import Path

from evallab.autoreview_monitor import build_compact_view, stage1_prompt
from evallab.trace_anatomy import LoadedRun, build_anatomy


def test_stage1_prompt_structure():
    doc = {
        "metadata": {"split": "dev"},
        "steps": [
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [{"tool": "bash", "command": "echo hello", "exit_code": 0}],
                "observation": {"results": ["hello\n"]},
            }
        ],
    }
    run = LoadedRun("test-run", "atif", doc, None, None, 100)
    anatomy = build_anatomy(run)
    view = build_compact_view(anatomy, run)
    prompt = stage1_prompt(view)

    assert "<view>" in prompt
    assert "</view>" in prompt
    assert "Categories of Cheating:" in prompt
    assert "## Action index" in prompt
    assert "## Backcheck" in prompt
    assert "## Mutation ledger" in prompt


def test_results_integrity():
    res_path = Path("research/experiments/on-production-stage1-20261102/production_stage1_results.json")
    assert res_path.exists(), "Results JSON must exist"
    data = json.loads(res_path.read_text())

    assert data["cohort_size"] == 272
    assert "T0_stored" in data["arms"]
    assert "T1_today" in data["arms"]
    assert "C1_25_lite" in data["arms"]
    assert "C2_31_lite" in data["arms"]

    # Verify decision rules presence
    dr = data["decision_rules"]
    assert "H1_d2_not_production_equivalent" in dr
    assert "H1_prime_code_drift" in dr
    assert "H2_cheap_arms_viable" in dr

    # Verify H1 is true
    assert dr["H1_d2_not_production_equivalent"]["verdict"] is True

    # Verify attestation counts
    t1_att = data["arms"]["T1_today"]["attestation"]
    assert t1_att["attested"] == 272
    c1_att = data["arms"]["C1_25_lite"]["attestation"]
    assert c1_att["attested"] == 272
    c2_att = data["arms"]["C2_31_lite"]["attestation"]
    assert c2_att["attested"] == 271
