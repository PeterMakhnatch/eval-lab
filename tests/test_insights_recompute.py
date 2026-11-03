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
    # The tolerance is half a unit of the digits the quote prints, not a fixed window.
    for stated, quote, status in (
        (0.293, "(29.3%)", "reproduced"),
        (0.294, "(29.4%)", "differs"),
        (0.2937, "(29.37%)", "differs"),
    ):
        result = recompute_claim(_claim(recipe, value=stated, quote=f"22 of 75 {quote}"), tmp_path)
        assert result is not None and result.status == status, stated
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
    for stated, status in ((100.0, "reproduced"), (100.4, "differs")):
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
    # The quote prints 29.3%; 0.2981 is 29.8%.
    claim = _claim(recipe, k=None, n=None, unit="proportion", value=0.293)
    result = recompute_claim(claim, tmp_path)
    assert result is not None and result.status == "differs"
    exact = _claim(recipe, k=None, n=None, unit="proportion", value=0.298, quote="rate 29.8%")
    result = recompute_claim(exact, tmp_path)
    assert result is not None and result.status == "reproduced"


def test_recompute_count_fallback_is_exact_even_without_k(tmp_path: Path) -> None:
    file = _json(tmp_path, [{}, {}, {}])
    recipe = Recipe(op="count", file=file)
    result = recompute_claim(_claim(recipe, k=None, value=3), tmp_path)
    assert result is not None and result.status == "reproduced"
    result = recompute_claim(_claim(recipe, k=None, value=3.001), tmp_path)
    assert result is not None and result.status == "differs"


def test_printed_digit_tolerance_replaces_the_half_point_window(tmp_path: Path) -> None:
    # 29.3% printed to a tenth of a point: 0.2981 is not 0.293 (the old +-0.51-point window
    # called it reproduced); 0.2931 is, and so is 0.29349.
    file = _json(tmp_path, {"rate": 0.2981, "near": 0.29349})
    for pointer, status in (("/rate", "differs"), ("/near", "reproduced")):
        recipe = Recipe(op="value", file=file, pointer=pointer)
        claim = _claim(recipe, k=None, n=None, value=0.293, quote="The rate was 29.3%.")
        result = recompute_claim(claim, tmp_path)
        assert result is not None and result.status == status, pointer
    # A whole printed percent keeps the half-point window: 29% reproduces 0.2949 exactly and
    # 0.2951 only as a rounding (one extra rounding step); 0.2956 is a real difference.
    for rate, status in ((0.2949, "reproduced"), (0.2951, "reproduced"), (0.2956, "differs")):
        file = _json(tmp_path, {"rate": rate}, "whole.json")
        recipe = Recipe(op="value", file=file, pointer="/rate")
        claim = _claim(recipe, k=None, n=None, value=0.29, quote="The rate was 29%.")
        result = recompute_claim(claim, tmp_path)
        assert result is not None and result.status == status, rate


def _ratio_of(file: str) -> Recipe:
    return Recipe(
        op="ratio", num=Recipe(op="value", file=file, pointer="/k"),
        den=Recipe(op="value", file=file, pointer="/n"),
    )


def test_a_gap_within_the_printed_rounding_is_reproduced_with_a_note(tmp_path: Path) -> None:
    # 215/254 = 0.846457. Printing 84.7% rounds twice (0.8465, then 84.7%); the truth is 84.6%.
    recipe = _ratio_of(_json(tmp_path, {"k": 215, "n": 254}))
    claim = _claim(recipe, k=215, n=254, value=0.847, quote="215/254 = 84.7%")
    result = recompute_claim(claim, tmp_path)
    assert result is not None and result.status == "reproduced" and result.note == "rounding"
    assert result.difference == pytest.approx(0.000543, abs=1e-6)
    assert "percentage points" in result.difference_text and "rounding" in result.detail
    # The right printout is clean, with no note and no gap.
    exact = recompute_claim(
        _claim(recipe, k=215, n=254, value=0.846, quote="215/254 = 84.6%"), tmp_path,
    )
    assert exact is not None and exact.status == "reproduced"
    assert exact.note is None and exact.difference is None


def test_a_real_difference_still_differs_and_carries_its_size(tmp_path: Path) -> None:
    recipe = _ratio_of(_json(tmp_path, {"k": 215, "n": 254}))
    claim = _claim(recipe, k=215, n=254, value=0.85, quote="215/254 = 85.0%")
    result = recompute_claim(claim, tmp_path)
    assert result is not None and result.status == "differs" and result.note is None
    assert result.difference == pytest.approx(0.003543, abs=1e-6)
    assert result.difference_text.endswith("(0.354 percentage points)")
    assert "differs by 0.00354" in result.detail
    # Counts are exact: off by one is a difference of one.
    rows = _json(tmp_path, [{}, {}, {}], "rows.json")
    count = recompute_claim(_claim(Recipe(op="count", file=rows), k=4), tmp_path)
    assert count is not None and count.status == "differs"
    assert count.difference == 1 and count.difference_text == "1"
    # A unit other than a proportion spells the gap in that unit.
    seconds = _json(tmp_path, {"v": 3.3}, "seconds.json")
    timed = _claim(
        Recipe(op="value", file=seconds, pointer="/v"), k=None, n=None, value=3.2, unit="s",
        quote="took 3.2 s",
    )
    result = recompute_claim(timed, tmp_path)
    assert result is not None and result.status == "differs"
    assert result.difference == pytest.approx(0.1) and result.difference_text == "0.1 s"


def test_a_ratio_of_two_count_recipes_reproduces_a_rate_with_a_zero_numerator(
    tmp_path: Path,
) -> None:
    rows = [{"kind": "real", "positive": False} for _ in range(42)]
    (tmp_path / "rollouts.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
    )
    recipe = Recipe(
        op="ratio", num=Recipe(op="count", file="rollouts.jsonl", where={"positive": True}),
        den=Recipe(op="count", file="rollouts.jsonl"),
    )
    for value in (None, 0.0):
        claim = _claim(recipe, k=0, n=42, value=value, quote="0/42 spontaneous subversions")
        result = recompute_claim(claim, tmp_path)
        assert result is not None and result.status == "reproduced" and result.value == 0
    wrong = _claim(recipe, k=1, n=42, value=None, quote="1/42 spontaneous subversions")
    result = recompute_claim(wrong, tmp_path)
    assert result is not None and result.status == "differs"


@pytest.mark.parametrize(
    ("receipt", "stated", "unit", "quote", "status"),
    [
        (0.6429, 64.3, "%", "64.3% of rollouts", "reproduced"),
        (64.29, 64.3, "%", "64.3% of rollouts", "reproduced"),
        (64.29, 64.4, "%", "64.4% of rollouts", "differs"),
        (3.24, 3.2, "s", "took 3.2 s", "reproduced"),
        (3.3, 3.2, "s", "took 3.2 s", "differs"),
        (0.243, 0.243, "s", "lag of 243ms", "reproduced"),
        (243.0, 243, "ms", "lag of 243ms", "reproduced"),
        (15.12, 15, "USD", "cost $15", "reproduced"),
        (1_240_000, 1_234_567, None, "ran 1,234,567 tokens", "differs"),
        (1_234_567, 1_234_567, None, "ran 1,234,567 tokens", "reproduced"),
        (1_234_567.4, 1_234_567, None, "ran 1,234,567 tokens", "reproduced"),
    ],
)
def test_recompute_compares_in_canonical_units(
    tmp_path: Path, receipt: float, stated: float, unit: str | None, quote: str, status: str,
) -> None:
    file = _json(tmp_path, {"v": receipt})
    recipe = Recipe(op="value", file=file, pointer="/v")
    claim = _claim(recipe, k=None, n=None, value=stated, unit=unit, quote=quote)
    result = recompute_claim(claim, tmp_path)
    assert result is not None and result.status == status, result


def test_large_scalar_tolerance_is_capped_relative_to_the_value(tmp_path: Path) -> None:
    # "1.2e6" prints to 50,000 of precision; the cap keeps a 4% miss from reproducing.
    file = _json(tmp_path, {"v": 1_250_000})
    recipe = Recipe(op="value", file=file, pointer="/v")
    claim = _claim(recipe, k=None, n=None, value=1_200_000, quote="about 1.2e6 tokens")
    result = recompute_claim(claim, tmp_path)
    assert result is not None and result.status == "differs"


def _counts(tmp_path: Path) -> str:
    return _json(tmp_path, {"weak_count": 18, "cohort_size": 28, "rate": 18 / 28}, "counts.json")


def test_target_defaults_follow_the_operation_and_the_claim(tmp_path: Path) -> None:
    file = _counts(tmp_path)
    weak = Recipe(op="value", file=file, pointer="/weak_count")
    # A claim that only has k (the C2 pattern) compares a value recipe with k.
    only_k = _claim(weak, k=18, n=None, value=None, quote="18 weak rollouts")
    result = recompute_claim(only_k, tmp_path)
    assert result is not None and result.status == "reproduced"
    wrong_k = _claim(weak, k=19, n=None, value=None, quote="19 weak rollouts")
    result = recompute_claim(wrong_k, tmp_path)
    assert result is not None and result.status == "differs"
    # An explicit target is honoured, and n compares the denominator.
    size = Recipe(op="value", file=file, pointer="/cohort_size", target="n")
    result = recompute_claim(_claim(size, k=18, n=28, value=0.643, quote="18 of 28 (64.3%)"), tmp_path)
    assert result is not None and result.status == "reproduced"
    missing = recompute_claim(_claim(size, k=18, n=None, value=None, quote="18 weak"), tmp_path)
    assert missing is not None and missing.status == "not_recomputable"
    assert "no n" in missing.detail


@pytest.mark.parametrize(
    ("pointer", "k", "n", "value", "quote"),
    [
        ("/weak_count", 18, 28, 0.643, "18 of 28 (64.3%) were weak"),
        ("/weak_count", 18, 28, 64.3, "18 of 28 (64.3%) were weak"),
        ("/weak_count", 18, 28, None, "18 of 28 were weak"),
    ],
)
def test_backtest_differs_pattern_count_recipe_for_a_rate(
    tmp_path: Path, pointer: str, k: int, n: int, value: float | None, quote: str,
) -> None:
    """The six real backtest 'differs': a count recipe read as the stated rate."""
    file = _counts(tmp_path)
    unit = "%" if value is not None and value > 1 else None
    untargeted = Recipe(op="value", file=file, pointer=pointer)
    claim = _claim(untargeted, k=k, n=n, value=value, unit=unit, quote=quote)
    result = recompute_claim(claim, tmp_path)
    assert result is not None and result.status != "differs"
    if value is not None:
        # A count against a stated rate is a wrong target, not a disagreement with the source.
        assert result.status == "not_recomputable"
        assert "selects the count k" in result.detail and "target" in result.detail
    # The rate with k/n reproduces from k when the recipe declares it ...
    targeted = Recipe(op="value", file=file, pointer=pointer, target="k")
    result = recompute_claim(claim.model_copy(update={"recipe": targeted}), tmp_path)
    assert result is not None and result.status == "reproduced"
    # ... from a count recipe (defaults to k) ...
    rows = _json(tmp_path, [{"w": index < 18} for index in range(28)], "rows.json")
    counted = Recipe(op="count", file=rows, where={"w": True})
    result = recompute_claim(claim.model_copy(update={"recipe": counted}), tmp_path)
    assert result is not None and result.status == "reproduced"
    # ... and from a ratio recipe against the rate itself.
    ratio = Recipe(
        op="ratio", num=Recipe(op="value", file=file, pointer="/weak_count"),
        den=Recipe(op="value", file=file, pointer="/cohort_size"),
    )
    result = recompute_claim(claim.model_copy(update={"recipe": ratio}), tmp_path)
    assert result is not None and result.status == "reproduced"


def test_precision_of_fifteen_of_fifteen_is_not_the_drop_count(tmp_path: Path) -> None:
    file = _json(tmp_path, {"counts": {"drop": 15}, "evaluation": {"precision": {"rate": 1.0}}})
    claim = _claim(
        Recipe(op="value", file=file, pointer="/counts/drop"), k=15, n=15, value=1.0,
        quote="drop precision 15/15=1.0", metric="precision",
    )
    result = recompute_claim(claim, tmp_path)
    assert result is not None and result.status == "not_recomputable"
    good = Recipe(op="value", file=file, pointer="/evaluation/precision/rate")
    result = recompute_claim(claim.model_copy(update={"recipe": good}), tmp_path)
    assert result is not None and result.status == "reproduced"
    # A genuinely different rate is still a disagreement.
    other = _json(tmp_path, {"evaluation": {"precision": {"rate": 0.8}}}, "other.json")
    different = Recipe(op="value", file=other, pointer="/evaluation/precision/rate")
    result = recompute_claim(claim.model_copy(update={"recipe": different}), tmp_path)
    assert result is not None and result.status == "differs"


def test_recipe_target_is_part_of_the_contract() -> None:
    assert Recipe(op="count", file="a.json").target is None
    assert Recipe(op="value", file="a.json", pointer="", target="n").target == "n"
    with pytest.raises(ValueError):
        Recipe.model_validate({"op": "count", "file": "a.json", "target": "rate"})
