"""Paired, per-case attribution for cheat-detection layers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import Field

from evallab.insights.recompute import RecipeError, json_pointer, load_recipe_file
from evallab.insights.stats import mcnemar_exact, min_n_mcnemar, wilson
from evallab.schemas import ContractModel


def _default_truthy() -> list[str | int | float | bool | None]:
    return [True]


class LayerSpec(ContractModel):
    field: str
    truthy: list[str | int | float | bool | None] = Field(
        default_factory=_default_truthy, min_length=1,
    )


class AttributionSpec(ContractModel):
    cases_file: str
    rows: str = ""
    case_id_field: str
    layers: dict[str, LayerSpec]


class LayerAttribution(ContractModel):
    name: str
    k: int
    n: int
    ci: dict[str, float | None]
    unique_catches: int
    union_without_layer: int
    p_value: float | None
    min_discordant_cases: int
    adds_value: bool
    verdict: str


class AttributionReport(ContractModel):
    n: int
    union_k: int
    union_ci: dict[str, float | None]
    layers: list[LayerAttribution]
    caveat: str = (
        "These are paired results on the supplied cases, not proof of performance on new cases. "
        "Per-layer tests are unadjusted; check multiple comparisons before selecting a layer."
    )


def _field(row: Mapping[str, object], field: str) -> object:
    value: object = row
    for part in field.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise ValueError(f"missing attribution field: {field}")
        value = value[part]
    return value


def build_attribution(
    rows: Sequence[Mapping[str, object]],
    layers: Mapping[str, LayerSpec | Mapping[str, object]],
) -> AttributionReport:
    """Compare the union with each leave-one-layer-out union on the same cases."""
    specs = {
        name: spec if isinstance(spec, LayerSpec) else LayerSpec.model_validate(spec)
        for name, spec in sorted(layers.items())
    }
    if not specs:
        raise ValueError("attribution needs at least one layer")
    catches: dict[str, list[bool]] = {}
    for name, spec in specs.items():
        values = [_field(row, spec.field) for row in rows]
        catches[name] = [
            any(type(value) is type(truth) and value == truth for truth in spec.truthy)
            for value in values
        ]
    n = len(rows)
    union = [any(values[index] for values in catches.values()) for index in range(n)]
    minimum = min_n_mcnemar()["n"]
    results: list[LayerAttribution] = []
    for name, values in catches.items():
        without = [any(
            other[index] for other_name, other in catches.items() if other_name != name
        ) for index in range(n)]
        unique = sum(full and not reduced for full, reduced in zip(union, without, strict=True))
        p_value = mcnemar_exact(unique, 0)["p_value"]
        shown = p_value is not None and p_value < 0.05
        noun = "catch" if unique == 1 else "catches"
        detail = (
            f"{unique} unique {noun}; paired exact p={p_value:.4g}"
            if shown and p_value is not None else
            f"{unique} unique {noun}; at least {minimum} discordant cases "
            "are needed for p<0.05"
        )
        results.append(LayerAttribution(
            name=name, k=sum(values), n=n, ci=wilson(sum(values), n),
            unique_catches=unique, union_without_layer=sum(without), p_value=p_value,
            min_discordant_cases=minimum, adds_value=shown,
            verdict=f"{name} adds value: {'shown on these cases' if shown else 'not shown'} "
                    f"({detail})",
        ))
    return AttributionReport(n=n, union_k=sum(union), union_ci=wilson(sum(union), n), layers=results)


def load_attribution(folder: Path) -> AttributionReport | None:
    """Read an optional attribution.json spec through the recipe confinement rules."""
    if not (folder / "attribution.json").exists():
        return None
    spec = AttributionSpec.model_validate(load_recipe_file("attribution.json", folder))
    document = load_recipe_file(spec.cases_file, folder)
    selected = json_pointer(document, spec.rows)
    if not isinstance(selected, list):
        raise RecipeError("attribution rows must be an array of objects")
    rows: list[Mapping[str, object]] = []
    for row in selected:
        if not isinstance(row, dict):
            raise RecipeError("attribution rows must be an array of objects")
        rows.append(row)
    identities = [str(_field(row, spec.case_id_field)) for row in rows]
    if len(set(identities)) != len(identities):
        raise RecipeError("attribution case ids must be unique")
    return build_attribution(rows, spec.layers)
