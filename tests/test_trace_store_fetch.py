"""Pure tests for read-only trace lookup, source boundaries, and provider limits."""

from __future__ import annotations

import argparse
import copy
import io
import json
from typing import Any
from urllib.request import Request

import pytest

from evallab.trace_store.cli import register_traces
from evallab.trace_store.core import trace_uuid
from evallab.trace_store.docent import DocentPrivacyError, collection_name
from evallab.trace_store.fetch import fetch

DATASET = "mimo-defect-map-v1"
GOODFIRE = "goodfire-reward-hack-data"


class _Opener:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[Request] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: Request, *, timeout: int) -> io.BytesIO:
        assert timeout == 30
        self.requests.append(request)
        assert request.data is not None
        self.bodies.append(json.loads(request.data))
        return io.BytesIO(json.dumps(self.response).encode())


class _Spend:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def __call__(self, route: str, **details: Any) -> None:
        self.rows.append({"route": route, **details})


class _Docent:
    def __init__(self, datasets: tuple[str, ...] = (DATASET,)) -> None:
        self.collections = [
            {"id": f"collection-{dataset}", "name": collection_name(dataset)}
            for dataset in datasets
        ]
        self.acl: Any = None
        self.shared_after = False
        self.acl_reads: dict[str, int] = {}
        self.events: list[tuple[str, str]] = []
        self.queries: list[tuple[str, str, dict[str, Any]]] = []
        self.result: dict[str, Any] = {"columns": ["answer"], "rows": [["stored"]], "truncated": False}

    def list_collections(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.collections)

    def get_collection(self, collection_id: str) -> dict[str, Any]:
        collection = next(row for row in self.collections if row["id"] == collection_id)
        return {**collection, "is_owner": True, "my_permission": "admin",
                "created_by": "owner", "owner": {"id": "owner"}}

    def get_collection_collaborators(self, collection_id: str) -> Any:
        self.events.append(("acl", collection_id))
        self.acl_reads[collection_id] = self.acl_reads.get(collection_id, 0) + 1
        if self.acl is not None:
            return copy.deepcopy(self.acl)
        owner = {"collection_id": collection_id, "subject_type": "user",
                 "subject_id": "owner", "permission_level": "admin"}
        if self.shared_after and self.acl_reads[collection_id] > 1:
            return [owner, {"subject_type": "public", "permission_level": "read"}]
        return [owner]

    def get_dql_schema(self, collection_id: str) -> dict[str, Any]:
        self.events.append(("schema", collection_id))
        return {"tables": ["agent_runs", "transcripts"]}

    def execute_dql(self, collection_id: str, query: str, **kwargs: Any) -> dict[str, Any]:
        self.events.append(("query", collection_id))
        self.queries.append((collection_id, query, kwargs))
        return copy.deepcopy(self.result)

    def dql_result_to_dicts(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(zip(result["columns"], row, strict=True)) for row in result["rows"]]

    def get_agent_run(self, *args: Any) -> None:
        raise AssertionError("Fetch must look up deterministic IDs with DQL, not state or SDK handles")

    def read(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("Fetch must never schedule model calls")

    def create_collection(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("Fetch must not create or alter remote collections")


def test_laminar_run_lookup_binds_deterministic_id_and_dataset_without_clipping() -> None:
    run_id = "run' OR true; --"
    rows = [
        {"trace_id": trace_uuid(run_id), "start_time": i,
         "output": "Ω" * 20_001 if i == 0 else "stored"}
        for i in range(1_001)
    ]
    opener = _Opener({"data": rows, "truncated": False, "rows_read": 1_001})
    spend = _Spend()
    result = fetch("laminar", run_id=run_id, dataset=DATASET, api_key="test-key",
                   endpoint="https://laminar.invalid", opener=opener, spend_recorder=spend)
    body = opener.bodies[0]
    assert body["parameters"] == {"trace_id": trace_uuid(run_id), "dataset": DATASET}
    assert "{trace_id:UUID}" in body["query"]
    assert "JSONExtractString(metadata, 'source') = {dataset:String}" in body["query"]
    assert body["query"].endswith("ORDER BY start_time ASC")
    assert "LIMIT" not in body["query"]
    assert run_id not in body["query"]
    request = opener.requests[0]
    assert request.full_url == "https://laminar.invalid/v1/sql/query"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer test-key"
    assert result["rows"] == rows
    assert result["server"] == {"truncated": False, "rows_read": 1_001}
    assert result["remote_id"] == trace_uuid(run_id)
    json.dumps(result)
    assert spend.rows[0]["route"] == "laminar-fetch"
    assert spend.rows[0]["cost_usd"] == 0
    assert spend.rows[0]["queries"] == 1
    assert spend.rows[0]["rows"] == len(rows)
    assert "no model" in spend.rows[0]["basis"]
    assert "query" not in spend.rows[0]
    assert "labels" not in spend.rows[0]


def test_laminar_raw_sql_passes_through_and_exposes_server_pagination() -> None:
    query = "SELECT trace_id FROM spans WHERE metadata = {dataset:String} /*" + "x" * 70_000 + "*/"
    opener = _Opener({"data": [{"trace_id": "stored"}], "truncated": True,
                      "next_page_token": "server-token", "applied_limit": 50_000})
    result = fetch("laminar", query=query, dataset=DATASET, api_key="test-key", opener=opener,
                   spend_recorder=_Spend())
    assert opener.bodies == [{"query": query, "parameters": {"dataset": DATASET}}]
    assert result["server"] == {"truncated": True, "next_page_token": "server-token",
                                "applied_limit": 50_000}
    assert result["remote_id"] is None


def test_laminar_query_without_dataset_has_no_invented_parameters() -> None:
    opener = _Opener({"data": []})
    fetch("laminar", query="SELECT count() FROM spans", api_key="key", opener=opener,
          spend_recorder=_Spend())
    assert opener.bodies[0]["parameters"] == {}


@pytest.mark.parametrize("kwargs", [
    {}, {"run_id": "r", "query": "SELECT id FROM spans"}, {"run_id": ""},
    {"run_id": " "}, {"query": ""}, {"query": "\n"},
    {"run_id": "r", "dataset": "sealed-test-data"},
])
def test_selection_bounds_fail_before_network_or_ledger(kwargs: dict[str, Any]) -> None:
    opener = _Opener({"data": []})
    spend = _Spend()
    with pytest.raises(ValueError):
        fetch("laminar", api_key="key", opener=opener, spend_recorder=spend, **kwargs)
    assert opener.requests == []
    assert spend.rows == []


def test_unknown_source_fails_before_any_hosted_call() -> None:
    with pytest.raises(ValueError, match="source"):
        fetch("other", run_id="run", spend_recorder=_Spend())


def test_malformed_laminar_response_is_not_empty_success() -> None:
    spend = _Spend()
    with pytest.raises(RuntimeError, match="stored query rows"):
        fetch("laminar", run_id="r", api_key="key", opener=_Opener({"status": "unknown"}),
              spend_recorder=spend)
    assert spend.rows[0]["outcome"] == "error"
    assert spend.rows[0]["cost_usd"] == 0


def test_docent_run_lookup_uses_schema_dql_and_preserves_entire_transcript() -> None:
    client = _Docent()
    messages = [{"role": "assistant", "content": [{"type": "reasoning", "reasoning": "Ω" * 20_001}],
                 "tool_calls": [{"id": "call", "function": "bash_command", "arguments": {"x": 1}}]},
                {"role": "tool", "tool_call_id": "call", "content": "result" * 10_000}]
    metadata = {"run_id": "local-db-id", "source": DATASET}
    client.result = {
        "columns": ["remote_id", "metadata", "transcript_id", "messages"],
        "rows": [[trace_uuid("local-db-id"), json.dumps(metadata), "transcript", json.dumps(messages)]],
        "truncated": False,
    }
    spend = _Spend()
    result = fetch("docent", run_id="local-db-id", dataset=DATASET, client=client,
                   spend_recorder=spend)
    collection_id = f"collection-{DATASET}"
    assert client.events == [("acl", collection_id), ("schema", collection_id),
                             ("query", collection_id), ("acl", collection_id)]
    query = client.queries[0][1]
    assert trace_uuid("local-db-id") in query
    assert "local-db-id" not in query
    assert "convert_from(t.messages, 'UTF8')" in query
    assert "LIMIT" not in query
    assert client.queries[0][2] == {"ensure_latest_results": True}
    rows = result["collections"][0]["rows"]
    assert rows[0]["messages"] == messages
    assert rows[0]["metadata"] == metadata
    json.dumps(result)
    assert spend.rows[0]["route"] == "docent-fetch"
    assert spend.rows[0]["cost_usd"] == 0
    assert spend.rows[0]["rows"] == 1


def test_docent_no_dataset_searches_only_allowlisted_exact_private_collections() -> None:
    client = _Docent((DATASET, GOODFIRE))
    client.collections.extend([
        {"id": "third-party", "name": "evallab-trace-store/third-party (private)"},
        {"id": "near-match", "name": collection_name(DATASET) + " copy"},
    ])
    result = fetch("docent", run_id="db-id", client=client, spend_recorder=_Spend())
    assert {collection["dataset"] for collection in result["collections"]} == {DATASET, GOODFIRE}
    assert {call[0] for call in client.queries} == {f"collection-{DATASET}", f"collection-{GOODFIRE}"}
    assert all(trace_uuid("db-id") in call[1] for call in client.queries)


def test_docent_raw_dql_is_not_rewritten_or_locally_capped() -> None:
    client = _Docent((DATASET, GOODFIRE))
    query = "SELECT id, metadata_json FROM agent_runs ORDER BY created_at"
    client.result = {"columns": ["metadata"], "rows": [["raw-value"]] * 1_001,
                     "truncated": True, "applied_limit": 1_001}
    result = fetch("docent", query=query, dataset=DATASET, client=client, spend_recorder=_Spend())
    assert len(client.queries) == 1
    assert client.queries[0] == (f"collection-{DATASET}", query, {"ensure_latest_results": True})
    collection = result["collections"][0]
    assert len(collection["rows"]) == 1_001
    assert collection["rows"][0] == {"metadata": "raw-value"}
    assert collection["server"]["truncated"] is True
    assert collection["server"]["applied_limit"] == 1_001


@pytest.mark.parametrize("acl", [[], {}, [{"subject_type": "public", "permission_level": "read"}],
                                  [{"subject_type": "organization", "subject_id": "owner", "permission_level": "admin"}]])
def test_docent_unknown_or_shared_collections_are_never_queried(acl: Any) -> None:
    client = _Docent()
    client.acl = acl
    spend = _Spend()
    with pytest.raises(DocentPrivacyError):
        fetch("docent", query="SELECT id FROM agent_runs", client=client, spend_recorder=spend)
    assert client.queries == []
    assert spend.rows[0]["outcome"] == "error"


def test_docent_privacy_is_rechecked_after_fetch() -> None:
    client = _Docent()
    client.shared_after = True
    with pytest.raises(DocentPrivacyError):
        fetch("docent", run_id="r", client=client, spend_recorder=_Spend())
    assert len(client.queries) == 1


def test_docent_duplicate_or_absent_exact_collection_does_not_guess() -> None:
    client = _Docent()
    client.collections.append({"id": "duplicate", "name": collection_name(DATASET)})
    with pytest.raises(DocentPrivacyError, match="Duplicate exact"):
        fetch("docent", run_id="r", dataset=DATASET, client=client, spend_recorder=_Spend())
    assert client.queries == []
    empty = _Docent(())
    with pytest.raises(ValueError, match="No exact private"):
        fetch("docent", run_id="r", dataset=DATASET, client=empty, spend_recorder=_Spend())
    assert empty.queries == []


def test_cli_selection_and_limit_bounds_are_parser_errors() -> None:
    parser = argparse.ArgumentParser()
    register_traces(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["traces", "fetch", "--source", "docent", "--run-id", "r"])
    assert args.traces_command == "fetch"
    assert args.run_id == "r"
    assert callable(args.func)
    args = parser.parse_args(["traces", "sync", "--target", "laminar", "--limit", "3"])
    assert args.limit == 3
    for argv in (
        ["traces", "fetch", "--source", "docent"],
        ["traces", "fetch", "--source", "docent", "--run-id", "r", "--query", "SELECT id FROM agent_runs"],
        ["traces", "sync", "--target", "laminar", "--limit", "0"],
        ["traces", "sync", "--target", "laminar", "--limit", "-1"],
    ):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(argv)
        assert exc.value.code == 2
