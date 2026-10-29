"""Explicit OQ10 registration/preparation/role/score commands; metadata-only stdout."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import time
import traceback
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import freeze_study as freeze
import study


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_inputs(code_path: Path, code_sha: str, inputs_sha: str) -> tuple[dict, dict, list[dict], list[dict]]:
    code = freeze.load_code(code_path, code_sha)
    if Path(__file__).resolve() != Path(code["own"]["run_study.py"]["path"]):
        raise ValueError("execute the frozen private driver, not a mutable checkout import")
    path = code_path.parent / "inputs-freeze.json"
    inputs = json.loads(study.verify_pin({"path": str(path), "sha256": inputs_sha}).read_bytes())
    if inputs["schema"] != freeze.SCHEMA_INPUTS or inputs["code_freeze"]["sha256"] != code_sha:
        raise ValueError("input/code freeze binding differs")
    for name in ("code_freeze", "public_cases", "sealed_cases", "selection", "dev_admission"):
        study.verify_pin(inputs[name])
    public = json.loads(Path(inputs["public_cases"]["path"]).read_bytes())["cases"]
    sealed = json.loads(Path(inputs["sealed_cases"]["path"]).read_bytes())["cases"]
    scoring = freeze.load_module("oq10_driver_scoring", code["own"]["scoring.py"])
    scoring.bind_inputs(public, sealed)
    if len(public) != 180 or len(sealed) != 180:
        raise ValueError("frozen 180-task cohort has been narrowed")
    return code, inputs, public, sealed


def register_role_proof(code_path: Path, code_sha: str, inputs_sha: str, *, role: str,
                        registration: Path, result: Path) -> dict:
    code, inputs, _, _ = load_inputs(code_path, code_sha, inputs_sha)
    actual = freeze.positive_route_proof(code, registration=registration, result=result)
    if code["roles"].get(role) != actual:
        raise ValueError("proof is for another model role")
    value = {"schema": "oq10.role-local-proof/v1", "role": role, "route": actual,
             "code_freeze_sha256": code_sha, "input_freeze_sha256": inputs_sha,
             "registration": study.pin(registration), "result": study.pin(result),
             "recorded_at": dt.datetime.now(dt.UTC).isoformat()}
    path = code_path.parent / "role-proofs" / (role + ".json")
    study.dump_new(path, value)
    return study.pin(path)


def run_role(code_path: Path, code_sha: str, inputs_sha: str, *, role: str, proof_sha: str,
             max_steps: int | None = None) -> dict:
    code, inputs, cases, _ = load_inputs(code_path, code_sha, inputs_sha)
    output = code_path.parent
    proof_path = output / "role-proofs" / (role + ".json")
    proof = json.loads(study.verify_pin({"path": str(proof_path), "sha256": proof_sha}).read_bytes())
    if proof["code_freeze_sha256"] != code_sha or proof["input_freeze_sha256"] != inputs_sha or proof["route"] != code["roles"][role]:
        raise ValueError("role-local proof is not bound to this frozen system/cohort")
    study.verify_pin(proof["registration"])
    study.verify_pin(proof["result"])
    freeze.positive_route_proof(code, registration=Path(proof["registration"]["path"]), result=Path(proof["result"]["path"]))
    import evallab
    if Path(evallab.__file__).resolve() != Path(code["config"]["runtime_root"]) / "src/evallab/__init__.py":
        raise ValueError("actual parser import root differs from the frozen runtime")
    native = freeze.load_module("oq10_driver_native", code["own"]["native_route.py"])
    paired = freeze.load_module("oq10_driver_paired", code["retained"]["paired"])
    retained = freeze.load_module("oq10_driver_retained", code["retained"]["prepare_inputs"])
    from evallab.production.tq_worker import TaskQualityLiveWorker
    manifest = json.loads(Path(code["native_runtime"]["path"]).read_bytes())
    route = code["roles"][role]
    steps = 0
    with file_lock(output / "controller-locks" / (role + ".lock")):
        for case in cases:
            final = output / "cells" / role / (case["case_id"] + ".json")
            if final.exists():
                continue
            while not final.exists():
                if max_steps is not None and steps >= max_steps:
                    return {"role": role, "status": "bounded_steps", "steps": steps}
                # The identical provider namespace is shared with the original
                # keyed-only OQ6 controllers. Admission still happens inside the
                # unchanged run_native path, after acquiring this lock.
                lock = Path(code["config"]["provider_lock_root"]) / ("provider-" + route.split("/", 1)[0] + ".lock")
                with file_lock(lock):
                    value = study.step_role(
                        case, role=role, route=route, output=output, system=retained.SYSTEM,
                        runtime_manifest=manifest, native_helper=native, paired=paired,
                        task_quality_worker=TaskQualityLiveWorker,
                        verify_freeze=lambda: load_inputs(code_path, code_sha, inputs_sha),
                    )
                steps += 1
                if value["terminal"]:
                    break
                # No request has been sent for quota/provider pending. Wait for
                # the known reset if supplied; otherwise use the frozen 60s
                # re-admission cadence. Never replay an actually consumed role.
                now_ms = time.time() * 1000
                pools = value.get("native", {}).get("admission", {}).get("pool_values", ())
                resets = [row["resets_at_ms"] for row in pools if isinstance(row.get("resets_at_ms"), (int, float)) and row["resets_at_ms"] > now_ms and row.get("used_fraction", 0) >= 0.9]
                delay = max(1.0, min(resets) / 1000 - time.time() + 1) if resets else 60.0
                time.sleep(delay)
    return {"role": role, "status": "all_planned_slots_terminal", "steps": steps,
            "planned": len(cases), "note": "terminal missing is not an answered judgment"}


def _integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def reconcile_usage(output: Path, role: str, case_ids: Sequence[str]) -> dict[str, Any]:
    """Count physical consumed slots independently of finalized quality cells.

    OQ6 transport._account already defines tokens_in as uncached input +
    cacheRead + cacheWrite. Reuse that normalization; never add caches again.
    Output includes thinking; reasoningTokens, when reported, is a subset and
    is never added to input+output. Client SDK cost estimates are not cash.
    """
    rows = []
    for case_id in dict.fromkeys(case_ids):
        directory = output / "calls" / role / case_id
        marker, receipt_path = directory / "call-consumed.json", directory / "receipt.json"
        if not marker.exists() and not receipt_path.exists():
            continue
        receipt = json.loads(receipt_path.read_bytes()) if receipt_path.is_file() else None
        incoming = outgoing = reasoning = None
        turns = None
        if receipt is not None:
            usage = receipt.get("usage")
            components = [_integer(usage.get(name)) if isinstance(usage, Mapping) else None
                          for name in ("input", "cacheRead", "cacheWrite")]
            expected_input = sum(components) if all(value is not None for value in components) else None
            incoming = _integer(receipt.get("tokens_in"))
            if expected_input is None:
                incoming = None
            elif incoming != expected_input:
                raise ValueError("native cached-prompt normalization differs from its attested usage")
            outgoing = _integer(receipt.get("tokens_out"))
            reasoning = _integer(receipt.get("reasoning_tokens"))
            turns = _integer(receipt.get("native_terminal_assistant_count"))
            if receipt.get("cost_usd") is not None or receipt.get("billing_mode") != "subscription":
                raise ValueError("native-only study found an unapproved cash/billing mode")
        rows.append({"case_id": case_id, "marker": study.pin(marker) if marker.is_file() else None,
                     "receipt": study.pin(receipt_path) if receipt_path.is_file() else None,
                     "input": incoming, "output": outgoing, "reasoning": reasoning,
                     "native_terminal_assistant_turns": turns, "orphan_without_receipt": receipt is None})
    def total(name: str) -> int | None:
        values = [row[name] for row in rows]
        return sum(values) if all(value is not None for value in values) else None
    return {"consumed_native_slots": len(rows), "orphan_without_receipt": sum(row["orphan_without_receipt"] for row in rows),
            "input_tokens": total("input"), "output_tokens": total("output"), "reasoning_tokens": total("reasoning"),
            "actual_paid_usd": None, "cash_applicability": "N/A: native subscription", "slots": rows,
            "physical_count_basis": "unique (role, opaque case); old/new gates share the same physical response and are never summed as separate requests"}


def _decided(record: Mapping[str, Any]) -> bool:
    return record.get("status") == "ok" and record.get("prediction", {}).get("status") == "ok" and all(type(record.get("flags", {}).get(gate)) is bool for gate in study.GATES)


def slot_coverage(output: Path, role: str, case_ids: Sequence[str], records: Sequence[Mapping[str, Any]],
                  usage: Mapping[str, Any]) -> dict[str, int]:
    """Partition planned slots; an unsent pending role is not a failed judgment."""
    planned = set(case_ids)
    finals = {row["case_id"]: row for row in records if row["role"] == role}
    consumed = {row["case_id"] for row in usage["slots"]}
    pending_root = output / "quota-pending" / role
    pending = {case for case in planned if (pending_root / case).is_dir()}
    if not (set(finals) | consumed | pending).issubset(planned):
        raise ValueError("slot accounting contains a case outside the frozen cohort")
    orphan = consumed - finals.keys()
    unsent = pending - consumed - finals.keys()
    untouched = planned - finals.keys() - consumed - pending
    completed = sum(_decided(row) for row in finals.values())
    return {
        "planned": len(planned), "complete": completed,
        "terminal_missing": len(finals) - completed,
        "consumed_without_final_quality": len(orphan),
        "quota_unsent": len(unsent), "not_yet_sent": len(untouched),
    }


def score_snapshot(code_path: Path, code_sha: str, inputs_sha: str) -> dict[str, Any]:
    code, inputs, cases, sealed = load_inputs(code_path, code_sha, inputs_sha)
    output = code_path.parent
    bridge = freeze.load_module("oq10_snapshot_scoring", code["own"]["scoring.py"])
    metrics = freeze.load_module("oq10_snapshot_metrics", code["own"]["report_metrics.py"])
    records = bridge.load_cells(output / "cells", code["roles"])
    if not records:
        raise ValueError("no finalized predictions; do not publish an all-null completion")
    haiku_cells = {
        row["case_id"]: json.loads((output / "cells" / "haiku" / (row["case_id"] + ".json")).read_bytes())
        for row in records if row["role"] == "haiku"
    }
    triage_ranking = study.triage_order(cases, haiku_cells)
    for case in cases:
        study.verify_case(case)
    frozen = bridge.load_scorer(scorer=code["library"], registry=code["registry"])
    bundle_files = {}
    for corpus, bundle in code["bundles"].items():
        selected = {"STAGE-MANIFEST.json": bundle["manifest"]["sha256"]}
        for name, digest in bundle["files"].items():
            if (corpus == "automationbench-verified" and name in {"tasks.csv", "fixes.csv"}) or (corpus == "paichecker" and (name.endswith(".csv") and name.startswith("labelled_data/") or name.endswith(".parquet") and name.startswith("graders/"))):
                selected[name] = digest
        bundle_files[corpus] = selected
    native = bridge.load_native_gold(
        frozen, eval_lab_root=output / "frozen-eval-lab",
        adapters={name: value["sha256"] for name, value in code["adapters"].items()}, bundle_files=bundle_files,
        automationbench_bundle=code["bundles"]["automationbench-verified"]["bundle"],
        paichecker_bundle=code["bundles"]["paichecker"]["bundle"],
    )
    quality = bridge.score_cross_test(frozen, native, public_cases=cases, sealed_cases=sealed, records=records, roles=code["roles"])
    usage = {role: reconcile_usage(output, role, [case["case_id"] for case in cases]) for role in code["roles"]}
    by_role = {(row["role"], row["case_id"]): row for row in records}
    intersection = {case["case_id"] for case in cases if all(_decided(by_role.get((role, case["case_id"]), {})) for role in ("sonnet", "kimi"))}
    paired = {"status": "unavailable", "shared_complete_tasks": 0, "quality": None}
    if intersection:
        paired_cases = [case for case in cases if case["case_id"] in intersection]
        paired = {"status": "observed", "shared_complete_tasks": len(intersection),
                  "quality": bridge.score_cross_test(frozen, native, public_cases=paired_cases,
                      sealed_cases=[row for row in sealed if row["case_id"] in intersection],
                      records=[row for row in records if row["role"] in {"sonnet", "kimi"} and row["case_id"] in intersection],
                      roles={role: code["roles"][role] for role in ("sonnet", "kimi")})}
    # Library scoring supplies the confusion, class denominators and cluster
    # intervals. report_metrics handles interpretation without inventing labels.
    normalized = []
    for view in quality["views"]:
        for role in code["roles"]:
            # The full role's usage is not allocated to a single corpus by
            # guessing: reconcile just that corpus's opaque physical slots.
            view_ids = {row["case_id"] for row in sealed if (row["subcorpus"] if row["corpus"] == "paichecker" else "parsewave__automationbench-verified") == view}
            view_usage = reconcile_usage(output, role, sorted(view_ids))
            for gate in study.GATES:
                args = bridge.summarize_cell_inputs(quality, view=view, role=role, gate=gate)
                normalized.append({"role": role, "route": code["roles"][role], "gate": gate,
                    "metrics": metrics.summarize_cell(**args, input_tokens=view_usage["input_tokens"], output_tokens=view_usage["output_tokens"],
                        reasoning_tokens=view_usage["reasoning_tokens"], actual_paid_usd=None)})
    value = {"schema": "oq10.results-snapshot/v1", "at": dt.datetime.now(dt.UTC).isoformat(),
             "code_freeze_sha256": code_sha, "input_freeze_sha256": inputs_sha,
             "quality": quality, "interpreted_cells": normalized, "usage": usage,
             "same_source_sonnet_kimi_shared_complete": paired,
             "status_counts": dict(Counter(row.get("status") for row in records)),
             "planned_tasks": 180, "planned_role_slots": 540, "finalized_slots": len(records),
             "unfinalized_planned_slots": 540 - len(records), "missing_is_not_zero": True,
             "coverage_by_role": {role: slot_coverage(
                 output, role, [case["case_id"] for case in cases], records, usage[role],
             ) for role in code["roles"]},
             "old_new_gates_share_each_physical_response": True,
             "haiku_triage_descriptive_order": triage_ranking,
             "triage_changes_detector_cohort_or_prompts": False,
             "causal_defect_precision_or_cost": None,
             "interpretation": "task-label transfer, not causal-bug replication; AB unchanged are uncertified; PAI alignment is a construct shift; samples do not estimate native prevalence"}
    path = output / "results" / (uuid.uuid4().hex + ".json")
    study.dump_new(path, value)
    return study.pin(path)


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--config", type=Path, required=True)
    register.add_argument("--output", type=Path, required=True)
    for name in ("prepare", "proof", "run", "score"):
        command = commands.add_parser(name)
        command.add_argument("--code", type=Path, required=True)
        command.add_argument("--code-sha256", required=True)
        if name != "prepare":
            command.add_argument("--inputs-sha256", required=True)
        if name in {"prepare", "proof"}:
            command.add_argument("--proof-registration", type=Path, required=True)
            command.add_argument("--proof-result", type=Path, required=True)
        if name in {"proof", "run"}:
            command.add_argument("--role", choices=tuple(freeze.ROLES), required=True)
        if name == "run":
            command.add_argument("--proof-sha256", required=True)
            command.add_argument("--max-steps", type=int)
    args = parser.parse_args()
    freeze.environment_guard()
    if args.command == "register":
        result = freeze.code_freeze(json.loads(args.config.read_bytes()), own_source=Path(__file__).parent, output=args.output)
    elif args.command == "prepare":
        result = freeze.prepare_inputs(args.code, args.code_sha256, proof_registration=args.proof_registration, proof_result=args.proof_result)
    elif args.command == "proof":
        result = register_role_proof(args.code, args.code_sha256, args.inputs_sha256, role=args.role, registration=args.proof_registration, result=args.proof_result)
    elif args.command == "run":
        result = run_role(args.code, args.code_sha256, args.inputs_sha256, role=args.role, proof_sha=args.proof_sha256, max_steps=args.max_steps)
    else:
        result = score_snapshot(args.code, args.code_sha256, args.inputs_sha256)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not let a parser/adapter exception print original source/label values
        # into shared tool traces. Full traceback stays private local evidence.
        failure = Path(os.environ.get("TMPDIR", "/private/tmp")) / "oq10-private-failures"
        failure.mkdir(parents=True, exist_ok=True)
        path = failure / (uuid.uuid4().hex + ".txt")
        path.write_text(traceback.format_exc(), encoding="utf-8")
        print(json.dumps({"status": "failed_closed", "error_type": type(error).__name__, "private_traceback_sha256": study.sha(path.read_bytes())}))
        raise SystemExit(1) from None
