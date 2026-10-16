"""Training-arm builder: held-out ids exclude identically whatever their form."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "build_arms", REPO / "research/experiments/qa-trace/training-arms/build_arms.py"
)
build_arms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_arms)

TRAINING_TASK = "mimo-v2.6-rl/format-code-task-000226"
OTHER_TASK = "mimo-v2.6-rl/format-code-task-000803"


def _sft_split(heldout_task_ids: list[str]) -> str:
    rows = [
        {"task_id": "format-code-task-000226", "task_name": TRAINING_TASK, "split": "heldout"},
        {"task_id": "other-0001", "task_name": "other-domain/other-0001", "split": "heldout"},
        {"task_id": "format-code-task-000803", "task_name": OTHER_TASK, "split": "train"},
    ]
    return json.dumps(
        {"contract": "evallab.sft_split/2", "tasks": rows, "heldout_task_ids": heldout_task_ids}
    )


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("plain-namespaced.txt", "# held-out\nmimo-v2.6-rl/format-code-task-000226\n\n"),
        ("plain-bare.txt", "format-code-task-000226\n"),
        ("list-namespaced.json", json.dumps(["mimo-v2.6-rl/format-code-task-000226"])),
        ("object-bare.json", json.dumps({"heldout": ["format-code-task-000226"]})),
        ("records.json", json.dumps([{"task_id": "format-code-task-000226", "split": "heldout"}])),
        ("sft-split.json", _sft_split(["format-code-task-000226", "other-0001"])),
    ],
)
def test_every_heldout_form_excludes_the_same_training_task(
    tmp_path: Path, name: str, content: str
) -> None:
    path = tmp_path / name
    path.write_text(content)
    heldout = build_arms.heldout_tasks(path)
    assert heldout == {TRAINING_TASK}
    assert build_arms.is_heldout(TRAINING_TASK, heldout)
    assert not build_arms.is_heldout(OTHER_TASK, heldout)


@pytest.mark.parametrize(
    "content",
    [
        "other-dataset/format-code-task-000226\n",
        json.dumps({"heldout": "format-code-task-000226"}),
        json.dumps([{"split": "heldout"}]),
        _sft_split(["format-code-task-000226"]),
    ],
)
def test_unrecognised_heldout_ids_fail_loudly_instead_of_matching_nothing(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "heldout"
    path.write_text(content)
    with pytest.raises(ValueError):
        build_arms.heldout_tasks(path)


def test_control_draw_and_its_replacement_order_never_leave_the_frame() -> None:
    arm_a = [
        {"execution_id": f"e{i:02d}", "task": f"mimo-v2.6-rl/format-code-task-{i % 7:06d}"}
        for i in range(70)
    ]
    excluded = {"mimo-v2.6-rl/format-code-task-000003", "mimo-v2.6-rl/format-code-task-000005"}
    ineligible = {"e00", "e01", "e02"}

    arm_c, order = build_arms.draw_control(arm_a, 17, 20261016, excluded, ineligible)

    assert {e["task"] for e in order}.isdisjoint(excluded)
    assert sorted(e["execution_id"] for e in order) == sorted(
        e["execution_id"]
        for e in arm_a
        if e["task"] not in excluded and e["execution_id"] not in ineligible
    )
    assert arm_c == order[:17]
    with pytest.raises(SystemExit):
        build_arms.draw_control(arm_a, len(order) + 1, 20261016, excluded, ineligible)
