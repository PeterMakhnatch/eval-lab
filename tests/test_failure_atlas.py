"""Consumer-boundary tests for the HAR-131 failure atlas.

These pin the atlas contract, not current corpus numbers: eligibility,
counts_verdict as sole counted authority, raw-verified exemplars, frozen-only
labels, and the fail-closed GEPA gate. Numbers in atlas.json are current-view
measures and will change on refresh; tests use synthetic rows or throwaway
tmp corpora only.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_builder_path = (
    Path(__file__).resolve().parents[1]
    / "research/explorations/trace-lab/failure-atlas/build.py"
)
_builder_spec = importlib.util.spec_from_file_location("har131_atlas_build_test", _builder_path)
assert _builder_spec is not None and _builder_spec.loader is not None
atlas_build = importlib.util.module_from_spec(_builder_spec)
sys.modules[_builder_spec.name] = atlas_build
_builder_spec.loader.exec_module(atlas_build)


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


def test_eligibility_requires_model_and_canonical_python_identity():
    python_tasks = {"format-code-task-000383"}
    assert atlas_build.is_python_eligible(_trial(), python_tasks)[0]
    assert atlas_build.is_python_eligible(
        _trial(model_name="XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129"), python_tasks
    )[0]
    for model in ("glm-5.3-flash", None, "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:unknown"):
        assert not atlas_build.is_python_eligible(_trial(model_name=model), python_tasks)[0]
    # Real HAR81 counterexamples: JavaScript 000240/000434 and unknown-language
    # 001520 share the format-code naming convention with Python tasks.
    for task in ("format-code-task-000240", "format-code-task-000434",
                 "format-code-task-001520", "candidate-0260-security-appsec"):
        assert not atlas_build.is_python_eligible(
            _trial(task_name=f"mimo-v2.6-rl/{task}"), python_tasks
        )[0]


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


def test_frozen_label_cohorts_retained_without_heuristic_or_unverified_rows():
    source_sha = "ab" * 32
    verified = [
        {"cohort": cohort, "rater": "rater_a", "provenance": "agent_rater",
         "source_file": f"{cohort}/rater_a/t1.json", "source_sha256": source_sha,
         "loop_kind": {"kind": "repetition", "confidence": "high"},
         "first_failure": {"step": 3}, "blame_confidence": "low"}
        for cohort in ("har119", "har128-har116", "har128-g2-a1", "har128-g2-r2", "har128-g2-tail")
    ]
    verified.append({
        "cohort": "har128-sft-pass", "provenance": "frozen_adjudication",
        "source_file": "sft_gate/labels.jsonl", "source_sha256": source_sha,
        "label_scope": "sft_pass_cleanliness", "batch": "labels",
        "label": {"trial": "t1", "clean": False, "genuine": False},
    })
    rejected = [
        {"cohort": "behavior_labels_parquet", "rater": "heuristic",
         "label": "repetition", "provenance": "derived_parquet"},
        {**verified[1], "provenance": "heuristic"},
        {**verified[2], "source_sha256": None},
        {**verified[3], "source_file": None},
        {**verified[4], "cohort": "unverified-study"},
    ]
    kept, dropped = atlas_build.summarize_labels(_trial(labels_json=json.dumps(verified + rejected)))
    assert kept == verified
    assert dropped == len(rejected)
    assert kept[-1]["label_scope"] == "sft_pass_cleanliness"
    assert "loop_kind" not in kept[-1]


def test_unverified_exemplars_never_generalize():
    matching = [_trial(trial_name="t1"), _trial(trial_name="t2")]
    # No steps at all: zero exemplars, not padded.
    assert atlas_build.select_exemplars(matching, {}) == []


DIG_GOOD = "sha256:" + "ab12" * 16
DIG_1181 = "sha256:a680b2bdae21a72de651b06f58968c7e881628efb2b3b6abc93ce9f8895bac3d"
DIG_0383 = "sha256:da5de5025dc1e2446c236bfd7d89337a7e478cc41d8645e8ae79c0d331941303"


def _ledger_entry(**kw):
    entry = {"status": "usable", "run": "original", "run_digest": DIG_GOOD,
             "split": "train", "project": "proj-a"}
    entry.update(kw)
    return entry


def _ledger(by_task):
    return {"path": "research/experiments/python-task-ledger/ledger.csv",
            "sha256": "sha256:ledger", "n_tasks": len(by_task), "by_task": by_task}


def _gate(task_ids=(), repos=(), digests=()):
    return {"path": "research/experiments/ovn-sft-v0/eval_tasks.csv",
            "sha256": "sha256:eval", "n_tasks": len(task_ids), "columns": ["task", "digest", "run", "repo"],
            "task_ids": set(task_ids), "repos": set(repos), "digests": set(digests)}


def _train_trial(**kw):
    base = {"task_name": "mimo-v2.6-rl/format-code-task-000495",
            "task_package_digest": DIG_GOOD, "counts_verdict": "counted_fail"}
    base.update(kw)
    return _trial(**base)


def _bindings(*pairs):
    return [{"task": task, "package_digest": digest} for task, digest in pairs]


def test_gepa_gate_fail_closed():
    gate = _gate()
    ledger = _ledger({"format-code-task-000495": _ledger_entry()})
    assert atlas_build.reflection_status(None, [], {}, ledger, gate)["status"] == "unavailable"
    assert atlas_build.reflection_status(
        [{"task": "x"}], [], {}, ledger, gate)["status"] == "unavailable"
    bound = _bindings(("format-code-task-000495", DIG_GOOD))
    # full authorization context missing: still unavailable, never half-open
    assert atlas_build.reflection_status(
        bound, [_train_trial()], {}, None, None)["status"] == "unavailable"
    assert atlas_build.reflection_status(
        bound, [_train_trial()], {}, ledger, None)["status"] == "unavailable"
    # bindings match nothing: unavailable, never an available-empty export
    assert atlas_build.reflection_status(
        _bindings(("format-code-task-000587", DIG_GOOD)),
        [_train_trial()], {}, ledger, gate)["status"] == "unavailable"


def test_g2_bindings_validation_rejects_file():
    good = ("format-code-task-000495", DIG_GOOD)
    binding = {"task": good[0], "package_digest": good[1]}
    allowed, error = atlas_build.validate_g2_bindings([binding])
    assert error is None and allowed == {good}
    # one malformed entry fails the entire file, loudly
    _, error = atlas_build.validate_g2_bindings(
        [binding, {"task": "format-code-task-000495"}])
    assert error is not None and "indices [1]" in error
    _, error = atlas_build.validate_g2_bindings(
        [binding, {"task": "candidate-1", "package_digest": DIG_GOOD}])
    assert error is not None
    _, error = atlas_build.validate_g2_bindings(
        [binding,
         {"task": "format-code-task-000495", "package_digest": 123}])
    assert error is not None
    # duplicates are explicit, not silently deduped
    _, error = atlas_build.validate_g2_bindings(
        [binding, binding])
    assert error is not None and "duplicate" in error
    assert atlas_build.validate_g2_bindings([])[1] is not None


def test_gepa_gate_selects_only_gated_training():
    ledger = _ledger({"format-code-task-000495": _ledger_entry()})
    gate = _gate()
    ok = atlas_build.reflection_status(
        _bindings(("format-code-task-000495", DIG_GOOD)), [_train_trial()], {}, ledger, gate)
    assert ok["status"] == "available"
    assert ok["payload_file"] is None  # main() writes the file on success
    assert ok["audit"]["withheld_counts"] == {
        "not_authorized": 0, "eval_identity": 0, "ledger_mismatch": 0, "non_counted": 0}
    payload = ok["payload"]
    assert payload["scope"] == "training-only"
    assert payload["aggregates"]["n_selected"] == 1
    (entry,) = payload["selected_trials"]
    assert (entry["task_id"], entry["trial_id"], entry["job_id"]) == (
        "format-code-task-000495", "trial-1", "job-1")
    assert entry["ledger"] == {"split": "train", "status": "usable", "run": "original",
                               "run_digest": DIG_GOOD, "project": "proj-a"}
    dump = json.dumps(payload)
    assert "command_text" not in dump and "observation_excerpt" not in dump
    assert "withheld_counts" not in dump and "eval_list" not in dump
    assert "001181" not in dump and "000383" not in dump
    assert "7/11" not in dump and "loop_kind_vs_agreed" not in dump


def test_gepa_gate_canonical_states_exclude_when_allowlisted():
    # ledger states mirror the real canonical rows: 001181 heldout, 000383 review
    ledger = _ledger({
        "format-code-task-001181": _ledger_entry(
            split="heldout", project="rich", run_digest=DIG_1181),
        "format-code-task-000383": _ledger_entry(
            project="quickfix", status="review", run_digest=DIG_0383),
        "format-code-task-000495": _ledger_entry(),
        "format-code-task-000587": _ledger_entry(project="eval-repo"),
        "format-code-task-001161": _ledger_entry(split="heldout"),
        "format-code-task-001832": _ledger_entry(status="review"),
        "format-code-task-002256": _ledger_entry(),
    })
    gate = _gate(repos={"eval-repo"})
    digests = {"format-code-task-001181": DIG_1181, "format-code-task-000383": DIG_0383}
    tasks = ["format-code-task-001181", "format-code-task-000383", "format-code-task-000495",
             "format-code-task-000587", "format-code-task-001161", "format-code-task-001832",
             "format-code-task-002256", "format-code-task-000927"]
    trials = [_train_trial(task_name=f"mimo-v2.6-rl/{task}",
                           task_package_digest=digests.get(task, DIG_GOOD)) for task in tasks]
    trials[6] = _train_trial(task_name="mimo-v2.6-rl/format-code-task-002256",
                             counts_verdict="excluded")
    bound = _bindings(*[(task, digests.get(task, DIG_GOOD)) for task in tasks[:7]])
    ok = atlas_build.reflection_status(bound, trials, {}, ledger, gate)
    assert ok["status"] == "available"
    assert ok["payload"]["aggregates"]["n_selected"] == 1
    assert ok["payload"]["selected_trials"][0]["task_id"] == "format-code-task-000495"
    assert ok["audit"]["withheld_counts"] == {
        "not_authorized": 1, "eval_identity": 1, "ledger_mismatch": 4, "non_counted": 1}


def test_eval_gate_rejects_missing_mismatch_empty(tmp_path):
    assert atlas_build.load_eval_gate(tmp_path) is None
    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\n1,2\n")
    assert atlas_build.load_eval_gate(tmp_path, bad) is None
    empty = tmp_path / "empty.csv"
    empty.write_text("task,digest,run,repo,image_mib\n")
    assert atlas_build.load_eval_gate(tmp_path, empty) is None
    good = tmp_path / "good.csv"
    good.write_text("task,digest,run,repo,image_mib\n"
                    f"format-code-task-000495,{DIG_GOOD},original,proj-a,1\n")
    gate = atlas_build.load_eval_gate(tmp_path, good)
    assert gate["task_ids"] == {"format-code-task-000495"}
    assert gate["repos"] == {"proj-a"} and gate["columns"][:2] == ["task", "digest"]


def test_stale_payload_removed_on_unavailable(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    stale = out / atlas_build.PAYLOAD_FILENAME
    stale.write_text("{}")
    result = atlas_build.sync_reflection_payload(
        out, {"status": "unavailable", "payload": None})
    assert result == (None, None, False)
    assert not stale.exists()


def test_payload_written_on_available(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    payload = {"scope": "training-only", "selected_trials": [], "aggregates": {}}
    name, sha, fresh = atlas_build.sync_reflection_payload(
        out, {"status": "available", "payload": payload})
    assert fresh and name == atlas_build.PAYLOAD_FILENAME
    assert json.loads((out / name).read_text()) == payload
    assert sha.startswith("sha256:")


def test_opinion_limits_derive_from_page_scores():
    scores = {"predictor": "p", "cohort": "c", "in_sample": False, "frozen_at": "f",
              "page_vs_agreed_loop_kind": {"agree": 7, "n": 11},
              "page_vs_agreed_loop_present": {"agree": 7, "n": 11},
              "page_vs_agreed_first_failure": {"agree": 1, "n": 9},
              "first_failure_coverage": {"expressed": 1, "of": 12, "abstentions": 11},
              "page_vs_agreed_blame": {"agree": 11, "n": 11, "abstained": 0},
              "blame_abstentions": 0,
              "rater_agreement_loop_kind": {"agree": 11, "n": 12, "not_expressed": 0},
              "rater_agreement_loop_present": {"agree": 11, "n": 12, "not_expressed": 0},
              "rater_agreement_first_failure": {"agree": 9, "n": 12, "not_expressed": 0},
              "rater_agreement_blame": {"agree": 11, "n": 12, "not_expressed": 0},
              "eligible_n": 11, "excluded_rater_disagreement": 1}
    limits = atlas_build.derive_opinion_limits(scores)
    assert limits["loop_kind_vs_agreed"] == "7/11"
    assert "1/9" in limits["first_failure_vs_agreed"]
    assert "11 abstentions" in limits["first_failure_vs_agreed"]
    assert limits["blame_vs_agreed"].startswith("11/11")
    assert limits["rater_agreement_first_failure"] == "9/12"


def test_opinion_limits_missing_is_unavailable():
    limits = atlas_build.derive_opinion_limits({})
    assert limits["loop_kind_vs_agreed"] == "unavailable"
    assert limits["first_failure_vs_agreed"] == "unavailable"
    assert limits["predictor"] == "unavailable"
    # agree without n borrows nothing
    partial = atlas_build.derive_opinion_limits({"page_vs_agreed_loop_kind": {"agree": 7}})
    assert partial["loop_kind_vs_agreed"] == "unavailable"


def test_ledger_keeps_split_and_project(tmp_path):
    root = tmp_path
    (root / "research/experiments/python-task-ledger").mkdir(parents=True)
    (root / "research/experiments/python-task-ledger/ledger.csv").write_text(
        "task_id,split,project,image_mib,status,reason,run,run_digest\n"
        "format-code-task-000495,train,proj-a,1,usable,ok,original,sha256:abc\n")
    ledger = atlas_build.load_ledger(root)
    assert ledger["by_task"]["format-code-task-000495"] == {
        "status": "usable", "run": "original", "run_digest": "sha256:abc",
        "split": "train", "project": "proj-a"}


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
    import shutil

    from evallab.trace_query import connect_trace_query

    repo = tmp_path / "repo"
    real_sql = Path(atlas_build.__file__).parents[4] / "sql" / "trace_queries.sql"
    if real_sql.is_file():
        (repo / "sql").mkdir(parents=True, exist_ok=True)
        shutil.copy(real_sql, repo / "sql" / "trace_queries.sql")
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
        trials = [dict(zip(cols, r, strict=True)) for r in
                  conn.execute("SELECT * FROM v_trace_trials").fetchall()]
        scols = [d[0] for d in conn.execute("SELECT * FROM v_trace_steps").description]
        steps_by_trial: dict = {}
        for r in conn.execute("SELECT * FROM v_trace_steps").fetchall():
            row = dict(zip(scols, r, strict=True))
            steps_by_trial.setdefault((row["job_id"], row["trial_id"]), []).append(row)
    finally:
        conn.close()
    assert len(trials) == 2

    ledger = atlas_build.load_ledger(repo)
    freeze = atlas_build.verify_har119_freeze(repo)
    page_scores = json.loads((har119 / "page_scores.json").read_text())
    atlas = atlas_build.build_atlas(trials, steps_by_trial, coverage, repo,
                                    ledger, freeze, page_scores, None)
    assert atlas["corpus"]["n_eligible"] == 2
    budget = next(c for c in atlas["categories"]
                  if c["id"] == "recorded-budget-stop")
    assert budget["n"] == 1 and budget["denominator_n"] == 2
    assert atlas["reflection"]["status"] == "unavailable"
    # The step-linked exemplar resolves against the throwaway raw file.
    assert budget["exemplars"][0]["raw_verification"]["verified"] is True


def test_empty_corpus_aborts_without_fallback(tmp_path, capsys):

    empty = tmp_path / "empty-job"
    empty.mkdir()
    rc = atlas_build.main(["--repo-root", str(tmp_path),
                           "--job-dir", str(empty),
                           "--out-dir", str(tmp_path / "out")])
    assert rc == 2


def _g5_fixture(tmp_path):
    """Synthetic binding and recorded metadata only; no live G5 sources or trajectories."""
    task_id = "format-code-task-000383"
    model = "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
    cells, trials = [], []
    for arm, verdict in (("stock", "counted_pass"), ("tuned", "counted_fail"), ("gepa", "excluded")):
        name = f"ovn-g5-000383-{arm}"
        spec = {
            "name": name, "question_ref": "ovn-g5", "task_id": task_id,
            "task_package_digest": DIG_GOOD, "model": model,
            "harness_tree_sha256": "sha256:" + "cd" * 32,
            "extra_instruction_sha256": ("sha256:" + "ef" * 32) if arm == "gepa" else None,
        }
        spec_path = tmp_path / name / "experiment-spec.json"
        spec_path.parent.mkdir()
        spec_path.write_text(json.dumps(spec))
        cells.append({
            "task_id": task_id, "arm": arm, "job_name": name,
            "task_package_digest": DIG_GOOD, "source_spec_file": str(spec_path),
            "source_spec_sha256": atlas_build._sha256_file(spec_path), "spec": spec,
        })
        trials.append(_trial(
            job_id=f"job-{arm}", trial_id=f"trial-{arm}", job_name=f"HAR-135-{name}",
            trial_name=f"t-{arm}", arm=arm, model_name=model,
            task_package_digest=DIG_GOOD, source_job_dir=str(spec_path.parent),
            source_trial_dir=str(spec_path.parent / f"t-{arm}"),
            counts_verdict=verdict, raw_reward=0.0 if verdict == "counted_fail" else 1.0,
            counts_reasons_json='["copied_fix"]' if verdict == "excluded" else "[]",
            processed_available=arm != "tuned",
            loop_kind="completion-claim" if arm == "gepa" else None,
        ))
    cohort = {
        "source_file": str(tmp_path / "cohort.json"), "source_sha256": "sha256:" + "12" * 32,
        "spec_manifest_sha256": "sha256:" + "34" * 32, "experiment": "synthetic G5 fixture",
        "eval_list": {}, "harness_tree": {}, "gepa_candidate": {"sha256": "sha256:" + "ef" * 32},
        "models": {arm: model for arm in ("stock", "tuned", "gepa")}, "cells": cells,
    }
    return cohort, trials, {task_id}


def test_g5_three_recorded_arms_sharing_base_model_remain_distinct(tmp_path):
    cohort, trials, python_tasks = _g5_fixture(tmp_path)
    comparison = atlas_build.build_g5_comparison(trials, {}, python_tasks, cohort)
    arms = {row["arm"]: row for row in comparison["arms"]}
    assert arms["stock"]["counts"]["pass"] == 1
    assert arms["stock"]["counts"]["fail"] == 0
    assert arms["tuned"]["counts"]["fail"] == 1
    assert arms["tuned"]["counts"]["pass"] == 0
    assert arms["gepa"]["counts"]["excluded"] == 1
    assert arms["gepa"]["counts"]["pass"] == 0
    assert arms["gepa"]["counted_denominator_n"] == 0
    gepa = next(cell for cell in comparison["cells"] if cell["arm"] == "gepa")
    assert gepa["raw_reward"] == 1.0 and gepa["counts_verdict"] == "excluded"
    loop = next(cat for cat in arms["gepa"]["categories"] if cat["id"] == "completion-claim-loop")
    assert (loop["n"], loop["denominator_n"]) == (1, 1)
    stock_loop = next(cat for cat in arms["stock"]["categories"] if cat["id"] == "completion-claim-loop")
    assert stock_loop["n"] is None and stock_loop["denominator_n"] == 0


def test_g5_expected_missing_arm_and_unscored_cell_stay_missing(tmp_path):
    cohort, trials, python_tasks = _g5_fixture(tmp_path)
    no_runs = atlas_build.build_g5_comparison([], {}, python_tasks, cohort)
    assert no_runs["status"] == "not_run"
    assert all(cell["raw_reward"] is None for cell in no_runs["cells"])
    trials[0].update(counts_verdict=None, raw_reward=None, scored=False)
    comparison = atlas_build.build_g5_comparison(trials[:2], {}, python_tasks, cohort)
    arms = {row["arm"]: row for row in comparison["arms"]}
    assert arms["gepa"]["status"] == "unavailable"
    assert arms["gepa"]["recorded_n"] == 0
    assert arms["gepa"]["counts"]["not_run"] == arms["gepa"]["counts"]["missing"] == 1
    absent = next(cell for cell in comparison["cells"] if cell["arm"] == "gepa")
    assert absent["status"] == "not_run"
    assert absent["raw_reward"] is None and absent["scored"] is None
    assert absent["counts_verdict"] is None
    assert all(cat["n"] is None for cat in arms["gepa"]["categories"])
    unknown = next(cell for cell in comparison["cells"] if cell["arm"] == "stock")
    assert unknown["status"] == "unknown"
    assert arms["stock"]["counts"]["unknown"] == 1
    assert arms["stock"]["counts"]["fail"] == arms["stock"]["counts"]["excluded"] == 0


def test_g5_historical_cohort_not_pooled_in_either_scope(tmp_path):
    cohort, trials, python_tasks = _g5_fixture(tmp_path)
    historical = _trial(job_id="old-job", trial_id="old-trial", trial_name="historical",
                        job_name="HAR-116-har116-a-000383-stock", arm="stock",
                        task_package_digest=DIG_GOOD, stop_reason="unknown",
                        source_job_dir=str(tmp_path / "historical"),
                        source_trial_dir=str(tmp_path / "historical" / "trial"))
    trials[0]["stop_reason"] = "TrialBudgetExhaustedError"
    atlas = atlas_build.build_atlas(
        trials + [historical], {}, {}, tmp_path, _ledger({
            task: _ledger_entry() for task in python_tasks}), {}, {}, None, g5_cohort=cohort)
    budget = next(cat for cat in atlas["categories"] if cat["id"] == "recorded-budget-stop")
    assert budget["trial_names"] == []
    assert budget["denominator_n"] == 1
    stock = next(arm for arm in atlas["g5_comparison"]["arms"] if arm["arm"] == "stock")
    assert stock["recorded_n"] == 1 and stock["counts"]["pass"] == 1
    assert all("historical" not in cat["trial_names"] for cat in stock["categories"])
    assert atlas["corpus"]["historical_patterns_n"] == 1
    # Without a freeze, reserved G5 rows still cannot contaminate historical frequencies.
    unbound = atlas_build.build_atlas(
        trials + [historical], {}, {}, tmp_path, _ledger({
            task: _ledger_entry() for task in python_tasks}), {}, {}, None)
    assert unbound["g5_comparison"]["status"] == "unavailable"
    assert next(cat for cat in unbound["categories"] if cat["id"] == "recorded-budget-stop")["trial_names"] == []


def test_g5_recorded_arm_and_addendum_binding_required(tmp_path):
    cohort, trials, python_tasks = _g5_fixture(tmp_path)
    trials[2]["arm"] = None
    missing_arm = atlas_build.build_g5_comparison(trials, {}, python_tasks, cohort)
    gepa = next(cell for cell in missing_arm["cells"] if cell["arm"] == "gepa")
    assert gepa["status"] == "binding_unavailable" and gepa["counts_verdict"] is None
    trials[2]["arm"] = "gepa"
    path = Path(trials[2]["source_job_dir"]) / "experiment-spec.json"
    wrong_spec = json.loads(path.read_text())
    wrong_spec["extra_instruction_sha256"] = None
    path.write_text(json.dumps(wrong_spec))
    wrong_addendum = atlas_build.build_g5_comparison(trials, {}, python_tasks, cohort)
    gepa = next(cell for cell in wrong_addendum["cells"] if cell["arm"] == "gepa")
    assert gepa["status"] == "binding_unavailable" and gepa["counts_verdict"] is None
    assert wrong_addendum["binding_rejections"][0]["trial_id"] == "trial-gepa"


def test_g5_duplicate_recorded_attempts_are_ambiguous_not_pooled(tmp_path):
    cohort, trials, python_tasks = _g5_fixture(tmp_path)
    duplicate = {**trials[0], "trial_id": "second-stock", "trial_name": "second-stock",
                 "source_trial_dir": str(Path(trials[0]["source_job_dir"]) / "second-stock")}
    comparison = atlas_build.build_g5_comparison(trials + [duplicate], {}, python_tasks, cohort)
    stock = next(arm for arm in comparison["arms"] if arm["arm"] == "stock")
    cell = next(cell for cell in comparison["cells"] if cell["arm"] == "stock")
    assert cell["status"] == "ambiguous"
    assert stock["counts"]["pass"] == 0 and stock["counts"]["binding_unavailable"] == 1
    assert {row["trial_id"] for row in cell["recorded_trials"]} == {"trial-stock", "second-stock"}


def _write_loop_calibration(root, suffix, label_dir, agree, n):
    manifest = root / f"research/explorations/trace-lab/har128/{label_dir}/MANIFEST.sha256"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(f"{suffix} fixture freeze\n")
    receipt = root / f"research/experiments/har117-results-home/har131-page-calibration-{suffix}.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "schema": "har131.page_loop_calibration/v1", "cohort": suffix,
        "labels_manifest_sha256": atlas_build._sha256_file(manifest).removeprefix("sha256:"),
        "page_vs_agreed": {"agree": agree, "n": n},
        "rater_agreement": {"agree": n, "n": n + 1}, "page_abstentions": 0,
    }))
    return receipt, manifest


def test_loop_calibration_accuracy_is_separate_by_frozen_study(tmp_path):
    first, _ = _write_loop_calibration(tmp_path, "har116", "labels_har116", 2, 3)
    second, _ = _write_loop_calibration(tmp_path, "g2-a1", "labels_g2_a1", 1, 4)
    studies = atlas_build.load_loop_calibrations(tmp_path)
    limits = atlas_build.derive_opinion_limits({
        "cohort": "historical-har119", "page_vs_agreed_loop_kind": {"agree": 1, "n": 2},
    }, studies)
    by_cohort = {study["label_cohort"]: study for study in limits["loop_calibration_studies"]}
    assert limits["loop_kind_vs_agreed"] == "1/2"
    assert by_cohort["har128-har116"]["page_vs_agreed"] == {"agree": 2, "n": 3}
    assert by_cohort["har128-g2-a1"]["page_vs_agreed"] == {"agree": 1, "n": 4}
    assert by_cohort["har128-har116"]["source_sha256"] == atlas_build._sha256_file(first)
    assert by_cohort["har128-g2-a1"]["source_sha256"] == atlas_build._sha256_file(second)
    assert by_cohort["har128-g2-r2"]["status"] == "unavailable"
    assert by_cohort["har128-g2-r2"]["loop_kind_vs_agreed"] == "unavailable"
    assert by_cohort["har128-g2-tail"]["page_abstentions"] is None
    assert limits["g5_calibration"]["status"] == "unavailable"
    assert "page_vs_agreed" not in limits["g5_calibration"]


def test_loop_calibration_mismatched_freeze_does_not_publish_accuracy(tmp_path):
    receipt, manifest = _write_loop_calibration(tmp_path, "har116", "labels_har116", 2, 3)
    manifest.write_text("different fixture freeze\n")
    study = atlas_build.load_loop_calibrations(tmp_path)[0]
    assert study["status"] == "unavailable"
    assert study["loop_kind_vs_agreed"] == "unavailable"
    assert study["page_abstentions"] is None
    assert "page_vs_agreed" not in study
    assert study["source_sha256"] == atlas_build._sha256_file(receipt)


def test_g5_cohort_unadmitted_bytes_rejected(tmp_path):
    import pytest

    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"arms": ["stock", "tuned", "gepa"], "cohort": []}))
    with pytest.raises(ValueError):
        atlas_build.load_g5_cohort(cohort)
