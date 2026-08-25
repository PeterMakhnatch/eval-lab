"""Human-reviewable catalog for the frozen ATIF behavior dimensions.

The catalog is intentionally small and strict: a catalog change must be visible in
review, and adding an experimental candidate must not silently become a calibrated
v1 dimension.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

CATALOG_VERSION = "behavior-catalog/v1"
CALIBRATED_BEHAVIORS = (
    "tool_error",
    "unchanged_retry",
    "recovered_progress",
    "verification_gap",
)
_REQUIRED_DIMENSION_FIELDS = frozenset(
    {
        "definition",
        "required_observables",
        "positive_rule",
        "exclusions",
        "counterexamples",
        "output_fields",
        "known_confounds",
        "detector_version",
        "calibration_status",
    }
)


@dataclass(frozen=True)
class BehaviorDimension:
    """One calibrated or experimental catalog entry."""

    behavior: str
    definition: str
    required_observables: tuple[str, ...]
    positive_rule: str
    exclusions: tuple[str, ...]
    counterexamples: tuple[str, ...]
    output_fields: tuple[str, ...]
    known_confounds: tuple[str, ...]
    detector_version: str
    calibration_status: str


@dataclass(frozen=True)
class BehaviorCatalog:
    """Validated catalog and explicitly separate experimental candidates."""

    catalog_version: str
    behaviors: Mapping[str, BehaviorDimension]
    experimental_candidates: Mapping[str, BehaviorDimension]

    @property
    def version(self) -> str:
        """Compatibility alias for callers that use a short version name."""

        return self.catalog_version


def _as_strings(value: Any, *, field: str, behavior: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError(f"catalog {behavior}.{field} must be a non-empty list of strings")
    return tuple(value)


def _parse_dimension(behavior: str, raw: Any) -> BehaviorDimension:
    if not isinstance(raw, dict):
        raise ValueError(f"catalog behavior {behavior!r} must be a mapping")
    missing = _REQUIRED_DIMENSION_FIELDS - raw.keys()
    if missing:
        raise ValueError(f"catalog behavior {behavior!r} missing fields: {sorted(missing)}")
    unknown = set(raw) - _REQUIRED_DIMENSION_FIELDS
    if unknown:
        raise ValueError(f"catalog behavior {behavior!r} has unknown fields: {sorted(unknown)}")
    if not isinstance(raw["definition"], str) or not raw["definition"]:
        raise ValueError(f"catalog {behavior}.definition must be a non-empty string")
    if not isinstance(raw["positive_rule"], str) or not raw["positive_rule"]:
        raise ValueError(f"catalog {behavior}.positive_rule must be a non-empty string")
    if not isinstance(raw["detector_version"], str) or not raw["detector_version"]:
        raise ValueError(f"catalog {behavior}.detector_version must be a non-empty string")
    if not isinstance(raw["calibration_status"], str) or not raw["calibration_status"]:
        raise ValueError(f"catalog {behavior}.calibration_status must be a non-empty string")
    return BehaviorDimension(
        behavior=behavior,
        definition=raw["definition"],
        required_observables=_as_strings(
            raw["required_observables"], field="required_observables", behavior=behavior
        ),
        positive_rule=raw["positive_rule"],
        exclusions=_as_strings(raw["exclusions"], field="exclusions", behavior=behavior),
        counterexamples=_as_strings(
            raw["counterexamples"], field="counterexamples", behavior=behavior
        ),
        output_fields=_as_strings(raw["output_fields"], field="output_fields", behavior=behavior),
        known_confounds=_as_strings(
            raw["known_confounds"], field="known_confounds", behavior=behavior
        ),
        detector_version=raw["detector_version"],
        calibration_status=raw["calibration_status"],
    )


def load_behavior_catalog(path: Path | None = None) -> BehaviorCatalog:
    """Load and validate the exact v1 calibrated catalog.

    Validation rejects missing dimensions and unknown calibrated dimensions.  The
    experimental section is parsed separately so a candidate cannot be counted as
    a fifth calibrated dimension by accident.
    """

    catalog_path = path or Path(__file__).resolve().parents[2] / "research/behavior/catalog-v1.yaml"
    try:
        raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"unable to read behavior catalog {catalog_path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("behavior catalog must be a mapping")
    if raw.get("catalog_version") != CATALOG_VERSION:
        raise ValueError(f"behavior catalog must declare {CATALOG_VERSION!r}")
    if set(raw) - {"catalog_version", "behaviors", "experimental_candidates"}:
        raise ValueError("behavior catalog has unknown top-level fields")
    behaviors_raw = raw.get("behaviors")
    if not isinstance(behaviors_raw, dict) or set(behaviors_raw) != set(CALIBRATED_BEHAVIORS):
        actual = sorted(behaviors_raw) if isinstance(behaviors_raw, dict) else behaviors_raw
        raise ValueError(
            "behavior catalog dimensions must be exactly "
            f"{list(CALIBRATED_BEHAVIORS)!r}; got {actual!r}"
        )
    experimental_raw = raw.get("experimental_candidates", {})
    if not isinstance(experimental_raw, dict):
        raise ValueError("experimental_candidates must be a mapping")
    if set(experimental_raw) - {"effect_loop_candidate"}:
        raise ValueError("unknown experimental behavior candidate")
    behaviors = {name: _parse_dimension(name, behaviors_raw[name]) for name in CALIBRATED_BEHAVIORS}
    experimental = {name: _parse_dimension(name, value) for name, value in experimental_raw.items()}
    return BehaviorCatalog(
        catalog_version=CATALOG_VERSION,
        behaviors=MappingProxyType(behaviors),
        experimental_candidates=MappingProxyType(experimental),
    )
