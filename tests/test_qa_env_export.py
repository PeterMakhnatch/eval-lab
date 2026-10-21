"""Teacher-data export: which runs may become rows, and why every other run is refused."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from evallab.qa import env, env_agent, env_critic
from evallab.qa.env_export import (
    CLASS_CATEGORY,
    RUNTIME_TOOLS,
    ExportError,
    SplitGuard,
    audit_session_candidates,
    executed_clean_candidates,
    export,
    seeker_candidates,
    write_export,
)

REPO = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "d" * 64
WITNESS = "a" * 32
MODEL = "meta/muse-wonder-1.0-contributor"


def guard(**kwargs) -> SplitGuard:
    rows = [
        {"key_id": "tb4-audit-v1", "task": "terminal-bench/t-dev", "split": "dev"},
        {"key_id": "tb4-audit-v1", "task": "terminal-bench/t-test", "split": "test"},
        {"key_id": "mimo-heldout", "task": "candidate-0001-held", "split": "heldout"},
        {"key_id": "other-key", "task": "o-test", "split": "test"},
        {"key_id": "other-key", "task": "o-dev", "split": "dev"},
    ]
    return SplitGuard(assignments=rows, tb4_test_digests=["sha256:" + "e" * 64], **kwargs)


# -- split guard ------------------------------------------------------------------------


@pytest.mark.parametrize("task_id", [
    "t-test", "terminal-bench/t-test", "terminal-bench/t-test@452bf305c6dc", "other/prefix/t-test"])
def test_guard_refuses_a_tb4_test_task_under_any_spelling(task_id):
    verdict = guard().check(task_id)
    assert not verdict.allowed and verdict.status == "refused"
    assert "tb4-audit-v1: test" in verdict.reasons[0]


def test_guard_refuses_a_tb4_test_task_by_digest_even_when_renamed():
    verdict = guard().check("renamed-task", "sha256:" + "e" * 64)
    assert not verdict.allowed and "task digest" in verdict.reasons[0]


def test_guard_refuses_heldout_ids_in_every_scope():
    for scope in ("reported", "tb4-heldout"):
        verdict = guard(scope=scope).check("candidate-0001-held")
        assert not verdict.allowed and verdict.reasons == ("mimo-heldout: heldout",)


def test_guard_scope_decides_whether_another_keys_test_split_counts():
    assert not guard(scope="reported").check("o-test").allowed
    assert guard(scope="tb4-heldout").check("o-test").allowed


def test_guard_allows_dev_and_labels_unregistered_tasks():
    assert guard().check("t-dev").status == "dev_only"
    verdict = guard().check("never-registered")
    assert verdict.allowed and verdict.status == "unregistered"


def test_guard_refuse_ids_extend_the_refusal_list():
    verdict = guard(refused_ids=["terminal-bench/t-dev@abc"]).check("t-dev")
    assert not verdict.allowed and verdict.reasons == ("refuse-ids list",)


def test_guard_rejects_an_unknown_scope():
    with pytest.raises(ExportError):
        guard(scope="everything")


def test_guard_from_repo_reads_the_real_answer_keys_and_refuses_every_tb4_test_task():
    real = SplitGuard.from_repo(REPO)
    tasks = [json.loads(line) for line in (REPO / "research/answer-keys/tb4-audit-v1/tasks.jsonl").read_text().splitlines()]
    test_tasks = [t for t in tasks if t["split"] == "test"]
    assert len(test_tasks) == 41
    for task in test_tasks:
        assert not real.check(task["subject"]["task_id"], task["subject"]["task_digest"]).allowed
    heldout_names = {
        r["task"] for r in csv.DictReader((REPO / "research/answer-keys/splits/assignments.csv").open())
        if r["split"] == "heldout"}
    assert heldout_names and all(not real.check(name).allowed for name in heldout_names)
    assert real.sources["assignments_sha256"] and real.sources["tb4_tasks_sha256"]


def test_guard_from_repo_missing_sources_fails_loudly(tmp_path):
    with pytest.raises(ExportError, match="split guard source missing"):
        SplitGuard.from_repo(tmp_path)


# -- audit session fixtures -------------------------------------------------------------


def finding(task="t-dev", refs=None, *, measurement="not_run", summary="grader accepts a constant",
            refutation="unverified", digest=DIGEST, defect="weak_check", verdict="fail"):
    return {
        "schema": "qa.finding/v1", "id": None,
        "subject": {"kind": "task", "task_id": f"terminal-bench/{task}", "task_digest": digest, "trial_id": None},
        "stage": "execution_review" if measurement == "measured" else "source_review",
        "verdict": verdict, "category": CLASS_CATEGORY[defect], "severity": "medium", "fault": "eval",
        "summary": summary, "evidence_refs": refs if refs is not None else ["tests/test_x.py:3"],
        "measurement": measurement, "root_cause": [defect],
        "refutation": {"status": refutation, "by": None, "reason": "no repro" if refutation == "refuted" else None},
        "reviewer": {"agent": "qa-task@2", "model": MODEL, "cost_usd": 0.0, "tokens": None}, "reproducer": None,
    }


def receipt(*, status="measured", tool="run-blank", digest=DIGEST, ident=WITNESS):
    return {"id": ident, "tool": tool, "task_digest": digest, "execution_status": status,
            "result": {"reward": 1.0, "error": None if status == "measured" else "Docker admission refused"}}


def audit_session(tmp_path: Path, name: str, *, findings=(), observations=(), model=MODEL, actual=MODEL,
                  status="final", task="t-dev", calls=(), stop_reason=None) -> Path:
    run = tmp_path / "runs" / name / "session"
    agent = run / "agent"
    (run / "findings").mkdir(parents=True)
    (run / "observations").mkdir()
    agent.mkdir(parents=True)
    (agent / "session.json").write_text(json.dumps({
        "schema": "qa.env.audit/v2", "status": status, "stop_reason": stop_reason, "model": model,
        "lane": "omp", "actual_model": actual, "used": {"tool_calls": len(calls)},
        "task_identity": {"task_id": f"terminal-bench/{task}", "task_digest": DIGEST}}))
    events = [{"seq": 1, "event": "session_started", "tools": ["task-info", "run-blank"]},
              {"seq": 2, "event": "model_turn", "invocation": 1, "requested_model": model,
               "actual_model": actual, "response": '{"calls": []}', "usage": {"input_tokens": 1}}]
    for tool, observation in calls:
        events.append({"seq": len(events) + 1, "event": "tool_call", "tool": tool, "args": {}, "observation": observation})
    (agent / "journal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    for index, item in enumerate(findings):
        (run / "findings" / f"{index:02d}.json").write_text(json.dumps(item))
    for item in observations:
        (run / "observations" / f"{item['id']}.json").write_text(json.dumps(item))
    return agent / "session.json"


def run_export(root: Path, **kwargs):
    return export(audit_session_candidates(root), kwargs.pop("guard", guard()), **kwargs)


def test_a_witnessed_finding_becomes_a_row_with_a_full_clearance_record(tmp_path):
    audit_session(tmp_path, "ok", findings=[finding(refs=["tests/test_x.py:3", f"witness:{WITNESS}"], measurement="measured")],
                  observations=[receipt()], calls=[("run-blank", {"execution_status": "measured"})])
    result = run_export(tmp_path)
    assert len(result.rows) == 1 and not result.refusals
    row = result.rows[0]
    clearance = row["clearance"]
    assert row["outcome"] == "finding" and row["defect_class"] == "weak_check" and row["category"] == "weak"
    assert clearance["execution"]["confirmed"] is True
    assert clearance["execution"]["evidence"][0]["witness"] == f"witness:{WITNESS}"
    assert clearance["model"]["status"] == "matched" and clearance["model"]["requested"] == MODEL
    assert clearance["guard"]["status"] == "dev_only" and clearance["guard"]["digest_checked"] is True
    assert clearance["dedup"]["body_sha256"] == row["body_sha256"]
    assert row["turns"][0]["response"] == '{"calls": []}' and row["turns"][0]["observations"][0]["tool"] == "run-blank"
    assert row["packet"]["tools_offered"] == ["task-info", "run-blank"]


def test_a_source_only_finding_is_never_a_row(tmp_path):
    audit_session(tmp_path, "src", findings=[finding()])
    result = run_export(tmp_path)
    assert result.rows == []
    refusal = result.refusals[0]
    assert refusal.primary == "no_execution_confirmation"
    assert "source-only finding" in refusal.reasons[0][1]


@pytest.mark.parametrize("case, observations, why", [
    ("unmeasured", [receipt(status="refused")], "execution_status='refused'"),
    ("no receipt", [], "no receipt in this run"),
    ("other task digest", [receipt(digest="sha256:" + "0" * 64)], "another task digest"),
    ("not a runtime tool", [receipt(tool="task-info")], "not a runtime-tool witness"),
])
def test_a_witness_that_does_not_check_out_is_refused(tmp_path, case, observations, why):
    audit_session(tmp_path, "w", findings=[finding(refs=[f"witness:{WITNESS}"], measurement="measured")],
                  observations=observations)
    result = run_export(tmp_path)
    assert result.rows == [], case
    assert why in result.refusals[0].reasons[-1][1]


def test_an_invented_witness_id_is_refused(tmp_path):
    audit_session(tmp_path, "inv", findings=[finding(refs=["witness:not-a-hex-id"], measurement="measured")])
    assert "malformed witness id" in run_export(tmp_path).refusals[0].reasons[-1][1]


def test_a_finding_that_claims_measured_but_cites_no_witness_is_refused(tmp_path):
    audit_session(tmp_path, "m", findings=[finding(measurement="measured")])
    assert run_export(tmp_path).rows == []


def test_a_refuted_or_non_fail_finding_is_refused(tmp_path):
    audit_session(tmp_path, "r", findings=[
        finding(refs=[f"witness:{WITNESS}"], measurement="measured", refutation="refuted"),
        finding(summary="another", refs=[f"witness:{WITNESS}"], measurement="measured", verdict="unknown")],
        observations=[receipt()])
    result = run_export(tmp_path)
    assert result.rows == []
    codes = {code for r in result.refusals for code, _ in r.reasons}
    assert {"refuted", "no_execution_confirmation"} <= codes


def test_a_session_without_a_finding_is_reported_not_called_clean(tmp_path):
    audit_session(tmp_path, "none")
    result = run_export(tmp_path)
    assert result.rows == [] and result.refusals[0].primary == "no_recorded_finding"
    assert result.report()["per_outcome"] == {}


def witnessed(tmp_path, name, **kwargs):
    audit_session(tmp_path, name, findings=[finding(refs=[f"witness:{WITNESS}"], measurement="measured",
                                                    **kwargs.pop("finding", {}))],
                  observations=[receipt()], **kwargs)


def test_a_tb4_test_task_is_refused_even_with_a_perfect_witness(tmp_path):
    witnessed(tmp_path, "test", task="t-test", finding={"task": "t-test"})
    refusal = run_export(tmp_path).refusals[0]
    assert refusal.primary == "split_guard" and "tb4-audit-v1: test" in refusal.reasons[0][1]


def test_a_heldout_task_is_refused_even_with_a_perfect_witness(tmp_path):
    witnessed(tmp_path, "held", task="candidate-0001-held", finding={"task": "candidate-0001-held"})
    assert run_export(tmp_path).refusals[0].primary == "split_guard"


def test_a_model_mismatch_is_refused_even_when_unattested_runs_are_allowed(tmp_path):
    witnessed(tmp_path, "codex", model="anthropic/claude-opus-5-5", actual="openai-codex/gpt-6.1-sol")
    for allow in (False, True):
        result = run_export(tmp_path, allow_unattested=allow)
        assert result.rows == [] and result.refusals[0].primary == "model_mismatch"
        assert "openai-codex/gpt-6.1-sol" in result.refusals[0].reasons[0][1]


def test_an_unreported_model_is_refused_by_default_and_marked_when_allowed(tmp_path):
    witnessed(tmp_path, "codexlane", model="codex/gpt-6.1-sol", actual=None)
    assert run_export(tmp_path).refusals[0].primary == "model_unattested"
    row = run_export(tmp_path, allow_unattested=True).rows[0]
    assert row["clearance"]["model"]["status"] == "unreported"
    assert row["clearance"]["model"]["unattested_allowed"] is True


def test_every_failing_gate_is_listed_not_just_the_first(tmp_path):
    audit_session(tmp_path, "all", task="t-test", model="anthropic/claude-opus-5-5", actual="openai-codex/gpt-6.1-sol",
                  findings=[finding(task="t-test")])
    refusal = run_export(tmp_path).refusals[0]
    assert [code for code, _ in refusal.reasons] == ["split_guard", "model_mismatch", "no_execution_confirmation"]
    assert refusal.primary == "split_guard"
    assert [r["code"] for r in refusal.as_record()["reasons"]] == [code for code, _ in refusal.reasons]


def test_dedup_keeps_one_row_per_task_and_body_and_reports_distinct_counts(tmp_path):
    witnessed(tmp_path, "a")
    witnessed(tmp_path, "b")  # same task, same body
    witnessed(tmp_path, "c", task="t-dev2", finding={"task": "t-dev2"})  # same body, another task
    witnessed(tmp_path, "d", finding={"summary": "a different defect"})  # same task, other body
    result = run_export(tmp_path)
    report = result.report()
    assert report["candidates"] == 4 and report["rows"] == 3
    assert report["distinct_tasks"] == 2 and report["distinct_task_body_pairs"] == 3
    assert report["refused_by_primary_reason"] == {"duplicate_body": 1}
    duplicate = result.refusals[0]
    assert duplicate.primary == "duplicate_body" and "same (task, body) as" in duplicate.reasons[0][1]
    assert report["per_class"] == {"weak_check": {"rows": 3, "distinct_tasks": 2}}


def test_body_normalization_ignores_case_and_whitespace(tmp_path):
    witnessed(tmp_path, "a", finding={"summary": "Grader  accepts\na constant"})
    witnessed(tmp_path, "b", finding={"summary": "grader accepts a CONSTANT"})
    assert len(run_export(tmp_path).rows) == 1


def test_write_export_writes_three_files_and_refuses_a_used_directory(tmp_path):
    witnessed(tmp_path / "src", "a")
    out = tmp_path / "out"
    report = write_export(run_export(tmp_path / "src"), out)
    assert report["rows"] == 1
    assert json.loads((out / "report.json").read_text())["schema"] == "qa.teacher_export/v1"
    assert json.loads((out / "rows.jsonl").read_text().splitlines()[0])["schema"] == "qa.teacher_row/v1"
    assert (out / "refusals.jsonl").read_text() == ""
    with pytest.raises(ExportError, match="not empty"):
        write_export(run_export(tmp_path / "src"), out)


# -- seeker runs ------------------------------------------------------------------------


def transcript(path: Path, model="claude-sonnet-5-5", provider="anthropic"):
    events = [
        {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "Task package: t-dev"}]}},
        {"type": "message_end", "message": {"role": "assistant", "provider": provider, "model": model, "content": [
            {"type": "toolCall", "name": "read", "arguments": {"path": "task/tests/t.py"}}]}},
        {"type": "message_end", "message": {"role": "toolResult", "toolName": "read",
                                            "content": [{"type": "text", "text": "assert True"}]}},
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))


def seeker_root(tmp_path: Path, *, upstream=(1.0, "measured"), port=(0.0, "measured"), driver="sonnet",
                task="t-dev", served=("anthropic", "claude-sonnet-5-5"), claim=True) -> Path:
    root = tmp_path / "seeker"
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    transcript(work / "transcript.jsonl", served[1], served[0])
    sub = root / "submissions" / driver / task
    sub.mkdir(parents=True)
    notes = {"exploit_found": claim, "mechanism": "golden values embedded in solve.sh", "class": "answer_reachable",
             "target_check": "test_x", "files_written": ["/app/out.csv"], "intended_work_skipped": "all of it"}
    (sub / "notes.json").write_text(json.dumps(notes))
    runs = root / "runs" / driver
    runs.mkdir(parents=True)
    (runs / f"{task}.json").write_text(json.dumps({
        "driver": driver, "task": task, "model": "anthropic/claude-sonnet-5-5", "error": None,
        "workspace": str(work), "tool_calls": ["read: {}"], "text": "done", "hackbook_entry_ids": ["HB-1"],
        "validation": {"valid": True, "digest": "sha256:" + "b" * 64, "notes": notes}}))
    for side, (reward, status) in (("upstream", upstream), ("port", port)):
        folder = root / "replay" / f"{driver}-{side}"
        folder.mkdir(parents=True)
        (folder / "candidate.json").write_text(json.dumps({
            "status": status, "reward": reward, "executed": status == "measured",
            "error": None if status == "measured" else "Docker admission refused", "backend": "docker",
            "grader": side, "input": {"path": f"/x/submissions/{driver}/{task}", "kind": "solution-bundle"},
            "task": {"id": f"terminal-bench/{task}", "digest": DIGEST}}))
    return root


def test_a_differentially_replayed_exploit_becomes_a_row(tmp_path):
    result = export(seeker_candidates(seeker_root(tmp_path)), guard())
    row = result.rows[0]
    assert row["family"] == "seeker" and row["defect_class"] == "answer_reachable" and row["category"] == "reachable"
    assert [(e["side"], e["reward"]) for e in row["clearance"]["execution"]["evidence"]] == [("upstream", 1.0), ("port", 0.0)]
    assert row["clearance"]["model"]["status"] == "matched"
    assert row["packet"]["prompt"] == "Task package: t-dev"
    assert row["turns"][0]["calls"][0]["tool"] == "read" and row["turns"][1]["role"] == "tool"


@pytest.mark.parametrize("upstream, port, why", [
    ((1.0, "measured"), (1.0, "measured"), "port reward 1.0, need 0.0"),
    ((0.0, "measured"), (0.0, "measured"), "upstream reward 0.0, need 1.0"),
    ((1.0, "measured"), (None, "refused"), "port replay 'refused'"),
    ((None, "refused"), (0.0, "measured"), "upstream replay 'refused'"),
])
def test_seeker_claims_without_both_measured_rewards_are_refused(tmp_path, upstream, port, why):
    result = export(seeker_candidates(seeker_root(tmp_path, upstream=upstream, port=port)), guard())
    assert result.rows == [] and why in result.refusals[0].reasons[0][1]


def test_a_seeker_run_that_claims_no_exploit_is_refused(tmp_path):
    result = export(seeker_candidates(seeker_root(tmp_path, claim=False)), guard())
    assert result.rows == [] and "no exploit claimed" in result.refusals[0].reasons[0][1]


def test_a_seeker_label_over_a_foreign_model_transcript_is_refused(tmp_path):
    root = seeker_root(tmp_path, served=("google-antigravity", "gemini-4-argon-high"))
    result = export(seeker_candidates(root), guard(), allow_unattested=True)
    assert result.rows == [] and result.refusals[0].primary == "model_mismatch"


def test_a_seeker_run_without_a_retained_transcript_is_unattested(tmp_path):
    root = seeker_root(tmp_path)
    (tmp_path / "work" / "transcript.jsonl").unlink()
    assert export(seeker_candidates(root), guard()).refusals[0].primary == "model_unattested"
    kept = export(seeker_candidates(root), guard(), allow_unattested=True).rows[0]
    assert kept["clearance"]["model"]["unattested_allowed"] is True


def test_the_v0_replay_layout_with_a_digest_file_and_a_static_port_extract_confirms(tmp_path):
    root = seeker_root(tmp_path)
    for folder in ("sonnet-upstream", "sonnet-port"):
        (root / "replay" / folder / "candidate.json").unlink()
    (root / "replay" / "upstream").mkdir()
    (root / "replay" / "upstream" / ("b" * 64 + ".json")).write_text(json.dumps({"result": {
        "status": "measured", "reward": 1.0, "error": None, "executed": True, "backend": "modal",
        "task": {"id": "terminal-bench/t-dev", "digest": DIGEST}}}))
    (root / "replay" / "port").mkdir()
    (root / "replay" / "port" / "sonnet-t-dev.json").write_text(json.dumps({
        "mode": "port-static-extract", "reward": 0.0, "error": None}))
    row = export(seeker_candidates(root), guard()).rows[0]
    assert [e["layout"] for e in row["clearance"]["execution"]["evidence"]] == ["digest", "static_extract"]


# -- executed-clean manifests -----------------------------------------------------------


def manifest(tmp_path: Path, members: list[dict]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"dataset_id": "mimo-executed-clean-v1", "not_semantic_certification": True,
                                "members": members}))
    return path


def member(task="candidate-0002-clean", reference=(1.0, 1.0, 1.0), nop=(0.0, 0.0, 0.0), heldout=False):
    return {"task_id": task, "record_id": f"{task}@sha256:x", "cohort": "unchanged-clean-controls",
            "package_digest": "sha256:" + "1" * 64, "harbor_digest": "sha256:" + "2" * 64,
            "verifier_digest": "sha256:" + "3" * 64, "heldout_excluded_from_training": heldout,
            "controls": {"reference": list(reference), "nop": list(nop)}}


def test_strict_repeat_controls_make_a_clean_row_that_is_not_a_semantic_certificate(tmp_path):
    result = export(executed_clean_candidates(manifest(tmp_path, [member()])), guard())
    row = result.rows[0]
    assert row["outcome"] == "clean" and row["target"]["findings"] == [] and row["target"]["verdict"] == "clean"
    assert row["target"]["not_semantic_certification"] is True
    assert row["clearance"]["model"]["status"] == "not_applicable"
    assert row["clearance"]["execution"]["kind"] == "executed_controls"
    assert result.report()["per_class"] == {"clean": {"rows": 1, "distinct_tasks": 1}}


@pytest.mark.parametrize("kwargs", [
    {"reference": (1.0, 1.0)}, {"reference": (1.0, 0.0, 1.0)}, {"nop": (0.0, 1.0, 0.0)}, {"nop": ()}])
def test_clean_requires_repeat_controls_that_all_agree(tmp_path, kwargs):
    result = export(executed_clean_candidates(manifest(tmp_path, [member(**kwargs)])), guard())
    assert result.rows == [] and result.refusals[0].primary == "no_execution_confirmation"


def test_a_manifest_heldout_flag_or_a_guarded_task_refuses_the_clean_row(tmp_path):
    members = [member(heldout=True), member(task="candidate-0001-held"), member(task="t-test")]
    result = export(executed_clean_candidates(manifest(tmp_path, members)), guard())
    assert result.rows == []
    assert {r.primary for r in result.refusals} == {"split_guard"}


def test_two_clean_packages_of_one_task_are_distinct_bodies(tmp_path):
    other = member()
    other["package_digest"] = "sha256:" + "9" * 64
    assert len(export(executed_clean_candidates(manifest(tmp_path, [member(), other])), guard()).rows) == 2


# -- constants that mirror private owners -----------------------------------------------


def test_mirrored_constants_match_their_owners():
    assert RUNTIME_TOOLS == env._RUNTIME_TOOLS
    assert CLASS_CATEGORY == env_agent._CLASS_CATEGORY


# -- failure-fact critic ----------------------------------------------------------------


def crit_session(tmp_path, name, status, *, task="t-dev", calls=(), stop_reason=None):
    return audit_session(tmp_path, name, status=status, task=task, calls=calls, stop_reason=stop_reason).parent


UNMEASURED = {"execution_status": "unmeasured", "result": {"error": "Docker admission refused: 5347 MiB admitted; task needs 8192 MiB"}}


def test_critic_classifies_sessions_from_recorded_facts(tmp_path):
    cases = {
        "parse": crit_session(tmp_path, "p", "invalid_action", stop_reason="2 consecutive unusable replies: no JSON"),
        "driver_error": crit_session(tmp_path, "d", "model_error", stop_reason="DriverError: x"),
        "no_finish": crit_session(tmp_path, "n", "tool_budget_exhausted"),
        "unmeasured": crit_session(tmp_path, "u", "final", calls=[("run-blank", UNMEASURED)]),
        "source_only": crit_session(tmp_path, "s", "final", calls=[("task-info", {})]),
    }
    for expected, agent in cases.items():
        assert env_critic.classify_session(agent)["class"] == expected
    measured = crit_session(tmp_path, "m", "final", calls=[("run-blank", {"execution_status": "measured"})])
    assert env_critic.classify_session(measured) is None
    assert set(cases) == set(env_critic.FAILURE_CLASSES)


COMPOSE = {"execution_status": "refused", "result": {"error": "GraderRefusal: Compose tasks are unsupported on 3 hosts"}}


def test_critic_clusters_by_cause_routes_each_and_lists_only_dev_tasks_for_rerun(tmp_path):
    for index, task in enumerate(("t-dev", "t-test", "t-dev3")):
        crit_session(tmp_path, f"u{index}", "final", task=task, calls=[("run-blank", UNMEASURED)])
    for index in range(2):
        crit_session(tmp_path, f"c{index}", "final", task="t-dev4", calls=[("probe", COMPOSE)])
    crit_session(tmp_path, "lonely", "model_error", stop_reason="DriverError: once")
    result = env_critic.critique([tmp_path], min_sessions=2, guard=guard())
    assert result["sessions"] == 6 and result["by_class"] == {"driver_error": 1, "unmeasured": 5}
    host, task_fact = result["proposals"]
    assert (host["class"], host["sessions"], host["route"]) == ("unmeasured", 3, "fix_harness")
    assert "Docker admission refused: N MiB admitted; task needs N MiB" in host["signature"]
    assert "no prompt fact can remove this" in host["fact"]
    assert (task_fact["route"], task_fact["sessions"]) == ("fact", 2)
    assert "never reward 0 or clean" in task_fact["fact"] and "probe" in task_fact["fact"]
    assert result["rerun_dev_tasks"] == {
        "tasks": ["t-dev", "t-dev3", "t-dev4"], "excluded_reported_split": ["t-test"]}
    markdown = env_critic.render_markdown(result)
    assert "## Proposed packet facts" in markdown and "## Harness or host defects" in markdown
    assert env_critic.route_for("driver_error", "anything") == "fix_harness"


def test_facts_need_evidence_and_the_shipped_facts_load():
    assert env_critic.parse_facts("- a fact — evidence: measured here\nprose\n") == [
        {"fact": "a fact", "evidence": "measured here"}]
    with pytest.raises(ValueError, match="needs"):
        env_critic.parse_facts("- a fact with no evidence\n")
    with pytest.raises(ValueError, match="longer"):
        env_critic.parse_facts("- " + "x" * 500 + " — evidence: y\n")
    shipped = env_critic.facts_for("terminal-bench/fin-saccr-rwa")
    assert len(shipped) == 2 and shipped[0]["fact"].startswith("`/tests` is absent")
    assert env_critic.facts_for("no-such-family") == []
