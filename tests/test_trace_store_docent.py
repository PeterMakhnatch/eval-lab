"""Pure Docent conversion, privacy, deterministic replay, and byte accounting."""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import replace
from typing import Any

import pytest

from evallab.trace_store import docent
from evallab.trace_store.core import TracePayload, trace_uuid
from evallab.trace_store.secrets import SecretScanRejected

DATASET = "mimo-defect-map-v1"
COLLECTION_ID = "collection-private"
OWNER_ID = "owner-user"


class _Model:
    def __init__(self, **fields: Any) -> None:
        self.fields = fields

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {
            key: [item.model_dump(mode=mode) if isinstance(item, _Model) else copy.deepcopy(item)
                  for item in value] if isinstance(value, list) else copy.deepcopy(value)
            for key, value in self.fields.items()
        }


class _Transcript(_Model):
    def __init__(self, **fields: Any) -> None:
        super().__init__(
            **{"name": None, "description": None, "transcript_group_id": None,
               "created_at": None, **fields}
        )


class _AgentRun(_Model):
    def __init__(self, **fields: Any) -> None:
        super().__init__(**{"name": None, "description": None, "transcript_groups": [], **fields})


@pytest.fixture(autouse=True)
def _pure_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    # Model construction and parse_chat_message are the optional SDK boundary;
    # these tests exercise our mapping and transport, not SDK/network wiring.
    monkeypatch.setattr(
        docent, "_sdk_models", lambda: (_AgentRun, _Transcript, lambda message: _Model(**message))
    )
    monkeypatch.setattr(docent, "secret_hits", lambda payload: [])


@pytest.fixture
def trace() -> TracePayload:
    return TracePayload(
        trace_id="own-run-001", dataset_id=DATASET,
        metadata={
            "run_id": "own-run-001", "source": DATASET, "benchmark": "mimo",
            "model": "test-model", "task_id": "task-1", "outcome": "unknown", "split": "DEV",
        },
        messages=[
            {"step_index": 0, "role": "user", "content": "Solve Ω", "reasoning": None,
             "tool_calls": [], "tool_call_id": None},
            {"step_index": 1, "role": "assistant", "content": "answer 🐍",
             "reasoning": "reasoning Ω" * 2_500,
             "tool_calls": [{"id": "call-1", "function": "bash_command",
                             "arguments": {"command": "printf Ω", "long": "a" * 20_001}}],
             "tool_call_id": None},
            {"step_index": 1, "role": "tool", "content": "result Ω" * 5_000,
             "reasoning": None, "tool_calls": [], "tool_call_id": "call-1"},
        ],
    )


def _renamed(trace: TracePayload, identity: str) -> TracePayload:
    return replace(trace, trace_id=identity, metadata={**trace.metadata, "run_id": identity})


def _details(dataset: str = DATASET, identity: str = COLLECTION_ID) -> dict[str, Any]:
    return {
        "id": identity, "name": docent.collection_name(dataset), "created_by": OWNER_ID,
        "owner": {"id": OWNER_ID}, "is_owner": True, "my_permission": "admin",
    }


def _acl() -> list[dict[str, Any]]:
    return [{"collection_id": COLLECTION_ID, "subject_type": "user", "subject_id": OWNER_ID,
             "subject": {"id": OWNER_ID}, "permission_level": "admin"}]


class _Response:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code = status
        self.payload = payload

    def json(self) -> Any:
        return self.payload


class _Session:
    def __init__(self, client: _Client) -> None:
        self.client = client
        self.posts: list[dict[str, Any]] = []

    def post(self, url: str, *, data: bytes, headers: dict[str, str], timeout: int) -> _Response:
        self.client.events.append("post")
        self.posts.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        if self.client.http_status < 400 and not self.client.drop_runs:
            for run in json.loads(data)["agent_runs"]:
                self.client.stored[run["id"]] = run
        return _Response(self.client.http_status, self.client.post_result)


class _Client:
    backend_url = "https://docent.invalid/rest"

    def __init__(self, collections: list[dict[str, Any]] | None = None) -> None:
        self.collections = [_details()] if collections is None else collections
        self.details: Any = _details()
        self.acl: Any = _acl()
        self.after_acl: Any = None
        self.stored: dict[str, Any] = {}
        self.events: list[str] = []
        self.queries: list[tuple[str, str, dict[str, Any]]] = []
        self.schema_calls: list[str] = []
        self.created: list[str] = []
        self.waited: list[Any] = []
        self.drop_runs = False
        self.truncated = False
        self.http_status = 200
        self.post_result: Any = {"status": "success"}
        self._session = _Session(self)

    def list_collections(self) -> Any:
        return copy.deepcopy(self.collections)

    def create_collection(self, *, name: str) -> str:
        self.created.append(name)
        self.collections.append({"id": COLLECTION_ID, "name": name})
        self.details["name"] = name
        return COLLECTION_ID

    def get_collection(self, collection_id: str) -> Any:
        assert collection_id == COLLECTION_ID
        return copy.deepcopy(self.details)

    def get_collection_collaborators(self, collection_id: str) -> Any:
        assert collection_id == COLLECTION_ID
        self.events.append("acl")
        if self._session.posts and self.after_acl is not None:
            return copy.deepcopy(self.after_acl)
        return copy.deepcopy(self.acl)

    def get_dql_schema(self, collection_id: str) -> dict[str, Any]:
        self.events.append("schema")
        self.schema_calls.append(collection_id)
        return {"tables": ["agent_runs", "transcripts", "transcript_groups"]}

    def execute_dql(self, collection_id: str, query: str, **kwargs: Any) -> dict[str, Any]:
        self.events.append("dql")
        self.queries.append((collection_id, query, kwargs))
        identities = re.findall(r"'([0-9a-f-]{36})'", query)
        rows = []
        for identity in identities:
            run = self.stored.get(identity)
            if run is None:
                continue
            for transcript in run["transcripts"]:
                rows.append({
                    "remote_id": identity, "name": run["name"], "description": run["description"],
                    "metadata": run["metadata"], "transcript_id": transcript["id"],
                    "transcript_name": transcript["name"],
                    "transcript_description": transcript["description"],
                    "transcript_group_id": transcript["transcript_group_id"],
                    "messages": json.dumps(transcript["messages"]),
                    "transcript_metadata": json.dumps(transcript["metadata"]), "group_id": None,
                })
        return {"rows": rows, "truncated": self.truncated}

    def dql_result_to_dicts(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        return result["rows"]

    def _handle_response_errors(self, response: _Response) -> None:
        if response.status_code >= 400:
            raise RuntimeError("Storage HTTP error")

    def _wait_for_jobs(self, collection_id: str, job_ids: list[str]) -> None:
        self.waited.append((collection_id, job_ids))

    def delete_agent_runs(self, *args: Any) -> None:
        raise AssertionError("Never delete remote evidence to emulate an upsert")

    def read(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("Storage must not call a model")

    def open_collection(self, *args: Any) -> None:
        raise AssertionError("Storage must not open a browser")


def test_conversion_preserves_all_reasoning_calls_results(trace: TracePayload) -> None:
    run = docent.agent_run(trace).model_dump(mode="json")
    assert run["id"] == trace_uuid(trace.trace_id)
    transcript = run["transcripts"][0]
    assert transcript["id"] == str(uuid.uuid5(uuid.UUID(run["id"]), "transcript/0"))
    assert transcript["metadata"] == {}
    assert set(run["metadata"]) == docent.METADATA_FIELDS
    messages = transcript["messages"]
    assert messages[0]["content"] == trace.messages[0]["content"]
    assert messages[1]["content"] == [
        {"type": "reasoning", "reasoning": trace.messages[1]["reasoning"]},
        {"type": "text", "text": trace.messages[1]["content"]},
    ]
    assert messages[1]["tool_calls"] == [{**trace.messages[1]["tool_calls"][0], "type": "function"}]
    assert messages[2]["content"] == trace.messages[2]["content"]
    assert messages[2]["tool_call_id"] == "call-1"
    assert docent.agent_run(trace).model_dump(mode="json") == run


@pytest.mark.parametrize("field", ["reward", "is_exploit", "labels", "flags", "trust_tier", "receipt"])
def test_metadata_rejects_every_extra_field(trace: TracePayload, field: str) -> None:
    with pytest.raises(ValueError, match="exactly"):
        docent.agent_run(replace(trace, metadata={**trace.metadata, field: "forbidden"}))


@pytest.mark.parametrize("updates", [{"run_id": "other"}, {"source": "other"}, {"split": "TEST"}, {"model": {"label": 1}}])
def test_metadata_identity_and_split_fail_closed(trace: TracePayload, updates: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="metadata"):
        docent.agent_run(replace(trace, metadata={**trace.metadata, **updates}))


def test_metadata_missing_field_is_not_filled(trace: TracePayload) -> None:
    metadata = dict(trace.metadata)
    del metadata["outcome"]
    with pytest.raises(ValueError):
        docent.agent_run(replace(trace, metadata=metadata))


@pytest.mark.parametrize("acl", [None, [], {}, [None], [{"subject_type": "public", "permission_level": "read"}], [{"subject_type": "organization", "subject_id": OWNER_ID, "permission_level": "admin"}], [{"subject_type": "user", "subject_id": "someone-else", "permission_level": "admin"}], [* _acl(), {"subject_type": "user", "subject_id": "other", "permission_level": "read"}]])
def test_unknown_or_shared_privacy_prevents_upload(trace: TracePayload, acl: Any) -> None:
    client = _Client()
    client.acl = acl
    with pytest.raises(docent.DocentPrivacyError):
        docent.DocentTarget(client).upload(trace)
    assert client._session.posts == []
    assert client.queries == []


@pytest.mark.parametrize("field,value", [("is_owner", False), ("is_owner", None), ("my_permission", "write"), ("created_by", "different-owner"), ("owner", None), ("name", "private-looking-name")])
def test_unknown_collection_owner_prevents_upload(trace: TracePayload, field: str, value: Any) -> None:
    client = _Client()
    client.details[field] = value
    with pytest.raises(docent.DocentPrivacyError):
        docent.DocentTarget(client).upload(trace)
    assert client._session.posts == []


def test_privacy_checked_after_success_and_failure(trace: TracePayload) -> None:
    client = _Client()
    target = docent.DocentTarget(client)
    result = target.upload(trace)
    post = client._session.posts[0]
    assert client.events == ["acl", "schema", "dql", "post", "dql", "acl"]
    assert result == {"remote_id": trace_uuid(trace.trace_id), "payload_bytes": len(post["data"]),
                      "collection_id": COLLECTION_ID}
    assert target.sent_bytes == len(post["data"])
    assert target.requests == 1
    assert post["url"] == "https://docent.invalid/rest/collection-private/agent_runs"
    assert post["headers"] == {"Content-Type": "application/json"}
    assert json.loads(post["data"])["agent_runs"][0]["metadata"] == trace.metadata
    failed = _Client()
    failed.http_status = 503
    target = docent.DocentTarget(failed)
    with pytest.raises(RuntimeError, match="HTTP error"):
        target.upload(trace)
    assert failed.events[-1] == "acl"
    assert target.sent_bytes == len(failed._session.posts[0]["data"])
    assert target.requests == 1


def test_after_upload_sharing_is_fatal_and_not_acknowledged(trace: TracePayload) -> None:
    client = _Client()
    client.after_acl = [*_acl(), {"subject_type": "public", "permission_level": "read"}]
    target = docent.DocentTarget(client)
    with pytest.raises(docent.DocentPrivacyError):
        target.upload(trace)
    assert target.requests == 1
    assert trace_uuid(trace.trace_id) in client.stored


def test_unchanged_replay_without_local_state_skips_post(trace: TracePayload) -> None:
    client = _Client()
    first = docent.DocentTarget(client).upload(trace)
    second = docent.DocentTarget(client).upload(trace)
    assert first["remote_id"] == second["remote_id"]
    assert second["payload_bytes"] == 0
    assert len(client._session.posts) == 1
    new = docent.DocentTarget(client).upload(_renamed(trace, "new-row"))
    assert new["remote_id"] != first["remote_id"]
    assert len(client._session.posts) == 2


def test_changed_existing_run_fails_without_delete(trace: TracePayload) -> None:
    client = _Client()
    target = docent.DocentTarget(client)
    target.upload(trace)
    changed = replace(trace, messages=[{**trace.messages[0], "content": "changed"}])
    with pytest.raises(docent.DocentContentConflict, match="no supported upsert"):
        target.upload(changed)
    assert len(client._session.posts) == 1
    assert client.events[-1] == "acl"


def test_batch_posts_missing_runs_once_with_exact_byte_allocation(trace: TracePayload) -> None:
    client = _Client()
    target = docent.DocentTarget(client)
    target.upload(trace)
    second, third = _renamed(trace, "row-2"), _renamed(trace, "row-3")
    results = target.upload_batch([trace, second, third])
    post = client._session.posts[-1]
    assert len(client._session.posts) == 2
    assert len(json.loads(post["data"])["agent_runs"]) == 2
    assert results[0]["payload_bytes"] == 0
    assert sum(result["payload_bytes"] for result in results) == len(post["data"])
    assert client.schema_calls == [COLLECTION_ID]
    assert len(client.queries) == 4
    assert all(call[2] == {"ensure_latest_results": True} for call in client.queries)
    assert all("LIMIT" not in call[1] for call in client.queries)


def test_batch_requires_one_dataset_and_unique_ids(trace: TracePayload) -> None:
    client = _Client()
    target = docent.DocentTarget(client)
    assert target.upload_batch([]) == []
    with pytest.raises(ValueError, match="one dataset"):
        target.upload_batch([trace, replace(trace, dataset_id="goodfire-reward-hack-data")])
    with pytest.raises(ValueError, match="duplicate"):
        target.upload_batch([trace, trace])
    assert client._session.posts == []


def test_exact_collection_name_creation_and_duplicates(trace: TracePayload) -> None:
    client = _Client(collections=[{"id": "unrelated", "name": "old " + docent.collection_name(DATASET)}])
    docent.DocentTarget(client).upload(trace)
    assert client.created == [docent.collection_name(DATASET)]
    duplicate = _Client(collections=[_details(), _details(identity="duplicate")])
    with pytest.raises(docent.DocentPrivacyError, match="Duplicate exact"):
        docent.DocentTarget(duplicate).upload(trace)
    assert duplicate._session.posts == []


def test_reservation_and_byte_limits_precede_actual_post(trace: TracePayload) -> None:
    expected = json.dumps({"agent_runs": [docent.agent_run(trace).model_dump(mode="json")]},
                          separators=(",", ":")).encode()
    client = _Client()
    target = docent.DocentTarget(client, max_bytes=len(expected) - 1)
    reservations = []
    target.reserve_bytes = reservations.append
    with pytest.raises(RuntimeError, match="allowance exhausted"):
        target.upload(trace)
    assert reservations == []
    assert target.sent_bytes == target.requests == 0
    assert client._session.posts == []
    target.max_bytes = len(expected)
    target.upload(trace)
    assert reservations == [len(expected)]
    assert target.sent_bytes == len(expected)


def test_failed_reservation_sends_nothing(trace: TracePayload) -> None:
    client = _Client()
    target = docent.DocentTarget(client)

    def reserve(size: int) -> None:
        assert size > 0
        assert target.sent_bytes == target.requests == 0
        raise RuntimeError("Monthly quota exhausted")

    target.reserve_bytes = reserve
    with pytest.raises(RuntimeError, match="Monthly quota"):
        target.upload(trace)
    assert target.sent_bytes == target.requests == 0
    assert client._session.posts == []


def test_missing_readback_or_truncated_lookup_never_succeeds(trace: TracePayload) -> None:
    client = _Client()
    client.drop_runs = True
    with pytest.raises(RuntimeError, match="persist"):
        docent.DocentTarget(client).upload(trace)
    truncated = _Client()
    truncated.truncated = True
    with pytest.raises(RuntimeError, match="truncated"):
        docent.DocentTarget(truncated).upload(trace)
    assert truncated._session.posts == []


def test_async_ingestion_waits_before_readback(trace: TracePayload) -> None:
    client = _Client()
    client.http_status = 202
    client.post_result = {"job_id": "job-1"}
    docent.DocentTarget(client).upload(trace)
    assert client.waited == [(COLLECTION_ID, ["job-1"])]


def test_sdk_pin_checked_only_on_actual_sdk_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docent, "version", lambda name: "0.1.87")
    client = _Client()
    assert docent.DocentTarget(client).client is client
    with pytest.raises(RuntimeError, match="docent==0.1.88"):
        docent.new_client()


def test_remote_flat_run_is_reused_under_collection_scoped_dql(
    trace: TracePayload, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    run = docent.agent_run(trace).model_dump(mode="json")
    client.stored[run["id"]] = run
    execute = client.execute_dql

    def scoped_query(collection_id: str, query: str, **kwargs: Any) -> dict[str, Any]:
        response = execute(collection_id, query, **kwargs)
        # Observed hosted DQL behavior: a scoped optional groups relation
        # excludes flat runs, even when the SQL uses LEFT JOIN.
        if re.search(r"\bJOIN\s+transcript_groups\b", query, re.IGNORECASE):
            response["rows"] = [row for row in response["rows"] if row["group_id"] is not None]
        return response

    monkeypatch.setattr(client, "execute_dql", scoped_query)
    result = docent.DocentTarget(client).upload(trace)
    assert result["remote_id"] == run["id"]
    assert result["payload_bytes"] == 0
    assert client._session.posts == []


def test_encoded_secret_is_trace_specific_and_precedes_any_transport(
    trace: TracePayload, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    monkeypatch.setattr(
        docent, "secret_hits",
        lambda run: ["secret-pattern"] if run["metadata"]["run_id"] == trace.trace_id else [],
    )
    clean = _renamed(trace, "clean-run")
    with pytest.raises(SecretScanRejected) as exc:
        docent.DocentTarget(client).upload_batch([clean, trace])
    assert exc.value.trace_id == trace.trace_id
    assert client.events == []
    assert client._session.posts == []
