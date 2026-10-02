from __future__ import annotations

import asyncio
import json
import sys
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
def test_native_wrapper_stop_paths_preserve_verifier_eligible_budget_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal,
    status,
    native_result,
    expected,
    error_type,
):
    pytest.importorskip("harbor")
    from harbor.models.agent.context import AgentContext

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
    assert (
        json.loads((tmp_path / "agent" / "trajectory.json").read_text())["extra"][
            "native_exit_status"
        ]
        == status
    )
