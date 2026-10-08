"""Focused behavioural tests for dashboard.integrity query functions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from dashboard import integrity as iq
from dashboard.integrity import SourceUnavailable


class LocalSource:
    """Minimal QuerySource over an in-memory DuckDB with explicit tables."""

    def __init__(self, tables: dict[str, tuple[str, list[tuple[Any, ...]]]]) -> None:
        self.con = duckdb.connect(":memory:")
        self.names = set(tables)
        for name, (columns, rows) in tables.items():
            self.con.execute(f"CREATE TABLE {name} ({columns})")
            if rows:
                placeholders = ", ".join(["?"] * len(rows[0]))
                self.con.executemany(f"INSERT INTO {name} VALUES ({placeholders})", rows)

    def query(self, statement: str, parameters: Any = ()) -> list[dict[str, Any]]:
        cursor = self.con.execute(statement, list(parameters))
        columns = [desc[0] for desc in cursor.description]
        out: list[dict[str, Any]] = []
        for values in cursor.fetchall():
            row: dict[str, Any] = {}
            for key, value in zip(columns, values, strict=True):
                row[key] = list(value) if isinstance(value, list) else value
            out.append(row)
        return out

    def relation_exists(self, name: str) -> bool:
        return name in self.names

    def close(self) -> None:
        self.con.close()


AUDIT_COLUMNS = (
    "task_version_digest VARCHAR, task_id VARCHAR, task_name VARCHAR, domain VARCHAR,"
    " split_group VARCHAR, grader_kind VARCHAR, backend VARCHAR, agent_name VARCHAR,"
    " model_name VARCHAR, n_attempts INTEGER, n_scored INTEGER, n_infra INTEGER,"
    " pass_rate DOUBLE, verdict VARCHAR, stability VARCHAR, exploit_status VARCHAR,"
    " qual_broken INTEGER, finding_rule VARCHAR"
)
OUTCOME_COLUMNS = (
    "task_version_digest VARCHAR, backend VARCHAR, agent_name VARCHAR,"
    " model_name VARCHAR, n_pass INTEGER, mean_reward DOUBLE"
)


def _audit_source(**extra: Any) -> LocalSource:
    tables: dict[str, tuple[str, list[tuple[Any, ...]]]] = {
        "v_task_audit": (
            AUDIT_COLUMNS,
            [
                ("d1", "task-a", "ns/task-a", "code", "train", "script", "daytona",
                 "nop", "", 2, 2, 0, 0.0, "always_fail", None, None, 0, None),
                ("d2", "task-b", "ns/task-b", "webdev", "train", "llm_judge", "daytona",
                 "nop", "", 2, 2, 0, 1.0, "always_pass", None, "none", 0, None),
                ("d3", "task-c", "ns/task-c", "code", "train", "script", "daytona",
                 "nop", "", 0, 0, 2, None, "infra_only", None, None, 1, "grader-broken"),
            ],
        ),
        "v_task_outcomes": (
            OUTCOME_COLUMNS,
            [("d1", "daytona", "nop", "", 0, 0.0), ("d2", "daytona", "nop", "", 2, 1.0)],
        ),
    }
    tables.update(extra)
    return LocalSource(tables)


def test_verdict_counts_aggregate_without_zero_fill() -> None:
    source = _audit_source()
    try:
        counts = {row["verdict"]: row["n"] for row in iq.verdict_counts(iq.task_audit_rows(source))}
    finally:
        source.close()
    assert counts == {"always_fail": 1, "always_pass": 1, "infra_only": 1}


def test_task_table_joins_passes_and_versions() -> None:
    source = _audit_source(
        task_findings=(
            "task_id VARCHAR, domain VARCHAR, rule VARCHAR, severity VARCHAR, message VARCHAR",
            [("task-a", "code", "mimo-verify-network-dep", "warning", "net"),
             ("task-a", "code", "mimo-answer-leak", "error", "leak")],
        ),
        task_versions=(
            "task_id VARCHAR, task_name VARCHAR, domain VARCHAR, category VARCHAR,"
            " grader_kind VARCHAR, grader_cost VARCHAR, has_solution BOOLEAN,"
            " network_mode VARCHAR, agent_user VARCHAR",
            [("task-a", "ns/task-a", "code", "fmt", "script", "free", False, "public", "root")],
        ),
        task_qualification=(
            "task_id VARCHAR, job_name VARCHAR, trial_name VARCHAR, reward DOUBLE,"
            " setup_ok BOOLEAN, verifier_completed BOOLEAN, grader_error VARCHAR,"
            " infra_error_class VARCHAR",
            [("task-a", "j", "t", 0.0, True, True, None, None)],
        ),
    )
    try:
        table = {row["task_id"]: row for row in iq.task_table(source)}
    finally:
        source.close()
    assert table["task-a"]["n_pass"] == 0
    assert table["task-a"]["finding_rules"] == "mimo-answer-leak, mimo-verify-network-dep"
    assert table["task-a"]["has_solution"] is False
    assert table["task-a"]["max_nop_reward"] == 0.0
    assert table["task-b"]["finding_rules"] == ""


def test_findings_by_rule_orders_most_common_first() -> None:
    source = LocalSource(
        {
            "task_findings": (
                "task_id VARCHAR, domain VARCHAR, rule VARCHAR, severity VARCHAR, message VARCHAR",
                [
                    ("a", "code", "rare-rule", "warning", ""),
                    ("b", "code", "common-rule", "warning", ""),
                    ("c", "webdev", "common-rule", "warning", ""),
                ],
            )
        }
    )
    try:
        assert iq.findings_by_rule(source) == [
            {"rule": "common-rule", "n": 2},
            {"rule": "rare-rule", "n": 1},
        ]
    finally:
        source.close()


def test_nop_positive_rule_is_strictly_greater_than_zero() -> None:
    source = LocalSource(
        {
            "task_qualification": (
                "task_id VARCHAR, job_name VARCHAR, trial_name VARCHAR, reward DOUBLE,"
                " setup_ok BOOLEAN, verifier_completed BOOLEAN, grader_error VARCHAR,"
                " infra_error_class VARCHAR",
                [
                    ("a", "j1", "t1", 0.0, True, True, None, None),
                    ("a", "j2", "t2", 0.5, True, True, None, None),
                    ("b", "j3", "t3", None, True, False, "boom", None),
                ],
            )
        }
    )
    try:
        positive = iq.nop_positive_rows(source)
        per_task = {row["task_id"]: row for row in iq.nop_per_task(source)}
    finally:
        source.close()
    assert [row["trial_name"] for row in positive] == ["t2"]
    assert per_task["a"] == {
        "task_id": "a", "n_nop_runs": 2, "max_nop_reward": 0.5, "nop_positive": True,
    }
    assert per_task["b"]["nop_positive"] is False


def test_missing_table_degrades_honestly() -> None:
    source = LocalSource({})
    try:
        with pytest.raises(SourceUnavailable, match="not available: task_findings"):
            iq.findings_rows(source)
        with pytest.raises(SourceUnavailable, match="not available: v_task_audit"):
            iq.task_audit_rows(source)
    finally:
        source.close()


def test_exploit_cracks_exclude_clean_and_unprobed() -> None:
    source = LocalSource(
        {
            "task_exploits": (
                "task_version_digest VARCHAR, job_name VARCHAR, trial_name VARCHAR,"
                " probe_config VARCHAR, reward DOUBLE, exploit_status VARCHAR,"
                " method VARCHAR, evidence_path VARCHAR, produced_at VARCHAR",
                [
                    ("d1", "j", "t", "redteam-v1", 0.0, "none", "m", "p", None),
                    ("d2", "j", "t", "redteam-v1", None, "not_probed", "m", "p", None),
                    ("d3", "j", "t", "redteam-v1", 1.0, "cracked", "m", "p", None),
                ],
            )
        }
    )
    try:
        cracks = iq.exploit_cracks(source)
    finally:
        source.close()
    assert [row["task_version_digest"] for row in cracks] == ["d3"]


def test_runs_overview_counts_verdicts_and_reasons() -> None:
    processed = [
        {"verdict": "counted_fail", "reasons": [], "passed": False},
        {"verdict": "counted_pass", "reasons": [], "passed": True},
        {"verdict": "excluded", "reasons": ["infra"], "passed": False},
        {"verdict": "excluded", "reasons": ["copied_fix"], "passed": True},
        {"verdict": None, "reasons": [], "passed": False},
    ]
    overview = iq.runs_overview(processed)
    assert overview["n_runs"] == 5
    assert overview["n_passed"] == 2
    assert overview["n_counted"] == 2
    assert overview["n_excluded"] == 2
    assert dict(overview["top_exclusion_reasons"]) == {"infra": 1, "copied_fix": 1}
    assert [row["verdict"] for row in iq.excluded_or_suspicious(processed)] == [
        "excluded", "excluded",
    ]


def test_action_mix_buckets_unknown_trials() -> None:
    actions = [
        {"job_id": "j1", "trial_id": "t1", "action_family": "execute"},
        {"job_id": "j9", "trial_id": "t9", "action_family": "edit"},
    ]
    mix = iq.action_mix_by_class(actions, {("j1", "t1"): "counted-pass"})
    assert {"action_family": "execute", "trial_class": "counted-pass", "n": 1} in mix
    assert {"action_family": "edit", "trial_class": "unknown", "n": 1} in mix


def test_ledger_counts_keep_fix_discard(tmp_path: Path) -> None:
    ledger = tmp_path / "research/experiments/python-task-ledger/ledger.csv"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "task_id,verdict\ntask-a,keep\ntask-b,fix\ntask-c,discard\ntask-d,keep\n",
        encoding="utf-8",
    )
    assert iq.ledger_counts(tmp_path) == [
        {"verdict": "discard", "n": 1},
        {"verdict": "fix", "n": 1},
        {"verdict": "keep", "n": 2},
    ]
    with pytest.raises(SourceUnavailable, match="ledger not found"):
        iq.ledger_counts(tmp_path / "missing")


def test_processed_scan_skips_unreadable_and_links_viewer(tmp_path: Path) -> None:
    processed = tmp_path / "2026-10-07" / "HAR-1-job" / "processed"
    processed.mkdir(parents=True)
    good = {
        "task_name": "ns/task-a",
        "trial_name": "job__abc",
        "counts": {
            "verdict": "excluded",
            "reasons": ["copied_fix"],
            "raw_reward": 1.0,
            "evidence": [{"command": "grep -r answer /testbed"}],
        },
    }
    (processed / "trial-job__abc.json").write_text(json.dumps(good), encoding="utf-8")
    legacy = {"schema": "process_job/v1", "task_name": "ns/task-old",
              "trial_name": "job__old", "reward": 0.0, "model_name": "m1"}
    (processed / "trial-job__old.json").write_text(json.dumps(legacy), encoding="utf-8")
    (processed / "trial-broken.json").write_text("{not json", encoding="utf-8")
    rows, meta = iq.processed_run_scan(tmp_path)
    assert meta == {"n_unreadable": 1, "n_legacy": 1}
    assert len(rows) == 2
    row = rows[0]
    assert row["job"] == "HAR-1-job"
    assert row["passed"] is True
    assert row["schema"] == "counts"
    assert row["model"] == "unknown"
    assert row["evidence_command"] == "grep -r answer /testbed"
    assert row["viewer_url"] == (
        "http://127.0.0.1:8100/jobs/HAR-1-job/trials/job__abc"
    )
    old = rows[1]
    assert old["schema"] == "legacy"
    assert old["verdict"] is None
    assert old["model"] == "m1"


def test_reason_histogram_uses_named_columns() -> None:
    processed = [
        {"verdict": "excluded", "reasons": ["infra", "copied_fix"], "passed": True},
        {"verdict": "excluded", "reasons": ["infra"], "passed": False},
    ]
    assert iq.reason_histogram(processed, only_passed=True) == [
        {"reason": "copied_fix", "n": 1},
        {"reason": "infra", "n": 1},
    ]
    assert iq.reason_histogram(processed, only_passed=False)[0] == {
        "reason": "infra", "n": 2,
    }


def test_task_key_and_control_rows() -> None:
    assert iq.task_key("mimo-v2.6-rl/format-code-task-000003") == "format-code-task-000003"
    assert iq.task_key("arvo_10055") == "arvo_10055"
    trials = [
        {"agent_name": "nop", "trial_name": "a"},
        {"agent_name": "evallab.module:oracle", "trial_name": "b"},
        {"agent_name": "codex", "trial_name": "c"},
    ]
    assert [r["trial_name"] for r in iq.control_rows(trials)] == ["a", "b"]


def test_rule_coverage_splits_universal_rules() -> None:
    findings = [{"rule": "all-rule"}, {"rule": "all-rule"}, {"rule": "half-rule"}]
    coverage = iq.rule_coverage(findings, pool_tasks=2)
    assert coverage[0] == {"rule": "all-rule", "n": 2, "pct": 100.0}
    assert coverage[1] == {"rule": "half-rule", "n": 1, "pct": 50.0}


def test_outside_test_labels_degrade_when_unmeasured(tmp_path: Path) -> None:
    with pytest.raises(SourceUnavailable, match="oracle.*not measured"):
        iq.oracle_labels(tmp_path)
    with pytest.raises(SourceUnavailable, match="regrade not measured"):
        iq.heldout_regrade_rows(tmp_path)
    with pytest.raises(SourceUnavailable, match="sealed corpus not scored"):
        iq.detector_score_rows(tmp_path)


def test_production_statements_are_select_only() -> None:
    statements = [
        value
        for key, value in vars(iq).items()
        if key.endswith("_SQL") and isinstance(value, str)
    ]
    assert statements, "integrity queries must declare their SQL"
    for statement in statements:
        lowered = statement.strip().lower()
        assert lowered.startswith("select") or lowered.startswith("with")
        for forbidden in ("insert", "update", "delete", "drop", "alter", "copy"):
            assert forbidden not in lowered


def test_audit_join_matches_full_cohort_grain_without_fanout() -> None:
    """A duplicate digest across backends must not multiply audit rows."""
    audit_row = (
        "task_version_digest VARCHAR, task_id VARCHAR, task_name VARCHAR, domain VARCHAR,"
        " split_group VARCHAR, grader_kind VARCHAR, backend VARCHAR, agent_name VARCHAR,"
        " model_name VARCHAR, n_attempts INTEGER, n_scored INTEGER, n_infra INTEGER,"
        " pass_rate DOUBLE, verdict VARCHAR, stability VARCHAR, exploit_status VARCHAR,"
        " qual_broken INTEGER, finding_rule VARCHAR"
    )
    source = LocalSource(
        {
            "v_task_audit": (
                audit_row,
                [
                    ("d", "task-a", "ns/a", "code", "train", "script", "daytona",
                     "nop", "", 2, 2, 0, 0.0, "always_fail", None, None, 0, None),
                    ("d", "task-a", "ns/a", "code", "train", "script", "docker",
                     "nop", "", 3, 3, 0, 0.0, "always_fail", None, None, 0, None),
                ],
            ),
            "v_task_outcomes": (
                OUTCOME_COLUMNS,
                [("d", "daytona", "nop", "", 0, 0.0), ("d", "docker", "nop", "", 1, 0.2)],
            ),
        }
    )
    try:
        rows = iq.task_audit_rows(source)
    finally:
        source.close()
    assert len(rows) == 2
    assert {(row["backend"], row["n_pass"]) for row in rows} == {
        ("daytona", 0), ("docker", 1),
    }


def test_verdict_disagreements_surface_cohort_conflict() -> None:
    audits = [
        {"task_id": "task-a", "verdict": "always_fail"},
        {"task_id": "task-a", "verdict": "infra_only"},
        {"task_id": "task-b", "verdict": "always_pass"},
    ]
    assert iq.verdict_disagreements(audits) == [
        {"task_id": "task-a", "verdicts": "always_fail, infra_only"}
    ]


def test_unjoined_nop_control_classified_by_job_marker() -> None:
    """A published nop run missing from the attach join stays a control."""
    row = {"job": "HAR-140-har140-lnop-000450", "trial": "har140-lnop-000450__NABvpoH",
           "verdict": "counted_pass", "reasons": [], "passed": True}
    agent, via = iq.classify_run_agent(row, {})
    assert (agent, via) == ("nop", "marker")
    resolved = dict(row, agent=agent)
    buckets = iq.partition_runs([resolved])
    assert buckets["control"] == [resolved]
    assert buckets["agent"] == []


def test_unjoined_run_reads_recorded_result_agent(tmp_path: Path) -> None:
    """A join miss with a readable result.json uses its recorded agent."""
    trial_dir = tmp_path / "2026-10-01" / "HAR-9-agent-job" / "agent-job__xyz"
    trial_dir.mkdir(parents=True)
    (trial_dir / "result.json").write_text(
        json.dumps({"agent_info": {"name": "codex"}}), encoding="utf-8"
    )
    row = {"date": "2026-10-01", "job": "HAR-9-agent-job", "trial": "agent-job__xyz"}
    assert iq.classify_run_agent(row, {}, home=tmp_path) == ("codex", "result")


def test_unresolvable_run_is_unknown_not_agent() -> None:
    row = {"job": "HAR-9-some-agent-job", "trial": "some-agent-job__xyz"}
    assert iq.classify_run_agent(row, {}) == ("unknown", "unknown")
    assert iq.partition_runs([dict(row, agent="unknown")])["unknown"] != []
    # Token match only: "canonical" must not read as a nop control.
    assert iq.marker_agent("HAR-9-canonical-job", "canonical-job__xyz") is None
    assert iq.is_control_agent("evallab.module:oracle") is True
    assert iq.is_control_agent("codex") is False


def test_build_action_mix_scopes_to_given_rows() -> None:
    actions = [
        {"job_id": "j1", "trial_id": "t1", "action_family": "execute"},
        {"job_id": "j2", "trial_id": "t2", "action_family": "edit"},
    ]
    identities = [
        {"job_id": "j1", "trial_id": "t1", "job_name": "job", "trial_name": "job__a"},
    ]
    processed = [
        {"job": "job", "trial": "job__a", "verdict": "counted_pass", "passed": True},
    ]
    mix = iq.build_action_mix(actions, identities, processed)
    assert {"action_family": "execute", "trial_class": "counted-pass", "n": 1} in mix
    with pytest.raises(SourceUnavailable, match="no trial id"):
        iq.build_action_mix(actions, [], processed)
