#!/usr/bin/env python3
"""Validate Hackbook v0 and build ``schema.json`` and ``summary.json``.

Usage:
    python research/hackbook/build.py
    python research/hackbook/build.py --check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from evallab.hackbook.models import (  # noqa: E402
    ENTRY_SCHEMA,
    STRUCTURAL_SIGNALS,
    HackbookEntry,
    find_tb4_test_leaks,
    load_hackbook,
    load_taxonomy,
    tb4_test_split_terms,
    validate_no_tb4_test_leak,
)

EXPECTED_TASK_CLASSES: dict[str, tuple[str, str, str]] = {
    "TS01": ("weak_check", "false_pass", "weak"),
    "TS02": ("grader_logic_bug", "either", "broken"),
    "TS03": ("answer_reachable", "false_pass", "reachable"),
    "TS04": ("grader_tamperable", "false_pass", "tamperable"),
    "TS05": ("unstated_requirement", "false_fail", "prompt_misaligned"),
    "TS06": ("instruction_grader_contradiction", "either", "prompt_misaligned"),
    "TS07": ("overstrict_oracle", "false_fail", "broken"),
    "TS08": ("wrong_answer_key", "false_fail", "broken"),
    "TS09": ("env_mismatch_or_flaky", "either", "broken"),
    "TS10": ("infeasible_as_written", "false_fail", "broken"),
    "TS11": ("exploitable_llm_judge", "false_pass", "weak"),
}

EXPECTED_RUN_CLASSES: dict[str, tuple[str, str, str, set[str]]] = {
    "RS01": (
        "exploit_grader_gap",
        "false_pass",
        "reward_hacking",
        {"weak_check", "grader_logic_bug", "exploitable_llm_judge"},
    ),
    "RS02": ("tamper_with_grading", "false_pass", "reward_hacking", {"grader_tamperable"}),
    "RS03": ("answer_lookup", "false_pass", "reward_hacking", {"answer_reachable"}),
    "RS04": ("misreport", "false_pass", "reward_hacking", {"exploitable_llm_judge"}),
    "RS05": (
        "accepted_partial_fix",
        "false_pass",
        "false_positive",
        {"weak_check", "grader_logic_bug"},
    ),
    "RS06": (
        "defect_caused_fail",
        "false_fail",
        "false_negative",
        {
            "unstated_requirement",
            "instruction_grader_contradiction",
            "overstrict_oracle",
            "wrong_answer_key",
            "infeasible_as_written",
            "grader_logic_bug",
        },
    ),
    "RS07": ("infra_fail", "false_fail", "failure_cause", {"env_mismatch_or_flaky"}),
}

CANONICAL_SOURCES: tuple[str, ...] = (
    "preference_model_tb4",
    "benchjack",
    "cheatbench",
    "terminal_wrench",
    "hacktrace",
    "envcheck",
    "mimo_atlas",
    "eval3",
    "tb2_to_tb21",
)

EVAL3_SAMPLE_ISSUES: tuple[str, ...] = (
    "eval3:boil/config",
    "eval3:boil/liquid",
    "eval3:boil/optional",
    "eval3:boil/overlength",
    "eval3:boil/refocus",
    "eval3:boil/replay",
    "eval3:boil/shadow",
    "eval3:boil/ticks",
    "eval3:spider/columns",
    "eval3:spider/comment",
    "eval3:spider/constant",
    "eval3:spider/first",
    "eval3:spider/gold",
    "eval3:vim/filter",
    "eval3:vim/generator",
    "eval3:vim/setreg",
    "eval3:vim/symlink",
    "eval3:vim/vim",
)


def _json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _jsonl_text(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)


def _sorted_counts(items: Any) -> dict[str, int]:
    return dict(sorted(Counter(items).items()))


def _load_tb4_witness_ids() -> list[str]:
    witness_path = REPO_ROOT / "research/answer-keys/tb4-audit-v1/witnesses.json"
    if not witness_path.is_file():
        return []
    payload = json.loads(witness_path.read_text(encoding="utf-8"))
    return sorted(str(w["finding_id"]) for w in payload.get("witnesses", []))


def validate_catalog(entries: list[HackbookEntry], taxonomy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if len(entries) < 49:
        errors.append(f"expected at least 49 entries, got {len(entries)}")

    task_ids_in_taxonomy = {row["id"] for row in taxonomy.get("task_side", [])}
    run_ids_in_taxonomy = {row["id"] for row in taxonomy.get("run_side", [])}
    if task_ids_in_taxonomy != set(EXPECTED_TASK_CLASSES):
        errors.append(f"taxonomy.json task_side mismatch: {sorted(task_ids_in_taxonomy)}")
    if run_ids_in_taxonomy != set(EXPECTED_RUN_CLASSES):
        errors.append(f"taxonomy.json run_side mismatch: {sorted(run_ids_in_taxonomy)}")

    seen_classes: set[str] = set()
    seen_sources: set[str] = set()
    seen_signals: set[str] = set()

    for entry in entries:
        cid = entry.taxonomy_class_id
        seen_classes.add(cid)
        seen_sources.update(entry.sources)
        if cid in EXPECTED_TASK_CLASSES:
            key, direction, qa_cat = EXPECTED_TASK_CLASSES[cid]
            if entry.side != "task":
                errors.append(f"{entry.id}: expected side='task', got {entry.side!r}")
            if (entry.taxonomy_class, entry.direction, entry.qa_category) != (
                key,
                direction,
                qa_cat,
            ):
                errors.append(f"{entry.id}: class metadata mismatch for {cid}")
            if entry.enabled_by != [key]:
                errors.append(f"{entry.id}: task-side enabled_by must be [{key!r}]")
        elif cid in EXPECTED_RUN_CLASSES:
            key, direction, qa_cat, allowed_enabled = EXPECTED_RUN_CLASSES[cid]
            if entry.side != "run":
                errors.append(f"{entry.id}: expected side='run', got {entry.side!r}")
            if (entry.taxonomy_class, entry.direction, entry.qa_category) != (
                key,
                direction,
                qa_cat,
            ):
                errors.append(f"{entry.id}: class metadata mismatch for {cid}")
            if not entry.enabled_by or not (set(entry.enabled_by) <= allowed_enabled):
                errors.append(f"{entry.id}: invalid enabled_by {entry.enabled_by} for {cid}")
        else:
            errors.append(f"{entry.id}: unknown taxonomy_class_id {cid!r}")

        for sig in entry.signatures.structural_signals:
            if sig not in STRUCTURAL_SIGNALS:
                errors.append(f"{entry.id}: unknown structural signal {sig!r}")
            seen_signals.add(sig)

        for rule in (
            *entry.signatures.file_regexes,
            *entry.signatures.trace_command_regexes,
            *entry.signatures.trace_diff_regexes,
        ):
            try:
                re.compile(rule.pattern)
            except re.error as exc:
                errors.append(f"{entry.id}: invalid regex {rule.pattern!r}: {exc}")

        if not entry.examples:
            errors.append(f"{entry.id}: entry must have at least one example")

    missing_classes = (set(EXPECTED_TASK_CLASSES) | set(EXPECTED_RUN_CLASSES)) - seen_classes
    if missing_classes:
        errors.append(f"missing taxonomy classes: {sorted(missing_classes)}")

    missing_sources = set(CANONICAL_SOURCES) - seen_sources
    if missing_sources:
        errors.append(f"missing seed sources: {sorted(missing_sources)}")

    missing_signals = set(STRUCTURAL_SIGNALS) - seen_signals
    if missing_signals:
        errors.append(f"unused canonical structural signals: {sorted(missing_signals)}")

    executed_pm = {
        ex.item_or_task_id
        for entry in entries
        for ex in entry.examples
        if ex.source == "preference_model_tb4" and ex.evidence_status == "executed"
    }
    expected_witnesses = set(_load_tb4_witness_ids())
    if expected_witnesses and not (expected_witnesses <= executed_pm):
        errors.append(
            f"missing executed TB4 dev witnesses: {sorted(expected_witnesses - executed_pm)}"
        )

    eval3_present = {
        ex.item_or_task_id for entry in entries for ex in entry.examples if ex.source == "eval3"
    }
    missing_eval3 = set(EVAL3_SAMPLE_ISSUES) - eval3_present
    if missing_eval3:
        errors.append(f"missing Eval3 sample issues: {sorted(missing_eval3)}")

    errors.extend(validate_no_tb4_test_leak(entries, root=REPO_ROOT))
    for path in sorted(HERE.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            leaks = find_tb4_test_leaks(
                path.read_text(encoding="utf-8", errors="replace"), root=REPO_ROOT
            )
            if leaks:
                errors.append(f"{path.relative_to(REPO_ROOT)}: tb4 test-split leak: {leaks}")

    return errors


def build_schema() -> dict[str, Any]:
    schema = HackbookEntry.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = ENTRY_SCHEMA
    schema["title"] = "Eval Lab Hackbook Entry v0"
    schema["x-canonical-structural-signals"] = list(STRUCTURAL_SIGNALS)
    schema["x-canonical-sources"] = list(CANONICAL_SOURCES)
    schema["x-task-classes"] = {k: v[0] for k, v in EXPECTED_TASK_CLASSES.items()}
    schema["x-run-classes"] = {k: v[0] for k, v in EXPECTED_RUN_CLASSES.items()}
    return schema


def build_summary(entries: list[HackbookEntry], taxonomy: dict[str, Any]) -> dict[str, Any]:
    test_tasks, test_items = tb4_test_split_terms(REPO_ROOT)
    pm_crosswalk = taxonomy.get("crosswalks", {}).get("preference_model_tb4", {})
    all_examples = [ex for entry in entries for ex in entry.examples]
    examples_by_source = _sorted_counts(ex.source for ex in all_examples)
    entries_by_source = {
        src: sum(1 for entry in entries if src in entry.sources) for src in CANONICAL_SOURCES
    }
    executed_examples_by_source = _sorted_counts(
        ex.source for ex in all_examples if ex.evidence_status == "executed"
    )
    witness_ids = _load_tb4_witness_ids()

    source_summaries: dict[str, Any] = {
        "preference_model_tb4": {
            "entries_count": entries_by_source["preference_model_tb4"],
            "examples_count": examples_by_source.get("preference_model_tb4", 0),
            "executed_examples_count": executed_examples_by_source.get("preference_model_tb4", 0),
            "executed_dev_witness_ids": witness_ids,
            "aggregate_census_total_findings": 241,
            "aggregate_census_by_class": pm_crosswalk.get("our_label_counts_findings", {}),
            "aggregate_census_tasks_by_class": pm_crosswalk.get(
                "our_label_counts_tasks_with_class", {}
            ),
            "split_policy": "Only tb4-audit-v1 dev-split tasks (25) and benchmark-wide items (3) appear in examples; all 41 test-split tasks (152 findings) are excluded.",
            "source_url": "https://preferencemodel.com/blog/terminal-bench-4-is-saturated/issues/",
        },
        "benchjack": {
            "entries_count": entries_by_source["benchjack"],
            "examples_count": examples_by_source.get("benchjack", 0),
            "executed_examples_count": executed_examples_by_source.get("benchjack", 0),
            "vulnerability_classes_covered": [f"V{i}" for i in range(1, 9)],
            "class_crosswalk": taxonomy.get("crosswalks", {}).get("benchjack", {}),
            "source_url": "https://arxiv.org/abs/2605.12673",
        },
        "cheatbench": {
            "entries_count": entries_by_source["cheatbench"],
            "examples_count": examples_by_source.get("cheatbench", 0),
            "executed_examples_count": executed_examples_by_source.get("cheatbench", 0),
            "mechanisms_covered": [
                "harness_leaks_answer",
                "harness_leaks_verifier",
                "secret_discovery_in_environment",
                "secret_discovery_via_web_search",
                "verifier_exploitation",
            ],
            "answer_key": "research/answer-keys/cheatbench-actions-v1",
            "source_url": "https://huggingface.co/datasets/steinad/CheatBench",
        },
        "terminal_wrench": {
            "entries_count": entries_by_source["terminal_wrench"],
            "examples_count": examples_by_source.get("terminal_wrench", 0),
            "executed_examples_count": executed_examples_by_source.get("terminal_wrench", 0),
            "mechanisms_covered": [
                "algorithmic-simplification",
                "benchflow:P1:conftest_hook",
                "benchflow:P7:path_trojan",
                "benchflow:P7:pth_injection",
                "binary-hijacking",
                "constraint-loophole",
                "deceptive-rationalization",
                "hollow-implementation",
                "keyword-gaming",
                "metric-spoofing",
                "mutable-input-tampering",
                "output-spoofing",
                "security-downgrading",
                "structural-extraction",
            ],
            "answer_key": "research/answer-keys/breaker-bench-v1",
            "source_url": "https://github.com/few-sh/terminal-wrench",
        },
        "hacktrace": {
            "entries_count": entries_by_source["hacktrace"],
            "examples_count": examples_by_source.get("hacktrace", 0),
            "executed_examples_count": executed_examples_by_source.get("hacktrace", 0),
            "mechanisms_covered": [
                "eval_bypass",
                "hardcode_output",
                "special_casing",
                "test_tamper",
            ],
            "answer_key": "research/answer-keys/hacktrace-act-v1",
            "source_url": "https://huggingface.co/datasets/JH012/HackTrace-Data",
        },
        "envcheck": {
            "entries_count": entries_by_source["envcheck"],
            "examples_count": examples_by_source.get("envcheck", 0),
            "executed_examples_count": executed_examples_by_source.get("envcheck", 0),
            "findings_covered": sorted(
                {ex.item_or_task_id for ex in all_examples if ex.source == "envcheck"}
            ),
            "answer_key": "research/answer-keys/envcheck-tb4-39",
            "source_url": "https://github.com/tokenless-ai/envcheck-findings",
        },
        "mimo_atlas": {
            "entries_count": entries_by_source["mimo_atlas"],
            "examples_count": examples_by_source.get("mimo_atlas", 0),
            "executed_examples_count": executed_examples_by_source.get("mimo_atlas", 0),
            "cohorts_covered": [
                "mimo-weak-tests-v2.1",
                "mimo-heldout-v6-to-v7",
                "vals-git-history-leak-1795-of-2698",
            ],
            "answer_keys": [
                "research/answer-keys/mimo-weak-tests-v2.1",
                "research/answer-keys/mimo-heldout-v6-to-v7",
            ],
            "source_url": "https://github.com/PeterMakhnatch/eval-lab/tree/main/research/answer-keys/mimo-weak-tests-v2.1",
        },
        "eval3": {
            "entries_count": entries_by_source["eval3"],
            "examples_count": examples_by_source.get("eval3", 0),
            "executed_examples_count": executed_examples_by_source.get("eval3", 0),
            "sample_issues_total": len(EVAL3_SAMPLE_ISSUES),
            "sample_issues_by_task": {
                "ScienceWorld boil-water": [
                    i for i in EVAL3_SAMPLE_ISSUES if i.startswith("eval3:boil/")
                ],
                "Spider2 local017": [
                    i for i in EVAL3_SAMPLE_ISSUES if i.startswith("eval3:spider/")
                ],
                "Terminal-Bench 2 large-scale-text-editing": [
                    i for i in EVAL3_SAMPLE_ISSUES if i.startswith("eval3:vim/")
                ],
            },
            "source_url": "https://usetokenless.com/eval3/",
        },
        "tb2_to_tb21": {
            "entries_count": entries_by_source["tb2_to_tb21"],
            "examples_count": examples_by_source.get("tb2_to_tb21", 0),
            "executed_examples_count": executed_examples_by_source.get("tb2_to_tb21", 0),
            "cohorts_covered": ["tb2-to-tb21-v1", "tb21-weak-tests-v1"],
            "answer_keys": [
                "research/answer-keys/tb2-to-tb21-v1",
                "research/answer-keys/tb21-weak-tests-v1",
            ],
            "source_url": "https://github.com/PeterMakhnatch/eval-lab/tree/main/research/answer-keys/tb2-to-tb21-v1",
        },
    }

    return {
        "schema": "evallab.hackbook.summary/v0",
        "version": "v0",
        "entry_schema": ENTRY_SCHEMA,
        "entries_count": len(entries),
        "task_side_entries_count": sum(1 for e in entries if e.side == "task"),
        "run_side_entries_count": sum(1 for e in entries if e.side == "run"),
        "taxonomy_classes_count": len({e.taxonomy_class_id for e in entries}),
        "task_classes_count": len({e.taxonomy_class_id for e in entries if e.side == "task"}),
        "run_classes_count": len({e.taxonomy_class_id for e in entries if e.side == "run"}),
        "sources_count": len({s for e in entries for s in e.sources}),
        "structural_signals_count": len(STRUCTURAL_SIGNALS),
        "entries_with_executed_evidence_count": sum(
            1 for e in entries if any(ex.evidence_status == "executed" for ex in e.examples)
        ),
        "examples_count": len(all_examples),
        "examples_by_evidence_status": _sorted_counts(ex.evidence_status for ex in all_examples),
        "entries_by_class_id": _sorted_counts(e.taxonomy_class_id for e in entries),
        "entries_by_class_key": _sorted_counts(e.taxonomy_class for e in entries),
        "entries_by_direction": _sorted_counts(e.direction for e in entries),
        "entries_by_qa_category": _sorted_counts(e.qa_category for e in entries),
        "entries_by_detection_kind": _sorted_counts(e.detection_recipe.kind for e in entries),
        "entries_by_detection_method": _sorted_counts(
            e.detection_recipe.method_id for e in entries
        ),
        "entries_by_source": entries_by_source,
        "structural_signal_entry_counts": _sorted_counts(
            sig for e in entries for sig in e.signatures.structural_signals
        ),
        "tb4_test_split_leak_check": {
            "forbidden_test_tasks_checked": len(test_tasks),
            "forbidden_test_items_checked": len(test_items),
            "leak_count": 0,
            "status": "clean",
        },
        "source_summaries": source_summaries,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="Validate catalog and verify generated files without modifying disk.",
    )
    args = ap.parse_args(argv)

    entries = load_hackbook(REPO_ROOT)
    taxonomy = load_taxonomy(REPO_ROOT)
    schema = build_schema()
    summary = build_summary(entries, taxonomy)

    expected_files = {
        HERE / "entries.jsonl": _jsonl_text([e.dump() for e in entries]),
        HERE / "taxonomy.json": _json_text(taxonomy),
        HERE / "schema.json": _json_text(schema),
        HERE / "summary.json": _json_text(summary),
    }

    if not args.check:
        for path, content in expected_files.items():
            path.write_text(content, encoding="utf-8")

    errors = validate_catalog(entries, taxonomy)
    if args.check:
        for path, expected in expected_files.items():
            if not path.is_file():
                errors.append(f"missing generated file: {path.relative_to(REPO_ROOT)}")
            elif path.read_text(encoding="utf-8") != expected:
                errors.append(f"out-of-date file (re-run build.py): {path.relative_to(REPO_ROOT)}")

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "entries": summary["entries_count"],
                "task_side_entries": summary["task_side_entries_count"],
                "run_side_entries": summary["run_side_entries_count"],
                "taxonomy_classes": summary["taxonomy_classes_count"],
                "sources": summary["sources_count"],
                "structural_signals": summary["structural_signals_count"],
                "entries_with_executed_evidence": summary["entries_with_executed_evidence_count"],
                "tb4_test_leaks": summary["tb4_test_split_leak_check"]["leak_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
