"""Contract counterexamples for analyst recipe engine v1 (R1–R7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.trajectory_ir import _normalize_argument_skeleton
from evallab.trajectory_judgment import canonical_json_digest
from evallab.trajectory_recipes import (
    CONTRACT_DIGEST,
    PRODUCER,
    RecipeFinding,
    TrialArtifacts,
    assert_citations_in_pack,
    load_trial_artifacts,
    run_recipes,
)

PROFILE = "sha256:" + "a" * 64
ICO_TRIAL = "69a3ed7f-4303-4fc8-931e-1e842c3cb810"
ANALYSES = Path("/Users/petermakhnatch/Developer/eval-lab/derived/analyses")
DIGEST_A = "sha256:" + "1" * 64
DIGEST_B = "sha256:" + "2" * 64
TASK_DIGEST = "sha256:" + "3" * 64
VERIFIER_DIGEST = "sha256:" + "4" * 64
AGENT_CLASSES = {
    "wrong_target_or_action",
    "infrastructure_failure",
    "successful_recovery",
    "missed_recovery_opportunity",
    "false_verification_or_unsupported_terminal_claim",
}
LEGACY_METRIC_KEYS = {
    "linear_innocence",
    "linear_innocence_screening",
    "tool_error_rate",
    "tool_error_rate_screening",
    "context_burn_velocity",
    "context_burn_velocity_screening",
    "cache_hit_rate",
    "cache_hit_rate_screening",
    "loop_index",
}


def make_citation(cid: str, step_id: int = 1) -> dict:
    return {
        "citation_id": cid,
        "availability": "available",
        "call_index": None,
        "cas_uri": None,
        "content_sha256": None,
        "ir_event_id": None,
        "observation_index": None,
        "raw_cas_uri": None,
        "redaction_profile_digest": None,
        "source_call_id": None,
        "source_document_id": "main",
        "source_path": "agent/trajectory.json",
        "source_sha256": "aa" * 32,
        "step_id": step_id,
        "step_index": step_id,
        "target_type": "step",
        "tool_call_id": None,
        "trial_id": None,
    }


def make_event(
    *,
    event_id: str,
    citation_id: str,
    event_type: str,
    actor: str = "agent",
    ordinal: int = 0,
    step_index: int = 1,
    exit_code: int | None = None,
    exit_semantics: str = "unobserved",
    is_error: bool = False,
    program: str | None = None,
    family: str = "other",
    payload_digest: str | None = None,
    summary: str = "",
    hydrated_content: str | None = None,
    call_index: int | None = 0,
    matched_result_digest: str | None = None,
) -> dict:
    hydrated_content = (
        hydrated_content
        if hydrated_content is not None
        else json.dumps({"message": summary or f"{event_type} {event_id}"})
    )
    return {
        "event_id": event_id,
        "event_ordinal": ordinal,
        "event_type": event_type,
        "actor": actor,
        "timestamp": None,
        "phase": "work",
        "episode_id": 1,
        "step_index": step_index,
        "call_index": call_index,
        "action_family": family,
        "status_owning_program": program,
        "argument_skeleton": program,
        "exit_code": exit_code,
        "exit_semantics": exit_semantics,
        "is_error": is_error,
        "payload_digest": payload_digest or DIGEST_A,
        "payload_bytes": 12,
        "source_citation": make_citation(citation_id, step_id=step_index),
        "summary": summary,
        "hydrated_content": hydrated_content,
        "matched_result_digest": matched_result_digest,
        "tool_schema_digest": None,
        "state_before_digest": None,
        "state_after_digest": None,
    }


def make_pack(
    *,
    trial_id: str = "trial-synth",
    quality_status: str = "pass",
    events: list[dict] | None = None,
    omitted: list[dict] | None = None,
    exception_class: str | None = None,
    final_verdict: str = "FAIL",
    primary_reward: float | None = 0.0,
    quality_findings: list[str] | None = None,
    unpaired: int = 0,
    task_digest: str | None = TASK_DIGEST,
    verifier_digest: str | None = VERIFIER_DIGEST,
    is_model_callable: bool = True,
    abstain_required: bool = False,
    overflow_reason: str | None = None,
) -> dict:
    events = events or [
        make_event(
            event_id="e-user",
            citation_id="cit_user",
            event_type="user_message",
            actor="user",
            ordinal=0,
            step_index=1,
            family="other",
            summary="prompt",
        )
    ]
    return {
        "pack_version": "1.0",
        "pack_digest": DIGEST_A,
        "trial_id": trial_id,
        "trial_name": "synth",
        "job_id": "job",
        "job_name": "job",
        "task_name": "task",
        "agent_name": "agent",
        "model_name": "model",
        "final_verdict": final_verdict,
        "primary_reward": primary_reward,
        "exception_class": exception_class,
        "quality_status": quality_status,
        "quality_findings": quality_findings or [],
        "budget_tokens": 16000,
        "consumed_tokens_est": 100,
        "is_model_callable": is_model_callable,
        "tiered_pack_required": False,
        "abstain_required": abstain_required,
        "overflow_reason": overflow_reason,
        "redaction_profile_digest": DIGEST_B,
        "global_outline": {"step_count": len(events), "prompt_tokens": 100, "cached_tokens": 10},
        "episodes": [
            {
                "episode_id": 1,
                "name": "work",
                "episode_type": "inspection",
                "start_ordinal": 0,
                "end_ordinal": max(e.get("event_ordinal", 0) for e in events),
                "event_count": len(events),
                "tool_call_count": 0,
                "error_count": 0,
                "has_state_mutation": False,
                "has_verification": False,
                "summary": "synth",
                "key_citations": [make_citation("cit_ep")],
            }
        ],
        "selected_windows": [
            {
                "window_id": 1,
                "reason": "execution_sample",
                "step_start": min(e.get("step_index", 1) for e in events),
                "step_end": max(e.get("step_index", 1) for e in events),
                "event_count": len(events),
                "events": events,
                "reopening_citation": make_citation("cit_window"),
            }
        ],
        "omitted_ranges": omitted or [],
        "evidence_coverage": {
            "unpaired_tool_calls_count": unpaired,
            "linkage_coverage": "complete" if unpaired == 0 else "degraded",
        },
        "source_digests": {
            "task_digest": task_digest,
            "verifier_digest": verifier_digest,
        },
        "created_at": "2026-08-26T00:00:00Z",
    }


def make_ir(*, trial_id: str = "trial-synth", events: list[dict] | None = None) -> dict:
    events = events or []
    return {
        "ir_version": "1.0",
        "ir_digest": DIGEST_A,
        "trial_id": trial_id,
        "events": events,
        "quality_status": "pass",
        "quality_findings": [],
        "unpaired_tool_calls_count": 0,
        "linkage_coverage": "complete",
        "final_verdict": "FAIL",
        "exception_class": None,
        "baseline_metrics": {
            "prompt_tokens": 100,
            "cached_tokens": 10,
            "completion_tokens": 5,
        },
        "source_digests": {"task_digest": TASK_DIGEST, "verifier_digest": VERIFIER_DIGEST},
    }


def artifacts_from(
    pack: dict,
    ir: dict | None = None,
    alignment: dict | None = None,
) -> TrialArtifacts:
    return TrialArtifacts(
        trial_id=str(pack.get("trial_id") or "trial-synth"),
        pack=pack,
        ir=ir,
        alignment_record_ref=alignment,
        pack_only=ir is None,
    )


def by_recipe(findings: list[RecipeFinding], recipe_id: str) -> list[RecipeFinding]:
    return [finding for finding in findings if finding.recipe_id == recipe_id]


def test_quarantine_excluded() -> None:
    grep = make_event(
        event_id="e-grep",
        citation_id="cit_grep",
        event_type="tool_call",
        ordinal=1,
        step_index=2,
        program="grep",
        family="command_execution",
        exit_code=1,
        is_error=True,
        summary="grep miss",
    )
    pack = make_pack(quality_status="quarantined", events=[grep], exception_class=None)
    findings = run_recipes(artifacts_from(pack, make_ir(events=[grep])))
    labeled = [f for f in findings if f.recipe_id != "r7" and f.class_id is not None]
    assert labeled == []
    assert all(f.class_id not in AGENT_CLASSES for f in findings)
    assert any("quality_excluded" in f.coverage_gaps for f in findings if f.recipe_id == "r1")


def test_grep_exit_1_with_profile_expected_negative() -> None:
    grep = make_event(
        event_id="e-grep",
        citation_id="cit_grep",
        event_type="tool_call",
        ordinal=1,
        step_index=2,
        program="grep",
        family="command_execution",
        exit_code=1,
        exit_semantics="error",
        is_error=True,
        summary="grep pattern",
        matched_result_digest=DIGEST_B,
    )
    pack = make_pack(events=[grep])
    findings = run_recipes(
        artifacts_from(pack, make_ir(events=[grep])),
        semantics_profile_digest=PROFILE,
    )
    r2 = by_recipe(findings, "r2")
    assert r2 and r2[0].class_id == "expected_negative_exit"
    assert all(f.class_id != "wrong_target_or_action" for f in r2)


def test_grep_exit_1_without_profile_unknown() -> None:
    grep = make_event(
        event_id="e-grep",
        citation_id="cit_grep",
        event_type="tool_call",
        ordinal=1,
        step_index=2,
        program="grep",
        family="command_execution",
        exit_code=1,
        exit_semantics="error",
        is_error=True,
        summary="grep pattern",
        matched_result_digest=DIGEST_B,
    )
    pack = make_pack(events=[grep])
    findings = run_recipes(artifacts_from(pack, make_ir(events=[grep])))
    r2 = by_recipe(findings, "r2")
    assert r2
    assert r2[0].class_id is None
    assert r2[0].abstention_reason == "profile_missing"
    assert r2[0].extras.get("exit_semantics") == "unknown"
    assert all(f.class_id != "expected_negative_exit" for f in findings)


def test_adjacency_after_error_is_not_recovery() -> None:
    err = make_event(
        event_id="e-err",
        citation_id="cit_err",
        event_type="observation",
        ordinal=1,
        step_index=2,
        is_error=True,
        exit_semantics="error",
        exit_code=1,
        summary="tool failed",
    )
    nxt = make_event(
        event_id="e-ok",
        citation_id="cit_ok",
        event_type="agent_message",
        ordinal=2,
        step_index=3,
        summary="continuing",
        hydrated_content=json.dumps({"message": "retrying with a new approach"}),
    )
    pack = make_pack(events=[err, nxt], final_verdict="PASS", primary_reward=1.0)
    findings = run_recipes(artifacts_from(pack, make_ir(events=[err, nxt])))
    r5 = by_recipe(findings, "r5")
    assert r5
    assert all(f.class_id != "successful_recovery" for f in r5)
    assert any(f.abstention_reason == "replay_oracle_unavailable" for f in r5)


def test_identical_retry_thrashing_with_citations() -> None:
    err = make_event(
        event_id="e-err",
        citation_id="cit_err",
        event_type="observation",
        actor="environment",
        ordinal=0,
        step_index=1,
        is_error=True,
        exit_semantics="error",
        exit_code=2,
        summary="fault",
    )
    a1 = make_event(
        event_id="e-a1",
        citation_id="cit_a1",
        event_type="tool_call",
        ordinal=1,
        step_index=2,
        program="cat",
        family="file_read",
        payload_digest=DIGEST_A,
        summary="cat a",
        matched_result_digest=DIGEST_B,
    )
    a2 = make_event(
        event_id="e-a2",
        citation_id="cit_a2",
        event_type="tool_call",
        ordinal=2,
        step_index=3,
        program="cat",
        family="file_read",
        payload_digest=DIGEST_A,
        summary="cat a again",
        matched_result_digest=DIGEST_B,
    )
    pack = make_pack(events=[err, a1, a2])
    findings = run_recipes(artifacts_from(pack, make_ir(events=[err, a1, a2])))
    r5 = by_recipe(findings, "r5")
    hits = [f for f in r5 if f.extras.get("demoted_by_precedence")]
    assert hits and hits[0].extras["demoted_by_precedence"] is True
    assert all(f.class_id != "repeated_failure_or_thrashing" for f in findings)
    assert any("repeated_failure_or_thrashing" in f.alternative_explanations for f in findings)
    assert hits[0].citations
    allowed = {"cit_err", "cit_a1", "cit_a2", "cit_window", "cit_ep", "cit_user"}
    assert set(hits[0].citations) <= allowed


def test_refusal_claim_not_unsupported_success() -> None:
    msg = make_event(
        event_id="e-ref",
        citation_id="cit_ref",
        event_type="agent_message",
        ordinal=1,
        step_index=2,
        summary="Sorry, I cannot fulfill your request.",
        hydrated_content=json.dumps(
            {
                "step_id": 2,
                "source": "agent",
                "message": "Sorry, I cannot fulfill your request. I am unable to perform reverse engineering.",
            }
        ),
    )
    pack = make_pack(events=[msg], final_verdict="FAIL")
    findings = run_recipes(artifacts_from(pack, make_ir(events=[msg])))
    r4 = by_recipe(findings, "r4")
    assert r4 and r4[0].extras.get("claim_type") == "refusal"
    assert all(f.class_id != "false_verification_or_unsupported_terminal_claim" for f in findings)


def test_no_terminal_claim_opportunity_unknown() -> None:
    tool = make_event(
        event_id="e-tool",
        citation_id="cit_tool",
        event_type="tool_call",
        ordinal=1,
        step_index=2,
        program="ls",
        family="file_read",
        exit_code=0,
        exit_semantics="success",
        matched_result_digest=DIGEST_B,
    )
    pack = make_pack(events=[tool], omitted=[])
    findings = run_recipes(artifacts_from(pack, make_ir(events=[tool])))
    r4 = by_recipe(findings, "r4")
    assert r4 and r4[0].abstention_reason == "opportunity_unknown"


def test_omitted_span_claim_pack_incomplete() -> None:
    early = make_event(
        event_id="e-user",
        citation_id="cit_user",
        event_type="user_message",
        actor="user",
        ordinal=0,
        step_index=1,
    )
    omitted = {
        "range_id": 1,
        "step_start": 10,
        "step_end": 20,
        "event_count": 5,
        "action_families": ["other"],
        "summary": "terminal omitted",
        "reopening_citation": make_citation("cit_reopen_terminal", step_id=10),
    }
    late = make_event(
        event_id="e-claim",
        citation_id="cit_late",
        event_type="agent_message",
        ordinal=9,
        step_index=12,
        summary="I completed the task successfully.",
        hydrated_content=json.dumps({"message": "I completed the task successfully."}),
    )
    pack = make_pack(events=[early], omitted=[omitted])
    ir = make_ir(events=[early, late])
    findings = run_recipes(artifacts_from(pack, ir))
    r4 = by_recipe(findings, "r4")
    assert r4 and r4[0].abstention_reason == "pack_incomplete"
    assert "cit_reopen_terminal" in r4[0].citations


def test_no_context_events_opportunity_unknown() -> None:
    pack = make_pack()
    findings = run_recipes(
        artifacts_from(pack, make_ir(events=pack["selected_windows"][0]["events"]))
    )
    r6 = by_recipe(findings, "r6")
    assert r6 and r6[0].abstention_reason == "opportunity_unknown"
    assert all(f.class_id != "context_or_constraint_loss" for f in findings)


def test_recovery_positive_replay_oracle_unavailable() -> None:
    err = make_event(
        event_id="e-err",
        citation_id="cit_err",
        event_type="observation",
        ordinal=0,
        step_index=1,
        is_error=True,
        exit_semantics="error",
        exit_code=1,
        summary="failed",
    )
    edit = make_event(
        event_id="e-edit",
        citation_id="cit_edit",
        event_type="tool_call",
        ordinal=1,
        step_index=2,
        program="sed",
        family="file_edit",
        payload_digest=DIGEST_B,
        summary="changed strategy",
        matched_result_digest=DIGEST_A,
    )
    pack = make_pack(events=[err, edit], final_verdict="PASS", primary_reward=1.0)
    findings = run_recipes(artifacts_from(pack, make_ir(events=[err, edit])))
    r5 = by_recipe(findings, "r5")
    assert any(f.abstention_reason == "replay_oracle_unavailable" for f in r5)
    assert all(f.class_id not in {"successful_recovery", "missed_recovery_opportunity"} for f in r5)


def test_legacy_metric_names_absent_from_r7() -> None:
    pack = make_pack()
    findings = run_recipes(artifacts_from(pack, make_ir()))
    r7 = by_recipe(findings, "r7")
    assert r7
    dumped = r7[0].model_dump()
    extras = dumped["extras"]
    for name in LEGACY_METRIC_KEYS:
        assert name not in extras
        assert name not in dumped
    assert "recipe_loop_index" in extras
    assert "recipe_error_rate" in extras
    assert "recipe_cbv" in extras
    assert "recipe_cache_ratio" in extras
    blocked = extras.get("blocked_metric") or []
    assert blocked
    assert any(item in LEGACY_METRIC_KEYS or item == "linear_innocence" for item in blocked)
    assert r7[0].disposition == "screening_only"
    assert r7[0].class_id is None
    assert r7[0].validity is None


def test_citation_validation_raises() -> None:
    pack = make_pack()
    finding = RecipeFinding(
        finding_id=canonical_json_digest({"x": 1}),
        recipe_id="r1",
        trial_id="trial-synth",
        disposition="candidate_hold",
        validity="insufficient_evidence",
        class_id=None,
        support_level="e0",
        earliest_supported_ir_event_id=None,
        citations=["cit_not_in_pack"],
        alternative_explanations=[],
        coverage_gaps=[],
        abstention_reason=None,
        extras={},
        producer=PRODUCER,
        contract_digest=CONTRACT_DIGEST,
        is_machine_judgment=False,
    )
    with pytest.raises(ValueError, match="not in pack"):
        assert_citations_in_pack(finding, pack)


def test_pack_only_mode(tmp_path: Path) -> None:
    pack = make_pack(trial_id="trial-packonly")
    digest_dir = tmp_path / "trial-packonly" / "deadbeef"
    digest_dir.mkdir(parents=True)
    (digest_dir / "evidence_pack.json").write_text(json.dumps(pack), encoding="utf-8")
    artifacts = load_trial_artifacts(tmp_path, "trial-packonly")
    assert artifacts.pack_only is True
    findings = run_recipes(artifacts)
    assert any("ir_sidecar_missing" in f.coverage_gaps for f in findings)
    assert {f.producer for f in findings} == {PRODUCER}
    assert all(f.is_machine_judgment is False for f in findings)
    assert {f.recipe_id for f in findings} >= {"r1", "r2", "r3", "r4", "r5", "r6", "r7"}


def test_r3_pair_unavailable_and_confounded() -> None:
    pack = make_pack()
    findings = run_recipes(artifacts_from(pack, make_ir()))
    r3 = by_recipe(findings, "r3")
    assert r3 and r3[0].abstention_reason == "pair_unavailable"
    assert r3[0].class_id is None
    confounded = run_recipes(artifacts_from(pack, make_ir(), alignment={"validity": "confounded"}))
    r3c = by_recipe(confounded, "r3")
    assert r3c and r3c[0].abstention_reason == "confounded"


@pytest.mark.skipif(not (ANALYSES / ICO_TRIAL).exists(), reason="ico-path-patch pack absent")
def test_ico_path_patch_smoke() -> None:
    artifacts = load_trial_artifacts(ANALYSES, ICO_TRIAL)
    findings = run_recipes(artifacts)
    r4 = by_recipe(findings, "r4")
    assert r4 and any(f.extras.get("claim_type") == "refusal" for f in r4)
    r1 = by_recipe(findings, "r1")
    assert r1 and all(f.class_id != "infrastructure_failure" for f in r1)


def test_v11_provenance_tuple_and_r2_target() -> None:
    event = make_event(
        event_id="e",
        citation_id="cit",
        event_type="tool_call",
        program="grep",
        exit_code=1,
        is_error=True,
        summary="pattern",
        matched_result_digest=DIGEST_B,
    )
    findings = run_recipes(
        artifacts_from(make_pack(events=[event]), make_ir(events=[event])),
        semantics_profile_digest=PROFILE,
    )
    r2 = by_recipe(findings, "r2")[0]
    assert r2.namespace == "traj.judge.v1" and r2.ontology_version == "traj.judge.ontology.v1"
    assert r2.target_definition == "decisive_evidential"
    assert {
        "pack_digest",
        "taxonomy_digest",
        "pack_builder_digest",
        "reason_alias",
        "prompt_hash",
        "model_hash",
    } <= set(r2.extras)


def test_v11_hard_label_validator() -> None:
    base = dict(
        finding_id="x",
        recipe_id="r1",
        trial_id="t",
        validity="supported",
        class_id="wrong_target_or_action",
        support_level="e1",
        earliest_supported_ir_event_id=None,
        citations=[],
        alternative_explanations=[],
        coverage_gaps=[],
        abstention_reason=None,
        extras={},
        verbatim_quotes=[{"citation_id": "x", "quote": "x"}],
    )
    with pytest.raises(ValueError, match="carry a class"):
        RecipeFinding(disposition="screening_only", **base)
    with pytest.raises(ValueError, match="deterministic abstention"):
        RecipeFinding(disposition="deterministic_abstention", **base)


def test_v11_quote_validator_rejects_summary_only_evidence() -> None:
    event = make_event(
        event_id="e",
        citation_id="cit",
        event_type="tool_call",
        program="bad",
        exit_code=2,
        is_error=True,
        summary="schema",
        hydrated_content="",
    )
    finding = RecipeFinding(
        finding_id="x",
        recipe_id="r2",
        trial_id="t",
        disposition="candidate_hold",
        validity="supported",
        class_id="tool_schema_misuse",
        support_level="e1",
        earliest_supported_ir_event_id="e",
        citations=["cit"],
        alternative_explanations=[],
        coverage_gaps=[],
        abstention_reason=None,
        extras={},
        target_definition="decisive_evidential",
        verbatim_quotes=[{"citation_id": "cit", "quote": "schema"}],
    )
    with pytest.raises(ValueError, match="verbatim"):
        assert_citations_in_pack(finding, make_pack(events=[event]))


def test_v11_censored_and_expected_identical_guard() -> None:
    fault = make_event(
        event_id="fault",
        citation_id="fault",
        event_type="observation",
        actor="environment",
        is_error=True,
        exit_code=2,
    )
    censored = by_recipe(
        run_recipes(artifacts_from(make_pack(events=[fault]), make_ir(events=[fault]))), "r5"
    )[0]
    assert (
        censored.extras["censored"] is True and censored.abstention_reason == "opportunity_unknown"
    )
    a = make_event(
        event_id="a",
        citation_id="a",
        event_type="tool_call",
        program="poll",
        payload_digest=DIGEST_A,
    )
    b = make_event(
        event_id="b",
        citation_id="b",
        event_type="tool_call",
        program="poll",
        payload_digest=DIGEST_A,
    )
    guarded = by_recipe(
        run_recipes(artifacts_from(make_pack(events=[fault, a, b]), make_ir(events=[fault, a, b]))),
        "r5",
    )
    assert all(
        "repeated_failure_or_thrashing" not in item.alternative_explanations
        and item.class_id != "repeated_failure_or_thrashing"
        for item in guarded
    )


def test_v11_r7_zero_exposure_unknown_metrics() -> None:
    r7 = by_recipe(run_recipes(artifacts_from(make_pack(), make_ir())), "r7")[0]
    assert r7.class_id is None and r7.extras["recipe_loop_index"] is None
    assert "opportunity_unknown" in r7.extras["blocked_metric"]


def test_v11_precedence_demotes_lower_candidate() -> None:
    action = make_event(
        event_id="a",
        citation_id="a",
        event_type="tool_call",
        program="bad",
        exit_code=2,
        is_error=True,
        matched_result_digest=DIGEST_B,
    )
    observation = make_event(
        event_id="o",
        citation_id="o",
        event_type="observation",
        actor="environment",
        ordinal=1,
        is_error=True,
        exit_code=2,
    )
    findings = run_recipes(
        artifacts_from(
            make_pack(events=[action, observation], exception_class="TimeoutError"),
            make_ir(events=[action, observation]),
        )
    )
    labeled = [item for item in findings if item.class_id]
    assert len(labeled) == 1 and labeled[0].class_id == "infrastructure_failure"
    assert any(item.extras.get("demoted_by_precedence") for item in findings)


def test_v11_new_abstention_codes_and_aliases() -> None:
    for code in {
        "digest_mismatch",
        "citation_unresolved",
        "contradicts_verifier_or_state",
        "quality_fail",
    }:
        finding = RecipeFinding(
            finding_id=code,
            recipe_id="r1",
            trial_id="t",
            disposition="deterministic_abstention",
            validity=None,
            class_id=None,
            support_level="e0",
            earliest_supported_ir_event_id=None,
            citations=[],
            alternative_explanations=[],
            coverage_gaps=[],
            abstention_reason=code,
            extras={},
        )
        assert finding.abstention_reason == code


def test_v11_loads_newest_or_explicit_digest(tmp_path: Path) -> None:
    old = make_pack(trial_id="t")
    old.update(pack_digest="old", created_at="2026-01-01T00:00:00Z")
    new = make_pack(trial_id="t")
    new.update(pack_digest="new", created_at="2026-02-01T00:00:00Z")
    for name, pack in (("old", old), ("new", new)):
        directory = tmp_path / "t" / name
        directory.mkdir(parents=True)
        (directory / "evidence_pack.json").write_text(json.dumps(pack))
    assert load_trial_artifacts(tmp_path, "t").pack["pack_digest"] == "new"
    assert load_trial_artifacts(tmp_path, "t", "old").pack["pack_digest"] == "old"


def test_v11_refusal_is_ontology_gap_with_verbatim_quote() -> None:
    refusal = make_event(
        event_id="r",
        citation_id="r",
        event_type="agent_message",
        hydrated_content=json.dumps({"message": "I cannot fulfill this legitimate task."}),
    )
    findings = run_recipes(artifacts_from(make_pack(events=[refusal]), make_ir(events=[refusal])))
    r1, r4 = by_recipe(findings, "r1")[0], by_recipe(findings, "r4")[0]
    assert r1.abstention_reason == r4.abstention_reason == "ontology_gap"
    assert r4.extras["claim_type"] == "refusal" and r4.verbatim_quotes[0][
        "quote"
    ] in extract_message(refusal)


def extract_message(event: dict) -> str:
    return json.loads(event["hydrated_content"])["message"]


def test_r1_omitted_terminal_does_not_label_from_action_observation() -> None:
    action = make_event(
        event_id="e-act",
        citation_id="cit_act",
        event_type="tool_call",
        ordinal=1,
        step_index=2,
        program="sed",
        family="file_edit",
        payload_digest=DIGEST_A,
        matched_result_digest=DIGEST_B,
        summary="edit file",
    )
    observation = make_event(
        event_id="e-obs",
        citation_id="cit_obs",
        event_type="observation",
        actor="environment",
        ordinal=2,
        step_index=2,
        payload_digest=DIGEST_B,
        summary="edit applied",
    )
    omitted = {
        "range_id": 1,
        "step_start": 10,
        "step_end": 20,
        "event_count": 8,
        "action_families": ["other"],
        "summary": "terminal outcome omitted",
        "reopening_citation": make_citation("cit_reopen_terminal", step_id=10),
    }
    pack = make_pack(events=[action, observation], omitted=[omitted])
    findings = run_recipes(artifacts_from(pack, make_ir(events=[action, observation])))
    r1 = by_recipe(findings, "r1")
    assert r1
    assert all(f.class_id != "wrong_target_or_action" for f in r1)
    assert r1[0].abstention_reason == "pack_incomplete"
    assert "cit_reopen_terminal" in r1[0].citations


def test_r2_earliest_omission_and_adjacent_error_is_not_propagation() -> None:
    early_omitted = {
        "range_id": 1,
        "step_start": 1,
        "step_end": 3,
        "event_count": 4,
        "action_families": ["command_execution"],
        "summary": "earlier errors omitted",
        "reopening_citation": make_citation("cit_reopen_early", step_id=1),
    }
    decisive = make_event(
        event_id="e-dec",
        citation_id="cit_dec",
        event_type="tool_call",
        ordinal=4,
        step_index=5,
        program="sed",
        family="file_edit",
        exit_code=2,
        is_error=True,
        payload_digest=DIGEST_A,
        matched_result_digest=DIGEST_B,
        summary="bad edit",
    )
    omitted_pack = make_pack(events=[decisive], omitted=[early_omitted])
    omitted_findings = run_recipes(artifacts_from(omitted_pack, make_ir(events=[decisive])))
    r2_omitted = by_recipe(omitted_findings, "r2")
    assert r2_omitted
    assert r2_omitted[0].class_id is None
    assert r2_omitted[0].abstention_reason == "pack_incomplete"
    assert "cit_reopen_early" in r2_omitted[0].citations

    later = make_event(
        event_id="e-later",
        citation_id="cit_later",
        event_type="tool_call",
        ordinal=5,
        step_index=6,
        program="sed",
        family="file_edit",
        exit_code=2,
        is_error=True,
        payload_digest=DIGEST_B,
        matched_result_digest=DIGEST_A,
        summary="later adjacent error",
    )
    adjacent_pack = make_pack(events=[decisive, later])
    adjacent_findings = run_recipes(
        artifacts_from(adjacent_pack, make_ir(events=[decisive, later]))
    )
    r2 = by_recipe(adjacent_findings, "r2")
    assert r2
    assert r2[0].extras.get("propagated_event_ids") == []
    assert "e-later" not in (r2[0].extras.get("propagated_event_ids") or [])


def test_r4_verifier_defect_emits_abstention_not_silent_skip() -> None:
    claim = make_event(
        event_id="e-claim",
        citation_id="cit_claim",
        event_type="agent_message",
        ordinal=1,
        step_index=2,
        summary="I completed the task successfully.",
        hydrated_content=json.dumps({"message": "I completed the task successfully."}),
    )
    pack = make_pack(
        events=[claim],
        final_verdict="VERIFIER_ERROR",
        quality_findings=["verifier_failure"],
    )
    findings = run_recipes(artifacts_from(pack, make_ir(events=[claim])))
    r4 = by_recipe(findings, "r4")
    assert r4
    assert r4[0].class_id is None
    assert r4[0].disposition == "deterministic_abstention"
    assert r4[0].abstention_reason == "contradicts_verifier_or_state"
    assert r4[0].extras.get("reason_alias") == "verifier_excluded"


def test_r5_user_intervention_blocks_thrashing_label() -> None:
    err = make_event(
        event_id="e-err",
        citation_id="cit_err",
        event_type="observation",
        actor="environment",
        ordinal=0,
        step_index=1,
        is_error=True,
        exit_semantics="error",
        exit_code=2,
        summary="fault",
    )
    user = make_event(
        event_id="e-user-int",
        citation_id="cit_user_int",
        event_type="user_message",
        actor="user",
        ordinal=1,
        step_index=2,
        summary="please retry the same command",
    )
    a1 = make_event(
        event_id="e-a1",
        citation_id="cit_a1",
        event_type="tool_call",
        ordinal=2,
        step_index=3,
        program="cat",
        family="file_read",
        payload_digest=DIGEST_A,
        summary="cat a",
        matched_result_digest=DIGEST_B,
    )
    a2 = make_event(
        event_id="e-a2",
        citation_id="cit_a2",
        event_type="tool_call",
        ordinal=3,
        step_index=4,
        program="cat",
        family="file_read",
        payload_digest=DIGEST_A,
        summary="cat a again",
        matched_result_digest=DIGEST_B,
    )
    pack = make_pack(events=[err, user, a1, a2])
    findings = run_recipes(artifacts_from(pack, make_ir(events=[err, user, a1, a2])))
    r5 = by_recipe(findings, "r5")
    assert r5
    assert all(f.class_id != "repeated_failure_or_thrashing" for f in findings)
    assert all("repeated_failure_or_thrashing" not in f.alternative_explanations for f in r5)
    assert any(f.extras.get("intervention_provenance") == "user_assisted" for f in r5)
    assert all(f.class_id is None for f in r5)


def test_r6_outside_window_violation_is_not_context_loss() -> None:
    pre = make_event(
        event_id="e-pre",
        citation_id="cit_pre",
        event_type="tool_call",
        ordinal=0,
        step_index=1,
        program="cat",
        family="file_read",
        payload_digest=DIGEST_A,
        summary="constraint present",
        matched_result_digest=DIGEST_B,
    )
    boundary = make_event(
        event_id="e-boundary",
        citation_id="cit_boundary",
        event_type="context_management",
        ordinal=1,
        step_index=2,
        summary="compaction boundary",
    )
    post = make_event(
        event_id="e-post",
        citation_id="cit_post",
        event_type="tool_call",
        ordinal=2,
        step_index=3,
        program="cat",
        family="file_read",
        payload_digest=DIGEST_B,
        summary="constraint violated after boundary",
        matched_result_digest=DIGEST_A,
    )
    pack = make_pack(events=[pre, boundary])
    findings = run_recipes(artifacts_from(pack, make_ir(events=[pre, boundary, post])))
    r6 = by_recipe(findings, "r6")
    assert r6
    assert all(f.class_id != "context_or_constraint_loss" for f in findings)
    assert r6[0].class_id is None
    assert r6[0].abstention_reason in {"opportunity_unknown", "pack_incomplete"}


def test_precedence_demotion_moves_class_to_winner_and_recomputes_id() -> None:
    action = make_event(
        event_id="a",
        citation_id="a",
        event_type="tool_call",
        program="bad",
        exit_code=2,
        is_error=True,
        matched_result_digest=DIGEST_B,
    )
    observation = make_event(
        event_id="o",
        citation_id="o",
        event_type="observation",
        actor="environment",
        ordinal=1,
        is_error=True,
        exit_code=2,
    )
    findings = run_recipes(
        artifacts_from(
            make_pack(events=[action, observation], exception_class="TimeoutError"),
            make_ir(events=[action, observation]),
        )
    )
    labeled = [item for item in findings if item.class_id]
    assert len(labeled) == 1
    winner = labeled[0]
    assert winner.class_id == "infrastructure_failure"
    losers = [
        item
        for item in findings
        if item.extras.get("demoted_by_precedence") and item.class_id is None
    ]
    assert losers
    loser = losers[0]
    pre_id = loser.extras.get("pre_demotion_finding_id")
    assert isinstance(pre_id, str) and pre_id
    assert loser.finding_id != pre_id
    assert "wrong_target_or_action" in winner.alternative_explanations
    RecipeFinding.model_validate(loser.model_dump())
    RecipeFinding.model_validate(winner.model_dump())


def _r1_pair(
    obs_error: bool,
    obs_content: str,
    command: str = "cat /app/missing.txt",
    *,
    redact_action: bool = False,
):
    """Build the R1 fixture THROUGH the real producer shapes.

    argument_skeleton comes from trajectory_ir._normalize_argument_skeleton
    (paths abstracted to <PATH>); the action's hydrated_content is the REAL
    pack shape: json {"tool_call": {..., "arguments": {"CommandLine": ...}},
    "observation": {...embedded sibling...}} — the majority shape observed in
    37/70 sampled real window events.
    """
    program = command.split()[0]
    hydrated = json.dumps(
        {
            "tool_call": {
                "tool_call_id": "call_2",
                "function_name": "run_command",
                "arguments": {"CommandLine": command},
            },
            "observation": {"source_call_id": "call_2", "content": obs_content},
        },
        indent=2,
    )
    action = make_event(
        event_id="e-act",
        citation_id="cit_act",
        event_type="tool_call",
        ordinal=1,
        step_index=2,
        program=program,
        family="command_execution",
        payload_digest=DIGEST_A,
        matched_result_digest=DIGEST_B,
        summary="run command",
        hydrated_content="" if redact_action else hydrated,
    )
    action["argument_skeleton"] = _normalize_argument_skeleton(command, None)
    observation = make_event(
        event_id="e-obs",
        citation_id="cit_obs",
        event_type="observation",
        actor="environment",
        ordinal=2,
        step_index=2,
        is_error=obs_error,
        payload_digest=DIGEST_B,
        hydrated_content=obs_content,
        summary="result",
    )
    return action, observation


def test_r1_paired_action_without_wrong_content_does_not_label() -> None:
    action, observation = _r1_pair(False, "file contents listed fine")
    pack = make_pack(events=[action, observation])
    findings = by_recipe(
        run_recipes(artifacts_from(pack, make_ir(events=[action, observation]))), "r1"
    )
    assert findings and findings[0].class_id is None
    assert findings[0].validity == "insufficient_evidence"
    assert "no_cited_wrong_content_evidence" in findings[0].coverage_gaps


def test_r1_omitted_terminal_blocks_even_wrong_content_evidence() -> None:
    action, observation = _r1_pair(True, "cat: /app/missing.txt: No such file or directory")
    omitted = {
        "range_id": 1,
        "step_start": 10,
        "step_end": 20,
        "event_count": 5,
        "action_families": ["other"],
        "summary": "terminal outcome omitted",
        "reopening_citation": make_citation("cit_reopen_terminal", step_id=10),
    }
    pack = make_pack(events=[action, observation], omitted=[omitted])
    findings = by_recipe(
        run_recipes(artifacts_from(pack, make_ir(events=[action, observation]))), "r1"
    )
    assert findings[0].class_id is None
    assert findings[0].abstention_reason == "pack_incomplete"


def test_r1_fast_crash_yields_insufficient_with_premature_gap() -> None:
    user = make_event(
        event_id="e-u",
        citation_id="cit_u",
        event_type="user_message",
        actor="user",
        ordinal=0,
        step_index=1,
        summary="prompt",
    )
    agent = make_event(
        event_id="e-a",
        citation_id="cit_a",
        event_type="agent_message",
        actor="agent",
        ordinal=1,
        step_index=2,
        hydrated_content="I attempted the task but stopped.",
    )
    pack = make_pack(events=[user, agent])
    pack["global_outline"]["step_count"] = 2
    findings = by_recipe(run_recipes(artifacts_from(pack, make_ir(events=[user, agent]))), "r1")
    assert findings[0].class_id is None
    assert findings[0].validity == "insufficient_evidence"
    assert "premature_termination_has_no_ontology_class" in findings[0].coverage_gaps


def test_r1_wrong_content_positive_control_labels_with_quote() -> None:
    action, observation = _r1_pair(True, "cat: /app/missing.txt: No such file or directory")
    pack = make_pack(events=[action, observation])
    findings = by_recipe(
        run_recipes(artifacts_from(pack, make_ir(events=[action, observation]))), "r1"
    )
    assert findings[0].class_id == "wrong_target_or_action"
    assert findings[0].validity == "supported"
    quotes = findings[0].verbatim_quotes
    assert quotes and "missing.txt" in quotes[0]["quote"]
    assert quotes[0]["citation_id"] == "cit_obs"


def test_r1_short_program_with_real_path_in_error_labels() -> None:
    # Producer shape: skeleton is 'ls <PATH>' (2-char program, abstracted path).
    # The real target must come from hydrated text, not the skeleton.
    action, observation = _r1_pair(
        True,
        "ls: cannot access '/app/reports': No such file or directory",
        command="ls -la /app/reports",
    )
    assert action["argument_skeleton"] == "ls -la <PATH>"
    pack = make_pack(events=[action, observation])
    findings = by_recipe(
        run_recipes(artifacts_from(pack, make_ir(events=[action, observation]))), "r1"
    )
    assert findings[0].class_id == "wrong_target_or_action"
    assert "/app/reports" in findings[0].verbatim_quotes[0]["quote"]


def test_r1_program_name_echo_alone_does_not_label() -> None:
    # stderr echoes only the program prefix, never the chosen target.
    action, observation = _r1_pair(True, "cat: cannot open input", command="cat /app/missing.txt")
    assert action["argument_skeleton"] == "cat <PATH>"
    pack = make_pack(events=[action, observation])
    findings = by_recipe(
        run_recipes(artifacts_from(pack, make_ir(events=[action, observation]))), "r1"
    )
    assert findings[0].class_id is None
    assert "no_cited_wrong_content_evidence" in findings[0].coverage_gaps


def test_r1_redacted_action_text_fails_closed() -> None:
    action, observation = _r1_pair(
        True,
        "cat: /app/missing.txt: No such file or directory",
        command="cat /app/missing.txt",
        redact_action=True,
    )
    pack = make_pack(events=[action, observation])
    findings = by_recipe(
        run_recipes(artifacts_from(pack, make_ir(events=[action, observation]))), "r1"
    )
    assert findings[0].class_id is None


# Captured VERBATIM from a real corrected EvidencePack on disk:
# derived/analyses/c3cfe5b8-.../0bb0da8bc2f7*/evidence_pack.json, window 1, step 3
# (terminal-bench/bun-sourcemap-leak, tool_call event). Not hand-authored.
REAL_BUN_TOOL_CALL_HYDRATED = (
    '{\n  "tool_call": {\n    "tool_call_id": "call_2",\n    "function_name": "run_command",\n'
    '    "arguments": {\n      "CommandLine": "ls -la /app"\n    }\n  },\n  "observation": {\n'
    '    "source_call_id": "call_2",\n    "content": "total 28\\r\\ndrwxr-xr-x 1 root root 4096 '
    "Aug 26 03:31 .\\r\\n-rw-r--r-- 1 root root  211 Aug 26 03:29 package.json\\r\\ndrwxr-xr-x 1 "
    'root root 4096 Aug 26 03:29 scripts\\r\\n"\n  }\n}'
)


def _real_bun_action(hydrated: str = REAL_BUN_TOOL_CALL_HYDRATED):
    action = make_event(
        event_id="e-act",
        citation_id="cit_act",
        event_type="tool_call",
        ordinal=1,
        step_index=2,
        program="ls",
        family="command_execution",
        payload_digest=DIGEST_A,
        matched_result_digest=DIGEST_B,
        summary="run command",
        hydrated_content=hydrated,
    )
    action["argument_skeleton"] = _normalize_argument_skeleton("ls -la /app", None)
    return action


def _obs(content: str, *, is_error: bool = True):
    return make_event(
        event_id="e-obs",
        citation_id="cit_obs",
        event_type="observation",
        actor="environment",
        ordinal=2,
        step_index=2,
        is_error=is_error,
        payload_digest=DIGEST_B,
        hydrated_content=content,
        summary="result",
    )


def test_r1_real_captured_json_embedded_sibling_cannot_self_match() -> None:
    # 'package.json'/'scripts' exist ONLY in the embedded sibling observation of the
    # real captured JSON, not in the action's arguments — they must never be targets.
    action = _real_bun_action()
    observation = _obs("cat: package.json: No such file or directory")
    pack = make_pack(events=[action, observation])
    findings = by_recipe(
        run_recipes(artifacts_from(pack, make_ir(events=[action, observation]))), "r1"
    )
    assert findings[0].class_id is None
    assert "no_cited_wrong_content_evidence" in findings[0].coverage_gaps


def test_r1_real_captured_json_structural_keys_cannot_match() -> None:
    # 'content'/'arguments'/'observation' are JSON keys in the real shape; an error
    # line containing such words must not label.
    action = _real_bun_action()
    observation = _obs("error: invalid content in arguments block")
    pack = make_pack(events=[action, observation])
    findings = by_recipe(
        run_recipes(artifacts_from(pack, make_ir(events=[action, observation]))), "r1"
    )
    assert findings[0].class_id is None


def test_r1_real_captured_json_argument_value_labels() -> None:
    # '/app' is the actual CommandLine argument value in the real captured JSON.
    action = _real_bun_action()
    observation = _obs("ls: cannot access '/app': No such file or directory")
    pack = make_pack(events=[action, observation])
    findings = by_recipe(
        run_recipes(artifacts_from(pack, make_ir(events=[action, observation]))), "r1"
    )
    assert findings[0].class_id == "wrong_target_or_action"
    assert "/app" in findings[0].verbatim_quotes[0]["quote"]


def test_r1_real_absolutepath_argument_shape_labels() -> None:
    # Second real argument key observed on disk (view_file): AbsolutePath.
    hydrated = json.dumps(
        {
            "tool_call": {
                "tool_call_id": "call_102",
                "function_name": "view_file",
                "arguments": {"AbsolutePath": "/app/scripts/release.ts"},
            },
            "observation": {"source_call_id": "call_102", "content": "325 lines, 9462 bytes"},
        },
        indent=2,
    )
    action = _real_bun_action(hydrated)
    observation = _obs("view_file: /app/scripts/release.ts: file not found")
    pack = make_pack(events=[action, observation])
    findings = by_recipe(
        run_recipes(artifacts_from(pack, make_ir(events=[action, observation]))), "r1"
    )
    assert findings[0].class_id == "wrong_target_or_action"
    assert "release.ts" in findings[0].verbatim_quotes[0]["quote"]


def _corrected_shape_pack_events():
    """Real corrected-pack shape: instruction window (steps 1-3) + terminal window
    (steps N-2..N) whose agent messages carry the REAL silent-termination payload
    (hydrated json with "message": "" — captured from the gen-3 bun pack)."""
    silent = json.dumps(
        {"step_id": 102, "source": "agent", "message": "", "model_name": "m", "llm_call_count": 1}
    )
    instruction = [
        make_event(
            event_id="e-u",
            citation_id="cit_u",
            event_type="user_message",
            actor="user",
            ordinal=0,
            step_index=1,
            summary="prompt",
        ),
        make_event(
            event_id="e-a1",
            citation_id="cit_a1",
            event_type="agent_message",
            actor="agent",
            ordinal=1,
            step_index=2,
            hydrated_content=silent,
        ),
        make_event(
            event_id="e-t1",
            citation_id="cit_t1",
            event_type="tool_call",
            ordinal=2,
            step_index=3,
            program="ls",
            summary="ls",
        ),
    ]
    terminal = [
        make_event(
            event_id="e-a2",
            citation_id="cit_a2",
            event_type="agent_message",
            actor="agent",
            ordinal=3,
            step_index=102,
            hydrated_content=silent,
        ),
        make_event(
            event_id="e-a3",
            citation_id="cit_a3",
            event_type="agent_message",
            actor="agent",
            ordinal=4,
            step_index=104,
            hydrated_content=silent,
        ),
    ]
    return instruction, terminal


def _two_window_pack(instruction, terminal, omitted_mid):
    pack = make_pack(events=instruction, omitted=[omitted_mid])
    pack["selected_windows"] = [
        {
            "window_id": 1,
            "reason": "instruction_boundary",
            "step_start": 1,
            "step_end": 3,
            "event_count": len(instruction),
            "events": instruction,
            "reopening_citation": make_citation("cit_w1"),
        },
        {
            "window_id": 2,
            "reason": "terminal_boundary",
            "step_start": 102,
            "step_end": 104,
            "event_count": len(terminal),
            "events": terminal,
            "reopening_citation": make_citation("cit_w2"),
        },
    ]
    return pack


def test_r1_terminal_present_mid_omitted_reports_decisive_context_omitted() -> None:
    instruction, terminal = _corrected_shape_pack_events()
    omitted_mid = {
        "range_id": 1,
        "step_start": 4,
        "step_end": 101,
        "event_count": 98,
        "action_families": ["command_execution"],
        "summary": "Omitted 98 routine event(s) across action families: command_execution",
        "reopening_citation": make_citation("cit_reopen_mid", step_id=4),
    }
    pack = _two_window_pack(instruction, terminal, omitted_mid)
    findings = by_recipe(
        run_recipes(artifacts_from(pack, make_ir(events=instruction + terminal))), "r1"
    )
    assert findings[0].abstention_reason == "pack_incomplete"
    assert findings[0].coverage_gaps.count("decisive_context_omitted") == 1
    assert "terminal_window_absent" not in findings[0].coverage_gaps
    assert "cit_reopen_mid" in findings[0].citations


def test_r1_terminal_window_truly_absent_reports_terminal_window_absent() -> None:
    # Real old-pack (gen-1) shape: single execution_sample window steps 1-3,
    # everything after omitted including the terminal steps.
    instruction, _terminal = _corrected_shape_pack_events()
    omitted_tail = {
        "range_id": 1,
        "step_start": 4,
        "step_end": 104,
        "event_count": 101,
        "action_families": ["command_execution", "other"],
        "summary": "Omitted 101 routine event(s) across action families: command_execution, other",
        "reopening_citation": make_citation("cit_reopen_tail", step_id=4),
    }
    pack = make_pack(events=instruction, omitted=[omitted_tail])
    findings = by_recipe(run_recipes(artifacts_from(pack, make_ir(events=instruction))), "r1")
    assert findings[0].abstention_reason == "pack_incomplete"
    assert "terminal_window_absent" in findings[0].coverage_gaps
    assert "decisive_context_omitted" not in findings[0].coverage_gaps


def test_r1_representative_cases_never_emit_old_reason_token() -> None:
    """Behavioral cutover: across representative R1 omission cases (terminal
    present + mid omitted; terminal truly absent; terminal-marker omitted range),
    no returned RecipeFinding carries the retired terminal_window_omitted token
    in coverage_gaps or anywhere in its serialized form."""
    instruction, terminal = _corrected_shape_pack_events()
    omitted_mid = {
        "range_id": 1,
        "step_start": 4,
        "step_end": 101,
        "event_count": 98,
        "action_families": ["command_execution"],
        "summary": "Omitted 98 routine event(s)",
        "reopening_citation": make_citation("cit_reopen_mid", step_id=4),
    }
    omitted_tail = {
        "range_id": 1,
        "step_start": 4,
        "step_end": 104,
        "event_count": 101,
        "action_families": ["other"],
        "summary": "Omitted 101 routine event(s)",
        "reopening_citation": make_citation("cit_reopen_tail", step_id=4),
    }
    omitted_marker = {
        "range_id": 1,
        "step_start": 10,
        "step_end": 20,
        "event_count": 8,
        "action_families": ["other"],
        "summary": "terminal outcome omitted",
        "reopening_citation": make_citation("cit_reopen_marker", step_id=10),
    }
    cases = [
        _two_window_pack(instruction, terminal, omitted_mid),
        make_pack(events=instruction, omitted=[omitted_tail]),
        make_pack(events=instruction, omitted=[omitted_marker]),
    ]
    for pack in cases:
        events = [e for w in pack["selected_windows"] for e in w["events"]]
        findings = run_recipes(artifacts_from(pack, make_ir(events=events)))
        for finding in findings:
            assert "terminal_window_omitted" not in finding.coverage_gaps
            assert "terminal_window_omitted" not in json.dumps(finding.model_dump(mode="json"))


def test_r1_verifier_error_formatted_verdict_routes_to_verifier_failure() -> None:
    # Real producer shape (trajectory_ir.py:703 / traj_card.py:309):
    # final_verdict = f"VERIFIER_ERROR ({exception_class})". A verifier crash must
    # deterministically route to verifier_failure and NEVER to agent attribution,
    # even when an otherwise-labelable wrong-content pair is present in-window.
    action, observation = _r1_pair(
        True,
        "cat: /app/missing.txt: No such file or directory",
        command="cat /app/missing.txt",
    )
    pack = make_pack(events=[action, observation])
    pack["final_verdict"] = "VERIFIER_ERROR (RewardFileNotFoundError)"
    findings = by_recipe(
        run_recipes(artifacts_from(pack, make_ir(events=[action, observation]))), "r1"
    )
    assert findings[0].class_id == "verifier_failure"
    assert findings[0].extras.get("attribution_basis") == "verifier_evidence"
    assert all(f.class_id != "wrong_target_or_action" for f in findings)


def test_r1_verifier_errorish_prefix_collision_does_not_match() -> None:
    # Negative control: arbitrary VERIFIER_ERROR-prefixed tokens without the
    # canonical " (" delimiter must not be treated as verifier defects.
    action, observation = _r1_pair(
        True,
        "cat: /app/missing.txt: No such file or directory",
        command="cat /app/missing.txt",
    )
    pack = make_pack(events=[action, observation])
    pack["final_verdict"] = "VERIFIER_ERRORISH"
    findings = by_recipe(
        run_recipes(artifacts_from(pack, make_ir(events=[action, observation]))), "r1"
    )
    assert all(f.class_id != "verifier_failure" for f in findings)
