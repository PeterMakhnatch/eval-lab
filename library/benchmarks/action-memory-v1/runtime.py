"""Streamable HTTP MCP JSON-RPC Server & Client for action-memory-v1."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


class StreamableMCPHandler(BaseHTTPRequestHandler):
    scenario_data: dict[str, Any] = {}
    evidence_dir: Path = Path("/app/evidence")
    event_counter: int = 0
    _lock = threading.Lock()

    def _log_event(self, event_type: str, payload: dict[str, Any]) -> None:
        with StreamableMCPHandler._lock:
            StreamableMCPHandler.event_counter += 1
            idx = StreamableMCPHandler.event_counter
            record = {
                "event_index": idx,
                "event_type": event_type,
                "payload": payload,
            }
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            events_file = self.evidence_dir / "benchmark-events.jsonl"
            with events_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")

    def _send_json_rpc_response(self, request_id: Any, result: Any = None, error: Any = None, status: int = 200) -> None:
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            body["error"] = error
        else:
            body["result"] = result

        payload = (json.dumps(body, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path in {"/health", "/ready", "/healthz"}:
            body = b'{"status": "ready"}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        raw_data = self.rfile.read(content_length).decode("utf-8")
        try:
            req = json.loads(raw_data)
        except Exception:
            self._send_json_rpc_response(None, error={"code": -32700, "message": "Parse error"}, status=400)
            return

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if method == "initialize":
            self._log_event("mcp_initialize", {"protocolVersion": params.get("protocolVersion", "2024-11-05")})
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "action-memory-mcp",
                    "version": "1.0.0",
                },
            }
            self._send_json_rpc_response(req_id, result=result)
            return

        if method == "notifications/initialized":
            self._log_event("mcp_initialized_notification", {})
            self._send_json_rpc_response(req_id, result={})
            return

        if method == "tools/list":
            self._log_event("mcp_tools_list", {})
            tools = [
                {
                    "name": "list_context_chunks",
                    "description": "List all available context log chunk IDs.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                    },
                },
                {
                    "name": "get_context_chunk",
                    "description": "Retrieve content and metadata for a specific chunk ID.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "chunk_id": {"type": "string", "description": "The ID of the chunk to read"},
                        },
                        "required": ["chunk_id"],
                    },
                },
                {
                    "name": "execute_mutation",
                    "description": "Execute final state-bound mutation action on target entity.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {"type": "string", "description": "Target entity ID"},
                            "attribute": {"type": "string", "description": "Target attribute key"},
                            "bound_value": {"type": "string", "description": "Latest state value to bind"},
                        },
                        "required": ["entity_id", "attribute", "bound_value"],
                    },
                },
            ]
            self._send_json_rpc_response(req_id, result={"tools": tools})
            return

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})

            if tool_name == "list_context_chunks":
                chunks = self.scenario_data.get("chunks", [])
                chunk_ids = [c["chunk_id"] for c in chunks]
                self._log_event("list_chunks", {"count": len(chunk_ids)})
                self._send_json_rpc_response(req_id, result={
                    "content": [{"type": "text", "text": json.dumps({"chunk_ids": chunk_ids})}]
                })
                return

            if tool_name == "get_context_chunk":
                chunk_id = arguments.get("chunk_id")
                chunks = self.scenario_data.get("chunks", [])
                for c in chunks:
                    if c["chunk_id"] == chunk_id:
                        self._log_event("read_chunk", {"chunk_id": chunk_id, "byte_count": c["byte_count"]})
                        self._send_json_rpc_response(req_id, result={
                            "content": [{"type": "text", "text": c["content"]}],
                            "metadata": {"byte_count": c["byte_count"], "chunk_type": c.get("chunk_type")}
                        })
                        return
                self._log_event("read_chunk_not_found", {"chunk_id": chunk_id})
                self._send_json_rpc_response(req_id, error={"code": -32602, "message": f"Chunk not found: {chunk_id}"}, status=404)
                return

            if tool_name == "execute_mutation":
                entity_id = arguments.get("entity_id")
                attribute = arguments.get("attribute")
                bound_value = arguments.get("bound_value")

                self._log_event("execute_mutation", {
                    "entity_id": entity_id,
                    "attribute": attribute,
                    "bound_value": bound_value,
                })

                final_state = {
                    "status": "executed",
                    "target_entity": entity_id,
                    "target_attribute": attribute,
                    "bound_value": bound_value,
                }
                final_state_file = self.evidence_dir / "final-state.json"
                self.evidence_dir.mkdir(parents=True, exist_ok=True)
                with final_state_file.open("w", encoding="utf-8") as f:
                    json.dump(final_state, f, indent=2, sort_keys=True)

                self._send_json_rpc_response(req_id, result={
                    "content": [{"type": "text", "text": json.dumps({"status": "executed", "state": final_state})}]
                })
                return

            self._send_json_rpc_response(req_id, error={"code": -32601, "message": f"Method/Tool not found: {tool_name}"}, status=404)
            return

        self._send_json_rpc_response(req_id, error={"code": -32601, "message": f"Method not supported: {method}"}, status=404)


class MCPClient:
    """Standard HTTP MCP client for solution and tests."""
    def __init__(self, endpoint_url: str) -> None:
        self.endpoint_url = endpoint_url
        parsed = urllib.parse.urlparse(endpoint_url)
        self.host = parsed.hostname or "localhost"
        self.port = parsed.port or 8080
        self.path = parsed.path or "/mcp"
        self._msg_id = 0

    def wait_until_ready(self, timeout_sec: float = 10.0) -> bool:
        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            try:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=1)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                if resp.status == 200:
                    conn.close()
                    return True
                conn.close()
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def call_rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._msg_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._msg_id,
            "method": method,
            "params": params or {},
        }
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        conn.request("POST", self.path, json.dumps(payload), headers)
        response = conn.getresponse()
        data = response.read().decode("utf-8")
        conn.close()
        res = json.loads(data)
        if "error" in res:
            raise RuntimeError(f"MCP RPC Error: {res['error']}")
        return res.get("result", {})

    def initialize(self) -> dict[str, Any]:
        return self.call_rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})

    def list_tools(self) -> list[dict[str, Any]]:
        res = self.call_rpc("tools/list")
        return res.get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.call_rpc("tools/call", {"name": name, "arguments": arguments})


def start_server(task_dir: Path, evidence_dir: Path, port: int = 8080) -> None:
    scenario_path = task_dir / "scenario.json"
    if not scenario_path.exists() and (task_dir / "task_state" / "scenario.json").exists():
        scenario_path = task_dir / "task_state" / "scenario.json"

    scenario_data = json.loads(scenario_path.read_text(encoding="utf-8")) if scenario_path.exists() else {}
    StreamableMCPHandler.scenario_data = scenario_data
    StreamableMCPHandler.evidence_dir = evidence_dir

    server = HTTPServer(("0.0.0.0", port), StreamableMCPHandler)
    print(f"Action Memory Streamable-HTTP MCP server listening on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-dir", type=Path, default=Path("/app/task_state"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("/app/evidence"))
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    start_server(args.task_dir, args.evidence_dir, args.port)
