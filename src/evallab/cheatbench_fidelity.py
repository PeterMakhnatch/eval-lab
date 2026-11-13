"""Consumer-fidelity and blinding audit for external CheatBench traces.

The retired CheatBench converter produces lossy detector ATIF and a separate
viewer annotation. This retained audit answers a
different question: when a downstream consumer (a QA-trace investigator, a
detector, or a critic-training exporter) reads a derived artifact, is the
externally labelled evidence still present, and is the answer itself absent?

The audit is deterministic and model-free.  It never turns missing evidence
into a pass: latent localizations, missing artifacts and unresolved snippets
stay ``unknown``/``not_run``.  Snippet text itself is evaluator-only; audit
rows carry hashes and coordinates, not the snippet.
"""

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
    """Find evaluator-only fields/text that must not reach an actor input."""
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
    """Actor-visible steps of ``doc``: every step except CheatBench annotation steps."""
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
    """Original ``step_id`` of each :func:`blind_atif` step, indexed by its new id."""
    return [step.get("step_id") for step in _kept_steps(doc)]


def blind_atif(doc: Mapping[str, Any], *, item_id: str) -> dict[str, Any]:
    """Return an actor-visible ATIF copy with evaluator fields removed.

    Evidence text, tool calls and observations are preserved byte-for-byte at
    the string level.  Top-level metadata is replaced, annotation steps are
    dropped, per-step ``extra`` span side-channels and a root ``extra.cheatbench``
    block are removed, and steps are renumbered from zero.  Mutating the original
    label/mechanism/rationale metadata therefore cannot change the projected
    document.
    """
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
