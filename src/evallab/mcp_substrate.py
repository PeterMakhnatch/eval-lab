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
- Substrate version & comprehensive digest computation (including execution_body, metadata, and runtime assets).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.benchmark_program_contracts import (
    FaultInjectionRecord,
    canonical_json,
    compute_sha256,
    safe_resolve_subpath,
    validate_safe_relative_path,
)

logger = logging.getLogger(__name__)

MCP_SUBSTRATE_VERSION = "0.3.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
MCP_TOOL_EVENT_SCHEMA_VERSION = "mcp-tool-event-v1"
DEFAULT_SIDECAR_SERVICE = "mcp-service"
DEFAULT_VOLUME_NAME = "evidence-volume"
DEFAULT_VOLUME_MOUNT = "/app/output"
DEFAULT_INTERNAL_NETWORK_NAME = "workbench-internal"
DEFAULT_MCP_PORT = 8080
DEFAULT_PINNED_BASE_IMAGE = (
    "python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f"
)
PINNED_BASE_IMAGE_INDEX_DIGEST = (
    "sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f"
)
PINNED_BASE_IMAGE_AMD64_MANIFEST_DIGEST = (
    "sha256:0b29ab9e420820f53d1cd5ce0157dfe07bea8a7cff5b4754d6d95c07b0e5bc47"
)
DEFAULT_TARGET_PYTHON_TAG = "cp312"
DEFAULT_TARGET_PLATFORM_TAG = "manylinux_2_17_x86_64"
_PINNED_PYTHON_IMAGE_RE = re.compile(r"^python:3\.12\.11-slim@sha256:[a-f0-9]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")

# Pinned FastMCP 3.4.7 streamable-HTTP sidecar dependencies with strict hash locking
FASTMCP_VERSION_CONSTRAINTS: tuple[str, ...] = ("fastmcp==3.4.7",)
RESERVED_RUNTIME_ASSET_PATHS = frozenset(
    {
        ".dockerignore",
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
        "Dockerfile",
        "Dockerfile.dockerignore",
        "offline-build-proof.json",
        "requirements.txt",
        "server.py",
        "wheelhouse",
    }
)
RESERVED_RUNTIME_ASSET_PATHS_FOLD = frozenset(
    name.casefold() for name in RESERVED_RUNTIME_ASSET_PATHS
)
_RUNTIME_ASSET_DEST_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")
_OP_REGISTRY_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class SubstrateError(Exception):
    """Raised when substrate configuration, validation, or runtime fails."""


# Checked-in reviewed trusted wheel manifest (CPython 3.12 manylinux_2_17_x86_64 FastMCP 3.4.7)
TRUSTED_WHEEL_MANIFEST_FILENAME = "fastmcp-3.4.7-cp312-manylinux_2_17_x86_64-manifest.json"
TRUSTED_WHEEL_MANIFEST_PATH = (
    Path(__file__).resolve().parent / "data" / TRUSTED_WHEEL_MANIFEST_FILENAME
)


def _read_trusted_manifest_bytes() -> bytes:
    """Read the checked-in manifest via filesystem or importlib.resources (installed wheel)."""
    if TRUSTED_WHEEL_MANIFEST_PATH.is_file():
        return TRUSTED_WHEEL_MANIFEST_PATH.read_bytes()
    import importlib.resources as _resources

    try:
        return (
            _resources.files("evallab")
            .joinpath("data")
            .joinpath(TRUSTED_WHEEL_MANIFEST_FILENAME)
            .read_bytes()
        )
    except (FileNotFoundError, ModuleNotFoundError, TypeError) as exc:
        raise SubstrateError(
            f"trusted wheel manifest missing: {TRUSTED_WHEEL_MANIFEST_PATH.as_posix()!r}"
        ) from exc


_MANIFEST_SCHEMA_VERSION = "1.0"
_MANIFEST_WHEEL_KEYS = frozenset({"filename", "name", "version", "size_bytes", "sha256"})


def load_trusted_wheel_manifest() -> dict[str, Any]:
    """Load and strictly validate the checked-in trusted wheel manifest.

    The manifest is the reviewed supply-chain trust root: downloaded wheel bytes
    must exactly match filenames/versions/sizes/SHA-256 recorded here for the
    pinned CPython 3.12 manylinux_2_17_x86_64 FastMCP 3.4.7 target.
    """
    try:
        raw = _read_trusted_manifest_bytes()
    except OSError as exc:
        raise SubstrateError(
            f"trusted wheel manifest missing: {TRUSTED_WHEEL_MANIFEST_PATH.as_posix()!r}"
        ) from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise SubstrateError("trusted wheel manifest is not valid JSON") from exc
    if not isinstance(data, Mapping):
        raise SubstrateError("trusted wheel manifest must be a JSON object")
    if data.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
        raise SubstrateError("trusted wheel manifest schema_version is unsupported")
    target = data.get("target")
    if not isinstance(target, Mapping):
        raise SubstrateError("trusted wheel manifest must declare a target")
    python_tag = target.get("python_tag")
    platform_tag = target.get("platform_tag")
    if python_tag != DEFAULT_TARGET_PYTHON_TAG or platform_tag != DEFAULT_TARGET_PLATFORM_TAG:
        raise SubstrateError(
            f"trusted wheel manifest target {python_tag!r}/{platform_tag!r} does not match "
            f"{DEFAULT_TARGET_PYTHON_TAG}/{DEFAULT_TARGET_PLATFORM_TAG}"
        )
    source = data.get("source")
    if not isinstance(source, str) or not source:
        raise SubstrateError("trusted wheel manifest must declare a non-empty source")
    if data.get("fastmcp_version") not in FASTMCP_VERSION_CONSTRAINTS[0].split("=="):
        raise SubstrateError(
            "trusted wheel manifest fastmcp_version does not match pinned constraint"
        )
    wheels = data.get("wheels")
    if not isinstance(wheels, list) or not wheels:
        raise SubstrateError("trusted wheel manifest must declare a non-empty wheels list")
    seen: set[str] = set()
    for entry in wheels:
        if not isinstance(entry, Mapping) or set(entry) != _MANIFEST_WHEEL_KEYS:
            raise SubstrateError(
                "each manifest wheel entry must contain exactly filename/name/version/size_bytes/sha256"
            )
        if not isinstance(entry["filename"], str) or not entry["filename"]:
            raise SubstrateError("manifest wheel filename must be a non-empty string")
        folded = entry["filename"].casefold()
        if folded in seen:
            raise SubstrateError(
                f"manifest contains duplicate wheel filename {entry['filename']!r}"
            )
        seen.add(folded)
        if not isinstance(entry["sha256"], str) or not _SHA256_HEX_RE.fullmatch(entry["sha256"]):
            raise SubstrateError(f"manifest wheel {entry['filename']!r} sha256 is invalid")
        if not isinstance(entry["size_bytes"], int) or entry["size_bytes"] <= 0:
            raise SubstrateError(
                f"manifest wheel {entry['filename']!r} size_bytes must be positive"
            )
        if not isinstance(entry["name"], str) or not isinstance(entry["version"], str):
            raise SubstrateError(
                f"manifest wheel {entry['filename']!r} name/version must be strings"
            )
    return data


def trusted_wheel_manifest_digest() -> str:
    """Deterministic SHA-256 of the exact checked-in trusted manifest bytes."""
    try:
        raw = _read_trusted_manifest_bytes()
    except OSError as exc:
        raise SubstrateError(
            f"trusted wheel manifest missing: {TRUSTED_WHEEL_MANIFEST_PATH.as_posix()!r}"
        ) from exc
    return compute_sha256(raw)


def trusted_wheel_manifest_source() -> str:
    """Return the explicit PyPI source declared in the checked-in trusted manifest."""
    return load_trusted_wheel_manifest()["source"]


def canonical_tool_definitions_payload(
    tools: Sequence[MCPToolDefinition],
    op_registry_module: str | None = None,
) -> dict[str, Any]:
    """Deterministic canonical payload binding the full tool/action alphabet semantics."""
    return {
        "event_schema_version": MCP_TOOL_EVENT_SCHEMA_VERSION,
        "op_registry_module": op_registry_module or "",
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": [p.to_dict() for p in t.parameters],
                "output_type": t.output_type,
                "is_distractor": t.is_distractor,
                "metadata": dict(t.metadata),
                "execution_body": t.execution_body or "",
            }
            for t in sorted(tools, key=lambda x: x.name)
        ],
    }


def compute_tool_definitions_sha256(
    tools: Sequence[MCPToolDefinition],
    op_registry_module: str | None = None,
) -> str:
    """Deterministic SHA-256 over tool names/descriptions/schemas/bodies/op_registry/event schema."""
    return compute_sha256(
        canonical_json(canonical_tool_definitions_payload(tools, op_registry_module))
    )


def validate_target_base_runtime(
    python_tag: str,
    platform_tag: str,
    base_image: str = DEFAULT_PINNED_BASE_IMAGE,
    *,
    base_image_index_digest: str = PINNED_BASE_IMAGE_INDEX_DIGEST,
    base_image_amd64_manifest_digest: str = PINNED_BASE_IMAGE_AMD64_MANIFEST_DIGEST,
) -> dict[str, str]:
    """Fail-closed compatibility check for Python tag, platform, and pinned base image."""
    if not isinstance(base_image, str) or not _PINNED_PYTHON_IMAGE_RE.fullmatch(base_image):
        raise SubstrateError(
            f"base image must be pinned python:3.12.11-slim@sha256:<index-digest>, got {base_image!r}"
        )
    if not isinstance(base_image_index_digest, str) or not _DIGEST_RE.fullmatch(
        base_image_index_digest
    ):
        raise SubstrateError(f"base image index digest is invalid: {base_image_index_digest!r}")
    if not isinstance(base_image_amd64_manifest_digest, str) or not _DIGEST_RE.fullmatch(
        base_image_amd64_manifest_digest
    ):
        raise SubstrateError(
            f"base image amd64 manifest digest is invalid: {base_image_amd64_manifest_digest!r}"
        )
    image_digest = base_image.split("@", 1)[1]
    if image_digest != base_image_index_digest:
        raise SubstrateError("base image reference digest does not match declared index digest")
    if base_image_index_digest != PINNED_BASE_IMAGE_INDEX_DIGEST:
        raise SubstrateError(
            "base image index digest is not the pinned CPython 3.12.11-slim runtime"
        )
    if base_image_amd64_manifest_digest != PINNED_BASE_IMAGE_AMD64_MANIFEST_DIGEST:
        raise SubstrateError(
            "base image amd64 manifest digest is not the pinned CPython 3.12.11-slim runtime"
        )
    if python_tag != DEFAULT_TARGET_PYTHON_TAG:
        raise SubstrateError(
            f"python tag {python_tag!r} is incompatible with pinned CPython 3.12 base runtime"
        )
    if platform_tag != DEFAULT_TARGET_PLATFORM_TAG:
        raise SubstrateError(
            f"platform tag {platform_tag!r} is incompatible with pinned manylinux CPython 3.12 base runtime"
        )
    return {
        "base_image": base_image,
        "base_image_index_digest": base_image_index_digest,
        "base_image_amd64_manifest_digest": base_image_amd64_manifest_digest,
        "target_python": python_tag,
        "target_platform": platform_tag,
    }


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
    """Mechanically verify that wheelhouse contains an exact matching wheel for every locked requirement."""
    if not wheelhouse_dir.is_dir() or wheelhouse_dir.is_symlink():
        raise SubstrateError(
            f"Wheelhouse directory does not exist or is symlink: {wheelhouse_dir.as_posix()!r}"
        )

    locked = parse_requirements_hashes(requirements_text)
    wheels = list(wheelhouse_dir.glob("*.whl"))
    if not wheels:
        raise SubstrateError(
            f"Wheelhouse {wheelhouse_dir.as_posix()!r} is empty (contains 0 wheels)"
        )

    matched_packages: set[str] = set()
    inventory: list[dict[str, Any]] = []

    for w_file in sorted(wheels, key=lambda p: p.name):
        w_bytes = _read_file_source(w_file)
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
class RuntimeAsset:
    """Explicit host file or raw bytes copied into the sidecar package at a confined relative path."""

    destination: str
    source: Path | None = None
    content: bytes | None = None

    def __post_init__(self) -> None:
        if (self.source is None and self.content is None) or (
            self.source is not None and self.content is not None
        ):
            raise SubstrateError("RuntimeAsset requires exactly one of 'source' or 'content'")


def op_registry_module_destination(module: str) -> str:
    """Map a dotted op-registry module name to its required runtime asset path."""
    if not module or not _OP_REGISTRY_MODULE_RE.fullmatch(module):
        raise SubstrateError(f"Invalid op_registry_module: {module!r}")
    return "/".join(module.split(".")) + ".py"


def _runtime_asset_has_control_chars(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def validate_runtime_asset_destination(destination: str) -> str:
    """Normalize a destination path and reject traversal, reserved, and injectable names."""
    if not isinstance(destination, str) or _runtime_asset_has_control_chars(destination):
        raise SubstrateError(
            f"Runtime asset destination contains control characters: {destination!r}"
        )
    destination = unicodedata.normalize("NFC", destination)
    try:
        destination = validate_safe_relative_path(destination)
    except ValueError as exc:
        raise SubstrateError(str(exc)) from exc
    if not _RUNTIME_ASSET_DEST_RE.fullmatch(destination):
        raise SubstrateError(
            f"Runtime asset destination is not a confined POSIX path: {destination!r}"
        )
    folded = destination.casefold()
    first_component = folded.split("/", 1)[0]
    if (
        folded in RESERVED_RUNTIME_ASSET_PATHS_FOLD
        or first_component in RESERVED_RUNTIME_ASSET_PATHS_FOLD
        or folded.startswith("dockerfile.")
        or first_component.startswith("dockerfile.")
    ):
        raise SubstrateError(f"Runtime asset destination is reserved: {destination!r}")
    return destination


def _dockerfile_copy_token(value: str, *, name: str) -> str:
    if not isinstance(value, str) or _runtime_asset_has_control_chars(value):
        raise SubstrateError(f"Invalid Dockerfile {name}: {value!r}")
    if not re.fullmatch(r"/?[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", value):
        raise SubstrateError(f"Invalid Dockerfile {name}: {value!r}")
    return value


def _read_file_source(source: Path) -> bytes:
    try:
        st = os.lstat(source)
    except OSError as exc:
        raise SubstrateError(f"Cannot stat runtime asset source: {source.as_posix()!r}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise SubstrateError(f"Runtime asset source is a symlink: {source.as_posix()!r}")
    if not stat.S_ISREG(st.st_mode):
        raise SubstrateError(f"Runtime asset source is not a regular file: {source.as_posix()!r}")

    flags = os.O_RDONLY | _NOFOLLOW
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise SubstrateError(
            f"Runtime asset source open failed (symlink/unreadable): {source.as_posix()!r}"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SubstrateError(
                f"Runtime asset source is not a regular file: {source.as_posix()!r}"
            )
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _read_runtime_asset(asset: RuntimeAsset) -> bytes:
    if asset.content is not None:
        if not isinstance(asset.content, bytes):
            raise SubstrateError("RuntimeAsset content must be bytes")
        return asset.content
    assert asset.source is not None
    source = asset.source if isinstance(asset.source, Path) else Path(asset.source)
    return _read_file_source(source)


def _reject_runtime_asset_prefix_conflicts(destinations: Sequence[str]) -> None:
    """Reject file destinations that collide with an ancestor/descendant prefix or reserved root."""
    folded = sorted({destination.casefold() for destination in destinations})
    for index, left in enumerate(folded):
        prefix = f"{left}/"
        for right in folded[index + 1 :]:
            if right.startswith(prefix):
                raise SubstrateError(
                    f"Runtime asset destination {right!r} conflicts with prefix {left!r}"
                )
    for dest in folded:
        first_comp = dest.split("/", 1)[0]
        if first_comp in RESERVED_RUNTIME_ASSET_PATHS_FOLD:
            raise SubstrateError(
                f"Runtime asset destination {dest!r} conflicts with reserved root {first_comp!r}"
            )


def validate_runtime_assets(
    runtime_assets: Sequence[RuntimeAsset],
) -> tuple[tuple[RuntimeAsset, bytes], ...]:
    """Fail-closed validation of explicit runtime assets."""
    prepared: dict[str, tuple[RuntimeAsset, bytes]] = {}
    seen_fold: set[str] = set()
    for asset in runtime_assets:
        destination = validate_runtime_asset_destination(asset.destination)
        folded = destination.casefold()
        if folded in seen_fold:
            raise SubstrateError(f"Duplicate runtime asset destination: {destination!r}")
        seen_fold.add(folded)
        content = _read_runtime_asset(asset)
        prepared[destination] = (
            RuntimeAsset(destination=destination, source=None, content=content),
            content,
        )
    destinations = tuple(prepared)
    _reject_runtime_asset_prefix_conflicts(destinations)
    return tuple(prepared[key] for key in sorted(prepared))


def _confined_relative_parts(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str) or _runtime_asset_has_control_chars(relative):
        raise SubstrateError(f"Invalid confined relative path: {relative!r}")
    if relative.startswith("/") or "\\" in relative:
        raise SubstrateError(f"Invalid confined relative path: {relative!r}")
    parts = Path(relative).parts
    if not parts or any(part in (".", "..") for part in parts):
        raise SubstrateError(f"Invalid confined relative path: {relative!r}")
    return parts


def _write_confined_bytes(root: Path, relative: str, data: bytes) -> None:
    """Write bytes under root without following any destination symlink component."""
    parts = _confined_relative_parts(relative)
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    tmp_name: str | None = None
    try:
        for part in parts[:-1]:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o755, dir_fd=dir_fd)
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
        name = parts[-1]
        tmp_name = f".{name}.{os.getpid()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
        fd = os.open(tmp_name, flags, 0o644, dir_fd=dir_fd)
        try:
            view = memoryview(data)
            offset = 0
            while offset < len(view):
                offset += os.write(fd, view[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        tmp_name = None
    except OSError as exc:
        raise SubstrateError(f"Failed confined write for {relative!r}") from exc
    finally:
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name, dir_fd=dir_fd)
        os.close(dir_fd)


def _write_confined_text(root: Path, relative: str, text: str) -> None:
    _write_confined_bytes(root, relative, text.encode("utf-8"))


def _runtime_asset_proof_records(
    prepared: Sequence[tuple[RuntimeAsset, bytes]],
) -> list[dict[str, Any]]:
    return [
        {
            "path": asset.destination,
            "sha256": compute_sha256(content),
            "size_bytes": len(content),
        }
        for asset, content in prepared
    ]


def _require_op_registry_asset(
    op_registry_module: str | None,
    prepared: Sequence[tuple[RuntimeAsset, bytes]],
) -> None:
    if op_registry_module is None:
        return
    required = op_registry_module_destination(op_registry_module)
    present = {asset.destination for asset, _content in prepared}
    if required not in present:
        raise SubstrateError(
            f"op_registry_module {op_registry_module!r} requires runtime asset {required!r}"
        )


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
        "from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext",
        "from fastmcp.tools.base import ToolResult",
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
            'EVENT_SCHEMA_VERSION = "mcp-tool-event-v1"',
            "EVENT_LOCK = threading.Lock()",
            "EVENT_ORDINAL = 0",
            "",
            "def _journal(event: dict[str, Any]) -> None:",
            "    global EVENT_ORDINAL",
            "    with EVENT_LOCK:",
            "        EVENT_ORDINAL += 1",
            "        record = dict(event)",
            '        record["schema_version"] = EVENT_SCHEMA_VERSION',
            '        record["event_ordinal"] = EVENT_ORDINAL',
            "        EVIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)",
            '        with open(EVIDENCE_FILE, "a", encoding="utf-8") as f:',
            '            f.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\\n")',
            "",
            "def _extract_result(result: Any) -> Any:",
            '    sc = getattr(result, "structured_content", None)',
            "    if sc is not None:",
            "        return sc",
            '    texts = [c.text for c in getattr(result, "content", []) if getattr(c, "type", None) == "text"]',
            "    if len(texts) == 1:",
            "        try:",
            "            return json.loads(texts[0])",
            "        except Exception:",
            "            return texts[0]",
            "    return None",
            "",
            "class EventJournalMiddleware(Middleware):",
            '    """Journal every tools/call outcome (success and error) with the mcp-tool-event-v1 schema."""',
            "",
            "    async def on_call_tool(",
            "        self,",
            "        context: MiddlewareContext[Any],",
            "        call_next: CallNext[Any, ToolResult],",
            "    ) -> ToolResult:",
            "        params = context.message",
            '        tool_name = getattr(params, "name", None)',
            '        arguments = getattr(params, "arguments", None)',
            "        if arguments is None:",
            "            arguments = {}",
            "        base = {",
            '            "tool_name": tool_name,',
            '            "arguments": arguments,',
            "        }",
            "        try:",
            "            result = await call_next(context)",
            "        except Exception as exc:",
            "            _journal({",
            "                **base,",
            '                "event_type": "tool_call_error",',
            '                "is_error": True,',
            '                "error": {"type": type(exc).__name__, "message": str(exc)},',
            "            })",
            "            raise",
            '        if bool(getattr(result, "is_error", False)):',
            '            err_texts = [c.text for c in getattr(result, "content", []) if getattr(c, "type", None) == "text"]',
            "            _journal({",
            "                **base,",
            '                "event_type": "tool_call_error",',
            '                "is_error": True,',
            '                "error": {"type": "tool_error", "message": err_texts[0] if err_texts else "tool error"},',
            "            })",
            "        else:",
            "            _journal({",
            "                **base,",
            '                "event_type": "tool_call_success",',
            '                "is_error": False,',
            '                "result": _extract_result(result),',
            "            })",
            "        return result",
            "",
            "mcp.add_middleware(EventJournalMiddleware())",
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
                    "    return res",
                ]
            )
        lines.append("")

    lines.extend(
        [
            'if __name__ == "__main__":',
            f'    mcp.run(transport="streamable-http", host="0.0.0.0", port={port})',
            "",
        ]
    )
    return "\n".join(lines)


def render_mcp_sidecar_dockerfile(
    base_image: str = DEFAULT_PINNED_BASE_IMAGE,
    wheelhouse_dir: str = "/wheelhouse",
    app_dir: str = "/app",
    server_script_name: str = "server.py",
    runtime_assets: Sequence[RuntimeAsset] = (),
) -> str:
    """Render canonical offline sidecar Dockerfile using strict hash-locked pip installation."""
    validate_target_base_runtime(DEFAULT_TARGET_PYTHON_TAG, DEFAULT_TARGET_PLATFORM_TAG, base_image)
    wheelhouse_dir = _dockerfile_copy_token(wheelhouse_dir, name="wheelhouse_dir")
    app_dir = _dockerfile_copy_token(app_dir, name="app_dir")
    server_script_name = _dockerfile_copy_token(server_script_name, name="server_script_name")
    if server_script_name != "server.py":
        raise SubstrateError(f"server_script_name must be server.py, got {server_script_name!r}")
    destinations: list[str] = []
    seen_fold: set[str] = set()
    for asset in runtime_assets:
        destination = validate_runtime_asset_destination(asset.destination)
        folded = destination.casefold()
        if folded in seen_fold:
            raise SubstrateError(f"Duplicate runtime asset destination: {destination!r}")
        seen_fold.add(folded)
        destinations.append(destination)
    _reject_runtime_asset_prefix_conflicts(destinations)
    asset_copy = "".join(
        f"COPY {destination} {app_dir}/{destination}\n" for destination in sorted(destinations)
    )
    return f"""FROM {base_image}

WORKDIR {app_dir}

COPY wheelhouse {wheelhouse_dir}
COPY requirements.txt {app_dir}/requirements.txt
COPY {server_script_name} {app_dir}/{server_script_name}
{asset_copy}
RUN pip install --no-cache-dir --no-index --find-links={wheelhouse_dir} --require-hashes -r {app_dir}/requirements.txt

RUN mkdir -p /app/output

CMD ["python", "{app_dir}/{server_script_name}"]
"""


def _stage_clean_package_directory(
    staging: Path,
    tools: Sequence[MCPToolDefinition],
    server_name: str,
    port: int,
    base_image: str,
    wheelhouse_source: Path | None,
    op_registry_module: str | None,
    fault_record: FaultInjectionRecord | None,
    plan_only: bool,
    selected_target: WheelhouseTarget,
    resolver_provenance: ResolverProvenance | None,
    runtime_meta: dict[str, str],
    prepared_assets: Sequence[tuple[RuntimeAsset, bytes]],
) -> tuple[dict[str, Any], tuple[RuntimeAsset, ...]]:
    """Stage all package artifacts into a clean, empty sibling directory."""
    server_code = generate_fastmcp_server_script(
        tools=tools,
        server_name=server_name,
        port=port,
        op_registry_module=op_registry_module,
        fault_record=fault_record,
    )
    _write_confined_text(staging, "server.py", server_code)

    sorted_assets = tuple(asset for asset, _content in prepared_assets)
    for asset, content in prepared_assets:
        _write_confined_bytes(staging, asset.destination, content)

    asset_proof = _runtime_asset_proof_records(prepared_assets)

    if plan_only:
        proof_data = {
            "mode": "plan_only",
            "substrate_version": MCP_SUBSTRATE_VERSION,
            **runtime_meta,
            "requirements_sha256": compute_sha256(canonical_json(FASTMCP_VERSION_CONSTRAINTS)),
            "event_schema_version": MCP_TOOL_EVENT_SCHEMA_VERSION,
            "tool_definitions_sha256": compute_tool_definitions_sha256(tools, op_registry_module),
            "trusted_manifest_digest": trusted_wheel_manifest_digest(),
            "trusted_manifest_source": trusted_wheel_manifest_source(),
            "runtime_assets": asset_proof,
        }
        _write_confined_text(staging, "offline-build-proof.json", canonical_json(proof_data) + "\n")
        _write_confined_text(
            staging,
            "requirements.txt",
            "# plan-only; resolve a target wheelhouse before build\n",
        )
    else:
        assert wheelhouse_source is not None
        assert resolver_provenance is not None
        dest_wheelhouse = staging / "wheelhouse"
        dest_wheelhouse.mkdir(parents=True, exist_ok=True)
        _, selected_inventory = stage_platform_wheelhouse(
            wheelhouse_source, dest_wheelhouse, selected_target
        )
        wheel_inventory = verify_provenance_wheelhouse(dest_wheelhouse, resolver_provenance)
        requirements_lock = render_provenance_lock(resolver_provenance)
        _write_confined_text(staging, "requirements.txt", requirements_lock)

        dockerfile_content = render_mcp_sidecar_dockerfile(
            base_image=base_image, runtime_assets=sorted_assets
        )
        _write_confined_text(staging, "Dockerfile", dockerfile_content)

        server_bytes = server_code.encode("utf-8")
        proof_data = {
            "mode": "complete_offline_package",
            "substrate_version": MCP_SUBSTRATE_VERSION,
            **runtime_meta,
            "requirements_sha256": compute_sha256(requirements_lock),
            "wheel_count": len(wheel_inventory),
            "wheels": wheel_inventory,
            "dockerfile_sha256": compute_sha256(dockerfile_content),
            "server_sha256": compute_sha256(server_bytes),
            "server_size_bytes": len(server_bytes),
            "event_schema_version": MCP_TOOL_EVENT_SCHEMA_VERSION,
            "tool_definitions_sha256": compute_tool_definitions_sha256(tools, op_registry_module),
            "trusted_manifest_digest": trusted_wheel_manifest_digest(),
            "trusted_manifest_source": trusted_wheel_manifest_source(),
            "runtime_assets": asset_proof,
        }
        _write_confined_text(staging, "offline-build-proof.json", canonical_json(proof_data) + "\n")

    return proof_data, sorted_assets


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
    resolver_provenance: ResolverProvenance | None = None,
    runtime_assets: Sequence[RuntimeAsset] = (),
) -> dict[str, Any]:
    """Boring task-authoring API emitting a complete, workbench-v2 compliant offline FastMCP sidecar package."""
    # 1. Preflight all configuration and arguments before touching filesystem
    selected_target = target or WheelhouseTarget(
        python_tag=DEFAULT_TARGET_PYTHON_TAG, platform_tag=DEFAULT_TARGET_PLATFORM_TAG
    )
    runtime_meta = validate_target_base_runtime(
        selected_target.python_tag, selected_target.platform_tag, base_image
    )
    prepared_assets = validate_runtime_assets(runtime_assets)
    _require_op_registry_asset(op_registry_module, prepared_assets)

    if not plan_only:
        if wheelhouse_source is None:
            raise SubstrateError(
                "wheelhouse_source is mandatory for production sidecar materialization; pass plan_only=True to emit plan without container build artifacts"
            )
        if resolver_provenance is None:
            raise SubstrateError("resolver_provenance is mandatory for production materialization")
        if resolver_provenance.target != selected_target:
            raise SubstrateError("resolver provenance target does not match requested target")
        if resolver_provenance.manifest_digest != trusted_wheel_manifest_digest():
            raise SubstrateError(
                "resolver provenance manifest_digest does not bind the checked-in trusted wheel manifest"
            )
        if resolver_provenance.manifest_source != trusted_wheel_manifest_source():
            raise SubstrateError(
                "resolver provenance manifest_source does not bind the checked-in trusted wheel manifest"
            )

    # 2. Target path MUST be absent up front; refuse existing file, directory, or symlink
    if target_dir.is_symlink():
        raise SubstrateError(f"target_dir is a symlink: {target_dir.as_posix()!r}")
    raw_target = target_dir
    target_dir = safe_resolve_subpath(target_dir.parent, target_dir.name)
    if (
        raw_target.exists()
        or raw_target.is_symlink()
        or target_dir.exists()
        or target_dir.is_symlink()
    ):
        raise SubstrateError(f"target_dir already exists: {target_dir.as_posix()!r}")

    parent = target_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)

    # 3. Create unguessable private staging directory in sibling location with mode 0700
    staging = Path(tempfile.mkdtemp(prefix=f".{target_dir.name}.", dir=parent))
    os.chmod(staging, 0o700)
    published = False
    try:
        proof_data, _sorted_assets = _stage_clean_package_directory(
            staging=staging,
            tools=tools,
            server_name=server_name,
            port=port,
            base_image=base_image,
            wheelhouse_source=wheelhouse_source,
            op_registry_module=op_registry_module,
            fault_record=fault_record,
            plan_only=plan_only,
            selected_target=selected_target,
            resolver_provenance=resolver_provenance,
            runtime_meta=runtime_meta,
            prepared_assets=prepared_assets,
        )

        # Fsync staging directory before publishing
        staging_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
        try:
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)

        # 4. Atomic publish via single rename under parent directory fd
        os.rename(staging.name, target_dir.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
        published = True
    finally:
        os.close(parent_fd)
        if not published:
            shutil.rmtree(staging, ignore_errors=True)

    # Compose and Collect fragments (strictly emitted only for complete production packages)
    if plan_only:
        compose_doc = None
        collect_fragment = None
    else:
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
    *,
    target: WheelhouseTarget | None = None,
    base_image: str = DEFAULT_PINNED_BASE_IMAGE,
    runtime_assets: Sequence[RuntimeAsset] = (),
    op_registry_module: str | None = None,
) -> str:
    """Compute deterministic SHA-256 digest of the MCP substrate manifest, requirements, and full tool definitions."""
    selected_target = target or WheelhouseTarget(
        python_tag=DEFAULT_TARGET_PYTHON_TAG, platform_tag=DEFAULT_TARGET_PLATFORM_TAG
    )
    payload: dict[str, Any] = {
        "substrate_version": MCP_SUBSTRATE_VERSION,
        "topology": topology,
        "requirements_hash": compute_sha256(canonical_json(FASTMCP_VERSION_CONSTRAINTS)),
        "base_runtime": validate_target_base_runtime(
            selected_target.python_tag, selected_target.platform_tag, base_image
        ),
    }
    prepared_assets = validate_runtime_assets(runtime_assets)
    if prepared_assets:
        payload["runtime_assets"] = _runtime_asset_proof_records(prepared_assets)
    payload["event_schema_version"] = MCP_TOOL_EVENT_SCHEMA_VERSION
    payload["trusted_manifest_digest"] = trusted_wheel_manifest_digest()
    payload["trusted_manifest_source"] = trusted_wheel_manifest_source()
    if tool_defs is not None:
        payload["tool_definitions_sha256"] = compute_tool_definitions_sha256(
            tool_defs, op_registry_module
        )
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


def _wheel_metadata_from_bytes(raw_bytes: bytes, wheel_name: str) -> tuple[str, str]:
    """Return normalized distribution name/version from raw wheel bytes."""
    import io
    import zipfile
    from email.parser import BytesParser

    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise SubstrateError(
                f"wheel {wheel_name!r} must contain exactly one dist-info/METADATA"
            )
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise SubstrateError(f"wheel {wheel_name!r} METADATA must declare Name and Version")
    return name.lower().replace("_", "-"), version


def _wheel_metadata(wheel: Path) -> tuple[str, str]:
    """Return normalized distribution name/version from a wheel file."""
    raw = _read_file_source(wheel)
    return _wheel_metadata_from_bytes(raw, wheel.name)


def _load_selected_wheelhouse_entries(
    wheelhouse_dir: Path, target: WheelhouseTarget
) -> tuple[str, list[dict[str, Any]], list[tuple[str, bytes]]]:
    """Read each selected wheel once via O_NOFOLLOW file descriptors, returning exact bytes, inventory, and lock."""
    try:
        st = os.lstat(wheelhouse_dir)
    except OSError as exc:
        raise SubstrateError(f"Cannot stat wheelhouse: {wheelhouse_dir.as_posix()!r}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise SubstrateError(f"wheelhouse directory is missing or symlink: {wheelhouse_dir}")

    src_fd = os.open(wheelhouse_dir, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    try:
        entries = sorted(os.listdir(src_fd))
        all_wheels = [name for name in entries if name.endswith(".whl")]
        wheels = [
            name
            for name in all_wheels
            if "-none-any.whl" in name
            or (
                target.python_tag in name
                and (
                    target.platform_tag in name
                    or (target.platform_tag.startswith("macosx") and "universal2" in name)
                )
            )
            or ("abi3" in name and target.platform_tag in name)
        ]
        if not wheels:
            raise SubstrateError("wheelhouse has no selected wheels")
        seen: set[str] = set()
        inventory: list[dict[str, Any]] = []
        loaded_bytes: list[tuple[str, bytes]] = []
        lock_lines = [f"# target-python={target.python_tag} target-platform={target.platform_tag}"]
        for wheel_name in wheels:
            w_fd = os.open(wheel_name, os.O_RDONLY | _NOFOLLOW, dir_fd=src_fd)
            try:
                w_st = os.fstat(w_fd)
                if not stat.S_ISREG(w_st.st_mode):
                    raise SubstrateError(f"wheel {wheel_name!r} is not a regular file")
                with os.fdopen(w_fd, "rb") as handle:
                    w_fd = -1
                    content = handle.read()
            finally:
                if w_fd >= 0:
                    os.close(w_fd)

            name, version = _wheel_metadata_from_bytes(content, wheel_name)
            if name in seen:
                raise SubstrateError(f"wheelhouse contains duplicate distribution {name!r}")
            seen.add(name)
            digest = hashlib.sha256(content).hexdigest()
            inventory.append(
                {
                    "filename": wheel_name,
                    "name": name,
                    "version": version,
                    "size_bytes": len(content),
                    "sha256": digest,
                }
            )
            loaded_bytes.append((wheel_name, content))
            lock_lines.append(f"{name}=={version} --hash=sha256:{digest}")
        if "fastmcp" not in seen:
            raise SubstrateError(
                "wheelhouse is missing required locked package fastmcp for selected target"
            )
        return "\n".join(lock_lines) + "\n", inventory, loaded_bytes
    finally:
        os.close(src_fd)


def render_selected_wheel_lock(
    wheelhouse_dir: Path, target: WheelhouseTarget
) -> tuple[str, list[dict[str, Any]]]:
    """Hash the selected platform wheel bytes and render a precise offline pip lock."""
    lock, inventory, _ = _load_selected_wheelhouse_entries(wheelhouse_dir, target)
    return lock, inventory


def stage_platform_wheelhouse(
    source: Path, destination: Path, target: WheelhouseTarget
) -> tuple[str, list[dict[str, Any]]]:
    """Copy a selected explicit-target wheelhouse from exact single-read bytes."""
    lock, inventory, loaded_bytes = _load_selected_wheelhouse_entries(source, target)
    if destination.is_symlink():
        raise SubstrateError("wheelhouse destination is a symlink")
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or not destination.is_dir():
        raise SubstrateError("wheelhouse destination is not a real directory")

    for filename, content in loaded_bytes:
        dest_file = destination / filename
        if dest_file.is_symlink():
            raise SubstrateError(f"wheelhouse entry is a symlink: {filename!r}")
        _write_confined_bytes(destination, filename, content)

    return lock, inventory


@dataclass(frozen=True)
class ResolverProvenance:
    """Trusted network-prepackaging result bound to the checked-in reviewed wheel manifest."""

    target: WheelhouseTarget
    manifest_digest: str
    manifest_source: str
    wheels: tuple[dict[str, Any], ...]

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ResolverProvenance:
        target = value.get("target")
        wheels = value.get("wheels")
        if not isinstance(target, Mapping) or not isinstance(wheels, list):
            raise SubstrateError("resolver provenance must contain target and wheels")
        python_tag = target.get("python_tag")
        platform_tag = target.get("platform_tag")
        if not isinstance(python_tag, str) or not isinstance(platform_tag, str):
            raise SubstrateError("resolver provenance target is invalid")
        manifest_digest = value.get("manifest_digest")
        manifest_source = value.get("manifest_source")
        if not isinstance(manifest_digest, str) or not _SHA256_HEX_RE.fullmatch(manifest_digest):
            raise SubstrateError(
                "resolver provenance manifest_digest must be a valid sha256 digest"
            )
        if not isinstance(manifest_source, str) or not manifest_source:
            raise SubstrateError("resolver provenance manifest_source must be a non-empty string")
        normalized: list[dict[str, Any]] = []
        for item in wheels:
            if not isinstance(item, Mapping):
                raise SubstrateError("resolver provenance wheel entry is invalid")
            required = {"filename", "name", "version", "size_bytes", "sha256"}
            if set(item) != required:
                raise SubstrateError(
                    "resolver provenance wheel entry must contain exact filename/name/version/size_bytes/sha256"
                )
            if not all(isinstance(item[k], str) for k in ("filename", "name", "version", "sha256")):
                raise SubstrateError("resolver provenance wheel scalar fields must be strings")
            if not isinstance(item["size_bytes"], int) or item["size_bytes"] <= 0:
                raise SubstrateError("resolver provenance wheel size_bytes must be a positive int")
            if not isinstance(item["sha256"], str) or not _SHA256_HEX_RE.fullmatch(item["sha256"]):
                raise SubstrateError(
                    f"resolver provenance wheel {item['filename']!r} sha256 is invalid"
                )
            normalized.append(dict(item))
        names = [item["name"].lower().replace("_", "-") for item in normalized]
        if len(names) != len(set(names)):
            raise SubstrateError("resolver provenance contains duplicate distributions")
        return cls(
            WheelhouseTarget(python_tag, platform_tag),
            manifest_digest,
            manifest_source,
            tuple(normalized),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": {
                "python_tag": self.target.python_tag,
                "platform_tag": self.target.platform_tag,
            },
            "manifest_digest": self.manifest_digest,
            "manifest_source": self.manifest_source,
            "wheels": list(self.wheels),
        }


def _manifest_by_filename() -> dict[str, dict[str, Any]]:
    """Return a mapping of manifest filename -> entry from the checked-in trusted manifest."""
    manifest = load_trusted_wheel_manifest()
    return {entry["filename"]: entry for entry in manifest["wheels"]}


def verify_provenance_wheelhouse(
    wheelhouse: Path, provenance: ResolverProvenance
) -> list[dict[str, Any]]:
    """Verify selected staged bytes exactly match the trusted resolver provenance manifest."""
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise SubstrateError("wheelhouse directory is missing or symlink")
    if provenance.manifest_digest != trusted_wheel_manifest_digest():
        raise SubstrateError(
            "resolver provenance manifest_digest does not match checked-in trusted wheel manifest"
        )
    if provenance.manifest_source != trusted_wheel_manifest_source():
        raise SubstrateError(
            "resolver provenance manifest_source does not match checked-in trusted wheel manifest"
        )
    expected = {item["filename"]: item for item in provenance.wheels}
    found = {wheel.name: wheel for wheel in wheelhouse.glob("*.whl")}
    if set(found) != set(expected):
        raise SubstrateError(
            f"wheelhouse files differ from resolver provenance: missing={sorted(set(expected) - set(found))}, extra={sorted(set(found) - set(expected))}"
        )
    inventory: list[dict[str, Any]] = []
    for filename, record in sorted(expected.items()):
        wheel = found[filename]
        name, version = _wheel_metadata(wheel)
        content = _read_file_source(wheel)
        actual = hashlib.sha256(content).hexdigest()
        if (
            name != record["name"].lower().replace("_", "-")
            or version != record["version"]
            or actual != record["sha256"]
            or len(content) != record["size_bytes"]
        ):
            raise SubstrateError(f"wheel {filename!r} does not match trusted resolver provenance")
        inventory.append(
            {
                "filename": filename,
                "name": name,
                "version": version,
                "size_bytes": len(content),
                "sha256": actual,
            }
        )
    return inventory


def render_provenance_lock(provenance: ResolverProvenance) -> str:
    lines = [
        f"# target-python={provenance.target.python_tag} target-platform={provenance.target.platform_tag}",
        f"# trusted-manifest-sha256={provenance.manifest_digest}",
        "# offline-only: no network/index access during build",
    ]
    lines.extend(
        f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}"
        for item in sorted(provenance.wheels, key=lambda i: i["name"])
    )
    return "\n".join(lines) + "\n"


def record_prepackaging_provenance(
    wheelhouse: Path, target: WheelhouseTarget
) -> ResolverProvenance:
    """Record trusted selected wheel bytes, verified exactly against the checked-in manifest.

    This is the trust root for production staging: the downloaded wheel inventory
    MUST exactly equal the reviewed filenames/versions/sizes/SHA-256 recorded in
    the checked-in trusted manifest for the target, else pure post-download TOFU
    is refused with SubstrateError.
    """
    _, inventory = render_selected_wheel_lock(wheelhouse, target)
    manifest_entries = _manifest_by_filename()
    expected = {item["filename"]: item for item in manifest_entries.values()}
    actual = {item["filename"]: item for item in inventory}
    if set(actual) != set(expected):
        raise SubstrateError(
            "downloaded wheel inventory does not match checked-in trusted manifest: "
            f"missing={sorted(set(expected) - set(actual))}, "
            f"extra={sorted(set(actual) - set(expected))}"
        )
    for filename, item in sorted(actual.items()):
        rec = expected[filename]
        if (
            item["name"] != rec["name"].lower().replace("_", "-")
            or item["version"] != rec["version"]
            or item["sha256"] != rec["sha256"]
            or item["size_bytes"] != rec["size_bytes"]
        ):
            raise SubstrateError(
                f"wheel {filename!r} does not exactly match checked-in trusted manifest "
                f"(name/version/size/sha256 drift)"
            )
    return ResolverProvenance(
        target,
        trusted_wheel_manifest_digest(),
        trusted_wheel_manifest_source(),
        tuple(
            {
                "filename": item["filename"],
                "name": item["name"],
                "version": item["version"],
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
            }
            for item in sorted(inventory, key=lambda i: i["filename"])
        ),
    )
