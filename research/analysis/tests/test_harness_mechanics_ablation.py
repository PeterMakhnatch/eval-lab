"""Tests for Harness Mechanics Lab counterfactual ablation planning.

Defends the scientific boundaries and invariants required by HAR-20:
- Execution readiness & authorization invariants: execution_authorized is strictly False;
  proposals are analytical designs, not executable jobs or runner formats.
- No invented task IDs, profile IDs, or synthetic benchmark performance targets.
- Causal claim prevention: target explanations are labeled hypotheses, not proven causal facts;
  no pass-rate rankings or performance superiority claims.
- Evidence gate: no proposal without relevant diagnostic evidence. Empty trajectories,
  unresolved references, post-hoc redactions, or missing telemetry alone do not justify
  an ablation intervention. Unknown-only inputs yield no proposals.
- One changed mechanism: each proposal strictly isolates one variable under fixed task,
  model, reward, and budget conditions, with explicit required measurements (never conflating
  multiple mechanisms like shell persistence versus clipping into one factor).
- Preserves actual source paths, sha256 digests, and RFC6901 JSON pointer locators in
  evidence links; no arbitrary raw prompt, reasoning, or command strings.
- Deterministic output generation and safe handling of malformed inputs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

try:
    from harness_mechanics.ablation import (
        ARTIFACT_KIND,
        EXPECTED_ANALYSIS_KIND,
        SCHEMA_VERSION,
        STANDARD_MECHANISMS,
        build_ablation_plan,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness-mechanics"))
    from harness_mechanics.ablation import (
        ARTIFACT_KIND,
        EXPECTED_ANALYSIS_KIND,
        SCHEMA_VERSION,
        STANDARD_MECHANISMS,
        build_ablation_plan,
    )

_CATALOG_PATH = Path(__file__).resolve().parents[1] / "harness-mechanics" / "source-catalog.json"


def _make_observation(
    code: str = "TEST_CODE",
    step_id: int | None = 1,
    tool_call_id: str | None = "call_abc123",
    locator: str = "/steps/0/tool_calls/0",
    evidence_kind: str = "observed",
    summary: str = "Observed test pattern in step",
) -> dict[str, Any]:
    """Helper to create an observation dictionary matching diagnostic_api contract."""
    return {
        "code": code,
        "step_id": step_id,
        "tool_call_id": tool_call_id,
        "locator": locator,
        "evidence_kind": evidence_kind,
        "summary": summary,
    }


def _make_diagnostic(
    mechanism: str,
    observations: list[dict[str, Any]] | None = None,
    unknowns: list[str] | None = None,
) -> dict[str, Any]:
    """Helper to create a diagnostic dictionary for a mechanism."""
    return {
        "mechanism": mechanism,
        "observations": observations if observations is not None else [],
        "unknowns": unknowns if unknowns is not None else [],
    }


def _make_record(
    path: str = "runs/job1/trial1/trajectory.json",
    sha256: str | None = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    status: str = "analyzed",
    model: str | None = "deepseek-v4.1-flash",
    harness: str | None = "dsh-minimal",
    diagnostics: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Helper to create a record dictionary matching record_contract."""
    if diagnostics is None:
        diagnostics = {m: _make_diagnostic(m) for m in STANDARD_MECHANISMS}
    return {
        "source": {
            "path": path,
            "sha256": sha256,
            "size_bytes": 1024,
        },
        "identity": {
            "session_id": "sess_12345",
            "model": model,
            "harness": harness,
        },
        "status": status,
        "diagnostics": diagnostics,
        "warnings": warnings if warnings is not None else [],
    }


def _make_report(
    records: list[dict[str, Any]] | None = None,
    evidence_kind: str = "historical",
) -> dict[str, Any]:
    """Helper to create a report dictionary matching report_api contract."""
    recs = records if records is not None else []
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_kind": EXPECTED_ANALYSIS_KIND,
        "evidence_kind": evidence_kind,
        "records": recs,
        "summary": {
            "source_count": len(recs),
            "analyzed_count": sum(
                1 for r in recs if isinstance(r, dict) and r.get("status") == "analyzed"
            ),
            "unsupported_count": sum(
                1 for r in recs if isinstance(r, dict) and r.get("status") == "unsupported"
            ),
            "error_count": sum(
                1 for r in recs if isinstance(r, dict) and r.get("status") == "error"
            ),
            "observed_counts_by_mechanism": {m: 0 for m in STANDARD_MECHANISMS},
            "hypothesis_counts_by_mechanism": {m: 0 for m in STANDARD_MECHANISMS},
            "unknown_counts_by_mechanism": {m: 0 for m in STANDARD_MECHANISMS},
        },
        "limitations": ["Observational reporting limitation."],
    }


# =============================================================================
# 1. Execution Readiness & Authorization Invariants
# =============================================================================


def test_ablation_plan_execution_authorized_is_strictly_false() -> None:
    """The ablation plan must never authorize execution at root or proposal level."""
    obs = _make_observation(code="STOPPING_STEP_FINISH_REASON_LENGTH", evidence_kind="observed")
    diag = _make_diagnostic("stopping", observations=[obs])
    rec = _make_record(
        diagnostics={
            "stopping": diag,
            "clipping": _make_diagnostic("clipping"),
            "shell": _make_diagnostic("shell"),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)

    assert plan["execution_authorized"] is False, "Plan root must have execution_authorized: False"
    assert len(plan["proposals"]) == 1
    for prop in plan["proposals"]:
        assert prop["execution_authorized"] is False, (
            "Every proposal must have execution_authorized: False"
        )


def test_proposals_do_not_contain_executable_payloads() -> None:
    """Proposals must be analytical designs, not executable jobs or runner formats."""
    obs = _make_observation(code="SHELL_TIMEOUT_EXPLICIT", evidence_kind="observed")
    diag = _make_diagnostic("shell", observations=[obs])
    rec = _make_record(
        diagnostics={
            "stopping": _make_diagnostic("stopping"),
            "clipping": _make_diagnostic("clipping"),
            "shell": diag,
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)
    proposal = plan["proposals"][0]

    forbidden_keys = {
        "command",
        "run_cmd",
        "docker_image",
        "container_id",
        "queue",
        "submit",
        "api_key",
        "execute",
    }
    assert not (set(proposal.keys()) & forbidden_keys)


def test_proposals_do_not_invent_task_ids_or_profile_ids() -> None:
    """Proposals must not invent synthetic benchmark task IDs or fake Harbor profiles."""
    obs = _make_observation(code="OBSERVATION_METADATA_TRUNCATED", evidence_kind="observed")
    diag = _make_diagnostic("clipping", observations=[obs])
    rec = _make_record(
        diagnostics={
            "stopping": _make_diagnostic("stopping"),
            "clipping": diag,
            "shell": _make_diagnostic("shell"),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)
    proposal = plan["proposals"][0]

    plan_str = json.dumps(proposal)
    assert "task-1" not in plan_str
    assert "SWE-bench/" not in plan_str
    assert "harbor-prod" not in plan_str


# =============================================================================
# 2. Causal Claim Prevention & Hypothesis Labeling
# =============================================================================


def test_target_explanations_are_explicit_hypotheses() -> None:
    """Target explanations must be articulated hypotheses, not verified causal assertions."""
    obs = _make_observation(code="STOPPING_STEP_FINISH_REASON_LENGTH", evidence_kind="hypothesis")
    diag = _make_diagnostic("stopping", observations=[obs])
    rec = _make_record(
        diagnostics={
            "stopping": diag,
            "clipping": _make_diagnostic("clipping"),
            "shell": _make_diagnostic("shell"),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)
    proposal = plan["proposals"][0]

    assert "target_hypothesis" in proposal
    assert "disambiguation_focus" in proposal
    assert isinstance(proposal["target_hypothesis"], str) and proposal["target_hypothesis"].strip()
    assert (
        isinstance(proposal["disambiguation_focus"], str)
        and proposal["disambiguation_focus"].strip()
    )


def test_no_causal_ranking_or_performance_claims() -> None:
    """Proposals must not rank harnesses or assert pass-rate superiority."""
    obs = _make_observation(code="SHELL_RESET_EXPLICIT", evidence_kind="observed")
    diag = _make_diagnostic("shell", observations=[obs])
    rec = _make_record(
        diagnostics={
            "stopping": _make_diagnostic("stopping"),
            "clipping": _make_diagnostic("clipping"),
            "shell": diag,
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)
    proposal = plan["proposals"][0]

    prop_text = json.dumps(proposal).lower()
    assert "is better than" not in prop_text
    assert "superior" not in prop_text
    assert "outperforms" not in prop_text


# =============================================================================
# 3. Evidence Gate: Relevant Evidence vs Non-Intervention Exclusions
# =============================================================================


def test_empty_proposals_when_no_diagnostic_evidence() -> None:
    """When a report contains zero observations and zero hypotheses, proposals must be empty."""
    rec = _make_record()
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)

    assert plan["proposals"] == [], "Must produce no proposals when there is no relevant evidence"
    assert len(plan["limitations"]) > 0


def test_empty_trajectory_alone_does_not_justify_proposal() -> None:
    """An empty trajectory code (zero steps executed) does not justify a stopping intervention."""
    obs = _make_observation(code="STOPPING_EMPTY_TRAJECTORY", evidence_kind="observed")
    diag = _make_diagnostic("stopping", observations=[obs])
    rec = _make_record(
        diagnostics={
            "stopping": diag,
            "clipping": _make_diagnostic("clipping"),
            "shell": _make_diagnostic("shell"),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)

    assert plan["proposals"] == [], "Empty trajectory alone must not trigger an ablation proposal"


def test_unresolved_refs_alone_do_not_justify_proposal() -> None:
    """Unresolved CAS/content references are evidence unknowns and do not justify clipping intervention."""
    obs = _make_observation(code="CONTENT_REF_UNRESOLVED", evidence_kind="observed")
    diag = _make_diagnostic("clipping", observations=[obs])
    rec = _make_record(
        diagnostics={
            "stopping": _make_diagnostic("stopping"),
            "clipping": diag,
            "shell": _make_diagnostic("shell"),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)

    assert plan["proposals"] == [], (
        "Unresolved reference alone must not trigger a clipping proposal"
    )


def test_missing_telemetry_alone_does_not_justify_proposal() -> None:
    """Missing full output telemetry does not certify clipping or justify an intervention."""
    obs = _make_observation(code="MISSING_FULL_OUTPUT", evidence_kind="observed")
    diag = _make_diagnostic("clipping", observations=[obs])
    rec = _make_record(
        diagnostics={
            "stopping": _make_diagnostic("stopping"),
            "clipping": diag,
            "shell": _make_diagnostic("shell"),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)

    assert plan["proposals"] == [], "Missing telemetry alone must not trigger an ablation proposal"


def test_observation_marker_indicator_and_ambiguous_marker_do_not_justify_proposal() -> None:
    """Unstructured truncation marker indicators or ambiguous quoted markers alone do not certify clipping or justify intervention."""
    obs_marker = _make_observation(code="OBSERVATION_MARKER_INDICATOR", evidence_kind="hypothesis")
    obs_ambig = _make_observation(code="AMBIGUOUS_QUOTED_MARKER", evidence_kind="hypothesis")
    diag = _make_diagnostic("clipping", observations=[obs_marker, obs_ambig])
    rec = _make_record(
        diagnostics={
            "stopping": _make_diagnostic("stopping"),
            "clipping": diag,
            "shell": _make_diagnostic("shell"),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)

    assert plan["proposals"] == [], (
        "Observation marker indicator alone must not trigger an ablation proposal"
    )


def test_final_assistant_with_tools_structure_alone_does_not_justify_proposal() -> None:
    """Neutral final assistant turn with tool calls is structural observation and must not trigger stopping proposal."""
    obs = _make_observation(
        code="STOPPING_FINAL_ASSISTANT_WITH_TOOL_CALLS", evidence_kind="observed"
    )
    diag = _make_diagnostic("stopping", observations=[obs])
    rec = _make_record(
        diagnostics={
            "stopping": diag,
            "clipping": _make_diagnostic("clipping"),
            "shell": _make_diagnostic("shell"),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)

    assert plan["proposals"] == [], (
        "Final assistant with tool calls structure alone must not trigger an ablation proposal"
    )


def test_neutral_completion_alone_does_not_justify_proposal() -> None:
    """Neutral assistant turn without tool calls is standard completion and must not trigger stopping proposal."""
    obs = _make_observation(code="STOPPING_FINAL_ASSISTANT_NO_TOOL", evidence_kind="observed")
    diag = _make_diagnostic("stopping", observations=[obs])
    rec = _make_record(
        diagnostics={
            "stopping": diag,
            "clipping": _make_diagnostic("clipping"),
            "shell": _make_diagnostic("shell"),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)

    assert plan["proposals"] == [], "Neutral completion alone must not trigger an ablation proposal"


def test_trailing_non_agent_step_alone_does_not_justify_proposal() -> None:
    """Trailing non-agent steps (e.g. unexecuted canaries with 0 agent turns) must not trigger proposal."""
    obs = _make_observation(code="STOPPING_TRAILING_NON_AGENT_STEP", evidence_kind="observed")
    diag = _make_diagnostic("stopping", observations=[obs])
    rec = _make_record(
        diagnostics={
            "stopping": diag,
            "clipping": _make_diagnostic("clipping"),
            "shell": _make_diagnostic("shell"),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)

    assert plan["proposals"] == [], (
        "Trailing non-agent step alone must not trigger an ablation proposal"
    )


def test_historical_initial_neutral_and_trailing_controls_yield_zero_proposals() -> None:
    """Actual historical initial scenario: 9 normal FINAL_ASSISTANT_NO_TOOL + 3 TRAILING_NON_AGENT_STEP must yield 0 proposals."""
    records = []
    for i in range(9):
        obs = _make_observation(
            code="STOPPING_FINAL_ASSISTANT_NO_TOOL", locator=f"/steps/{i}/tool_calls"
        )
        diag = _make_diagnostic("stopping", observations=[obs])
        records.append(
            _make_record(
                path=f"runs/historical/normal_{i}/trajectory.json",
                diagnostics={
                    "stopping": diag,
                    "clipping": _make_diagnostic("clipping"),
                    "shell": _make_diagnostic("shell"),
                },
            )
        )
    for i in range(3):
        obs = _make_observation(
            code="STOPPING_TRAILING_NON_AGENT_STEP", locator=f"/steps/{i}/source"
        )
        diag = _make_diagnostic("stopping", observations=[obs])
        records.append(
            _make_record(
                path=f"runs/historical/canary_{i}/trajectory.json",
                diagnostics={
                    "stopping": diag,
                    "clipping": _make_diagnostic("clipping"),
                    "shell": _make_diagnostic("shell"),
                },
            )
        )

    report = _make_report(records=records)
    plan = build_ablation_plan(report)

    assert plan["proposals"] == [], (
        "Initial historical run with only neutral controls and canary steps must produce 0 proposals"
    )


def test_unknown_only_inputs_yield_no_proposals() -> None:
    """Reports with unknowns only and no substantive observations yield zero proposals."""
    rec = _make_record(
        diagnostics={
            "stopping": _make_diagnostic(
                "stopping", unknowns=["finish_reason is not retained in telemetry"]
            ),
            "clipping": _make_diagnostic(
                "clipping",
                unknowns=["unannotated_observation_boundaries: No clipping metadata retained"],
            ),
            "shell": _make_diagnostic(
                "shell", unknowns=["shell_session_persistence: cannot be determined"]
            ),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)

    assert plan["proposals"] == [], "Unknown-only input must yield no proposals"


def test_proposal_generated_only_for_mechanisms_with_evidence() -> None:
    """Only mechanisms with substantive observations/hypotheses receive a proposal."""
    stopping_obs = _make_observation(
        code="STOPPING_STEP_FINISH_REASON_LENGTH", evidence_kind="observed"
    )
    diag_stopping = _make_diagnostic("stopping", observations=[stopping_obs])
    rec = _make_record(
        diagnostics={
            "stopping": diag_stopping,
            "clipping": _make_diagnostic("clipping", observations=[]),
            "shell": _make_diagnostic("shell", observations=[]),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)

    assert len(plan["proposals"]) == 1
    assert plan["proposals"][0]["mechanism"] == "stopping"


def test_multi_mechanism_evidence_generates_matching_proposals() -> None:
    """Multiple mechanisms with substantive evidence each receive a bounded proposal."""
    obs_clip = _make_observation(
        code="OBSERVATION_METADATA_TRUNCATED",
        locator="/steps/0/observation/results/0/extra",
        evidence_kind="observed",
    )
    obs_shell = _make_observation(
        code="SHELL_REPEATED_FAILED_COMMAND_HYPOTHESIS",
        locator="/steps/1/tool_calls/0/arguments",
        evidence_kind="hypothesis",
    )

    diag_clip = _make_diagnostic("clipping", observations=[obs_clip])
    diag_shell = _make_diagnostic("shell", observations=[obs_shell])
    rec = _make_record(
        diagnostics={
            "stopping": _make_diagnostic("stopping"),
            "clipping": diag_clip,
            "shell": diag_shell,
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)

    mechs = [p["mechanism"] for p in plan["proposals"]]
    assert mechs == ["clipping", "shell"], "Proposals must be sorted deterministically by mechanism"


# =============================================================================
# 4. Mechanism Categories & Code/Locator/Source Passthrough
# =============================================================================


def test_preserves_actual_source_paths_hashes_and_json_pointers() -> None:
    """Evidence links must preserve source path, sha256 hash, and RFC6901 JSON pointer locators without raw strings."""
    source_path = "runs/canary/trial1/trajectory.json"
    source_hash = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    json_pointer = "/steps/42/tool_calls/3/arguments"
    custom_code = "STOPPING_STEP_FINISH_REASON_LENGTH"

    obs = _make_observation(code=custom_code, locator=json_pointer, evidence_kind="observed")
    diag = _make_diagnostic("stopping", observations=[obs], unknowns=["unknown_exit_reason"])
    rec = _make_record(
        path=source_path,
        sha256=source_hash,
        diagnostics={
            "stopping": diag,
            "clipping": _make_diagnostic("clipping"),
            "shell": _make_diagnostic("shell"),
        },
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)
    proposal = plan["proposals"][0]

    evidence = proposal["linked_evidence"]
    assert custom_code in evidence["observed_codes"]
    assert json_pointer in evidence["locators"]
    assert source_path in evidence["source_paths"]
    assert source_hash in evidence["source_hashes"]
    assert "unknown_exit_reason" in evidence["retained_unknowns"]
    assert evidence["observation_count"] == 1
    assert evidence["records_affected_count"] == 1

    # Check structured evidence links
    assert len(evidence["evidence_links"]) == 1
    link = evidence["evidence_links"][0]
    assert link["code"] == custom_code
    assert link["source_path"] == source_path
    assert link["source_sha256"] == source_hash
    assert link["locator"] == json_pointer
    assert link["evidence_kind"] == "observed"

    # Confirm no arbitrary raw text/payload leaks in proposal
    for raw_field in ("raw_data", "payload", "command", "stdout", "message"):
        assert raw_field not in link
        assert raw_field not in evidence


def test_supports_both_observed_and_hypothesis_evidence_kinds() -> None:
    """Distinguishes observed from hypothesis codes while carrying both."""
    obs_factual = _make_observation(
        code="STOPPING_STEP_FINISH_REASON_LENGTH", evidence_kind="observed"
    )
    obs_hypothetical = _make_observation(
        code="STOPPING_PREMATURE_CUTOFF_HYPOTHESIS", evidence_kind="hypothesis"
    )

    diag = _make_diagnostic("stopping", observations=[obs_factual, obs_hypothetical])
    rec = _make_record(
        diagnostics={
            "stopping": diag,
            "clipping": _make_diagnostic("clipping"),
            "shell": _make_diagnostic("shell"),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)
    proposal = plan["proposals"][0]

    evidence = proposal["linked_evidence"]
    assert "STOPPING_STEP_FINISH_REASON_LENGTH" in evidence["observed_codes"]
    assert "STOPPING_PREMATURE_CUTOFF_HYPOTHESIS" in evidence["hypothesis_codes"]
    assert evidence["observation_count"] == 2


# =============================================================================
# 5. One Changed Mechanism & Single-Variable Isolation
# =============================================================================


def test_each_proposal_isolates_one_variable_and_states_measurements() -> None:
    """Each proposal must strictly isolate one changed mechanism variable and state required measurements."""
    obs_clip = _make_observation(code="OBSERVATION_METADATA_TRUNCATED", evidence_kind="observed")
    obs_shell = _make_observation(code="SHELL_RESET_EXPLICIT", evidence_kind="observed")
    rec = _make_record(
        diagnostics={
            "stopping": _make_diagnostic("stopping"),
            "clipping": _make_diagnostic("clipping", observations=[obs_clip]),
            "shell": _make_diagnostic("shell", observations=[obs_shell]),
        }
    )
    report = _make_report(records=[rec])

    plan = build_ablation_plan(report)
    assert len(plan["proposals"]) == 2

    for proposal in plan["proposals"]:
        # Verify single changed mechanism
        one_changed = proposal["one_changed_mechanism"]
        assert "mechanism_variable" in one_changed
        assert "baseline_condition" in one_changed
        assert "counterfactual_condition" in one_changed
        assert isinstance(one_changed["mechanism_variable"], str)

        # Confirm variables are not conflated (e.g. no 'shell persistence versus clipping' combined factor)
        assert "versus clipping" not in one_changed["mechanism_variable"].lower()
        assert "versus shell" not in one_changed["mechanism_variable"].lower()

        # Check fixed conditions
        fixed = proposal["fixed_conditions"]
        for condition_key in (
            "task_conditions",
            "model_conditions",
            "reward_conditions",
            "budget_conditions",
        ):
            assert condition_key in fixed
            assert isinstance(fixed[condition_key], str) and len(fixed[condition_key]) > 10

        # Check required measurements and observable checks
        assert len(proposal["observable_checks"]) >= 2
        assert len(proposal["required_measurements"]) >= 2
        assert len(proposal["evidence_needed"]) >= 2
        assert len(proposal["held_out_evaluation_limits"]) >= 1


# =============================================================================
# 6. Validation & Robustness Against Malformed Inputs
# =============================================================================


def test_raises_on_invalid_report_structure() -> None:
    """build_ablation_plan must raise ValueError on malformed report top-level structure."""
    with pytest.raises(ValueError):
        build_ablation_plan(["not", "a", "dict"])  # type: ignore

    with pytest.raises(ValueError):
        build_ablation_plan(
            {
                "schema_version": 999,
                "analysis_kind": EXPECTED_ANALYSIS_KIND,
                "records": [],
                "summary": {},
                "limitations": [],
            }
        )

    with pytest.raises(ValueError):
        build_ablation_plan(
            {
                "schema_version": SCHEMA_VERSION,
                "analysis_kind": "unsupported-kind",
                "records": [],
                "summary": {},
                "limitations": [],
            }
        )

    with pytest.raises(ValueError):
        build_ablation_plan(
            {
                "schema_version": SCHEMA_VERSION,
                "analysis_kind": EXPECTED_ANALYSIS_KIND,
                "records": [],
            }
        )


def test_safe_against_malformed_records() -> None:
    """build_ablation_plan must safely tolerate malformed record entries without crashing."""
    malformed_records: list[Any] = [
        None,
        "not-a-record",
        {"status": "error", "source": "invalid"},
        {"status": "error", "source": None},
        {"status": "analyzed", "source": None, "diagnostics": {"stopping": None}},
        {"status": "analyzed", "diagnostics": "not-a-dict"},
        {"status": "analyzed", "diagnostics": {"stopping": None}},
        {"status": "analyzed", "diagnostics": {"stopping": {"observations": "not-a-list"}}},
        {
            "status": "analyzed",
            "source": {"path": "safe.json", "sha256": None},
            "diagnostics": {
                "stopping": {
                    "observations": [
                        "not-a-dict",
                        {"code": None, "locator": None},
                        {"code": "   ", "locator": "/steps/0"},
                        {
                            "code": "STOPPING_STEP_FINISH_REASON_LENGTH",
                            "locator": "/steps/0/finish_reason",
                            "evidence_kind": "observed",
                        },
                    ],
                    "unknowns": [None, 123, ""],
                }
            },
        },
    ]
    report = _make_report(records=malformed_records)

    plan = build_ablation_plan(report)

    assert plan["artifact_kind"] == ARTIFACT_KIND
    assert len(plan["proposals"]) == 1
    assert plan["proposals"][0]["mechanism"] == "stopping"


# =============================================================================
# 7. Determinism
# =============================================================================


def test_ablation_plan_generation_is_deterministic() -> None:
    """Repeated calls with identical input must produce bit-for-bit identical plans."""
    obs_stopping = _make_observation(
        code="STOPPING_STEP_FINISH_REASON_LENGTH", evidence_kind="observed"
    )
    obs_shell = _make_observation(
        code="SHELL_REPEATED_FAILED_COMMAND_HYPOTHESIS", evidence_kind="hypothesis"
    )

    diag_stopping = _make_diagnostic("stopping", observations=[obs_stopping])
    diag_shell = _make_diagnostic("shell", observations=[obs_shell])
    rec = _make_record(
        diagnostics={
            "stopping": diag_stopping,
            "clipping": _make_diagnostic("clipping"),
            "shell": diag_shell,
        }
    )
    report = _make_report(records=[rec])

    plan1 = build_ablation_plan(report)
    plan2 = build_ablation_plan(report)

    assert json.dumps(plan1, sort_keys=True) == json.dumps(plan2, sort_keys=True)


# =============================================================================
# 8. Source Catalog Validation
# =============================================================================


def test_source_catalog_json_validity_and_pinned_sources() -> None:
    """source-catalog.json must be valid JSON and contain pinned report sections and commits."""
    assert _CATALOG_PATH.is_file(), f"Missing source catalog at {_CATALOG_PATH}"

    catalog = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))

    assert catalog.get("schema_version") == 1
    assert catalog.get("catalog_id") == "harness-mechanics-source-catalog"

    sources = catalog.get("pinned_sources", {})
    assert "deepseek_report_section_5_1_2" in sources
    assert "deepseek_report_section_5_3_4" in sources
    assert "deepseek_report_appendix_b" in sources
    assert "deepseek_harness_c291e79" in sources
    assert "mini_swe_agent_04d809c" in sources

    # Reproduction gaps preserved
    gaps = catalog.get("reproduction_gaps", [])
    assert len(gaps) >= 4
    gap_ids = {g["gap_id"] for g in gaps}
    assert "custom_build_dependency" in gap_ids
    assert "sandbox_scheduler_discrepancy" in gap_ids
    assert "native_harbor_adapter_absence" in gap_ids
    assert "environment_cache_sanitization" in gap_ids


def test_catalog_dsh_minimal_clips_to_16000_chars_and_unverified_build_limit() -> None:
    """Catalog records DSH Minimal 16,000 char clipping and notes unverified custom build does not make all future ablations impossible."""
    catalog = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))

    sources = catalog.get("pinned_sources", {})
    dsh = sources.get("deepseek_harness_c291e79", {})
    mechanisms = dsh.get("pinned_mechanisms", {})

    # Check that DSH Minimal observation clipping is recorded with 16,000 character limit
    assert "observation_clipping" in mechanisms
    assert "16,000" in mechanisms["observation_clipping"]

    # Check that custom_build_dependency gap notes future counterfactual ablations are not rendered impossible
    gaps = catalog.get("reproduction_gaps", [])
    custom_gap = next((g for g in gaps if g.get("gap_id") == "custom_build_dependency"), None)
    assert custom_gap is not None
    impact_text = custom_gap.get("impact", "").lower()
    assert (
        "not make all future counterfactual ablations impossible" in impact_text
        or "does not make" in impact_text
    )


# =============================================================================
# 9. Cross-Module Real Inspect & Native ATIF Integration Tests
# =============================================================================


def test_cross_module_marker_only_and_final_structure_yield_zero_proposals() -> None:
    """Marker-only output spoofing and neutral final-agent-with-tools structure produce zero proposals.

    Uses real inspect functions from clipping, stopping, and shell modules over
    native ATIF TrajectoryIR instances, and passes the resulting records to
    build_ablation_plan.
    """
    from harness_mechanics.clipping import inspect as inspect_clipping
    from harness_mechanics.shell import inspect as inspect_shell
    from harness_mechanics.stopping import inspect as inspect_stopping

    from evallab.trajectory_ir import (
        ObservationResultRecord,
        StepRecord,
        ToolCallRecord,
        TrajectoryIR,
    )

    # 1. Marker-only spoof trajectory: has <<evallab-truncated...>> marker string
    # in observation content, but NO structured clipping metadata in extra.
    # Also has final agent step with tool calls (neutral structure).
    ir_marker_only = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                message="checking files",
                tool_calls=(
                    ToolCallRecord(
                        tool_call_id="call_spoof",
                        function_name="bash",
                        arguments={"command": "cat /tmp/large.log"},
                    ),
                ),
            ),
            StepRecord(
                step_id=2,
                source="tool",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_spoof",
                        content=(
                            "Command stdout header\n"
                            "<<evallab-truncated: 8192 bytes omitted, full deadbeef1234>>\n"
                            "trailing lines"
                        ),
                        # Crucial: extra is empty/neutral; no structured truncation metadata
                        extra={"exit_code": 0},
                    ),
                ),
            ),
            StepRecord(
                step_id=3,
                source="agent",
                message="final step with tool call",
                tool_calls=(
                    ToolCallRecord(
                        tool_call_id="call_next",
                        function_name="bash",
                        arguments={"command": "echo done"},
                    ),
                ),
            ),
        ),
    )

    diag_stopping = inspect_stopping(ir_marker_only)
    diag_clipping = inspect_clipping(ir_marker_only)
    diag_shell = inspect_shell(ir_marker_only)

    # Verify producer emitted OBSERVATION_MARKER_INDICATOR and STOPPING_FINAL_ASSISTANT_WITH_TOOL_CALLS
    clipping_codes = [o["code"] for o in diag_clipping["observations"]]
    stopping_codes = [o["code"] for o in diag_stopping["observations"]]
    assert "OBSERVATION_MARKER_INDICATOR" in clipping_codes
    assert "STOPPING_FINAL_ASSISTANT_WITH_TOOL_CALLS" in stopping_codes

    rec_marker = _make_record(
        path="runs/marker-only/trajectory.json",
        sha256="1111111111111111111111111111111111111111111111111111111111111111",
        diagnostics={
            "stopping": diag_stopping,
            "clipping": diag_clipping,
            "shell": diag_shell,
        },
    )
    report = _make_report(records=[rec_marker])
    plan = build_ablation_plan(report)

    # Acceptance: marker-only and neutral final structure MUST produce ZERO proposals
    assert plan["proposals"] == [], (
        f"Marker-only spoof and final structure must yield zero proposals, got: {[p['mechanism'] for p in plan['proposals']]}"
    )
    assert plan["execution_authorized"] is False


def test_cross_module_real_inspect_explicit_clipping_and_limits_yield_proposals() -> None:
    """Real inspect results with explicit clipping metadata and explicit limit exhaustion yield actionable proposals.

    Ensures positive actual metadata remains useful and single-variable proposals
    are generated with valid locators and execution_authorized=False.
    """
    from harness_mechanics.clipping import inspect as inspect_clipping
    from harness_mechanics.shell import inspect as inspect_shell
    from harness_mechanics.stopping import inspect as inspect_stopping

    from evallab.trajectory_ir import (
        ObservationResultRecord,
        SamplingParams,
        StepRecord,
        ToolCallRecord,
        TrajectoryIR,
    )

    # Trajectory with verified structured truncation metadata and explicit token limit finish reason
    ir_substantive = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                message="running large test suite",
                tool_calls=(
                    ToolCallRecord(
                        tool_call_id="call_test",
                        function_name="bash",
                        arguments={"command": "pytest"},
                    ),
                ),
                sampling_params=SamplingParams(
                    max_tokens=2048,
                    extra={"finish_reason": "length"},
                ),
            ),
            StepRecord(
                step_id=2,
                source="tool",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_test",
                        content="pytest failures...",
                        # Real structured truncation metadata
                        extra={"truncated": True, "truncated_bytes": 16384},
                    ),
                ),
            ),
        ),
    )

    diag_stopping = inspect_stopping(ir_substantive)
    diag_clipping = inspect_clipping(ir_substantive)
    diag_shell = inspect_shell(ir_substantive)

    clipping_codes = [o["code"] for o in diag_clipping["observations"]]
    stopping_codes = [o["code"] for o in diag_stopping["observations"]]
    assert "OBSERVATION_METADATA_TRUNCATED" in clipping_codes
    assert "STOPPING_STEP_FINISH_REASON_LENGTH" in stopping_codes

    rec = _make_record(
        path="runs/substantive/trajectory.json",
        sha256="2222222222222222222222222222222222222222222222222222222222222222",
        diagnostics={
            "stopping": diag_stopping,
            "clipping": diag_clipping,
            "shell": diag_shell,
        },
    )
    report = _make_report(records=[rec])
    plan = build_ablation_plan(report)

    # Both clipping and stopping have substantive evidence; exactly two bounded proposals
    mechs = [p["mechanism"] for p in plan["proposals"]]
    assert mechs == ["clipping", "stopping"]

    for prop in plan["proposals"]:
        assert prop["execution_authorized"] is False
        assert len(prop["linked_evidence"]["source_paths"]) == 1
        assert len(prop["linked_evidence"]["source_hashes"]) == 1
        assert len(prop["linked_evidence"]["locators"]) > 0
        # All locators must be valid RFC6901 pointers
        for loc in prop["linked_evidence"]["locators"]:
            assert loc.startswith("/")
