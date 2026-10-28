"""Read-only verification that the 108 granted Laminar copies are gone.

The owner-action packet grants removal of exactly 108 TT6 ledger/digest/project
and full-authored-payload-verified control-journal copies from one private
Laminar project. This module never deletes anything: it classifies those copies
against an authoritative, complete, project-bound span inventory as ``absent``,
``present``, ``drifted`` or ``unknown``. A cleanup is verified only when every
target is absent under an inventory whose project probe matches the packet,
whose positive controls show the project still answers with other spans, and
whose per-trace detail rows agree with independent per-trace counts. Failed,
unauthorized, wrong-project, empty-control, contaminated or partially paginated
output is ``unknown``, never success.

The inventory comes from the existing read-only Laminar SQL interface (explicit
``--live``) or from the raw query receipt that interface produces
(``--capture-out``), so the same receipt can be re-verified offline. Only
``GET /v1/project`` and read-only ``POST /v1/sql/query`` calls are made. The
module never reads credentials, calls Signals, model, billing, privacy or
Settings endpoints, enables a timer, or infers account privacy/funding. The 12
held safe catchups are a separate decision and are not evaluated here.

Owner workflow for later cleanup without broad deletion::

    uv run python -m evallab.trace_store.owner_cleanup --live \\
        --capture-out ~/.local/state/evallab-prod-live/trace-store/inventory.json
    uv run python -m evallab.trace_store.owner_cleanup --receipt .../inventory.json

Exit status is 0 only for a verified cleanup (all 108 absent, complete
inventory), 1 for not verified (present/drifted/unknown), and 2 for unusable
evidence or a failed live query. A recorded receipt is evidence of one
project-bound query run, not authentication; preserve its file SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from uuid import UUID

from evallab import laminar
from evallab.trace_store.core import trace_uuid
from evallab.trace_store.state import now, record_spend, state_root

SCHEMA_INVENTORY = "evallab.laminar-owner-inventory/v1"
SCHEMA_VERDICT = "evallab.owner-cleanup-verdict/v1"

OWNED_COPY_COUNT = 108
BATCH_SIZE = 16
ROOT_SPAN_NAME = "evallab.trace-store"
#: Fields the Laminar consumer derives from its own span tree; never authored.
CONSUMER_ATTRIBUTES = frozenset({"lmnr.span.ids_path", "lmnr.span.path"})
DETAIL_COLUMNS = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "name",
    "span_type",
    "input",
    "output",
    "attributes",
)
SCOPES = frozenset({"base104", "extra4"})
DEFAULT_PACKET = state_root() / "laminar-p1-owner-action-20261028.json"

ABSENT = "absent"
PRESENT = "present"
DRIFTED = "drifted"
UNKNOWN = "unknown"
VERDICTS = (ABSENT, PRESENT, DRIFTED, UNKNOWN)

AGGREGATE_SPANS_SQL = "SELECT COUNT(*) AS span_count FROM spans"
AGGREGATE_TRACES_SQL = "SELECT COUNT(*) AS trace_count FROM traces"

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_CAS_URI = re.compile(r"cas://sha256/[0-9a-f]{64}\Z")
_TARGET_ID = re.compile(r"\S+\Z")

LIMITS = (
    "A recorded receipt is evidence of one project-bound query run, not an "
    "authenticated statement; preserve its file SHA-256 and provenance.",
    "Laminar returns an empty string for an omitted optional span output. Only "
    "the encoder rule (non-root DEFAULT spans omit output) is normalized; an "
    "authored empty output on such a span is indistinguishable.",
    "Only trace identity and the authored span projection are compared. "
    "Consumer-derived path fields, import timestamps and storage metadata are "
    "excluded.",
    "Account privacy/funding, Signals state and timers are never read or "
    "inferred, and no delete, Signal, config or model request is made.",
    "The 12 held safe catchups are a separate owner decision and are not "
    "evaluated or gated by this cleanup verdict.",
)


class OwnerCleanupError(ValueError):
    """The packet, proof or receipt cannot bind these 108 copies."""


@dataclass(frozen=True)
class OwnerPacket:
    path: Path
    sha256: str
    project_id: str
    workspace_id: str
    status: str
    method: str
    proof_path: Path
    proof_sha256: str
    trace_ids: tuple[str, ...]


@dataclass(frozen=True)
class OwnerTarget:
    trace_id: str
    remote_id: str
    dataset: str
    scope: str
    span_count_expected: int
    absent_output_spans_expected: int
    expected_authored_sha256: str
    content_digest: str
    raw_cas_uri: str
    original_upload_at: str


@dataclass(frozen=True)
class BatchInventory:
    remote_ids: tuple[str, ...]
    rows: tuple[Mapping[str, Any], ...] | None
    counts: Mapping[str, int] | None
    problem: str | None


@dataclass(frozen=True)
class Inventory:
    kind: str
    captured_at: str
    project_id: str | None
    span_total: int | None
    trace_total: int | None
    batches: tuple[BatchInventory, ...]
    problems: tuple[str, ...]


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(encoded.encode())


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise OwnerCleanupError(f"Unreadable {label}: {path}") from exc
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise OwnerCleanupError(f"Invalid JSON {label}: {path}") from exc
    if not isinstance(value, dict):
        raise OwnerCleanupError(f"{label} must be a JSON object: {path}")
    return value, raw


def _canonical_uuid(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise OwnerCleanupError(f"{label} must be a nonempty UUID string")
    try:
        parsed = UUID(value)
    except ValueError:
        raise OwnerCleanupError(f"{label} is not a UUID") from None
    if str(parsed) != value:
        raise OwnerCleanupError(f"{label} must be a canonical lowercase UUID")
    return value


def _digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise OwnerCleanupError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OwnerCleanupError(f"{label} must be a positive integer")
    return value


def _count(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a count")
    if isinstance(value, int):
        count = value
    elif isinstance(value, str) and value.isdigit():
        count = int(value)
    else:
        raise ValueError("not an integer count")
    if count < 0:
        raise ValueError("negative count")
    return count


def _chunks(values: Sequence[str], size: int) -> list[tuple[str, ...]]:
    return [tuple(values[start : start + size]) for start in range(0, len(values), size)]


def load_owner_packet(path: Path | str = DEFAULT_PACKET) -> OwnerPacket:
    """Load the exact owner-action packet and its receipt-bound scope."""
    path = Path(path)
    data, raw = _read_json(path, "owner packet")
    project_id = _canonical_uuid(data.get("project_id"), label="packet project_id")
    workspace_id = _canonical_uuid(data.get("workspace_id"), label="packet workspace_id")
    proof_sha256 = _digest(data.get("proof_sha256"), label="packet proof_sha256")
    proof_path = Path(str(data.get("proof_path")))
    status = data.get("status")
    method = data.get("method")
    trace_ids = data.get("trace_ids")
    if not proof_path.is_absolute():
        raise OwnerCleanupError("Packet proof_path must be absolute")
    if not isinstance(status, str) or not status:
        raise OwnerCleanupError("Packet status must be a nonempty string")
    if method != "DELETE":
        raise OwnerCleanupError("Owner packet is not a DELETE remediation request")
    if not isinstance(trace_ids, list) or len(trace_ids) != OWNED_COPY_COUNT:
        raise OwnerCleanupError(f"Owner packet must name exactly {OWNED_COPY_COUNT} traces")
    ids = tuple(_canonical_uuid(value, label="packet trace_ids entry") for value in trace_ids)
    if len(set(ids)) != len(ids):
        raise OwnerCleanupError("Owner packet trace_ids must be unique")
    return OwnerPacket(
        path=path,
        sha256=_sha256(raw),
        project_id=project_id,
        workspace_id=workspace_id,
        status=status,
        method=method,
        proof_path=proof_path,
        proof_sha256=proof_sha256,
        trace_ids=ids,
    )


def load_owner_targets(
    packet: OwnerPacket,
    *,
    proof_path: Path | None = None,
) -> tuple[OwnerTarget, ...]:
    """Load exactly the 108 receipt-bound original IDs, spans and ownership."""
    path = Path(proof_path) if proof_path is not None else packet.proof_path
    data, raw = _read_json(path, "ownership proof")
    if _sha256(raw) != packet.proof_sha256:
        raise OwnerCleanupError("Ownership proof bytes do not match the packet receipt")
    if data.get("project_id") != packet.project_id:
        raise OwnerCleanupError("Ownership proof belongs to another project")
    if data.get("eligible_remote_copies") != OWNED_COPY_COUNT:
        raise OwnerCleanupError(f"Ownership proof does not bind {OWNED_COPY_COUNT} copies")
    if data.get("full_authored_payload_matches") != OWNED_COPY_COUNT:
        raise OwnerCleanupError("Ownership proof did not verify every authored payload")
    entries = data.get("entries")
    if not isinstance(entries, list) or len(entries) != OWNED_COPY_COUNT:
        raise OwnerCleanupError(f"Ownership proof must contain exactly {OWNED_COPY_COUNT} entries")
    targets: list[OwnerTarget] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise OwnerCleanupError(f"Ownership proof entry {index} is not an object")
        trace_id = entry.get("trace_id")
        if not isinstance(trace_id, str) or not _TARGET_ID.fullmatch(trace_id):
            raise OwnerCleanupError(f"Ownership proof entry {index} has no original trace ID")
        remote_id = _canonical_uuid(entry.get("remote_id"), label=f"entry {index} remote_id")
        if remote_id != trace_uuid(trace_id):
            raise OwnerCleanupError(
                f"Ownership proof entry {index} remote ID is not derived from its original ID"
            )
        dataset = entry.get("dataset")
        if not isinstance(dataset, str) or not dataset:
            raise OwnerCleanupError(f"Ownership proof entry {index} has no dataset")
        scope = entry.get("scope")
        if scope not in SCOPES:
            raise OwnerCleanupError(f"Ownership proof entry {index} has an unknown scope")
        span_count = _positive_int(
            entry.get("span_count_expected"), label=f"entry {index} span_count_expected"
        )
        if entry.get("span_count_remote") != span_count:
            raise OwnerCleanupError(
                f"Ownership proof entry {index} remote span count is not its expectation"
            )
        expected = _digest(
            entry.get("expected_authored_sha256"), label=f"entry {index} expected digest"
        )
        if entry.get("remote_authored_sha256") != expected:
            raise OwnerCleanupError(
                f"Ownership proof entry {index} did not verify the authored payload"
            )
        if entry.get("remote_authored_payload_matches_original_ledger_projection") is not True:
            raise OwnerCleanupError(f"Ownership proof entry {index} is not a verified owned copy")
        normalized = entry.get("absent_output_attribute_server_empty_default")
        if (
            isinstance(normalized, bool)
            or not isinstance(normalized, int)
            or not 0 <= normalized <= span_count
        ):
            raise OwnerCleanupError(
                f"Ownership proof entry {index} has an invalid absent-output count"
            )
        content_digest = _digest(entry.get("content_digest"), label=f"entry {index} content_digest")
        raw_cas_uri = entry.get("raw_cas_uri")
        if not isinstance(raw_cas_uri, str) or not _CAS_URI.fullmatch(raw_cas_uri):
            raise OwnerCleanupError(f"Ownership proof entry {index} has no CAS source")
        uploaded = entry.get("original_upload_at")
        try:
            if not isinstance(uploaded, str):
                raise ValueError
            datetime.fromisoformat(uploaded)
        except ValueError:
            raise OwnerCleanupError(f"Ownership proof entry {index} has no upload time") from None
        targets.append(
            OwnerTarget(
                trace_id=trace_id,
                remote_id=remote_id,
                dataset=dataset,
                scope=scope,
                span_count_expected=span_count,
                absent_output_spans_expected=normalized,
                expected_authored_sha256=expected,
                content_digest=content_digest,
                raw_cas_uri=raw_cas_uri,
                original_upload_at=uploaded,
            )
        )
    remote_ids = [target.remote_id for target in targets]
    trace_ids = [target.trace_id for target in targets]
    if len(set(remote_ids)) != OWNED_COPY_COUNT or len(set(trace_ids)) != OWNED_COPY_COUNT:
        raise OwnerCleanupError("Ownership proof repeats an original or remote ID")
    if set(remote_ids) != set(packet.trace_ids):
        raise OwnerCleanupError("Ownership proof remote IDs differ from the packet grant")
    per_source = data.get("per_source")
    if per_source is not None:
        if not isinstance(per_source, Mapping):
            raise OwnerCleanupError("Ownership proof per_source must be an object")
        try:
            expected_sources = {str(key): int(value) for key, value in per_source.items()}
        except (TypeError, ValueError):
            raise OwnerCleanupError("Ownership proof per_source counts are not integers") from None
        observed_sources: dict[str, int] = defaultdict(int)
        for target in targets:
            observed_sources[target.dataset] += 1
        if dict(observed_sources) != expected_sources:
            raise OwnerCleanupError("Ownership proof source counts differ from its entries")
    base_count = data.get("base104_candidates")
    if base_count is not None:
        if isinstance(base_count, bool) or not isinstance(base_count, int):
            raise OwnerCleanupError("Ownership proof base104_candidates is not an integer")
        if sum(target.scope == "base104" for target in targets) != base_count:
            raise OwnerCleanupError("Ownership proof base copies differ from its entries")
    extra_count = data.get("extra4_candidates")
    if extra_count is not None:
        if isinstance(extra_count, bool) or not isinstance(extra_count, int):
            raise OwnerCleanupError("Ownership proof extra4_candidates is not an integer")
        if sum(target.scope == "extra4" for target in targets) != extra_count:
            raise OwnerCleanupError("Ownership proof extra copies differ from its entries")
    by_remote = {target.remote_id: target for target in targets}
    return tuple(by_remote[remote_id] for remote_id in packet.trace_ids)


def _detail_sql(remote_ids: Sequence[str]) -> str:
    literals = ",".join(f"toUUID('{UUID(identity)}')" for identity in remote_ids)
    return (
        "SELECT "
        + ",".join(DETAIL_COLUMNS)
        + " FROM spans WHERE trace_id IN ("
        + literals
        + ") ORDER BY trace_id, span_id"
    )


def _count_sql(remote_ids: Sequence[str]) -> str:
    literals = ",".join(f"toUUID('{UUID(identity)}')" for identity in remote_ids)
    return (
        "SELECT trace_id, COUNT(*) AS span_count FROM spans WHERE trace_id IN ("
        + literals
        + ") GROUP BY trace_id ORDER BY trace_id"
    )


def _sql_response(
    query: str,
    *,
    call: dict[str, Any],
) -> dict[str, Any]:
    response = laminar._json_call(
        "POST",
        "/v1/sql/query",
        body={"query": query, "parameters": {}},
        **call,
    )
    if not isinstance(response, Mapping):
        raise OwnerCleanupError("Laminar SQL returned a non-object response")
    return dict(response)


def request_inventory(
    packet: OwnerPacket,
    targets: Sequence[OwnerTarget],
    *,
    api_key: str | None = None,
    endpoint: str | None = None,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Capture raw read-only project and span inventory; no target mutation."""
    key = api_key if api_key is not None else os.environ.get(laminar.LAMINAR_KEY_ENV, "")
    if not key:
        raise OwnerCleanupError("LMNR_PROJECT_API_KEY is required (use keys run --)")
    call = {
        "api_key": key,
        "endpoint": endpoint
        or os.environ.get(laminar.LAMINAR_ENDPOINT_ENV)
        or laminar.LAMINAR_ENDPOINT,
        "opener": opener,
    }
    project = laminar._json_call("GET", "/v1/project", **call)
    if not isinstance(project, Mapping):
        raise OwnerCleanupError("Laminar project probe returned a non-object response")
    aggregate = {
        "spans": {
            "sql": AGGREGATE_SPANS_SQL,
            "response": _sql_response(AGGREGATE_SPANS_SQL, call=call),
        },
        "traces": {
            "sql": AGGREGATE_TRACES_SQL,
            "response": _sql_response(AGGREGATE_TRACES_SQL, call=call),
        },
    }
    batches = []
    for chunk in _chunks(tuple(target.remote_id for target in targets), BATCH_SIZE):
        detail = _detail_sql(chunk)
        counts = _count_sql(chunk)
        batches.append(
            {
                "remote_ids": list(chunk),
                "detail": {"sql": detail, "response": _sql_response(detail, call=call)},
                "counts": {"sql": counts, "response": _sql_response(counts, call=call)},
            }
        )
    return {
        "schema": SCHEMA_INVENTORY,
        "captured_at": now(),
        "packet_sha256": packet.sha256,
        "proof_sha256": packet.proof_sha256,
        "project": {"path": "/v1/project", "response": dict(project)},
        "aggregate": aggregate,
        "batches": batches,
        "http_requests": 3 + 2 * len(batches),
    }


def _rows(response: Any) -> tuple[tuple[Mapping[str, Any], ...] | None, str | None]:
    if not isinstance(response, Mapping):
        return None, "missing_response"
    if "error" in response:
        return None, "provider_error"
    data = response.get("data")
    if not isinstance(data, list):
        return None, "missing_data"
    if any(not isinstance(row, Mapping) for row in data):
        return None, "malformed_rows"
    return tuple(data), None


def _project_binding(
    packet: OwnerPacket, receipt: Mapping[str, Any]
) -> tuple[str | None, list[str]]:
    problems: list[str] = []
    entry = receipt.get("project")
    if not isinstance(entry, Mapping):
        return None, ["project_probe_missing"]
    if entry.get("path") != "/v1/project":
        problems.append("project_probe_path_mismatch")
    response = entry.get("response")
    if not isinstance(response, Mapping):
        return None, problems + ["project_probe_unavailable"]
    project_id = response.get("projectId")
    if not isinstance(project_id, str) or not project_id:
        return None, problems + ["project_probe_unbound"]
    if project_id != packet.project_id:
        problems.append("wrong_project")
    workspace_id = response.get("workspaceId")
    if workspace_id is not None and workspace_id != packet.workspace_id:
        problems.append("wrong_workspace")
    return project_id, problems


def inventory_from_receipt(
    packet: OwnerPacket,
    targets: Sequence[OwnerTarget],
    receipt: Mapping[str, Any],
    *,
    kind: str = "recorded",
) -> Inventory:
    """Validate a captured inventory without trusting any derived verdict."""
    if not isinstance(receipt, Mapping):
        raise OwnerCleanupError("Inventory receipt must be a JSON object")
    if receipt.get("schema") != SCHEMA_INVENTORY:
        raise OwnerCleanupError("Inventory receipt has an unknown schema")
    if receipt.get("packet_sha256") != packet.sha256:
        raise OwnerCleanupError("Inventory receipt belongs to another owner packet")
    if receipt.get("proof_sha256") != packet.proof_sha256:
        raise OwnerCleanupError("Inventory receipt belongs to another ownership proof")
    captured_at = receipt.get("captured_at")
    try:
        if not isinstance(captured_at, str):
            raise ValueError
        datetime.fromisoformat(captured_at)
    except ValueError:
        raise OwnerCleanupError("Inventory receipt has no valid capture time") from None
    problems: list[str] = []
    project_id, project_problems = _project_binding(packet, receipt)
    problems.extend(project_problems)

    aggregate = receipt.get("aggregate")
    span_total: int | None = None
    trace_total: int | None = None
    if not isinstance(aggregate, Mapping):
        problems.append("aggregate_missing")
    else:
        for key, sql in (("spans", AGGREGATE_SPANS_SQL), ("traces", AGGREGATE_TRACES_SQL)):
            entry = aggregate.get(key)
            if not isinstance(entry, Mapping) or entry.get("sql") != sql:
                problems.append(f"aggregate_query_mismatch:{key}")
                continue
            rows, problem = _rows(entry.get("response"))
            if rows is None or len(rows) != 1:
                problems.append(f"aggregate_unavailable:{key}")
                continue
            field = "span_count" if key == "spans" else "trace_count"
            try:
                value = _count(rows[0].get(field))
            except ValueError:
                problems.append(f"aggregate_unavailable:{key}")
                continue
            if key == "spans":
                span_total = value
            else:
                trace_total = value
    if span_total == 0 or trace_total == 0:
        problems.append("empty_control")
    if span_total is None or trace_total is None:
        problems.append("aggregate_unavailable")

    expected_chunks = _chunks(tuple(target.remote_id for target in targets), BATCH_SIZE)
    raw_batches = receipt.get("batches")
    batches: list[BatchInventory] = []
    if not isinstance(raw_batches, list) or len(raw_batches) != len(expected_chunks):
        problems.append("coverage_mismatch")
    for index, chunk in enumerate(expected_chunks):
        if not isinstance(raw_batches, list) or index >= len(raw_batches):
            batches.append(BatchInventory(chunk, None, None, "missing_batch"))
            continue
        entry = raw_batches[index]
        if not isinstance(entry, Mapping):
            batches.append(BatchInventory(chunk, None, None, "malformed_batch"))
            continue
        remote_ids = entry.get("remote_ids")
        if not isinstance(remote_ids, list) or tuple(remote_ids) != chunk:
            batches.append(BatchInventory(chunk, None, None, "batch_scope_mismatch"))
            continue
        detail = entry.get("detail")
        counts = entry.get("counts")
        if not isinstance(detail, Mapping) or detail.get("sql") != _detail_sql(chunk):
            batches.append(BatchInventory(chunk, None, None, "detail_query_mismatch"))
            continue
        if not isinstance(counts, Mapping) or counts.get("sql") != _count_sql(chunk):
            batches.append(BatchInventory(chunk, None, None, "count_query_mismatch"))
            continue
        detail_rows, detail_problem = _rows(detail.get("response"))
        count_rows, count_problem = _rows(counts.get("response"))
        if detail_rows is None:
            batches.append(
                BatchInventory(chunk, None, None, detail_problem or "detail_unavailable")
            )
            continue
        if count_rows is None:
            batches.append(BatchInventory(chunk, None, None, count_problem or "counts_unavailable"))
            continue
        problem = None
        batch_ids = set(chunk)
        for row in detail_rows:
            try:
                identity = _canonical_uuid(row.get("trace_id"), label="span trace_id")
                _norm_id(row.get("span_id"))
                _norm_id(row.get("parent_span_id"))
            except (OwnerCleanupError, ValueError):
                problem = "malformed_detail"
                break
            if identity not in batch_ids or any(column not in row for column in DETAIL_COLUMNS):
                problem = "detail_scope_mismatch"
                break
        if problem is not None:
            batches.append(BatchInventory(chunk, None, None, problem))
            continue
        parsed_counts: dict[str, int] = {}
        for row in count_rows:
            try:
                identity = _canonical_uuid(row.get("trace_id"), label="count trace_id")
                value = _count(row.get("span_count"))
            except (OwnerCleanupError, ValueError):
                problem = "malformed_count"
                break
            if identity not in batch_ids or identity in parsed_counts:
                problem = "count_scope_mismatch"
                break
            parsed_counts[identity] = value
        if problem is not None:
            batches.append(BatchInventory(chunk, None, None, problem))
            continue
        batches.append(BatchInventory(chunk, detail_rows, parsed_counts, None))
    return Inventory(
        kind=kind,
        captured_at=captured_at,
        project_id=project_id,
        span_total=span_total,
        trace_total=trace_total,
        batches=tuple(batches),
        problems=tuple(dict.fromkeys(problems)),
    )


def _norm_id(value: Any) -> str:
    if not value:
        return "0" * 32
    text = str(value).replace("-", "")
    if len(text) not in (16, 32):
        raise ValueError("span ID is neither 16 nor 32 hexadecimal characters")
    return UUID(hex=text.zfill(32)).hex


def _norm_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _project_row(row: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    attributes_value = row.get("attributes")
    if isinstance(attributes_value, str):
        attributes = json.loads(attributes_value) if attributes_value.strip() else {}
    elif isinstance(attributes_value, Mapping):
        attributes = dict(attributes_value)
    else:
        raise ValueError("span attributes are neither JSON nor an object")
    if not isinstance(attributes, dict):
        raise ValueError("span attributes are not an object")
    attributes = {key: value for key, value in attributes.items() if key not in CONSUMER_ATTRIBUTES}
    span_type = row.get("span_type") or attributes.get("lmnr.span.type")
    input_value = _norm_json(row.get("input"))
    output_value = _norm_json(row.get("output"))
    normalized = False
    if output_value == "" and span_type == "DEFAULT" and row.get("name") != ROOT_SPAN_NAME:
        # The encoder omits lmnr.span.output for non-root DEFAULT spans; the
        # read path renders that absent optional field as an empty string.
        output_value = None
        normalized = True
    return {
        "trace_id": _norm_id(row.get("trace_id")),
        "span_id": _norm_id(row.get("span_id")),
        "parent_id": _norm_id(row.get("parent_span_id")),
        "name": row.get("name"),
        "span_type": span_type,
        "input": input_value,
        "output": output_value,
        "attributes": attributes,
    }, normalized


def authored_digest(spans: Sequence[Mapping[str, Any]]) -> str:
    """Canonical digest of the authored span projection recorded by the proof."""
    ordered = sorted(spans, key=lambda span: span["span_id"])
    return _json_digest(ordered)


def _target_verdict(
    target: OwnerTarget,
    rows: Sequence[Mapping[str, Any]],
    counted: int,
) -> dict[str, Any]:
    observed = {
        "span_count_observed": len(rows),
        "observed_authored_sha256": None,
        "reason": None,
    }
    if len(rows) != counted:
        # Independent per-trace count disagrees with detail rows: the read was
        # partial, so absence (or presence) is unknowable, never success.
        observed["reason"] = "inventory_partial"
        return {**observed, "verdict": UNKNOWN}
    if not rows:
        observed["reason"] = "absent_verified"
        return {**observed, "verdict": ABSENT}
    span_ids = [row.get("span_id") for row in rows]
    if len(set(span_ids)) != len(span_ids):
        observed["reason"] = "duplicate_span_rows"
        return {**observed, "verdict": DRIFTED}
    if len(rows) != target.span_count_expected:
        observed["reason"] = "span_count_mismatch"
        return {**observed, "verdict": DRIFTED}
    projected: list[dict[str, Any]] = []
    normalized = 0
    try:
        for row in rows:
            span, was_normalized = _project_row(row)
            projected.append(span)
            normalized += int(was_normalized)
    except (TypeError, ValueError):
        observed["reason"] = "malformed_row"
        return {**observed, "verdict": UNKNOWN}
    digest = authored_digest(projected)
    observed["observed_authored_sha256"] = digest
    if digest != target.expected_authored_sha256:
        observed["reason"] = "authored_digest_mismatch"
        return {**observed, "verdict": DRIFTED}
    if normalized != target.absent_output_spans_expected:
        observed["reason"] = "normalization_count_mismatch"
        return {**observed, "verdict": DRIFTED}
    observed["reason"] = "present_exact"
    return {**observed, "verdict": PRESENT}


def judge_inventory(
    packet: OwnerPacket,
    targets: Sequence[OwnerTarget],
    inventory: Inventory,
) -> dict[str, Any]:
    """Classify exactly the granted copies; never turns incomplete reads into success."""
    if len(targets) != OWNED_COPY_COUNT:
        raise OwnerCleanupError(f"Refusing to judge {len(targets)} targets")
    fatal = list(inventory.problems)
    reported = list(inventory.problems)
    results: dict[str, dict[str, Any]] = {}
    batches_by_remote: dict[str, BatchInventory] = {}
    for batch in inventory.batches:
        for remote_id in batch.remote_ids:
            batches_by_remote[remote_id] = batch
    rows_by_trace: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    total_rows = 0
    incomplete = bool(fatal)
    for index, batch in enumerate(inventory.batches):
        if batch.problem is not None or batch.rows is None or batch.counts is None:
            incomplete = True
            reported.append(f"batch_{index}:{batch.problem or 'inventory_unavailable'}")
            continue
        for row in batch.rows:
            rows_by_trace[row["trace_id"]].append(row)
            total_rows += 1
        for remote_id in batch.remote_ids:
            rows = rows_by_trace.get(remote_id, [])
            counted = batch.counts.get(remote_id, 0)
            if len(rows) != counted:
                incomplete = True
                reported.append("partial_inventory")
    for target in targets:
        batch = batches_by_remote.get(target.remote_id)
        if fatal:
            results[target.remote_id] = {
                "verdict": UNKNOWN,
                "span_count_observed": None,
                "observed_authored_sha256": None,
                "reason": fatal[0],
            }
        elif batch is None:
            incomplete = True
            results[target.remote_id] = {
                "verdict": UNKNOWN,
                "span_count_observed": None,
                "observed_authored_sha256": None,
                "reason": "missing_batch",
            }
        elif batch.problem is not None or batch.rows is None or batch.counts is None:
            results[target.remote_id] = {
                "verdict": UNKNOWN,
                "span_count_observed": None,
                "observed_authored_sha256": None,
                "reason": batch.problem or "inventory_unavailable",
            }
        else:
            rows = rows_by_trace.get(target.remote_id, [])
            results[target.remote_id] = _target_verdict(
                target,
                rows,
                batch.counts.get(target.remote_id, 0),
            )
            if results[target.remote_id]["verdict"] == UNKNOWN:
                incomplete = True
    if inventory.span_total is not None and total_rows > inventory.span_total:
        fatal.append("aggregate_inconsistent")
        incomplete = True
    present_or_drifted = sum(
        1 for result in results.values() if result["verdict"] in (PRESENT, DRIFTED)
    )
    if inventory.trace_total is not None and present_or_drifted > inventory.trace_total:
        fatal.append("aggregate_inconsistent")
        incomplete = True
    if fatal:
        first = fatal[0]
        for result in results.values():
            if result["verdict"] != UNKNOWN:
                result.update(verdict=UNKNOWN, reason=first)
        incomplete = True
    counts = {verdict: 0 for verdict in VERDICTS}
    entries = []
    for target in targets:
        result = results[target.remote_id]
        counts[result["verdict"]] += 1
        entries.append(
            {
                "trace_id": target.trace_id,
                "remote_id": target.remote_id,
                "ownership": {
                    "project_id": packet.project_id,
                    "workspace_id": packet.workspace_id,
                    "scope": target.scope,
                    "dataset": target.dataset,
                },
                "span_count_expected": target.span_count_expected,
                "absent_output_spans_expected": target.absent_output_spans_expected,
                "expected_authored_sha256": target.expected_authored_sha256,
                **result,
            }
        )
    cleanup_verified = (
        not fatal
        and not incomplete
        and counts[ABSENT] == OWNED_COPY_COUNT
        and counts[PRESENT] == counts[DRIFTED] == counts[UNKNOWN] == 0
    )
    return {
        "schema": SCHEMA_VERDICT,
        "generated_at": now(),
        "packet": {
            "path": str(packet.path),
            "sha256": packet.sha256,
            "status": packet.status,
            "project_id": packet.project_id,
            "workspace_id": packet.workspace_id,
        },
        "proof": {"path": str(packet.proof_path), "sha256": packet.proof_sha256},
        "inventory": {
            "kind": inventory.kind,
            "captured_at": inventory.captured_at,
            "project_id": inventory.project_id,
            "span_total": inventory.span_total,
            "trace_total": inventory.trace_total,
            "batches": len(inventory.batches),
            "target_span_rows": total_rows,
            "complete": not incomplete,
            "problems": list(dict.fromkeys(reported)),
        },
        "counts": {**counts, "targets": len(entries)},
        "cleanup_verified": cleanup_verified,
        "owner_action": {
            "required": not cleanup_verified,
            "packet_status": packet.status,
            "deletion": "not attempted by this verifier",
            "next": (
                "After the owner performs the packet's normal-session deletion, capture a "
                "fresh inventory and rerun this verifier offline; only 108 absent verdicts "
                "under a complete project-bound inventory verify cleanup."
            ),
        },
        "safe_catchup": {
            "evaluated": False,
            "decision": "separate",
            "note": (
                "The 12 held safe catchups remain a separate owner decision; this verifier "
                "never derives or gates them from the cleanup verdict."
            ),
        },
        "limits": list(LIMITS),
        "targets": entries,
    }


def _write_private_json(path: Path, value: Any) -> None:
    path = Path(path)
    if path.exists():
        raise OwnerCleanupError(f"Refusing to overwrite existing evidence: {path}")
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def verify_receipt(
    packet_path: Path | str = DEFAULT_PACKET,
    receipt_path: Path | str | None = None,
    *,
    receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify a captured inventory entirely offline; no network or state writes."""
    if (receipt_path is None) == (receipt is None):
        raise OwnerCleanupError("Specify exactly one of receipt_path or receipt")
    packet = load_owner_packet(packet_path)
    targets = load_owner_targets(packet)
    file_sha256 = None
    if receipt is None:
        receipt, raw = _read_json(Path(str(receipt_path)), "inventory receipt")
        file_sha256 = _sha256(raw)
    inventory = inventory_from_receipt(packet, targets, receipt)
    verdict = judge_inventory(packet, targets, inventory)
    verdict["inventory"].update(
        receipt_path=None if receipt_path is None else str(receipt_path),
        receipt_sha256=_json_digest(receipt),
        receipt_file_sha256=file_sha256,
    )
    return verdict


def verify_live(
    packet_path: Path | str = DEFAULT_PACKET,
    *,
    capture_out: Path | str | None = None,
    api_key: str | None = None,
    endpoint: str | None = None,
    opener: Callable[..., Any] = urlopen,
    spend_recorder: Callable[..., None] | None = record_spend,
) -> dict[str, Any]:
    """Capture a fresh read-only inventory and verify it; never deletes."""
    packet = load_owner_packet(packet_path)
    targets = load_owner_targets(packet)
    receipt = request_inventory(
        packet,
        targets,
        api_key=api_key,
        endpoint=endpoint,
        opener=opener,
    )
    if capture_out is not None:
        _write_private_json(Path(capture_out), receipt)
    inventory = inventory_from_receipt(packet, targets, receipt, kind="live")
    verdict = judge_inventory(packet, targets, inventory)
    verdict["inventory"].update(
        receipt_path=None if capture_out is None else str(capture_out),
        receipt_sha256=_json_digest(receipt),
        receipt_file_sha256=None,
    )
    if spend_recorder is not None:
        spend_recorder(
            "laminar-owner-cleanup",
            operation="verify-cleanup",
            cost_usd=0,
            basis=(
                "Read-only project probe and SQL span inventory under the existing "
                "allowance; no model, Signal, configuration, billing or deletion call"
            ),
            http_requests=receipt["http_requests"],
            target_span_rows=verdict["inventory"]["target_span_rows"],
            outcome="ok" if verdict["cleanup_verified"] else "not-verified",
        )
    return verdict


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.trace_store.owner_cleanup",
        description=(
            "Read-only owner-cleanup verification for the 108 granted Laminar copies. "
            "Never deletes, calls Signals/models/settings, or enables timers."
        ),
    )
    parser.add_argument(
        "--packet",
        type=Path,
        default=DEFAULT_PACKET,
        help="Owner-action packet (default: the existing TT6 packet)",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--receipt", type=Path, help="Recorded raw inventory receipt (offline)")
    source.add_argument("--live", action="store_true", help="Capture a fresh read-only inventory")
    parser.add_argument("--capture-out", type=Path, help="Write the live raw receipt here")
    parser.add_argument("--json-out", type=Path, help="Write the verdict JSON here")
    args = parser.parse_args(argv)
    if args.capture_out is not None and not args.live:
        parser.error("--capture-out requires --live")
    try:
        if args.live:
            verdict = verify_live(args.packet, capture_out=args.capture_out)
        else:
            verdict = verify_receipt(args.packet, args.receipt)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: owner cleanup verification refused: {exc}", file=sys.stderr)
        return 2
    if args.json_out is not None:
        try:
            _write_private_json(args.json_out, verdict)
        except OSError as exc:
            print(f"error: cannot write verdict: {exc}", file=sys.stderr)
            return 2
    print(json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if verdict["cleanup_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
