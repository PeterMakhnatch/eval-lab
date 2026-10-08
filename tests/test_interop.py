"""HAR-204 interop converters, flags, and parity math (no docker, $0).

Live-Docker paths are admission-gated and tested through the skip branch only;
unit + fixture tests carry acceptance per the shared Docker note.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from evallab import interop
from evallab.interop import (
    export_inspect,
    export_karotte,
    karotte_flags,
    load_harbor_task,
    matrix_equal,
    parity_row,
    parse_reward_bytes,
    read_reward_file,
    render_matrix,
    run_harbor_control,
    run_inspect_oracle,
    validate_karotte,
    verdict_for_reward,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_TASK = REPO_ROOT / "library/tasks/transaction-reconciliation"


def make_task(
    root: Path,
    *,
    name: str = "lab/synthetic",
    network_mode: str | None = None,
    resources: dict | None = None,
    environment_mode: str | None = None,
    artifacts: list[str] | None = None,
    mcp_servers: list | None = None,
    solution_env: dict | None = None,
    test_sh: str = "echo 1 > /logs/verifier/reward.txt\n",
    metadata: dict | None = None,
) -> Path:
    task_dir = root / "task"
    (task_dir / "solution").mkdir(parents=True)
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "instruction.md").write_text("Do the thing.\n", encoding="utf-8")
    (task_dir / "solution" / "solve.sh").write_text("#!/bin/bash\necho solved\n", encoding="utf-8")
    (task_dir / "tests" / "test.sh").write_text("#!/bin/bash\n" + test_sh, encoding="utf-8")
    (task_dir / "environment" / "Dockerfile").write_text(
        "FROM python:3.12-slim-bookworm\n", encoding="utf-8"
    )
    lines = ["artifacts = []\n", "\n[task]\n", f'name = "{name}"\n']
    if artifacts:
        lines[0] = f"artifacts = {json.dumps(artifacts)}\n"
    lines += ["\n[metadata]\n", 'difficulty = "easy"\n']
    if metadata:
        for key, value in metadata.items():
            lines.append(f"{key} = {json.dumps(value)}\n")
    lines += ["\n[verifier]\n", "timeout_sec = 120.0\n"]
    if environment_mode:
        lines.append(f'environment_mode = "{environment_mode}"\n')
    lines += ["\n[environment]\n"]
    if network_mode:
        lines.append(f'network_mode = "{network_mode}"\n')
    if resources:
        for key, value in resources.items():
            lines.append(f"{key} = {value}\n")
    if mcp_servers:
        lines.append(f"mcp_servers = {json.dumps(mcp_servers)}\n")
    if solution_env:
        lines += ["\n[solution.env]\n"]
        for key, value in solution_env.items():
            lines.append(f"{key} = {json.dumps(value)}\n")
    (task_dir / "task.toml").write_text("".join(lines), encoding="utf-8")
    return task_dir


# -- reward parsing -----------------------------------------------------------


def test_reward_txt_wins_over_json(tmp_path: Path) -> None:
    assert parse_reward_bytes(b"1\n", b'{"score": 0}') == 1.0
    assert parse_reward_bytes(None, b'{"score": 0}') == 0.0
    assert parse_reward_bytes(None, b'{"reward": 1}') == 1.0


def test_reward_missing_is_error_not_fail() -> None:
    with pytest.raises(FileNotFoundError):
        parse_reward_bytes(None, None)


def test_reward_malformed_is_value_error() -> None:
    with pytest.raises(ValueError, match="not a number"):
        parse_reward_bytes(b"great\n", None)
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_reward_bytes(None, b"{nope")
    with pytest.raises(ValueError, match="no numeric"):
        parse_reward_bytes(None, b'{"verdict": "pass"}')


def test_read_reward_file_and_verdict_boundaries(tmp_path: Path) -> None:
    (tmp_path / "reward.txt").write_text("1\n", encoding="utf-8")
    assert read_reward_file(tmp_path) == 1.0
    assert verdict_for_reward(1.0) == "pass"
    assert verdict_for_reward(0.0) == "fail"
    assert verdict_for_reward(0.5) == "fail"
    with pytest.raises(FileNotFoundError):
        read_reward_file(tmp_path / "absent")


# -- task loading -------------------------------------------------------------


def test_load_errors_are_specific(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        load_harbor_task(tmp_path / "absent")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="instruction.md"):
        load_harbor_task(empty)
    (empty / "instruction.md").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="task.toml"):
        load_harbor_task(empty)
    (empty / "task.toml").write_text("[task]\nname = 'x'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="solve.sh"):
        load_harbor_task(empty)
    (empty / "solution").mkdir()
    (empty / "solution" / "solve.sh").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="test.sh"):
        load_harbor_task(empty)


def test_load_real_task() -> None:
    task = load_harbor_task(REAL_TASK)
    assert task.task_id == "petermakhnatch/transaction-reconciliation"
    assert "settlement" in task.instruction
    assert task.metadata.get("difficulty") == "easy"
    assert task.network_mode == "public"


# -- export-harbor --to inspect ------------------------------------------------


def _stub_inspect_ai(monkeypatch: pytest.MonkeyPatch) -> dict:
    recorded: dict = {}

    def _decorator(*args, **kwargs):
        def wrap(fn):
            return fn

        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return args[0]
        return wrap

    mod = types.ModuleType("inspect_ai")

    class Task:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

    mod.Task = Task
    mod.task = _decorator
    dataset = types.ModuleType("inspect_ai.dataset")

    class Sample:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    dataset.Sample = Sample
    solver_mod = types.ModuleType("inspect_ai.solver")
    solver_mod.Generate = type("Generate", (), {})
    solver_mod.Solver = type("Solver", (), {})
    solver_mod.TaskState = type("TaskState", (), {})
    solver_mod.solver = _decorator
    scorer_mod = types.ModuleType("inspect_ai.scorer")

    class Score:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    scorer_mod.Score = Score
    scorer_mod.Scorer = type("Scorer", (), {})
    scorer_mod.Target = type("Target", (), {})
    scorer_mod.scorer = _decorator
    util = types.ModuleType("inspect_ai.util")
    util.sandbox = lambda: None  # noqa: E731
    monkeypatch.setitem(sys.modules, "inspect_ai", mod)
    monkeypatch.setitem(sys.modules, "inspect_ai.dataset", dataset)
    monkeypatch.setitem(sys.modules, "inspect_ai.solver", solver_mod)
    monkeypatch.setitem(sys.modules, "inspect_ai.scorer", scorer_mod)
    monkeypatch.setitem(sys.modules, "inspect_ai.util", util)
    return recorded


def _exec_module(path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    namespace: dict = {"__file__": str(path), "__name__": "exported_under_test"}
    exec(path.read_text(encoding="utf-8"), namespace)  # noqa: S102
    return namespace


def test_export_inspect_executes_with_stub_framework(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "inspect-out"
    summary = export_inspect(REAL_TASK, out)
    assert (out / "task.py").is_file()
    assert (out / "compose.yaml").is_file()
    assert (out / "metadata.json").is_file()
    assert (out / "MAPPING.md").is_file()
    assert summary["task_id"] == "petermakhnatch/transaction-reconciliation"

    recorded = _stub_inspect_ai(monkeypatch)
    namespace = _exec_module(out / "task.py", monkeypatch)
    task_obj = namespace["harbor_task"]()
    assert task_obj is not None
    sample = recorded["dataset"][0]
    assert sample.input == (out / "instruction.md").read_text(encoding="utf-8")
    assert "settlement" in sample.input
    assert recorded["metadata"]["harbor_task"] == summary["task_id"]
    assert sample.metadata["difficulty"] == "easy"

    parse = namespace["parse_reward_bytes"]
    assert parse(b"1\n", None) == 1.0
    assert parse(None, b'{"score": 0}') == 0.0
    with pytest.raises(FileNotFoundError):
        parse(None, None)
    with pytest.raises(ValueError):
        parse(None, b"{}")


def test_export_inspect_copies_solution_helpers(tmp_path: Path) -> None:
    task_dir = make_task(tmp_path / "helpers")
    (task_dir / "solution" / "solve.py").write_text("HELPER = 1\n", encoding="utf-8")
    out = tmp_path / "o"
    export_inspect(task_dir, out)
    assert (out / "solution" / "solve.sh").is_file()
    assert (out / "solution" / "solve.py").read_text(encoding="utf-8") == "HELPER = 1\n"


def test_export_inspect_lossy_notes(tmp_path: Path) -> None:
    clean = make_task(tmp_path / "clean")
    assert export_inspect(clean, tmp_path / "o1")["lossy_notes"] == []
    lossy = make_task(tmp_path / "lossy", network_mode="public", environment_mode="separate")
    notes = export_inspect(lossy, tmp_path / "o2")["lossy_notes"]
    assert len(notes) == 2
    mapping = (tmp_path / "o2" / "MAPPING.md").read_text(encoding="utf-8")
    assert "missing file = error" in mapping


def test_export_refuses_nonempty_dir(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "junk.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        export_inspect(REAL_TASK, out)
    with pytest.raises(ValueError, match="non-empty"):
        export_karotte(REAL_TASK, out)


# -- export-harbor --to karotte -------------------------------------------------


def test_karotte_flags_cover_lossy_case(tmp_path: Path) -> None:
    task_dir = make_task(
        tmp_path / "lossy",
        network_mode="public",
        resources={"cpus": 2, "memory_mb": 4096},
        environment_mode="separate",
        mcp_servers=["server"],
        solution_env={"FOO": "bar"},
        test_sh="apt-get install -y curl\n",
    )
    (task_dir / "environment" / "docker-compose.yaml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    codes = {f["code"] for f in karotte_flags(load_harbor_task(task_dir))}
    assert codes == {
        "multi-service-compose",
        "verifier-as-root",
        "network-policy",
        "resource-rounding",
        "separate-verifier-image",
        "no-submission-paths",
        "mcp-servers",
        "solution-env",
    }
    out = tmp_path / "scaffold"
    summary = export_karotte(task_dir, out)
    assert set(summary["flags"]) == codes
    mapping = (out / "MAPPING.md").read_text(encoding="utf-8")
    for code in codes:
        assert code in mapping
    assert "student_data" in mapping and "root_data" in mapping


def test_karotte_flags_real_task() -> None:
    codes = {f["code"] for f in karotte_flags(load_harbor_task(REAL_TASK))}
    assert {"verifier-as-root", "network-policy", "no-submission-paths"} <= codes
    assert "multi-service-compose" not in codes
    assert "mcp-servers" not in codes
    assert "solution-env" not in codes


def _stub_karotte(monkeypatch: pytest.MonkeyPatch) -> None:
    karotte = types.ModuleType("karotte")

    class Task:
        def __init__(self, config):
            self.config = config

    class Step:
        def __init__(self, config):
            self.config = config

    karotte.Task = Task
    karotte.Step = Step
    judges = types.ModuleType("karotte.judges")
    judge_mod = types.ModuleType("karotte.judges.judge")

    class Judge:
        pass

    judge_mod.Judge = Judge
    exec_mod = types.ModuleType("karotte.judges.executable_judge")

    class ExecutableJudge(Judge):
        def __init__(self, args, continue_threshold=-1, cwd=None):
            self.subprocess_run_args = args

    exec_mod.ExecutableJudge = ExecutableJudge
    monkeypatch.setitem(sys.modules, "karotte", karotte)
    monkeypatch.setitem(sys.modules, "karotte.judges", judges)
    monkeypatch.setitem(sys.modules, "karotte.judges.judge", judge_mod)
    monkeypatch.setitem(sys.modules, "karotte.judges.executable_judge", exec_mod)
    karotte.judges = judges  # type: ignore[attr-defined]


def test_karotte_scaffold_executes_with_stub_framework(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "scaffold"
    export_karotte(REAL_TASK, out)
    assert (out / "Containerfile").is_file()
    assert "python:3.12-slim" in (out / "Containerfile").read_text(encoding="utf-8")

    _stub_karotte(monkeypatch)
    namespace = _exec_module(out / "environment" / "task.py", monkeypatch)
    task_cls = next(
        obj
        for obj in namespace.values()
        if isinstance(obj, type) and obj.__name__.endswith("Task") and obj.__name__ != "Task"
    )
    inst = task_cls(config=object())
    assert inst.system_prompt is None
    (step,) = inst.steps
    assert "settlement" in step.instructions
    assert type(step.judge).__name__ == "ExecutableJudge"
    assert "judge_entry.py" in " ".join(step.judge.subprocess_run_args)
    # No artifacts declared -> whole-workdir scoring, no submission paths.
    assert step.submission_paths is None


def test_karotte_scaffold_submission_paths_from_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = make_task(tmp_path / "t", artifacts=["/app/filter.py"])
    out = tmp_path / "scaffold"
    export_karotte(task_dir, out)
    _stub_karotte(monkeypatch)
    namespace = _exec_module(out / "environment" / "task.py", monkeypatch)
    task_cls = next(
        obj
        for obj in namespace.values()
        if isinstance(obj, type) and obj.__name__.endswith("Task") and obj.__name__ != "Task"
    )
    (step,) = task_cls(config=object()).steps
    assert [str(p) for p in step.submission_paths or []] == ["/app/filter.py"]


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_judge_entry_reward_conversion(tmp_path: Path) -> None:
    out = tmp_path / "scaffold"
    export_karotte(REAL_TASK, out)
    judge = _load_module(out / "environment" / "judge_entry.py", "judge_entry")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "reward.txt").write_text("0\n", encoding="utf-8")
    reward, error = judge.read_reward(logs)
    assert (reward, error) == (0.0, None)
    (logs / "reward.txt").unlink()
    (logs / "reward.json").write_text('{"score": 1}', encoding="utf-8")
    assert judge.read_reward(logs) == (1.0, None)
    (logs / "reward.json").unlink()
    reward, error = judge.read_reward(logs)
    assert reward is None and error is not None


def test_validate_karotte_scaffold(tmp_path: Path) -> None:
    out = tmp_path / "scaffold"
    export_karotte(REAL_TASK, out)
    result = validate_karotte(out / "environment")
    assert result["verdict"] == "pass", result.get("reason")
    assert result["karotte"] == interop.KAROTTE_PIN
    assert result["report"]["steps"][0]["judge"] == "ExecutableJudge"
    assert "validate-only" in result["note"]


def test_validate_karotte_missing_task() -> None:
    result = validate_karotte(REPO_ROOT / "does-not-exist")
    assert result["verdict"] == "error"


# -- parity math ----------------------------------------------------------------


def test_matrix_equal_boundaries() -> None:
    assert matrix_equal({"a": "pass", "b": "pass"}) is True
    assert matrix_equal({"a": "pass", "b": "fail"}) is False
    assert matrix_equal({"a": "pass"}) is None
    assert matrix_equal({}) is None
    assert matrix_equal({"a": "skip", "b": "error"}) is None
    # Skips and errors carry no signal; comparable verdicts still decide.
    assert matrix_equal({"a": "pass", "b": "skip", "c": "error"}) is None
    assert matrix_equal({"a": "pass", "b": "pass", "c": "skip"}) is True
    assert matrix_equal({"a": "pass", "b": "fail", "c": "skip"}) is False


def test_render_matrix_has_karotte_column() -> None:
    text = render_matrix(
        [
            {
                "task_id": "lab/t",
                "cells": {
                    "harbor:oracle": "pass",
                    "harbor:nop": "fail",
                    "inspect:oracle": "pass",
                    "karotte-validate": "pass",
                },
                "equal": False,
            }
        ]
    )
    assert "karotte-validate" in text
    assert "lab/t" in text
    assert "False" in text


# -- admission-gated skip paths --------------------------------------------------


def test_live_paths_skip_when_daemon_not_admitted(tmp_path: Path) -> None:
    with patch.object(
        interop, "docker_admission", return_value=(False, "shared daemon not admitted: test")
    ):
        harbor_result = run_harbor_control(REAL_TASK, "oracle", repo_root=REPO_ROOT)
        assert harbor_result["verdict"] == "skip"
        assert "shared daemon not admitted" in harbor_result["reason"]
        assert "harbor_rev" in harbor_result

        inspect_result = run_inspect_oracle(REAL_TASK)
        assert inspect_result["verdict"] == "skip"
        assert "shared daemon not admitted" in inspect_result["reason"]
        assert inspect_result["inspect_harbor"] == interop.INSPECT_HARBOR_PIN


def test_parity_row_reports_skips_without_docker(tmp_path: Path) -> None:
    refused = (False, "shared daemon not admitted: test")
    with (
        patch.object(interop, "docker_admission", return_value=refused),
        patch.object(
            interop,
            "validate_karotte",
            return_value={"target": "karotte-validate", "verdict": "pass"},
        ),
    ):
        row = parity_row(REAL_TASK, repo_root=REPO_ROOT, workdir=tmp_path / "work")
    assert row["cells"]["harbor:oracle"] == "skip"
    assert row["cells"]["harbor:nop"] == "skip"
    assert row["cells"]["inspect:oracle"] == "skip"
    assert row["cells"]["karotte-validate"] == "pass"
    assert row["equal"] is None
    assert "harbor_rev" in row


# -- CLI wiring -----------------------------------------------------------------


def test_cli_wiring() -> None:
    from evallab.interop import build_interop_parser

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers()
    build_interop_parser(commands)
    args = parser.parse_args(
        ["interop", "export-harbor", "library/tasks/x", "--to", "karotte", "--out", "o"]
    )
    assert args.to == "karotte"
    assert callable(args.func)
    args = parser.parse_args(["interop", "parity", "a", "b", "--targets", "harbor"])
    assert args.targets == ["harbor"]
    assert callable(args.func)


def test_parity_all_skip_prints_not_agreement_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab.interop import build_interop_parser, interop_command

    parser = argparse.ArgumentParser()
    build_interop_parser(parser.add_subparsers())
    args = parser.parse_args(["interop", "parity", "some-task"])
    row = {
        "task_id": "lab/t",
        "cells": {"harbor:oracle": "skip"},
        "equal": None,
        "harbor_rev": "test-rev",
        "details": {"harbor:oracle": {"verdict": "skip", "reason": "no daemon"}},
    }
    with patch.object(interop, "parity_row", return_value=row):
        assert interop_command(args, REPO_ROOT) == 0
    assert "not agreement" in capsys.readouterr().out
