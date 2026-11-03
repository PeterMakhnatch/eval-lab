"""Structural rollout admission: the pure helpers and the single-file allow-list."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import rollout_admission as ra
from evallab.line.io import digest as line_digest


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
    assert "build-pmars" in ra.name_forms("Build-Pmars__123.TXT")
    assert "build-pmars" in ra.name_forms("build-pmars.tar.gz")
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


def _trial(tmp: Path, name: str = "trial__X", *, agent: bool = True) -> Path:
    trial = tmp / name
    (trial / "verifier").mkdir(parents=True)
    if agent:
        (trial / "agent").mkdir()
        (trial / "agent" / "trajectory.json").write_text(
            json.dumps({"steps": [{"step_id": 1, "message": "original"}]}), encoding="utf-8"
        )
    (trial / "result.json").write_text(json.dumps({"task_name": "fine"}), encoding="utf-8")
    (trial / "verifier" / "agent.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    return trial


def test_the_digest_is_the_lines_and_a_wrong_one_is_an_integrity_refusal(tmp_path: Path) -> None:
    trial = _trial(tmp_path)
    expected = line_digest(trial)
    admitted = ra.admit_rollout(str(trial), registry=Registry(), expected_sha256=expected)
    assert admitted.sha256 == expected and admitted.kind == "trial"
    single = _single(tmp_path, "solo")
    assert ra.admit_rollout(str(single), registry=Registry()).sha256 == line_digest(single)
    with pytest.raises(ra.AdmissionRefused, match="sha256 expected") as refused:
        ra.admit_rollout(str(trial), registry=Registry(), expected_sha256="ab" * 32)
    assert refused.value.integrity and not refused.value.unreadable


def test_the_document_served_is_the_one_the_digest_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trial = _trial(tmp_path)
    expected = line_digest(trial)
    read_tree = ra._read_tree

    def swap_after_reading(root: Path, sealed: frozenset) -> object:
        tree = read_tree(root, sealed)
        (root / "agent" / "trajectory.json").write_text('{"steps": []}', encoding="utf-8")
        (root / "verifier" / "agent.diff").write_text("SWAPPED", encoding="utf-8")
        return tree

    monkeypatch.setattr(ra, "_read_tree", swap_after_reading)
    admitted = ra.admit_rollout(str(trial), registry=Registry(), expected_sha256=expected)
    assert admitted.document["steps"][0]["message"] == "original"
    assert admitted.diff == "diff --git a/x b/x\n"


def test_a_bundle_without_a_trajectory_is_admitted_only_when_asked(tmp_path: Path) -> None:
    trial = _trial(tmp_path, agent=False)
    with pytest.raises(ra.AdmissionRefused, match="no ATIF trajectory"):
        ra.admit_rollout(str(trial), registry=Registry())
    admitted = ra.admit_rollout(str(trial), registry=Registry(), require_trajectory=False)
    assert admitted.document is None and admitted.trajectory is None
    assert admitted.diff == "diff --git a/x b/x\n"


def test_a_json_too_deep_to_parse_is_an_unreadable_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def too_deep(_text: str) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(ra.json, "loads", too_deep)
    with pytest.raises(ra.AdmissionRefused, match="not JSON") as refused:
        ra.admit_rollout(str(_single(tmp_path)), registry=Registry())
    assert refused.value.unreadable


def test_a_name_that_differs_only_by_case_from_the_disk_is_refused(tmp_path: Path) -> None:
    (tmp_path / "Probe").write_text("x", encoding="utf-8")
    if (tmp_path / "probe").exists():
        file = _single(tmp_path, "Folder")
        listed = str(file).replace("Folder", "folder")
        with pytest.raises(ra.AdmissionRefused, match="case does not match"):
            ra.admit_rollout(listed, registry=Registry())
    else:  # a case-sensitive filesystem cannot reach a file by the wrong case at all
        with pytest.raises(ra.AdmissionRefused, match="cannot stat"):
            ra.admit_rollout(str(tmp_path / "PROBE"), registry=Registry())


def test_links_above_an_allowed_root_are_fine_and_links_inside_it_never_are(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    runs = base / "runs"
    runs.mkdir(parents=True)
    trial = _trial(runs)
    (tmp_path / "top").symlink_to(base, target_is_directory=True)  # an OS-level link above
    (runs / "inner").symlink_to(trial, target_is_directory=True)  # a link inside the root
    allowed = [tmp_path / "top" / "runs"]
    listed = str(tmp_path / "top" / "runs" / trial.name)
    assert ra.admit_rollout(listed, registry=Registry(), allowed_roots=allowed).kind == "trial"
    with pytest.raises(ra.AdmissionRefused, match="symlink on the rollout path"):
        ra.admit_rollout(listed, registry=Registry())  # no trusted root: every link refuses
    with pytest.raises(ra.AdmissionRefused, match="symlink on the rollout path"):
        ra.admit_rollout(
            str(tmp_path / "top" / "runs" / "inner"), registry=Registry(), allowed_roots=allowed
        )
    with pytest.raises(ra.AdmissionRefused, match="not under an allowed root"):
        ra.admit_rollout(str(trial), registry=Registry(), allowed_roots=allowed)


def test_an_allowed_root_that_is_itself_a_link_is_refused(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    trial = _trial(tmp_path / "real")
    link = tmp_path / "link-root"
    link.symlink_to(trial.parent, target_is_directory=True)
    with pytest.raises(ra.AdmissionRefused, match="symlink on the rollout path"):
        ra.admit_rollout(str(link / trial.name), registry=Registry(), allowed_roots=[link])


def test_a_link_above_an_allowed_root_cannot_hide_a_test_name_on_its_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "build-pmars"  # a TEST task name, reached only through a link
    (target / "runs").mkdir(parents=True)
    trial = _trial(target / "runs")
    (tmp_path / "top").symlink_to(target, target_is_directory=True)
    listed = str(tmp_path / "top" / "runs" / trial.name)
    allowed = [tmp_path / "top" / "runs"]
    with pytest.raises(ra.AdmissionRefused, match="assigned TEST"):
        ra.admit_rollout(listed, registry=Registry(), allowed_roots=allowed)
