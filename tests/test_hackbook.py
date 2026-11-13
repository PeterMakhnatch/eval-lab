"""Hackbook v0: catalog validation and leak guards."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from evallab.hackbook.models import (
    STRUCTURAL_SIGNALS,
    HackbookEntry,
    HackbookExample,
    find_tb4_test_leaks,
    get_entry,
    load_hackbook,
    load_taxonomy,
    tb4_test_split_terms,
    validate_no_tb4_test_leak,
)

REPO = Path(__file__).resolve().parents[1]
#: Class ids the taxonomy declares, read from it so a new class cannot leave this
#: file stale (RS08 was added by the B3 hackbook change and a literal copy missed it).
CLASSES = {
    row["id"]
    for side in ("task_side", "run_side")
    for row in json.loads((REPO / "research/hackbook/taxonomy.json").read_text(encoding="utf-8"))[
        side
    ]
}
#: Declared before its first catalog entry; ``build.py`` tolerates that class alone.
CLASSES_WITHOUT_ENTRIES = {"RS08"}
SOURCES = {
    "preference_model_tb4",
    "benchjack",
    "cheatbench",
    "terminal_wrench",
    "hacktrace",
    "envcheck",
    "mimo_atlas",
    "eval3",
    "tb2_to_tb21",
    "reward_seeker",
    "tb4_run_census",
}
TASK_CLASS_NAMES = {
    "TS01": ("weak_check", "false_pass", "weak"),
    "TS02": ("grader_logic_bug", "either", "broken"),
    "TS03": ("answer_reachable", "false_pass", "reachable"),
    "TS04": ("grader_tamperable", "false_pass", "tamperable"),
    "TS05": ("unstated_requirement", "false_fail", "prompt_misaligned"),
    "TS09": ("env_mismatch_or_flaky", "either", "broken"),
    "RS01": ("exploit_grader_gap", "false_pass", "reward_hacking"),
    "RS02": ("tamper_with_grading", "false_pass", "reward_hacking"),
    "RS03": ("answer_lookup", "false_pass", "reward_hacking"),
    "RS07": ("infra_fail", "false_fail", "failure_cause"),
}


# --------------------------------------------------------------------------- fixtures


def _entry(
    entry_id: str,
    *,
    signals: list[str] | None = None,
    file_regexes: list[dict[str, Any]] | None = None,
    command_regexes: list[dict[str, Any]] | None = None,
    diff_regexes: list[dict[str, Any]] | None = None,
) -> HackbookEntry:
    class_id = entry_id[3:7]
    name, direction, category = TASK_CLASS_NAMES[class_id]
    return HackbookEntry.model_validate(
        {
            "schema": "evallab.hackbook.entry/v0",
            "version": "v0",
            "id": entry_id,
            "slug": f"synthetic-{entry_id.lower()}",
            "title": f"Synthetic {name}",
            "taxonomy_class_id": class_id,
            "taxonomy_class": name,
            "side": "task" if class_id.startswith("TS") else "run",
            "direction": direction,
            "qa_category": category,
            "enabled_by": [],
            "sources": ["benchjack"],
            "mechanism": "synthetic",
            "signatures": {
                "file_globs": ["tests/*.py"],
                "file_regexes": file_regexes or [],
                "trace_command_regexes": command_regexes or [],
                "trace_diff_regexes": diff_regexes or [],
                "structural_signals": signals or [],
            },
            "detection_recipe": {
                "kind": "static_check",
                "method_id": "static_review",
                "summary": "s",
                "steps": [],
                "rule_or_probe": "",
            },
            "exploit_template": {
                "applicable": False,
                "mode": "none",
                "goal": "",
                "preconditions": [],
                "template_steps": [],
                "snippet": None,
                "differential_verdict": {},
            },
            "fix_pattern": {
                "fix_location": "grader",
                "summary": "s",
                "checklist": [],
                "example_diff_or_rule": "",
            },
            "examples": [],
        }
    )


SYNTHETIC = [
    _entry("HB-TS01-001", signals=["unread_output_mentions", "single_seed_or_no_repeat"]),
    _entry("HB-TS02-001", signals=["nan_or_null_comparison_bug"]),
    _entry("HB-TS03-001", signals=["oracle_or_answer_file_in_image", "git_history_or_remote_leak"]),
    _entry(
        "HB-TS04-001",
        signals=["in_process_grader_import", "conftest_or_sitecustomize_writable"],
        file_regexes=[{"surface": "tests", "pattern": r"sys\.path\.insert", "weight": 0.5}],
    ),
    _entry("HB-TS05-001", signals=["test_only_string_literals"]),
    _entry("HB-TS09-001", signals=["env_python_or_runtime_mismatch"]),
    _entry(
        "HB-RS01-001",
        diff_regexes=[{"pattern": r"^\+\s*return\s+\[", "weight": 0.5}],
    ),
    _entry("HB-RS02-001", command_regexes=[{"pattern": r"conftest\.py", "weight": 0.5}]),
    _entry("HB-RS03-001", command_regexes=[{"pattern": r"git\s+log\s+--all", "weight": 0.5}]),
    _entry("HB-RS07-001"),
]


# --------------------------------------------------------------------------- catalog


@pytest.fixture(scope="module")
def catalog() -> list[HackbookEntry]:
    return load_hackbook(REPO)


def test_catalog_validates_and_covers_classes_and_sources(catalog: list[HackbookEntry]) -> None:
    assert len(catalog) >= 49
    assert {e.taxonomy_class_id for e in catalog} == CLASSES - CLASSES_WITHOUT_ENTRIES
    assert {s for e in catalog for s in e.sources} >= SOURCES
    for entry in catalog:
        assert set(entry.signatures.structural_signals) <= set(STRUCTURAL_SIGNALS), entry.id
        assert entry.side == ("task" if entry.taxonomy_class_id.startswith("TS") else "run")
    assert load_taxonomy(REPO)


def test_build_check_passes() -> None:
    proc = subprocess.run(
        [sys.executable, "research/hackbook/build.py", "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_catalog_cites_no_test_split_item_of_the_mimo_weak_test_keys(
    catalog: list[HackbookEntry],
) -> None:
    spec = importlib.util.spec_from_file_location(
        "hackbook_build", REPO / "research/hackbook/build.py"
    )
    assert spec and spec.loader
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    taxonomy = load_taxonomy(REPO)
    assert not [e for e in build.validate_catalog(catalog, taxonomy) if "test-split item" in e]
    held_out = sorted(build._mimo_weak_test_split_tasks())
    assert held_out
    cite = HackbookExample(
        evidence_status="executed",
        item_or_task_id=f"mimo-weak-tests-v2.1:{held_out[0]}",
        source="mimo_atlas",
        source_url="https://example.com/witness",
        summary="a TEST item must not become catalog evidence",
    )
    leaky = catalog[0].model_copy(update={"examples": [*catalog[0].examples, cite]})
    errors = build.validate_catalog([leaky, *catalog[1:]], taxonomy)
    assert [e for e in errors if "test-split item of mimo-weak-tests-v2.1" in e]


def test_get_entry(catalog: list[HackbookEntry]) -> None:
    assert get_entry("hb-ts01-001", catalog).slug == "hardcoded-fixture-outputs"
    assert get_entry("hardcoded-fixture-outputs", catalog).id == "HB-TS01-001"
    with pytest.raises(KeyError):
        get_entry("no-such-entry-zzz", catalog)


def test_no_tb4_test_split_leak(catalog: list[HackbookEntry]) -> None:
    assert validate_no_tb4_test_leak(catalog, root=REPO) == []
    paths = [
        *sorted((REPO / "research/hackbook").rglob("*")),
        *sorted((REPO / "src/evallab/hackbook").glob("*.py")),
        REPO / ".omp/skills/hackbook/SKILL.md",
        Path(__file__),
    ]
    for path in paths:
        if path.is_file() and "__pycache__" not in path.parts:
            assert find_tb4_test_leaks(path.read_text(errors="replace"), root=REPO) == [], path


def test_leak_guard_detects_and_respects_boundaries() -> None:
    tasks, items = tb4_test_split_terms(REPO)
    assert len(tasks) == 41 and items
    assert find_tb4_test_leaks(f"see {tasks[0]} here", root=REPO) == [tasks[0]]
    assert find_tb4_test_leaks(f"id {items[0]}", root=REPO)
    assert find_tb4_test_leaks("layout-config-recreation2 is a dev task", root=REPO) == []
    leaky = SYNTHETIC[0].model_copy(update={"mechanism": f"like {tasks[0]}"})
    assert validate_no_tb4_test_leak([leaky], root=REPO) == [f"HB-TS01-001: {tasks[0]}"]
