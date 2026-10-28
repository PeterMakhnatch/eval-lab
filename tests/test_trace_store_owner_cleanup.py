from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any

import pytest

from evallab.trace_store import owner_cleanup as oc
from evallab.trace_store.core import trace_uuid

PROJECT = "a5586b6d-55e8-466f-9718-c38255c2ae94"
WORKSPACE = "be4b237b-b050-401f-b8a3-ff181a3974c8"
ROOT = oc.ROOT_SPAN_NAME
CAPTURED_AT = "2026-10-28T12:00:00+00:00"


def _span_ids(target: str) -> tuple[str, str]:
    seed = hashlib.sha256(target.encode()).hexdigest()
    return seed[:16], seed[16:32]


def _attributes(source: str | None = None) -> str:
    attributes = {"lmnr.span.type": "DEFAULT", "evallab.timing": "import_sequence_not_event_time"}
    if source is not None:
        attributes["evallab.source"] = source
    attributes["lmnr.span.ids_path"] = ["consumer-derived"]
    attributes["lmnr.span.path"] = ["consumer-derived"]
    return json.dumps(attributes, sort_keys=True)


def _expected_attributes(source: str | None = None) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "lmnr.span.type": "DEFAULT",
        "evallab.timing": "import_sequence_not_event_time",
    }
    if source is not None:
        attributes["evallab.source"] = source
    return attributes


def _expected_spans(remote_id: str) -> list[dict[str, Any]]:
    root_id, step_id = _span_ids(remote_id)
    return [
        {
            "trace_id": remote_id.replace("-", ""),
            "span_id": root_id.zfill(32),
            "parent_id": "0" * 32,
            "name": ROOT,
            "span_type": "DEFAULT",
            "input": {},
            "output": {"messages": 1},
            "attributes": _expected_attributes(),
        },
        {
            "trace_id": remote_id.replace("-", ""),
            "span_id": step_id.zfill(32),
            "parent_id": root_id.zfill(32),
            "name": "turn 0",
            "span_type": "DEFAULT",
            "input": {"text": "hi"},
            "output": None,
            "attributes": _expected_attributes("user"),
        },
    ]


def _digest(spans: list[dict[str, Any]]) -> str:
    ordered = sorted(spans, key=lambda span: span["span_id"])
    encoded = json.dumps(ordered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _rows(remote_id: str) -> list[dict[str, Any]]:
    root_id, step_id = _span_ids(remote_id)
    return [
        {
            "trace_id": remote_id,
            "span_id": root_id,
            "parent_span_id": None,
            "name": ROOT,
            "span_type": "DEFAULT",
            "input": "{}",
            "output": json.dumps({"messages": 1}),
            "attributes": _attributes(),
        },
        {
            "trace_id": remote_id,
            "span_id": step_id,
            "parent_span_id": root_id,
            "name": "turn 0",
            "span_type": "DEFAULT",
            "input": json.dumps({"text": "hi"}),
            "output": "",
            "attributes": _attributes("user"),
        },
    ]


def _targets() -> list[dict[str, Any]]:
    entries = []
    for index in range(oc.OWNED_COPY_COUNT):
        trace_id = f"retained-trace:{index:032x}"
        remote_id = trace_uuid(trace_id)
        expected = _digest(_expected_spans(remote_id))
        entries.append(
            {
                "trace_id": trace_id,
                "remote_id": remote_id,
                "dataset": "mimo-defect-map-v1" if index < 100 else "tb4-audit-v1",
                "scope": "base104" if index < 104 else "extra4",
                "span_count_expected": 2,
                "span_count_remote": 2,
                "absent_output_attribute_server_empty_default": 1,
                "expected_authored_sha256": expected,
                "remote_authored_sha256": expected,
                "remote_authored_payload_matches_original_ledger_projection": True,
                "content_digest": hashlib.sha256(f"content/{index}".encode()).hexdigest(),
                "raw_cas_uri": "cas://sha256/"
                + hashlib.sha256(f"cas/{index}".encode()).hexdigest(),
                "original_upload_at": "2026-10-27T22:55:58.936090+00:00",
            }
        )
    return entries


def _write_evidence(
    tmp_path: Path,
    *,
    proof_mutator: Any = None,
    packet_mutator: Any = None,
) -> tuple[Path, Path, list[dict[str, Any]]]:
    entries = _targets()
    proof: dict[str, Any] = {
        "at": CAPTURED_AT,
        "project_id": PROJECT,
        "base104_candidates": 104,
        "extra4_candidates": 4,
        "eligible_remote_copies": 108,
        "full_authored_payload_matches": 108,
        "per_source": {"mimo-defect-map-v1": 100, "tb4-audit-v1": 8},
        "entries": entries,
    }
    if proof_mutator is not None:
        proof = proof_mutator(proof)
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(json.dumps(proof, indent=2))
    packet: dict[str, Any] = {
        "status": "BLOCKED_NORMAL_USER_SESSION",
        "at": CAPTURED_AT,
        "project_id": PROJECT,
        "workspace_id": WORKSPACE,
        "project_url": "https://laminar.sh/project/" + PROJECT + "/traces",
        "proof_path": str(proof_path),
        "proof_sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest(),
        "trace_ids": [entry["remote_id"] for entry in entries],
        "method": "DELETE",
    }
    if packet_mutator is not None:
        packet = packet_mutator(packet)
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet, indent=2))
    return packet_path, proof_path, entries


def _receipt(
    packet_path: Path,
    entries: list[dict[str, Any]],
    *,
    rows: dict[str, list[dict[str, Any]]] | None = None,
    counts: dict[str, int] | None = None,
    project: dict[str, Any] | None = None,
    span_total: int = 3549,
    trace_total: int = 3698,
) -> dict[str, Any]:
    packet = oc.load_owner_packet(packet_path)
    by_remote = {entry["remote_id"]: entry for entry in entries}
    supplied = (
        rows
        if rows is not None
        else {entry["remote_id"]: _rows(entry["remote_id"]) for entry in entries}
    )
    assert set(supplied) <= set(by_remote)
    batches = []
    for chunk in oc._chunks(packet.trace_ids, oc.BATCH_SIZE):
        detail_rows = [row for remote_id in chunk for row in supplied.get(remote_id, [])]
        counts_rows = []
        for remote_id in chunk:
            span_count = (
                counts.get(remote_id) if counts is not None else len(supplied.get(remote_id, []))
            )
            if span_count:
                counts_rows.append({"trace_id": remote_id, "span_count": span_count})
        batches.append(
            {
                "remote_ids": list(chunk),
                "detail": {"sql": oc._detail_sql(chunk), "response": {"data": detail_rows}},
                "counts": {"sql": oc._count_sql(chunk), "response": {"data": counts_rows}},
            }
        )
    return {
        "schema": oc.SCHEMA_INVENTORY,
        "captured_at": CAPTURED_AT,
        "packet_sha256": packet.sha256,
        "proof_sha256": packet.proof_sha256,
        "project": {
            "path": "/v1/project",
            "response": project
            if project is not None
            else {
                "projectId": PROJECT,
                "workspaceId": WORKSPACE,
            },
        },
        "aggregate": {
            "spans": {
                "sql": oc.AGGREGATE_SPANS_SQL,
                "response": {"data": [{"span_count": span_total}]},
            },
            "traces": {
                "sql": oc.AGGREGATE_TRACES_SQL,
                "response": {"data": [{"trace_count": trace_total}]},
            },
        },
        "batches": batches,
        "http_requests": 3 + 2 * len(batches),
    }


def _write_receipt(path: Path, receipt: dict[str, Any]) -> Path:
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True))
    return path


def _verify(packet_path: Path, entries: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return oc.verify_receipt(packet_path, receipt=_receipt(packet_path, entries, **kwargs))


def test_packet_and_proof_bind_exactly_the_granted_copies(tmp_path: Path) -> None:
    packet_path, proof_path, entries = _write_evidence(tmp_path)
    packet = oc.load_owner_packet(packet_path)
    targets = oc.load_owner_targets(packet)
    assert len(targets) == 108
    assert [target.remote_id for target in targets] == [entry["remote_id"] for entry in entries]
    assert [target.trace_id for target in targets] == [entry["trace_id"] for entry in entries]
    assert sum(target.scope == "base104" for target in targets) == 104
    assert sum(target.scope == "extra4" for target in targets) == 4
    assert all(target.remote_id == trace_uuid(target.trace_id) for target in targets)
    assert packet.status == "BLOCKED_NORMAL_USER_SESSION"
    assert packet.proof_path == proof_path


def test_authored_projection_and_digest_follow_the_documented_contract() -> None:
    remote_id = trace_uuid("retained-trace:" + "0" * 32)
    spans, normalized = [], 0
    for row in _rows(remote_id):
        projected, was_normalized = oc._project_row(row)
        spans.append(projected)
        normalized += int(was_normalized)
    assert spans == _expected_spans(remote_id)
    assert normalized == 1
    assert oc.authored_digest(spans) == _digest(_expected_spans(remote_id))
    assert oc._norm_id(None) == "0" * 32
    assert oc._norm_id("a" * 16) == "0" * 16 + "a" * 16
    attributes = json.loads(_attributes("agent"))
    assert (
        "lmnr.span.ids_path"
        not in oc._project_row(
            {
                **{key: value for key, value in _rows(remote_id)[0].items()},
                "attributes": json.dumps(attributes),
            }
        )[0]["attributes"]
    )


def test_complete_inventory_with_every_copy_absent_verifies_cleanup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    packet_path, _, entries = _write_evidence(tmp_path)
    receipt = _receipt(packet_path, entries, rows={})
    receipt_path = _write_receipt(tmp_path / "receipt.json", receipt)
    verdict_path = tmp_path / "verdict.json"
    assert (
        oc.main(
            [
                "--packet",
                str(packet_path),
                "--receipt",
                str(receipt_path),
                "--json-out",
                str(verdict_path),
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["cleanup_verified"] is True
    assert printed["counts"] == {
        "absent": 108,
        "present": 0,
        "drifted": 0,
        "unknown": 0,
        "targets": 108,
    }
    assert printed["inventory"]["complete"] is True
    assert printed["inventory"]["project_id"] == PROJECT
    assert printed["safe_catchup"]["evaluated"] is False
    assert printed["owner_action"]["required"] is False
    assert len(printed["targets"]) == 108
    assert all(target["verdict"] == "absent" for target in printed["targets"])
    assert verdict_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(verdict_path.read_text())["cleanup_verified"] is True


def test_present_copies_are_reported_and_never_success(tmp_path: Path) -> None:
    packet_path, _, entries = _write_evidence(tmp_path)
    verdict = _verify(packet_path, entries)
    assert verdict["counts"] == {
        "absent": 0,
        "present": 108,
        "drifted": 0,
        "unknown": 0,
        "targets": 108,
    }
    assert verdict["cleanup_verified"] is False
    assert verdict["owner_action"]["required"] is True
    assert all(target["reason"] == "present_exact" for target in verdict["targets"])
    assert (
        verdict["targets"][0]["observed_authored_sha256"] == entries[0]["expected_authored_sha256"]
    )


def test_wrong_project_or_workspace_is_unknown_not_success(tmp_path: Path) -> None:
    packet_path, _, entries = _write_evidence(tmp_path)
    wrong_project = _verify(
        packet_path, entries, project={"projectId": "00000000-0000-0000-0000-000000000000"}
    )
    assert wrong_project["counts"]["unknown"] == 108
    assert wrong_project["cleanup_verified"] is False
    assert "wrong_project" in wrong_project["inventory"]["problems"]
    wrong_workspace = _verify(
        packet_path, entries, project={"projectId": PROJECT, "workspaceId": "other"}
    )
    assert wrong_workspace["counts"]["unknown"] == 108
    assert "wrong_workspace" in wrong_workspace["inventory"]["problems"]
    unbound = _verify(packet_path, entries, project={"error": "unauthorized"})
    assert unbound["counts"]["unknown"] == 108
    assert unbound["cleanup_verified"] is False


def test_partial_pagination_is_unknown_and_never_cleanup(tmp_path: Path) -> None:
    packet_path, _, entries = _write_evidence(tmp_path)
    partial = {entry["remote_id"]: [] for entry in entries}
    partial[entries[0]["remote_id"]] = _rows(entries[0]["remote_id"])[:1]
    verdict = _verify(
        packet_path,
        entries,
        rows=partial,
        counts={entries[0]["remote_id"]: 2},
    )
    target = next(
        item for item in verdict["targets"] if item["remote_id"] == entries[0]["remote_id"]
    )
    assert target["verdict"] == "unknown"
    assert target["reason"] == "inventory_partial"
    assert verdict["counts"]["absent"] == 107
    assert verdict["counts"]["unknown"] == 1
    assert verdict["cleanup_verified"] is False
    assert verdict["inventory"]["complete"] is False
    assert "partial_inventory" in verdict["inventory"]["problems"]


def test_replacement_and_unexpected_additional_span_are_drifted(tmp_path: Path) -> None:
    packet_path, _, entries = _write_evidence(tmp_path)
    rows = {entry["remote_id"]: [] for entry in entries}
    replaced = entries[0]["remote_id"]
    alternate = _rows(replaced)
    alternate[1]["input"] = json.dumps({"text": "replaced"})
    rows[replaced] = alternate
    extra = entries[1]["remote_id"]
    extra_rows = _rows(extra)
    root_id, _ = _span_ids(extra)
    extra_rows.append(
        {
            "trace_id": extra,
            "span_id": "f" * 16,
            "parent_span_id": root_id,
            "name": "turn 1",
            "span_type": "DEFAULT",
            "input": json.dumps({"text": "extra"}),
            "output": "",
            "attributes": _attributes("user"),
        }
    )
    rows[extra] = extra_rows
    verdict = _verify(packet_path, entries, rows=rows)
    by_remote = {target["remote_id"]: target for target in verdict["targets"]}
    assert by_remote[replaced]["verdict"] == "drifted"
    assert by_remote[replaced]["reason"] == "authored_digest_mismatch"
    assert by_remote[extra]["verdict"] == "drifted"
    assert by_remote[extra]["reason"] == "span_count_mismatch"
    assert verdict["counts"]["drifted"] == 2
    assert verdict["counts"]["absent"] == 106
    assert verdict["cleanup_verified"] is False


def test_duplicate_span_rows_are_drifted(tmp_path: Path) -> None:
    packet_path, _, entries = _write_evidence(tmp_path)
    rows = {entry["remote_id"]: [] for entry in entries}
    target = entries[3]["remote_id"]
    duplicate = _rows(target)
    rows[target] = [duplicate[0], duplicate[1], duplicate[1]]
    verdict = _verify(packet_path, entries, rows=rows)
    item = next(entry for entry in verdict["targets"] if entry["remote_id"] == target)
    assert item["verdict"] == "drifted"
    assert item["reason"] == "duplicate_span_rows"
    assert verdict["cleanup_verified"] is False


def test_absent_targets_are_classified_individually(tmp_path: Path) -> None:
    packet_path, _, entries = _write_evidence(tmp_path)
    rows = {entry["remote_id"]: [] for entry in entries}
    rows[entries[7]["remote_id"]] = _rows(entries[7]["remote_id"])
    verdict = _verify(packet_path, entries, rows=rows)
    by_remote = {target["remote_id"]: target for target in verdict["targets"]}
    assert by_remote[entries[7]["remote_id"]]["verdict"] == "present"
    assert by_remote[entries[8]["remote_id"]]["verdict"] == "absent"
    assert verdict["counts"]["absent"] == 107
    assert verdict["counts"]["present"] == 1
    assert verdict["cleanup_verified"] is False


def test_failed_unauthorized_or_empty_output_is_unknown(tmp_path: Path) -> None:
    packet_path, _, entries = _write_evidence(tmp_path)
    receipt = _receipt(packet_path, entries, rows={})
    receipt["batches"][0]["detail"]["response"] = {"error": "unauthorized"}
    failed = oc.verify_receipt(packet_path, receipt=receipt)
    first_batch = set(receipt["batches"][0]["remote_ids"])
    failed_targets = [target for target in failed["targets"] if target["remote_id"] in first_batch]
    assert all(target["verdict"] == "unknown" for target in failed_targets)
    assert failed["cleanup_verified"] is False
    assert failed["inventory"]["complete"] is False
    empty = _verify(packet_path, entries, rows={}, span_total=0, trace_total=0)
    assert empty["counts"]["unknown"] == 108
    assert "empty_control" in empty["inventory"]["problems"]
    assert empty["cleanup_verified"] is False
    missing = _receipt(packet_path, entries, rows={})
    del missing["aggregate"]
    assert oc.verify_receipt(packet_path, receipt=missing)["cleanup_verified"] is False


def test_unexpected_additional_records_are_inventory_contamination(tmp_path: Path) -> None:
    packet_path, _, entries = _write_evidence(tmp_path)
    receipt = _receipt(packet_path, entries, rows={})
    foreign = "11111111-2222-3333-4444-555555555555"
    receipt["batches"][0]["detail"]["response"]["data"].append(
        {
            "trace_id": foreign,
            "span_id": "a" * 16,
            "parent_span_id": None,
            "name": "foreign",
            "span_type": "DEFAULT",
            "input": "{}",
            "output": "{}",
            "attributes": "{}",
        }
    )
    verdict = oc.verify_receipt(packet_path, receipt=receipt)
    first_batch = set(receipt["batches"][0]["remote_ids"])
    assert all(
        target["verdict"] == "unknown"
        for target in verdict["targets"]
        if target["remote_id"] in first_batch
    )
    assert verdict["cleanup_verified"] is False
    assert verdict["inventory"]["complete"] is False
    assert any(problem.startswith("batch_0:") for problem in verdict["inventory"]["problems"])


class _FakeLaminar:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries
        self.requests: list[tuple[str, str, bytes | None]] = []

    def __call__(self, request: Any, timeout: int) -> io.BytesIO:
        self.requests.append((request.get_method(), request.full_url, request.data))
        if request.get_method() == "GET":
            payload: dict[str, Any] = {"projectId": PROJECT, "workspaceId": WORKSPACE}
        else:
            query = json.loads(request.data)["query"]
            remote_ids = re.findall(r"toUUID\('([0-9a-f-]{36})'\)", query)
            if query == oc.AGGREGATE_SPANS_SQL:
                payload = {"data": [{"span_count": 2 * len(self.entries) + 100}]}
            elif query == oc.AGGREGATE_TRACES_SQL:
                payload = {"data": [{"trace_count": len(self.entries) + 100}]}
            elif "GROUP BY trace_id" in query:
                payload = {
                    "data": [{"trace_id": remote_id, "span_count": 2} for remote_id in remote_ids]
                }
            else:
                payload = {"data": [row for remote_id in remote_ids for row in _rows(remote_id)]}
        return io.BytesIO(json.dumps(payload).encode())


def test_live_capture_is_read_only_and_replayable_offline(tmp_path: Path) -> None:
    packet_path, _, entries = _write_evidence(tmp_path)
    fake = _FakeLaminar(entries)
    spends: list[dict[str, Any]] = []
    capture = tmp_path / "capture.json"
    verdict = oc.verify_live(
        packet_path,
        capture_out=capture,
        api_key="test-secret",
        opener=fake,
        spend_recorder=lambda target, **details: spends.append({"target": target, **details}),
    )
    assert len(fake.requests) == 17 == verdict["inventory"]["batches"] * 2 + 3
    assert [method for method, _, _ in fake.requests] == ["GET"] + ["POST"] * 16
    assert fake.requests[0][1].endswith("/v1/project")
    assert all(url.endswith("/v1/sql/query") for _, url, _ in fake.requests[1:])
    assert {method for method, _, _ in fake.requests} == {"GET", "POST"}
    assert all("signals" not in url and "delete" not in url for _, url, _ in fake.requests)
    assert verdict["inventory"]["kind"] == "live"
    assert verdict["counts"]["present"] == 108
    assert verdict["cleanup_verified"] is False
    assert spends and spends[0]["cost_usd"] == 0 and spends[0]["http_requests"] == 17
    raw = capture.read_bytes()
    assert b"test-secret" not in raw
    assert capture.stat().st_mode & 0o777 == 0o600
    offline = oc.verify_receipt(packet_path, capture)
    assert offline["inventory"]["kind"] == "recorded"
    assert offline["inventory"]["receipt_file_sha256"] == hashlib.sha256(raw).hexdigest()
    assert offline["counts"] == verdict["counts"]
    assert offline["cleanup_verified"] is False


def test_live_query_failure_is_never_a_verdict(tmp_path: Path) -> None:
    from urllib.error import HTTPError
    from urllib.request import Request

    def refuse(request: Request, timeout: int) -> Any:
        raise HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO(b"denied"))

    packet_path, _, _ = _write_evidence(tmp_path)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        oc.request_inventory(
            oc.load_owner_packet(packet_path),
            oc.load_owner_targets(oc.load_owner_packet(packet_path)),
            api_key="test-secret",
            opener=refuse,
        )


def test_tampered_packet_or_proof_is_refused(tmp_path: Path) -> None:
    packet_path, proof_path, entries = _write_evidence(tmp_path)
    packet = oc.load_owner_packet(packet_path)
    proof_path.write_text(proof_path.read_text() + "\n")
    with pytest.raises(oc.OwnerCleanupError, match="bytes do not match"):
        oc.load_owner_targets(packet)

    def wrong_remote(proof: dict[str, Any]) -> dict[str, Any]:
        proof["entries"][0]["remote_id"] = "00000000-0000-0000-0000-000000000001"
        return proof

    other = tmp_path / "other"
    other.mkdir()
    packet_path, _, _ = _write_evidence(other, proof_mutator=wrong_remote)
    with pytest.raises(oc.OwnerCleanupError, match="not derived"):
        oc.load_owner_targets(oc.load_owner_packet(packet_path))

    def short_scope(packet: dict[str, Any]) -> dict[str, Any]:
        packet["trace_ids"] = packet["trace_ids"][:-1]
        return packet

    short = tmp_path / "short"
    short.mkdir()
    packet_path, _, _ = _write_evidence(short, packet_mutator=short_scope)
    with pytest.raises(oc.OwnerCleanupError, match="exactly 108"):
        oc.load_owner_packet(packet_path)


def test_receipt_for_another_packet_is_refused(tmp_path: Path) -> None:
    packet_path, _, entries = _write_evidence(tmp_path)
    receipt = _receipt(packet_path, entries, rows={})
    receipt["packet_sha256"] = "0" * 64
    with pytest.raises(oc.OwnerCleanupError, match="another owner packet"):
        oc.verify_receipt(packet_path, receipt=receipt)


def test_cli_reports_unusable_evidence(tmp_path: Path) -> None:
    packet_path, _, entries = _write_evidence(tmp_path)
    assert oc.main(["--packet", str(packet_path), "--receipt", str(tmp_path / "missing.json")]) == 2
    receipt = _receipt(packet_path, entries, rows={})
    receipt["schema"] = "unknown/v9"
    receipt_path = _write_receipt(tmp_path / "bad.json", receipt)
    assert oc.main(["--packet", str(packet_path), "--receipt", str(receipt_path)]) == 2
    with pytest.raises(oc.OwnerCleanupError, match="overwrite"):
        oc._write_private_json(receipt_path, receipt)
