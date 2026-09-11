"""Token-space turn capture for the RLM runtime (HAR-10 capture boundary).

``record_root_turn`` builds proxy_capture-shaped records for root-model turns
and ``qualify`` decides their training eligibility. Both port the semantics
proven out in the Harbor capture-repair lane:

    origin file:   lanes/post-training/capture-repair/src/capture_qualification/decoder.py
    origin repo:   /Users/petermakhnatch/Developer/harbor-rl-exploration
    origin commit: 227a5ed7b6b1efbcca945172b1fce36e34b2a961
    origin branch: fix/capture-logprob-alignment (2026-09-08)

Ported semantics (the origin file carries the full contract narrative):

- ``_valid_logprob``: a usable behavior-policy logprob is a real number
  (never a bool), finite, and ``<= 0``.
- Consumer eligibility gate: a keepable trajectory requires
  ``not disable_proxy_trajectory``, a nonempty ``traj_acc_ids``,
  ``0 < initial_prompt_token_len <= len(traj_acc_ids)``,
  ``len(traj_response_mask) == len(traj_acc_ids) - initial_prompt_token_len``,
  ``len(traj_response_logprobs) == len(traj_response_mask)``, and nonempty
  prompt/response splits. Routing survives only when its length matches the
  mask; otherwise it is reported absent (R3 off / non-MoE), never synthesized.
- Per-slot audit over the response region: mask==1 slots need a valid
  logprob (else ``invalid_logprob_missing``), mask==0 slots are excluded
  (``masked_out_excluded``), any other mask value is a malformed slot.
- Weight span (``min_global_steps``/``max_global_steps``) is reported as
  bound / partial / missing / inconsistent and never gates eligibility;
  eligibility is decided on the arrays alone.
- Provenance labels stay explicit: ``sampling_lineage`` says where the
  numbers came from, ``sampling_fidelity`` says whether the sampling
  distribution was a live model or a stand-in. Eligibility is never upgraded
  by rewriting an origin; missing or invalid arrays are reported with
  reasons, never synthesized, repaired, or imputed.

Deliberate, scoped differences from the origin decoder:

- The origin decoder received ``record_origin`` as a caller argument; here
  the origin label travels inside the record (``record["origin"]``) and
  unknown or missing origins become rejections instead of exceptions,
  because ``qualify`` reads data, not CLI arguments.
- Origins prefixed with ``fixture-`` (Eval Lab fixtures and CPU logic
  proofs) are never eligible as live evidence, even with healthy arrays.
- ``record_root_turn`` stamps one honest counters block for the single turn
  it recorded and carries the caller's ``weight`` and ``sampling`` dicts
  verbatim; unknown stays ``None``, never zero.
"""

from __future__ import annotations

import math
import numbers

#: Recognized origin labels. ``live_trial`` is the only origin whose records
#: can qualify as live-model evidence; the others prove capture logic with
#: synthetic stand-ins and are labeled as such by fidelity.
ORIGINS = (
    "live_trial",
    "cpu_proxy_session_synthetic",
    "actual_source_extraction_smoke",
    "diagnostic_recompute",
)

#: Origins beginning with this prefix are Eval Lab fixtures / CPU logic
#: proofs. They can demonstrate capture wiring, never live-model evidence.
FIXTURE_ORIGIN_PREFIX = "fixture-"

_FIDELITY_BY_ORIGIN = {
    "live_trial": "live_model",
    "cpu_proxy_session_synthetic": "synthetic_server",
    "actual_source_extraction_smoke": "synthetic_probabilities",
    "diagnostic_recompute": "diagnostic_only",
}

FIXTURE_FIDELITY = "fixture"
UNKNOWN_FIDELITY = "unknown_origin"

MASKED_OUT = "masked_out_excluded"
INVALID_LP = "invalid_logprob_missing"
ALIGNMENT_GAP = "alignment_gap_missing"


def _valid_logprob(value: object) -> bool:
    """Mirror of the proxy's ``_valid_logprob`` (real, non-bool, finite, <= 0)."""
    return (
        isinstance(value, numbers.Real)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value <= 0
    )


def _fidelity_label(origin: object) -> str:
    """Map an origin label to its sampling fidelity, never upgrading it."""
    if isinstance(origin, str):
        if origin in _FIDELITY_BY_ORIGIN:
            return _FIDELITY_BY_ORIGIN[origin]
        if origin.startswith(FIXTURE_ORIGIN_PREFIX):
            return FIXTURE_FIDELITY
    return UNKNOWN_FIDELITY


def _record_kind(record: dict) -> str:
    if (
        "traj_acc_ids" in record
        and "traj_response_mask" in record
        and "traj_response_logprobs" in record
    ):
        return "proxy_capture"
    if "response_mask" in record and "response_logprobs" in record:
        return "response_split"
    return "unknown"


def _weight_span(record: dict) -> dict:
    mn = record.get("min_global_steps")
    mx = record.get("max_global_steps")
    mn_ok = isinstance(mn, int) and not isinstance(mn, bool)
    mx_ok = isinstance(mx, int) and not isinstance(mx, bool)
    if mn_ok and mx_ok:
        origin = "bound" if mn <= mx else "inconsistent"
    elif mn_ok or mx_ok:
        origin = "partial"
    else:
        origin = "missing"
    return {
        "min_global_steps": mn if mn_ok else None,
        "max_global_steps": mx if mx_ok else None,
        "origin": origin,
        "span_complete": origin == "bound",
    }


def _audit_slots(mask: list, logprobs: list) -> tuple[list, list, int, int]:
    """Return (missing, excluded, n_in, n_out) over the shared R region."""
    missing: list = []
    excluded: list = []
    n_in = n_out = 0
    for i, (m, lp) in enumerate(zip(mask, logprobs, strict=True)):
        if m == 1:
            n_in += 1
            if not _valid_logprob(lp):
                missing.append({"index": i, "reason": INVALID_LP})
        elif m == 0:
            n_out += 1
            excluded.append({"index": i, "reason": MASKED_OUT})
        else:
            missing.append({"index": i, "reason": "malformed_mask_missing"})
    return missing, excluded, n_in, n_out


def _require_list(value: object, name: str) -> None:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be a list, got {type(value).__name__}")


def record_root_turn(
    *,
    session_id: str,
    prompt_ids: list[int],
    response_ids: list[int],
    response_mask: list[int],
    response_logprobs: list[float],
    weight: dict | None,
    origin: str,
    sampling: dict | None,
) -> dict:
    """Record one root-model turn as a proxy_capture-shaped dict.

    The record keeps the Lego session proxy's token/mask schema (the shared
    contract forbids a second one): ``traj_acc_ids`` is the reading-order
    concatenation P+R, ``initial_prompt_token_len`` is ``len(prompt_ids)``,
    and the mask/logprob arrays cover exactly the response region. Arrays
    are stored verbatim; content problems are left for :func:`qualify` to
    report. ``weight`` and ``sampling`` are preserved verbatim alongside the
    flattened ``min_global_steps``/``max_global_steps`` keys, and missing
    values stay ``None`` rather than being imputed.
    """
    if not isinstance(session_id, str) or not session_id:
        raise TypeError("session_id must be a non-empty string")
    if not isinstance(origin, str) or not origin:
        raise TypeError("origin must be a non-empty string")
    _require_list(prompt_ids, "prompt_ids")
    _require_list(response_ids, "response_ids")
    _require_list(response_mask, "response_mask")
    _require_list(response_logprobs, "response_logprobs")
    if weight is not None and not isinstance(weight, dict):
        raise TypeError("weight must be a dict or None")
    if sampling is not None and not isinstance(sampling, dict):
        raise TypeError("sampling must be a dict or None")

    return {
        "session_id": session_id,
        "traj_acc_ids": list(prompt_ids) + list(response_ids),
        "traj_response_mask": list(response_mask),
        "traj_response_logprobs": list(response_logprobs),
        "traj_response_routing": [],
        "initial_prompt_token_len": len(prompt_ids),
        "disable_proxy_trajectory": False,
        "trajectory_logprobs_error": None,
        "min_global_steps": weight.get("min_global_steps") if weight is not None else None,
        "max_global_steps": weight.get("max_global_steps") if weight is not None else None,
        "num_calls": 1,
        "num_aborts": 0,
        "num_preempted": 0,
        "total_prompt_tokens": len(prompt_ids),
        "total_completion_tokens": len(response_ids),
        "context_overflow": False,
        "diagnostic_logprobs_complete": None,
        "origin": origin,
        "sampling_fidelity": _fidelity_label(origin),
        "weight": weight,
        "sampling": sampling,
    }


def qualify(record: dict) -> dict:
    """Qualify one capture record for training eligibility.

    Mirrors the origin decoder: the consumer structural gate runs first,
    then the per-slot logprob audit, then the prompt/response split check.
    Every failure appends a reason to ``rejections``; the record itself is
    never repaired. A record only reaches ``eligible_for_training == True``
    when it has no rejections at all, which for a ``fixture-`` or unknown
    origin never happens regardless of array health.
    """
    if not isinstance(record, dict):
        raise ValueError("record must be a dict")

    origin = record.get("origin")
    kind = _record_kind(record)

    qual: dict = {
        "session_id": record.get("session_id"),
        "record_kind": kind,
        "record_origin": origin,
        "actor": record.get("actor"),
        "sampling_lineage": (
            "diagnostic_recomputation"
            if origin == "diagnostic_recompute" or kind == "unknown"
            else "original_generation_records"
        ),
        "sampling_fidelity": _fidelity_label(origin),
        "eligible_for_training": False,
        "rejections": [],
        "missing": [],
        "excluded_masked_out": [],
        "identity": None,
        "arrays": None,
        "routing": {"present": False, "reason": "not_evaluated"},
        "weight_span": _weight_span(record),
    }

    if kind == "unknown":
        qual["rejections"].append("unrecognized_record_shape")
        return qual

    if origin is None or not isinstance(origin, str):
        qual["rejections"].append("missing_record_origin")
    elif origin.startswith(FIXTURE_ORIGIN_PREFIX):
        qual["rejections"].append("fixture_origin_not_live")
    elif origin not in ORIGINS:
        qual["rejections"].append("unknown_record_origin")

    if qual["sampling_lineage"] == "diagnostic_recomputation":
        qual["rejections"].append("diagnostic_not_behavior_policy")
        return qual

    if kind == "response_split":
        mask = record.get("response_mask")
        lp = record.get("response_logprobs")
        if not isinstance(mask, list) or not isinstance(lp, list):
            qual["rejections"].append("malformed_response_split")
            return qual
        return _finish_response_region(qual, mask, lp, routing=[])

    # Full proxy_capture record: mirror the consumer eligibility gate first.
    acc = record.get("traj_acc_ids")
    mask = record.get("traj_response_mask")
    lp = record.get("traj_response_logprobs")
    routing = record.get("traj_response_routing") or []
    plen = record.get("initial_prompt_token_len")
    disabled = bool(record.get("disable_proxy_trajectory"))
    lp_error = record.get("trajectory_logprobs_error")

    structural_ok = (
        isinstance(acc, list)
        and len(acc) > 0
        and isinstance(plen, int)
        and not isinstance(plen, bool)
        and 0 < plen <= len(acc)
        and isinstance(mask, list)
        and isinstance(lp, list)
        and len(mask) == len(acc) - plen
        and len(lp) == len(mask)
    )
    qual["arrays"] = {
        "n_acc_ids": len(acc) if isinstance(acc, list) else None,
        "n_mask": len(mask) if isinstance(mask, list) else None,
        "n_logprobs": len(lp) if isinstance(lp, list) else None,
        "n_routing": len(routing) if isinstance(routing, list) else None,
        "initial_prompt_token_len": plen,
        "mask_logprobs_length_match": isinstance(mask, list)
        and isinstance(lp, list)
        and len(mask) == len(lp),
        "response_length_expected": len(acc) - plen
        if isinstance(acc, list) and isinstance(plen, int)
        else None,
    }
    qual["identity"] = {
        "initial_prompt_token_len": plen,
        "num_calls": record.get("num_calls"),
        "num_aborts": record.get("num_aborts"),
        "num_preempted": record.get("num_preempted"),
        "total_prompt_tokens": record.get("total_prompt_tokens"),
        "total_completion_tokens": record.get("total_completion_tokens"),
        "context_overflow": bool(record.get("context_overflow")),
        "disable_proxy_trajectory": disabled,
        "trajectory_logprobs_error": lp_error,
        "diagnostic_logprobs_complete": record.get("diagnostic_logprobs_complete"),
    }

    if disabled:
        qual["rejections"].append("proxy_trajectory_disabled")
    if lp_error:
        qual["rejections"].append(f"capture_error:{lp_error}")
    if not structural_ok:
        qual["rejections"].append("consumer_gate_structural_mismatch")
        # Still audit whatever R region exists so the gap is visible.
        if isinstance(mask, list) and isinstance(lp, list):
            shared = min(len(mask), len(lp))
            missing, excluded, n_in, n_out = _audit_slots(mask[:shared], lp[:shared])
            qual["missing"].extend(missing)
            qual["excluded_masked_out"].extend(excluded)
            for i in range(shared, max(len(mask), len(lp))):
                qual["missing"].append({"index": i, "reason": ALIGNMENT_GAP})
            qual["arrays"]["masked_in"] = n_in
            qual["arrays"]["masked_out"] = n_out
        _finish_routing(qual, routing, mask)
        return qual

    qual = _finish_response_region(qual, mask, lp, routing=routing)

    # P+R split per the consumer: prompt_ids = acc[:P], response = acc[P:].
    prompt_ids = acc[:plen]
    response_ids = acc[plen:]
    qual["arrays"]["n_prompt_ids"] = len(prompt_ids)
    qual["arrays"]["n_response_ids"] = len(response_ids)
    qual["identity"]["prompt_ids_present"] = len(prompt_ids) > 0
    qual["identity"]["response_ids_present"] = len(response_ids) > 0
    if not prompt_ids or not response_ids:
        qual["rejections"].append("empty_prompt_or_response_split")
        qual["eligible_for_training"] = False
    return qual


def _finish_response_region(qual: dict, mask: list, lp: list, routing: list) -> dict:
    missing, excluded, n_in, n_out = _audit_slots(mask, lp)
    qual["missing"].extend(missing)
    qual["excluded_masked_out"].extend(excluded)
    if qual["arrays"] is None:
        qual["arrays"] = {
            "n_mask": len(mask),
            "n_logprobs": len(lp),
            "mask_logprobs_length_match": len(mask) == len(lp),
        }
    qual["arrays"]["masked_in"] = n_in
    qual["arrays"]["masked_out"] = n_out
    qual["arrays"]["invalid_mask1_slots"] = sum(
        1 for m in qual["missing"] if m["reason"] == INVALID_LP
    )
    _finish_routing(qual, routing, mask)
    if any(m["reason"] != MASKED_OUT for m in qual["missing"]):
        qual["rejections"].append("missing_behavior_policy_probabilities")
    elif len(mask) != len(lp):
        qual["rejections"].append("mask_logprobs_length_mismatch")
    qual["eligible_for_training"] = not qual["rejections"]
    return qual


def _finish_routing(qual: dict, routing: object, mask: object) -> None:
    if (
        isinstance(routing, list)
        and isinstance(mask, list)
        and len(routing) == len(mask)
        and any(r is not None for r in routing)
    ):
        qual["routing"] = {
            "present": True,
            "n_rows": len(routing),
            "non_null_rows": sum(1 for r in routing if r is not None),
        }
    else:
        qual["routing"] = {
            "present": False,
            "reason": "absent_r3_off_or_non_moe",
        }
