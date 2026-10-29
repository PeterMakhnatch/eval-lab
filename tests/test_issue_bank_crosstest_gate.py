from copy import deepcopy

from evallab.failure_library.bank.crosstest_gate import decision

TRACE_BASE = {"complete": True, "recall": 0.4, "citation_invalid": 0}
TRACE_CANDIDATE = {"complete": True, "recall": 0.5, "citation_invalid": 0}
TASK_BASE = {"complete": True, "recall": 0.4, "key_backed_precision": 0.7}
TASK_CANDIDATE = {"complete": True, "recall": 0.5, "key_backed_precision": 0.7}


def receipt():
    return {
        "set_id": "sample",
        "status": "COMPLETE",
        "expected_n": 10,
        "fa_basis": {
            "fa_rate": "yes",
            "established_negatives": 5,
            "construct": "cheating-or-exploit",
        },
        "relevant_cheat_exploit": True,
        "arms": {
            "baseline": {"n": 10, "recall": 0.4, "far": 0.02, "citation_invalid": 1},
            "candidate": {"n": 10, "recall": 0.5, "far": 0.03, "citation_invalid": 1},
        },
    }


def trace(cross):
    return decision(TRACE_BASE, TRACE_CANDIDATE, prompt="trace", changed=True, cross_test=cross)[
        "decision"
    ]


def document(row):
    return {"complete": True, "missing": [], "sets": [row]}


def test_complete_trace_promotes_and_far_regression_rejects():
    row = receipt()
    assert trace(document(row)) == "PROMOTED"
    row["arms"]["candidate"]["far"] = 0.04
    assert trace(document(row)) == "NOT_PROMOTED"


def test_missing_ineligible_and_undefined_are_unmeasured():
    row = receipt()
    assert trace(None) == "UNMEASURED"
    assert trace({"complete": False, "missing": ["other"], "sets": [row]}) == "UNMEASURED"
    row["status"] = "INELIGIBLE"
    assert trace(document(row)) == "UNMEASURED"
    row = receipt()
    row["arms"]["candidate"]["recall"] = None
    assert trace(document(row)) == "UNMEASURED"


def test_known_regression_takes_precedence_over_missing_and_citations():
    row = receipt()
    row["arms"]["candidate"]["citation_invalid"] = 2
    assert trace({"complete": False, "missing": ["other"], "sets": [row]}) == "NOT_PROMOTED"


def test_all_positive_and_non_cheat_construct_do_not_invent_gates():
    row = receipt()
    row["fa_basis"] = {
        "fa_rate": "no",
        "established_negatives": 0,
        "construct": "cheating-or-exploit",
    }
    row["arms"]["candidate"]["far"] = None
    assert trace(document(row)) == "UNMEASURED"
    row = receipt()
    row["fa_basis"]["construct"] = "wrong-pass-outcome"
    row["relevant_cheat_exploit"] = False
    row["arms"]["baseline"]["recall"] = 0.05
    row["arms"]["candidate"]["recall"] = 0.1
    assert trace(document(row)) == "PROMOTED"
    row["fa_basis"]["construct"] = "cheating-or-exploit"
    row["relevant_cheat_exploit"] = True
    assert trace(document(row)) == "NOT_PROMOTED"


def test_taskqa_requires_independent_adjudication():
    row = receipt()

    def task(cross):
        return decision(TASK_BASE, TASK_CANDIDATE, prompt="taskqa", changed=True, cross_test=cross)[
            "decision"
        ]

    assert task(document(row)) == "UNMEASURED"
    row["adjudication"] = {
        "complete": True,
        "baseline": {"cited_defect_precision": 0.8, "unfair_fail": 0.1},
        "candidate": {"cited_defect_precision": 0.8, "unfair_fail": 0.1},
    }
    assert task(document(row)) == "PROMOTED"
    changed = deepcopy(row)
    changed["adjudication"]["candidate"]["unfair_fail"] = 0.2
    assert task(document(changed)) == "NOT_PROMOTED"
