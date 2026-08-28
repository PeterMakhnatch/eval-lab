from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from evallab.benchmark_program_contracts import FaultClass, FaultInjectionRecord
from evallab.mcp_substrate import (
    FASTMCP_SIDECAR_REQUIREMENTS_TXT,
    MCPToolDefinition,
    MCPToolParameter,
    SubstrateError,
    compute_mcp_substrate_digest,
    generate_fastmcp_server_script,
    materialize_mcp_sidecar_package,
    render_mcp_compose_document,
    validate_mcp_compose_document,
)


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
    with pytest.raises(SubstrateError, match="missing required locked package"):
        materialize_mcp_sidecar_package(
            target_dir=tmp_path / "fail_incomplete",
            tools=[tool],
            wheelhouse_source=incomplete_wheelhouse,
        )

    # 4. Tampered wheel hash fails closed
    tampered_wheelhouse = tmp_path / "tampered_wheelhouse"
    tampered_wheelhouse.mkdir()
    for w in wheelhouse.glob("*.whl"):
        shutil.copy2(w, tampered_wheelhouse / w.name)
    target_corrupt = next(tampered_wheelhouse.glob("*.whl"))
    target_corrupt.write_bytes(target_corrupt.read_bytes() + b"\x00corrupted")
    with pytest.raises(SubstrateError, match="does not match any locked hash"):
        materialize_mcp_sidecar_package(
            target_dir=tmp_path / "fail_tampered",
            tools=[tool],
            wheelhouse_source=tampered_wheelhouse,
        )

    # 5. Full valid production materialization succeeds
    prod_dir = tmp_path / "prod_pkg"
    prod_pkg = materialize_mcp_sidecar_package(
        target_dir=prod_dir,
        tools=[tool],
        server_name="math-sidecar",
        port=8080,
        wheelhouse_source=wheelhouse,
    )
    assert (prod_dir / "server.py").is_file()
    assert (prod_dir / "requirements.txt").is_file()
    assert (prod_dir / "Dockerfile").is_file()
    assert (prod_dir / "wheelhouse").is_dir()
    assert len(list((prod_dir / "wheelhouse").glob("*.whl"))) == len(list(wheelhouse.glob("*.whl")))
    prod_proof = json.loads((prod_dir / "offline-build-proof.json").read_text())
    assert prod_proof["mode"] == "complete_offline_package"
    assert prod_proof["wheel_count"] > 0

    valid, errs = validate_mcp_compose_document(prod_pkg["compose_doc"])
    assert valid, f"Generated Compose invalid: {errs}"


def test_fastmcp_script_generation_and_requirements_pinning():
    tool = MCPToolDefinition(
        name="calculate_sum",
        description="Compute sum of two integers",
        parameters=(
            MCPToolParameter(name="x", type_name="int", description="First number"),
            MCPToolParameter(name="y", type_name="int", description="Second number"),
        ),
        metadata={"op_kind": "add_op"},
    )
    script = generate_fastmcp_server_script([tool], server_name="test-server", port=8080)
    assert "from fastmcp import FastMCP" in script
    assert 'mcp = FastMCP("test-server")' in script
    assert "@mcp.tool()" in script
    assert "def calculate_sum(x: int, y: int) -> dict[str, Any]:" in script

    # Verify requirements.txt has all packages strictly hash locked with sha256
    lines = [
        line.strip()
        for line in FASTMCP_SIDECAR_REQUIREMENTS_TXT.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    for req_line in lines:
        assert "==" in req_line
        assert "--hash=sha256:" in req_line


def test_offline_pip_install_smoke_with_require_hashes(tmp_path: Path):
    """Smoke test proving FASTMCP_SIDECAR_REQUIREMENTS_TXT installs offline with --require-hashes against local wheelhouse."""
    wheelhouse = Path("/tmp/fastmcp3_wheelhouse")
    if not wheelhouse.is_dir():
        pytest.skip("FastMCP 3.4.7 wheelhouse not populated on this host")

    reqs_file = tmp_path / "requirements.txt"
    reqs_file.write_text(FASTMCP_SIDECAR_REQUIREMENTS_TXT)

    target_env = tmp_path / "target_env"
    target_env.mkdir()

    cmd = [
        "uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "--no-index",
        "--find-links",
        str(wheelhouse),
        "--require-hashes",
        "-r",
        str(reqs_file),
        "--target",
        str(target_env),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, (
        f"Offline pip install failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )
    assert (target_env / "fastmcp").is_dir() or (target_env / "fastmcp-3.4.7.dist-info").is_dir()


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
        from mcp.client.sse import sse_client

        async with (
            sse_client("http://127.0.0.1:8588/sse") as (read, write),
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
        from mcp.client.sse import sse_client

        async with (
            sse_client("http://127.0.0.1:8589/sse") as (read, write),
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
