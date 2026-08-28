"""Streamable HTTP MCP Server & Runtime for action-memory-v1."""
from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import threading
from pathlib import Path
from typing import Any


class MemoryMCPHandler(http.server.BaseHTTPRequestHandler):
    def __init__(self, *args: Any, scenario_data: dict[str, Any], evidence_dir: Path, **kwargs: Any) -> None:
        self.scenario_data = scenario_data
        self.evidence_dir = evidence_dir
        self.events_log = evidence_dir / "benchmark-events.jsonl"
        self.final_state_file = evidence_dir / "final-state.json"
        self.event_counter = 0
        super().__init__(*args, **kwargs)

    def log_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.event_counter += 1
        record = {
            "event_index": self.event_counter,
            "event_type": event_type,
            "payload": payload,
        }
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        with self.events_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        try:
            req = json.loads(raw_body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        method = req.get("method")
        params = req.get("params", {})

        if method == "list_tools":
            tools = [
                {"name": "get_context_chunk", "description": "Fetch a specific chunk of log/context by chunk_id."},
                {"name": "list_context_chunks", "description": "List all available context chunk IDs."},
                {"name": "execute_mutation", "description": "Execute final state-bound mutation on the target entity."},
            ]
            self._send_json(200, {"tools": tools})
            return

        if method == "list_context_chunks":
            chunk_ids = [c["chunk_id"] for c in self.scenario_data.get("chunks", [])]
            self.log_event("list_chunks", {"count": len(chunk_ids)})
            self._send_json(200, {"chunk_ids": chunk_ids})
            return

        if method == "get_context_chunk":
            chunk_id = params.get("chunk_id")
            for c in self.scenario_data.get("chunks", []):
                if c["chunk_id"] == chunk_id:
                    self.log_event("read_chunk", {"chunk_id": chunk_id, "byte_count": c["byte_count"]})
                    self._send_json(200, {"chunk": c})
                    return
            self._send_json(404, {"error": f"Chunk not found: {chunk_id}"})
            return

        if method == "execute_mutation":
            entity_id = params.get("entity_id")
            attribute = params.get("attribute")
            bound_value = params.get("bound_value")

            self.log_event("execute_mutation", {
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
            with self.final_state_file.open("w", encoding="utf-8") as f:
                json.dump(final_state, f, indent=2, sort_keys=True)

            self._send_json(200, {"result": "success", "state": final_state})
            return

        self._send_json(400, {"error": f"Unknown method: {method}"})


def run_server(task_dir: Path, evidence_dir: Path, port: int = 8080) -> None:
    scenario_path = task_dir / "scenario.json"
    scenario_data = json.loads(scenario_path.read_text(encoding="utf-8")) if scenario_path.exists() else {}

    def handler_factory(*args: Any, **kwargs: Any) -> MemoryMCPHandler:
        return MemoryMCPHandler(*args, scenario_data=scenario_data, evidence_dir=evidence_dir, **kwargs)

    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler_factory)
    print(f"Action Memory MCP Server running on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-dir", type=Path, default=Path("/app/task_state"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("/app/evidence"))
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    run_server(args.task_dir, args.evidence_dir, args.port)
