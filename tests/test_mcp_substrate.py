from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from evallab.benchmark_program_contracts import (
    FaultClass,
    FaultInjectionRecord,
    compute_sha256,
)
from evallab.mcp_substrate import (
    DEFAULT_PINNED_BASE_IMAGE,
    DEFAULT_TARGET_PLATFORM_TAG,
    DEFAULT_TARGET_PYTHON_TAG,
    FASTMCP_VERSION_CONSTRAINTS,
    PINNED_BASE_IMAGE_AMD64_MANIFEST_DIGEST,
    PINNED_BASE_IMAGE_INDEX_DIGEST,
    MCPToolDefinition,
    MCPToolParameter,
    RuntimeAsset,
    SubstrateError,
    WheelhouseTarget,
    compute_mcp_substrate_digest,
    generate_fastmcp_server_script,
    materialize_mcp_sidecar_package,
    record_prepackaging_provenance,
    render_mcp_compose_document,
    render_mcp_sidecar_dockerfile,
    render_selected_wheel_lock,
    validate_mcp_compose_document,
    validate_target_base_runtime,
)
from evallab.task_workbench import _validate_compose_topology


def test_mcp_compose_document_rendering_and_validation():
    doc = render_mcp_compose_document(
        sidecar_service="mcp-service",
        volume_name="evidence-volume",
        volume_mount="/app/output",
    )
    valid, errors = validate_mcp_compose_document(doc)
    assert valid, f"Validation failed: {errors}"
    assert "main" in doc["services"]
    assert "mcp-service" in doc["services"]
    assert "evidence-volume" in doc["volumes"]
    assert doc["services"]["main"]["volumes"] == ["evidence-volume:/app/output:ro"]
    assert doc["services"]["mcp-service"]["volumes"] == ["evidence-volume:/app/output:rw"]
    assert doc["networks"]["workbench-internal"] == {"internal": True}
    assert doc["services"]["main"]["networks"] == ["workbench-internal"]
    assert doc["services"]["mcp-service"]["networks"] == ["workbench-internal"]


def test_mcp_substrate_workbench_v2_integration_acceptance(tmp_path: Path):
    """Integration test proving materialize_mcp_sidecar_package's rendered Compose topology is accepted by updated task_workbench."""
    resolved_root = tmp_path.resolve()
    env_dir = resolved_root / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "Dockerfile").write_text(
        f"FROM {DEFAULT_PINNED_BASE_IMAGE}\nWORKDIR /app\n", encoding="utf-8"
    )

    sidecar_dir = env_dir / "mcp-server"
    tool = MCPToolDefinition(
        name="test_tool",
        description="A test tool",
        parameters=(MCPToolParameter(name="x", type_name="int", description="val"),),
    )
    pkg = materialize_mcp_sidecar_package(
        target_dir=sidecar_dir,
        tools=[tool],
        plan_only=True,
    )
    (sidecar_dir / "Dockerfile").write_text(f"FROM {DEFAULT_PINNED_BASE_IMAGE}\n", encoding="utf-8")

    (env_dir / "docker-compose.yaml").write_text(yaml.dump(pkg["compose_doc"]), encoding="utf-8")

    diagnostics: list[Any] = []
    topology, sidecar_name = _validate_compose_topology(resolved_root, diagnostics)
    assert sidecar_name == "mcp-service"
    assert topology is not None
    assert len(diagnostics) == 0, f"task_workbench emitted unexpected diagnostics: {diagnostics}"
    assert topology["sidecar_service"] == "mcp-service"
    assert topology["volume"]["name"] == "evidence-volume"


def test_mcp_sidecar_package_materialization_and_fail_closed_validation(tmp_path: Path):
    """Test boring task-authoring API emitting complete offline package and rejecting invalid/tampered wheelhouses."""
    wheelhouse = Path("/tmp/fastmcp3_wheelhouse")
    if not wheelhouse.is_dir():
        pytest.skip("FastMCP 3.4.7 wheelhouse not populated on this host")

    tool = MCPToolDefinition(
        name="calculate_sum",
        description="Compute sum of two integers",
        parameters=(
            MCPToolParameter(name="x", type_name="int", description="First number"),
            MCPToolParameter(name="y", type_name="int", description="Second number"),
        ),
        metadata={"op_kind": "add_op"},
    )

    # 1. Production mode without wheelhouse fails closed
    with pytest.raises(SubstrateError, match="wheelhouse_source is mandatory"):
        materialize_mcp_sidecar_package(
            target_dir=tmp_path / "fail_no_wheelhouse",
            tools=[tool],
            wheelhouse_source=None,
            plan_only=False,
        )

    # 2. Plan-only mode succeeds without Dockerfile or wheelhouse
    plan_dir = tmp_path / "plan_only_pkg"
    _ = materialize_mcp_sidecar_package(
        target_dir=plan_dir,
        tools=[tool],
        wheelhouse_source=None,
        plan_only=True,
    )
    assert (plan_dir / "server.py").is_file()
    assert (plan_dir / "requirements.txt").is_file()
    assert not (plan_dir / "Dockerfile").exists()
    assert not (plan_dir / "wheelhouse").exists()
    plan_proof = json.loads((plan_dir / "offline-build-proof.json").read_text())
    assert plan_proof["mode"] == "plan_only"

    # 3. Incomplete wheelhouse fails closed
    incomplete_wheelhouse = tmp_path / "incomplete_wheelhouse"
    incomplete_wheelhouse.mkdir()
    first_wheel = next(wheelhouse.glob("*.whl"))
    shutil.copy2(first_wheel, incomplete_wheelhouse / first_wheel.name)
    with pytest.raises(SubstrateError, match="resolver_provenance is mandatory"):
        materialize_mcp_sidecar_package(
            target_dir=tmp_path / "fail_incomplete",
            tools=[tool],
            wheelhouse_source=incomplete_wheelhouse,
        )

    # 4. Host macosx target is incompatible with the pinned Linux 3.12 base.
    with pytest.raises(SubstrateError, match="platform tag"):
        materialize_mcp_sidecar_package(
            target_dir=tmp_path / "fail_macosx_target",
            tools=[tool],
            wheelhouse_source=wheelhouse,
            target=WheelhouseTarget("cp312", "macosx_11_0_arm64"),
            resolver_provenance=record_prepackaging_provenance(
                wheelhouse, WheelhouseTarget("cp312", "macosx_11_0_arm64")
            ),
        )

    linux_target = WheelhouseTarget(DEFAULT_TARGET_PYTHON_TAG, DEFAULT_TARGET_PLATFORM_TAG)
    try:
        provenance = record_prepackaging_provenance(wheelhouse, linux_target)
    except SubstrateError:
        pytest.skip("Linux CPython 3.12 manylinux wheelhouse not populated on this host")
    prod_dir = tmp_path / "prod_pkg"
    prod_pkg = materialize_mcp_sidecar_package(
        target_dir=prod_dir,
        tools=[tool],
        server_name="math-sidecar",
        port=8080,
        wheelhouse_source=wheelhouse,
        target=provenance.target,
        resolver_provenance=provenance,
    )
    assert (prod_dir / "server.py").is_file()
    assert (prod_dir / "requirements.txt").is_file()
    assert (prod_dir / "Dockerfile").is_file()
    assert (prod_dir / "wheelhouse").is_dir()
    assert len(list((prod_dir / "wheelhouse").glob("*.whl"))) > 0
    assert len(list((prod_dir / "wheelhouse").glob("*.whl"))) < len(list(wheelhouse.glob("*.whl")))
    prod_proof = json.loads((prod_dir / "offline-build-proof.json").read_text())
    assert prod_proof["mode"] == "complete_offline_package"
    assert prod_proof["wheel_count"] > 0

    valid, errs = validate_mcp_compose_document(prod_pkg["compose_doc"])
    assert valid, f"Generated Compose invalid: {errs}"


def test_fastmcp_script_generation_and_version_constraints():
    tool = MCPToolDefinition(
        name="calculate_sum",
        description="Compute sum",
        parameters=(MCPToolParameter(name="x", type_name="int", description="x"),),
    )
    script = generate_fastmcp_server_script([tool], server_name="test-server", port=8080)
    assert "from fastmcp import FastMCP" in script
    assert 'mcp.run(transport="streamable-http"' in script
    assert "fastmcp==3.4.7" in FASTMCP_VERSION_CONSTRAINTS


def test_linux_cp312_manylinux_cffi_hash_is_selected_from_wheel_bytes(tmp_path: Path):
    """Regression: selected Linux CPython 3.12 cffi wheel must write its real c1453022 hash into the lock."""
    cffi_wheel = next(
        Path("/tmp/mcp-linux-cffi").glob("cffi-2.1.1-cp312-cp312-manylinux*.whl"), None
    )
    fastmcp_wheel = next(
        Path("/tmp/fastmcp3_wheelhouse").glob("fastmcp-3.4.7-py3-none-any.whl"), None
    )
    if cffi_wheel is None or fastmcp_wheel is None:
        pytest.skip("Linux cffi or FastMCP selected-wheel fixtures are unavailable")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    shutil.copy2(cffi_wheel, wheelhouse / cffi_wheel.name)
    shutil.copy2(fastmcp_wheel, wheelhouse / fastmcp_wheel.name)
    lock, inventory = render_selected_wheel_lock(
        wheelhouse, WheelhouseTarget("cp312", "manylinux_2_17_x86_64")
    )
    assert (
        "cffi==2.1.1 --hash=sha256:c1453022f490d2459a11819d83ad1d586e9ff65a12ac3e705ffebd46d3685dcf"
        in lock
    )
    assert any(
        item["sha256"] == "c1453022f490d2459a11819d83ad1d586e9ff65a12ac3e705ffebd46d3685dcf"
        for item in inventory
    )


def test_selected_platform_lock_installs_offline(tmp_path: Path):
    wheelhouse = Path("/tmp/fastmcp3_wheelhouse")
    if not wheelhouse.is_dir():
        pytest.skip("selected wheelhouse unavailable")
    from evallab.mcp_substrate import WheelhouseTarget, stage_platform_wheelhouse

    staged = tmp_path / "wheelhouse"
    lock, _ = stage_platform_wheelhouse(
        wheelhouse, staged, WheelhouseTarget("cp312", "macosx_11_0_arm64")
    )
    lock_path = tmp_path / "requirements.txt"
    lock_path.write_text(lock)
    result = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--no-index",
            "--find-links",
            str(staged),
            "--require-hashes",
            "-r",
            str(lock_path),
            "--target",
            str(tmp_path / "target"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_real_fastmcp_generated_script_and_client_e2e(tmp_path: Path):
    """End-to-end smoke test exercising generated FastMCP script with real FastMCP engine and MCP ClientSession."""
    target_env = Path("/tmp/test_fastmcp3_env")
    if not (target_env / "fastmcp").is_dir():
        pytest.skip("Isolated fastmcp environment not available at /tmp/test_fastmcp3_env")

    evidence_file = tmp_path / "e2e-events.jsonl"
    tool1 = MCPToolDefinition(
        name="multiply_values",
        description="Multiply two integers",
        parameters=(
            MCPToolParameter(name="a", type_name="int", description="first operand"),
            MCPToolParameter(name="b", type_name="int", description="second operand"),
        ),
        metadata={"op_kind": "multiply"},
        execution_body="""val = a * b
res = {"status": "ok", "value": val}
log_tool_event("multiply_values", args, res, is_distractor=False)
return res""",
    )

    # Render server script using generated code path
    server_script_code = generate_fastmcp_server_script(
        [tool1],
        server_name="test-real-fastmcp",
        port=8588,
        evidence_path=str(evidence_file),
    )
    server_file = tmp_path / "server.py"
    server_file.write_text(server_script_code)

    proc = subprocess.Popen(
        [sys.executable, str(server_file)],
        env={"PYTHONPATH": str(target_env)},
    )
    time.sleep(2)

    async def _exercise_real_mcp():
        sys.path.insert(0, str(target_env))
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            streamable_http_client("http://127.0.0.1:8588/mcp") as (read, write, _),
            ClientSession(read, write) as session,
        ):
            init_res = await session.initialize()
            assert init_res.serverInfo.name == "test-real-fastmcp"

            tools_res = await session.list_tools()
            assert len(tools_res.tools) == 1
            assert tools_res.tools[0].name == "multiply_values"

            call_res = await session.call_tool("multiply_values", arguments={"a": 7, "b": 6})
            assert call_res.isError is False
            assert len(call_res.content) > 0
            assert (
                '"value":42' in call_res.content[0].text
                or '"value": 42' in call_res.content[0].text
            )

    try:
        asyncio.run(_exercise_real_mcp())
    finally:
        proc.terminate()
        proc.wait()

    # Assert that event ledger was logged by the real FastMCP tool execution
    assert evidence_file.is_file(), "Evidence state journal was not created by tool execution"
    events = [json.loads(line) for line in evidence_file.read_text().splitlines() if line.strip()]
    assert len(events) == 1
    assert events[0]["tool_name"] == "multiply_values"
    assert events[0]["result"]["value"] == 42


def test_real_fastmcp_fault_injection_and_client_recovery_e2e(tmp_path: Path):
    """End-to-end smoke test exercising generated FastMCP script with deterministic fault injection."""
    target_env = Path("/tmp/test_fastmcp3_env")
    if not (target_env / "fastmcp").is_dir():
        pytest.skip("Isolated fastmcp environment not available at /tmp/test_fastmcp3_env")

    evidence_file = tmp_path / "fault-events.jsonl"
    fault_record = FaultInjectionRecord(
        fault_id="a" * 64,
        task_id="task_e2e_f",
        twin_task_id="twin_e2e_f",
        target_tool="db_query",
        fault_class=FaultClass.SILENT_WRONG_PAYLOAD,
        target_canonical_event_ordinal=1,
        injection_payload={"corrupted_value": "CORRUPTED_E2E"},
        recovery_contract="detect_silent_corruption",
        verifier_oracle_digest="b" * 64,
    )

    tool = MCPToolDefinition(
        name="db_query",
        description="Query database",
        parameters=(MCPToolParameter(name="key", type_name="str", description="key"),),
        metadata={"op_kind": "db"},
    )

    server_script_code = generate_fastmcp_server_script(
        [tool],
        server_name="test-fault-fastmcp",
        port=8589,
        evidence_path=str(evidence_file),
        fault_record=fault_record,
    )
    server_file = tmp_path / "server_fault.py"
    server_file.write_text(server_script_code)

    proc = subprocess.Popen(
        [sys.executable, str(server_file)],
        env={"PYTHONPATH": str(target_env)},
    )
    time.sleep(2)

    async def _exercise_fault():
        sys.path.insert(0, str(target_env))
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            streamable_http_client("http://127.0.0.1:8589/mcp") as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            # Call 1 -> Injected silent wrong payload
            call1 = await session.call_tool("db_query", arguments={"key": "k1"})
            assert "CORRUPTED_E2E" in call1.content[0].text

            # Call 2 -> Normal execution
            call2 = await session.call_tool("db_query", arguments={"key": "k1"})
            assert "CORRUPTED_E2E" not in call2.content[0].text

    try:
        asyncio.run(_exercise_fault())
    finally:
        proc.terminate()
        proc.wait()

    assert evidence_file.is_file()
    events = [json.loads(line) for line in evidence_file.read_text().splitlines() if line.strip()]
    assert len(events) >= 1
    assert events[0]["result"]["value"] == "CORRUPTED_E2E"


def test_mcp_substrate_digest_sensitivity_to_metadata_and_body():
    doc = render_mcp_compose_document()
    tool1 = MCPToolDefinition(
        name="tool_a",
        description="Tool A",
        parameters=(MCPToolParameter(name="x", type_name="int", description="x"),),
        metadata={"op_kind": "op_v1"},
        execution_body="return {'x': x}",
    )
    tool2 = MCPToolDefinition(
        name="tool_a",
        description="Tool A",
        parameters=(MCPToolParameter(name="x", type_name="int", description="x"),),
        metadata={"op_kind": "op_v2"},
        execution_body="return {'x': x}",
    )
    tool3 = MCPToolDefinition(
        name="tool_a",
        description="Tool A",
        parameters=(MCPToolParameter(name="x", type_name="int", description="x"),),
        metadata={"op_kind": "op_v1"},
        execution_body="return {'x': x + 1}",
    )

    d1 = compute_mcp_substrate_digest(doc, [tool1])
    d2 = compute_mcp_substrate_digest(doc, [tool2])
    d3 = compute_mcp_substrate_digest(doc, [tool3])

    assert d1 != d2, "Digest must differ when metadata/op_kind differs"
    assert d1 != d3, "Digest must differ when execution_body differs"

    with pytest.raises(SubstrateError, match="platform tag"):
        compute_mcp_substrate_digest(
            doc, [tool1], target=WheelhouseTarget("cp312", "macosx_11_0_arm64")
        )
    with pytest.raises(SubstrateError, match="python tag"):
        compute_mcp_substrate_digest(
            doc, [tool1], target=WheelhouseTarget("cp313", DEFAULT_TARGET_PLATFORM_TAG)
        )
    with pytest.raises(SubstrateError, match="python:3.12.11-slim@sha256"):
        compute_mcp_substrate_digest(doc, [tool1], base_image="python:3.13-slim")


def test_mcp_compose_validation_rejects_unauthorized_constructs():
    bad_doc1 = render_mcp_compose_document()
    bad_doc1["services"]["mcp-service"]["ports"] = ["8080:8080"]
    valid, errs = validate_mcp_compose_document(bad_doc1)
    assert not valid
    assert any("ports" in e for e in errs)

    bad_doc2 = render_mcp_compose_document()
    bad_doc2["services"]["main"]["environment"] = {"SECRET_KEY": "leaked"}
    valid, errs = validate_mcp_compose_document(bad_doc2)
    assert not valid
    assert any("main service may not declare an environment" in e for e in errs)

    bad_doc3 = render_mcp_compose_document()
    bad_doc3["services"]["main"]["volumes"] = ["evidence-volume:/app/output:rw"]
    valid, errs = validate_mcp_compose_document(bad_doc3)
    assert not valid
    assert any("read-only" in e for e in errs)

    bad_doc4 = render_mcp_compose_document()
    bad_doc4["services"]["mcp-service"]["network_mode"] = "host"
    valid, errs = validate_mcp_compose_document(bad_doc4)
    assert not valid
    assert any("network_mode" in e for e in errs)


def test_default_base_runtime_is_pinned_cpython312_slim():
    dockerfile = render_mcp_sidecar_dockerfile()
    assert PINNED_BASE_IMAGE_INDEX_DIGEST in dockerfile
    assert "bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251" not in dockerfile
    assert dockerfile.startswith(f"FROM {DEFAULT_PINNED_BASE_IMAGE}\n")
    runtime = validate_target_base_runtime(DEFAULT_TARGET_PYTHON_TAG, DEFAULT_TARGET_PLATFORM_TAG)
    assert runtime["base_image"] == DEFAULT_PINNED_BASE_IMAGE
    assert runtime["base_image_index_digest"] == PINNED_BASE_IMAGE_INDEX_DIGEST
    assert runtime["base_image_amd64_manifest_digest"] == PINNED_BASE_IMAGE_AMD64_MANIFEST_DIGEST


def test_target_base_runtime_rejects_mismatch_and_unpinned_images():
    with pytest.raises(SubstrateError, match="python:3.12.11-slim@sha256"):
        validate_target_base_runtime(
            DEFAULT_TARGET_PYTHON_TAG, DEFAULT_TARGET_PLATFORM_TAG, "python:3.13-slim"
        )
    with pytest.raises(SubstrateError, match="python:3.12.11-slim@sha256"):
        validate_target_base_runtime(
            DEFAULT_TARGET_PYTHON_TAG, DEFAULT_TARGET_PLATFORM_TAG, "python:latest"
        )
    with pytest.raises(SubstrateError, match="does not match declared index digest"):
        validate_target_base_runtime(
            DEFAULT_TARGET_PYTHON_TAG,
            DEFAULT_TARGET_PLATFORM_TAG,
            "python:3.12.11-slim@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
    with pytest.raises(SubstrateError, match="index digest is not the pinned"):
        validate_target_base_runtime(
            DEFAULT_TARGET_PYTHON_TAG,
            DEFAULT_TARGET_PLATFORM_TAG,
            "python:3.12.11-slim@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            base_image_index_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
    with pytest.raises(SubstrateError, match="amd64 manifest digest is not the pinned"):
        validate_target_base_runtime(
            DEFAULT_TARGET_PYTHON_TAG,
            DEFAULT_TARGET_PLATFORM_TAG,
            DEFAULT_PINNED_BASE_IMAGE,
            base_image_amd64_manifest_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
    with pytest.raises(SubstrateError, match="python tag"):
        validate_target_base_runtime("cp313", DEFAULT_TARGET_PLATFORM_TAG)
    with pytest.raises(SubstrateError, match="platform tag"):
        validate_target_base_runtime(DEFAULT_TARGET_PYTHON_TAG, "macosx_11_0_arm64")


def test_plan_only_proof_binds_target_and_base_manifest(tmp_path: Path):
    tool = MCPToolDefinition(
        name="noop",
        description="noop",
        parameters=(MCPToolParameter(name="x", type_name="int", description="x"),),
    )
    plan_dir = tmp_path / "plan"
    materialize_mcp_sidecar_package(target_dir=plan_dir, tools=[tool], plan_only=True)
    proof = json.loads((plan_dir / "offline-build-proof.json").read_text())
    assert proof["base_image"] == DEFAULT_PINNED_BASE_IMAGE
    assert proof["base_image_index_digest"] == PINNED_BASE_IMAGE_INDEX_DIGEST
    assert proof["base_image_amd64_manifest_digest"] == PINNED_BASE_IMAGE_AMD64_MANIFEST_DIGEST
    assert proof["target_python"] == DEFAULT_TARGET_PYTHON_TAG
    assert proof["target_platform"] == DEFAULT_TARGET_PLATFORM_TAG
    mismatch_dir = tmp_path / "plan_mismatch"
    with pytest.raises(SubstrateError, match="platform tag"):
        materialize_mcp_sidecar_package(
            target_dir=mismatch_dir,
            tools=[tool],
            plan_only=True,
            target=WheelhouseTarget("cp312", "macosx_11_0_arm64"),
        )
    assert not mismatch_dir.exists()


def test_digest_binds_base_runtime_identity():
    doc = render_mcp_compose_document()
    tool = MCPToolDefinition(
        name="tool_a",
        description="Tool A",
        parameters=(MCPToolParameter(name="x", type_name="int", description="x"),),
    )
    d1 = compute_mcp_substrate_digest(doc, [tool])
    d2 = compute_mcp_substrate_digest(
        doc,
        [tool],
        target=WheelhouseTarget(DEFAULT_TARGET_PYTHON_TAG, DEFAULT_TARGET_PLATFORM_TAG),
        base_image=DEFAULT_PINNED_BASE_IMAGE,
    )
    assert d1 == d2


def _runtime_asset_tool() -> MCPToolDefinition:
    return MCPToolDefinition(
        name="ping",
        description="ping",
        parameters=(MCPToolParameter(name="x", type_name="int", description="x"),),
        metadata={"op_kind": "ping"},
    )


def test_runtime_assets_copied_sorted_and_bound_in_proof(tmp_path: Path):
    ops = tmp_path / "src_ops.py"
    ops.write_text("OP_REGISTRY = {'ping': lambda: 'pong'}\n", encoding="utf-8")
    sealed = tmp_path / "sealed.bin"
    sealed.write_bytes(b"\x00sealed\xff")
    pkg = tmp_path / "pkg"
    materialize_mcp_sidecar_package(
        target_dir=pkg,
        tools=[_runtime_asset_tool()],
        plan_only=True,
        op_registry_module="ops",
        runtime_assets=(
            RuntimeAsset("ops.py", ops),
            RuntimeAsset("evidence/sealed.bin", sealed),
        ),
    )
    assert (pkg / "ops.py").read_bytes() == ops.read_bytes()
    assert (pkg / "evidence" / "sealed.bin").read_bytes() == b"\x00sealed\xff"
    assert "from ops import OP_REGISTRY" in (pkg / "server.py").read_text(encoding="utf-8")
    proof = json.loads((pkg / "offline-build-proof.json").read_text(encoding="utf-8"))
    assert [item["path"] for item in proof["runtime_assets"]] == [
        "evidence/sealed.bin",
        "ops.py",
    ]
    assert proof["runtime_assets"][0]["sha256"] == compute_sha256(b"\x00sealed\xff")
    assert proof["runtime_assets"][0]["size_bytes"] == 8
    assert proof["runtime_assets"][1]["sha256"] == compute_sha256(ops.read_bytes())
    dockerfile = render_mcp_sidecar_dockerfile(
        runtime_assets=(
            RuntimeAsset("ops.py", ops),
            RuntimeAsset("evidence/sealed.bin", sealed),
        )
    )
    assert dockerfile.index("COPY server.py /app/server.py\n") < dockerfile.index(
        "COPY evidence/sealed.bin /app/evidence/sealed.bin\n"
    )
    assert dockerfile.index(
        "COPY evidence/sealed.bin /app/evidence/sealed.bin\n"
    ) < dockerfile.index("COPY ops.py /app/ops.py\n")
    copy_lines = [line for line in dockerfile.splitlines() if line.startswith("COPY ")]
    assert copy_lines == [
        "COPY wheelhouse /wheelhouse",
        "COPY requirements.txt /app/requirements.txt",
        "COPY server.py /app/server.py",
        "COPY evidence/sealed.bin /app/evidence/sealed.bin",
        "COPY ops.py /app/ops.py",
    ]


def test_runtime_asset_byte_and_path_changes_shift_proof_and_digest(tmp_path: Path):
    first = tmp_path / "first.bin"
    first.write_bytes(b"alpha")
    second = tmp_path / "second.bin"
    second.write_bytes(b"beta!")
    tool = _runtime_asset_tool()
    topology = render_mcp_compose_document()
    digest_a = compute_mcp_substrate_digest(
        topology, [tool], runtime_assets=(RuntimeAsset("evidence/a.bin", first),)
    )
    digest_byte = compute_mcp_substrate_digest(
        topology, [tool], runtime_assets=(RuntimeAsset("evidence/a.bin", second),)
    )
    digest_path = compute_mcp_substrate_digest(
        topology, [tool], runtime_assets=(RuntimeAsset("evidence/b.bin", first),)
    )
    digest_none = compute_mcp_substrate_digest(topology, [tool])
    assert digest_a != digest_byte
    assert digest_a != digest_path
    assert digest_none != digest_a
    assert digest_none == compute_mcp_substrate_digest(topology, [tool], runtime_assets=())

    pkg_a = tmp_path / "pkg_a"
    pkg_byte = tmp_path / "pkg_byte"
    pkg_path = tmp_path / "pkg_path"
    materialize_mcp_sidecar_package(
        target_dir=pkg_a,
        tools=[tool],
        plan_only=True,
        runtime_assets=(RuntimeAsset("evidence/a.bin", first),),
    )
    materialize_mcp_sidecar_package(
        target_dir=pkg_byte,
        tools=[tool],
        plan_only=True,
        runtime_assets=(RuntimeAsset("evidence/a.bin", second),),
    )
    materialize_mcp_sidecar_package(
        target_dir=pkg_path,
        tools=[tool],
        plan_only=True,
        runtime_assets=(RuntimeAsset("evidence/b.bin", first),),
    )
    proof_a = (pkg_a / "offline-build-proof.json").read_text(encoding="utf-8")
    assert proof_a != (pkg_byte / "offline-build-proof.json").read_text(encoding="utf-8")
    assert proof_a != (pkg_path / "offline-build-proof.json").read_text(encoding="utf-8")


def test_production_runtime_assets_bind_final_dockerfile_digest(tmp_path: Path):
    wheelhouse = Path("/tmp/fastmcp3_wheelhouse")
    if not wheelhouse.is_dir():
        pytest.skip("FastMCP 3.4.7 wheelhouse not populated on this host")
    linux_target = WheelhouseTarget(DEFAULT_TARGET_PYTHON_TAG, DEFAULT_TARGET_PLATFORM_TAG)
    try:
        provenance = record_prepackaging_provenance(wheelhouse, linux_target)
    except SubstrateError:
        pytest.skip("Linux CPython 3.12 manylinux wheelhouse not populated on this host")
    ops = tmp_path / "ops.py"
    ops.write_text("OP_REGISTRY = {'ping': lambda: 'pong'}\n", encoding="utf-8")
    pkg = tmp_path / "prod"
    materialize_mcp_sidecar_package(
        target_dir=pkg,
        tools=[_runtime_asset_tool()],
        op_registry_module="ops",
        runtime_assets=(RuntimeAsset("ops.py", ops),),
        wheelhouse_source=wheelhouse,
        target=linux_target,
        resolver_provenance=provenance,
    )
    dockerfile = (pkg / "Dockerfile").read_text(encoding="utf-8")
    proof = json.loads((pkg / "offline-build-proof.json").read_text(encoding="utf-8"))
    assert "COPY ops.py /app/ops.py" in dockerfile
    assert proof["dockerfile_sha256"] == compute_sha256(dockerfile)
    assert proof["runtime_assets"][0]["path"] == "ops.py"


@pytest.mark.parametrize(
    ("destination", "match"),
    [
        ("../secret", "directory escape|Path must be relative|confined POSIX"),
        ("server.py", "reserved"),
        ("wheelhouse/extra.whl", "reserved"),
    ],
)
def test_runtime_asset_rejects_traversal_and_reserved(
    tmp_path: Path, destination: str, match: str
):
    source = tmp_path / "payload.bin"
    source.write_bytes(b"payload")
    pkg = tmp_path / "pkg"
    with pytest.raises(SubstrateError, match=match):
        materialize_mcp_sidecar_package(
            target_dir=pkg,
            tools=[_runtime_asset_tool()],
            plan_only=True,
            runtime_assets=(RuntimeAsset(destination, source),),
        )
    assert not pkg.exists()


def test_runtime_asset_rejects_symlink_source(tmp_path: Path):
    real = tmp_path / "real.py"
    real.write_text("OP_REGISTRY = {}\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    pkg = tmp_path / "pkg"
    with pytest.raises(SubstrateError, match="symlink"):
        materialize_mcp_sidecar_package(
            target_dir=pkg,
            tools=[_runtime_asset_tool()],
            plan_only=True,
            runtime_assets=(RuntimeAsset("ops.py", link),),
        )
    assert not pkg.exists()


def test_runtime_asset_rejects_symlink_destination(tmp_path: Path):
    real = tmp_path / "real.py"
    real.write_text("OP_REGISTRY = {}\n", encoding="utf-8")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "ops.py").symlink_to(real)
    with pytest.raises(SubstrateError, match="symlink"):
        materialize_mcp_sidecar_package(
            target_dir=pkg,
            tools=[_runtime_asset_tool()],
            plan_only=True,
            runtime_assets=(RuntimeAsset("ops.py", real),),
        )
    assert not (pkg / "server.py").exists()


def test_runtime_asset_rejects_duplicate_destinations(tmp_path: Path):
    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    first.write_text("OP_REGISTRY = {'a': 1}\n", encoding="utf-8")
    second.write_text("OP_REGISTRY = {'b': 2}\n", encoding="utf-8")
    pkg = tmp_path / "pkg"
    with pytest.raises(SubstrateError, match="Duplicate"):
        materialize_mcp_sidecar_package(
            target_dir=pkg,
            tools=[_runtime_asset_tool()],
            plan_only=True,
            runtime_assets=(
                RuntimeAsset("ops.py", first),
                RuntimeAsset("ops.py", second),
            ),
        )
    assert not pkg.exists()


def test_op_registry_module_requires_matching_runtime_asset(tmp_path: Path):
    evidence = tmp_path / "scenario.json"
    evidence.write_text("{}", encoding="utf-8")
    pkg = tmp_path / "pkg"
    with pytest.raises(SubstrateError, match="requires runtime asset 'ops.py'"):
        materialize_mcp_sidecar_package(
            target_dir=pkg,
            tools=[_runtime_asset_tool()],
            plan_only=True,
            op_registry_module="ops",
            runtime_assets=(RuntimeAsset("scenario.json", evidence),),
        )
    assert not pkg.exists()
