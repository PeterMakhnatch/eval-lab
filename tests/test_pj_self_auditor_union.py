import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "pj_union",
    Path(__file__).resolve().parents[1] / "research/experiments/pj-self-cb2a4d-2-auditor-union/pool_judge.py",
)
union = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(union)


def _mapping():
    return {
        "D1": {
            "task_parity": "even",
            "keys": ["K1", "K2"],
            "pooled": [
                {"id": "C1", "audit": "S", "orig": "C1"},
                {"id": "C2", "audit": "TA", "orig": "C1"},
                {"id": "C3", "audit": "TB", "orig": "C2"},
            ],
        }
    }


def _judged():
    return {
        "D1": {
            "C1": {"matched_key_ids": ["K1"], "verdict": "match"},
            "C2": {"matched_key_ids": ["K1", "K2"], "verdict": "match"},
            "C3": {"matched_key_ids": [], "verdict": "plausible_unkeyed"},
        }
    }


def test_a_key_matched_by_two_audits_counts_once_but_both_findings_count_as_matched():
    row = union.matches(_mapping(), _judged(), ("S", "TA"), None)
    assert row["keys"] == {("D1", "K1"), ("D1", "K2")}
    assert (row["eligible"], row["matched_findings"]) == (2, 2)


def test_subset_limits_which_audits_contribute_findings_and_keys():
    row = union.matches(_mapping(), _judged(), ("TB",), None)
    assert row["keys"] == set() and row["eligible"] == 1 and row["matched_findings"] == 0


def test_parity_filter_excludes_the_other_half():
    assert union.matches(_mapping(), _judged(), ("S",), "odd")["eligible"] == 0
    assert union.matches(_mapping(), _judged(), ("S",), "even")["eligible"] == 1


def test_metrics_keys_per_million_tokens_and_unknown_tokens_stay_null(tmp_path):
    root = tmp_path
    tokens = {"D1": {"S": 1000, "TA": 2000, "TB": None}}
    (root / "pool-map.json").write_text(json.dumps({"mapping": _mapping(), "tokens": tokens}))
    for name in ("sol", "terra"):
        directory = root / "judge-out" / name
        directory.mkdir(parents=True)
        rows = [{"id": k, **v} for k, v in _judged()["D1"].items()]
        (directory / "D1.json").write_text(json.dumps({"judgments": rows}))
    out = union.metrics(root, {"sol": root / "judge-out/sol", "terra": root / "judge-out/terra"})["sets"]
    assert out["S+TA"]["audit_tokens"] == 3000
    assert out["S+TA"]["sol"]["keys_per_1m_tokens"] == round(2 / 3000 * 1e6, 1)
    assert out["TB"]["audit_tokens"] is None and out["TB"]["sol"]["keys_per_1m_tokens"] is None
    assert out["S+TA"]["both_judges_agree_keys"] == 2
