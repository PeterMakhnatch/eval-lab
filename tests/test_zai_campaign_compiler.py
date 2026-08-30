"""Focused unit tests for the Z.ai Action Memory campaign compiler and runner.

Covers:
- Default definition construction and canonical digest computation.
- Deterministic 38-trial manifest compilation (36 phase A + 2 phase B).
- Budget admission refusal on exceeded token ceilings and unmeasured doses.
- Model allowlist enforcement and Highspeed access-gated classification.
- Provider-only auth staging and non-secret auth shape (no credential leakage).
- Resumable job identity and durable state recovery.
- Cleanup in ``finally`` for staged auth and process locks.
- Conditional phase-B gating on phase-A ceiling adherence.
- Matched-contrast pairing and separate retrieval fidelity reporting.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.zai_campaign import (
    ALLOWED_MODELS,
    CAMPAIGN_ID,
    HIGHSPEED_SELECTOR,
    PHASE_A_CEILING_INPUT_TOKENS,
    PHASE_A_PROJECTED_INPUT_TOKENS,
    PHASE_A_TRIALS,
    PHASE_B_TRIALS,
    PROVIDER_TOKEN_BUDGET,
    TOTAL_TRIALS,
    ConcurrencyGate,
    RecorderTrialRunner,
    RetrievalFidelity,
    TrialOutcome,
    ZaiAttemptRecord,
    ZaiAuthShape,
    ZaiCampaignAuthError,
    ZaiCampaignBudgetError,
    ZaiCampaignError,
    ZaiCampaignModelError,
    ZaiCampaignPreconditionError,
    ZaiCampaignRunner,
    ZaiCampaignState,
    ZaiCampaignStatus,
    ZaiCampaignTaskError,
    ZaiPhaseSpec,
    ZaiTrial,
    build_campaign_definition,
    build_default_definition,
    campaign_design_digest,
    check_budget_admission,
    check_lane_preconditions,
    classify_attempt,
    classify_verifier_retrieval,
    compile_campaign,
    default_campaign_limits,
    describe_auth_shape,
    filter_zai_auth,
    is_scored,
    matched_contrast_report,
    pairing_key_of,
    read_opencode_auth,
    stage_provider_auth,
    validate_model,
)


# --------------------------------------------------------------------------- #
# Helpers & Fixtures
# --------------------------------------------------------------------------- #


def _make_dummy_task_root(tmp_path: Path) -> Path:
    """Create stub .task directories for every cell in the 38-trial design."""
    root = tmp_path / "tasks"
    root.mkdir(parents=True, exist_ok=True)
    definition = build_default_definition()
    # Create phase-A task directories
    for dose in definition.phase("a").doses:
        for seed in definition.phase("a").seeds:
            for arm in definition.phase("a").arms:
                cell = f"dl-{arm.replace('_', '-')}-{dose}-s{seed}"
                (root / f"{cell}.task").mkdir(parents=True, exist_ok=True)
    # Create phase-B task directories
    for dose in definition.phase("b").doses:
        for seed in definition.phase("b").seeds:
            for arm in definition.phase("b").arms:
                cell = f"dl-{arm.replace('_', '-')}-{dose}-s{seed}"
                (root / f"{cell}.task").mkdir(parents=True, exist_ok=True)
    return root


def _make_mixed_auth_doc(tmp_path: Path) -> Path:
    """Auth document with both Z.ai and non-Z.ai provider credentials."""
    auth_file = tmp_path / "auth.json"
    content = {
        "codex": {
            "type": "oauth",
            "access_token": "secret-codex-token-12345",
            "refresh_token": "secret-codex-refresh-67890",
        },
        "anthropic": {
            "type": "api_key",
            "key": "secret-anthropic-key-abcde",
        },
        "zai": {
            "type": "token",
            "access_token": "secret-zai-token-xyz987",
            "endpoint": "https://api.z.ai/v1",
        },
    }
    auth_file.write_text(json.dumps(content), encoding="utf-8")
    return auth_file


def _make_non_zai_auth_doc(tmp_path: Path) -> Path:
    """Auth document without any Z.ai provider key."""
    auth_file = tmp_path / "auth_no_zai.json"
    content = {
        "codex": {"type": "oauth", "access_token": "secret-codex-token"},
        "cursor": {"type": "session", "session_id": "secret-cursor-session"},
    }
    auth_file.write_text(json.dumps(content), encoding="utf-8")
    return auth_file


# --------------------------------------------------------------------------- #
# 0. Default Definition Construction & Digest Invariants
# --------------------------------------------------------------------------- #


def test_build_default_definition_validates_canonical_digest() -> None:
    definition = build_default_definition()
    assert definition.campaign_id == CAMPAIGN_ID
    assert definition.design_digest.startswith("sha256:")
    assert definition.design_digest == campaign_design_digest(definition)
    assert definition.lane_model in ALLOWED_MODELS
    assert len(definition.phases) == 2
    assert definition.phase("a").name == "a"
    assert definition.phase("b").name == "b"


def test_build_campaign_definition_custom_limits() -> None:
    custom_limits = default_campaign_limits(
        host_isolation_enforced=True,
        credential_proxy_holds_secret=True,
        max_concurrency=2,
    )
    definition = build_campaign_definition(limits=custom_limits)
    assert definition.limits.host_isolation_enforced is True
    assert definition.limits.credential_proxy_holds_secret is True
    assert definition.limits.max_concurrency == 2
    assert definition.design_digest == campaign_design_digest(definition)


# --------------------------------------------------------------------------- #
# 1. Deterministic Manifest Compilation
# --------------------------------------------------------------------------- #


def test_manifest_compilation_deterministic_38_trials(tmp_path: Path) -> None:
    task_root = _make_dummy_task_root(tmp_path)
    definition = build_default_definition()
    manifest_1 = compile_campaign(definition, task_root=task_root)
    manifest_2 = compile_campaign(definition, task_root=task_root)

    # Invariants
    assert manifest_1.campaign_id == CAMPAIGN_ID
    assert len(manifest_1.phase_a) == PHASE_A_TRIALS  # 36
    assert len(manifest_1.phase_b) == PHASE_B_TRIALS  # 2
    assert manifest_1.total_trials == TOTAL_TRIALS  # 38
    assert manifest_1.manifest_digest == manifest_2.manifest_digest

    # Deterministic trial ordering and properties
    for trial in manifest_1.trials:
        assert trial.phase in {"a", "b"}
        assert trial.trial_id.startswith(f"{trial.phase}-")
        assert trial.job_identity.startswith("zai-am-campaign-")
        assert trial.prompt_token_ceiling > 0
        assert Path(trial.task_path).is_dir()


def test_manifest_compilation_refuses_missing_task_directory(tmp_path: Path) -> None:
    task_root = tmp_path / "empty_tasks"
    task_root.mkdir()
    definition = build_default_definition()
    with pytest.raises(ZaiCampaignTaskError, match="missing task directory"):
        compile_campaign(definition, task_root=task_root)


# --------------------------------------------------------------------------- #
# 2. Budget Admission (Fail-Closed Refusals)
# --------------------------------------------------------------------------- #


def test_budget_admission_accepts_certified_defaults() -> None:
    definition = build_default_definition()
    # Must not raise
    check_budget_admission(definition)


def test_budget_admission_refuses_exceeded_phase_a_ceiling() -> None:
    definition = build_default_definition()
    # Tighten phase A ceiling below the 6,291,672 projection
    low_phase_a = ZaiPhaseSpec(
        name="a",
        doses=definition.phase("a").doses,
        arms=definition.phase("a").arms,
        seeds=definition.phase("a").seeds,
        reps=definition.phase("a").reps,
        ceiling_input_tokens=5_000_000,  # Below 6,291,672
    )
    over_budget_def = build_campaign_definition(
        phases=(low_phase_a, definition.phase("b"))
    )
    with pytest.raises(ZaiCampaignBudgetError, match="exceeding its 5000000 ceiling"):
        check_budget_admission(over_budget_def)


def test_budget_admission_refuses_unmeasured_dose_in_phase_a() -> None:
    definition = build_default_definition()
    # Attempt to inject 128k (unmeasured) into phase A
    invalid_phase_a = ZaiPhaseSpec(
        name="a",
        doses=(4096, 16384, 131072),  # 131072 is unmeasured
        arms=definition.phase("a").arms,
        seeds=definition.phase("a").seeds,
        reps=definition.phase("a").reps,
        ceiling_input_tokens=PHASE_A_CEILING_INPUT_TOKENS,
    )
    invalid_def = build_campaign_definition(
        phases=(invalid_phase_a, definition.phase("b"))
    )
    with pytest.raises(ZaiCampaignBudgetError, match="unmeasured"):
        check_budget_admission(invalid_def)


def test_budget_admission_refuses_low_provider_trial_ceiling() -> None:
    low_limits = default_campaign_limits(max_concurrency=1).model_copy(
        update={"max_trials": 20}
    )
    invalid_def = build_campaign_definition(limits=low_limits)
    with pytest.raises(ZaiCampaignBudgetError, match="admits fewer than the 38 runnable trials"):
        check_budget_admission(invalid_def)


# --------------------------------------------------------------------------- #
# 3. Model Allowlist & Attempt Classification
# --------------------------------------------------------------------------- #


def test_model_allowlist_admits_certified_models() -> None:
    for model in ALLOWED_MODELS:
        assert validate_model(model) == model


@pytest.mark.parametrize(
    "invalid_model",
    [
        "openai/gpt-4o",
        "anthropic/claude-3-5-sonnet",
        "zai-coding-plan/glm-4",
        "cursor/grok-4.6",
    ],
)
def test_model_allowlist_rejects_unapproved_models(invalid_model: str) -> None:
    with pytest.raises(ZaiCampaignModelError, match="outside the Z.ai Coding Plan allowlist"):
        validate_model(invalid_model)


def test_highspeed_classified_as_provider_access_not_reward_zero() -> None:
    # Highspeed failure is classified as non-scored provider-access
    outcome = {
        "model": HIGHSPEED_SELECTOR,
        "exception_type": "HarborError",
        "exception_message": "HTTP 429 subscription plan does not yet include access",
        "reward_present": False,
    }
    kind, reason = classify_attempt(outcome)
    assert kind == "provider_access_refused"
    assert not is_scored(kind)
    assert reason is not None


def test_harness_infra_exception_classified_non_scored() -> None:
    outcome = {
        "exception_class": "AgentTimeoutError",
        "exception_message": "Agent step loop timed out after 1800s",
        "reward_present": False,
    }
    kind, reason = classify_attempt(outcome)
    assert kind == "harness_infra_exception"
    assert not is_scored(kind)


def test_scored_attempt_enters_denominator() -> None:
    outcome = {
        "reward_present": True,
        "reward": 1.0,
    }
    kind, reason = classify_attempt(outcome)
    assert kind == "scored"
    assert is_scored(kind)
    assert reason is None


# --------------------------------------------------------------------------- #
# 4. Auth Filtering & Staging (Non-Secret Shape)
# --------------------------------------------------------------------------- #


def test_auth_filtering_preserves_zai_only_without_leaking_values(tmp_path: Path) -> None:
    mixed_auth = _make_mixed_auth_doc(tmp_path)
    staged_dest = tmp_path / "staged" / "zai-auth.json"

    staged_path, shape = stage_provider_auth(mixed_auth, staged_dest)

    assert staged_path.is_file()
    assert shape.zai_present is True
    assert shape.retained_provider_keys == ("zai",)
    assert shape.retained_entry_count == 1

    # Verify staged document contains ONLY the zai key
    staged_doc = json.loads(staged_path.read_text(encoding="utf-8"))
    assert set(staged_doc.keys()) == {"zai"}
    assert "codex" not in staged_doc
    assert "anthropic" not in staged_doc

    # Verify permissions are restrictive (0o600)
    mode = staged_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_auth_staging_fails_closed_when_no_zai_provider(tmp_path: Path) -> None:
    no_zai_auth = _make_non_zai_auth_doc(tmp_path)
    staged_dest = tmp_path / "staged" / "zai-auth.json"

    with pytest.raises(ZaiCampaignAuthError, match="no Z.ai provider entry"):
        stage_provider_auth(no_zai_auth, staged_dest)
    assert not staged_dest.exists()


def test_auth_shape_contains_no_secret_values(tmp_path: Path) -> None:
    mixed_auth = _make_mixed_auth_doc(tmp_path)
    doc = read_opencode_auth(mixed_auth)
    shape = describe_auth_shape(doc)
    redacted = shape.to_redacted()

    raw_json = json.dumps(redacted)
    assert "secret-" not in raw_json
    assert "token" not in raw_json.lower() or "token" in shape.schema_version


# --------------------------------------------------------------------------- #
# 5. Resumable Job Identity & Durable State
# --------------------------------------------------------------------------- #


def test_resume_behavior_picks_up_settled_trials(tmp_path: Path) -> None:
    task_root = _make_dummy_task_root(tmp_path)
    auth_file = _make_mixed_auth_doc(tmp_path)
    state_root = tmp_path / "state"
    definition = build_default_definition()

    runner_1 = ZaiCampaignRunner(
        definition=definition,
        task_root=task_root,
        auth_path=auth_file,
        state_root=state_root,
        runner=RecorderTrialRunner(kind="scored", reward=1.0, prompt_tokens=1000),
    )
    status_1 = runner_1.run(resume=False, dry_run=True)
    assert status_1.state == "complete"
    assert len(status_1.attempts) == TOTAL_TRIALS

    # A second run with resume=True reloads existing attempts
    mock_runner = RecorderTrialRunner()
    runner_2 = ZaiCampaignRunner(
        definition=definition,
        task_root=task_root,
        auth_path=auth_file,
        state_root=state_root,
        runner=mock_runner,
    )
    status_2 = runner_2.run(resume=True, dry_run=True)
    assert len(status_2.attempts) == TOTAL_TRIALS
    # No new trials should have been executed by mock_runner
    assert len(mock_runner.calls) == 0


def test_running_without_resume_on_partial_state_raises(tmp_path: Path) -> None:
    task_root = _make_dummy_task_root(tmp_path)
    auth_file = _make_mixed_auth_doc(tmp_path)
    state_root = tmp_path / "state"
    definition = build_default_definition()

    # Pre-write a partial state
    state = ZaiCampaignState(state_root, CAMPAIGN_ID, "sha256:" + "0" * 64)
    state.write(
        status=ZaiCampaignStatus(
            campaign_id=CAMPAIGN_ID,
            manifest_digest="sha256:" + "0" * 64,
            state="running",
            attempts=(),
        )
    )

    runner = ZaiCampaignRunner(
        definition=definition,
        task_root=task_root,
        auth_path=auth_file,
        state_root=state_root,
    )
    with pytest.raises(ZaiCampaignError, match="campaign already started; pass resume=True"):
        runner.run(resume=False)


# --------------------------------------------------------------------------- #
# 6. Cleanup in Finally
# --------------------------------------------------------------------------- #


class _FailingTrialRunner:
    """Simulates a trial execution failure."""

    def run_trial(self, trial: ZaiTrial, *, staged_auth_path: Path, attempt_id: str) -> TrialOutcome:
        raise RuntimeError("simulated agent crash during trial execution")


def test_cleanup_in_finally_removes_staged_auth_on_raise(tmp_path: Path) -> None:
    task_root = _make_dummy_task_root(tmp_path)
    auth_file = _make_mixed_auth_doc(tmp_path)
    state_root = tmp_path / "state"
    definition = build_default_definition()

    runner = ZaiCampaignRunner(
        definition=definition,
        task_root=task_root,
        auth_path=auth_file,
        state_root=state_root,
        runner=_FailingTrialRunner(),
    )

    with pytest.raises(RuntimeError, match="simulated agent crash"):
        runner.run(resume=False)

    # Confirm the staged auth file was unlinked in finally
    staging_file = state_root / CAMPAIGN_ID / ".staging" / "zai-auth.json"
    assert not staging_file.exists()


# --------------------------------------------------------------------------- #
# 7. Conditional Phase-B Canary Gating
# --------------------------------------------------------------------------- #


def test_conditional_phase_b_skipped_when_phase_a_exceeds_ceiling(tmp_path: Path) -> None:
    task_root = _make_dummy_task_root(tmp_path)
    auth_file = _make_mixed_auth_doc(tmp_path)
    state_root = tmp_path / "state"
    definition = build_default_definition()

    # Each phase-A trial reports 300,000 prompt tokens -> 36 * 300k = 10,800,000 (> 7M ceiling)
    runner = ZaiCampaignRunner(
        definition=definition,
        task_root=task_root,
        auth_path=auth_file,
        state_root=state_root,
        runner=RecorderTrialRunner(kind="scored", reward=1.0, prompt_tokens=300_000),
    )

    status = runner.run(resume=False)
    assert status.phase_b_skipped is True
    assert "exceeded its token ceiling" in str(status.phase_b_reason)
    # Only phase A trials should be present in status
    assert len(status.attempts) == PHASE_A_TRIALS


# --------------------------------------------------------------------------- #
# 8. Matched Contrast & Retrieval Fidelity Reporting
# --------------------------------------------------------------------------- #


def test_matched_contrast_report_pairing_fidelity(tmp_path: Path) -> None:
    task_root = _make_dummy_task_root(tmp_path)
    definition = build_default_definition()
    manifest = compile_campaign(definition, task_root=task_root)

    # Construct mock retrieval evidence for one trial
    evidence_by_trial = {
        "a-dl-neutral-padding-4096-s42-r1": {
            "expected_handles": ["h1", "h2", "h3"],
            "issued_handles": ["h1", "h2", "h4", "h2"],  # unknown h4, duplicate h2, omitted h3
        }
    }

    rows = matched_contrast_report(
        manifest,
        attempts=(),
        evidence_by_trial=evidence_by_trial,
    )

    assert len(rows) > 0
    # Find the row for (4096, seed 42)
    s42_row = next(r for r in rows if r.dose_bytes == 4096 and r.seed == 42)
    assert s42_row.unknown == 1  # h4
    assert s42_row.omitted == 1  # h3
    assert s42_row.duplicate == 1  # extra h2
    assert s42_row.order_fidelity is False


def test_matched_contrast_report_preserves_observed_fidelity_when_siblings_unobserved(tmp_path: Path) -> None:
    task_root = _make_dummy_task_root(tmp_path)
    definition = build_default_definition()
    manifest = compile_campaign(definition, task_root=task_root)

    # Trial 1 has perfect order fidelity; sibling trials (r2, semantic arms) have no evidence
    evidence_by_trial = {
        "a-dl-neutral-padding-4096-s42-r1": {
            "expected_handles": ["h1", "h2", "h3"],
            "issued_handles": ["h1", "h2", "h3"],
        }
    }

    rows = matched_contrast_report(
        manifest,
        attempts=(),
        evidence_by_trial=evidence_by_trial,
    )

    s42_row = next(r for r in rows if r.dose_bytes == 4096 and r.seed == 42)
    assert s42_row.order_fidelity is True
    assert s42_row.unknown == 0
    assert s42_row.omitted == 0
    assert s42_row.duplicate == 0


def test_matched_contrast_report_mixed_order_fidelity_resolves_false(tmp_path: Path) -> None:
    task_root = _make_dummy_task_root(tmp_path)
    definition = build_default_definition()
    manifest = compile_campaign(definition, task_root=task_root)

    # Trial 1 preserved order, but Trial 2 reordered handles
    evidence_by_trial = {
        "a-dl-neutral-padding-4096-s42-r1": {
            "expected_handles": ["h1", "h2", "h3"],
            "issued_handles": ["h1", "h2", "h3"],
        },
        "a-dl-semantic-distractor-4096-s42-r1": {
            "expected_handles": ["h1", "h2", "h3"],
            "issued_handles": ["h2", "h1", "h3"],  # reordered
        },
    }

    rows = matched_contrast_report(
        manifest,
        attempts=(),
        evidence_by_trial=evidence_by_trial,
    )

    s42_row = next(r for r in rows if r.dose_bytes == 4096 and r.seed == 42)
    assert s42_row.order_fidelity is False


def test_matched_contrast_report_all_unobserved_resolves_none(tmp_path: Path) -> None:
    task_root = _make_dummy_task_root(tmp_path)
    definition = build_default_definition()
    manifest = compile_campaign(definition, task_root=task_root)

    # No retrieval evidence provided for any trial
    rows = matched_contrast_report(manifest, attempts=(), evidence_by_trial={})

    for row in rows:
        assert row.order_fidelity is None
        assert row.unknown == 0
        assert row.omitted == 0
        assert row.duplicate == 0


def test_retrieval_fidelity_calculation() -> None:
    # Perfect retrieval
    perfect = classify_verifier_retrieval({
        "expected_handles": ["h1", "h2", "h3"],
        "issued_handles": ["h1", "h2", "h3"],
    })
    assert perfect.coverage_unique == 1.0
    assert perfect.unknown == 0
    assert perfect.omitted == 0
    assert perfect.duplicate == 0
    assert perfect.order_fidelity is True

    # Reordered retrieval
    reordered = classify_verifier_retrieval({
        "expected_handles": ["h1", "h2", "h3"],
        "issued_handles": ["h2", "h1", "h3"],
    })
    assert reordered.coverage_unique == 1.0
    assert reordered.order_fidelity is False


# --------------------------------------------------------------------------- #
# 9. Concurrency Gate
# --------------------------------------------------------------------------- #


def test_concurrency_gate_bounds_parallel_slots() -> None:
    gate = ConcurrencyGate(2)
    assert gate.limit == 2
    assert gate.active == 0

    with gate.slot():
        assert gate.active == 1
        with gate.slot():
            assert gate.active == 2
        assert gate.active == 1
    assert gate.active == 0
