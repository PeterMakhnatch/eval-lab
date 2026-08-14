from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

import duckdb
import psycopg
import yaml
from psycopg.rows import dict_row

from evallab.cohort import wilson_interval

Row = Mapping[str, Any]


class QuerySource(Protocol):
    def query(self, statement: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]: ...

    def relation_exists(self, name: str) -> bool: ...


class ReadOnlyPostgres:
    """Small PostgreSQL reader that makes accidental writes fail at the server."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def query(
        self, statement: str, parameters: Sequence[Any] = ()
    ) -> list[dict[str, Any]]:
        with psycopg.connect(
            self.database_url,
            connect_timeout=2,
            options="-c default_transaction_read_only=on -c statement_timeout=1500",
            row_factory=dict_row,
        ) as connection:
            return [dict(row) for row in connection.execute(statement, parameters).fetchall()]

    def relation_exists(self, name: str) -> bool:
        rows = self.query("SELECT to_regclass(%s) IS NOT NULL AS present", (name,))
        return bool(rows and rows[0]["present"])


LEADERBOARD_SQL = """
SELECT
    COALESCE(j.experiment_id, 'unassigned') AS cohort,
    CAST(t.id AS text) AS trial_id,
    COALESCE(t.task_name, 'unknown') AS task_name,
    COALESCE(t.agent_name, 'unknown') AS agent_name,
    COALESCE(t.model_name, 'adhoc') AS model_name,
    t.primary_reward,
    t.exception_type
FROM trials t
JOIN jobs j ON j.id = t.job_id
ORDER BY cohort, task_name, agent_name, model_name, trial_id
"""

CANARY_SQL = """
SELECT
    observation_date,
    task_name,
    task_version,
    agent_name,
    reward,
    attempt_count,
    exception_count,
    baseline_n,
    baseline_mean,
    baseline_stddev,
    is_harness_drift_suspect,
    drift_reason
FROM canary_drift_observations
ORDER BY observation_date, task_name, agent_name
"""

SPEND_SQL = """
SELECT
    (
        CAST(finished_at AS timestamptz)
        AT TIME ZONE current_setting('TIMEZONE')
    )::date AS spend_date,
    count(*) AS trial_count,
    COALESCE(sum(cost_usd), 0) AS spend_usd
FROM trials
WHERE finished_at IS NOT NULL
  AND (
      CAST(finished_at AS timestamptz)
      AT TIME ZONE current_setting('TIMEZONE')
  )::date >= %s
GROUP BY spend_date
ORDER BY spend_date
"""

CALIBRATION_SQL = """
SELECT
    record_id,
    family,
    status,
    judge_backend,
    judge_model,
    per_criterion_agreement,
    agreement_floor,
    meets_floor,
    reportable,
    document_count,
    evaluated_on
FROM judge_calibrations
ORDER BY evaluated_on DESC, family, judge_model, record_id
"""

ATIF_SUMMARY_SQL = """
SELECT
    count(*) AS trial_count,
    COALESCE(sum(trajectory_count), 0) AS trajectory_count,
    COALESCE(sum(step_count), 0) AS step_count,
    COALESCE(sum(llm_call_count), 0) AS llm_call_count,
    COALESCE(sum(tool_call_count), 0) AS tool_call_count,
    count(*) FILTER (WHERE invalid_trajectory_count > 0) AS invalid_trial_count
FROM read_parquet(?, hive_partitioning = true, union_by_name = true)
"""

TOOL_USAGE_SQL = """
SELECT
    function_name,
    sum(call_count) AS call_count,
    count(DISTINCT trial_id) AS trial_count
FROM read_parquet(?, hive_partitioning = true, union_by_name = true)
GROUP BY function_name
ORDER BY call_count DESC, function_name
LIMIT 12
"""

_DISCOVERY_HEADER = re.compile(r"^## (?P<discovery_id>D-[^ ]+) — (?P<status>[^\s]+)\s*$")


def leaderboard(source: QuerySource, *, pass_threshold: float = 1.0) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[Row]] = defaultdict(list)
    for row in source.query(LEADERBOARD_SQL):
        key = tuple(
            str(row[field]) for field in ("cohort", "task_name", "agent_name", "model_name")
        )
        grouped[key].append(row)

    summaries: list[dict[str, Any]] = []
    for (cohort, task_name, agent_name, model_name), rows in grouped.items():
        scored = [
            float(row["primary_reward"])
            for row in rows
            if row["exception_type"] is None and row["primary_reward"] is not None
        ]
        passes = sum(value >= pass_threshold for value in scored)
        interval = wilson_interval(passes, len(scored))
        summaries.append(
            {
                "cohort": cohort,
                "task": task_name,
                "agent": agent_name,
                "model": model_name,
                "n_total": len(rows),
                "n": len(scored),
                "exceptions": sum(row["exception_type"] is not None for row in rows),
                "passes": passes,
                "pass_rate": passes / len(scored) if scored else None,
                "ci_95_low": interval[0] if interval else None,
                "ci_95_high": interval[1] if interval else None,
            }
        )
    return sorted(
        summaries,
        key=lambda row: (
            row["cohort"],
            -(row["pass_rate"] if row["pass_rate"] is not None else -1),
            row["task"],
            row["agent"],
            row["model"],
        ),
    )


def _normal_interval(mean: Any, standard_deviation: Any, n: int) -> tuple[float, float] | None:
    if mean is None or standard_deviation is None or n < 2:
        return None
    half_width = 1.959963984540054 * float(standard_deviation) / math.sqrt(n)
    return float(mean) - half_width, float(mean) + half_width


def canary_history(source: QuerySource) -> list[dict[str, Any]]:
    history = []
    for raw in source.query(CANARY_SQL):
        row = dict(raw)
        baseline_n = int(row["baseline_n"])
        interval = _normal_interval(row["baseline_mean"], row["baseline_stddev"], baseline_n)
        row["baseline_95_low"] = interval[0] if interval else None
        row["baseline_95_high"] = interval[1] if interval else None
        history.append(row)
    return history


def spend_history(source: QuerySource, *, through: date, days: int = 7) -> list[dict[str, Any]]:
    start = through - timedelta(days=days - 1)
    observed: dict[date, dict[str, Any]] = {}
    for row in source.query(SPEND_SQL, (start,)):
        spend_date = row["spend_date"]
        normalized_date = (
            date.fromisoformat(spend_date) if isinstance(spend_date, str) else spend_date
        )
        observed[normalized_date] = {
            "date": normalized_date,
            "trial_count": int(row["trial_count"]),
            "spend_usd": float(row["spend_usd"]),
        }
    return [
        observed.get(
            start + timedelta(days=offset),
            {"date": start + timedelta(days=offset), "trial_count": 0, "spend_usd": 0.0},
        )
        for offset in range(days)
    ]


def daily_ceiling(policy_path: Path) -> float:
    payload = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "daily_cost_ceiling_usd" not in payload:
        raise ValueError(f"missing daily cost ceiling: {policy_path}")
    return float(payload["daily_cost_ceiling_usd"])


def queue_funnel(queue_root: Path) -> list[dict[str, Any]]:
    return [
        {"state": state, "count": sum(1 for _ in (queue_root / state).glob("*.json"))}
        for state in ("pending", "approved", "running", "done", "failed")
    ]


def _agreement_summary(raw: Row) -> dict[str, Any]:
    criterion = raw.get("per_criterion_agreement") or {}
    if isinstance(criterion, str):
        criterion = json.loads(criterion)
    agreements = sum(int(item["agreements"]) for item in criterion.values())
    decisions = sum(int(item["total"]) for item in criterion.values())
    interval = wilson_interval(agreements, decisions)
    return {
        "date": raw["evaluated_on"],
        "family": raw["family"],
        "backend": raw["judge_backend"],
        "model": raw["judge_model"],
        "status": raw["status"],
        "documents": int(raw["document_count"]),
        "n": decisions,
        "agreements": agreements,
        "agreement": agreements / decisions if decisions else None,
        "ci_95_low": interval[0] if interval else None,
        "ci_95_high": interval[1] if interval else None,
        "floor": float(raw["agreement_floor"]),
        "meets_floor": bool(raw["meets_floor"]),
        "reportable": bool(raw["reportable"]),
        "record_id": raw["record_id"],
    }


def _file_calibrations(records_root: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(records_root.glob("*/*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "record_id" not in payload:
            continue
        records.append(_agreement_summary(payload))
    return records


def calibration_history(source: QuerySource, *, records_root: Path) -> list[dict[str, Any]]:
    catalog = source.query(CALIBRATION_SQL) if source.relation_exists("judge_calibrations") else []
    rows = [_agreement_summary(row) for row in catalog]
    known = {row["record_id"] for row in rows}
    rows.extend(row for row in _file_calibrations(records_root) if row["record_id"] not in known)
    return sorted(rows, key=lambda row: (str(row["date"]), row["record_id"]), reverse=True)


def atif_activity(parquet_root: Path) -> dict[str, Any]:
    trial_files = list(parquet_root.glob("**/trial_facts.parquet"))
    if not trial_files:
        return {"summary": None, "tools": []}
    trial_glob = (parquet_root / "**/trial_facts.parquet").as_posix()
    tool_glob = (parquet_root / "**/tool_usage.parquet").as_posix()
    with duckdb.connect(database=":memory:") as connection:
        summary_row = connection.execute(ATIF_SUMMARY_SQL, [trial_glob]).fetchone()
        columns = [item[0] for item in connection.description]
        summary = dict(zip(columns, summary_row, strict=True)) if summary_row else None
        tools = []
        if list(parquet_root.glob("**/tool_usage.parquet")):
            result = connection.execute(TOOL_USAGE_SQL, [tool_glob])
            tool_columns = [item[0] for item in result.description]
            tools = [dict(zip(tool_columns, row, strict=True)) for row in result.fetchall()]
    return {"summary": summary, "tools": tools}


def discoveries(path: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        header = _DISCOVERY_HEADER.match(line)
        if header:
            current = header.groupdict()
            current["claim"] = ""
            entries.append(current)
        elif current is not None and line.startswith("- Claim: "):
            current["claim"] = line.removeprefix("- Claim: ")
    return entries
