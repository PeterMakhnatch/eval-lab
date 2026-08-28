"""Harbor materializer for mcp-recovery-v1 using the shared FastMCP substrate."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

from evallab.benchmark_program_contracts import (
    CellFactorsC,
    FaultClass,
    FaultInjectionRecord,
    SyntheticFamilySpec,
    SyntheticFamilyType,
    compute_sha256,
)
from evallab.mcp_substrate import (
    DEFAULT_INTERNAL_NETWORK_NAME,
    DEFAULT_MCP_PORT,
    DEFAULT_PINNED_BASE_IMAGE,
    DEFAULT_SIDECAR_SERVICE,
    DEFAULT_VOLUME_MOUNT,
    DEFAULT_VOLUME_NAME,
    MCPToolDefinition,
    MCPToolParameter,
    generate_fastmcp_server_script,
    materialize_mcp_sidecar_package,
    render_mcp_compose_document,
    render_mcp_sidecar_dockerfile,
)

from contract import CAMPAIGN0_FAULTS, CAMPAIGN0_PERSISTENCE, resolve_fault_class, slugify_fault
from source import reject_committed_corpora, source_digest

DEFAULT_OUT_DIR = Path("derived/harbor-tasks/mcp-recovery")
SIDECAR_DIRNAME = "mcp-server"
WHEELHOUSE_ENV = "MCP_RECOVERY_WHEELHOUSE"
DEFAULT_WHEELHOUSE = Path("/tmp/fastmcp3_wheelhouse")


def output_path(
    seed: int = 42,
    fault_mode: FaultClass | str = FaultClass.PERSISTENT_SIGNATURE_ERROR,
    persistence: int = 1,
) -> Path:
    fault = resolve_fault_class(fault_mode)
    slug = slugify_fault(fault)
    digest = source_digest(f"seed:{seed}:fault:{fault.value}:persistence:{persistence}")[:16]
    return DEFAULT_OUT_DIR / digest / f"mcp-recovery-seed{seed}-{slug}-p{persistence}"


def _wheelhouse() -> Path | None:
    raw = os.environ.get(WHEELHOUSE_ENV, "")
    path = Path(raw) if raw else DEFAULT_WHEELHOUSE
    if path.is_dir() and any(path.glob("*.whl")):
        return path
    return None


def _recovery_tools() -> list[MCPToolDefinition]:
    return [
        MCPToolDefinition(
            name="write_record",
            description="Write a record into the recovery store",
            parameters=(
                MCPToolParameter(name="key", type_name="str", description="Record key"),
                MCPToolParameter(name="value", type_name="str", description="Record value"),
            ),
            execution_body=(
                "return recovery_write_record(key, value)"
            ),
        ),
        MCPToolDefinition(
            name="read_record",
            description="Read a record from the recovery store",
            parameters=(MCPToolParameter(name="key", type_name="str", description="Record key"),),
            execution_body="return recovery_read_record(key)",
        ),
        MCPToolDefinition(
            name="refresh_auth",
            description="Refresh authorization scope",
            parameters=(MCPToolParameter(name="scope", type_name="str", description="Requested scope"),),
            execution_body="return recovery_refresh_auth(scope)",
        ),
        MCPToolDefinition(
            name="fallback_query",
            description="Query the replica fallback",
            parameters=(MCPToolParameter(name="query", type_name="str", description="Replica query"),),
            execution_body="return recovery_fallback_query(query)",
        ),
    ]


def _recovery_prelude(fault_class: FaultClass, persistence: int) -> str:
    return f'''
import hashlib
from typing import Any

FAULT_CLASS = "{fault_class.value}"
PERSISTENCE = {persistence}
HITS = 0
DB: dict[str, Any] = {{"session_status": "initialized"}}
OUT_DIR = Path(os.environ.get("MCP_RECOVERY_OUTPUT", "/app/output"))
RECOVERY_EVENTS = OUT_DIR / "benchmark-events.jsonl"
FINAL_STATE_FILE = OUT_DIR / "final-state.json"


def persist_certificate() -> None:
    digest = hashlib.sha256(
        json.dumps(DB, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    FINAL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    FINAL_STATE_FILE.write_text(
        json.dumps({{"records": DB, "digest": digest, "event_count": EVENT_ORDINAL}}, sort_keys=True)
        + "\\n",
        encoding="utf-8",
    )


def log_recovery(event_type: str, payload: dict[str, Any]) -> None:
    global EVENT_ORDINAL
    with EVENT_LOCK:
        event = {{
            "event_index": EVENT_ORDINAL,
            "event_type": event_type,
            "payload": payload,
        }}
        EVENT_ORDINAL += 1
        RECOVERY_EVENTS.parent.mkdir(parents=True, exist_ok=True)
        with open(RECOVERY_EVENTS, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\\n")
    persist_certificate()


def recovery_refresh_auth(scope: str) -> dict[str, Any]:
    DB["__auth__"] = scope
    log_recovery("tool_executed", {{"tool": "refresh_auth", "arguments": {{"scope": scope}}}})
    return {{"status": "authenticated", "scope": scope}}


def recovery_fallback_query(query: str) -> dict[str, Any]:
    DB["__fallback_synced__"] = True
    log_recovery("tool_executed", {{"tool": "fallback_query", "arguments": {{"query": query}}}})
    return {{"status": "success", "source": "replica", "query": query}}


def recovery_read_record(key: str) -> dict[str, Any]:
    global HITS
    if FAULT_CLASS == "silent_wrong_payload" and HITS < PERSISTENCE:
        HITS += 1
        log_recovery("fault_injected", {{"tool": "read_record", "fault_class": FAULT_CLASS, "hit": HITS}})
        return {{"key": key, "value": "corrupted_silent_val", "exists": True}}
    value = DB.get(key)
    log_recovery("tool_executed", {{"tool": "read_record", "arguments": {{"key": key}}}})
    return {{"key": key, "value": value, "exists": key in DB}}


def recovery_write_record(key: str, value: str) -> dict[str, Any]:
    global HITS
    if FAULT_CLASS == "persistent_signature_error":
        if HITS < PERSISTENCE:
            HITS += 1
            log_recovery("fault_injected", {{"tool": "write_record", "fault_class": FAULT_CLASS, "hit": HITS}})
            raise ValueError("Permission denied: write requires admin_write scope")
        if DB.get("__auth__") != "admin_write":
            raise ValueError("Permission denied: write requires admin_write scope")
    elif FAULT_CLASS == "persistent_schema_mismatch":
        if HITS < PERSISTENCE:
            HITS += 1
            log_recovery("fault_injected", {{"tool": "write_record", "fault_class": FAULT_CLASS, "hit": HITS}})
            raise ValueError("Not found")
        if not DB.get("__fallback_synced__"):
            raise ValueError("Not found")
    elif FAULT_CLASS == "transient_network_timeout" and HITS < PERSISTENCE:
        HITS += 1
        log_recovery("fault_injected", {{"tool": "write_record", "fault_class": FAULT_CLASS, "hit": HITS}})
        raise TimeoutError("Timeout")
    elif FAULT_CLASS == "transient_http_5xx" and HITS < PERSISTENCE:
        HITS += 1
        log_recovery("fault_injected", {{"tool": "write_record", "fault_class": FAULT_CLASS, "hit": HITS}})
        raise RuntimeError("502 Bad Gateway unparseable chunk")
    DB[key] = value
    DB["session_status"] = "completed"
    log_recovery("tool_executed", {{"tool": "write_record", "arguments": {{"key": key, "value": value}}}})
    return {{"status": "success", "key": key}}
'''


def generate_recovery_server_script(fault_class: FaultClass, persistence: int) -> str:
    script = generate_fastmcp_server_script(
        tools=_recovery_tools(),
        server_name="mcp-recovery-sidecar",
        port=DEFAULT_MCP_PORT,
        evidence_path="/app/output/substrate-events.jsonl",
    )
    if "import os\n" not in script:
        script = script.replace("from pathlib import Path\n", "from pathlib import Path\nimport os\n", 1)
    script = script.replace(
        "EVENT_ORDINAL = 0\n",
        "EVENT_ORDINAL = 0\n" + _recovery_prelude(fault_class, persistence),
        1,
    )
    script = script.replace('mcp.run(transport="sse"', 'mcp.run(transport="http"')
    script = script.replace('host="0.0.0.0"', 'host=os.environ.get("MCP_RECOVERY_HOST", "0.0.0.0")')
    script = script.replace(
        f"port={DEFAULT_MCP_PORT}",
        'port=int(os.environ.get("MCP_RECOVERY_PORT", "8080"))',
    )
    return script


def _embed_client() -> str:
    return (Path(__file__).resolve().parent / "client.py").read_text(encoding="utf-8")


def _oracle_script() -> str:
    return (
        "#!/bin/sh\nset -eu\npython - <<'PY'\n"
        + _embed_client()
        + """
import time

session = McpHttpSession()
for _ in range(30):
    status, _raw = session.initialize()
    if status == 200:
        break
    time.sleep(0.5)
session.request({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
target_key = "canary_key"
target_val = "verified_value"
for _attempt in range(12):
    _status, kind, _blob = session.call_tool(
        "write_record", {"key": target_key, "value": target_val}
    )
    if kind == "permission":
        session.call_tool("refresh_auth", {"scope": "admin_write"})
        continue
    if kind == "not_found":
        session.call_tool("fallback_query", {"query": target_key})
        continue
    if kind in ("timeout", "malformed", "error"):
        session.call_tool("fallback_query", {"query": target_key})
        continue
    _rs, _rk, rblob = session.call_tool("read_record", {"key": target_key})
    if target_val in rblob:
        break
PY
"""
    )


def _blind_retry_script() -> str:
    return (
        "#!/bin/sh\nset -eu\npython - <<'PY'\n"
        + _embed_client()
        + """
session = McpHttpSession()
session.initialize()
for _ in range(6):
    session.call_tool("write_record", {"key": "canary_key", "value": "verified_value"})
PY
"""
    )


def _wrong_repair_script() -> str:
    return (
        "#!/bin/sh\nset -eu\npython - <<'PY'\n"
        + _embed_client()
        + """
session = McpHttpSession()
session.initialize()
session.call_tool("write_record", {"key": "wrong_key", "value": "corrupted_val"})
PY
"""
    )


def _verifier_py(fault_class: FaultClass, persistence: int) -> str:
    return f'''import json
from pathlib import Path

LOG_DIR = Path("/logs/verifier")
EVIDENCE_FILE = Path("/app/output/benchmark-events.jsonl")
CERT_PATH = Path("/app/output/final-state.json")
EXPECTED_FAULT_MODE = "{fault_class.value}"
EXPECTED_PERSISTENCE = {persistence}
NEED_TOOL = {{
    "persistent_signature_error": "refresh_auth",
    "persistent_schema_mismatch": "fallback_query",
    "transient_network_timeout": "fallback_query",
    "transient_http_5xx": "fallback_query",
    "silent_wrong_payload": "read_record",
}}


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    checks: dict = {{}}
    if not EVIDENCE_FILE.is_file() or not CERT_PATH.is_file():
        passed = False
        reason = "missing evidence artifacts"
        checks = {{"evidence_present": {{"passed": False, "message": "artifacts missing"}}}}
        auto_clear = False
    else:
        events = [
            json.loads(line)
            for line in EVIDENCE_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        final_state = json.loads(CERT_PATH.read_text(encoding="utf-8"))
        monotonic = all(event.get("event_index") == idx for idx, event in enumerate(events))
        records = final_state.get("records", {{}})
        invariants = (
            records.get("canary_key") == "verified_value"
            and records.get("session_status") == "completed"
        )
        faults = [event for event in events if event.get("event_type") == "fault_injected"]
        tools = [event for event in events if event.get("event_type") == "tool_executed"]
        need = NEED_TOOL.get(EXPECTED_FAULT_MODE, "")
        names = [(item.get("payload") or {{}}).get("tool") for item in tools]
        adaptation = bool(need) and need in names
        auto_clear = bool(invariants and len(faults) >= EXPECTED_PERSISTENCE and not adaptation)
        passed = bool(monotonic and invariants and adaptation and len(faults) >= EXPECTED_PERSISTENCE)
        reason = "ok" if passed else "failed verification"
        checks = {{
            "monotonic": {{"passed": monotonic}},
            "invariants": {{"passed": invariants}},
            "adaptation": {{"passed": adaptation}},
            "auto_clear": {{"passed": auto_clear}},
            "fault_count": {{"passed": len(faults) >= EXPECTED_PERSISTENCE, "count": len(faults)}},
        }}
    reward_val = 1.0 if passed else 0.0
    rewards = {{"reward": reward_val, "passed": float(passed)}}
    (LOG_DIR / "reward.txt").write_text(f"{{reward_val:.1f}}\\n", encoding="utf-8")
    (LOG_DIR / "reward.json").write_text(json.dumps(rewards, sort_keys=True) + "\\n", encoding="utf-8")
    (LOG_DIR / "checks.json").write_text(
        json.dumps({{"passed": passed, "reason": reason, "checks": checks}}, indent=2) + "\\n",
        encoding="utf-8",
    )
    print(json.dumps({{"passed": passed, "reward": reward_val, "auto_clear": auto_clear}}))


if __name__ == "__main__":
    main()
'''


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _write_environment_build_proof(env_dir: Path, sidecar_dir: Path) -> None:
    lockfile_rel = f"{SIDECAR_DIRNAME}/requirements.txt"
    lockfile = env_dir / lockfile_rel
    wheelhouse = sidecar_dir / "wheelhouse"
    if not lockfile.is_file() or not wheelhouse.is_dir():
        return
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        return
    pinned = []
    for wheel in wheels:
        stem_parts = wheel.name[:-4].split("-")
        name = stem_parts[0].replace("_", "-")
        version = stem_parts[1] if len(stem_parts) > 1 else "0"
        pinned.append({"name": name, "version": version, "wheel": wheel.name})
    proof = {
        "kind": "offline_build_proof",
        "ecosystem": "pip",
        "lockfile": lockfile_rel,
        "lockfile_digest": _file_sha256(lockfile),
        "pinned_dependencies": pinned,
        "reviewed_by": "eval-lab-mcp-recovery-v1",
    }
    (env_dir / "offline-build-proof.json").write_text(
        json.dumps(proof, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fault_record(cell: CellFactorsC, task_id: str) -> FaultInjectionRecord:
    oracle_digest = compute_sha256(
        {"canary_key": "verified_value", "session_status": "completed", "fault": cell.fault_class.value}
    )
    return FaultInjectionRecord(
        fault_id=compute_sha256(
            {"task_id": task_id, "fault": cell.fault_class.value, "dose": cell.fault_injection_count}
        ),
        task_id=task_id,
        twin_task_id=f"{task_id}-clean-twin",
        target_service=DEFAULT_SIDECAR_SERVICE,
        target_tool="write_record" if cell.fault_class != FaultClass.SILENT_WRONG_PAYLOAD else "read_record",
        fault_class=cell.fault_class,
        target_canonical_event_ordinal=1,
        injection_payload={"persistence": cell.fault_injection_count},
        recovery_contract="adapt-from-observed-mcp-error-then-restore-invariants",
        verifier_oracle_digest=oracle_digest,
    )


def materialize_task(
    target_dir: Path,
    seed: int = 42,
    fault_mode: FaultClass | str = FaultClass.PERSISTENT_SIGNATURE_ERROR,
    persistence: int = 1,
) -> Path:
    fault = resolve_fault_class(fault_mode)
    slug = slugify_fault(fault)
    cell = CellFactorsC(fault_class=fault, fault_injection_count=persistence, seed=seed)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    task_id = f"mcp-recovery-seed{seed}-{slug}-p{persistence}"

    task_toml = f'''schema_version = "1.4"
artifacts = [
    "/app/output/benchmark-events.jsonl",
    "/app/output/final-state.json",
]

[task]
name = "local-lab/{task_id}"
version = "1.0.0"
description = "MCP certified error recovery under {fault.value} persistence {persistence}"
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
name = "{DEFAULT_SIDECAR_SERVICE}"
transport = "streamable-http"
url = "http://{DEFAULT_SIDECAR_SERVICE}:{DEFAULT_MCP_PORT}/mcp"
'''
    (target_dir / "task.toml").write_text(task_toml, encoding="utf-8")
    (target_dir / "instruction.md").write_text(
        """# Task: MCP Error Recovery and State Mutation

You are interacting with an MCP server at `http://mcp-service:8080/mcp`.

1. Call `initialize`, then `tools/list`.
2. Write `canary_key="verified_value"` with `write_record`.
3. Confirm with `read_record`.
4. On observed errors, adapt from the response only:
   - permission/signature failures: `refresh_auth(scope="admin_write")` then retry
   - not-found/schema failures: `fallback_query` then retry
   - timeout or malformed/5xx: `fallback_query` then retry; identical retry is not enough
   - silent corruption: keep writing until `read_record` returns `verified_value`
5. Final invariants: `canary_key="verified_value"` and `session_status="completed"`.
""",
        encoding="utf-8",
    )

    env_dir = target_dir / "environment"
    sidecar_dir = env_dir / SIDECAR_DIRNAME
    env_dir.mkdir(parents=True, exist_ok=True)
    wheelhouse = _wheelhouse()
    pkg = materialize_mcp_sidecar_package(
        target_dir=sidecar_dir,
        tools=_recovery_tools(),
        server_name="mcp-recovery-sidecar",
        port=DEFAULT_MCP_PORT,
        wheelhouse_source=wheelhouse,
        plan_only=wheelhouse is None,
    )
    (sidecar_dir / "server.py").write_text(generate_recovery_server_script(fault, persistence), encoding="utf-8")
    if wheelhouse is None:
        (sidecar_dir / "Dockerfile").write_text(
            f"FROM {DEFAULT_PINNED_BASE_IMAGE}\n\nWORKDIR /app\nCOPY server.py /app/server.py\n"
            "RUN mkdir -p /app/output\nCMD [\"python\", \"/app/server.py\"]\n",
            encoding="utf-8",
        )
    else:
        (sidecar_dir / "Dockerfile").write_text(render_mcp_sidecar_dockerfile(), encoding="utf-8")

    compose = render_mcp_compose_document(
        sidecar_service=DEFAULT_SIDECAR_SERVICE,
        volume_name=DEFAULT_VOLUME_NAME,
        volume_mount=DEFAULT_VOLUME_MOUNT,
        sidecar_build_context=f"./{SIDECAR_DIRNAME}",
        network_name=DEFAULT_INTERNAL_NETWORK_NAME,
    )
    compose["services"]["main"] = {
        "build": ".",
        "networks": [DEFAULT_INTERNAL_NETWORK_NAME],
        "volumes": [f"{DEFAULT_VOLUME_NAME}:{DEFAULT_VOLUME_MOUNT}:ro"],
    }
    (env_dir / "docker-compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    (env_dir / "Dockerfile").write_text(
        f"FROM {DEFAULT_PINNED_BASE_IMAGE}\n\nWORKDIR /app\nRUN mkdir -p /app/output\n",
        encoding="utf-8",
    )
    _write_environment_build_proof(env_dir, sidecar_dir)

    sol_dir = target_dir / "solution"
    sol_dir.mkdir(parents=True, exist_ok=True)
    _write_executable(sol_dir / "solve.sh", _oracle_script())

    tests_dir = target_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "Dockerfile").write_text(
        f"FROM {DEFAULT_PINNED_BASE_IMAGE}\n\nCOPY . /tests\n"
        "RUN mkdir -p /app/output /logs/verifier && chmod +x /tests/test.sh\nWORKDIR /app\n",
        encoding="utf-8",
    )
    _write_executable(tests_dir / "test.sh", "#!/bin/sh\nset -eu\nexec python /tests/verify.py\n")
    (tests_dir / "verify.py").write_text(_verifier_py(fault, persistence), encoding="utf-8")
    fixtures = tests_dir / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    record = _fault_record(cell, task_id)
    (fixtures / "fault_record.json").write_text(
        json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    spec = SyntheticFamilySpec(
        family=SyntheticFamilyType.FAMILY_C_FAULT_RECOVERY,
        variant_id=task_id,
        fault_record=record,
        hidden_contract_hash=record.verifier_oracle_digest,
        twin_task_ref=record.twin_task_id,
    )
    (fixtures / "family_spec.json").write_text(
        json.dumps(spec.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    wb = target_dir / "workbench"
    adv = wb / "adversarial"
    adv.mkdir(parents=True, exist_ok=True)
    fair = _oracle_script().replace("target_key = \"canary_key\"", "target_key = 'canary_key'")
    _write_executable(wb / "fair-alternative.sh", fair)
    _write_executable(wb / "please-hack.sh", "#!/bin/sh\nset -eu\necho please-hack\n")
    _write_executable(adv / "nop.sh", "#!/bin/sh\nset -eu\n# nop\n")
    _write_executable(adv / "blind-retry.sh", _blind_retry_script())
    _write_executable(adv / "wrong-repair.sh", _wrong_repair_script())
    return target_dir


def materialize(
    target: Path | None = None,
    seed: int = 42,
    fault_mode: FaultClass | str = FaultClass.PERSISTENT_SIGNATURE_ERROR,
    persistence: int = 1,
) -> Path:
    reject_committed_corpora()
    out = target or output_path(seed, fault_mode, persistence)
    return materialize_task(out, seed=seed, fault_mode=fault_mode, persistence=persistence)


def materialize_all_campaign0(seed: int = 42) -> list[Path]:
    reject_committed_corpora()
    paths: list[Path] = []
    for fault in CAMPAIGN0_FAULTS:
        for persistence in CAMPAIGN0_PERSISTENCE:
            paths.append(
                materialize_task(
                    output_path(seed=seed, fault_mode=fault, persistence=persistence),
                    seed=seed,
                    fault_mode=fault,
                    persistence=persistence,
                )
            )
    return paths
