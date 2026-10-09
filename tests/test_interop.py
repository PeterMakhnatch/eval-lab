"""HAR-204 interop converters, plans, cells, and grading matrix (no docker, $0).

Live-Docker cells attempt execution like ``evallab run`` (no admission gate);
unit + fixture tests inject every external seam (control runner, cheat lane,
uv binary, target registry) per the deterministic-test rule.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from evallab import interop
from evallab.interop import (
    MATRIX_TARGETS,
    export_inspect,
    export_karotte,
    grade_task_row,
    karotte_flags,
    load_harbor_task,
    matrix_task_row,
    parse_reward_bytes,
    read_reward_file,
    render_grading_matrix,
    run_harbor_cell,
    run_inspect_cell,
    scripted_agent_plan,
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
    workdir: str | None = None,
    docker_image: str | None = None,
    with_setup: bool = False,
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
    if workdir:
        lines.append(f'workdir = "{workdir}"\n')
    if docker_image:
        lines.append(f'docker_image = "{docker_image}"\n')
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
    if with_setup:
        setup_dir = task_dir / "environment" / "setup"
        setup_dir.mkdir(parents=True, exist_ok=True)
        (setup_dir / "setup.sh").write_text("#!/bin/bash\necho setup\n", encoding="utf-8")
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
# The export writes the same runnable env tree run_cell assembles (offline,
# deterministic: no karotte import, no uv lock, no docker).


def test_karotte_flags_cover_lossy_case(tmp_path: Path) -> None:
    task_dir = make_task(
        tmp_path / "lossy",
        network_mode="public",
        resources={"cpus": 2, "memory_mb": 4096},
        environment_mode="separate",
        mcp_servers=["server"],
        solution_env={"FOO": "bar"},
        test_sh="apt-get install -y curl\n",
        workdir="/app",
    )
    codes = {f["code"] for f in karotte_flags(load_harbor_task(task_dir))}
    assert codes == {
        "network-policy",
        "resource-rounding",
        "separate-verifier-image",
        "mcp-servers",
        "solution-env",
    }
    compose_dir = make_task(tmp_path / "composed", workdir="/app")
    (compose_dir / "environment" / "docker-compose.yaml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    assert "multi-service-compose" in {
        f["code"] for f in karotte_flags(load_harbor_task(compose_dir))
    }
    with pytest.raises(ValueError, match="multi-service compose"):
        export_karotte(compose_dir, tmp_path / "composed-out")
    out = tmp_path / "env"
    summary = export_karotte(task_dir, out)
    assert set(summary["flags"]) == codes
    mapping = (out / "MAPPING.md").read_text(encoding="utf-8")
    for code in codes:
        assert code in mapping
    assert "uv lock" in mapping and "nop" in mapping


def test_karotte_flags_real_task() -> None:
    codes = {f["code"] for f in karotte_flags(load_harbor_task(REAL_TASK))}
    assert codes == {"network-policy"}


def test_export_karotte_writes_runnable_tree(tmp_path: Path) -> None:
    out = tmp_path / "env"
    summary = export_karotte(REAL_TASK, out)
    assert summary["target"] == "karotte"
    assert summary["karotte_id"] == "petermakhnatch-transaction-reconciliation"
    digest = summary["base"].removeprefix("evallab-harbor-")
    assert len(digest) == 16 and all(c in "0123456789abcdef" for c in digest)
    containerfile = (out / "Containerfile").read_text(encoding="utf-8")
    assert f"FROM {summary['base']}" in containerfile
    assert f"karotte=={interop.KAROTTE_PIN}" in containerfile
    assert "chown -R student:student '/app'" in containerfile
    pkg = out / "src" / "environment" / "tasks" / "petermakhnatch_transaction_reconciliation"
    assert (pkg / "__init__.py").is_file()
    assert (pkg / "scoring_script.py").is_file()
    assert (out / "src" / "environment" / "fake_model.py").is_file()
    assert (out / "root_data" / "tests" / "test.sh").is_file()
    assert (out / "root_data" / "tests" / "test_outputs.py").is_file()
    assert f'"karotte=={interop.KAROTTE_PIN}"' in (out / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert not (out / "uv.lock").exists()
    for support in (
        "__init__.py",
        "paths.py",
        "submissions.py",
        "system_prompts.py",
        "check_permissions.py",
    ):
        assert (out / "src" / "environment" / support).is_file()
    assert (out / "src" / "environment" / "tasks" / "__init__.py").is_file()
    assert (out / "MAPPING.md").is_file()
    assert "MAPPING.md" in summary["files"]


def test_export_karotte_docker_image_base_and_setup(tmp_path: Path) -> None:
    task_dir = make_task(
        tmp_path / "img",
        workdir="/testbed",
        docker_image="example.com/img@sha256:abc",
        with_setup=True,
    )
    out = tmp_path / "env"
    summary = export_karotte(task_dir, out)
    assert summary["base"] == "example.com/img@sha256:abc"
    containerfile = (out / "Containerfile").read_text(encoding="utf-8")
    assert "FROM example.com/img@sha256:abc" in containerfile
    assert "setup.sh" in containerfile
    assert (out / "root_data" / "setup" / "setup.sh").is_file()


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
            self.continue_threshold = continue_threshold

    exec_mod.ExecutableJudge = ExecutableJudge
    monkeypatch.setitem(sys.modules, "karotte", karotte)
    monkeypatch.setitem(sys.modules, "karotte.judges", judges)
    monkeypatch.setitem(sys.modules, "karotte.judges.judge", judge_mod)
    monkeypatch.setitem(sys.modules, "karotte.judges.executable_judge", exec_mod)
    karotte.judges = judges  # type: ignore[attr-defined]


def test_exported_task_module_behavior(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "env"
    export_karotte(REAL_TASK, out)
    _stub_karotte(monkeypatch)
    collected: dict = {}
    env_pkg = types.ModuleType("environment")
    submissions = types.ModuleType("environment.submissions")

    def collect(config, paths):
        collected["config"] = config
        collected["paths"] = tuple(str(p) for p in paths)
        return tuple(Path(f"/saved/{p.name}") for p in paths)

    submissions.collect_submission = collect
    prompts = types.ModuleType("environment.system_prompts")
    prompts.get_system_prompt = lambda model, extra: "sys"
    monkeypatch.setitem(sys.modules, "environment", env_pkg)
    monkeypatch.setitem(sys.modules, "environment.submissions", submissions)
    monkeypatch.setitem(sys.modules, "environment.system_prompts", prompts)
    pkg = out / "src" / "environment" / "tasks" / "petermakhnatch_transaction_reconciliation"
    namespace = _exec_module(pkg / "__init__.py", monkeypatch)
    task_cls = next(
        obj
        for obj in namespace.values()
        if isinstance(obj, type) and obj.__name__.endswith("Task") and obj.__name__ != "Task"
    )
    assert task_cls.id == "petermakhnatch-transaction-reconciliation"
    config = types.SimpleNamespace(model="fake/fake", extra_config=None)
    inst = task_cls(config=config)
    assert inst.system_prompt == "sys"
    (step,) = inst.steps
    assert "settlement" in step.instructions
    assert [str(p) for p in step.submission_paths] == ["/app"]
    step.pre_scoring_hook()
    assert collected["config"] is config
    assert collected["paths"] == ("/app",)
    judge = step.judge
    assert type(judge).__name__ == "ExecutableJudge"
    args = judge.subprocess_run_args
    assert args[1:3] == [
        "-m",
        "environment.tasks.petermakhnatch_transaction_reconciliation.scoring_script",
    ]
    assert args[3] == "300.0" and args[4] == "/app"
    assert args[5:7] == ["/app", "/saved/app"] and args[-1] == "score_output.json"
    assert judge.continue_threshold == 1.0


def test_exported_scoring_restores_and_grades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.interop_karotte import _render_scoring

    namespace: dict = {"__name__": "scoring_under_test"}
    exec(compile(_render_scoring(), "scoring_script.py", "exec"), namespace)  # noqa: S102
    work = tmp_path / "work"
    work.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    rtests = tmp_path / "rtests"
    rtests.mkdir()
    staged = tmp_path / "staged"
    staged.mkdir()
    namespace["TESTS_SRC"] = rtests
    namespace["TESTS_DST"] = staged / "tests"
    namespace["LOG_DIR"] = logs
    orig = work / "answer.txt"
    saved = tmp_path / "saved.txt"
    saved.write_text("graded-bytes\n", encoding="utf-8")
    (rtests / "test.sh").write_text(
        "#!/bin/bash\ncat answer.txt > /dev/null && echo 1 > " + str(logs / "reward.txt") + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "score.json"
    monkeypatch.setattr(sys, "argv", ["scoring", "60", str(work), str(orig), str(saved), str(out)])
    assert namespace["main"]() == 0
    assert orig.read_text(encoding="utf-8") == "graded-bytes\n"
    assert json.loads(out.read_text(encoding="utf-8"))["score"] == 1.0
    (logs / "reward.txt").unlink()
    (rtests / "test.sh").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["scoring", "60", str(work), str(work / "missing.txt"), str(tmp_path / "nope"), str(out)],
    )
    assert namespace["main"]() == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["score"] == 0.0
    assert payload["metadata"]["missing_submissions"] == [str(work / "missing.txt")]
    assert "error" in payload["metadata"]


def _exec_fake_model(path: Path) -> list[dict]:
    calls: list[dict] = []

    class Function:
        def __init__(self, name=None, arguments=""):
            self.name = name
            self.arguments = arguments

    class ToolCall:
        def __init__(self, id=None, type=None, function=None):
            self.id = id
            self.type = type
            self.function = function

    class Message:
        def __init__(self, role="assistant", content=None, tool_calls=None, **_kw):
            calls.append({"role": role, "content": content, "tool_calls": tool_calls or []})

    schemas = types.ModuleType("karotte.schemas")
    schemas.ChatCompletionMessageToolCall = ToolCall
    schemas.Function = Function
    schemas.Message = Message
    karotte = types.ModuleType("karotte")
    karotte.schemas = schemas
    namespace: dict = {}
    old = {k: sys.modules.get(k) for k in ("karotte", "karotte.schemas")}
    sys.modules["karotte"] = karotte
    sys.modules["karotte.schemas"] = schemas
    try:
        exec(compile(path.read_text(encoding="utf-8"), "fake_model.py", "exec"), namespace)  # noqa: S102
        namespace["get_messages"](object())
        return calls
    finally:
        for key, mod in old.items():
            if mod is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = mod


def test_exported_fake_model_is_nop(tmp_path: Path) -> None:
    out = tmp_path / "env"
    export_karotte(REAL_TASK, out)
    messages = _exec_fake_model(out / "src" / "environment" / "fake_model.py")
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant" and messages[0]["tool_calls"] == []


# -- scripted agent plans -------------------------------------------------------


def test_scripted_agent_plan_oracle_nop_cheat() -> None:
    task = load_harbor_task(REAL_TASK)
    oracle = scripted_agent_plan(task, "oracle", ())
    assert oracle.files == {}
    assert oracle.command == "bash solution/solve.sh"
    nop = scripted_agent_plan(task, "nop", ())
    assert nop.files == {} and nop.command is None
    cheat = scripted_agent_plan(task, "cheat", ("reward_plant", "skip_plant"))
    assert list(cheat.files) == [interop.CHEAT_LADDER_CONTAINER_PATH]
    assert (
        cheat.files[interop.CHEAT_LADDER_CONTAINER_PATH]
        == (REPO_ROOT / "src/evallab/cheat_ladder.py").read_bytes()
    )
    assert "--attacks reward_plant,skip_plant" in (cheat.command or "")
    full = scripted_agent_plan(task, "cheat", ())
    assert "--attacks" not in (full.command or "")


def test_scripted_agent_plan_rejects_unknown() -> None:
    task = load_harbor_task(REAL_TASK)
    with pytest.raises(ValueError, match="unknown scripted agent"):
        scripted_agent_plan(task, "codex", ())
    with pytest.raises(ValueError, match="unknown cheat attack"):
        scripted_agent_plan(task, "cheat", ("nope",))


# -- inspect staging (no docker) -------------------------------------------------


def test_write_inspect_run_stages_manifest_and_blobs(tmp_path: Path) -> None:
    from evallab.interop import _write_inspect_run

    task = load_harbor_task(REAL_TASK)
    work = tmp_path / "cell"
    driver = _write_inspect_run(
        work,
        task,
        agent="cheat",
        attacks=("skip_plant",),
        agent_timeout=60,
        override_cpus=2,
        override_memory_mb=2048,
    )
    assert driver.is_file()
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["agent"] == "cheat"
    assert manifest["attacks"] == ["skip_plant"]
    assert "--attacks skip_plant" in manifest["command"]
    assert manifest["workdir"] == interop.INSPECT_TASK_WORKDIR
    assert "solution/solve.sh" in manifest["task_files"]
    assert "tests/test.sh" in manifest["task_files"]
    assert manifest["skipped_large"] == []
    assert manifest["override_cpus"] == 2
    assert manifest["override_memory_mb"] == 2048
    ((container_path, blob),) = manifest["files"]
    assert container_path == interop.CHEAT_LADDER_CONTAINER_PATH
    assert (work / blob).read_bytes() == (REPO_ROOT / "src/evallab/cheat_ladder.py").read_bytes()
    source = driver.read_text(encoding="utf-8")
    assert "scripted_solver" in source
    assert "write_file" in source
    assert ".exec(" in source
    assert "mockllm/model" in source


def test_write_inspect_run_nop_stages_nothing(tmp_path: Path) -> None:
    from evallab.interop import _write_inspect_run

    task = load_harbor_task(REAL_TASK)
    work = tmp_path / "cell"
    _write_inspect_run(
        work,
        task,
        agent="nop",
        attacks=(),
        agent_timeout=60,
        override_cpus=2,
        override_memory_mb=2048,
    )
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["command"] is None
    assert manifest["files"] == []
    assert manifest["task_files"] == []


def test_run_inspect_cell_without_uv_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    monkeypatch.setattr(interop, "_docker_ps", lambda: {})
    cell = run_inspect_cell(REAL_TASK, "oracle", (), workdir=tmp_path / "w")
    assert cell["target"] == "inspect"
    assert cell["verdict"] == "error"
    assert "uv" in (cell["reason"] or "")
    assert cell["evidence"] == str(tmp_path / "w")


def test_run_inspect_cell_rejects_unknown_agent_and_attack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = run_inspect_cell(REAL_TASK, "codex", (), workdir=tmp_path / "a")
    assert cell["verdict"] == "error"
    cell = run_inspect_cell(REAL_TASK, "cheat", ("nope",), workdir=tmp_path / "c")
    assert cell["verdict"] == "error"
    assert "cannot build cheat plan" in (cell["reason"] or "")


def test_run_inspect_cell_control_ignores_attacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def fake_stage(work: Path, task: object, **kwargs: object) -> Path:
        captured.update(kwargs)
        driver = Path(work) / "inspect_driver.py"
        driver.write_text("x", encoding="utf-8")
        return driver

    monkeypatch.setattr(interop, "_write_inspect_run", fake_stage)
    monkeypatch.setattr(interop, "_docker_ps", lambda: {})
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    cell = run_inspect_cell(REAL_TASK, "oracle", ("skip_plant",), workdir=tmp_path / "b")
    assert captured["attacks"] == ()
    assert cell["attacks"] == []


# -- harbor cells (seams injected) ----------------------------------------------


def _control_result(verdict: str, reward: float | None, job: str = "job-x") -> dict:
    return {
        "target": "harbor",
        "agent": "oracle",
        "task_id": "t",
        "verdict": verdict,
        "reward": reward,
        "reason": None,
        "job_dir": job,
        "harbor_rev": "rev",
    }


def test_run_harbor_cell_oracle_delegates_to_control(tmp_path: Path) -> None:
    with (
        patch.object(interop, "run_harbor_control", return_value=_control_result("pass", 1.0)),
        patch.object(interop, "harbor_revision", return_value="rev"),
    ):
        cell = run_harbor_cell(REAL_TASK, "oracle", (), workdir=tmp_path / "w")
        ignored = run_harbor_cell(REAL_TASK, "nop", ("skip_plant",), workdir=tmp_path / "w2")
    assert ignored["verdict"] == "pass"
    assert ignored["attacks"] == []
    assert cell["target"] == "harbor"
    assert cell["agent"] == "oracle"
    assert cell["attacks"] == []
    assert cell["verdict"] == "pass"
    assert cell["reward"] == 1.0
    assert cell["evidence"] == "job-x"
    assert cell["platform_version"] == "rev"


def test_run_harbor_cell_control_error_maps(tmp_path: Path) -> None:
    with (
        patch.object(interop, "run_harbor_control", side_effect=RuntimeError("boom")),
        patch.object(interop, "harbor_revision", return_value="rev"),
    ):
        cell = run_harbor_cell(REAL_TASK, "nop", (), workdir=tmp_path)
    assert cell["verdict"] == "error"
    assert "boom" in (cell["reason"] or "")


def test_run_harbor_cell_cheat_uses_lane(tmp_path: Path) -> None:
    from evallab import cheat as cheat_module
    from evallab import harbor_view

    with (
        patch.object(interop, "harbor_revision", return_value="rev"),
        patch.object(
            cheat_module,
            "run_cheat_trial",
            return_value=(tmp_path / "job", {"trials": []}),
        ) as trial,
        patch.object(interop, "_trial_reward_from_job", return_value=1.0),
        patch.object(harbor_view, "installed_harbor_version", return_value=(0, 24)),
    ):
        cell = run_harbor_cell(REAL_TASK, "cheat", ("reward_plant",), workdir=tmp_path / "w")
    assert trial.call_args[0][1] == ("reward_plant",)
    assert cell["verdict"] == "pass"
    assert cell["reward"] == 1.0
    assert cell["evidence"] == str(tmp_path / "job")


def test_run_harbor_cell_cheat_rejects_bad_attack(tmp_path: Path) -> None:
    with patch.object(interop, "harbor_revision", return_value="rev"):
        cell = run_harbor_cell(REAL_TASK, "cheat", ("nope",), workdir=tmp_path)
    assert cell["verdict"] == "error"
    assert "unknown attack" in (cell["reason"] or "")


# -- grading math ----------------------------------------------------------------


def test_grade_task_row_boundaries() -> None:
    ok, problems = grade_task_row(
        {"h:oracle": "pass", "h:nop": "fail"}, targets=["h"], agents=["oracle", "nop"]
    )
    assert (ok, problems) == ("ok", [])
    broken, problems = grade_task_row(
        {"h:oracle": "fail", "h:nop": "fail"}, targets=["h"], agents=["oracle", "nop"]
    )
    assert broken == "broken"
    assert any("oracle" in problem for problem in problems)
    broken, problems = grade_task_row(
        {"h:oracle": "pass", "h:nop": "pass"}, targets=["h"], agents=["oracle", "nop"]
    )
    assert broken == "broken"
    assert any("nop" in problem for problem in problems)
    status, _ = grade_task_row({"h:oracle": "error"}, targets=["h"], agents=["oracle"])
    assert status == "n/a"
    assert grade_task_row({}, targets=["h"], agents=["oracle"])[0] == "n/a"


def test_render_grading_matrix_cheat_display() -> None:
    text = render_grading_matrix(
        [
            {
                "task_id": "lab/t",
                "cells": {"h:oracle": "pass", "h:nop": "fail", "h:cheat": "pass"},
                "grading": "ok",
            }
        ],
        targets=["h"],
        agents=["oracle", "nop", "cheat"],
    )
    assert "lab/t" in text
    assert "pass" in text and "fail" in text
    assert "cracked" in text
    assert "ok" in text


# -- matrix rows -----------------------------------------------------------------


def _fake_cell(target: str, agent: str, verdict: str, reward: float) -> dict:
    return {
        "target": target,
        "agent": agent,
        "attacks": [],
        "verdict": verdict,
        "reward": reward,
        "reason": None,
        "platform_version": f"{target}-v1",
        "evidence": f"ev:{target}:{agent}",
    }


def test_matrix_task_row_uses_registry_and_grades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_runner(
        task_dir: Path,
        agent: str,
        attacks: tuple,
        *,
        workdir: Path,
        timeout_seconds: int | None,
    ) -> dict:
        verdict = {"oracle": "pass", "nop": "fail", "cheat": "fail"}[agent]
        return _fake_cell("fake", agent, verdict, 1.0 if verdict == "pass" else 0.0)

    monkeypatch.setitem(MATRIX_TARGETS, "fake", fake_runner)
    row = matrix_task_row(
        REAL_TASK,
        targets=("fake",),
        agents=("oracle", "nop", "cheat"),
        attacks=(),
        workdir=tmp_path / "w",
    )
    assert row["cells"] == {"fake:oracle": "pass", "fake:nop": "fail", "fake:cheat": "fail"}
    assert row["grading"] == "ok"
    assert row["problems"] == []
    assert row["versions"] == {"fake": "fake-v1"}


def test_matrix_task_row_broken_and_unknown_target(tmp_path: Path) -> None:
    def bad_runner(
        task_dir: Path,
        agent: str,
        attacks: tuple,
        *,
        workdir: Path,
        timeout_seconds: int | None,
    ) -> dict:
        return _fake_cell("bad", agent, "fail", 0.0)

    with patch.dict(MATRIX_TARGETS, {"bad": bad_runner}):
        row = matrix_task_row(
            REAL_TASK,
            targets=("bad", "nope"),
            agents=("oracle", "nop"),
            attacks=(),
            workdir=tmp_path / "w",
        )
    assert row["cells"]["bad:oracle"] == "fail"
    assert row["cells"]["nope:oracle"] == "skipped"
    assert row["grading"] == "broken"
    assert any("bad:oracle" in problem for problem in row["problems"])


def test_matrix_targets_registry_wires_harbor_and_inspect() -> None:
    assert MATRIX_TARGETS["harbor"] is interop.run_harbor_cell
    assert MATRIX_TARGETS["inspect"] is interop.run_inspect_cell


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
    args = parser.parse_args(
        [
            "interop",
            "matrix",
            "a",
            "b",
            "--targets",
            "harbor",
            "--agents",
            "oracle",
            "nop",
            "--attacks",
            "skip_plant",
        ]
    )
    assert args.targets == ["harbor"]
    assert args.agents == ["oracle", "nop"]
    assert args.attacks == "skip_plant"
    assert callable(args.func)


def _matrix_row(task_id: str = "lab/t", grading: str = "ok", problems: tuple = ()) -> dict:
    return {
        "task_id": task_id,
        "cells": {},
        "grading": grading,
        "problems": list(problems),
        "versions": {},
        "details": {},
    }


def test_matrix_command_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from evallab.interop import build_interop_parser, interop_command

    parser = argparse.ArgumentParser()
    build_interop_parser(parser.add_subparsers())
    args = parser.parse_args(["interop", "matrix", "some-task"])
    with patch.object(interop, "matrix_task_row", return_value=_matrix_row()):
        assert interop_command(args, REPO_ROOT) == 0
    with patch.object(
        interop,
        "matrix_task_row",
        return_value=_matrix_row(grading="broken", problems=["h:oracle expected pass, got fail"]),
    ):
        assert interop_command(args, REPO_ROOT) == 1
        assert "grading broken" in capsys.readouterr().out
    with patch.object(interop, "matrix_task_row", return_value=_matrix_row(grading="n/a")):
        assert interop_command(args, REPO_ROOT) == 0
        assert "not agreement" in capsys.readouterr().out


def test_matrix_command_rejects_bad_attacks() -> None:
    from evallab.interop import build_interop_parser, interop_command

    parser = argparse.ArgumentParser()
    build_interop_parser(parser.add_subparsers())
    args = parser.parse_args(["interop", "matrix", "some-task", "--attacks", "nope"])
    with patch.object(interop, "matrix_task_row", side_effect=AssertionError("must not run")):
        assert interop_command(args, REPO_ROOT) == 2


# -- solution-less tasks (oracle needs solution/, nop/cheat do not) ---------------


def _solution_less_task(root: Path) -> Path:
    task_dir = make_task(root)
    shutil.rmtree(task_dir / "solution")
    return task_dir


def test_load_solution_required_only_for_oracle(tmp_path: Path) -> None:
    task_dir = _solution_less_task(tmp_path / "nosol")
    with pytest.raises(ValueError, match="solution/solve.sh"):
        load_harbor_task(task_dir)
    task = load_harbor_task(task_dir, require_solution=False)
    assert task.task_dir == task_dir
    no_tests = make_task(tmp_path / "notests")
    (no_tests / "tests" / "test.sh").unlink()
    with pytest.raises(ValueError, match="test.sh"):
        load_harbor_task(no_tests, require_solution=False)


def test_scripted_agent_plan_oracle_needs_solution(tmp_path: Path) -> None:
    task = load_harbor_task(_solution_less_task(tmp_path / "nosol"), require_solution=False)
    with pytest.raises(ValueError, match="oracle plan needs solution"):
        scripted_agent_plan(task, "oracle", ())
    assert scripted_agent_plan(task, "nop", ()).command is None
    assert scripted_agent_plan(task, "cheat", ()).command is not None


def test_run_harbor_cell_solution_less_oracle_errors_nop_runs(tmp_path: Path) -> None:
    task_dir = _solution_less_task(tmp_path / "nosol")
    with (
        patch.object(interop, "run_harbor_control", side_effect=AssertionError("must not run")),
        patch.object(interop, "harbor_revision", return_value="rev"),
    ):
        cell = run_harbor_cell(task_dir, "oracle", (), workdir=tmp_path / "o")
    assert cell["verdict"] == "error"
    assert "solution/solve.sh" in (cell["reason"] or "")
    with (
        patch.object(interop, "run_harbor_control", return_value=_control_result("fail", 0.0)),
        patch.object(interop, "harbor_revision", return_value="rev"),
    ):
        cell = run_harbor_cell(task_dir, "nop", (), workdir=tmp_path / "n")
    assert cell["verdict"] == "fail"


def test_run_inspect_cell_solution_less_oracle_errors_nop_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _solution_less_task(tmp_path / "nosol")
    with patch.object(interop, "_write_inspect_run", side_effect=AssertionError("must not run")):
        cell = run_inspect_cell(task_dir, "oracle", (), workdir=tmp_path / "o")
    assert cell["verdict"] == "error"
    assert "solution/solve.sh" in (cell["reason"] or "")
    captured: dict = {}

    def fake_stage(work: Path, task: object, **kwargs: object) -> Path:
        captured.update(kwargs)
        driver = Path(work) / "inspect_driver.py"
        driver.write_text("x", encoding="utf-8")
        return driver

    monkeypatch.setattr(interop, "_write_inspect_run", fake_stage)
    monkeypatch.setattr(interop, "_docker_ps", lambda: {})
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    cell = run_inspect_cell(task_dir, "nop", (), workdir=tmp_path / "n")
    assert captured["agent"] == "nop"
    assert cell["verdict"] == "error"
    assert "uv" in (cell["reason"] or "")
