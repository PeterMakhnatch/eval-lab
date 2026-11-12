"""Focused behavior tests for pinned HF intake and the MiMo task catalog."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.task_catalog import (
    build_catalog,
    export_train_eligible,
    outcome_verdict,
    pull_hf_snapshot,
    task_audit_sql,
    task_outcomes_sql,
    wilson_interval,
)
from evallab.task_lint import mimo_manifest_sha

REF = "TestOrg/MiMo-V2.6-RL-harbor-fake@" + "a" * 40
REPO_ID = "TestOrg/MiMo-V2.6-RL-harbor-fake"

SCRIPT_TOML = """\
schema_version = "1.4"

[task]
name = "mimo-v2.6-rl/alpha-task"
description = "alpha fixture"

[metadata]
category = "Go"
domain = "fake"
source_id = "alpha-task"

[agent]
timeout_sec = 100.0

[verifier]
timeout_sec = 60.0
user = "root"

[environment]
docker_image = "docker.io/example/img@sha256:abc"
network_mode = "public"
cpus = 2
memory_mb = 1024

[environment.healthcheck]
command = "bash -c 'echo setup'"
timeout_sec = 60.0
"""


def _write_task(root: Path, task_id: str, *, toml: str, instruction: str,
                 command: str = "bash tests/test.sh", patch: str = "") -> Path:
    task = root / "tasks" / task_id
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(toml)
    (task / "instruction.md").write_text(instruction)
    (task / "environment" / "Dockerfile").write_text("FROM example\n")
    (task / "tests" / "test.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "tests" / "test_command.sh").write_text(command + "\n")
    if patch:
        (task / "tests" / "test.patch").write_text(patch)
    return task


def _write_snapshot_fixture(root: Path, *, bad_sha_tasks: tuple[str, ...] = ()) -> Path:
    """Minimal HF-layout fixture; optionally corrupt manifest entries."""
    _write_task(root, "alpha-task", toml=SCRIPT_TOML, instruction="Fix alpha.\n",
                command="go test example.com/acme/widgets/pkg -run TestX")
    judge_toml = SCRIPT_TOML.replace('name = "mimo-v2.6-rl/alpha-task"',
                                     'name = "mimo-v2.6-rl/beta-task"')
    judge_toml = judge_toml.replace('source_id = "alpha-task"', 'source_id = "beta-task"')
    judge_toml += '\n[verifier.env]\nGA_JUDGE_MODEL = "mimo-judge-v1"\n'
    beta = _write_task(root, "beta-task", toml=judge_toml,
                       instruction="Judge beta.\n", command="python tests/grade.py")
    (beta / "tests" / "grade.py").write_text("import os\n# llm judge grades here\n")
    manifest = {}
    for task_id in ("alpha-task", "beta-task"):
        sha = mimo_manifest_sha(root / "tasks" / task_id)
        manifest[task_id] = "0" * 64 if task_id in bad_sha_tasks else sha
    (root / "manifest.json").write_text(json.dumps({
        "adapter": "mimo_harbor 1.1.0", "source": "x", "revision": "a" * 40,
        "tasks": manifest,
    }))
    (root / "registry.json").write_text(json.dumps([{
        "name": "MiMo-V2.6-RL-harbor-fake", "version": "1.1.0",
        "tasks": [{"name": t, "path": f"tasks/{t}"} for t in ("alpha-task", "beta-task")],
    }]))
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "tasks.jsonl").write_text(
        "".join(json.dumps({"task_id": t, "source_id": t}) + "\n"
                for t in ("alpha-task", "beta-task"))
    )
    (root / "README.md").write_text("# fake\n")
    (root / "LICENSE").write_text("fake license\n")
    return root


def _fake_downloader(fixture: Path):
    def download(repo_id: str, revision: str, dest: Path) -> None:
        shutil.copytree(fixture, dest)

    return download




# ---------------------------------------------------------------- intake








# ---------------------------------------------------------------- catalog rows


def _pulled_snapshot(tmp_path: Path, *, bad: tuple[str, ...] = ()) -> Path:
    fixture = _write_snapshot_fixture(tmp_path / "fixture", bad_sha_tasks=bad)
    derived = tmp_path / "derived"
    result = pull_hf_snapshot(
        REF, repo_root=tmp_path, derived_root=derived,
        downloader=_fake_downloader(fixture),
    )
    assert result.status == "downloaded"
    return derived


def test_build_writes_four_tables_with_derived_rows(tmp_path: Path) -> None:
    derived = _pulled_snapshot(tmp_path)
    report = build_catalog(repo_root=tmp_path, derived_root=derived)
    assert report.tables == {
        "task_sources": 1, "task_versions": 2,
        "task_findings": report.tables["task_findings"], "task_lineage": 0,
    }
    assert report.snapshots == ["TestOrg__MiMo-V2.6-RL-harbor-fake@aaaaaaaaaaaa"]
    assert report.n_variants == 0

    versions = pq.read_table(derived / "external/task_catalog/task_versions.parquet")
    rows = {row["task_id"]: row for row in versions.to_pylist()}
    alpha = rows["alpha-task"]
    task_dir = (derived / "task-store/hf/TestOrg__MiMo-V2.6-RL-harbor-fake@aaaaaaaaaaaa"
                / "tasks" / "alpha-task")
    assert alpha["task_version_digest"] == task_directory_digest(task_dir)
    assert alpha["harbor_digest"] == harbor_task_digest(task_dir)
    assert alpha["origin"] == "external"
    assert alpha["source_repo"] == REPO_ID
    assert alpha["grader_kind"] == "script" and alpha["grader_cost"] == "free"
    assert alpha["network_mode"] == "public" and alpha["agent_user"] == "root"
    assert alpha["instruction_chars"] == len("Fix alpha.\n") and alpha["instruction_lang"] == "en"
    assert alpha["split_group"] == "fake:alpha-task"
    assert alpha["upstream_manifest_sha256"] == mimo_manifest_sha(task_dir)
    assert rows["beta-task"]["grader_kind"] == "llm_judge"
    assert rows["beta-task"]["grader_cost"] == "paid"

    sources = pq.read_table(derived / "external/task_catalog/task_sources.parquet")
    source = sources.to_pylist()[0]
    assert (source["n_manifest"], source["n_disk"], source["n_registry"]) == (2, 2, 2)








# ---------------------------------------------------------------- lint rules












# ---------------------------------------------------------------- verdicts


@pytest.mark.parametrize(("attempts", "scored", "passed", "want"), [
    (0, 0, 0, "untested"),
    (1, 0, 0, "infra_only"),
    (2, 2, 2, "always_pass"),
    (2, 2, 0, "always_fail"),
    (3, 2, 1, "learnable"),
])
def test_outcome_verdict_cases(attempts: int, scored: int, passed: int, want: str) -> None:
    assert outcome_verdict(attempts, scored, passed) == want


def test_wilson_interval_bounds() -> None:
    assert wilson_interval(0, 0) == (None, None)
    for n_pass, n_scored in ((0, 1), (1, 1), (1, 4), (7, 10)):
        lo, hi = wilson_interval(n_pass, n_scored)
        assert lo is not None and hi is not None
        assert 0.0 <= lo <= n_pass / n_scored <= hi <= 1.0


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


def _write_trial_facts(derived: Path, rows: list[dict]) -> None:
    job_dir = derived / "job_id=testjob" / "trial_id=testtrial"
    job_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), job_dir / "trial_facts.parquet")


def _write_aux_table(derived: Path, name: str, rows: list[dict]) -> None:
    catalog = derived / "external/task_catalog"
    pq.write_table(pa.Table.from_pylist(rows), catalog / f"{name}.parquet")


def test_export_provisional_then_split_gated(tmp_path: Path, capsys) -> None:
    derived = _pulled_snapshot(tmp_path)
    build_catalog(repo_root=tmp_path, derived_root=derived)
    versions = pq.read_table(derived / "external/task_catalog/task_versions.parquet")
    digests = {r["task_id"]: r for r in versions.to_pylist()}
    alpha_harbor = digests["alpha-task"]["harbor_digest"]
    _write_trial_facts(derived, [
        {"trial_id": "a1", "job_id": "j", "task_digest": alpha_harbor,
         "agent_name": "agentx", "model_name": "m", "environment_type": "docker",
         "primary_reward": 1.0, "exception_class": None},
        {"trial_id": "a2", "job_id": "j", "task_digest": alpha_harbor,
         "agent_name": "agentx", "model_name": "m", "environment_type": "docker",
         "primary_reward": 0.0, "exception_class": None},
    ])
    _write_aux_table(derived, "task_stability", [
        {"task_version_digest": digests["alpha-task"]["task_version_digest"],
         "harbor_digest": alpha_harbor, "job_name": "j", "trial_name": "a1",
         "method": "repeat_verifier", "backend": "docker",
         "state_preservation": "image snapshot", "n_runs": 2,
         "rewards": [1.0, 1.0], "verdict": "stable",
         "evidence_path": "runs/j", "produced_at": "2026-09-28T00:00:00Z"},
    ])
    _write_aux_table(derived, "task_exploits", [
        {"task_version_digest": digests["alpha-task"]["task_version_digest"],
         "harbor_digest": alpha_harbor, "job_name": "j", "trial_name": "a1",
         "probe_config": "probe-1@abc", "reward": 0.0, "exploit_status": "none",
         "method": "prompt-injection", "evidence_path": "runs/j",
         "produced_at": "2026-09-28T00:00:00Z"},
    ])

    out = tmp_path / "eligible.json"
    result = export_train_eligible(out, repo_root=tmp_path, derived_root=derived)
    assert result.provisional is True
    assert result.n_eligible == 1
    payload = json.loads(out.read_text())
    assert payload["items"][0]["split"] == "unassigned"
    canonical = json.dumps(
        {key: payload[key] for key in ("schema", "items")},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    assert payload["sha256"] == f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    # Same eligible set exported again -> same digest (created_at is not identity).
    again = export_train_eligible(tmp_path / "again.json", repo_root=tmp_path, derived_root=derived)
    assert again.sha256 == result.sha256
    assert set(payload["meta"]["table_digests"]) >= {
        "task_sources", "task_versions", "task_findings", "task_lineage",
        "task_stability", "task_exploits",
    }

    _write_aux_table(derived, "task_qualification", [
        {"task_version_digest": digests["alpha-task"]["task_version_digest"],
         "harbor_digest": alpha_harbor, "task_id": "alpha-task",
         "domain": "fake", "backend": "docker", "job_name": "j",
         "trial_name": "a1", "status": "broken",
         "reasons": ["nop_passes"],
         "finished_at": "2026-09-28T00:00:00Z"},
    ])
    out3 = tmp_path / "eligible3.json"
    broken = export_train_eligible(out3, repo_root=tmp_path, derived_root=derived)
    assert broken.n_eligible == 0
    assert broken.provisional is True

    digest = digests["alpha-task"]["task_version_digest"]
    split_path = tmp_path / "split.json"
    split_path.write_text(json.dumps({digest: "heldout"}))
    out2 = tmp_path / "eligible2.json"
    held = export_train_eligible(
        out2, repo_root=tmp_path, derived_root=derived, split_path=split_path)
    assert held.provisional is False
    assert held.n_eligible == 0

    capsys.readouterr()









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
