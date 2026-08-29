"""Streamable-HTTP MCP client for FuncDAG Harbor oracles and controls."""

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


def frame_payload(frame: dict[str, Any]) -> Any:
    result = frame.get("result")
    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
    return frame


class McpHttpSession:
    def __init__(self, host: str | None = None, port: int | None = None, url: str | None = None) -> None:
        if url:
            remainder = url.split("://", 1)[-1]
            hostport = remainder.split("/", 1)[0]
            if ":" in hostport:
                parsed_host, port_s = hostport.rsplit(":", 1)
                self.host = parsed_host or mcp_host()
                self.port = int(port_s) if port_s.isdigit() else mcp_port()
            else:
                self.host = hostport or mcp_host()
                self.port = mcp_port()
        else:
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
                    "clientInfo": {"name": "mcp-funcdag-client", "version": "1.0.0"},
                },
            }
        )
        self.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return status, raw

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        _status, raw = self.request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        frames = parse_mcp_frames(raw)
        if not frames:
            return {"raw": raw}
        payload = frame_payload(frames[0])
        if isinstance(payload, dict) and "value" in payload:
            return payload["value"]
        return payload
