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
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.cheatbench_fidelity import (
    ANNOTATION_MARKER,
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


class _StagedExport:
    """A staging directory beside ``output`` and an exclusive private join file.

    ``link_manifest`` publishes the complete join file with a hard link (it never
    replaces even a racing symlink) before the run tree, so every visible export
    has its scoring join; ``close`` discards the staging tree and only our own
    link when the export was not published.
    """

    def __init__(self, output: Path, join_path: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        join_path.parent.mkdir(parents=True, exist_ok=True)
        self.output, self.join_path = output, join_path
        self.directory = Path(tempfile.mkdtemp(prefix=".cloud-trace-export-", dir=output.parent))
        self._status = directory_status(self.directory)
        self._temporary: Path | None = None
        self._manifest_status: os.stat_result | None = None
        self._published = False

    def link_manifest(self, data: bytes) -> None:
        descriptor, filename = tempfile.mkstemp(prefix=".cloud-trace-join-", dir=self.join_path.parent)
        self._temporary = Path(filename)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            self._manifest_status = os.fstat(stream.fileno())
        os.link(self._temporary, self.join_path)

    def publish(self) -> None:
        try:
            publish_staged_directory(self.directory, self.output, expected=self._status)
        except PublicationUncertainError:
            self._published = True
            raise
        self._published = True

    def close(self) -> None:
        if self._temporary is not None:
            self._temporary.unlink(missing_ok=True)
        if not self._published:
            discard_owned_directory(self.directory, self._status)
            if self._manifest_status is not None:
                try:
                    current = self.join_path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (current.st_dev, current.st_ino) == (
                        self._manifest_status.st_dev, self._manifest_status.st_ino,
                    ):
                        self.join_path.unlink()


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
    stage = _StagedExport(output, join_path)
    staging = stage.directory
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
        stage.link_manifest(manifest_bytes)
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
        stage.publish()
        return receipt
    finally:
        stage.close()


_CLUSTER_SCOPE_SCHEMA = "evallab.cluster_scope/v1"
_CLUSTER_SCOPE_SQL = (
    "SELECT m.value AS member, a.trace_id, a.task_id, t.split AS stored_split,"
    " CASE WHEN json_valid(t.metadata_json)"
    " THEN json_extract(t.metadata_json, '$.native_identity.task_name') END AS native_task,"
    " f.facet_version, f.facet_model"
    " FROM json_each(?) AS m"
    " LEFT JOIN agent_traces AS a ON a.trace_id = m.value"
    " LEFT JOIN corpus_tasks AS t ON t.task_id = a.task_id"
    " LEFT JOIN run_facets AS f ON f.run_id = a.trace_id AND f.task_id = a.task_id"
    " ORDER BY m.value"
)


def _cluster_members(raw: Any) -> list[str]:
    members = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(members, list) or not all(isinstance(item, str) and item for item in members):
        raise ValueError("stored cluster membership is not a list of run ids")
    return list(dict.fromkeys(members))


def export_cluster_scope(
    database_path: str | Path,
    cluster_id: str,
    manifest: str | Path,
    *,
    registry: CorpusRegistry | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    """Publish a metadata-only scope file for one stored Run Atlas cluster.

    One row per member run whose task the shared admission gate
    (``admitted_cloud_task_ids`` with the registry's ``assignments.csv`` authority,
    which includes the strict cross-key TEST check) admits as DEV. Members that
    are not admitted get no row, name and id: only counts by coarse reason are
    published, so the file cannot say which hidden runs exist. No trace body,
    label, reward, facet text, cluster name or run source is read or written.
    The manifest must not exist; it is published whole, never replaced.
    """
    if not isinstance(cluster_id, str) or not cluster_id.strip() or "\x00" in cluster_id:
        raise ValueError("cluster_id must be a nonempty string without NUL")
    started = time.perf_counter()
    join_path = _destination(manifest, "manifest")
    source = Path(database_path).expanduser().resolve(strict=True)
    authority = registry if registry is not None else CorpusRegistry()
    if not authority.has_assignment_authority():
        raise ValueError("assignments.csv authority is unavailable; refusing to certify any run as DEV")
    digest = authority.split_assignments_digest
    assert digest is not None  # has_assignment_authority() validated it
    assignments_sha256 = digest.removeprefix("sha256:")
    with closing(sqlite3.connect(source_read_uri(source), uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        cluster = connection.execute(
            "SELECT cluster_id, level, parent, size, run_ids, provenance_json"
            " FROM run_clusters WHERE cluster_id = ?", (cluster_id,),
        ).fetchone()
        if cluster is None:
            raise ValueError("no stored cluster has that id")
        members = _cluster_members(cluster["run_ids"])
        provenance = json.loads(cluster["provenance_json"])
        identities = [dict(row) for row in connection.execute(_IDENTITY_SQL)]
        admitted = admitted_cloud_task_ids(identities, registry=authority)
        rows = []
        refused = {"db_non_dev_split": 0, "registry_cross_key_or_sealed": 0, "unstored_run": 0}
        for row in connection.execute(_CLUSTER_SCOPE_SQL, (json.dumps(members),)):
            if row["trace_id"] is None:
                refused["unstored_run"] += 1
            elif row["task_id"] not in admitted:
                label = row["stored_split"]
                refused[
                    "registry_cross_key_or_sealed"
                    if isinstance(label, str) and label.strip().casefold() == "dev" else "db_non_dev_split"
                ] += 1
            else:
                rows.append({
                    "trace_id": row["trace_id"],
                    "task_id": row["task_id"],
                    "native_task_id": row["native_task"],
                    "canonical_task_id": authority.task_name(row["task_id"]),
                    "assignments_sha256": assignments_sha256,
                    "admission": "dev-admitted",
                    "facet_build_id": provenance.get("build_id"),
                    "facet_producer_revision": row["facet_version"],
                    "facet_served_model": row["facet_model"],
                })
    document = {
        "schema": _CLUSTER_SCOPE_SCHEMA,
        "cluster": {
            "cluster_id": cluster["cluster_id"], "level": cluster["level"], "parent": cluster["parent"],
            "stored_size": cluster["size"], "members": len(members),
            "build_id": provenance.get("build_id"), "cluster_version": provenance.get("cluster_version"),
            "facet_count": provenance.get("facet_count"), "created_at": provenance.get("created_at"),
            "embedder": provenance.get("embedder"), "embedder_digest": provenance.get("embedder_digest"),
        },
        "assignments_sha256": assignments_sha256,
        "admission_gate": "admitted_cloud_task_ids + CorpusRegistry (assignments.csv cross-key TEST check)",
        "counts": {
            "members": len(members), "admitted": len(rows),
            "refused": sum(refused.values()), "refused_by_reason": refused,
        },
        "export_revision": revision,
        "rows": rows,
    }
    manifest_bytes = _json_bytes(document)
    join_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".cluster-scope-", dir=join_path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(manifest_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, join_path)  # never replaces a racing file or symlink
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "schema": _CLUSTER_SCOPE_SCHEMA, "cluster_id": cluster["cluster_id"], "level": cluster["level"],
        **document["counts"], "assignments_sha256": assignments_sha256,
        "facet_build_id": provenance.get("build_id"), "manifest_path": str(join_path),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


CHEATBENCH_DATASET = "cheatbench-actions-v1"
# view -> (registry reference field, registry digest field). ``full`` is the trajectory the Run
# Atlas facets were produced from; ``actions_only`` is the registry's separate actions view.
_CHEATBENCH_VIEWS = {
    "full": ("trial_ref", "trajectory_sha256"),
    "actions_only": ("actions_only_ref", "actions_only_sha256"),
}
_ATIF_SOURCES = frozenset({"user", "agent", "system"})
_ANNOTATION_MARKER_FOLDED = ANNOTATION_MARKER.casefold()
_CHEATBENCH_DROPPED = {"extra", "timestamp", "model_name", "reasoning_effort", "metrics"}
_ATIF_VERSION = re.compile(r"ATIF-v\d+\.\d+")
# Registry answer-key fields that must never appear as tool-argument names (plus ``expected_*``).
_ANSWER_KEY_NAMES = frozenset({"label_source", "source_labels", "cheating_mechanism", "cheating_type"})
# Held only in the sealed join, for scoring; never in a reviewer-visible file.
_CHEATBENCH_KEY_FIELDS = ("expected_verdict", "expected_category", "expected_fault", "expected_root_cause")


def cheatbench_item_admitted(registry: CorpusRegistry, item: Any) -> bool:
    """Whether one CheatBench action item may be shown as DEV.

    CheatBench items share task names across splits, so the task-keyed gate used for cloud
    traces cannot decide them; admission is per item: TEST wins in the effective split, the
    item must be affirmatively DEV in ``assignments.csv``, no assignment key may mark the
    item or its task TEST, and its identity must not be sealed. Same rule as the Run Atlas
    facet producer, plus the affirmative-DEV and seal checks.
    """
    return (
        registry.has_assignment_authority()
        and item.split == "dev"
        and registry.assigned_split(CHEATBENCH_DATASET, item_id=item.item_id, task_id=item.task_id) == "dev"
        and not registry.assigned_test_anywhere(item_id=item.item_id, task_id=item.task_id)
        and registry.sealed_identity(item_id=item.item_id) is None
    )


def _cheatbench_trajectory(item: Any, view: str, root: Path) -> tuple[dict[str, Any], dict[str, Any], str]:
    """The registry-pinned ATIF document of an admitted item, its reference and observed digest."""
    reference_field, digest_field = _CHEATBENCH_VIEWS[view]
    reference = item.metadata.get(reference_field)
    expected = item.metadata.get(digest_field)
    relative = reference.get("path") if isinstance(reference, dict) else None
    if (
        not isinstance(reference, dict) or reference.get("kind") != "published"
        or not isinstance(relative, str) or not relative
        or Path(relative).is_absolute() or ".." in Path(relative).parts
    ):
        raise ValueError("registry supplies no usable published trajectory reference for an admitted item")
    trajectory = (root / relative / "agent/trajectory.json").resolve()
    if not trajectory.is_relative_to(root.resolve()):
        raise ValueError("registry trajectory reference resolves outside the results home")
    pinned = expected.removeprefix("sha256:") if isinstance(expected, str) else ""
    raw = trajectory.read_bytes()
    observed = hashlib.sha256(raw).hexdigest()
    if observed != pinned:
        raise ValueError("trajectory bytes differ from the registry's pinned digest")
    document = json.loads(raw)
    steps = document.get("steps") if isinstance(document, dict) else None
    if not isinstance(steps, list) or not steps or not all(isinstance(step, dict) for step in steps):
        raise ValueError("pinned trajectory has no ATIF step objects")
    return document, reference, observed


def _rekey_calls(step: dict[str, Any], index: int) -> None:
    """Give every call a blind per-step id and point each observation at its call, or at none.

    Recorded call ids are not copied (they can carry identities), and native ATIF requires each
    observation's call id to name a call in the same step; recorded placeholders such as
    ``"unknown"`` become null, like the cloud exporter's unpaired tool output.
    """
    mapping: dict[Any, str] = {}
    for position, call in enumerate(step.get("tool_calls") or [], start=1):
        blind = f"call-{index}-{position}"
        mapping.setdefault(call.get("tool_call_id"), blind)
        call["tool_call_id"] = blind
    for result in (step.get("observation") or {}).get("results") or []:
        result["source_call_id"] = mapping.get(result.get("source_call_id"))


def _refuse_answer_key_names(value: Any) -> None:
    """Tool arguments are actor content, but they must not carry the registry's answer-key fields."""
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).casefold()
            if name.startswith("expected_") or name in _ANSWER_KEY_NAMES:
                raise ValueError("blinded trajectory contains evaluator-only fields or text")
            _refuse_answer_key_names(item)
    elif isinstance(value, list):
        for item in value:
            _refuse_answer_key_names(item)


def _redact_strings(value: Any, patterns: tuple[re.Pattern[str], ...]) -> Any:
    """Redact identities in every string, including mapping keys; colliding keys refuse the export."""
    if isinstance(value, str):
        value = _CAS_REFERENCE.sub(_REDACTED, value)
        for pattern in patterns:
            value = pattern.sub(_REDACTED, value)
        return value
    if isinstance(value, list):
        return [_redact_strings(item, patterns) for item in value]
    if isinstance(value, dict):
        redacted = {_redact_strings(str(key), patterns): _redact_strings(item, patterns) for key, item in value.items()}
        if len(redacted) != len(value):
            raise ValueError("identity redaction merged distinct argument names")
        return redacted
    return value


def _is_annotation_step(step: Any) -> bool:
    message = step.get("message") if isinstance(step, dict) else None
    return isinstance(message, str) and message.lstrip().casefold().startswith(_ANNOTATION_MARKER_FOLDED)


def _only(node: Any, allowed: set[str], dropped: set[str], what: str) -> dict[str, Any]:
    if not isinstance(node, dict) or set(node) - allowed - dropped:
        raise ValueError(f"unsupported ATIF {what}")
    return {key: node[key] for key in allowed if key in node}


def _project_cheatbench_step(step: dict[str, Any], redact: Callable[[Any], Any]) -> dict[str, Any]:
    """Explicit native-ATIF projection: content is redacted, structural ids and enums are not.

    Unknown fields refuse the export instead of being dropped or passed through (a dropped
    ``reasoning_content`` would silently thin what a reviewer sees); ``extra`` blocks and
    run-environment fields (timestamps, model names, metrics) are discarded.
    """
    kept = _only(step, {"step_id", "source", "message", "tool_calls", "observation"}, _CHEATBENCH_DROPPED, "step field")
    if kept.get("source") not in _ATIF_SOURCES:
        raise ValueError("unsupported ATIF step source")
    projected: dict[str, Any] = {"step_id": kept.get("step_id"), "source": kept["source"], "message": redact(kept.get("message", ""))}
    calls = []
    for call in kept.get("tool_calls") or []:
        call = _only(call, {"tool_call_id", "function_name", "arguments"}, {"extra"}, "tool call field")
        _refuse_answer_key_names(call.get("arguments"))
        calls.append({
            "tool_call_id": call.get("tool_call_id"), "function_name": redact(call.get("function_name")),
            "arguments": redact(call.get("arguments")),
        })
    if calls:
        projected["tool_calls"] = calls
    if kept.get("observation") is not None:
        results = []
        for result in _only(kept["observation"], {"results"}, set(), "observation field").get("results") or []:
            result = _only(result, {"source_call_id", "content"}, {"extra"}, "observation result field")
            results.append({"source_call_id": result.get("source_call_id"), "content": redact(result.get("content"))})
        projected["observation"] = {"results": results}
    return projected




def export_cheatbench_runs(
    output_directory: str | Path,
    *,
    format: str,
    registry: CorpusRegistry | None = None,
    run_ids: Iterable[str] | None = None,
    limit: int | None = None,
    manifest: str | Path | None = None,
    view: str = "full",
    results_home: str | Path | None = None,
) -> dict[str, Any]:
    """Blinded export of DEV-admitted CheatBench runs that have no cloud-database trace.

    ``run_ids`` are the facet run ids (registry item ids); omitted means every registered
    CheatBench item is a candidate. Each admitted item's registry-pinned ``trial_ref``
    trajectory goes through ``blind_atif``, a field allowlist, identity redaction and the
    same writers as :func:`export_runs`. The private join (blind id to run id, ``trial_ref``,
    observed digest, step origins and the registry's expected verdict fields) is published
    outside the export; the export itself holds no reward, label, registry key or source
    name. ``admitted``/``refused`` count candidates before ``limit``; unknown ids refuse.
    """
    if format not in _FORMATS:
        raise ValueError("format must be harbor, atif or scout")
    if view not in _CHEATBENCH_VIEWS:
        raise ValueError("view must be full or actions_only")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a nonnegative integer or null")
    requested = _requested_ids(run_ids)
    # No admission timing is reported: it would time registry lookups of refused ids.
    output = _destination(output_directory, "output directory")
    join_path = _destination(
        manifest if manifest is not None else output.with_name(output.name + ".manifest.json"), "manifest",
    )
    if join_path == output or join_path.is_relative_to(output):
        raise ValueError("manifest must be outside the export directory")
    from evallab.results_home import results_root

    root = Path(results_home).expanduser() if results_home is not None else results_root()
    authority = registry if registry is not None else CorpusRegistry()
    scout_api = _scout_api() if format == "scout" else None
    candidate_ids = sorted(
        requested if requested is not None else (item.item_id for item in authority.items(dataset_id=CHEATBENCH_DATASET))
    )
    canonical_candidates: set[str] = set()
    admitted_by_id: dict[str, Any] = {}
    for run_id in candidate_ids:
        try:
            item = authority.get(CHEATBENCH_DATASET, run_id)
        except KeyError:
            canonical_candidates.add(run_id.strip().casefold())
            continue
        canonical_candidates.add(item.item_id)  # aliases of one registry id collapse here
        if cheatbench_item_admitted(authority, item):
            admitted_by_id[item.item_id] = item
    candidate_count = len(canonical_candidates)
    admitted = [admitted_by_id[item_id] for item_id in sorted(admitted_by_id)]
    selected = admitted if limit is None else admitted[:limit]
    broad = set(_SOURCE_NAMES | _EVALUATOR_NAMES | _PROTECTED_TASKS_FROZEN)
    broad.update(authority.sealed_identifiers())
    own: set[str] = set()
    for item in selected:
        subject = item.metadata.get("subject") or {}
        own.update(
            value for value in (
                item.item_id, item.task_id, authority.task_name(item.task_id),
                item.metadata.get("source_id"), subject.get("trial_id"),
                *(
                    segment for field in ("trial_ref", "actions_only_ref")
                    for segment in ((item.metadata.get(field) or {}).get("path") or "").split("/")
                ),
            ) if isinstance(value, str) and value
        )
    # Broad names and ASCII identities use the exporter's ASCII-only matching, which already covers
    # every letter case. A run's own non-ASCII identities also match every Unicode case spelling, so an
    # uppercase alias of such a registry identity cannot survive (a Unicode-wide pattern over all
    # identities is several times slower on large trajectories).
    unicode_identities = sorted((value for value in own if not value.isascii()), key=len, reverse=True)
    ascii_pattern = _redaction_pattern(broad | own)
    unicode_pattern = re.compile("|".join(map(re.escape, unicode_identities)) or r"(?!)", re.IGNORECASE)

    def redact(value: Any) -> Any:
        return _redact_strings(value, (ascii_pattern, unicode_pattern))

    stage = _StagedExport(output, join_path)
    joins: dict[str, Any] = {}
    try:
        if format == "scout":
            _write_bytes(stage.directory / "transcripts.jsonl", b"")
        for item in selected:
            document, reference, observed = _cheatbench_trajectory(item, view, root)
            blind_id = _blind_id(item.item_id)
            if blind_id in joins:
                raise ValueError("blind identifier collision")
            # Annotation steps are recognised in any letter case, and any surviving marker fails the export
            # before identity redaction could hide it and leave its label text readable.
            kept = [step for step in document["steps"] if not _is_annotation_step(step)]
            if _ANNOTATION_MARKER_FOLDED in json.dumps(kept, ensure_ascii=False).casefold():
                raise ValueError("blinded trajectory contains evaluator-only fields or text")
            allowlisted = {"schema_version": document.get("schema_version"), "steps": kept}
            origins = blind_atif_step_origins(allowlisted)
            projected = blind_atif(allowlisted, item_id=blind_id)
            if find_forbidden_json(projected):
                raise ValueError("blinded trajectory contains evaluator-only fields or text")

            steps = [_project_cheatbench_step(step, redact) for step in projected["steps"]]
            if not steps:
                raise ValueError("trace has no actor-visible steps")
            for index, step in enumerate(steps, start=1):
                step["step_id"] = index
                _rekey_calls(step, index)
            version = document.get("schema_version")
            blinded = {
                "schema_version": version if isinstance(version, str) and _ATIF_VERSION.fullmatch(version) else "ATIF-v1.6",
                "session_id": blind_id, "agent": {"name": "agent", "version": "unknown"}, "steps": steps,
            }
            if find_forbidden_json(blinded):
                raise ValueError("blinded trajectory contains evaluator-only fields or text")
            # Registry normalisation is Unicode case folding, which can expand characters; a folded
            # identity left in the output refuses the export instead of relying on pattern matching.
            folded = json.dumps(blinded, ensure_ascii=False).casefold()
            if any(value.casefold() in folded for value in unicode_identities):
                raise ValueError("blinded trajectory still contains a registry identity")
            _write_run(stage.directory, blind_id, blinded, format=format, scout_api=scout_api)
            joins[blind_id] = {
                "run_id": item.item_id, "trial_ref": reference, "view": view, "trajectory_sha256": observed,
                "step_origin": origins, "reward": None,
                "answer_key": {key: item.metadata.get(key) for key in _CHEATBENCH_KEY_FIELDS},
            }
        manifest_bytes = _json_bytes(joins)
        stage.link_manifest(manifest_bytes)
        receipt: dict[str, Any] = {
            "admitted": len(admitted), "exported": len(joins), "refused": candidate_count - len(admitted),
            "candidates": candidate_count, "format": format, "view": view,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
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
        stage.publish()
        return receipt
    finally:
        stage.close()
