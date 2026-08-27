from __future__ import annotations

import contextlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

import yaml

from evallab.cohort import wilson_interval
from evallab.storage.attach import AttachResult, ZoneStatus, attach

Row = Mapping[str, Any]

PANES: dict[str, str] = {
    "leaderboard": "z2.trials",
    "canaries": "z2.canary_drift_observations",
    "spend": "z2.trials",
    "calibrations": "z2.judge_calibrations",
    "atif": "trial_facts",
    "discoveries": "z4.front_matter",
}


class ZoneUnavailableError(RuntimeError):
    """Raised when a query requires a storage zone that is not attached."""

    def __init__(self, zone: str, reason: str | None = None) -> None:
        message = f"zone {zone} unavailable: {reason or 'not attached'}"
        super().__init__(message)
        self.zone = zone
        self.reason = reason


class QuerySource(Protocol):
    def query(
        self, statement: str, parameters: Sequence[Any] = ()
    ) -> list[dict[str, Any]]: ...

    def relation_exists(self, name: str) -> bool: ...


class AttachSource:
    """Unified DuckDB attach surface reader (Z2 PostgreSQL + Z3 Parquet + Z4 docs)."""

    def __init__(
        self,
        attach_result: AttachResult | None = None,
        *,
        repo_root: Path | None = None,
        explicit_derived: Path | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        if attach_result is not None:
            self.result = attach_result
        else:
            self.result = attach(
                repo_root=repo_root,
                explicit_derived=explicit_derived,
                environ=environ,
            )
        self.connection = self.result.connection
        self.zones: dict[str, ZoneStatus] = {z.name: z for z in self.result.zones}

    def zone_status(self, name: str) -> ZoneStatus | None:
        return self.zones.get(name)

    def is_zone_attached(self, name: str) -> bool:
        z = self.zones.get(name)
        return bool(z and z.attached)

    def require_zone(self, name: str) -> None:
        z = self.zones.get(name)
        if z is None or not z.attached:
            reason = z.reason if z else "zone not found"
            detail = f" ({z.detail})" if z and z.detail else ""
            raise ZoneUnavailableError(name, f"{reason}{detail}")

    def query(
        self, statement: str, parameters: Sequence[Any] = ()
    ) -> list[dict[str, Any]]:
        cursor = self.connection.execute(statement, list(parameters))
        if cursor.description is None:
            return []
        cols = [c[0] for c in cursor.description]
        return [dict(zip(cols, row, strict=True)) for row in cursor.fetchall()]

    def relation_exists(self, name: str) -> bool:
        try:
            rows = self.connection.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = ? LIMIT 1",
                [name],
            ).fetchall()
            if rows:
                return True
            views = self.connection.execute(
                "SELECT 1 FROM duckdb_views() WHERE view_name = ? LIMIT 1",
                [name],
            ).fetchall()
            return len(views) > 0
        except Exception:
            return False

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.connection.close()


LEADERBOARD_SQL = """
SELECT
    COALESCE(j.experiment_id, 'unassigned') AS cohort,
    CAST(t.id AS text) AS trial_id,
    COALESCE(t.task_name, 'unknown') AS task_name,
    COALESCE(t.agent_name, 'unknown') AS agent_name,
    COALESCE(t.model_name, 'adhoc') AS model_name,
    t.primary_reward,
    t.exception_type
FROM z2.public.trials t
JOIN z2.public.jobs j ON j.id = t.job_id
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
FROM z2.public.canary_drift_observations
ORDER BY observation_date, task_name, agent_name
"""

SPEND_SQL = """
SELECT
    CAST(finished_at AS date) AS spend_date,
    count(*) AS trial_count,
    COALESCE(sum(cost_usd), 0) AS spend_usd
FROM z2.public.trials
WHERE finished_at IS NOT NULL
  AND CAST(finished_at AS date) >= ?
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
FROM z2.public.judge_calibrations
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
FROM trial_facts
"""

TOOL_USAGE_SQL = """
SELECT
    function_name,
    sum(call_count) AS call_count,
    count(DISTINCT trial_id) AS trial_count
FROM tool_usage
GROUP BY function_name
ORDER BY call_count DESC, function_name
LIMIT 12
"""

_DISCOVERY_HEADER = re.compile(r"^## (?P<discovery_id>D-[^ ]+) — (?P<status>[^\s]+)\s*$")


def leaderboard(source: QuerySource, *, pass_threshold: float = 1.0) -> list[dict[str, Any]]:
    if isinstance(source, AttachSource):
        source.require_zone("z2")
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
        exceptions = sum(row["exception_type"] is not None for row in rows)
        # A cohort with trials but nothing scorable is not the same state as a
        # cohort with no trials, and the renderer must be able to say which.
        no_reward = sum(
            row["exception_type"] is None and row["primary_reward"] is None for row in rows
        )
        summaries.append(
            {
                "cohort": cohort,
                "task": task_name,
                "agent": agent_name,
                "model": model_name,
                "n_total": len(rows),
                "n": len(scored),
                "exceptions": exceptions,
                "unscored_no_reward": no_reward,
                "scorable": bool(scored),
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
    if isinstance(source, AttachSource):
        source.require_zone("z2")
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
    if isinstance(source, AttachSource):
        source.require_zone("z2")
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
    catalog: list[dict[str, Any]] = []
    if isinstance(source, AttachSource):
        if source.is_zone_attached("z2") and source.relation_exists("judge_calibrations"):
            catalog = source.query(CALIBRATION_SQL)
        elif not source.is_zone_attached("z2"):
            files = _file_calibrations(records_root)
            if not files:
                source.require_zone("z2")
            return sorted(files, key=lambda row: (str(row["date"]), row["record_id"]), reverse=True)
    elif source.relation_exists("judge_calibrations"):
        catalog = source.query(CALIBRATION_SQL)

    rows = [_agreement_summary(row) for row in catalog]
    known = {row["record_id"] for row in rows}
    rows.extend(row for row in _file_calibrations(records_root) if row["record_id"] not in known)
    return sorted(rows, key=lambda row: (str(row["date"]), row["record_id"]), reverse=True)


def atif_activity(source: QuerySource) -> dict[str, Any]:
    if isinstance(source, AttachSource):
        source.require_zone("z3")
    summary_rows = source.query(ATIF_SUMMARY_SQL)
    summary_row = summary_rows[0] if summary_rows else None
    summary = (
        summary_row
        if summary_row and summary_row.get("trial_count", 0) > 0
        else None
    )
    tools = source.query(TOOL_USAGE_SQL) if source.relation_exists("tool_usage") else []
    return {"summary": summary, "tools": tools}


def knowledge_front_matter(source: QuerySource) -> list[dict[str, Any]]:
    if isinstance(source, AttachSource):
        source.require_zone("z4")
    return source.query(
        "SELECT path, title, status, audience, generated_by FROM z4.front_matter ORDER BY path"
    )


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
