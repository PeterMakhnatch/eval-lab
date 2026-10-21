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
        lambda: tools.read_file("submission/solve.sh"),
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
