import importlib.util
import json
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1] / "research/experiments/tq-unverified-alarms"


def _module(name: str):
    spec = importlib.util.spec_from_file_location(f"tq_unverified_test_{name}", BASE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = _module("core")
prompts = _module("prompts")
run = _module("run")


def _cell(gate_flag=True, members=("C1",), count=2):
    payloads = [
        {
            "candidate_id": f"model#{n}",
            "rank": 1,
            "direction": "wrong_pass",
            "mechanism": f"Mechanism {n}",
            "citations": [{"path": "tests/a.py", "line": n, "end_line": n, "quote": f"q{n}"}],
        }
        for n in range(1, count + 1)
    ]
    return {
        "status": "ok",
        "flags": {core.GATE_KEY: gate_flag},
        "prediction": {"memberships": {core.GATE_KEY: list(members)}, "parsed_payloads": payloads},
    }


def test_eligible_findings_follow_membership_not_rank_field():
    found = core.eligible_findings(_cell(members=("C2",)), "sonnet")
    assert [f["mechanism"] for f in found] == ["Mechanism 2"]
    assert core.eligible_findings(_cell(gate_flag=False), "sonnet") == []
    with pytest.raises(ValueError):
        core.eligible_findings(_cell(members=("C9",)), "sonnet")


def test_dedupe_merges_identical_findings_across_gates_and_keeps_attribution():
    a = {
        "gate": "sonnet",
        "rank": 1,
        "direction": "wrong_pass",
        "mechanism": "Same  Thing",
        "citations": [{"path": "p", "line": 1, "end_line": 2, "quote": "x"}],
    }
    b = {**a, "gate": "terra", "mechanism": "same thing"}
    c = {
        **a,
        "gate": "terra",
        "rank": 2,
        "mechanism": "different words",
        "citations": [{"path": "q", "line": 9, "end_line": 9, "quote": "y"}],
    }
    merged = core.dedupe([a, b, c])
    assert len(merged) == 2
    assert {s["gate"] for s in merged[0]["sources"]} == {"sonnet", "terra"}
    labelled = core.shuffle_and_label("C0001", merged)
    assert sorted(f["finding_id"] for f in labelled) == ["F1", "F2"]
    assert labelled == core.shuffle_and_label("C0001", list(reversed(merged)))


def test_downgrade_rule_needs_verifiable_evidence_for_real_defect_only():
    files = core.parse_source(
        "FILE tests/a.py\n1: assert x == 1\n2: pass\nFILE instruction.md\n1: Return x\n"
    )
    good = {
        "verdict": "real_defect",
        "evidence": [{"path": "tests/a.py", "line": 1, "quote": "assert x == 1"}],
    }
    bad = {
        "verdict": "real_defect",
        "evidence": [{"path": "tests/a.py", "line": 9, "quote": "assert x == 1"}],
    }
    invented = {
        "verdict": "real_defect",
        "evidence": [{"path": "tests/a.py", "line": 1, "quote": "assert y"}],
    }
    absent = {"verdict": "not_a_defect", "evidence": []}
    assert core.effective_verdict(good, files) == ("real_defect", False)
    assert core.effective_verdict(bad, files) == ("uncertain", True)
    assert core.effective_verdict(invented, files) == ("uncertain", True)
    assert core.effective_verdict(absent, files) == ("not_a_defect", False)


def test_final_and_alarm_labels_treat_disagreement_and_missing_as_uncertain():
    assert core.final_label("real_defect", "real_defect") == "real_defect"
    assert core.final_label("real_defect", "not_a_defect") == "uncertain"
    assert core.final_label("real_defect", None) == "uncertain"
    assert core.alarm_label(["not_a_defect", "real_defect"]) == "real"
    assert core.alarm_label(["not_a_defect", "not_a_defect"]) == "not_a_defect"
    assert core.alarm_label(["not_a_defect", "uncertain"]) == "uncertain"


def test_headline_reading_thresholds_and_validity_gate():
    assert core.headline_reading(1, 10, True).startswith("alarms mostly not real")
    assert core.headline_reading(6, 10, True).startswith("alarms mostly real")
    assert core.headline_reading(4, 10, True) == "mixed"
    assert core.headline_reading(9, 10, False) == "not validated"


def test_wilson_kappa_and_corrected_balanced_accuracy():
    low, high = core.wilson(22, 30)
    assert 0.55 < low < 0.57 and 0.85 < high < 0.87
    assert core.wilson(0, 0) is None
    assert core.kappa([("a", "a"), ("b", "b"), ("a", "b"), ("b", "a")]) == pytest.approx(0.0)
    assert core.kappa([("a", "a"), ("b", "b")]) == pytest.approx(1.0)
    bounds = core.corrected_balanced_accuracy(
        defective=30,
        defective_flagged=22,
        unchanged=30,
        unchanged_flagged=22,
        real_alarms=10,
        uncertain_alarms=4,
    )
    assert bounds["lower"] == pytest.approx(0.5 * (32 / 40 + 8 / 20), abs=1e-4)
    assert bounds["upper"] == pytest.approx(0.5 * (36 / 44 + 8 / 16), abs=1e-4)


def test_parse_verdicts_is_strict():
    ids = ["F1", "F2"]
    ok = {
        "verdicts": [
            {"finding_id": "F1", "verdict": "uncertain", "rationale": "r", "evidence": []},
            {"finding_id": "F2", "verdict": "not_a_defect", "rationale": "r", "evidence": []},
        ]
    }
    assert set(prompts.parse_verdicts("```json\n" + json.dumps(ok) + "\n```", ids)) == set(ids)
    for broken in (
        {"verdicts": ok["verdicts"][:1]},
        {"verdicts": [ok["verdicts"][0], ok["verdicts"][0]]},
        {"verdicts": [{**ok["verdicts"][0], "verdict": "maybe"}, ok["verdicts"][1]]},
    ):
        with pytest.raises(ValueError):
            prompts.parse_verdicts(json.dumps(broken), ids)
    with pytest.raises(ValueError):
        prompts.parse_verdicts("I cannot help with that.", ids)


def test_adjudicator_prompt_hides_gate_and_confidence_fields():
    finding = {
        "finding_id": "F1",
        "direction": "unfair_fail",
        "mechanism": "m",
        "citations": [{"path": "p", "line": 1, "end_line": 2, "quote": "q"}],
        "sources": [{"gate": "sonnet", "rank": 1}],
        "confidence": 0.9,
    }
    text = prompts.render_adjudicator_prompt("FILE p\n1: q\n", [finding])
    assert "REPORT F1" in text and "sonnet" not in text.lower() and "0.9" not in text


def _write_session(
    directory: Path, provider: str, model: str, fallback=False, tool=False, text='{"x": 1}'
):
    session = directory / "sessions"
    session.mkdir(parents=True)
    content = [{"type": "text", "text": text}]
    if tool:
        content.append({"type": "toolCall"})
    rows = [
        {
            "type": "model_change",
            "model": f"{provider}/{model}",
            "resolvedModelIsFallback": fallback,
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "provider": provider,
                "model": model,
                "content": content,
                "stopReason": "stop",
                "usage": {"input": 10, "output": 2},
            },
        },
    ]
    (session / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


def test_session_receipt_attests_served_route_and_rejects_fallback_or_tools(tmp_path):
    route = "openai-codex/gpt-6.1-sol"
    _write_session(tmp_path / "ok", "openai-codex", "gpt-6.1-sol")
    assert run.session_receipt(tmp_path / "ok", route, 0)["status"] == "answered"
    _write_session(tmp_path / "other", "openai-codex", "gpt-6.1-terra")
    assert run.session_receipt(tmp_path / "other", route, 0)["status"] == "invalid"
    _write_session(tmp_path / "fb", "openai-codex", "gpt-6.1-sol", fallback=True)
    assert run.session_receipt(tmp_path / "fb", route, 0)["status"] == "invalid"
    _write_session(tmp_path / "tool", "openai-codex", "gpt-6.1-sol", tool=True)
    assert run.session_receipt(tmp_path / "tool", route, 0)["status"] == "invalid"
    _write_session(tmp_path / "empty", "openai-codex", "gpt-6.1-sol", text="")
    receipt = run.session_receipt(tmp_path / "empty", route, 0)
    assert receipt["status"] == "unanswered" and receipt["refusal"]


def test_quota_admission_stops_at_85_percent_and_on_stop_file(tmp_path, monkeypatch):
    def report(fraction):
        return {
            "reports": [
                {
                    "provider": "anthropic",
                    "limits": [
                        {"window": {"id": "5h"}, "amount": {"usedFraction": 0.99}},
                        {"window": {"id": "7d"}, "amount": {"usedFraction": fraction}},
                    ],
                }
            ]
        }

    assert run.Quota.weekly(report(0.79), "anthropic") == pytest.approx(0.79)
    with pytest.raises(RuntimeError):
        run.Quota.weekly(report(0.1), "openai-codex")

    class Done:
        def __init__(self, fraction):
            self.stdout = json.dumps(report(fraction))

    monkeypatch.setattr(run.subprocess, "run", lambda *a, **k: Done(0.85))
    with pytest.raises(run.Stopped):
        run.Quota("anthropic", tmp_path / "STOP").admit()
    monkeypatch.setattr(run.subprocess, "run", lambda *a, **k: Done(0.84))
    assert run.Quota("anthropic", tmp_path / "STOP").admit() == pytest.approx(0.84)
    (tmp_path / "STOP").write_text("")
    with pytest.raises(run.Stopped):
        run.Quota("anthropic", tmp_path / "STOP").admit()
