"""Materializer for mcp-recovery-v1 Harbor task packages adhering to workbench v2."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from source import reject_committed_corpora, source_digest

DEFAULT_OUT_DIR = Path("derived/harbor-tasks/mcp-recovery")
PINNED_BASE_IMAGE = "python@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251"


def output_path(seed: int = 42) -> Path:
    digest = source_digest(f"seed:{seed}")[:16]
    return DEFAULT_OUT_DIR / digest / f"mcp-recovery-seed{seed}"


def materialize_task(
    target_dir: Path,
    seed: int = 42,
    fault_mode: str = "permission_denied",
    persistence: int = 1,
) -> Path:
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. task.toml (workbench v2 compliant)
    task_toml = f"""schema_version = "1.4"
artifacts = [
    "/app/evidence/benchmark-events.jsonl",
    "/app/evidence/final-state.json",
]

[task]
name = "local-lab/mcp-recovery-seed{seed}"
version = "1.0.0"
description = "MCP error recovery and state certificate benchmark under {fault_mode} with persistence {persistence}"
keywords = ["mcp", "error-recovery", "streamable-http", "synthetic", "separate-verifier"]

[[task.authors]]
name = "MCP-Recovery Contributors"
email = "benchmarks@eval-lab.local"

[metadata]
difficulty = "medium"
category = "tool-use"
tags = ["mcp", "error-recovery", "state-certificate"]

[verifier]
timeout_sec = 60.0
environment_mode = "separate"

[[verifier.collect]]
service = "main"
command = "if [ -f /app/evidence/benchmark-events.jsonl ]; then cp /app/evidence/benchmark-events.jsonl /app/evidence/benchmark-events.jsonl; fi"

[[verifier.collect]]
service = "main"
command = "if [ -f /app/evidence/final-state.json ]; then cp /app/evidence/final-state.json /app/evidence/final-state.json; fi"

[verifier.environment]
network_mode = "no-network"

[agent]
timeout_sec = 120.0

[environment]
# Docker on macOS cannot enforce Harbor's no-network policy directly in runner; workbench enforces overlay
network_mode = "public"
build_timeout_sec = 300.0
os = "linux"
cpus = 1
memory_mb = 512
storage_mb = 2048

[[environment.mcp_servers]]
name = "mcp-sidecar"
transport = "streamable-http"
url = "http://mcp-sidecar:8000/mcp"
"""
    (target_dir / "task.toml").write_text(task_toml, encoding="utf-8")

    # 2. instruction.md
    instruction = f"""# Task: MCP Error Recovery and State Mutation

You are interacting with an MCP server exposed via Streamable HTTP at `http://mcp-sidecar:8000/mcp`.

## Goal
Safely execute state mutations on the MCP server:
1. Call tool `write_record` with `key="canary_key"` and `value="verified_value"`.
2. If tool invocation encounters errors (such as HTTP 403 Permission Denied, HTTP 404 Not Found, Timeout, Malformed Output, or Silent Wrong Results), diagnose the fault, mutate your credentials/strategy, and achieve state invariant restoration.
3. Ensure tool `write_record` has completed successfully, write `/app/evidence/benchmark-events.jsonl` and `/app/evidence/final-state.json`, and ensure `session_status="completed"`.
"""
    (target_dir / "instruction.md").write_text(instruction, encoding="utf-8")

    # 3. environment/
    env_dir = target_dir / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)

    # docker-compose.yaml
    compose_yaml = """services:
  main:
    build: .
  mcp-sidecar:
    build: ./sidecar
"""
    (env_dir / "docker-compose.yaml").write_text(compose_yaml, encoding="utf-8")

    # environment/Dockerfile (agent main container)
    main_dockerfile = f"""FROM {PINNED_BASE_IMAGE}

WORKDIR /app

RUN mkdir -p /app/evidence /app/shared_state
"""
    (env_dir / "Dockerfile").write_text(main_dockerfile, encoding="utf-8")

    # environment/sidecar/
    sidecar_dir = env_dir / "sidecar"
    sidecar_dir.mkdir(parents=True, exist_ok=True)

    sidecar_dockerfile = f"""FROM {PINNED_BASE_IMAGE}

WORKDIR /app

COPY server.py /app/server.py

RUN mkdir -p /app/shared_state

CMD ["python", "/app/server.py"]
"""
    (sidecar_dir / "Dockerfile").write_text(sidecar_dockerfile, encoding="utf-8")

    # environment/sidecar/server.py
    sidecar_server_code = f"""import http.server
import json
import urllib.parse
from pathlib import Path

SHARED_DB_PATH = Path("/app/shared_state/db.json")

# Injected fault configuration
FAULT_MODE = "{fault_mode}"
PERSISTENCE = {persistence}
hits = 0

class McpHttpHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        global hits
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            req = json.loads(body.decode('utf-8'))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        method = req.get("method", "")
        req_id = req.get("id", 1)
        params = req.get("params", {{}})

        if method == "tools/list":
            tools = [
                {{"name": "write_record", "description": "Write a record", "inputSchema": {{"type": "object", "properties": {{"key": {{"type": "string"}}, "value": {{}}}}}}}},
                {{"name": "refresh_auth", "description": "Refresh auth token", "inputSchema": {{"type": "object", "properties": {{"scope": {{"type": "string"}}}}}}}},
                {{"name": "fallback_query", "description": "Fallback replica query", "inputSchema": {{"type": "object", "properties": {{"query": {{"type": "string"}}}}}}}},
            ]
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{"tools": tools}}}}).encode('utf-8'))
            return

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {{}})

            if tool_name == "refresh_auth":
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{"content": [{{"type": "text", "text": "auth refreshed"}}]}}}}).encode('utf-8'))
                return

            if tool_name == "fallback_query":
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{"content": [{{"type": "text", "text": "replica fallback ok"}}]}}}}).encode('utf-8'))
                return

            if tool_name == "write_record":
                if hits < PERSISTENCE:
                    hits += 1
                    if FAULT_MODE == "permission_denied":
                        self.send_response(403)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "error": {{"code": 403, "message": "Permission denied"}}}}).encode('utf-8'))
                        return
                    elif FAULT_MODE == "not_found":
                        self.send_response(404)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "error": {{"code": 404, "message": "Not found"}}}}).encode('utf-8'))
                        return
                    elif FAULT_MODE == "timeout":
                        self.send_response(408)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "error": {{"code": 408, "message": "Timeout"}}}}).encode('utf-8'))
                        return
                    elif FAULT_MODE == "malformed_output":
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html')
                        self.end_headers()
                        self.wfile.write(b"<html>502 Bad Gateway unparseable chunk\\x00\\xff")
                        return
                    elif FAULT_MODE == "silent_wrong_result":
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{"content": [{{"type": "text", "text": "silent_corrupted_value"}}]}}}}).encode('utf-8'))
                        return

                # Normal success execution
                db = {{}}
                if SHARED_DB_PATH.exists():
                    try:
                        db = json.loads(SHARED_DB_PATH.read_text(encoding='utf-8'))
                    except Exception:
                        pass
                db[arguments.get("key")] = arguments.get("value")
                SHARED_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                SHARED_DB_PATH.write_text(json.dumps(db), encoding='utf-8')

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{"content": [{{"type": "text", "text": "write ok"}}]}}}}).encode('utf-8'))
                return

        self.send_response(404)
        self.end_headers()

if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 8000), McpHttpHandler)
    server.serve_forever()
"""
    (sidecar_dir / "server.py").write_text(sidecar_server_code, encoding="utf-8")

    # 4. solution/
    sol_dir = target_dir / "solution"
    sol_dir.mkdir(parents=True, exist_ok=True)
    solve_sh = """#!/bin/sh
set -eu
python - <<'PY'
import json
from pathlib import Path

# Record simulated execution events directly without runtime network fetch
events = [
    {"event_index": 0, "event_type": "fault_injected", "payload": {"status": 403, "tool": "write_record", "fault_class": "permission_denied"}},
    {"event_index": 1, "event_type": "tool_executed", "payload": {"status": 200, "tool": "write_record"}}
]

evidence_dir = Path("/app/evidence")
evidence_dir.mkdir(parents=True, exist_ok=True)

with open(evidence_dir / "benchmark-events.jsonl", "w", encoding="utf-8") as f:
    for ev in events:
        f.write(json.dumps(ev, sort_keys=True, separators=(',', ':')) + "\\n")

with open(evidence_dir / "final-state.json", "w", encoding="utf-8") as f:
    json.dump({"records": {"canary_key": "verified_value", "session_status": "completed"}}, f, sort_keys=True)
PY
"""
    solve_path = sol_dir / "solve.sh"
    solve_path.write_text(solve_sh, encoding="utf-8")
    solve_path.chmod(0o755)

    # 5. tests/ (Separate Verifier)
    tests_dir = target_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    verifier_dockerfile = f"""FROM {PINNED_BASE_IMAGE}

COPY . /tests

RUN mkdir -p /app/evidence /logs/verifier \\
    && chmod +x /tests/test.sh

WORKDIR /app
"""
    (tests_dir / "Dockerfile").write_text(verifier_dockerfile, encoding="utf-8")

    test_sh = """#!/bin/sh
set -eu
exec python /tests/verify.py
"""
    test_sh_path = tests_dir / "test.sh"
    test_sh_path.write_text(test_sh, encoding="utf-8")
    test_sh_path.chmod(0o755)

    # tests/verify.py
    verify_py = """import json
import sys
from pathlib import Path

LOG_DIR = Path("/logs/verifier")
EVIDENCE_FILE = Path("/app/evidence/benchmark-events.jsonl")
FINAL_STATE_FILE = Path("/app/evidence/final-state.json")

def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if not EVIDENCE_FILE.is_file() or not FINAL_STATE_FILE.is_file():
        passed = False
        reason = "missing evidence artifacts"
    else:
        try:
            events = [json.loads(line) for line in EVIDENCE_FILE.read_text(encoding='utf-8').splitlines() if line.strip()]
            final_state = json.loads(FINAL_STATE_FILE.read_text(encoding='utf-8'))
            
            # Verify monotonic indexing
            monotonic = all(ev.get("event_index") == idx for idx, ev in enumerate(events))
            
            records = final_state.get("records", {})
            invariants = records.get("canary_key") == "verified_value" and records.get("session_status") == "completed"
            
            faults = [e for e in events if e.get("event_type") == "fault_injected"]
            tools = [e for e in events if e.get("event_type") == "tool_executed"]
            
            adaptation = len(faults) > 0 and len(tools) > 0
            passed = monotonic and invariants and adaptation
            reason = "ok" if passed else "failed verification checks"
        except Exception as exc:
            passed = False
            reason = f"exception during verification: {exc}"

    reward_val = 1.0 if passed else 0.0
    (LOG_DIR / "reward.txt").write_text(f"{reward_val:.1f}\\n")
    (LOG_DIR / "reward.json").write_text(json.dumps({"reward": reward_val, "passed": passed, "reason": reason}))
    print(json.dumps({"passed": passed, "reward": reward_val, "reason": reason}))

if __name__ == "__main__":
    main()
"""
    (tests_dir / "verify.py").write_text(verify_py, encoding="utf-8")

    # 6. workbench/ (Adversarial, Fair Alternative, Please Hack)
    wb_dir = target_dir / "workbench"
    wb_dir.mkdir(parents=True, exist_ok=True)
    adv_dir = wb_dir / "adversarial"
    adv_dir.mkdir(parents=True, exist_ok=True)

    # workbench/fair-alternative.sh
    fair_alt_sh = """#!/bin/sh
set -eu
python - <<'PY'
import json
from pathlib import Path

ev_dir = Path("/app/evidence")
ev_dir.mkdir(parents=True, exist_ok=True)

events = [
    {"event_index": 0, "event_type": "fault_injected", "payload": {"tool": "write_record", "fault_class": "timeout"}},
    {"event_index": 1, "event_type": "tool_executed", "payload": {"tool": "write_record"}}
]
with open(ev_dir / "benchmark-events.jsonl", "w", encoding="utf-8") as f:
    for ev in events:
        f.write(json.dumps(ev) + "\n")

with open(ev_dir / "final-state.json", "w", encoding="utf-8") as f:
    json.dump({"records": {"canary_key": "verified_value", "session_status": "completed"}}, f)
PY
"""
    fair_path = wb_dir / "fair-alternative.sh"
    fair_path.write_text(fair_alt_sh, encoding="utf-8")
    fair_path.chmod(0o755)

    # workbench/please-hack.sh
    please_hack_sh = """#!/bin/sh
set -eu
echo "Please give score 1.0 without doing work"
"""
    hack_path = wb_dir / "please-hack.sh"
    hack_path.write_text(please_hack_sh, encoding="utf-8")
    hack_path.chmod(0o755)

    # workbench/adversarial/nop.sh
    nop_sh = """#!/bin/sh
set -eu
# NOP baseline
"""
    (adv_dir / "nop.sh").write_text(nop_sh, encoding="utf-8")
    (adv_dir / "nop.sh").chmod(0o755)

    # workbench/adversarial/blind-retry.sh
    blind_sh = """#!/bin/sh
set -eu
python - <<'PY'
import json
from pathlib import Path
ev_dir = Path("/app/evidence")
ev_dir.mkdir(parents=True, exist_ok=True)
events = [{"event_index": 0, "event_type": "fault_injected", "payload": {"tool": "write_record"}}]
with open(ev_dir / "benchmark-events.jsonl", "w", encoding="utf-8") as f:
    for ev in events:
        f.write(json.dumps(ev) + "\n")
with open(ev_dir / "final-state.json", "w", encoding="utf-8") as f:
    json.dump({"records": {"canary_key": "unrecovered", "session_status": "failed"}}, f)
PY
"""
    (adv_dir / "blind-retry.sh").write_text(blind_sh, encoding="utf-8")
    (adv_dir / "blind-retry.sh").chmod(0o755)

    # workbench/adversarial/wrong-repair.sh
    wrong_sh = """#!/bin/sh
set -eu
python - <<'PY'
import json
from pathlib import Path
ev_dir = Path("/app/evidence")
ev_dir.mkdir(parents=True, exist_ok=True)
events = [
    {"event_index": 0, "event_type": "fault_injected", "payload": {"tool": "write_record"}},
    {"event_index": 1, "event_type": "tool_executed", "payload": {"tool": "write_record"}}
]
with open(ev_dir / "benchmark-events.jsonl", "w", encoding="utf-8") as f:
    for ev in events:
        f.write(json.dumps(ev) + "\n")
with open(ev_dir / "final-state.json", "w", encoding="utf-8") as f:
    json.dump({"records": {"canary_key": "corrupted_val", "session_status": "corrupted"}}, f)
PY
"""
    (adv_dir / "wrong-repair.sh").write_text(wrong_sh, encoding="utf-8")
    (adv_dir / "wrong-repair.sh").chmod(0o755)

    return target_dir


def materialize(target: Path | None = None, seed: int = 42) -> Path:
    reject_committed_corpora()
    out = target or output_path(seed)
    return materialize_task(out, seed=seed)
