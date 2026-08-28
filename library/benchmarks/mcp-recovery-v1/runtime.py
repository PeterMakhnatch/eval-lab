"""Streamable-HTTP MCP Runtime supporting clean and fault twins."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
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
        mode: str = "clean",  # "clean" or "fault"
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
            key = args.get("key", "")
            val = args.get("value")
            db.set(key, val)
            return {"status": "success", "key": key, "digest": db.digest()}

        def delete_record(args: dict[str, Any], db: DatabaseState) -> Any:
            key = args.get("key", "")
            existed = db.delete(key)
            return {"status": "success", "deleted": existed, "digest": db.digest()}

        def refresh_auth(args: dict[str, Any], db: DatabaseState) -> Any:
            scope = args.get("scope", "read")
            db.set("__auth_scope__", scope)
            return {"status": "authenticated", "scope": scope}

        def fallback_query(args: dict[str, Any], db: DatabaseState) -> Any:
            query = args.get("query", "")
            # Deterministic fallback result
            return {"status": "success", "source": "replica", "data": db.get(query)}

        self.register_tool(ToolDefinition("read_record", "Read a database record", {"type": "object", "properties": {"key": {"type": "string"}}}, read_record))
        self.register_tool(ToolDefinition("write_record", "Write a database record", {"type": "object", "properties": {"key": {"type": "string"}, "value": {}}}, write_record))
        self.register_tool(ToolDefinition("delete_record", "Delete a database record", {"type": "object", "properties": {"key": {"type": "string"}}}, delete_record))
        self.register_tool(ToolDefinition("refresh_auth", "Refresh auth token credentials", {"type": "object", "properties": {"scope": {"type": "string"}}}, refresh_auth))
        self.register_tool(ToolDefinition("fallback_query", "Fallback read query to replica", {"type": "object", "properties": {"query": {"type": "string"}}}, fallback_query))

    def register_tool(self, tool: ToolDefinition) -> None:
        self.tools[tool.name] = tool

    def emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {
            "event_index": self.event_index,
            "event_type": event_type,
            "payload": payload,
        }
        self.event_index += 1
        self.recorded_events.append(event)
        if self.evidence_file:
            self.evidence_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.evidence_file, "a", encoding="utf-8") as f:
                f.write(canonical_json(event) + "\n")

    def handle_request(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        """Handles JSON-RPC / MCP call_tool requests."""
        req_id = request_payload.get("id", 1)
        method = request_payload.get("method", "")
        params = request_payload.get("params", {})

        if method == "tools/list":
            tools_list = [
                {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                for t in self.tools.values()
            ]
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools_list}}

        if method != "tools/call":
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}

        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name not in self.tools:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Tool {tool_name} not found"}}

        # Check fault injection
        should_fault, fault_class, fault_payload = self.fault_controller.evaluate(tool_name, arguments)
        if should_fault:
            self.emit_event(
                "fault_injected",
                {
                    "tool": tool_name,
                    "fault_class": fault_class.value if fault_class else "unknown",
                    "arguments_digest": compute_digest(arguments),
                },
            )
            if fault_class == FaultClass.MALFORMED_OUTPUT:
                # Return unparseable or raw string payload simulating transport corruption
                return {"jsonrpc": "2.0", "id": req_id, "raw_corrupted_response": fault_payload}
            elif fault_class in (FaultClass.PERMISSION_DENIED, FaultClass.NOT_FOUND, FaultClass.TIMEOUT):
                return {"jsonrpc": "2.0", "id": req_id, "error": fault_payload}
            elif fault_class == FaultClass.SILENT_WRONG_RESULT:
                return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(fault_payload)}]}}

        # Normal tool execution
        tool = self.tools[tool_name]
        try:
            res = tool.handler(arguments, self.state)
            self.emit_event(
                "tool_executed",
                {
                    "tool": tool_name,
                    "arguments_digest": compute_digest(arguments),
                    "state_digest": self.state.digest(),
                },
            )
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(res)}]}}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}}

    def export_state_certificate(self, expected_final_invariants: dict[str, Any] | None = None) -> StateCertificate:
        final_digest = self.state.digest()
        invariants_passed = True
        if expected_final_invariants:
            for k, v in expected_final_invariants.items():
                if self.state.get(k) != v:
                    invariants_passed = False
                    break

        return StateCertificate(
            initial_digest=self.initial_digest,
            final_digest=final_digest,
            step_count=len(self.recorded_events),
            mutations=list(self.state.history),
            invariants_passed=invariants_passed,
            details={"recorded_event_count": len(self.recorded_events)},
        )
