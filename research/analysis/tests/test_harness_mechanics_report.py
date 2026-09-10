"""Tests for Harness Mechanics Lab observational report generation and rendering."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

try:
    from harness_mechanics.report import (
        ANALYSIS_KIND,
        SCHEMA_VERSION,
        STANDARD_MECHANISMS,
        build_report,
        render_text,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness-mechanics"))
    from harness_mechanics.report import (
        ANALYSIS_KIND,
        SCHEMA_VERSION,
        STANDARD_MECHANISMS,
        build_report,
        render_text,
    )


def _make_observation(
    code: str = "STOP_MAX_TOKENS",
    step_id: int | None = 0,
    tool_call_id: str | None = None,
    locator: str = "steps[0].tool_calls",
    evidence_kind: str = "observed",
    summary: str = "Standard test observation",
) -> dict[str, Any]:
    """Helper to create a valid observation dictionary."""
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
    """Helper to create a valid diagnostic dictionary for a mechanism."""
    return {
        "mechanism": mechanism,
        "observations": observations if observations is not None else [],
        "unknowns": unknowns if unknowns is not None else [],
    }


def _make_record(
    path: str = "runs/job1/trial1/trajectory.json",
    status: str = "analyzed",
    sha256: str | None = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    size_bytes: int | None = 1024,
    session_id: str | None = "session-001",
    model: str | None = "test-model",
    harness: str | None = "harbor",
    diagnostics: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Helper to create a valid trajectory inspection record."""
    if diagnostics is None:
        if status == "analyzed":
            diagnostics = {mech: _make_diagnostic(mech) for mech in STANDARD_MECHANISMS}
        else:
            diagnostics = {}

    return {
        "source": {
            "path": path,
            "sha256": sha256,
            "size_bytes": size_bytes,
        },
        "identity": {
            "session_id": session_id,
            "model": model,
            "harness": harness,
        },
        "status": status,
        "diagnostics": diagnostics,
        "warnings": warnings if warnings is not None else [],
    }


# ==============================================================================
# Tests for build_report: Normal Operations & Deterministic Aggregation
# ==============================================================================


def test_build_report_valid_analyzed_records() -> None:
    """Verify build_report generates correct report structure and aggregates counts."""
    diag_stopping = _make_diagnostic(
        "stopping",
        observations=[
            _make_observation(
                code="STOP_MAX_TOKENS",
                step_id=5,
                evidence_kind="observed",
                summary="Reached max tokens",
            ),
            _make_observation(
                code="STOP_NO_TOOL_CALL",
                step_id=6,
                evidence_kind="hypothesis",
                summary="Final response lacked tool call",
            ),
        ],
        unknowns=["No finish_reason in metadata"],
    )
    diag_clipping = _make_diagnostic(
        "clipping",
        observations=[
            _make_observation(
                code="CLIP_RETAINED_MARKER",
                step_id=2,
                evidence_kind="observed",
                summary="Truncation marker found in stdout",
            ),
        ],
        unknowns=[],
    )
    diag_shell = _make_diagnostic(
        "shell",
        observations=[],
        unknowns=["Working directory across calls uninstrumented", "Subprocess exit code omitted"],
    )

    rec1 = _make_record(
        path="runs/eval_01.json",
        diagnostics={
            "stopping": diag_stopping,
            "clipping": diag_clipping,
            "shell": diag_shell,
        },
    )

    rec2 = _make_record(
        path="runs/eval_02.json",
        diagnostics={
            "stopping": _make_diagnostic("stopping"),
            "clipping": _make_diagnostic("clipping"),
            "shell": _make_diagnostic(
                "shell",
                observations=[
                    _make_observation(
                        code="SHELL_RESET_TIMEOUT",
                        step_id=3,
                        evidence_kind="hypothesis",
                        summary="Shell command timed out and session was reset",
                    ),
                ],
                unknowns=["TTY state uninstrumented"],
            ),
        },
    )

    report = build_report([rec1, rec2], evidence_kind="historical")

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["analysis_kind"] == ANALYSIS_KIND
    assert report["evidence_kind"] == "historical"
    assert len(report["records"]) == 2

    summary = report["summary"]
    assert summary["source_count"] == 2
    assert summary["analyzed_count"] == 2
    assert summary["unsupported_count"] == 0
    assert summary["error_count"] == 0

    # Observed counts: stopping=1, clipping=1, shell=0
    assert summary["observed_counts_by_mechanism"] == {
        "stopping": 1,
        "clipping": 1,
        "shell": 0,
    }

    # Hypothesis counts: stopping=1, clipping=0, shell=1
    assert summary["hypothesis_counts_by_mechanism"] == {
        "stopping": 1,
        "clipping": 0,
        "shell": 1,
    }

    # Unknown counts: stopping=1, clipping=0, shell=3
    assert summary["unknown_counts_by_mechanism"] == {
        "stopping": 1,
        "clipping": 0,
        "shell": 3,
    }

    # Verify limitations include research integrity warnings
    assert len(report["limitations"]) >= 6
    assert any("evidence of absence" in lim.lower() for lim in report["limitations"])
    assert any("causal comparisons" in lim.lower() for lim in report["limitations"])


def test_build_report_deterministic_ordering() -> None:
    """Verify records and observations are sorted deterministically regardless of input order."""
    rec_c = _make_record(path="c_eval.json")
    rec_a = _make_record(path="a_eval.json")
    rec_b = _make_record(path="b_eval.json")

    # Observations in unsorted order (step 10 then step 2)
    rec_b["diagnostics"]["stopping"]["observations"] = [
        _make_observation(code="B_CODE", step_id=10, locator="steps[10]"),
        _make_observation(code="A_CODE", step_id=2, locator="steps[2]"),
    ]
    rec_b["diagnostics"]["stopping"]["unknowns"] = ["z_unknown", "a_unknown"]

    # Pass in scrambled order
    report1 = build_report([rec_c, rec_a, rec_b], evidence_kind="fixture")
    report2 = build_report([rec_b, rec_c, rec_a], evidence_kind="fixture")

    # Paths must be sorted a, b, c
    paths1 = [r["source"]["path"] for r in report1["records"]]
    paths2 = [r["source"]["path"] for r in report2["records"]]
    assert paths1 == ["a_eval.json", "b_eval.json", "c_eval.json"]
    assert paths2 == ["a_eval.json", "b_eval.json", "c_eval.json"]

    # Check observations inside record b were sorted by step_id (2 before 10)
    b_record = report1["records"][1]
    b_obs_steps = [o["step_id"] for o in b_record["diagnostics"]["stopping"]["observations"]]
    assert b_obs_steps == [2, 10]

    # Check unknowns were sorted alphabetically
    assert b_record["diagnostics"]["stopping"]["unknowns"] == ["a_unknown", "z_unknown"]

    # Rendered text must be byte-for-byte identical
    text1 = render_text(report1)
    text2 = render_text(report2)
    assert text1 == text2


def test_build_report_preserves_unsupported_and_error_records() -> None:
    """Verify unsupported and error records are retained and accounted for in denominators."""
    rec_analyzed = _make_record(path="analyzed.json", status="analyzed")
    rec_unsupported = _make_record(
        path="unsupported.json",
        status="unsupported",
        diagnostics={},
        warnings=["Unknown ATIF version 0.1"],
    )
    rec_error = _make_record(
        path="corrupted.json",
        status="error",
        diagnostics={},
        warnings=["Failed to parse JSON: unexpected EOF"],
    )

    report = build_report([rec_analyzed, rec_unsupported, rec_error], evidence_kind="historical")

    summary = report["summary"]
    assert summary["source_count"] == 3
    assert summary["analyzed_count"] == 1
    assert summary["unsupported_count"] == 1
    assert summary["error_count"] == 1

    # Check that all records are preserved in records list
    assert len(report["records"]) == 3
    statuses = {r["source"]["path"]: r["status"] for r in report["records"]}
    assert statuses == {
        "analyzed.json": "analyzed",
        "unsupported.json": "unsupported",
        "corrupted.json": "error",
    }

    # Denominator limitation should explain unanalyzed records
    assert any("could not be analyzed" in lim for lim in report["limitations"])


def test_distinguishes_no_observations_from_evidence_of_absence() -> None:
    """Verify that 0 observations is not treated as evidence of absence."""
    rec = _make_record(
        path="clean.json",
        diagnostics={
            "stopping": _make_diagnostic(
                "stopping",
                observations=[_make_observation(code="STOP_OK", evidence_kind="observed")],
            ),
            "clipping": _make_diagnostic("clipping", observations=[], unknowns=[]),
            "shell": _make_diagnostic("shell", observations=[], unknowns=[]),
        },
    )

    report = build_report([rec], evidence_kind="historical")

    # Clipping and shell have 0 observations
    assert report["summary"]["observed_counts_by_mechanism"]["clipping"] == 0
    assert report["summary"]["observed_counts_by_mechanism"]["shell"] == 0

    # Ensure limitation specifically warns against evidence of absence interpretation
    assert any(
        "absence of evidence from evidence of absence" in lim for lim in report["limitations"]
    )

    # Ensure rendered text contains the clear caveat
    text = render_text(report)
    assert "0 observed count indicates no explicit retained markers were detected" in text
    assert "NOT evidence of mechanism absence or harmlessness" in text


def test_build_report_empty_records() -> None:
    """Verify build_report handles an empty record list gracefully and deterministically."""
    report = build_report([], evidence_kind="fixture")

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["summary"]["source_count"] == 0
    assert report["summary"]["analyzed_count"] == 0
    assert report["summary"]["unsupported_count"] == 0
    assert report["summary"]["error_count"] == 0
    assert report["summary"]["observed_counts_by_mechanism"] == {
        "stopping": 0,
        "clipping": 0,
        "shell": 0,
    }
    assert report["records"] == []
    assert any("No trajectories were successfully analyzed" in lim for lim in report["limitations"])

    # render_text on empty report should not fail
    text = render_text(report)
    assert "Total Sources  : 0" in text
    assert "No records in report." in text


def test_caller_records_immutability() -> None:
    """Verify build_report does not mutate the input records passed by caller."""
    rec = _make_record(path="runs/t1.json")
    rec["diagnostics"]["stopping"]["unknowns"] = ["z", "a"]
    original_copy = copy.deepcopy(rec)

    records_list = [rec]
    _ = build_report(records_list, evidence_kind="historical")

    # Caller's input record must remain unchanged
    assert rec == original_copy
    assert rec["diagnostics"]["stopping"]["unknowns"] == ["z", "a"]


def test_rejects_unrequested_mechanism_expansion() -> None:
    """Verify build_report enforces exactly three mechanisms and rejects unrequested expansions."""
    rec = _make_record(path="custom_mech.json")
    rec["diagnostics"]["memory"] = _make_diagnostic(
        "memory",
        observations=[
            _make_observation(
                code="MEM_SPIKE", evidence_kind="observed", summary="Memory exceeded 8GB"
            ),
        ],
        unknowns=["OOM handler unrecorded"],
    )

    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")


# ==============================================================================
# Tests for Validation & Error Handling (ValueError guarantees)
# ==============================================================================


def test_rejects_invalid_evidence_kind() -> None:
    """Verify build_report rejects invalid, empty, or non-string evidence_kind."""
    with pytest.raises(ValueError):
        build_report([], evidence_kind="invalid_kind")

    with pytest.raises(ValueError):
        build_report([], evidence_kind="")

    with pytest.raises(ValueError):
        build_report([], evidence_kind=123)  # type: ignore[arg-type]


def test_rejects_unhashable_and_invalid_evidence_kind() -> None:
    """Verify unhashable (list, dict, set) evidence_kind raises safe ValueError, not TypeError."""
    with pytest.raises(ValueError):
        build_report([], evidence_kind=["historical"])  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        build_report([], evidence_kind={"kind": "historical"})  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        build_report([], evidence_kind={"historical"})  # type: ignore[arg-type]


def test_rejects_non_list_records() -> None:
    """Verify build_report rejects records that are not a list."""
    with pytest.raises(ValueError):
        build_report("not a list", evidence_kind="historical")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        build_report(None, evidence_kind="historical")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        build_report({}, evidence_kind="historical")  # type: ignore[arg-type]


def test_rejects_duplicate_source_paths() -> None:
    """Verify build_report rejects duplicate resolved source paths."""
    rec1 = _make_record(path="same/path.json")
    rec2 = _make_record(path="same/path.json")

    with pytest.raises(ValueError):
        build_report([rec1, rec2], evidence_kind="historical")


def test_rejects_malformed_record_shape() -> None:
    """Verify build_report rejects records missing required top-level keys."""
    with pytest.raises(ValueError):
        build_report([None], evidence_kind="historical")  # type: ignore[list-item]

    with pytest.raises(ValueError):
        build_report(["not-a-dict"], evidence_kind="historical")  # type: ignore[list-item]

    bad_rec = {"source": {"path": "t.json"}}
    with pytest.raises(ValueError):
        build_report([bad_rec], evidence_kind="historical")  # type: ignore[list-item]


def test_rejects_malformed_source() -> None:
    """Verify build_report validates source dictionary fields."""
    rec = _make_record()
    rec["source"] = None
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    rec = _make_record()
    rec["source"] = "not-a-dict"
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    rec = _make_record()
    rec["source"]["path"] = ""
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    rec = _make_record()
    rec["source"]["size_bytes"] = -10
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    rec = _make_record()
    rec["source"]["sha256"] = 12345
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")


def test_requires_all_contract_source_and_identity_keys() -> None:
    """Verify build_report requires all contract source and identity keys."""
    # Missing source.sha256
    rec = _make_record()
    del rec["source"]["sha256"]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Missing source.size_bytes
    rec = _make_record()
    del rec["source"]["size_bytes"]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Missing identity.session_id
    rec = _make_record()
    del rec["identity"]["session_id"]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Missing identity.model
    rec = _make_record()
    del rec["identity"]["model"]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Missing identity.harness
    rec = _make_record()
    del rec["identity"]["harness"]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")


def test_rejects_invalid_status() -> None:
    """Verify build_report rejects unapproved status strings."""
    rec = _make_record()
    rec["status"] = "successful"
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")


def test_rejects_unhashable_and_invalid_status_types() -> None:
    """Verify unhashable (list, dict, set) or non-string status values raise safe ValueError."""
    rec = _make_record()
    rec["status"] = ["analyzed"]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    rec = _make_record()
    rec["status"] = {"status": "analyzed"}
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    rec = _make_record()
    rec["status"] = None
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    rec = _make_record()
    rec["status"] = 123
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")


def test_rejects_malformed_warnings() -> None:
    """Verify build_report rejects non-list warnings or non-string warning items."""
    rec = _make_record()
    rec["warnings"] = "warning string instead of list"
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    rec = _make_record()
    rec["warnings"] = [123]  # type: ignore[list-item]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")


def test_analyzed_record_requires_standard_mechanisms() -> None:
    """Verify analyzed record must include all standard mechanisms."""
    rec = _make_record()
    del rec["diagnostics"]["shell"]

    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")


def test_disallows_diagnostics_on_unsupported_and_error_records() -> None:
    """Verify diagnostics must be empty for unsupported and error records."""
    rec_unsupported = _make_record(
        status="unsupported",
        diagnostics={"stopping": _make_diagnostic("stopping")},
    )
    with pytest.raises(ValueError):
        build_report([rec_unsupported], evidence_kind="historical")

    rec_error = _make_record(
        status="error",
        diagnostics={"shell": _make_diagnostic("shell")},
    )
    with pytest.raises(ValueError):
        build_report([rec_error], evidence_kind="historical")


def test_rejects_diagnostic_mechanism_mismatch() -> None:
    """Verify mechanism inside diagnostic must match its dictionary key."""
    rec = _make_record()
    rec["diagnostics"]["stopping"]["mechanism"] = "mismatched_name"

    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")


def test_rejects_malformed_unknowns() -> None:
    """Verify unknowns must be a list of non-empty strings."""
    rec = _make_record()
    rec["diagnostics"]["clipping"]["unknowns"] = "not-a-list"

    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    rec["diagnostics"]["clipping"]["unknowns"] = [""]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")


def test_rejects_malformed_observations_never_translates_to_zero() -> None:
    """Verify malformed observations raise clear ValueErrors and never translate to 0 counts."""
    # Observations not a list
    rec = _make_record()
    rec["diagnostics"]["stopping"]["observations"] = None
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Observation not a dict
    rec = _make_record()
    rec["diagnostics"]["stopping"]["observations"] = ["not-a-dict"]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Observation missing required key
    rec = _make_record()
    rec["diagnostics"]["stopping"]["observations"] = [{"code": "STOP"}]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Observation invalid evidence_kind
    rec = _make_record()
    bad_obs = _make_observation(evidence_kind="unsupported_evidence_kind")
    rec["diagnostics"]["stopping"]["observations"] = [bad_obs]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Observation invalid step_id (boolean True rejected despite being int subclass)
    rec = _make_record()
    bad_obs = _make_observation(step_id=True)  # type: ignore[arg-type]
    rec["diagnostics"]["stopping"]["observations"] = [bad_obs]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Observation negative step_id
    rec = _make_record()
    bad_obs = _make_observation(step_id=-5)
    rec["diagnostics"]["stopping"]["observations"] = [bad_obs]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Observation empty locator
    rec = _make_record()
    bad_obs = _make_observation(locator="")
    rec["diagnostics"]["stopping"]["observations"] = [bad_obs]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Observation empty summary
    rec = _make_record()
    bad_obs = _make_observation(summary="")
    rec["diagnostics"]["stopping"]["observations"] = [bad_obs]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")


def test_rejects_observation_unhashable_evidence_kind() -> None:
    """Verify unhashable evidence_kind in an observation raises safe ValueError, not TypeError."""
    rec = _make_record()
    bad_obs = _make_observation()
    bad_obs["evidence_kind"] = ["observed"]
    rec["diagnostics"]["stopping"]["observations"] = [bad_obs]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    bad_obs["evidence_kind"] = {"kind": "observed"}
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")


def test_rejects_extraneous_private_payload() -> None:
    """Verify validation strictly rejects leaked extraneous keys on all structures."""
    # Extraneous key on top-level record
    rec = _make_record()
    rec["raw_prompt"] = "system prompt leaking into record"
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Extraneous key on source
    rec = _make_record()
    rec["source"]["raw_content"] = "file contents leaking"
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Extraneous key on identity
    rec = _make_record()
    rec["identity"]["api_key"] = "sk-secret-key"
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Extraneous key on diagnostic
    rec = _make_record()
    rec["diagnostics"]["stopping"]["raw_trace"] = "stack trace dump"
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")

    # Extraneous key on observation
    rec = _make_record()
    bad_obs = _make_observation()
    bad_obs["raw_command"] = "cat /etc/passwd"
    rec["diagnostics"]["stopping"]["observations"] = [bad_obs]
    with pytest.raises(ValueError):
        build_report([rec], evidence_kind="historical")


def test_deterministic_preservation_of_partial_results() -> None:
    """Verify partial results across analyzed, unsupported, and error records are deterministically preserved."""
    rec_analyzed_partial = _make_record(
        path="runs/partial.json",
        diagnostics={
            "stopping": _make_diagnostic(
                "stopping",
                observations=[_make_observation(code="STOP_EARLY", step_id=1)],
                unknowns=["Unknown timeout"],
            ),
            "clipping": _make_diagnostic("clipping", observations=[], unknowns=[]),
            "shell": _make_diagnostic(
                "shell",
                observations=[
                    _make_observation(code="SHELL_RESET", step_id=2, evidence_kind="hypothesis")
                ],
                unknowns=["CWD uninstrumented"],
            ),
        },
    )
    rec_unsupported = _make_record(
        path="runs/unsupported.json",
        status="unsupported",
        diagnostics={},
        warnings=["Unsupported schema v0.9"],
    )
    rec_error = _make_record(
        path="runs/error.json",
        status="error",
        diagnostics={},
        warnings=["JSON decode error"],
    )

    # Scramble order
    report1 = build_report(
        [rec_error, rec_analyzed_partial, rec_unsupported], evidence_kind="historical"
    )
    report2 = build_report(
        [rec_unsupported, rec_error, rec_analyzed_partial], evidence_kind="historical"
    )

    # Denominators
    assert report1["summary"]["source_count"] == 3
    assert report1["summary"]["analyzed_count"] == 1
    assert report1["summary"]["unsupported_count"] == 1
    assert report1["summary"]["error_count"] == 1

    # Mechanism counts
    assert report1["summary"]["observed_counts_by_mechanism"] == {
        "stopping": 1,
        "clipping": 0,
        "shell": 0,
    }
    assert report1["summary"]["hypothesis_counts_by_mechanism"] == {
        "stopping": 0,
        "clipping": 0,
        "shell": 1,
    }
    assert report1["summary"]["unknown_counts_by_mechanism"] == {
        "stopping": 1,
        "clipping": 0,
        "shell": 1,
    }

    # Determinism across permutations
    assert report1["records"] == report2["records"]
    assert render_text(report1) == render_text(report2)

    # Check that no leaked keys exist in any record
    for r in report1["records"]:
        assert set(r.keys()) == {"source", "identity", "status", "diagnostics", "warnings"}
        assert set(r["source"].keys()) == {"path", "sha256", "size_bytes"}
        assert set(r["identity"].keys()) == {"session_id", "model", "harness"}


# ==============================================================================
# Tests for render_text and Research Integrity
# ==============================================================================


def test_no_causal_rankings_or_pass_rates() -> None:
    """Verify report and rendered text never invent pass-rates or cross-model rankings."""
    rec1 = _make_record(path="traj_model_a.json", model="model-alpha")
    rec2 = _make_record(path="traj_model_b.json", model="model-beta")

    report = build_report([rec1, rec2], evidence_kind="historical")

    # Ensure no pass-rate or win-rate metrics appear in report summary
    forbidden_metric_keys = [
        "pass_rate",
        "pass rate",
        "win_rate",
        "win rate",
        "ranking",
        "rankings",
        "score",
        "scores",
        "winner",
    ]
    for term in forbidden_metric_keys:
        assert term not in report["summary"], f"Forbidden metric {term!r} found in report summary"


def test_render_text_contains_no_raw_prompts_or_unredacted_content() -> None:
    """Verify render_text formats summary locators and codes without leaking raw data."""
    rec = _make_record(
        path="traj_01.json",
        diagnostics={
            "stopping": _make_diagnostic(
                "stopping",
                observations=[
                    _make_observation(
                        code="STOP_TOKEN_LIMIT",
                        step_id=14,
                        locator="steps[14].final_metrics",
                        evidence_kind="observed",
                        summary="Context window limit hit",
                    ),
                ],
                unknowns=["Terminal reason unrecorded"],
            ),
            "clipping": _make_diagnostic("clipping"),
            "shell": _make_diagnostic("shell"),
        },
    )

    report = build_report([rec], evidence_kind="historical")
    text = render_text(report)

    # Check key sections are present
    assert "HARNESS MECHANICS OBSERVATIONAL ANALYSIS REPORT" in text
    assert "EXECUTION & INGESTION DENOMINATORS" in text
    assert "MECHANISM EVIDENCE COUNTS" in text
    assert "DIAGNOSTIC OBSERVATIONS BREAKDOWN" in text
    assert "UNOBSERVED STATE & MISSING INSTRUMENTATION (UNKNOWNS)" in text
    assert "SOURCE TRAJECTORY INVENTORY (DETERMINISTIC ORDER)" in text
    assert "LIMITATIONS & RESEARCH INTEGRITY NON-CLAIMS" in text

    # Verify diagnostic code and locator are presented
    assert "STOP_TOKEN_LIMIT" in text
    assert "steps[14].final_metrics" in text
    assert "Context window limit hit" in text
    assert "Terminal reason unrecorded" in text


def test_render_text_rejects_malformed_report() -> None:
    """Verify render_text validates the report dictionary shape."""
    with pytest.raises(ValueError):
        render_text("not a dict")  # type: ignore[arg-type]

    bad_report = {"schema_version": 999}
    with pytest.raises(ValueError):
        render_text(bad_report)

    bad_report = {"schema_version": SCHEMA_VERSION, "analysis_kind": "wrong-kind"}
    with pytest.raises(ValueError):
        render_text(bad_report)

    bad_report = {
        "schema_version": SCHEMA_VERSION,
        "analysis_kind": ANALYSIS_KIND,
        "evidence_kind": "historical",
    }
    with pytest.raises(ValueError):
        render_text(bad_report)


def test_render_text_rejects_malformed_records() -> None:
    """Verify render_text rejects reports with non-dict records or records with invalid source."""
    valid_report = build_report([], evidence_kind="historical")

    # Record is not a dict (e.g. integer 42)
    bad_report = copy.deepcopy(valid_report)
    bad_report["records"] = [42]
    with pytest.raises(ValueError):
        render_text(bad_report)

    # Record source is None
    bad_report = copy.deepcopy(valid_report)
    bad_rec = _make_record()
    bad_rec["source"] = None
    bad_report["records"] = [bad_rec]
    with pytest.raises(ValueError):
        render_text(bad_report)

    # Record missing source key
    bad_report = copy.deepcopy(valid_report)
    bad_rec = _make_record()
    del bad_rec["source"]
    bad_report["records"] = [bad_rec]
    with pytest.raises(ValueError):
        render_text(bad_report)
