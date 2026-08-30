"""Harbor materializer for mcp-recovery-v1 using authenticated AES-GCM evidence envelopes."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import stat
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
    DEFAULT_TARGET_PLATFORM_TAG,
    DEFAULT_TARGET_PYTHON_TAG,
    DEFAULT_VOLUME_MOUNT,
    DEFAULT_VOLUME_NAME,
    MCPToolDefinition,
    MCPToolParameter,
    ResolverProvenance,
    RuntimeAsset,
    WheelhouseTarget,
    generate_fastmcp_server_script,
    materialize_mcp_sidecar_package,
    render_mcp_compose_document,
)

from contract import (
    ALTERNATIVE_REPAIR_MOVES,
    CAMPAIGN0_FAULTS,
    CAMPAIGN0_PERSISTENCE,
    DESIGNATED_REPAIR_MOVES,
    FAMILY,
    compute_mutation_digest,
    get_alternative_repair,
    get_designated_repair,
    resolve_fault_class,
    slugify_fault,
)
from source import reject_committed_corpora, source_digest

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = Path("derived/harbor-tasks/mcp-recovery")
SIDECAR_DIRNAME = "mcp-server"
WHEELHOUSE_ENV = "MCP_RECOVERY_WHEELHOUSE"
RESOLVER_PROVENANCE_ENV = "MCP_RECOVERY_RESOLVER_PROVENANCE"
EVIDENCE_KEY_ENV = "MCP_RECOVERY_EVIDENCE_KEY_FILE"
SIDECAR_TARGET = WheelhouseTarget(DEFAULT_TARGET_PYTHON_TAG, DEFAULT_TARGET_PLATFORM_TAG)


def _read_evidence_key(key_input: bytes | Path | str | None) -> bytes:
    """Read an unpredictable 32-byte evidence key.

    Requires an explicit 32-byte key or a trusted 0400/0600 key file.
    Deterministic/public-metadata fallback derivation is strictly prohibited.
    """
    if isinstance(key_input, bytes):
        if len(key_input) != 32:
            raise ValueError(f"Evidence key must be exactly 32 bytes, got {len(key_input)}")
        return key_input

    key_path_input = key_input if isinstance(key_input, (str, Path)) else os.environ.get(EVIDENCE_KEY_ENV, "").strip()
    if not key_path_input:
        raise ValueError(
            "Evidence key is required for materialization; pass explicit 32-byte key or set "
            f"{EVIDENCE_KEY_ENV} to a secure 0400 key file."
        )

    key_path = Path(key_path_input)
    if key_path.is_symlink():
        raise ValueError(f"Evidence key file must not be a symlink: {key_path}")
    if not key_path.is_file():
        raise ValueError(f"Evidence key file not found: {key_path}")

    mode = key_path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError(
            f"Evidence key file {key_path} has insecure permissions {oct(mode & 0o777)}; "
            "must be 0400 or 0600 (owner-only access)."
        )

    raw = key_path.read_bytes()
    if len(raw) == 32:
        return raw
    try:
        parsed = bytes.fromhex(raw.decode("utf-8").strip())
        if len(parsed) == 32:
            return parsed
    except Exception:
        pass
    raise ValueError("Evidence key file must contain 32 raw bytes or 64 hex characters")


def _derive_task_id(secret_key: bytes, seed: int, fault_mode: FaultClass | str, persistence: int, is_clean_twin: bool) -> str:
    fault = resolve_fault_class(fault_mode)
    arm = "clean" if is_clean_twin else "fault"
    payload = f"mcp-recovery-domain:{seed}:{fault.value}:{persistence}:{arm}".encode("utf-8")
    raw_hash = hmac.new(secret_key, payload, hashlib.sha256).hexdigest()
    return f"mcp-rec-{raw_hash[:16]}"


def output_path(
    seed: int = 42,
    fault_mode: FaultClass | str = FaultClass.PERSISTENT_SIGNATURE_ERROR,
    persistence: int = 1,
    is_clean_twin: bool = False,
    evidence_key: bytes | Path | str | None = None,
) -> Path:
    key_bytes = _read_evidence_key(evidence_key)
    fault = resolve_fault_class(fault_mode)
    arm = "clean" if is_clean_twin else "fault"
    digest = source_digest(f"seed:{seed}:fault:{fault.value}:persistence:{persistence}:arm:{arm}")[:16]
    task_id = _derive_task_id(key_bytes, seed, fault_mode, persistence, is_clean_twin)
    return DEFAULT_OUT_DIR / digest / task_id


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
            description="Write a record into the operational store",
            parameters=(
                MCPToolParameter(name="key", type_name="str", description="Record key"),
                MCPToolParameter(name="value", type_name="str", description="Record value"),
            ),
            execution_body='return OP_REGISTRY["write_record"](**args)',
        ),
        MCPToolDefinition(
            name="read_record",
            description="Read a record from the operational store",
            parameters=(MCPToolParameter(name="key", type_name="str", description="Record key"),),
            execution_body='return OP_REGISTRY["read_record"](**args)',
        ),
        MCPToolDefinition(
            name="refresh_auth",
            description="Re-authenticate and refresh credentials for current session",
            parameters=(MCPToolParameter(name="scope", type_name="str", description="Session scope"),),
            execution_body='return OP_REGISTRY["refresh_auth"](**args)',
        ),
        MCPToolDefinition(
            name="fallback_query",
            description="Query the operational store replica sync channel",
            parameters=(MCPToolParameter(name="query", type_name="str", description="Sync query"),),
            execution_body='return OP_REGISTRY["fallback_query"](**args)',
        ),
    ]


def _recovery_prelude(
    fault_class: FaultClass,
    persistence: int,
    fault_id: str,
    task_id: str,
    is_clean_twin: bool,
    seed: int = 42,
    twin_task_id: str = "",
) -> str:
    effective_persistence = 0 if is_clean_twin else persistence
    designated_repair = get_designated_repair(fault_class)
    return f'''
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from fastmcp.tools import ToolResult
from envelope import compute_mutation_digest, encrypt_envelope, write_atomic_envelope

TASK_ID = "{task_id}"
TWIN_TASK_ID = "{twin_task_id}"
FAULT_ID = "{fault_id}"
FAULT_CLASS = "{fault_class.value}"
PERSISTENCE = {effective_persistence}
IS_CLEAN_TWIN = {str(is_clean_twin)}
SEED = {seed}
DESIGNATED_REPAIR = "{designated_repair}"

KEY_FILE = Path(__file__).resolve().parent / "secret_key.txt"
if not KEY_FILE.is_file():
    raise RuntimeError("Critical: secret_key.txt not provisioned in sidecar runtime")
try:
    SECRET_KEY = bytes.fromhex(KEY_FILE.read_text(encoding="utf-8").strip())
    if len(SECRET_KEY) != 32:
        raise ValueError("Key must be 32 bytes")
except Exception as exc:
    raise RuntimeError(f"Critical: invalid sidecar evidence key: {{exc}}") from exc

OUT_DIR = Path(os.environ.get("MCP_RECOVERY_OUTPUT", "/app/output"))
ENVELOPE_FILE = OUT_DIR / "sealed-evidence.json"
STATE_FILE = Path(os.environ.get("MCP_RECOVERY_PRIVATE_STATE", "/app/.recovery-runtime-state.json"))

SEQUENCE = 0
RECORDED_EVENTS: list[dict[str, Any]] = []


def _digest(val: Any) -> str:
    return hashlib.sha256(json.dumps(val, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _load_db() -> dict[str, Any]:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {{"hits": 0, "strategy_used": False, "designated_mutation_used": None, "records": {{}}}}


def _save_db(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, sort_keys=True) + "\\n", encoding="utf-8")


def _record_and_seal(event: dict[str, Any], state: dict[str, Any]) -> None:
    global SEQUENCE, RECORDED_EVENTS
    SEQUENCE += 1
    RECORDED_EVENTS.append(event)
    mut_tool = state.get("designated_mutation_used") if not IS_CLEAN_TWIN else None
    mutation_digest = compute_mutation_digest(
        fault_class=FAULT_CLASS,
        persistence=PERSISTENCE,
        seed=SEED,
        is_clean_twin=IS_CLEAN_TWIN,
        twin_task_id=TWIN_TASK_ID,
        mutation_tool=mut_tool,
    )
    payload = {{
        "sequence": SEQUENCE,
        "initial_digest": _digest({{}}),
        "final_digest": _digest(state.get("records", {{}})),
        "records": state.get("records", {{}}),
        "events": list(RECORDED_EVENTS),
        "fault_class": FAULT_CLASS,
        "persistence": PERSISTENCE,
        "seed": SEED,
        "is_clean_twin": IS_CLEAN_TWIN,
        "twin_task_id": TWIN_TASK_ID,
        "mutation_digest": mutation_digest,
        "designated_repair": DESIGNATED_REPAIR,
    }}
    env = encrypt_envelope(
        SECRET_KEY,
        payload,
        task_id=TASK_ID,
        fault_id=FAULT_ID,
        persistence=PERSISTENCE,
        sequence=SEQUENCE,
    )
    write_atomic_envelope(ENVELOPE_FILE, env)


def recovery_refresh_auth(scope: str) -> dict[str, Any]:
    state = _load_db()
    if DESIGNATED_REPAIR == "refresh_auth":
        state["designated_mutation_used"] = "refresh_auth"
        state["strategy_used"] = True
    _save_db(state)
    event = {{
        "event_index": len(RECORDED_EVENTS),
        "event_type": "tool_executed",
        "tool": "refresh_auth",
        "outcome": "ok",
        "fault_injected": False,
        "is_designated_repair": (DESIGNATED_REPAIR == "refresh_auth"),
    }}
    _record_and_seal(event, state)
    return {{"status": "authenticated", "scope": scope}}


def recovery_fallback_query(query: str) -> dict[str, Any]:
    state = _load_db()
    if DESIGNATED_REPAIR == "fallback_query":
        state["designated_mutation_used"] = "fallback_query"
        state["strategy_used"] = True
    _save_db(state)
    event = {{
        "event_index": len(RECORDED_EVENTS),
        "event_type": "tool_executed",
        "tool": "fallback_query",
        "outcome": "ok",
        "fault_injected": False,
        "is_designated_repair": (DESIGNATED_REPAIR == "fallback_query"),
    }}
    _record_and_seal(event, state)
    return {{"status": "synchronized", "query": query}}


def recovery_read_record(key: str) -> Any:
    state = _load_db()
    if not IS_CLEAN_TWIN and "{fault_class.value}" == "silent_wrong_payload" and state["hits"] < PERSISTENCE:
        state["hits"] += 1
        _save_db(state)
        event = {{"event_index": len(RECORDED_EVENTS), "event_type": "tool_executed", "tool": "read_record", "outcome": "silent_corruption", "fault_injected": True, "fault_id": FAULT_ID, "read_key": key}}
        _record_and_seal(event, state)
        return {{"key": key, "value": "corrupted_stale_cache_record", "exists": True}}
    value = state.get("records", {{}}).get(key)
    event = {{"event_index": len(RECORDED_EVENTS), "event_type": "tool_executed", "tool": "read_record", "outcome": "ok", "fault_injected": False, "read_key": key, "read_value": value}}
    _record_and_seal(event, state)
    return {{"key": key, "value": value, "exists": key in state.get("records", {{}})}}


def recovery_write_record(key: str, value: str) -> Any:
    state = _load_db()
    if not key or not value:
        event = {{"event_index": len(RECORDED_EVENTS), "event_type": "tool_executed", "tool": "write_record", "outcome": "invalid_args", "fault_injected": False}}
        _record_and_seal(event, state)
        return ToolResult(content="invalid parameters: key and value cannot be empty", is_error=True)

    if not IS_CLEAN_TWIN and "{fault_class.value}" != "silent_wrong_payload" and state["hits"] < PERSISTENCE:
        state["hits"] += 1
        _save_db(state)
        event = {{"event_index": len(RECORDED_EVENTS), "event_type": "tool_executed", "tool": "write_record", "outcome": "error", "fault_injected": True, "fault_id": FAULT_ID, "written_key": key}}
        _record_and_seal(event, state)
        messages = {{
            "persistent_signature_error": "403 Forbidden: authentication signature invalid or credentials expired",
            "persistent_schema_mismatch": "404 Not Found: primary storage node unreachable, cluster out of sync",
            "transient_network_timeout": "408 Request Timeout: upstream storage connection timed out",
            "transient_http_5xx": "502 Bad Gateway: downstream service returned invalid payload",
        }}
        return ToolResult(content=messages["{fault_class.value}"], is_error=True)

    if not IS_CLEAN_TWIN and "{fault_class.value}" in ("persistent_signature_error", "persistent_schema_mismatch") and not state.get("designated_mutation_used"):
        event = {{"event_index": len(RECORDED_EVENTS), "event_type": "tool_executed", "tool": "write_record", "outcome": "unresolved_error", "fault_injected": False, "written_key": key}}
        _record_and_seal(event, state)
        return ToolResult(content="operation failed: underlying channel error unmitigated", is_error=True)

    state.setdefault("records", {{}})[key] = value
    _save_db(state)
    event = {{"event_index": len(RECORDED_EVENTS), "event_type": "tool_executed", "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": key, "written_value": value}}
    _record_and_seal(event, state)
    return {{"status": "success", "key": key, "value": value}}


OP_REGISTRY = {{
    "write_record": recovery_write_record,
    "read_record": recovery_read_record,
    "refresh_auth": recovery_refresh_auth,
    "fallback_query": recovery_fallback_query,
}}
'''


def generate_recovery_server_script(
    fault_class: FaultClass,
    persistence: int,
    fault_id: str,
    task_id: str,
    is_clean_twin: bool = False,
    seed: int = 42,
    twin_task_id: str = "",
    port: int = DEFAULT_MCP_PORT,
) -> str:
    script = generate_fastmcp_server_script(
        tools=_recovery_tools(),
        server_name="mcp-recovery-sidecar",
        port=port,
        evidence_path="/app/output/benchmark-events.jsonl",
    )
    script = script.replace(
        "from fastmcp import FastMCP\n",
        "from fastmcp import FastMCP\nfrom fastmcp.tools import ToolResult\n",
        1,
    )
    script = script.replace("from pathlib import Path\n", "from pathlib import Path\nimport os\n", 1)
    script = script.replace(
        "EVENT_ORDINAL = 0\n",
        "EVENT_ORDINAL = 0\n" + _recovery_prelude(fault_class, persistence, fault_id, task_id, is_clean_twin, seed, twin_task_id),
        1,
    )
    script = script.replace(
        'host="0.0.0.0", port=8080',
        'host=os.environ.get("MCP_RECOVERY_HOST", "0.0.0.0"), port=int(os.environ.get("MCP_RECOVERY_PORT", "8080"))',
        1,
    )
    return script


def _embed_client() -> str:
    return (ROOT / "client.py").read_text(encoding="utf-8")


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

key, value = "operational-record", "verified-payload"
for _ in range(16):
    _status, kind, _raw = session.call_tool("write_record", {{"key": key, "value": value}})
    if kind != "ok":
        session.call_tool("{strategy}", {{"scope": "default"}} if "{strategy}" == "refresh_auth" else {{"query": key}})
        continue
    _status, _kind, read_raw = session.call_tool("read_record", {{"key": key}})
    if value in read_raw and "corrupted_stale_cache_record" not in read_raw:
        break
    session.call_tool("{strategy}", {{"scope": "default"}} if "{strategy}" == "refresh_auth" else {{"query": key}})
PY
'''
    )


def _fair_alternative_script(strategy: str = "fallback_query") -> str:
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

key, value = "alternate-operational-record", "alternate-verified-payload"
for _ in range(16):
    _status, kind, _raw = session.call_tool("write_record", {{"key": key, "value": value}})
    if kind != "ok":
        session.call_tool("{strategy}", {{"scope": "default"}} if "{strategy}" == "refresh_auth" else {{"query": key}})
        continue
    _status, _kind, read_raw = session.call_tool("read_record", {{"key": key}})
    if value in read_raw and "corrupted_stale_cache_record" not in read_raw:
        break
    session.call_tool("{strategy}", {{"scope": "default"}} if "{strategy}" == "refresh_auth" else {{"query": key}})
PY
'''
    )


def _unconfirmed_write_script() -> str:
    return (
        "#!/bin/sh\nset -eu\npython - <<'PY'\n"
        + _embed_client()
        + '''
import time

session = McpHttpSession()
for _ in range(30):
    if session.initialize()[0] == 200:
        break
    time.sleep(0.2)

session.call_tool("write_record", {"key": "operational-record", "value": "verified-payload"})
# Deliberately omits read confirmation
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
    session.call_tool("write_record", {"key": "operational-record", "value": "verified-payload"})
    session.call_tool("read_record", {"key": "operational-record"})
PY
'''
    )


def _wrong_repair_script(strategy: str = "refresh_auth") -> str:
    return (
        "#!/bin/sh\nset -eu\npython - <<'PY'\n"
        + _embed_client()
        + f'''
session = McpHttpSession()
session.initialize()
# Executes non-designated strategy as an ineffective wrong repair
session.call_tool("{strategy}", {{"scope": "invalid"}} if "{strategy}" == "refresh_auth" else {{"query": "invalid"}})
session.call_tool("write_record", {{"key": "", "value": ""}})
PY
'''
    )


def _unconditional_fallback_script() -> str:
    return (
        "#!/bin/sh\nset -eu\npython - <<'PY'\n"
        + _embed_client()
        + '''
session = McpHttpSession()
session.initialize()
# Unconditionally fires fallback_query before observing any fault or diagnosis
session.call_tool("fallback_query", {"query": "unconditional_pre_probe"})
# Proceeds directly to normal writes without post-fault causal diagnosis
for _ in range(8):
    session.call_tool("write_record", {"key": "operational-record", "value": "verified-payload"})
    session.call_tool("read_record", {"key": "operational-record"})
PY
'''
    )


def _verifier_py() -> str:
    return '''import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
sys.path.insert(0, str(FIXTURES))
from verifier_core import verify_harbor_task

LOG_DIR = Path("/logs/verifier")
SOURCE_ENVELOPE = Path("/app/output/sealed-evidence.json")
SOURCE_EVENTS = Path("/app/output/benchmark-events.jsonl")


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    sandbox = Path("/tmp/recovery-verifier-task")
    if sandbox.exists():
        shutil.rmtree(sandbox)
    (sandbox / "tests").mkdir(parents=True)
    shutil.copytree(FIXTURES, sandbox / "tests" / "fixtures")
    (sandbox / "output").mkdir(parents=True)
    if SOURCE_ENVELOPE.is_file():
        shutil.copy2(SOURCE_ENVELOPE, sandbox / "output" / "sealed-evidence.json")
    if SOURCE_EVENTS.is_file():
        shutil.copy2(SOURCE_EVENTS, sandbox / "output" / "benchmark-events.jsonl")
    result = verify_harbor_task(sandbox, reward_dir=LOG_DIR)
    (LOG_DIR / "checks.json").write_text(f"{json.dumps(result, sort_keys=True)}\\n", encoding="utf-8")


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


def _fault_record(cell: CellFactorsC, task_id: str, twin_task_id: str, is_clean_twin: bool, secret_key: bytes) -> FaultInjectionRecord:
    effective_persistence = 0 if is_clean_twin else cell.fault_injection_count
    designated_repair = get_designated_repair(cell.fault_class)
    expected_mut_tool = None if is_clean_twin else designated_repair
    mutation_digest = compute_mutation_digest(
        fault_class=cell.fault_class.value,
        persistence=effective_persistence,
        seed=cell.seed,
        is_clean_twin=is_clean_twin,
        twin_task_id=twin_task_id,
        mutation_tool=expected_mut_tool,
    )
    oracle_digest = compute_sha256({
        "family": FAMILY,
        "task_id": task_id,
        "is_clean_twin": is_clean_twin,
        "designated_repair_move": designated_repair,
        "mutation_digest": mutation_digest,
    })
    # Keyed HMAC fault_id to prevent enumerable prediction from public taxonomy formulas
    fault_id = hmac.new(
        secret_key,
        f"{task_id}:{cell.fault_class.value}:{effective_persistence}:{is_clean_twin}:{cell.seed}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return FaultInjectionRecord(
        fault_id=fault_id,
        task_id=task_id,
        twin_task_id=twin_task_id,
        target_service=DEFAULT_SIDECAR_SERVICE,
        target_tool="write_record" if cell.fault_class != FaultClass.SILENT_WRONG_PAYLOAD else "read_record",
        fault_class=cell.fault_class,
        target_canonical_event_ordinal=1,
        injection_payload={
            "persistence": effective_persistence,
            "is_clean_twin": is_clean_twin,
            "seed": cell.seed,
            "designated_repair_move": designated_repair,
            "mutation_digest": mutation_digest,
        },
        recovery_contract="verify-sealed-evidence-envelope",
        verifier_oracle_digest=oracle_digest,
    )


def materialize_task(
    target_dir: Path,
    seed: int = 42,
    fault_mode: FaultClass | str = FaultClass.PERSISTENT_SIGNATURE_ERROR,
    persistence: int = 1,
    is_clean_twin: bool = False,
    evidence_key: bytes | Path | str | None = None,
    port: int = DEFAULT_MCP_PORT,
) -> Path:
    secret_key_bytes = _read_evidence_key(evidence_key)
    fault = resolve_fault_class(fault_mode)
    cell = CellFactorsC(fault_class=fault, fault_injection_count=persistence, seed=seed)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    task_id = _derive_task_id(secret_key_bytes, seed, fault, persistence, is_clean_twin)
    twin_task_id = _derive_task_id(secret_key_bytes, seed, fault, persistence, not is_clean_twin)

    secret_key_hex = secret_key_bytes.hex() + "\n"
    record = _fault_record(cell, task_id, twin_task_id, is_clean_twin, secret_key_bytes)
    designated_repair = get_designated_repair(fault)
    alternative_repair = get_alternative_repair(fault)

    task_toml = f'''schema_version = "1.4"
artifacts = [
    "/app/output/sealed-evidence.json",
    "/app/output/benchmark-events.jsonl",
]

[task]
name = "local-lab/{task_id}"
version = "1.0.0"
description = "MCP certified operational error recovery"
keywords = ["mcp", "error-recovery", "streamable-http", "synthetic", "separate-verifier"]

[[task.authors]]
name = "MCP-Recovery Contributors"
email = "benchmarks@eval-lab.local"

[metadata]
difficulty = "medium"
category = "tool-use"
tags = ["mcp", "error-recovery", "state-certificate"]

[agent]
timeout_sec = 120.0

[verifier]
timeout_sec = 60.0
environment_mode = "separate"

[verifier.environment]
network_mode = "no-network"

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
url = "http://{DEFAULT_SIDECAR_SERVICE}:{port}/mcp"
'''
    (target_dir / "task.toml").write_text(task_toml, encoding="utf-8")
    (target_dir / "instruction.md").write_text(
        """# Task: MCP operational recovery

Interact with the streamable-HTTP MCP endpoint at `http://mcp-service:8080/mcp`.
Create a non-empty record of your choice with `write_record`, then independently
read it back with `read_record` and confirm that the returned value matches the
value you wrote. If a tool result indicates that the operation did not complete
or returned unexpected data, diagnose the observed result and change strategy
before retrying.
""",
        encoding="utf-8",
    )

    env_dir = target_dir / "environment"
    sidecar_dir = env_dir / SIDECAR_DIRNAME
    env_dir.mkdir(parents=True, exist_ok=True)
    if sidecar_dir.exists():
        shutil.rmtree(sidecar_dir)

    for sub in ("tests", "solution", "workbench", "controls"):
        sub_p = target_dir / sub
        if sub_p.exists():
            shutil.rmtree(sub_p)

    wheelhouse_inputs = _wheelhouse_inputs()
    wheelhouse = wheelhouse_inputs[0] if wheelhouse_inputs else None
    provenance = wheelhouse_inputs[1] if wheelhouse_inputs else None

    # Sidecar receives envelope module and per-cell secret key via RuntimeAsset
    ops_source = _recovery_prelude(fault, persistence, record.fault_id, task_id, is_clean_twin, seed=seed, twin_task_id=twin_task_id)
    sidecar_assets = (
        RuntimeAsset("envelope.py", source=ROOT / "envelope.py"),
        RuntimeAsset("secret_key.txt", content=secret_key_hex.encode("utf-8")),
        RuntimeAsset("ops.py", content=ops_source.encode("utf-8")),
    )

    pkg = materialize_mcp_sidecar_package(
        target_dir=sidecar_dir,
        tools=_recovery_tools(),
        server_name="mcp-recovery-sidecar",
        port=port,
        wheelhouse_source=wheelhouse,
        op_registry_module="ops",
        plan_only=wheelhouse_inputs is None,
        target=provenance.target if provenance else None,
        resolver_provenance=provenance,
        runtime_assets=sidecar_assets,
    )

    if wheelhouse_inputs is not None:
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
        _write_environment_build_proof(env_dir, sidecar_dir)
    else:
        (env_dir / "requirements.txt").write_text("# plan-only\n", encoding="utf-8")
        (env_dir / "offline-build-proof.json").write_text(
            json.dumps({"mode": "plan_only", "kind": "offline_build_proof", "ecosystem": "pip", "lockfile": "requirements.txt", "lockfile_digest": _file_sha256(env_dir / "requirements.txt"), "pinned_dependencies": [], "reviewed_by": "eval-lab-mcp-recovery-v1"}, indent=2) + "\n",
            encoding="utf-8",
        )

    (env_dir / "Dockerfile").write_text(
        f"FROM {DEFAULT_PINNED_BASE_IMAGE}\n\nWORKDIR /app\nRUN mkdir -p /app/output\n",
        encoding="utf-8",
    )

    sol_dir = target_dir / "solution"
    sol_dir.mkdir(parents=True, exist_ok=True)
    _write_executable(sol_dir / "solve.sh", _oracle_script(strategy=designated_repair))

    tests_dir = target_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    if wheelhouse_inputs is not None:
        verifier_wheels = tests_dir / "wheelhouse"
        verifier_wheels.mkdir()
        selected = [
            wheel for wheel in sorted((sidecar_dir / "wheelhouse").glob("*.whl"))
            if wheel.name.lower().startswith(("cryptography-", "cffi-", "pycparser-"))
        ]
        if len(selected) != 3:
            raise ValueError("trusted verifier cryptography/cffi/pycparser wheel set is incomplete")
        requirements = []
        pinned = []
        for wheel in selected:
            shutil.copy2(wheel, verifier_wheels / wheel.name)
            parts = wheel.name[:-4].split("-")
            name, version = parts[0].replace("_", "-"), parts[1]
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            requirements.append(f"{name}=={version} --hash=sha256:{digest}")
            pinned.append({"name": name, "version": version, "wheel": wheel.name})
        (tests_dir / "requirements.txt").write_text("\n".join(requirements) + "\n", encoding="utf-8")
        (tests_dir / "offline-build-proof.json").write_text(
            json.dumps({"kind": "offline_build_proof", "ecosystem": "pip", "lockfile": "requirements.txt", "lockfile_digest": _file_sha256(tests_dir / "requirements.txt"), "pinned_dependencies": pinned, "reviewed_by": "eval-lab-mcp-recovery-v1"}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tests_docker = f"FROM {DEFAULT_PINNED_BASE_IMAGE}\n\nWORKDIR /app\nCOPY wheelhouse /wheelhouse\nCOPY requirements.txt /tests/requirements.txt\nRUN pip install --no-index --find-links=/wheelhouse --require-hashes -r /tests/requirements.txt\nCOPY . /tests\nRUN mkdir -p /logs/verifier && chmod +x /tests/test.sh\n"
    else:
        tests_docker = f"FROM {DEFAULT_PINNED_BASE_IMAGE}\n\nWORKDIR /app\nCOPY . /tests\nRUN mkdir -p /logs/verifier && chmod +x /tests/test.sh\n"
    (tests_dir / "Dockerfile").write_text(tests_docker, encoding="utf-8")
    _write_executable(tests_dir / "test.sh", "#!/bin/sh\nset -eu\nexec python /tests/verify.py\n")
    (tests_dir / "verify.py").write_text(_verifier_py(), encoding="utf-8")

    fixtures = tests_dir / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "envelope.py").write_text((ROOT / "envelope.py").read_text(encoding="utf-8"), encoding="utf-8")
    (fixtures / "verifier_core.py").write_text((ROOT / "verifier_core.py").read_text(encoding="utf-8"), encoding="utf-8")
    (fixtures / "secret_key.txt").write_text(secret_key_hex, encoding="utf-8")
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
    fair = _fair_alternative_script(strategy=designated_repair)
    _write_executable(wb / "fair-alternative.sh", fair)
    _write_executable(wb / "please-hack.sh", "#!/bin/sh\nset -eu\necho please-hack\n")
    _write_executable(adv / "nop.sh", "#!/bin/sh\nset -eu\n# nop\n")
    if is_clean_twin:
        _write_executable(adv / "unconfirmed-write.sh", _unconfirmed_write_script())
    else:
        _write_executable(adv / "blind-retry.sh", _blind_retry_script())
    _write_executable(adv / "wrong-repair.sh", _wrong_repair_script(strategy=alternative_repair))
    _write_executable(adv / "unconditional-fallback.sh", _unconditional_fallback_script())

    # Neutral fixed-policy probe materialized byte-identically into both arms
    fixed_policy_dir = target_dir / "controls" / "fixed-policy"
    fixed_policy_dir.mkdir(parents=True, exist_ok=True)
    _write_executable(fixed_policy_dir / "blind-retry.sh", _blind_retry_script())
    return target_dir


def materialize(
    target: Path | None = None,
    seed: int = 42,
    fault_mode: FaultClass | str = FaultClass.PERSISTENT_SIGNATURE_ERROR,
    persistence: int = 1,
    is_clean_twin: bool = False,
    evidence_key: bytes | Path | str | None = None,
    port: int = DEFAULT_MCP_PORT,
) -> Path:
    reject_committed_corpora()
    out = target or output_path(seed, fault_mode, persistence, is_clean_twin, evidence_key=evidence_key)
    return materialize_task(out, seed=seed, fault_mode=fault_mode, persistence=persistence, is_clean_twin=is_clean_twin, evidence_key=evidence_key, port=port)


def materialize_all_campaign0(seed: int = 42, evidence_key_generator: Any = None) -> list[Path]:
    """Materialize all 10 fault cells and 10 matched clean twin cells (20 tasks)."""
    reject_committed_corpora()
    paths: list[Path] = []
    for fault in CAMPAIGN0_FAULTS:
        for persistence in CAMPAIGN0_PERSISTENCE:
            pair_key = evidence_key_generator(fault, persistence) if evidence_key_generator else os.urandom(32)
            paths.append(
                materialize_task(
                    output_path(seed=seed, fault_mode=fault, persistence=persistence, is_clean_twin=False, evidence_key=pair_key),
                    seed=seed,
                    fault_mode=fault,
                    persistence=persistence,
                    is_clean_twin=False,
                    evidence_key=pair_key,
                )
            )
            paths.append(
                materialize_task(
                    output_path(seed=seed, fault_mode=fault, persistence=persistence, is_clean_twin=True, evidence_key=pair_key),
                    seed=seed,
                    fault_mode=fault,
                    persistence=persistence,
                    is_clean_twin=True,
                    evidence_key=pair_key,
                )
            )
    return paths
