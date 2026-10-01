"""The decision page answers the six questions from records already on disk."""

from __future__ import annotations

import json
from pathlib import Path

from evallab.process_job import process_job
from evallab.trial_decision import build_decision, compare_hand_key, render_decision_markdown

INSTRUCTION = (
    "You are a security researcher. There is a heap overflow in ndpi_int_check_stun. "
    "Fix the bug."
)
FETCH = "pip download waitress==2.0.0 --no-deps -d /tmp/wtr"


def _terminus(keystrokes: str) -> str:
    return json.dumps(
        {"analysis": "", "plan": "", "commands": [{"keystrokes": keystrokes}]}
    )


def _layout(root: Path) -> Path:
    """A job whose instruction the existing lookup can find."""
    task = root / "runs" / ".prepared-tasks" / "task"
    task.mkdir(parents=True)
    (task / "instruction.md").write_text(INSTRUCTION, encoding="utf-8")
    job = root / "runs" / "job"
    trial = job / "trial-leak"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    (job / "lab-metadata.json").write_text(
        json.dumps(
            {
                "experiment": {"task_path": "runs/.prepared-tasks/task"},
                "trial_budget": {"max_input_tokens": 50000, "max_output_tokens": 8000},
            }
        ),
        encoding="utf-8",
    )
    steps = [
        {
            "step_id": 4,
            "source": "agent",
            "message": _terminus(FETCH),
            "observation": "Saved ./waitress-2.0.0.tar.gz\n",
            "metrics": {"prompt_tokens": 1000, "completion_tokens": 20},
        },
        {
            "step_id": 8,
            "source": "agent",
            "message": _terminus("echo done"),
            "observation": "done\n",
            "metrics": {"prompt_tokens": 1200, "completion_tokens": 8},
        },
    ]
    (trial / "agent" / "trajectory.json").write_text(
        json.dumps({"steps": steps}), encoding="utf-8"
    )
    (trial / "verifier" / "result.json").write_text(
        json.dumps({"message": "No PoC was submitted with submit.sh.", "reward": 0.0}),
        encoding="utf-8",
    )
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": "arvo_18737",
                "trial_name": "trial-leak",
                "verifier_result": {"rewards": {"reward": 0.0}},
                "exception_info": {"exception_type": "TrialBudgetExhaustedError"},
                "agent_result": {"n_input_tokens": 2200, "n_output_tokens": 28},
            }
        ),
        encoding="utf-8",
    )
    return job


def test_page_names_the_grader_gap_and_the_fetch(tmp_path: Path) -> None:
    job = _layout(tmp_path)
    process_job(job, output_dir=tmp_path / "out", ingest=False, publish=False)
    saved = json.loads((tmp_path / "out" / "trial-trial-leak.json").read_text(encoding="utf-8"))
    decision = saved["decision"]

    assert decision["schema"] == "trial_decision/v2"
    assert decision["asked"]["grader_tests_asked"] == "no"
    assert "deliverable_not_in_instruction" in decision["asked"]["grader_gap"]
    assert "submit.sh" in decision["asked"]["grader_gap"]
    assert decision["asked"]["excerpt"].startswith("You are a security researcher")
    assert decision["fetched_fix"]["fetched"] is True
    assert decision["fetched_fix"]["command"] == FETCH
    assert decision["fetched_fix"]["step"] == "head#4"
    assert decision["pass_tainted"]["flagged"] is False
    assert decision["pass_tainted"]["status"] == "not_a_pass"
    assert decision["nop_same_crash"]["answer"] == "not_this_shape"
    assert decision["rule_id"] is not None
    assert decision["attribution"] in {"model", "harness", "unclear", "n/a"}

    page = (tmp_path / "out" / "trial-trial-leak.md").read_text(encoding="utf-8")
    assert page.startswith("# Run report:")
    assert "### Facts" in page
    assert "### Judgments" in page
    assert "Opinions. Do not treat these as facts." in page
    assert "Verdict: `counted_fail`" in page
    assert "Does the grader test that? no" in page
    assert FETCH in page
    assert "not this shape" in page


def test_pass_with_fetch_is_a_candidate_not_a_ruling() -> None:
    decision = build_decision(
        Path("/tmp/does-not-need-to-exist"),
        reward=1.0,
        scored=True,
        outcome={
            "rule_id": "R-NONE-01",
            "attribution": "n/a",
            "note": "scored pass with no outcome-relevant failure",
            "step_ref": None,
        },
        first_failure=None,
        grader_evidence=None,
        taint=[
            {
                "kind": "upstream_fetch",
                "rule": "upstream_fetch:pip-download-remote-package",
                "evidence": "head#4",
                "command": FETCH,
            }
        ],
        token_flow={"last_useful_edit": {"step_id": 8, "command_excerpt": "apply_patch parser.py"}},
    )
    assert decision["whose"] == "none"
    assert decision["pass_tainted"]["flagged"] is True
    assert decision["counts"]["status"] == "pending"
    assert decision["counts"]["verdict"] is None
    assert decision["facts"]["fetches"][0]["command"] == FETCH
    text = "\n".join(render_decision_markdown(decision))
    assert "does not decide whether the result counts" in text
    assert FETCH in text


def test_suspect_grader_is_the_task_and_nop_can_confirm() -> None:
    decision = build_decision(
        Path("/tmp/does-not-need-to-exist"),
        reward=0.0,
        scored=True,
        outcome={
            "rule_id": "R-ENV-02",
            "attribution": "harness",
            "note": "suspect grader: verifier/test-stdout.txt has 4 ERROR at setup",
            "step_ref": "head#15",
        },
        first_failure=None,
        grader_evidence={
            "note": "suspect grader: same setup errors",
            "nop_control_confirms": "true",
            "nop_trial": "qual-1789",
        },
        taint=[],
        token_flow=None,
    )
    assert decision["whose"] == "task"
    assert decision["attribution"] == "harness"
    assert decision["asked"]["grader_tests_asked"] == "no"
    assert decision["nop_same_crash"]["answer"] == "yes"
    assert decision["nop_same_crash"]["nop_trial"] == "qual-1789"
    compared = compare_hand_key(
        decision,
        {"outcome": {"rule": "R-ENV-02", "attribution": "harness"}},
    )
    assert compared["rule_match"] is True
    assert compared["attribution_match"] is True
    mismatch = compare_hand_key(
        decision,
        {"outcome": {"rule": "R-COMP-02", "attribution": "model"}},
    )
    assert mismatch["rule_match"] is False
    assert mismatch["attribution_match"] is False


def test_counts_field_is_rendered_and_never_computed() -> None:
    from evallab.trial_decision import render_counts

    pending = render_counts(None)
    assert pending["status"] == "pending"
    assert pending["verdict"] is None
    rendered = render_counts(
        {
            "verdict": "excluded",
            "reasons": ["copied_fix"],
            "evidence": [{"step": "head#4", "command": FETCH}],
        }
    )
    assert rendered["status"] == "rendered"
    assert rendered["verdict"] == "excluded"
    assert rendered["reasons"] == ["copied_fix"]


def test_echo_task_complete_is_a_completion_claim_loop(tmp_path: Path) -> None:
    from evallab.trial_decision import classify_loop_kind

    trial = tmp_path / "trial"
    agent = trial / "agent"
    agent.mkdir(parents=True)
    steps = [
        {
            "step_id": 1,
            "source": "agent",
            "message": "echo start",
            "observation": {
                "results": [
                    {"content": "Are you sure you want to mark the task as complete?"}
                ]
            },
        }
    ]
    steps.extend(
        {
            "step_id": index,
            "source": "agent",
            "message": 'echo "task_complete"',
            "observation": {"results": [{"content": "task_complete"}]},
        }
        for index in range(2, 14)
    )
    (agent / "trajectory.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    kind = classify_loop_kind(trial, "ceiling:input_tokens", {"loop_onset": {"step_id": 4}})
    assert kind["kind"] == "completion-claim"
    assert kind["claim_bearing_turns"] == 12
    assert kind["turns_after_prompt"] == 12
