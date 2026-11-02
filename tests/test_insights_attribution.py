from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.insights.attribution import LayerSpec, build_attribution, load_attribution
from evallab.insights.models import Recipe
from evallab.insights.recompute import RecipeError, row_matches, run_recipe
from evallab.insights.stats import cases_for_layer_value


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
    assert result.min_discordant_cases == 6
    assert result.equals_single_layer is None
    assert result.detection_k == 3
    assert result.prevention_k == 0
    for layer in result.layers:
        assert layer.k == 2
        assert layer.unique_catches == 1
        assert layer.role == "detection"
        assert layer.unique_rate == 0.25
        assert layer.cases_for_80pct_power == cases_for_layer_value(0.25)
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


def test_detection_union_and_overlapping_prevention_are_paired() -> None:
    rows = [
        {"detect": index < 5, "prevent": index < 4}
        for index in range(6)
    ]
    result = build_attribution(rows, {
        "Detection": {"field": "detect"},
        "Prevention": {"field": "prevent", "role": "prevention"},
    })
    assert (result.n, result.union_k, result.detection_k, result.prevention_k) == (6, 5, 5, 4)
    assert result.equals_single_layer == "Detection"
    assert result.min_discordant_cases == 6
    detection, prevention = result.layers
    assert detection.role == "detection"
    assert prevention.role == "prevention"
    assert (detection.unique_catches, prevention.unique_catches) == (1, 0)
    assert detection.unique_rate == pytest.approx(1 / 6)
    assert prevention.unique_rate == 0
    assert detection.p_value == prevention.p_value == 1
    assert not detection.adds_value and not prevention.adds_value
    assert detection.cases_for_80pct_power == cases_for_layer_value(1 / 6)
    assert prevention.cases_for_80pct_power is None


def test_role_counts_are_unions_not_sums() -> None:
    result = build_attribution(
        [{"one": True, "two": True, "detect": False}],
        {
            "One": {"field": "one", "role": "prevention"},
            "Two": {"field": "two", "role": "prevention"},
            "Detect": {"field": "detect"},
        },
    )
    assert result.prevention_k == result.union_k == 1
    assert result.detection_k == 0


def test_empty_attribution_has_no_estimated_rate_or_power() -> None:
    result = build_attribution([], {"Review": {"field": "caught"}})
    assert result.n == result.union_k == result.prevention_k == result.detection_k == 0
    assert result.layers[0].unique_rate is None
    assert result.layers[0].cases_for_80pct_power is None


def test_where_filters_before_identity_and_layer_validation(tmp_path: Path) -> None:
    (tmp_path / "cases.json").write_text(json.dumps([
        {"id": "same", "keep": True, "caught": True},
        {"id": "same", "keep": False},
        {"keep": False},
    ]), encoding="utf-8")
    spec = {
        "cases_file": "cases.json", "case_id_field": "id",
        "where": {"keep": True}, "layers": {"Review": {"field": "caught"}},
    }
    (tmp_path / "attribution.json").write_text(json.dumps(spec), encoding="utf-8")
    result = load_attribution(tmp_path)
    assert result is not None
    assert result.n == result.union_k == 1


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        (None, 2),
        ({"score": 2}, 1),
        ({"score": {"$gte": 2, "$lt": 3}}, 1),
        ({"score": [2, 3]}, 2),
        ({"score": {"$in": [2]}}, 1),
        ({"score": {"$nin": [2]}}, 1),
        ({"score": {"$ne": 2}}, 1),
        ({"flag": True}, 1),
        ({"missing": {"$exists": False}}, 2),
        ({"optional": {"$exists": True}}, 1),
        ({"nested.0.value": "x"}, 1),
        ({"tags": {"$contains": "yes"}}, 1),
    ],
)
def test_where_matches_recipe_semantics(
    tmp_path: Path, where: dict[str, object] | None, expected: int,
) -> None:
    rows = [
        {
            "id": "a", "caught": True, "score": "2", "flag": True,
            "nested": [{"value": "x"}], "tags": ["yes"], "optional": None,
        },
        {"id": "b", "caught": False, "score": 3, "flag": 1, "tags": []},
    ]
    (tmp_path / "cases.json").write_text(json.dumps(rows), encoding="utf-8")
    spec = {
        "cases_file": "cases.json", "case_id_field": "id", "where": where,
        "layers": {"Review": {"field": "caught"}},
    }
    (tmp_path / "attribution.json").write_text(json.dumps(spec), encoding="utf-8")
    report = load_attribution(tmp_path)
    recipe = Recipe(op="count", file="cases.json", where=where)
    assert report is not None
    assert report.n == run_recipe(recipe, tmp_path) == expected
    assert sum(row_matches(row, where) for row in rows) == expected


@pytest.mark.parametrize(
    "where",
    [{"caught": {"$unknown": True}}, {"caught": {"$in": True}}],
)
def test_where_is_validated_even_when_cases_are_empty(
    tmp_path: Path, where: dict[str, object],
) -> None:
    (tmp_path / "cases.json").write_text("[]", encoding="utf-8")
    (tmp_path / "attribution.json").write_text(json.dumps({
        "cases_file": "cases.json", "case_id_field": "id", "where": where,
        "layers": {"Review": {"field": "caught"}},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="where operator|needs a list"):
        load_attribution(tmp_path)


def test_external_spec_keeps_cases_confined_to_experiment(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    folder.mkdir()
    (folder / "cases.json").write_text(
        '[{"id":"a","caught":true}]', encoding="utf-8",
    )
    spec = {
        "cases_file": "cases.json", "case_id_field": "id",
        "layers": {"Review": {"field": "caught"}},
    }
    spec_path = tmp_path / "operator.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    assert load_attribution(folder) is None
    result = load_attribution(folder, spec_path)
    assert result is not None and result.n == result.union_k == 1
    spec["cases_file"] = "../cases.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(RecipeError, match="relative"):
        load_attribution(folder, spec_path)


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        "[]",
        '{"cases_file":"cases.json","case_id_field":"id","layers":'
        '{"Review":{"field":"caught","role":"other"}}}',
    ],
)
def test_external_spec_is_explicitly_validated(tmp_path: Path, payload: str) -> None:
    spec_path = tmp_path / "operator.json"
    spec_path.write_text(payload, encoding="utf-8")
    with pytest.raises(RecipeError, match="cannot read attribution spec"):
        load_attribution(tmp_path, spec_path)


def test_external_missing_spec_does_not_fall_back(tmp_path: Path) -> None:
    with pytest.raises(RecipeError, match="cannot read attribution spec"):
        load_attribution(tmp_path, tmp_path / "missing.json")


def test_selected_case_ids_must_still_be_unique(tmp_path: Path) -> None:
    (tmp_path / "cases.json").write_text(json.dumps([
        {"id": "same", "caught": True}, {"id": "same", "caught": False},
    ]), encoding="utf-8")
    (tmp_path / "attribution.json").write_text(json.dumps({
        "cases_file": "cases.json", "case_id_field": "id",
        "layers": {"Review": {"field": "caught"}},
    }), encoding="utf-8")
    with pytest.raises(RecipeError, match="case ids must be unique"):
        load_attribution(tmp_path)


def test_default_spec_cannot_follow_an_external_symlink(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    folder.mkdir()
    external = tmp_path / "operator.json"
    external.write_text("{}", encoding="utf-8")
    (folder / "attribution.json").symlink_to(external)
    with pytest.raises(RecipeError, match="outside the experiment folder"):
        load_attribution(folder)
