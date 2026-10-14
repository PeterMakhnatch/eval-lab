"""`evallab qa trace`: bundle sealing, skip rule, stage precedence, paired scoring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.qa_trace import (
    T1_SCHEMA,
    T2_SCHEMA,
    T3_SCHEMA,
    Bundle,
    CallResult,
    ModelRoute,
    QuotaGate,
    QuotaStop,
    TraceConfig,
    build_bundle,
    review_bundle,
    score_paired,
    skip_rule,
    t2_route,
    tj_atif,
    tj_bundle,
    tj_source_step,
)


def _trial(tmp_path: Path, *, result: dict[str, Any]) -> Path:
    trial = tmp_path / "trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    steps = [
        {"step_id": 1, "source": "user", "message": "Fix parse() in app.py"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "editing",
            "tool_calls": [
                {"function_name": "shell", "arguments": {"keystrokes": "sed -i s/a/b/ app.py"}}
            ],
            "observation": {"results": [{"content": "ok"}]},
        },
    ]
    (trial / "agent" / "trajectory.json").write_text(json.dumps({"steps": steps}))
    (trial / "result.json").write_text(json.dumps(result))
    (trial / "verifier" / "test-stdout.txt").write_text("1 passed")
    (trial / "verifier" / "agent.diff").write_text("--- a/app.py\n+++ b/app.py\n-a\n+b\n")
    return trial


@pytest.mark.parametrize(
    ("reward", "grader", "skipped"),
    [
        (1.0, True, {"false_negative", "failure_cause"}),
        (0.0, True, {"false_positive"}),
        (0.5, True, set()),
        (None, True, {"false_negative"}),
        (0.5, False, {"prompt_grader_alignment"}),
    ],
)
def test_skip_rule_follows_the_recorded_reward(
    reward: float | None, grader: bool, skipped: set[str]
) -> None:
    assert set(skip_rule(reward, has_grader=grader)) == skipped


def test_bundle_reads_only_the_verifier_reward_and_seals_its_files(tmp_path: Path) -> None:
    # A converter-written top-level reward/label must never reach the reviewer.
    trial = _trial(tmp_path, result={"reward": 1, "cheatbench_label": "cheating"})
    bundle = build_bundle(trial, tmp_path / "bundle")
    assert bundle.reward is None
    assert "cheating" not in json.dumps(bundle.manifest)
    assert not any("cheating" in p.read_text() for p in bundle.root.rglob("*") if p.is_file())
    assert bundle.manifest["availability"]["diff"].startswith("verifier/agent.diff")
    assert bundle.verify() == []

    (bundle.root / "steps.txt").write_text("rewritten")
    (bundle.root / "notes.txt").write_text("added")
    assert bundle.verify() == ["steps.txt changed", "notes.txt added"]


def test_bundle_takes_the_harbor_reward(tmp_path: Path) -> None:
    trial = _trial(
        tmp_path, result={"verifier_result": {"rewards": {"reward": 0.0}}, "trial_name": "t1"}
    )
    bundle = build_bundle(trial, tmp_path / "bundle")
    assert bundle.reward == 0.0
    assert bundle.subject["trial_id"] == "t1"


class FakeModels:
    """Answers by stage (schema) and question (prompt text); records calls."""

    def __init__(
        self,
        t1: dict[str, str | None],
        t2: dict[str, str] | None = None,
        t3: dict[str, str] | None = None,
    ):
        self.t1, self.t2, self.t3 = t1, t2 or {}, t3 or {}
        self.calls: list[str] = []

    def __call__(
        self, route: ModelRoute, prompt: str, schema: dict[str, Any], **_: Any
    ) -> CallResult:
        if schema is T1_SCHEMA:
            question = next(
                q
                for q in self.t1
                if f"QUESTION ({q.replace('_', ' ')}" in prompt
                or f"QUESTION ({_TITLE[q]})" in prompt
            )
            self.calls.append(f"T1:{question}")
            answer = self.t1[question]
            if answer is None:
                return CallResult(route.label, None, "", {}, 0.1, error="no JSON object")
            out = {
                "answer": answer,
                "confidence": 0.8,
                "fault": "agent" if answer == "yes" else "none",
                "failure_step": 2 if answer == "yes" else None,
                "evidence_steps": [2],
                "rationale": "r",
            }
        elif schema is T2_SCHEMA:
            self.calls.append("T2")
            out = {
                "claims": [
                    {
                        "question": q,
                        "holds": h,
                        "claim": "c",
                        "fault": "agent",
                        "failure_step": 2,
                        "evidence_steps": [2],
                        "commands": ["grep"],
                        "confidence": 0.7,
                    }
                    for q, h in self.t2.items()
                ],
                "summary": "s",
            }
        else:
            assert schema is T3_SCHEMA
            self.calls.append("T3")
            out = {
                "rulings": [
                    {"question": q, "stands": s, "reason": "why", "counterfactual": "cf"}
                    for q, s in self.t3.items()
                ]
            }
        return CallResult(route.label, out, json.dumps(out), {"input_tokens": 10}, 0.1)


_TITLE = {
    "reward_shortcut": "reward shortcut",
    "false_positive": "false positive",
    "false_negative": "false negative",
    "prompt_grader_alignment": "prompt-grader alignment",
    "failure_cause": "failure cause",
}


def _rewarded_bundle(tmp_path: Path) -> tuple[Bundle, Path]:
    trial = _trial(tmp_path, result={"verifier_result": {"rewards": {"reward": 1.0}}})
    return build_bundle(trial, tmp_path / "out" / "bundle"), trial


def _findings(out: Path) -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text()) for p in (out / "findings").glob("*.json")}


def test_skipped_questions_are_not_run_and_never_pass(tmp_path: Path) -> None:
    bundle, _ = _rewarded_bundle(tmp_path)
    models = FakeModels(
        {"reward_shortcut": "no", "false_positive": "no", "prompt_grader_alignment": "no"}
    )
    verdict = review_bundle(bundle, tmp_path / "out", config=TraceConfig(), call=models)
    findings = _findings(tmp_path / "out")
    assert sorted(models.calls) == [
        "T1:false_positive",
        "T1:prompt_grader_alignment",
        "T1:reward_shortcut",
    ]
    for question in ("false_negative", "failure_cause"):
        assert findings[question]["verdict"] == "unknown"
        assert findings[question]["measurement"] == "not_run"
    assert verdict["reward_real"] is True
    assert verdict["flagged_any"] is False


def test_investigator_overrides_a_cheap_reviewer_flag(tmp_path: Path) -> None:
    bundle, _ = _rewarded_bundle(tmp_path)
    models = FakeModels(
        {"reward_shortcut": "yes", "false_positive": "no", "prompt_grader_alignment": "no"},
        t2={"reward_shortcut": "no"},
    )
    verdict = review_bundle(bundle, tmp_path / "out", config=TraceConfig(), call=models)
    assert "T3" not in models.calls
    assert _findings(tmp_path / "out")["reward_shortcut"]["verdict"] == "pass"
    assert verdict["reward_hacking"] is False


@pytest.mark.parametrize(
    ("stands", "verdict", "status"), [("no", "pass", "refuted"), ("yes", "fail", "unverified")]
)
def test_deterministic_flag_is_cleared_only_by_the_refuter(
    tmp_path: Path, stands: str, verdict: str, status: str
) -> None:
    bundle, trial = _rewarded_bundle(tmp_path)
    models = FakeModels(
        {"reward_shortcut": "no", "false_positive": "no", "prompt_grader_alignment": "no"},
        t2={"reward_shortcut": "no"},
        t3={"reward_shortcut": stands},
    )
    result = review_bundle(
        bundle,
        tmp_path / "out",
        config=TraceConfig(),
        trial_dir=trial,
        # combined_v1's cache stores the fired literals as the flag.
        cached_combined={"flagged": ["rules:hidden_info_read"]},
        call=models,
    )
    finding = _findings(tmp_path / "out")["reward_shortcut"]
    assert models.calls[-1] == "T3"
    assert (finding["verdict"], finding["refutation"]["status"]) == (verdict, status)
    assert result["reward_hacking"] is (verdict == "fail")


def test_unusable_reviewer_output_abstains(tmp_path: Path) -> None:
    bundle, _ = _rewarded_bundle(tmp_path)
    models = FakeModels(
        {"reward_shortcut": None, "false_positive": None, "prompt_grader_alignment": "no"}
    )
    verdict = review_bundle(bundle, tmp_path / "out", config=TraceConfig(), call=models)
    finding = _findings(tmp_path / "out")["reward_shortcut"]
    assert (finding["verdict"], finding["measurement"]) == ("unknown", "ran_without_usable_result")
    assert verdict["reward_real"] is None
    assert verdict["reward_hacking"] is None


def test_review_resumes_from_a_matching_verdict(tmp_path: Path) -> None:
    bundle, _ = _rewarded_bundle(tmp_path)
    answers = {"reward_shortcut": "no", "false_positive": "no", "prompt_grader_alignment": "no"}
    review_bundle(bundle, tmp_path / "out", config=TraceConfig(), call=FakeModels(answers))
    again = FakeModels(answers)
    review_bundle(bundle, tmp_path / "out", config=TraceConfig(), call=again)
    assert again.calls == []


def test_investigator_never_shares_the_trace_producer_family() -> None:
    config = TraceConfig()
    assert t2_route(config, "openai/gpt-5.4").provider == "claude"
    assert t2_route(config, "gpt-5.4").provider == "claude"
    assert t2_route(config, "zai-coding-plan/glm-5.3-flash").provider == "codex"
    assert t2_route(config, None).provider == "codex"


def test_quota_gate_stops_at_the_limit_and_on_missing_readings() -> None:
    QuotaGate(0.6, reader=lambda _: 0.59).check("codex")
    with pytest.raises(QuotaStop):
        QuotaGate(0.6, reader=lambda _: 0.6).check("codex")
    with pytest.raises(QuotaStop):
        QuotaGate(0.6, reader=lambda _: None).check("codex")


def _key_item(
    trial: str, task: str, stratum: str, parent: str | None, step: int | None = None
) -> dict[str, Any]:
    return {
        "subject": {"kind": "trace", "task_id": task, "trial_id": trial},
        "split": "heldout",
        "extra": {
            "stratum": stratum,
            "parent_trial_id": parent,
            "failure_step": step,
            "failure_type": None if stratum == "clean" else "t",
        },
    }


def test_paired_discrimination_credits_only_flags_the_parent_did_not_get() -> None:
    items = [
        _key_item("a-clean", "a", "clean", None),
        _key_item("a-f", "a", "silent", "a-clean", 3),
        _key_item("b-clean", "b", "clean", None),
        _key_item("b-f", "b", "loud", "b-clean", 1),
        _key_item("c-f", "c", "silent", None, 2),  # no parent: recall only
    ]
    always = score_paired(items, {i["subject"]["trial_id"]: True for i in items}, bootstrap=50)
    assert always["flag_rate"]["silent"]["rate"] == 1.0
    assert always["paired"]["all_faults"]["delta"] == 0.0

    perfect = {"a-clean": False, "a-f": True, "b-clean": False, "b-f": True, "c-f": True}
    sc = score_paired(items, perfect, steps={"a-f": 3, "b-f": 0, "c-f": 2}, bootstrap=50)
    assert sc["paired"]["all_faults"] == {"n": 2, "delta": 1.0, "ci95": [1.0, 1.0]}
    assert sc["step_exact"] == {"hit": 2, "n": 3, "rate": 0.667}

    partial = score_paired(items, {**perfect, "b-f": None}, bootstrap=50)
    assert partial["abstained"] == 1
    assert partial["paired"]["all_faults"]["n"] == 1


def test_trajectory_judge_steps_keep_their_upstream_index(tmp_path: Path) -> None:
    row = {
        "trajectory_id": "INS-1-wrong_tool",
        "instance_id": "INS-1",
        "goal": "refund ORD-1",
        "steps": [
            {
                "index": 0,
                "thought": "t",
                "call": {"tool": "get_customer", "args": {}},
                "observation": {"ok": True},
            },
            {
                "index": 1,
                "thought": "t",
                "call": {"tool": "lookup_order", "args": {}},
                "observation": {"ok": True},
            },
        ],
        "final_answer": "done",
        "label": {"faulty": True},
    }
    atif = tj_atif(row)
    assert [s["step_id"] for s in atif["steps"]] == [1, 2, 3, 4]
    assert "wrong_tool" not in json.dumps(atif)
    bundle = tj_bundle(row, tmp_path / "b", key_id="trajectory-judge")
    assert "label" not in bundle.text("trajectory.json")
    assert not any("wrong_tool" in p.read_text() for p in bundle.root.rglob("*") if p.is_file())
    assert tj_source_step(bundle.root, 3) == 1
    assert tj_source_step(bundle.root, 4) == 1  # final answer: emitted at the last step
    assert tj_source_step(bundle.root, 1) is None
