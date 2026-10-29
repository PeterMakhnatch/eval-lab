import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "research/experiments/id-oq1-miss-analysis"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"idoq1_{name}", BASE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


enrich = _load("enrich_keys")
metrics = _load("rejudge_metrics")


def _finding(fid, confidence, verified=True):
    return {"id": fid, "confidence": confidence, "citation_verified": verified}


def _bundle(findings, keys=2, parity="even"):
    return {"task_parity": parity, "keys": [{}] * keys, "findings": findings}


def test_enriched_detail_uses_item_evidence_and_instruction_lines_for_a_line_key():
    key = "mimo-weak-tests-v2.1:format-code-task-1:line-abc123"
    text = enrich.detail(
        key,
        items={"format-code-task-1": ["parser drops the second hunk"]},
        refs={key: ["8-9", "15"]},
    )
    assert "one line of the reference change" in text
    assert "instruction.md:8-9, instruction.md:15" in text
    assert "parser drops the second hunk" in text


def test_enriched_detail_is_none_for_unknown_task_or_foreign_key():
    assert enrich.detail("mimo-weak-tests-v2.1:other:block", {}, {}) is None
    assert enrich.detail("pm-tb4:html-js-filter:3f0a3d0cb4", {"html-js-filter": ["x"]}, {}) is None


def test_floor_counts_only_confident_verified_findings_and_distinct_keys():
    bundles = {"D1": _bundle([_finding("C1", 0.6), _finding("C2", 0.4), _finding("C3", 0.9, verified=False)])}
    judged = {
        "D1": {
            "C1": {"matched_key_ids": ["K1"], "verdict": "match"},
            "C2": {"matched_key_ids": ["K2"], "verdict": "match"},
            "C3": {"matched_key_ids": ["K2"], "verdict": "match"},
        }
    }
    at_half = metrics.evaluate(bundles, judged, 0.5, None)
    assert (at_half["matched_keys"], at_half["eligible_findings"]) == (1, 1)
    at_forty = metrics.evaluate(bundles, judged, 0.4, None)
    assert (at_forty["matched_keys"], at_forty["eligible_findings"]) == (2, 2)


def test_one_finding_naming_two_keys_counts_two_keys_but_one_matched_finding():
    bundles = {"D1": _bundle([_finding("C1", 0.7)])}
    judged = {"D1": {"C1": {"matched_key_ids": ["K1", "K2"], "verdict": "match"}}}
    row = metrics.evaluate(bundles, judged, 0.5, None)
    assert (row["matched_keys"], row["matched_findings"], row["tasks_with_key_hit"]) == (2, 1, 1)


def test_parity_filter_selects_only_that_half():
    bundles = {
        "D1": _bundle([_finding("C1", 0.7)], parity="even"),
        "D2": _bundle([_finding("C1", 0.7)], parity="odd"),
    }
    judged = {
        "D1": {"C1": {"matched_key_ids": ["K1"], "verdict": "match"}},
        "D2": {"C1": {"matched_key_ids": [], "verdict": "plausible_unkeyed"}},
    }
    assert metrics.evaluate(bundles, judged, 0.5, "even")["matched_keys"] == 1
    odd = metrics.evaluate(bundles, judged, 0.5, "odd")
    assert (odd["matched_keys"], odd["plausible_unkeyed"]) == (0, 1)
