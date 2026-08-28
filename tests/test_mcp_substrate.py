from __future__ import annotations

import http.client
import json
import socketserver
import threading
from pathlib import Path

from evallab.benchmark_program_contracts import FaultClass, FaultInjectionRecord
from evallab.mcp_substrate import (
    DEFAULT_PROTOCOL_VERSION,
    FastMCPRuntime,
    MCPToolDefinition,
    MCPToolParameter,
    compute_mcp_substrate_digest,
    make_fastmcp_http_handler,
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


def test_mcp_compose_validation_rejects_unauthorized_constructs():
    # 1. Custom host ports
    bad_doc1 = render_mcp_compose_document()
    bad_doc1["services"]["mcp-service"]["ports"] = ["8080:8080"]
    valid, errs = validate_mcp_compose_document(bad_doc1)
    assert not valid
    assert any("ports" in e for e in errs)

    # 2. Main service environment variable
    bad_doc2 = render_mcp_compose_document()
    bad_doc2["services"]["main"]["environment"] = {"SECRET_KEY": "leaked"}
    valid, errs = validate_mcp_compose_document(bad_doc2)
    assert not valid
    assert any("main service may not declare an environment" in e for e in errs)

    # 3. Main service writable volume
    bad_doc3 = render_mcp_compose_document()
    bad_doc3["services"]["main"]["volumes"] = ["evidence-volume:/app/output:rw"]
    valid, errs = validate_mcp_compose_document(bad_doc3)
    assert not valid
    assert any("read-only" in e for e in errs)

    # 4. Custom network mode / networks
    bad_doc4 = render_mcp_compose_document()
    bad_doc4["services"]["mcp-service"]["network_mode"] = "host"
    valid, errs = validate_mcp_compose_document(bad_doc4)
    assert not valid
    assert any("network_mode" in e for e in errs)


def test_mcp_substrate_digest_calculation():
    tool = MCPToolDefinition(
        name="tool_add",
        description="Add two numbers",
        parameters=(
            MCPToolParameter(name="a", type_name="int", description="first operand"),
            MCPToolParameter(name="b", type_name="int", description="second operand"),
        ),
    )
    doc = render_mcp_compose_document()
    digest1 = compute_mcp_substrate_digest(doc, [tool])
    digest2 = compute_mcp_substrate_digest(doc, [tool])
    assert digest1 == digest2
    assert len(digest1) == 64


def test_standard_fastmcp_http_server_and_jsonrpc_protocol(tmp_path: Path):
    """Proves standard initialize, tools/list, tools/call JSON-RPC compliance over streamable HTTP."""
    tool1 = MCPToolDefinition(
        name="compute_power",
        description="Compute base ** exponent",
        parameters=(
            MCPToolParameter(name="base", type_name="int", description="Base integer"),
            MCPToolParameter(name="exponent", type_name="int", description="Power exponent"),
        ),
    )

    def handle_power(args: dict) -> dict:
        return {"value": args["base"] ** args["exponent"]}

    evidence_dir = tmp_path / "evidence"
    runtime = FastMCPRuntime(
        tools=[tool1],
        handlers={"compute_power": handle_power},
        evidence_dir=evidence_dir,
    )
    handler = make_fastmcp_http_handler(runtime)

    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        # 1. Health check
        conn.request("GET", "/health")
        res = conn.getresponse()
        assert res.status == 200
        health_data = json.loads(res.read().decode())
        assert health_data["status"] == "ok"

        # 2. Standard initialize handshake
        init_payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": DEFAULT_PROTOCOL_VERSION, "capabilities": {}},
            }
        )
        conn.request(
            "POST", "/mcp", body=init_payload, headers={"Content-Type": "application/json"}
        )
        res = conn.getresponse()
        assert res.status == 200
        init_res = json.loads(res.read().decode())
        assert init_res["result"]["protocolVersion"] == DEFAULT_PROTOCOL_VERSION
        assert "tools" in init_res["result"]["capabilities"]

        # 3. tools/list
        list_payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )
        conn.request(
            "POST", "/mcp", body=list_payload, headers={"Content-Type": "application/json"}
        )
        res = conn.getresponse()
        assert res.status == 200
        list_res = json.loads(res.read().decode())
        tools = list_res["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "compute_power"
        assert "inputSchema" in tools[0]
        assert "base" in tools[0]["inputSchema"]["properties"]

        # 4. tools/call
        call_payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "compute_power", "arguments": {"base": 2, "exponent": 8}},
            }
        )
        conn.request(
            "POST", "/mcp", body=call_payload, headers={"Content-Type": "application/json"}
        )
        res = conn.getresponse()
        assert res.status == 200
        call_res = json.loads(res.read().decode())
        assert call_res["result"]["value"] == 256

        # 5. Invalid tool call
        bad_call = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "compute_power", "arguments": {"base": 2}},  # missing exponent
            }
        )
        conn.request("POST", "/mcp", body=bad_call, headers={"Content-Type": "application/json"})
        res = conn.getresponse()
        assert res.status == 200
        bad_res = json.loads(res.read().decode())
        assert "error" in bad_res

        # 6. Event ledger check
        conn.request("GET", "/events")
        res = conn.getresponse()
        assert res.status == 200
        events = [json.loads(line) for line in res.read().decode().splitlines() if line.strip()]
        assert len(events) == 2
        assert events[0]["event_type"] == "tool_call_success"
        assert events[1]["event_type"] == "tool_call_rejected"

        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_fault_interceptor_middleware_determinism(tmp_path: Path):
    """Test deterministic fault injection at target ordinal across fault classes."""
    fault_hash = "1" * 64
    verifier_hash = "2" * 64

    fault_record = FaultInjectionRecord(
        fault_id=fault_hash,
        task_id="task_f",
        twin_task_id="twin_f",
        target_tool="db_query",
        fault_class=FaultClass.PERSISTENT_SCHEMA_MISMATCH,
        target_canonical_event_ordinal=2,
        injection_payload={"message": "Column not found"},
        recovery_contract="adapt_schema",
        verifier_oracle_digest=verifier_hash,
    )

    tool = MCPToolDefinition(
        name="db_query",
        description="Query database",
        parameters=(MCPToolParameter(name="sql", type_name="str", description="SQL string"),),
    )

    runtime = FastMCPRuntime(
        tools=[tool],
        handlers={"db_query": lambda args: {"rows": [{"id": 1}]}},
        fault_record=fault_record,
        evidence_dir=tmp_path / "evidence",
    )

    # Call 1 -> Success
    res1, code1 = runtime.call_tool("db_query", {"sql": "SELECT 1"})
    assert code1 == 200
    assert "result" in res1
    assert res1["result"]["rows"] == [{"id": 1}]

    # Call 2 -> Intercepted at ordinal 2
    res2, code2 = runtime.call_tool("db_query", {"sql": "SELECT 1"})
    assert "error" in res2
    assert "Column not found" in res2["error"]["message"]

    # Call 3 -> Normal behavior resumes
    res3, code3 = runtime.call_tool("db_query", {"sql": "SELECT 1"})
    assert code3 == 200
    assert "result" in res3
