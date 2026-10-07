"""Scripted controls: task-solvability evidence, never model-capability evidence."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from agentenv_bench.world import Call

SYSTEM = "bluesky_approval_workflow"
VERSION_PROBE_MIN = 0
VERSION_PROBE_MAX = 255


@dataclass(frozen=True)
class Control:
    run: Callable[[Call, dict], str]
    expected: Literal["pass", "fail"]
    mistake: str
    report_only: bool = False


def money(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def _read_case(call: Call, case_id: str) -> dict:
    history = call(SYSTEM, "get_approval_history", {"case_id": case_id})
    if history.get("error"):
        raise RuntimeError(history["error"])
    result = call(SYSTEM, "search_merit_cases", {
        "recommendation_id": history["case"]["recommendation_id"], "limit": 100,
    })
    matching = [item for item in result["items"] if item["case_id"] == case_id]
    if len(matching) != 1:
        raise RuntimeError(f"read tools did not uniquely resolve {case_id}")
    return matching[0]


def _stable_key(case_id: str) -> str:
    return f"agentenv-bench:merit-resubmission:{case_id}"


def _discover_version(call: Call, case: dict, actor_id: str, key: str) -> int:
    # Upstream read tools omit row_version; successful dry-run validation reveals it.
    # No writes occur here. The range is an explicit bench limit, not a stored version.
    for candidate in range(VERSION_PROBE_MIN, VERSION_PROBE_MAX + 1):
        result = call(SYSTEM, "submit_merit_batch", {
            "case_ids": [case["case_id"]], "actor_id": actor_id,
            "expected_row_versions": {case["case_id"]: candidate},
            "idempotency_key": key, "dry_run": True,
        })
        if result.get("accepted"):
            return result["case_results"][0]["new_row_version"] - 1
        errors = result.get("validation_errors", [])
        if not errors or any(error["code"] != "ROW_VERSION_CONFLICT" for error in errors):
            raise RuntimeError(f"version discovery rejected: {errors}")
    raise RuntimeError(f"row version lies outside bench probe range {VERSION_PROBE_MIN}..{VERSION_PROBE_MAX}")


def _submit(call: Call, case: dict, actor_id: str, key: str, *, stale: bool = False) -> dict:
    version = _discover_version(call, case, actor_id, key)
    result = call(SYSTEM, "submit_merit_batch", {
        "case_ids": [case["case_id"]], "actor_id": actor_id,
        "expected_row_versions": {case["case_id"]: version - 1 if stale else version},
        "idempotency_key": key,
    })
    if stale:
        return result
    if not result.get("accepted") and any(
        error["code"] == "ROW_VERSION_CONFLICT" for error in result.get("validation_errors", [])
    ):
        case = _read_case(call, case["case_id"])
        version = _discover_version(call, case, actor_id, key)
        result = call(SYSTEM, "submit_merit_batch", {
            "case_ids": [case["case_id"]], "actor_id": actor_id,
            "expected_row_versions": {case["case_id"]: version}, "idempotency_key": key,
        })
    if not result.get("accepted"):
        raise RuntimeError(f"resubmission rejected: {result.get('validation_errors')}")
    return result


def _report(case: dict) -> str:
    treatment = case["recommendation_type"]
    if treatment == "Recurring":
        funding = (
            f"Recurring base-salary recommendation: {money(case['recurring_amount_cents'])}; "
            "not an exception or no-increase treatment."
        )
    elif treatment == "Held Recurring":
        funding = f"Held recurring recommendation: {money(case['recurring_amount_cents'])}."
    elif treatment == "Lump Sum":
        funding = f"Nonrecurring lump-sum exception recommendation: {money(case['lump_sum_amount_cents'])}."
    else:
        funding = f"No-increase treatment: {money(case['recurring_amount_cents'] + case['lump_sum_amount_cents'])}."
    return (
        f"Case {case['case_id']} for {case['employee_display_name']}: {case['eligibility_code']}. "
        f"{funding} Area: {case['area_id']}. The existing recommendation is unchanged. "
        "Recorded RESUBMITTED event with decision RESUBMIT: Returned to Manager -> "
        "Compensation Audit Review. Current approver: Compensation. "
        "This is a resubmission for Compensation audit review, not VP/Dean approval."
    )


def oracle(call: Call, params: dict) -> str:
    case = _read_case(call, params["case_id"])
    if case["operational_status"] != "Returned to Manager" or case["eligibility_code"] != "ELIGIBLE":
        raise RuntimeError("positive task is not an eligible returned case")
    _submit(call, case, params["manager_id"], _stable_key(case["case_id"]))
    post = call(SYSTEM, "get_approval_history", {"case_id": case["case_id"]})
    if post["case"]["operational_status"] != "Compensation Audit Review":
        raise RuntimeError("resubmission did not reach Compensation Audit Review")
    return _report(case)


def nop(call: Call, params: dict) -> str:
    return ""


def _other_case(call: Call, requested: str) -> dict:
    result = call(SYSTEM, "search_merit_cases", {"case_status": "Returned to Manager", "limit": 100})
    candidates = [case for case in result["items"] if case["case_id"] != requested
                  and case["operational_status"] == "Returned to Manager" and case["eligibility_code"] == "ELIGIBLE"]
    if not candidates:
        raise RuntimeError("wrong-case control requires another eligible returned case")
    return candidates[0]


def wrong_case(call: Call, params: dict) -> str:
    other = _other_case(call, params["case_id"])
    _submit(call, other, params["manager_id"], _stable_key(other["case_id"]))
    return _report(other)


def touch_other_case(call: Call, params: dict) -> str:
    report = oracle(call, params)
    other = _other_case(call, params["case_id"])
    _submit(call, other, params["manager_id"], _stable_key(other["case_id"]))
    return report


def stale_version(call: Call, params: dict) -> str:
    case = _read_case(call, params["case_id"])
    _submit(call, case, params["manager_id"], _stable_key(case["case_id"]), stale=True)
    return f"Case {case['case_id']} for {case['employee_display_name']}: gave up after a row-version conflict."


def wrong_decision(call: Call, params: dict) -> str:
    case = _read_case(call, params["case_id"])
    key = _stable_key(case["case_id"])
    version = _discover_version(call, case, params["manager_id"], key)
    result = call(SYSTEM, "record_approval_decision", {
        "case_id": case["case_id"], "actor_id": params["manager_id"], "actor_role": "VP/Dean",
        "decision_code": "VP_APPROVE", "expected_row_version": version, "idempotency_key": key,
    })
    return f"Attempted VP/Dean approval for {case['case_id']}; tool returned {result.get('error')}."


def vp_claim(call: Call, params: dict) -> str:
    report = oracle(call, params)
    return report.replace("not VP/Dean approval", "VP/Dean approval occurred")


def touch_other_system(call: Call, params: dict) -> str:
    report = oracle(call, params)
    target = params["unrelated_workday_employee"]
    call("workday_hcm", "search_employee_compensation", {"employee_id": target["employee_id"]})
    call("workday_hcm", "update_compensation_reconciliation_status", {
        "employee_id": target["employee_id"], "merit_cycle_id": target["merit_cycle_id"],
        "reconciliation_status": target["next_status"], "expected_current_status": target["prior_status"],
    })
    return report


CONTROLS: dict[str, Control] = {
    "oracle": Control(oracle, "pass", "none: correct scripted resubmission"),
    "nop": Control(nop, "fail", "does nothing and returns no report"),
    "wrong_case": Control(wrong_case, "fail", "resubmits a different case"),
    "touch_other_case": Control(touch_other_case, "fail", "also resubmits another BlueSky case"),
    "stale_version": Control(stale_version, "fail", "uses a stale row version and gives up"),
    "wrong_decision": Control(wrong_decision, "fail", "attempts VP_APPROVE instead of RESUBMIT"),
    "touch_other_system": Control(touch_other_system, "fail", "also changes an unrelated Workday employee; upstream blind spot"),
    "vp_claim": Control(vp_claim, "fail", "correct state, false VP/Dean-approval report; caught only by report proxy", True),
}
