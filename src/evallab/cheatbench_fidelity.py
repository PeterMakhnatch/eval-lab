from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

SCHEMA = "evallab.trace_consumer_audit/v1"
ANNOTATION_MARKER = "[CheatBench annotation"

_GENERIC_EVALUATOR_KEYS = frozenset(
    {
        "annotator_id",
        "cheating_mechanism",
        "cheating_type",
        "label_source",
        "rationale",
    }
)


def find_forbidden_json(obj: Any) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []

    def walk(node: Any, path: str, parent_key: str | None) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_text = str(key)
                lowered = key_text.lower()
                child = f"{path}.{key_text}"
                forbidden = (
                    lowered.startswith("cheatbench_")
                    or lowered.startswith("loc_")
                    or (lowered == "cheatbench" and parent_key == "extra")
                    or (
                        lowered in _GENERIC_EVALUATOR_KEYS
                        and path in {"$", "$.metadata"}
                    )
                )
                if forbidden:
                    hits.append(
                        {"path": child, "kind": "forbidden_key", "detail": key_text}
                    )
                walk(value, child, key_text)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]", parent_key)
        elif isinstance(node, str) and ANNOTATION_MARKER in node:
            hits.append(
                {"path": path, "kind": "annotation_text", "detail": ANNOTATION_MARKER}
            )

    walk(obj, "$", None)
    return hits


def _is_forbidden_top_key(key: Any) -> bool:
    lowered = str(key).lower()
    return (
        lowered.startswith("cheatbench_")
        or lowered.startswith("loc_")
        or lowered in _GENERIC_EVALUATOR_KEYS
    )


def _kept_steps(doc: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    kept: list[Mapping[str, Any]] = []
    for step in doc.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        message = step.get("message")
        if isinstance(message, str) and message.lstrip().startswith(ANNOTATION_MARKER):
            continue
        kept.append(step)
    return kept


def blind_atif_step_origins(doc: Mapping[str, Any]) -> list[Any]:
    return [step.get("step_id") for step in _kept_steps(doc)]


def blind_atif(doc: Mapping[str, Any], *, item_id: str) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for step in _kept_steps(doc):
        projected = copy.deepcopy(dict(step))
        projected.pop("extra", None)
        steps.append(projected)
    for index, step in enumerate(steps):
        step["step_id"] = index
    out: dict[str, Any] = {
        str(key): copy.deepcopy(value)
        for key, value in doc.items()
        if key not in {"metadata", "steps"} and not _is_forbidden_top_key(key)
    }
    extra = out.get("extra")
    if isinstance(extra, dict):
        extra.pop("cheatbench", None)
    out["steps"] = steps
    out["metadata"] = {"blind_projection": SCHEMA, "item_id": item_id}
    return out
