from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.dispatch import (
    DispatchError,
    compile_set,
    compile_spec,
    default_timeout,
    load_dispatch_set,
    resolve_task,
)


def _task_dir(root: Path, name: str = "demo-task", timeout: int | None = 60) -> Path:
    task_dir = root / name
    task_dir.mkdir(parents=True)
    agent_block = f"[agent]\ntimeout_sec = {timeout}\n" if timeout is not None else ""
    (task_dir / "task.toml").write_text(f"[task]\nname = {name!r}\n{agent_block}")
    return task_dir


def test_resolve_task_requires_package(tmp_path: Path) -> None:
    with pytest.raises(DispatchError, match="not found"):
        resolve_task(tmp_path, "missing-task")
    with pytest.raises(DispatchError, match="relative"):
        resolve_task(tmp_path, "../escape")
    assert resolve_task(tmp_path, "registered/some-task") is None


def test_billable_agent_requires_model(tmp_path: Path) -> None:
    _task_dir(tmp_path)
    with pytest.raises(DispatchError, match="pass --model"):
        compile_spec(
            tmp_path,
            task="demo-task",
            agent="codex",
            model=None,
            name="demo-run",
            submitted_by="tester",
        )


def test_control_agent_rejects_model(tmp_path: Path) -> None:
    _task_dir(tmp_path)
    with pytest.raises(ValidationError, match="does not accept a model"):
        compile_spec(
            tmp_path,
            task="demo-task",
            agent="oracle",
            model="gpt-5.6-terra",
            name="demo-run",
            submitted_by="tester",
        )


def test_compiled_spec_binds_task_package_and_timeout(tmp_path: Path) -> None:
    _task_dir(tmp_path, timeout=120)
    spec = compile_spec(
        tmp_path,
        task="demo-task",
        agent="codex",
        model="gpt-5.6-terra",
        name="demo-run",
        submitted_by="tester",
    )
    assert spec.task_path == "demo-task"
    assert spec.timeout_seconds == 120
    assert spec.billable is True
    assert "codex" in spec.hypothesis and "gpt-5.6-terra" in spec.hypothesis


def test_timeout_falls_back_without_agent_block(tmp_path: Path) -> None:
    _task_dir(tmp_path, timeout=None)
    assert default_timeout(tmp_path / "demo-task") == 1_800


def test_name_must_satisfy_schema(tmp_path: Path) -> None:
    _task_dir(tmp_path)
    with pytest.raises(ValidationError):
        compile_spec(
            tmp_path,
            task="demo-task",
            agent="codex",
            model="gpt-5.6-terra",
            name="Bad Name!",
            submitted_by="tester",
        )


def test_set_rejects_unknown_keys_and_duplicates(tmp_path: Path) -> None:
    _task_dir(tmp_path)
    entries = [{"task": "demo-task", "agent": "codex", "model": "gpt-5.6-terra", "name": "abc"}]
    assert len(compile_set(tmp_path, entries, submitted_by="tester")) == 1
    with pytest.raises(DispatchError, match="unknown keys"):
        load_dispatch_set(_write(tmp_path, [{"task": "x", "bogus": 1}]))
    with pytest.raises(DispatchError, match="unique"):
        compile_set(
            tmp_path,
            [
                {"task": "demo-task", "agent": "codex", "model": "m", "name": "dup"},
                {"task": "demo-task", "agent": "codex", "model": "m", "name": "dup"},
            ],
            submitted_by="tester",
        )


def _write(root: Path, payload: object) -> Path:
    path = root / "set.json"
    path.write_text(json.dumps(payload))
    return path
