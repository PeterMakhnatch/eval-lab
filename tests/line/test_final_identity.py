"""A TEST-assigned task or trial name is refused wherever a rollout's JSON declares it.

Not only the trajectory and the sidecars: ``metadata.json``, ``verifier/result.json`` and any
other ``.json`` file of the trial name a task too (a split declared in the same files was already
refused, a name was not).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from evallab.rollout_admission import AdmissionRefused, admit_rollout

ATIF: dict[str, Any] = {
    "schema_version": "ATIF-v1.4",
    "steps": [{"step_id": 1, "source": "user", "message": "hi"}],
}
SECRET = "t-secret-held"


class _Registry:
    def assigned_test_anywhere(
        self, *, item_id: str | None = None, task_id: str | None = None
    ) -> bool:
        return SECRET in (item_id, task_id)


def _write(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content), encoding="utf-8")


def _trial(tmp_path: Path, files: dict[str, Any]) -> str:
    trial = tmp_path / "job" / "a1"
    _write(trial / "agent" / "trajectory.json", ATIF)
    _write(trial / "result.json", {"task_name": "t-cert", "trial_name": "a1"})
    for relative, content in files.items():
        _write(trial / relative, content)
    return os.path.realpath(trial)


def test_an_honest_extra_json_file_is_admitted(tmp_path: Path) -> None:
    trial = _trial(tmp_path, {"metadata.json": {"task_id": "t-cert", "note": "fine"}})
    assert admit_rollout(trial, registry=_Registry(), require_trajectory=False).kind == "trial"


@pytest.mark.parametrize(
    "relative",
    ["metadata.json", "verifier/result.json", "agent/extra.json", "sub/deep/notes.json"],
)
@pytest.mark.parametrize("key", ["task_id", "task_name", "trial_name", "item_id"])
def test_a_test_name_in_any_other_json_file_is_refused(
    tmp_path: Path, relative: str, key: str
) -> None:
    trial = _trial(tmp_path, {relative: {key: SECRET}})
    with pytest.raises(AdmissionRefused, match="assigned TEST in registry"):
        admit_rollout(trial, registry=_Registry(), require_trajectory=False)


def test_a_test_name_nested_in_another_json_file_is_refused(tmp_path: Path) -> None:
    trial = _trial(tmp_path, {"metadata.json": {"info": {"task": {"name": SECRET}}}})
    with pytest.raises(AdmissionRefused, match="assigned TEST in registry"):
        admit_rollout(trial, registry=_Registry(), require_trajectory=False)


def test_a_manifest_row_id_that_is_a_test_name_is_refused(tmp_path: Path) -> None:
    trial = _trial(tmp_path, {})
    with pytest.raises(AdmissionRefused, match="assigned TEST in registry"):
        admit_rollout(
            trial,
            registry=_Registry(),
            row={"id": SECRET, "path": trial},
            run_id=SECRET,
            require_trajectory=False,
        )
