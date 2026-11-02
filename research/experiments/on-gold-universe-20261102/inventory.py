"""A1-INVENTORY: The Executable Gold Universe Inventory.

Inventories candidate tasks from 3 sources:
1. 36 MiMo format-code tasks (Task A classification)
2. 30 repair-qualified MiMo packages (mimo-task-quality-30-20261024)
3. 14 TB4 DEV tasks (oq6_cohort_manifest.json)

Produces:
- gold-universe.json with eligibility (audit_ok, gold_ok) and cost estimates
- summary.json (counts and task ids only; no task text)
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from evallab.gold_certifier import extract_hook_command_from_test_sh
from evallab.mutant_auditor import mutants_for_patch, parse_probe, python_targets
from evallab.package_anatomy import analyze_package
from evallab.production.corpus_queue import CorpusRegistry

REPO_ROOT = Path(__file__).resolve().parents[3]

# Candidate sources
SRC1_PATH = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-30-factory/raw/A/classification.json")
SRC2_ORIG_DIR = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-10-24/mimo-task-quality-30-20261024/original-packages")
SRC3_MANIFEST = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/fable/raw/T2B-OQ6-DIAGNOSIS/receipts/oq6_cohort_manifest.json")
TB4_TASKS_DIR = Path("/Users/petermakhnatch/Developer/eval-lab/derived/kev/raw/terminal-bench-v4.0.0/tasks")

REFERENCES_CSV = REPO_ROOT / "research/experiments/mimo-v6-full-fleet/references.csv"

# Evidence directories for post-patch sources
STING_ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-01/w2-sting-mutants/tasks")
EVIDENCE_ORIGINALS_ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-10-24/mimo-task-quality-30-20261024/evidence/originals")

# Cost constants from measured receipts
REAL_USD_PER_EXEC = 0.0065
RESERVED_USD_PER_EXEC = 0.0100
NOMINAL_AUDIT_CELLS = 4
NOMINAL_GOLD_R1_CELLS = 12
NOMINAL_GOLD_R2_CELLS = 24


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_inventory() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    registry = CorpusRegistry(REPO_ROOT)

    # 1. Source 1
    with open(SRC1_PATH) as f:
        s1_data = json.load(f)
    s1_tasks = {t["task_id"]: t for t in s1_data["tasks"]}

    # 2. Source 2
    s2_ids = {p.name for p in SRC2_ORIG_DIR.iterdir() if p.is_dir()}

    # 3. Source 3
    with open(SRC3_MANIFEST) as f:
        s3_data = json.load(f)
    s3_tasks = {t["task_id"]: t for t in s3_data["tasks"] if t.get("dataset_id") == "tb4-audit-v1"}

    # References
    with open(REFERENCES_CSV) as f:
        refs = {r["task_id"]: r for r in csv.DictReader(f)}

    all_ids = sorted(set(s1_tasks.keys()) | set(s2_ids) | set(s3_tasks.keys()))

    rows: list[dict[str, Any]] = []

    for tid in all_ids:
        source_tags = []
        if tid in s1_tasks:
            source_tags.append("format-code-36")
        if tid in s2_ids:
            source_tags.append("repaired-30")
        if tid in s3_tasks:
            source_tags.append("tb4-dev-14")

        # Resolve package path
        if tid in s1_tasks:
            pkg = Path(s1_tasks[tid]["package"])
            if not pkg.exists():
                pkg = SRC2_ORIG_DIR / tid
        elif tid in s2_ids:
            pkg = SRC2_ORIG_DIR / tid
        else:
            pkg = TB4_TASKS_DIR / tid

        pkg_exists = pkg.exists()
        has_env = (pkg / "environment").is_dir() if pkg_exists else False
        has_tests = (pkg / "tests").is_dir() if pkg_exists else False

        # Split check from CorpusRegistry
        dataset = "tb4-audit-v1" if "tb4-dev-14" in source_tags else "mimo-defect-map-v1"
        assigned = registry.assigned_split(dataset, task_id=tid)
        sealed = registry.sealed_identity(task_id=tid)
        test_anywhere = registry.assigned_test_anywhere(task_id=tid)
        strict_dev = (assigned == "dev" and sealed is None and not test_anywhere)

        # Runner & language classification
        runner = "unknown"
        language = "unknown"
        if tid in s1_tasks:
            runner_raw = s1_tasks[tid].get("runner", "unknown")
            if runner_raw == "pytest":
                runner = "pytest"
                language = "python"
            elif tid in ("format-code-task-000603", "format-code-task-001514"):
                runner = "vitest"
                language = "typescript"
            elif tid == "format-code-task-000872":
                runner = "mocha"
                language = "javascript"
            elif tid in ("format-code-task-000838", "format-code-task-001267", "format-code-task-002160", "format-code-task-002976"):
                runner = "unittest"
                language = "python"
            elif tid == "format-code-task-000792":
                runner = "custom_script"
                language = "python"
            else:
                runner = runner_raw
                language = "python"
        elif tid in s3_tasks:
            test_sh = (pkg / "tests/test.sh").read_text(errors="replace") if (pkg / "tests/test.sh").exists() else ""
            runner = "pytest" if "pytest" in test_sh else "custom_script"
            language = "python"

        card = analyze_package(pkg) if pkg_exists else None
        layout = card["container_and_boundary"]["layout"] if card else "unknown"

        # Reference patch check
        ref_present = tid in refs
        ref_path = refs[tid]["patch_path"] if ref_present else None
        ref_sha = refs[tid]["patch_sha256"] if ref_present else None
        ref_verified = False
        added_py_lines = 0
        patch_text = ""

        if ref_present and ref_path:
            p_file = REPO_ROOT / ref_path
            if p_file.exists():
                p_bytes = p_file.read_bytes()
                actual_sha = sha256_bytes(p_bytes)
                if actual_sha == ref_sha:
                    ref_verified = True
                    patch_text = p_bytes.decode(errors="replace")
                    targets = python_targets(patch_text)
                    added_py_lines = sum(len(lines) for lines in targets.values())

        # Offline AST mutants
        sources: dict[str, str] = {}
        probe_file = STING_ROOT / tid / "probe/result.json"
        if probe_file.exists():
            pr = json.loads(probe_file.read_text())
            sources.update(parse_probe(pr.get("agent_log", "")))

        sources_dir = EVIDENCE_ORIGINALS_ROOT / tid / "trial/artifacts/logs/artifacts/sources"
        if sources_dir.exists() and patch_text:
            for t_file in python_targets(patch_text):
                if t_file not in sources:
                    f = sources_dir / t_file
                    if f.exists():
                        sources[t_file] = f.read_text(errors="replace")

        offline_mutants = mutants_for_patch(patch_text, sources) if (patch_text and sources) else []
        ast_mutants_count = len(offline_mutants)

        # Hook command extraction
        test_sh_path = pkg / "tests/test.sh"
        test_sh_text = test_sh_path.read_text(errors="replace") if test_sh_path.exists() else ""
        hook_cmd = extract_hook_command_from_test_sh(test_sh_text) if test_sh_text else None

        # Eligibility evaluation
        # audit_ok: python-addressable single-container grader
        audit_ok = True
        audit_reason = None
        if not strict_dev:
            audit_ok = False
            audit_reason = "not_strictly_dev"
        elif not (pkg_exists and has_env and has_tests):
            audit_ok = False
            audit_reason = "missing_package_structure"
        elif layout != "single_container":
            audit_ok = False
            audit_reason = "not_single_container"
        elif language != "python" or runner not in ("pytest", "unittest", "python"):
            audit_ok = False
            audit_reason = f"non_python_runner_{runner}"

        # gold_ok: audit_ok AND reference patch verified with >= 1 added non-test .py line AND hook command
        gold_ok = True
        gold_reason = None
        if not audit_ok:
            gold_ok = False
            gold_reason = audit_reason
        elif not ref_verified:
            gold_ok = False
            gold_reason = "no_verified_reference_patch"
        elif added_py_lines < 1:
            gold_ok = False
            gold_reason = "no_added_python_lines"
        elif not hook_cmd:
            gold_ok = False
            gold_reason = "no_extractable_hook_command"

        # Cell and cost projections
        audit_cells = NOMINAL_AUDIT_CELLS if audit_ok else 0
        gold_r1_cells = NOMINAL_GOLD_R1_CELLS if gold_ok else 0
        gold_r2_cells = NOMINAL_GOLD_R2_CELLS if gold_ok else 0

        rows.append({
            "task_id": tid,
            "sources": source_tags,
            "strictly_dev": strict_dev,
            "package_path": str(pkg),
            "package_exists": pkg_exists,
            "has_environment": has_env,
            "has_tests": has_tests,
            "layout": layout,
            "runner": runner,
            "language": language,
            "reference_patch_present": ref_present,
            "reference_patch_path": ref_path,
            "reference_patch_sha256": ref_sha,
            "reference_patch_verified": ref_verified,
            "added_non_test_py_lines": added_py_lines,
            "ast_mutants_offline_count": ast_mutants_count,
            "extractable_hook_command": hook_cmd,
            "audit_ok": audit_ok,
            "audit_reason": audit_reason,
            "gold_ok": gold_ok,
            "gold_reason": gold_reason,
            "expected_cells": {
                "audit_only": audit_cells,
                "gold_round_1": gold_r1_cells,
                "gold_round_2": gold_r2_cells,
            },
            "expected_cost_real_usd": {
                "audit_only": round(audit_cells * REAL_USD_PER_EXEC, 4),
                "gold_round_1": round(gold_r1_cells * REAL_USD_PER_EXEC, 4),
                "gold_round_2": round(gold_r2_cells * REAL_USD_PER_EXEC, 4),
            },
            "expected_cost_reserved_usd": {
                "audit_only": round(audit_cells * RESERVED_USD_PER_EXEC, 4),
                "gold_round_1": round(gold_r1_cells * RESERVED_USD_PER_EXEC, 4),
                "gold_round_2": round(gold_r2_cells * RESERVED_USD_PER_EXEC, 4),
            },
        })

    # Summary metrics
    n_total = len(rows)
    audit_ok_tasks = [r["task_id"] for r in rows if r["audit_ok"]]
    gold_ok_tasks = [r["task_id"] for r in rows if r["gold_ok"]]

    audit_ineligible_reasons: dict[str, int] = {}
    for r in rows:
        if not r["audit_ok"]:
            audit_ineligible_reasons[r["audit_reason"]] = audit_ineligible_reasons.get(r["audit_reason"], 0) + 1

    gold_ineligible_reasons: dict[str, int] = {}
    for r in rows:
        if not r["gold_ok"]:
            gold_ineligible_reasons[r["gold_reason"]] = gold_ineligible_reasons.get(r["gold_reason"], 0) + 1

    by_source: dict[str, int] = {}
    for r in rows:
        key = "+".join(r["sources"])
        by_source[key] = by_source.get(key, 0) + 1

    by_runner: dict[str, int] = {}
    for r in rows:
        by_runner[r["runner"]] = by_runner.get(r["runner"], 0) + 1

    by_language: dict[str, int] = {}
    for r in rows:
        by_language[r["language"]] = by_language.get(r["language"], 0) + 1

    total_audit_cells = sum(r["expected_cells"]["audit_only"] for r in rows)
    total_gold_r1_cells = sum(r["expected_cells"]["gold_round_1"] for r in rows)
    total_gold_r2_cells = sum(r["expected_cells"]["gold_round_2"] for r in rows)

    summary = {
        "candidate_count": n_total,
        "headline": f"{len(audit_ok_tasks)} packages are executable for audit, {len(gold_ok_tasks)} for gold",
        "counts": {
            "total_candidates": n_total,
            "executable_audit": len(audit_ok_tasks),
            "executable_gold": len(gold_ok_tasks),
            "ineligible_audit": len(rows) - len(audit_ok_tasks),
            "ineligible_gold": len(rows) - len(gold_ok_tasks),
        },
        "by_source": by_source,
        "by_runner": by_runner,
        "by_language": by_language,
        "ineligible_audit_reasons": audit_ineligible_reasons,
        "ineligible_gold_reasons": gold_ineligible_reasons,
        "task_ids": {
            "all_candidates": [r["task_id"] for r in rows],
            "executable_audit": audit_ok_tasks,
            "executable_gold": gold_ok_tasks,
        },
        "costs": {
            "cell_rates": {
                "real_usd": REAL_USD_PER_EXEC,
                "reserved_usd": RESERVED_USD_PER_EXEC,
                "citation": "spend.jsonl measured ~0.00322/cell; K4-report.md §Slice 2 reservation cap ~0.0065 real vs 0.0100 reserved",
            },
            "totals": {
                "audit_only": {
                    "cells": total_audit_cells,
                    "real_usd": round(total_audit_cells * REAL_USD_PER_EXEC, 4),
                    "reserved_usd": round(total_audit_cells * RESERVED_USD_PER_EXEC, 4),
                },
                "gold_round_1": {
                    "cells": total_gold_r1_cells,
                    "real_usd": round(total_gold_r1_cells * REAL_USD_PER_EXEC, 4),
                    "reserved_usd": round(total_gold_r1_cells * RESERVED_USD_PER_EXEC, 4),
                },
                "gold_round_2": {
                    "cells": total_gold_r2_cells,
                    "real_usd": round(total_gold_r2_cells * REAL_USD_PER_EXEC, 4),
                    "reserved_usd": round(total_gold_r2_cells * RESERVED_USD_PER_EXEC, 4),
                },
            },
        },
    }

    return rows, summary


def main() -> None:
    rows, summary = build_inventory()

    gold_universe = {
        "schema_version": "gold-universe/v1",
        "description": "Executable gold universe inventory (MiMo strictly-DEV packages, repaired-30, TB4 DEV 14)",
        "headline": summary["headline"],
        "counts": summary["counts"],
        "costs": summary["costs"],
        "tasks": rows,
    }

    universe_json_str = json.dumps(gold_universe, indent=2) + "\n"
    universe_sha = sha256_bytes(universe_json_str.encode())
    summary["gold_universe_sha256"] = universe_sha

    summary_json_str = json.dumps(summary, indent=2) + "\n"

    # Targets
    exp_dir = REPO_ROOT / "research/experiments/on-gold-universe-20261102"
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "summary.json").write_text(summary_json_str)

    results_dir = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A1-INVENTORY")
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "gold-universe.json").write_text(universe_json_str)
    (results_dir / "summary.json").write_text(summary_json_str)

    gates_file = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates/gold-universe.json")
    gates_file.parent.mkdir(parents=True, exist_ok=True)
    gates_file.write_text(universe_json_str)

    receipts_dir = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/raw/A1-INVENTORY/receipts")
    receipts_dir.mkdir(parents=True, exist_ok=True)
    (receipts_dir / "gold-universe.json").write_text(universe_json_str)
    (receipts_dir / "summary.json").write_text(summary_json_str)

    print(f"Emitted gold-universe.json ({len(rows)} tasks, sha256={universe_sha[:12]})")
    print(f"Headline: {summary['headline']}")


if __name__ == "__main__":
    main()
