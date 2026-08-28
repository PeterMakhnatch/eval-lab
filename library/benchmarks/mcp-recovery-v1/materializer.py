"""Materializer for mcp-recovery-v1 Harbor task packages adhering to workbench v2."""
from __future__ import annotations

import json
from pathlib import Path

from source import reject_committed_corpora, source_digest

DEFAULT_OUT_DIR = Path("derived/harbor-tasks/mcp-recovery")
PINNED_BASE_IMAGE = "python@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251"

FAULT_MODES = [
    "permission-denied",
    "not-found",
    "timeout",
    "malformed-output",
    "silent-wrong-result",
]

PERSISTENCE_LEVELS = [1, 2]


def safe_slug_fault_mode(mode: str) -> str:
    return mode.replace("_", "-")


def output_path(seed: int = 42, fault_mode: str = "permission-denied", persistence: int = 1) -> Path:
    slug_mode = safe_slug_fault_mode(fault_mode)
    digest = source_digest(f"seed:{seed}:fault:{slug_mode}:persistence:{persistence}")[:16]
    return DEFAULT_OUT_DIR / digest / f"mcp-recovery-seed{seed}-{slug_mode}-p{persistence}"


def materialize_task(
    target_dir: Path,
    seed: int = 42,
    fault_mode: str = "permission-denied",
    persistence: int = 1,
) -> Path:
    slug_mode = safe_slug_fault_mode(fault_mode)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. task.toml (workbench v2 compliant, Linux no-network baseline)
    task_toml = f"""schema_version = "1.4"
artifacts = [
    "/app/shared_state/benchmark-events.jsonl",
    "/app/shared_state/final-state.json",
]

[task]
name = "local-lab/mcp-recovery-seed{seed}-{slug_mode}-p{persistence}"
version = "1.0.0"
description = "MCP error recovery and state certificate benchmark under {slug_mode} with persistence {persistence}"
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
command = "if [ -f /app/shared_state/benchmark-events.jsonl ]; then cp /app/shared_state/benchmark-events.jsonl /app/shared_state/benchmark-events.jsonl; fi"

[[verifier.collect]]
service = "main"
command = "if [ -f /app/shared_state/final-state.json ]; then cp /app/shared_state/final-state.json /app/shared_state/final-state.json; fi"

[verifier.environment]
network_mode = "no-network"

[agent]
timeout_sec = 120.0

[environment]
network_mode = "no-network"
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
1. Initialize session with `initialize` call.
2. Query available tools with `tools/list`.
3. Call tool `write_record` with `key="canary_key"` and `value="verified_value"`.
4. If tool invocation encounters errors (such as HTTP 403 Permission Denied, HTTP 404 Not Found, Timeout, Malformed Output, or Silent Wrong Results), diagnose the fault, mutate your credentials/strategy, and achieve state invariant restoration.
5. Invariant requirement: The MCP server must contain `canary_key="verified_value"` and `session_status="completed"`.
"""
    (target_dir / "instruction.md").write_text(instruction, encoding="utf-8")

    # 3. environment/
    env_dir = target_dir / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)

    # docker-compose.yaml
    compose_yaml = """services:
  main:
    build: .
    volumes:
      - mcp_data:/app/shared_state:ro
  mcp-sidecar:
    build: ./sidecar
    volumes:
      - mcp_data:/app/shared_state:rw

volumes:
  mcp_data:
"""
    (env_dir / "docker-compose.yaml").write_text(compose_yaml, encoding="utf-8")

    # environment/Dockerfile (agent main container)
    main_dockerfile = f"""FROM {PINNED_BASE_IMAGE}

WORKDIR /app

RUN mkdir -p /app/shared_state
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
import hashlib
from pathlib import Path

SHARED_DIR = Path("/app/shared_state")
EVENTS_FILE = SHARED_DIR / "benchmark-events.jsonl"
FINAL_STATE_FILE = SHARED_DIR / "final-state.json"

FAULT_MODE = "{slug_mode}"
PERSISTENCE = {persistence}
hits = 0
event_idx = 0

db = {{"session_status": "initialized"}}

def log_event(event_type, payload):
    global event_idx
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    ev = {{
        "event_index": event_idx,
        "event_type": event_type,
        "payload": payload,
    }}
    event_idx += 1
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, sort_keys=True, separators=(',', ':')) + "\\n")
    update_final_state()

def update_final_state():
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(json.dumps(db, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    with open(FINAL_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({{"records": db, "digest": digest, "event_count": event_idx}}, f, sort_keys=True)

class McpHttpHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        global hits, db
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

        if method == "initialize":
            log_event("mcp_initialized", {{"protocolVersion": "2024-11-05"}})
            res = {{
                "protocolVersion": "2024-11-05",
                "capabilities": {{"tools": {{}}}},
                "serverInfo": {{"name": "mcp-recovery-sidecar", "version": "1.0.0"}}
            }}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": res}}).encode('utf-8'))
            return

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
                db["__auth__"] = arguments.get("scope", "admin")
                log_event("tool_executed", {{"tool": "refresh_auth", "arguments": arguments}})
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{"content": [{{"type": "text", "text": "auth refreshed"}}]}}}}).encode('utf-8'))
                return

            if tool_name == "fallback_query":
                log_event("tool_executed", {{"tool": "fallback_query", "arguments": arguments}})
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{"content": [{{"type": "text", "text": "replica fallback ok"}}]}}}}).encode('utf-8'))
                return

            if tool_name == "write_record":
                if hits < PERSISTENCE:
                    hits += 1
                    log_event("fault_injected", {{"tool": "write_record", "fault_class": FAULT_MODE, "hit": hits}})
                    if FAULT_MODE == "permission-denied":
                        self.send_response(403)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "error": {{"code": 403, "message": "Permission denied"}}}}).encode('utf-8'))
                        return
                    elif FAULT_MODE == "not-found":
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
                    elif FAULT_MODE == "malformed-output":
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html')
                        self.end_headers()
                        self.wfile.write(b"<html>502 Bad Gateway unparseable chunk\\x00\\xff")
                        return
                    elif FAULT_MODE == "silent-wrong-result":
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{"content": [{{"type": "text", "text": "silent_corrupted_value"}}]}}}}).encode('utf-8'))
                        return

                # Normal success execution
                db[arguments.get("key")] = arguments.get("value")
                db["session_status"] = "completed"
                log_event("tool_executed", {{"tool": "write_record", "arguments": arguments}})
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{"content": [{{"type": "text", "text": "write ok"}}]}}}}).encode('utf-8'))
                return

        self.send_response(404)
        self.end_headers()

if __name__ == "__main__":
    update_final_state()
    server = http.server.HTTPServer(("0.0.0.0", 8000), McpHttpHandler)
    server.serve_forever()
"""
    (sidecar_dir / "server.py").write_text(sidecar_server_code, encoding="utf-8")

    # 4. solution/ (Oracle Solution calling actual MCP HTTP server and adapting)
    sol_dir = target_dir / "solution"
    sol_dir.mkdir(parents=True, exist_ok=True)
    solve_sh = f"""#!/bin/sh
set -eu
python - <<'PY'
import json
import http.client
import time

def call_mcp(method, params=None):
    payload = {{"jsonrpc": "2.0", "id": 1, "method": method}}
    if params is not None:
        payload["params"] = params
    body = json.dumps(payload)
    headers = {{"Content-Type": "application/json"}}
    conn = http.client.HTTPConnection("mcp-sidecar", 8000, timeout=10)
    try:
        conn.request("POST", "/mcp", body, headers)
        resp = conn.getresponse()
        data = resp.read().decode('utf-8')
        return resp.status, data
    except Exception as exc:
        return 500, str(exc)
    finally:
        conn.close()

# Wait for sidecar server
for _ in range(30):
    try:
        st, res = call_mcp("initialize")
        if st == 200:
            break
    except Exception:
        pass
    time.sleep(0.5)

# Step 1: Initial call encountering fault
st1, res1 = call_mcp("tools/call", {{"name": "write_record", "arguments": {{"key": "canary_key", "value": "verified_value"}}}})

# Step 2: Adaptation based on fault
fault_mode = "{slug_mode}"
if fault_mode == "permission-denied":
    call_mcp("tools/call", {{"name": "refresh_auth", "arguments": {{"scope": "admin_write"}}}})
elif fault_mode == "not-found":
    call_mcp("tools/call", {{"name": "fallback_query", "arguments": {{"query": "canary_key"}}}})

# Persistence retry if required
if {persistence} > 1 and fault_mode in ("timeout", "malformed-output", "silent-wrong-result"):
    call_mcp("tools/call", {{"name": "write_record", "arguments": {{"key": "canary_key", "value": "verified_value"}}}})

# Final successful mutation
st_final, res_final = call_mcp("tools/call", {{"name": "write_record", "arguments": {{"key": "canary_key", "value": "verified_value"}}}})
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

RUN mkdir -p /app/shared_state /logs/verifier \\
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
EVIDENCE_FILE = Path("/app/shared_state/benchmark-events.jsonl")
FINAL_STATE_FILE = Path("/app/shared_state/final-state.json")

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
import http.client
import time

def call_mcp(method, params=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params: payload["params"] = params
    body = json.dumps(payload)
    headers = {"Content-Type": "application/json"}
    conn = http.client.HTTPConnection("mcp-sidecar", 8000, timeout=10)
    try:
        conn.request("POST", "/mcp", body, headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode('utf-8')
    except Exception as e: return 500, str(e)
    finally: conn.close()

for _ in range(30):
    try:
        if call_mcp("initialize")[0] == 200: break
    except: pass
    time.sleep(0.5)

call_mcp("tools/call", {"name": "write_record", "arguments": {"key": "canary_key", "value": "verified_value"}})
call_mcp("tools/call", {"name": "refresh_auth", "arguments": {"scope": "admin_write"}})
call_mcp("tools/call", {"name": "write_record", "arguments": {"key": "canary_key", "value": "verified_value"}})
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
import http.client
import time

def call_mcp(method, params=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params: payload["params"] = params
    body = json.dumps(payload)
    headers = {"Content-Type": "application/json"}
    conn = http.client.HTTPConnection("mcp-sidecar", 8000, timeout=10)
    try:
        conn.request("POST", "/mcp", body, headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode('utf-8')
    except Exception as e: return 500, str(e)
    finally: conn.close()

for _ in range(30):
    try:
        if call_mcp("initialize")[0] == 200: break
    except: pass
    time.sleep(0.5)

# Blind identical calls without auth/adaptation
call_mcp("tools/call", {"name": "write_record", "arguments": {"key": "canary_key", "value": "verified_value"}})
PY
"""
    (adv_dir / "blind-retry.sh").write_text(blind_sh, encoding="utf-8")
    (adv_dir / "blind-retry.sh").chmod(0o755)

    # workbench/adversarial/wrong-repair.sh
    wrong_sh = """#!/bin/sh
set -eu
python - <<'PY'
import json
import http.client
import time

def call_mcp(method, params=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params: payload["params"] = params
    body = json.dumps(payload)
    headers = {"Content-Type": "application/json"}
    conn = http.client.HTTPConnection("mcp-sidecar", 8000, timeout=10)
    try:
        conn.request("POST", "/mcp", body, headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode('utf-8')
    except Exception as e: return 500, str(e)
    finally: conn.close()

for _ in range(30):
    try:
        if call_mcp("initialize")[0] == 200: break
    except: pass
    time.sleep(0.5)

call_mcp("tools/call", {"name": "write_record", "arguments": {"key": "wrong_key", "value": "corrupted_val"}})
PY
"""
    (adv_dir / "wrong-repair.sh").write_text(wrong_sh, encoding="utf-8")
    (adv_dir / "wrong-repair.sh").chmod(0o755)

    return target_dir


def materialize(target: Path | None = None, seed: int = 42, fault_mode: str = "permission-denied", persistence: int = 1) -> Path:
    reject_committed_corpora()
    out = target or output_path(seed, fault_mode, persistence)
    return materialize_task(out, seed=seed, fault_mode=fault_mode, persistence=persistence)


def materialize_all_campaign0(seed: int = 42) -> list[Path]:
    """Materializes all 10 Campaign 0 cells (5 fault classes x 2 persistence levels)."""
    reject_committed_corpora()
    paths = []
    for fm in FAULT_MODES:
        for p in PERSISTENCE_LEVELS:
            path = materialize_task(output_path(seed=seed, fault_mode=fm, persistence=p), seed=seed, fault_mode=fm, persistence=p)
            paths.append(path)
    return paths
