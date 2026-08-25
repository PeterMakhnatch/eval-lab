"""Optional Phoenix span annotations for reviewed behavior episodes.

Phoenix is a disposable projection: this module only exchanges structured annotation
metadata with the official ``phoenix-client`` API.  ATIF bytes and episode storage
remain owned by their respective canonical layers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any


def span_ids_by_step(otel_payload: Mapping[str, Any]) -> dict[int, str]:
    """Delegate OTLP step identity extraction to the tracing identity helpers."""
    from .tracing import span_ids_by_step as _span_ids_by_step

    return _span_ids_by_step(dict(otel_payload))


if TYPE_CHECKING:
    from .behavior_episodes import BehaviorDetectionContext, BehaviorEpisode


class PhoenixAnnotationError(ValueError):
    """An adapter operation cannot be completed without risking an ambiguous match."""


@dataclass(frozen=True)
class PhoenixAnnotationSyncResult:
    published: int = 0
    retrieved: int = 0
    persisted: int = 0
    skipped: int = 0
    annotation_ids: tuple[str, ...] = ()


def _official_client(client: Any) -> Any:
    if client is not None:
        return client
    try:
        from phoenix.client import Client
    except ImportError as exc:  # pragma: no cover - depends on optional installation
        raise PhoenixAnnotationError(
            "Phoenix annotations require the official arize-phoenix-client package; "
            "install the observability dependency group with `uv sync --group observability`."
        ) from exc
    return Client()


def _core_types() -> tuple[Any, Any]:
    try:
        from .behavior_episodes import BehaviorEpisode, deterministic_episode_id
    except ImportError as exc:  # pragma: no cover - integration ordering
        raise PhoenixAnnotationError(
            "BehaviorEpisode support is unavailable; merge the core behavior episode package first."
        ) from exc
    return BehaviorEpisode, deterministic_episode_id


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_mapping(obj: Any) -> dict[str, Any]:
    if isinstance(obj, Mapping):
        return dict(obj)
    if hasattr(obj, "model_dump"):
        return dict(obj.model_dump(mode="json"))
    if is_dataclass(obj):
        return asdict(obj)
    fields = getattr(obj, "__dict__", None)
    return dict(fields) if isinstance(fields, dict) else {}


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if value is None:
        return None
    text = str(value)
    return text or None


def _context_value(context: Any, key: str) -> Any:
    return _value(context, key)


def _episode_metadata(
    episode: Any,
    *,
    trace_id: str,
    root_span_id: str,
) -> dict[str, Any]:
    raw = _as_mapping(episode)
    metadata: dict[str, Any] = {
        "schema_version": raw.get("schema_version", 1),
        "episode_id": str(raw.get("episode_id") or ""),
        "trial_id": str(raw.get("trial_id") or ""),
        "document_id": str(raw.get("document_id") or ""),
        "trajectory_id": raw.get("trajectory_id"),
        "session_id": raw.get("session_id"),
        "start_step": raw.get("start_step"),
        "end_step": raw.get("end_step"),
        "label": str(raw.get("label") or ""),
        "status": str(raw.get("status") or ""),
        "score": raw.get("score"),
        "confidence": raw.get("confidence"),
        "evidence_step_ids": list(raw.get("evidence_step_ids") or ()),
        "evidence_span_ids": list(raw.get("evidence_span_ids") or ()),
        "annotator_kind": str(raw.get("annotator_kind") or ""),
        "annotator_id": str(raw.get("annotator_id") or ""),
        "detector_version": raw.get("detector_version"),
        "rubric_version": raw.get("rubric_version"),
        "catalog_version": raw.get("catalog_version", "behavior-catalog/v1"),
        "source_sha256": raw.get("source_sha256"),
        "input_digest": raw.get("input_digest"),
        "provenance": raw.get("provenance") or {},
        "created_at": _iso(raw.get("created_at")),
        "updated_at": _iso(raw.get("updated_at")),
        "trace_id": trace_id,
        "root_span_id": root_span_id,
    }
    return {key: value for key, value in metadata.items() if value is not None}


def _span_for_episode(
    episode: Any,
    span_ids_by_step: Mapping[int, str],
    root_span_id: str,
) -> str:
    raw = _as_mapping(episode)
    mapped = dict(span_ids_by_step)
    exact = {str(value) for value in raw.get("evidence_span_ids") or () if value}
    for step in raw.get("evidence_step_ids") or ():
        candidate = mapped.get(int(step))
        if candidate and (not exact or str(candidate) in exact):
            return str(candidate)
    # An explicit span list that cannot be reconciled to the step mapping is
    # ambiguous; never attach to a merely temporal or unrelated child span.
    if exact:
        return str(root_span_id)
    for step in raw.get("evidence_step_ids") or ():
        candidate = mapped.get(int(step))
        if candidate:
            return str(candidate)
    return str(root_span_id)


def publish_behavior_episodes(
    client: Any,
    episodes: Iterable[BehaviorEpisode],
    *,
    project_name: str,
    trace_id: str,
    root_span_id: str,
    span_ids_by_step: Mapping[int, str],
) -> PhoenixAnnotationSyncResult:
    """Publish candidate episodes using the official Phoenix span resource API."""
    phoenix = _official_client(client)
    items: list[dict[str, Any]] = []
    for episode in episodes:
        raw = _as_mapping(episode)
        label = str(raw.get("label") or "").strip()
        episode_id = str(raw.get("episode_id") or "").strip()
        if not label or not episode_id:
            continue
        kind = str(raw.get("annotator_kind") or "").lower()
        annotator_kind = {"code": "CODE", "model": "LLM", "human": "HUMAN"}.get(kind)
        if annotator_kind is None:
            continue
        metadata = _episode_metadata(episode, trace_id=trace_id, root_span_id=root_span_id)
        result: dict[str, Any] = {"explanation": str(raw.get("rationale") or "")}
        if raw.get("score") is not None:
            result["score"] = float(raw["score"])
        result["label"] = label
        items.append(
            {
                "name": f"evallab.behavior.{label}",
                "annotator_kind": annotator_kind,
                "span_id": _span_for_episode(episode, span_ids_by_step, root_span_id),
                "result": result,
                "metadata": metadata,
                "identifier": episode_id,
            }
        )
    if not items:
        return PhoenixAnnotationSyncResult()
    spans = getattr(phoenix, "spans", None)
    add_annotation = getattr(spans, "add_span_annotation", None)
    log_annotations = getattr(spans, "log_span_annotations", None)
    if callable(add_annotation):
        inserted = []
        for item in items:
            result = item["result"]
            inserted_item = add_annotation(
                span_id=item["span_id"],
                annotation_name=item["name"],
                annotator_kind=item["annotator_kind"],
                label=result.get("label"),
                score=result.get("score"),
                explanation=result.get("explanation"),
                metadata=item["metadata"],
                identifier=item["identifier"],
                sync=True,
            )
            if inserted_item is not None:
                inserted.append(inserted_item)
    elif callable(log_annotations):
        inserted = log_annotations(span_annotations=items, sync=True) or []
    else:
        raise PhoenixAnnotationError(
            "The installed Phoenix client does not expose spans.add_span_annotation or "
            "spans.log_span_annotations; use arize-phoenix-client>=3.3.0."
        )
    ids = tuple(
        str(identifier) for item in inserted if (identifier := _value(item, "id")) is not None
    )
    return PhoenixAnnotationSyncResult(published=len(items), annotation_ids=ids)


def _annotation_metadata(annotation: Any) -> dict[str, Any]:
    metadata = _value(annotation, "metadata")
    if isinstance(metadata, Mapping):
        return dict(metadata)
    return {}


def _timestamp(annotation: Any, metadata: Mapping[str, Any]) -> datetime | None:
    for value in (
        _value(annotation, "updated_at"),
        _value(annotation, "created_at"),
        metadata.get("updated_at"),
        metadata.get("created_at"),
    ):
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if value:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def _reviewed(annotation: Any) -> bool:
    kind = str(_value(annotation, "annotator_kind") or "").upper()
    source = str(_value(annotation, "source") or "").upper()
    user_id = _value(annotation, "user_id")
    return kind == "HUMAN" and source == "APP" and bool(user_id)


def retrieve_reviewed_behavior_episodes(
    client: Any,
    *,
    project_name: str,
    context: BehaviorDetectionContext,
    trace_id: str,
    root_span_id: str,
    span_ids_by_step: Mapping[int, str],
) -> tuple[BehaviorEpisode, ...]:
    """Retrieve only unambiguous, reviewed annotations matching the exact identity."""
    phoenix = _official_client(client)
    spans = getattr(phoenix, "spans", None)
    getter = getattr(spans, "get_span_annotations", None)
    if not callable(getter):
        raise PhoenixAnnotationError(
            "The installed Phoenix client does not expose spans.get_span_annotations; "
            "use arize-phoenix-client>=3.3.0."
        )
    span_ids = tuple(
        dict.fromkeys([*(str(v) for v in span_ids_by_step.values()), str(root_span_id)])
    )
    annotations = getter(span_ids=span_ids, project_identifier=project_name, limit=1000) or []
    expected = {
        "trace_id": trace_id,
        "root_span_id": root_span_id,
        "trial_id": _context_value(context, "trial_id"),
        "document_id": _context_value(context, "document_id"),
        "trajectory_id": _context_value(context, "trajectory_id"),
        "session_id": _context_value(context, "session_id"),
    }
    candidates: dict[str, list[tuple[Any, dict[str, Any], dict[str, Any]]]] = {}
    allowed_spans = set(span_ids)
    for annotation in annotations:
        if not _reviewed(annotation):
            continue
        span_id = str(_value(annotation, "span_id") or "")
        if span_id not in allowed_spans:
            continue
        name = str(_value(annotation, "name") or "")
        if not name.startswith("evallab.behavior."):
            continue
        metadata = _annotation_metadata(annotation)
        if any(str(metadata.get(key)) != str(value) for key, value in expected.items()):
            continue
        episode_id = str(metadata.get("episode_id") or "")
        label = name.removeprefix("evallab.behavior.")
        if not episode_id or not label or str(metadata.get("label", label)) != label:
            continue
        expected_span = _span_for_episode(metadata, span_ids_by_step, root_span_id)
        if span_id != expected_span:
            continue
        result_map = _as_mapping(_value(annotation, "result") or {})
        candidates.setdefault(episode_id, []).append((annotation, metadata, result_map))
    candidates = {key: items for key, items in candidates.items() if len(items) == 1}
    BehaviorEpisode, deterministic_episode_id = _core_types()
    reconstructed: list[BehaviorEpisode] = []
    for source_episode_id, items in candidates.items():
        annotation, metadata, result = items[0]
        stamp = _timestamp(annotation, metadata)
        annotation_id = str(_value(annotation, "id") or "").strip()
        if stamp is None or not annotation_id:
            continue
        kind_raw = str(_value(annotation, "annotator_kind") or "").upper()
        kind = {"HUMAN": "human", "LLM": "model", "CODE": "code"}.get(kind_raw)
        annotator_id = str(_value(annotation, "user_id") or "").strip()
        if kind is None or not annotator_id:
            continue
        raw_status = str(result.get("label") or "").lower()
        status = raw_status if raw_status in {"confirmed", "rejected"} else "reviewed"
        provenance = dict(metadata.get("provenance") or {})
        provenance.update(
            {
                "source": "phoenix",
                "source_episode_id": source_episode_id,
                "source_annotator_kind": str(metadata.get("annotator_kind") or ""),
                "source_annotator_id": str(metadata.get("annotator_id") or ""),
                "phoenix_annotation_id": annotation_id,
                "phoenix_source": str(_value(annotation, "source") or ""),
                "reviewed_annotator_kind": kind,
                "reviewed_annotator_id": annotator_id,
            }
        )
        try:
            trial_id = str(metadata["trial_id"])
            document_id = str(metadata["document_id"])
            trajectory_id = metadata.get("trajectory_id")
            session_id = metadata.get("session_id")
            start_step = int(metadata["start_step"])
            end_step = int(metadata["end_step"])
            evidence_step_ids = tuple(
                int(value) for value in metadata.get("evidence_step_ids") or ()
            )
            evidence_span_ids = tuple(
                str(value) for value in metadata.get("evidence_span_ids") or ()
            )
            detector_version = metadata.get("detector_version")
            rubric_version = metadata.get("rubric_version")
            catalog_version = metadata.get("catalog_version", "behavior-catalog/v1")
            episode_id = deterministic_episode_id(
                trial_id,
                document_id,
                start_step,
                end_step,
                label,
                evidence_step_ids=evidence_step_ids,
                evidence_span_ids=evidence_span_ids,
                detector_version=detector_version,
                rubric_version=rubric_version,
                catalog_version=catalog_version,
                annotator_kind=kind,
                annotator_id=annotator_id,
                trajectory_id=trajectory_id,
                session_id=session_id,
                provenance=provenance,
            )
            fields = {
                "schema_version": int(metadata.get("schema_version", 1)),
                "episode_id": episode_id,
                "trial_id": trial_id,
                "document_id": document_id,
                "trajectory_id": trajectory_id,
                "session_id": session_id,
                "start_step": start_step,
                "end_step": end_step,
                "label": label,
                "status": status,
                "score": float(result["score"]) if result.get("score") is not None else None,
                "confidence": metadata.get("confidence"),
                "evidence_step_ids": evidence_step_ids,
                "evidence_span_ids": evidence_span_ids,
                "annotator_kind": kind,
                "annotator_id": annotator_id,
                "detector_version": detector_version,
                "rubric_version": rubric_version,
                "catalog_version": catalog_version,
                "source_sha256": str(metadata["source_sha256"]),
                "input_digest": str(metadata["input_digest"]),
                "rationale": str(result.get("explanation") or ""),
                "provenance": provenance,
                "created_at": stamp,
                "updated_at": stamp,
                "reviewed_at": stamp,
            }
            try:
                reconstructed.append(BehaviorEpisode.model_validate(fields))
            except AttributeError:
                reconstructed.append(BehaviorEpisode(**fields))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(sorted(reconstructed, key=lambda episode: str(_value(episode, "episode_id"))))


def sync_behavior_annotations(
    client: Any,
    episodes: Iterable[BehaviorEpisode],
    *,
    project_name: str,
    context: BehaviorDetectionContext,
    trace_id: str,
    root_span_id: str,
    span_ids_by_step: Mapping[int, str],
    persist: bool = True,
) -> PhoenixAnnotationSyncResult:
    """Publish, retrieve reviewed episodes, and optionally invoke core persistence lazily."""
    source = tuple(episodes)
    published = publish_behavior_episodes(
        client,
        source,
        project_name=project_name,
        trace_id=trace_id,
        root_span_id=root_span_id,
        span_ids_by_step=span_ids_by_step,
    )
    reviewed = retrieve_reviewed_behavior_episodes(
        client,
        project_name=project_name,
        context=context,
        trace_id=trace_id,
        root_span_id=root_span_id,
        span_ids_by_step=span_ids_by_step,
    )
    persisted = 0
    if persist and reviewed:
        try:
            from .behavior_episodes import persist_behavior_episodes
        except ImportError as exc:  # pragma: no cover - integration ordering
            raise PhoenixAnnotationError(
                "Core behavior episode persistence is unavailable"
            ) from exc
        persisted = len(persist_behavior_episodes(reviewed))
    return PhoenixAnnotationSyncResult(
        published=published.published,
        retrieved=len(reviewed),
        persisted=persisted,
        skipped=max(0, published.published - len(published.annotation_ids)),
        annotation_ids=published.annotation_ids,
    )


__all__ = [
    "PhoenixAnnotationError",
    "PhoenixAnnotationSyncResult",
    "publish_behavior_episodes",
    "retrieve_reviewed_behavior_episodes",
    "span_ids_by_step",
    "sync_behavior_annotations",
]
