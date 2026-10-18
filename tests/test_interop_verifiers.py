"""Deterministic verifier interop boundaries; Docker state is always injected.

Parser/admission cases use the real opt-in verifiers dependency when installed,
without provisioning a runtime or calling a model.
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


def make_task(
    root: Path,
    *,
    name: str = "lab/synthetic",
    workdir: str | None = None,
    environment: str = "",
    verifier: str = "",
    agent: str = "",
    network: str = "public",
    artifacts: str = "[]",
    preamble: str = "",
) -> Path:
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
        f"artifacts = {artifacts}\n",
        preamble + "\n",
        "\n[task]\n",
        f'name = "{name}"\n',
        "\n[metadata]\n",
        'difficulty = "easy"\n',
        "\n[verifier]\n",
        "timeout_sec = 120.0\n",
        verifier + "\n",
        "\n[environment]\n",
        f'network_mode = "{network}"\n',
        environment + "\n",
    ]
    if workdir is not None:
        lines.append(f'workdir = "{workdir}"\n')
    if agent:
        lines.extend(("\n[agent]\n", agent + "\n"))
    (task_dir / "task.toml").write_text("".join(lines), encoding="utf-8")
    return task_dir


# -- plans (single factory in interop; this runner only materializes) -----------


def test_factory_oracle_runs_solve_in_workdir(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    plan = _interop.scripted_agent_plan(task, "oracle", ())
    assert plan.command == "bash /solution/solve.sh"
    assert plan.files == {}


def test_materialize_oracle_uploads_solution(tmp_path: Path) -> None:
    task = _interop.load_harbor_task(make_task(tmp_path))
    plan = _interop.scripted_agent_plan(task, "oracle", ())
    files = vf.materialize_files(task, "oracle", plan)
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


@pytest.mark.parametrize("missing_parent", ["verifiers", "verifiers.v1"])
def test_missing_nested_dependency_is_recorded_error(
    tmp_path: Path, monkeypatch, missing_parent: str
) -> None:
    def absent(_name: str):
        raise ModuleNotFoundError(f"No module named {missing_parent!r}", name=missing_parent)

    def forbidden(*args, **kwargs):
        pytest.fail("dependency absence must not contact Docker")

    monkeypatch.setattr(vf.importlib.util, "find_spec", absent)
    monkeypatch.setattr(subprocess, "run", forbidden)
    workdir = tmp_path / "cell"
    cell = vf.run_cell(tmp_path, "nop", (), workdir=workdir, timeout_seconds=1)
    assert (cell["verdict"], cell["reward"], cell["evidence"]) == ("error", None, str(workdir))
    assert "not installed" in cell["reason"]
    assert json.loads((workdir / "error.json").read_text())["reason"] == cell["reason"]


@pytest.mark.parametrize(
    ("task_options", "extra_file", "reason"),
    [
        ({"verifier": "user = 0"}, None, "[verifier].user"),
        ({"agent": 'user = "student"'}, None, "[agent].user"),
        ({}, "environment/docker-compose.yaml", "single-image"),
        (
            {"verifier": 'environment_mode = "separate"'},
            "tests/Dockerfile",
            "tests/Dockerfile",
        ),
        (
            {"verifier": "[verifier.environment]\ncpus = 2"},
            None,
            "without docker_image",
        ),
        ({"environment": "storage_mb = 1024"}, None, "disk limits are not enforced"),
        ({"environment": 'os = "windows"'}, None, "Linux containers only"),
        ({"environment": 'gpu_types = ["A100"]'}, None, "gpu_types"),
        ({"agent": 'network_mode = "no-network"'}, None, "shared verifier phase network"),
        (
            {"network": "allowlist", "environment": 'allowed_hosts = ["10.0.0.0/8"]'},
            None,
            "IP CIDR/IPv6",
        ),
        (
            {"verifier": 'collect = [{command = "true", user = 0}]'},
            None,
            "[[verifier.collect]].user",
        ),
        (
            {"artifacts": '[{source = "/db", service = "db"}]'},
            None,
            "artifact additional service",
        ),
        ({"preamble": 'steps = [{name = "one"}]'}, None, "multi-step"),
    ],
)
def test_incompatible_contract_is_error_before_docker(
    tmp_path: Path, monkeypatch, task_options: dict, extra_file: str | None, reason: str
) -> None:
    task_dir = make_task(tmp_path, **task_options)
    if extra_file is not None:
        (task_dir / extra_file).write_text("unsupported\n")

    def forbidden(*args, **kwargs):
        pytest.fail("an incompatible contract must not build, pull, or start Docker")

    monkeypatch.setattr(vf, "verifiers_available", lambda: True)
    monkeypatch.setattr(subprocess, "run", forbidden)
    workdir = tmp_path / "cell"
    cell = vf.run_cell(task_dir, "nop", (), workdir=workdir, timeout_seconds=1)
    assert (cell["verdict"], cell["reward"], cell["evidence"]) == ("error", None, str(workdir))
    assert reason in cell["reason"]
    assert json.loads((workdir / "error.json").read_text())["verdict"] == "error"
    assert not (workdir / "admission.json").exists()


@pytest.mark.parametrize("isolation", ["default", "shared"])
def test_isolation_override_does_not_hide_verifier_user(
    tmp_path: Path, monkeypatch, isolation: str
) -> None:
    task_dir = make_task(tmp_path, verifier='environment_mode = "separate"\nuser = 0')

    def forbidden(*args, **kwargs):
        pytest.fail("an isolation override must not bypass unsupported verifier users")

    monkeypatch.setattr(vf, "verifiers_available", lambda: True)
    monkeypatch.setattr(subprocess, "run", forbidden)
    workdir = tmp_path / "cell"
    cell = vf.run_cell(
        task_dir, "nop", (), workdir=workdir, timeout_seconds=1, isolation=isolation
    )
    assert cell["verdict"] == "error" and cell["reward"] is None
    assert "[verifier].user" in cell["reason"]
    assert json.loads((workdir / "error.json").read_text())["isolation_requested"] == isolation


@pytest.mark.parametrize(
    ("verifier", "isolation", "sandboxes", "cpus", "memory_mb"),
    [
        ("", "default", 1, 2, 2048),
        ('environment_mode = "separate"', "default", 2, 4, 4096),
        ('environment_mode = "separate"', "shared", 1, 2, 2048),
        (
            '[verifier.environment]\ndocker_image = "verifier:1"\ncpus = 6\nmemory_mb = 8192',
            "default",
            2,
            12,
            16384,
        ),
        (
            '[verifier.environment]\ndocker_image = "verifier:1"\ncpus = 6\nmemory_mb = 8192',
            "shared",
            1,
            2,
            2048,
        ),
    ],
)
def test_real_parser_admission_refuses_before_image_build(
    tmp_path: Path,
    monkeypatch,
    verifier: str,
    isolation: str,
    sandboxes: int,
    cpus: int,
    memory_mb: int,
) -> None:
    pytest.importorskip("verifiers.v1.tasksets.harbor")
    from evallab import campaign_execution

    task_dir = make_task(tmp_path, verifier=verifier)

    def forbidden(*args, **kwargs):
        pytest.fail("refused admission must not build, pull, or start Docker")

    monkeypatch.setattr(campaign_execution, "docker_available_resources", lambda: (1.0, 1024))
    monkeypatch.setattr(subprocess, "run", forbidden)
    workdir = tmp_path / "cell"
    cell = vf.run_cell(
        task_dir, "nop", (), workdir=workdir, timeout_seconds=1, isolation=isolation
    )
    assert (cell["verdict"], cell["reward"], cell["evidence"]) == ("error", None, str(workdir))
    assert "admission refused" in cell["reason"]
    admission = json.loads((workdir / "admission.json").read_text())
    assert admission["admitted"] is False and admission["sandboxes"] == sandboxes
    assert admission["requested"] == {"cpus": cpus, "memory_mb": memory_mb}
    preflight = json.loads((workdir / "preflight.json").read_text())
    assert preflight["isolation"] == ("separate" if sandboxes == 2 else "shared")
    assert preflight["isolation_requested"] == isolation
    assert bool(preflight["isolation_override"]) == (isolation == "shared")


def test_unreachable_daemon_is_admission_error_with_evidence(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("verifiers.v1.tasksets.harbor")
    from evallab import campaign_execution

    task_dir = make_task(tmp_path)

    def unavailable():
        raise ValueError("docker unreachable: down")

    def forbidden(*args, **kwargs):
        pytest.fail("unreachable Docker must not build, pull, or start")

    monkeypatch.setattr(campaign_execution, "docker_available_resources", unavailable)
    monkeypatch.setattr(subprocess, "run", forbidden)
    workdir = tmp_path / "cell"
    cell = vf.run_cell(task_dir, "nop", (), workdir=workdir, timeout_seconds=1)
    assert (cell["verdict"], cell["reward"], cell["evidence"]) == ("error", None, str(workdir))
    assert "docker unreachable: down" in cell["reason"]
    admission = json.loads((workdir / "admission.json").read_text())
    assert admission["admitted"] is False
    assert "docker unreachable: down" in admission["reason"]


def test_admitted_supported_task_reports_image_build_failure(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("verifiers.v1.tasksets.harbor")
    from evallab import campaign_execution

    task_dir = make_task(tmp_path, environment="cpus = 3\nmemory_mb = 3072")

    def failed_build(*args, **kwargs):
        raise OSError("synthetic image builder unavailable")

    monkeypatch.setattr(campaign_execution, "docker_available_resources", lambda: (8.0, 8192))
    monkeypatch.setattr(subprocess, "run", failed_build)
    workdir = tmp_path / "cell"
    cell = vf.run_cell(task_dir, "oracle", (), workdir=workdir, timeout_seconds=1)
    assert (cell["verdict"], cell["reward"], cell["evidence"]) == ("error", None, str(workdir))
    assert "docker build failed" in cell["reason"]
    admission = json.loads((workdir / "admission.json").read_text())
    assert admission["admitted"] is True
    assert admission["requested"] == {"cpus": 3, "memory_mb": 3072}
