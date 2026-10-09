"""Karotte generic runner: plans, specs, transcript verdicts, no docker ($0).

Live-Docker execution is covered by manual smoke cells (see the worker's
final report), never by these tests: every external probe or subprocess is
injected or stubbed here per the deterministic-test rule. Agent scripts come
from ``evallab.interop.scripted_agent_plan`` (identical on every platform).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from evallab import interop_karotte as ik
from evallab.interop import ScriptedPlan, load_harbor_task, scripted_agent_plan

REPO_ROOT = Path(__file__).resolve().parent.parent
TXN_TASK = REPO_ROOT / "library/tasks/transaction-reconciliation"
EVSUM_TASK = REPO_ROOT / "library/tasks/event-summary"


def make_task(
    root: Path,
    *,
    name: str = "lab/synthetic",
    workdir: str = "/app",
    docker_image: str | None = None,
    with_solution: bool = True,
    resources: dict | None = None,
) -> Path:
    task_dir = root / "task"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "instruction.md").write_text("Do the thing.\n", encoding="utf-8")
    if with_solution:
        (task_dir / "solution").mkdir(parents=True)
        (task_dir / "solution" / "solve.sh").write_text(
            "#!/bin/bash\necho solved\n", encoding="utf-8"
        )
    (task_dir / "tests" / "test.sh").write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    (task_dir / "environment" / "Dockerfile").write_text(
        "FROM python:3.12-slim-bookworm\n", encoding="utf-8"
    )
    env_lines = "workdir = " + json.dumps(workdir) + "\n"
    if docker_image is not None:
        env_lines += "docker_image = " + json.dumps(docker_image) + "\n"
    if resources:
        for key, value in resources.items():
            env_lines += f"{key} = {value}\n"
    (task_dir / "task.toml").write_text(
        'artifacts = []\n\n[task]\nname = "'
        + name
        + '"\n\n[metadata]\ndifficulty = "easy"\n'
        + "\n[verifier]\ntimeout_sec = 60.0\n\n[environment]\n"
        + env_lines,
        encoding="utf-8",
    )
    return task_dir


def exec_fake_model(source: str) -> list[dict]:
    """Execute rendered fake_model.py against stub Message classes; return dicts."""
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
            calls.append(
                {
                    "role": role,
                    "content": content,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": c.type,
                            "name": c.function.name,
                            "arguments": json.loads(c.function.arguments),
                        }
                        for c in (tool_calls or [])
                    ],
                }
            )

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
        exec(compile(source, "fake_model.py", "exec"), namespace)  # noqa: S102
        namespace["get_messages"](object())
        return calls
    finally:
        for key, mod in old.items():
            if mod is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = mod


def test_plan_messages_nop_has_no_tool_calls():
    messages = ik.plan_messages(())
    assert len(messages) == 1
    assert not messages[0].get("tool_calls")
    assert messages[0]["role"] == "assistant"


def test_plan_messages_one_bash_call_per_command():
    messages = ik.plan_messages(("echo a", "echo b"))
    assert len(messages) == 3
    for i, command in enumerate(("echo a", "echo b")):
        (call,) = messages[i]["tool_calls"]
        assert call["function"]["name"] == "bash"
        args = json.loads(call["function"]["arguments"])
        assert args["command"] == command
        assert args["timeout_s"] == ik.BASH_TIMEOUT_S
    assert messages[-1].get("tool_calls") in (None, [])


def test_canonical_plans():
    task = load_harbor_task(TXN_TASK)
    oracle = scripted_agent_plan(task, "oracle", ())
    assert oracle == ScriptedPlan(files={}, command="bash solution/solve.sh")
    assert scripted_agent_plan(task, "nop", ()) == ScriptedPlan(files={}, command=None)
    cheat = scripted_agent_plan(task, "cheat", ("skip_plant",))
    assert list(cheat.files) == ["/tmp/cheat_ladder.py"]
    assert "--attacks skip_plant" in (cheat.command or "")
    assert "--cwd ." in (cheat.command or "")
    full = scripted_agent_plan(task, "cheat", ())
    assert "--attacks" not in (full.command or "")
    with pytest.raises(ValueError, match="unknown scripted agent"):
        scripted_agent_plan(task, "oracle-ish", ())
    with pytest.raises(ValueError, match="unknown cheat attack"):
        scripted_agent_plan(task, "cheat", ("nope",))


def test_canonical_oracle_needs_solution(tmp_path):
    task_dir = make_task(tmp_path)
    (task_dir / "solution" / "solve.sh").unlink()
    task = load_harbor_task(task_dir, require_solution=False)
    with pytest.raises(ValueError, match="oracle plan needs solution/solve.sh"):
        scripted_agent_plan(task, "oracle", ())
    assert scripted_agent_plan(task, "nop", ()).command is None
    assert scripted_agent_plan(task, "cheat", ()).command


def test_lenient_load_solution_less_task(tmp_path):
    task_dir = make_task(
        tmp_path / "mimolike",
        name="mimo-v2.6-rl/format-code-task-002552",
        workdir="/testbed",
        docker_image="example.com/mimo@sha256:abc",
        with_solution=False,
        resources={"cpus": 4, "memory_mb": 4096},
    )
    assert not (task_dir / "solution" / "solve.sh").is_file()
    task = load_harbor_task(task_dir, require_solution=False)
    assert task.task_id == "mimo-v2.6-rl/format-code-task-002552"
    with pytest.raises(ValueError, match="instruction.md"):
        load_harbor_task(REPO_ROOT, require_solution=False)


def test_resolve_spec_real_tasks(tmp_path):
    txn = ik.resolve_spec(load_harbor_task(TXN_TASK))
    assert txn.workdir == "/app"
    assert txn.submission_paths == ("/app",)
    assert txn.base_kind == "dockerfile" and txn.has_solution
    assert (txn.cpus, txn.memory) == (ik.RUN_CPUS, ik.RUN_MEMORY)
    evsum = ik.resolve_spec(load_harbor_task(EVSUM_TASK))
    assert evsum.workdir == "/app"
    assert evsum.submission_paths == (
        "/app/input/events.jsonl",
        "/app/output/summary.json",
    )
    like = ik.resolve_spec(
        load_harbor_task(
            make_task(
                tmp_path / "mimolike",
                name="mimo-v2.6-rl/format-code-task-002552",
                workdir="/testbed",
                docker_image="example.com/mimo@sha256:abc",
                with_solution=False,
                resources={"cpus": 4, "memory_mb": 4096},
            ),
            require_solution=False,
        )
    )
    assert like.workdir == "/testbed"
    assert like.submission_paths == ("/testbed",)
    assert like.base_kind == "docker_image" and not like.has_solution
    assert like.base_ref == "example.com/mimo@sha256:abc"
    assert (like.cpus, like.memory) == ("4", "4096m")


def test_resolve_spec_rejects_compose_and_root(tmp_path):
    task_dir = make_task(tmp_path / "compose")
    (task_dir / "environment" / "docker-compose.yaml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="multi-service compose"):
        ik.resolve_spec(load_harbor_task(task_dir))
    task_dir = make_task(tmp_path / "rootfs", workdir="/")
    with pytest.raises(ValueError, match="whole-filesystem"):
        ik.resolve_spec(load_harbor_task(task_dir))


def test_fake_model_from_canonical_plan_executes():
    task = load_harbor_task(TXN_TASK)
    oracle = exec_fake_model(ik.fake_model_source(scripted_agent_plan(task, "oracle", ()), "/app"))
    assert len(oracle) == 2
    command = oracle[0]["tool_calls"][0]["arguments"]["command"]
    assert command == "cd /app && bash solution/solve.sh"
    assert oracle[-1]["tool_calls"] == []
    nop = exec_fake_model(ik.fake_model_source(scripted_agent_plan(task, "nop", ()), "/app"))
    assert len(nop) == 1 and nop[0]["tool_calls"] == []
    cheat = exec_fake_model(
        ik.fake_model_source(scripted_agent_plan(task, "cheat", ("skip_plant",)), "/testbed")
    )
    command = cheat[0]["tool_calls"][0]["arguments"]["command"]
    assert command.startswith("cd /testbed && python3 /tmp/cheat_ladder.py")
    assert "skip_plant" in command
    assert cheat[0]["tool_calls"][0]["arguments"]["timeout_s"] == ik.CHEAT_TIMEOUT_S


def test_render_scoring_embeds_canonical_parser():
    from evallab.interop import parse_reward_bytes

    source = ik._render_scoring()
    assert "def parse_reward_bytes(" in source
    assert "@@PARSE_REWARD_BYTES@@" not in source
    compile(source, "scoring_script.py", "exec")
    namespace: dict = {}
    exec(compile(source, "scoring_script.py", "exec"), namespace)  # noqa: S102
    embedded = namespace["parse_reward_bytes"]
    assert embedded(b"1", None) == parse_reward_bytes(b"1", None) == 1.0
    with pytest.raises(FileNotFoundError):
        embedded(None, None)


def scoring_event(score, **meta):
    return {
        "timestamp": "2026-10-09T00:00:00Z",
        "type": "scoring",
        "scoring": {"score": score, "metadata": meta, "continue_task": False},
        "resource_metrics": None,
    }


def test_parse_transcript_pass_fail():
    for score, verdict in ((1.0, "pass"), (0.0, "fail"), (2, "pass")):
        summary = ik.parse_transcript(
            {
                "run_id": "r",
                "events": [
                    scoring_event(0.0),
                    scoring_event(score),
                    {"type": "task_completed", "status": "passed"},
                ],
            }
        )
        assert summary["reward"] == float(score)
        assert summary["misbehavior"] is None
        from evallab.interop import verdict_for_reward

        assert verdict_for_reward(summary["reward"]) == verdict


def test_parse_transcript_error_and_misbehavior():
    summary = ik.parse_transcript(
        {
            "run_id": "r",
            "events": [
                {"type": "error", "exception_type": "Boom", "message": "kablam"},
            ],
        }
    )
    assert summary["reward"] is None
    assert summary["errors"] == ["kablam"]
    summary = ik.parse_transcript(
        {"run_id": "r", "events": [scoring_event(0.0, misbehavior="symlink")]},
    )
    assert summary["reward"] == 0.0
    assert summary["misbehavior"] == "symlink"
    for bad in (None, [], {"no": "events"}, {"events": "x"}):
        assert ik.parse_transcript(bad)["reward"] is None


def test_ladder_records_extraction():
    records = [{"name": "skip_plant", "status": "executed", "detail": "x"}]
    payload = {
        "run_id": "r",
        "events": [
            {
                "type": "tool_call_completed",
                "result": {
                    "structuredContent": {
                        "stdout": "CHEAT_LADDER_RESULT=" + json.dumps(records) + "\n"
                    }
                },
            }
        ],
    }
    assert ik.ladder_records(payload) == records
    assert ik.ladder_records({"run_id": "r", "events": []}) is None
    assert ik.ladder_records(None) is None


def test_image_digest_ignores_fake_model(tmp_path):
    env = tmp_path / "env"
    (env / "src" / "environment").mkdir(parents=True)
    (env / "Containerfile").write_text("FROM x\n", encoding="utf-8")
    (env / "src" / "environment" / "task.py").write_text("v1\n", encoding="utf-8")
    (env / "src" / "environment" / "fake_model.py").write_text("a\n", encoding="utf-8")
    before = ik.image_digest(env)
    (env / "src" / "environment" / "fake_model.py").write_text("b\n", encoding="utf-8")
    assert ik.image_digest(env) == before
    (env / "src" / "environment" / "task.py").write_text("v2\n", encoding="utf-8")
    assert ik.image_digest(env) != before
    assert ik.image_tag(env).startswith(ik.IMAGE_CACHE_PREFIX + "-")


def test_dir_digest_stable(tmp_path):
    context = tmp_path / "environment"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
    assert ik._dir_digest(context) == ik._dir_digest(context)


def test_karotte_env_drops_ci(monkeypatch):
    monkeypatch.setenv("CI", "true")
    assert "CI" not in ik._karotte_env()
    assert ik._karotte_env({"A": "b"})["A"] == "b"


def test_run_cell_rejects_bad_agent(tmp_path):
    with pytest.raises(ValueError, match="unknown karotte agent"):
        ik.run_cell(TXN_TASK, "oracle-ish", (), workdir=tmp_path)


def test_run_cell_validation_errors_without_docker(tmp_path):
    cell = ik.run_cell(tmp_path / "missing", "oracle", (), workdir=tmp_path)
    assert cell["verdict"] == "error"
    task_dir = make_task(tmp_path / "compose")
    (task_dir / "environment" / "docker-compose.yaml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    cell = ik.run_cell(task_dir, "nop", (), workdir=tmp_path)
    assert cell["verdict"] == "error" and "compose" in (cell["reason"] or "")


def test_run_cell_oracle_without_solution_skips_without_docker(tmp_path):
    task_dir = make_task(tmp_path / "nosol")
    (task_dir / "solution" / "solve.sh").unlink()
    cell = ik.run_cell(task_dir, "oracle", (), workdir=tmp_path)
    assert cell["verdict"] == "skipped" and "solution" in (cell["reason"] or "")
    cell = ik.run_cell(task_dir, "cheat", ("nope",), workdir=tmp_path)
    assert cell["verdict"] == "error"


def test_run_cell_skipped_without_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(ik, "docker_daemon_ok", lambda timeout=30: (False, "down"))
    cell = ik.run_cell(TXN_TASK, "nop", (), workdir=tmp_path)
    assert cell["verdict"] == "skipped"
    assert cell["reward"] is None and cell["evidence"] is None


class _Done:
    returncode = 0
    stdout = ""
    stderr = ""


def _stub_run_factory(score: float):
    def _stub_run(cmd, **kwargs):
        assert "--dev" in cmd  # per-cell fake_model rides the dev bind-mount
        config_path = cmd[cmd.index("--config") + 1]
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        assert config["use_fake_model"] is True
        out = Path(config["transcript_file"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "run_id": config["run_id"],
                    "events": [
                        scoring_event(score),
                        {"type": "task_completed", "status": "passed"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return _Done()

    return _stub_run


def test_run_cell_pass_and_fail_with_stubbed_karotte(tmp_path, monkeypatch):
    def _assemble(task, spec, env_dir, *, base_tag):
        (Path(env_dir) / "src" / "environment").mkdir(parents=True, exist_ok=True)
        return {"env_dir": str(env_dir)}

    monkeypatch.setattr(ik, "docker_daemon_ok", lambda timeout=30: (True, "mock"))
    monkeypatch.setattr(
        ik, "ensure_harbor_base", lambda task, spec, build_timeout: {"tag": "base", "kind": "mock"}
    )
    monkeypatch.setattr(ik, "assemble_env", _assemble)
    monkeypatch.setattr(ik, "resolved_karotte_version", lambda env_dir: "3.0.59")
    monkeypatch.setattr(
        ik, "ensure_image", lambda env_dir, build_timeout=1800: {"tag": "t", "rebuilt": False}
    )
    monkeypatch.setattr(
        ik,
        "_watch_bounds",
        lambda name, stop, record, cpus="2", memory="2g": record.update(bounded=True),
    )
    monkeypatch.setattr(ik, "_remove_own_container", lambda name: True)
    import evallab.cheat_ladder as ladder

    assert ladder.ATTACKS  # ladder module present for cheat staging
    monkeypatch.setattr(ik.subprocess, "run", _stub_run_factory(1.0))
    work = tmp_path / "work"
    cell = ik.run_cell(TXN_TASK, "oracle", (), workdir=work)
    assert cell["verdict"] == "pass" and cell["reward"] == 1.0
    assert cell["platform_version"] == "3.0.59"
    evidence = json.loads(Path(cell["evidence"]).read_text(encoding="utf-8"))
    assert evidence["container_bounded"] is True
    assert evidence["container_removed"] is True
    assert evidence["spec"]["workdir"] == "/app"
    assert evidence["plan"]["command"] == "bash solution/solve.sh"
    monkeypatch.setattr(ik.subprocess, "run", _stub_run_factory(0.0))
    cell = ik.run_cell(TXN_TASK, "nop", (), workdir=work)
    assert cell["verdict"] == "fail" and cell["reward"] == 0.0
