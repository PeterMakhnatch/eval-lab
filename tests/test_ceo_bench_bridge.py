"""CEO-Bench bridge: conversion, spend separation, bankruptcy/no-op, malformed inputs.

Harness log shapes mirror the upstream writers (cited per builder):
config.json / checkpoint.json keys from
``src/saas_bench/agents/bash_agent/run_test.py`` (config writer ~line 977,
checkpoint writer ~line 689); tool_results entries (``_log_tool_result``
~line 345: timestamp/turn/day/tool/arguments/result); timing events
(``_log_timing`` ~line 358 with ``day_summary`` ~line 1269 and ``llm_call``
~line 1131). SQLite fixtures use minimal tables with the columns the bridge
selects (upstream DDL: ``src/saas_bench/database.py`` ledger ~line 443,
api_costs ~line 660, predictions ~line 1041); they exercise the SQL reader
path, not the upstream schema itself.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

RUN_ID = "ab12cd34"
STAMP = "2026-09-20T10:00:00Z"
ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from evallab.interpretation.run_report import build_run_report  # noqa: E402
from library.adapters.ceo_bench.bridge import (  # noqa: E402
    CeoBenchBridgeError,
    bridge_ceo_bench_run,
    is_completed_week_advance,
    is_state_changing_call,
)


def _config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "run_id": RUN_ID,
        "model": "test-model",
        "provider": "test-provider",
        "seed": 7,
        "scenario": "default",
        "total_days": 500,
        "initial_cash": 1000000.0,
        "agent_type": "bash_agent",
        "session_id": "sess1",
        "label": None,
    }
    config.update(overrides)
    return config


def _checkpoint(**overrides: Any) -> dict[str, Any]:
    checkpoint: dict[str, Any] = {
        "day": 14,
        "run_id": RUN_ID,
        "model": "test-model",
        "provider": "test-provider",
        "seed": 7,
        "scenario": "default",
        "agent_total_turns": 6,
        "total_input_tokens": 60000,
        "total_output_tokens": 9000,
        "total_cached_tokens": 15000,
        "total_reasoning_tokens": 4000,
        "total_anthropic_fallbacks": 0,
        "daily_scripts": {},
        "session_id": "sess1",
    }
    checkpoint.update(overrides)
    return checkpoint


def _tool(
    turn: int, day: int, tool: str, arguments: Any, result: Any
) -> dict[str, Any]:
    return {
        "timestamp": STAMP,
        "turn": turn,
        "day": day,
        "tool": tool,
        "arguments": arguments,
        "result": result,
    }


def _bash(day: int, turn: int, command: str, result: str) -> dict[str, Any]:
    return _tool(turn, day, "bash", {"command": command}, result)


def _day_summary(
    day: int, cash: float, in_tok: int = 0, out_tok: int = 0
) -> dict[str, Any]:
    return {
        "timestamp": STAMP,
        "run_id": RUN_ID,
        "event": "day_summary",
        "day": day,
        "turn": 0,
        "elapsed_s": 5.0,
        "turns": 2,
        "cash": cash,
        "day_input_tokens": in_tok,
        "day_output_tokens": out_tok,
        "day_cached_tokens": 0,
        "day_reasoning_tokens": 0,
        "total_input_tokens": in_tok,
        "total_output_tokens": out_tok,
        "total_cached_tokens": 0,
        "total_reasoning_tokens": 0,
    }


def _llm_call(day: int, turn: int, in_tok: int, out_tok: int) -> dict[str, Any]:
    return {
        "timestamp": STAMP,
        "run_id": RUN_ID,
        "event": "llm_call",
        "day": day,
        "turn": turn,
        "elapsed_s": 1.0,
        "tool": "bash",
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "requested_model": "test-model",
        "served_model": "test-model",
        "anthropic_fallback_used": False,
    }


def _write_logs(
    run: Path,
    tools: list[dict[str, Any]],
    timing: list[dict[str, Any]],
    *,
    corrupt_tool_line: bool = False,
) -> None:
    logs = run / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / f"tool_results_{RUN_ID}.jsonl", "w", encoding="utf-8") as handle:
        for row in tools:
            handle.write(json.dumps(row) + "\n")
        if corrupt_tool_line:
            handle.write("{not json\n")
    with open(logs / f"timing_{RUN_ID}.jsonl", "w", encoding="utf-8") as handle:
        for row in timing:
            handle.write(json.dumps(row) + "\n")


def _write_db(run: Path, *, ledger: list[tuple], costs: list[tuple], preds: list[tuple]) -> None:
    conn = sqlite3.connect(run / "world.nmdb")
    conn.execute(
        "CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, day INTEGER NOT NULL,"
        " category TEXT NOT NULL, amount REAL NOT NULL, note TEXT)"
    )
    conn.execute(
        "CREATE TABLE api_costs (id INTEGER PRIMARY KEY AUTOINCREMENT, day INTEGER NOT NULL,"
        " model TEXT NOT NULL, purpose TEXT NOT NULL, input_tokens INTEGER NOT NULL,"
        " output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE predictions (submit_day INTEGER NOT NULL, horizon_days INTEGER NOT NULL,"
        " metric TEXT NOT NULL, predicted_value REAL NOT NULL, predicted_lower REAL,"
        " predicted_upper REAL, submitted_at REAL NOT NULL,"
        " PRIMARY KEY (submit_day, horizon_days, metric))"
    )
    conn.executemany("INSERT INTO ledger (day, category, amount, note) VALUES (?, ?, ?, ?)", ledger)
    conn.executemany(
        "INSERT INTO api_costs (day, model, purpose, input_tokens, output_tokens, cost_usd)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        costs,
    )
    conn.executemany(
        "INSERT INTO predictions (submit_day, horizon_days, metric, predicted_value,"
        " predicted_lower, predicted_upper, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        preds,
    )
    conn.commit()
    conn.close()


def _run_dir(
    root: Path,
    *,
    tools: list[dict[str, Any]] | None = None,
    timing: list[dict[str, Any]] | None = None,
    db: dict[str, list[tuple]] | None = None,
    with_logs: bool = True,
) -> Path:
    run = root / f"run_{RUN_ID}"
    run.mkdir(parents=True)
    (run / "config.json").write_text(json.dumps(_config()), encoding="utf-8")
    (run / "checkpoint.json").write_text(json.dumps(_checkpoint()), encoding="utf-8")
    if with_logs:
        _write_logs(run, tools or [], timing or [])
    if db is not None:
        _write_db(
            run,
            ledger=db.get("ledger", []),
            costs=db.get("costs", []),
            preds=db.get("preds", []),
        )
    return run


def _sidecar(trial: Path, name: str) -> dict[str, Any]:
    return json.loads((trial / "ceo_bench" / name).read_text(encoding="utf-8"))


def _standard_tools() -> list[dict[str, Any]]:
    return [
        _tool(0, 0, "_dashboard", {}, "=== DAY 0 DASHBOARD ===\nCASH: $1,000,000"),
        _bash(0, 1, "./novamind-operation query \"SELECT day FROM ledger\"", "rows..."),
        _bash(
            0,
            2,
            "./novamind-operation python-c \"import novamind_api as nm; nm.pricing.set_prices(A=29.99)\"",
            "prices updated",
        ),
        _bash(0, 3, "./novamind-operation next-week 1005000 1020000 1100000",
              "=== Week 1 Dashboard (Day 7) ===\nCash: $999,500"),
        _bash(7, 1, "./novamind-operation status", "ok"),
        _bash(7, 2, "./novamind-operation next-week 1000000 1010000 1090000",
              "=== Week 2 Dashboard (Day 14) ===\nCash: $999,000"),
    ]


def _standard_db() -> dict[str, list[tuple]]:
    return {
        "ledger": [
            (0, "initial_funding", 1000000.0, "seed"),
            (3, "operations", -200.0, "ops"),
            (7, "operations", -300.0, "ops"),
        ],
        "costs": [
            (3, "test-agent-model", "agent", 10000, 2000, 0.045),
            (5, "test-agent-model", "agent", 20000, 3000, 0.085),
            (4, "test-sim-model", "customer_social_post", 50000, 8000, 0.21),
            (6, "test-sim-model", "customer_negotiation", 30000, 5000, 0.13),
        ],
        "preds": [
            (0, 7, "cash", 1000500.0, 999000.0, 1002000.0, 1_700_000_000.0),
            (0, 28, "cash", 1002000.0, None, None, 1_700_000_000.0),
        ],
    }


def test_bridge_converts_harness_logs_to_reportable_trial(tmp_path: Path) -> None:
    timing = [
        _llm_call(0, 1, 10000, 1500),
        _llm_call(0, 2, 12000, 1600),
        _day_summary(7, 999500.0),
        _day_summary(14, 999000.0),
    ]
    run = _run_dir(tmp_path, tools=_standard_tools(), timing=timing, db=_standard_db())
    trial = tmp_path / "trial"

    summary = bridge_ceo_bench_run(run, trial)

    assert summary["agent_cost_usd"] == pytest.approx(0.13)
    assert summary["simulator_cost_usd"] == pytest.approx(0.34)
    assert summary["trial_name"] == f"ceo-bench__run_{RUN_ID}"
    assert summary["trajectory_steps"] == 5  # _dashboard excluded
    assert summary["noop_weeks"] == [1]

    report = build_run_report(trial)
    assert report["availability"]["trajectory"] == "present"
    assert report["identity"]["task"] == "ceo-bench/novamind-operation"
    assert report["identity"]["agent"] == "bash_agent"
    assert report["outcome"]["verdict"] == "not_scored"
    assert report["tokens"]["input"] == 60000
    assert report["cost"]["total_usd"] == 0.13
    assert report["cost"]["source"] == "result_json"
    assert report["timeline"]["total_steps"] == 5
    meta = _sidecar(trial, "meta.json")
    assert meta["plugin"] == "ceo_bench"
    assert meta["bankruptcy"] == {"bankrupt": False, "day": None}
    assert meta["world_nmdb"]["status"] == "ok"


def test_spend_separation_never_merges_simulator(tmp_path: Path) -> None:
    run = _run_dir(tmp_path, tools=_standard_tools(), timing=[_day_summary(7, 999500.0)],
                   db=_standard_db())
    trial = tmp_path / "trial"
    bridge_ceo_bench_run(run, trial)

    spend = _sidecar(trial, "spend.json")
    assert spend["agent"] == {
        "input_tokens": 60000,
        "cached_input_tokens": 15000,
        "output_tokens": 9000,
        "cost_usd": 0.13,
        "source": "api_costs:purpose=agent",
    }
    assert spend["simulator"]["cost_usd"] == 0.34
    assert spend["simulator"]["input_tokens"] == 80000
    assert set(spend["simulator"]["by_purpose"]) == {"customer_social_post", "customer_negotiation"}
    assert "agent" not in spend["simulator"]["by_purpose"]

    report = build_run_report(trial)
    assert report["cost"]["total_usd"] == 0.13  # simulator excluded from agent cost


def test_bankruptcy_detected_from_ledger_first_negative_day(tmp_path: Path) -> None:
    db = {
        "ledger": [
            (0, "initial_funding", 1000.0, "seed"),
            (1, "compute", -400.0, "c"),
            (2, "compute", -900.0, "c"),
            (3, "advertising", -100.0, "a"),
        ],
        "costs": [],
        "preds": [],
    }
    run = _run_dir(tmp_path, tools=_standard_tools(), timing=[], db=db)
    trial = tmp_path / "trial"
    summary = bridge_ceo_bench_run(run, trial)

    assert summary["outcome"] == "bankrupt"
    assert summary["bankrupt_day"] == 2
    meta = _sidecar(trial, "meta.json")
    assert meta["bankruptcy"] == {"bankrupt": True, "day": 2}
    cash = _sidecar(trial, "cash_daily.json")
    assert cash["source"] == "ledger"
    assert [row["cash"] for row in cash["daily"]] == [1000.0, 600.0, -300.0, -400.0]


def test_state_change_rule_matrix() -> None:
    assert is_state_changing_call("read_file", {"path": "a.py"}) is False
    assert is_state_changing_call("search_files", {"pattern": "x"}) is False
    assert is_state_changing_call("_dashboard", {}) is False
    assert is_state_changing_call("write_file", {"path": "a.py"}) is True
    assert is_state_changing_call("edit_file", {"path": "a.py"}) is True
    assert is_state_changing_call("mystery_tool", {}) is True  # unknown: conservative
    query = "./novamind-operation query \"SELECT 1\""
    assert is_state_changing_call("bash", {"command": query}) is False
    assert is_state_changing_call("bash", {"command": "./novamind-operation status"}) is False
    advance = "./novamind-operation next-week 1 2 3"
    assert is_state_changing_call("bash", {"command": advance}) is False
    mutate = "./novamind-operation python-c \"import novamind_api as nm\""
    assert is_state_changing_call("bash", {"command": mutate}) is True
    assert is_state_changing_call("bash", {"command": "python3 strategy.py"}) is True
    assert is_state_changing_call("bash", {"command": "./novamind-operation frobnicate"}) is True

    done = "=== Week 3 Dashboard (Day 21) ===\nCash: $5"
    assert is_completed_week_advance("bash", {"command": advance}, done) is True
    assert is_completed_week_advance("bash", {"command": advance}, "still running") is False
    assert is_completed_week_advance("bash", {"command": query}, done) is False


def test_noop_weeks_split_at_completed_advances(tmp_path: Path) -> None:
    tools = [
        _bash(0, 1, "./novamind-operation next-week 1 2 3", "=== Week 1 Dashboard (Day 7) ==="),
        _bash(7, 1, "./novamind-operation query \"SELECT 1\"", "rows"),
        _bash(7, 2, "./novamind-operation next-week 1 2 3", "timeout, retry later"),
        _bash(7, 3, "./novamind-operation next-week 1 2 3", "=== Week 2 Dashboard (Day 14) ==="),
        _bash(14, 1, "./novamind-operation query \"SELECT 2\"", "rows"),
    ]
    run = _run_dir(tmp_path, tools=tools, timing=[])
    trial = tmp_path / "trial"
    bridge_ceo_bench_run(run, trial)

    weeks = _sidecar(trial, "weeks.json")
    assert [(w["week"], w["noop"], w["partial"]) for w in weeks["weeks"]] == [
        (0, True, False),  # only the advance call: no business decision taken
        (1, True, False),  # query + failed advance: no state change
        (2, True, True),  # trailing partial week, read-only
    ]
    assert weeks["noop_weeks"] == [0, 1, 2]
    assert weeks["weeks"][0]["end_day"] == 6  # up to (not including) week 1 day 7


def test_timing_cash_fallback_when_db_encrypted(tmp_path: Path) -> None:
    run = _run_dir(
        tmp_path,
        tools=_standard_tools(),
        timing=[_day_summary(7, 999500.0), _day_summary(14, 999000.0)],
        db=_standard_db(),
    )
    (run / "world.nmdb").write_bytes(b"\x00\x01\x02not a database\xff" * 64)

    trial = tmp_path / "trial"
    summary = bridge_ceo_bench_run(run, trial)

    meta = _sidecar(trial, "meta.json")
    assert meta["world_nmdb"]["status"] == "unavailable"
    assert "unreadable" in meta["world_nmdb"]["reason"]
    cash = _sidecar(trial, "cash_daily.json")
    assert cash["source"] == "timing_day_summary"
    assert [row["cash"] for row in cash["daily"]] == [999500.0, 999000.0]
    forecasts = _sidecar(trial, "forecasts.json")
    assert forecasts["status"] == "unavailable"
    assert forecasts["rows"] == []
    spend = _sidecar(trial, "spend.json")
    assert spend["agent"]["cost_usd"] is None
    assert spend["simulator"]["cost_usd"] is None
    assert any("agent cost unavailable" in note for note in summary["notes"])
    report = build_run_report(trial)
    assert report["cost"]["total_usd"] is None
    assert any("cost unavailable" in note for note in report["data_quality"])


def test_session_event_log_supplies_simulator_spend_and_outcome(tmp_path: Path) -> None:
    run = _run_dir(tmp_path, tools=_standard_tools(), timing=[_day_summary(7, 999500.0)])
    conn = sqlite3.connect(run / "world.nmdb")
    conn.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY, day INTEGER, category TEXT, amount REAL)")
    conn.execute("INSERT INTO ledger (day, category, amount) VALUES (0, 'initial_funding', 1000000.0)")
    conn.commit()
    conn.close()
    session_logs = run / "agent_workspace" / "sessions" / "sess1" / "logs"
    session_logs.mkdir(parents=True)
    events = [
        {"timestamp": STAMP, "day": 4, "event_type": "llm_call", "category": "customer_social_post",
         "details": {"model": "sim", "input_tokens": 50000, "output_tokens": 8000}, "cost_usd": 0.21},
        {"timestamp": STAMP, "day": 5, "event_type": "agent_action", "category": "tool",
         "details": {"tool_name": "query"}},
    ]
    (session_logs / "run_sess1.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8"
    )
    (session_logs / "run_sess1_meta.json").write_text(
        json.dumps({"run_id": "sess1", "outcome": "completed", "final_cash": 999500.0, "days_run": 7}),
        encoding="utf-8",
    )

    trial = tmp_path / "trial"
    summary = bridge_ceo_bench_run(run, trial)

    assert summary["outcome"] == "completed"
    spend = _sidecar(trial, "spend.json")
    assert spend["simulator"]["cost_usd"] == 0.21
    assert spend["simulator"]["source"] == "session_event_log:llm_call"


def test_forecast_error_math_and_zero_actual_guard(tmp_path: Path) -> None:
    db = {
        "ledger": [(0, "initial_funding", 1000.0, "s"), (7, "operations", -1000.0, "o")],
        "costs": [],
        "preds": [
            (0, 7, "cash", 1100.0, 900.0, 1300.0, 1_700_000_000.0),
            (0, 28, "cash", 2000.0, None, None, 1_700_000_000.0),
        ],
    }
    run = _run_dir(tmp_path, tools=_standard_tools(), timing=[], db=db)
    trial = tmp_path / "trial"
    bridge_ceo_bench_run(run, trial)

    forecasts = _sidecar(trial, "forecasts.json")
    assert forecasts["status"] == "ok"
    scored, unscored = forecasts["rows"]
    assert scored["target_day"] == 7
    assert scored["actual_cash"] == 0.0
    assert scored["abs_error"] == 1100.0
    assert scored["pct_error"] is None  # actual is 0: no fabricated ratio
    assert unscored["target_day"] == 28
    assert unscored["actual_cash"] is None
    assert unscored["abs_error"] is None


def test_malformed_inputs_degrade_honestly(tmp_path: Path) -> None:
    with_paths = _run_dir(tmp_path, tools=[], timing=[], with_logs=False)
    with_paths.rename(tmp_path / "renamed")
    run = tmp_path / "renamed"
    (run / "logs").mkdir()
    _write_logs(run, _standard_tools(), [], corrupt_tool_line=True)

    trial = tmp_path / "trial"
    summary = bridge_ceo_bench_run(run, trial)

    assert any("malformed" in note for note in summary["notes"])
    assert summary["trajectory_steps"] == 5
    meta = _sidecar(trial, "meta.json")
    assert meta["run_id"] == RUN_ID  # config still identifies the run

    empty = tmp_path / "empty-run"
    empty.mkdir()
    (empty / "config.json").write_text(json.dumps(_config()), encoding="utf-8")
    (empty / "checkpoint.json").write_text(json.dumps(_checkpoint()), encoding="utf-8")
    bridged = tmp_path / "empty-trial"
    bridge_ceo_bench_run(empty, bridged)
    report = build_run_report(bridged)
    assert report["availability"]["trajectory"] == "present"  # file exists, zero steps
    assert report["timeline"]["total_steps"] == 0
    assert report["outcome"]["verdict"] == "not_scored"
    assert report["cost"]["total_usd"] is None
    assert report["tokens"]["total"] == 69000  # checkpoint totals still reported

    nocfg = tmp_path / "no-config"
    nocfg.mkdir()
    try:
        bridge_ceo_bench_run(nocfg, tmp_path / "bad-trial")
    except CeoBenchBridgeError:
        pass
    else:
        raise AssertionError("missing config.json must raise CeoBenchBridgeError")


def _bridged_trial(tmp_path: Path) -> Path:
    timing = [
        _llm_call(0, 1, 10000, 1500),
        _day_summary(7, 999500.0),
        _day_summary(14, 999000.0),
    ]
    run = _run_dir(tmp_path, tools=_standard_tools(), timing=timing, db=_standard_db())
    trial = tmp_path / "trial"
    bridge_ceo_bench_run(run, trial)
    return trial


def test_domain_plugin_detects_builds_and_renders(tmp_path: Path) -> None:
    from evallab.interpretation.domains import domain_section, render_domain_markdown
    from evallab.interpretation.domains.ceo_bench import CeoBenchPlugin
    from evallab.interpretation.run_report import render_run_report_markdown

    trial = _bridged_trial(tmp_path)
    result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
    plugin = CeoBenchPlugin()

    assert plugin.detect(trial, result) is True
    section = domain_section(trial, result)
    assert section is not None
    assert section["plugin"] == "ceo_bench"
    assert section["version"] == "1"
    assert section["sources"] == [
        "ceo_bench/meta.json",
        "ceo_bench/cash_daily.json",
        "ceo_bench/spend.json",
        "ceo_bench/forecasts.json",
        "ceo_bench/weeks.json",
    ]
    assert section["bankruptcy"] == {"bankrupt": False, "day": None}
    assert section["cash"]["final"] == 999500.0
    assert section["cash"]["source"] == "ledger"
    assert section["spend"]["agent"]["cost_usd"] == pytest.approx(0.13)
    assert section["spend"]["simulator"]["cost_usd"] == pytest.approx(0.34)
    assert section["forecasts"]["n_scored"] == 1
    assert section["weeks"]["noop_weeks"] == [1]

    report = build_run_report(trial)
    assert report["domain"] is not None
    assert report["domain"]["plugin"] == "ceo_bench"
    markdown = render_run_report_markdown(report)
    assert "## Domain: ceo_bench" in markdown
    assert "No bankruptcy" in markdown
    assert "never merged into agent cost" in markdown
    assert render_domain_markdown(None) == []


def test_domain_plugin_ignores_non_ceo_trials(tmp_path: Path) -> None:
    from evallab.interpretation.domains import domain_section
    from evallab.interpretation.domains.ceo_bench import CeoBenchPlugin

    trial = tmp_path / "plain"
    trial.mkdir()
    (trial / "result.json").write_text(json.dumps({"trial_name": "plain"}), encoding="utf-8")

    assert CeoBenchPlugin().detect(trial, {"trial_name": "plain"}) is False
    assert domain_section(trial, {"trial_name": "plain"}) is None


def test_domain_plugin_malformed_sidecar_is_unreadable(tmp_path: Path) -> None:
    from evallab.interpretation.domains import domain_section
    from evallab.interpretation.domains.ceo_bench import CeoBenchPlugin

    trial = _bridged_trial(tmp_path)
    (trial / "ceo_bench" / "spend.json").write_text("{broken", encoding="utf-8")
    result = json.loads((trial / "result.json").read_text(encoding="utf-8"))

    assert CeoBenchPlugin().detect(trial, result) is True
    section = domain_section(trial, result)
    assert section is not None
    assert section["plugin"] == "ceo_bench"
    assert section["status"] == "unreadable"
    assert "spend.json" in section["reason"]

    report = build_run_report(trial)
    assert report["domain"] is not None
    assert report["domain"]["status"] == "unreadable"


def test_cli_exits_nonzero_with_message_on_unbridgeable_run(tmp_path: Path) -> None:
    import subprocess

    empty = tmp_path / "empty-run"
    empty.mkdir()
    completed = subprocess.run(
        [sys.executable, "-m", "library.adapters.ceo_bench", str(empty)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 1
    assert "error:" in completed.stderr
    assert completed.stdout == ""
