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

## Protocol & Execution Flow
1. Initialize connection using standard MCP `initialize`.
2. Discover tools with `tools/list` (`write_record`, `read_record`, `refresh_auth`, `fallback_query`).
3. Set record `canary_key="verified_value"` via `write_record`.
4. Read back and verify record integrity via `read_record(key="canary_key")`.
5. Error Recovery & Invariant Policy:
   - If HTTP 403 / Permission Denied occurs, invoke `refresh_auth(scope="admin_write")` and retry.
   - If HTTP 404 / Not Found occurs, query `fallback_query(query="canary_key")` to sync replica and retry write.
   - If HTTP 408 / Timeout or malformed stream occurs, mutate strategy and retry with backoff.
   - If `read_record` or `write_record` returns corrupted / unexpected payload (such as silent corruption), re-issue verified write until `read_record` confirms `value="verified_value"`.
   - Ensure final state satisfies: `canary_key="verified_value"` and `session_status="completed"`.
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
                {{"name": "read_record", "description": "Read a record", "inputSchema": {{"type": "object", "properties": {{"key": {{"type": "string"}}}}}}}},
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
                db["__auth__"] = arguments.get("scope", "admin_write")
                log_event("tool_executed", {{"tool": "refresh_auth", "arguments": arguments}})
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{"content": [{{"type": "text", "text": "auth refreshed"}}]}}}}).encode('utf-8'))
                return

            if tool_name == "fallback_query":
                db["__fallback_synced__"] = True
                log_event("tool_executed", {{"tool": "fallback_query", "arguments": arguments}})
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{"content": [{{"type": "text", "text": "replica fallback ok"}}]}}}}).encode('utf-8'))
                return

            if tool_name == "read_record":
                key = arguments.get("key")
                val = db.get(key)
                # Check for silent wrong result fault on read
                if FAULT_MODE == "silent-wrong-result" and hits < PERSISTENCE:
                    hits += 1
                    log_event("fault_injected", {{"tool": "read_record", "fault_class": FAULT_MODE, "hit": hits}})
                    corrupt_val = "corrupted_silent_val"
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{"content": [{{"type": "text", "text": json.dumps({{"key": key, "value": corrupt_val, "exists": True}})}}]}}}}).encode('utf-8'))
                    return

                log_event("tool_executed", {{"tool": "read_record", "arguments": arguments}})
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": {{"content": [{{"type": "text", "text": json.dumps({{"key": key, "value": val, "exists": key in db}})}}]}}}}).encode('utf-8'))
                return

            if tool_name == "write_record":
                # Permission check: if fault mode is permission-denied, require admin_write scope
                if FAULT_MODE == "permission-denied" and db.get("__auth__") != "admin_write":
                    if hits < PERSISTENCE:
                        hits += 1
                    log_event("fault_injected", {{"tool": "write_record", "fault_class": FAULT_MODE, "hit": hits}})
                    self.send_response(403)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "error": {{"code": 403, "message": "Permission denied: write requires admin_write scope"}}}}).encode('utf-8'))
                    return

                # Not-found check: if fault mode is not-found, require fallback replica sync
                if FAULT_MODE == "not-found" and not db.get("__fallback_synced__"):
                    if hits < PERSISTENCE:
                        hits += 1
                    log_event("fault_injected", {{"tool": "write_record", "fault_class": FAULT_MODE, "hit": hits}})
                    self.send_response(404)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "error": {{"code": 404, "message": "Not found"}}}}).encode('utf-8'))
                    return

                # Recurrent faults (timeout, malformed-output) require strategy mutation (backoff parameter or retry)
                if hits < PERSISTENCE:
                    hits += 1
                    log_event("fault_injected", {{"tool": "write_record", "fault_class": FAULT_MODE, "hit": hits}})
                    if FAULT_MODE == "timeout":
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

    # 4. solution/ (Universal Oracle Solution diagnosing actual responses without hardcoded fault knowledge)
    sol_dir = target_dir / "solution"
    sol_dir.mkdir(parents=True, exist_ok=True)
    solve_sh = """#!/bin/sh
set -eu
python - <<'PY'
import json
import http.client
import time

def call_mcp(method, params=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    body = json.dumps(payload)
    headers = {"Content-Type": "application/json"}
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

# Wait for sidecar server initialize
for _ in range(30):
    try:
        st, res = call_mcp("initialize")
        if st == 200:
            break
    except Exception:
        pass
    time.sleep(0.5)

# Discover tools
call_mcp("tools/list")

# Diagnostic loop: Attempt write and adapt dynamically based on observed response
target_key = "canary_key"
target_val = "verified_value"

for attempt in range(10):
    st, res = call_mcp("tools/call", {"name": "write_record", "arguments": {"key": target_key, "value": target_val}})
    
    if st == 403:
        # Permission denied: mutate authentication scope
        call_mcp("tools/call", {"name": "refresh_auth", "arguments": {"scope": "admin_write"}})
        continue
    elif st == 404:
        # Not found: query fallback replica
        call_mcp("tools/call", {"name": "fallback_query", "arguments": {"query": target_key}})
        continue
    elif st in (408, 500, 502) or not res.startswith("{"):
        # Timeout or malformed stream: retry with exponential backoff / strategy mutation
        time.sleep(0.2 * (attempt + 1))
        continue
    elif st == 200:
        # Check integrity by reading back
        st_r, res_r = call_mcp("tools/call", {"name": "read_record", "arguments": {"key": target_key}})
        if st_r == 200 and res_r.startswith("{"):
            try:
                parsed = json.loads(res_r)
                text_content = parsed.get("result", {}).get("content", [{}])[0].get("text", "")
                data = json.loads(text_content)
                if data.get("value") == target_val:
                    # Verified integrity restored
                    break
            except Exception:
                pass
        # If read returned corrupted data, re-issue write
        time.sleep(0.2)
PY
"""
    solve_path = sol_dir / "solve.sh"
    solve_path.write_text(solve_sh, encoding="utf-8")
    solve_path.chmod(0o755)

    # 5. tests/ (Separate Verifier with Strict Adaptive Scoring)
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

    # tests/verify.py (Strict verifier checking adaptation, numeric reward only in reward.json)
    verify_py = f"""import json
import sys
from pathlib import Path

LOG_DIR = Path("/logs/verifier")
EVIDENCE_FILE = Path("/app/shared_state/benchmark-events.jsonl")
FINAL_STATE_FILE = Path("/app/shared_state/final-state.json")

EXPECTED_FAULT_MODE = "{slug_mode}"
EXPECTED_PERSISTENCE = {persistence}

def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if not EVIDENCE_FILE.is_file() or not FINAL_STATE_FILE.is_file():
        passed = False
        reason = "missing evidence artifacts"
        checks = {{"evidence_present": {{"passed": False, "message": "artifacts missing"}}}}
    else:
        try:
            events = [json.loads(line) for line in EVIDENCE_FILE.read_text(encoding='utf-8').splitlines() if line.strip()]
            final_state = json.loads(FINAL_STATE_FILE.read_text(encoding='utf-8'))
            
            # 1. Monotonic indexing
            monotonic = all(ev.get("event_index") == idx for idx, ev in enumerate(events))
            
            # 2. Invariants restored
            records = final_state.get("records", {{}})
            invariants = records.get("canary_key") == "verified_value" and records.get("session_status") == "completed"
            
            # 3. Fault events and tool events
            faults = [e for e in events if e.get("event_type") == "fault_injected"]
            tools = [e for e in events if e.get("event_type") == "tool_executed"]
            
            # 4. Strict adaptation check (Must not reward un-adapted blind retry)
            adaptation = False
            if EXPECTED_FAULT_MODE == "permission-denied":
                adaptation = any(t.get("payload", {{}}).get("tool") == "refresh_auth" for t in tools)
            elif EXPECTED_FAULT_MODE == "not-found":
                adaptation = any(t.get("payload", {{}}).get("tool") == "fallback_query" for t in tools)
            elif EXPECTED_FAULT_MODE == "silent-wrong-result":
                adaptation = any(t.get("payload", {{}}).get("tool") == "read_record" for t in tools)
            elif EXPECTED_FAULT_MODE in ("timeout", "malformed-output"):
                # For timeout/malformed, must observe required fault hits before tool success
                adaptation = len(faults) >= EXPECTED_PERSISTENCE and len(tools) >= 1

            # NOP and blind-retry without adaptive tool calls will fail adaptation
            passed = monotonic and invariants and adaptation and len(faults) >= EXPECTED_PERSISTENCE
            reason = "ok" if passed else f"failed verification (monotonic={{monotonic}}, invariants={{invariants}}, adaptation={{adaptation}}, faults={{len(faults)}})"
            checks = {{
                "monotonic": {{"passed": monotonic, "message": "monotonic event index"}},
                "invariants": {{"passed": invariants, "message": "canary invariants restored"}},
                "adaptation": {{"passed": adaptation, "message": f"adapted under {{EXPECTED_FAULT_MODE}}"}},
            }}
        except Exception as exc:
            passed = False
            reason = f"exception during verification: {{exc}}"
            checks = {{"exception": {{"passed": False, "message": str(exc)}}}}

    reward_val = 1.0 if passed else 0.0
    
    rewards_dict = {{"reward": reward_val, "passed": float(passed)}}
    (LOG_DIR / "reward.txt").write_text(f"{{reward_val:.1f}}\\n")
    (LOG_DIR / "reward.json").write_text(json.dumps(rewards_dict, sort_keys=True) + "\\n")
    (LOG_DIR / "checks.json").write_text(json.dumps({{"passed": passed, "reason": reason, "checks": checks}}, indent=2) + "\\n")
    print(json.dumps({{"passed": passed, "reward": reward_val, "reason": reason}}))

if __name__ == "__main__":
    main()
"""
    (tests_dir / "verify.py").write_text(verify_py, encoding="utf-8")

    # 6. workbench/ (Adversarial, Fair Alternative, Please Hack)
    wb_dir = target_dir / "workbench"
    wb_dir.mkdir(parents=True, exist_ok=True)
    adv_dir = wb_dir / "adversarial"
    adv_dir.mkdir(parents=True, exist_ok=True)

    # workbench/fair-alternative.sh (Valid general solver)
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

call_mcp("tools/list")

target_k = "canary_key"
target_v = "verified_value"

for attempt in range(10):
    st, res = call_mcp("tools/call", {"name": "write_record", "arguments": {"key": target_k, "value": target_v}})
    if st == 403:
        call_mcp("tools/call", {"name": "refresh_auth", "arguments": {"scope": "admin_write"}})
        continue
    elif st == 404:
        call_mcp("tools/call", {"name": "fallback_query", "arguments": {"query": target_k}})
        continue
    elif st in (408, 500, 502) or not res.startswith("{"):
        time.sleep(0.2 * (attempt + 1))
        continue
    elif st == 200:
        st_r, res_r = call_mcp("tools/call", {"name": "read_record", "arguments": {"key": target_k}})
        if st_r == 200 and target_v in res_r:
            break
        time.sleep(0.2)
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

    # workbench/adversarial/blind-retry.sh (Fixed policy blind retry control that fails on all faults)
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

# Blind identical calls without auth/fallback mutation or integrity verification
for _ in range(1):
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
