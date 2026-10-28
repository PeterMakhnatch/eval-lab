"""Read stored Laminar spans or private Docent runs without model calls."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.request import urlopen

from evallab import laminar
from evallab.trace_store.core import ALLOWED_DATASETS, trace_uuid
from evallab.trace_store.state import record_spend


def _laminar_fetch(
    *, run_id: str | None, query: str | None, dataset: str | None,
    api_key: str | None, endpoint: str | None, opener: Callable[..., Any],
    stats: dict[str, int],
) -> dict[str, Any]:
    key = api_key if api_key is not None else os.environ.get(laminar.LAMINAR_KEY_ENV, "")
    if not key:
        raise ValueError("LMNR_PROJECT_API_KEY is required (use keys run --)")
    parameters: dict[str, Any] = {}
    remote_id = trace_uuid(run_id) if run_id is not None else None
    if run_id is not None:
        query = "SELECT * FROM spans WHERE trace_id = {trace_id:UUID}"
        parameters["trace_id"] = remote_id
        if dataset is not None:
            query += (
                " AND trace_id IN (SELECT id FROM traces"
                " WHERE JSONExtractString(metadata, 'source') = {dataset:String})"
            )
        query += " ORDER BY start_time ASC"
    if dataset is not None:
        # Raw SQL can reference {dataset:String}; aggregates have no safe generic
        # outer dataset filter. The read-only API owns SQL parsing and limits.
        parameters["dataset"] = dataset
    stats["queries"] += 1
    response = laminar._json_call(
        "POST", "/v1/sql/query", api_key=key,
        endpoint=endpoint or os.environ.get(laminar.LAMINAR_ENDPOINT_ENV) or laminar.LAMINAR_ENDPOINT,
        body={"query": query, "parameters": parameters}, opener=opener,
    )
    if not isinstance(response, Mapping) or not isinstance(response.get("data"), list):
        raise RuntimeError("Laminar SQL did not return stored query rows")
    rows = response["data"]
    stats["rows"] = len(rows)
    return {
        "source": "laminar", "run_id": run_id, "remote_id": remote_id,
        "dataset": dataset, "rows": rows,
        "server": {key: value for key, value in response.items() if key != "data"},
    }


def _docent_fetch(
    *, run_id: str | None, query: str | None, dataset: str | None,
    client: Any, stats: dict[str, int],
) -> dict[str, Any]:
    from evallab.trace_store.docent import new_client, require_owner_only, resolve_collections

    client = client if client is not None else new_client()
    collections = resolve_collections(client, dataset=dataset)
    remote_id = trace_uuid(run_id) if run_id is not None else None
    if run_id is not None:
        # Only the UUID produced by trace_uuid is interpolated, never caller SQL.
        query = (
            "SELECT ar.id AS remote_id, ar.metadata_json AS metadata,"
            " t.id AS transcript_id, convert_from(t.messages, 'UTF8') AS messages"
            " FROM agent_runs ar JOIN transcripts t ON t.agent_run_id = ar.id"
            f" WHERE ar.id = '{remote_id}' ORDER BY t.created_at ASC"
        )
    results = []
    for collection in collections:
        owner_id = require_owner_only(client, collection)
        try:
            client.get_dql_schema(collection["id"])
            stats["queries"] += 1
            response = client.execute_dql(
                collection["id"], query, ensure_latest_results=True,
            )
            rows = client.dql_result_to_dicts(response)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise RuntimeError("Docent DQL did not return stored query rows")
            if run_id is not None:
                for row in rows:
                    for field in ("messages", "metadata"):
                        value = row.get(field)
                        if isinstance(value, str):
                            row[field] = json.loads(value)
        finally:
            if require_owner_only(client, collection) != owner_id:
                raise RuntimeError("Docent collection owner changed during fetch")
        stats["rows"] += len(rows)
        results.append({
            "collection_id": collection["id"], "dataset": collection["dataset"],
            "rows": rows,
            "server": {key: value for key, value in response.items() if key != "rows"},
        })
    return {
        "source": "docent", "run_id": run_id, "remote_id": remote_id,
        "dataset": dataset, "collections": results,
    }


def fetch(
    source: str, *, run_id: str | None = None, query: str | None = None,
    dataset: str | None = None, client: Any = None,
    opener: Callable[..., Any] = urlopen, api_key: str | None = None,
    endpoint: str | None = None, spend_recorder: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Fetch exactly one run or a raw provider read-only query, without local state.

    No client-side row/text caps are imposed. Provider pagination/truncation
    fields are retained in ``server``. For raw Laminar SQL, ``dataset`` supplies
    the bound ``{dataset:String}`` parameter, not an implicit aggregate rewrite.
    Inject the client/opener and spend recorder for entirely local tests.
    """
    if source not in {"laminar", "docent"}:
        raise ValueError("source must be laminar or docent")
    if (run_id is None) == (query is None):
        raise ValueError("Specify exactly one of run_id or query")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError("run_id must be nonempty")
    if query is not None and (not isinstance(query, str) or not query.strip()):
        raise ValueError("query must be nonempty")
    if dataset is not None and dataset not in ALLOWED_DATASETS:
        raise ValueError("Dataset is not admitted to the private trace store")
    started = time.monotonic()
    stats = {"queries": 0, "rows": 0}
    outcome = "error"
    recorder = spend_recorder if spend_recorder is not None else record_spend
    try:
        if source == "laminar":
            result = _laminar_fetch(
                run_id=run_id, query=query, dataset=dataset, api_key=api_key,
                endpoint=endpoint, opener=opener, stats=stats,
            )
        else:
            result = _docent_fetch(
                run_id=run_id, query=query, dataset=dataset, client=client, stats=stats,
            )
        outcome = "ok"
        return result
    finally:
        recorder(
            source + "-fetch", operation="fetch", cost_usd=0,
            basis="Read-only stored trace APIs within existing allowance; no model or paid Signal calls",
            dataset=dataset, queries=stats["queries"], rows=stats["rows"], outcome=outcome,
            wall_seconds=round(time.monotonic() - started, 3),
        )
