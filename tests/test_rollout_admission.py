"""Structural rollout admission: the pure helpers and the single-file allow-list."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import rollout_admission as ra


class Registry:
    def assigned_test_anywhere(
        self, *, item_id: str | None = None, task_id: str | None = None
    ) -> bool:
        return "build-pmars" in (item_id, task_id)


@pytest.mark.parametrize(
    "value",
    [
        {"split": "test"},
        {"Split": "test"},
        {"SPLIT": "held-out"},
        {"split": "   "},
        {"split": ["test"]},
        {"split": 1},
        {"a": {"b": {"split": "test"}}},
        {"tasks": [{"split": "test"}]},
        {"metadata": {"split": "Test"}},
    ],
)
def test_any_split_that_is_not_exactly_dev_is_refused(value: dict) -> None:
    assert ra.declares_non_dev(value)


@pytest.mark.parametrize(
    "value",
    [{}, {"split": "dev"}, {"split": " DEV "}, {"split": None}, {"steps": [{"split": "test"}]}],
)
def test_dev_or_absent_splits_and_agent_written_steps_are_accepted(value: dict) -> None:
    assert not ra.declares_non_dev(value)


def test_a_document_nested_past_the_limit_is_refused_even_without_a_split() -> None:
    nested: dict = {"note": 1}
    for _ in range(ra.MAX_DEPTH + 2):
        nested = {"inner": nested}
    assert ra.declares_non_dev(nested)


def test_name_forms_cover_segments_stems_case_and_trailing_separators() -> None:
    forms = ra.name_forms("/Runs/TB2/Build-PMARS__123/")
    assert {"build-pmars__123", "build-pmars", "tb2", "runs"} <= forms
    assert "build-pmars" in ra.name_forms("build-pmars.json")
    assert "build-pmars" in ra.name_forms("tasks\\Build-Pmars")
    assert "build-pmars" in ra.name_forms("\uff42uild-pmars")  # NFKC folds the fullwidth b


def _single(tmp: Path, folder: str = "singles") -> Path:
    file = tmp / folder / "trajectory.json"
    file.parent.mkdir()
    file.write_text(json.dumps({"steps": []}), encoding="utf-8")
    return file


def test_a_plain_single_file_is_admitted(tmp_path: Path) -> None:
    file = _single(tmp_path)
    admitted = ra.admit_rollout(str(file), registry=Registry(), run_id="r1")
    assert admitted.kind == "file" and admitted.trajectory == file


def test_the_listed_path_must_be_canonical(tmp_path: Path) -> None:
    file = _single(tmp_path)
    for text in (str(file.parent) + "/../singles/trajectory.json", "relative/path.json"):
        with pytest.raises(ra.AdmissionRefused, match="not canonical"):
            ra.admit_rollout(text, registry=Registry())


def test_a_registry_test_name_in_the_run_id_is_refused(tmp_path: Path) -> None:
    file = _single(tmp_path)
    with pytest.raises(ra.AdmissionRefused, match="assigned TEST"):
        ra.admit_rollout(str(file), registry=Registry(), run_id="build-pmars__9")
