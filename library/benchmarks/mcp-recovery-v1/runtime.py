"""In-process MCP JSON-RPC runtime for recovery CI-contract twins."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from faults import FaultClass, FaultController, FaultSpec
from state import DatabaseState, StateCertificate, canonical_json, compute_digest


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], DatabaseState], Any]


class McpServerRuntime:
    def __init__(
        self,
        mode: str = "clean",
        initial_state: dict[str, Any] | None = None,
        fault_specs: list[FaultSpec] | None = None,
        evidence_file: Path | str | None = None,
    ):
        self.mode = mode
        self.state = DatabaseState(initial_state or {})
        self.initial_digest = self.state.digest()
        self.fault_controller = FaultController(fault_specs if mode == "fault" else [])
        self.evidence_file = Path(evidence_file) if evidence_file else None
        self.tools: dict[str, ToolDefinition] = {}
        self.event_index = 0
        self.recorded_events: list[dict[str, Any]] = []
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        def read_record(args: dict[str, Any], db: DatabaseState) -> Any:
            key = args.get("key", "")
            return {"key": key, "value": db.get(key), "exists": key in db.records}

        def write_record(args: dict[str, Any], db: DatabaseState) -> Any:
            db.set(args.get("key", ""), args.get("value"))
            return {"status": "success", "key": args.get("key", ""), "digest": db.digest()}

        def refresh_auth(args: dict[str, Any], db: DatabaseState) -> Any:
            db.set("__auth__", args.get("scope", "read"))
            return {"status": "authenticated", "scope": args.get("scope", "read")}

        def fallback_query(args: dict[str, Any], db: DatabaseState) -> Any:
            db.set("__fallback_synced__", True)
            return {"status": "success", "source": "replica", "query": args.get("query", "")}

        self.register_tool(ToolDefinition("read_record", "Read a database record", {"type": "object"}, read_record))
        self.register_tool(ToolDefinition("write_record", "Write a database record", {"type": "object"}, write_record))
        self.register_tool(ToolDefinition("refresh_auth", "Refresh auth token credentials", {"type": "object"}, refresh_auth))
        self.register_tool(ToolDefinition("fallback_query", "Fallback read query to replica", {"type": "object"}, fallback_query))

    def register_tool(self, tool: ToolDefinition) -> None:
        self.tools[tool.name] = tool

    def emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {"event_index": self.event_index, "event_type": event_type, "payload": payload}
        self.event_index += 1
        self.recorded_events.append(event)
        if self.evidence_file:
            self.evidence_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.evidence_file, "a", encoding="utf-8") as handle:
                handle.write(canonical_json(event) + "\n")

    def handle_request(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        req_id = request_payload.get("id", 1)
        method = request_payload.get("method", "")
        params = request_payload.get("params", {})
        if method == "initialize":
            self.emit_event("mcp_initialized", {"protocolVersion": "2024-11-05"})
            return {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "2024-11-05"}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
                        for tool in self.tools.values()
                    ]
                },
            }
        if method != "tools/call":
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        if tool_name not in self.tools:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Tool {tool_name} not found"}}
        should_fault, fault_class, fault_payload = self.fault_controller.evaluate(
            tool_name, arguments, state=self.state
        )
        if should_fault:
            self.emit_event(
                "fault_injected",
                {
                    "tool": tool_name,
                    "fault_class": fault_class.value if fault_class else "unknown",
                },
            )
            if fault_class == FaultClass.SILENT_WRONG_PAYLOAD:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(fault_payload)}], "isError": False},
                }
            if isinstance(fault_payload, dict) and fault_payload.get("isError") is True:
                return {"jsonrpc": "2.0", "id": req_id, "result": fault_payload}
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": str(fault_payload)}], "isError": True}}
        result = self.tools[tool_name].handler(arguments, self.state)
        self.emit_event("tool_executed", {"tool": tool_name, "arguments": arguments, "state_digest": self.state.digest()})
        return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False}}

    def export_state_certificate(self, expected_final_invariants: dict[str, Any] | None = None) -> StateCertificate:
        invariants_passed = True
        if expected_final_invariants:
            invariants_passed = all(self.state.get(key) == value for key, value in expected_final_invariants.items())
        return StateCertificate(
            initial_digest=self.initial_digest,
            final_digest=self.state.digest(),
            step_count=len(self.recorded_events),
            mutations=list(self.state.history),
            invariants_passed=invariants_passed,
            details={"recorded_event_count": len(self.recorded_events)},
        )
