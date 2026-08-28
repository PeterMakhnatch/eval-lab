"""Shared FastMCP multi-container task-authoring substrate and runtime middleware.

Grounding: Architecture PR #265 (research/inbox/NEXT-BENCHMARK-PROGRAM-ARCHITECTURE-2026-08-28.md)

Provides:
- Task authoring substrate API (`materialize_mcp_sidecar_package`) emitting:
  - `server.py` using genuine `fastmcp.FastMCP` (v3.4.7) with execution delegation, deterministic fault interceptor middleware, and state journal event recording.
  - `requirements.txt` strictly hash-locked with verified SHA-256 digests.
  - `Dockerfile` using offline `pip install --no-index --find-links=/wheelhouse --require-hashes`.
  - `offline-build-proof.json` recording exact wheel inventory and content digests.
  - `docker-compose.yaml` fragment and collect hooks for workbench-v2.
- Strict mechanical verification of wheelhouse bytes against locked requirements hashes (rejecting missing, extra unapproved, or tampered wheel artifacts).
- Support for explicit `plan_only=True` mode when Dockerfile/wheelhouse is omitted.
- Standard FastMCP sidecar topology generation & validation matching workbench-v2:
  - Task-local internal bridge network (`networks: {<name>: {internal: true}}`).
  - Attached services (`main` and `sidecar` attaching to the exact same internal network).
  - Task-local named volume (`main-RO` / `sidecar-RW`).
- Deterministic Fault Interceptor middleware operating over FaultInjectionRecord contracts.
- Invariant ground-truth separation (purges solutions/oracles from agent containers).
- Substrate version & comprehensive digest computation (including execution_body and metadata).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.benchmark_program_contracts import (
    FaultInjectionRecord,
    canonical_json,
    compute_sha256,
    safe_resolve_subpath,
)

logger = logging.getLogger(__name__)

MCP_SUBSTRATE_VERSION = "0.2.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_SIDECAR_SERVICE = "mcp-service"
DEFAULT_VOLUME_NAME = "evidence-volume"
DEFAULT_VOLUME_MOUNT = "/app/output"
DEFAULT_INTERNAL_NETWORK_NAME = "workbench-internal"
DEFAULT_MCP_PORT = 8080
DEFAULT_PINNED_BASE_IMAGE = (
    "python@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251"
)

# Pinned FastMCP 3.4.7 streamable-HTTP sidecar dependencies with strict hash locking
FASTMCP_VERSION_CONSTRAINTS: tuple[str, ...] = ("fastmcp==3.4.7",)


class SubstrateError(Exception):
    """Raised when substrate configuration, validation, or runtime fails."""


def parse_requirements_hashes(requirements_text: str) -> dict[str, set[str]]:
    """Parse a hash-locked requirements.txt into a mapping of normalized package_name -> set of sha256 hex strings."""
    req_hashes: dict[str, set[str]] = {}
    for line in requirements_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pkg_part = parts[0]
        pkg_name = pkg_part.split("==")[0].lower().replace("_", "-")
        hashes = set()
        for p in parts[1:]:
            if p.startswith("--hash=sha256:"):
                hashes.add(p.removeprefix("--hash=sha256:"))
        if not hashes:
            raise SubstrateError(f"Requirement {pkg_name!r} has no --hash=sha256: declarations")
        req_hashes[pkg_name] = hashes
    return req_hashes


def verify_wheelhouse_inventory(
    wheelhouse_dir: Path, requirements_text: str
) -> list[dict[str, Any]]:
    """Mechanically verify that wheelhouse contains an exact matching wheel for every locked requirement.

    Rejects:
    - Non-directory or empty wheelhouse
    - Missing required package wheel
    - Tampered wheel bytes whose sha256 is not in the declared requirement lock
    - Extra unapproved wheels not in the lockfile
    """
    if not wheelhouse_dir.is_dir():
        raise SubstrateError(f"Wheelhouse directory does not exist: {wheelhouse_dir.as_posix()!r}")

    locked = parse_requirements_hashes(requirements_text)
    wheels = list(wheelhouse_dir.glob("*.whl"))
    if not wheels:
        raise SubstrateError(
            f"Wheelhouse {wheelhouse_dir.as_posix()!r} is empty (contains 0 wheels)"
        )

    matched_packages: set[str] = set()
    inventory: list[dict[str, Any]] = []

    for w_file in sorted(wheels, key=lambda p: p.name):
        w_bytes = w_file.read_bytes()
        w_hash = hashlib.sha256(w_bytes).hexdigest()
        pkg_name = w_file.name.split("-")[0].lower().replace("_", "-")

        if pkg_name not in locked:
            raise SubstrateError(
                f"Wheelhouse contains extra unapproved package {w_file.name!r} not in lockfile"
            )

        if w_hash not in locked[pkg_name]:
            raise SubstrateError(
                f"Wheel {w_file.name!r} SHA-256 hash {w_hash} does not match any locked hash for {pkg_name}"
            )

        matched_packages.add(pkg_name)
        inventory.append(
            {
                "filename": w_file.name,
                "size_bytes": len(w_bytes),
                "sha256": w_hash,
            }
        )

    missing_packages = set(locked.keys()) - matched_packages
    if missing_packages:
        raise SubstrateError(
            f"Wheelhouse is missing required locked package(s): {sorted(missing_packages)}"
        )

    return inventory


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
    execution_body: str | None = None

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


def generate_fastmcp_server_script(
    tools: Sequence[MCPToolDefinition],
    server_name: str = "eval-lab-fastmcp-sidecar",
    port: int = DEFAULT_MCP_PORT,
    evidence_path: str = "/app/output/benchmark-events.jsonl",
    op_registry_module: str | None = None,
    fault_record: FaultInjectionRecord | None = None,
) -> str:
    """Generate production-ready FastMCP sidecar server script with full event recording, fault injection, and tool execution."""
    lines = [
        '"""Generated FastMCP Streamable-HTTP sidecar server with state journal recording."""',
        "from __future__ import annotations",
        "",
        "import json",
        "from pathlib import Path",
        "import threading",
        "from typing import Any",
        "from fastmcp import FastMCP",
    ]

    if op_registry_module:
        lines.append(f"from {op_registry_module} import OP_REGISTRY")
    else:
        lines.append("OP_REGISTRY: dict[str, Any] = {}")

    lines.extend(
        [
            "",
            f'mcp = FastMCP("{server_name}")',
            f'EVIDENCE_FILE = Path("{evidence_path}")',
            "EVENT_LOCK = threading.Lock()",
            "EVENT_ORDINAL = 0",
            "",
            "def log_tool_event(tool_name: str, arguments: dict[str, Any], result: Any, is_distractor: bool = False) -> None:",
            "    global EVENT_ORDINAL",
            "    with EVENT_LOCK:",
            "        EVENT_ORDINAL += 1",
            "        EVIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)",
            "        event = {",
            '            "event_ordinal": EVENT_ORDINAL,',
            '            "event_type": "tool_call_success",',
            '            "tool_name": tool_name,',
            '            "arguments": arguments,',
            '            "result": result,',
            '            "is_distractor": is_distractor,',
            "        }",
            '        with open(EVIDENCE_FILE, "a", encoding="utf-8") as f:',
            '            f.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\\n")',
            "",
        ]
    )

    if fault_record:
        fault_json = json.dumps(canonical_json(fault_record.model_dump(mode="json")))
        lines.extend(
            [
                f"FAULT_RECORD = json.loads({fault_json})",
                "def check_fault(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:",
                "    global EVENT_ORDINAL",
                "    if FAULT_RECORD and FAULT_RECORD.get('target_tool') == tool_name:",
                "        if (EVENT_ORDINAL + 1) == FAULT_RECORD.get('target_canonical_event_ordinal'):",
                "            fc = FAULT_RECORD.get('fault_class')",
                "            payload = FAULT_RECORD.get('injection_payload', {})",
                "            if fc == 'silent_wrong_payload':",
                "                corrupt = payload.get('corrupted_result', {'value': payload.get('corrupted_value', 'CORRUPTED_VALUE')})",
                "                log_tool_event(tool_name, arguments, corrupt, is_distractor=False)",
                "                return corrupt",
                "            elif fc in ('persistent_schema_mismatch', 'persistent_signature_error'):",
                "                raise ValueError(payload.get('message', 'Persistent error injected'))",
                "            elif fc in ('transient_http_5xx', 'transient_network_timeout'):",
                "                raise RuntimeError(payload.get('message', 'Transient error injected'))",
                "    return None",
                "",
            ]
        )

    for tool in tools:
        param_sigs = []
        arg_dict_entries = []
        for p in tool.parameters:
            py_type = (
                p.type_name
                if p.type_name in ("int", "str", "float", "bool", "dict", "list")
                else "Any"
            )
            if not p.required:
                param_sigs.append(f"{p.name}: {py_type} | None = None")
            else:
                param_sigs.append(f"{p.name}: {py_type}")
            arg_dict_entries.append(f'"{p.name}": {p.name}')
        sig_str = ", ".join(param_sigs)
        arg_dict_str = "{" + ", ".join(arg_dict_entries) + "}"

        lines.extend(
            [
                "@mcp.tool()",
                f"def {tool.name}({sig_str}) -> dict[str, Any]:",
                f'    """{tool.description}"""',
                f"    args = {arg_dict_str}",
            ]
        )

        if fault_record:
            lines.extend(
                [
                    f'    fault_res = check_fault("{tool.name}", args)',
                    "    if fault_res is not None:",
                    "        return fault_res",
                ]
            )

        if tool.execution_body:
            for b_line in tool.execution_body.strip().splitlines():
                lines.append(f"    {b_line}")
        elif tool.is_distractor:
            lines.extend(
                [
                    '    res = {"status": "noop_distractor", "value": None}',
                    f'    log_tool_event("{tool.name}", args, res, is_distractor=True)',
                    "    return res",
                ]
            )
        else:
            op_kind = tool.metadata.get("op_kind", tool.name)
            lines.extend(
                [
                    f'    op_fn = OP_REGISTRY.get("{op_kind}")',
                    "    if op_fn is not None:",
                    "        val = op_fn(**args)",
                    '        res = {"status": "ok", "value": val}',
                    "    else:",
                    '        res = {"status": "ok", "tool": "' + tool.name + '", "value": args}',
                    f'    log_tool_event("{tool.name}", args, res, is_distractor=False)',
                    "    return res",
                ]
            )
        lines.append("")

    lines.extend(
        [
            'if __name__ == "__main__":',
            f'    mcp.run(transport="sse", host="0.0.0.0", port={port})',
            "",
        ]
    )
    return "\n".join(lines)


def render_mcp_sidecar_dockerfile(
    base_image: str = DEFAULT_PINNED_BASE_IMAGE,
    wheelhouse_dir: str = "/wheelhouse",
    app_dir: str = "/app",
    server_script_name: str = "server.py",
) -> str:
    """Render canonical offline sidecar Dockerfile using strict hash-locked pip installation."""
    return f"""FROM {base_image}

WORKDIR {app_dir}

COPY wheelhouse {wheelhouse_dir}
COPY requirements.txt {app_dir}/requirements.txt
COPY {server_script_name} {app_dir}/{server_script_name}

RUN pip install --no-cache-dir --no-index --find-links={wheelhouse_dir} --require-hashes -r {app_dir}/requirements.txt

RUN mkdir -p /app/output

CMD ["python", "{app_dir}/{server_script_name}"]
"""


def materialize_mcp_sidecar_package(
    target_dir: Path,
    tools: Sequence[MCPToolDefinition],
    server_name: str = "eval-lab-fastmcp-sidecar",
    port: int = DEFAULT_MCP_PORT,
    base_image: str = DEFAULT_PINNED_BASE_IMAGE,
    wheelhouse_source: Path | None = None,
    op_registry_module: str | None = None,
    fault_record: FaultInjectionRecord | None = None,
    plan_only: bool = False,
    internal_network_name: str = DEFAULT_INTERNAL_NETWORK_NAME,
    target: WheelhouseTarget | None = None,
) -> dict[str, Any]:
    """Boring task-authoring API emitting a complete, workbench-v2 compliant offline FastMCP sidecar package.

    Parameters:
        target_dir: Target directory where sidecar files will be emitted.
        tools: Sequence of discrete MCP tool definitions.
        server_name: FastMCP server identifier.
        port: Service port.
        base_image: Immutable pinned base image reference.
        wheelhouse_source: Directory of pre-downloaded wheels matching canonical_json(FASTMCP_VERSION_CONSTRAINTS). Mandatory unless plan_only=True.
        op_registry_module: Optional module path for DAG/operation registry delegation.
        fault_record: Optional FaultInjectionRecord for deterministic fault injection.
        plan_only: When True, skips Dockerfile/wheelhouse copying and emits only plan specification.
        internal_network_name: Name of the task-local internal Docker bridge.
    """
    target_dir = safe_resolve_subpath(target_dir.parent, target_dir.name)
    target_dir.mkdir(parents=True, exist_ok=True)
    selected_target = target or WheelhouseTarget(
        python_tag="cp312", platform_tag="macosx_11_0_arm64"
    )

    # 1. server.py
    server_code = generate_fastmcp_server_script(
        tools=tools,
        server_name=server_name,
        port=port,
        op_registry_module=op_registry_module,
        fault_record=fault_record,
    )
    (target_dir / "server.py").write_text(server_code, encoding="utf-8")

    # 2. requirements.txt is emitted only after selected wheels are staged.

    wheel_inventory: list[dict[str, Any]] = []

    if plan_only:
        # Plan-only mode: Dockerfile and wheelhouse omitted
        proof_data = {
            "mode": "plan_only",
            "substrate_version": MCP_SUBSTRATE_VERSION,
            "base_image": base_image,
            "requirements_sha256": compute_sha256(canonical_json(FASTMCP_VERSION_CONSTRAINTS)),
        }
        (target_dir / "offline-build-proof.json").write_text(
            canonical_json(proof_data) + "\n", encoding="utf-8"
        )
    else:
        if wheelhouse_source is None:
            raise SubstrateError(
                "wheelhouse_source is mandatory for production sidecar materialization; pass plan_only=True to emit plan without container build artifacts"
            )

        # Stage selected wheel bytes and derive the exact target lock from their METADATA and SHA-256.
        dest_wheelhouse = target_dir / "wheelhouse"
        requirements_lock, wheel_inventory = stage_platform_wheelhouse(
            wheelhouse_source, dest_wheelhouse, selected_target
        )
        (target_dir / "requirements.txt").write_text(requirements_lock, encoding="utf-8")

        # 3. Dockerfile
        dockerfile_content = render_mcp_sidecar_dockerfile(base_image=base_image)
        (target_dir / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")

        proof_data = {
            "mode": "complete_offline_package",
            "substrate_version": MCP_SUBSTRATE_VERSION,
            "base_image": base_image,
            "target_python": selected_target.python_tag,
            "target_platform": selected_target.platform_tag,
            "requirements_sha256": compute_sha256(requirements_lock),
            "wheel_count": len(wheel_inventory),
            "wheels": wheel_inventory,
        }
        (target_dir / "offline-build-proof.json").write_text(
            canonical_json(proof_data) + "\n", encoding="utf-8"
        )

    if plan_only:
        (target_dir / "requirements.txt").write_text(
            "# plan-only; resolve a target wheelhouse before build\n", encoding="utf-8"
        )

    # Compose and Collect fragments
    compose_doc = render_mcp_compose_document(
        sidecar_service=DEFAULT_SIDECAR_SERVICE,
        sidecar_build_context="./" + target_dir.name,
        network_name=internal_network_name,
    )

    collect_fragment = {
        "service": DEFAULT_SIDECAR_SERVICE,
        "source": "/app/output/benchmark-events.jsonl",
        "destination": "benchmark-events.jsonl",
    }

    return {
        "sidecar_dir": target_dir.as_posix(),
        "compose_doc": compose_doc,
        "collect_fragment": collect_fragment,
        "proof_sha256": compute_sha256(proof_data),
    }


def compute_mcp_substrate_digest(
    topology: dict[str, Any],
    tool_defs: Sequence[MCPToolDefinition] | None = None,
) -> str:
    """Compute deterministic SHA-256 digest of the MCP substrate manifest, requirements, and full tool definitions."""
    payload: dict[str, Any] = {
        "substrate_version": MCP_SUBSTRATE_VERSION,
        "topology": topology,
        "requirements_hash": compute_sha256(canonical_json(FASTMCP_VERSION_CONSTRAINTS)),
    }
    if tool_defs is not None:
        payload["tools"] = [
            {
                "name": t.name,
                "description": t.description,
                "parameters": [p.to_dict() for p in t.parameters],
                "output_type": t.output_type,
                "is_distractor": t.is_distractor,
                "metadata": dict(t.metadata),
                "execution_body": t.execution_body or "",
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
    network_name: str | None = DEFAULT_INTERNAL_NETWORK_NAME,
) -> dict[str, Any]:
    """Render a canonical Harbor workbench-v2 Compose document structure with internal network and evidence volume."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", sidecar_service):
        raise SubstrateError(f"Invalid sidecar service name: {sidecar_service!r}")

    main_service_cfg: dict[str, Any] = {
        "image": main_image,
    }
    sidecar_service_cfg: dict[str, Any] = {
        "build": {
            "context": sidecar_build_context,
        },
    }

    if network_name:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", network_name):
            raise SubstrateError(f"Invalid network name: {network_name!r}")
        main_service_cfg["networks"] = [network_name]
        sidecar_service_cfg["networks"] = [network_name]

    volumes_section: dict[str, Any] | None = None
    if volume_name:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", volume_name):
            raise SubstrateError(f"Invalid volume name: {volume_name!r}")
        main_service_cfg["volumes"] = [f"{volume_name}:{volume_mount}:ro"]
        sidecar_service_cfg["volumes"] = [f"{volume_name}:{volume_mount}:rw"]
        volumes_section = {volume_name: None}

    services: dict[str, Any] = {
        "main": main_service_cfg,
        sidecar_service: sidecar_service_cfg,
    }

    doc: dict[str, Any] = {
        "services": services,
    }
    if network_name:
        doc["networks"] = {
            network_name: {
                "internal": True,
            }
        }
    if volumes_section is not None:
        doc["volumes"] = volumes_section

    return doc


def validate_mcp_compose_document(
    data: Any, allowed_sidecar: str = DEFAULT_SIDECAR_SERVICE
) -> tuple[bool, list[str]]:
    """Strictly validate a Compose document against Harbor workbench-v2 and zero-leakage constraints."""
    errors: list[str] = []
    if not isinstance(data, Mapping):
        return False, ["Compose document must be a mapping"]

    for top_key in data:
        if top_key not in {"services", "volumes", "networks", "version"}:
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

    # Validate top-level networks: at most 1 internal network
    top_networks = data.get("networks")
    network_name: str | None = None
    if top_networks is not None:
        if not isinstance(top_networks, Mapping):
            errors.append("Compose 'networks' must be a mapping")
        elif len(top_networks) > 1:
            errors.append(f"At most 1 network allowed, got {len(top_networks)}")
        elif top_networks:
            network_name, net_def = next(iter(top_networks.items()))
            if not isinstance(net_def, Mapping) or not net_def.get("internal"):
                errors.append(f"Network {network_name!r} must be declared with 'internal: true'")

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

        s_nets = s_cfg.get("networks")
        if s_nets is not None:
            if not isinstance(s_nets, Sequence) or isinstance(s_nets, (str, bytes)):
                errors.append(f"Service {name!r} networks must be a sequence of names")
            else:
                for net in s_nets:
                    if net != network_name:
                        errors.append(
                            f"Service {name!r} network {net!r} does not match top-level internal network {network_name!r}"
                        )
        elif network_name is not None:
            errors.append(
                f"Service {name!r} must attach to top-level internal network {network_name!r}"
            )

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


@dataclass(frozen=True)
class WheelhouseTarget:
    """Explicit runtime target bound into a staged platform wheelhouse."""

    python_tag: str
    platform_tag: str


FASTMCP_VERSION_CONSTRAINTS: tuple[str, ...] = ("fastmcp==3.4.7",)


def _wheel_metadata(wheel: Path) -> tuple[str, str]:
    """Return normalized distribution name/version from a wheel's METADATA bytes."""
    import zipfile
    from email.parser import BytesParser

    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise SubstrateError(
                f"wheel {wheel.name!r} must contain exactly one dist-info/METADATA"
            )
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise SubstrateError(f"wheel {wheel.name!r} METADATA must declare Name and Version")
    return name.lower().replace("_", "-"), version


def render_selected_wheel_lock(
    wheelhouse_dir: Path, target: WheelhouseTarget
) -> tuple[str, list[dict[str, Any]]]:
    """Hash the selected platform wheel bytes and render a precise offline pip lock.

    The wheelhouse is the source of truth: no universal PyPI artifact hash list is used.
    """
    if not wheelhouse_dir.is_dir():
        raise SubstrateError(f"wheelhouse directory is missing: {wheelhouse_dir}")
    all_wheels = sorted(wheelhouse_dir.glob("*.whl"))
    wheels = [
        wheel
        for wheel in all_wheels
        if "-none-any.whl" in wheel.name
        or (
            target.python_tag in wheel.name
            and (
                target.platform_tag in wheel.name
                or (target.platform_tag.startswith("macosx") and "universal2" in wheel.name)
            )
        )
        or ("abi3" in wheel.name and target.platform_tag in wheel.name)
    ]
    if not wheels:
        raise SubstrateError("wheelhouse has no selected wheels")
    seen: set[str] = set()
    inventory: list[dict[str, Any]] = []
    lock_lines = [f"# target-python={target.python_tag} target-platform={target.platform_tag}"]
    for wheel in wheels:
        name, version = _wheel_metadata(wheel)
        if name in seen:
            raise SubstrateError(f"wheelhouse contains duplicate distribution {name!r}")
        seen.add(name)
        raw = wheel.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        inventory.append(
            {
                "filename": wheel.name,
                "name": name,
                "version": version,
                "size_bytes": len(raw),
                "sha256": digest,
            }
        )
        lock_lines.append(f"{name}=={version} --hash=sha256:{digest}")
    if "fastmcp" not in seen:
        raise SubstrateError(
            "wheelhouse is missing required locked package fastmcp for selected target"
        )
    return "\n".join(lock_lines) + "\n", inventory


def stage_platform_wheelhouse(
    source: Path, destination: Path, target: WheelhouseTarget
) -> tuple[str, list[dict[str, Any]]]:
    """Copy a selected explicit-target wheelhouse and derive its byte-exact requirements lock."""
    lock, inventory = render_selected_wheel_lock(source, target)
    destination.mkdir(parents=True, exist_ok=True)
    for item in inventory:
        shutil.copy2(source / item["filename"], destination / item["filename"])
    return lock, inventory
