"""`./q locate` proposes receipt locations; every proposed recipe must really evaluate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.insights import locate as locate_module
from evallab.insights.locate import locate_values
from evallab.insights.models import Recipe
from evallab.insights.recompute import run_recipe


def _folder(tmp_path: Path) -> Path:
    root = tmp_path / "exp"
    root.mkdir()
    (root / "summary.json").write_text(json.dumps({
        "summary": {"caught": 2, "total": 3, "rate": 0.667},
        "unrelated": {"caught": 7},
    }), encoding="utf-8")
    (root / "metrics_summary.json").write_text(json.dumps({"defense_rate": 60.0}),
                                               encoding="utf-8")
    rows = [{"id": index, "success": index < 7, "arm": "a" if index % 2 else "b"}
            for index in range(10)]
    (root / "rollouts.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                                         encoding="utf-8")
    return root


def _evaluate(hit: dict[str, object], folder: Path, key: str) -> float:
    recipes = hit["recipes"]
    assert isinstance(recipes, dict)
    return run_recipe(Recipe.model_validate(recipes[key]), folder)


def test_k_and_n_in_one_object_rank_first_with_working_recipes(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    result = locate_values([folder], k=2, n=3)
    top = result["hits"][0]
    assert top["file"] == "summary.json"
    assert top["pointers"] == {"k": "/summary/caught", "n": "/summary/total"}
    assert top["same_object"] is True
    assert _evaluate(top, folder, "k") == 2
    assert _evaluate(top, folder, "n") == 3
    assert _evaluate(top, folder, "ratio") == pytest.approx(2 / 3)
    assert top["recipes"]["k"]["target"] == "k"
    assert top["recipes"]["ratio"]["target"] == "value"


def test_value_matching_is_percent_aware_in_both_directions(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    stored_percent = locate_values([folder], value="60.0%")["hits"]
    assert [hit["pointers"] for hit in stored_percent] == [{"value": "/defense_rate"}]
    assert _evaluate(stored_percent[0], folder, "value") == 60.0
    stored_fraction = locate_values([folder], value="66.7%")["hits"]
    assert stored_fraction[0]["pointers"] == {"value": "/summary/rate"}
    printed_fraction = locate_values([folder], value="0.667")["hits"]
    assert printed_fraction[0]["pointers"] == {"value": "/summary/rate"}
    # A bare 60.0 with an explicit unit is read as a percentage too.
    assert locate_values([folder], value="60.0", unit="%")["hits"][0]["file"] == (
        "metrics_summary.json"
    )
    assert locate_values([folder], value="61.0%")["hits"] == []


def test_row_files_propose_count_recipes_and_a_ratio(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    hits = locate_values([folder], k=7, n=10)["hits"]
    field_hit = next(hit for hit in hits if hit["kind"] == "field" and hit["field"] == "success")
    assert field_hit["file"] == "rollouts.jsonl"
    assert field_hit["recipes"]["k"]["where"] == {"success": True}
    assert _evaluate(field_hit, folder, "k") == 7
    assert _evaluate(field_hit, folder, "ratio") == pytest.approx(0.7)
    assert any(hit["kind"] == "row_count" and "n" in hit["found"] for hit in hits)


def test_json_row_arrays_are_located_with_a_rows_pointer(tmp_path: Path) -> None:
    folder = tmp_path / "exp"
    folder.mkdir()
    cases = [{"case": index, "caught": index < 4} for index in range(6)]
    (folder / "results.json").write_text(json.dumps({"cases": cases}), encoding="utf-8")
    hits = locate_values([folder], k=4)["hits"]
    caught = next(hit for hit in hits if hit.get("field") == "caught")
    assert caught["recipes"]["k"]["rows"] == "/cases"
    assert _evaluate(caught, folder, "k") == 4


def test_numeric_field_sums_and_means_are_proposed(tmp_path: Path) -> None:
    folder = tmp_path / "exp"
    folder.mkdir()
    rows = [{"hits": 3}, {"hits": 5}, {"hits": 4}]
    (folder / "scores.jsonl").write_text("\n".join(json.dumps(row) for row in rows),
                                         encoding="utf-8")
    sums = locate_values([folder], k=12)["hits"]
    assert sums[0]["recipes"]["k"]["op"] == "sum"
    assert _evaluate(sums[0], folder, "k") == 12
    means = locate_values([folder], value="4.0")["hits"]
    assert means[0]["recipes"]["value"]["op"] == "mean"
    assert _evaluate(means[0], folder, "value") == 4.0


def test_requires_a_target_and_a_numeric_value(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    with pytest.raises(ValueError, match="at least one"):
        locate_values([folder])
    with pytest.raises(ValueError, match="not a number"):
        locate_values([folder], value="two thirds")


def test_hits_and_runtime_are_bounded(tmp_path: Path) -> None:
    folder = tmp_path / "exp"
    folder.mkdir()
    for index in range(10):
        (folder / f"summary-{index}.json").write_text(json.dumps({"caught": 5}),
                                                      encoding="utf-8")
    result = locate_values([folder], k=5, max_hits=3)
    assert len(result["hits"]) == 3
    assert any("7 lower-ranked hits omitted" in note for note in result["notes"])
    expired = locate_values([folder], k=5, deadline_seconds=-1.0)
    assert expired["hits"] == []
    assert any("deadline" in note for note in expired["notes"])


def test_aggregate_files_are_searched_before_per_case_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "exp"
    (folder / "cases").mkdir(parents=True)
    for index in range(30):
        (folder / "cases" / f"case-{index:03d}.json").write_text(json.dumps({"caught": 2}),
                                                                 encoding="utf-8")
    (folder / "summary.json").write_text(json.dumps({"caught": 2, "total": 3}),
                                         encoding="utf-8")
    monkeypatch.setattr(locate_module, "MAX_FILES", 2)
    result = locate_values([folder], k=2, n=3)
    assert result["hits"][0]["file"] == "summary.json"
    assert any("first 2 data files" in note for note in result["notes"])


def test_symlinks_and_escapes_are_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"caught": 41}), encoding="utf-8")
    folder = tmp_path / "exp"
    folder.mkdir()
    (folder / "summary.json").symlink_to(outside)
    assert locate_values([folder], k=41)["hits"] == []


def test_small_counts_carry_a_warning(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    assert any("Small k or n" in note for note in locate_values([folder], k=2)["notes"])
    assert not any("Small k or n" in note for note in locate_values([folder], k=7)["notes"])
