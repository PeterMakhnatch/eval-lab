"""Native Xiaomi DefaultAgent outside the task sandbox, with Harbor tool transport."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import uuid
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent  # ty: ignore[unresolved-import]
from harbor.environments.base import BaseEnvironment  # ty: ignore[unresolved-import]
from harbor.models.agent.context import AgentContext  # ty: ignore[unresolved-import]
from harbor.models.trajectories.trajectory import Trajectory  # ty: ignore[unresolved-import]

from evallab.execution_contracts import (
    MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV,
    TERMINUS_PROXY_URL_ENV,
    collected_secret_values,
    parse_mimo_selfhosted_model,
    redact_secret_material,
)
from evallab.mimoagent_trajectory import native_to_atif
from evallab.mimoagent_worker import NATIVE_REVISION

_RUNTIME_ROOT = Path(__file__).resolve().parents[2]
_NATIVE_PYTHON = _RUNTIME_ROOT / "tools/mimoagent-harbor/.venv/bin/python"
_NATIVE_CONFIG = _RUNTIME_ROOT / "tools/mimoagent-harbor/swe.yaml"
_WORKER = Path(__file__).with_name("mimoagent_worker.py")


class NativeMimoAgent(BaseAgent):
    SUPPORTS_ATIF = True

    def __init__(self, logs_dir: Path, model_name: str | None = None, **kwargs: Any):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        parse_mimo_selfhosted_model(model_name)
        self._native: dict = {"trajectory_format": "mimoagent", "info": {}, "trajs": {}}
        self._calls: list[dict] = []
        self._trajectory_id = str(uuid.uuid4())
        self._secrets = tuple(secret.encode() for secret in collected_secret_values(os.environ))

    @staticmethod
    def name() -> str:
        return "mimoagent"

    def version(self) -> str:
        return NATIVE_REVISION

    async def setup(self, environment: BaseEnvironment) -> None:
        # No package, controller, model endpoint or credential is installed in
        # the task image. Native OpenAI 3.x and Harbor/LiteLLM 2.x are isolated.
        if not _NATIVE_PYTHON.is_file():
            raise RuntimeError(
                "native runtime missing: uv sync --project tools/mimoagent-harbor --locked"
            )
        if not _NATIVE_CONFIG.is_file():
            raise RuntimeError("pinned native swe.yaml is missing")

    def _persist(self, context: AgentContext) -> None:
        if not self._native["trajs"].get("main", {}).get("messages"):
            return
        atif = native_to_atif(
            self._native, self._calls, trajectory_id=self._trajectory_id, model_name=self.model_name
        )
        payload = Trajectory.model_validate(atif).model_dump(mode="json", exclude_none=True)
        text = redact_secret_material(
            json.dumps(payload, ensure_ascii=False, indent=2).encode(), self._secrets
        )
        temporary = self.logs_dir / ".trajectory.json.tmp"
        temporary.write_bytes(text + b"\n")
        temporary.replace(self.logs_dir / "trajectory.json")
        totals = payload["final_metrics"]
        context.n_input_tokens = totals.get("total_prompt_tokens")
        context.n_output_tokens = totals.get("total_completion_tokens")
        context.n_cache_tokens = totals.get("total_cached_tokens")
        context.metadata = {
            "native_revision": NATIVE_REVISION,
            "native_exit_status": self._native["info"].get("exit_status"),
            "model_requests": len(self._calls),
            "antihack": False,
        }
        if self._native["info"].get("exit_status") == "LimitsExceeded":
            context.metadata["native_exit_result"] = self._native["info"].get("result")
        if self._native["info"].get("stop_reason") is not None:
            context.metadata["stop_reason"] = self._native["info"]["stop_reason"]

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        proxy_url = os.environ.get(TERMINUS_PROXY_URL_ENV)
        capability = os.environ.get(MIMO_SELFHOSTED_PROXY_CAPABILITY_ENV)
        if not proxy_url or not capability:
            raise RuntimeError(
                "mimoagent requires the runner's host proxy URL and trial capability"
            )
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        native_logs = self.logs_dir / "mimoagent"
        native_logs.mkdir(exist_ok=True)
        global_config = native_logs / "global-config"
        global_config.mkdir(exist_ok=True)
        cwd_result = await environment.exec("pwd", timeout_sec=30)
        if (
            cwd_result.return_code != 0
            or not cwd_result.stdout
            or not cwd_result.stdout.strip().startswith("/")
        ):
            raise RuntimeError("cannot determine native task working directory")
        initial = {
            "instruction": instruction,
            "cwd": cwd_result.stdout.strip(),
            "model_name": self.model_name,
            "proxy_url": proxy_url,
            "capability": capability,
            "config_path": str(_NATIVE_CONFIG),
            "global_config_dir": str(global_config),
            "native_logs_dir": str(native_logs),
            "native_trajectory_path": str(native_logs / "native-trajectory.json"),
        }
        worker_env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "TMPDIR", "LANG", "LC_ALL"}
        }
        worker_env["PYTHONUNBUFFERED"] = "1"
        tasks: set[asyncio.Task] = set()
        response_lock = asyncio.Lock()
        finished = False
        with (native_logs / "worker-stderr.txt").open("wb") as stderr:
            process = await asyncio.create_subprocess_exec(
                str(_NATIVE_PYTHON),
                "-I",
                str(_WORKER),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                env=worker_env,
                start_new_session=True,
                limit=16 * 1024 * 1024,
            )
            stdin = process.stdin
            assert stdin is not None and process.stdout is not None

            async def tool_request(request: dict) -> None:
                response: dict = {"id": request["id"]}
                try:
                    if request["method"] == "exec":
                        result = await environment.exec(
                            request["command"], cwd=request["cwd"], timeout_sec=request["timeout"]
                        )
                        response["result"] = {
                            "output": (result.stdout or "") + (result.stderr or ""),
                            "returncode": result.return_code,
                        }
                    elif request["method"] == "upload":
                        parent = str(Path(request["target"]).parent)
                        if parent != ".":
                            result = await environment.exec(
                                f"mkdir -p -- {shlex.quote(parent)}", timeout_sec=request["timeout"]
                            )
                            if result.return_code != 0:
                                raise RuntimeError(
                                    result.stderr
                                    or result.stdout
                                    or "native write directory creation failed"
                                )
                        upload = (
                            environment.upload_dir
                            if Path(request["source"]).is_dir()
                            else environment.upload_file
                        )
                        await asyncio.wait_for(
                            upload(request["source"], request["target"]), timeout=request["timeout"]
                        )
                        response["result"] = None
                    else:
                        raise ValueError(f"unknown native tool transport: {request['method']}")
                except TimeoutError:
                    if request["method"] == "exec":
                        response["result"] = {
                            "output": "",
                            "returncode": -1,
                            "reason": "client_timeout",
                        }
                    else:
                        response["error"] = "native file upload timed out"
                except Exception as exc:
                    response["error"] = str(exc)
                async with response_lock:
                    if process.returncode is None:
                        stdin.write((json.dumps(response) + "\n").encode())
                        await stdin.drain()

            try:
                stdin.write((json.dumps(initial) + "\n").encode())
                await stdin.drain()
                while line := await process.stdout.readline():
                    event = json.loads(line)
                    kind = event["event"]
                    if kind == "tool":
                        task = asyncio.create_task(tool_request(event))
                        tasks.add(task)
                    elif kind == "agent_start":
                        self._native["trajs"][event["name"]] = {
                            "messages": [],
                            "tools": event["tools"],
                            "tool_choice": event["tool_choice"],
                        }
                    elif kind == "message":
                        self._native["trajs"][event["name"]]["messages"].append(event["message"])
                        self._persist(context)
                    elif kind == "model_call":
                        self._calls.append(
                            {key: value for key, value in event.items() if key != "event"}
                        )
                        with (native_logs / "model-calls.jsonl").open("a") as handle:
                            handle.write(
                                redact_secret_material(
                                    json.dumps(event).encode(), self._secrets
                                ).decode()
                                + "\n"
                            )
                    elif kind == "finished":
                        finished = True
                        self._native["info"] = {
                            key: value for key, value in event.items() if key != "event"
                        }
                    else:
                        raise ValueError(f"unknown native worker event: {kind}")
                await process.wait()
                if tasks:
                    await asyncio.gather(*tasks)
                refusal = next(
                    (
                        call["proxy_budget_reason"]
                        for call in reversed(self._calls)
                        if isinstance(call.get("proxy_budget_reason"), str)
                    ),
                    None,
                )
                native_result = self._native["info"].get("result")
                if (
                    refusal is None
                    and isinstance(native_result, str)
                    and "trial budget exhausted" in native_result
                ):
                    refusal = native_result
                if refusal is not None:
                    from evallab.harbor_terminus import TrialBudgetExhaustedError

                    ceiling = re.search(r"ceiling:[A-Za-z0-9_]+", refusal)
                    self._native["info"]["stop_reason"] = (
                        ceiling.group(0) if ceiling is not None else "trial_budget_exhausted"
                    )
                    raise TrialBudgetExhaustedError(
                        "the trial proxy refused a native model call: "
                        + self._native["info"]["stop_reason"]
                    )
                if process.returncode != 0 or not finished:
                    raise RuntimeError(
                        f"native Xiaomi worker exited without completion (exit {process.returncode}); see worker-stderr.txt"
                    )
                if self._native["info"].get("exit_status") in {"InfraError", "ModelQueryError"}:
                    raise RuntimeError(
                        f"native Xiaomi agent stopped on {self._native['info']['exit_status']}"
                    )
            except asyncio.CancelledError:
                self._native["info"]["exit_status"] = "HarborCancelled"
                raise
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except TimeoutError:
                        os.killpg(process.pid, signal.SIGKILL)
                        await process.wait()
                stdin.close()
                self._persist(context)
                for path in native_logs.rglob("*"):
                    if path.is_file():
                        text = path.read_bytes()
                        safe = redact_secret_material(text, self._secrets)
                        if safe != text:
                            path.write_bytes(safe)
