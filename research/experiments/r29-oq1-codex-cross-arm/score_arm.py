#!/usr/bin/env python3
"""R29-OQ-1: score the Codex arm once, with the frozen scorer, next to the already-scored Sonnet arm.

Mirrors `run_study.score_snapshot` for roles {sonnet, codex}. Before reporting anything it requires the Sonnet
view/gate scores to equal the earlier retained result byte for byte (sealed-label scoring must be reproducible);
otherwise it aborts. The library/registry pins lose their two extra keys (`bytes`, `original_path`) exactly as in
the earlier Sonnet scoring wrapper; paths and SHA-256 are still verified.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_arm  # noqa: E402

PRIOR = STUDY_RESULT = codex_arm.STUDY / "results/b2614842a23445aa846cdf2ec3f6b249.json"


def keep(pin: dict) -> dict:
    return {"path": pin["path"], "sha256": pin["sha256"]}


def main() -> None:
    frozen = codex_arm.Frozen()
    freeze, study, run_study, code = frozen.freeze, frozen.study, frozen.run_study, frozen.code
    bridge = freeze.load_module("oq10_snapshot_scoring", code["own"]["scoring.py"])
    metrics = freeze.load_module("oq10_snapshot_metrics", code["own"]["report_metrics.py"])
    roles = {"sonnet": code["roles"]["sonnet"], "codex": codex_arm.ROUTE}
    records = bridge.load_cells(codex_arm.STUDY / "cells", {"sonnet": roles["sonnet"]})
    records += bridge.load_cells(codex_arm.OUT / "cells", {"codex": roles["codex"]})
    for case in frozen.cases:
        study.verify_case(case)
    scorer = bridge.load_scorer(scorer=keep(code["library"]), registry=keep(code["registry"]))
    bundle_files = {}
    for corpus, bundle in code["bundles"].items():
        selected = {"STAGE-MANIFEST.json": bundle["manifest"]["sha256"]}
        for name, digest in bundle["files"].items():
            if (corpus == "automationbench-verified" and name in {"tasks.csv", "fixes.csv"}) or (
                corpus == "paichecker"
                and (name.endswith(".csv") and name.startswith("labelled_data/")
                     or name.endswith(".parquet") and name.startswith("graders/"))
            ):
                selected[name] = digest
        bundle_files[corpus] = selected
    native = bridge.load_native_gold(
        scorer, eval_lab_root=codex_arm.STUDY / "frozen-eval-lab",
        adapters={name: value["sha256"] for name, value in code["adapters"].items()}, bundle_files=bundle_files,
        automationbench_bundle=code["bundles"]["automationbench-verified"]["bundle"],
        paichecker_bundle=code["bundles"]["paichecker"]["bundle"],
    )
    quality = bridge.score_cross_test(
        scorer, native, public_cases=frozen.cases, sealed_cases=frozen.sealed, records=records, roles=roles,
    )
    prior = json.loads(PRIOR.read_bytes())["quality"]
    for view, entry in prior["views"].items():
        if entry["roles"]["sonnet"] != quality["views"][view]["roles"]["sonnet"]:
            raise SystemExit(f"Sonnet reproduction failed for {view}; not reporting")
    if prior["inputs"]["records_sha256"]["sonnet"] != quality["inputs"]["records_sha256"]["sonnet"]:
        raise SystemExit("Sonnet record digest differs from the earlier result; not reporting")
    by_role = {(row["role"], row["case_id"]): row for row in records}
    both = {
        case["case_id"] for case in frozen.cases
        if all(run_study._decided(by_role.get((role, case["case_id"]), {})) for role in roles)
    }
    paired = {"status": "unavailable", "shared_complete_tasks": 0, "quality": None}
    if both:
        subset = [case for case in frozen.cases if case["case_id"] in both]
        paired = {
            "status": "observed", "shared_complete_tasks": len(both),
            "quality": bridge.score_cross_test(
                scorer, native, public_cases=subset,
                sealed_cases=[row for row in frozen.sealed if row["case_id"] in both],
                records=[row for row in records if row["case_id"] in both], roles=roles,
            ),
        }
    usage_roots = {"sonnet": codex_arm.STUDY, "codex": codex_arm.OUT}
    case_ids = [case["case_id"] for case in frozen.cases]
    usage = {role: run_study.reconcile_usage(root, role, case_ids) for role, root in usage_roots.items()}
    normalized = []
    for view in quality["views"]:
        for role in roles:
            view_ids = {
                row["case_id"] for row in frozen.sealed
                if (row["subcorpus"] if row["corpus"] == "paichecker" else "parsewave__automationbench-verified") == view
            }
            view_usage = run_study.reconcile_usage(usage_roots[role], role, sorted(view_ids))
            for gate in study.GATES:
                args = bridge.summarize_cell_inputs(quality, view=view, role=role, gate=gate)
                normalized.append({
                    "role": role, "route": roles[role], "gate": gate,
                    "metrics": metrics.summarize_cell(
                        **args, input_tokens=view_usage["input_tokens"], output_tokens=view_usage["output_tokens"],
                        reasoning_tokens=view_usage["reasoning_tokens"], actual_paid_usd=None,
                    ),
                })
    coverage = {
        role: {
            "decided": sum(run_study._decided(by_role.get((role, c), {})) for c in case_ids),
            "records": sum((role, c) in by_role for c in case_ids),
            "planned": len(case_ids),
        }
        for role in roles
    }
    value = {
        "schema": "r29.codex-arm-results/v1", "at": dt.datetime.now(dt.UTC).isoformat(),
        "code_freeze_sha256": codex_arm.CODE_SHA, "input_freeze_sha256": codex_arm.INPUTS_SHA,
        "roles": roles, "quality": quality, "interpreted_cells": normalized, "usage": usage,
        "paired_codex_sonnet_shared_decided": paired, "coverage": coverage, "missing_is_not_zero": True,
        "sonnet_reproduced_exactly": True,
    }
    path = codex_arm.OUT / "results" / "results.json"
    study.dump_new(path, value)
    print(json.dumps({"results": str(path), "sha256": study.sha(path.read_bytes()), "coverage": coverage}))


if __name__ == "__main__":
    main()
