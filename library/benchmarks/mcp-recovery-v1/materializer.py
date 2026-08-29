"""Harbor materializer for mcp-recovery-v1 using the shared FastMCP substrate."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
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
    ResolverProvenance,
    WheelhouseTarget,
    DEFAULT_TARGET_PLATFORM_TAG,
    DEFAULT_TARGET_PYTHON_TAG,
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
RESOLVER_PROVENANCE_ENV = "MCP_RECOVERY_RESOLVER_PROVENANCE"
SIDECAR_TARGET = WheelhouseTarget(DEFAULT_TARGET_PYTHON_TAG, DEFAULT_TARGET_PLATFORM_TAG)


def output_path(
    seed: int = 42,
    fault_mode: FaultClass | str = FaultClass.PERSISTENT_SIGNATURE_ERROR,
    persistence: int = 1,
) -> Path:
    fault = resolve_fault_class(fault_mode)
    slug = slugify_fault(fault)
    digest = source_digest(f"seed:{seed}:fault:{fault.value}:persistence:{persistence}")[:16]
    return DEFAULT_OUT_DIR / digest / f"mcp-recovery-seed{seed}-{slug}-p{persistence}"


def _wheelhouse_inputs() -> tuple[Path, ResolverProvenance] | None:
    wheelhouse_raw = os.environ.get(WHEELHOUSE_ENV, "").strip()
    provenance_raw = os.environ.get(RESOLVER_PROVENANCE_ENV, "").strip()
    if not wheelhouse_raw and not provenance_raw:
        return None
    if not wheelhouse_raw or not provenance_raw:
        raise ValueError(f"{WHEELHOUSE_ENV} and {RESOLVER_PROVENANCE_ENV} must be set together")
    wheelhouse = Path(wheelhouse_raw)
    provenance_path = Path(provenance_raw)
    if not wheelhouse.is_dir() or not provenance_path.is_file():
        raise ValueError("target wheelhouse and resolver provenance must exist")
    provenance = ResolverProvenance.from_json(json.loads(provenance_path.read_text(encoding="utf-8")))
    if provenance.target != SIDECAR_TARGET:
        raise ValueError("resolver provenance target does not match the pinned sidecar runtime")
    return wheelhouse, provenance


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


def _recovery_prelude(fault_class: FaultClass, persistence: int, fault_id: str) -> str:
    return f'''
import hashlib
from typing import Any

FAULT_ID = "{fault_id}"
PERSISTENCE = {persistence}
PRIVATE_STATE = Path(os.environ.get("MCP_RECOVERY_PRIVATE_STATE", "/app/.recovery-runtime-state.json"))
PUBLIC_ROOT = Path(os.environ.get("MCP_RECOVERY_OUTPUT", "/app/output"))
PUBLIC_EVENTS = PUBLIC_ROOT / "agent-events.jsonl"
PUBLIC_CERTIFICATE = PUBLIC_ROOT / "agent-state.json"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _load() -> dict[str, Any]:
    if PRIVATE_STATE.is_file():
        return json.loads(PRIVATE_STATE.read_text(encoding="utf-8"))
    return {{"hits": 0, "strategy_used": False, "records": {{}}}}


def _save(state: dict[str, Any]) -> None:
    PRIVATE_STATE.parent.mkdir(parents=True, exist_ok=True)
    PRIVATE_STATE.write_text(json.dumps(state, sort_keys=True) + "\\n", encoding="utf-8")


def _public_certificate(state: dict[str, Any]) -> None:
    PUBLIC_CERTIFICATE.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC_CERTIFICATE.write_text(
        json.dumps(
            {{
                "initial_digest": _digest({{}}),
                "final_digest": _digest(state["records"]),
                "event_count": EVENT_ORDINAL,
            }},
            sort_keys=True,
        ) + "\\n",
        encoding="utf-8",
    )


def _emit(tool: str, outcome: str, *, injection: bool = False) -> None:
    global EVENT_ORDINAL
    with EVENT_LOCK:
        event = {{"event_index": EVENT_ORDINAL, "event_type": "tool_result", "tool": tool, "outcome": outcome}}
        if injection:
            event["fault_id"] = FAULT_ID
        EVENT_ORDINAL += 1
        PUBLIC_EVENTS.parent.mkdir(parents=True, exist_ok=True)
        with open(PUBLIC_EVENTS, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\\n")
    _public_certificate(_load())


def _error(message: str) -> ToolResult:
    return ToolResult(content=message, is_error=True)


def recovery_refresh_auth(scope: str) -> dict[str, Any]:
    state = _load()
    state["strategy_used"] = True
    _save(state)
    _emit("refresh_auth", "ok")
    return {{"status": "updated"}}


def recovery_fallback_query(query: str) -> dict[str, Any]:
    state = _load()
    state["strategy_used"] = True
    _save(state)
    _emit("fallback_query", "ok")
    return {{"status": "updated"}}


def recovery_read_record(key: str) -> dict[str, Any]:
    state = _load()
    if "{fault_class.value}" == "silent_wrong_payload" and state["hits"] < PERSISTENCE:
        state["hits"] += 1
        _save(state)
        _emit("read_record", "ok", injection=True)
        return {{"key": key, "value": "stale-value", "exists": True}}
    value = state["records"].get(key)
    _emit("read_record", "ok")
    return {{"key": key, "value": value, "exists": key in state["records"]}}


def recovery_write_record(key: str, value: str) -> Any:
    state = _load()
    if not key or not value:
        _emit("write_record", "error")
        return _error("record key and value must be non-empty")
    if "{fault_class.value}" != "silent_wrong_payload" and state["hits"] < PERSISTENCE:
        state["hits"] += 1
        _save(state)
        _emit("write_record", "error", injection=True)
        messages = {{
            "persistent_signature_error": "operation rejected by the current session",
            "persistent_schema_mismatch": "requested record is unavailable",
            "transient_network_timeout": "operation did not complete",
            "transient_http_5xx": "tool response is unavailable",
        }}
        return _error(messages["{fault_class.value}"])
    if "{fault_class.value}" in ("persistent_signature_error", "persistent_schema_mismatch") and not state["strategy_used"]:
        _emit("write_record", "error")
        return _error("operation remains unavailable after retry")
    state["records"][key] = value
    _save(state)
    _emit("write_record", "ok")
    return {{"key": key, "value": value}}
'''


def generate_recovery_server_script(fault_class: FaultClass, persistence: int, fault_id: str) -> str:
    script = generate_fastmcp_server_script(
        tools=_recovery_tools(),
        server_name="mcp-recovery-sidecar",
        port=DEFAULT_MCP_PORT,
        evidence_path="/app/output/substrate-events.jsonl",
    )
    script = script.replace(
        "from fastmcp import FastMCP\n",
        "from fastmcp import FastMCP\nfrom fastmcp.tools import ToolResult\n",
        1,
    )
    script = script.replace("from pathlib import Path\n", "from pathlib import Path\nimport os\n", 1)
    script = script.replace(
        "EVENT_ORDINAL = 0\n",
        "EVENT_ORDINAL = 0\n" + _recovery_prelude(fault_class, persistence, fault_id),
        1,
    )
    script = script.replace(
        'host="0.0.0.0", port=8080',
        'host=os.environ.get("MCP_RECOVERY_HOST", "0.0.0.0"), port=int(os.environ.get("MCP_RECOVERY_PORT", "8080"))',
        1,
    )
    return script


def _embed_client() -> str:
    return (Path(__file__).resolve().parent / "client.py").read_text(encoding="utf-8")


def _oracle_script(strategy: str = "fallback_query") -> str:
    return (
        "#!/bin/sh\nset -eu\npython - <<'PY'\n"
        + _embed_client()
        + f'''
import time

session = McpHttpSession()
for _ in range(30):
    if session.initialize()[0] == 200:
        break
    time.sleep(0.2)
key, value = "agent-record", "agent-value"
for _ in range(12):
    _status, kind, _raw = session.call_tool("write_record", {{"key": key, "value": value}})
    if kind != "ok":
        session.call_tool("{strategy}", {{"scope": "retry"}} if "{strategy}" == "refresh_auth" else {{"query": key}})
        continue
    _status, _kind, read_raw = session.call_tool("read_record", {{"key": key}})
    if value in read_raw:
        break
    session.call_tool("{strategy}", {{"scope": "reconcile"}} if "{strategy}" == "refresh_auth" else {{"query": key}})
PY
'''
    )


def _blind_retry_script() -> str:
    return (
        "#!/bin/sh\nset -eu\npython - <<'PY'\n"
        + _embed_client()
        + '''
session = McpHttpSession()
session.initialize()
for _ in range(8):
    session.call_tool("write_record", {"key": "agent-record", "value": "agent-value"})
    session.call_tool("read_record", {"key": "agent-record"})
PY
'''
    )


def _wrong_repair_script() -> str:
    return (
        "#!/bin/sh\nset -eu\npython - <<'PY'\n"
        + _embed_client()
        + '''
session = McpHttpSession()
session.initialize()
session.call_tool("refresh_auth", {"scope": "retry"})
session.call_tool("write_record", {"key": "", "value": ""})
PY
'''
    )


def _verifier_py(fault_id: str, persistence: int) -> str:
    return f'''import json
from pathlib import Path

LOG_DIR = Path("/logs/verifier")
EVENTS = Path("/app/output/benchmark-events.jsonl")
CERTIFICATE = Path("/app/output/final-state.json")
EXPECTED_FAULT_ID = "{fault_id}"
EXPECTED_INJECTIONS = {persistence}
MUTATIONS = {{"refresh_auth", "fallback_query"}}


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    passed = False
    reason = "missing evidence"
    checks: dict = {{}}
    if EVENTS.is_file() and CERTIFICATE.is_file():
        events = [json.loads(line) for line in EVENTS.read_text(encoding="utf-8").splitlines() if line.strip()]
        certificate = json.loads(CERTIFICATE.read_text(encoding="utf-8"))
        monotonic = all(event.get("event_index") == index for index, event in enumerate(events))
        injections = [event for event in events if event.get("fault_id") == EXPECTED_FAULT_ID]
        first = min((event["event_index"] for event in injections), default=len(events))
        adapted = any(
            event.get("tool") in MUTATIONS and event.get("event_index", -1) > first
            for event in events
        )
        writes = [event for event in events if event.get("tool") == "write_record" and event.get("outcome") == "ok"]
        reads = [event for event in events if event.get("tool") == "read_record" and event.get("outcome") == "ok"]
        changed = certificate.get("initial_digest") != certificate.get("final_digest")
        exact = len(injections) == EXPECTED_INJECTIONS
        passed = bool(monotonic and exact and adapted and writes and reads and changed)
        reason = "ok" if passed else "verification failed"
        checks = {{"monotonic": monotonic, "exact_injections": exact, "adapted": adapted, "write": bool(writes), "read": bool(reads), "changed": changed}}
    reward = 1.0 if passed else 0.0
    (LOG_DIR / "reward.txt").write_text(f"{{reward:.1f}}\\n", encoding="utf-8")
    (LOG_DIR / "reward.json").write_text(json.dumps({{"reward": reward, "passed": float(passed)}}, sort_keys=True) + "\\n", encoding="utf-8")
    (LOG_DIR / "checks.json").write_text(json.dumps({{"passed": passed, "reason": reason, "checks": checks}}, sort_keys=True) + "\\n", encoding="utf-8")


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
    sidecar_proof = json.loads((sidecar_dir / "offline-build-proof.json").read_text(encoding="utf-8"))
    proof = {
        "kind": "offline_build_proof",
        "ecosystem": "pip",
        "lockfile": lockfile_rel,
        "lockfile_digest": _file_sha256(lockfile),
        "pinned_dependencies": pinned,
        "reviewed_by": "eval-lab-mcp-recovery-v1",
        # Workbench's generic proof schema ignores these, but they retain the
        # exact resolver-selected target/base/wheel bytes for provenance review.
        "target_python": sidecar_proof["target_python"],
        "target_platform": sidecar_proof["target_platform"],
        "base_image": sidecar_proof["base_image"],
        "base_image_index_digest": sidecar_proof["base_image_index_digest"],
        "base_image_amd64_manifest_digest": sidecar_proof["base_image_amd64_manifest_digest"],
        "wheels": sidecar_proof["wheels"],
    }
    (env_dir / "offline-build-proof.json").write_text(
        json.dumps(proof, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fault_record(cell: CellFactorsC, task_id: str) -> FaultInjectionRecord:
    # The fixture is verifier-only. It deliberately contains no agent task data.
    oracle_digest = compute_sha256({"family": "mcp-recovery-v1", "task_id": task_id})
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
        recovery_contract="verify-opaque-tool-outcomes-and-state-certificate",
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
    task_id = "mcp-recovery-" + compute_sha256({"seed": seed, "fault": fault.value, "dose": persistence})[:16]
    record = _fault_record(cell, task_id)

    task_toml = f'''schema_version = "1.4"
artifacts = [
    "/app/output/benchmark-events.jsonl",
    "/app/output/final-state.json",
]

[task]
name = "local-lab/{task_id}"
version = "1.0.0"
description = "MCP certified operational recovery"
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

[[verifier.collect]]
service = "main"
command = "if [ -f /app/output/agent-events.jsonl ]; then cp -f /app/output/agent-events.jsonl /app/output/benchmark-events.jsonl; fi"

[[verifier.collect]]
service = "main"
command = "if [ -f /app/output/agent-state.json ]; then cp -f /app/output/agent-state.json /app/output/final-state.json; fi"

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
        """# Task: MCP operational recovery

Interact with the streamable-HTTP MCP endpoint at `http://mcp-service:8080/mcp`.
Create a non-empty record of your choice, then independently read it back and
confirm that the returned value is the value you wrote. If a tool result shows
that the operation did not complete, diagnose the observed result and change
strategy before retrying. Do not treat a later success after identical retries
as sufficient evidence of recovery.
""",
        encoding="utf-8",
    )

    env_dir = target_dir / "environment"
    sidecar_dir = env_dir / SIDECAR_DIRNAME
    env_dir.mkdir(parents=True, exist_ok=True)
    if sidecar_dir.exists():
        shutil.rmtree(sidecar_dir)
    wheelhouse_inputs = _wheelhouse_inputs()
    wheelhouse = wheelhouse_inputs[0] if wheelhouse_inputs else None
    provenance = wheelhouse_inputs[1] if wheelhouse_inputs else None
    pkg = materialize_mcp_sidecar_package(
        target_dir=sidecar_dir,
        tools=_recovery_tools(),
        server_name="mcp-recovery-sidecar",
        port=DEFAULT_MCP_PORT,
        wheelhouse_source=wheelhouse,
        plan_only=wheelhouse_inputs is None,
        target=provenance.target if provenance else None,
        resolver_provenance=provenance,
    )
    (sidecar_dir / "server.py").write_text(generate_recovery_server_script(fault, persistence, record.fault_id), encoding="utf-8")
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
    (tests_dir / "verify.py").write_text(_verifier_py(record.fault_id, persistence), encoding="utf-8")
    fixtures = tests_dir / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
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
    fair = _oracle_script(strategy="refresh_auth")
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
