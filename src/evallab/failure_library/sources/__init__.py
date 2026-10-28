"""Deterministic finding collections; source modules export a ``COLLECTIONS`` map.

Builders take a repository root and optional local state root. Discovery imports
modules only: downloads, database access and writes belong inside builders.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import pkgutil
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from evallab.failure_library.records import (
    DefectClass,
    Direction,
    Evidence,
    Finding,
    Split,
    Status,
    TaskRef,
    derive_tier,
    make_finding_id,
)


class CollectionBuilder(Protocol):
    def __call__(self, root: Path, *, state_root: Path | None = None) -> list[Finding]: ...


def collection_builders() -> dict[str, CollectionBuilder]:
    """Discover collections in module order, refusing ambiguous registrations."""
    builders: dict[str, CollectionBuilder] = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda item: item.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        for name, builder in getattr(module, "COLLECTIONS", {}).items():
            if name in builders:
                raise ValueError(f"duplicate Failure Library collection: {name}")
            if not callable(builder):
                raise TypeError(f"collection {name} does not have a callable builder")
            builders[name] = builder
    return dict(sorted(builders.items()))


def json_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def checked_file(path: Path, digest: str) -> bool:
    """Missing evidence is not execution; changed evidence is a provenance error."""
    if not path.is_file():
        return False
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != digest.removeprefix("sha256:"):
        raise ValueError(f"source digest mismatch: {path}")
    return True


def normalize_split(value: str) -> Split:
    """Preserve an existing key assignment, changing spelling rather than policy."""
    splits: dict[str, Split] = {
        "dev": "DEV",
        "test": "TEST",
        "heldout": "heldout",
        "cross_test": "cross_test",
        "unassigned": "unassigned",
    }
    try:
        return splits[value.lower()]
    except KeyError as exc:
        raise ValueError(f"unknown source split: {value}") from exc


_DEFENSIVE_DESCRIPTIONS: dict[DefectClass, tuple[str, str]] = {
    "TS01": (
        "The verifier may leave required behavior unchecked.",
        "Validate the complete requirement with an independent oracle and improve coverage.",
    ),
    "TS02": (
        "Verifier decision logic may disagree with intended correctness.",
        "Review decision boundaries against correct and incorrect benign reference cases.",
    ),
    "TS03": (
        "Evaluation reference information may not be isolated from task execution.",
        "Audit information boundaries and isolate reference artifacts from the task environment.",
    ),
    "TS04": (
        "Task execution may affect the integrity of verification or scoring.",
        "Validate results in a protected verification environment with immutable scoring inputs.",
    ),
    "TS05": (
        "The verifier may require behavior not stated in the task instructions.",
        "Reconcile the published requirement and oracle, then make the contract explicit.",
    ),
    "TS06": (
        "The instructions and verifier may require contradictory behavior.",
        "Resolve the contradiction and validate both against the same explicit requirement.",
    ),
    "TS07": (
        "The oracle may reject behavior allowed by the stated requirement.",
        "Check valid equivalent results and constrain assertions to the published contract.",
    ),
    "TS08": (
        "The expected answer may disagree with the task's intended result.",
        "Independently derive the reference result and review the answer-key provenance.",
    ),
    "TS09": (
        "Environment assumptions or nondeterminism may affect the recorded grade.",
        "Pin dependencies and resources, and assess reproducibility with benign controls.",
    ),
    "TS10": (
        "Required behavior may be infeasible under the pinned task constraints.",
        "Validate a legitimate reference solution within the documented resource limits.",
    ),
    "TS11": (
        "An LLM judge may confuse untrusted task content with the evaluation rubric.",
        "Separate rubric and evidence, and review judge decisions against independent labels.",
    ),
    "unclassified": (
        "The source reports a task or verifier inconsistency without an established mechanism.",
        "Reconcile instructions, oracle behavior and pinned version changes before classifying.",
    ),
}
_DEFENSIVE_DIRECTIONS: dict[Direction, str] = {
    "wrong_pass": "Reported direction: acceptance despite a requirement mismatch.",
    "unfair_fail": "Reported direction: rejection despite stated requirement compliance.",
    "other": "The direction of the reported correctness mismatch remains unresolved.",
}


def defensive_summary(
    defect_class: DefectClass, direction: Direction, evidence: list[Evidence]
) -> str:
    """Publish symptoms and validation needs, never raw payloads or bypass narratives."""
    kinds = {item.kind for item in evidence}
    if "executed" in kinds:
        provenance = (
            "Retained source evidence reports historical execution; "
            "the importer performs no independent replay."
        )
    elif "upstream_fix" in kinds:
        provenance = (
            "A pinned upstream change is retained as fix evidence; "
            "no differential replay is claimed here."
        )
    elif "human_label" in kinds:
        provenance = (
            "Source-reported label or consensus assessment; "
            "it is not independent execution evidence."
        )
    elif "judge_label" in kinds:
        provenance = "Source-reported judge assessment; agreement is not independent verification."
    else:
        provenance = "Static source claim; the retained evidence does not establish execution."
    symptom, validation = _DEFENSIVE_DESCRIPTIONS[defect_class]
    return (
        f"{provenance} {symptom} {_DEFENSIVE_DIRECTIONS[direction]} {validation} "
        "Use the pinned source and independent validation before asserting current behavior."
    )


def make_record(
    *,
    collection: str,
    locator: str,
    task: TaskRef,
    defect_class: DefectClass,
    direction: Direction,
    summary: str,
    evidence: list[Evidence],
    finder: str,
    source_url: str,
    observed_at: str,
    split: str,
    license: str | None,
    status_now: Status = "unknown",
    fixed_in_revision: str | None = None,
) -> Finding:
    if not task.upstream_revision:
        raise ValueError(f"{collection}/{locator}: missing task revision")
    # The contract has a bounded synopsis; complete narratives stay at the
    # source-native locator, never silently substituted for executed evidence.
    synopsis = summary if len(summary) <= 1000 else summary[:997] + "..."
    return Finding(
        finding_id=make_finding_id(
            task.source_id, task.task_id, task.upstream_revision, defect_class, locator
        ),
        collection=collection,
        locator=locator,
        task=task,
        defect_class=defect_class,
        direction=direction,
        summary=synopsis,
        evidence=evidence,
        tier=derive_tier(item.kind for item in evidence),
        finder=finder,
        source_url=source_url,
        observed_at=date.fromisoformat(observed_at[:10]),
        status_now=status_now,
        fixed_in_revision=fixed_in_revision,
        split=normalize_split(split),
        license=license,
    )
