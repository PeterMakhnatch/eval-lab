"""Local, DEV-admitted run exports with a separate sealed scoring join.

Only identity columns are read before the shared admission gate. Trace bodies
are selected through that gate in SQL; labels, issues, receipts and CAS objects
are never hydrated. All formats share the same blinded ATIF projection.
Actual verifier rewards stay in the sealed join, not reviewer-visible files.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.cheatbench_fidelity import (
    blind_atif,
    blind_atif_step_origins,
    find_forbidden_json,
)
from evallab.production.actor_cas import (
    PublicationUncertainError,
    directory_status,
    discard_owned_directory,
    publish_staged_directory,
)
from evallab.production.actor_projection import source_read_uri
from evallab.production.cloud_trace_db import (
    _PROTECTED_TASKS_FROZEN,
    TRUST_TIERS,
    admitted_cloud_task_ids,
)
from evallab.production.corpus_queue import DATASETS, CorpusRegistry

_IDENTITY_SQL = (
    "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest,"
    " CASE WHEN json_valid(metadata_json)"
    " THEN json_extract(metadata_json, '$.item_id') END AS item_id,"
    " CASE WHEN json_valid(metadata_json)"
    " THEN json_type(metadata_json, '$.item_id') ELSE 'invalid' END AS item_type"
    " FROM corpus_tasks ORDER BY task_id"
)
_TASK_SCOPE = "task_id IN (SELECT value FROM json_each(?))"
_FORMATS = frozenset({"harbor", "atif", "scout"})
_SOURCE_NAMES = frozenset({
    *DATASETS, "goodfire", "mimo", "tb4", "terminal-bench", "terminal bench",
    "terminal_bench", "terminalbench", "cheatbench", "hacktrace", "hackbench",
})
_EVALUATOR_NAMES = frozenset({
    *TRUST_TIERS, "task_id", "dataset_id", "trace_id", "model_id", "run_id",
    "trust_tier", "is_exploit", "is_task_defect_fail", "flagged_passage_steps",
    "flags_json", "verified_receipt", "task_issues", "raw_cas_uri",
    "observation_cas_uri", "cas_uri",
})
_CAS_REFERENCE = re.compile(r"cas://[^\s\"'<>]+", re.IGNORECASE)
_REDACTED = "[redacted]"
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _redaction_pattern(identities: Iterable[str]) -> re.Pattern[str]:
    # Factor shared prefixes, but keep a greedy optional suffix when one
    # identity prefixes another: replacing "mimo" must consume the full dataset
    # name too. Retrieval's boolean seal matcher intentionally drops that suffix.
    def alternatives(values: list[str]) -> str:
        prefix = os.path.commonprefix((values[0], values[-1]))
        groups: dict[str, list[str]] = {}
        terminal = False
        for value in values:
            suffix = value[len(prefix):]
            if suffix:
                groups.setdefault(suffix[0], []).append(suffix)
            else:
                terminal = True
        branches = "|".join(alternatives(group) for group in groups.values())
        tail = f"(?:{branches})" + ("?" if terminal else "") if branches else ""
        return re.escape(prefix) + tail

    # ASCII-only folding preserves every literal Unicode identity, including
    # characters whose Unicode case fold expands to multiple code points.
    values = sorted({value.translate(_ASCII_FOLD) for value in identities if value})
    return re.compile(alternatives(values) if values else r"(?!)", re.IGNORECASE | re.ASCII)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2,
    ) + "\n").encode("utf-8")


def _write_bytes(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _destination(path: str | Path, label: str) -> Path:
    destination = Path(path).expanduser()
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"{label} already exists; refusing overwrite")
    return destination.resolve()


def _requested_ids(trace_ids: Iterable[str] | None) -> frozenset[str] | None:
    if trace_ids is None:
        return None
    if isinstance(trace_ids, (str, bytes)):
        raise ValueError("trace_ids must be a collection of nonempty strings")
    requested = set()
    for trace_id in trace_ids:
        if not isinstance(trace_id, str) or not trace_id.strip() or "\x00" in trace_id:
            raise ValueError("trace_ids must contain nonempty strings without NUL")
        requested.add(trace_id)
    return frozenset(requested)


def _blind_id(trace_id: str) -> str:
    digest = hashlib.sha256(b"evallab.cloud_trace_export/v1\x00" + trace_id.encode("utf-8"))
    return "run-" + digest.hexdigest()[:32]


def _atif(steps: Sequence[Mapping[str, Any]], blind_id: str) -> dict[str, Any]:
    projected_steps: list[dict[str, Any]] = []
    for position, row in enumerate(steps):
        role = row["role"].strip().casefold()
        content = row["content_text"] or ""
        step: dict[str, Any] = {"step_id": row["step_index"]}
        call_id: str | None = None
        if role in {"user", "system"}:
            step.update(source=role, message=content)
        elif role in {"assistant", "agent"}:
            step.update(source="agent", message=content)
            if row["tool_name"] or row["command_or_action"]:
                call_id = f"call-{position}"
                step["tool_calls"] = [{
                    "tool_call_id": call_id,
                    "function_name": row["tool_name"] or "tool",
                    "arguments": {"command": row["command_or_action"] or ""},
                }]
        else:
            # Preserve each recorded output and its source index without
            # inventing an action from tool-side metadata or borrowing a call
            # from another step: native ATIF references are local to a step.
            step.update(source="agent", message="")
            step["observation"] = {"results": [{
                "source_call_id": None, "content": content,
            }]}
        preview = row["observation_preview"]
        if preview and preview != content:
            observation = step.setdefault("observation", {"results": []})
            observation["results"].append({
                "source_call_id": call_id, "content": preview,
            })
        projected_steps.append(step)
    return {
        "schema_version": "ATIF-v1.6", "session_id": blind_id,
        "agent": {"name": "agent", "version": "unknown"}, "steps": projected_steps,
    }


def _redact_steps(document: dict[str, Any], identities: re.Pattern[str]) -> None:
    def text(value: str) -> str:
        return identities.sub(_REDACTED, _CAS_REFERENCE.sub(_REDACTED, value))

    for step in document["steps"]:
        step["message"] = text(step["message"])
        for call in step.get("tool_calls", []):
            call["function_name"] = text(call["function_name"])
            call["arguments"]["command"] = text(call["arguments"]["command"])
        for result in step.get("observation", {}).get("results", []):
            result["content"] = text(result["content"])


def _scout_api() -> Any:
    try:
        return importlib.import_module("inspect_scout").Transcript
    except ModuleNotFoundError as error:
        if error.name != "inspect_scout":
            raise
        return None


def _scout_transcript(document: Mapping[str, Any], blind_id: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for step in document["steps"]:
        source = step["source"]
        message = {"role": "assistant" if source == "agent" else source,
                   "content": step["message"]}
        calls = step.get("tool_calls", [])
        if calls:
            message["tool_calls"] = [{
                "id": call["tool_call_id"], "function": call["function_name"],
                "arguments": call["arguments"], "type": "function",
            } for call in calls]
        if message["content"] or calls or source != "agent":
            messages.append(message)
        for result in step.get("observation", {}).get("results", []):
            message = {"role": "tool", "content": result["content"]}
            if result["source_call_id"] is not None:
                message["tool_call_id"] = result["source_call_id"]
            messages.append(message)
    return {"transcript_id": blind_id, "metadata": {"item_id": blind_id}, "messages": messages}


def _write_run(
    staging: Path, blind_id: str, document: dict[str, Any],
    *, format: str, scout_api: Any,
) -> None:
    if format == "harbor":
        trial = staging / blind_id
        (trial / "agent").mkdir(parents=True, mode=0o700)
        _write_bytes(trial / "agent/trajectory.json", _json_bytes(document))
        _write_bytes(trial / "result.json", _json_bytes({
            "trial_name": blind_id, "verifier_result": {"rewards": {"reward": None}},
        }))
    elif format == "atif":
        _write_bytes(staging / f"{blind_id}.json", _json_bytes(document))
    else:
        transcript = _scout_transcript(document, blind_id)
        data = json.dumps(
            transcript, ensure_ascii=False, sort_keys=True, allow_nan=False,
        ).encode("utf-8") + b"\n"
        if scout_api is not None:
            scout_api.model_validate_json(data)
        with (staging / "transcripts.jsonl").open("ab") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


def export_runs(
    database_path: str | Path,
    output_directory: str | Path,
    *,
    format: str,
    registry: CorpusRegistry | None = None,
    trace_ids: Iterable[str] | None = None,
    limit: int | None = None,
    manifest: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically publish a blinded local run directory and a private join file.

    ``manifest`` defaults to the sibling ``<output-name>.manifest.json`` for
    Python callers; the CLI requires an explicit path. Neither destination may
    already exist. ``admitted`` counts candidate traces before ``limit``;
    ``refused`` counts candidates denied admission (plus missing requested IDs).
    No limit-excluded run is counted as refused.
    """
    if format not in _FORMATS:
        raise ValueError("format must be harbor, atif or scout")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a nonnegative integer or null")
    requested = _requested_ids(trace_ids)
    output = _destination(output_directory, "output directory")
    join_path = _destination(
        manifest if manifest is not None else output.with_name(output.name + ".manifest.json"),
        "manifest",
    )
    if join_path == output or join_path.is_relative_to(output):
        raise ValueError("manifest must be outside the export directory")
    source = Path(database_path).expanduser().resolve(strict=True)
    authority = registry if registry is not None else CorpusRegistry()
    scout_api = _scout_api() if format == "scout" else None
    output.parent.mkdir(parents=True, exist_ok=True)
    join_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".cloud-trace-export-", dir=output.parent))
    staging_status = directory_status(staging)
    temporary_manifest: Path | None = None
    manifest_status: os.stat_result | None = None
    published = False
    joins: dict[str, Any] = {}
    try:
        with closing(sqlite3.connect(source_read_uri(source), uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            identities = [dict(row) for row in connection.execute(_IDENTITY_SQL)]
            admitted = admitted_cloud_task_ids(identities, registry=authority)
            task_parameters = (json.dumps(sorted(admitted)),)
            trace_scope = _TASK_SCOPE
            parameters = task_parameters
            if requested is not None:
                trace_scope += " AND trace_id IN (SELECT value FROM json_each(?))"
                parameters += (json.dumps(sorted(requested)),)
                candidate_count = len(requested)
            else:
                candidate_count = connection.execute("SELECT count(*) FROM agent_traces").fetchone()[0]
            admitted_count = connection.execute(
                f"SELECT count(*) FROM agent_traces WHERE {trace_scope}", parameters,
            ).fetchone()[0]
            query = (
                "SELECT trace_id, task_id, run_id, model_id, reward FROM agent_traces WHERE "
                + trace_scope + " ORDER BY trace_id"
            )
            if limit is not None:
                query += " LIMIT ?"
                parameters += (limit,)
            traces = list(connection.execute(query, parameters))
            redactions = set(_SOURCE_NAMES | _EVALUATOR_NAMES)
            redactions.update(_PROTECTED_TASKS_FROZEN)
            redactions.update(authority.sealed_identifiers())
            for row in identities:
                for key in ("task_id", "dataset_id"):
                    redactions.add(row[key])
                redactions.add(authority.task_name(row["task_id"]))
                if row["task_id"] in admitted and isinstance(row["item_id"], str) and row["item_id"]:
                    redactions.add(row["item_id"])
            for trace in traces:
                redactions.update(trace[key] for key in ("trace_id", "model_id", "run_id"))
            pattern = _redaction_pattern(redactions)
            if format == "scout":
                _write_bytes(staging / "transcripts.jsonl", b"")
            for trace in traces:
                # Admission remains part of the body SELECT itself, not a filter
                # applied after hydrating a whole table or an untrusted pointer.
                steps = [dict(row) for row in connection.execute(
                    "SELECT s.step_index, s.role, s.tool_name, s.command_or_action,"
                    " s.content_text, s.observation_preview FROM trace_steps AS s"
                    " JOIN agent_traces AS t ON t.trace_id = s.trace_id"
                    " WHERE t.task_id IN (SELECT value FROM json_each(?)) AND s.trace_id = ?"
                    " ORDER BY s.step_index", (*task_parameters, trace["trace_id"]),
                )]
                blind_id = _blind_id(trace["trace_id"])
                if blind_id in joins:
                    raise ValueError("blind identifier collision")
                document = _atif(steps, blind_id)
                origins = blind_atif_step_origins(document)
                projected = blind_atif(document, item_id=blind_id)
                # Detect surviving annotations before benchmark-name redaction
                # can hide their marker while leaving evaluator labels behind.
                if find_forbidden_json(projected):
                    raise ValueError("blinded trajectory contains evaluator-only fields or text")
                if not projected["steps"]:
                    raise ValueError("trace has no actor-visible steps")
                _redact_steps(projected, pattern)
                if find_forbidden_json(projected):
                    raise ValueError("blinded trajectory contains evaluator-only fields or text")
                # blind_atif's metadata and zero-based IDs are an internal audit
                # representation, not native Harbor's external ATIF contract.
                projected.pop("metadata")
                for index, step in enumerate(projected["steps"], start=1):
                    step["step_id"] = index
                _write_run(staging, blind_id, projected, format=format, scout_api=scout_api)
                joins[blind_id] = {
                    "trace_id": trace["trace_id"], "step_origin": origins, "reward": trace["reward"],
                }
        manifest_bytes = _json_bytes(joins)
        descriptor, filename = tempfile.mkstemp(prefix=".cloud-trace-join-", dir=join_path.parent)
        temporary_manifest = Path(filename)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(manifest_bytes)
            stream.flush()
            os.fsync(stream.fileno())
            manifest_status = os.fstat(stream.fileno())
        # A hard link publishes the complete private file without replacing even
        # a racing symlink. Publish it before the run tree so every visible export
        # has a scoring join, and roll back only our own link on failure.
        os.link(temporary_manifest, join_path)
        receipt: dict[str, Any] = {
            "admitted": admitted_count, "exported": len(joins),
            "refused": candidate_count - admitted_count, "admitted_task_count": len(admitted),
            "format": format, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "output_path": str(output), "recorded_at_utc": datetime.now(UTC).isoformat(),
        }
        if format == "scout":
            receipt["scout"] = {
                "inspect_scout_installed": scout_api is not None, "format": "jsonl",
                "loader": "Transcript.model_validate_json",
                "compatibility": (
                    "SDK-validated" if scout_api is not None else "documented schema; SDK unavailable"
                ),
            }
        try:
            publish_staged_directory(staging, output, expected=staging_status)
        except PublicationUncertainError:
            published = True
            raise
        published = True
        return receipt
    finally:
        if temporary_manifest is not None:
            temporary_manifest.unlink(missing_ok=True)
        if not published:
            discard_owned_directory(staging, staging_status)
            if manifest_status is not None:
                try:
                    current = join_path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (current.st_dev, current.st_ino) == (
                        manifest_status.st_dev, manifest_status.st_ino,
                    ):
                        join_path.unlink()
