"""Behavioral checks for the HAR-116 Part B leak-study selection rule."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

EXP = Path("research/experiments/har116-loopfix-leak/make_specs.py")


def _load():
    spec = importlib.util.spec_from_file_location("har116_make_specs", EXP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MAKE = _load()


def _row(task_id: str, *, label: str = "sound", channel: str = "pypi_fix_released") -> dict:
    return {"task_id": task_id, "label": label, "leak_channel": channel}


def _pool() -> list[dict]:
    return [_row(f"format-code-task-{i:06d}") for i in (1, 2, 3, 4, 5, 6, 7, 8)] + [
        _row("format-code-task-000009", label="broken_environment"),
        _row("format-code-task-000010", label="unknown"),
        _row("format-code-task-000011", channel="pypi_package"),
        _row("format-code-task-000012", channel="none_found"),
    ]


def test_selection_takes_three_eligible_tasks_only() -> None:
    pool, selected = MAKE.select_part_b(_pool(), part_a=[], forced=[], salt="har116-leak-v1")
    assert len(pool) == 8
    assert len(selected) == 3
    assert set(selected) <= {f"format-code-task-{i:06d}" for i in range(1, 9)}


def test_selection_excludes_part_a_and_forced_tasks() -> None:
    rows = _pool()
    pool, selected = MAKE.select_part_b(
        rows,
        part_a=["format-code-task-000001", "format-code-task-000002"],
        forced=["format-code-task-000003", "format-code-task-000004"],
        salt="har116-leak-v1",
    )
    assert len(pool) == 4
    excluded = {
        "format-code-task-000001",
        "format-code-task-000002",
        "format-code-task-000003",
        "format-code-task-000004",
    }
    assert not (set(pool) & excluded)
    assert not (set(selected) & excluded)


def test_selection_is_deterministic_and_order_independent() -> None:
    rows = _pool()
    first = MAKE.select_part_b(rows, part_a=[], forced=[], salt="har116-leak-v1")
    again = MAKE.select_part_b(list(reversed(rows)), part_a=[], forced=[], salt="har116-leak-v1")
    assert first == again
