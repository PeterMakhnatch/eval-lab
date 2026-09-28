"""Focused behavior tests for pinned HF intake and the MiMo task catalog."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab import cli
from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.storage.attach import attach
from evallab.task_catalog import (
    CatalogError,
    build_catalog,
    derive_grader_kind_cost,
    derive_split_group,
    export_train_eligible,
    load_lineage_dicts,
    outcome_verdict,
    pull_hf_snapshot,
    task_audit_sql,
    task_outcomes_sql,
    wilson_interval,
)
from evallab.task_lint import lint_mimo_task, mimo_manifest_sha

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


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    monkeypatch.setattr(cli, "load_local_env", lambda _path: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:5432/nowhere")


# ---------------------------------------------------------------- intake


@pytest.mark.parametrize("ref", [
    "",
    "TestOrg/repo",
    "TestOrg/repo@latest",
    "TestOrg/repo@main",
    "TestOrg/repo@abc123",
    "TestOrg/repo@" + "a" * 39,
    "TestOrg/repo@" + "A" * 40,
    "noslash@" + "a" * 40,
    "a/b/c@" + "a" * 40,
])
def test_pull_hf_refuses_unpinned(tmp_path: Path, ref: str) -> None:
    with pytest.raises(CatalogError):
        pull_hf_snapshot(ref, repo_root=tmp_path, derived_root=tmp_path / "derived")


def test_pull_download_reuse_and_tamper_refusal(tmp_path: Path) -> None:
    fixture = _write_snapshot_fixture(tmp_path / "fixture")
    derived = tmp_path / "derived"
    first = pull_hf_snapshot(
        REF, repo_root=tmp_path, derived_root=derived,
        downloader=_fake_downloader(fixture),
    )
    assert first.status == "downloaded"
    assert first.n_tasks == 2
    assert first.manifest_mismatches == ()
    assert first.provenance_path is not None and first.provenance_path.is_file()
    provenance = json.loads(first.provenance_path.read_text())
    assert provenance["zone"] == "01-external"
    assert provenance["revision"] == "a" * 40
    assert provenance["material_digest"].startswith("sha256:")
    mode = (first.snapshot / "tasks" / "alpha-task" / "task.toml").stat().st_mode
    assert not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)

    second = pull_hf_snapshot(REF, repo_root=tmp_path, derived_root=derived)
    assert second.status == "reused"
    assert second.snapshot == first.snapshot

    target = first.snapshot / "tasks" / "alpha-task" / "instruction.md"
    target.chmod(0o644)
    target.write_text("tampered\n")
    with pytest.raises(CatalogError):
        pull_hf_snapshot(REF, repo_root=tmp_path, derived_root=derived)


def test_pull_reports_manifest_mismatch_without_crashing(tmp_path: Path) -> None:
    fixture = _write_snapshot_fixture(tmp_path / "fixture", bad_sha_tasks=("beta-task",))
    result = pull_hf_snapshot(
        REF, repo_root=tmp_path, derived_root=tmp_path / "derived",
        downloader=_fake_downloader(fixture),
    )
    assert result.status == "downloaded"
    assert result.manifest_mismatches == ("beta-task",)


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


def test_build_records_manifest_mismatch_as_error_finding(tmp_path: Path) -> None:
    derived = _pulled_snapshot(tmp_path, bad=("beta-task",))
    report = build_catalog(repo_root=tmp_path, derived_root=derived)
    assert report.findings_by_rule_domain["mimo-manifest-digest-mismatch"]["fake"] == 1
    findings = pq.read_table(derived / "external/task_catalog/task_findings.parquet")
    rows = [r for r in findings.to_pylist()
            if r["rule"] == "mimo-manifest-digest-mismatch"]
    assert len(rows) == 1
    assert rows[0]["task_id"] == "beta-task" and rows[0]["severity"] == "error"


def test_grader_derivation_prefers_files_over_domain(tmp_path: Path) -> None:
    task = tmp_path / "task"
    (task / "tests").mkdir(parents=True)
    assert derive_grader_kind_cost(task, {}) == ("script", "free")
    (task / "tests" / "grade.py").write_text("vision model grades\n")
    toml = {"verifier": {"env": {"WEBDEV_JUDGE_MODEL": "m-vlm"}}}
    assert derive_grader_kind_cost(task, toml) == ("vlm_judge", "paid")
    toml = {"verifier": {"env": {"GA_JUDGE_MODEL": "m-llm"}}}
    (task / "tests" / "grade.py").write_text("plain judge\n")
    assert derive_grader_kind_cost(task, toml) == ("llm_judge", "paid")


def test_split_group_rules(tmp_path: Path) -> None:
    code = _write_task(tmp_path / "code", "w-task", toml=SCRIPT_TOML,
                       instruction="x\n",
                       command="go test github.com/acme/widgets/pkg -run TestA",
                       patch="diff --git a/x b/x\n+go test github.com/acme/widgets/other -run TestB\n")
    group, unresolved = derive_split_group("code", "w-task", code, {}, None)
    assert (group, unresolved) == ("code:github.com/acme/widgets", False)

    linked = _write_task(tmp_path / "linked", "l-task", toml=SCRIPT_TOML,
                         instruction="Fix https://github.com/acme/widgets/issues/42\n",
                         command="go test ./... -run TestX")
    group, unresolved = derive_split_group("code", "l-task", linked, {}, None)
    assert (group, unresolved) == ("code:github.com/acme/widgets", False)

    bare = _write_task(tmp_path / "bare", "b-task", toml=SCRIPT_TOML, instruction="x\n")
    group, unresolved = derive_split_group("code", "b-task", bare, {}, None)
    assert (group, unresolved) == ("code:b-task", True)

    group, unresolved = derive_split_group(
        "cyber", "c-task", bare, {},
        {"expected_crash": {"file": "graphicsmagick/crash.bin"}},
    )
    assert (group, unresolved) == ("cyber:graphicsmagick", False)

    group, unresolved = derive_split_group("general", "quiz_rl_007", bare, {}, None)
    assert (group, unresolved) == ("general:quiz", False)

    group, unresolved = derive_split_group("terminal", "t-1", bare, {}, None)
    assert (group, unresolved) == ("terminal:t-1", False)


# ---------------------------------------------------------------- lint rules


def test_mimo_lint_true_positive_and_negative(tmp_path: Path) -> None:
    leaky = _write_task(
        tmp_path / "leaky", "leak", toml=SCRIPT_TOML,
        instruction="See /tests/grade.py; reward.txt holds the score.\n",
    )
    findings = {finding.rule: finding for finding in lint_mimo_task(
        leaky, expected_manifest_sha="0" * 64, domain="fake")}
    assert findings["mimo-manifest-digest-mismatch"].severity == "error"
    assert findings["mimo-answer-leak"].severity == "warning"
    assert "/tests/" in findings["mimo-answer-leak"].message
    assert "mimo-no-oracle" in findings
    assert "mimo-verifier-not-isolated" in findings
    assert "mimo-network-public" in findings
    assert "mimo-setup-healthcheck-only" in findings
    assert "mimo-paid-judge" not in findings
    assert "mimo-id-case-collision" not in findings

    clean_toml = SCRIPT_TOML.replace('user = "root"', 'user = "agent"')
    clean_toml = clean_toml.replace('network_mode = "public"', 'network_mode = "isolated"')
    clean_toml = clean_toml.replace("[environment.healthcheck]\ncommand = \"bash -c 'echo setup'\"\ntimeout_sec = 60.0\n", "")
    clean = _write_task(tmp_path / "clean", "clean", toml=clean_toml,
                        instruction="Do the thing.\n")
    (clean / "solution").mkdir()
    (clean / "solution" / "solve.sh").write_text("exit 0\n")
    sha = mimo_manifest_sha(clean)
    assert lint_mimo_task(clean, expected_manifest_sha=sha, domain="fake") == []


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
         "model_name": "", "primary_reward": 0.0, "exception_class": None},
        {"trial_id": "e1", "task_digest": "sha256:hb1", "agent_name": "agentx",
         "model_name": "m", "primary_reward": None, "exception_class": "VerifierError"},
        {"trial_id": "m1", "task_digest": "sha256:hb2", "agent_name": "agentx",
         "model_name": "m", "primary_reward": -1.0, "exception_class": None},
    ])
    conn.register("trials", trials)
    rows = conn.execute(task_outcomes_sql(trials="trials", versions="v")).fetchall()
    by_key = {(row[2], row[4]): row for row in rows}
    nop = by_key[("t1", "nop")]
    assert (nop[6], nop[7], nop[8], nop[9], nop[11], nop[14]) == (1, 1, 0, 0, 0.0, "always_fail")
    ctl = by_key[("t1", "agentx")]
    assert (ctl[6], ctl[7], ctl[8], ctl[14]) == (1, 0, 1, "infra_only")
    neg = by_key[("t2", "agentx")]
    assert (neg[7], neg[8], neg[14]) == (0, 1, "infra_only")
    bare = by_key[("t3", "")]
    assert (bare[6], bare[7], bare[14]) == (0, 0, "untested")
    assert bare[10] is None and bare[11] is None and bare[12] is None and bare[13] is None
    conn.close()


def test_audit_view_eligibility_and_missing_tables() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE v AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'sha256:h1', 't1', 'n1', 'terminal', 'terminal:t1', 'script', 'free'),"
                 "('sha256:p2', 'sha256:h2', 't2', 'n2', 'terminal', 'terminal:t2', 'script', 'free')"
                 ") AS t(task_version_digest, harbor_digest, task_id, task_name,"
                 " domain, split_group, grader_kind, grader_cost)")
    conn.execute("CREATE TABLE o AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'a', 'm', 2, 2, 0, 0.5, 0.1, 0.9, 'learnable'),"
                 "('sha256:p2', 'a', 'm', 1, 1, 0, 0.0, 0.0, 0.8, 'always_fail')"
                 ") AS t(task_version_digest, agent_name, model_name, n_attempts,"
                 " n_scored, n_infra, pass_rate, pass_rate_lo, pass_rate_hi, verdict)")
    conn.execute("CREATE TABLE s AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'stable', 3, 'runs/e1'),"
                 "('sha256:p2', 'flipped', 2, 'runs/e2')"
                 ") AS t(task_version_digest, verdict, n_runs, evidence_path)")
    conn.execute("CREATE TABLE e AS SELECT * FROM (VALUES "
                 "('sha256:p1', 'none', 'runs/x1')"
                 ") AS t(task_version_digest, exploit_status, evidence_path)")
    rows = conn.execute(task_audit_sql(
        outcomes="o", versions="v", stability="s", exploits="e")).fetchall()
    by_id = {row[2]: row for row in rows}
    assert by_id["t1"][-2] is True
    assert by_id["t1"][-1] is None
    assert by_id["t2"][-2] is False
    assert "not learnable" in str(by_id["t2"][-1])

    rows = conn.execute(task_audit_sql(
        outcomes="o", versions="v", has_stability=False, has_exploits=False)).fetchall()
    by_id = {row[2]: row for row in rows}
    assert by_id["t1"][-2] is False
    assert by_id["t1"][-1] == "no stability evidence"
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
         "agent_name": "agentx", "model_name": "m", "primary_reward": 1.0,
         "exception_class": None},
        {"trial_id": "a2", "job_id": "j", "task_digest": alpha_harbor,
         "agent_name": "agentx", "model_name": "m", "primary_reward": 0.0,
         "exception_class": None},
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
        {key: payload[key] for key in ("schema", "items", "meta")},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    assert payload["sha256"] == f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    assert set(payload["meta"]["table_digests"]) >= {
        "task_sources", "task_versions", "task_findings", "task_lineage",
        "task_stability", "task_exploits",
    }

    digest = digests["alpha-task"]["task_version_digest"]
    split_path = tmp_path / "split.json"
    split_path.write_text(json.dumps({digest: "heldout"}))
    out2 = tmp_path / "eligible2.json"
    held = export_train_eligible(
        out2, repo_root=tmp_path, derived_root=derived, split_path=split_path)
    assert held.provisional is False
    assert held.n_eligible == 0

    capsys.readouterr()


def test_cli_pull_refusal_build_and_show(tmp_path: Path, capsys) -> None:
    code = cli.run_cli(["tasks", "pull-hf", "TestOrg/repo@latest"], workspace=tmp_path)
    assert code == 2
    assert "refused" in capsys.readouterr().err

    derived = _pulled_snapshot(tmp_path)
    code = cli.run_cli(
        ["tasks", "catalog", "build", "--derived-root", str(derived)], workspace=tmp_path)
    assert code == 0
    out = capsys.readouterr().out
    assert "task_versions: 2 rows" in out

    code = cli.run_cli(
        ["tasks", "catalog", "show", "alpha-task", "--derived-root", str(derived)],
        workspace=tmp_path,
    )
    assert code == 0
    shown = capsys.readouterr().out
    assert "task_id=alpha-task" in shown
    assert "harbor_digest: sha256:" in shown
    assert "split_group: fake:alpha-task" in shown

    result = attach(repo_root=tmp_path, explicit_derived=derived)
    try:
        names = {row[0] for row in result.connection.execute("SHOW TABLES").fetchall()}
    finally:
        result.connection.close()
    assert {"task_versions", "v_task_outcomes", "v_task_audit"} <= names


def _valid_variant_record() -> dict:
    digest = "sha256:" + "1" * 64
    return {
        "schema": "evallab.task_variant/v1",
        "task_name": "mimo-v2.6-rl/alpha-task",
        "variant_digest": digest,
        "variant_harbor_digest": "sha256:" + "2" * 64,
        "parent": {
            "digest": "sha256:" + "3" * 64,
            "harbor_digest": "sha256:" + "4" * 64,
            "source": {
                "kind": "hf", "repo": "o/r", "revision": "5" * 40,
                "path": "tasks/alpha-task", "record": None,
            },
        },
        "transform": "handfix@v1",
        "components_changed": ["verifier"],
        "files": [{
            "path": "tests/test.sh",
            "before_sha256": "sha256:" + "6" * 64,
            "after_sha256": "sha256:" + "7" * 64,
            "content": "exit 0\n",
        }],
        "rationale": "test record",
        "inputs": {},
        "created_by": "test",
        "created_at": "2026-09-28T00:00:00Z",
        "status": "candidate",
        "evidence": [],
    }


def test_lineage_loader_uses_strict_module_and_skips_invalid(tmp_path: Path) -> None:
    variants = tmp_path / "library" / "task-variants" / "slug"
    variants.mkdir(parents=True)
    (variants / "good.json").write_text(json.dumps(_valid_variant_record()))
    (variants / "bad.json").write_text("{not json")
    (variants / "wrong-schema.json").write_text(json.dumps({"schema": "other/v9"}))
    records, skipped = load_lineage_dicts(tmp_path)
    assert len(records) == 1 and skipped == 2
    payload, relpath = records[0]
    assert payload["variant_digest"] == "sha256:" + "1" * 64
    assert payload["parent"]["source"]["kind"] == "hf"
    assert relpath == "library/task-variants/slug/good.json"


def test_build_indexes_variant_versions_and_lineage(tmp_path: Path) -> None:
    derived = _pulled_snapshot(tmp_path)
    variants = tmp_path / "library" / "task-variants" / "slug"
    variants.mkdir(parents=True)
    record = _valid_variant_record()
    (variants / "good.json").write_text(json.dumps(record))
    report = build_catalog(repo_root=tmp_path, derived_root=derived)
    assert report.n_variants == 1 and report.skipped_variant_records == 0
    versions = pq.read_table(derived / "external/task_catalog/task_versions.parquet")
    rows = {row["task_id"]: row for row in versions.to_pylist()}
    assert rows["alpha-task"]["origin"] == "variant"
    assert rows["alpha-task"]["transform"] == "handfix@v1"
    lineage = pq.read_table(derived / "external/task_catalog/task_lineage.parquet")
    links = lineage.to_pylist()
    assert len(links) == 1
    assert links[0]["child_digest"] == "sha256:" + "1" * 64
    assert links[0]["transform"] == "handfix@v1"
