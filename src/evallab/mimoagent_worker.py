"""Pinned Xiaomi controller subprocess; its SDK must not share Harbor's interpreter.

Only tool transport and append-only evidence are adapted. DefaultAgent, its
five native tools, prompts, parallelism, retries and stop rules are unchanged.
This file is executed by tools/mimoagent-harbor/.venv/bin/python, not imported.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

NATIVE_REVISION = "467f0a19016f0ac4d63b8d17a1f0da9ba07f232c"
SWE_SHA256 = "a03457e611f7f531105ba23e8c9f9d4360e91341e305470c90845860f3315985"
SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 20}


class SandboxRpc:
    def __init__(self, cwd: str):
        self.cwd = cwd
        self._lock = threading.Lock()
        self._pending: dict[int, Future] = {}
        self._sequence = 0
        threading.Thread(target=self._receive, daemon=True).start()

    def emit(self, event: dict) -> None:
        with self._lock:
            sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def _receive(self) -> None:
        try:
            for line in sys.stdin:
                response = json.loads(line)
                with self._lock:
                    future = self._pending.pop(response["id"])
                if "error" in response:
                    from mimoagent.environments import (  # ty: ignore[unresolved-import]
                        TransportError,
                    )

                    future.set_exception(TransportError(response["error"]))
                else:
                    future.set_result(response["result"])
        finally:
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for future in pending:
                future.set_exception(ConnectionError("Harbor sandbox transport closed"))

    def _request(self, method: str, **kwargs) -> Any:
        future = Future()
        with self._lock:
            self._sequence += 1
            request_id = self._sequence
            self._pending[request_id] = future
        self.emit({"event": "tool", "id": request_id, "method": method, **kwargs})
        return future.result()

    def execute(self, command: str, cwd: str = "", timeout: int | None = None) -> dict:
        return self._request("exec", command=command, cwd=cwd or self.cwd, timeout=timeout or 300)

    def execute_detached(self, command: str, cwd: str = "", timeout: int | None = None, **_kwargs) -> dict:
        return self.execute(command, cwd=cwd, timeout=timeout)

    def copy_to(self, src_path: str, dest_path: str, *, timeout: int = 300, max_retries: int = 10) -> None:
        # Native write creates this controller-side tempfile. No task tool may
        # access the host filesystem; only the trusted transport uploads it.
        self._request("upload", source=src_path, target=dest_path, timeout=timeout)

    def get_template_vars(self) -> dict:
        return {"cwd": self.cwd, "timeout": 300}


def main() -> None:
    initial = json.loads(sys.stdin.readline())
    # Prevent mimoagent's global .env loader from importing an unrelated host
    # configuration. This directory is created and owned by the Harbor agent.
    os.environ["MIMOAGENT_GLOBAL_CONFIG_DIR"] = initial["global_config_dir"]
    import yaml
    from mimoagent.agents.base import BaseAgent  # ty: ignore[unresolved-import]
    from mimoagent.agents.default import DefaultAgent  # ty: ignore[unresolved-import]
    from mimoagent.models.openai_chat import OpenAIChatModel  # ty: ignore[unresolved-import]
    from mimoagent.run.utils.save import save_traj  # ty: ignore[unresolved-import]
    from mimoagent.utils.log import (  # ty: ignore[unresolved-import]
        AgentLogContext,
        use_log_context,
    )

    distribution = importlib.metadata.distribution("mimoagent")
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    if direct_url.get("vcs_info", {}).get("commit_id") != NATIVE_REVISION:
        raise RuntimeError("mimoagent runtime does not match the pinned Xiaomi commit")
    config_path = Path(initial["config_path"])
    config_bytes = config_path.read_bytes()
    if hashlib.sha256(config_bytes).hexdigest() != SWE_SHA256:
        raise RuntimeError("native swe.yaml changed; refusing a non-parity harness")
    config = yaml.safe_load(config_bytes)
    rpc = SandboxRpc(initial["cwd"])
    logs = AgentLogContext.create(Path(initial["native_logs_dir"]))
    names_started: set[str] = set()
    evidence_lock = threading.Lock()
    local = threading.local()

    def name_of(agent=None, messages=None) -> str:
        with logs._lock:
            for name, candidate in logs.trajectories.items():
                if candidate is agent or (messages is not None and candidate.messages is messages):
                    return name
        raise RuntimeError("native agent was not registered for trajectory capture")

    original_add_message = BaseAgent.add_message

    def observe_message(agent, role: str, content: str, **kwargs) -> None:
        original_add_message(agent, role, content, **kwargs)
        name = name_of(agent=agent)
        with evidence_lock:
            if name not in names_started:
                names_started.add(name)
                rpc.emit({"event": "agent_start", "name": name, **agent.get_model_query_kwargs()})
            rpc.emit({"event": "message", "name": name, "message": agent.messages[-1]})

    BaseAgent.add_message = observe_message

    class RecordedModel(OpenAIChatModel):
        def _query(self, messages: list[dict], **kwargs):
            local.name = name_of(messages=messages)
            local.assistant_index = sum(message["role"] == "assistant" for message in messages)
            return super()._query(messages, **kwargs)

    model_config = config["model"] | {"model_name": initial["model_name"]}
    model_config["model_kwargs"] = config["model"]["model_kwargs"] | {
        "base_url": initial["proxy_url"],
        "api_key": initial["capability"],
        **SAMPLING,
    }
    model = RecordedModel(**model_config)

    def record_response(response) -> None:
        response.read()
        request = json.loads(response.request.content)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        choices = payload.get("choices") or []
        rpc.emit({
            "event": "model_call",
            "name": local.name,
            "assistant_index": local.assistant_index,
            "sampling": {key: request.get(key) for key in SAMPLING},
            "response_status": response.status_code,
            "usage": payload.get("usage"),
            "finish_reason": choices[0].get("finish_reason") if choices else None,
        })

    model.client._client.event_hooks["response"].append(record_response)
    agent = DefaultAgent(model=model, env=rpc, msg_path=logs.agent_msg_path("main"), **config["agent"])
    logs.register_agent("main", agent)
    status, result = None, None
    try:
        with use_log_context(logs):
            status, result = agent.run(initial["instruction"])
    finally:
        # Official native serialization is retained for parity comparisons; it
        # does not serialize model_kwargs, so no proxy capability enters it.
        save_traj(agent, Path(initial["native_trajectory_path"]), print_path=False,
                  exit_status=status, result=result, log_context=logs,
                  extra_info={"native_revision": NATIVE_REVISION, "swe_sha256": SWE_SHA256,
                              "sampling": SAMPLING, "antihack": False})
        model.client.close()
        logs.close()
    rpc.emit({"event": "finished", "exit_status": status, "result": result,
              "model_stats": {"api_calls": model.n_calls, **vars(model.token_stats)}})


if __name__ == "__main__":
    main()
