"""Streamable-HTTP MCP client that parses FastMCP CallToolResult isError frames."""
import json
import os
from typing import Any

import http.client


def mcp_host() -> str:
    return os.environ.get("MCP_HOST", "mcp-service")


def mcp_port() -> int:
    return int(os.environ.get("MCP_PORT", "8080"))


def parse_mcp_frames(raw: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    if "data:" in raw:
        for line in raw.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    frames.append({"raw": payload})
                else:
                    if isinstance(parsed, dict):
                        frames.append(parsed)
        if frames:
            return frames
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return [{"raw": raw}]
        if isinstance(parsed, dict):
            return [parsed]
    return [{"raw": raw}]


def _frame_text(frame: dict[str, Any]) -> str:
    chunks: list[str] = []
    err = frame.get("error")
    if isinstance(err, dict):
        chunks.append(str(err.get("message", "")))
    result = frame.get("result")
    if isinstance(result, dict):
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(str(item.get("text", "")))
        structured = result.get("structuredContent")
        if structured is not None:
            chunks.append(json.dumps(structured, sort_keys=True))
    if frame.get("raw"):
        chunks.append(str(frame["raw"]))
    return "\n".join(chunk for chunk in chunks if chunk)


def classify_tool_response(status: int, raw: str) -> tuple[str, str]:
    """Map FastMCP HTTP/SSE tool responses onto recovery classes.

    FastMCP returns HTTP 200 + result.isError for raised tool exceptions.
    """
    frames = parse_mcp_frames(raw)
    blob = raw
    is_error = status >= 400
    for frame in frames:
        result = frame.get("result") if isinstance(frame, dict) else None
        if isinstance(result, dict) and result.get("isError") is True:
            is_error = True
        blob = blob + "\n" + _frame_text(frame if isinstance(frame, dict) else {})
    lowered = blob.lower()
    if "permission denied" in lowered:
        return "permission", blob
    if "not found" in lowered:
        return "not_found", blob
    if "timeout" in lowered:
        return "timeout", blob
    if "502" in lowered or "unparseable" in lowered:
        return "malformed", blob
    if "corrupted_silent_val" in lowered:
        return "silent", blob
    if is_error:
        return "error", blob
    return "ok", blob


class McpHttpSession:
    def __init__(self, host: str | None = None, port: int | None = None) -> None:
        self.host = host or mcp_host()
        self.port = port if port is not None else mcp_port()
        self.session_id: str | None = None

    def request(self, payload: dict[str, Any], timeout: int = 10) -> tuple[int, str]:
        body = json.dumps(payload)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.request("POST", "/mcp", body, headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            session = resp.getheader("mcp-session-id")
            if session:
                self.session_id = session
            return resp.status, raw
        except Exception as exc:
            return 500, str(exc)
        finally:
            conn.close()

    def initialize(self) -> tuple[int, str]:
        status, raw = self.request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-recovery-client", "version": "1"},
                },
            }
        )
        self.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return status, raw

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[int, str, str]:
        status, raw = self.request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        kind, blob = classify_tool_response(status, raw)
        return status, kind, blob
