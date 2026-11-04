"""Two things the PR made worse than main, found by the final classification of the review.

* A raw-bundle trajectory ``{"traces": [{"steps": [...]}]}`` is admitted by main; the label check
  refused an honest tool argument named ``label`` inside those steps. Steps inside ``traces`` are
  exempt like top-level steps, and nothing else in a trace is.
* A job folder with a link next to its trials was refused only when the intake receipt hashed it,
  after the batch directory existed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from evallab.line import core
from evallab.line.io import read_json, write_json
from evallab.rollout_admission import AdmissionRefused, admit_rollout, label_key

STEP = {
    "step_id": 1,
    "source": "agent",
    "message": "m",
    "tool_calls": [{"function_name": "tag", "arguments": {"label": "urgent", "is_cheat": "no"}}],
}


class _Registry:
    def assigned_test_anywhere(self, **_: str) -> bool:
        return False


def _bundle(**trace_extra: Any) -> dict[str, Any]:
    return {"traces": [{"steps": [STEP], **trace_extra}]}


def test_tool_arguments_named_like_labels_inside_trace_steps_are_not_a_declaration() -> None:
    assert label_key(_bundle()) is None
    assert label_key({"steps": [STEP]}) is None


@pytest.mark.parametrize(
    "document",
    [
        _bundle(label="cheat"),  # a label on the trace itself
        _bundle(metadata={"is_cheat": True}),
        {"traces": [{"steps": [STEP]}], "ground_truth": "cheat"},
        {"x": {"traces": [{"steps": [STEP]}]}},  # not a top-level traces list
        {"traces": [{"inner": {"steps": [STEP]}}]},  # steps one level too deep
        {"traces": {"steps": [STEP]}},  # traces is not a list
    ],
)
def test_nothing_else_in_a_bundle_is_exempt(document: dict[str, Any]) -> None:
    assert label_key(document) is not None


def test_a_file_that_is_not_a_trajectory_gets_no_steps_exemption() -> None:
    assert label_key(_bundle(), trajectory=False) is not None
    assert label_key({"steps": [STEP]}, trajectory=False) is not None


def test_a_raw_bundle_trajectory_with_label_named_arguments_is_admitted(tmp_path: Path) -> None:
    path = tmp_path / "files" / "bundle.json"
    path.parent.mkdir()
    path.write_text(json.dumps(_bundle()), encoding="utf-8")
    admitted = admit_rollout(os.path.realpath(path), registry=_Registry(), require_trajectory=False)
    assert admitted.kind == "file"


def test_a_bundle_with_a_label_on_a_trace_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "files" / "bundle.json"
    path.parent.mkdir()
    path.write_text(json.dumps(_bundle(label="cheat")), encoding="utf-8")
    with pytest.raises(AdmissionRefused, match="evaluator/label field"):
        admit_rollout(os.path.realpath(path), registry=_Registry(), require_trajectory=False)


@pytest.mark.parametrize("extra", ["link", "fifo"])
def test_a_job_folder_with_a_link_or_special_file_is_refused_before_a_batch_exists(
    fixture_spec: Path, tmp_path: Path, extra: str
) -> None:
    job = tmp_path / "job"
    trial = job / "a1" / "agent"
    trial.mkdir(parents=True)
    (trial / "trajectory.json").write_text(
        json.dumps({"schema_version": "ATIF-v1.4", "steps": [STEP]}), encoding="utf-8"
    )
    (job / "a1" / "result.json").write_text(json.dumps({"task_name": "t1"}), encoding="utf-8")
    if extra == "link":
        (job / "outside.txt").write_text("x", encoding="utf-8")
        (job / "bad_link.txt").symlink_to(job / "outside.txt")
    else:
        os.mkfifo(job / "pipe")
    spec = read_json(fixture_spec)
    spec["sources"][0]["rollouts"] = str(job)
    write_json(fixture_spec, spec)
    with pytest.raises(ValueError, match="symlink|special file"):
        core.run_line(fixture_spec, fixture_spec.parent)
    assert not Path(spec["out_root"]).exists()
