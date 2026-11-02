from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.insights.models import ExtractedClaim, Recipe
from evallab.insights.recompute import (
    MAX_FILE_BYTES,
    RecipeError,
    json_pointer,
    load_recipe_file,
    recompute_claim,
    run_recipe,
)


def _claim(recipe: Recipe | None, **changes: object) -> ExtractedClaim:
    return ExtractedClaim.model_validate(
        {
            "local_id": "rate",
            "experiment": "E1",
            "statement": "The pass rate was 22 of 75 (29.3%).",
            "kind": "measurement",
            "k": 22,
            "n": 75,
            "value": 0.293,
            "quote": "22 of 75 (29.3%)",
            "section_id": "section-a",
            "recipe": recipe,
            **changes,
        }
    )


def _json(tmp_path: Path, value: object, name: str = "rows.json") -> str:
    (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    return name


def test_count_nested_where_and_operator_combinations(tmp_path: Path) -> None:
    file = _json(
        tmp_path,
        [
            {"state": "ok", "score": 3, "nested": {"arms": ["a", "b"]}, "tag": None},
            {"state": "ok", "score": 8, "nested": {"arms": ["b"]}},
            {"state": "bad", "score": 12, "nested": {"arms": ["b"]}},
        ],
    )
    assert run_recipe(Recipe(op="count", file=file), tmp_path) == 3
    filters = [
        ({"state": "ok"}, 2),
        ({"state": ["ok", "missing"]}, 2),
        ({"score": {"$gte": 3, "$lt": 10}}, 2),
        ({"score": {"$gt": 3, "$lte": 8}}, 1),
        ({"state": {"$ne": "bad", "$in": ["ok"], "$nin": ["bad"]}}, 2),
        ({"tag": {"$exists": True}}, 1),
        ({"tag": {"$exists": False}}, 2),
        ({"nested.arms.0": "a"}, 1),
        ({"nested.arms": {"$contains": "b"}}, 3),
        ({"state": {"$contains": "k"}}, 2),
    ]
    for where, count in filters:
        assert run_recipe(Recipe(op="count", file=file, where=where), tmp_path) == count


def test_aggregates_rows_pointer_and_canonical_distinct_values(tmp_path: Path) -> None:
    file = _json(
        tmp_path,
        {"rows": [{"v": 2, "key": {"a": 1, "b": 2}}, {"v": 4, "key": {"b": 2, "a": 1}}]},
    )
    assert run_recipe(Recipe(op="sum", file=file, rows="/rows", field="v"), tmp_path) == 6
    assert run_recipe(Recipe(op="mean", file=file, rows="/rows", field="v"), tmp_path) == 3
    recipe = Recipe(op="distinct_count", file=file, rows="/rows", field="key")
    assert run_recipe(recipe, tmp_path) == 1
    with pytest.raises(RecipeError, match="array"):
        run_recipe(Recipe(op="count", file=file), tmp_path)
    with pytest.raises(RecipeError, match="missing"):
        run_recipe(Recipe(op="sum", file=file, rows="/rows", field="absent"), tmp_path)


def test_ratio_and_json_value_with_rfc6901_escapes(tmp_path: Path) -> None:
    file = _json(tmp_path, {"a/b": {"~key": [22, 75]}})
    numerator = Recipe(op="value", file=file, pointer="/a~1b/~0key/0")
    denominator = Recipe(op="value", file=file, pointer="/a~1b/~0key/1")
    recipe = Recipe(op="ratio", num=numerator, den=denominator)
    assert run_recipe(recipe, tmp_path) == pytest.approx(22 / 75)
    assert json_pointer({"": 4}, "/") == 4
    assert json_pointer(4, "") == 4
    for pointer in ("no-slash", "/~2", "/a~1b/~0key/01", "/a~1b/~0key/9"):
        with pytest.raises(RecipeError):
            run_recipe(Recipe(op="value", file=file, pointer=pointer), tmp_path)
    zero_file = _json(tmp_path, 0, "zero.json")
    zero = Recipe(op="value", file=zero_file, pointer="")
    with pytest.raises(RecipeError, match="denominator is zero"):
        run_recipe(Recipe(op="ratio", num=numerator, den=zero), tmp_path)


def test_jsonl_and_csv_numeric_filtering(tmp_path: Path) -> None:
    (tmp_path / "rows.jsonl").write_text('{"v": 2}\n\n{"v": 7}\n', encoding="utf-8")
    assert run_recipe(Recipe(op="sum", file="rows.jsonl", field="v"), tmp_path) == 9
    (tmp_path / "rows.csv").write_text("state,score\nok,2\nok,10\nbad,7\n", encoding="utf-8")
    recipe = Recipe(op="count", file="rows.csv", where={"score": {"$gt": 3}})
    assert run_recipe(recipe, tmp_path) == 2
    assert run_recipe(Recipe(op="count", file="rows.csv", where={"score": 10}), tmp_path) == 1
    assert run_recipe(Recipe(op="mean", file="rows.csv", field="score"), tmp_path) == 19 / 3


@pytest.mark.parametrize("bad_line", ["{", "[1, 2]"])
def test_bad_jsonl_names_physical_line_number(tmp_path: Path, bad_line: str) -> None:
    (tmp_path / "bad.jsonl").write_text(f'{{"ok": true}}\n\n{bad_line}\n', encoding="utf-8")
    with pytest.raises(RecipeError, match="line 3"):
        run_recipe(Recipe(op="count", file="bad.jsonl"), tmp_path)


def test_paths_and_external_symlinks_are_refused(tmp_path: Path) -> None:
    folder = tmp_path / "experiment"
    folder.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("[]", encoding="utf-8")
    (folder / "linked.json").symlink_to(outside)
    (folder / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    for file in (str(outside), "../outside.json", "linked.json", "linked-dir/outside.json"):
        with pytest.raises(RecipeError, match="relative|outside"):
            run_recipe(Recipe(op="count", file=file), folder)
    internal = folder / "internal.json"
    internal.write_text("[]", encoding="utf-8")
    (folder / "safe.json").symlink_to(internal)
    assert run_recipe(Recipe(op="count", file="safe.json"), folder) == 0


def test_oversized_and_non_numeric_receipts_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "huge.json"
    with path.open("wb") as handle:
        handle.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(RecipeError, match="64 MiB"):
        load_recipe_file(path.name, tmp_path)
    file = _json(tmp_path, [{"v": "not a number"}])
    with pytest.raises(RecipeError, match="numeric"):
        run_recipe(Recipe(op="sum", file=file, field="v"), tmp_path)
    empty = _json(tmp_path, [], "empty.json")
    assert run_recipe(Recipe(op="sum", file=empty, field="v"), tmp_path) == 0
    with pytest.raises(RecipeError, match="no selected rows"):
        run_recipe(Recipe(op="mean", file=empty, field="v"), tmp_path)
    with pytest.raises(RecipeError, match="unknown where operator"):
        run_recipe(Recipe(op="count", file=empty, where={"v": {"$wrong": 1}}), tmp_path)


def test_recompute_ratio_printed_percent_and_tolerance_boundaries(tmp_path: Path) -> None:
    file = _json(tmp_path, {"k": 22, "n": 75})
    recipe = Recipe(
        op="ratio",
        num=Recipe(op="value", file=file, pointer="/k"),
        den=Recipe(op="value", file=file, pointer="/n"),
    )
    result = recompute_claim(_claim(recipe), tmp_path)
    assert result is not None and result.status == "reproduced"
    assert result.value == pytest.approx(22 / 75)
    for offset, status in ((0.00509, "reproduced"), (0.00511, "differs")):
        result = recompute_claim(_claim(recipe, value=22 / 75 + offset), tmp_path)
        assert result is not None and result.status == status
    result = recompute_claim(_claim(recipe, value=None), tmp_path)
    assert result is not None and result.status == "reproduced"


def test_recompute_counts_exact_and_scalars_relative(tmp_path: Path) -> None:
    file = _json(tmp_path, [{}, {}, {}])
    recipe = Recipe(op="count", file=file)
    for count, status in ((3, "reproduced"), (4, "differs")):
        result = recompute_claim(_claim(recipe, k=count), tmp_path)
        assert result is not None and result.status == status
    scalar = _json(tmp_path, 100, "scalar.json")
    value_recipe = Recipe(op="value", file=scalar, pointer="")
    for stated, status in ((100.49, "reproduced"), (100.51, "differs")):
        result = recompute_claim(_claim(value_recipe, value=stated), tmp_path)
        assert result is not None and result.status == status
    assert recompute_claim(_claim(None), tmp_path) is None
    result = recompute_claim(_claim(recipe), None)
    assert result is not None and result.status == "not_recomputable"
    result = recompute_claim(_claim(Recipe(op="count", file="missing.json")), tmp_path)
    assert result is not None and result.status == "not_recomputable"
    assert "missing.json" in result.detail


def test_recompute_proportion_value_recipes_use_printed_percent_tolerance(tmp_path: Path) -> None:
    file = _json(tmp_path, {"rate": 0.2981})
    recipe = Recipe(op="value", file=file, pointer="/rate")
    claim = _claim(recipe, k=None, n=None, unit="proportion", value=0.293)
    result = recompute_claim(claim, tmp_path)
    assert result is not None and result.status == "reproduced"
    scalar = _claim(recipe, k=None, n=None, unit=None, value=0.293)
    result = recompute_claim(scalar, tmp_path)
    assert result is not None and result.status == "differs"


def test_recompute_count_fallback_is_exact_even_without_k(tmp_path: Path) -> None:
    file = _json(tmp_path, [{}, {}, {}])
    recipe = Recipe(op="count", file=file)
    result = recompute_claim(_claim(recipe, k=None, value=3), tmp_path)
    assert result is not None and result.status == "reproduced"
    result = recompute_claim(_claim(recipe, k=None, value=3.001), tmp_path)
    assert result is not None and result.status == "differs"
