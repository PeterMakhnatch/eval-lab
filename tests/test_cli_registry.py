"""Public CLI recovery refuses ambiguous or unattributed operator actions."""

from __future__ import annotations

import json

import pytest

from evallab.cli import parser, run_cli


@pytest.mark.parametrize(
    "resolution",
    [
        ["--action", "retry"],
        ["--actor", "operator"],
        ["--action", "discard", "--actor", "operator"],
    ],
)
def test_ambiguous_recovery_requires_explicit_action_and_attribution(resolution) -> None:
    with pytest.raises(SystemExit) as error:
        parser().parse_args(["analyze", "worker-resolve-ambiguous", "request-id", *resolution])
    assert error.value.code == 2


def test_trace_autoreview_cli_compact_only_json(tmp_path, capsys) -> None:
    # Create synthetic minimal ATIF trial
    trial_path = tmp_path / "trajectory.json"
    doc = {
        "session_id": "test-trial-01",
        "steps": [
            {
                "source": "user",
                "message": "Please fix calculation in calc.py",
            },
            {
                "source": "agent",
                "tool_calls": [
                    {
                        "function_name": "bash",
                        "tool_call_id": "c1",
                        "arguments": {"command": "echo 'x = 1' > calc.py"},
                    }
                ],
            },
            {
                "source": "tool",
                "observation": {
                    "results": [
                        {
                            "source_call_id": "c1",
                            "content": "",
                        }
                    ]
                },
            },
        ],
    }
    trial_path.write_text(json.dumps(doc), encoding="utf-8")

    code = run_cli([
        "trace",
        "autoreview",
        str(trial_path),
        "--compact-only",
        "--with-l0-cascade",
        "--json",
    ])
    assert code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "compact_view" in parsed
    assert "should_run_stage2" in parsed
    assert "candidate_step_ids" in parsed
    assert "l0_hit" in parsed
    assert parsed["should_run_stage2"] is False
