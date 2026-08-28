"""Shared FastMCP multi-container task-authoring substrate and runtime middleware.

Grounding: Architecture PR #265 (research/inbox/NEXT-BENCHMARK-PROGRAM-ARCHITECTURE-2026-08-28.md)

Provides:
- Standard FastMCP streamable-HTTP sidecar topology generation & validation matching workbench-v2.
- Zero-egress internal bridge (internal: true), task-local named volume (main-RO / sidecar-RW).
- JSON-RPC 2.0 endpoint (/mcp) supporting initialize (2024-11-05), tools/list, and tools/call.
- Deterministic Fault Interceptor middleware operating over FaultInjectionRecord contracts.
- Deterministic state journal / event ledger logging to /app/output or specified evidence path.
- Invariant ground-truth separation (purges solutions/oracles from agent containers).
- Substrate version & digest computation.
"""

from __future__ import annotations

import http.server
import json
import logging
import re
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.benchmark_program_contracts import (
    FaultClass,
    FaultInjectionRecord,
    canonical_bytes,
    canonical_json,
    compute_sha256,
)

logger = logging.getLogger(__name__)

MCP_SUBSTRATE_VERSION = "0.2.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_SIDECAR_SERVICE = "mcp-service"
DEFAULT_VOLUME_NAME = "evidence-volume"
DEFAULT_VOLUME_MOUNT = "/app/output"
DEFAULT_MCP_PORT = 8080


class SubstrateError(Exception):
    """Raised when substrate configuration, validation, or runtime fails."""


@dataclass(frozen=True)
class MCPToolParameter:
    name: str
    type_name: str
    description: str
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type_name": self.type_name,
            "description": self.description,
            "required": self.required,
        }


@dataclass(frozen=True)
class MCPToolDefinition:
    name: str
    description: str
    parameters: tuple[MCPToolParameter, ...]
    output_type: str = "object"
    is_distractor: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_mcp_tool_schema(self) -> dict[str, Any]:
        """Convert to standard MCP tools/list tool schema (JSON Schema inputSchema)."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in self.parameters:
            type_mapping = {
                "int": "integer",
                "integer": "integer",
                "float": "number",
                "number": "number",
                "str": "string",
                "string": "string",
                "bool": "boolean",
                "boolean": "boolean",
                "dict": "object",
                "object": "object",
                "list": "array",
                "array": "array",
            }
            json_type = type_mapping.get(param.type_name.lower(), "string")
            properties[param.name] = {
                "type": json_type,
                "description": param.description,
            }
            if param.required:
                required.append(param.name)

        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }


@dataclass
class ToolExecutionContext:
    tool_name: str
    arguments: dict[str, Any]
    call_ordinal: int
    raw_event_ordinal: int


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


class FaultInterceptorMiddleware:
    """Deterministic fault interceptor operating on FaultInjectionRecord ledgers.

    Evaluates every tool call against the registered fault record. When the sequence ordinal
    matches target_canonical_event_ordinal and target_tool matches, intercepts the call
    and returns or raises the configured fault response.
    """

    def __init__(self, fault_record: FaultInjectionRecord | None = None) -> None:
        self.fault_record = fault_record
        self.injected_calls: list[dict[str, Any]] = []

    def should_intercept(self, tool_name: str, call_ordinal: int) -> bool:
        if self.fault_record is None:
            return False
        if self.fault_record.target_tool != tool_name:
            return False
        return call_ordinal == self.fault_record.target_canonical_event_ordinal

    def apply_fault(
        self, tool_name: str, arguments: dict[str, Any], call_ordinal: int
    ) -> dict[str, Any]:
        assert self.fault_record is not None
        record = self.fault_record
        fault_class = record.fault_class
        payload = record.injection_payload

        self.injected_calls.append(
            {
                "fault_id": record.fault_id,
                "fault_class": fault_class.value,
                "tool_name": tool_name,
                "call_ordinal": call_ordinal,
                "arguments": arguments,
            }
        )

        if fault_class == FaultClass.TRANSIENT_HTTP_5XX:
            return {
                "is_error": True,
                "http_status": 500,
                "error": {
                    "code": -32000,
                    "message": payload.get(
                        "message", "Internal Server Error: transient sidecar 500"
                    ),
                },
            }
        elif fault_class == FaultClass.TRANSIENT_NETWORK_TIMEOUT:
            return {
                "is_error": True,
                "http_status": 504,
                "error": {
                    "code": -32001,
                    "message": payload.get(
                        "message", "Gateway Timeout: upstream tool response timed out"
                    ),
                },
            }
        elif fault_class == FaultClass.PERSISTENT_SCHEMA_MISMATCH:
            return {
                "is_error": True,
                "http_status": 200,
                "error": {
                    "code": -32602,
                    "message": payload.get(
                        "message",
                        f"Schema mismatch for tool {tool_name}: unexpected schema mutation",
                    ),
                    "data": payload.get(
                        "data",
                        {"expected_schema": payload.get("expected_schema", "v2_signature")},
                    ),
                },
            }
        elif fault_class == FaultClass.PERSISTENT_SIGNATURE_ERROR:
            return {
                "is_error": True,
                "http_status": 200,
                "error": {
                    "code": -32602,
                    "message": payload.get(
                        "message",
                        f"Signature error in tool {tool_name}: invalid positional argument binding",
                    ),
                },
            }
        elif fault_class == FaultClass.SILENT_WRONG_PAYLOAD:
            # Returns HTTP 200 OK with mutated payload without error flag
            corrupted_result = payload.get(
                "corrupted_result",
                {"value": payload.get("corrupted_value", "CORRUPTED_VALUE")},
            )
            return {
                "is_error": False,
                "http_status": 200,
                "result": corrupted_result,
                "_silent_fault_injected": True,
            }
        else:
            raise SubstrateError(f"Unhandled fault class: {fault_class}")


class FastMCPRuntime:
    """In-memory FastMCP engine managing tools, execution, event ledgers, and fault middleware."""

    def __init__(
        self,
        tools: Sequence[MCPToolDefinition],
        handlers: Mapping[str, ToolHandler] | None = None,
        fault_record: FaultInjectionRecord | None = None,
        evidence_dir: Path | None = None,
        state_log_name: str = "state-journal.jsonl",
    ) -> None:
        self.tools = {t.name: t for t in tools}
        self.handlers = dict(handlers or {})
        self.fault_interceptor = FaultInterceptorMiddleware(fault_record)
        self.evidence_dir = evidence_dir
        self.state_log_name = state_log_name
        self.call_count = 0
        self.events: list[dict[str, Any]] = []
        self._prior_tool_calls: set[str] = set()

        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def register_tool(self, tool_def: MCPToolDefinition, handler: ToolHandler) -> None:
        self.tools[tool_def.name] = tool_def
        self.handlers[tool_def.name] = handler

    def list_tools(self) -> list[dict[str, Any]]:
        return [t.to_mcp_tool_schema() for t in self.tools.values()]

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], int]:
        self.call_count += 1
        ordinal = self.call_count
        call_sig = f"{tool_name}:{canonical_json(arguments)}"
        is_redundant = call_sig in self._prior_tool_calls
        self._prior_tool_calls.add(call_sig)

        if tool_name not in self.tools:
            err_resp = {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32601,
                    "message": f"Method not found: unknown tool {tool_name!r}",
                },
            }
            self._log_event(
                {
                    "event_ordinal": ordinal,
                    "event_type": "tool_call_rejected",
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "is_redundant": is_redundant,
                    "error": err_resp["error"],
                }
            )
            return err_resp, 200

        tool_def = self.tools[tool_name]
        # Basic validation against parameter definitions
        missing_params = [
            p.name for p in tool_def.parameters if p.required and p.name not in arguments
        ]
        if missing_params:
            err_resp = {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32602,
                    "message": f"Invalid params: missing required argument(s): {missing_params}",
                },
            }
            self._log_event(
                {
                    "event_ordinal": ordinal,
                    "event_type": "tool_call_rejected",
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "is_redundant": is_redundant,
                    "schema_conforming": False,
                    "error": err_resp["error"],
                }
            )
            return err_resp, 200

        # Check Fault Interceptor
        if self.fault_interceptor.should_intercept(tool_name, ordinal):
            fault_res = self.fault_interceptor.apply_fault(tool_name, arguments, ordinal)
            status_code = fault_res.get("http_status", 200)
            if fault_res.get("is_error"):
                response = {"jsonrpc": "2.0", "error": fault_res["error"]}
                self._log_event(
                    {
                        "event_ordinal": ordinal,
                        "event_type": "tool_call_fault_injected",
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "fault_class": self.fault_interceptor.fault_record.fault_class.value,  # type: ignore
                        "fault_id": self.fault_interceptor.fault_record.fault_id,  # type: ignore
                        "error": fault_res["error"],
                    }
                )
                return response, status_code
            else:
                response = {"jsonrpc": "2.0", "result": fault_res["result"]}
                self._log_event(
                    {
                        "event_ordinal": ordinal,
                        "event_type": "tool_call_silent_fault_injected",
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "fault_class": self.fault_interceptor.fault_record.fault_class.value,  # type: ignore
                        "fault_id": self.fault_interceptor.fault_record.fault_id,  # type: ignore
                        "result": fault_res["result"],
                    }
                )
                return response, status_code

        # Normal execution
        handler = self.handlers.get(tool_name)
        if handler is None:
            # Default echo/stub if handler omitted
            res_data = {"status": "ok", "tool": tool_name, "arguments": arguments}
        else:
            try:
                res_data = handler(arguments)
            except Exception as exc:
                err_resp = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32000, "message": f"Tool execution failed: {exc}"},
                }
                self._log_event(
                    {
                        "event_ordinal": ordinal,
                        "event_type": "tool_call_exception",
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "is_redundant": is_redundant,
                        "error": str(exc),
                    }
                )
                return err_resp, 200

        response = {"jsonrpc": "2.0", "result": res_data}
        self._log_event(
            {
                "event_ordinal": ordinal,
                "event_type": "tool_call_success",
                "tool_name": tool_name,
                "arguments": arguments,
                "is_redundant": is_redundant,
                "schema_conforming": True,
                "result": res_data,
            }
        )
        return response, 200

    def _log_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if self.evidence_dir is not None:
            log_file = self.evidence_dir / self.state_log_name
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(canonical_json(event) + "\n")


def make_fastmcp_http_handler(
    runtime: FastMCPRuntime,
) -> type[http.server.BaseHTTPRequestHandler]:
    """Create a standard HTTP request handler serving the FastMCP JSON-RPC streamable interface."""

    class FastMCPHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Suppress default stderr logging during tests
            pass

        def _send_json(self, status: int, data: Any) -> None:
            body = canonical_bytes(data)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health" or parsed.path == "/":
                self._send_json(200, {"status": "ok", "version": MCP_SUBSTRATE_VERSION})
            elif parsed.path == "/events":
                # Return newline-delimited JSON of all runtime events
                body = "\n".join(canonical_json(e) for e in runtime.events).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_json(404, {"error": f"Path not found: {parsed.path}"})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/mcp" and parsed.path != "/":
                self._send_json(404, {"error": f"Invalid endpoint: {parsed.path}, use /mcp"})
                return

            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length)
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except Exception as exc:
                self._send_json(
                    400,
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": f"Parse error: {exc}"},
                    },
                )
                return

            req_id = payload.get("id")
            method = payload.get("method")
            params = payload.get("params", {})

            if method == "initialize":
                protocol_version = params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": protocol_version,
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "logging": {},
                        },
                        "serverInfo": {
                            "name": "eval-lab-fastmcp-sidecar",
                            "version": MCP_SUBSTRATE_VERSION,
                        },
                    },
                }
                self._send_json(200, res)
            elif method == "tools/list":
                tools = runtime.list_tools()
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "tools": tools,
                    },
                }
                self._send_json(200, res)
            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})
                call_resp, status_code = runtime.call_tool(tool_name, arguments)
                call_resp["id"] = req_id
                self._send_json(status_code, call_resp)
            else:
                self._send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Method not implemented: {method!r}"},
                    },
                )

    return FastMCPHandler


def compute_mcp_substrate_digest(
    topology: dict[str, Any],
    tool_defs: Sequence[MCPToolDefinition] | None = None,
) -> str:
    """Compute deterministic SHA-256 digest of the MCP substrate manifest and tool definitions."""
    payload: dict[str, Any] = {
        "substrate_version": MCP_SUBSTRATE_VERSION,
        "topology": topology,
    }
    if tool_defs is not None:
        payload["tools"] = [
            {
                "name": t.name,
                "description": t.description,
                "parameters": [p.to_dict() for p in t.parameters],
                "output_type": t.output_type,
                "is_distractor": t.is_distractor,
            }
            for t in sorted(tool_defs, key=lambda x: x.name)
        ]
    return compute_sha256(payload)


def render_mcp_compose_document(
    sidecar_service: str = DEFAULT_SIDECAR_SERVICE,
    volume_name: str | None = DEFAULT_VOLUME_NAME,
    volume_mount: str = DEFAULT_VOLUME_MOUNT,
    sidecar_build_context: str = "./mcp-server",
    main_image: str = "ghcr.io/eval-lab/eval-lab-agent-base@sha256:ba5e000000000000000000000000000000000000000000000000000000000000",
) -> dict[str, Any]:
    """Render a canonical Harbor workbench-v2 Compose document structure."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", sidecar_service):
        raise SubstrateError(f"Invalid sidecar service name: {sidecar_service!r}")

    services: dict[str, Any] = {
        "main": {
            "image": main_image,
        },
        sidecar_service: {
            "build": {
                "context": sidecar_build_context,
            },
        },
    }

    volumes_section: dict[str, Any] | None = None
    if volume_name:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", volume_name):
            raise SubstrateError(f"Invalid volume name: {volume_name!r}")
        services["main"]["volumes"] = [f"{volume_name}:{volume_mount}:ro"]
        services[sidecar_service]["volumes"] = [f"{volume_name}:{volume_mount}:rw"]
        volumes_section = {volume_name: None}

    doc: dict[str, Any] = {
        "services": services,
    }
    if volumes_section is not None:
        doc["volumes"] = volumes_section

    return doc


def validate_mcp_compose_document(
    data: Any, allowed_sidecar: str = DEFAULT_SIDECAR_SERVICE
) -> tuple[bool, list[str]]:
    """Strictly validate a Compose document against Harbor workbench-v2 and zero-leakage constraints.

    Rejects:
    - Host binds / ports
    - Unauthorized networks or network_mode
    - Environment variables on main
    - Privileged flags
    - More than 2 services
    - More than 1 task-local named volume
    """
    errors: list[str] = []
    if not isinstance(data, Mapping):
        return False, ["Compose document must be a mapping"]

    for top_key in data:
        if top_key not in {"services", "volumes", "version"}:
            errors.append(f"Unauthorized top-level Compose key: {top_key!r}")

    services = data.get("services")
    if not isinstance(services, Mapping):
        return False, ["Compose 'services' must be a mapping"]

    if "main" not in services:
        errors.append("Compose topology must declare 'main' service")

    service_names = list(services.keys())
    if len(service_names) > 2:
        errors.append(
            f"Compose topology admits at most 2 services, got {len(service_names)}: {service_names}"
        )

    top_volumes = data.get("volumes")
    volume_name: str | None = None
    if top_volumes is not None:
        if not isinstance(top_volumes, Mapping):
            errors.append("Compose 'volumes' must be a mapping")
        elif len(top_volumes) > 1:
            errors.append(f"At most 1 volume allowed, got {len(top_volumes)}")
        elif top_volumes:
            volume_name = next(iter(top_volumes.keys()))

    for name, s_cfg in services.items():
        if not isinstance(s_cfg, Mapping):
            errors.append(f"Service {name!r} configuration must be a mapping")
            continue

        if "network_mode" in s_cfg:
            errors.append(f"Service {name!r} may not declare custom network_mode")
        if "networks" in s_cfg:
            errors.append(f"Service {name!r} may not declare custom networks")
        if "ports" in s_cfg or "expose" in s_cfg:
            errors.append(f"Service {name!r} may not publish or expose host ports")
        if "privileged" in s_cfg:
            errors.append(f"Service {name!r} may not request privileged mode")
        if "depends_on" in s_cfg:
            errors.append(f"Service {name!r} may not declare depends_on")

        if name == "main" and "environment" in s_cfg and s_cfg["environment"]:
            errors.append("main service may not declare an environment")

        mounts = s_cfg.get("volumes", [])
        if mounts:
            if not isinstance(mounts, Sequence):
                errors.append(f"Service {name!r} volumes must be a sequence")
            else:
                for m in mounts:
                    if not isinstance(m, str):
                        errors.append(f"Service {name!r} volume entry must be a string, got {m!r}")
                        continue
                    parts = m.split(":")
                    if len(parts) < 2:
                        errors.append(f"Invalid volume syntax in service {name!r}: {m!r}")
                        continue
                    v_source, v_target = parts[0], parts[1]
                    mode = parts[2] if len(parts) > 2 else "rw"
                    if v_source != volume_name:
                        errors.append(
                            f"Service {name!r} volume source {v_source!r} does not match top-level {volume_name!r}"
                        )
                    if not v_target.startswith("/"):
                        errors.append(
                            f"Service {name!r} volume target {v_target!r} must be absolute"
                        )
                    if name == "main" and mode != "ro":
                        errors.append(
                            f"main service must mount evidence volume as read-only (:ro), got {mode!r}"
                        )
                    elif name != "main" and mode != "rw":
                        errors.append(
                            f"sidecar service {name!r} must mount evidence volume as read-write (:rw), got {mode!r}"
                        )

    return len(errors) == 0, errors
