"""The decision page answers from records already on disk.

Facts are observed signals; every judgment is an opinion, and only loop
kind carries a measured error rate (PAGE_CALIBRATION, frozen HAR-119
cohort). Counts are rendered, never computed.
"""

from __future__ import annotations

import json
from pathlib import Path

from evallab.process_job import process_job
from evallab.trial_decision import (
    PAGE_CALIBRATION,
    build_decision,
    compare_hand_key,
    render_counts,
    render_decision_markdown,
    render_task_ledger,
)

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


def test_page_keeps_grader_alignment_as_opinion(tmp_path: Path) -> None:
    job = _layout(tmp_path)
    process_job(job, output_dir=tmp_path / "out", ingest=False, publish=False)
    saved = json.loads((tmp_path / "out" / "trial-trial-leak.json").read_text(encoding="utf-8"))
    decision = saved["decision"]

    assert decision["schema"] == "trial_decision/v3"
    # The literal deliverable check is an observed signal; the alignment
    # verdict is an opinion in judgments, not a fact.
    assert "grader_tests_asked" not in decision["facts"]
    assert decision["judgments"]["grader_alignment"]["answer"] == "no"
    assert "deliverable_not_in_instruction" in decision["judgments"]["grader_alignment"]["why"]
    assert "deliverable_not_in_instruction" in decision["facts"]["grader_gap"]
    assert decision["asked"]["grader_tests_asked"] == "no"
    assert decision["asked"]["excerpt"].startswith("You are a security researcher")
    # Legacy string output has no source_call_id: keep the attempt, but do
    # not turn an unbound observation into acquisition evidence.
    assert decision["fetched_fix"]["fetched"] is False
    assert decision["facts"]["fetches"][0]["command"] == FETCH
    assert decision["facts"]["fetches"][0]["outcome"] == "unknown"
    assert decision["pass_tainted"]["flagged"] is False
    assert decision["pass_tainted"]["status"] == "not_a_pass"
    # Verifier output alone says nothing about an unobserved nop control.
    assert decision["facts"]["nop_same_crash"]["answer"] == "unknown"
    # Legacy counts carry no task_status: usability stays unknown, never
    # inferred, and the verdict still renders with its reasons.
    assert decision["counts"]["verdict"] == "counted_fail"
    assert decision["counts"]["task_ledger"]["status"] == "unknown"
    assert decision["rule_id"] is not None
    assert decision["attribution"] in {"model", "harness", "unclear", "n/a"}

    lines = render_decision_markdown(decision)
    assert lines[0] == "## Decision"
    assert "### Facts" in lines
    assert "### Judgments" in lines
    assert "### Counts" in lines


def test_pass_with_command_only_fetch_is_not_a_taint_candidate() -> None:
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
    assert decision["pass_tainted"]["flagged"] is False
    assert decision["fetched_fix"]["fetched"] is False
    assert decision["counts"]["status"] == "pending"
    assert decision["counts"]["verdict"] is None
    assert decision["counts"]["task_ledger"]["status"] == "unavailable"
    assert decision["facts"]["fetches"][0]["command"] == FETCH
    # No trial records exist here: absent data is unknown, not a negative.
    assert decision["facts"]["nop_same_crash"]["answer"] == "unknown"
    assert render_decision_markdown(decision)[0] == "## Decision"


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
    assert decision["judgments"]["grader_alignment"]["answer"] == "no"
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
    pending = render_counts(None)
    assert pending["status"] == "pending"
    assert pending["verdict"] is None
    assert pending["task_ledger"]["status"] == "unavailable"
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
    # Legacy counts without task_status: usability unknown, never usable.
    assert rendered["task_ledger"]["status"] == "unknown"


def test_page_calibration_measures_fields_with_coverage() -> None:
    assert PAGE_CALIBRATION["page_vs_agreed_loop_kind"] == {"agree": 7, "n": 11}
    assert PAGE_CALIBRATION["page_vs_agreed_first_failure"] == {"agree": 1, "n": 9}
    assert PAGE_CALIBRATION["first_failure_coverage"] == {
        "expressed": 1,
        "of": 12,
        "abstentions": 11,
    }
    assert PAGE_CALIBRATION["page_vs_agreed_blame"] == {"agree": 11, "n": 11}
    assert PAGE_CALIBRATION["blame_abstentions"] == 0
    assert PAGE_CALIBRATION["in_sample"] is False
    assert len(PAGE_CALIBRATION["predictor_functions"]["sha256"]) == 64
def test_counts_task_status_renders_match_and_mismatch() -> None:
    matched = render_task_ledger(
        {
            "verdict": "counted_pass",
            "reasons": [],
            "evidence": [],
            "task_status": {
                "status": "usable",
                "ledger_status": "usable",
                "source": "python_task_ledger",
                "path": "ledger.csv#task001618",
                "source_sha256": "abc123",
                "task_id": "task001618",
                "run_digest": "sha256:match",
                "trial_digest": "sha256:match",
                "digest_match": True,
                "reason": "canonical ledger row matched",
                "evidence": [],
            },
        }
    )
    assert matched["status"] == "usable"
    assert matched["digest_match"] is True
    assert matched["task_id"] == "task001618"

    mismatched = render_task_ledger(
        {
            "verdict": "counted_fail",
            "reasons": [],
            "evidence": [],
            "task_status": {
                "status": None,
                "ledger_status": "broken",
                "source": "python_task_ledger",
                "path": "ledger.csv#task000495",
                "source_sha256": "abc123",
                "task_id": "task000495",
                "run_digest": "sha256:other",
                "trial_digest": "sha256:run",
                "digest_match": False,
                "reason": "digest mismatch",
                "evidence": [],
            },
        }
    )
    # The ledger row was not matched: the legacy verdict stands and the
    # status stays unknown rather than borrowing the unmatched row.
    assert mismatched["status"] == "unknown"
    assert mismatched["digest_match"] is False
    assert "not matched" in (mismatched["reason"] or "")




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
