"""A scripted A2A agent executing controls via role-bearing MCP, never a model."""
from __future__ import annotations

import asyncio
import json
import socket
import sys
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx
from agentenv_protocol.a2a_agent import (
    MCP_CONFIG_V1,
    TRAJECTORY_V1,
    AgentConfig,
    AgentEnvAgent,
    AgentIdentity,
    TaskRequest,
    TaskResult,
    TextPart,
    Usage,
    a2a_agent,
)
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from agentenv_bench.s3k_1591.controls import CONTROLS

WORLD_TOOL = "bluesky_approval_workflow_bench_bump_row_version"


@contextmanager
def gateway_only_network(url):
    """Fail closed on outbound sockets to any endpoint except this run's gateway."""
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    permitted = {address[4][0] for address in socket.getaddrinfo(parsed.hostname, port)}
    attempts = []
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    def check(address):
        if not isinstance(address, tuple) or address[0] not in permitted or address[1] != port:
            attempts.append(str(address))
            raise RuntimeError("Model-free agent denied non-gateway outbound socket")
    def guarded(sock, address):
        check(address)
        return connect(sock, address)
    def guarded_ex(sock, address):
        check(address)
        return connect_ex(sock, address)
    socket.socket.connect, socket.socket.connect_ex = guarded, guarded_ex
    try:
        yield attempts
    finally:
        socket.socket.connect, socket.socket.connect_ex = connect, connect_ex


class StoppedOnConflict(Exception):
    pass


def naive(oracle, call, params):
    """Exactly the oracle except that the first real version conflict ends the run."""
    def no_retry(system, tool, args):
        result = call(system, tool, args)
        if (
            system == "bluesky_approval_workflow"
            and tool == "submit_merit_batch"
            and not args.get("dry_run")
            and any(e.get("code") == "ROW_VERSION_CONFLICT" for e in result.get("validation_errors", []))
        ):
            raise StoppedOnConflict
        return result
    try:
        return oracle.run(no_retry, params)
    except StoppedOnConflict:
        return "Stopped after ROW_VERSION_CONFLICT; did not resubmit."


@a2a_agent(
    identity=AgentIdentity(name="mimo-scripted-control", description="Deterministic controls; no model client", version="1.0.0"),
    config=AgentConfig, extensions=(MCP_CONFIG_V1, TRAJECTORY_V1),
)
class ScriptedAgent(AgentEnvAgent):
    async def run(self, request: TaskRequest[AgentConfig]) -> TaskResult:
        spec = json.loads("\n".join(part.text for part in request.parts if isinstance(part, TextPart)))
        registration = next(iter(request.mcp_servers.values()))
        url = registration["url"]
        role = request.config.role or "default"
        headers = {**(registration.get("headers") or {}), "AgentEnv-Role": role}
        events = []
        loop = asyncio.get_running_loop()
        with gateway_only_network(url) as denied:
            async with streamablehttp_client(url, headers=headers) as (reader, writer, _):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    offered = await session.list_tools()
                    names = [tool.name for tool in offered.tools]
                    refusal = await session.call_tool(WORLD_TOOL, {"case_id": spec["params"]["case_id"]})
                    refusal_text = "\n".join(getattr(part, "text", "") for part in refusal.content)
                    proof = {
                        "role": role, "tool": WORLD_TOOL, "listed": WORLD_TOOL in names,
                        "call_refused": refusal.isError, "refusal": refusal_text,
                        "tool_names": names,
                    }
                    if proof["listed"] or not proof["call_refused"]:
                        raise RuntimeError("Agent role can access world-only tool")
                    # Test endpoint isolation from the actual agent container, not
                    # from the trusted verifier. Read only; never expose payloads.
                    base = url.removesuffix("/mcp")
                    leak = {}
                    async with httpx.AsyncClient(headers=headers, trust_env=False) as client:
                        card_response = await client.get(base + "/.well-known/agent-env.json")
                        card_response.raise_for_status()
                        trajectory = await client.get(base + "/trajectory")
                        leak["trajectory"] = {"status": trajectory.status_code, "reachable": trajectory.is_success, "bytes": len(trajectory.content)}
                        for child in card_response.json().get("children_environments", []):
                            rpc = child["url"]
                            response = await client.post(base + rpc, json={"jsonrpc": "2.0", "id": 1, "method": "data/get", "params": {}})
                            body = response.json() if response.is_success else {}
                            leak[child["name"]] = {"status": response.status_code, "reachable": response.is_success and "result" in body, "bytes": len(response.content)}
                    async def call_async(system, tool, args):
                        reply = await session.call_tool(f"{system}_{tool}", args)
                        text = "\n".join(getattr(part, "text", "") for part in reply.content)
                        if reply.isError:
                            events.append({"system": system, "tool": tool, "args": args, "raised": text})
                            raise RuntimeError(text)
                        # MiMo return dictionaries stay dictionaries (including
                        # its validation errors); only raised errors raise here.
                        result = reply.structuredContent
                        if result is None:
                            result = json.loads(text)
                        if isinstance(result, dict) and set(result) == {"result"}:
                            result = result["result"]
                        events.append({"system": system, "tool": tool, "args": args, "result": result})
                        return result
                    def call(system, tool, args):
                        return asyncio.run_coroutine_threadsafe(call_async(system, tool, args), loop).result(timeout=60)
                    if spec["control"] == "naive":
                        report = await asyncio.to_thread(naive, CONTROLS["oracle"], call, spec["params"])
                    else:
                        report = await asyncio.to_thread(CONTROLS[spec["control"]].run, call, spec["params"])
        evidence = {
            "type": "bench_evidence", "rbac": proof, "answer_leak_probe": leak,
            "non_gateway_network_attempts": denied,
            "model_client_modules_loaded": [name for name in ("litellm", "openai", "anthropic") if name in sys.modules],
            "model_calls": 0, "control": spec["control"], "role": role,
        }
        events.append(evidence)
        return (
            TaskResult.builder().succeeded().add_text(report)
            .usage(Usage(tool_call_count=len(events) - 1, input_tokens=0, output_tokens=0))
            .native_trajectory(format="mimo-scripted-controls/v1", payload=events).build()
        )


if __name__ == "__main__":
    ScriptedAgent().serve()
