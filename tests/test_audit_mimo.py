"""Stored MiMo routing and evidence boundaries, without execution."""

from __future__ import annotations

import pytest

from evallab import audit_mimo

TASK = "format-code-task-000792"


@pytest.mark.parametrize("name", [TASK, f"mimo-v2.6-rl/{TASK}", f"mimo-v2.6-rl__{TASK}"])
def test_exact_task_identity_and_numeric_pages(name: str) -> None:
    assert audit_mimo.recognizes_task(name)
    assert audit_mimo.normalize_task_id(name) == TASK
    assert audit_mimo.task_page_name(name) == "task-000792"


@pytest.mark.parametrize("name", [f"unrelated/{TASK}", f"unrelated__{TASK}", f"prefix/{TASK}",
                                 "format-code-task-792", "event-summary", f"mimo-v2.6-rl/{TASK}/x"])
def test_no_unscoped_basename_identity(name: str) -> None:
    assert not audit_mimo.recognizes_task(name)
    with pytest.raises(ValueError):
        audit_mimo.normalize_task_id(name)

