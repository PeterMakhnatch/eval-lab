"""Focused regressions for the canonical MCP supply-chain trust roots.

Covers: checked-in trusted wheel manifest integrity/tamper, TOFU/poisoned-index
refusal, dependency drift, unknown/extra wheel, mcp-tool-event-v1 error
outcomes, offline-build-proof server/tool/event/manifest fields, and exact
Workbench server-bytes validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.mcp_substrate import (
    DEFAULT_TARGET_PLATFORM_TAG,
    DEFAULT_TARGET_PYTHON_TAG,
    MCP_TOOL_EVENT_SCHEMA_VERSION,
    TRUSTED_WHEEL_MANIFEST_PATH,
    MCPToolDefinition,
    MCPToolParameter,
    RuntimeAsset,
    SubstrateError,
    WheelhouseTarget,
    compute_tool_definitions_sha256,
    generate_fastmcp_server_script,
    load_trusted_wheel_manifest,
    materialize_mcp_sidecar_package,
    record_prepackaging_provenance,
    trusted_wheel_manifest_digest,
    trusted_wheel_manifest_source,
    verify_provenance_wheelhouse,
)

REAL_WHEELHOUSE = Path("/tmp/fastmcp3_wheelhouse")
TARGET = WheelhouseTarget(DEFAULT_TARGET_PYTHON_TAG, DEFAULT_TARGET_PLATFORM_TAG)


def _real_wheelhouse_or_skip() -> Path:
    if not REAL_WHEELHOUSE.is_dir():
        pytest.skip("FastMCP 3.4.7 trusted wheelhouse not populated on this host")
    return REAL_WHEELHOUSE


def _simple_tool() -> MCPToolDefinition:
    return MCPToolDefinition(
        name="add",
        description="Add",
        parameters=(MCPToolParameter(name="a", type_name="int", description="a"),),
        execution_body="return {'status': 'ok', 'value': a}",
    )


def test_trusted_manifest_integrity():
    manifest = load_trusted_wheel_manifest()
    assert manifest["target"]["python_tag"] == "cp312"
    assert manifest["target"]["platform_tag"] == "manylinux_2_17_x86_64"
    assert manifest["source"] == "https://pypi.org/simple"
    assert manifest["fastmcp_version"] == "3.4.7"
    assert len(manifest["wheels"]) == 66
    for entry in manifest["wheels"]:
        assert set(entry) == {"filename", "name", "version", "size_bytes", "sha256"}
        assert len(entry["sha256"]) == 64
        assert entry["size_bytes"] > 0
        assert entry["filename"].endswith(".whl")
    # Digest is stable
    assert trusted_wheel_manifest_digest() == trusted_wheel_manifest_digest()
    # Source matches the trusted source helper
    assert trusted_wheel_manifest_source() == "https://pypi.org/simple"


def test_trusted_manifest_rejects_tamper_and_duplicate(tmp_path: Path):
    original = TRUSTED_WHEEL_MANIFEST_PATH.read_bytes()
    try:
        # Tamper sha256 -> invalid (non-hex)
        manifest = json.loads(original)
        manifest["wheels"][0]["sha256"] = "g" * 64
        TRUSTED_WHEEL_MANIFEST_PATH.write_text(json.dumps(manifest))
        with pytest.raises(SubstrateError, match="sha256 is invalid"):
            load_trusted_wheel_manifest()

        # Tamper size_bytes -> invalid (non-positive)
        manifest = json.loads(original)
        manifest["wheels"][0]["size_bytes"] = -5
        TRUSTED_WHEEL_MANIFEST_PATH.write_text(json.dumps(manifest))
        with pytest.raises(SubstrateError, match="size_bytes"):
            load_trusted_wheel_manifest()

        # Duplicate filename -> invalid
        manifest = json.loads(original)
        manifest["wheels"].append(dict(manifest["wheels"][0]))
        TRUSTED_WHEEL_MANIFEST_PATH.write_text(json.dumps(manifest))
        with pytest.raises(SubstrateError, match="duplicate wheel filename"):
            load_trusted_wheel_manifest()

        # Wrong target -> invalid
        manifest = json.loads(original)
        manifest["target"]["platform_tag"] = "macosx_11_0_arm64"
        TRUSTED_WHEEL_MANIFEST_PATH.write_text(json.dumps(manifest))
        with pytest.raises(SubstrateError, match="does not match"):
            load_trusted_wheel_manifest()
    finally:
        TRUSTED_WHEEL_MANIFEST_PATH.write_bytes(original)


def test_tofu_refusal_unknown_and_extra_wheel(tmp_path: Path):
    import zipfile

    wheelhouse = _real_wheelhouse_or_skip()
    # Copy the exact set, then add an unknown wheel -> provenance must refuse
    drifted = tmp_path / "drifted"
    drifted.mkdir()
    for w in wheelhouse.glob("*.whl"):
        (drifted / w.name).write_bytes(w.read_bytes())
    evil = drifted / "evil-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr(
            "evil-1.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: evil\nVersion: 1.0.0\n"
        )
    with pytest.raises(SubstrateError, match="extra="):
        record_prepackaging_provenance(drifted, TARGET)


def test_tofu_refusal_missing_wheel(tmp_path: Path):
    wheelhouse = _real_wheelhouse_or_skip()
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    wheels = sorted(wheelhouse.glob("*.whl"))
    for w in wheels[1:]:
        (incomplete / w.name).write_bytes(w.read_bytes())
    with pytest.raises(SubstrateError, match="missing="):
        record_prepackaging_provenance(incomplete, TARGET)


def test_dependency_drift_rejected(tmp_path: Path):
    wheelhouse = _real_wheelhouse_or_skip()
    drifted = tmp_path / "drift"
    drifted.mkdir()
    for w in wheelhouse.glob("*.whl"):
        (drifted / w.name).write_bytes(w.read_bytes())
    # Tamper the pydantic wheel bytes -> sha256 drift
    target = next(drifted.glob("pydantic-*.whl"))
    target.write_bytes(target.read_bytes() + b"\x00")
    with pytest.raises(SubstrateError, match="drift"):
        record_prepackaging_provenance(drifted, TARGET)


def test_provenance_binds_checked_in_manifest(tmp_path: Path):
    wheelhouse = _real_wheelhouse_or_skip()
    prov = record_prepackaging_provenance(wheelhouse, TARGET)
    assert prov.manifest_digest == trusted_wheel_manifest_digest()
    assert prov.manifest_source == trusted_wheel_manifest_source()
    # from_json round-trips and requires the manifest binding
    prov2 = type(prov).from_json(prov.to_dict())
    assert prov2.manifest_digest == trusted_wheel_manifest_digest()
    # A syntactically valid but non-trusted manifest_digest is refused at verification
    bad = type(prov)(prov.target, "f" * 64, prov.manifest_source, prov.wheels)
    with pytest.raises(SubstrateError, match="manifest_digest does not match"):
        verify_provenance_wheelhouse(wheelhouse, bad)
    # A malformed digest (non-hex) is refused at parse time
    malformed = prov.to_dict()
    malformed["manifest_digest"] = "g" * 64
    with pytest.raises(SubstrateError):
        type(prov).from_json(malformed)


def test_verify_provenance_refuses_wrong_manifest_binding(tmp_path: Path):
    wheelhouse = _real_wheelhouse_or_skip()
    prov = record_prepackaging_provenance(wheelhouse, TARGET)
    staged = tmp_path / "staged"
    from evallab.mcp_substrate import stage_platform_wheelhouse

    stage_platform_wheelhouse(wheelhouse, staged, TARGET)
    # Same bytes, but a provenance bound to a different manifest digest must refuse
    bad = type(prov)(prov.target, "0" * 64, prov.manifest_source, prov.wheels)
    with pytest.raises(SubstrateError, match="manifest_digest does not match"):
        verify_provenance_wheelhouse(staged, bad)


def test_verify_provenance_rejects_forged_records_with_valid_manifest_digest(tmp_path: Path):
    """P1 regression: provenance with correct manifest digest/source but substituted
    wheel records must be rejected — a forged resolver-provenance must not pass."""
    wheelhouse = _real_wheelhouse_or_skip()
    prov = record_prepackaging_provenance(wheelhouse, TARGET)
    staged = tmp_path / "staged"
    from evallab.mcp_substrate import stage_platform_wheelhouse

    stage_platform_wheelhouse(wheelhouse, staged, TARGET)
    # Forge: keep the true manifest digest/source but swap one wheel's records
    forged_wheels = list(prov.wheels)
    swapped = dict(forged_wheels[0])
    swapped["sha256"] = "f" * 64
    swapped["name"] = "evil"
    swapped["version"] = "9.9.9"
    forged_wheels[0] = swapped
    forged = type(prov)(prov.target, prov.manifest_digest, prov.manifest_source, tuple(forged_wheels))
    with pytest.raises(SubstrateError, match="do not exactly match checked-in trusted manifest"):
        verify_provenance_wheelhouse(staged, forged)



    code = generate_fastmcp_server_script([_simple_tool()])
    assert f'EVENT_SCHEMA_VERSION = "{MCP_TOOL_EVENT_SCHEMA_VERSION}"' in code
    assert "tool_call_error" in code
    assert "tool_call_success" in code
    assert "is_error" in code
    assert "class EventJournalMiddleware" in code
    assert "mcp.add_middleware" in code
    # Gold-label is_distractor must NOT appear in the agent-readable canonical events
    assert "is_distractor" not in code
    assert "DISTRACTOR_TOOLS" not in code
    # No backward-compat shim
    assert "def log_tool_event" not in code


def test_tool_definitions_digest_binds_schema_and_event_version():
    t1 = _simple_tool()
    t2 = MCPToolDefinition(
        name="add",
        description="Add",
        parameters=(MCPToolParameter(name="a", type_name="int", description="a"),),
        execution_body="return {'status': 'ok', 'value': a + 1}",
    )
    assert compute_tool_definitions_sha256([t1]) != compute_tool_definitions_sha256([t2])
    assert compute_tool_definitions_sha256([t1], "ops") != compute_tool_definitions_sha256([t1])
    # Op-registry binding sensitivity
    assert compute_tool_definitions_sha256([t1], "ops_a") != compute_tool_definitions_sha256(
        [t1], "ops_b"
    )


def test_offline_proof_binds_server_tool_event_and_manifest(tmp_path: Path):
    wheelhouse = _real_wheelhouse_or_skip()
    prov = record_prepackaging_provenance(wheelhouse, TARGET)
    sidecar = tmp_path / "mcp-server"
    materialize_mcp_sidecar_package(
        target_dir=sidecar,
        tools=[_simple_tool()],
        wheelhouse_source=wheelhouse,
        resolver_provenance=prov,
        plan_only=False,
    )
    proof = json.loads((sidecar / "offline-build-proof.json").read_text())
    server_bytes = (sidecar / "server.py").read_bytes()
    from evallab.benchmark_program_contracts import compute_sha256

    assert proof["mode"] == "complete_offline_package"
    assert proof["server_sha256"] == compute_sha256(server_bytes)
    assert proof["server_size_bytes"] == len(server_bytes)
    assert proof["event_schema_version"] == MCP_TOOL_EVENT_SCHEMA_VERSION
    assert proof["trusted_manifest_digest"] == trusted_wheel_manifest_digest()
    assert proof["trusted_manifest_source"] == trusted_wheel_manifest_source()
    assert len(proof["tool_definitions_sha256"]) == 64


def test_plan_only_proof_binds_tool_defs_and_manifest(tmp_path: Path):
    sidecar = tmp_path / "plan_only"
    materialize_mcp_sidecar_package(target_dir=sidecar, tools=[_simple_tool()], plan_only=True)
    proof = json.loads((sidecar / "offline-build-proof.json").read_text())
    assert proof["mode"] == "plan_only"
    assert proof["event_schema_version"] == MCP_TOOL_EVENT_SCHEMA_VERSION
    assert len(proof["tool_definitions_sha256"]) == 64
    assert proof["trusted_manifest_digest"] == trusted_wheel_manifest_digest()
    assert proof["trusted_manifest_source"] == trusted_wheel_manifest_source()


def test_workbench_rejects_tampered_server_bytes_and_missing_fields(tmp_path: Path):

    import yaml

    from evallab.task_workbench import (
        _validate_compose_topology,
        _validate_offline_build_proofs,
    )

    wheelhouse = _real_wheelhouse_or_skip()
    prov = record_prepackaging_provenance(wheelhouse, TARGET)
    env_dir = tmp_path / "environment"
    env_dir.mkdir()
    (env_dir / "Dockerfile").write_text(
        "FROM python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f\n",
        encoding="utf-8",
    )
    sidecar = env_dir / "mcp-server"
    ops = tmp_path / "ops.py"
    ops.write_text("OP_REGISTRY = {}\n", encoding="utf-8")
    tool = MCPToolDefinition(
        name="ping", description="ping", parameters=(), metadata={"op_kind": "ping"}
    )
    pkg = materialize_mcp_sidecar_package(
        target_dir=sidecar,
        tools=[tool],
        wheelhouse_source=wheelhouse,
        resolver_provenance=prov,
        op_registry_module="ops",
        runtime_assets=(RuntimeAsset("ops.py", ops),),
        plan_only=False,
    )
    (env_dir / "docker-compose.yaml").write_text(yaml.dump(pkg["compose_doc"]), encoding="utf-8")
    compose_topology, _ = _validate_compose_topology(tmp_path, [])
    proof_path = sidecar / "offline-build-proof.json"
    raw = json.loads(proof_path.read_text())

    def _revalidate() -> list:
        d = []
        _validate_offline_build_proofs(tmp_path, d, compose_topology=compose_topology)
        return d

    # Baseline: zero diagnostics
    assert _revalidate() == []

    # 1. Tampered server.py bytes -> digest/size mismatch
    server_path = sidecar / "server.py"
    server_path.write_text(server_path.read_text() + "\n# tampered\n", encoding="utf-8")
    d = _revalidate()
    assert any("server.py" in x.message for x in d)
    # restore
    from evallab.mcp_substrate import generate_fastmcp_server_script

    server_path.write_text(
        generate_fastmcp_server_script([tool], op_registry_module="ops"), encoding="utf-8"
    )
    raw["server_size_bytes"] = len(server_path.read_bytes())
    proof_path.write_text(json.dumps(raw))
    assert _revalidate() == []

    # 2. server.py replaced by a symlink
    server_path.unlink()
    server_path.symlink_to(ops)
    d = _revalidate()
    assert any("server.py" in x.message for x in d)
    server_path.unlink()
    server_path.write_text(
        generate_fastmcp_server_script([tool], op_registry_module="ops"), encoding="utf-8"
    )

    # 3. Missing event_schema_version
    p = json.loads(proof_path.read_text())
    del p["event_schema_version"]
    proof_path.write_text(json.dumps(p))
    d = _revalidate()
    assert any("event_schema_version" in x.message for x in d)
    proof_path.write_text(json.dumps(raw))

    # 4. Missing tool_definitions_sha256
    p = dict(raw)
    del p["tool_definitions_sha256"]
    proof_path.write_text(json.dumps(p))
    d = _revalidate()
    assert any("tool_definitions_sha256" in x.message for x in d)
    proof_path.write_text(json.dumps(raw))

    # 5. Wrong trusted_manifest_digest
    p = dict(raw)
    p["trusted_manifest_digest"] = "0" * 64
    proof_path.write_text(json.dumps(p))
    d = _revalidate()
    assert any("trusted_manifest_digest" in x.message for x in d)
