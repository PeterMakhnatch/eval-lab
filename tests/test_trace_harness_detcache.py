"""detectors-v1 adapter: a synthetic sealed corpus and cache in tmp_path; no real corpus is read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import detectors
from evallab.production import trace_harness_detcache as dc
from evallab.production.trace_harness import FindingStore, HarnessOptions, run_detectors
from evallab.production.trace_harness_corpora import CorpusLabels
from evallab.production.trace_harness_score import score_corpus

AGENT = {"step_id": 7, "source": "agent", "message": "ran it", "tool_calls": []}
PROMPT = {"step_id": 1, "source": "user", "message": "fix it"}

# id, source, reward_hacking label, loop label, flagged by `rules`, score of `kev`
ROWS = [
    ("a-1", "alpha", True, None, True, 0.9),
    ("a-2", "alpha", True, None, False, 0.2),
    ("a-3", "alpha", False, None, True, 0.4),
    ("a-4", "alpha", False, None, False, 0.1),
    ("b-1", "beta", True, True, True, 0.8),
    ("b-2", "beta", False, False, False, 0.05),
    ("b-3", "beta", None, False, False, 0.3),  # unlabelled for reward_hacking: never a negative
]


def make_corpus(tmp_path: Path, *, trials: set[str] | None = None) -> tuple[detectors.Corpus, Path]:
    root = tmp_path / "detectors-v1"
    results = tmp_path / "results"
    (root / "cache").mkdir(parents=True)
    (root / "corpus.json").write_text(
        json.dumps({"schema": detectors.CORPUS_SCHEMA, "name": "detectors", "version": "1"})
    )
    manifest = []
    for row_id, source, hack, loop, _, _ in ROWS:
        manifest.append({
            "id": row_id, "source": source, "families": [],
            "labels": {"reward_hacking": hack, "stuck_loop": loop},
            "trial": {"kind": "published", "path": f"t/{row_id}"},
        })
        if trials is None or row_id in trials:
            trial = results / "t" / row_id / "agent"
            trial.mkdir(parents=True)
            (trial / "trajectory.json").write_text(json.dumps({"steps": [PROMPT, AGENT]}))
    (root / "manifest.jsonl").write_text("".join(json.dumps(row) + "\n" for row in manifest))
    corpus = detectors.Corpus.load(root)
    for row_id, _, _, _, flagged, score in ROWS:
        corpus.store("rules", {"id": row_id, "detector": "rules", "flagged": flagged, "score": None,
                               "explanation": f"fired: {flagged}", "cost_usd": 0.0})
        corpus.store("kev_any_cheat", {"id": row_id, "detector": "kev_any_cheat", "flagged": None,
                                       "score": score, "model": "jaredpalmer/kev-4b@v1.0",
                                       "explanation": "max P", "cost_usd": 0.0002})
    corpus.seal(labels=True)
    return corpus, results


def roots(results: Path) -> dc.TrialRoots:
    return dc.TrialRoots(None, None, None, results)


def detector(corpus: detectors.Corpus, name: str) -> dc.DetectorCacheDetector:
    return dc.DetectorCacheDetector(
        HarnessOptions(repo_root=Path("."), reader=name), corpus=corpus
    )


def test_labels_are_exactly_the_rows_detectors_score_scores(tmp_path):
    corpus, _ = make_corpus(tmp_path)
    run_ids, positive, steps = dc.detectors_labels(corpus=corpus)
    assert run_ids == {f"detectors-v1:{r[0]}" for r in ROWS if r[2] is not None}
    assert positive["detectors-v1:a-1"] is True and positive["detectors-v1:b-2"] is False
    assert "detectors-v1:b-3" not in positive and steps == {}
    only_beta, beta_positive, _ = dc.detectors_labels("detectors-v1:beta", corpus=corpus)
    assert only_beta == {"detectors-v1:b-1", "detectors-v1:b-2"} and beta_positive["detectors-v1:b-1"]
    loop_ids, loop_positive, _ = dc.detectors_labels(check="stuck_loop", corpus=corpus)
    assert {r for r, v in loop_positive.items() if v} == {"detectors-v1:b-1"}
    assert len(loop_ids) == 3
    with pytest.raises(ValueError, match="unknown source"):
        dc.detectors_labels("detectors-v1:gamma", corpus=corpus)
    with pytest.raises(ValueError, match="unknown detector corpus selector"):
        dc.detectors_labels("other-v1", corpus=corpus)


def test_runs_keep_original_step_ids_and_skip_missing_trials(tmp_path):
    corpus, results = make_corpus(tmp_path, trials={"a-1", "a-2", "b-1", "b-3"})
    runs = list(dc.detectors_runs(corpus=corpus, roots=roots(results)))
    assert [run.run_id for run in runs] == [
        "detectors-v1:a-1", "detectors-v1:a-2", "detectors-v1:b-1", "detectors-v1:b-3",
    ]
    assert runs[0].step_ids == [1, 7] and runs[0].actor_inputs_available
    beta = list(dc.detectors_runs(selector="detectors-v1:beta", corpus=corpus, roots=roots(results)))
    assert [run.run_id for run in beta] == ["detectors-v1:b-1", "detectors-v1:b-3"]
    assert len(list(dc.detectors_runs(1, corpus=corpus, roots=roots(results)))) == 1
    assert list(dc.detectors_runs(0, corpus=corpus, roots=roots(results))) == []


def test_a_changed_cache_is_refused(tmp_path):
    corpus, _ = make_corpus(tmp_path)
    corpus.store("rules", {"id": "a-1", "detector": "rules", "flagged": False, "score": None,
                           "explanation": "edited", "cost_usd": 0.0})
    with pytest.raises(ValueError, match="checksum mismatch"):
        dc.load_corpus(corpus.root)


def test_verdict_mapping_identity_and_native_score(tmp_path):
    corpus, results = make_corpus(tmp_path)
    runs = list(dc.detectors_runs(corpus=corpus, roots=roots(results)))
    rules = {f["run_id"]: f for f in detector(corpus, "rules").detect(runs)}
    assert rules["detectors-v1:a-1"]["verdict"] == "alarm" and rules["detectors-v1:a-2"]["verdict"] == "clear"
    kev_detector = detector(corpus, "kev_any_cheat")
    assert kev_detector.name == "detcache.kev_any_cheat"
    assert kev_detector.reader_model == "jaredpalmer/kev-4b@v1.0"
    assert kev_detector.version == "jaredpalmer/kev-4b@v1.0"
    kev = {f["run_id"]: f for f in kev_detector.detect(runs)}
    assert kev["detectors-v1:a-1"]["verdict"] == "abstain" and kev["detectors-v1:a-1"]["score"] == 0.9
    assert kev["detectors-v1:a-1"]["cost_usd"] == 0.0002
    assert detector(corpus, "rules").version == "detectors-v1" and detector(corpus, "rules").reader_model is None


def test_list_flags_unrecorded_models_and_other_corpus_runs(tmp_path):
    corpus, results = make_corpus(tmp_path)
    corpus.store("combo", {"id": "a-1", "detector": "combo", "flagged": ["x"], "score": None,
                           "explanation": "oof", "cost_usd": 0, "model": "plain-id"})
    corpus.store("combo", {"id": "a-2", "detector": "combo", "flagged": [], "score": None,
                           "explanation": "oof", "cost_usd": 0, "model": "plain-id",
                           "evidence_steps": [7, 99], "detector_version": "v/2", "reused": True,
                           "tokens": {"input": 5, "output": 6}})
    combo = detector(corpus, "combo")
    assert combo.reader_model is None  # "plain-id" is not provider/id shaped
    assert combo.version == "plain-id+v/2"
    runs = list(dc.detectors_runs(corpus=corpus, roots=roots(results)))[:3]
    other = type(runs[0])("elsewhere:a-1", "x", "DEV", "unknown", runs[0].steps, True)
    found = {f["run_id"]: f for f in combo.detect([*runs, other])}
    assert set(found) == {"detectors-v1:a-1", "detectors-v1:a-2"}
    assert found["detectors-v1:a-1"]["verdict"] == "alarm" and found["detectors-v1:a-2"]["verdict"] == "clear"
    assert found["detectors-v1:a-2"]["evidence_steps"] == [7]  # ids absent from the run are dropped
    assert (found["detectors-v1:a-2"]["input_tokens"], found["detectors-v1:a-2"]["cost_usd"]) == (0, 0.0)


def test_long_versions_are_fitted_and_unknown_detectors_refused(tmp_path):
    corpus, _ = make_corpus(tmp_path)
    corpus.store("wordy", {"id": "a-1", "detector": "wordy", "flagged": True, "score": None,
                           "explanation": "", "cost_usd": 0, "model": "p/" + "m" * 100})
    wordy = detector(corpus, "wordy")
    assert len(wordy.version) <= 64 and wordy.reader_model is None
    with pytest.raises(ValueError, match="no cache for detector"):
        detector(corpus, "missing")
    with pytest.raises(ValueError, match="needs the cached detector name"):
        dc.DetectorCacheDetector(HarnessOptions(repo_root=Path(".")), corpus=corpus)


def test_harness_score_reproduces_detectors_score_counts(tmp_path):
    corpus, results = make_corpus(tmp_path)
    runs = list(dc.detectors_runs(corpus=corpus, roots=roots(results)))
    store = FindingStore(tmp_path / "findings.jsonl")
    run_detectors(runs, [detector(corpus, "rules"), detector(corpus, "kev_any_cheat")], store)
    run_ids, positive, steps = dc.detectors_labels(corpus=corpus)
    labels = CorpusLabels("detectors-v1", run_ids, positive, steps, "step", {})
    result = score_corpus(labels, store.records(), detectors=["detcache.rules"])
    native = result["detectors"][0]["native"]
    own = detectors.score(corpus, "rules", check="reward_hacking").operating_points[0]["cells"]
    assert (native["tp"], native["positives"]) == (
        own["alpha:pos"]["hit"] + own["beta:pos"]["hit"], own["alpha:pos"]["n"] + own["beta:pos"]["n"],
    )
    assert (native["fp"], native["negatives"]) == (
        own["alpha:neg"]["hit"] + own["beta:neg"]["hit"], own["alpha:neg"]["n"] + own["beta:neg"]["n"],
    )
    kev = score_corpus(labels, store.records(), detectors=["detcache.kev_any_cheat"])
    assert kev["detectors"][0]["coverage"]["excluded"]["abstain"] == 6  # score-only: no native verdict

