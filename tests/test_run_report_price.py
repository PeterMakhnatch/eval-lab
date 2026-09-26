"""Run-report price-table estimates: precedence, matching, markdown, rollup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.interpretation.price_table import (
    estimate_cost_usd,
    lookup_price,
    normalize_model_id,
)
from evallab.interpretation.run_report import (
    build_job_report,
    build_run_report,
    render_job_report_markdown,
    render_run_report_markdown,
)

ZAI_MODEL = "zai-coding-plan/glm-5.3-flash"
ZAI_URL = "https://docs.z.ai/guides/overview/pricing.md"


def _result(
    model: str | None = ZAI_MODEL,
    agent_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "trial-id",
        "trial_name": "trial",
        "task_name": "lab/task",
        "config": {"agent": {"name": "opencode", "model_name": model}},
        "agent_info": {"name": "opencode", "version": "1.3.9"},
        "agent_result": (
            agent_result
            if agent_result is not None
            else {
                "n_input_tokens": 3000,
                "n_cache_tokens": 1000,
                "n_output_tokens": 300,
                "cost_usd": None,
            }
        ),
        "verifier_result": {"rewards": {"reward": 1.0}},
        "started_at": "2026-09-01T00:00:00Z",
        "finished_at": "2026-09-01T00:10:00Z",
    }
    return body


def _trial(
    root: Path,
    result: dict[str, Any],
    *,
    name: str = "trial",
    steps: list[dict[str, Any]] | None = None,
) -> Path:
    trial = root / name
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({**result, "trial_name": name}), encoding="utf-8"
    )
    if steps is not None:
        agent = trial / "agent"
        agent.mkdir()
        doc = {
            "schema_version": "ATIF-v1.7",
            "session_id": "s",
            "agent": {"name": "opencode", "version": "1.3.9"},
            "steps": [
                {
                    "step_id": 0,
                    "source": "user",
                    "message": "Do it",
                    "timestamp": "2026-09-01T00:02:00Z",
                },
                *steps,
            ],
        }
        (agent / "trajectory.json").write_text(json.dumps(doc), encoding="utf-8")
    return trial


def _metered_step(step_id: int, **metrics: Any) -> dict[str, Any]:
    call_id = f"call-{step_id}"
    return {
        "step_id": step_id,
        "timestamp": f"2026-09-01T00:02:{step_id:02d}Z",
        "source": "agent",
        "message": "",
        "tool_calls": [
            {"tool_call_id": call_id, "function_name": "bash", "arguments": {"command": "ls"}}
        ],
        "observation": {
            "results": [
                {
                    "source_call_id": call_id,
                    "content": json.dumps({"returncode": 0, "output": "ok"}),
                }
            ]
        },
        "metrics": {"prompt_tokens": 1000, "completion_tokens": 100, **metrics},
    }


def test_harness_cost_wins_over_price_table(tmp_path: Path) -> None:
    metered = _result(
        agent_result={
            "n_input_tokens": 3000,
            "n_cache_tokens": 1000,
            "n_output_tokens": 300,
            "cost_usd": 0.5,
        }
    )
    report = build_run_report(_trial(tmp_path, metered))
    assert (report["cost"]["total_usd"], report["cost"]["source"]) == (0.5, "result_json")
    assert report["cost"]["price_table"] is None
    assert not any("cost unavailable" in q for q in report["data_quality"])


def test_step_sum_cost_wins_over_price_table(tmp_path: Path) -> None:
    result = _result(
        agent_result={
            "n_input_tokens": 3000,
            "n_cache_tokens": 1000,
            "n_output_tokens": 300,
            "cost_usd": None,
        }
    )
    report = build_run_report(
        _trial(tmp_path, result, steps=[_metered_step(1, cost_usd=0.1)])
    )
    assert report["cost"]["source"] == "step_sum"
    assert report["cost"]["price_table"] is None


def test_estimate_prices_cached_input_at_cached_rate(tmp_path: Path) -> None:
    report = build_run_report(_trial(tmp_path, _result()))
    cost = report["cost"]
    # (2000 uncached * 0.15 + 1000 cached * 0.03 + 300 output * 0.50) / 1e6
    assert cost["total_usd"] == 0.00048
    assert cost["source"] == "price_table_estimate"
    assert cost["price_table"] == {
        "model": ZAI_MODEL,
        "matched_key": "glm-5.3-flash",
        "source_url": ZAI_URL,
        "retrieved_on": "2026-09-26",
        "usd_per_mtok": {"input": 0.15, "cached_input": 0.03, "output": 0.50},
    }
    assert not any("cost unavailable" in q for q in report["data_quality"])


def test_unknown_model_stays_unknown_with_reason(tmp_path: Path) -> None:
    report = build_run_report(_trial(tmp_path, _result(model="mystery/model-x")))
    assert report["cost"]["total_usd"] is None
    assert report["cost"]["source"] is None
    assert report["cost"]["price_table"] is None
    assert any(
        "cost unavailable" in q and "mystery/model-x" in q
        for q in report["data_quality"]
    )


def test_tier_suffix_does_not_fuzzy_match(tmp_path: Path) -> None:
    report = build_run_report(
        _trial(tmp_path, _result(model="google/gemini-3.7-flash-high"))
    )
    assert report["cost"]["total_usd"] is None
    assert any("no pinned price-table entry" in q for q in report["data_quality"])
    # The bare fixture alias from the older test suite is not a table key either.
    assert lookup_price("zai/glm") is None


def test_partial_token_counts_yield_no_estimate(tmp_path: Path) -> None:
    no_output = _result(
        agent_result={
            "n_input_tokens": 3000,
            "n_cache_tokens": None,
            "n_output_tokens": None,
            "cost_usd": None,
        }
    )
    report = build_run_report(_trial(tmp_path, no_output, name="a"))
    assert report["cost"]["total_usd"] is None
    assert any("token counts are missing" in q for q in report["data_quality"])

    no_input = _result(
        agent_result={
            "n_input_tokens": None,
            "n_cache_tokens": None,
            "n_output_tokens": 300,
            "cost_usd": None,
        }
    )
    report_b = build_run_report(_trial(tmp_path, no_input, name="b"))
    assert report_b["cost"]["total_usd"] is None
    assert any("token counts are missing" in q for q in report_b["data_quality"])


def test_markdown_labels_estimate_with_provenance(tmp_path: Path) -> None:
    estimated = build_run_report(_trial(tmp_path, _result()))
    md = render_run_report_markdown(estimated)
    assert "$0.0005 (estimate)" in md
    assert "price_table_estimate" in md
    assert ZAI_URL in md
    assert "Not a metered charge" in md


def test_markdown_leaves_harness_cost_unmarked(tmp_path: Path) -> None:
    metered = _result(
        agent_result={
            "n_input_tokens": 3000,
            "n_cache_tokens": 1000,
            "n_output_tokens": 300,
            "cost_usd": 0.5,
        }
    )
    md = render_run_report_markdown(build_run_report(_trial(tmp_path, metered)))
    assert "(estimate)" not in md
    assert ZAI_URL not in md


def test_rollup_never_mixes_estimates_with_harness_cost(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    metered = _result(
        agent_result={
            "n_input_tokens": 3000,
            "n_cache_tokens": 1000,
            "n_output_tokens": 300,
            "cost_usd": 0.5,
        }
    )
    _trial(job, metered, name="trial-a")
    _trial(job, _result(), name="trial-b")
    rollup, _ = build_job_report(job)
    assert rollup["total_cost_usd"] == 0.5
    assert rollup["cost_per_pass_usd"] == 0.25
    assert rollup["trials_without_cost"] == 0
    assert rollup["trials_with_estimated_cost"] == 1
    assert rollup["estimated_cost_usd"] == 0.00048
    md = render_job_report_markdown(rollup)
    assert "(est)" in md
    assert "Price-table estimates $0.0005" in md


def test_model_matching_is_exact_after_prefix_normalization() -> None:
    assert normalize_model_id("zai-coding-plan/glm-5.3-flash") == "glm-5.3-flash"
    assert normalize_model_id("ZAI/GLM-5.3-Flash ") == "glm-5.3-flash"
    assert lookup_price("openai/gpt-4o-mini") is not None
    assert lookup_price("google/gemini-3.7-flash") is not None
    assert lookup_price("deepseek/deepseek-flash") is None
    assert lookup_price(None) is None
    assert lookup_price("") is None


def test_estimate_refuses_inconsistent_counts() -> None:
    price = lookup_price("glm-5.3-flash")
    assert price is not None
    assert estimate_cost_usd(100, 200, 10, price) is None
    assert estimate_cost_usd(-1, 0, 10, price) is None
    assert estimate_cost_usd(100, 0, None, price) is None
    assert estimate_cost_usd(100, None, 10, price) == estimate_cost_usd(
        100, 0, 10, price
    )
