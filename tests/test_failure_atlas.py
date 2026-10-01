"""Consumer-boundary tests for the HAR-131 failure atlas (provisional).

These pin the atlas contract, not current corpus numbers: eligibility,
counts_verdict as sole counted authority, raw-verified exemplars, frozen-only
labels, and the fail-closed GEPA gate. Numbers in atlas.json are current-view
measures and will change on refresh; tests use synthetic rows or throwaway
tmp corpora only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "research/explorations/trace-lab/failure-atlas"))

import build as atlas_build


def _trial(**overrides):
    base = {
        "job_id": "job-1", "trial_id": "trial-1", "job_name": "job-1",
        "trial_name": "t1", "task_name": "mimo-v2.6-rl/format-code-task-000383",
        "card": "HAR-116", "model_name": "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
        "arm": None, "split": None, "task_package_digest": "sha256:abc",
        "raw_reward": 0.0, "scored": True, "counts_verdict": "counted_fail",
        "counts_reasons_json": "[]", "counts_evidence_json": "[]",
        "stop_reason": "TrialBudgetExhaustedError",
        "input_tokens": None, "output_tokens": None, "agent_steps": 10,
        "decision_schema": None, "decision_facts_json": None,
        "decision_judgments_json": None, "token_flow_json": None,
        "taint_json": None, "diagnosis_json": None, "outline_json": None,
        "loop_kind": None, "first_failure_ref": None, "outcome_rule": None,
        "outcome_attribution": None, "last_edit_step": None,
        "first_edit_step": None, "tokens_after_last_edit_input": None,
        "tokens_after_last_edit_output": None,
        "source_job_dir": "/tmp/job", "source_trial_dir": "/tmp/job/t1",
        "published_job_dir": None, "report_path": None,
        "trajectory_available": False, "processed_available": False,
        "counts_available": True, "step_evidence_source": "none",
        "labels_json": "[]",
    }
    base.update(overrides)
    return base


def test_eligibility_requires_mimo_and_python_family():
    ok, _ = atlas_build.is_python_eligible(_trial())
    assert ok
    ok, reason = atlas_build.is_python_eligible(
        _trial(model_name="glm-5.3-flash"))
    assert not ok and "model" in reason
    ok, reason = atlas_build.is_python_eligible(
        _trial(task_name="mimo-v2.6-rl/candidate-0260-security-appsec"))
    assert not ok and "non-Python" in reason
    ok, _ = atlas_build.is_python_eligible(_trial(model_name=None))
    assert not ok


def test_unknown_verdict_is_not_infra_or_failure():
    assert not atlas_build.match_category(
        "counts-excluded-infra", _trial(counts_verdict=None))
    assert not atlas_build.match_category(
        "counts-excluded-copied-pass", _trial(counts_verdict=None))
    assert atlas_build.match_category(
        "counts-excluded-infra",
        _trial(counts_verdict="excluded", counts_reasons_json='["infra"]'))
    assert atlas_build.match_category(
        "counts-excluded-copied-pass",
        _trial(counts_verdict="excluded",
               counts_reasons_json='["copied_fix", "pass_tainted"]'))
    # counted rows are never exclusions
    assert not atlas_build.match_category(
        "counts-excluded-infra", _trial(counts_verdict="counted_fail"))


def test_budget_stop_predicate_is_recorded_stop_only():
    assert atlas_build.match_category(
        "recorded-budget-stop", _trial(stop_reason="ceiling:input_tokens"))
    assert not atlas_build.match_category(
        "recorded-budget-stop", _trial(stop_reason="completed"))
    assert not atlas_build.match_category(
        "recorded-budget-stop", _trial(stop_reason="unknown"))


def test_heuristic_labels_dropped_and_counted():
    trial = _trial(labels_json=json.dumps([
        {"cohort": "har119", "rater": "rater_a", "provenance": "agent_rater"},
        {"cohort": "behavior_labels_parquet", "rater": "heuristic",
         "label": "repetition", "provenance": "derived_parquet"},
    ]))
    kept, dropped = atlas_build.summarize_labels(trial)
    assert dropped == 1
    assert [e["cohort"] for e in kept] == ["har119"]


def test_unverified_exemplars_never_generalize():
    matching = [_trial(trial_name="t1"), _trial(trial_name="t2")]
    # No steps at all: zero exemplars, not padded.
    assert atlas_build.select_exemplars(matching, {}) == []


def test_gepa_gate_fail_closed():
    assert atlas_build.reflection_status(None, [], {})["status"] == "unavailable"
    assert atlas_build.reflection_status([], [], {})["status"] == "unavailable"
    assert atlas_build.reflection_status(
        [{"task": "x"}], [], {})["status"] == "unavailable"
    ok = atlas_build.reflection_status(
        [{"task": "format-code-task-000383", "package_digest": "sha256:abc"}],
        [_trial()], {})
    assert ok["status"] == "available"
    assert ok["matched_trial_names"] == ["t1"]
    # Held-out-shaped bindings that match nothing stay unavailable, never empty-ok.
    assert atlas_build.reflection_status(
        [{"task": "other", "package_digest": "sha256:zzz"}],
        [_trial()], {})["status"] == "unavailable"


def _write_minimal_job(root: Path, job_name: str, trial_name: str, *,
                       reward=0.0, stop="TrialBudgetExhaustedError",
                       with_steps=True) -> Path:
    job_dir = root / job_name
    trial_dir = job_dir / trial_name
    trial_dir.mkdir(parents=True)
    (job_dir / "result.json").write_text(json.dumps(
        {"id": job_name, "n_total_trials": 1,
         "stats": {"n_completed_trials": 1}}))
    (job_dir / "provenance.json").write_text(json.dumps(
        {"card": "HAR-116", "source_path": str(job_dir),
         "model": "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"}))
    (job_dir / "experiment-spec.json").write_text(json.dumps(
        {"model": "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
         "task_package_digest": "sha256:abc"}))
    trial_result: dict = {"id": trial_name, "job_id": job_name,
                          "task_name": "mimo-v2.6-rl/format-code-task-000383"}
    if reward is not None:
        trial_result["verifier_result"] = {"rewards": {"reward": reward}}
    (trial_dir / "result.json").write_text(json.dumps(trial_result))
    proc_dir = job_dir / "processed"
    proc_dir.mkdir(exist_ok=True)
    (proc_dir / f"trial-{trial_name}.json").write_text(json.dumps(
        {"trial_name": trial_name, "stop_reason": stop, "agent_steps": 2,
         "reward": reward, "scored": reward is not None}))
    if with_steps:
        agent_dir = trial_dir / "agent"
        agent_dir.mkdir()
        (agent_dir / "trajectory.json").write_text(json.dumps({"steps": [
            {"step_id": 1, "source": "agent", "timestamp": "2026-10-01T00:00:01Z",
             "is_copied_context": False,
             "metrics": {"prompt_tokens": 10, "completion_tokens": 5},
             "tool_calls": [{"function_name": "bash_command",
                             "arguments": {"keystrokes": "true"}}],
             "observation": {"results": [{"content": "ok"}]}},
            {"step_id": 2, "source": "agent", "timestamp": "2026-10-01T00:00:02Z",
             "is_copied_context": False,
             "metrics": {"prompt_tokens": 10, "completion_tokens": 5},
             "tool_calls": [{"function_name": "bash_command",
                             "arguments": {"keystrokes": "echo tail"}}],
             "observation": {"results": [{"content": "tail"}]}},
        ]}))
    return job_dir


def test_consumer_boundary_on_throwaway_corpus(tmp_path):
    """Atlas consumes the real view surface on a synthetic 2-trial corpus."""
    tq = pytest.importorskip("evallab.trace_query", reason="needs parent trace_query integration")
    connect_trace_query = tq.connect_trace_query

    repo = tmp_path / "repo"
    (repo / "research/experiments/python-task-ledger").mkdir(parents=True)
    (repo / "research/experiments/python-task-ledger/ledger.csv").write_text(
        "task_id,split,project,image_mib,status,reason,run,run_digest\n"
        "format-code-task-000383,train,x,1,usable,nop sound,original,sha256:abc\n")
    har119 = repo / "research/explorations/trace-lab/har119"
    (har119 / "labels").mkdir(parents=True)
    (har119 / "labels" / "MANIFEST.sha256").write_text("")
    (har119 / "labels" / "FROZEN_AT").write_text("2026-10-01T00:00:02Z\n")
    (har119 / "page_scores.json").write_text(json.dumps(
        {"labels_manifest_sha256": "none"}))
    jobs = tmp_path / "jobs"
    d1 = _write_minimal_job(jobs, "job-a", "trial-a")
    d2 = _write_minimal_job(jobs, "job-b", "trial-b", reward=None,
                            stop="unknown", with_steps=False)

    conn, coverage = connect_trace_query(
        repo_root=repo, results_home=tmp_path, job_dirs=[d1, d2])
    try:
        cols = [d[0] for d in conn.execute("SELECT * FROM v_trace_trials").description]
        trials = [dict(zip(cols, r)) for r in
                  conn.execute("SELECT * FROM v_trace_trials").fetchall()]
        scols = [d[0] for d in conn.execute("SELECT * FROM v_trace_steps").description]
        steps_by_trial: dict = {}
        for r in conn.execute("SELECT * FROM v_trace_steps").fetchall():
            row = dict(zip(scols, r))
            steps_by_trial.setdefault((row["job_id"], row["trial_id"]), []).append(row)
    finally:
        conn.close()
    assert len(trials) == 2

    ledger = atlas_build.load_ledger(repo)
    freeze = atlas_build.verify_har119_freeze(repo)
    page_scores = json.loads((har119 / "page_scores.json").read_text())
    atlas = atlas_build.build_atlas(trials, steps_by_trial, coverage, repo,
                                    ledger, freeze, page_scores, None)
    assert atlas["status"] == "provisional"
    assert atlas["corpus"]["n_eligible"] == 2
    budget = next(c for c in atlas["categories"]
                  if c["id"] == "recorded-budget-stop")
    assert budget["n"] == 1 and budget["denominator_n"] == 2
    assert atlas["reflection"]["status"] == "unavailable"
    # The step-linked exemplar resolves against the throwaway raw file.
    assert budget["exemplars"][0]["raw_verification"]["verified"] is True


def test_empty_corpus_aborts_without_fallback(tmp_path, capsys):
    pytest.importorskip("evallab.trace_query", reason="needs parent trace_query integration")

    empty = tmp_path / "empty-job"
    empty.mkdir()
    rc = atlas_build.main(["--repo-root", str(tmp_path),
                           "--job-dir", str(empty),
                           "--out-dir", str(tmp_path / "out")])
    assert rc == 2
