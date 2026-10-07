from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evallab import cli
from evallab.dataset_audit import _action, _generic_finalize, audit_dataset
from evallab.dataset_audit_contracts import AuditObservation, AuditRecord, AuditTask
from evallab.dataset_audit_execution import run_local_control, run_or_stage_paid
from evallab.dataset_audit_sources import resolve_harbor_dataset


def _package(root: Path, name: str = "example/task") -> Path:
    root.mkdir(parents=True)
    (root / "task.toml").write_text(
        f'[task]\nname = "{name}"\nversion = "1.0.0"\n'
        '[agent]\ntimeout_sec = 30\n[verifier]\ntimeout_sec = 30\n'
        '[environment]\ncpus = 1\nmemory_mb = 512\ngpus = 0\n',
    )
    (root / "instruction.md").write_text("Write an answer file containing the requested greeting.")
    (root / "environment").mkdir()
    (root / "environment/Dockerfile").write_text("FROM ubuntu:24.04\nWORKDIR /app\n")
    (root / "solution").mkdir()
    (root / "solution/solve.sh").write_text("#!/bin/sh\nprintf hello > /app/answer\n")
    (root / "tests").mkdir()
    (root / "tests/test.sh").write_text("#!/bin/sh\npython /tests/test_state.py\n")
    (root / "tests/test_state.py").write_text('assert open("/app/answer").read() == "hello"\n')
    return root


def _task() -> AuditTask:
    return AuditTask(dataset_id="example@1", task_id="example/task", task_name="example/task",
                     package_digest="sha256:" + "a" * 64, harbor_digest="sha256:" + "b" * 64,
                     source_uri="harbor:example@1/task", aliases=("example/task",))


def _control(task: AuditTask, stage: str, value: float, **changes) -> AuditObservation:
    facts = {"agent": stage, "reward": value, "completed": True, "exception": None,
             "package_digest": task.package_digest, "harbor_digest": task.harbor_digest,
             "label": "sound" if stage == "nop" else "oracle:pass"}
    facts.update(changes)
    return AuditObservation(stage=stage, status="executed", facts=facts)


@pytest.mark.parametrize(
    ("options", "estimate", "disposition"),
    [
        ([], 0, "requires_opt_in"),
        (["--allow-paid", "--model", "zai/glm-5.3-flash", "--est-cost-usd", "1.25"], 1.25, "run"),
    ],
)
def test_default_cli_dry_run_cannot_launch_or_write(tmp_path, monkeypatch, capsys, options, estimate, disposition):
    package = _package(tmp_path / "dataset/task")
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(tmp_path / "parquet"))
    monkeypatch.setenv("EVALLAB_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("EVALLAB_READERS_STORE", str(tmp_path / "readers"))
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    def forbidden(*args, **kwargs):
        raise AssertionError("dry audit launched a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    code = cli.run_cli(
        ["audit", str(package), "--stages", "static,oracle,nop,exploit", "--json", *options],
        workspace=tmp_path,
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert result["dry_run"] is True
    assert result["outputs"] == {}
    assert result["estimated_cost_usd"] == estimate
    assert result["counts"] == {"unknown": 1}
    assert result["records"][0]["task"]["task_id"] == "example/task"
    assert next(action for action in result["actions"] if action["stage"] == "exploit")["disposition"] == disposition
    assert {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert not (tmp_path / "queue").exists()
    assert not (tmp_path / "parquet").exists()


def test_remote_controls_are_paid_and_opt_in_does_not_hide_the_estimate():
    task = _task()
    denied = _action(task, "nop", None, dry_run=False, allow_paid=False,
                     environment="daytona", model=None, estimated_cost_usd=0.5)
    assert denied.paid is True
    assert denied.disposition == "requires_opt_in"
    assert denied.estimated_cost_usd == 0.5


def test_static_flags_cannot_override_bound_controls():
    task = _task()
    record = AuditRecord(task=task, stages={
        "nop": _control(task, "nop", 0.0), "oracle": _control(task, "oracle", 1.0),
        "static": AuditObservation(stage="static", status="recorded",
                                   facts={"a_unstated_literal": True, "b_network": True, "c_nondeterminism": True}),
    })
    assert _generic_finalize(record).verdict == "keep"
    assert _generic_finalize(record.model_copy(update={"stages": {"static": record.stages["static"]}})).verdict == "unknown"


@pytest.mark.parametrize("changes", [
    {"package_digest": "sha256:" + "c" * 64},
    {"harbor_digest": "sha256:" + "c" * 64},
    {"completed": False}, {"exception": {"exception_type": "TimeoutError"}},
    {"reward": True}, {"agent": "terminus-2"},
])
def test_foreign_or_incomplete_controls_cannot_produce_keep(changes):
    task = _task()
    record = AuditRecord(task=task, stages={"nop": _control(task, "nop", 0.0),
                                         "oracle": _control(task, "oracle", 1.0, **changes)})
    assert _generic_finalize(record).verdict == "unknown"


def test_unbound_leak_cannot_label_the_selected_package():
    task = _task()
    record = AuditRecord(task=task, stages={"leak": AuditObservation(
        stage="leak", status="executed", facts={"has_future_history": True,
                                                  "audited_package_digest": "sha256:" + "c" * 64})})
    result = _generic_finalize(record)
    assert result.verdict == "unknown"
    assert result.facets["leak"]["found"] is None


def test_execution_helpers_refuse_spend_bypasses_before_creating_state(tmp_path):
    task = _task()
    with pytest.raises(ValueError):
        run_local_control(task, repo_root=tmp_path, agent="terminus-2", name="refused-run")
    with pytest.raises(ValueError):
        run_or_stage_paid(task, "exploit", repo_root=tmp_path, allow_paid=False,
                          environment="docker", model="zai/glm-5.3-flash", estimated_cost_usd=1,
                          cost_limit_usd=0.25, timeout_seconds=30, linear_card=None)
    assert not (tmp_path / "queue").exists()
    assert not (tmp_path / "runs").exists()


def test_namespace_and_duplicate_dataset_identities(tmp_path):
    _package(tmp_path / "dataset/a/leaf", "a/leaf")
    _package(tmp_path / "dataset/b/leaf", "b/leaf")
    dataset = resolve_harbor_dataset(str(tmp_path / "dataset"), repo_root=tmp_path)
    assert {task.task_id: task.aliases for task in dataset.tasks} == {"a/leaf": ("a/leaf",), "b/leaf": ("b/leaf",)}
    _package(tmp_path / "dataset/duplicate/leaf", "a/leaf")
    with pytest.raises(ValueError):
        resolve_harbor_dataset(str(tmp_path / "dataset"), repo_root=tmp_path)


def test_uncached_dataset_dry_run_never_downloads(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("dry audit tried acquisition")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ValueError):
        audit_dataset("uncached-dataset@1.0", repo_root=tmp_path, stages=("static",))
    assert not (tmp_path / "library").exists()
