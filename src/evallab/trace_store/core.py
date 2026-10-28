"""Read-only, admitted trace payloads for the private trace stores.

Selection shares the blind reader's exact DEV/cross-key gate. Observation CAS
objects are verified before actor text is projected; evaluator envelopes are
never exported wholesale, even when their previews were stored as messages.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from evallab.readers.clouddb_blind import TraceRef, blind_messages, dev_trace_refs

GOODFIRE_DATASET = "goodfire-reward-hack-data"
ALLOWED_DATASETS = frozenset({
    GOODFIRE_DATASET, "mimo-defect-map-v1", "tb2-to-tb21-v1", "tb4-audit-v1",
})
METADATA_FIELDS = frozenset({
    "run_id", "source", "benchmark", "model", "task_id", "outcome", "split",
})
_OWN_RUNS = {
    "mimo-defect-map-v1": frozenset({"mimo30", "muse-r1", "muse-r2", "muse-w5"}),
    "tb2-to-tb21-v1": frozenset({"tb2-to-tb21"}),
    "tb4-audit-v1": frozenset({"kimi-tb4", "muse-r1"}),
}
_CAS_URI = re.compile(r"cas://sha256/([0-9a-f]{64})\Z")
# These are evaluator/source annotations, not actor-visible observation fields.
# Normalize hyphens only so answer-key and answer_key have the same boundary.
_EVALUATOR_FIELDS = frozenset({
    "label", "labels", "source_label", "is_exploit", "trust_tier",
    "flagged_passage_steps", "receipt", "verified_receipt_json", "answer_key",
    "generation_receipt", "source_receipt",
    "flags_json", "source_passages", "source_judge_flags", "passage_positive",
    "reward", "rewards",
})
_JSON_KEY = re.compile(r'"(?:[^"\\]|\\.)*"\s*:')


@dataclass(frozen=True)
class TracePayload:
    trace_id: str
    dataset_id: str
    metadata: dict[str, Any]
    messages: list[dict[str, Any]]
    hydrated_bytes: int = 0


class HydrationError(ValueError):
    """An observation cannot be exported faithfully and without evaluator data."""

    def __init__(self, category: str, *, trace_id: str, step_index: int) -> None:
        self.category = category
        self.trace_id = trace_id
        self.step_index = step_index
        super().__init__(f"Trace hydration {category}: trace {trace_id}, step {step_index}")


def trace_uuid(trace_id: str) -> str:
    """The same deterministic remote run identity for every target."""
    return str(uuid5(NAMESPACE_URL, "evallab-trace-store/v1:" + trace_id))


def _verify_own_sources(path: Path, runs: set[str]) -> None:
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Invalid ingestion-sources.json evidence") from exc
    sources = evidence.get("artifact_sources") if isinstance(evidence, dict) else None
    if not isinstance(sources, dict):
        raise ValueError("Ingestion evidence must contain an artifact_sources object")
    missing = sorted(run for run in runs if not isinstance(sources.get(run), dict))
    if missing:
        raise ValueError("Own runs absent from ingestion evidence artifact_sources: "
                         + ", ".join(missing))


def select_traces(
    con: sqlite3.Connection,
    *,
    repo_root: Path,
    dataset: str | None = None,
    evidence_path: Path | None = None,
) -> list[TraceRef]:
    """Select attested own runs first, then Goodfire, without modifying the DB.

    ``evidence_path`` injects the retained ingestion source authority. It is read
    only when at least one own run survives all dataset/split/admission filters.
    """
    if dataset is not None and dataset not in ALLOWED_DATASETS:
        raise ValueError(f"Dataset is not allowed for trace storage: {dataset}")
    allowed_runs = sorted(set().union(*_OWN_RUNS.values()))
    placeholders = ",".join("?" for _ in allowed_runs)
    own_runs = dict(con.execute(
        f"SELECT trace_id, run_id FROM agent_traces WHERE run_id IN ({placeholders})",
        allowed_runs,
    ))
    selected = []
    sources = set()
    for ref in dev_trace_refs(con, repo_root=repo_root):
        if ref.dataset_id not in ALLOWED_DATASETS or (
            dataset is not None and ref.dataset_id != dataset
        ):
            continue
        if ref.dataset_id != GOODFIRE_DATASET:
            run = own_runs.get(ref.trace_id)
            if run not in _OWN_RUNS[ref.dataset_id]:
                continue
            sources.add(run)
        selected.append(ref)
    if sources:
        path = evidence_path if evidence_path is not None else (
            Path.home() / ".local/state/evallab-prod-live/cloud-trace-db-evidence/ingestion-sources.json"
        )
        _verify_own_sources(path, sources)
    return sorted(selected, key=lambda ref: (ref.dataset_id == GOODFIRE_DATASET, ref.trace_id))


def _has_evaluator_fields(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold().replace("-", "_") in _EVALUATOR_FIELDS
            or _has_evaluator_fields(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_evaluator_fields(item) for item in value)
    if isinstance(value, str):
        return _unsafe_actor_text(value)
    return False


def _unsafe_actor_text(text: str) -> bool:
    if not text.lstrip().startswith(("{", "[", '"')):
        return False
    try:
        return _has_evaluator_fields(json.loads(text))
    except ValueError:
        # A clipped JSON preview must not hide an evaluator-only key that is
        # already visible. This check does not inspect arbitrary prose/code.
        for match in _JSON_KEY.finditer(text):
            try:
                key = json.loads(match[0].rsplit(":", 1)[0])
            except ValueError:
                continue
            if key.casefold().replace("-", "_") in _EVALUATOR_FIELDS:
                return True
        return False


def _content(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _observations(bundle: Any) -> list[tuple[str, str | None]] | None:
    """Project only known actor observation fields, never an entire envelope.

    ``put_bundle`` stores arbitrary deterministic JSON, not a universal CAS
    schema. ATIF observations use results[].content; native bundles may contain
    tool messages or an explicit message/content. All sibling annotations stay
    outside the projection. Other JSON is eligible only as preview-bound raw
    actor text in ``_hydrate``.
    """
    if not isinstance(bundle, dict):
        return None
    if "observation" in bundle:
        observation = bundle["observation"]
        if isinstance(observation, str):
            return [(observation, None)]
        return _observations(observation)
    if isinstance(bundle.get("results"), list):
        results = bundle["results"]
    elif isinstance(bundle.get("messages"), list):
        results = [message for message in bundle["messages"]
                   if isinstance(message, dict) and message.get("role") == "tool"]
    elif "content" in bundle:
        results = [bundle]
    elif "message" in bundle:
        message = bundle["message"]
        if isinstance(message, dict):
            if message.get("role", "tool") != "tool" or "content" not in message:
                raise ValueError("invalid actor message")
            results = [message]
        else:
            return [(_content(message), None)]
    else:
        return None
    if not results:
        raise ValueError("empty actor observations")
    projected = []
    for result in results:
        if not isinstance(result, dict) or "content" not in result:
            raise ValueError("invalid actor observation")
        content = result["content"]
        call_id = result.get("source_call_id", result.get("tool_call_id"))
        projected.append((_content(content), None if call_id is None else str(call_id)))
    return projected


def _hydrate(
    uri: Any,
    preview: str,
    *,
    cas_root: Path,
    trace_id: str,
    step_index: int,
) -> list[tuple[str, str | None]]:
    def fail(category: str) -> HydrationError:
        return HydrationError(category, trace_id=trace_id, step_index=step_index)

    match = _CAS_URI.fullmatch(uri) if isinstance(uri, str) else None
    if match is None:
        raise fail("invalid_uri")
    try:
        raw = (cas_root / "sha256" / match[1]).read_bytes()
    except FileNotFoundError as exc:
        raise fail("missing_object") from exc
    except OSError as exc:
        raise fail("unreadable_object") from exc
    if hashlib.sha256(raw).hexdigest() != match[1]:
        raise fail("digest_mismatch")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise fail("invalid_utf8") from exc
    if _unsafe_actor_text(preview):
        raise fail("evaluator_fields")
    try:
        bundle = json.loads(text)
    except ValueError as exc:
        if text.lstrip().startswith(("{", "[")):
            raise fail("malformed_json") from exc
        bundle = None
    # Prefer exact raw actor bytes when a nonempty stored prefix binds them.
    # This also preserves JSON tool output whose ordinary keys happen to be
    # named "message" or "content". Evaluator JSON never takes this raw path.
    bound_raw = (preview and text.startswith(preview)) or text == preview
    if bound_raw and not _has_evaluator_fields(bundle):
        observations = [(text, None)]
    else:
        try:
            observations = _observations(bundle)
        except ValueError as exc:
            raise fail("malformed_observation") from exc
        if observations is None:
            category = "evaluator_fields" if _has_evaluator_fields(bundle) else "unsupported_schema"
            raise fail(category)
    if any(_unsafe_actor_text(content) for content, _ in observations):
        raise fail("evaluator_fields")
    return observations


def _hydrate_step(
    group: list[dict[str, Any]],
    role: str,
    preview: str,
    observations: list[tuple[str, str | None]],
    *,
    observation_slot: bool,
    trace_id: str,
) -> tuple[list[dict[str, Any]], int]:
    replaced = [message for message in group
                if message["role"] == ("tool" if observation_slot else role)]
    kept = [message for message in group if message not in replaced]
    template = replaced[0] if replaced else {
        "step_index": group[0]["step_index"], "role": "tool", "content": "",
        "reasoning": None, "tool_calls": [], "tool_call_id": None,
    }
    calls = [call for message in kept for call in message["tool_calls"]]
    if len(calls) > 1 and len(calls) != len(observations) and any(
        call_id is None for _, call_id in observations
    ):
        raise HydrationError(
            "ambiguous_observation", trace_id=trace_id, step_index=group[0]["step_index"],
        )
    hydrated = []
    for position, (content, call_id) in enumerate(observations):
        if call_id is None and len(calls) == len(observations):
            call_id = calls[position]["id"]
        elif call_id is None and len(calls) == 1:
            call_id = calls[0]["id"]
        hydrated.append({
            **template, "content": content,
            "tool_call_id": call_id or template["tool_call_id"],
        })
    extra = sum(len(message["content"].encode("utf-8")) for message in hydrated)
    extra -= len(preview.encode("utf-8"))
    return kept + hydrated, max(0, extra)


def load_trace(
    con: sqlite3.Connection, ref: TraceRef, *, cas_root: Path | None = None,
) -> TracePayload:
    """Load a selected ref with exact metadata and lossless actor observations.

    The caller obtains refs through ``select_traces``; the source identity and
    exact uppercase DEV split are checked again against the read-only rows.
    ``cas_root`` is the directory containing ``sha256/``, not that subdirectory.
    """
    row = con.execute(
        "SELECT a.trace_id, t.dataset_id, a.task_id, a.model_id, a.outcome, t.split, a.run_id "
        "FROM agent_traces a JOIN corpus_tasks t ON t.task_id = a.task_id WHERE a.trace_id = ?",
        (ref.trace_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Trace does not exist: {ref.trace_id}")
    trace_id, dataset_id, task_id, model_id, outcome, split, run_id = row
    if (trace_id, dataset_id, task_id) != (ref.trace_id, ref.dataset_id, ref.task_id):
        raise ValueError("Trace reference does not match database source identity")
    if dataset_id not in ALLOWED_DATASETS or split != "DEV" or (
        dataset_id != GOODFIRE_DATASET and run_id not in _OWN_RUNS[dataset_id]
    ):
        raise ValueError("Trace source is not allowed for trace storage")
    metadata = {
        "run_id": trace_id, "source": dataset_id, "benchmark": task_id.partition("/")[0],
        "model": model_id, "task_id": task_id, "outcome": outcome, "split": split,
    }
    messages = blind_messages(con, trace_id)
    groups: dict[int, list[dict[str, Any]]] = {}
    for message in messages:
        if _unsafe_actor_text(message["content"]):
            raise HydrationError(
                "evaluator_fields", trace_id=trace_id, step_index=message["step_index"],
            )
        groups.setdefault(message["step_index"], []).append(message)
    root = cas_root if cas_root is not None else (
        Path.home() / ".local/state/evallab-prod-live/cas"
    )
    hydrated_bytes = 0
    for index, role, content, preview, uri in con.execute(
        "SELECT step_index, role, content_text, observation_preview, observation_cas_uri "
        "FROM trace_steps WHERE trace_id = ? AND observation_cas_uri IS NOT NULL ORDER BY step_index",
        (trace_id,),
    ):
        observation_slot = role == "agent" or (role != "tool" and preview is not None)
        stored = preview if observation_slot else content if content is not None else preview
        stored = _content(stored)
        observations = _hydrate(uri, stored, cas_root=root, trace_id=trace_id, step_index=index)
        if not observation_slot and role != "tool" and len(observations) != 1:
            raise HydrationError("ambiguous_observation", trace_id=trace_id, step_index=index)
        groups[index], extra = _hydrate_step(
            groups[index], role, stored, observations, observation_slot=observation_slot,
            trace_id=trace_id,
        )
        hydrated_bytes += extra
    return TracePayload(
        trace_id=trace_id, dataset_id=dataset_id, metadata=metadata,
        messages=[message for group in groups.values() for message in group],
        hydrated_bytes=hydrated_bytes,
    )
