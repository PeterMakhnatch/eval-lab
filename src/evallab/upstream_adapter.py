"""Offline, file-only boundaries for pinned upstream evaluation results."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse

ADAPTER_MANIFEST_VERSION = "adapter-manifest/v1"
OUTPUT_SCHEMA_VERSION = "external-evidence/v1"
ATIF_SCHEMA_VERSION = "ATIF-v1.7"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40,64}\Z")


class AdapterRefusal(ValueError):
    """A source, manifest, or file failed a declared adapter boundary."""


@dataclass(frozen=True)
class UpstreamSource:
    canonical_url: str
    revision: str
    license: str
    license_status: Literal["verified", "declared", "unknown"]
    status: Literal["active", "archived", "unknown"]


@dataclass(frozen=True)
class VersionedIO:
    format: str
    version: str


@dataclass(frozen=True)
class CompatibilityFixture:
    path: str
    digest: str


@dataclass(frozen=True)
class AdapterManifest:
    schema_version: str
    adapter_id: str
    upstream: UpstreamSource
    role: Literal["trajectory", "result"]
    capabilities: tuple[str, ...]
    isolation: Literal["file", "subprocess", "oci"]
    input: VersionedIO
    output: VersionedIO
    compatibility_fixture: CompatibilityFixture
    adapter_code_digest: str
    last_verified: str


@dataclass(frozen=True)
class ImportResult:
    destination: Path
    raw_path: Path
    atif_path: Path | None
    evidence_path: Path
    source_digest: str
    revision: str


_ALLOWED_CAPABILITIES = frozenset({"trajectory-events", "session-result", "reward"})
_EXGENTIC_FIELDS = frozenset(
    {"event", "step", "initial", "session_id", "task_id", "observation", "action"}
)
_RECOVERY_FIELDS = frozenset(
    {
        "id",
        "task_name",
        "trial_name",
        "agent_info",
        "verifier_result",
        "exception_info",
        "started_at",
        "finished_at",
    }
)

_INPUT_CONTRACTS = {
    ("exgentic-trajectory-jsonl", "v1"): (
        "trajectory",
        ("trajectory-events",),
        VersionedIO("atif+external-evidence", OUTPUT_SCHEMA_VERSION),
    ),
    ("recovery-bench-result-json", "v1"): (
        "result",
        ("reward", "session-result"),
        VersionedIO("external-evidence", OUTPUT_SCHEMA_VERSION),
    ),
}


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AdapterRefusal(f"{label} must be a JSON object with string keys")
    return value


def _keys(value: dict[str, Any], *, required: set[str], label: str) -> None:
    missing = sorted(required - value.keys())
    if missing:
        raise AdapterRefusal(f"{label} missing required fields: {', '.join(missing)}")


def _safe_relative(value: str, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise AdapterRefusal(f"{label} must be a non-empty repository-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise AdapterRefusal(f"{label} must be a repository-relative path")
    return path


def _source_file(path: Path, source_root: Path) -> Path:
    root = source_root.resolve(strict=True)
    if path.is_symlink():
        raise AdapterRefusal("source path must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise AdapterRefusal(f"source file does not exist: {path}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AdapterRefusal("source path escapes the declared source root") from exc
    if not resolved.is_file():
        raise AdapterRefusal("source path must be a regular file")
    return resolved


def _parse_manifest(payload: dict[str, Any]) -> AdapterManifest:
    _keys(
        payload,
        required={
            "schema_version",
            "adapter_id",
            "upstream",
            "role",
            "capabilities",
            "isolation",
            "input",
            "output",
            "compatibility_fixture",
            "adapter_code_digest",
            "last_verified",
        },
        label="adapter manifest",
    )
    if set(payload) != {
        "schema_version",
        "adapter_id",
        "upstream",
        "role",
        "capabilities",
        "isolation",
        "input",
        "output",
        "compatibility_fixture",
        "adapter_code_digest",
        "last_verified",
    }:
        raise AdapterRefusal("adapter manifest contains unknown fields")
    if not isinstance(payload["adapter_id"], str) or not payload["adapter_id"]:
        raise AdapterRefusal("adapter_id must be a non-empty string")
    upstream = _object(payload["upstream"], "upstream")
    _keys(
        upstream,
        required={"canonical_url", "revision", "license", "license_status", "status"},
        label="upstream",
    )
    if set(upstream) != {
        "canonical_url",
        "revision",
        "license",
        "license_status",
        "status",
    }:
        raise AdapterRefusal("upstream contains unknown fields")
    canonical_url = upstream["canonical_url"]
    parsed_url = urlparse(canonical_url if isinstance(canonical_url, str) else "")
    if (
        parsed_url.scheme != "https"
        or not parsed_url.netloc
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise AdapterRefusal("upstream canonical_url must be a canonical HTTPS URL")
    revision = upstream["revision"]
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise AdapterRefusal(
            "upstream revision must be an immutable 40-64 character lowercase hex id"
        )
    if upstream["license_status"] not in {"verified", "declared", "unknown"}:
        raise AdapterRefusal("unsupported upstream license_status")
    if upstream["status"] not in {"active", "archived", "unknown"}:
        raise AdapterRefusal("unsupported upstream status")
    if not isinstance(upstream["license"], str) or not upstream["license"].strip():
        raise AdapterRefusal("upstream license must be explicit")
    role = payload["role"]
    if role not in {"trajectory", "result"}:
        raise AdapterRefusal("unsupported adapter role")
    isolation = payload["isolation"]
    if isolation not in {"file", "subprocess", "oci"}:
        raise AdapterRefusal("unsupported isolation boundary")
    capabilities = payload["capabilities"]
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or not all(isinstance(item, str) for item in capabilities)
        or capabilities != sorted(set(capabilities))
        or not set(capabilities) <= _ALLOWED_CAPABILITIES
    ):
        raise AdapterRefusal("capabilities must be a sorted, unique supported list")
    io_values: dict[str, VersionedIO] = {}
    for name in ("input", "output"):
        value = _object(payload[name], name)
        if set(value) != {"format", "version"} or not all(
            isinstance(value[field], str) and value[field] for field in ("format", "version")
        ):
            raise AdapterRefusal(f"{name} must contain only non-empty format and version")
        io_values[name] = VersionedIO(**value)
    fixture = _object(payload["compatibility_fixture"], "compatibility_fixture")
    if set(fixture) != {"path", "digest"}:
        raise AdapterRefusal("compatibility_fixture must contain only path and digest")
    _safe_relative(fixture["path"], "compatibility fixture path")
    if not isinstance(fixture["digest"], str) or not _SHA256.fullmatch(fixture["digest"]):
        raise AdapterRefusal("compatibility fixture digest must be sha256:<lowercase hex>")
    code_digest = payload["adapter_code_digest"]
    if not isinstance(code_digest, str) or not _SHA256.fullmatch(code_digest):
        raise AdapterRefusal("adapter code digest must be sha256:<lowercase hex>")
    try:
        date.fromisoformat(payload["last_verified"])
    except (TypeError, ValueError) as exc:
        raise AdapterRefusal("last_verified must be an ISO calendar date") from exc
    if payload["schema_version"] != ADAPTER_MANIFEST_VERSION:
        raise AdapterRefusal(f"unsupported manifest schema: {payload['schema_version']!r}")
    contract = _INPUT_CONTRACTS.get(
        (io_values["input"].format, io_values["input"].version)
    )
    if contract is None:
        raise AdapterRefusal(
            f"incompatible input schema: {io_values['input'].format}@"
            f"{io_values['input'].version}"
        )
    expected_role, expected_capabilities, expected_output = contract
    if (
        role != expected_role
        or tuple(capabilities) != expected_capabilities
        or isolation != "file"
        or io_values["output"] != expected_output
    ):
        raise AdapterRefusal("adapter claims do not match the strict input contract")
    return AdapterManifest(
        schema_version=payload["schema_version"],
        adapter_id=payload["adapter_id"],
        upstream=UpstreamSource(**upstream),
        role=role,
        capabilities=tuple(capabilities),
        isolation=isolation,
        input=io_values["input"],
        output=io_values["output"],
        compatibility_fixture=CompatibilityFixture(**fixture),
        adapter_code_digest=code_digest,
        last_verified=payload["last_verified"],
    )


def load_adapter_manifest(manifest_path: Path, repo_root: Path) -> AdapterManifest:
    """Load a manifest and bind it to its fixture and the exact adapter code bytes."""
    manifest_file = _source_file(manifest_path, repo_root)
    try:
        payload = _object(json.loads(manifest_file.read_bytes()), "adapter manifest")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterRefusal(f"malformed adapter manifest: {exc}") from exc
    manifest = _parse_manifest(payload)
    root = repo_root.resolve(strict=True)
    fixture = root / _safe_relative(
        manifest.compatibility_fixture.path, "compatibility fixture path"
    )
    fixture_file = _source_file(fixture, root)
    actual_fixture_digest = _digest(fixture_file.read_bytes())
    if actual_fixture_digest != manifest.compatibility_fixture.digest:
        raise AdapterRefusal(
            "compatibility fixture drift: "
            f"expected {manifest.compatibility_fixture.digest}, got {actual_fixture_digest}"
        )
    code_file = Path(__file__).resolve(strict=True)
    actual_code_digest = _digest(code_file.read_bytes())
    if actual_code_digest != manifest.adapter_code_digest:
        raise AdapterRefusal(
            f"adapter code drift: expected {manifest.adapter_code_digest}, got {actual_code_digest}"
        )
    return manifest


def _exgentic(raw: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            raise AdapterRefusal(f"Exgentic JSONL line {number} is empty")
        try:
            record = _object(json.loads(line), f"Exgentic JSONL line {number}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterRefusal(f"malformed Exgentic JSONL line {number}: {exc}") from exc
        _keys(record, required={"event", "step", "session_id", "task_id"}, label=f"line {number}")
        if record["event"] not in {"action", "observation"}:
            raise AdapterRefusal(f"line {number} has unsupported event {record['event']!r}")
        if (
            not isinstance(record["step"], int)
            or isinstance(record["step"], bool)
            or record["step"] < 0
        ):
            raise AdapterRefusal(f"line {number} step must be a non-negative integer")
        records.append(record)
    if not records:
        raise AdapterRefusal("Exgentic JSONL must contain at least one record")
    for field in ("session_id", "task_id"):
        if not all(isinstance(record[field], str) and record[field] for record in records):
            raise AdapterRefusal(f"Exgentic {field} must be a non-empty string")
    session_ids = {record["session_id"] for record in records}
    task_ids = {record["task_id"] for record in records}
    if len(session_ids) != 1 or len(task_ids) != 1:
        raise AdapterRefusal("Exgentic records must have one session_id and task_id")
    steps_by_id: dict[int, dict[str, Any]] = {}
    for index, record in enumerate(records):
        step_id = record["step"]
        step = steps_by_id.setdefault(
            step_id,
            {
                "step_id": 0,
                "source": "user" if record.get("initial") is True else "agent",
                "message": "[unavailable: source event has no message field]",
            },
        )
        if record["event"] == "action":
            if "tool_calls" in step:
                raise AdapterRefusal(f"line {index + 1} duplicates an action for step {step_id}")
            action = _object(record.get("action"), f"line {index + 1} action")
            _keys(action, required={"name", "arguments"}, label=f"line {index + 1} action")
            if not isinstance(action["name"], str) or not action["name"]:
                raise AdapterRefusal(f"line {index + 1} action name must be non-empty")
            if not isinstance(action["arguments"], dict):
                raise AdapterRefusal(f"line {index + 1} action arguments must be an object")
            step["source"] = "agent"
            step["tool_calls"] = [
                {
                    "tool_call_id": f"unavailable-source-call-{index}",
                    "function_name": action["name"],
                    "arguments": action["arguments"],
                }
            ]
        else:
            if "observation" in step:
                raise AdapterRefusal(
                    f"line {index + 1} duplicates an observation for step {step_id}"
                )
            step["observation"] = {
                "results": [{"source_call_id": None, "content": record.get("observation")}]
            }
    steps = list(steps_by_id.values())
    for step_id, step in enumerate(steps, start=1):
        step["step_id"] = step_id
    trajectory = {
        "schema_version": ATIF_SCHEMA_VERSION,
        "session_id": next(iter(session_ids)),
        "agent": {"name": "[unavailable]", "version": "[unavailable]"},
        "steps": steps,
    }
    return trajectory, records


def _recovery(raw: bytes) -> tuple[None, list[dict[str, Any]]]:
    try:
        record = _object(json.loads(raw), "Recovery-Bench result")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterRefusal(f"malformed Recovery-Bench JSON result: {exc}") from exc
    _keys(
        record,
        required={"id", "task_name", "trial_name", "agent_info", "verifier_result"},
        label="Recovery-Bench result",
    )
    for field in ("id", "task_name", "trial_name"):
        if not isinstance(record[field], str) or not record[field]:
            raise AdapterRefusal(f"Recovery-Bench {field} must be a non-empty string")
    agent = _object(record["agent_info"], "Recovery-Bench agent_info")
    if not isinstance(agent.get("name"), str) or not agent["name"]:
        raise AdapterRefusal("Recovery-Bench agent_info.name must be non-empty")
    verifier = _object(record["verifier_result"], "Recovery-Bench verifier_result")
    _object(verifier.get("rewards"), "Recovery-Bench verifier_result.rewards")
    return None, [record]


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def import_upstream_file(
    source_path: Path,
    destination: Path,
    manifest_path: Path,
    repo_root: Path,
    *,
    source_root: Path,
    source_revision: str,
    accepted_licenses: frozenset[str],
) -> ImportResult:
    """Convert a pinned local result without importing or executing upstream code."""
    manifest = load_adapter_manifest(manifest_path, repo_root)
    if manifest.isolation != "file":
        raise AdapterRefusal("this importer accepts file-isolated adapters only")
    if source_revision != manifest.upstream.revision:
        raise AdapterRefusal(
            "source revision mismatch: "
            f"expected {manifest.upstream.revision}, got {source_revision}"
        )
    if manifest.upstream.license not in accepted_licenses:
        raise AdapterRefusal(f"upstream license not accepted: {manifest.upstream.license}")
    source = _source_file(source_path, source_root)
    source_tree = source_root.resolve(strict=True)
    if destination.is_symlink():
        raise AdapterRefusal("destination path must not be a symlink")
    destination = destination.resolve()
    if (
        destination == source_tree
        or source_tree in destination.parents
        or destination in source_tree.parents
    ):
        raise AdapterRefusal("destination must not overlap the declared source tree")
    for child in ("raw", "atif"):
        if (destination / child).is_symlink():
            raise AdapterRefusal(f"destination {child} path must not be a symlink")
    raw = source.read_bytes()
    source_digest = _digest(raw)
    if manifest.input == VersionedIO("exgentic-trajectory-jsonl", "v1"):
        trajectory, records = _exgentic(raw)
        known_fields = _EXGENTIC_FIELDS
        mapped_to_atif = [
            "action.arguments",
            "action.name",
            "event",
            "initial",
            "observation",
            "session_id",
            "step (grouping only; ATIF step_id is 1-based output order)",
        ]
        excluded_from_atif = [
            "task_id",
            "observation on action events",
            "action on observation events",
        ]
        required_unavailable_markers = [
            "agent.name",
            "agent.version",
            "steps[].message",
            "steps[].tool_calls[].tool_call_id",
        ]
        raw_name = "trajectory.jsonl"
    elif manifest.input == VersionedIO("recovery-bench-result-json", "v1"):
        trajectory, records = _recovery(raw)
        known_fields = _RECOVERY_FIELDS
        mapped_to_atif = []
        excluded_from_atif = [
            "agent_info",
            "exception_info",
            "finished_at",
            "id",
            "started_at",
            "task_name",
            "trial_name",
            "verifier_result",
        ]
        required_unavailable_markers = []
        raw_name = "result.json"
    else:
        raise AdapterRefusal(
            f"incompatible input schema: {manifest.input.format}@{manifest.input.version}"
        )
    unknown = [
        {key: record[key] for key in sorted(record.keys() - known_fields)}
        for record in records
    ]
    evidence = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "adapter": {
            "id": manifest.adapter_id,
            "manifest_digest": _digest(manifest_path.resolve(strict=True).read_bytes()),
            "manifest_version": manifest.schema_version,
            "code_digest": manifest.adapter_code_digest,
            "role": manifest.role,
            "capabilities": list(manifest.capabilities),
            "isolation": manifest.isolation,
            "input": asdict(manifest.input),
            "output": asdict(manifest.output),
            "last_verified": manifest.last_verified,
        },
        "source": {
            **asdict(manifest.upstream),
            "raw_digest": source_digest,
            "raw_path": f"raw/{raw_name}",
        },
        "trajectory": (
            None
            if trajectory is None
            else {"path": "atif/trajectory.json", "schema_version": ATIF_SCHEMA_VERSION}
        ),
        "mapping": {
            "mapped_to_atif": mapped_to_atif,
            "excluded_from_atif": excluded_from_atif,
            "required_unavailable_markers": required_unavailable_markers,
            "unknown_field_policy": "retained-in-evidence-not-mapped-to-atif",
            "unknown_fields_by_record": unknown,
        },
        "observed_records": records,
    }
    _write_atomic(destination / "raw" / raw_name, raw)
    atif_path = (
        None if trajectory is None else destination / "atif" / "trajectory.json"
    )
    evidence_path = destination / "external-evidence.json"
    if atif_path is not None:
        _write_atomic(atif_path, _canonical(trajectory))
    _write_atomic(evidence_path, _canonical(evidence))
    return ImportResult(
        destination=destination,
        raw_path=destination / "raw" / raw_name,
        atif_path=atif_path,
        evidence_path=evidence_path,
        source_digest=source_digest,
        revision=source_revision,
    )
