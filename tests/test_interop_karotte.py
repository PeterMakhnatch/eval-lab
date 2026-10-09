"""Karotte runner cells: scripted agents, transcript verdicts, no docker ($0).

Live-Docker execution is covered by manual smoke cells (see the worker's
final report), never by these tests: every external probe or subprocess is
injected or stubbed here per the deterministic-test rule.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from evallab import interop_karotte as ik
from evallab.interop import load_harbor_task

REPO_ROOT = Path(__file__).resolve().parent.parent
TXN_TASK = REPO_ROOT / "library/tasks/transaction-reconciliation"
EVSUM_TASK = REPO_ROOT / "library/tasks/event-summary"


def make_task(root: Path, *, name: str = "lab/synthetic") -> Path:
    task_dir = root / "task"
    (task_dir / "solution").mkdir(parents=True)
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "instruction.md").write_text("Do the thing.\n", encoding="utf-8")
    (task_dir / "solution" / "solve.sh").write_text("#!/bin/bash\necho solved\n", encoding="utf-8")
    (task_dir / "tests" / "test.sh").write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    (task_dir / "environment" / "Dockerfile").write_text(
        "FROM python:3.12-slim-bookworm\n", encoding="utf-8"
    )
    (task_dir / "task.toml").write_text(
        'artifacts = []\n\n[task]\nname = "'
        + name
        + '"\n\n[metadata]\ndifficulty = "easy"\n'
        + "\n[verifier]\ntimeout_sec = 60.0\n\n[environment]\n",
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
    assert messages[0]["tool_calls"] is None or not messages[0].get("tool_calls")
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


def test_cheat_command_full_and_subset():
    full = ik.cheat_command(())
    assert "--attacks" not in full
    assert "/tmp/cheat_ladder.py" in full and "--cwd /workdir" in full
    subset = ik.cheat_command(("reward_plant", "skip_plant"))
    assert "--attacks reward_plant,skip_plant" in subset


def test_selected_cheat_attacks_validates_names():
    assert ik.selected_cheat_attacks(())  # full ladder, fixed order
    assert ik.selected_cheat_attacks(("skip_plant",)) == ("skip_plant",)
    with pytest.raises(ValueError):
        ik.selected_cheat_attacks(("nope",))


def test_fake_model_oracle_nop_cheat_execute():
    txn = ik.adapter_for(load_harbor_task(TXN_TASK))
    oracle = exec_fake_model(ik.fake_model_source("oracle", adapter=txn))
    assert len(oracle) == len(txn.oracle_commands) + 1
    assert "sqlite3" in oracle[0]["tool_calls"][0]["arguments"]["command"]
    assert oracle[-1]["tool_calls"] == []
    nop = exec_fake_model(ik.fake_model_source("nop", adapter=txn))
    assert len(nop) == 1 and nop[0]["tool_calls"] == []
    cheat = exec_fake_model(
        ik.fake_model_source("cheat", adapter=txn, attacks=("skip_plant",))
    )
    assert len(cheat) == 2
    call = cheat[0]["tool_calls"][0]
    assert "cheat_ladder.py" in call["arguments"]["command"]
    assert "skip_plant" in call["arguments"]["command"]
    assert call["arguments"]["timeout_s"] == ik.CHEAT_TIMEOUT_S
    with pytest.raises(ValueError):
        ik.fake_model_source("oracle-ish", adapter=txn)


def test_adapters_from_real_tasks():
    for task_dir, key, submissions in (
        (TXN_TASK, "transaction-reconciliation", ("/workdir/data/ledger.db",)),
        (
            EVSUM_TASK,
            "event-summary",
            (
                "/workdir/data/input/events.jsonl",
                "/workdir/data/output/summary.json",
            ),
        ),
    ):
        adapter = ik.adapter_for(load_harbor_task(task_dir))
        assert adapter.task_key == key
        assert adapter.submission_paths == submissions
        assert adapter.oracle_commands
        assert adapter.student_files and adapter.root_files
        compile(adapter.scoring_source, "scoring_script.py", "exec")
        compile(
            ik.fake_model_source("oracle", adapter=adapter), "fake_model.py", "exec"
        )


def test_txn_seed_matches_harbor_start_state(tmp_path):
    import sqlite3

    adapter = ik.adapter_for(load_harbor_task(TXN_TASK))
    db_path = tmp_path / "ledger.db"
    db_path.write_bytes(adapter.student_files["ledger.db"])
    connection = sqlite3.connect(db_path)
    try:
        (pending,) = connection.execute(
            "SELECT COUNT(*) FROM ledger_entries WHERE reconciliation_status='pending'"
        ).fetchone()
    finally:
        connection.close()
    assert pending == 1


def test_adapter_for_unknown_task(tmp_path):
    task = load_harbor_task(make_task(tmp_path))
    with pytest.raises(ValueError, match="no karotte runner adapter"):
        ik.adapter_for(task)


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


def test_karotte_env_drops_ci(monkeypatch):
    monkeypatch.setenv("CI", "true")
    assert "CI" not in ik._karotte_env()
    assert ik._karotte_env({"A": "b"})["A"] == "b"


def test_run_cell_rejects_bad_agent(tmp_path):
    with pytest.raises(ValueError, match="unknown karotte agent"):
        ik.run_cell(TXN_TASK, "oracle-ish", (), workdir=tmp_path)


def test_run_cell_validation_errors_without_docker(tmp_path):
    cell = ik.run_cell(TXN_TASK, "oracle", ("skip_plant",), workdir=tmp_path)
    assert cell["verdict"] == "error" and "cheat" in (cell["reason"] or "")
    cell = ik.run_cell(TXN_TASK, "cheat", ("nope",), workdir=tmp_path)
    assert cell["verdict"] == "error" and "nope" in (cell["reason"] or "")
    cell = ik.run_cell(tmp_path / "missing", "oracle", (), workdir=tmp_path)
    assert cell["verdict"] == "error"
    cell = ik.run_cell(make_task(tmp_path / "syn"), "oracle", (), workdir=tmp_path)
    assert cell["verdict"] == "error" and "adapter" in (cell["reason"] or "")


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
        assert "--dev" in cmd  # per-agent fake_model rides the dev bind-mount
        config_path = cmd[cmd.index("--config") + 1]
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        assert config["use_fake_model"] is True
        out = Path(kwargs["cwd"]) / config["transcript_file"]
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
    def _assemble(task, adapter, env_dir):
        (Path(env_dir) / "src" / "environment").mkdir(parents=True, exist_ok=True)
        return {"env_dir": str(env_dir)}

    monkeypatch.setattr(ik, "docker_daemon_ok", lambda timeout=30: (True, "mock"))
    monkeypatch.setattr(ik, "assemble_env", _assemble)
    monkeypatch.setattr(ik, "resolved_karotte_version", lambda env_dir: "3.0.59")
    monkeypatch.setattr(
        ik, "ensure_image", lambda env_dir: {"tag": "t", "digest": "d", "rebuilt": False}
    )
    monkeypatch.setattr(
        ik, "_watch_bounds", lambda name, stop, record: record.update(bounded=True)
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
    assert evidence["container_bounded_2cpu_2g"] is True
    assert evidence["container_removed"] is True
    monkeypatch.setattr(ik.subprocess, "run", _stub_run_factory(0.0))
    cell = ik.run_cell(TXN_TASK, "nop", (), workdir=work)
    assert cell["verdict"] == "fail" and cell["reward"] == 0.0
