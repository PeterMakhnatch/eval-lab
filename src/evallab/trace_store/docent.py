"""Private, deterministic Docent ingestion using the pinned 0.1.88 wire format.

The public SDK uploader rejects caller-supplied IDs and has no replace/upsert
operation. This adapter uses its authenticated session and ingestion endpoint,
then verifies the persisted deterministic identity. An existing identical run is
reused; a changed existing run fails closed rather than deleting evidence.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Callable, Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from evallab.trace_store.core import ALLOWED_DATASETS, METADATA_FIELDS, TracePayload, trace_uuid
from evallab.trace_store.secrets import SecretScanRejected, secret_hits

SDK_VERSION = "0.1.88"
MAX_PAYLOAD_BYTES = 100 * 1024 * 1024  # Docent's documented single-request limit.


class DocentPrivacyError(RuntimeError):
    """Collection ownership or its complete owner-only ACL cannot be proven."""


class DocentContentConflict(RuntimeError):
    """An existing deterministic run differs; Docent has no supported upsert."""


def _require_sdk() -> None:
    try:
        installed = version("docent")
    except PackageNotFoundError:
        raise RuntimeError("Docent trace storage requires optional docent==0.1.88") from None
    if installed != SDK_VERSION:
        raise RuntimeError("Docent trace storage requires optional docent==0.1.88")


def new_client() -> Any:
    """Construct only the storage/query client; never schedule readings or a UI."""
    _require_sdk()
    from docent import Docent  # ty: ignore[unresolved-import]

    client = Docent(log_stream=sys.stderr)
    client.auto_flush = False
    return client


def _sdk_models() -> tuple[Any, Any, Callable[..., Any]]:
    _require_sdk()
    from docent.data_models import AgentRun, Transcript  # ty: ignore[unresolved-import]
    from docent.data_models.chat import parse_chat_message  # ty: ignore[unresolved-import]

    return AgentRun, Transcript, parse_chat_message


def collection_name(dataset: str) -> str:
    if dataset not in ALLOWED_DATASETS:
        raise ValueError("Dataset is not admitted to the private trace store")
    return f"evallab-trace-store/{dataset} (private)"


def resolve_collections(
    client: Any, *, dataset: str | None = None, create: bool = False,
) -> list[dict[str, Any]]:
    """Resolve only exact allowlisted names, rejecting ambiguous collections."""
    datasets = [dataset] if dataset is not None else sorted(ALLOWED_DATASETS)
    names = {collection_name(source): source for source in datasets}
    listed = client.list_collections()
    if not isinstance(listed, list) or any(not isinstance(row, Mapping) for row in listed):
        raise DocentPrivacyError("Cannot resolve Docent collection names")
    result = []
    for name, source in names.items():
        matches = [dict(row) for row in listed if row.get("name") == name]
        if len(matches) > 1:
            raise DocentPrivacyError(f"Duplicate exact Docent collection name: {name}")
        if not matches and create:
            collection_id = client.create_collection(name=name)
            if not isinstance(collection_id, str) or not collection_id:
                raise DocentPrivacyError("Docent did not return a collection ID")
            matches = [{"id": collection_id, "name": name}]
        if matches:
            row = matches[0]
            if not isinstance(row.get("id"), str) or not row["id"]:
                raise DocentPrivacyError("Docent collection has no valid ID")
            result.append({"id": row["id"], "name": name, "dataset": source})
    if dataset is not None and not result:
        raise ValueError("No exact private Docent collection exists for the dataset")
    return result


def require_owner_only(client: Any, collection: Mapping[str, Any]) -> str:
    """Require the authenticated owner and exactly their single user-admin ACL."""
    collection_id = collection["id"]
    details = client.get_collection(collection_id)
    collaborators = client.get_collection_collaborators(collection_id)
    if not isinstance(details, Mapping):
        raise DocentPrivacyError("Docent collection ownership is unknown")
    owner = details.get("owner")
    owner_id = owner.get("id") if isinstance(owner, Mapping) else None
    if (
        details.get("id") != collection_id
        or details.get("name") != collection["name"]
        or details.get("is_owner") is not True
        or details.get("my_permission") != "admin"
        or not isinstance(owner_id, str)
        or not owner_id
        or details.get("created_by") != owner_id
    ):
        raise DocentPrivacyError("Docent collection is not proven to belong to the authenticated owner")
    if not isinstance(collaborators, list) or len(collaborators) != 1:
        raise DocentPrivacyError("Docent collection ACL is unknown or shared")
    collaborator = collaborators[0]
    if (
        not isinstance(collaborator, Mapping)
        or collaborator.get("subject_type") != "user"
        or collaborator.get("subject_id") != owner_id
        or collaborator.get("permission_level") != "admin"
        or collaborator.get("collection_id", collection_id) != collection_id
    ):
        raise DocentPrivacyError("Docent collection ACL is not owner-only")
    return owner_id


def agent_run(trace: TracePayload) -> Any:
    """Convert every turn without clipping reasoning, function calls, or results."""
    collection_name(trace.dataset_id)
    if (
        set(trace.metadata) != METADATA_FIELDS
        or trace.metadata["run_id"] != trace.trace_id
        or trace.metadata["source"] != trace.dataset_id
        or trace.metadata["split"] != "DEV"
        or any(value is not None and not isinstance(value, str) for value in trace.metadata.values())
    ):
        raise ValueError("Docent metadata must contain exactly the admitted scalar fields")
    AgentRun, Transcript, parse_chat_message = _sdk_models()
    messages = []
    for message in trace.messages:
        content: Any = message["content"]
        reasoning = message.get("reasoning")
        if reasoning is not None:
            content = [
                {"type": "reasoning", "reasoning": reasoning},
                {"type": "text", "text": content},
            ]
        payload = {"role": message["role"], "content": content}
        if message["role"] == "assistant":
            payload["tool_calls"] = [
                {"id": call["id"], "function": call["function"], "arguments": call["arguments"], "type": "function"}
                for call in message.get("tool_calls", [])
            ]
        elif message["role"] == "tool":
            payload["tool_call_id"] = message.get("tool_call_id")
        messages.append(parse_chat_message(payload))
    remote_id = trace_uuid(trace.trace_id)
    transcript_id = str(uuid.uuid5(uuid.UUID(remote_id), "transcript/0"))
    return AgentRun(
        id=remote_id,
        transcripts=[Transcript(id=transcript_id, messages=messages, metadata={})],
        metadata=dict(trace.metadata),
    )


def _content(run: Any) -> dict[str, Any]:
    if hasattr(run, "model_dump"):
        run = run.model_dump(mode="json")
    if not isinstance(run, Mapping):
        raise RuntimeError("Docent returned an invalid agent run")
    # created_at is server bookkeeping, not source conversation content.
    result = {key: run.get(key) for key in ("id", "name", "description", "metadata", "transcript_groups")}
    transcripts = run.get("transcripts")
    if not isinstance(transcripts, list) or any(not isinstance(row, Mapping) for row in transcripts):
        raise RuntimeError("Docent returned invalid transcripts")
    result["transcripts"] = [
        {key: value for key, value in transcript.items() if key != "created_at"}
        for transcript in transcripts
    ]
    return result


def _stored_runs(client: Any, collection_id: str, remote_ids: list[str]) -> dict[str, Any]:
    # IDs come only from trace_uuid, not user-controlled SQL fragments.
    # The collection-scoped DQL LEFT JOIN to optional groups drops ungrouped
    # runs (observed on 0.1.88). Read the transcript's nullable group id
    # instead, without excluding the flat transcripts emitted by this store.
    identities = ", ".join(f"'{uuid.UUID(identity)}'" for identity in remote_ids)
    query = (
        "SELECT ar.id AS remote_id, ar.name, ar.description, ar.metadata_json AS metadata,"
        " t.id AS transcript_id, t.name AS transcript_name, t.description AS transcript_description,"
        " t.transcript_group_id, convert_from(t.messages, 'UTF8') AS messages,"
        " convert_from(t.metadata_json, 'UTF8') AS transcript_metadata,"
        " t.transcript_group_id AS group_id"
        " FROM agent_runs ar LEFT JOIN transcripts t ON t.agent_run_id = ar.id"
        f" WHERE ar.id IN ({identities}) ORDER BY ar.created_at ASC, t.created_at ASC"
    )
    response = client.execute_dql(collection_id, query, ensure_latest_results=True)
    if not isinstance(response, Mapping) or response.get("truncated") is True:
        raise RuntimeError("Docent deterministic lookup was truncated or invalid; sync paused")
    rows = client.dql_result_to_dicts(response)
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise RuntimeError("Docent deterministic lookup returned invalid rows")
    result: dict[str, Any] = {}
    for row in rows:
        remote_id = row.get("remote_id")
        if remote_id not in remote_ids:
            raise RuntimeError("Docent lookup returned an unexpected agent run")
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        run = result.setdefault(remote_id, {
            "id": remote_id, "name": row.get("name"), "description": row.get("description"),
            "metadata": metadata, "transcripts": [], "transcript_groups": [],
        })
        if row.get("group_id") is not None:
            run["transcript_groups"].append({"id": row["group_id"]})
        if row.get("transcript_id") is not None:
            messages = row.get("messages")
            if isinstance(messages, str):
                messages = json.loads(messages)
            transcript_metadata = row.get("transcript_metadata")
            if isinstance(transcript_metadata, str):
                transcript_metadata = json.loads(transcript_metadata)
            run["transcripts"].append({
                "id": row["transcript_id"], "name": row.get("transcript_name"),
                "description": row.get("transcript_description"),
                "transcript_group_id": row.get("transcript_group_id"),
                "messages": messages, "metadata": transcript_metadata,
            })
    return result


class DocentTarget:
    def __init__(self, client: Any = None, *, max_bytes: int | None = None) -> None:
        self.client = client if client is not None else new_client()
        self.max_bytes = max_bytes
        self.reserve_bytes: Callable[[int], None] | None = None
        self.sent_bytes = 0
        self.requests = 0
        self._schema_ready: set[str] = set()

    def upload(self, trace: TracePayload) -> dict[str, Any]:
        return self.upload_batch([trace])[0]

    def upload_batch(self, traces: list[TracePayload]) -> list[dict[str, Any]]:
        """Upload one dataset batch, checking its complete private ACL on each side.

        Returned byte allocations sum exactly to the logical JSON POST body;
        unchanged remote runs receive zero, and the first inserted run owns the
        envelope. No server data is ever deleted or overwritten.
        """
        if not traces:
            return []
        dataset = traces[0].dataset_id
        if any(trace.dataset_id != dataset for trace in traces):
            raise ValueError("Docent upload batches must contain exactly one dataset")
        runs = [agent_run(trace).model_dump(mode="json") for trace in traces]
        remote_ids = [run["id"] for run in runs]
        if len(set(remote_ids)) != len(remote_ids):
            raise ValueError("Docent upload batch contains duplicate trace identities")
        for trace, run in zip(traces, runs, strict=True):
            if secret_hits(run):
                raise SecretScanRejected(trace.trace_id)
        collection = resolve_collections(self.client, dataset=dataset, create=True)[0]
        collection_id = collection["id"]
        owner_id = require_owner_only(self.client, collection)
        results = [
            {"remote_id": identity, "payload_bytes": 0, "collection_id": collection_id}
            for identity in remote_ids
        ]
        try:
            if collection_id not in self._schema_ready:
                self.client.get_dql_schema(collection_id)
                self._schema_ready.add(collection_id)
            existing = _stored_runs(self.client, collection_id, remote_ids)
            for run in runs:
                if run["id"] in existing and _content(existing[run["id"]]) != _content(run):
                    raise DocentContentConflict(
                        "Existing deterministic Docent run changed; no supported upsert; sync paused"
                    )
            missing = [run for run in runs if run["id"] not in existing]
            if not missing:
                return results
            serialized = [
                json.dumps(run, separators=(",", ":")).encode("utf-8") for run in missing
            ]
            prefix, suffix = b'{"agent_runs":[', b"]}"
            body = prefix + b",".join(serialized) + suffix
            if len(body) > MAX_PAYLOAD_BYTES:
                raise RuntimeError("Docent batch exceeds its 100 MiB request limit; nothing was clipped")
            if self.max_bytes is not None and self.sent_bytes + len(body) > self.max_bytes:
                raise RuntimeError("Docent payload byte allowance exhausted; upload paused")
            if self.reserve_bytes is not None:
                self.reserve_bytes(len(body))
            # Avoid SDK retries so sync owns every retry and byte reservation.
            self.sent_bytes += len(body)
            self.requests += 1
            response = self.client._session.post(
                self.client.backend_url.rstrip("/") + f"/{collection_id}/agent_runs",
                data=body, headers={"Content-Type": "application/json"}, timeout=120,
            )
            self.client._handle_response_errors(response)
            try:
                result = response.json()
            except ValueError:
                result = None
            job_id = result.get("job_id") if isinstance(result, Mapping) else None
            if job_id:
                self.client._wait_for_jobs(collection_id, [job_id])
            elif response.status_code == 202:
                raise RuntimeError("Docent enqueued ingestion without returning a job ID")
            persisted = _stored_runs(self.client, collection_id, [run["id"] for run in missing])
            for run in missing:
                if run["id"] not in persisted or _content(persisted[run["id"]]) != _content(run):
                    raise RuntimeError("Docent did not persist the complete deterministic agent run")
            allocations = {
                run["id"]: len(encoded) + (1 if index else len(prefix) + len(suffix))
                for index, (run, encoded) in enumerate(zip(missing, serialized, strict=True))
            }
            for result in results:
                result["payload_bytes"] = allocations.get(result["remote_id"], 0)
            return results
        finally:
            if require_owner_only(self.client, collection) != owner_id:
                raise DocentPrivacyError("Docent collection owner changed during upload")
