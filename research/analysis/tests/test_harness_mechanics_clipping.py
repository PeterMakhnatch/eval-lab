"""Tests for observation clipping and compaction diagnostic (HAR-20).

Validates:
- Exact contract compliance: mechanism, observations, unknowns
- RFC6901 JSON pointer locators into original trajectory fields (/steps/0/...)
- Standalone marker spoofing treated as indicator/hypothesis absent structured metadata
- Post-hoc evidence redaction (evallab-redacted) kept as evidence unknown, not observed clipping
- Unresolved content_ref kept in unknowns without remote/CAS fetching or observed event counts
- Multi-byte Unicode content and character vs byte unit preservation
- Rejection of bool-as-int and invalid amount values
- Nonconsecutive step_id preservation with zero-based array pointer locators
- Step and trajectory level truncation and compaction metadata
- No raw text leaks in summary or locator
- Non-causal unknowns accounting (unretained content unknown, no error/failure claims)
- Clean untruncated and empty trajectory boundaries
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from evallab.trajectory_ir import (
    ObservationResultRecord,
    StepRecord,
    ToolCallRecord,
    TrajectoryIR,
)

_HARNESS_MECHANICS_ROOT = Path(__file__).resolve().parents[1] / "harness-mechanics"
if str(_HARNESS_MECHANICS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_MECHANICS_ROOT))

inspect = importlib.import_module("harness_mechanics.clipping").inspect


def test_schema_contract_compliance() -> None:
    """Diagnostic returns exact contract keys and observation item shape."""
    ir = TrajectoryIR(schema_version="ATIF-v1.7", steps=())
    res = inspect(ir)

    assert set(res.keys()) == {"mechanism", "observations", "unknowns"}
    assert res["mechanism"] == "clipping"
    assert isinstance(res["observations"], list)
    assert isinstance(res["unknowns"], list)
    assert any("empty_trajectory_steps" in u for u in res["unknowns"])


def test_positive_metadata_byte_units() -> None:
    """Explicit observation metadata with byte truncation amounts is observed."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_bytes_01",
                        content="partial log output",
                        content_bytes=18,
                        extra={"truncated": True, "truncated_bytes": 4096},
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)

    meta_obs = [o for o in res["observations"] if o["code"] == "OBSERVATION_METADATA_TRUNCATED"]
    assert len(meta_obs) == 1
    obs = meta_obs[0]
    assert obs["evidence_kind"] == "observed"
    assert obs["step_id"] == 1
    assert obs["tool_call_id"] == "call_bytes_01"
    assert obs["locator"] == "/steps/0/observation/results/0/extra"
    assert "4096 bytes" in obs["summary"]

    # Missing full output is accounted in unknowns, never as an observed event
    assert not any(o["code"] == "MISSING_FULL_OUTPUT" for o in res["observations"])
    assert any("missing_full_output" in u for u in res["unknowns"])
    assert any("unretained_content_unknown" in u for u in res["unknowns"])


def test_positive_metadata_character_units_and_unicode() -> None:
    """Character units are distinct from byte units, especially on multi-byte Unicode."""
    # 7 characters, 21 UTF-8 bytes
    unicode_content = "こんにちは世界"
    assert len(unicode_content) == 7
    assert len(unicode_content.encode("utf-8")) == 21

    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=2,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_unicode_02",
                        content=unicode_content,
                        content_bytes=len(unicode_content.encode("utf-8")),
                        extra={"truncated": True, "truncated_chars": 150},
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)

    meta_obs = next(o for o in res["observations"] if o["code"] == "OBSERVATION_METADATA_TRUNCATED")
    assert meta_obs["evidence_kind"] == "observed"
    assert "150 characters" in meta_obs["summary"]
    # Ensure characters was not conflated with bytes
    assert "bytes" not in meta_obs["summary"]


def test_bool_as_int_and_invalid_amounts_rejected() -> None:
    """Boolean values are rejected as integer amounts; invalid amounts are discarded."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_bool",
                        content="log data",
                        content_bytes=8,
                        extra={
                            "truncated": True,
                            "truncated_bytes": True,  # bool must NOT be parsed as 1 byte
                            "truncated_chars": False,
                            "omitted_lines": -10,
                            "tokens_shed": "invalid_num",
                        },
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)

    meta_obs = [o for o in res["observations"] if o["code"] == "OBSERVATION_METADATA_TRUNCATED"]
    assert len(meta_obs) == 1
    # Truncation flag detected, but no fake amount "1 bytes" extracted
    assert "1 bytes" not in meta_obs[0]["summary"]
    assert "amount:" not in meta_obs[0]["summary"]


def test_standalone_marker_spoof_is_hypothesis_not_observed() -> None:
    """Standalone markers emitted by tasks (incl. DSH response clipped) are hypotheses, not observed clipping."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=5,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_spoof",
                        content="Standard command header\n<<evallab-truncated: 8192 bytes omitted, full deadbeef1234>>",
                    ),
                    ObservationResultRecord(
                        source_call_id="call_spoof_bracket",
                        content="Error output:\n[Output truncated to 500 bytes]",
                    ),
                    ObservationResultRecord(
                        source_call_id="call_dsh_clipped",
                        content=(
                            "File inspection:\n"
                            "<response clipped><NOTE>To save on context only part of this file has been shown to you. "
                            "You should retry this tool after you have searched inside the file with `grep -n`.</NOTE>"
                        ),
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)

    # No marker string alone proves the model saw clipping absent structured harness metadata
    assert not any(o["evidence_kind"] == "observed" for o in res["observations"])
    assert not any(o["code"] == "OBSERVATION_METADATA_TRUNCATED" for o in res["observations"])
    assert not any(o["code"] == "MISSING_FULL_OUTPUT" for o in res["observations"])

    hypotheses = [o for o in res["observations"] if o["evidence_kind"] == "hypothesis"]
    assert len(hypotheses) == 3
    for h in hypotheses:
        assert h["code"] == "OBSERVATION_MARKER_INDICATOR"
        assert h["locator"].startswith("/steps/0/observation/results/")
        assert h["locator"].endswith("/content")

    dsh_hyp = hypotheses[2]
    assert "dsh_response_clipped" in dsh_hyp["summary"]
    assert any("ambiguous_marker_unverified" in u for u in res["unknowns"])


def test_post_hoc_step_message_redaction_is_unknown_not_clipping() -> None:
    """Post-hoc redaction in step.message (Finding 10) is an evidence unknown, not a clipping event."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="user",
                message="Task instructions:\n<<evallab-redacted: 451 bytes, sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319>>",
                observation_results=(),
            ),
            StepRecord(
                step_id=2,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_clean",
                        content="clean output",
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)

    # Message redaction must NOT produce clipping observations
    assert res["observations"] == []
    assert any("evidence_redaction_post_hoc" in u for u in res["unknowns"])
    step_redaction_unknown = next(u for u in res["unknowns"] if "evidence_redaction_post_hoc" in u)
    assert "Step 1" in step_redaction_unknown
    assert "artifact withholding or secret masking" in step_redaction_unknown


def test_post_hoc_evidence_redaction_is_unknown_not_observed_clipping() -> None:
    """Post-hoc evidence redaction (evallab-redacted) is an evidence unknown, NOT harness clipping."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=4,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_redacted",
                        content=(
                            "Log output prefix\n"
                            "<<evallab-redacted: 256 bytes, "
                            "sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef>>"
                        ),
                        content_bytes=100,
                        extra={"evallab_redaction": True},
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)

    # Must NOT report observed clipping
    assert not any(o["code"] == "OBSERVATION_METADATA_TRUNCATED" for o in res["observations"])
    assert not any(o["code"] == "MISSING_FULL_OUTPUT" for o in res["observations"])
    assert res["observations"] == []

    # Must account as an evidence-availability unknown
    assert any("evidence_redaction_post_hoc" in u for u in res["unknowns"])
    redaction_unknown = next(u for u in res["unknowns"] if "evidence_redaction_post_hoc" in u)
    assert "artifact withholding or secret masking" in redaction_unknown
    assert "not runtime harness clipping" in redaction_unknown


def test_unresolved_content_ref_is_unknown_does_not_fetch() -> None:
    """Content reference without inline content is reported as unresolved unknown without remote/CAS fetch or event count."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=6,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_cas",
                        content=None,
                        content_ref="cas://sha256/a1b2c3d4e5f67890",
                        extra={"truncated": True, "truncated_bytes": 1000},
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)

    # Truncation is observed from metadata
    obs = [o for o in res["observations"] if o["code"] == "OBSERVATION_METADATA_TRUNCATED"]
    assert len(obs) == 1
    assert obs[0]["evidence_kind"] == "observed"

    # Unresolved reference is NOT an observation event count
    assert not any(o["code"] == "CONTENT_REF_UNRESOLVED" for o in res["observations"])
    assert not any(o["code"] == "MISSING_FULL_OUTPUT" for o in res["observations"])

    # Unresolved CAS reference without inline content is strictly an unknown
    assert any("content_ref_unresolved" in u for u in res["unknowns"])


def test_available_inline_content_with_synthesized_content_ref() -> None:
    """Available inline content with a synthesized content_ref is not marked as missing/unresolved external data."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=7,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_inline_ok",
                        content="Regular intact command output without any clipping",
                        content_ref="cas://sha256/0123456789abcdef",
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)

    assert res["observations"] == []
    # Content is present inline; must not be flagged as unresolved external data
    assert not any("content_ref_unresolved" in u for u in res["unknowns"])
    assert not any("missing_full_output" in u for u in res["unknowns"])


def test_inline_content_with_clipping_retains_unknown_full_output() -> None:
    """When clipping is indicated on inline content with content_ref, full output extent is retained as unknown."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=8,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_inline_clipped",
                        content="First 10 lines...\nlog tail",
                        content_ref="cas://sha256/fedcba9876543210",
                        extra={"truncated": True, "truncated_bytes": 4096},
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)

    # Truncation observed from metadata
    meta_obs = [o for o in res["observations"] if o["code"] == "OBSERVATION_METADATA_TRUNCATED"]
    assert len(meta_obs) == 1
    # Since content is available inline, it's not an unhydrated external CAS unknown
    assert not any("content_ref_unresolved" in u for u in res["unknowns"])
    # But because clipping occurred, missing full output extent is accounted in unknowns
    assert any("missing_full_output" in u for u in res["unknowns"])


def test_nonconsecutive_step_pointer_behavior() -> None:
    """Locators use zero-based array indices (/steps/0/...) while preserving recorded step_id."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            # Step at array index 0 has step_id=42
            StepRecord(
                step_id=42,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_step42",
                        content="log slice",
                        extra={"truncated": True, "truncated_bytes": 1024},
                    ),
                ),
            ),
            # Step at array index 1 has step_id=105
            StepRecord(
                step_id=105,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_step105",
                        content="[Output truncated]",
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)

    obs42 = next(o for o in res["observations"] if o["step_id"] == 42)
    assert obs42["step_id"] == 42
    # Must use array index 0, NOT step_id 42
    assert obs42["locator"] == "/steps/0/observation/results/0/extra"
    assert obs42["evidence_kind"] == "observed"

    obs105 = next(o for o in res["observations"] if o["step_id"] == 105)
    assert obs105["step_id"] == 105
    # Must use array index 1, NOT step_id 105
    assert obs105["locator"] == "/steps/1/observation/results/0/content"
    assert obs105["evidence_kind"] == "hypothesis"


def test_clean_untruncated_trajectory_has_no_clipping_observations() -> None:
    """Clean trajectory without truncation markers has empty observations and honest unknowns."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="call_clean",
                        content="5 passed, 0 failed in 0.12s",
                        content_bytes=28,
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)

    assert res["observations"] == []
    assert any("unannotated_observation_boundaries" in u for u in res["unknowns"])


def test_no_raw_text_leaks_in_summaries_or_locators() -> None:
    """Sensitive prompt, command, reasoning, and observation text never leaks into report fields."""
    secret_command = "export API_KEY=secret_live_token_abcdef12345"
    secret_output = "INTERNAL_STACK_TRACE: password=super_secret_db_password"

    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                tool_calls=(
                    ToolCallRecord(
                        tool_call_id="c_leak",
                        function_name="bash",
                        arguments={"command": secret_command},
                    ),
                ),
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="c_leak",
                        content=f"{secret_output}\n[Output truncated to 500 bytes]",
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)

    for o in res["observations"]:
        assert secret_command not in o["summary"]
        assert secret_output not in o["summary"]
        assert secret_command not in o["locator"]
        assert secret_output not in o["locator"]


def test_non_causal_unknowns_never_claim_error_or_failure() -> None:
    """Diagnostic explicitly says unretained content unknown and never claims error or failure."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=1,
                source="agent",
                observation_results=(
                    ObservationResultRecord(
                        source_call_id="c_err",
                        content="exit 1\n[Output truncated]",
                    ),
                ),
            ),
        ),
    )
    res = inspect(ir)

    for u in res["unknowns"]:
        assert "caused failure" not in u.lower()
        assert "contained an error" not in u.lower()

    # Explicit statement that unretained content is unknown
    assert any(
        "cannot determine whether omitted output contained errors" in u for u in res["unknowns"]
    )


def test_step_level_truncation_and_compaction_metadata() -> None:
    """Step-level metadata flags (ring_buffer_truncated, compaction) are properly observed."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        steps=(
            StepRecord(
                step_id=7,
                source="agent",
                is_copied_context=True,
                extra={"ring_buffer_truncated": True, "compaction": True},
            ),
        ),
    )
    res = inspect(ir)

    obs_by_code = {o["code"]: o for o in res["observations"]}
    assert "STEP_METADATA_TRUNCATED" in obs_by_code
    assert (
        obs_by_code["STEP_METADATA_TRUNCATED"]["locator"] == "/steps/0/extra/ring_buffer_truncated"
    )
    assert obs_by_code["STEP_METADATA_TRUNCATED"]["evidence_kind"] == "observed"

    assert "STEP_METADATA_COMPACTION" in obs_by_code
    assert obs_by_code["STEP_METADATA_COMPACTION"]["locator"] == "/steps/0/extra"

    assert "STEP_COPIED_CONTEXT" in obs_by_code
    assert obs_by_code["STEP_COPIED_CONTEXT"]["locator"] == "/steps/0/is_copied_context"


def test_trajectory_level_policy_metadata() -> None:
    """Trajectory-level truncation and compaction settings in extra are observed."""
    ir = TrajectoryIR(
        schema_version="ATIF-v1.7",
        extra={"truncation": "tail", "compaction_settings": {"max_tokens": 16000}},
        steps=(),
    )
    res = inspect(ir)

    obs_by_code = {o["code"]: o for o in res["observations"]}
    assert "TRAJECTORY_POLICY_TRUNCATION" in obs_by_code
    assert obs_by_code["TRAJECTORY_POLICY_TRUNCATION"]["locator"] == "/extra/truncation"
    assert obs_by_code["TRAJECTORY_POLICY_TRUNCATION"]["evidence_kind"] == "observed"

    assert "TRAJECTORY_POLICY_COMPACTION" in obs_by_code
    assert obs_by_code["TRAJECTORY_POLICY_COMPACTION"]["locator"] == "/extra/compaction_settings"


def test_invalid_input_raises_value_error() -> None:
    """Non-TrajectoryIR input raises ValueError."""
    with pytest.raises(ValueError, match="Expected TrajectoryIR instance"):
        inspect(None)  # type: ignore[arg-type]
