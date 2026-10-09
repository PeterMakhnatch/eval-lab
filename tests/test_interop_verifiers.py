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
    (task_dir / "solution" / "solve.sh").write_text(
        "#!/bin/bash\necho solved\n", encoding="utf-8"
    )
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


def load_plan_fallback(task, agent, attacks):
    # Call the fallback directly: it stays unit-testable after
    # interop.scripted_agent_plan lands (load_plan then prefers the factory).
    return vf._fallback_plan(task, agent, tuple(attacks))


# -- plans --------------------------------------------------------------------


def test_plan_oracle_stages_solution(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    plan = load_plan_fallback(task, "oracle", ())
    assert plan.command == f"bash {vf.ORACLE_STAGE_DIR}/solve.sh"
    assert plan.files[f"{vf.ORACLE_STAGE_DIR}/solve.sh"] == b"#!/bin/bash\necho solved\n"


def test_plan_nop_is_empty(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    plan = load_plan_fallback(task, "oracle", ())
    nop = load_plan_fallback(task, "nop", ())
    assert nop.files == {} and nop.command is None
    assert plan.files and plan.command


def test_plan_cheat_stages_ladder_with_subset(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    plan = load_plan_fallback(task, "cheat", ("reward_plant", "skip_plant"))
    assert set(plan.files) == {vf.CHEAT_STAGE_PATH}
    assert b"CHEAT_LADDER_RESULT=" in plan.files[vf.CHEAT_STAGE_PATH]
    assert "--attacks reward_plant,skip_plant" in (plan.command or "")
    assert "--cwd /app" in (plan.command or "")
    full = load_plan_fallback(task, "cheat", ())
    assert "--attacks" not in (full.command or "")


def test_plan_cheat_rejects_unknown_attack(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    with pytest.raises(ValueError, match="unknown cheat attacks"):
        load_plan_fallback(task, "cheat", ("nope",))


def test_plan_rejects_unknown_agent(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    with pytest.raises(ValueError, match="oracle/nop/cheat"):
        load_plan_fallback(task, "wizard", ())


def test_load_plan_prefers_interop_factory(tmp_path: Path, monkeypatch) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    sentinel = object()
    monkeypatch.setattr(
        _interop, "scripted_agent_plan", lambda t, a, s: sentinel, raising=False
    )
    assert vf.load_plan(task, "nop", ()) is sentinel


def test_harness_alias_registers_module() -> None:
    pytest.importorskip("verifiers.v1.tasksets.harbor")
    vf._ensure_harness_alias()
    import sys

    module = sys.modules[vf.VERIFIERS_HARNESS_ID]
    assert module.HARNESS_PLUGIN_ID == vf.VERIFIERS_HARNESS_ID
    assert "ScriptedHarness" in module.__all__



# -- harness config / workdir / image ------------------------------------------


def test_harness_config_round_trips_files() -> None:
    plan = vf.Plan(files={"/x/solve.sh": b"echo hi\n"}, command="bash /x/solve.sh")
    cfg = vf.harness_config(plan)
    assert cfg["id"] == vf.VERIFIERS_HARNESS_ID
    assert cfg["command"] == "bash /x/solve.sh"
    assert base64.b64decode(cfg["files_b64"]["/x/solve.sh"]) == b"echo hi\n"


def test_harness_config_nop_has_no_command() -> None:
    cfg = vf.harness_config(vf.Plan(files={}, command=None))
    assert cfg["command"] is None and cfg["files_b64"] == {}


def test_container_workdir_default_and_override(tmp_path: Path) -> None:
    assert vf.container_workdir(make_task(tmp_path)) == "/app"
    assert vf.container_workdir(make_task(tmp_path / "w", workdir="/task")) == "/task"


def test_resolve_task_image_declared(tmp_path: Path) -> None:
    task_dir = make_task(tmp_path)
    config_path = task_dir / "task.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + 'docker_image = "example/img:1"\n',
        encoding="utf-8",
    )
    task = _interop.load_harbor_task(task_dir)
    assert vf.resolve_task_image(task) == ("example/img:1", "declared")


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
