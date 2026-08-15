from __future__ import annotations

import json
from pathlib import Path

import duckdb

from evallab.facts import extract_job_facts, extract_trial_fact, rebuild_from_raw
from evallab.results import load_job, load_jobs

from .test_atif import _make_job

ROOT = Path(__file__).resolve().parents[3]


def test_existing_oracle_and_nop_controls_extract_deterministic_facts() -> None:
    jobs = load_jobs([ROOT / "evidence/runs", ROOT / "research/evidence/runs"])

    facts = {
        fact.agent_name: fact
        for job in jobs
        for fact in extract_job_facts(job).trials
    }

    assert set(facts) == {"oracle", "nop"}
    assert facts["oracle"].primary_reward == 1.0
    assert facts["nop"].primary_reward == 0.0
    assert facts["oracle"].exception_class is None
    assert facts["oracle"].trajectory_count == 0
    assert facts["oracle"].tool_call_count == 0
    assert facts["oracle"].artifact_count == 3  # manifest entries, including missing paths
    assert facts["nop"].artifact_count == 3
    assert facts["oracle"].missing_artifact_count == 1  # empty /logs/artifacts entry
    assert facts["nop"].missing_artifact_count == 2  # empty logs plus absent summary
    assert facts["oracle"].agent_execution_seconds is not None
    assert facts["oracle"].verifier_seconds is not None
    assert facts["oracle"].artifact_set_digest.startswith("sha256:")


def test_trial_facts_include_atif_tokens_tools_failures_and_association(tmp_path: Path) -> None:
    job_dir = _make_job(tmp_path)
    metadata = {
        "schema_version": 1,
        "experiment": {"spec_id": "experiment-123", "task": "sample-task"},
    }
    (job_dir / "lab-metadata.json").write_text(json.dumps(metadata))
    job = load_job(job_dir)

    fact = extract_trial_fact(job, job.trials[0])

    assert fact.experiment_id == "experiment-123"
    assert fact.input_tokens == 11
    assert fact.cache_tokens == 3
    assert fact.output_tokens == 5
    assert fact.cost_usd == 0.01
    assert fact.trajectory_count == 4
    assert fact.step_count == 5
    assert fact.llm_call_count == 1
    assert fact.tool_call_count == 1
    assert fact.command_failure_count == 1
    assert fact.repeated_failed_command_count == 0


def test_rebuild_from_raw_writes_joinable_fact_and_trajectory_tables(tmp_path: Path) -> None:
    job = load_job(_make_job(tmp_path / "raw"))
    output = tmp_path / "derived"

    first = rebuild_from_raw([job], output)
    second = rebuild_from_raw([job], output)

    assert {table.table for table in first.tables} == {
        "trajectories",
        "steps",
        "tool_calls",
        "observations",
        "trial_facts",
        "reward_facts",
        "artifact_facts",
        "tool_usage",
    }
    assert [(table.table, table.rows, table.sha256) for table in first.tables] == [
        (table.table, table.rows, table.sha256) for table in second.tables
    ]
    trial_glob = (output / "**/trial_facts.parquet").as_posix()
    trajectory_glob = (output / "**/trajectories.parquet").as_posix()
    joined = duckdb.sql(
        f"""
        SELECT f.trial_name, count(t.document_id) AS trajectory_count
        FROM read_parquet('{trial_glob}') f
        JOIN read_parquet('{trajectory_glob}') t USING (job_id, trial_id)
        GROUP BY f.trial_name
        """
    ).fetchone()
    assert joined == (job.trials[0].name, 4)
