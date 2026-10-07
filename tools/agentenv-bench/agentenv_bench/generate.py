"""Generate data-level tasks from the pristine pinned BlueSky world."""
from __future__ import annotations

import argparse
import json
import platform
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from agentenv_bench import __version__
from agentenv_bench.s3k_1591.controls import SYSTEM, money
from agentenv_bench.s3k_1591.grader import fingerprint
from agentenv_bench.world import direct_call, fetch_world, sha256_file, world_receipt

UPSTREAM_CASE_ID = "MAC-FY26-0274"
UPSTREAM_CONSTANTS = {
    "pre_row_version": 8,
    "other_case_count": 59,
    "other_case_fingerprint": "c225f1fce4aa5fccb12e6eb06e2264971e0dfcc976cbb050c82092c0550a717f",
    "other_event_count": 78,
    "other_event_fingerprint": "e1abf2f9df394a4b2bf62edac7daf279760db67a4024d71a942354341e053874",
}
SELECTION_RULE = (
    "Operational status = Returned to Manager (latest valid event by event_timestamp DESC, event_id DESC, "
    "falling back to stored case_status), eligibility_code = ELIGIBLE, and pristine submit_merit_batch "
    "dry_run=True accepts the exact DB row_version. This follows the upstream submit tool's INVALID_STATUS, "
    "INELIGIBLE_CASE and ROW_VERSION_CONFLICT guards; no recommendation facts are rewritten."
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def build_params(work_dir: Path, case_id: str, task_dir: Path, *, upstream: bool = False) -> dict:
    """Compute all baselines before any control acts; never infer them post-run."""
    with closing(_connect(work_dir / "system" / SYSTEM / "state.db")) as connection:
        row = connection.execute("SELECT * FROM merit_approval_cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise ValueError(f"missing pristine case: {case_id}")
        case = dict(row)
        prior_events = connection.execute("SELECT * FROM approval_events WHERE case_id=?", (case_id,)).fetchall()
        other_cases = connection.execute("SELECT * FROM merit_approval_cases WHERE case_id<>?", (case_id,)).fetchall()
        other_events = connection.execute("SELECT * FROM approval_events WHERE case_id<>?", (case_id,)).fetchall()
    with closing(_connect(work_dir / "system/workday_hcm/state.db")) as connection:
        unrelated = connection.execute(
            "SELECT employee_id,merit_cycle_id,reconciliation_status FROM employee_compensation_snapshot "
            "WHERE snapshot_status='REVIEWED' AND employee_id<>? "
            "AND reconciliation_status IN ('NOT_SENT','PENDING_ANAPLAN_MATCH','MATCHED') "
            "ORDER BY employee_id LIMIT 1", (case["employee_id"],),
        ).fetchone()
    if unrelated is None:
        raise ValueError("no unrelated reviewed Workday employee for cross-system negative control")
    prior_status = unrelated["reconciliation_status"]
    params = {
        "case_id": case_id, "employee": case["employee_display_name"], "employee_id": case["employee_id"],
        "manager_id": case["manager_id"], "recommendation_id": case["recommendation_id"], "area_id": case["area_id"],
        "recommendation_type": case["recommendation_type"], "recurring_amount_cents": case["recurring_amount_cents"],
        "lump_sum_amount_cents": case["lump_sum_amount_cents"], "eligibility_code": case["eligibility_code"],
        "pre_row_version": case["row_version"], "expected_row_version": case["row_version"] + 1,
        "pre_case_event_count": len(prior_events), "pre_case_event_fingerprint": fingerprint(prior_events),
        "pre_case_event_ids": [event["event_id"] for event in prior_events],
        "other_case_count": len(other_cases), "other_case_fingerprint": fingerprint(other_cases),
        "other_event_count": len(other_events), "other_event_fingerprint": fingerprint(other_events),
        "other_system_db_sha256": {
            path.parent.name: sha256_file(path) for path in sorted((work_dir / "system").glob("*/state.db"))
            if path.parent.name != SYSTEM
        },
        "unrelated_workday_employee": {
            "employee_id": unrelated["employee_id"], "merit_cycle_id": unrelated["merit_cycle_id"],
            "prior_status": prior_status,
            "next_status": "MATCHED" if prior_status == "PENDING_ANAPLAN_MATCH" else "PENDING_ANAPLAN_MATCH",
        },
        "upstream": upstream,
        "verifier_meta_path": str((task_dir / "tests/verifier/verifier_meta.json").resolve()),
        "verifier_meta_sha256": sha256_file(task_dir / "tests/verifier/verifier_meta.json"),
    }
    amount = case["lump_sum_amount_cents"] if case["recommendation_type"] == "Lump Sum" else case["recurring_amount_cents"]
    params["expected_report_facts"] = {
        "case_id": case_id, "employee": params["employee"], "eligibility": case["eligibility_code"],
        "recommendation_type": case["recommendation_type"], "recommendation_amount": money(amount),
        "area": case["area_id"], "event_type": "RESUBMITTED", "decision_code": "RESUBMIT",
        "prior_status": "Returned to Manager", "resulting_status": "Compensation Audit Review",
        "current_approver": "Compensation", "vp_dean_approval_occurred": False,
    }
    if upstream:
        differences = {key: {"actual": params[key], "expected": value}
                       for key, value in UPSTREAM_CONSTANTS.items() if params[key] != value}
        expected_facts = {"employee": "Marcus Doyle", "area_id": "AREA-IT", "recommendation_type": "Recurring",
                          "recurring_amount_cents": 116950, "lump_sum_amount_cents": 0, "eligibility_code": "ELIGIBLE",
                          "pre_case_event_count": 0}
        differences.update({key: {"actual": params[key], "expected": value}
                            for key, value in expected_facts.items() if params[key] != value})
        if case_id != UPSTREAM_CASE_ID or differences:
            raise ValueError("pristine upstream self-check FAILED: " + json.dumps(differences, sort_keys=True))
    return params


def generate_tasks(task_dir: Path, world_root: Path, out_dir: Path) -> list[dict]:
    """Emit the unchanged upstream task and all valid returned-case substitutions."""
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = world_root / "work"
    upstream_params = build_params(work_dir, UPSTREAM_CASE_ID, task_dir, upstream=True)
    instruction = (task_dir / "instruction.md").read_text()
    with closing(_connect(work_dir / "system" / SYSTEM / "state.db")) as connection:
        rows = connection.execute(
            "SELECT c.*,COALESCE((SELECT e.resulting_status FROM approval_events e "
            "WHERE e.case_id=c.case_id AND e.is_valid=1 ORDER BY e.event_timestamp DESC,e.event_id DESC LIMIT 1),"
            "c.case_status) AS operational_status FROM merit_approval_cases c ORDER BY c.case_id"
        ).fetchall()
    call = direct_call(work_dir)
    returned = [dict(row) for row in rows if row["operational_status"] == "Returned to Manager"]
    selected = []
    rejected = []
    for case in returned:
        if case["eligibility_code"] != "ELIGIBLE":
            rejected.append({"case_id": case["case_id"], "reason": "INELIGIBLE_CASE", "eligibility_code": case["eligibility_code"]})
            continue
        validation = call(SYSTEM, "submit_merit_batch", {
            "case_ids": [case["case_id"]], "actor_id": case["manager_id"],
            "expected_row_versions": {case["case_id"]: case["row_version"]},
            "idempotency_key": f"bench-generation-validation:{case['case_id']}", "dry_run": True,
        })
        if not validation.get("accepted"):
            rejected.append({"case_id": case["case_id"], "reason": validation.get("validation_errors")})
            continue
        selected.append(case)
    if UPSTREAM_CASE_ID not in {case["case_id"] for case in selected}:
        raise ValueError("upstream case is not accepted by pristine nonmutating submit validation")
    tasks = []
    upstream_case = next(case for case in selected if case["case_id"] == UPSTREAM_CASE_ID)
    for case in [upstream_case] + [case for case in selected if case["case_id"] != UPSTREAM_CASE_ID]:
        upstream = case["case_id"] == UPSTREAM_CASE_ID
        task_id = "upstream" if upstream else f"returned-{case['case_id']}"
        params = upstream_params if upstream else build_params(work_dir, case["case_id"], task_dir)
        text = instruction if upstream else instruction.replace("Marcus Doyle", case["employee_display_name"]).replace(UPSTREAM_CASE_ID, case["case_id"])
        task = {"task_id": task_id, "kind": "upstream" if upstream else "positive_data_variant",
                "instruction": text, "params": params, "expected_report_facts": params["expected_report_facts"]}
        directory = out_dir / task_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "instruction.txt").write_text(text)
        write_json(directory / "params.json", params)
        write_json(directory / "expected_report_facts.json", params["expected_report_facts"])
        tasks.append(task)
    ineligible = [case for case in returned if case["eligibility_code"] != "ELIGIBLE"]
    if ineligible:
        # This pinned world has none. Never silently claim a refusal task is implemented.
        raise ValueError("unexpected returned ineligible cases: negative-task policy must be defined before emitting them")
    generation = {
        "schema": "agentenv_bench.generation/v1", "generated_at_utc": utc_now(),
        "tool_versions": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                          "agentenv_bench": __version__, "transport": "stdlib-only generation"},
        "selection_rule": SELECTION_RULE, "pristine_upstream_self_check": "PASS",
        "upstream_constants": UPSTREAM_CONSTANTS, "returned_case_count": len(returned),
        "positive_variant_count": len(tasks) - 1, "negative_variant_count": 0,
        "negative_variants_skipped_reason": (
            "The pristine DB has zero operationally Returned to Manager cases with eligibility_code != ELIGIBLE. "
            "All three returned cases are ELIGIBLE; no eligibility data was synthesized or modified."
        ),
        "selected_case_ids": [case["case_id"] for case in selected], "rejected_cases": rejected,
        "inputs": world_receipt(task_dir, world_root),
    }
    write_json(out_dir / "tasks.json", tasks)
    write_json(out_dir / "generation.json", generation)
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path("runs/agentenv-bench/cache/s3k_1591"))
    args = parser.parse_args()
    root = fetch_world(args.task_dir, args.cache_dir)
    tasks = generate_tasks(args.task_dir, root, args.out)
    print(f"Generated upstream + {len(tasks) - 1} positive data variants; 0 negative variants.")
    print("Negative variants skipped: no returned ineligible cases in the pristine DB.")
    print(f"Manifest: {args.out / 'generation.json'}")


if __name__ == "__main__":
    main()
