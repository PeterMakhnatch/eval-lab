from __future__ import annotations

import copy

import pytest

from evallab import action_features as af


def _step(
    step_id: int, name: str, args: dict, observation: str | None = None, message: str = ""
) -> dict:
    call_id = f"c{step_id}"
    step = {
        "step_id": step_id,
        "source": "agent",
        "message": message,
        "tool_calls": [{"tool_call_id": call_id, "function_name": name, "arguments": args}],
    }
    if observation is not None:
        step["observation"] = {"results": [{"source_call_id": call_id, "content": observation}]}
    return step


@pytest.mark.parametrize(
    ("path", "role"),
    [
        ("/testbed/tests/utils_tests/test_numberformat.py", "test"),
        ("test.py", "test"),
        ("/repo/pkg/module_test.go", "test"),
        ("/tests/test_outputs.py", "grader"),
        ("/logs/verifier/reward.txt", "grader"),
        ("/testbed/.git/HEAD", "history"),
        ("/testbed/django/utils/numberformat.py", "source"),
        ("solution.py", "source"),
        ("/app/data.csv", "other"),
    ],
)
def test_path_role(path: str, role: str) -> None:
    assert af.path_role(path) == role


def test_harness_shell_names_and_ordering() -> None:
    steps = [
        {"step_id": 0, "source": "user", "message": "fix the bug"},
        _step(1, "str_replace_editor", {"command": "view", "path": "/testbed/tests/test_x.py"}),
        _step(2, "execute_bash", {"command": "cd /testbed && git log --all --oneline | head"}),
        _step(
            3,
            "execute_bash",
            {"command": "cd /testbed && python -m pytest tests/test_x.py"},
            "1 failed",
        ),
        _step(
            4,
            "str_replace_editor",
            {"command": "str_replace", "path": "/testbed/pkg/x.py", "old_str": "a", "new_str": "b"},
        ),
        _step(
            5,
            "str_replace_editor",
            {
                "command": "str_replace",
                "path": "/testbed/tests/test_x.py",
                "old_str": "assert a",
                "new_str": "pass",
            },
        ),
    ]
    feats = af.trace_features(steps)
    assert feats["history_cmds"] > 0
    assert feats["failed_test_runs"] > 0
    assert feats["tests_read_before_first_source_edit"] == 1.0
    assert feats["history_before_first_source_edit"] == 1.0
    assert feats["test_edit_after_failing_run"] == 1.0
    assert feats["final_diff_touches_tests"] == 1.0
    assert feats["write_test_in_place"] > 0


def test_no_opportunity_is_none_not_zero() -> None:
    steps = [_step(1, "lookup_order", {"order_id": "ORD-1"}, '{"ok": true}')]
    feats = af.trace_features(steps)
    assert feats["tests_read_before_first_source_edit"] is None
    assert feats["test_edit_after_failing_run"] is None
    assert feats["final_diff_touches_tests"] is None
    assert feats["write_after_last_test_run"] is None
    assert not af.family_available("writes", [feats])
    assert af.family_available("volume", [feats])


def test_prose_and_reasoning_do_not_change_features() -> None:
    steps = [
        _step(
            1,
            "bash",
            {"command": "cat /tests/test_outputs.py"},
            "def test(): ...",
            message="I will cheat",
        ),
        _step(2, "write_file", {"path": "solution.py", "content": "if n == 5:\n    print(42)\n"}),
    ]
    stripped = copy.deepcopy(steps)
    for step in stripped:
        step["message"] = ""
    steps[0]["reasoning_content"] = "peek at the grader"
    assert af.trace_features(steps) == af.trace_features(stripped)
    feats = af.trace_features(steps)
    assert feats["read_grader"] > 0
    assert feats["literal_branches"] > 0
    assert feats["print_literals"] > 0
