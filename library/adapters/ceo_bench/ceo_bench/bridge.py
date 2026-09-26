"""CEO-Bench run directory to Harbor-shaped trial directory bridge.

Converts one CEO-Bench harness run (``bash_agent_runs/run_<id>/`` as written by
``src/saas_bench/agents/bash_agent/run_test.py`` in the upstream repository)
into a trial directory that ``evallab report run`` reads: ``result.json`` with
honest nulls where the harness records nothing, an ATIF-shaped
``agent/trajectory.json`` built from the harness tool-result log, and
trial-relative ``ceo_bench/*.json`` sidecars that the ``ceo_bench`` domain
plugin reads (the plugin never touches ``world.nmdb``).

Upstream: https://github.com/zlab-princeton/ceobench-src
Pinned commit: ``UPSTREAM_COMMIT`` below (shallow clone under
``/Users/petermakhnatch/Developer/.sources/ceobench-src``).

Deterministic: no wall clock, network, model calls, or host state. Missing
data stays null with a reason in the sidecar meta file, never a zero.
Simulator-LLM spend is metered separately from agent spend and never merged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

PLUGIN_NAME = "ceo_bench"
PLUGIN_VERSION = "1"
BRIDGE_VERSION = "1"
UPSTREAM_REPO = "https://github.com/zlab-princeton/ceobench-src"
UPSTREAM_COMMIT = "d2b7b32e5301a571b77f5f68bd1032adbcd5b464"

#: ``world.nmdb`` tables this bridge reads, with the upstream writer cited.
#: ledger/api_costs/predictions DDL: upstream ``src/saas_bench/database.py``
#: (``CREATE TABLE IF NOT EXISTS ledger`` ~line 443, ``api_costs`` ~line 660,
#: ``predictions`` ~line 1041). ``api_costs.purpose`` carries ``'agent'`` for
#: the benchmarked agent (upstream ``src/saas_bench/llm.py`` CostTracker call
#: sites) and simulator purposes such as ``customer_social_post``,
#: ``customer_negotiation``, ``customer_initial_outreach``,
#: ``agent_social_judge``, ``agent_social_reply``, ``competitor_event_post``
#: (upstream ``src/saas_bench/customer_llm.py`` and ``simulation.py``).
AGENT_COST_PURPOSE = "agent"

#: Bankruptcy: cash below $0 ends the game immediately (upstream
#: ``src/saas_bench/simulation.py`` cash check, ``environment.py`` step result,
#: ``agents/bash_agent/run_test.py`` resume/loop guards).
BANKRUPTCY_CASH_BELOW = 0.0


#: Published SQLCipher key for post-hoc analysis. Upstream publishes the key
#: for exactly this use (``KEYS.md``: "The key", ``_embedded_key.py`` L16,
#: ``docs/analyze_trajectory.md`` "Decrypt and open"). Resolution order here:
#: ``NMDB_KEY`` env first, then the ``_NMDB_KEY`` constant parsed as text from
#: a ``--ceobench-src`` checkout (read with a regex, never imported or
#: executed — importing upstream code would pull its model/serving deps).
#: Compare upstream ``db_protection._get_key`` (``db_protection.py`` L42-68:
#: embedded key, then ``NMDB_KEY`` env, else ``RuntimeError``).
_EMBEDDED_KEY_RE = re.compile(r'_NMDB_KEY\s*=\s*"([0-9a-fA-F]{64})"')


def resolve_nmdb_key(ceobench_src: str | Path | None = None) -> str | None:
    """Return the SQLCipher key for an encrypted ``world.nmdb``, if findable."""
    key = os.environ.get("NMDB_KEY")
    if key:
        return key
    if ceobench_src is not None:
        candidate = Path(ceobench_src) / "src" / "saas_bench" / "_embedded_key.py"
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            return None
        match = _EMBEDDED_KEY_RE.search(text)
        if match:
            return match.group(1)
    return None


#: Marker of a completed week advance in a bash tool result (upstream
#: server response begins with the ``=== Week N Dashboard`` header).
NEXT_WEEK_DONE_MARKER = "=== Week "

#: bash_agent tool namespace (upstream
#: ``src/saas_bench/agents/bash_agent/tools.py`` ToolExecutor.dispatch).
_READ_ONLY_TOOLS = frozenset({"read_file", "search_files", "glob_files", "_dashboard"})

#: ``novamind-operation`` subcommands that only read (upstream
#: ``public/docs/cli-reference.md``: query is documented read-only SQL,
#: status/history/list-sessions are views).
_READ_ONLY_SUBCOMMANDS = frozenset({"query", "status", "history", "list-sessions"})

#: Subcommands that advance simulated time without themselves being a business
#: decision (upstream ``public/docs/cli-reference.md`` next-week/next-day).
_TIME_ADVANCE_SUBCOMMANDS = frozenset({"next-week", "next-day"})


class CeoBenchBridgeError(Exception):
    """The CEO-Bench run directory cannot be bridged (missing inputs)."""


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _parse_ts(value: Any) -> datetime | None:
    text = _as_str(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Read a JSONL log; skip malformed lines, counting them."""
    rows: list[dict[str, Any]] = []
    skipped = 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows, skipped
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(data, dict):
            rows.append(data)
        else:
            skipped += 1
    return rows, skipped


def _sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _log_files(logs_dir: Path, run_id: str, stem: str) -> Path | None:
    """Locate ``<stem>_<run_id>.jsonl``; fall back to a lone ``<stem>_*.jsonl``."""
    exact = logs_dir / f"{stem}_{run_id}.jsonl"
    if exact.is_file():
        return exact
    candidates = sorted(logs_dir.glob(f"{stem}_*.jsonl"))
    return candidates[0] if len(candidates) == 1 else None


def _novamind_subcommand(command: str) -> str | None:
    """Subcommand of a ``novamind-operation`` bash invocation, if parseable."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    for index, token in enumerate(tokens):
        if Path(token).name == "novamind-operation":
            for following in tokens[index + 1 :]:
                if not following.startswith("-"):
                    return following
            return None
    return None


def is_state_changing_call(tool: str, arguments: Any) -> bool:
    """Whether a harness tool call may change business or workspace state.

    Read-only file tools and the harness ``_dashboard`` observation never do.
    For ``bash``, read-only and time-advance ``novamind-operation``
    subcommands are not business decisions; every other command is treated as
    potentially state-changing (conservative: unknown commands count as
    state-changing so no-op weeks are never over-claimed).
    """
    if tool in _READ_ONLY_TOOLS:
        return False
    if tool == "bash" and isinstance(arguments, dict):
        command = arguments.get("command")
        if isinstance(command, str):
            subcommand = _novamind_subcommand(command)
            if subcommand is not None:
                return subcommand not in (_READ_ONLY_SUBCOMMANDS | _TIME_ADVANCE_SUBCOMMANDS)
            return True
        return True
    if tool in {"write_file", "edit_file"}:
        return True
    return True


def is_completed_week_advance(tool: str, arguments: Any, result: Any) -> bool:
    """Whether a tool call is a finished ``next-week``/``next-day`` advance."""
    if tool != "bash" or not isinstance(arguments, dict):
        return False
    command = arguments.get("command")
    if not isinstance(command, str):
        return False
    subcommand = _novamind_subcommand(command)
    if subcommand not in _TIME_ADVANCE_SUBCOMMANDS:
        return False
    return isinstance(result, str) and NEXT_WEEK_DONE_MARKER in result


def _query_all(conn: Any, sql: str) -> list[tuple[Any, ...]] | None:
    # ``sqlite3.Error`` for plaintext; ``sqlcipher3`` raises its own
    # ``sqlcipher3.dbapi2`` hierarchy, so catch broadly — callers treat
    # ``None`` as a missing/unreadable table with a named reason.
    try:
        return list(conn.execute(sql))
    except Exception:
        return None


def _read_world_db(
    nmdb_path: Path, ceobench_src: str | Path | None = None
) -> dict[str, Any]:
    """Read cash/spend/forecast tables from ``world.nmdb`` (or plain SQLite).

    Real runs encrypt ``world.nmdb`` with SQLCipher; upstream publishes the key
    for post-hoc analysis (``KEYS.md`` L8-18, ``_embedded_key.py`` L16), so an
    encrypted ledger is opened with :func:`resolve_nmdb_key` and read directly
    — ledger cash-by-day, ``api_costs`` agent-vs-simulator spend, and
    ``predictions`` forecasts. Key application mirrors upstream
    ``db_protection._apply_key`` (``db_protection.py`` L71-74: quote-escape,
    ``PRAGMA key = '<hex string>'`` verbatim, never the ``x'..'`` raw-bytes
    syntax) plus the fail-fast page-1 read of ``_verify_key`` (L77-79).
    Anything unreadable yields ``status`` reasons, never an exception.
    """
    info: dict[str, Any] = {
        "present": nmdb_path.is_file(),
        "sha256": _sha256_file(nmdb_path) if nmdb_path.is_file() else None,
        "status": "unavailable",
        "reason": None,
        "cash": None,
        "spend": None,
        "forecasts": None,
    }
    if not nmdb_path.is_file():
        info["reason"] = "world.nmdb absent (run may predate checkpointing)"
        return info
    conn: Any = None
    try:
        conn = sqlite3.connect(f"file:{nmdb_path}?mode=ro", uri=True)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.DatabaseError as exc:
            conn.close()
            conn = None
            try:
                import sqlcipher3
            except ImportError:
                info["reason"] = (
                    f"encrypted world.nmdb unreadable: {type(exc).__name__} "
                    "(sqlcipher3 is not installed; run the bridge from the "
                    "ceo_bench adapter project)"
                )
                return info
            key = resolve_nmdb_key(ceobench_src)
            if not key:
                info["reason"] = (
                    "encrypted world.nmdb unreadable: no key "
                    "(set NMDB_KEY or pass --ceobench-src <ceobench-src checkout>)"
                )
                return info
            try:
                conn = sqlcipher3.connect(f"file:{nmdb_path}?mode=ro", uri=True)
                escaped = key.replace("'", "''")
                conn.execute(f"PRAGMA key = '{escaped}'")
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            except Exception as exc2:
                if conn is not None:
                    conn.close()
                info["reason"] = (
                    f"encrypted world.nmdb unreadable: {type(exc2).__name__} "
                    "(wrong key?)"
                )
                return info
        ledger = _query_all(
            conn, "SELECT day, SUM(amount) FROM ledger GROUP BY day ORDER BY day"
        )
        if ledger is None:
            info["reason"] = "ledger table missing or unreadable"
            return info
        cash_rows: list[dict[str, Any]] = []
        running = 0.0
        for day, net in ledger:
            day_i, net_f = _as_int(day), _as_float(net)
            if day_i is None or net_f is None:
                continue
            running += net_f
            cash_rows.append({"day": day_i, "cash": running})
        info["cash"] = {"source": "ledger", "daily": cash_rows}

        api_costs = _query_all(
            conn,
            "SELECT purpose, model, SUM(input_tokens), SUM(output_tokens), "
            "SUM(cost_usd), COUNT(*) FROM api_costs GROUP BY purpose, model",
        )
        if api_costs is None:
            info["spend"] = None
            info["reason"] = "api_costs table missing or unreadable"
        else:
            by_purpose: dict[str, dict[str, Any]] = {}
            for purpose, model, in_tok, out_tok, cost, calls in api_costs:
                bucket = by_purpose.setdefault(
                    str(purpose),
                    {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "models": {}},
                )
                bucket["input_tokens"] += _as_int(in_tok) or 0
                bucket["output_tokens"] += _as_int(out_tok) or 0
                bucket["cost_usd"] += _as_float(cost) or 0.0
                models = bucket["models"]
                assert isinstance(models, dict)
                models[str(model)] = models.get(str(model), 0) + (_as_int(calls) or 0)
            info["spend"] = {"source": "api_costs", "by_purpose": by_purpose}

        predictions = _query_all(
            conn,
            "SELECT submit_day, horizon_days, metric, predicted_value, "
            "predicted_lower, predicted_upper, submitted_at "
            "FROM predictions ORDER BY submit_day, horizon_days",
        )
        if predictions is None:
            info["forecasts"] = None
            if info["reason"] is None:
                info["reason"] = "predictions table missing or unreadable"
        else:
            rows = []
            for s_day, horizon, metric, value, lower, upper, at in predictions:
                s_day_i, horizon_i = _as_int(s_day), _as_int(horizon)
                value_f = _as_float(value)
                if s_day_i is None or horizon_i is None or value_f is None:
                    continue
                rows.append(
                    {
                        "submit_day": s_day_i,
                        "horizon_days": horizon_i,
                        "metric": str(metric),
                        "predicted_value": value_f,
                        "predicted_lower": _as_float(lower),
                        "predicted_upper": _as_float(upper),
                        "submitted_at": _as_float(at),
                    }
                )
            info["forecasts"] = {"source": "predictions", "rows": rows}
        info["status"] = "ok"
        info["reason"] = None
    except sqlite3.Error as exc:
        info["reason"] = f"world.nmdb unreadable: {type(exc).__name__}"
        return info
    finally:
        if conn is not None:
            conn.close()
    return info


def _read_session_event_logs(run_dir: Path, session_id: str | None) -> dict[str, Any]:
    """Simulator-side event logs (server ``run_<session>.jsonl`` + meta).

    The server writes these under the session directory inside
    ``agent_workspace`` (upstream ``server_entry.py`` ``sdir / "logs"``); they
    carry ``llm_call`` events with per-call simulator cost, the fallback spend
    source when ``world.nmdb`` is encrypted.
    """
    outcome: dict[str, Any] = {"source": None, "outcome": None, "simulator_spend": None}
    candidates: list[Path] = []
    if session_id:
        candidates.extend(run_dir.rglob(f"run_{session_id}.jsonl"))
    if not candidates:
        candidates.extend(run_dir.rglob("run_*.jsonl"))
    for log_path in sorted(candidates):
        if "raw_responses" in log_path.name or "tool_results" in log_path.name:
            continue
        if "timing" in log_path.name:
            continue
        rows, _ = _read_jsonl(log_path)
        if not rows:
            continue
        if not any(isinstance(r.get("event_type"), str) for r in rows):
            continue
        outcome["source"] = str(log_path.relative_to(run_dir))
        by_purpose: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.get("event_type") != "llm_call":
                continue
            purpose = str(row.get("category") or "unknown")
            details = row.get("details") if isinstance(row.get("details"), dict) else {}
            bucket = by_purpose.setdefault(
                purpose, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}
            )
            in_tok = details.get("input_tokens") if isinstance(details, dict) else None
            out_tok = details.get("output_tokens") if isinstance(details, dict) else None
            bucket["input_tokens"] += _as_int(in_tok) or 0
            bucket["output_tokens"] += _as_int(out_tok) or 0
            bucket["cost_usd"] += _as_float(row.get("cost_usd")) or 0.0
            bucket["calls"] += 1
        if by_purpose:
            outcome["simulator_spend"] = {"source": "session_event_log", "by_purpose": by_purpose}
        meta_path = log_path.parent / log_path.name.replace(".jsonl", "_meta.json")
        meta = _read_json(meta_path)
        if meta:
            outcome["outcome"] = meta.get("outcome")
            outcome["final_cash"] = _as_float(meta.get("final_cash"))
            outcome["days_run"] = _as_int(meta.get("days_run"))
        break
    return outcome


def bridge_ceo_bench_run(
    run_dir: str | Path,
    trial_dir: str | Path,
    *,
    trial_name: str | None = None,
    ceobench_src: str | Path | None = None,
) -> dict[str, Any]:
    """Bridge one CEO-Bench run directory into a Harbor-shaped trial directory.

    Raises :class:`CeoBenchBridgeError` when the run directory has no usable
    harness inputs (missing ``config.json``). Everything else degrades to
    explicit null/unavailable sidecar states.

    ``ceobench_src`` is a checkout of the upstream repository used only to
    parse the published ``_NMDB_KEY`` constant as text when ``world.nmdb``
    is encrypted and ``NMDB_KEY`` is unset (see :func:`resolve_nmdb_key`).
    """
    run = Path(run_dir).resolve()
    trial = Path(trial_dir).resolve()
    config = _read_json(run / "config.json")
    if config is None:
        raise CeoBenchBridgeError(f"{run / 'config.json'} missing or unreadable")
    run_id = str(config.get("run_id") or run.name.replace("run_", "") or run.name)
    checkpoint = _read_json(run / "checkpoint.json") or {}
    session_id = _as_str(checkpoint.get("session_id")) or _as_str(config.get("session_id"))

    logs_dir = run / "logs"
    notes: list[str] = []
    tool_rows: list[dict[str, Any]] = []
    timing_rows: list[dict[str, Any]] = []
    malformed: dict[str, int] = {}
    if logs_dir.is_dir():
        for stem, bucket in (("tool_results", tool_rows), ("timing", timing_rows)):
            path = _log_files(logs_dir, run_id, stem)
            if path is None:
                notes.append(f"logs/{stem}_{run_id}.jsonl absent: {stem} unavailable")
                continue
            rows, skipped = _read_jsonl(path)
            bucket.extend(rows)
            malformed[stem] = skipped
            if skipped:
                notes.append(f"{path.name}: {skipped} malformed line(s) skipped")
    else:
        notes.append("logs/ directory absent: tool results and timing unavailable")

    # Per-turn agent tokens from timing llm_call events, keyed by (day, turn).
    # Upstream run_test.py logs one llm_call per agent turn with input/output/
    # cached/reasoning tokens (llm_call event) and per-day totals plus cash in
    # day_summary events.
    turn_tokens: dict[tuple[int, int], dict[str, Any]] = {}
    day_summary: dict[int, dict[str, Any]] = {}
    stamps: list[datetime] = []
    for event in timing_rows:
        stamp = _parse_ts(event.get("timestamp"))
        if stamp is not None:
            stamps.append(stamp)
        day, turn = _as_int(event.get("day")), _as_int(event.get("turn"))
        if event.get("event") == "llm_call" and day is not None and turn is not None:
            turn_tokens.setdefault(
                (day, turn),
                {
                    "input": _as_int(event.get("input_tokens")),
                    "output": _as_int(event.get("output_tokens")),
                    "cached": _as_int(event.get("cached_tokens")),
                    "reasoning": _as_int(event.get("reasoning_tokens")),
                    "model": _as_str(event.get("served_model"))
                    or _as_str(event.get("requested_model")),
                },
            )
        elif event.get("event") == "day_summary" and day is not None:
            day_summary[day] = event
    for entry in tool_rows:
        stamp = _parse_ts(entry.get("timestamp"))
        if stamp is not None:
            stamps.append(stamp)

    world = _read_world_db(run / "world.nmdb", ceobench_src)
    if world["status"] != "ok" and world["reason"]:
        notes.append(f"world.nmdb: {world['reason']}")
    session = _read_session_event_logs(run, session_id)

    # Cash-by-sim-day: the ledger running sum is the source of truth (upstream
    # docs/analyze_trajectory.md); timing day_summary cash is the fallback.
    cash_source: str | None = None
    cash_daily: list[dict[str, Any]] = []
    if isinstance(world.get("cash"), dict):
        cash_source = "ledger"
        cash_daily = world["cash"]["daily"]
    elif day_summary:
        cash_source = "timing_day_summary"
        for day in sorted(day_summary):
            cash = _as_float(day_summary[day].get("cash"))
            if cash is not None:
                cash_daily.append({"day": day, "cash": cash})
        if not cash_daily:
            cash_source = None
            notes.append("timing day_summary events carry no cash readings")
    else:
        notes.append("no cash series: ledger unreadable and no timing day_summary events")
    cash_by_day = {row["day"]: row["cash"] for row in cash_daily}

    bankrupt_day: int | None = None
    for row in cash_daily:
        if row["cash"] < BANKRUPTCY_CASH_BELOW:
            bankrupt_day = row["day"]
            break
    days_run = max((row["day"] for row in cash_daily), default=None)
    if days_run is None:
        days_run = _as_int(checkpoint.get("day"))
    total_days = _as_int(config.get("total_days"))
    harness_outcome = _as_str(session.get("outcome"))
    if bankrupt_day is not None or harness_outcome == "bankrupt":
        outcome = "bankrupt"
    elif harness_outcome in {"completed", "budget_exceeded"}:
        outcome = harness_outcome
    elif total_days is not None and days_run is not None and days_run >= total_days:
        outcome = "completed"
    else:
        outcome = "unknown"
        notes.append("run outcome unknown: no bankruptcy signal and no completion marker")

    # Spend separation: purpose 'agent' is the benchmarked agent; every other
    # api_costs purpose is simulator spend, metered separately and never added
    # to the agent cost.
    agent_tokens = {
        "input": _as_int(checkpoint.get("total_input_tokens")),
        "cached": _as_int(checkpoint.get("total_cached_tokens")),
        "output": _as_int(checkpoint.get("total_output_tokens")),
        "reasoning": _as_int(checkpoint.get("total_reasoning_tokens")),
    }
    if all(value is None for value in agent_tokens.values()) and day_summary:
        agent_tokens = {
            "input": sum((_as_int(e.get("day_input_tokens")) or 0) for e in day_summary.values())
            or None,
            "cached": sum((_as_int(e.get("day_cached_tokens")) or 0) for e in day_summary.values())
            or None,
            "output": sum(
                (_as_int(e.get("day_output_tokens")) or 0) for e in day_summary.values()
            )
            or None,
            "reasoning": sum(
                (_as_int(e.get("day_reasoning_tokens")) or 0) for e in day_summary.values()
            )
            or None,
        }
        notes.append("checkpoint token totals absent: summed timing day_summary totals")
    agent_cost: float | None = None
    agent_cost_source: str | None = None
    simulator_spend: dict[str, Any] | None = None
    simulator_source: str | None = None
    spend_by_purpose = (
        world["spend"]["by_purpose"] if isinstance(world.get("spend"), dict) else {}
    )
    if spend_by_purpose:
        agent_bucket = spend_by_purpose.get(AGENT_COST_PURPOSE, {})
        agent_cost = _as_float(agent_bucket.get("cost_usd"))
        agent_cost_source = "api_costs:purpose=agent" if agent_cost is not None else None
        sim_purposes = {
            purpose: bucket
            for purpose, bucket in spend_by_purpose.items()
            if purpose != AGENT_COST_PURPOSE
        }
        if sim_purposes:
            simulator_spend = {
                "input_tokens": sum(b["input_tokens"] for b in sim_purposes.values()),
                "output_tokens": sum(b["output_tokens"] for b in sim_purposes.values()),
                "cost_usd": round(sum(b["cost_usd"] for b in sim_purposes.values()), 6),
                "by_purpose": sim_purposes,
            }
            simulator_source = "api_costs:purpose!=agent"
    if simulator_spend is None and isinstance(session.get("simulator_spend"), dict):
        entry = session["simulator_spend"]
        simulator_spend = {
            "input_tokens": sum(b["input_tokens"] for b in entry["by_purpose"].values()),
            "output_tokens": sum(b["output_tokens"] for b in entry["by_purpose"].values()),
            "cost_usd": round(sum(b["cost_usd"] for b in entry["by_purpose"].values()), 6),
            "by_purpose": entry["by_purpose"],
        }
        simulator_source = "session_event_log:llm_call"
    if agent_cost is None:
        notes.append("agent cost unavailable: no api_costs purpose='agent' rows readable")
    if simulator_spend is None:
        notes.append(
            "simulator spend unavailable: world.nmdb encrypted or absent "
            "and no session event log with llm_call costs"
        )

    # Forecast error: predictions (submit_day + horizon_days -> target day)
    # against actual cash then (upstream docs/analyze_trajectory.md recipe).
    forecast_rows: list[dict[str, Any]] = []
    forecast_status = "unavailable"
    forecast_reason: str | None = None
    world_forecasts = world.get("forecasts")
    if isinstance(world_forecasts, dict) and cash_by_day:
        forecast_status = "ok"
        for pred in world_forecasts["rows"]:
            if pred["metric"] != "cash":
                continue
            target = pred["submit_day"] + pred["horizon_days"]
            actual = cash_by_day.get(target)
            predicted = pred["predicted_value"]
            row: dict[str, Any] = {
                "submit_day": pred["submit_day"],
                "horizon_days": pred["horizon_days"],
                "target_day": target,
                "predicted_value": predicted,
                "predicted_lower": pred["predicted_lower"],
                "predicted_upper": pred["predicted_upper"],
                "actual_cash": actual,
                "abs_error": abs(predicted - actual) if actual is not None else None,
                "pct_error": ((predicted - actual) / actual)
                if actual is not None and actual != 0
                else None,
            }
            forecast_rows.append(row)
    elif not isinstance(world_forecasts, dict):
        forecast_reason = "predictions table unreadable (world.nmdb encrypted or absent)"
    else:
        forecast_reason = "no cash series to score predictions against"
    if forecast_status == "unavailable" and forecast_reason:
        notes.append(f"forecasts: {forecast_reason}")

    # No-op weeks: weeks bounded by completed next-week advances; a week is a
    # no-op when it holds no state-changing tool call (see is_state_changing_call).
    weeks: list[dict[str, Any]] = []
    week_calls: list[dict[str, Any]] = []
    week_index = 0
    week_start_day: int | None = None
    seen_arg_shapes: set[str] = set()
    dashboard_excluded = 0

    def close_week(end_day: int | None, *, partial: bool) -> None:
        nonlocal week_index, week_calls, week_start_day
        if not week_calls and week_start_day is None:
            return
        changing = sum(1 for call in week_calls if call["state_changing"])
        days = [c["day"] for c in week_calls if c["day"] is not None]
        weeks.append(
            {
                "week": week_index,
                "start_day": week_start_day if week_start_day is not None else (min(days) if days else None),
                "end_day": end_day if end_day is not None else (max(days) if days else None),
                "tool_calls": len(week_calls),
                "state_changing_calls": changing,
                "noop": changing == 0,
                "partial": partial,
            }
        )
        week_index += 1
        week_calls = []
        week_start_day = None

    for entry in tool_rows:
        tool = str(entry.get("tool") or "unknown")
        arguments = entry.get("arguments")
        if tool == "_dashboard":
            dashboard_excluded += 1
            continue
        if week_start_day is None:
            week_start_day = _as_int(entry.get("day"))
        result = entry.get("result")
        call = {
            "day": _as_int(entry.get("day")),
            "state_changing": is_state_changing_call(
                tool, arguments if isinstance(arguments, dict) else {}
            ),
        }
        if is_completed_week_advance(tool, arguments if isinstance(arguments, dict) else {}, result):
            week_calls.append(call)
            close_week(_as_int(entry.get("day")), partial=False)
        else:
            week_calls.append(call)
    close_week(None, partial=True)
    if weeks and weeks[-1]["tool_calls"] == 0:
        weeks.pop()
    noop_weeks = [w["week"] for w in weeks if w["noop"]]

    # A week covers days up to (not including) the next week's first day.
    for prev, nxt in zip(weeks, weeks[1:], strict=False):
        start = nxt["start_day"]
        if start is not None and (prev["end_day"] is None or start - 1 > prev["end_day"]):
            prev["end_day"] = max(start - 1, prev["start_day"] or 0)
    # Trajectory: one step per tool call (dashboard observations excluded).
    steps: list[dict[str, Any]] = []
    used_turn_tokens: set[tuple[int, int]] = set()
    ordinal = 0
    for entry in tool_rows:
        tool = str(entry.get("tool") or "unknown")
        if tool == "_dashboard":
            continue
        ordinal += 1
        arguments = entry.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
            shape = type(entry.get("arguments")).__name__
            if shape not in seen_arg_shapes:
                seen_arg_shapes.add(shape)
                notes.append(f"non-object tool arguments ({shape}) coerced to {{}}")
        result = entry.get("result")
        content = result if isinstance(result, str) else json.dumps(result, default=str)
        day, turn = _as_int(entry.get("day")), _as_int(entry.get("turn"))
        key = (day, turn) if day is not None and turn is not None else None
        usage = turn_tokens.get(key) if key not in used_turn_tokens and key is not None else None
        if key is not None and usage is not None:
            used_turn_tokens.add(key)
        metrics: dict[str, Any] = {
            "prompt_tokens": (usage or {}).get("input"),
            "completion_tokens": (usage or {}).get("output"),
            "cached_tokens": (usage or {}).get("cached"),
            "reasoning_tokens": (usage or {}).get("reasoning"),
            "cost_usd": None,
        }
        call_id = f"ceo-{ordinal}"
        steps.append(
            {
                "step_id": ordinal,
                "source": "agent",
                "timestamp": entry.get("timestamp"),
                "model_name": (usage or {}).get("model") or _as_str(config.get("model")),
                "message": "",
                "tool_calls": [
                    {"tool_call_id": call_id, "function_name": tool, "arguments": arguments}
                ],
                "observation": {
                    "results": [{"source_call_id": call_id, "content": content}]
                },
                "metrics": metrics,
                "extra": {"ceo_bench": {"day": day, "turn": turn}},
            }
        )

    model = _as_str(config.get("model"))
    agent_type = _as_str(config.get("agent_type")) or "bash_agent"
    name = trial_name or f"ceo-bench__run_{run_id}"
    started = min(stamps).strftime("%Y-%m-%dT%H:%M:%SZ") if stamps else None
    finished = max(stamps).strftime("%Y-%m-%dT%H:%M:%SZ") if stamps else None
    if started is None:
        notes.append("no parseable timestamps in harness logs: phase timing unavailable")

    result_doc: dict[str, Any] = {
        "id": run_id,
        "trial_name": name,
        "task_name": "ceo-bench/novamind-operation",
        "config": {
            "agent": {"name": agent_type, "model_name": model},
            "ceo_bench": {
                "seed": config.get("seed"),
                "scenario": config.get("scenario"),
                "total_days": total_days,
                "initial_cash": _as_float(config.get("initial_cash")),
                "session_id": session_id,
                "label": config.get("label"),
            },
        },
        "agent_info": {
            "name": agent_type,
            "version": None,
            "model_info": {"name": model, "provider": _as_str(config.get("provider"))},
        },
        "agent_result": {
            "n_input_tokens": agent_tokens["input"],
            "n_cache_tokens": agent_tokens["cached"],
            "n_output_tokens": agent_tokens["output"],
            "cost_usd": agent_cost,
        },
        "verifier_result": {"rewards": {}},
        "started_at": started,
        "finished_at": finished,
        "agent_execution": {"started_at": started, "finished_at": finished},
    }
    trajectory_doc: dict[str, Any] = {
        "schema_version": "ATIF-v1.7",
        "session_id": run_id,
        "agent": {"name": agent_type, "version": None, "model_name": model},
        "steps": steps,
        "final_metrics": {
            "total_prompt_tokens": agent_tokens["input"],
            "total_completion_tokens": agent_tokens["output"],
            "total_cached_tokens": agent_tokens["cached"],
            "total_cost_usd": agent_cost,
            "extra": {
                "total_reasoning_tokens": agent_tokens["reasoning"],
                "cost_source": agent_cost_source,
            },
        },
    }

    final_cash = cash_daily[-1]["cash"] if cash_daily else None
    if final_cash is None and isinstance(session.get("final_cash"), float):
        final_cash = session["final_cash"]
    meta: dict[str, Any] = {
        "plugin": PLUGIN_NAME,
        "version": PLUGIN_VERSION,
        "bridge": {
            "module": "ceo_bench.bridge",
            "version": BRIDGE_VERSION,
            "upstream_repo": UPSTREAM_REPO,
            "upstream_commit": UPSTREAM_COMMIT,
        },
        "run_id": run_id,
        "trial_name": name,
        "model": model,
        "provider": config.get("provider"),
        "seed": config.get("seed"),
        "scenario": config.get("scenario"),
        "total_days": total_days,
        "days_run": days_run,
        "initial_cash": _as_float(config.get("initial_cash")),
        "final_cash": final_cash,
        "outcome": outcome,
        "bankruptcy": {"bankrupt": bankrupt_day is not None, "day": bankrupt_day},
        "world_nmdb": {
            "present": world["present"],
            "sha256": world["sha256"],
            "status": world["status"],
            "reason": world["reason"],
        },
        "harness_inputs": {
            "tool_result_rows": len(tool_rows),
            "timing_rows": len(timing_rows),
            "trajectory_steps": len(steps),
            "dashboard_rows_excluded": dashboard_excluded,
            "malformed_jsonl_lines": malformed,
        },
        "sources": [
            "ceo_bench/meta.json",
            "ceo_bench/cash_daily.json",
            "ceo_bench/spend.json",
            "ceo_bench/forecasts.json",
            "ceo_bench/weeks.json",
        ],
        "notes": sorted(set(notes)),
    }
    spend_doc: dict[str, Any] = {
        "separation_rule": (
            "api_costs purpose 'agent' is benchmarked-agent spend; every other "
            "purpose is simulator spend. Simulator spend is metered separately "
            "and never merged into agent cost."
        ),
        "agent": {
            "input_tokens": agent_tokens["input"],
            "cached_input_tokens": agent_tokens["cached"],
            "output_tokens": agent_tokens["output"],
            "cost_usd": agent_cost,
            "source": agent_cost_source,
        },
        "simulator": {
            **(simulator_spend or {"input_tokens": None, "output_tokens": None, "cost_usd": None}),
            "source": simulator_source,
        },
    }
    forecasts_doc: dict[str, Any] = {
        "status": forecast_status,
        "reason": forecast_reason,
        "recipe": (
            "predicted_value at submit_day for target_day = submit_day + horizon_days "
            "scored against cash then; pct_error null when actual is 0 or unknown"
        ),
        "rows": forecast_rows,
    }
    weeks_doc: dict[str, Any] = {
        "rule": (
            "weeks split at completed next-week/next-day advances (result holds "
            "'=== Week '); a week is a no-op when it holds no state-changing "
            "tool call (read-only file tools, _dashboard, read-only/query "
            "novamind subcommands and time advances excluded; unknown commands "
            "count as state-changing)"
        ),
        "noop_weeks": noop_weeks,
        "weeks": weeks,
    }
    cash_doc: dict[str, Any] = {
        "source": cash_source,
        "recipe": (
            "running sum of ledger.amount by day" if cash_source == "ledger"
            else "timing day_summary cash readings"
            if cash_source == "timing_day_summary"
            else None
        ),
        "daily": cash_daily,
    }

    trial.mkdir(parents=True, exist_ok=True)
    (trial / "result.json").write_text(json.dumps(result_doc, indent=2), encoding="utf-8")
    agent_dir = trial / "agent"
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / "trajectory.json").write_text(
        json.dumps(trajectory_doc), encoding="utf-8"
    )
    ceo_dir = trial / "ceo_bench"
    ceo_dir.mkdir(exist_ok=True)
    for filename, doc in (
        ("meta.json", meta),
        ("cash_daily.json", cash_doc),
        ("spend.json", spend_doc),
        ("forecasts.json", forecasts_doc),
        ("weeks.json", weeks_doc),
    ):
        (ceo_dir / filename).write_text(json.dumps(doc, indent=2), encoding="utf-8")

    return {
        "trial_dir": str(trial),
        "trial_name": name,
        "run_id": run_id,
        "outcome": outcome,
        "bankrupt_day": bankrupt_day,
        "trajectory_steps": len(steps),
        "noop_weeks": noop_weeks,
        "forecast_rows": len(forecast_rows),
        "agent_cost_usd": agent_cost,
        "simulator_cost_usd": (simulator_spend or {}).get("cost_usd"),
        "notes": sorted(set(notes)),
    }
