"""Deterministic Karotte grading and runtime boundaries; no Docker or models."""

from __future__ import annotations

import json
import subprocess
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
    assert oracle == ScriptedPlan(files={}, command="bash /solution/solve.sh")
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
    assert command == "cd /app && bash /solution/solve.sh"
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


@pytest.fixture
def rendered_scoring(tmp_path, monkeypatch):
    namespace: dict = {"__name__": "scoring_under_test"}
    exec(compile(ik._render_scoring(), "scoring_script.py", "exec"), namespace)  # noqa: S102
    work = tmp_path / "work"
    tests = tmp_path / "verifier"
    logs = tmp_path / "logs"
    for path in (work, tests, logs):
        path.mkdir()
    (tests / "test.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    output = tmp_path / "score.json"
    namespace.update(TESTS_SRC=tests, TESTS_DST=tmp_path / "tests", LOG_DIR=logs)
    monkeypatch.setattr(sys, "argv", ["scoring", "60", str(work), str(output)])
    return namespace, work, logs, output


@pytest.mark.parametrize(
    ("reward_bytes", "verifier_exit", "timed_out", "reward"),
    [
        (b"1", 0, False, 1.0),
        (b"0", 1, False, 0.0),
        (None, 0, False, None),
        (b"not-a-reward", 0, False, None),
        (b"1", 0, True, None),
    ],
)
def test_rendered_scoring_distinguishes_rewards_from_errors(
    rendered_scoring, reward_bytes, verifier_exit, timed_out, reward
):
    namespace, _work, logs, output = rendered_scoring
    (logs / "reward.txt").write_bytes(b"1")  # Must not survive a verifier with no reward.

    def verify(command, **kwargs):
        if reward_bytes is not None:
            (logs / "reward.txt").write_bytes(reward_bytes)
        if timed_out:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, verifier_exit, stdout="", stderr="")

    namespace["subprocess"] = types.SimpleNamespace(
        run=verify, TimeoutExpired=subprocess.TimeoutExpired
    )
    assert namespace["main"]() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    # ExecutableJudge serializes every metadata value with str().
    metadata = {key: str(value) for key, value in payload["metadata"].items()}
    summary = ik.parse_transcript(
        {
            "events": [
                scoring_event(payload["score"], **metadata),
                {"type": "task_completed", "status": "failed" if payload["score"] < 1 else "passed"},
            ]
        }
    )
    assert summary["reward"] == reward
    if reward is None:
        assert metadata["error"]
        assert "verdict" not in metadata
        assert summary["errors"]
    else:
        assert summary["errors"] == []
        assert metadata["verdict"] == ("pass" if reward >= 1 else "fail")
        assert int(metadata["exit"]) == verifier_exit


def test_rendered_scoring_rejects_partial_submission_restore(rendered_scoring, monkeypatch):
    namespace, work, logs, output = rendered_scoring
    blocker = work / "not-a-directory"
    blocker.write_text("cannot restore beneath this file", encoding="utf-8")
    saved = work / "saved.txt"
    saved.write_text("submission", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["scoring", "60", str(work), str(blocker / "answer.txt"), str(saved), str(output)],
    )

    def verify(command, **kwargs):
        (logs / "reward.txt").write_bytes(b"1")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    namespace["subprocess"] = types.SimpleNamespace(
        run=verify, TimeoutExpired=subprocess.TimeoutExpired
    )
    assert namespace["main"]() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["metadata"]["unrestored_submissions"]
    assert payload["metadata"]["error"]
    assert "verdict" not in payload["metadata"]
    assert ik.parse_transcript(
        {
            "events": [
                scoring_event(payload["score"], **payload["metadata"]),
                {"type": "task_completed", "status": "failed"},
            ]
        }
    )["reward"] is None


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
                    {"type": "task_completed", "status": "failed" if score < 1 else "passed"},
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
        {
            "run_id": "r",
            "events": [
                scoring_event(0.0, misbehavior="symlink"),
                {"type": "task_completed", "status": "failed"},
            ],
        },
    )
    assert summary["reward"] == 0.0
    assert summary["misbehavior"] == "symlink"
    for bad in (None, [], {"no": "events"}, {"events": "x"}):
        assert ik.parse_transcript(bad)["reward"] is None


@pytest.mark.parametrize(
    "metadata",
    [
        {"error": "neither reward.txt nor reward.json exists"},
        {"error": "test.sh timed out after 60s"},
        {"tempdir_error": "No space left on device"},
        {"CalledProcessError": "scoring process exited 1"},
        {"FileNotFoundError": "scoring executable missing"},
        {"json_read_error": "score output missing"},
    ],
)
def test_parse_transcript_rejects_judge_placeholder_zeroes(metadata):
    summary = ik.parse_transcript(
        {
            "events": [
                scoring_event(0.0, **metadata),
                {"type": "task_completed", "status": "failed"},
            ]
        }
    )
    assert summary["reward"] is None
    assert summary["errors"]
    assert summary["scoring"]["metadata"] == metadata


@pytest.mark.parametrize("score", [True, None, "1", float("nan"), float("inf")])
def test_parse_transcript_rejects_invalid_scores(score):
    summary = ik.parse_transcript(
        {"events": [scoring_event(score), {"type": "task_completed", "status": "passed"}]}
    )
    assert summary["reward"] is None
    assert summary["errors"]


@pytest.mark.parametrize(
    "events",
    [
        [scoring_event(1.0)],
        [scoring_event(1.0), {"type": "task_completed", "status": "error"}],
        [
            {"type": "error", "message": "post-scoring hook failed"},
            scoring_event(1.0),
            {"type": "task_completed", "status": "passed"},
        ],
        [
            scoring_event(0.0, error="verifier failed to launch"),
            scoring_event(1.0),
            {"type": "task_completed", "status": "passed"},
        ],
    ],
)
def test_parse_transcript_rejects_incomplete_or_fatal_runs(events):
    summary = ik.parse_transcript({"events": events})
    assert summary["reward"] is None
    assert summary["errors"]


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


@pytest.mark.parametrize(
    ("returncode", "stdout", "gone"),
    [(0, "", True), (0, "karotte_run_owned\n", False), (1, "", False)],
)
def test_container_cleanup_requires_a_successful_observation(monkeypatch, returncode, stdout, gone):
    monkeypatch.setattr(
        ik.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], returncode, stdout=stdout),
    )
    assert ik._container_gone("karotte_run_owned") is gone


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


def _stub_run_factory(score: float, *, metadata=None, events=None, returncode=0):
    def run(command, **kwargs):
        config_path = command[command.index("--config") + 1]
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        out = Path(config["transcript_file"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "run_id": config["run_id"],
                    "events": events
                    if events is not None
                    else [
                        scoring_event(score, **(metadata or {})),
                        {"type": "task_completed", "status": "failed" if score < 1 else "passed"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr="")

    return run


@pytest.fixture
def karotte_runtime(monkeypatch):
    def assemble(task, spec, env_dir, *, base_tag):
        (Path(env_dir) / "src" / "environment").mkdir(parents=True, exist_ok=True)
        return {"env_dir": str(env_dir)}

    monkeypatch.setattr(
        "evallab.campaign_execution.docker_available_resources", lambda: (8.0, 16384)
    )
    monkeypatch.setattr(ik, "docker_daemon_ok", lambda timeout=30: (True, "injected daemon"))
    monkeypatch.setattr(
        ik, "ensure_harbor_base", lambda task, spec, build_timeout: {"tag": "base", "kind": "injected"}
    )
    monkeypatch.setattr(ik, "assemble_env", assemble)
    monkeypatch.setattr(ik, "resolved_karotte_version", lambda env_dir: "3.0.59")
    monkeypatch.setattr(
        ik, "ensure_image", lambda env_dir, build_timeout=1800: {"tag": "t", "rebuilt": False}
    )
    monkeypatch.setattr(
        ik,
        "_watch_bounds",
        lambda name, stop, record, **kwargs: record.update(bounded=True),
    )
    monkeypatch.setattr(ik, "_remove_own_container", lambda name: True)
    monkeypatch.setattr(ik.subprocess, "run", _stub_run_factory(1.0))


@pytest.mark.parametrize("refusal", ["unbounded container", "insufficient capacity"])
def test_run_cell_admission_refusal_prevents_runtime(tmp_path, monkeypatch, refusal):
    def available():
        if refusal == "unbounded container":
            raise ValueError("an existing Docker container has unbounded CPU or memory")
        return 0.0, 0

    def unexpected(*args, **kwargs):
        pytest.fail("image preparation or runtime reached after admission refusal")

    monkeypatch.setattr("evallab.campaign_execution.docker_available_resources", available)
    for name in ("docker_daemon_ok", "ensure_harbor_base", "assemble_env", "ensure_image"):
        monkeypatch.setattr(ik, name, unexpected)
    monkeypatch.setattr(ik.subprocess, "run", unexpected)
    work = tmp_path / "work"
    cell = ik.run_cell(TXN_TASK, "nop", (), workdir=work)
    assert cell["verdict"] == "error" and cell["reward"] is None
    assert cell["evidence"] == str(work)
    admission = json.loads((work / "admission.json").read_text(encoding="utf-8"))
    assert admission["admitted"] is False
    assert admission["reason"] == cell["reason"]


def test_run_cell_unreachable_daemon_is_error_with_evidence(
    tmp_path, monkeypatch, karotte_runtime
):
    monkeypatch.setattr(ik, "docker_daemon_ok", lambda timeout=30: (False, "connection refused"))
    work = tmp_path / "work"
    cell = ik.run_cell(TXN_TASK, "nop", (), workdir=work)
    assert cell["verdict"] == "error" and cell["reward"] is None
    assert cell["evidence"] == str(work)
    assert json.loads((work / "admission.json").read_text(encoding="utf-8"))["admitted"] is True
    daemon = json.loads((work / "daemon.json").read_text(encoding="utf-8"))
    assert daemon == {"available": False, "detail": "connection refused"}


@pytest.mark.parametrize(
    ("agent", "score", "verdict"),
    [("oracle", 1.0, "pass"), ("nop", 0.0, "fail"), ("cheat", 1.0, "pass")],
)
def test_run_cell_returns_only_valid_bounded_scores(
    tmp_path, monkeypatch, karotte_runtime, agent, score, verdict
):
    monkeypatch.setattr(ik.subprocess, "run", _stub_run_factory(score))
    work = tmp_path / "work"
    cell = ik.run_cell(TXN_TASK, agent, (), workdir=work)
    assert cell["verdict"] == verdict and cell["reward"] == score
    assert cell["platform_version"] == "3.0.59"
    evidence = json.loads(Path(cell["evidence"]).read_text(encoding="utf-8"))
    assert evidence["container_bounded"] is True
    assert evidence["container_removed"] is True
    assert evidence["spec"]["workdir"] == "/app"
    assert json.loads(Path(evidence["admission"]).read_text(encoding="utf-8"))["admitted"] is True
    config = json.loads(next(work.glob("*.config.json")).read_text(encoding="utf-8"))
    assert config["use_fake_model"] is True and config["model"] == "fake/fake"


@pytest.mark.parametrize(
    ("score", "metadata", "returncode", "bounded"),
    [
        (1.0, {}, 0, False),
        (0.0, {"error": "neither reward.txt nor reward.json exists"}, 0, True),
        (0.0, {"CalledProcessError": "scoring process failed"}, 0, True),
        (1.0, {}, 125, True),
    ],
)
def test_run_cell_invalid_runtime_cannot_pass_or_fail(
    tmp_path, monkeypatch, karotte_runtime, score, metadata, returncode, bounded
):
    monkeypatch.setattr(
        ik.subprocess, "run", _stub_run_factory(score, metadata=metadata, returncode=returncode)
    )
    monkeypatch.setattr(
        ik, "_watch_bounds", lambda name, stop, record, **kwargs: record.update(bounded=bounded)
    )
    cell = ik.run_cell(TXN_TASK, "nop", (), workdir=tmp_path / "work")
    assert cell["verdict"] == "error" and cell["reward"] is None
    evidence = json.loads(Path(cell["evidence"]).read_text(encoding="utf-8"))
    assert evidence["error"] == cell["reason"]
    assert evidence["container_bounded"] is bounded
    assert evidence["run_exit"] == returncode
    assert evidence["scoring"]["score"] == score


@pytest.mark.parametrize(
    ("stage", "exception"),
    [
        ("setup", RuntimeError("image setup failed")),
        ("setup", subprocess.TimeoutExpired(["karotte", "build"], 3)),
        ("run", OSError("runtime executable unavailable")),
        ("run", subprocess.TimeoutExpired(["karotte", "run"], 3)),
        ("run", None),
    ],
)
def test_run_cell_runtime_failures_preserve_evidence(
    tmp_path, monkeypatch, karotte_runtime, stage, exception
):
    def fail(*args, **kwargs):
        if exception is not None:
            raise exception
        return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="no transcript")

    if stage == "setup":
        monkeypatch.setattr(ik, "ensure_image", fail)
    else:
        monkeypatch.setattr(ik.subprocess, "run", fail)
    cell = ik.run_cell(TXN_TASK, "nop", (), workdir=tmp_path / "work", timeout_seconds=3)
    assert cell["verdict"] == "error" and cell["reward"] is None
    evidence = json.loads(Path(cell["evidence"]).read_text(encoding="utf-8"))
    assert evidence["error"] == cell["reason"]
    assert evidence["task_id"] == cell["task_id"]


def test_run_cell_fatal_transcript_error_cannot_hide_behind_a_score(
    tmp_path, monkeypatch, karotte_runtime
):
    events = [
        scoring_event(1.0),
        {"type": "error", "message": "post-scoring infrastructure failure"},
        {"type": "task_completed", "status": "error"},
    ]
    monkeypatch.setattr(ik.subprocess, "run", _stub_run_factory(1.0, events=events))
    cell = ik.run_cell(TXN_TASK, "nop", (), workdir=tmp_path / "work")
    assert cell["verdict"] == "error" and cell["reward"] is None
    evidence = json.loads(Path(cell["evidence"]).read_text(encoding="utf-8"))
    assert "post-scoring infrastructure failure" in evidence["errors"]
    assert evidence["error"] == cell["reason"]
