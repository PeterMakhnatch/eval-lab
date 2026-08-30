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
    "experiment_operations": "z2.trials",
}

OUTCOME_SCORED = "scored"
OUTCOME_PROVIDER_ACCESS = "provider_access"
OUTCOME_HARNESS_FAILURE = "harness_failure"
OUTCOME_REFUSED = "refused"

ELIGIBILITY_CAUSAL_ADMISSIBLE = "causal_admissible"
ELIGIBILITY_CALIBRATION_ONLY = "calibration_only"

_DOSE_LADDER_MAP: dict[str, int] = {
    "4096": 4096,
    "16384": 16384,
    "65536": 65536,
    "131072": 131072,
    "4k": 4096,
    "16k": 16384,
    "64k": 65536,
    "128k": 131072,
}
_DOSE_PATTERN = re.compile(r"\b(4096|16384|65536|131072|4k|16k|64k|128k)\b", re.IGNORECASE)


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

EXPERIMENT_OPERATIONS_SQL = """
SELECT
    COALESCE(j.experiment_id, 'unassigned') AS cohort,
    CAST(j.id AS text) AS job_id,
    j.job_name,
    CAST(t.id AS text) AS trial_id,
    t.trial_name,
    COALESCE(t.task_name, 'unknown') AS task_name,
    COALESCE(t.agent_name, 'unknown') AS agent_name,
    COALESCE(t.model_name, 'adhoc') AS model_name,
    t.primary_reward,
    t.exception_type,
    t.started_at,
    t.finished_at,
    t.cost_usd,
    j.lab_metadata #>> '{experiment,policy_rule}' AS policy_rule,
    j.lab_metadata #>> '{experiment,purpose}' AS purpose,
    j.lab_metadata #>> '{network_adaptation,network_isolation_enforced}' AS isolation_enforced,
    j.lab_metadata #>> '{network_adaptation,effective_agent_network}' AS effective_network,
    j.lab_metadata #>> '{host,platform}' AS host_platform,
    CASE WHEN j.lab_metadata #>> '{provider_usage}' IS NOT NULL THEN true ELSE false END AS has_credential_proxy,
    t.raw_result #>> '{exception_info,exception_type}' AS result_exception_type,
    t.raw_result #>> '{exception_info,exception_message}' AS result_exception_message,
    t.raw_result #>> '{agent_result,refusal_reason}' AS raw_refusal_reason,
    f.task_family,
    f.task_block_id,
    f.task_instance_id,
    f.arm_id,
    f.generator_seed_json,
    f.trajectory_count,
    f.invalid_trajectory_count
FROM z2.public.trials t
JOIN z2.public.jobs j ON j.id = t.job_id
LEFT JOIN z2.public.deterministic_trial_facts f ON f.trial_id = t.id
ORDER BY cohort, job_name, trial_name
"""

_DISCOVERY_HEADER = re.compile(r"^## (?P<discovery_id>D-[^ ]+) — (?P<status>[^\s]+)\s*$")


def classify_trial_outcome(row: Row) -> str:
    """Classify a trial into scored, provider_access, harness_failure, or refused.

    Invariants:
    - Provider transient exceptions (429, 5xx, overloaded) are provider_access, NOT harness_failure.
    - Non-transient exceptions are harness_failure.
    - Trials with primary_reward are scored.
    - Trials with neither exception nor reward are refused (abstentions / missing outcomes),
      never coerced to zero reward.
    """
    exc = row.get("exception_type")
    reward = row.get("primary_reward")

    if exc == "transient_harness":
        return OUTCOME_PROVIDER_ACCESS
    if exc is not None:
        return OUTCOME_HARNESS_FAILURE
    if reward is not None:
        return OUTCOME_SCORED
    return OUTCOME_REFUSED


def refusal_reason_for_trial(row: Row) -> str | None:
    """Extract or derive refusal/unscored rationale without coercing to zero."""
    outcome = classify_trial_outcome(row)
    if outcome == OUTCOME_SCORED:
        return None
    if outcome == OUTCOME_PROVIDER_ACCESS:
        msg = row.get("result_exception_message")
        return f"provider access failure: {msg or 'transient rate limit or capacity exhaustion'}"
    if outcome == OUTCOME_HARNESS_FAILURE:
        exc_type = row.get("exception_type")
        msg = row.get("result_exception_message")
        return f"harness exception: {exc_type}{f' ({msg})' if msg else ''}"

    explicit = row.get("raw_refusal_reason")
    if explicit:
        return str(explicit)
    msg = row.get("result_exception_message")
    if msg:
        return str(msg)
    return "unscored: no reward recorded (unreached or unmeasured verdict)"


def classify_evidence_eligibility(row: Row) -> str:
    """Classify trial evidence eligibility: causal_admissible vs calibration_only.

    Invariants:
    - Darwin hosts or public-egress network modes are calibration_only.
    - Controls (oracle, nop) are calibration_only.
    - Calibration policy rules / purposes are calibration_only.
    - Causal-grade admission requires Linux enforced isolation AND credential proxy,
      and trial must be scored (not errored/refused).
    """
    agent = str(row.get("agent_name") or "")
    if agent in {"oracle", "nop"}:
        return ELIGIBILITY_CALIBRATION_ONLY

    purpose = str(row.get("purpose") or "")
    policy_rule = str(row.get("policy_rule") or "")
    if purpose == "calibration" or policy_rule == "calibration":
        return ELIGIBILITY_CALIBRATION_ONLY

    host_platform = str(row.get("host_platform") or "").lower()
    if "darwin" in host_platform or "macos" in host_platform:
        return ELIGIBILITY_CALIBRATION_ONLY

    effective_network = str(row.get("effective_network") or "").lower()
    if effective_network == "public":
        return ELIGIBILITY_CALIBRATION_ONLY

    isolation_raw = row.get("isolation_enforced")
    isolation_enforced = (
        isolation_raw is True
        or str(isolation_raw).lower() == "true"
    )
    if not isolation_enforced:
        return ELIGIBILITY_CALIBRATION_ONLY

    has_proxy = bool(row.get("has_credential_proxy"))
    if not has_proxy:
        return ELIGIBILITY_CALIBRATION_ONLY

    outcome = classify_trial_outcome(row)
    if outcome != OUTCOME_SCORED:
        return ELIGIBILITY_CALIBRATION_ONLY

    return ELIGIBILITY_CAUSAL_ADMISSIBLE


def experiment_operations(source: QuerySource) -> list[dict[str, Any]]:
    """Return trial-level operational view with clean outcome and eligibility separation.

    Guarantees:
    - Scored trials are separated from harness failures and provider access failures.
    - Refusal reasons are reported instead of coercing missing outcomes to zero.
    - Causal-grade admissibility is distinguished from calibration-only evidence.
    - Pass rates exclude all un-scored trials (zero denominator contamination).
    """
    if isinstance(source, AttachSource):
        source.require_zone("z2")

    trials: list[dict[str, Any]] = []
    for raw in source.query(EXPERIMENT_OPERATIONS_SQL):
        row = dict(raw)
        outcome = classify_trial_outcome(row)
        eligibility = classify_evidence_eligibility(row)
        refusal = refusal_reason_for_trial(row)

        row["outcome_class"] = outcome
        row["eligibility"] = eligibility
        row["refusal_reason"] = refusal
        row["is_scored"] = (outcome == OUTCOME_SCORED)
        row["is_passed"] = (
            outcome == OUTCOME_SCORED
            and row.get("primary_reward") is not None
            and float(row["primary_reward"]) >= 1.0
        )
        trials.append(row)
    return trials


def experiment_operations_summary(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize operational campaign progress by cohort, task, agent, and model.

    Preserves capability denominator (scored only) strictly separate from access
    failures, harness failures, and calibration-only trials.
    """
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for trial in trials:
        key = (
            str(trial.get("cohort") or "unassigned"),
            str(trial.get("task_name") or "unknown"),
            str(trial.get("agent_name") or "unknown"),
            str(trial.get("model_name") or "adhoc"),
        )
        grouped[key].append(trial)

    summaries: list[dict[str, Any]] = []
    for (cohort, task, agent, model), rows in grouped.items():
        n_total = len(rows)
        scored_rows = [r for r in rows if r["outcome_class"] == OUTCOME_SCORED]
        n_scored = len(scored_rows)

        passes = sum(1 for r in scored_rows if r["is_passed"])
        harness_failures = sum(1 for r in rows if r["outcome_class"] == OUTCOME_HARNESS_FAILURE)
        provider_access_failures = sum(1 for r in rows if r["outcome_class"] == OUTCOME_PROVIDER_ACCESS)
        refusals = sum(1 for r in rows if r["outcome_class"] == OUTCOME_REFUSED)

        calibration_only = sum(1 for r in rows if r["eligibility"] == ELIGIBILITY_CALIBRATION_ONLY)
        causal_admissible = sum(1 for r in rows if r["eligibility"] == ELIGIBILITY_CAUSAL_ADMISSIBLE)

        interval = wilson_interval(passes, n_scored) if n_scored > 0 else None

        summaries.append({
            "cohort": cohort,
            "task": task,
            "agent": agent,
            "model": model,
            "n_total": n_total,
            "n_scored": n_scored,
            "passes": passes,
            "pass_rate": (passes / n_scored) if n_scored > 0 else None,
            "ci_95_low": interval[0] if interval else None,
            "ci_95_high": interval[1] if interval else None,
            "harness_failures": harness_failures,
            "provider_access_failures": provider_access_failures,
            "refusals": refusals,
            "calibration_only": calibration_only,
            "causal_admissible": causal_admissible,
            "scorable": (n_scored > 0),
        })

    return sorted(
        summaries,
        key=lambda s: (
            s["cohort"],
            -(s["pass_rate"] if s["pass_rate"] is not None else -1),
            s["task"],
            s["agent"],
            s["model"],
        ),
    )


def model_access_vs_capability(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Report model access reliability separately from capability pass rates.

    Prevents confusing provider access outages / rate limits with poor capability.
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for trial in trials:
        agent = str(trial.get("agent_name") or "unknown")
        model = str(trial.get("model_name") or "adhoc")
        grouped[(agent, model)].append(trial)

    rows: list[dict[str, Any]] = []
    for (agent, model), cohort_trials in grouped.items():
        total_attempts = len(cohort_trials)
        access_failures = sum(
            1 for t in cohort_trials if t["outcome_class"] == OUTCOME_PROVIDER_ACCESS
        )
        harness_failures = sum(
            1 for t in cohort_trials if t["outcome_class"] == OUTCOME_HARNESS_FAILURE
        )
        refused_count = sum(
            1 for t in cohort_trials if t["outcome_class"] == OUTCOME_REFUSED
        )
        scored_trials = [t for t in cohort_trials if t["outcome_class"] == OUTCOME_SCORED]
        n_scored = len(scored_trials)
        passes = sum(1 for t in scored_trials if t["is_passed"])

        access_success_rate = (
            (total_attempts - access_failures) / total_attempts if total_attempts > 0 else None
        )
        capability_rate = (passes / n_scored) if n_scored > 0 else None
        interval = wilson_interval(passes, n_scored) if n_scored > 0 else None

        if access_failures == 0:
            access_status = "accessible"
        elif n_scored == 0:
            access_status = "access_blocked"
        else:
            access_status = "degraded_access"

        if access_failures > 0:
            basis = (
                f"{n_scored} of {total_attempts} attempts scored "
                f"({access_failures} provider access failure(s))"
            )
        elif total_attempts == n_scored:
            basis = f"all {total_attempts} attempts reached model and scored"
        else:
            other_unscored = total_attempts - n_scored
            basis = (
                f"{n_scored} of {total_attempts} attempts scored "
                f"({other_unscored} non-access un-scored attempt(s))"
            )

        rows.append({
            "agent": agent,
            "model": model,
            "total_attempts": total_attempts,
            "access_failures": access_failures,
            "access_success_rate": access_success_rate,
            "access_status": access_status,
            "harness_failures": harness_failures,
            "refusals": refused_count,
            "n_scored": n_scored,
            "passes": passes,
            "capability_pass_rate": capability_rate,
            "ci_95_low": interval[0] if interval else None,
            "ci_95_high": interval[1] if interval else None,
            "access_vs_capability_basis": basis,
        })

    return sorted(
        rows,
        key=lambda r: (
            r["access_status"] != "accessible",
            -(r["capability_pass_rate"] if r["capability_pass_rate"] is not None else -1),
            r["agent"],
            r["model"],
        ),
    )


def action_memory_contrast_fidelity(trials: list[dict[str, Any]]) -> dict[str, Any]:
    """Report Action Memory matched contrast fidelity across dose ladder cells.

    Pairs by (task_block_id, dose_bytes, seed) and exposes:
    - coverage: fraction of expected matched pairs with complete observed arms
    - unknown: trials where contrast key (task_block_id, dose_bytes, seed) is unresolved
    - omitted: missing expected arms within a contrast group
    - duplicate: duplicate trials per arm in the same contrast cell
    - order fidelity: deterministic ordering and distinction across arms
    """
    am_trials = [
        t for t in trials
        if "action-memory" in str(t.get("task_family") or "").lower()
        or "action-memory" in str(t.get("task_name") or "").lower()
    ]

    if not am_trials:
        return {
            "total_trials": 0,
            "matched_pairs": 0,
            "coverage_fidelity": None,
            "unknown_count": 0,
            "omitted_count": 0,
            "duplicate_count": 0,
            "order_fidelity_rate": None,
            "contrast_groups": [],
            "unknown_trials": [],
        }

    def extract_seed(trial: dict[str, Any]) -> int | None:
        raw_seed = trial.get("generator_seed_json")
        if raw_seed is None:
            return None
        try:
            parsed = json.loads(str(raw_seed)) if isinstance(raw_seed, str) else raw_seed
            if isinstance(parsed, dict) and "seed" in parsed:
                return int(parsed["seed"])
            if isinstance(parsed, int):
                return parsed
        except Exception:
            pass
        return None

    def extract_dose(trial: dict[str, Any]) -> int | None:
        for field in ("arm_id", "task_instance_id", "task_name"):
            val = str(trial.get(field) or "")
            match = _DOSE_PATTERN.search(val)
            if match:
                return _DOSE_LADDER_MAP[match.group(1).lower()]
        return None

    grouped: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
    unknown_trials: list[dict[str, Any]] = []

    for t in am_trials:
        block_id = t.get("task_block_id")
        seed = extract_seed(t)
        dose = extract_dose(t)

        if block_id is None or seed is None or dose is None:
            unknown_trials.append(t)
            continue

        key = (str(block_id), dose, seed)
        grouped[key].append(t)

    contrast_groups = []
    total_omitted = 0
    total_duplicate = 0
    complete_pairs = 0
    order_consistent_count = 0

    for (block_id, dose_bytes, seed), group_trials in sorted(grouped.items()):
        arms_present: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in group_trials:
            arm_name = str(t.get("arm_id") or t.get("task_instance_id") or "unspecified")
            arms_present[arm_name].append(t)

        duplicates = sum(max(0, len(items) - 1) for items in arms_present.values())
        total_duplicate += duplicates

        distinct_arms = len(arms_present)
        is_complete = distinct_arms >= 2
        if is_complete:
            complete_pairs += 1
        else:
            total_omitted += (2 - distinct_arms)

        has_order_fidelity = is_complete and distinct_arms == len(group_trials) and duplicates == 0
        if has_order_fidelity:
            order_consistent_count += 1

        contrast_groups.append({
            "task_block_id": block_id,
            "dose_bytes": dose_bytes,
            "seed": seed,
            "trial_count": len(group_trials),
            "distinct_arms": distinct_arms,
            "arms": list(arms_present.keys()),
            "is_complete_pair": is_complete,
            "has_duplicates": (duplicates > 0),
            "order_fidelity": has_order_fidelity,
        })

    n_groups = len(grouped)
    coverage_fidelity = (complete_pairs / n_groups) if n_groups > 0 else None
    order_fidelity_rate = (order_consistent_count / n_groups) if n_groups > 0 else None

    return {
        "total_trials": len(am_trials),
        "matched_pairs": complete_pairs,
        "total_contrast_groups": n_groups,
        "coverage_fidelity": coverage_fidelity,
        "unknown_count": len(unknown_trials),
        "omitted_count": total_omitted,
        "duplicate_count": total_duplicate,
        "order_fidelity_rate": order_fidelity_rate,
        "contrast_groups": contrast_groups,
        "unknown_trials": [
            {
                "trial_id": t.get("trial_id"),
                "task_name": t.get("task_name"),
                "task_block_id": t.get("task_block_id"),
                "arm_id": t.get("arm_id"),
            }
            for t in unknown_trials
        ],
    }


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
