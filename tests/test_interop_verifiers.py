"""xplat verifiers runner: plans, cells, and seams (no docker, no verifiers, $0).

Live-Docker cells are proven by manual smoke runs, not committed tests: the
deterministic-test rule forbids Docker-daemon, network, and host-state
dependence. These tests cover the pure seams (plan building, harness config,
workdir resolution, image choice, verdict mapping, run_cell guards) with
synthetic task dirs and stubbed collaborators.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab import interop as _interop
from evallab import interop_verifiers as vf


def make_task(root: Path, *, name: str = "lab/synthetic", workdir: str | None = None) -> Path:
    task_dir = root / "task"
    (task_dir / "solution").mkdir(parents=True)
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "instruction.md").write_text("Do the thing.\n", encoding="utf-8")
    (task_dir / "solution" / "solve.sh").write_text("#!/bin/bash\necho solved\n", encoding="utf-8")
    (task_dir / "tests" / "test.sh").write_text(
        "#!/bin/bash\necho 1 > /logs/verifier/reward.txt\n", encoding="utf-8"
    )
    (task_dir / "environment" / "Dockerfile").write_text(
        "FROM python:3.12-slim-bookworm\n", encoding="utf-8"
    )
    lines = [
        "artifacts = []\n",
        "\n[task]\n",
        f'name = "{name}"\n',
        "\n[metadata]\n",
        'difficulty = "easy"\n',
        "\n[verifier]\n",
        "timeout_sec = 120.0\n",
        "\n[environment]\n",
        'network_mode = "public"\n',
    ]
    if workdir is not None:
        lines.append(f'workdir = "{workdir}"\n')
    (task_dir / "task.toml").write_text("".join(lines), encoding="utf-8")
    return task_dir


# -- plans (single factory in interop; this runner only materializes) -----------


def test_factory_oracle_runs_solve_in_workdir(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    plan = _interop.scripted_agent_plan(task, "oracle", ())
    assert plan.command == "bash solution/solve.sh"
    assert plan.files == {}


def test_materialize_oracle_uploads_solution(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    plan = _interop.scripted_agent_plan(task, "oracle", ())
    files = vf.materialize_files(task, "oracle", plan)
    assert files["/solution/solve.sh"] == b"#!/bin/bash\necho solved\n"


def test_materialize_oracle_uses_task_workdir(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path / "w", workdir="/task"))
    plan = _interop.scripted_agent_plan(task, "oracle", ())
    files = vf.materialize_files(task, "oracle", plan)
    assert files["/task/solution/solve.sh"] == b"#!/bin/bash\necho solved\n"
    assert files["/solution/solve.sh"] == b"#!/bin/bash\necho solved\n"


def test_materialize_oracle_needs_solve_sh(tmp_path: Path) -> None:
    task_dir = make_task(tmp_path)
    (task_dir / "solution" / "solve.sh").unlink()
    task = SimpleNamespace(task_dir=task_dir)
    with pytest.raises(ValueError, match="oracle plan needs"):
        _interop.scripted_agent_plan(task, "oracle", ())


def test_factory_nop_is_empty(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    plan = _interop.scripted_agent_plan(task, "nop", ())
    assert plan.files == {} and plan.command is None
    assert vf.materialize_files(task, "nop", plan) == {}


def test_factory_cheat_stages_ladder_with_subset(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    plan = _interop.scripted_agent_plan(task, "cheat", ("reward_plant", "skip_plant"))
    assert set(plan.files) == {_interop.CHEAT_LADDER_CONTAINER_PATH}
    assert b"CHEAT_LADDER_RESULT=" in plan.files[_interop.CHEAT_LADDER_CONTAINER_PATH]
    assert "--attacks reward_plant,skip_plant" in (plan.command or "")
    assert "--cwd ." in (plan.command or "")
    full = _interop.scripted_agent_plan(task, "cheat", ())
    assert "--attacks" not in (full.command or "")


def test_factory_cheat_rejects_unknown_attack(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    with pytest.raises(ValueError, match="unknown cheat attack"):
        _interop.scripted_agent_plan(task, "cheat", ("nope",))


def test_factory_rejects_unknown_agent(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    with pytest.raises(ValueError, match="oracle/nop/cheat"):
        _interop.scripted_agent_plan(task, "wizard", ())


def test_relaxed_load_allows_solution_less_tasks(tmp_path: Path) -> None:
    task_dir = make_task(tmp_path)
    shutil.rmtree(task_dir / "solution")
    task = _interop.load_harbor_task(task_dir, require_solution=False)
    with pytest.raises(ValueError, match="oracle plan needs"):
        _interop.scripted_agent_plan(task, "oracle", ())
    assert _interop.scripted_agent_plan(task, "nop", ()).command is None
    full = _interop.scripted_agent_plan(task, "cheat", ())
    assert "--attacks" not in (full.command or "")


def test_harness_alias_registers_module() -> None:
    pytest.importorskip("verifiers.v1.tasksets.harbor")
    vf._ensure_harness_alias()
    import sys

    module = sys.modules[vf.VERIFIERS_HARNESS_ID]
    assert module.HARNESS_PLUGIN_ID == vf.VERIFIERS_HARNESS_ID
    assert "ScriptedHarness" in module.__all__


# -- harness config / workdir / image ------------------------------------------


def test_harness_config_round_trips_files() -> None:
    plan = _interop.ScriptedPlan(files={"/x/solve.sh": b"echo hi\n"}, command="bash /x/solve.sh")
    cfg = vf.harness_config(plan, "/app")
    assert cfg["id"] == vf.VERIFIERS_HARNESS_ID
    assert cfg["command"] == "bash /x/solve.sh"
    assert cfg["workdir"] == "/app"
    assert base64.b64decode(cfg["files_b64"]["/x/solve.sh"]) == b"echo hi\n"


def test_harness_config_nop_has_no_command() -> None:
    cfg = vf.harness_config(_interop.ScriptedPlan(files={}, command=None), "/")
    assert cfg["command"] is None and cfg["files_b64"] == {}
    assert cfg["workdir"] == "/"


def test_task_workdir_default_and_override(tmp_path: Path) -> None:
    assert _interop.task_workdir(_interop.load_harbor_task(make_task(tmp_path))) == "/"
    task = _interop.load_harbor_task(make_task(tmp_path / "w", workdir="/task"))
    assert _interop.task_workdir(task) == "/task"


def test_resolve_task_image_declared(tmp_path: Path) -> None:
    task_dir = make_task(tmp_path)
    config_path = task_dir / "task.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + 'docker_image = "example/img:1"\n',
        encoding="utf-8",
    )
    task = _interop.load_harbor_task(task_dir)
    assert vf.resolve_task_image(task) == ("example/img:1", "declared")


def test_agent_runtime_uses_declared_resources() -> None:
    declared = SimpleNamespace(cpu=2.0, memory=8.0)
    assert vf.agent_runtime("img:1", declared) == {
        "type": "docker",
        "cpu": 2.0,
        "memory": 8.0,
        "image": "img:1",
    }


def test_agent_runtime_defaults_when_undeclared() -> None:
    empty = SimpleNamespace(cpu=None, memory=None)
    assert vf.agent_runtime(None, empty) == {
        "type": "docker",
        "cpu": vf.AGENT_CPU,
        "memory": vf.AGENT_MEMORY_GB,
    }


def test_resolve_task_image_default_without_dockerfile(tmp_path: Path) -> None:
    task_dir = make_task(tmp_path)
    (task_dir / "environment" / "Dockerfile").unlink()
    task = _interop.load_harbor_task(task_dir)
    assert vf.resolve_task_image(task) == (None, "default")


def test_resolve_task_image_builds_dockerfile(tmp_path: Path, monkeypatch) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    image, how = vf.resolve_task_image(task)
    assert how == "built" and image == "evallab-vf/lab-synthetic:latest"
    assert calls[0][:3] == ["docker", "build", "-t"]


def test_resolve_task_image_failed_build_is_error(tmp_path: Path, monkeypatch) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="nope")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="docker build failed"):
        vf.resolve_task_image(task)


# -- verdict mapping ------------------------------------------------------------


def test_cell_from_trace_contract_keys() -> None:
    cell = vf.cell_from_trace(
        task_id="lab/synthetic",
        agent="oracle",
        attacks=(),
        reward=1.0,
        scored=True,
        isolation="shared",
        platform_version="verifiers 0.3.1",
        evidence="/tmp/cell",
    )
    assert cell == {
        "target": "verifiers",
        "agent": "oracle",
        "attacks": [],
        "verdict": "pass",
        "reward": 1.0,
        "reason": None,
        "platform_version": "verifiers 0.3.1",
        "evidence": "/tmp/cell",
        "task_id": "lab/synthetic",
        "isolation": "shared",
    }


def test_cell_from_trace_fail_and_unscored_is_error() -> None:
    failed = vf.cell_from_trace(
        task_id="t",
        agent="nop",
        attacks=(),
        reward=0.0,
        scored=True,
        isolation="shared",
        platform_version="v",
        evidence=None,
    )
    assert (failed["verdict"], failed["reward"]) == ("fail", 0.0)
    unscored = vf.cell_from_trace(
        task_id="t",
        agent="oracle",
        attacks=(),
        reward=None,
        scored=False,
        isolation="shared",
        platform_version="v",
        evidence=None,
        error="boom",
    )
    assert unscored["verdict"] == "error" and unscored["reason"] == "boom"


# -- run_cell guards (no docker, no verifiers) ----------------------------------


def test_run_cell_rejects_unknown_agent(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="oracle/nop/cheat"):
        vf.run_cell(tmp_path, "wizard", (), workdir=tmp_path / "cell", timeout_seconds=1)


def test_run_cell_rejects_unknown_isolation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="default/shared"):
        vf.run_cell(
            tmp_path,
            "nop",
            (),
            workdir=tmp_path / "cell",
            timeout_seconds=1,
            isolation="separate",
        )


def test_run_cell_passes_isolation_through(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(vf, "verifiers_available", lambda: True)
    monkeypatch.setattr(vf, "_docker_reachable", lambda: (True, "docker x"))
    seen: dict = {}

    async def fake_arun(*args, **kwargs):
        seen.update(kwargs)
        return {"verdict": "fail"}

    monkeypatch.setattr(vf, "_arun_cell", fake_arun)
    vf.run_cell(
        tmp_path,
        "nop",
        (),
        workdir=tmp_path / "cell",
        timeout_seconds=1,
        isolation="shared",
    )
    assert seen.get("isolation") == "shared"


def test_run_cell_without_verifiers_is_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(vf, "verifiers_available", lambda: False)
    cell = vf.run_cell(tmp_path, "nop", (), workdir=tmp_path / "cell", timeout_seconds=1)
    assert cell["verdict"] == "error"
    assert "not installed" in (cell["reason"] or "")


def test_run_cell_skipped_when_daemon_down(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(vf, "verifiers_available", lambda: True)
    monkeypatch.setattr(vf, "_docker_reachable", lambda: (False, "docker unreachable: down"))
    cell = vf.run_cell(tmp_path, "nop", (), workdir=tmp_path / "cell", timeout_seconds=1)
    assert cell["verdict"] == "skipped"
    assert cell["reason"] == "docker unreachable: down"


def test_run_cell_surfaces_driver_failure_as_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(vf, "verifiers_available", lambda: True)
    monkeypatch.setattr(vf, "_docker_reachable", lambda: (True, "docker x"))

    async def fake_arun(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(vf, "_arun_cell", fake_arun)
    cell = vf.run_cell(tmp_path, "nop", (), workdir=tmp_path / "cell", timeout_seconds=1)
    assert cell["verdict"] == "error"
    assert "RuntimeError: nope" in (cell["reason"] or "")


def test_meta_json_shape(tmp_path: Path) -> None:
    meta = {
        "task_id": "t",
        "agent": "oracle",
        "attacks": [],
        "image": "img",
        "image_source": "built",
        "isolation": "shared",
        "command": "bash /tmp/oracle-solve/solve.sh",
        "staged_files": ["/tmp/oracle-solve/solve.sh"],
        "platform_version": "verifiers 0.3.1",
        "tracked_rewards": ["solved"],
    }
    path = tmp_path / "meta.json"
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["tracked_rewards"] == ["solved"]
