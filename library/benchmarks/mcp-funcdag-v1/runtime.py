"""Deterministic Streamable-HTTP MCP server runtime and canonical event logger."""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import socketserver
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dag_generator import OP_REGISTRY


class EventRecorder:
    def __init__(self, evidence_dir: Path):
        self.evidence_dir = evidence_dir
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = evidence_dir / "benchmark-events.jsonl"
        self.final_state_path = evidence_dir / "final-state.json"
        self.event_index = 0
        self._lock = threading.Lock()

    def record_event(
        self,
        event_type: str,
        tool_name: str | None = None,
        arguments: dict[str, Any] | None = None,
        result: Any = None,
        error: str | None = None,
        schema_conforming: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            event = {
                "event_index": self.event_index,
                "event_type": event_type,
                "tool_name": tool_name,
                "arguments": arguments,
                "result": result,
                "error": error,
                "schema_conforming": schema_conforming,
            }
            if extra:
                event.update(extra)
            self.event_index += 1
            line = json.dumps(event, sort_keys=True, separators=(",", ":"))
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return event

    def write_final_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            serialized = json.dumps(state, indent=2, sort_keys=True)
            self.final_state_path.write_text(serialized, encoding="utf-8")


class MCPRuntime:
    def __init__(self, spec_data: dict[str, Any], evidence_dir: Path):
        self.spec_data = spec_data
        self.evidence_dir = evidence_dir
        self.recorder = EventRecorder(evidence_dir)
        self.tools = {t["name"]: t for t in spec_data["tools"]}
        self.nodes = {n["node_id"]: n for n in spec_data.get("nodes", [])}
        self.executed_calls: list[str] = []  # list of call digests (tool_name + args_json)
        self.executed_tools: list[str] = []
        self.total_calls = 0
        self.redundant_calls = 0

    def list_tools(self) -> list[dict[str, Any]]:
        mcp_tools = []
        for t in self.tools.values():
            properties = {}
            required = []
            for p in t["parameters"]:
                prop_type = "integer" if p["type_name"] == "integer" else ("boolean" if p["type_name"] == "boolean" else "string")
                properties[p["name"]] = {
                    "type": prop_type,
                    "description": p["description"],
                }
                if p.get("required", True):
                    required.append(p["name"])
            mcp_tools.append({
                "name": t["name"],
                "description": t["description"],
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            })
        return mcp_tools

    def _call_digest(self, name: str, arguments: dict[str, Any]) -> str:
        canonical_args = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        return f"{name}:{canonical_args}"

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.total_calls += 1
        call_key = self._call_digest(name, arguments)

        if name not in self.tools:
            err = f"Tool '{name}' not found."
            self.recorder.record_event(
                event_type="tool_call_rejected",
                tool_name=name,
                arguments=arguments,
                error=err,
                schema_conforming=False,
            )
            return {"error": {"code": -32601, "message": err}}

        tool_spec = self.tools[name]
        # Validate schema conformance
        req_params = [p["name"] for p in tool_spec["parameters"] if p.get("required", True)]
        missing = [p for p in req_params if p not in arguments]
        if missing:
            err = f"Missing required parameters: {missing}"
            self.recorder.record_event(
                event_type="tool_call_schema_error",
                tool_name=name,
                arguments=arguments,
                error=err,
                schema_conforming=False,
            )
            return {"error": {"code": -32602, "message": err}}

        # Check types
        for p in tool_spec["parameters"]:
            pname = p["name"]
            if pname in arguments:
                val = arguments[pname]
                ptype = p["type_name"]
                if ptype == "integer" and not (isinstance(val, int) and not isinstance(val, bool)):
                    err = f"Parameter '{pname}' must be an integer, got {type(val).__name__}"
                    self.recorder.record_event(
                        event_type="tool_call_schema_error",
                        tool_name=name,
                        arguments=arguments,
                        error=err,
                        schema_conforming=False,
                    )
                    return {"error": {"code": -32602, "message": err}}

        # Check for redundant execution: exact same (tool_name, arguments)
        is_redundant = call_key in self.executed_calls
        if is_redundant:
            self.redundant_calls += 1

        # Execute operation
        if tool_spec.get("is_distractor", False):
            result = "NOOP_DISTRACTOR_LOGGED"
        else:
            op_kind = tool_spec.get("op_kind", "")
            fn = OP_REGISTRY.get(op_kind)
            if not fn:
                err = f"Unknown op kind: {op_kind}"
                self.recorder.record_event(
                    event_type="tool_execution_error",
                    tool_name=name,
                    arguments=arguments,
                    error=err,
                    schema_conforming=True,
                )
                return {"error": {"code": -32000, "message": err}}
            try:
                result = fn(**arguments)
            except Exception as e:
                err = f"Execution exception: {e}"
                self.recorder.record_event(
                    event_type="tool_execution_error",
                    tool_name=name,
                    arguments=arguments,
                    error=err,
                    schema_conforming=True,
                )
                return {"error": {"code": -32000, "message": err}}

        self.executed_calls.append(call_key)
        self.executed_tools.append(name)
        self.recorder.record_event(
            event_type="tool_call_success",
            tool_name=name,
            arguments=arguments,
            result=result,
            schema_conforming=True,
            extra={"is_redundant": is_redundant, "call_digest": call_key},
        )

        final_state = {
            "total_calls": self.total_calls,
            "executed_tools": self.executed_tools,
            "executed_calls": self.executed_calls,
            "redundant_calls": self.redundant_calls,
            "last_result": result,
        }
        self.recorder.write_final_state(final_state)

        return {"result": {"content": [{"type": "text", "text": str(result)}], "value": result}}


def make_mcp_handler(runtime: MCPRuntime):
    class MCPHTTPHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"healthy"}\n')
                return
            if self.path in ("/mcp", "/mcp/"):
                tools = runtime.list_tools()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"tools": tools}, indent=2).encode("utf-8"))
                return
            if self.path == "/events":
                events_file = runtime.recorder.events_path
                content = events_file.read_bytes() if events_file.exists() else b""
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(content)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"Invalid JSON: {e}"}).encode("utf-8"))
                return

            method = data.get("method")
            msg_id = data.get("id")

            if method == "tools/list":
                tools = runtime.list_tools()
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}
            elif method == "tools/call":
                params = data.get("params", {})
                name = params.get("name")
                args = params.get("arguments", {})
                out = runtime.call_tool(name, args)
                if "error" in out:
                    resp = {"jsonrpc": "2.0", "id": msg_id, "error": out["error"]}
                else:
                    resp = {"jsonrpc": "2.0", "id": msg_id, "result": out["result"]}
            elif method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mcp-funcdag-server", "version": "1.0.0"},
                    },
                }
            else:
                name = data.get("tool") or data.get("name")
                args = data.get("arguments") or data.get("args", {})
                if name:
                    out = runtime.call_tool(name, args)
                    resp = out
                else:
                    resp = {"error": f"Unsupported method or payload: {method}"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp, indent=2).encode("utf-8"))

        def log_message(self, format, *args):
            pass

    return MCPHTTPHandler


def start_server(spec_path: Path, evidence_dir: Path, port: int = 8000, host: str = "0.0.0.0") -> None:
    spec_data = json.loads(spec_path.read_text(encoding="utf-8"))
    runtime = MCPRuntime(spec_data, evidence_dir)
    handler = make_mcp_handler(runtime)
    with socketserver.TCPServer((host, port), handler) as httpd:
        print(f"MCP Funcdag streamable-HTTP server running on {host}:{port}")
        httpd.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run MCP streamable-HTTP server")
    parser.add_argument("--spec", required=True, type=Path, help="Path to runtime_tools.json")
    parser.add_argument("--evidence-dir", required=True, type=Path, help="Path to evidence output dir")
    parser.add_argument("--port", default=8000, type=int, help="Port to bind")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    args = parser.parse_args()
    start_server(args.spec, args.evidence_dir, port=args.port, host=args.host)
