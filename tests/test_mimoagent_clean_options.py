"""Opt-in trial options for the Xiaomi mimoagent lane (mimo-clean program).

Behavioral coverage for the recorded-in-metadata options: the Xiaomi
antihack guard on/off (default off), the explicit-rules instruction addendum
(default off), and the task chain digest. The adapter normalizes Harbor
agent kwargs, forwards the flags to the pinned worker, and records the
effective values in trial metadata whether on or off.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.execution_contracts import (
    MIMO_ANTIHACK_ENV_VAR,
    MIMO_EXPLICIT_RULES_ENV_VAR,
    MIMO_SELFHOSTED_MODEL_SELECTOR,
    MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV,
    TERMINUS_PROXY_URL_ENV,
    mimoagent_agent_kwargs,
)
from evallab.mimoagent_trajectory import native_to_atif


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("", False),
        (None, False),
        (0, False),
        (1, True),
        (2, False),
    ],
)
def test_flag_normalization_is_fail_closed(value, expected):
    pytest.importorskip("harbor.models.agent.context", reason="adapter needs Harbor")
    from evallab.harbor_mimoagent import _flag

    assert _flag(value) is expected


def test_explicit_rules_addendum_uses_vals_tested_wording():
    pytest.importorskip("harbor.models.agent.context", reason="adapter needs Harbor")
    from evallab.harbor_mimoagent import EXPLICIT_RULES_ADDENDUM

    assert "future or unreachable Git commits" in EXPLICIT_RULES_ADDENDUM
    assert "upstream patches" in EXPLICIT_RULES_ADDENDUM
    assert "newer package versions" in EXPLICIT_RULES_ADDENDUM


def test_with_explicit_rules_preserves_instruction_and_appends_paragraph():
    pytest.importorskip("harbor.models.agent.context", reason="adapter needs Harbor")
    from evallab.harbor_mimoagent import EXPLICIT_RULES_ADDENDUM, _with_explicit_rules

    body = "Repair /testbed/task.py\n"
    rendered = _with_explicit_rules(body)
    assert rendered.startswith("Repair /testbed/task.py\n\n")
    assert rendered.endswith(EXPLICIT_RULES_ADDENDUM)


def _run_agent(tmp_path, events, **kwargs):
    """Drive NativeMimoAgent.run against a stub worker that replays events."""
    event_file = tmp_path / "events.json"
    event_file.write_text(json.dumps(events))
    initial_path = tmp_path / "initial.json"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, pathlib, sys\n"
        "initial = json.loads(sys.stdin.readline())\n"
        f"pathlib.Path({str(initial_path)!r}).write_text(json.dumps(initial))\n"
        f"events = json.loads(pathlib.Path({str(event_file)!r}).read_text())\n"
        "for event in events:\n"
        "    print(json.dumps(event), flush=True)\n"
    )
    return worker, initial_path


def _idle_finished(*, antihack_blocks=0):
    return {
        "event": "finished",
        "exit_status": "Idle",
        "result": "done",
        "model_stats": {},
        "antihack": False,
        "antihack_blocks": antihack_blocks,
        "explicit_rules": False,
    }


class _Environment:
    async def exec(self, *_args, **_kwargs):
        return SimpleNamespace(return_code=0, stdout="/testbed\n")


def _base_events():
    return [
        {"event": "agent_start", "name": "main", "tools": [], "tool_choice": "auto"},
        {
            "event": "message",
            "name": "main",
            "message": {"role": "user", "content": "fix the task"},
        },
        _idle_finished(),
    ]


def test_adapter_defaults_record_off_and_forward_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    AgentContext = pytest.importorskip("harbor.models.agent.context").AgentContext
    import evallab.harbor_mimoagent as module
    from evallab.harbor_mimoagent import EXPLICIT_RULES_ADDENDUM

    worker, initial_path = _run_agent(tmp_path, _base_events())
    monkeypatch.setattr(module, "_NATIVE_PYTHON", Path(sys.executable))
    monkeypatch.setattr(module, "_WORKER", worker)
    monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, "http://127.0.0.1:1/v1")
    monkeypatch.setenv(MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV, "test-capability")
    agent = module.NativeMimoAgent(
        logs_dir=tmp_path / "agent", model_name=MIMO_SELFHOSTED_MODEL_SELECTOR
    )
    context = AgentContext()
    asyncio.run(agent.run("Fix the following issue:\n\nfix the task", _Environment(), context))
    initial = json.loads(initial_path.read_text())
    assert initial["instruction"] == "fix the task"
    assert initial["antihack"] is False
    assert initial["explicit_rules"] is False
    assert EXPLICIT_RULES_ADDENDUM not in initial["instruction"]
    assert context.metadata["antihack"] is False
    assert context.metadata["explicit_rules"] is False
    assert context.metadata["task_chain"] is None
    assert context.metadata["task_chain_digest"] is None
    assert context.metadata["antihack_blocks"] == 0


def test_adapter_options_record_on_and_reach_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    AgentContext = pytest.importorskip("harbor.models.agent.context").AgentContext
    import evallab.harbor_mimoagent as module
    from evallab.harbor_mimoagent import EXPLICIT_RULES_ADDENDUM
    events = _base_events()
    events[-1] = _idle_finished(antihack_blocks=2)
    worker, initial_path = _run_agent(tmp_path, events)
    monkeypatch.setattr(module, "_NATIVE_PYTHON", Path(sys.executable))
    monkeypatch.setattr(module, "_WORKER", worker)
    monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, "http://127.0.0.1:1/v1")
    monkeypatch.setenv(MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV, "test-capability")
    agent = module.NativeMimoAgent(
        logs_dir=tmp_path / "agent",
        model_name=MIMO_SELFHOSTED_MODEL_SELECTOR,
        antihack="true",
        explicit_rules=True,
        task_chain="strip-future-history@1>separate-verifier@2",
        task_chain_digest="sha256:abc123",
    )
    context = AgentContext()
    asyncio.run(agent.run("fix the task", _Environment(), context))
    initial = json.loads(initial_path.read_text())
    assert initial["antihack"] is True
    assert initial["explicit_rules"] is True
    assert initial["instruction"].endswith(EXPLICIT_RULES_ADDENDUM)
    assert "fix the task" in initial["instruction"]
    assert context.metadata["antihack"] is True
    assert context.metadata["explicit_rules"] is True
    assert context.metadata["antihack_blocks"] == 2
    assert context.metadata["task_chain"] == "strip-future-history@1>separate-verifier@2"
    assert context.metadata["task_chain_digest"] == "sha256:abc123"


def test_trajectory_carries_trial_options():
    assistant = {
        "role": "assistant",
        "content": "done",
        "tool_calls": [],
    }
    native = {
        "info": {
            "exit_status": "Idle",
            "result": "done",
            "antihack": True,
            "antihack_blocks": 1,
            "explicit_rules": True,
        },
        "trajs": {
            "main": {
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "fix"},
                    assistant,
                ],
                "tools": [],
                "tool_choice": "auto",
            }
        },
    }
    atif = native_to_atif(
        native, [], trajectory_id="traj-1", model_name=MIMO_SELFHOSTED_MODEL_SELECTOR
    )
    assert atif["extra"]["antihack"] is True
    assert atif["extra"]["antihack_blocks"] == 1
    assert atif["extra"]["explicit_rules"] is True
    assert atif["agent"]["extra"]["antihack"] is True


def _fixture_task(tmp_path: Path) -> Path:
    task_dir = tmp_path / "task"
    task_dir.mkdir(exist_ok=True)
    (task_dir / "task.toml").write_text('schema_version = "1.4"\n')
    return task_dir


def test_kwargs_default_off_with_exact_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MIMO_ANTIHACK_ENV_VAR, raising=False)
    monkeypatch.delenv(MIMO_EXPLICIT_RULES_ENV_VAR, raising=False)
    kwargs = mimoagent_agent_kwargs(_fixture_task(tmp_path))
    assert kwargs["antihack"] is False
    assert kwargs["explicit_rules"] is False
    assert kwargs["task_chain_digest"].startswith("sha256:")
    assert "task_chain" not in kwargs


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_kwargs_env_knobs_arm_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
):
    monkeypatch.setenv(MIMO_ANTIHACK_ENV_VAR, value)
    monkeypatch.setenv(MIMO_EXPLICIT_RULES_ENV_VAR, value)
    kwargs = mimoagent_agent_kwargs(_fixture_task(tmp_path))
    assert kwargs["antihack"] is True
    assert kwargs["explicit_rules"] is True


@pytest.mark.parametrize("value", ["0", "false", "False", "", "off"])
def test_kwargs_env_knobs_stay_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
):
    monkeypatch.setenv(MIMO_ANTIHACK_ENV_VAR, value)
    monkeypatch.setenv(MIMO_EXPLICIT_RULES_ENV_VAR, value)
    kwargs = mimoagent_agent_kwargs(_fixture_task(tmp_path))
    assert kwargs["antihack"] is False
    assert kwargs["explicit_rules"] is False


def test_kwargs_serialize_for_harbor_agent_kwarg(tmp_path: Path, monkeypatch):
    import json as json_lib

    monkeypatch.delenv(MIMO_ANTIHACK_ENV_VAR, raising=False)
    monkeypatch.delenv(MIMO_EXPLICIT_RULES_ENV_VAR, raising=False)
    kwargs = mimoagent_agent_kwargs(_fixture_task(tmp_path))
    rendered = {
        key: json_lib.loads(json_lib.dumps(value, separators=(",", ":")))
        for key, value in kwargs.items()
    }
    assert rendered == kwargs
    assert rendered["antihack"] is False


def test_real_adapter_accepts_emitted_kwargs(tmp_path: Path):
    """Harbor 0.24 has no options_model for import-path agents; the adapter
    itself normalizes whatever the kwarg parser delivers."""
    pytest.importorskip("harbor.models.agent.context", reason="adapter needs Harbor")
    import evallab.harbor_mimoagent as module

    agent = module.NativeMimoAgent(
        logs_dir=tmp_path / "agent",
        model_name=MIMO_SELFHOSTED_MODEL_SELECTOR,
        antihack=True,
        explicit_rules=False,
        task_chain_digest="sha256:abc123",
    )
    assert agent._antihack is True
    assert agent._explicit_rules is False
    assert agent._task_chain is None
    assert agent._task_chain_digest == "sha256:abc123"
    module.NativeMimoAgent.preflight(
        {"antihack": True, "explicit_rules": False, "task_chain_digest": "sha256:abc123"},
        {},
    )


def test_format_task_chain_orders_oldest_first():
    from evallab.execution_contracts import format_task_chain

    assert format_task_chain([]) == "original"
    assert format_task_chain(["only@1"]) == "only@1"
    assert (
        format_task_chain(["separate-verifier@2", "strip-future-history@1"])
        == "strip-future-history@1>separate-verifier@2"
    )


def test_kwargs_chain_resolves_through_tmp_lineage(tmp_path: Path, monkeypatch):
    from evallab.execution_contracts import mimoagent_agent_kwargs
    from evallab.task_variants import derive_task, materialize

    monkeypatch.delenv(MIMO_ANTIHACK_ENV_VAR, raising=False)
    monkeypatch.delenv(MIMO_EXPLICIT_RULES_ENV_VAR, raising=False)
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "task.toml").write_text(
        'schema_version = "1.4"\n[task]\nname = "smoke-chain-task"\n'
    )
    (parent / "instruction.md").write_text("do the thing\n")
    first = derive_task(
        parent,
        changes={"instruction.md": b"do the thing, then more\n"},
        transform="probe-a@1",
        rationale="smoke",
        created_by="smoke",
        repo_root=tmp_path,
        variants_root=tmp_path / "variants",
    )
    first_pkg = materialize(
        first, parent_dir=parent, repo_root=tmp_path, variants_root=tmp_path / "variants"
    )
    second = derive_task(
        first_pkg,
        changes={"instruction.md": b"do the thing, then even more\n"},
        transform="probe-b@1",
        rationale="smoke",
        created_by="smoke",
        parent_source={"kind": "variant", "record": first.record_relpath().as_posix()},
        repo_root=tmp_path,
        variants_root=tmp_path / "variants",
    )
    second_pkg = materialize(
        second,
        parent_dir=first_pkg,
        repo_root=tmp_path,
        variants_root=tmp_path / "variants",
    )
    kwargs = mimoagent_agent_kwargs(second_pkg, repo_root=tmp_path)
    assert kwargs["task_chain"] == "probe-a@1>probe-b@1"
    assert kwargs["task_chain_digest"] == second.variant_digest


def test_emitted_kwargs_pass_real_harbor_preflight(tmp_path: Path, monkeypatch):
    harbor_factory = pytest.importorskip(
        "harbor.agents.factory", reason="Harbor 0.24 is not installed here"
    )
    parse_kwargs = pytest.importorskip(
        "harbor.cli.utils", reason="Harbor 0.24 is not installed here"
    ).parse_kwargs
    monkeypatch.delenv(MIMO_ANTIHACK_ENV_VAR, raising=False)
    monkeypatch.delenv(MIMO_EXPLICIT_RULES_ENV_VAR, raising=False)
    kwargs = mimoagent_agent_kwargs(_fixture_task(tmp_path), repo_root=tmp_path)
    rendered = [
        f"{key}={json.dumps(value, separators=(',', ':'))}"
        for key, value in sorted(kwargs.items())
    ]
    parsed = parse_kwargs(rendered)
    config = SimpleNamespace(
        name="evallab.harbor_mimoagent:NativeMimoAgent", import_path=None
    )
    agent_class = harbor_factory.AgentFactory.get_agent_class_from_config(config)
    import evallab.harbor_mimoagent as module

    assert agent_class is module.NativeMimoAgent
    agent_class.preflight(parsed, {})
