"""TT-8 product trials: ATIF mapping, run-level flag rules, result rows, spend ledgers."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from evallab.readers import openrouter_ledger_proxy as proxy
from evallab.readers import trace_product_trials as trials
from evallab.readers.tool_scorecard import load_results

ATIF = {
    "steps": [
        {"step_id": 1, "source": "system", "message": "You are an agent."},
        {"step_id": 2, "source": "user", "message": "Fix the bug."},
        {
            "step_id": 3,
            "source": "agent",
            "message": "Look at the tests.",
            "tool_calls": [{"function_name": "bash", "arguments": {"command": "ls"}}],
        },
        {
            "step_id": 4,
            "source": "agent",
            "message": "",
            "observation": {"results": [{"content": "a.py"}]},
        },
        {
            "step_id": 5,
            "source": "agent",
            "message": "",
            "observation": {"results": [{"content": ""}]},
        },
        {"step_id": 6, "source": "agent", "message": "Done."},
    ],
}


def test_atif_to_hodoscope_keeps_text_calls_and_feedback_with_step_map() -> None:
    trajectory, steps = trials.atif_to_hodoscope(ATIF, run_id="run-a", cohort="c1")

    assert trajectory["id"] == "run-a"
    assert trajectory["metadata"] == {"cohort": "c1"}
    roles = [message["role"] for message in trajectory["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    action = trajectory["messages"][2]["content"]
    assert action.startswith("Look at the tests.")
    assert 'TOOL_CALL: bash {"command": "ls"}' in action
    assert trajectory["messages"][3]["content"] == "a.py"
    assert steps == [1, 2, 3, 4, 6]


def _ranked(*groups: tuple[str, list[tuple[int, str, int]]]) -> dict:
    return {
        "groups": {
            name: {
                "samples": [
                    {"rank": rank, "trajectory_id": run, "turn_id": turn, "summary": f"s{rank}"}
                    for rank, run, turn in samples
                ]
            }
            for name, samples in groups
        },
    }


def test_flagged_runs_uses_top_n_per_cohort_and_best_rank() -> None:
    ranked = _ranked(
        ("c1", [(0, "run-a", 4), (1, "run-b", 2), (2, "run-a", 9)]),
        ("c2", [(0, "run-c", 1), (1, "run-b", 7)]),
    )

    top1 = trials.flagged_runs(ranked, 1)
    assert set(top1) == {"run-a", "run-c"}

    top2 = trials.flagged_runs(ranked, 2)
    assert set(top2) == {"run-a", "run-b", "run-c"}
    assert top2["run-b"]["rank"] == 2
    assert top2["run-b"]["cohort"] == "c1"


def test_pxi_flags_separate_flagged_reviewed_and_unread_runs() -> None:
    runs = ["run-a", "run-b", "run-c"]
    reply = 'ok\n{"flagged": [{"run_id": "run-a", "evidence": "x"}], "reviewed": ["run-b"]}'

    assert trials.pxi_flags(reply, runs) == {"run-a": True, "run-b": False, "run-c": None}
    assert trials.pxi_flags("no json here", runs) == {"run-a": None, "run-b": None, "run-c": None}


def test_result_rows_round_trip_through_the_scorecard_loader(tmp_path) -> None:
    at = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    rows = [
        trials.result_row(
            run_id="r1",
            tool="hodoscope",
            detector="top50",
            detector_version="v",
            flag=True,
            value={},
            evidence_steps=[3],
            scan="s",
            at=at,
        ),
        trials.result_row(
            run_id="r2",
            tool="hodoscope",
            detector="top50",
            detector_version="v",
            flag=None,
            value={},
            evidence_steps=[],
            scan="s",
            at=at,
        ),
    ]
    store = tmp_path / "results.jsonl"
    store.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    assert load_results(store, tool="hodoscope") == {
        ("hodoscope", "top50"): {"r1": True, "r2": None}
    }
    with pytest.raises(ValueError, match="flag must be"):
        trials.result_row(
            run_id="r",
            tool="t",
            detector="d",
            detector_version="v",
            flag="yes",  # type: ignore[arg-type]
            value={},
            evidence_steps=[],
            scan="s",
        )


def test_call_ledger_caps_spend_and_flags_served_model_mismatch(tmp_path) -> None:
    ledger = trials.CallLedger(
        tmp_path / "calls.jsonl",
        cap_usd=0.01,
        list_usd_per_mtok_in={"text-embedding-3-large": 0.13},
    )
    ledger.record(
        requested="openai/gpt-4o-mini",
        served="openai/gpt-4o-mini-2024-07-18",
        usage={"prompt_tokens": 10, "completion_tokens": 2},
        cost=0.004,
    )
    ledger.record(
        requested="openai/text-embedding-3-large",
        served="text-embedding-3-large",
        usage={"prompt_tokens": 1_000_000},
        cost=None,
    )
    ledger.record(requested="openai/gpt-4o-mini", served="other/model", usage={}, cost=0.0)

    summary = ledger.summary()
    assert summary["reported_usd"] == 0.004
    assert summary["estimated_usd"] == 0.13
    assert summary["served_mismatch_calls"] == 1
    assert trials.ledger_spend(tmp_path / "calls.jsonl") == 0.004
    with pytest.raises(RuntimeError, match="cap"):
        ledger.guard()


def test_proxy_parse_response_reads_json_and_both_streaming_shapes() -> None:
    plain = json.dumps(
        {
            "model": "m",
            "id": "g1",
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.5},
        }
    )
    parsed = proxy.parse_response(plain.encode())
    assert (parsed["served"], parsed["input_tokens"], parsed["cost_usd"]) == ("m", 5, 0.5)

    chat = 'data: {"model":"a","choices":[]}\n\ndata: {"model":"a","usage":{"prompt_tokens":3,"cost":0.2}}\n\ndata: [DONE]\n'
    assert proxy.parse_response(chat.encode())["cost_usd"] == 0.2

    responses = 'data: {"type":"response.completed","response":{"model":"b","id":"r","usage":{"input_tokens":7,"output_tokens":2,"cost":0.1}}}\n'
    parsed = proxy.parse_response(responses.encode())
    assert (parsed["served"], parsed["input_tokens"], parsed["output_tokens"]) == ("b", 7, 2)


def test_hodoscope_rows_flag_clear_and_abstain_on_incomplete_runs() -> None:
    ranked = _ranked(("c1", [(0, "run-a", 1), (1, "run-b", 0)]))
    summaries = [
        {"trajectory_id": "run-a", "embedding": [0.1]},
        {"trajectory_id": "run-b", "embedding": [0.1]},
        {"trajectory_id": "run-c", "embedding": [0.1]},
        {"trajectory_id": "run-c", "embedding": None},
    ]
    runs = {"run-a": "A", "run-b": "B", "run-c": "C"}
    rows = trials.hodoscope_rows(
        summaries=summaries,
        ranked=ranked,
        runs=runs,
        step_maps={"run-a": [10, 20], "run-b": [30], "run-c": [40, 50]},
        action_counts={"run-a": 1, "run-b": 1, "run-c": 2},
        top_ns=[1, 2],
        scan="s",
        at=datetime(2026, 10, 28, tzinfo=UTC),
    )

    flags = {(row["detector"], row["run_id"]): row["flag"] for row in rows}
    assert flags == {
        ("top1_per_cohort", "A"): True,
        ("top1_per_cohort", "B"): False,
        ("top1_per_cohort", "C"): None,
        ("top2_per_cohort", "A"): True,
        ("top2_per_cohort", "B"): True,
        ("top2_per_cohort", "C"): None,
    }
    flagged_a = next(
        row for row in rows if row["run_id"] == "A" and row["detector"] == "top1_per_cohort"
    )
    assert flagged_a["evidence_steps"] == [20]


def test_pxi_rows_use_the_reviewed_ledger_and_ignore_unknown_ids() -> None:
    answer = {
        "flagged": [
            {"run_id": "run-a", "evidence": "edited tests"},
            {"run_id": "run-x", "evidence": "?"},
        ],
        "reviewed": ["run-a", "run-b", "run-x"],
    }
    rows = trials.pxi_rows(
        answer=answer,
        runs={"run-a": "A", "run-b": "B", "run-c": "C"},
        detector="generic_investigation_r1",
        usage={"turns": 1},
        scan="s",
        at=datetime(2026, 10, 28, tzinfo=UTC),
    )

    assert {row["run_id"]: row["flag"] for row in rows} == {"A": True, "B": False, "C": None}
    assert rows[0]["value"]["evidence"] == "edited tests"
    assert {
        row["flag"]
        for row in trials.pxi_rows(
            answer=None,
            runs={"run-a": "A"},
            detector="d",
            usage={},
            scan="s",
            at=datetime(2026, 10, 28, tzinfo=UTC),
        )
    } == {None}
