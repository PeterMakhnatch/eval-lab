"""Reader-comparison regressions with synthetic records and injected label joins."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "compare_readers", Path(__file__).resolve().parents[1] / "scripts" / "compare_readers.py",
)
assert _SPEC is not None and _SPEC.loader is not None
cr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cr)


def _facet(run_id: str, suspicion: int, awareness: str = "none", steps=(), reader="reader-a") -> dict:
    return {
        "run_id": run_id, "facet_model": reader, "input_tokens": 1000, "output_tokens": 100,
        "suspicion": suspicion, "eval_awareness": awareness, "evidence_steps": list(steps),
    }


def _score(facets, labels, second=None, ids=None, reader="reader-a", references=None):
    return cr.score_arm(
        list(labels) if ids is None else ids,
        {f["run_id"]: f for f in facets},
        {f["run_id"]: f for f in (facets if second is None else second)},
        labels, reader=reader, price_input=1.0, price_output=5.0, reference_steps=references,
    )


def test_wilson_interval_brackets_point_estimate():
    low, high = cr.wilson_interval(3, 100)
    assert low < 0.03 < high


def test_empty_denominator_is_not_measured_not_pass():
    result = _score([], {})
    assert result["criteria"]["a_far_suspicion"]["status"] == "not measured"
    assert result["criteria"]["a_far_suspicion"]["passed"] is False
    assert result["metrics"]["far_suspicion"]["rate"] is None


def test_far_criterion_passes_when_clean_false_alarms_are_low():
    ids = [f"c{i}" for i in range(100)]
    result = _score([_facet(run_id, 0) for run_id in ids], dict.fromkeys(ids, False))
    assert result["criteria"]["a_far_suspicion"]["status"] == "pass"


def test_far_criterion_fails_when_clean_runs_are_flagged():
    result = _score([_facet("c1", 3), _facet("c2", 0)], {"c1": False, "c2": False})
    assert result["criteria"]["a_far_suspicion"]["status"] == "fail"
    assert result["metrics"]["far_suspicion"]["successes"] == 1


def test_scorecard_boolean_labels_and_considered_only_stay_distinct():
    result = _score(
        [_facet("clean", 0), _facet("positive", 3, "plans_to_game"), _facet("considered", 3)],
        {"clean": False, "positive": True, "considered": None},
    )
    assert result["clean_runs"] == result["positive_runs"] == 1
    assert result["considered_only_runs"] == 1
    assert result["metrics"]["recall_suspicion"]["rate"] == 1
    assert result["metrics"]["recall_grader_awareness"]["rate"] == 1
    assert result["metrics"]["far_suspicion"]["rate"] == 0


def test_kappa_perfect_agreement_and_genuinely_empty_second_pass():
    labels = {"p1": True, "c1": False}
    facets = [_facet("p1", 3), _facet("c1", 0)]
    result = _score(facets, labels)
    assert result["criteria"]["c_kappa_vs_second_pass"]["status"] == "pass"
    empty = _score(facets, labels, second=[])
    assert empty["second_pass_pairs"] == 0
    assert empty["metrics"]["kappa_suspicion"]["value"] is None
    assert empty["criteria"]["c_kappa_vs_second_pass"]["status"] == "not measured"
    assert empty["criteria"]["c_kappa_vs_second_pass"]["passed"] is False


@pytest.mark.parametrize("references", [{"p1": ["10", 10], "p2": {10}}, {"p1": {10}, "p2": ["10"]}])
def test_citations_normalise_both_sides_to_integer_sets(references):
    result = _score(
        [_facet("p1", 3, steps=["10", 10]), _facet("p2", 3, steps=["14"])],
        {"p1": True, "p2": True}, references=references,
    )
    exact = result["metrics"]["goodfire_exact"]
    within5 = result["metrics"]["goodfire_within5"]
    assert exact["successes"] == 1 and exact["denominator"] == 2
    assert within5["successes"] == 2
    assert result["criteria"]["d_goodfire_cited_step"]["status"] == "pass"


def test_missing_facets_and_unlabelled_are_counted_not_scored():
    result = _score(
        [_facet("c1", 0), _facet("unjudged", 3)], {"c1": False},
        ids=["c1", "gone", "unjudged"],
    )
    assert result["missing_facets"] == result["unlabelled"] == result["scored"] == 1


def test_other_reader_is_counted_not_scored_or_priced():
    result = _score(
        [_facet("own", 0), _facet("other", 3, reader="reader-b")],
        {"own": False, "other": False},
    )
    assert result["reader_mix"] == {"reader-a": 1}
    assert result["not_this_arm"] == 1
    assert result["scored"] == 1
    assert result["metrics"]["far_suspicion"]["rate"] == 0
    assert result["list_cost_usd"] == pytest.approx(0.0015)


def test_second_pass_from_different_reader_is_not_same_reader_retest():
    result = _score(
        [_facet("c1", 0)], {"c1": False}, second=[_facet("c1", 0, reader="reader-b")],
    )
    assert result["second_pass_pairs"] == 0
    assert result["criteria"]["c_kappa_vs_second_pass"]["status"] == "not measured"


def test_decide_prefers_more_passes_then_cheaper_then_tie():
    def arm(reader, passes, cost):
        return {
            "schema": cr.RESULT_SCHEMA, "reader": reader, "sample_size": 10,
            "passes": passes, "list_cost_usd": cost,
            "metrics": {"kappa_suspicion": {"value": 0.5, "pairs": 10}},
            "criteria": {"c_kappa_vs_second_pass": {"status": "pass"}},
        }
    assert cr.decide([arm("a", 3, 5.0), arm("b", 2, 1.0)], None)["winner"] == "a"
    assert cr.decide([arm("a", 2, 5.0), arm("b", 2, 1.0)], None)["winner"] == "b"
    tie = cr.decide([arm("a", 2, 5.0), arm("b", 2, 5.0)], None)
    assert tie["winner"] is None
    assert tie["tie_break"] == "exact tie on passes and list cost"


def test_decide_rejects_mismatched_samples():
    base = {"schema": cr.RESULT_SCHEMA, "reader": "a", "sample_size": 10, "passes": 1,
            "list_cost_usd": 1.0, "metrics": {}, "criteria": {}}
    with pytest.raises(SystemExit):
        cr.decide([base, dict(base, reader="b", sample_size=11)], None)


def _manifest(path: Path, ids: list[str], *, with_files: bool = True) -> None:
    cloud_bytes = ("\n".join(sorted(ids)) + "\n").encode()
    cheat_bytes = b""
    path.write_text(json.dumps({
        "schema": cr.SAMPLE_SCHEMA, "seed": 20261029,
        "strata": {"synthetic": ids}, "sizes": {"synthetic": len(ids)},
        "cloud_ids_sha256": hashlib.sha256(cloud_bytes).hexdigest(),
        "cheatbench_ids_sha256": hashlib.sha256(cheat_bytes).hexdigest(),
    }))
    if with_files:
        (path.parent / "cloud-ids.txt").write_bytes(cloud_bytes)
        (path.parent / "cheatbench-ids.txt").write_bytes(cheat_bytes)


def test_sample_digest_is_verified(tmp_path: Path):
    manifest = tmp_path / "sample.json"
    _manifest(manifest, ["r1", "r2"])
    assert cr.load_sample(manifest) == {"r1", "r2"}
    (tmp_path / "cloud-ids.txt").write_text("tampered\n")
    with pytest.raises(SystemExit, match="digest mismatch: cloud-ids.txt"):
        cr.load_sample(manifest)


def test_sample_without_id_files_warns_once(tmp_path: Path, capsys):
    manifest = tmp_path / "sample.json"
    _manifest(manifest, ["r1", "r2"], with_files=False)
    assert cr.load_sample(manifest) == {"r1", "r2"}
    warning = capsys.readouterr().err
    assert warning.count("warning:") == 1
    assert "skipping digest checks" in warning
    assert "cloud-ids.txt" in warning and "cheatbench-ids.txt" in warning


def test_cli_joins_scorecard_labels_without_reading_answer_key_json(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "sample.json"
    _manifest(manifest, ["private-run"])
    store = tmp_path / "first"
    second = tmp_path / "second"
    store.mkdir()
    second.mkdir()
    (store / cr.FACETS_FILE).write_text(json.dumps(_facet("private-run", 0)) + "\n")
    calls = []

    def fake_labels(**kwargs):
        calls.append(kwargs)
        return {"private-run": False}

    monkeypatch.setattr(cr, "load_labels", fake_labels)
    monkeypatch.setattr(cr, "load_reference_steps", lambda *args: {})
    output = tmp_path / "result.json"
    database = tmp_path / "unused.sqlite"
    assert cr.main([
        "run", "--sample", str(manifest), "--store", str(store), "--second-pass-store", str(second),
        "--arm", "reader-a", "--database", str(database), "--repo-root", str(tmp_path),
        "--price-input", "1", "--price-output", "5", "--out", str(output),
    ]) == 0
    assert calls == [{"database": database, "repo_root": tmp_path, "run_ids": {"private-run"}}]
    result = json.loads(output.read_text())
    assert result["clean_runs"] == 1
    assert result["metrics"]["kappa_suspicion"]["value"] is None
    assert "private-run" not in output.read_text() + capsys.readouterr().out
    assert not database.exists()
