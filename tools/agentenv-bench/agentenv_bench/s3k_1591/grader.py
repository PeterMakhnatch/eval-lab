"""Verbatim upstream rules, parametrized variants, and a labeled report proxy."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from agentenv_bench.s3k_1591.controls import money
from agentenv_bench.world import sha256_file

DB_NAME = "bluesky_approval_workflow"


def fingerprint(rows) -> str:
    parts = sorted(hashlib.sha256(repr(tuple(row)).encode()).hexdigest() for row in rows)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _db(work_dir: Path) -> sqlite3.Connection:
    path = (work_dir / "system" / DB_NAME / "state.db").resolve()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _row(criterion: str, result: bool, detail: str, source: str, weight: float = 1.0) -> dict:
    return {"criterion": criterion, "result": result, "score": float(result),
            "weight": weight, "detail": detail, "source": source}


def _param_rules(work_dir: Path, params: dict) -> list[dict]:
    case_id = params["case_id"]
    with closing(_db(work_dir)) as con:
        row = con.execute("SELECT * FROM merit_approval_cases WHERE case_id=?", (case_id,)).fetchone()
        events = con.execute("SELECT * FROM approval_events WHERE case_id=? ORDER BY event_timestamp,event_id", (case_id,)).fetchall()
        other_cases = con.execute("SELECT * FROM merit_approval_cases WHERE case_id<>?", (case_id,)).fetchall()
        other_events = con.execute("SELECT * FROM approval_events WHERE case_id<>?", (case_id,)).fetchall()
    expected_fields = {
        "case_status": "Compensation Audit Review", "current_approver_role": "Compensation",
        "row_version": params.get("expected_row_version", params["pre_row_version"] + 1),
        "employee_display_name": params["employee"], "area_id": params["area_id"],
        "recommendation_type": params["recommendation_type"],
        "recurring_amount_cents": params["recurring_amount_cents"],
        "lump_sum_amount_cents": params["lump_sum_amount_cents"], "eligibility_code": params["eligibility_code"],
    }
    mismatch = [field for field, value in expected_fields.items() if row is None or row[field] != value]
    state = _row("case_post_state", not mismatch, "mismatched fields=" + repr(mismatch), "param_rule")
    original_ids = set(params["pre_case_event_ids"])
    original = [event for event in events if event["event_id"] in original_ids]
    new = [event for event in events if event["event_id"] not in original_ids]
    event_ok = (
        len(original) == params["pre_case_event_count"]
        and fingerprint(original) == params["pre_case_event_fingerprint"]
        and len(new) == 1
        and new[0]["event_type"] == "RESUBMITTED" and new[0]["decision_code"] == "RESUBMIT"
        and new[0]["prior_status"] == "Returned to Manager"
        and new[0]["resulting_status"] == "Compensation Audit Review"
        and new[0]["actor_role"] == "Manager" and new[0]["is_valid"] == 1
    )
    event = _row("resubmission_event", event_ok,
                 f"original_case_events={len(original)}, new_case_events={len(new)}", "param_rule")
    others_ok = (
        len(other_cases) == params["other_case_count"] and fingerprint(other_cases) == params["other_case_fingerprint"]
        and len(other_events) == params["other_event_count"] and fingerprint(other_events) == params["other_event_fingerprint"]
    )
    others = _row("unchanged_other_cases", others_ok,
                  f"other_cases={len(other_cases)}, other_events={len(other_events)}", "param_rule")
    return [state, event, others]


def _report_proxy(report: str, params: dict) -> dict:
    """Limited lexical facts check, not the upstream LLM grader or semantic judge."""
    normalized = " ".join(report.split())
    lower = normalized.lower()
    missing = []
    for field in ("case_id", "employee", "area_id"):
        if params[field].lower() not in lower:
            missing.append(field)
    if not re.search(r"\b" + re.escape(params["eligibility_code"]) + r"\b", normalized, re.IGNORECASE):
        missing.append("eligibility_code")
    treatment = params["recommendation_type"]
    amount = params["lump_sum_amount_cents"] if treatment == "Lump Sum" else params["recurring_amount_cents"]
    if money(amount) not in normalized and money(amount).replace(",", "") not in normalized:
        missing.append("recommendation_amount")
    if treatment == "Recurring" and not ("recurring" in lower and "base-salary" in lower):
        missing.append("recurring_base_salary_treatment")
    elif treatment != "Recurring" and treatment.lower() not in lower:
        missing.append("recommendation_type")
    for token in ("RESUBMIT", "Returned to Manager", "Compensation Audit Review"):
        if not re.search(r"\b" + re.escape(token) + r"(?:TED)?\b", normalized, re.IGNORECASE):
            missing.append(token)
    if not re.search(r"current approver\s*:\s*Compensation\b|Compensation\s+(?:is|as)\s+(?:the\s+)?current approver\b", normalized, re.IGNORECASE):
        missing.append("current_approver_compensation")
    approval_clauses = [clause for clause in re.split(r"(?<=[.!?])\s+|[;\n]", report)
                        if re.search(r"VP\s*/\s*Dean|VP approval|Dean approval", clause, re.IGNORECASE)]
    denial = re.compile(r"\bnot\b|\bno\b|\bnever\b|\bwithout\b|rather than", re.IGNORECASE)
    if not approval_clauses or any(not denial.search(clause) for clause in approval_clauses):
        missing.append("explicit_no_vp_dean_approval_claim")
    for stage in ("SRAC Release Queue", "Released to Payroll"):
        if stage.lower() in lower:
            missing.append("no_later_workflow_stage_claim")
    detail = (
        "Deterministic lexical report-facts PROXY, NOT the upstream LLM final_report grader; "
        "no model calls. Missing/contradictory facts=" + repr(missing)
    )
    return _row("report_facts", not missing, detail, "report_proxy")


def grade(work_dir: Path, final_report: str, params: dict) -> list[dict]:
    if params.get("upstream"):
        meta_path = Path(params["verifier_meta_path"])
        expected_hash = params["verifier_meta_sha256"]
        if sha256_file(meta_path) != expected_hash:
            raise ValueError("upstream verifier_meta.json changed after parameter generation")
        meta = json.loads(meta_path.read_text())
        namespace: dict = {"__name__": "bench_verbatim_upstream_rules"}
        # This exact string is compiled/executed without substitution or rewriting.
        exec(compile(meta["check_code"], str(meta_path) + ":check_code", "exec"), namespace)
        rows = []
        for item in meta["items"]:
            if item["method"] != "rule":
                continue
            result = namespace[item["fn"]](work_dir / "workspace")
            score = float(result["score"])
            rows.append({"criterion": item["id"], "result": score == 1.0, "score": score,
                         "weight": float(item.get("weight", 1.0)), "detail": result["detail"],
                         "source": "upstream_rule"})
    else:
        rows = _param_rules(work_dir, params)
    changed = []
    for system, expected_hash in params["other_system_db_sha256"].items():
        path = work_dir / "system" / system / "state.db"
        if not path.is_file() or sha256_file(path) != expected_hash:
            changed.append(system)
    rows.append(_row(
        "supplemental_not_upstream_world_isolation", not changed,
        "Bench-only supplement, NOT upstream: unchanged non-BlueSky DB bytes; changed=" + repr(changed),
        "param_rule",
    ))
    rows.append(_report_proxy(final_report, params))
    return rows


def verdicts(rows: list[dict]) -> dict:
    upstream = [row for row in rows if row["source"] == "upstream_rule"]
    rules = [row for row in rows if row["source"] != "report_proxy"]
    task_rules = [row for row in rules if not row["criterion"].startswith("supplemental_")]
    reports = [row for row in rows if row["source"] == "report_proxy"]
    passed = bool(rules) and all(row["result"] for row in rules)
    return {
        "upstream_verdict": ("PASS" if all(row["result"] for row in upstream) else "FAIL") if upstream else "NOT_APPLICABLE",
        "task_rule_verdict": "PASS" if task_rules and all(row["result"] for row in task_rules) else "FAIL",
        "bench_verdict": "PASS" if passed else "FAIL", "pass_rules": passed,
        "report_proxy_pass": bool(reports) and all(row["result"] for row in reports),
        "pass_with_report": passed and bool(reports) and all(row["result"] for row in reports),
    }
