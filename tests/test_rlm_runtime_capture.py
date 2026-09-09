"""Focused tests for the RLM runtime capture boundary (HAR-10).

Pure synchronous calls: ``evallab.rlm_runtime.capture`` and
``evallab.rlm_runtime.root`` are pure functions with no I/O and no async
code, so every test calls them directly.

Coverage:
1. A healthy root turn record is eligible and mirrors the reference
   qualification output (arrays, identity, routing, weight span, counters).
2. Behavior-policy logprob audit: NaN / positive / bool / non-finite values
   in mask==1 slots are rejected with per-slot reasons; zero is valid;
   mask==0 slots are excluded without needing logprobs.
3. Length mismatches (logprobs vs mask, mask vs response region) are
   ineligible with explicit reasons and alignment-gap indices.
4. Missing/unknown weight yields an incomplete span but never gates the
   structural eligibility decided on the arrays.
5. Origin labels are preserved verbatim; fixture origins are never eligible
   as live evidence; diagnostic lineage is rejected; unknown/missing origins
   are reported, never guessed; synthetic proxy origins stay labeled
   synthetic.
6. OpenAI binding: matching lengths eligible; mismatched lengths ineligible;
   token ids come only from the caller's tokenizer (never derived from
   text); the actor identity is required and preserved; raw text never
   enters the record.
"""

from __future__ import annotations

import hashlib
import math

import pytest

from evallab.rlm_runtime.capture import qualify, record_root_turn
from evallab.rlm_runtime.root import extract_openai_turn

WEIGHT = {"min_global_steps": 5, "max_global_steps": 5}
SAMPLING = {"temperature": 0.2}


def _healthy_record(**overrides):
    params = dict(
        session_id="healthy",
        prompt_ids=[11, 12, 13, 14],
        response_ids=[21, 22, 23, 24, 25],
        response_mask=[1, 1, 1, 0, 1],
        response_logprobs=[-0.1, -0.2, -0.3, -1.5, -0.5],
        weight=WEIGHT,
        origin="live_trial",
        sampling=SAMPLING,
    )
    params.update(overrides)
    return record_root_turn(**params)


TOKEN_IDS = [101, 102, 103, 104, 201, 202, 203, 204, 205]
LOGPROBS = [-0.1, -0.2, -0.3, -1.0, -0.4]
MASK = [1, 1, 1, 0, 1]


def _openai_record(**overrides):
    params = dict(
        session_id="sess-1",
        prompt_text="Solve the task.",
        response_text="Done.",
        token_ids=list(TOKEN_IDS),
        logprobs=list(LOGPROBS),
        mask=list(MASK),
        weight=WEIGHT,
        origin="live_trial",
        actor="root",
    )
    params.update(overrides)
    return extract_openai_turn(**params)


def test_healthy_root_turn_record_is_eligible():
    record = _healthy_record()
    assert record["traj_acc_ids"] == [11, 12, 13, 14, 21, 22, 23, 24, 25]
    assert record["initial_prompt_token_len"] == 4
    assert record["traj_response_routing"] == []
    assert record["disable_proxy_trajectory"] is False
    assert record["trajectory_logprobs_error"] is None
    assert record["origin"] == "live_trial"
    assert record["weight"] == WEIGHT
    assert record["sampling"] == SAMPLING
    assert record["num_calls"] == 1
    assert record["num_aborts"] == 0
    assert record["num_preempted"] == 0
    assert record["total_prompt_tokens"] == 4
    assert record["total_completion_tokens"] == 5
    assert record["context_overflow"] is False

    qual = qualify(record)
    assert qual["session_id"] == "healthy"
    assert qual["record_kind"] == "proxy_capture"
    assert qual["eligible_for_training"] is True
    assert qual["rejections"] == []
    assert qual["missing"] == []
    assert qual["excluded_masked_out"] == [{"index": 3, "reason": "masked_out_excluded"}]
    assert qual["sampling_fidelity"] == "live_model"
    assert qual["sampling_lineage"] == "original_generation_records"
    assert qual["routing"] == {"present": False, "reason": "absent_r3_off_or_non_moe"}
    assert qual["arrays"]["n_acc_ids"] == 9
    assert qual["arrays"]["n_mask"] == 5
    assert qual["arrays"]["n_logprobs"] == 5
    assert qual["arrays"]["masked_in"] == 4
    assert qual["arrays"]["masked_out"] == 1
    assert qual["arrays"]["invalid_mask1_slots"] == 0
    assert qual["arrays"]["n_prompt_ids"] == 4
    assert qual["arrays"]["n_response_ids"] == 5
    assert qual["identity"]["prompt_ids_present"] is True
    assert qual["identity"]["response_ids_present"] is True
    assert qual["weight_span"] == {
        "min_global_steps": 5,
        "max_global_steps": 5,
        "origin": "bound",
        "span_complete": True,
    }


@pytest.mark.parametrize(
    "bad_logprob",
    [float("nan"), 0.5, False, -math.inf],
    ids=["nan", "positive", "bool", "non-finite"],
)
def test_invalid_mask_in_logprob_is_rejected_with_slot_reason(bad_logprob):
    record = _healthy_record(response_logprobs=[-0.1, bad_logprob, -0.3, -1.5, -0.5])
    qual = qualify(record)
    assert qual["eligible_for_training"] is False
    assert "missing_behavior_policy_probabilities" in qual["rejections"]
    assert {"index": 1, "reason": "invalid_logprob_missing"} in qual["missing"]
    assert qual["arrays"]["invalid_mask1_slots"] == 1
    # The masked-out slot is excluded regardless of logprob validity.
    assert qual["excluded_masked_out"] == [{"index": 3, "reason": "masked_out_excluded"}]


@pytest.mark.parametrize(
    "ok_logprob",
    [0.0, -0.0, 0, -1e-9],
    ids=["zero", "negative-zero", "int-zero", "tiny"],
)
def test_zero_logprob_is_a_valid_behavior_probability(ok_logprob):
    record = _healthy_record(response_logprobs=[ok_logprob, -0.2, -0.3, -1.5, -0.5])
    assert qualify(record)["eligible_for_training"] is True


def test_masked_out_slots_do_not_require_logprobs():
    record = _healthy_record(response_logprobs=[-0.1, -0.2, -0.3, None, -0.5])
    qual = qualify(record)
    assert qual["eligible_for_training"] is True
    assert qual["missing"] == []
    assert qual["excluded_masked_out"] == [{"index": 3, "reason": "masked_out_excluded"}]


def test_malformed_mask_value_is_reported_missing():
    record = _healthy_record(response_mask=[1, 1, 2, 0, 1])
    qual = qualify(record)
    assert qual["eligible_for_training"] is False
    assert {"index": 2, "reason": "malformed_mask_missing"} in qual["missing"]
    assert "missing_behavior_policy_probabilities" in qual["rejections"]


def test_logprobs_shorter_than_mask_is_rejected_with_alignment_gap():
    record = _healthy_record(response_logprobs=[-0.1, -0.2, -0.3, -0.4])
    qual = qualify(record)
    assert qual["eligible_for_training"] is False
    assert "consumer_gate_structural_mismatch" in qual["rejections"]
    assert qual["arrays"]["mask_logprobs_length_match"] is False
    assert qual["arrays"]["n_logprobs"] == 4
    assert {"index": 4, "reason": "alignment_gap_missing"} in qual["missing"]


def test_mask_length_off_response_region_is_rejected():
    record = _healthy_record(response_ids=[21, 22, 23])
    qual = qualify(record)
    assert qual["eligible_for_training"] is False
    assert "consumer_gate_structural_mismatch" in qual["rejections"]
    # Structural failure keeps the prompt/response split unreported.
    assert "n_prompt_ids" not in qual["arrays"]


def test_empty_prompt_split_is_rejected():
    record = _healthy_record(prompt_ids=[])
    qual = qualify(record)
    assert qual["eligible_for_training"] is False
    assert "consumer_gate_structural_mismatch" in qual["rejections"]


def test_empty_response_split_is_rejected_after_audit():
    record = _healthy_record(response_ids=[], response_mask=[], response_logprobs=[])
    qual = qualify(record)
    assert qual["eligible_for_training"] is False
    assert "empty_prompt_or_response_split" in qual["rejections"]
    assert qual["identity"]["response_ids_present"] is False
    assert qual["identity"]["prompt_ids_present"] is True


@pytest.mark.parametrize(
    ("weight", "span_origin", "span_min", "span_max"),
    [
        (None, "missing", None, None),
        ({}, "missing", None, None),
        ({"unrelated_metric": 7}, "missing", None, None),
        ({"min_global_steps": True, "max_global_steps": 5}, "partial", None, 5),
        ({"min_global_steps": 5}, "partial", 5, None),
        ({"min_global_steps": 9, "max_global_steps": 3}, "inconsistent", 9, 3),
        ({"min_global_steps": 5, "max_global_steps": 5}, "bound", 5, 5),
    ],
    ids=[
        "none",
        "empty-dict",
        "unknown-keys",
        "bool-min-partial",
        "min-only-partial",
        "inverted-inconsistent",
        "bound",
    ],
)
def test_weight_span_is_reported_without_gating_eligibility(
    weight, span_origin, span_min, span_max
):
    record = _healthy_record(weight=weight)
    if weight is None:
        # Missing weight stays None; it is never imputed as zero.
        assert record["min_global_steps"] is None
        assert record["max_global_steps"] is None
    qual = qualify(record)
    assert qual["eligible_for_training"] is True
    assert qual["rejections"] == []
    assert qual["weight_span"] == {
        "min_global_steps": span_min,
        "max_global_steps": span_max,
        "origin": span_origin,
        "span_complete": span_origin == "bound",
    }


def test_origin_label_preserved_verbatim_and_fixture_never_eligible_as_live():
    record = _healthy_record(origin="fixture-cpu-logic-proof")
    assert record["origin"] == "fixture-cpu-logic-proof"
    assert record["sampling_fidelity"] == "fixture"
    qual = qualify(record)
    assert qual["record_origin"] == "fixture-cpu-logic-proof"
    assert qual["sampling_fidelity"] == "fixture"
    assert qual["eligible_for_training"] is False
    assert "fixture_origin_not_live" in qual["rejections"]
    # Arrays are still audited and healthy; the failure is provenance-only.
    assert qual["missing"] == []
    assert qual["arrays"]["masked_in"] == 4


def test_diagnostic_origin_is_not_behavior_policy_evidence():
    record = _healthy_record(origin="diagnostic_recompute")
    qual = qualify(record)
    assert qual["sampling_lineage"] == "diagnostic_recomputation"
    assert qual["sampling_fidelity"] == "diagnostic_only"
    assert qual["eligible_for_training"] is False
    assert "diagnostic_not_behavior_policy" in qual["rejections"]
    # Early return mirrors the reference decoder: no identity audit.
    assert qual["identity"] is None


def test_unknown_or_missing_origin_is_rejected_not_guessed():
    unknown = qualify(_healthy_record(origin="made_up_origin"))
    assert unknown["eligible_for_training"] is False
    assert "unknown_record_origin" in unknown["rejections"]
    assert unknown["sampling_fidelity"] == "unknown_origin"

    missing = _healthy_record()
    del missing["origin"]
    gone = qualify(missing)
    assert gone["eligible_for_training"] is False
    assert "missing_record_origin" in gone["rejections"]


def test_synthetic_proxy_origin_stays_eligible_as_capture_logic_proof_only():
    # Mirrors the reference decoder's healthy sample: a synthetic-server
    # record can prove capture logic; its fidelity label — never its
    # eligibility — says the sampler was not a live model.
    record = _healthy_record(origin="cpu_proxy_session_synthetic")
    qual = qualify(record)
    assert qual["eligible_for_training"] is True
    assert qual["sampling_fidelity"] == "synthetic_server"
    assert qual["sampling_lineage"] == "original_generation_records"


def test_disabled_trajectory_and_capture_error_are_rejected():
    disabled = _healthy_record()
    disabled["disable_proxy_trajectory"] = True
    qual = qualify(disabled)
    assert "proxy_trajectory_disabled" in qual["rejections"]
    assert qual["identity"]["disable_proxy_trajectory"] is True
    assert qual["eligible_for_training"] is False

    errored = _healthy_record()
    errored["trajectory_logprobs_error"] = "logprobs unavailable"
    qual = qualify(errored)
    assert "capture_error:logprobs unavailable" in qual["rejections"]
    assert qual["eligible_for_training"] is False


def test_routing_present_only_when_it_matches_the_mask():
    record = _healthy_record()
    record["traj_response_routing"] = [{"expert": 0}, None, {"expert": 1}, None, None]
    qual = qualify(record)
    assert qual["routing"] == {"present": True, "n_rows": 5, "non_null_rows": 2}
    assert qual["eligible_for_training"] is True  # routing never gates

    mismatched = _healthy_record()
    mismatched["traj_response_routing"] = [{"expert": 0}]
    assert qualify(mismatched)["routing"]["present"] is False


def test_unrecognized_shape_is_rejected_not_raised():
    qual = qualify({"session_id": "x"})
    assert qual["record_kind"] == "unknown"
    assert qual["eligible_for_training"] is False
    assert qual["rejections"] == ["unrecognized_record_shape"]
    assert qual["identity"] is None
    assert qual["arrays"] is None
    assert qual["sampling_lineage"] == "diagnostic_recomputation"


def test_invalid_arrays_are_reported_never_synthesized():
    record = _healthy_record()
    record["traj_response_mask"] = None
    qual = qualify(record)
    assert qual["eligible_for_training"] is False
    assert "consumer_gate_structural_mismatch" in qual["rejections"]
    assert qual["arrays"]["n_mask"] is None
    # No audit rows are fabricated for arrays that do not exist.
    assert qual["missing"] == []
    assert qual["excluded_masked_out"] == []


def test_record_root_turn_rejects_wrong_argument_types():
    with pytest.raises(TypeError):
        _healthy_record(response_mask="11101")
    with pytest.raises(TypeError):
        _healthy_record(session_id="")
    with pytest.raises(TypeError):
        _healthy_record(origin="")
    with pytest.raises(TypeError):
        _healthy_record(weight=[1, 2])


def test_openai_turn_with_matching_lengths_is_eligible():
    record = _openai_record()
    assert record["traj_acc_ids"] == TOKEN_IDS
    assert record["initial_prompt_token_len"] == 4
    assert record["actor"] == "root"
    assert record["binding"] == "openai_chat_completion"
    assert record["prompt_text_sha256"] == hashlib.sha256(b"Solve the task.").hexdigest()
    assert record["response_text_sha256"] == hashlib.sha256(b"Done.").hexdigest()
    # Raw text never enters the record (AGENTS.md rule 22).
    assert "prompt_text" not in record
    assert "response_text" not in record
    # This binding has no sampling surface; missingness stays explicit.
    assert record["sampling"] is None
    qual = qualify(record)
    assert qual["eligible_for_training"] is True
    assert qual["actor"] == "root"


def test_openai_token_ids_are_never_inferred_from_text():
    # Word/character counts of the texts deliberately disagree with the
    # caller's tokenizer ids; any derivation from text would differ.
    record = _openai_record(
        prompt_text="one two three four",
        response_text="five",
        token_ids=[7, 8, 9, 10, 11],
        logprobs=[-0.3, -0.9],
        mask=[1, 1],
    )
    assert record["traj_acc_ids"] == [7, 8, 9, 10, 11]
    assert record["initial_prompt_token_len"] == 3
    assert qualify(record)["eligible_for_training"] is True


def test_openai_actor_is_required_and_distinguishes_identities():
    with pytest.raises(TypeError):
        extract_openai_turn(
            session_id="sess-1",
            prompt_text="p",
            response_text="r",
            token_ids=[1, 2, 3],
            logprobs=[-0.1],
            mask=[1],
            weight=None,
            origin="live_trial",
        )
    with pytest.raises(TypeError):
        _openai_record(actor="")

    root = qualify(_openai_record(actor="root"))
    worker = qualify(_openai_record(actor="worker-fixed"))
    assert root["actor"] == "root"
    assert worker["actor"] == "worker-fixed"


def test_openai_logprobs_mask_mismatch_is_ineligible():
    record = _openai_record(logprobs=[-0.1, -0.2, -0.3, -0.4])
    qual = qualify(record)
    assert qual["eligible_for_training"] is False
    assert "consumer_gate_structural_mismatch" in qual["rejections"]
    assert qual["arrays"]["mask_logprobs_length_match"] is False
    assert {"index": 4, "reason": "alignment_gap_missing"} in qual["missing"]


def test_openai_mask_longer_than_token_ids_is_ineligible_without_repair():
    record = _openai_record(token_ids=[101, 102, 103])
    qual = qualify(record)
    assert record["traj_acc_ids"] == [101, 102, 103]  # nothing padded or dropped
    assert record["initial_prompt_token_len"] == 0
    assert qual["eligible_for_training"] is False
    assert "consumer_gate_structural_mismatch" in qual["rejections"]
    assert qual["arrays"]["n_acc_ids"] == 3


def test_openai_empty_response_region_is_ineligible():
    record = _openai_record(mask=[], logprobs=[])
    qual = qualify(record)
    assert qual["eligible_for_training"] is False
    assert "empty_prompt_or_response_split" in qual["rejections"]
