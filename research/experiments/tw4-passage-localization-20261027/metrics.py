"""Pure source-labelled QC cases; no corpus reads, scans or metric implementation."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from evallab.hackbook.qc import ScoredCase

_LABELS = ("positive", "clean", "unscored")


def _validated_rule_ids(
    values: Iterable[str], *, name: str, allow_empty: bool
) -> frozenset[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ValueError(f"{name} must be an iterable of unique nonempty rule IDs")
    ordered = tuple(values)
    if any(not isinstance(value, str) or not value.strip() for value in ordered):
        raise ValueError(f"{name} must contain nonempty string rule IDs")
    unique = frozenset(ordered)
    if len(unique) != len(ordered):
        raise ValueError(f"{name} must contain unique rule IDs")
    if not unique and not allow_empty:
        raise ValueError(f"{name} requires a nonempty rule universe")
    return unique


def _native_index(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def build_qc_cases(
    *,
    trace_id: str,
    run_label: Literal["positive", "clean", "unscored"],
    steps: Sequence[Mapping[str, Any]],
    hit_steps: Mapping[str, Iterable[int]],
    rule_ids: Iterable[str],
    gating_rule_ids: Iterable[str],
    radius: int = 3,
) -> dict[str, Any]:
    """Build production ``ScoredCase`` rows without inferring any gold labels.

    ``run_cases`` is a one-element list; ``step_cases`` and ``window_cases``
    are sorted by native step index. Exact-anchor step cases are primary QC.
    Window cases retain each center's original label and are localization
    diagnostics only, as is the sorted ``missed_positive_steps`` index list.
    An anchor on an unscored step remains evidence, not a clean gold label.

    The caller owns source admission and the active rule universe, including
    its gating subset. Context firings are retained in every case family but
    cannot remove a gating miss. Duplicate hits at one anchor are idempotent;
    malformed inputs, duplicate coordinates and unknown anchors/IDs raise
    ``ValueError``. No supplied collection or source label is modified.
    """
    if not isinstance(trace_id, str) or not trace_id.strip():
        raise ValueError("trace_id must be a nonempty string")
    if run_label not in _LABELS:
        raise ValueError("run_label must be positive, clean or unscored")
    if isinstance(radius, bool) or not isinstance(radius, int) or radius < 0:
        raise ValueError("radius must be a nonnegative integer")

    active = _validated_rule_ids(rule_ids, name="rule_ids", allow_empty=False)
    gating = _validated_rule_ids(gating_rule_ids, name="gating_rule_ids", allow_empty=True)
    if not gating <= active:
        raise ValueError("gating_rule_ids must be a subset of rule_ids")

    labels: dict[int, Literal["positive", "clean", "unscored"]] = {}
    for step in steps:
        if not isinstance(step, Mapping):
            raise ValueError("steps must contain mappings with step_index and passage_label")
        index = _native_index(step.get("step_index"), name="step_index")
        if index in labels:
            raise ValueError(f"duplicate step_index: {index}")
        label = step.get("passage_label")
        if label not in _LABELS:
            raise ValueError("passage_label must be explicitly positive, clean or unscored")
        labels[index] = label

    if not isinstance(hit_steps, Mapping):
        raise ValueError("hit_steps must map rule IDs to anchor indices")
    fired_by_step: dict[int, set[str]] = {}
    for rule_id, anchors in hit_steps.items():
        if rule_id not in active:
            raise ValueError(f"hit_steps contains unknown rule ID: {rule_id}")
        if isinstance(anchors, (str, bytes)) or not isinstance(anchors, Iterable):
            raise ValueError(f"hit_steps anchors for {rule_id} must be iterable indices")
        for anchor in anchors:
            index = _native_index(anchor, name=f"anchor index for {rule_id}")
            if index not in labels:
                raise ValueError(f"anchor references nonexistent step_index: {index}")
            fired_by_step.setdefault(index, set()).add(rule_id)

    anchor_indices = sorted(fired_by_step)
    step_cases: list[ScoredCase] = []
    window_cases: list[ScoredCase] = []
    missed_positive_steps: list[int] = []
    for index in sorted(labels):
        label = labels[index]
        step_cases.append(
            ScoredCase(
                case_id=f"{trace_id}:step:{index}",
                label=label,
                fired=frozenset(fired_by_step.get(index, ())),
            )
        )
        left = bisect_left(anchor_indices, index - radius)
        right = bisect_right(anchor_indices, index + radius)
        window_fired = frozenset(
            rule_id
            for position in range(left, right)
            for rule_id in fired_by_step[anchor_indices[position]]
        )
        window_cases.append(
            ScoredCase(
                case_id=f"{trace_id}:window:{index}:radius:{radius}",
                label=label,
                fired=window_fired,
            )
        )
        if label == "positive" and not window_fired & gating:
            missed_positive_steps.append(index)

    return {
        "run_cases": [
            ScoredCase(
                case_id=trace_id,
                label=run_label,
                fired=frozenset(
                    rule_id for fired in fired_by_step.values() for rule_id in fired
                ),
            )
        ],
        "step_cases": step_cases,
        "window_cases": window_cases,
        "missed_positive_steps": missed_positive_steps,
    }
