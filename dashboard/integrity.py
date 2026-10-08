"""Integrity queries: read-only row builders over the attach surface."""

from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dashboard.queries import QuerySource

Row = dict[str, Any]

#: Per-run viewer (always-on results viewer) route templates.
VIEWER_BASE = "http://127.0.0.1:8100"


def trial_url(job: str, trial: str) -> str:
    """Viewer URL for one trial (route only; not a liveness claim)."""
    return f"{VIEWER_BASE}/jobs/{job}/trials/{trial}"


class SourceUnavailable(RuntimeError):
    """A dashboard source is missing; render "not available: <reason>"."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"not available: {reason}")
        self.reason = reason


def require_relation(source: QuerySource, name: str, *, zone: str = "z3") -> None:
    """Raise an honest error when an attach-surface relation is absent."""
    if not source.relation_exists(name):
        raise SourceUnavailable(f"{name} is not in the attach surface")


def _rows(source: QuerySource, statement: str) -> list[Row]:
    return [dict(row) for row in source.query(statement)]


# ---------------------------------------------------------------------------
# Task layer (attach views)
# ---------------------------------------------------------------------------

TASK_AUDIT_SQL = """
SELECT
    a.task_version_digest AS task_version_digest,
    a.task_id AS task_id,
    a.task_name AS task_name,
    a.domain AS domain,
    a.split_group AS split_group,
    a.grader_kind AS grader_kind,
    a.backend AS backend,
    a.agent_name AS agent_name,
    a.model_name AS model_name,
    a.n_attempts AS n_attempts,
    a.n_scored AS n_scored,
    a.n_infra AS n_infra,
    o.n_pass AS n_pass,
    o.mean_reward AS mean_reward,
    a.pass_rate AS pass_rate,
    a.verdict AS verdict,
    a.stability AS stability,
    a.exploit_status AS exploit_status,
    a.qual_broken AS qual_broken,
    a.finding_rule AS finding_rule
FROM v_task_audit AS a
LEFT JOIN v_task_outcomes AS o
  ON o.task_version_digest = a.task_version_digest
  AND o.backend IS NOT DISTINCT FROM a.backend
  AND o.agent_name IS NOT DISTINCT FROM a.agent_name
  AND o.model_name IS NOT DISTINCT FROM a.model_name
"""

FINDINGS_SQL = """
SELECT task_id AS task_id, domain AS domain, rule AS rule,
       severity AS severity, message AS message
FROM task_findings
"""

VERSIONS_SQL = """
SELECT task_id AS task_id, task_name AS task_name, domain AS domain,
       category AS category, grader_kind AS grader_kind,
       grader_cost AS grader_cost, has_solution AS has_solution,
       network_mode AS network_mode, agent_user AS agent_user
FROM task_versions
"""

QUALIFICATION_SQL = """
SELECT task_id AS task_id, job_name AS job_name, trial_name AS trial_name,
       reward AS reward, setup_ok AS setup_ok,
       verifier_completed AS verifier_completed,
       grader_error AS grader_error,
       infra_error_class AS infra_error_class
FROM task_qualification
"""

STABILITY_SQL = """
SELECT task_version_digest AS task_version_digest, verdict AS verdict,
       n_runs AS n_runs, rewards AS rewards, evidence_path AS evidence_path
FROM task_stability
"""

EXPLOITS_SQL = """
SELECT task_version_digest AS task_version_digest, job_name AS job_name,
       trial_name AS trial_name, probe_config AS probe_config,
       reward AS reward, exploit_status AS exploit_status,
       method AS method, evidence_path AS evidence_path,
       produced_at AS produced_at
FROM task_exploits
"""


def task_audit_rows(source: QuerySource) -> list[Row]:
    """One row per audited cohort (version x backend x agent x model)."""
    require_relation(source, "v_task_audit")
    require_relation(source, "v_task_outcomes")
    return _rows(source, TASK_AUDIT_SQL)


def trial_identity_rows(source: QuerySource) -> list[Row]:
    """Trial id <-> name mapping (joins actions to published verdicts)."""
    require_relation(source, "trial_facts")
    return _rows(
        source,
        "SELECT job_id AS job_id, trial_id AS trial_id, job_name AS job_name,"
        " trial_name AS trial_name FROM trial_facts",
    )


def verdict_counts(audits: list[Row]) -> list[Row]:
    """Aggregate audit verdicts: ``[{"verdict": ..., "n": ...}]``."""
    counts = Counter(row.get("verdict") or "unknown" for row in audits)
    return [{"verdict": verdict, "n": counts[verdict]} for verdict in sorted(counts)]


def verdict_disagreements(audits: list[Row]) -> list[Row]:
    """Tasks whose cohorts disagree on verdict (ledger-vs-audit style doubt)."""
    seen: dict[str, set[str]] = {}
    for row in audits:
        seen.setdefault(str(row.get("task_id") or "unknown"), set()).add(
            str(row.get("verdict") or "unknown")
        )
    return [
        {"task_id": task_id, "verdicts": ", ".join(sorted(verdicts))}
        for task_id, verdicts in sorted(seen.items())
        if len(verdicts) > 1
    ]


def findings_rows(source: QuerySource) -> list[Row]:
    """Raw task findings (one row per task x rule hit)."""
    require_relation(source, "task_findings")
    return _rows(source, FINDINGS_SQL)


def findings_by_rule(source: QuerySource) -> list[Row]:
    """Finding hits per rule, most common first."""
    counts = Counter(row.get("rule") or "unknown" for row in findings_rows(source))
    return [
        {"rule": rule, "n": counts[rule]}
        for rule in sorted(counts, key=lambda rule: (-counts[rule], rule))
    ]


def rules_per_task(source: QuerySource) -> dict[str, list[str]]:
    """Task id -> sorted finding rules (for the task table and detail view)."""
    grouped: dict[str, set[str]] = {}
    for row in findings_rows(source):
        task_id = row.get("task_id")
        if task_id is None:
            continue
        grouped.setdefault(str(task_id), set()).add(str(row.get("rule") or "unknown"))
    return {task_id: sorted(rules) for task_id, rules in grouped.items()}


def domain_rule_matrix(source: QuerySource) -> list[Row]:
    """Long-form domain x rule hit counts (rendering pivots to a heatmap)."""
    counts = Counter(
        (str(row.get("domain") or "unknown"), str(row.get("rule") or "unknown"))
        for row in findings_rows(source)
    )
    return [
        {"domain": domain, "rule": rule, "n": counts[(domain, rule)]}
        for domain, rule in sorted(counts)
    ]


def versions_rows(source: QuerySource) -> list[Row]:
    """Task dimension rows (grader, network, user, has_solution)."""
    require_relation(source, "task_versions")
    return _rows(source, VERSIONS_SQL)


def qualification_rows(source: QuerySource) -> list[Row]:
    """Nop/qualification runs per task (reward, setup, verifier completion)."""
    require_relation(source, "task_qualification")
    return _rows(source, QUALIFICATION_SQL)


def nop_positive_rows(source: QuerySource) -> list[Row]:
    """Qualification runs with reward > 0: grader passes with no work."""
    return [
        row
        for row in qualification_rows(source)
        if isinstance(row.get("reward"), (int, float)) and float(row["reward"]) > 0
    ]


def nop_per_task(source: QuerySource) -> list[Row]:
    """Per-task nop summary: runs, max reward, and the nop>0 flag."""
    grouped: dict[str, dict[str, Any]] = {}
    for row in qualification_rows(source):
        task_id = str(row.get("task_id") or "unknown")
        entry = grouped.setdefault(task_id, {"n_nop_runs": 0, "max_nop_reward": None})
        entry["n_nop_runs"] += 1
        reward = row.get("reward")
        if isinstance(reward, (int, float)):
            current = entry["max_nop_reward"]
            if current is None or float(reward) > float(current):
                entry["max_nop_reward"] = float(reward)
    return [
        {
            "task_id": task_id,
            "n_nop_runs": entry["n_nop_runs"],
            "max_nop_reward": entry["max_nop_reward"],
            "nop_positive": entry["max_nop_reward"] is not None
            and float(entry["max_nop_reward"]) > 0,
        }
        for task_id, entry in sorted(grouped.items())
    ]


def stability_rows(source: QuerySource) -> list[Row]:
    """Task stability probes (flaky reruns)."""
    require_relation(source, "task_stability")
    return _rows(source, STABILITY_SQL)


def exploits_rows(source: QuerySource) -> list[Row]:
    """Exploit-probe results per task version."""
    require_relation(source, "task_exploits")
    return _rows(source, EXPLOITS_SQL)


def exploit_cracks(source: QuerySource) -> list[Row]:
    """Probes that cracked the task: a decided status other than clean."""
    return [
        row
        for row in exploits_rows(source)
        if (row.get("exploit_status") or "unknown")
        not in ("none", "not_probed", "unknown")
    ]


def task_table(source: QuerySource) -> list[Row]:
    """One row per audited cohort (task version x backend x agent x model)."""
    audits = task_audit_rows(source)
    rules = rules_per_task(source)
    try:
        versions = {str(r.get("task_id")): r for r in versions_rows(source)}
    except SourceUnavailable:
        versions = {}
    try:
        nops = {r["task_id"]: r for r in nop_per_task(source)}
    except SourceUnavailable:
        nops = {}
    table: list[Row] = []
    for audit in audits:
        task_id = str(audit.get("task_id") or "unknown")
        version = versions.get(task_id, {})
        nop = nops.get(task_id, {})
        table.append(
            {
                "task_id": task_id,
                "task_name": audit.get("task_name"),
                "domain": audit.get("domain") or version.get("domain"),
                "verdict": audit.get("verdict"),
                "finding_rules": ", ".join(rules.get(task_id, [])),
                "n_finding_rules": len(rules.get(task_id, [])),
                "exploit_status": audit.get("exploit_status"),
                "max_nop_reward": nop.get("max_nop_reward"),
                "nop_positive": nop.get("nop_positive", False),
                "n_nop_runs": nop.get("n_nop_runs", 0),
                "has_solution": version.get("has_solution"),
                "qual_broken": audit.get("qual_broken"),
                "grader_kind": audit.get("grader_kind") or version.get("grader_kind"),
                "network_mode": version.get("network_mode"),
                "agent_user": version.get("agent_user"),
                "n_attempts": audit.get("n_attempts"),
                "n_scored": audit.get("n_scored"),
                "pass_rate": audit.get("pass_rate"),
                "n_pass": audit.get("n_pass"),
            }
        )
    return table


# ---------------------------------------------------------------------------
# Trace/run layer (attach views)
# ---------------------------------------------------------------------------

TRIAL_FACTS_SQL = """
SELECT job_name AS job_name, trial_name AS trial_name, task_name AS task_name,
       agent_name AS agent_name, model_name AS model_name,
       primary_reward AS primary_reward, exception_class AS exception_class
FROM trial_facts
"""

REWARD_FACTS_SQL = """
SELECT job_id AS job_id, trial_id AS trial_id,
       reward_name AS reward_name, reward_value AS reward_value
FROM reward_facts
"""

ACTIONS_SQL = """
SELECT job_id AS job_id, trial_id AS trial_id,
       action_family AS action_family, outcome AS outcome,
       exit_code AS exit_code
FROM agent_actions
"""

TRAJ_QUALITY_SQL = """
SELECT job_id AS job_id, trial_id AS trial_id, severity AS severity,
       category AS category, code AS code, message AS message,
       evaluated_at AS evaluated_at
FROM trajectory_quality_findings
"""

FEATURES_SQL = """
SELECT job_name AS job_name, trial_name AS trial_name, task_name AS task_name,
       loop_suspicion_detected AS loop_suspicion_detected,
       loop_suspicion_score AS loop_suspicion_score,
       status AS status, unavailable_reason AS unavailable_reason
FROM traj_features
"""


def trial_facts_rows(source: QuerySource) -> list[Row]:
    """Scored-trial facts with names (passes-over-time and model splits)."""
    require_relation(source, "trial_facts")
    return _rows(source, TRIAL_FACTS_SQL)


def reward_dims_rows(source: QuerySource) -> list[Row]:
    """Raw reward dims (reward / integrity / reward_gated) per trial id."""
    require_relation(source, "reward_facts")
    return _rows(source, REWARD_FACTS_SQL)


def integrity_gated_rows(source: QuerySource) -> list[Row]:
    """Trials joined to integrity dims, with names for viewer links."""
    trials = {
        (str(r.get("job_id")), str(r.get("trial_id"))): r
        for r in _rows(
            source,
            "SELECT job_id AS job_id, trial_id AS trial_id, job_name AS job_name,"
            " trial_name AS trial_name, task_name AS task_name,"
            " agent_name AS agent_name, model_name AS model_name FROM trial_facts",
        )
    }
    dims: dict[tuple[str, str], dict[str, float]] = {}
    for row in reward_dims_rows(source):
        key = (str(row.get("job_id")), str(row.get("trial_id")))
        value = row.get("reward_value")
        if isinstance(value, (int, float)):
            dims.setdefault(key, {})[str(row.get("reward_name"))] = float(value)
    joined: list[Row] = []
    for key, names in trials.items():
        dim = dims.get(key, {})
        reward = dim.get("reward")
        gated = dim.get("reward_gated")
        integrity = dim.get("integrity")
        suspicious = (
            reward is not None
            and reward > 0
            and (
                (gated is not None and gated == 0)
                or (integrity is not None and integrity < 1)
            )
        )
        joined.append(
            {
                **names,
                "reward": reward,
                "reward_gated": gated,
                "integrity": integrity,
                "suspicious_pass": bool(suspicious),
            }
        )
    return joined


def actions_rows(source: QuerySource) -> list[Row]:
    """Agent action mix per trial id (arguments stay hashed upstream)."""
    require_relation(source, "agent_actions")
    return _rows(source, ACTIONS_SQL)


def action_mix_by_class(
    actions: list[Row],
    classes: dict[tuple[str, str], str],
) -> list[Row]:
    """Count action_family x trial-class rows for the cheater-vs-clean view."""
    counts = Counter(
        (
            str(row.get("action_family") or "unknown"),
            classes.get((str(row.get("job_id")), str(row.get("trial_id"))), "unknown"),
        )
        for row in actions
    )
    return [
        {"action_family": family, "trial_class": trial_class, "n": counts[(family, trial_class)]}
        for family, trial_class in sorted(counts)
    ]


def traj_quality_rows(source: QuerySource) -> list[Row]:
    """Trajectory quality findings (timestamped: usable for the New tab)."""
    require_relation(source, "trajectory_quality_findings")
    return _rows(source, TRAJ_QUALITY_SQL)


def traj_quality_by_code(source: QuerySource) -> list[Row]:
    """Quality finding hits per code, most common first."""
    counts = Counter(str(row.get("code") or "unknown") for row in traj_quality_rows(source))
    return [
        {"code": code, "n": counts[code]}
        for code in sorted(counts, key=lambda code: (-counts[code], code))
    ]


def loop_suspicion_rows(source: QuerySource) -> list[Row]:
    """Trials flagged by the loop-suspicion screen."""
    require_relation(source, "traj_features")
    return [
        row for row in _rows(source, FEATURES_SQL) if row.get("loop_suspicion_detected")
    ]


# ---------------------------------------------------------------------------
# Trials census entry point (evallab trials, transient DuckDB)
# ---------------------------------------------------------------------------


def trials_census_rows(
    *,
    repo_root: Path,
    roots: list[Path] | None = None,
    task_names: list[str] | None = None,
    derived_root: Path | None = None,
) -> list[Row]:
    """Run-level census rows via the ``evallab trials`` entry point."""
    from evallab.storage.trials import connect_trials

    try:
        connection, _info = connect_trials(
            repo_root=repo_root,
            roots=roots,
            derived_root=derived_root,
            task_names=task_names,
            read_only=True,
        )
    except Exception as exc:
        raise SourceUnavailable(f"trials census failed: {exc}") from exc
    try:
        cursor = connection.execute(
            "SELECT job AS job, trial AS trial, task AS task,"
            " date AS date, model AS model, harness AS harness,"
            " reward AS reward, integrity AS integrity,"
            " reward_gated AS reward_gated, copy_verdict AS copy_verdict,"
            " legit AS legit, infra AS infra,"
            " source_job_dir AS source_job_dir,"
            " source_trial_dir AS source_trial_dir,"
            " projection_error AS projection_error FROM trials"
        )
        columns = [desc[0] for desc in cursor.description]
        rows = [dict(zip(columns, values, strict=True)) for values in cursor.fetchall()]
    except Exception as exc:
        raise SourceUnavailable(f"trials census query failed: {exc}") from exc
    finally:
        connection.close()
    for row in rows:
        value = row.get("date")
        row["date"] = value.isoformat() if isinstance(value, datetime) else value
    return rows


def task_dossier_summary(
    task_id: str,
    *,
    repo_root: Path,
    derived_root: Path | None = None,
) -> Row:
    """Full dossier dict via the ``evallab task`` builder (JSON-native)."""
    from evallab.task_dossier import task_dossier

    try:
        return dict(task_dossier(task_id, repo_root=repo_root, derived_root=derived_root))
    except Exception as exc:
        raise SourceUnavailable(f"task dossier failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Results-home processed runs (counted / excluded verdicts + evidence)
# ---------------------------------------------------------------------------


def results_home() -> Path:
    """Published-runs root (``EVALLAB_RESULTS_HOME`` override supported)."""
    from evallab.results_home import results_root

    return results_root()


def processed_run_scan(home: Path | None = None) -> tuple[list[Row], Row]:
    """Scan processed reports; returns ``(rows, meta)``.

    Pre-counts ``process_job/v1`` reports carry no ``counts`` block (no
    counted/excluded verdict): they still contribute reward/model/task rows
    with ``schema="legacy"`` instead of inflating the unreadable count.
    """
    root = Path(home) if home is not None else results_home()
    if not root.is_dir():
        raise SourceUnavailable(f"results home is not a directory: {root}")
    rows: list[Row] = []
    n_unreadable = 0
    n_legacy = 0
    for path in sorted(root.glob("*/*/processed/trial-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            n_unreadable += 1
            continue
        if not isinstance(payload, dict) or not payload.get("trial_name"):
            n_unreadable += 1
            continue
        job = path.parents[1].name
        trial = path.stem.removeprefix("trial-")
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
        except OSError:
            mtime = None
        base = {
            "date": path.parents[2].name,
            "job": job,
            "trial": trial,
            "task": payload.get("task_name"),
            "model": payload.get("model_name") or "unknown",
            "mtime": mtime,
            "viewer_url": trial_url(job, trial),
        }
        counts = payload.get("counts")
        if not isinstance(counts, dict):
            raw = payload.get("reward")
            n_legacy += 1
            rows.append(
                {
                    **base,
                    "schema": "legacy",
                    "verdict": None,
                    "reasons": [],
                    "reason_str": "",
                    "raw_reward": raw,
                    "passed": isinstance(raw, (int, float)) and float(raw) > 0,
                    "evidence_command": None,
                }
            )
            continue
        reasons = counts.get("reasons") or []
        evidence = counts.get("evidence") or []
        first_command: str | None = None
        for item in evidence:
            if isinstance(item, dict) and item.get("command"):
                first_command = str(item["command"])
                break
        raw = counts.get("raw_reward")
        rows.append(
            {
                **base,
                "schema": "counts",
                "verdict": counts.get("verdict"),
                "reasons": list(reasons) if isinstance(reasons, list) else [],
                "reason_str": ", ".join(str(r) for r in reasons) if reasons else "",
                "raw_reward": raw,
                "passed": isinstance(raw, (int, float)) and float(raw) > 0,
                "evidence_command": first_command,
            }
        )
    return rows, {"n_unreadable": n_unreadable, "n_legacy": n_legacy}


def runs_overview(processed: list[Row]) -> Row:
    """Runs totals: total / passed / counted / excluded + reason histogram."""
    verdicts = Counter(str(row.get("verdict") or "unknown") for row in processed)
    reasons = Counter(
        str(reason) for row in processed for reason in (row.get("reasons") or [])
    )
    excluded_reasons = Counter(
        str(reason)
        for row in processed
        if row.get("verdict") == "excluded"
        for reason in (row.get("reasons") or [])
    )
    return {
        "n_runs": len(processed),
        "n_passed": sum(1 for row in processed if row.get("passed")),
        "n_counted": sum(
            1 for row in processed if str(row.get("verdict") or "").startswith("counted")
        ),
        "n_excluded": verdicts.get("excluded", 0),
        "verdicts": dict(verdicts),
        "top_reasons": reasons.most_common(8),
        "top_exclusion_reasons": excluded_reasons.most_common(8),
    }


def excluded_or_suspicious(processed: list[Row]) -> list[Row]:
    """Excluded trials plus counted passes that carry an exclusion reason."""
    return [
        row
        for row in processed
        if row.get("verdict") == "excluded"
        or (row.get("verdict") == "counted_pass" and row.get("reasons"))
    ]


#: Reasons that mark a pass as cheating (vs infra/eligibility exclusions).
CHEAT_REASONS = frozenset({"copied_fix", "pass_tainted"})

#: Agents that run controls, not model capability.
CONTROL_AGENTS = frozenset({"nop", "oracle", "probe"})


def reason_histogram(processed: list[Row], *, only_passed: bool) -> list[Row]:
    """Named-column reason counts, most common first (never 0/1 columns)."""
    counts = Counter(
        str(reason)
        for row in processed
        if (row.get("passed") if only_passed else True)
        for reason in (row.get("reasons") or [])
    )
    return [
        {"reason": reason, "n": counts[reason]}
        for reason in sorted(counts, key=lambda reason: (-counts[reason], reason))
    ]


def task_key(name: Any) -> str:
    """Join key for a task reference: the trailing id (``a/b/id`` -> ``id``)."""
    text = str(name or "")
    return text.rsplit("/", 1)[-1] if "/" in text else text


def control_rows(trials: list[Row]) -> list[Row]:
    """Nop/oracle/probe control runs from census-style trial rows."""
    return [
        row
        for row in trials
        if str(row.get("agent_name") or "").split(":")[-1] in CONTROL_AGENTS
    ]


#: Job/trial name markers that identify control runs when the attach join
#: misses (e.g. ``har140-lnop-000450``). Tokens, never substrings: an agent
#: run merely mentioning "nop" elsewhere does not match.
CONTROL_MARKERS: tuple[tuple[str, str], ...] = (
    ("lnop", "nop"),
    ("snop", "nop"),
    ("snop2", "nop"),
    ("rnop", "nop"),
    ("r2nop", "nop"),
    ("vnop", "nop"),
    ("nop", "nop"),
    ("oracle", "oracle"),
    ("probe", "probe"),
)


def is_control_agent(name: Any) -> bool:
    """Whether an agent name is a control (nop/oracle/probe), short or pathed."""
    return str(name or "").split(":")[-1] in CONTROL_AGENTS


def marker_agent(job: Any, trial: Any) -> str | None:
    """Control agent from job/trial name tokens, or None when unmarked."""
    parts = set(re.split(r"[-_]", f"{job or ''} {trial or ''}".lower()))
    for marker, agent in CONTROL_MARKERS:
        if marker in parts:
            return agent
    return None


def read_trial_agent(home: Path, date: Any, job: Any, trial: Any) -> str | None:
    """Recorded agent name from a published trial result.json, if readable."""
    try:
        payload = json.loads(
            (Path(home) / str(date) / str(job) / str(trial) / "result.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if isinstance(payload, dict):
        info = payload.get("agent_info")
        if isinstance(info, dict) and info.get("name"):
            return str(info["name"])
    return None


def classify_run_agent(
    row: Row, name_agent: dict[str, str], *, home: Path | None = None
) -> tuple[str, str]:
    """``(agent, via)`` for one processed row, fail-closed.

    Census join first, then the recorded trial result.json, then job-name
    markers. Anything unresolvable is ``("unknown", "unknown")``: an
    unjoined run must never default into the agent bucket.
    """
    trial = str(row.get("trial") or "")
    agent = name_agent.get(trial, "")
    if agent:
        return agent, "census"
    if home is not None:
        agent = read_trial_agent(home, row.get("date"), row.get("job"), trial)
        if agent:
            return agent, "result"
    marked = marker_agent(row.get("job"), trial)
    if marked is not None:
        return marked, "marker"
    return "unknown", "unknown"


def partition_runs(processed: list[Row]) -> dict[str, list[Row]]:
    """Split rows with a resolved ``agent`` key: agent / control / unknown."""
    buckets = {"agent": [], "control": [], "unknown": []}
    for row in processed:
        agent = row.get("agent") or "unknown"
        if agent == "unknown":
            buckets["unknown"].append(row)
        elif is_control_agent(agent):
            buckets["control"].append(row)
        else:
            buckets["agent"].append(row)
    return buckets


def build_action_mix(
    actions: list[Row],
    trials: list[Row],
    processed: list[Row],
) -> list[Row]:
    """Action-family x trial-class mix over explicit identity rows.

    ``trials`` are identity rows (job_id/trial_id/job_name/trial_name);
    published job-dir names and census job names disagree, so exact
    (job, trial) pairs win and unambiguous trial-name matches fill the
    rest. Raises ``SourceUnavailable`` when nothing joins.
    """
    if not actions:
        raise SourceUnavailable("agent_actions has no rows")
    pair_class: dict[tuple[str, str], str] = {}
    trial_classes: dict[str, set[str]] = {}
    for row in processed:
        verdict = str(row.get("verdict") or "")
        if row.get("passed") and verdict == "counted_pass":
            trial_class = "counted-pass"
        elif row.get("passed") and verdict == "excluded":
            trial_class = "excluded-pass"
        elif not row.get("passed"):
            trial_class = "fail"
        else:
            continue
        pair_class[(str(row.get("job")), str(row.get("trial")))] = trial_class
        trial_classes.setdefault(str(row.get("trial")), set()).add(trial_class)
    id_class: dict[tuple[str, str], str] = {}
    for trial in trials:
        if not isinstance(trial, dict) or trial.get("job_id") is None:
            continue
        key = (str(trial.get("job_id")), str(trial.get("trial_id")))
        exact = pair_class.get((str(trial.get("job_name")), str(trial.get("trial_name"))))
        if exact is not None:
            id_class[key] = exact
            continue
        options = trial_classes.get(str(trial.get("trial_name")), set())
        if len(options) == 1:
            id_class[key] = next(iter(options))
    if not id_class:
        raise SourceUnavailable("no trial id <-> verdict join (no trial-name overlap?)")
    return action_mix_by_class(actions, id_class)


def rule_coverage(findings: list[Row], *, pool_tasks: int) -> list[Row]:
    """Per-rule hits with share of the selected pool, most common first."""
    counts = Counter(str(row.get("rule") or "unknown") for row in findings)
    covered = sorted(counts, key=lambda rule: (-counts[rule], rule))
    return [
        {
            "rule": rule,
            "n": counts[rule],
            "pct": (100.0 * counts[rule] / pool_tasks) if pool_tasks else 0.0,
        }
        for rule in covered
    ]


def passes_over_time(processed: list[Row]) -> list[Row]:
    """Per-day counted vs excluded passes (day from the published job date)."""
    days: dict[str, dict[str, int]] = {}
    for row in processed:
        if not row.get("passed"):
            continue
        day = str(row.get("date") or "unknown")
        entry = days.setdefault(day, {"counted": 0, "excluded": 0})
        if str(row.get("verdict") or "").startswith("counted"):
            entry["counted"] += 1
        elif row.get("verdict") == "excluded":
            entry["excluded"] += 1
    return [{"date": day, **counts} for day, counts in sorted(days.items())]


# ---------------------------------------------------------------------------
# Task ledger (keep / fix / discard)
# ---------------------------------------------------------------------------

LEDGER_RELATIVE = Path("research/experiments/python-task-ledger/ledger.csv")


def ledger_rows(repo_root: Path) -> list[Row]:
    """Python task ledger rows (task_id, verdict keep/fix/discard)."""
    path = Path(repo_root) / LEDGER_RELATIVE
    if not path.is_file():
        raise SourceUnavailable(f"ledger not found: {LEDGER_RELATIVE}")
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise SourceUnavailable(f"ledger unreadable: {exc}") from exc


def ledger_counts(repo_root: Path) -> list[Row]:
    """Ledger verdict histogram (keep / fix / discard)."""
    counts = Counter(
        (row.get("verdict") or "unknown").strip() or "unknown"
        for row in ledger_rows(repo_root)
    )
    return [{"verdict": verdict, "n": counts[verdict]} for verdict in sorted(counts)]


# ---------------------------------------------------------------------------
# Outside-test stored outputs (HAR-191 / HAR-197 / HAR-198, best effort)
# ---------------------------------------------------------------------------


def newest_stored_output(home: Path, names: tuple[str, ...]) -> Path | None:
    """Newest stored file with one of ``names`` under the results home."""
    candidates: list[Path] = []
    for name in names:
        candidates.extend(home.glob(f"**/{name}"))
    dated = [p for p in candidates if p.is_file()]
    if not dated:
        return None
    return max(dated, key=lambda p: p.stat().st_mtime)


def oracle_labels(home: Path | None = None) -> list[Row]:
    """HAR-191 oracle sweep labels (task_id, label) from stored outputs."""
    root = Path(home) if home is not None else results_home()
    found = newest_stored_output(root, ("oracle_sweep.csv",))
    if found is None:
        raise SourceUnavailable("no stored oracle sweep output (oracle labels not measured)")
    try:
        with found.open(encoding="utf-8", newline="") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise SourceUnavailable(f"oracle sweep unreadable: {exc}") from exc
    for row in rows:
        row["source"] = str(found)
    return rows


def oracle_label_counts(home: Path | None = None) -> list[Row]:
    """Histogram over oracle sweep labels."""
    counts = Counter(str(row.get("label") or "unknown") for row in oracle_labels(home))
    return [{"label": label, "n": counts[label]} for label in sorted(counts)]


def heldout_regrade_rows(home: Path | None = None) -> list[Row]:
    """HAR-197 held-out regrade disagreements from stored outputs."""
    root = Path(home) if home is not None else results_home()
    found = newest_stored_output(root, ("heldout_regrade.csv", "heldout-regrade.csv"))
    if found is None:
        raise SourceUnavailable("no stored held-out regrade output (regrade not measured)")
    try:
        with found.open(encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise SourceUnavailable(f"held-out regrade unreadable: {exc}") from exc


def detector_score_rows(home: Path | None = None) -> list[Row]:
    """HAR-198 detector sealed-corpus scores from stored outputs."""
    root = Path(home) if home is not None else results_home()
    found = newest_stored_output(root, ("detector_scores.csv", "detector-scores.csv"))
    if found is None:
        raise SourceUnavailable("no stored detector scores (sealed corpus not scored)")
    try:
        with found.open(encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise SourceUnavailable(f"detector scores unreadable: {exc}") from exc


# ---------------------------------------------------------------------------
# Environment roots
# ---------------------------------------------------------------------------


def resolve_derived(repo_root: Path) -> Path | None:
    """Explicit derived Parquet root from the repo's env var, if set."""
    configured = os.environ.get("EVALLAB_DERIVED_ROOT")
    if configured:
        return Path(configured).expanduser()
    return None


def open_source(repo_root: Path, explicit_derived: Path | None = None):
    """Attach surface wrapped for dashboard queries (caller closes it)."""
    from dashboard.queries import AttachSource

    try:
        return AttachSource(repo_root=repo_root, explicit_derived=explicit_derived)
    except Exception as exc:
        raise SourceUnavailable(f"attach failed: {exc}") from exc


def zone_notes(source: object) -> list[Row]:
    """Attach zone availability for the page header."""
    zones = getattr(source, "zones", None)
    if not isinstance(zones, dict):
        return []
    notes: list[Row] = []
    for name in ("z2", "z3", "z4"):
        zone = zones.get(name)
        if zone is None:
            continue
        notes.append(
            {
                "zone": name,
                "attached": bool(zone.attached),
                "reason": None if zone.attached else zone.reason,
            }
        )
    return notes
