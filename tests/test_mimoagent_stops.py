from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.mimoagent_worker import proxy_budget_reason
from evallab.step_layers import classify_stop_reason


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (429, "trial budget exhausted\n", "trial budget exhausted"),
        (
            429,
            "trial budget exhausted: ceiling:input_tokens\n",
            "trial budget exhausted: ceiling:input_tokens",
        ),
        (
            429,
            '{"error":{"reason":"ceiling:requests","message":"trial budget exhausted"}}',
            "ceiling:requests",
        ),
        (429, '{"error":{"message":"provider rate limit exceeded"}}', None),
        (503, "trial budget exhausted", None),
        (200, "trial budget exhausted", None),
    ],
)
def test_proxy_refusal_preserves_dimension_without_confusing_provider_throttling(
    status, body, expected
):
    assert proxy_budget_reason(status, body) == expected


@pytest.mark.parametrize(
    ("refusal", "status", "native_result", "expected", "error_type"),
    [
        (
            "trial budget exhausted",
            "ModelQueryError",
            "query failed",
            "trial_budget_exhausted",
            "TrialBudgetExhaustedError",
        ),
        (
            "ceiling:input_tokens",
            "ModelQueryError",
            "query failed",
            "ceiling:input_tokens",
            "TrialBudgetExhaustedError",
        ),
        (
            None,
            "ModelQueryError",
            "query failed: trial budget exhausted",
            "trial_budget_exhausted",
            "TrialBudgetExhaustedError",
        ),
        (None, "ModelQueryError", "provider rate limit exceeded", "error", "RuntimeError"),
        (None, "LimitsExceeded", "", "harness_step_limit", None),
        (None, "LimitsExceeded", "Empty assistant response", "error", None),
        (None, "Idle", "done", "task_complete", None),
    ],
)
@pytest.mark.parametrize("observer_fault", [None, "replace"])
def test_native_wrapper_stop_paths_preserve_verifier_eligible_budget_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal,
    status,
    native_result,
    expected,
    error_type,
    observer_fault,
):
    # Other adapter tests install a top-level Harbor stub when the optional
    # controller dependency is absent; require the real submodule, not that stub.
    AgentContext = pytest.importorskip("harbor.models.agent.context").AgentContext

    import evallab.harbor_mimoagent as module
    from evallab.execution_contracts import (
        MIMO_SELFHOSTED_MODEL_SELECTOR,
        MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV,
        TERMINUS_PROXY_URL_ENV,
    )
    from evallab.harbor_terminus import TrialBudgetExhaustedError

    events = [
        {"event": "agent_start", "name": "main", "tools": [], "tool_choice": "auto"},
        {
            "event": "message",
            "name": "main",
            "message": {"role": "user", "content": "fix the task"},
        },
        {
            "event": "model_call",
            "name": "main",
            "assistant_index": 0,
            "response_status": 429 if status == "ModelQueryError" else 200,
            "usage": None,
            "proxy_budget_reason": refusal,
        },
        {"event": "finished", "exit_status": status, "result": native_result, "model_stats": {}},
    ]
    event_file = tmp_path / "events.json"
    event_file.write_text(json.dumps(events))
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, pathlib, sys\n"
        "json.loads(sys.stdin.readline())\n"
        f"events = json.loads(pathlib.Path({str(event_file)!r}).read_text())\n"
        "for event in events:\n"
        "    print(json.dumps(event), flush=True)\n"
    )
    monkeypatch.setattr(module, "_NATIVE_PYTHON", Path(sys.executable))
    monkeypatch.setattr(module, "_WORKER", worker)
    monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, "http://127.0.0.1:1/v1")
    monkeypatch.setenv(MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV, "test-capability")

    class Environment:
        async def exec(self, *_args, **_kwargs):
            return SimpleNamespace(return_code=0, stdout="/testbed\n")

    agent = module.NativeMimoAgent(
        logs_dir=tmp_path / "agent", model_name=MIMO_SELFHOSTED_MODEL_SELECTOR
    )
    context = AgentContext()
    trajectory_path = tmp_path / "agent" / "trajectory.json"
    last_good = None
    if observer_fault is not None:
        agent.logs_dir.mkdir(parents=True, exist_ok=True)
        agent._native["trajs"]["main"] = {
            "messages": [{"role": "user", "content": "last good prefix"}],
            "tools": [],
        }
        agent._publish(*agent._snapshot())
        last_good = trajectory_path.read_bytes()
        agent._native["trajs"].clear()
        original_replace = Path.replace

        def fail_trajectory_replace(path, target):
            if path == agent.logs_dir / ".trajectory.json.tmp":
                raise OSError("secret observer path /controller/private")
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_trajectory_replace)
    caught = None
    try:
        asyncio.run(agent.run("fix the task", Environment(), context))
    except Exception as exc:
        caught = exc
    assert (type(caught).__name__ if caught else None) == error_type
    if error_type == "TrialBudgetExhaustedError":
        from harbor.agents.installed.base import NonZeroAgentExitCodeError

        assert isinstance(caught, TrialBudgetExhaustedError)
        assert isinstance(caught, NonZeroAgentExitCodeError)
        assert context.metadata["stop_reason"] == expected
    reason, _ = classify_stop_reason(
        agent_metadata=context.metadata,
        exception_info={"exception_type": type(caught).__name__} if caught else {},
    )
    assert reason == expected
    assert context.n_input_tokens is None
    assert context.n_output_tokens is None
    assert context.metadata["native_exit_status"] == status
    assert context.metadata["model_requests"] == 1
    if observer_fault is not None:
        assert trajectory_path.read_bytes() == last_good
    else:
        assert json.loads(trajectory_path.read_text())["extra"]["native_exit_status"] == status


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("fail_later", [False, True])
def test_live_prefixes_are_isolated_nonblocking_and_cannot_overwrite_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool, fail_later: bool
):
    AgentContext = pytest.importorskip("harbor.models.agent.context").AgentContext
    import evallab.harbor_mimoagent as module
    from evallab.execution_contracts import (
        MIMO_SELFHOSTED_MODEL_SELECTOR,
        MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV,
        TERMINUS_PROXY_URL_ENV,
    )

    prefix = [
        {"event": "agent_start", "name": "main", "tools": [], "tool_choice": "auto"},
        {"event": "message", "name": "main", "message": {"role": "user", "content": "fix"}},
        {
            "event": "model_call", "name": "main", "assistant_index": 0,
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}, "response_status": 200,
        },
        {
            "event": "message", "name": "main",
            "message": {
                "role": "assistant", "content": "working",
                "tool_calls": [{
                    "id": "first-call", "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
                }],
            },
        },
        {
            "event": "message", "name": "main",
            "message": {
                "role": "tool", "name": "bash", "tool_call_id": "first-call",
                "content": "/testbed\nBearer test-capability",
            },
        },
        {"event": "step_complete", "name": "main", "native_step": 1, "message_count": 3},
        {"event": "tool", "id": 1, "method": "exec", "command": "next", "cwd": "/testbed", "timeout": 30},
    ]
    suffix = [
        {
            "event": "model_call", "name": "main", "assistant_index": 1,
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}, "response_status": 200,
        },
        {"event": "message", "name": "main", "message": {"role": "assistant", "content": "done"}},
        {"event": "step_complete", "name": "main", "native_step": 2, "message_count": 4},
        {"event": "finished", "exit_status": "Idle", "result": "done", "model_stats": {"api_calls": 2}},
    ]
    pid_path = tmp_path / "pid"
    worker = tmp_path / "interactive-worker.py"
    worker.write_text(
        "import json, os, pathlib, sys, time\n"
        "json.loads(sys.stdin.readline())\n"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
        f"prefix = {prefix!r}\n"
        "for event in prefix:\n"
        "    print(json.dumps(event), flush=True)\n"
        "assert json.loads(sys.stdin.readline())['id'] == 1\n"
        + ("time.sleep(60)\n" if cancel else (
            f"suffix = {suffix!r}\n"
            "for event in suffix:\n"
            "    print(json.dumps(event), flush=True)\n"
        ))
    )
    monkeypatch.setattr(module, "_NATIVE_PYTHON", Path(sys.executable))
    monkeypatch.setattr(module, "_WORKER", worker)
    monkeypatch.setenv(TERMINUS_PROXY_URL_ENV, "http://127.0.0.1:1/v1")
    monkeypatch.setenv(MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV, "test-capability")
    agent = module.NativeMimoAgent(
        logs_dir=tmp_path / "agent", model_name=MIMO_SELFHOSTED_MODEL_SELECTOR
    )
    context = AgentContext()
    writer_started = threading.Event()
    release_writer = threading.Event()
    published = []
    original_publish = agent._publish
    original_replace = Path.replace

    def fail_later_replace(path, target):
        if fail_later and published and path == agent.logs_dir / ".trajectory.json.tmp":
            raise OSError("disk failed after the last good prefix")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_later_replace)

    def gated_publish(native, calls):
        if not published:
            writer_started.set()
            if not release_writer.wait(10):
                raise TimeoutError("fixture writer was not released")
        original_publish(native, calls)
        published.append(json.loads((agent.logs_dir / "trajectory.json").read_text()))

    monkeypatch.setattr(agent, "_publish", gated_publish)

    async def scenario():
        rpc_seen = asyncio.Event()
        settling = asyncio.Event()
        original_update_context = agent._update_context

        def observe_context(ctx):
            original_update_context(ctx)
            settling.set()

        monkeypatch.setattr(agent, "_update_context", observe_context)

        class Environment:
            async def exec(self, command, **_kwargs):
                if command == "next":
                    rpc_seen.set()
                return SimpleNamespace(return_code=0, stdout="/testbed\n", stderr="")

        running = asyncio.create_task(agent.run("fix", Environment(), context))
        try:
            assert await asyncio.to_thread(writer_started.wait, 5)
            await asyncio.wait_for(rpc_seen.wait(), timeout=5)
            if cancel:
                running.cancel()
            await asyncio.wait_for(settling.wait(), timeout=5)
            assert not running.done()
            assert not (agent.logs_dir / "trajectory.json").exists()
            release_writer.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await running
            else:
                await running
        finally:
            release_writer.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())
    final = json.loads((agent.logs_dir / "trajectory.json").read_text())
    expected = module.Trajectory.model_validate(
        module.native_to_atif(
            agent._native, agent._calls,
            trajectory_id=agent._trajectory_id, model_name=agent.model_name,
        )
    ).model_dump(mode="json", exclude_none=True)
    expected = json.loads(module.redact_secret_material(
        json.dumps(expected, ensure_ascii=False, indent=2).encode(), agent._secrets
    ))
    if fail_later:
        assert final == published[0]
        assert final["final_metrics"]["total_prompt_tokens"] == 7
    else:
        assert final == expected
        assert [len(document["steps"]) for document in published] == ([2, 2] if cancel else [2, 3, 3])
    assert published[-1] == final
    assert published[0]["final_metrics"]["total_prompt_tokens"] == 7
    observation = published[0]["steps"][1]["observation"]["results"][0]
    assert observation["source_call_id"] == "first-call"
    assert "/testbed" in observation["content"]
    assert "test-capability" not in json.dumps(published)
    assert all(document["trajectory_id"] == agent._trajectory_id for document in published)
    assert context.metadata["native_exit_status"] == ("HarborCancelled" if cancel else "Idle")
    assert context.n_input_tokens == (7 if cancel else 14)
    assert context.n_output_tokens == (3 if cancel else 6)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)


def test_redaction_failure_preserves_last_valid_trajectory(tmp_path, monkeypatch):
    pytest.importorskip("harbor.models.agent.context")
    import evallab.harbor_mimoagent as module
    from evallab.execution_contracts import MIMO_SELFHOSTED_MODEL_SELECTOR

    agent = module.NativeMimoAgent(logs_dir=tmp_path, model_name=MIMO_SELFHOSTED_MODEL_SELECTOR)
    agent._native["trajs"]["main"] = {
        "messages": [{"role": "user", "content": "unchanged /testbed content"}], "tools": [],
    }
    agent._publish(*agent._snapshot())
    path = tmp_path / "trajectory.json"
    good = path.read_bytes()
    monkeypatch.setattr(module, "redact_secret_material", lambda *_: b'{"broken":')
    agent._native["trajs"]["main"]["messages"].append({"role": "assistant", "content": "later"})
    agent._publish(*agent._snapshot())
    assert path.read_bytes() == good
    assert not (tmp_path / ".trajectory.json.tmp").exists()


@pytest.mark.parametrize("suffix", ["", '\n{"token": "legitimate /testbed content"}'])
def test_bearer_redaction_keeps_latest_trajectory_json_intact(tmp_path, suffix):
    pytest.importorskip("harbor.models.agent.context")
    from harbor.models.trajectories.trajectory import Trajectory

    import evallab.harbor_mimoagent as module
    from evallab.execution_contracts import (
        MIMO_SELFHOSTED_MODEL_SELECTOR,
        REDACTED_SECRET_VALUE,
    )

    agent = module.NativeMimoAgent(logs_dir=tmp_path, model_name=MIMO_SELFHOSTED_MODEL_SELECTOR)
    agent._native["trajs"]["main"] = {
        "messages": [{"role": "user", "content": "unchanged /testbed content"}], "tools": [],
    }
    agent._publish(*agent._snapshot())
    agent._native["trajs"]["main"]["messages"].append({
        "role": "assistant",
        "content": f"Authorization: Bearer final-only-token{suffix}",
    })
    agent._publish(*agent._snapshot())

    data = (tmp_path / "trajectory.json").read_bytes()
    final = Trajectory.model_validate_json(data)
    assert final.steps[-1].message == f"Authorization: Bearer {REDACTED_SECRET_VALUE}{suffix}"
    assert b"final-only-token" not in data
    assert not (tmp_path / ".trajectory.json.tmp").exists()


@pytest.mark.parametrize("fail_write", [False, True])
def test_partial_temporary_write_never_exposes_torn_trajectory(tmp_path, monkeypatch, fail_write):
    pytest.importorskip("harbor.models.agent.context")
    import evallab.harbor_mimoagent as module
    from evallab.execution_contracts import MIMO_SELFHOSTED_MODEL_SELECTOR

    agent = module.NativeMimoAgent(logs_dir=tmp_path, model_name=MIMO_SELFHOSTED_MODEL_SELECTOR)
    agent._native["trajs"]["main"] = {
        "messages": [{"role": "user", "content": "first intact document"}], "tools": [],
    }
    agent._publish(*agent._snapshot())
    trajectory_path = tmp_path / "trajectory.json"
    good = trajectory_path.read_bytes()
    agent._native["trajs"]["main"]["messages"].append({"role": "assistant", "content": "second"})
    partial_written = threading.Event()
    release_write = threading.Event()
    original_write = Path.write_bytes

    def partial_write(path, data):
        if path != tmp_path / ".trajectory.json.tmp":
            return original_write(path, data)
        with path.open("wb") as handle:
            halfway = len(data) // 2
            handle.write(data[:halfway])
            handle.flush()
            partial_written.set()
            if not release_write.wait(5):
                raise TimeoutError("fixture partial write was not released")
            if fail_write:
                raise OSError("disk full")
            return halfway + handle.write(data[halfway:])

    monkeypatch.setattr(Path, "write_bytes", partial_write)
    writer = threading.Thread(target=agent._publish, args=agent._snapshot())
    writer.start()
    try:
        assert partial_written.wait(5)
        assert trajectory_path.read_bytes() == good
        module.Trajectory.model_validate_json(trajectory_path.read_bytes())
    finally:
        release_write.set()
        writer.join(timeout=5)
    assert not writer.is_alive()
    published = module.Trajectory.model_validate_json(trajectory_path.read_bytes()).model_dump(
        mode="json", exclude_none=True
    )
    if fail_write:
        assert trajectory_path.read_bytes() == good
    else:
        assert [step["message"] for step in published["steps"]] == ["first intact document", "second"]
    assert not (tmp_path / ".trajectory.json.tmp").exists()
