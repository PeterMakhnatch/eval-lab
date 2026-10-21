"""Hackbook v0: catalog validation, leak guard, matcher, findings, and CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from evallab.cli import parser
from evallab.hackbook import (
    STRUCTURAL_SIGNALS,
    HackbookEntry,
    get_entry,
    list_entries,
    load_hackbook,
    load_taxonomy,
    match_target,
    match_task,
    match_trace,
    search_entries,
    validate_no_tb4_test_leak,
)
from evallab.hackbook.matcher import extract_structural_signals, load_task_surface
from evallab.hackbook.models import find_tb4_test_leaks, tb4_test_split_terms
from evallab.qa.models import load_findings_dir

REPO = Path(__file__).resolve().parents[1]
CLASSES = {f"TS{n:02d}" for n in range(1, 12)} | {f"RS{n:02d}" for n in range(1, 8)}
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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _planted_task(task: Path) -> Path:
    _write(
        task / "instruction.md",
        "# Dispatch planner\n"
        "Write the plan to `/app/plan.json` and include the `fuel_margin_kg` field.\n"
        "The planner must be deterministic across runs.\n",
    )
    _write(task / "task.toml", '[task]\nname = "synthetic/planted-task"\n')
    _write(
        task / "tests" / "test_outputs.py",
        "import json\n"
        "import random\n"
        "import sys\n"
        'sys.path.insert(0, "/app")\n'
        "random.seed(42)\n"
        "\n"
        "def test_plan():\n"
        '    plan = json.load(open("/app/plan.json"))\n'
        "    a, b, tol = plan['cost'], 12.5, 0.01\n"
        "    if abs(a - b) > tol:\n"
        "        raise AssertionError('cost')\n"
        "    assert plan['route_label'] == 'ALPHA_ROUTE'\n",
    )
    _write(task / "tests" / "test.sh", "#!/bin/bash\ncd /app && pytest -q /tests\n")
    _write(task / "tests" / "Dockerfile", "FROM python:3.12-slim\n")
    _write(
        task / "environment" / "Dockerfile",
        "FROM python:3.11-slim\nCOPY solution /oracle\nRUN git clone https://example.com/x /app\n",
    )
    _write(task / "solution" / "solve.sh", "#!/bin/bash\npython /app/plan.py\n")
    return task


# --------------------------------------------------------------------------- catalog


@pytest.fixture(scope="module")
def catalog() -> list[HackbookEntry]:
    return load_hackbook(REPO)


def test_catalog_validates_and_covers_classes_and_sources(catalog: list[HackbookEntry]) -> None:
    assert len(catalog) >= 49
    assert {e.taxonomy_class_id for e in catalog} == CLASSES
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


def test_get_list_search(catalog: list[HackbookEntry]) -> None:
    assert get_entry("hb-ts01-001", catalog).slug == "hardcoded-fixture-outputs"
    assert get_entry("hardcoded-fixture-outputs", catalog).id == "HB-TS01-001"
    with pytest.raises(KeyError):
        get_entry("no-such-entry-zzz", catalog)
    by_id = list_entries(catalog, taxonomy_class="TS01")
    assert by_id and by_id == list_entries(catalog, taxonomy_class="weak_check")
    assert all(e.side == "run" for e in list_entries(catalog, side="run"))
    assert list_entries(catalog, source="benchjack")
    assert list_entries(catalog, evidence_status="executed")
    rows = search_entries("conftest sitecustomize", catalog, limit=5)
    assert rows and rows[0]["entry"].taxonomy_class_id in {"TS04", "RS02"}
    assert all(r["entry"].side == "task" for r in search_entries("nan", catalog, side="task"))


# --------------------------------------------------------------------------- task matching


def test_structural_signals_on_planted_task(tmp_path: Path) -> None:
    signals = extract_structural_signals(load_task_surface(_planted_task(tmp_path / "pkg")))
    for name in (
        "unread_output_mentions",
        "single_seed_or_no_repeat",
        "nan_or_null_comparison_bug",
        "oracle_or_answer_file_in_image",
        "git_history_or_remote_leak",
        "in_process_grader_import",
        "conftest_or_sitecustomize_writable",
        "test_only_string_literals",
        "env_python_or_runtime_mismatch",
    ):
        assert name in signals, (name, signals)
        assert all(":" in ref for ref in signals[name])
    assert signals["nan_or_null_comparison_bug"][0].startswith("tests/test_outputs.py:10")
    assert signals["single_seed_or_no_repeat"][0].startswith("tests/test_outputs.py:5")


def test_match_task_ranks_planted_classes(tmp_path: Path) -> None:
    result = match_task(_planted_task(tmp_path / "pkg"), SYNTHETIC)
    assert result["kind"] == "task" and result["task_id"] == "planted-task"
    classes = {m["taxonomy_class_id"] for m in result["matches"]}
    assert {"TS01", "TS02", "TS03", "TS04", "TS05", "TS09"} <= classes
    assert not classes & {"RS01", "RS02", "RS03", "RS07"}
    top = result["matches"][0]
    assert top["evidence_refs"] and top["score"] >= result["matches"][-1]["score"]
    ts04 = next(m for m in result["matches"] if m["taxonomy_class_id"] == "TS04")
    assert ts04["regex_hits"][0]["ref"] == "tests/test_outputs.py:4"
    only = match_task(tmp_path / "pkg", SYNTHETIC, taxonomy_class="TS02")
    assert [m["entry_id"] for m in only["matches"]] == ["HB-TS02-001"]


def test_match_task_real_catalog(tmp_path: Path, catalog: list[HackbookEntry]) -> None:
    result = match_task(_planted_task(tmp_path / "pkg"), catalog)
    classes = {m["taxonomy_class_id"] for m in result["matches"]}
    assert {"TS03", "TS04"} <= classes


def test_match_view_and_emit_findings(tmp_path: Path) -> None:
    view = tmp_path / "view"
    _write(view / "bundle.json", "{}")
    _planted_task(view / "tasks" / "alpha")
    _write(view / "tasks" / "beta" / "instruction.md", "Print hello.\n")
    _write(view / "tasks" / "beta" / "tests" / "test_a.py", "def test_a():\n    assert True\n")
    out = tmp_path / "findings"
    result = match_target(view, entries=SYNTHETIC, emit_findings_dir=out)
    assert result["kind"] == "view" and len(result["tasks"]) == 2
    alpha = next(t for t in result["tasks"] if t["task_id"] == "alpha")
    assert all(
        ref.startswith("tasks/alpha/") for m in alpha["matches"] for ref in m["evidence_refs"]
    )
    findings = load_findings_dir(out)
    assert findings and len(findings) == len(result["findings"])
    assert {f.subject.task_id for f in findings} == {"alpha"}
    assert len({(f.subject.task_id, f.root_cause[0]) for f in findings}) == len(findings)
    assert all(f.verdict == "fail" and f.fault == "eval" for f in findings)
    inside = match_target(view / "tasks" / "alpha", entries=SYNTHETIC)
    assert inside["kind"] == "task"
    assert inside["matches"][0]["evidence_refs"][0].startswith("tasks/alpha/")


# --------------------------------------------------------------------------- trace matching


def _atif(path: Path) -> Path:
    steps = [
        {"step_id": 1, "source": "user", "message": "Fix the planner."},
        {
            "step_id": 2,
            "source": "agent",
            "message": "Looking at history.",
            "tool_calls": [
                {"function_name": "bash", "arguments": {"command": "git log --all --oneline"}}
            ],
        },
        {
            "step_id": 3,
            "source": "agent",
            "message": "",
            "tool_calls": [
                {"function_name": "bash", "arguments": {"command": "cat /tests/oracle.py"}}
            ],
        },
        {
            "step_id": 4,
            "source": "agent",
            "message": "",
            "tool_calls": [
                {
                    "function_name": "write",
                    "arguments": {
                        "path": "/app/plan.py",
                        "content": "def plan():\n    return [1, 2, 3]\n",
                    },
                }
            ],
        },
        {
            "step_id": 5,
            "source": "agent",
            "message": "",
            "tool_calls": [
                {
                    "function_name": "bash",
                    "arguments": {"command": "echo 'import sys; sys.exit(0)' > /app/conftest.py"},
                }
            ],
        },
    ]
    _write(path, json.dumps({"schema_version": "ATIF-v1.4", "steps": steps}))
    return path


def test_match_trace_atif(tmp_path: Path) -> None:
    result = match_trace(_atif(tmp_path / "trajectory.json"), SYNTHETIC)
    assert result["kind"] == "trace"
    by_class = {m["taxonomy_class_id"]: m for m in result["matches"]}
    assert {"RS01", "RS02", "RS03"} <= set(by_class)
    assert "step:2" in by_class["RS03"]["evidence_refs"]
    assert "step:3" in by_class["RS03"]["evidence_refs"]
    assert "step:4" in by_class["RS01"]["evidence_refs"]
    assert "step:5" in by_class["RS02"]["evidence_refs"]
    assert {
        "git_history_probe",
        "tests_or_solution_read",
        "hardcoded_return",
        "exit_or_skip_bypass",
    } <= set(result["signals"])
    assert not any(m["side"] == "task" for m in result["matches"])


def test_match_trace_actions_jsonl_and_harbor_trial(tmp_path: Path) -> None:
    actions = tmp_path / "actions.jsonl"
    _write(
        actions, "\n".join(json.dumps({"command": c}) for c in ["ls", "git log --all -p"]) + "\n"
    )
    result = match_trace(actions, SYNTHETIC)
    assert result["matches"][0]["taxonomy_class_id"] == "RS03"
    assert "step:1" in result["matches"][0]["evidence_refs"]

    trial = tmp_path / "trial-1"
    _atif(trial / "agent" / "trajectory.json")
    _write(trial / "verifier" / "agent.diff", "--- a/x.py\n+++ b/x.py\n@@\n+    return [4, 5]\n")
    _write(
        trial / "result.json",
        json.dumps(
            {"task_name": "planted-task", "trial_name": "trial-1", "exception": "returncode 137"},
            indent=2,
        ),
    )
    out = tmp_path / "findings"
    payload = match_target(trial, entries=SYNTHETIC, emit_findings_dir=out)
    assert payload["kind"] == "trace" and payload["task_id"] == "planted-task"
    by_class = {m["taxonomy_class_id"]: m for m in payload["matches"]}
    assert "verifier/agent.diff:4" in by_class["RS01"]["evidence_refs"]
    assert any(ref.startswith("result.json:") for ref in by_class["RS07"]["evidence_refs"])
    findings = load_findings_dir(out)
    assert {f.subject.kind for f in findings} == {"trace"}
    rs07 = next(f for f in findings if "infra_fail" in f.root_cause)
    assert rs07.fault == "platform" and rs07.subject.trial_id == "trial-1"


# --------------------------------------------------------------------------- CLI


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    args = parser().parse_args(["hackbook", *argv])
    code = args.func(args, REPO)
    return code, capsys.readouterr().out


def test_cli_list_show_search(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _cli(capsys, "list", "--class", "TS04", "--json")
    assert code == 0 and {e["taxonomy_class_id"] for e in json.loads(out)} == {"TS04"}
    code, out = _cli(capsys, "list", "--side", "run")
    assert code == 0 and "HB-RS01-001" in out and "HB-TS01-001" not in out
    code, out = _cli(capsys, "show", "HB-TS01-001", "--json")
    assert code == 0 and json.loads(out)["slug"] == "hardcoded-fixture-outputs"
    code, out = _cli(capsys, "show", "hardcoded-fixture-outputs")
    assert code == 0 and "Detection" in out and "Fix" in out
    assert _cli(capsys, "show", "no-such-entry-zzz")[0] == 2
    code, out = _cli(capsys, "search", "git history", "--limit", "3", "--json")
    rows = json.loads(out)
    assert code == 0 and 0 < len(rows) <= 3 and rows[0]["score"] > 0
    code, out = _cli(capsys, "search", "conftest")
    assert code == 0 and "HB-" in out


def test_cli_match(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    task = _planted_task(tmp_path / "pkg")
    code, out = _cli(capsys, "match", str(task), "--json", "--top", "5")
    payload = json.loads(out)
    assert code == 0 and payload["kind"] == "task" and 0 < len(payload["matches"]) <= 5
    findings = tmp_path / "findings"
    code, out = _cli(capsys, "match", str(task), "--emit-findings", str(findings))
    assert code == 0 and "task planted-task" in out and "qa.finding/v1" in out
    assert load_findings_dir(findings)
    trace = _atif(tmp_path / "trajectory.json")
    code, out = _cli(capsys, "match", str(trace), "--kind", "trace", "--class", "RS03")
    assert code == 0 and "RS03" in out
