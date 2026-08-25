"""Human-grounded calibration for ATIF behavior episodes.

Calibration is deliberately dimension-by-dimension.  Unreviewed detector output is
never treated as a negative label, and no aggregate score is produced.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from evallab.behavior_catalog import CALIBRATED_BEHAVIORS, BehaviorCatalog, load_behavior_catalog

_REVIEWED_STATUSES = frozenset({"reviewed", "confirmed", "rejected"})
_ACCEPTED_STATUSES = frozenset({"reviewed", "confirmed"})
_STATUS_RANK = {"rejected": 0, "reviewed": 1, "confirmed": 2}
_ROUTE_LABEL = "unresolved_error"


@dataclass(frozen=True)
class BehaviorConfusionCounts:
    behavior: str
    tp: int
    fp: int
    fn: int
    tn: int
    evaluated_count: int
    precision: float | None
    recall: float | None

    @property
    def evaluated(self) -> int:
        return self.evaluated_count


@dataclass(frozen=True)
class BehaviorCalibrationReport:
    """Per-dimension counts and deterministic, reviewable disagreements."""

    per_behavior: Mapping[str, BehaviorConfusionCounts]
    disagreements: tuple[dict[str, Any], ...]
    catalog_version: str

    @property
    def behaviors(self) -> Mapping[str, BehaviorConfusionCounts]:
        return self.per_behavior

    @property
    def counts(self) -> Mapping[str, BehaviorConfusionCounts]:
        return self.per_behavior


def _value(episode: Any, name: str, default: Any = None) -> Any:
    if isinstance(episode, Mapping):
        return episode.get(name, default)
    return getattr(episode, name, default)


def _tuple_text(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _identity_key(episode: Any) -> tuple[Any, ...]:
    """Use identity and bounds, not mutable status or timestamps, for joins."""

    return (
        str(_value(episode, "trial_id", "")),
        str(_value(episode, "document_id", "")),
        _value(episode, "trajectory_id"),
        _value(episode, "session_id"),
        int(_value(episode, "start_step", 0)),
        int(_value(episode, "end_step", 0)),
    )


def _label(episode: Any) -> str:
    return str(_value(episode, "label", ""))


def _status(episode: Any) -> str:
    return str(_value(episode, "status", "candidate"))


def _is_human(episode: Any) -> bool:
    return _value(episode, "annotator_kind") == "human"


def _route(label: str) -> str:
    return "recovered_progress" if label == _ROUTE_LABEL else label


def _episode_details(episode: Any) -> dict[str, Any]:
    """Return evidence/provenance without changing the source record."""

    return {
        "status": _status(episode),
        "annotator_kind": _value(episode, "annotator_kind"),
        "annotator_id": _value(episode, "annotator_id"),
        "evidence_step_ids": _tuple_text(_value(episode, "evidence_step_ids", ())),
        "evidence_span_ids": _tuple_text(_value(episode, "evidence_span_ids", ())),
        "source_sha256": _value(episode, "source_sha256"),
        "input_digest": _value(episode, "input_digest"),
        "catalog_version": _value(episode, "catalog_version"),
        "detector_version": _value(episode, "detector_version"),
        "rubric_version": _value(episode, "rubric_version"),
    }


def _sort_episode(episode: Any) -> tuple[Any, ...]:
    details = _episode_details(episode)
    return (
        _STATUS_RANK.get(_status(episode), -1),
        str(_value(episode, "annotator_id", "")),
        str(_value(episode, "episode_id", "")),
        details["evidence_step_ids"],
        details["evidence_span_ids"],
    )


def _ground_truth_by_key(
    episodes: Iterable[Any],
) -> dict[tuple[Any, ...], dict[str, tuple[Any, bool]]]:
    grouped: dict[tuple[Any, ...], list[Any]] = {}
    for episode in episodes:
        if not _is_human(episode) or _status(episode) not in _REVIEWED_STATUSES:
            continue
        grouped.setdefault(_identity_key(episode), []).append(episode)
    result: dict[tuple[Any, ...], dict[str, tuple[Any, bool]]] = {}
    for key, values in grouped.items():
        by_label: dict[str, tuple[Any, bool]] = {}
        for episode in sorted(values, key=_sort_episode, reverse=True):
            raw_label = _label(episode)
            route = _route(raw_label)
            if route not in CALIBRATED_BEHAVIORS or route in by_label:
                continue
            # unresolved_error is the explicit negative/right-censored route.
            positive = raw_label != _ROUTE_LABEL and _status(episode) in _ACCEPTED_STATUSES
            by_label[route] = (episode, positive)
        result[key] = by_label
    return result


def _candidate_by_key(episodes: Iterable[Any]) -> dict[tuple[Any, ...], dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[Any]] = {}
    for episode in episodes:
        if _status(episode) == "rejected":
            continue
        raw_label = _label(episode)
        # unresolved_error is a negative route, never a positive prediction.
        if raw_label == _ROUTE_LABEL:
            continue
        label = _route(raw_label)
        if label not in CALIBRATED_BEHAVIORS:
            continue
        grouped.setdefault(_identity_key(episode), []).append(episode)
    result: dict[tuple[Any, ...], dict[str, Any]] = {}
    for key, values in grouped.items():
        chosen: dict[str, Any] = {}
        for episode in sorted(values, key=_sort_episode, reverse=True):
            chosen.setdefault(_route(_label(episode)), episode)
        result[key] = chosen
    return result


def _disagreement(
    behavior: str,
    key: tuple[Any, ...],
    candidate: Any,
    human: Any,
    *,
    reason: str,
) -> dict[str, Any]:
    candidate_details = _episode_details(candidate) if candidate is not None else None
    human_details = _episode_details(human) if human is not None else None
    return {
        "behavior": behavior,
        "trial_id": key[0],
        "document_id": key[1],
        "trajectory_id": key[2],
        "session_id": key[3],
        "start_step": key[4],
        "end_step": key[5],
        "candidate_label": _label(candidate) if candidate is not None else None,
        "human_label": _label(human) if human is not None else None,
        "candidate": candidate_details,
        "human": human_details,
        "reason": reason,
    }


def calibrate_behavior_episodes(
    candidate_episodes: Iterable[Any],
    human_episodes: Iterable[Any],
    *,
    catalog: BehaviorCatalog | None = None,
) -> BehaviorCalibrationReport:
    """Compare detector labels with reviewed human labels, per dimension.

    Human records in candidate status (or with non-human provenance) are excluded
    from ground truth.  A missing reviewed unit is excluded rather than counted as
    a negative; only keys present in the reviewed human set are evaluated.
    """

    selected_catalog = catalog or load_behavior_catalog()
    if set(selected_catalog.behaviors) != set(CALIBRATED_BEHAVIORS):
        raise ValueError("calibration requires exactly the four v1 behavior dimensions")
    candidates = _candidate_by_key(candidate_episodes)
    ground_truth = _ground_truth_by_key(human_episodes)
    counts: dict[str, list[int]] = {name: [0, 0, 0, 0] for name in CALIBRATED_BEHAVIORS}
    disagreements: list[dict[str, Any]] = []
    for key in sorted(ground_truth, key=repr):
        truth = ground_truth[key]
        predicted = candidates.get(key, {})
        for behavior in CALIBRATED_BEHAVIORS:
            truth_entry = truth.get(behavior)
            # A reviewed unit for another dimension is not evidence for this one.
            if truth_entry is None:
                continue
            human_episode, truth_positive = truth_entry
            candidate_episode = predicted.get(behavior)
            prediction_positive = candidate_episode is not None
            if prediction_positive and truth_positive:
                counts[behavior][0] += 1
            elif prediction_positive and not truth_positive:
                counts[behavior][1] += 1
            elif not prediction_positive and truth_positive:
                counts[behavior][2] += 1
            else:
                counts[behavior][3] += 1
            if prediction_positive != truth_positive:
                disagreements.append(
                    _disagreement(
                        behavior,
                        key,
                        candidate_episode,
                        human_episode,
                        reason="false_positive" if prediction_positive else "false_negative",
                    )
                )
    disagreements.sort(
        key=lambda item: (
            item["behavior"],
            item["trial_id"],
            item["document_id"],
            item["start_step"],
            item["end_step"],
            item["reason"],
            repr(item["candidate"]),
            repr(item["human"]),
        )
    )
    result: dict[str, BehaviorConfusionCounts] = {}
    for behavior in CALIBRATED_BEHAVIORS:
        tp, fp, fn, tn = counts[behavior]
        result[behavior] = BehaviorConfusionCounts(
            behavior=behavior,
            tp=tp,
            fp=fp,
            fn=fn,
            tn=tn,
            evaluated_count=tp + fp + fn + tn,
            precision=tp / (tp + fp) if tp + fp else None,
            recall=tp / (tp + fn) if tp + fn else None,
        )
    return BehaviorCalibrationReport(
        per_behavior=result,
        disagreements=tuple(disagreements),
        catalog_version=selected_catalog.catalog_version,
    )
