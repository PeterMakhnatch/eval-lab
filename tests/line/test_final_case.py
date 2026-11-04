"""Rollout file names are matched case-insensitively wherever admission and the loader meet.

Admission already folds case (``Result.json`` is a sidecar, ``Final.diff`` a diff). The rollout
that is scored must be built the same way, or an upper-case name downgrades a cheat: a
``Final.diff`` was scored as a source file instead of as a diff, ``Agent/trajectory.json`` as
bundle text, and ``rollout.JSON`` was never parsed as a trajectory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from evallab import hack_drop as hd
from evallab.rollout_admission import admit_rollout

ATIF: dict[str, Any] = {
    "schema_version": "ATIF-v1.4",
    "steps": [
        {"step_id": 1, "source": "user", "message": "fix it"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "write",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "function_name": "write",
                    "arguments": {"path": "x.py", "content": "y = 1\n"},
                }
            ],
        },
    ],
}
DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-y = 0\n+y = 1\n"


class _Registry:
    def assigned_test_anywhere(self, **_: str) -> bool:
        return False


def _load(path: Path) -> hd.RolloutItem:
    real = os.path.realpath(path)
    admitted = admit_rollout(real, registry=_Registry(), require_trajectory=False)
    return hd.rollout_from_admitted(Path(real), admitted)


def _trial(tmp_path: Path, files: dict[str, str]) -> Path:
    trial = tmp_path / "job" / "a1"
    for relative, text in files.items():
        target = trial / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return trial


@pytest.mark.parametrize("name", ["final.diff", "Final.diff", "FINAL.DIFF"])
def test_a_final_diff_is_the_rollouts_diff_whatever_its_case(tmp_path: Path, name: str) -> None:
    trial = _trial(tmp_path, {name: DIFF, "agent/trajectory.json": json.dumps(ATIF)})
    assert _load(trial).diff_text == DIFF


@pytest.mark.parametrize("name", ["final.diff", "Final.diff"])
def test_the_loader_without_admission_reads_the_diff_the_same_way(
    tmp_path: Path, name: str
) -> None:
    trial = _trial(tmp_path, {name: DIFF})
    assert hd._load_single_path_rollout(trial).diff_text == DIFF


@pytest.mark.parametrize("name", ["rollout.json", "rollout.JSON", "rollout.Json"])
def test_a_single_file_trajectory_is_parsed_whatever_the_suffix_case(
    tmp_path: Path, name: str
) -> None:
    path = tmp_path / "files" / name
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(ATIF), encoding="utf-8")
    assert _load(path).run is not None
    assert hd._load_single_path_rollout(path).run is not None


@pytest.mark.parametrize("name", ["rollout.diff", "rollout.DIFF", "rollout.Patch"])
def test_a_single_file_diff_is_a_diff_whatever_the_suffix_case(tmp_path: Path, name: str) -> None:
    path = tmp_path / "files" / name
    path.parent.mkdir(parents=True)
    path.write_text(DIFF, encoding="utf-8")
    assert _load(path).diff_text == DIFF
    assert hd._load_single_path_rollout(path).diff_text == DIFF


@pytest.mark.parametrize(
    "relative",
    [
        "Result.json",
        "RESULT.JSON",
        "Final.diff",
        "Agent/notes.txt",
        "VERIFIER/out.txt",
        ".GIT/config",
        "Grade.json",
    ],
)
def test_metadata_and_trajectory_folders_are_not_bundle_source_files_in_any_case(
    relative: str,
) -> None:
    assert not hd.bundle_file_eligible(relative)


def test_an_ordinary_file_is_still_a_bundle_source_file() -> None:
    assert hd.bundle_file_eligible("src/Main.PY")
    assert hd.bundle_file_eligible("Makefile")


def test_a_cheating_patch_is_dropped_whatever_the_case_of_the_diff_name(tmp_path: Path) -> None:
    """The end to end effect: the same cheat is decided alike under ``final.diff`` and
    ``Final.diff``."""
    cheat = "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -1 +1 @@\n-assert f() == 2\n+assert True\n"
    decisions = []
    for index, name in enumerate(("final.diff", "Final.diff")):
        trial = tmp_path / f"case{index}" / "a1"
        trial.mkdir(parents=True)
        (trial / name).write_text(cheat, encoding="utf-8")
        decisions.append(hd.evaluate_l0_stage(_load(trial)).decision)
    assert decisions[0] == decisions[1]
