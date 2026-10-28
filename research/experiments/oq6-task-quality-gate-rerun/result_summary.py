"""Read-only OQ-6 reporting: paired cohorts and actual native attempt custody.

The scorer, eligibility policies and native accounting remain the frozen science
helpers. No response is reinterpreted and no model request can be made here.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def shared_complete_rows(
    left: list[dict[str, Any]], right: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Intersect one exact route/stage's complete A/B cells, never providers."""
    a = {row["case_id"]: row for row in left}
    b = {row["case_id"]: row for row in right}
    if len(a) != len(left) or len(b) != len(right):
        raise ValueError("duplicate A/B cell identity")
    shared = sorted(
        case_id for case_id in a.keys() & b.keys()
        if a[case_id]["complete"] is True and b[case_id]["complete"] is True
    )
    if any(a[case_id]["key_count"] != b[case_id]["key_count"] for case_id in shared):
        raise ValueError("shared A/B key denominators differ")
    return [a[case_id] for case_id in shared], [b[case_id] for case_id in shared], shared


def paired_ab_report(left: list[dict], right: list[dict], aggregate) -> dict:
    a, b, identities = shared_complete_rows(left, right)
    if not identities:
        return {
            "status": "unavailable", "reason": "empty same-route shared-complete-cell intersection",
            "shared_complete_tasks": 0, "shared_evaluable_keys": 0,
            "shared_case_ids": [], "A": None, "B": None,
        }
    return {
        "status": "observed_subset", "shared_complete_tasks": len(identities),
        "shared_evaluable_keys": sum(row["key_count"] for row in a),
        "shared_case_ids": identities, "A": aggregate(a), "B": aggregate(b),
        "not_full_ALL_DEV_coverage": len(identities) != len(left) or len(identities) != len(right),
    }


def reconcile_native_attempts(root: Path, models: list[str], cases: list[dict], inspect_consumed) -> list[dict]:
    """Count native directories once, independently of final cell.json existence.

    inspect_consumed is the pinned transport's read-only abandoned-attempt
    snapshot, not a dispatcher. It recovers available native usage/session
    evidence; absent usage stays unknown. Never fabricate a native receipt.
    """
    result = []
    visited: set[Path] = set()
    for model in models:
        for arm in ("A", "B"):
            for case in cases:
                base = root / "calls" / model.replace("/", "--") / arm / case["stage"] / case["case_id"]
                for role in ("audit", "scorer"):
                    directory = base / role
                    receipt_path = directory / "receipt.json"
                    marker_path = directory / "call-consumed.json"
                    if not receipt_path.is_file() and not marker_path.is_file():
                        continue
                    identity = directory.resolve()
                    if identity in visited:
                        raise ValueError("duplicate native attempt directory")
                    visited.add(identity)
                    marker = json.loads(marker_path.read_bytes()) if marker_path.is_file() else None
                    receipt = None
                    receipt_error = None
                    if receipt_path.is_file():
                        try:
                            receipt = json.loads(receipt_path.read_bytes())
                            if not isinstance(receipt, dict) or receipt.get("attempt_consumed") is not True:
                                raise ValueError("malformed consumed native receipt")
                        except (ValueError, TypeError) as error:
                            receipt_error = type(error).__name__ + ": " + str(error)
                            receipt = None
                    if receipt is None:
                        if not isinstance(marker, dict):
                            raise ValueError("native evidence exists without a valid consumption marker")
                        receipt = inspect_consumed(directory, marker, "report_consumed_without_complete_receipt")
                    if receipt.get("attempt_consumed") is not True:
                        raise ValueError("consumed attempt snapshot lost its actual marker")
                    result.append({
                        "auditor_route": model, "arm": arm, "stage": case["stage"],
                        "case_id": case["case_id"], "role": role,
                        "directory": str(directory), "receipt": receipt,
                        "complete_native_receipt": receipt_path.is_file() and receipt_error is None,
                        "receipt_error": receipt_error,
                        "receipt_sha256": sha(receipt_path.read_bytes()) if receipt_path.is_file() else None,
                        "consumption_sha256": sha(marker_path.read_bytes()) if marker_path.is_file() else None,
                    })
    return result


def _science(protocol: dict):
    root = Path(protocol["execution"]["science_root"])
    # Check code and admitted data BEFORE importing external selector/helper code.
    for row in protocol["code_files"] + protocol["pre_cross_data_files"]:
        if sha(Path(row["path"]).read_bytes()) != row["sha256"]:
            raise ValueError("frozen reporting dependency drift: " + row["path"])
    sys.path.insert(0, str(root))
    paired = importlib.import_module("paired")
    accounting = importlib.import_module("aggregate").accounting
    transport = importlib.import_module("transport")
    return paired, accounting, transport


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path, required=True)
    args = parser.parse_args(argv)
    data = args.protocol.read_bytes()
    digest = sha(data)
    if digest != args.protocol.with_suffix(".sha256").read_text().strip():
        raise ValueError("frozen reporting protocol drift")
    protocol = json.loads(data)
    paired, accounting, transport = _science(protocol)
    root = Path(protocol["execution"]["raw_root"])
    plan_bytes = Path(protocol["execution"]["plan_path"]).read_bytes()
    if sha(plan_bytes) != protocol["execution"]["plan_sha256"]:
        raise ValueError("frozen plan drift")
    cases = json.loads(plan_bytes)["cases"]
    models = protocol["models"]
    attempts = reconcile_native_attempts(root, models, cases, transport._abandoned)
    quality_rows: dict[tuple[str, str, str], list[dict]] = {}
    attempt_index = {
        (row["auditor_route"], row["arm"], row["stage"], row["case_id"], row["role"]): row
        for row in attempts
    }
    groups = []
    cutoff = dt.datetime.fromisoformat(protocol["execution"]["cutoff_at"].replace("Z", "+00:00"))
    now = dt.datetime.now(dt.UTC)
    for model in models:
        for arm in ("A", "B"):
            for stage in ("dev", "cross"):
                selected = [case for case in cases if case["stage"] == stage]
                rows = []
                final_count = 0
                scheduler_missing = cutoff_pending = unfinalized = 0
                unsent = answered_unfinalized = consumed_unfinalized = 0
                for case in selected:
                    worker = model.replace("/", "--") + "__" + arm
                    path = root / "calls" / model.replace("/", "--") / arm / stage / case["case_id"] / "cell.json"
                    outcome_path = root / "outcomes" / worker / (case["case_id"] + ".json")
                    if path.is_file() or outcome_path.is_file():
                        is_cell = path.is_file()
                        record = json.loads((path if is_cell else outcome_path).read_bytes())
                        if record["freeze_sha256"] != digest or record["route"] != model or record["arm"] != arm or record["case_id"] != case["case_id"]:
                            raise ValueError("quality outcome does not match its frozen identity")
                        if record["paired"]["key_count"] != case["key_count"]:
                            raise ValueError("quality outcome changed its frozen key denominator")
                        if not is_cell and record["status"] == "complete":
                            raise ValueError("complete outcome is missing its final scientific cell")
                        if not is_cell and record["plan_sha256"] != protocol["execution"]["plan_sha256"]:
                            raise ValueError("scheduler outcome changed its frozen plan binding")
                        rows.append(record["paired"])
                        final_count += is_cell
                        scheduler_missing += not is_cell
                        if not is_cell and record.get("reason") == "cutoff_unsent_or_pending":
                            cutoff_pending += case["preparation_status"] == "prepared" and model not in protocol["execution"]["disabled_routes"]
                    else:
                        disabled = model in protocol["execution"]["disabled_routes"]
                        unavailable = case["preparation_status"] != "prepared"
                        audit = attempt_index.get((model, arm, stage, case["case_id"], "audit"))
                        if disabled:
                            status = "disabled_route"
                        elif unavailable:
                            status = "unavailable_input"
                        elif audit is not None:
                            status = "attempted_without_final_quality"
                            unfinalized += 1
                            if audit["receipt"].get("status") == "answered":
                                answered_unfinalized += 1
                            else:
                                consumed_unfinalized += 1
                        else:
                            status = "unsent_pending" if now < cutoff else "cutoff_unsent_missing"
                            unsent += 1
                            unfinalized += 1
                        key_bytes = Path(case["keys_path"]).read_bytes()
                        if sha(key_bytes) != case["keys_sha256"]:
                            raise ValueError("frozen admitted keys drifted during reporting")
                        rows.append(paired.evaluate_cell(case, None, None, json.loads(key_bytes), audit_status=status))
                observed = [row for row in attempts if row["auditor_route"] == model and row["arm"] == arm and row["stage"] == stage]
                audits = [row["receipt"] for row in observed if row["role"] == "audit"]
                scorers = [row["receipt"] for row in observed if row["role"] == "scorer"]
                quality_rows[model, arm, stage] = rows
                groups.append({
                    "auditor_route": model, "arm": arm, "stage": stage,
                    "quality": paired.aggregate_cells(rows), "final_cell_records": final_count,
                    "attempted_audits": len(audits),
                    "answered_audits": sum(row.get("status") == "answered" for row in audits),
                    "scheduler_missing_outcomes": scheduler_missing,
                    "pending_at_cutoff_tasks": cutoff_pending,
                    "unfinalized_planned_tasks": unfinalized,
                    "unsent_unfinalized_tasks": unsent,
                    "answered_without_final_quality_tasks": answered_unfinalized,
                    "consumed_without_final_quality_tasks": consumed_unfinalized,
                    "audit_accounting": accounting(audits), "scorer_accounting": accounting(scorers),
                    "combined_accounting": accounting(audits + scorers),
                    "gate_tokens_shared_not_additive": True,
                })
    comparisons = []
    for model in models:
        for stage in ("dev", "cross"):
            comparisons.append({
                "auditor_route": model, "stage": stage,
                **paired_ab_report(quality_rows[model, "A", stage], quality_rows[model, "B", stage], paired.aggregate_cells),
            })
    value = {
        "schema": "oq6.quota-pending-results/v1", "at": now.isoformat(),
        "freeze_sha256": digest, "study_version": protocol["study_version"],
        "groups": groups, "same_route_AB_shared_complete": comparisons,
        "native_attempts": attempts,
        "combined_accounting": accounting([row["receipt"] for row in attempts]),
        "paid_spend_usd": 0, "subscription_usd": None,
        "all_DEV_complete": all(row["quality"]["full_cohort_complete"] for row in groups if row["stage"] == "dev"),
        "not_certified_negative_unkeyed_tasks": True,
        "repair_qualification_rate": None, "new_grader_executions": 0,
        "protected_bodies_hydrated_or_prompted": 0,
    }
    destination = root / ("results-" + now.strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    destination.open("xb").write((json.dumps(value, sort_keys=True, indent=2) + "\n").encode())
    print(json.dumps({"path": str(destination), "native_attempts": len(attempts), "all_DEV_complete": value["all_DEV_complete"], "groups": len(groups)}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
