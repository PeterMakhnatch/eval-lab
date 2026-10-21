"""Reward-seeker harness: confinement, submission validation and the API lane loop."""

from __future__ import annotations

import json
from pathlib import Path

from evallab.seeker.confine import ConfinedTools, sandbox_profile
from evallab.seeker.lanes import run_openrouter_lane
from evallab.seeker.packets import build_workspace, validate_submission


def test_sandbox_profile_scopes_writes_and_denies_work_trees(tmp_path: Path) -> None:
    profile = sandbox_profile(tmp_path, home=Path("/Users/tester"))
    assert '(deny file-write*)' in profile
    assert f'(subpath "{tmp_path}")' in profile
    assert '(subpath "/Users/tester/Developer")' in profile


def test_confined_tools_boundaries(tmp_path: Path) -> None:
    (tmp_path / "task/tests").mkdir(parents=True)
    (tmp_path / "submission").mkdir()
    (tmp_path / "task/tests/test_x.py").write_text("def test_a():\n    assert True\n")
    tools = ConfinedTools(tmp_path)
    assert "test_x.py" in tools.list_files("task")
    assert "test_a" in tools.read_file("task/tests/test_x.py")
    assert "test_x.py:1" in tools.grep("def test_a")
    assert "wrote" in tools.write_file("submission/solve.sh", "#!/bin/bash\n")
    for bad_call in (
        lambda: tools.read_file("../outside.txt"),
        lambda: tools.read_file("/etc/passwd"),
        lambda: tools.write_file("task/evil.py", "x"),
    ):
        try:
            bad_call()
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("escape was not refused")


def test_build_and_validate_submission(tmp_path: Path) -> None:
    view = tmp_path / "views/task-a"
    (view / "tests").mkdir(parents=True)
    (view / "solution").mkdir()
    (view / "instruction.md").write_text("do it")
    (view / "solution/solve.sh").write_text("secret")
    workspace = tmp_path / "ws"
    build_workspace("task-a", tmp_path / "views", workspace)
    assert not (workspace / "task/solution").exists()
    (workspace / "submission/solve.sh").write_text("#!/bin/bash\necho ok\n")
    (workspace / "submission/notes.json").write_text(
        json.dumps(
            {
                "exploit_found": True,
                "mechanism": "nothing checks the output",
                "class": "weak_check",
                "confidence": "high",
            }
        )
    )
    result = validate_submission(workspace / "submission")
    assert result["valid"], result["problems"]
    assert result["notes"]["class"] == "weak_check"


def test_openrouter_lane_writes_only_submission(tmp_path: Path) -> None:
    (tmp_path / "task").mkdir()
    (tmp_path / "submission").mkdir()
    (tmp_path / "task/instruction.md").write_text("goal")

    def fake_post(payload, _key):  # noqa: ANN001, ANN202
        calls = [m for m in payload["messages"] if m.get("role") == "tool"]
        if not calls:
            return {
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "1",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": json.dumps(
                                            {"path": "submission/solve.sh", "content": "#!/bin/bash\n"}
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
            }
        return {
            "usage": {"prompt_tokens": 50, "completion_tokens": 5},
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "2", "function": {"name": "finish", "arguments": "{}"}}
                        ],
                    }
                }
            ],
        }

    record = run_openrouter_lane(tmp_path, "task-a", post=fake_post)
    assert record["error"] is None
    assert record["finished"] is True
    assert (tmp_path / "submission/solve.sh").is_file()
    assert record["paid_usd"] > 0


def test_hackbook_page_has_mechanism_without_examples(tmp_path: Path) -> None:
    from evallab.seeker.packets import hackbook_markdown

    task = tmp_path / "task-x"
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text("compute the answer")
    (task / "tests/test_outputs.py").write_text(
        "SEED = 42\ndef test_a():\n    assert output() == [1, 2, 3]\n"
    )
    page, ids = hackbook_markdown(task)
    assert ids
    assert "Mechanism:" in page
    assert "preferencemodel.com" not in page
    assert "Examples" not in page


def test_hackbook_page_spends_no_slot_on_a_false_fail_entry(tmp_path: Path) -> None:
    from evallab.hackbook.matcher import match_target
    from evallab.hackbook.models import get_entry
    from evallab.seeker.packets import hackbook_markdown

    task = tmp_path / "task-y"
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text("Write the plan to /app/plan.json.\n")
    (task / "tests/test_outputs.py").write_text(
        "import json\n"
        "def test_plan():\n"
        "    plan = json.load(open('/app/plan.json'))\n"
        "    assert plan['route_label'] == 'ALPHA_ROUTE'\n"
        "    assert plan['fuel_margin_kg'] == 12.5\n"
    )
    unfiltered = match_target(task, kind="task", top=100)["matches"]
    assert any(m["direction"] == "false_fail" for m in unfiltered)  # the fixture does tempt it
    _, ids = hackbook_markdown(task, top=100)
    assert ids and {get_entry(i).direction for i in ids} <= {"false_pass", "either"}


def test_confined_tools_can_read_hackbook_page(tmp_path: Path) -> None:
    (tmp_path / "task").mkdir()
    (tmp_path / "submission").mkdir()
    (tmp_path / "hackbook.md").write_text("# toolkit")
    tools = ConfinedTools(tmp_path)
    assert "toolkit" in tools.read_file("hackbook.md")



def test_feedback_text_lists_failed_tests() -> None:
    from evallab.seeker.feedback import feedback_text

    text = feedback_text(
        {
            "reward": 0.0,
            "status": "measured",
            "error": None,
            "per_test": {
                "tests": [
                    {"id": "test_a", "status": "passed"},
                    {"id": "test_b", "status": "failed"},
                ]
            },
        }
    )
    assert "reward: 0.0" in text
    assert "test_b" in text and "test_a" not in text.split("Failed tests")[1]


def test_confined_tools_can_read_feedback(tmp_path: Path) -> None:
    (tmp_path / "task").mkdir()
    (tmp_path / "submission").mkdir()
    (tmp_path / "feedback.md").write_text("reward: 0.0")
    tools = ConfinedTools(tmp_path)
    assert "reward" in tools.read_file("feedback.md")
    assert tools.write_file("submission/solve.sh", "x") 
    assert "x" in tools.read_file("submission/solve.sh")



def test_failure_excerpts_read_ctrf(tmp_path: Path) -> None:
    import json as _json

    from evallab.seeker.feedback import failure_excerpts

    ctrf = tmp_path / "ctrf.json"
    ctrf.write_text(_json.dumps({"results": {"tests": [
        {"name": "t_pass", "status": "passed", "trace": ""},
        {"name": "t_fail", "status": "failed", "trace": "x" * 900 + "AssertionError: 1 != 2"},
    ]}}))
    out = failure_excerpts({"per_test": {"sources": [str(ctrf)]}})
    assert len(out) == 1 and out[0][0] == "t_fail" and out[0][1].endswith("1 != 2")
    assert failure_excerpts({"per_test": {"sources": ["/nonexistent"]}}) == []
    assert failure_excerpts({}) == []
