import importlib.util
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1] / "research/experiments/tq-blind"


def _module(name: str):
    spec = importlib.util.spec_from_file_location(f"tq_blind_test_{name}", BASE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


redact = _module("redact")
run = _module("run")


def test_redaction_replaces_name_url_and_holder_and_nothing_else():
    text = (
        '1: # Copyright 2026 Zapier, Inc.\n2: "repository": "https://github.com/zapier/AutomationBench",\n'
        "3: from automationbench.rubric import x\n4: if os.environ.get('AUTOMATIONBENCH_DEBUG_ASSERTIONS'):\n"
        '5: FILE tests/original-grader/automationbench/schema/a.py\n6: "zapier_tools": []\n'
    )
    out, counts = redact.redact(text)
    assert out == (
        '1: # Copyright 2026 Example Org, Inc.\n2: "repository": "https://example.invalid/org/TaskPkg",\n'
        "3: from taskpkg.rubric import x\n4: if os.environ.get('TASKPKG_DEBUG_ASSERTIONS'):\n"
        '5: FILE tests/original-grader/taskpkg/schema/a.py\n6: "zapier_tools": []\n'
    )
    assert counts == {"holder": 1, "url": 1, "name": 3}
    proof = redact.verify(text, out)
    assert proof["segments_identical"] and proof["placeholder_counts_equal"]
    assert proof["residual_tokens"] == 0


def test_url_is_one_token_not_a_url_plus_a_name():
    _, counts = redact.redact("see https://github.com/zapier/AutomationBench now")
    assert counts == {"url": 1}


def test_unknown_casing_and_pre_existing_placeholders_are_rejected():
    with pytest.raises(ValueError, match="casing"):
        redact.redact("Automationbench")
    with pytest.raises(ValueError, match="ambiguous"):
        redact.redact("already TaskPkg here")


def test_verify_detects_any_extra_change():
    text = "x automationbench y"
    assert redact.verify(text, "x taskpkg Z")["segments_identical"] is False
    assert redact.verify(text, "x taskpkg y")["segments_identical"] is True


def test_prompt_without_tokens_is_untouched():
    out, counts = redact.redact("a PAIChecker task about swe-gym")
    assert out == "a PAIChecker task about swe-gym" and not counts


def test_transition_matrix_counts_flips_and_missing():
    t = run.transition_matrix(
        [("real_defect", "real_defect"), ("real_defect", "uncertain"), (None, "not_a_defect")]
    )
    assert (t["n"], t["unchanged"], t["flips"]) == (3, 1, 2)
    assert t["cells"]["real_defect->uncertain"] == 1 and t["cells"]["missing->not_a_defect"] == 1


def _gate(k, n, low):
    return {"k": k, "n": n, "wilson95": [low, 0.97]}


RECALL_OK = {"k": 12, "n": 17}


@pytest.mark.parametrize(
    ("sonnet_k", "terra_k", "expected"),
    [
        (20, 22, "stands"),
        (16, 18, "stands"),  # 72.7% / 69.2% sit above the lower bounds 72.19% / 66.47%
        (15, 22, "weakened"),  # 68.2% < 72.19% but >= 60%
        (20, 17, "weakened"),  # terra 65.4% < 66.47% but >= 60%
        (20, 15, "changed"),  # terra 57.7% < 60%
        (13, 22, "changed"),  # sonnet 59.1% < 60%
    ],
)
def test_decision_rule_boundaries(sonnet_k, terra_k, expected):
    original = {"sonnet": _gate(20, 22, 0.7219), "terra": _gate(22, 26, 0.6647)}
    arm = {"sonnet": _gate(sonnet_k, 22, 0), "terra": _gate(terra_k, 26, 0)}
    result = run.decision(original, arm, RECALL_OK)
    assert result["alarm_conclusion"] == expected


def test_validity_gate_is_reported_separately_from_alarm_conclusion():
    original = {"sonnet": _gate(20, 22, 0.7219)}
    arm = {"sonnet": _gate(20, 22, 0)}
    result = run.decision(original, arm, {"k": 11, "n": 17})
    assert result["alarm_conclusion"] == "stands"
    assert result["calibration_recall"]["validity_gate_passed"] is False
