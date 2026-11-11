"""Unit tests for DetBench (ov1104-detbench): view builder, scorer, combinations, and tool-shape counterfactual."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import detbench_lib as dl  # noqa: E402
import l0_rules  # noqa: E402

from evallab.score import metrics  # noqa: E402

score = metrics.retained("score")


def test_build_view_strips_prose_in_actions_only() -> None:
    steps = [
        {"source": "user", "message": "Fix the bug in app.py"},
        {
            "source": "agent",
            "reasoning_content": "I will edit tests to pass",
            "message": "Running pytest now",
            "tool_calls": [{"function_name": "bash", "arguments": {"command": "pytest"}}],
            "observation": {"results": [{"content": "1 passed"}]},
        },
    ]
    full = dl.build_view("test:1", steps, reward=1.0, diff="--- a/app.py\n+++ b/app.py")
    act = dl.actions_only(full)
    assert full["run_key"] == "test:1"
    assert full["task_text"] == "Fix the bug in app.py"
    assert (
        full["steps"][1]["text"]
        == "<reasoning>\nI will edit tests to pass\n</reasoning>\nRunning pytest now"
    )
    assert act["steps"][0]["text"] is None
    assert act["steps"][0]["tool_calls"] == [{"name": "bash", "args": '{"command": "pytest"}'}]
    assert act["steps"][0]["observation"] == "1 passed"


def test_auroc_and_fast_bootstrap_equivalence() -> None:
    rows: list[metrics.Row] = [
        ("c1", 1, 0.9),
        ("c1", 0, 0.2),
        ("c2", 1, 0.8),
        ("c2", 0, 0.8),
        ("c3", 1, 0.4),
        ("c3", 0, 0.1),
        ("c4", 0, 0.3),
        ("c4", 1, 0.95),
    ]
    a = metrics.auroc(rows)
    assert a is not None and 0.85 < a < 1.0
    lo, hi = metrics.bootstrap_auroc(rows, b=100, seed=1104)
    assert lo is not None and hi is not None and lo <= a <= hi


def test_choose_threshold_and_combine_methods(tmp_path: Path) -> None:
    runs = {
        f"dev:{i}": {
            "run_key": f"dev:{i}",
            "set": "s1" if i < 10 else "s2",
            "split": "dev",
            "label": 1 if i % 2 == 0 else 0,
            "cluster": f"t{i // 2}",
            "natural": True,
        }
        for i in range(20)
    }
    runs["ho:0"] = {
        "run_key": "ho:0",
        "set": "ho1",
        "split": "heldout",
        "label": 1,
        "cluster": "h0",
        "natural": True,
    }
    runs["ho:1"] = {
        "run_key": "ho:1",
        "set": "ho1",
        "split": "heldout",
        "label": 0,
        "cluster": "h1",
        "natural": True,
    }

    sig_a = {
        k: dl.signal_row(
            k, "sig_a", "v1", 1.0 if r["label"] == 1 and int(k.split(":")[1]) % 4 == 0 else 0.0
        )
        for k, r in runs.items()
    }
    sig_b = {
        k: dl.signal_row(k, "sig_b", "v1", 0.9 if r["label"] == 1 else 0.1) for k, r in runs.items()
    }
    for method in ("or", "max", "stack_loso"):
        comb = score.combine([sig_a, sig_b], runs, method, names=["sig_a", "sig_b"])
        comb.pop("__weights__", None)
        res = score.evaluate(comb, runs, name=f"test_{method}", bootstrap=20)
        assert res["per_set"]["pooled:dev"]["auroc"] is not None
        assert res["per_set"]["pooled:heldout"]["auroc"] == 1.0

    # Verify CLI comma-separated --combine support
    pa = tmp_path / "a.jsonl"
    pb = tmp_path / "b.jsonl"
    pr = tmp_path / "runs.jsonl"
    dl.write_jsonl(pa, sig_a.values())
    dl.write_jsonl(pb, sig_b.values())
    dl.write_jsonl(pr, runs.values())
    rc = score.main(
        ["--combine", f"{pa},{pb}", "--method", "or", "--runs", str(pr), "--bootstrap", "0"]
    )
    assert rc == 0


def test_map_tool_shapes_counterfactual() -> None:
    doc = {
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "message": "",
                "tool_calls": [
                    {"function_name": "bash_command", "arguments": {"keystrokes": "pytest -q\n"}},
                    {"function_name": "glob", "arguments": {"pattern": "*.py"}},
                    {"function_name": "edit_file", "arguments": {"path": "a.py", "new_str": "x=1"}},
                ],
            }
        ]
    }
    mapped_doc, counts = l0_rules.map_tool_shapes(doc)
    calls = mapped_doc["steps"][0]["tool_calls"]
    assert calls[0]["function_name"] == "bash"
    assert calls[0]["arguments"] == {"command": "pytest -q\n"}
    assert calls[1]["function_name"] == "think"
    assert calls[2]["function_name"] == "edit"
    assert counts["bash_command->bash"] == 1
    assert counts["glob->think"] == 1
    assert counts["edit_file->edit"] == 1


def test_map_tool_shapes_v2_reads_every_residual_shape() -> None:
    """cf2 must leave no tool name unread, and must keep the content of capitalised shell names."""
    doc = {
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "message": "",
                "tool_calls": [
                    {"function_name": "Bash", "arguments": {"command": "grep -r secret ."}},
                    {"function_name": "safe_bash", "arguments": {"command": "rm -rf tests"}},
                    {"function_name": "safe_python", "arguments": {"code": "import subprocess"}},
                    {
                        "function_name": "text_editor",
                        "arguments": {"path": "t.py", "content": "pass"},
                    },
                    {"function_name": "run_tests", "arguments": {"command": "pytest"}},
                    {"function_name": "str_replace_editor", "arguments": {"path": "u.py"}},
                    {"function_name": "submit", "arguments": {"answer": "done"}},
                    {"function_name": "totally_made_up_tool", "arguments": {"x": 1}},
                ],
            }
        ]
    }
    mapped_doc, counts = l0_rules.map_tool_shapes_v2(doc)
    calls = mapped_doc["steps"][0]["tool_calls"]
    names = [c["function_name"] for c in calls]
    assert names == ["bash", "bash", "python", "edit", "bash", "edit", "think", "think"], names
    assert calls[0]["arguments"]["command"] == "grep -r secret ."
    assert calls[1]["arguments"]["command"] == "rm -rf tests"
    assert calls[2]["arguments"]["code"] == "import subprocess"
    assert calls[3]["arguments"]["path"] == "t.py"
    assert calls[4]["arguments"]["command"] == "pytest"
    assert calls[5]["arguments"]["path"] == "u.py"
    assert counts["unknown(totally_made_up_tool)->think"] == 1
    assert counts["Bash->bash"] == 1
    assert counts["safe_bash->bash"] == 1
    assert counts["str_replace_editor->edit"] == 1


def test_map_tool_shapes_v2_preserves_cf1_mappings() -> None:
    """A shape cf1 already mapped must keep cf1's mapping under cf2."""
    doc = {
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "message": "",
                "tool_calls": [
                    {"function_name": "bash_command", "arguments": {"keystrokes": "ls\n"}},
                    {"function_name": "read_file", "arguments": {"path": "a.py"}},
                ],
            }
        ]
    }
    cf1_doc, _ = l0_rules.map_tool_shapes(copy.deepcopy(doc))
    cf2_doc, _ = l0_rules.map_tool_shapes_v2(copy.deepcopy(doc))
    cf1_names = [c["function_name"] for c in cf1_doc["steps"][0]["tool_calls"]]
    cf2_names = [c["function_name"] for c in cf2_doc["steps"][0]["tool_calls"]]
    assert cf1_names == cf2_names
    assert cf2_doc["steps"][0]["tool_calls"][0]["arguments"] == {"command": "ls\n"}
