"""Focused behavior tests for pinned HF intake and the MiMo task catalog."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from evallab.task_catalog import (
    task_audit_sql,
    task_outcomes_sql,
)

# ---------------------------------------------------------------- intake


# ---------------------------------------------------------------- catalog rows


def _catalog_fixture(tmp_path: Path) -> Path:
    from evallab.task_catalog import (
        TASK_FINDINGS_SCHEMA,
        TASK_LINEAGE_SCHEMA,
        TASK_SOURCES_SCHEMA,
        TASK_VERSIONS_SCHEMA,
    )
    derived = tmp_path / "derived"
    catalog = derived / "external/task_catalog"
    catalog.mkdir(parents=True)
    versions = [
        {"task_version_digest": "sha256:" + str(index) * 64,
         "harbor_digest": "sha256:" + str(index + 2) * 64, "task_id": name,
         "task_name": name, "domain": "fake", "split_group": "fake:" + name,
         "grader_kind": "script", "grader_cost": "free"}
        for index, name in enumerate(("alpha-task", "beta-task"), start=1)
    ]
    for name, rows, schema in (
        ("task_sources", [], TASK_SOURCES_SCHEMA), ("task_versions", versions, TASK_VERSIONS_SCHEMA),
        ("task_findings", [], TASK_FINDINGS_SCHEMA), ("task_lineage", [], TASK_LINEAGE_SCHEMA),
    ):
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), catalog / f"{name}.parquet")
    return derived


# ---------------------------------------------------------------- lint rules


# ---------------------------------------------------------------- verdicts


def _trial_table(rows: list[dict]) -> pa.Table:
    return pa.Table.from_pylist(rows)


def test_outcomes_sql_join_counts_infra_not_fail() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE v AS SELECT * FROM (VALUES "
                 "('sha256:pkg1', 'sha256:hb1', 't1', 'terminal'),"
                 "('sha256:pkg2', 'sha256:hb2', 't2', 'terminal'),"
                 "('sha256:pkg3', 'sha256:hb3', 't3', 'terminal')"
                 ") AS t(task_version_digest, harbor_digest, task_id, domain)")
    trials = _trial_table([
        {"trial_id": "n1", "task_digest": "sha256:hb1", "agent_name": "nop",
         "model_name": "", "environment_type": "docker",
         "primary_reward": 0.0, "exception_class": None},
        {"trial_id": "e1", "task_digest": "sha256:hb1", "agent_name": "agentx",
         "model_name": "m", "environment_type": "docker",
         "primary_reward": None, "exception_class": "VerifierError"},
        {"trial_id": "scored-timeout", "task_digest": "sha256:hb1", "agent_name": "agentx",
         "model_name": "m", "environment_type": "docker",
         "primary_reward": 1.0, "exception_class": "AgentTimeoutError"},
        {"trial_id": "m1", "task_digest": "sha256:hb2", "agent_name": "agentx",
         "model_name": "m", "environment_type": "daytona",
         "primary_reward": -1.0, "exception_class": None},
    ])
    conn.register("trials", trials)
    rows = conn.execute(task_outcomes_sql(trials="trials", versions="v")).fetchall()
    by_key = {(row[2], row[4], row[5]): row for row in rows}
    nop = by_key[("t1", "docker", "nop")]
    assert (nop[7], nop[8], nop[9], nop[10], nop[12], nop[15]) == (1, 1, 0, 0, 0.0, "always_fail")
    ctl = by_key[("t1", "docker", "agentx")]
    assert (ctl[7], ctl[8], ctl[9], ctl[10], ctl[15]) == (2, 1, 1, 1, "always_pass")
    neg = by_key[("t2", "daytona", "agentx")]
    assert (neg[8], neg[9], neg[15]) == (0, 1, "infra_only")
    bare = by_key[("t3", "unknown", "")]
    assert (bare[7], bare[8], bare[15]) == (0, 0, "untested")
    assert bare[11] is None and bare[12] is None and bare[13] is None and bare[14] is None
    conn.close()


def test_outcomes_sql_keeps_backends_apart() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE v AS SELECT * FROM (VALUES "
                 "('sha256:pkg1', 'sha256:hb1', 't1', 'terminal')"
                 ") AS t(task_version_digest, harbor_digest, task_id, domain)")
    trials = _trial_table([
        {"trial_id": "d1", "task_digest": "sha256:hb1", "agent_name": "nop",
         "model_name": "", "environment_type": "docker",
         "primary_reward": 0.0, "exception_class": None},
        {"trial_id": "c1", "task_digest": "sha256:hb1", "agent_name": "nop",
         "model_name": "", "environment_type": "daytona",
         "primary_reward": 0.0, "exception_class": None},
    ])
    conn.register("trials", trials)
    rows = conn.execute(task_outcomes_sql(trials="trials", versions="v")).fetchall()
    assert {(row[4], row[7]) for row in rows} == {("docker", 1), ("daytona", 1)}
    conn.close()


def test_audit_view_eligibility_and_missing_tables() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE v AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'sha256:h1', 't1', 'n1', 'terminal', 'terminal:t1', 'script', 'free'),"
                 "('sha256:p2', 'sha256:h2', 't2', 'n2', 'terminal', 'terminal:t2', 'script', 'free')"
                 ") AS t(task_version_digest, harbor_digest, task_id, task_name,"
                 " domain, split_group, grader_kind, grader_cost)")
    conn.execute("CREATE TABLE o AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'docker', 'a', 'm', 2, 2, 0, 0.5, 0.1, 0.9, 'learnable'),"
                 "('sha256:p2', 'docker', 'a', 'm', 1, 1, 0, 0.0, 0.0, 0.8, 'always_fail')"
                 ") AS t(task_version_digest, backend, agent_name, model_name, n_attempts,"
                 " n_scored, n_infra, pass_rate, pass_rate_lo, pass_rate_hi, verdict)")
    conn.execute("CREATE TABLE s AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'stable', 3, 'runs/e1'),"
                 "('sha256:p2', 'flipped', 2, 'runs/e2')"
                 ") AS t(task_version_digest, verdict, n_runs, evidence_path)")
    conn.execute("CREATE TABLE e AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'none', 'runs/x1')"
                 ") AS t(task_version_digest, exploit_status, evidence_path)")
    rows = conn.execute(task_audit_sql(
        outcomes="o", versions="v", stability="s", exploits="e",
        has_findings=False)).fetchall()
    by_id = {row[2]: row for row in rows}
    assert by_id["t1"][-2] is True
    assert by_id["t1"][-1] is None
    assert by_id["t2"][-2] is False
    assert "not learnable" in str(by_id["t2"][-1])
    rows = conn.execute(task_audit_sql(
        outcomes="o", versions="v", has_stability=False, has_exploits=False,
        has_findings=False)).fetchall()
    by_id = {row[2]: row for row in rows}
    assert by_id["t1"][-2] is False
    assert by_id["t1"][-1] == "no stability evidence"
    conn.close()

def test_audit_view_broken_backend_ineligible_without_fanout() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE v AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'sha256:h1', 't1', 'n1', 'terminal', 'terminal:t1', 'script', 'free')"
                 ") AS t(task_version_digest, harbor_digest, task_id, task_name,"
                 " domain, split_group, grader_kind, grader_cost)")
    conn.execute("CREATE TABLE o AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'docker', 'a', 'm', 2, 2, 0, 0.5, 0.1, 0.9, 'learnable'),"
                 "('sha256:p1', 'daytona', 'a', 'm', 2, 2, 0, 0.5, 0.1, 0.9, 'learnable')"
                 ") AS t(task_version_digest, backend, agent_name, model_name, n_attempts,"
                 " n_scored, n_infra, pass_rate, pass_rate_lo, pass_rate_hi, verdict)")
    conn.execute("CREATE TABLE s AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'stable', 3, 'runs/e1')"
                 ") AS t(task_version_digest, verdict, n_runs, evidence_path)")
    conn.execute("CREATE TABLE e AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'none', 'runs/x1')"
                 ") AS t(task_version_digest, exploit_status, evidence_path)")
    conn.execute("CREATE TABLE q AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'docker', 'broken', '2026-09-27T00:00:00Z', 'j1', 't1'),"
                 "('sha256:p1', 'docker', 'ok', '2026-09-28T00:00:00Z', 'j2', 't2'),"
                 "('sha256:p1', 'daytona', 'broken', '2026-09-28T00:00:00Z', 'j3', 't3'),"
                 "('sha256:p1', 'daytona', 'inconclusive', '2026-09-26T00:00:00Z', 'j0', 't0')"
                 ") AS t(task_version_digest, backend, status, finished_at,"
                 " job_name, trial_name)")
    rows = conn.execute(task_audit_sql(
        outcomes="o", versions="v", stability="s", exploits="e",
        qualification="q", has_qualification=True, has_findings=False)).fetchall()
    assert len(rows) == 2  # one audit row per backend, never fanned out
    columns = [desc[0] for desc in conn.description]
    by_backend = {row[columns.index("backend")]: dict(zip(columns, row, strict=True)) for row in rows}
    assert by_backend["docker"]["train_eligible"] is True
    assert by_backend["docker"]["train_ineligible_reason"] is None
    assert by_backend["daytona"]["train_eligible"] is False
    assert by_backend["daytona"]["train_ineligible_reason"] == "broken on daytona"
    conn.close()


# ---------------------------------------------------------------- export + CLI


# ---------------------------------------------------------------- curated findings


def test_audit_error_finding_ineligible_with_precedence() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE v AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'sha256:h1', 't1', 'n1', 'terminal', 'terminal:t1', 'script', 'free'),"
                 "('sha256:p2', 'sha256:h2', 't2', 'n2', 'terminal', 'terminal:t2', 'script', 'free')"
                 ") AS t(task_version_digest, harbor_digest, task_id, task_name,"
                 " domain, split_group, grader_kind, grader_cost)")
    conn.execute("CREATE TABLE o AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'docker', 'a', 'm', 2, 2, 0, 0.5, 0.1, 0.9, 'learnable'),"
                 "('sha256:p2', 'docker', 'a', 'm', 2, 2, 0, 0.5, 0.1, 0.9, 'learnable')"
                 ") AS t(task_version_digest, backend, agent_name, model_name, n_attempts,"
                 " n_scored, n_infra, pass_rate, pass_rate_lo, pass_rate_hi, verdict)")
    conn.execute("CREATE TABLE s AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'stable', 3, 'runs/e1'),"
                 "('sha256:p2', 'stable', 3, 'runs/e2')"
                 ") AS t(task_version_digest, verdict, n_runs, evidence_path)")
    conn.execute("CREATE TABLE e AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'confirmed', 'runs/x1'),"
                 "('sha256:p2', 'none', 'runs/x2')"
                 ") AS t(task_version_digest, exploit_status, evidence_path)")
    conn.execute("CREATE TABLE f AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'grader-broken', 'error', 'collection broken'),"
                 "('sha256:p2', 'mimo-no-oracle', 'warning', 'no oracle')"
                 ") AS t(task_version_digest, rule, severity, message)")
    rows = conn.execute(task_audit_sql(
        outcomes="o", versions="v", stability="s", exploits="e",
        findings="f")).fetchall()
    columns = [desc[0] for desc in conn.description]
    by_id = {row[2]: dict(zip(columns, row, strict=True)) for row in rows}
    # The hard defect wins over the confirmed exploit: finding reason first.
    assert by_id["t1"]["train_eligible"] is False
    assert by_id["t1"]["train_ineligible_reason"] == "finding: grader-broken"
    # Warnings change nothing: still eligible.
    assert by_id["t2"]["train_eligible"] is True
    assert by_id["t2"]["train_ineligible_reason"] is None
    conn.close()


def test_audit_finding_beats_qualification_broken_reason() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE v AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'sha256:h1', 't1', 'n1', 'terminal', 'terminal:t1', 'script', 'free')"
                 ") AS t(task_version_digest, harbor_digest, task_id, task_name,"
                 " domain, split_group, grader_kind, grader_cost)")
    conn.execute("CREATE TABLE o AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'daytona', 'a', 'm', 2, 2, 0, 0.5, 0.1, 0.9, 'learnable')"
                 ") AS t(task_version_digest, backend, agent_name, model_name, n_attempts,"
                 " n_scored, n_infra, pass_rate, pass_rate_lo, pass_rate_hi, verdict)")
    conn.execute("CREATE TABLE s AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'stable', 3, 'runs/e1')"
                 ") AS t(task_version_digest, verdict, n_runs, evidence_path)")
    conn.execute("CREATE TABLE e AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'none', 'runs/x1')"
                 ") AS t(task_version_digest, exploit_status, evidence_path)")
    conn.execute("CREATE TABLE q AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'daytona', 'broken', '2026-09-28T00:00:00Z', 'j1', 't1')"
                 ") AS t(task_version_digest, backend, status, finished_at,"
                 " job_name, trial_name)")
    conn.execute("CREATE TABLE f AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'grader-broken', 'error', 'collection broken')"
                 ") AS t(task_version_digest, rule, severity, message)")
    rows = conn.execute(task_audit_sql(
        outcomes="o", versions="v", stability="s", exploits="e",
        qualification="q", has_qualification=True, findings="f")).fetchall()
    columns = [desc[0] for desc in conn.description]
    record = dict(zip(columns, rows[0], strict=True))
    assert record["train_eligible"] is False
    assert record["train_ineligible_reason"] == "finding: grader-broken"
    conn.close()
