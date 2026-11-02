from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.insights.attribution import LayerSpec, build_attribution, load_attribution
from evallab.insights.recompute import RecipeError


def test_layer_unique_contribution_and_union() -> None:
    rows = [
        {"screen": True, "review": True},
        {"screen": True, "review": False},
        {"screen": False, "review": True},
        {"screen": False, "review": False},
    ]
    result = build_attribution(rows, {
        "Cheap screen": LayerSpec(field="screen"),
        "AutoReview": LayerSpec(field="review"),
    })
    assert result.union_k == 3
    assert result.n == 4
    assert result.union_ci["low"] is not None
    for layer in result.layers:
        assert layer.k == 2
        assert layer.unique_catches == 1
        assert layer.union_without_layer == 2
        assert layer.p_value == 1
        assert "adds value: not shown (1 unique catch" in layer.verdict
        assert layer.min_discordant_cases == 6


def test_five_of_six_is_underpowered_for_added_value() -> None:
    rows = [{"review": index < 5, "screen": False} for index in range(6)]
    result = build_attribution(rows, {
        "AutoReview": LayerSpec(field="review"), "Screen": LayerSpec(field="screen"),
    })
    review = next(layer for layer in result.layers if layer.name == "AutoReview")
    assert (review.k, review.n, review.unique_catches) == (5, 6, 5)
    assert review.p_value == 0.0625
    assert not review.adds_value
    assert "at least 6 discordant cases are needed for p<0.05" in review.verdict
    six = build_attribution([{"review": True}] * 6, {"AutoReview": {"field": "review"}})
    assert six.layers[0].adds_value


def test_attribution_spec_pointer_and_confinement(tmp_path: Path) -> None:
    (tmp_path / "cases.json").write_text(json.dumps({"cases": [
        {"id": "a", "result": {"caught": "yes"}},
        {"id": "b", "result": {"caught": "no"}},
    ]}), encoding="utf-8")
    spec = {
        "cases_file": "cases.json", "rows": "/cases", "case_id_field": "id",
        "layers": {"Review": {"field": "result.caught", "truthy": ["yes"]}},
    }
    path = tmp_path / "attribution.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    result = load_attribution(tmp_path)
    assert result is not None and result.layers[0].k == 1
    spec["cases_file"] = "../outside.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(RecipeError):
        load_attribution(tmp_path)


def test_missing_layer_is_not_silently_a_miss() -> None:
    with pytest.raises(ValueError, match="missing attribution field"):
        build_attribution([{"other": True}], {"Review": {"field": "caught"}})
