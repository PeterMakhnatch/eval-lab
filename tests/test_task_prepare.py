"""Behavioral regression tests for repeatable task preparation."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.execution_contracts import RunRequest, build_command
from evallab.task_import import package_digest
from evallab.task_prepare import prepare_task, replay_task
from evallab.terminus_harness import load_harness_tree

ORACLE_KWARGS: dict[str, object] = {
    "agent": "oracle",
    "model": None,
    "environment": "docker",
}


def _task(
    root: Path,
    *,
    name: str = "lab/demo-task",
    version: str = "1.2.0",
    agent_timeout: float = 120.0,
    verifier_timeout: float = 60.0,
) -> Path:
    package = root / "demo-task"
    package.mkdir(parents=True)
    (package / "task.toml").write_text(
        'schema_version = "1.4"\n'
        f'[task]\nname = "{name}"\nversion = "{version}"\n'
        f"[agent]\ntimeout_sec = {agent_timeout}\n"
        f"[verifier]\ntimeout_sec = {verifier_timeout}\n"
        "[environment]\ncpus = 1\nmemory_mb = 512\n",
        encoding="utf-8",
    )
    (package / "instruction.md").write_text("Do the demo thing.\n", encoding="utf-8")
    environment = package / "environment"
    environment.mkdir()
    (environment / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    tests = package / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text("def test_demo():\n    assert True\n", encoding="utf-8")
    script = package / "run.sh"
    script.write_text("#!/bin/sh\necho demo\n", encoding="utf-8")
    script.chmod(0o755)
    return package


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    return repo


def test_prepare_freezes_snapshot_against_later_source_edits(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    prepared = prepare_task(repo, source, name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]

    snapshot_bytes = (prepared.task_path / "instruction.md").read_bytes()
    snapshot_digest = package_digest(prepared.task_path)

    (source / "instruction.md").write_text("Do something else entirely.\n", encoding="utf-8")
    (source / "extra.txt").write_text("new file\n", encoding="utf-8")

    assert (prepared.task_path / "instruction.md").read_bytes() == snapshot_bytes
    assert package_digest(prepared.task_path) == snapshot_digest
    assert not (prepared.task_path / "extra.txt").exists()


def test_prepare_preserves_executable_modes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prepared = prepare_task(repo, _task(tmp_path / "external"), name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]
    assert os.stat(prepared.task_path / "run.sh").st_mode & 0o111


def test_prepare_is_idempotent_for_same_request(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    first = prepare_task(repo, source, name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]
    before = first.spec_path.read_bytes()
    second = prepare_task(repo, source, name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]
    assert second.task_path == first.task_path
    assert second.spec_path.read_bytes() == before


def test_prepare_refuses_to_overwrite_a_different_spec(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    first = prepare_task(repo, source, name="demo-run", **ORACLE_KWARGS)  # type: ignore[arg-type]
    before = first.spec_path.read_bytes()
    with pytest.raises(FileExistsError, match="refusing overwrite"):
        prepare_task(repo, source, name="demo-run", agent="nop", model=None, environment="docker")
    assert first.spec_path.read_bytes() == before


def test_prepare_refuses_drifted_snapshot(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    prepared = prepare_task(repo, source, name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]
    (prepared.task_path / "instruction.md").write_text("drifted\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="drifted"):
        prepare_task(repo, source, name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]


def test_prepare_rejects_bad_paths_and_inputs_without_a_spec(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    with pytest.raises(ValueError, match="job names"):
        prepare_task(repo, source, name="Bad_Name", **ORACLE_KWARGS)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="escapes repository"):
        prepare_task(
            repo,
            source,
            name="demo-oracle",
            output=Path("/tmp/elsewhere.json"),
            **ORACLE_KWARGS,  # type: ignore[arg-type]
        )
    corpus = tmp_path / "corpus"
    (corpus / "nested").mkdir(parents=True)
    (corpus / "nested" / "task.toml").write_text('[task]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no task.toml"):
        prepare_task(repo, corpus, name="demo-oracle", **ORACLE_KWARGS)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported environment"):
        prepare_task(
            repo, source, name="demo-oracle", agent="oracle", model=None, environment="mars"
        )
    assert not (repo / "derived").exists()
    assert not (repo / "runs").exists()


def test_prepare_requires_explicit_cost_for_metered_and_est_for_remote(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    with pytest.raises(ValueError, match="cost_limit_usd"):
        prepare_task(
            repo,
            source,
            name="demo-paid",
            agent="mini-swe-agent",
            model="deepseek/deepseek-flash",
            environment="docker",
        )
    with pytest.raises(ValueError, match="est_cost_usd"):
        prepare_task(
            repo,
            source,
            name="demo-remote",
            agent="mini-swe-agent",
            model="zai/glm-5.3-flash",
            environment="daytona",
            cost_limit_usd=2.5,
        )
    assert not (repo / "derived").exists()
    assert not (repo / "runs").exists()


def test_prepare_keeps_agent_and_verifier_deadlines_separate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external", agent_timeout=120.0, verifier_timeout=60.0)
    official = prepare_task(repo, source, name="demo-official", **ORACLE_KWARGS)
    command = build_command(
        RunRequest(
            task=official.task_path,
            agent="oracle",
            name=official.spec.name,
            jobs_dir=repo / "runs",
            timeout_seconds=official.spec.timeout_seconds,
        )
    )
    assert command[command.index("--agent-timeout-multiplier") + 1] == "1"

    diagnostic = prepare_task(
        repo, source, name="demo-diagnostic", timeout_seconds=60, **ORACLE_KWARGS
    )
    assert diagnostic.spec.timeout_seconds == 60
    assert diagnostic.task_timeout_seconds == 120
    assert diagnostic.warnings


def test_prepare_never_silently_truncates_a_task_deadline(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external", agent_timeout=36_000)
    with pytest.raises(ValueError, match="explicit shorter diagnostic"):
        prepare_task(repo, source, name="demo-official", **ORACLE_KWARGS)
    assert not (repo / "derived").exists()
    assert not (repo / "runs").exists()

    explicit = prepare_task(
        repo, source, name="demo-diagnostic", timeout_seconds=600, **ORACLE_KWARGS
    )
    assert explicit.task_timeout_seconds == 36_000
    assert explicit.spec.timeout_seconds == 600


def test_prepare_cannot_write_through_a_snapshot_root_symlink(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "runs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes repository"):
        prepare_task(repo, source, name="demo-oracle", **ORACLE_KWARGS)

    assert list(outside.iterdir()) == []
    assert not (repo / "derived").exists()


@pytest.mark.parametrize(
    ("model", "cost_limit", "reason"),
    [
        ("zai/glm-5.3-flash", None, "cost_limit_usd"),
        ("zai-coding-plan/glm-5.3-flash", 0.4, "standard-API"),
    ],
)
def test_terminus_prepare_refuses_unmetered_or_unentitled_routes_before_freezing(
    tmp_path: Path, model: str, cost_limit: float | None, reason: str
) -> None:
    repo = _repo(tmp_path)
    source = _task(tmp_path / "external")
    with pytest.raises(ValueError, match=reason):
        prepare_task(
            repo,
            source,
            name="terminus-guarded",
            agent="terminus-2",
            model=model,
            environment="daytona",
            cost_limit_usd=cost_limit,
            est_cost_usd=0.9,
        )
    assert not (repo / "runs").exists()
    assert not (repo / "derived").exists()


def _harness(root: Path, rules: str) -> Path:
    terminus = root / "terminus"
    terminus.mkdir(parents=True)
    (terminus / "config.json").write_text('{"max_turns": 4}\n', encoding="utf-8")
    (terminus / "AGENTS.md").write_text(rules, encoding="utf-8")
    return root


def test_local_harness_snapshot_survives_source_drift_without_fake_caps(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tree = _harness(tmp_path / "source-tree", "Inspect inputs.\n")
    prepared = prepare_task(
        repo, _task(tmp_path / "external"), name="local-harness",
        agent="terminus-2", model="ollama_chat/qwen2.5:7b", environment="docker",
        harness_tree_path=tree,
    )
    spec = prepared.spec
    (tree / "terminus/AGENTS.md").write_text("Changed after preparation.\n", encoding="utf-8")
    frozen = load_harness_tree(repo / spec.harness_tree_path, spec.harness_tree_sha256)
    assert frozen.rules_path.read_text() == "Inspect inputs.\n"
    assert load_harness_tree(tree).sha256 != frozen.sha256
    assert (spec.max_requests, spec.max_input_tokens, spec.max_output_tokens,
            spec.max_total_tokens, spec.cost_limit_usd) == (None, None, None, None, None)
    assert spec.timeout_seconds == 120
    assert spec.billable  # Local non-control execution still needs recorded authorization.


def test_harness_replay_preserves_controls_but_never_inherits_approval(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prepared = prepare_task(
        repo, _task(tmp_path / "external"), name="pinned-baseline",
        agent="terminus-2", model="zai/glm-5.3-flash", environment="docker",
        cost_limit_usd=0.4, max_requests=9, max_input_tokens=8000,
        max_output_tokens=2000, max_total_tokens=10000,
        harness_tree_path=_harness(tmp_path / "baseline-tree", "Baseline rules.\n"),
    )
    base = prepared.spec.model_copy(update={
        "spec_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "submitted_at": datetime(2026, 1, 2, tzinfo=UTC),
        "policy_rule": "prior-human-approval",
        "grid_point": {"point_id": "fixed-task-model"},
    })
    retained = repo / "retained-spec.json"
    retained.write_text(base.model_dump_json(), encoding="utf-8")
    original = retained.read_bytes()
    replayed, _ = replay_task(
        repo, retained, name="pinned-candidate",
        harness_tree_path=_harness(tmp_path / "candidate-tree", "Candidate rules.\n"),
    )
    controls = (
        "task_package_digest", "verifier_digest", "task_path", "agent", "model",
        "environment", "attempts", "concurrency", "timeout_seconds", "grid_point",
        "max_requests", "max_input_tokens", "max_output_tokens", "max_total_tokens",
        "cost_limit_usd", "est_cost_usd",
    )
    assert {key: getattr(replayed, key) for key in controls} == {
        key: getattr(base, key) for key in controls
    }
    assert replayed.harness_tree_sha256 != base.harness_tree_sha256
    assert (replayed.spec_id, replayed.submitted_at, replayed.policy_rule) == (None, None, None)
    assert retained.read_bytes() == original
    assert replayed.billable


def test_harness_replay_refuses_changed_task_before_publishing_candidate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prepared = prepare_task(
        repo, _task(tmp_path / "external"), name="local-baseline",
        agent="terminus-2", model="ollama_chat/qwen2.5:7b", environment="docker",
        harness_tree_path=_harness(tmp_path / "baseline-tree", "Baseline rules.\n"),
    )
    (prepared.task_path / "instruction.md").write_text("A different task.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="task_package_digest"):
        replay_task(
            repo, prepared.spec_path, name="local-candidate",
            harness_tree_path=_harness(tmp_path / "candidate-tree", "Candidate rules.\n"),
        )
    assert not (repo / "derived/prepared/local-candidate.json").exists()
