from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.copy_check import copy_check
from evallab.counts import classify_counts

FIX = [
    "def bind_args(signature, args, kwargs):",
    "    bound = signature.bind_partial(*args, **kwargs)",
    "    bound.apply_defaults()",
    "    missing = [name for name in signature.parameters if name not in bound.arguments]",
    "    raise_missing_arguments(missing, signature)",
    "    return dict(bound.arguments.items())",
]
PROMPT = "root@abc:/testbed# "


def _trial(tmp_path: Path, steps: list[tuple[str, str]], added: list[str]) -> Path:
    trial = tmp_path / "trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    (trial / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "step_id": index,
                        "source": "agent",
                        "tool_calls": [{"arguments": {"keystrokes": command}}],
                        "observation": {"results": [{"content": output}]},
                    }
                    for index, (command, output) in enumerate(steps, start=1)
                ]
            }
        )
    )
    diff = [
        "diff --git a/pkg/core.py b/pkg/core.py",
        "--- a/pkg/core.py",
        "+++ b/pkg/core.py",
        "@@ -1 +1 @@",
    ]
    diff += [f"+{line}" for line in added]
    (trial / "verifier" / "agent.diff").write_text("\n".join(diff) + "\n")
    return trial


def test_lines_read_from_a_downloaded_wheel_then_added_are_copied(tmp_path: Path) -> None:
    listing = "\n".join(f"{n:6d}  {line}" for n, line in enumerate(FIX, start=40))
    trial = _trial(
        tmp_path,
        [
            ("pip download pkg==1.0.0 --no-deps -d /tmp/dl\n", "Saved /tmp/dl/pkg-1.0.0.whl"),
            ("cat -n /tmp/x/pkg/core.py | sed -n 40,46p\n", listing),
            ("python3 apply_fix.py\n", ""),
        ],
        FIX,
    )
    flag = copy_check(trial)
    assert flag is not None
    assert flag["kind"] == "copied_code"
    assert flag["matched_lines"] == len(FIX)
    assert [item["step"] for item in flag["source_steps"]] == [2]


def test_echo_of_the_agents_own_heredoc_is_not_outside_code(tmp_path: Path) -> None:
    # The screen of an outside-read step still shows the agent's earlier heredoc,
    # and the heredoc came through a summarised turn, so its text is in no command.
    screen = "\n".join([f"{PROMPT}python3 - <<'EOF'", *(f"> {line}" for line in FIX), "> EOF"])
    trial = _trial(tmp_path, [("cat /tmp/work/notes.txt\n", screen)], FIX)
    assert copy_check(trial) is None


def test_outside_lines_the_agent_had_already_typed_do_not_count(tmp_path: Path) -> None:
    write = "cat > /testbed/pkg/core.py <<'EOF'\n" + "\n".join(FIX) + "\nEOF\n"
    trial = _trial(
        tmp_path,
        [
            (write, ""),
            (
                "diff /testbed/pkg/core.py /tmp/x/pkg/core.py\n",
                "\n".join(f"< {line}" for line in FIX),
            ),
        ],
        FIX,
    )
    assert copy_check(trial) is None


def test_below_the_matched_line_floor_is_not_copied(tmp_path: Path) -> None:
    trial = _trial(tmp_path, [("sed -n 1,9p /tmp/x/pkg/core.py\n", "\n".join(FIX[:4]))], FIX)
    assert copy_check(trial) is None


def test_copied_code_excludes_a_pass_and_leaves_a_failure_counted() -> None:
    flag = {
        "kind": "copied_code",
        "rule": "copy_check/v1",
        "matched_lines": 6,
        "added_lines": 6,
        "source_steps": [{"step": 2, "command": "cat -n /tmp/x/pkg/core.py"}],
        "examples": [],
    }
    passed = classify_counts(reward=1.0, scored=True, taint=[flag])
    assert passed["verdict"] == "excluded"
    assert passed["reasons"] == ["copied_fix"]
    assert passed["evidence"][0]["detector"] == "copy_check"
    failed = classify_counts(reward=0.0, scored=True, taint=[flag])
    assert failed["verdict"] == "counted_fail"


@pytest.mark.parametrize("parent_writes_first", [False, True])
def test_delegated_copy_respects_parent_child_causality(
    tmp_path: Path, parent_writes_first: bool
) -> None:
    write = "cat > /testbed/pkg/core.py <<'EOF'\n" + "\n".join(FIX) + "\nEOF\n"
    commands = [(write, ""), ("delegate lookup", "answer found")]
    if not parent_writes_first:
        commands.reverse()
    trial = _trial(tmp_path, commands, FIX)
    path = trial / "agent" / "trajectory.json"
    trajectory = json.loads(path.read_text())
    delegation = trajectory["steps"][1 if parent_writes_first else 0]
    delegation["observation"]["results"][0]["subagent_trajectory_ref"] = [
        {"trajectory_id": "child"}
    ]
    trajectory["subagent_trajectories"] = [{
        "trajectory_id": "child",
        "steps": [{
            "step_id": 1,
            "source": "agent",
            "tool_calls": [{"arguments": {"path": "/tmp/download/pkg/core.py"}}],
            "observation": {"results": [{"content": "\n".join(FIX)}]},
        }],
    }]
    path.write_text(json.dumps(trajectory))
    flag = copy_check(trial)
    if parent_writes_first:
        assert flag is None
    else:
        assert flag is not None
        assert flag["matched_lines"] == len(FIX)
        assert [source["step"] for source in flag["source_steps"]] == ["child:1"]
