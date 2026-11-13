"""Hackbook v0: catalog validation, leak guard, matcher, and findings."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from evallab.hackbook.matcher import (
    extract_structural_signals,
    load_task_surface,
    match_target,
    match_task,
)
from evallab.hackbook.models import (
    STRUCTURAL_SIGNALS,
    HackbookEntry,
    HackbookExample,
    coverage_summary,
    find_tb4_test_leaks,
    get_entry,
    load_hackbook,
    load_taxonomy,
    tb4_test_split_terms,
    validate_no_tb4_test_leak,
)
from evallab.hackbook.refcov import change_blocks, unnamed_blocks
from evallab.qa.models import load_findings_dir

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


def test_heldout_runner_lets_a_hackbook_version_spend_the_test_split_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = REPO / "research/experiments/hackbook-tb4-heldout-20261021/run.py"
    spec = importlib.util.spec_from_file_location("hackbook_heldout_run", path)
    assert spec and spec.loader
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    version = run.version_id()
    assert version.startswith("hackbook-match@") and version == run.version_id()
    exposure = tmp_path / "exposure.jsonl"
    _write(exposure, json.dumps({"system": f"{version}: frozen catalog and matcher"}) + "\n")
    monkeypatch.setattr(run, "EXPOSURE", exposure)
    assert run.already_exposed(version) and not run.already_exposed("hackbook-match@000000000000")
    argv = ["run.py", "--splits", "dev,test", "--out", str(tmp_path / "out.json")]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit, match="--spend-the-test-split"):
        run.main()
    monkeypatch.setattr(sys, "argv", [*argv, "--spend-the-test-split"])
    with pytest.raises(SystemExit, match="already has a TEST exposure"):
        run.main()
    assert not (tmp_path / "out.json").exists()


def test_get_entry(catalog: list[HackbookEntry]) -> None:
    assert get_entry("hb-ts01-001", catalog).slug == "hardcoded-fixture-outputs"
    assert get_entry("hardcoded-fixture-outputs", catalog).id == "HB-TS01-001"
    with pytest.raises(KeyError):
        get_entry("no-such-entry-zzz", catalog)


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


REFERENCE_PATCH = """\
diff --git a/app/pricing.py b/app/pricing.py
index 1111111..2222222 100644
--- a/app/pricing.py
+++ b/app/pricing.py
@@ -10,6 +10,9 @@ def base_price(order):
     total = order.subtotal
+    surcharge = compute_surcharge(order.region)
+    total += surcharge
     return total
@@ -30,5 +33,7 @@ def apply_discount(order):
     rate = lookup()
-    return order.subtotal
+    return order.subtotal * discount_rate(order)
     done = True
diff --git a/app/ledger.py b/app/ledger.py
--- a/app/ledger.py
+++ b/app/ledger.py
@@ -3,4 +3,5 @@
 import os
+    record_posting(entry)
 x = 1
diff --git a/app/audit.py b/app/audit.py
--- a/app/audit.py
+++ b/app/audit.py
@@ -3,4 +3,5 @@
 import os
+from app.rates import regional_rates
 x = 1
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,2 +1,3 @@
 intro
+Describe the surcharge in prose only.
 end
diff --git a/tests/test_pricing.py b/tests/test_pricing.py
--- a/tests/test_pricing.py
+++ b/tests/test_pricing.py
@@ -1,1 +1,2 @@
 import pytest
+def test_new_helper_name_unique_xyz(): pass
diff --git a/db/schema.sql b/db/schema.sql
--- a/db/schema.sql
+++ b/db/schema.sql
@@ -4,3 +4,3 @@
 create table t (id int);
--- legacy comment kept for history
+CREATE INDEX posting_lookup ON t (id);
 drop table u;
"""
TEST_TEXT = (
    "from app import ledger\n\ndef test_discount():\n    assert discount_rate(cart) == 0.1\n"
)


def test_change_blocks_split_a_diff_into_contiguous_runs() -> None:
    blocks = change_blocks(REFERENCE_PATCH)
    by_path = [(b.path, b.line, len(b.plus), len(b.minus)) for b in blocks]
    assert by_path[:4] == [
        ("app/pricing.py", 11, 2, 0),
        ("app/pricing.py", 31, 1, 1),
        ("app/ledger.py", 4, 1, 0),
        ("app/audit.py", 4, 1, 0),
    ]
    # A removed SQL comment (`-- legacy ...`) inside a hunk is a removed line, not a file header.
    sql = next(b for b in blocks if b.path == "db/schema.sql")
    assert sql.minus == ("-- legacy comment kept for history",) and len(sql.plus) == 1


def test_unnamed_blocks_are_behaviour_no_test_names_by_file_or_identifier() -> None:
    flagged = {(b.path, b.line) for b in unnamed_blocks(REFERENCE_PATCH, TEST_TEXT)}
    # `compute_surcharge` is named nowhere and the SQL index is code too.
    assert ("app/pricing.py", 11) in flagged and ("db/schema.sql", 5) in flagged
    # `discount_rate` is named by a test; `ledger` is named through the module import; an
    # import-only block, a README edit and an edit of a test file are not behaviour a test misses.
    assert ("app/pricing.py", 31) not in flagged
    assert ("app/ledger.py", 4) not in flagged
    assert not {p for p, _ in flagged} & {"app/audit.py", "README.md", "tests/test_pricing.py"}


def _reference_task(task: Path) -> Path:
    _write(task / "instruction.md", "Add a regional surcharge to prices.\n")
    _write(task / "tests" / "test_pricing.py", TEST_TEXT)
    # Runner boilerplate names identifiers too, but it runs tests; it does not exercise code.
    _write(
        task / "tests" / "test.sh", "#!/bin/bash\n# compute_surcharge record_posting\npytest -q\n"
    )
    return task


def test_reference_patch_feeds_the_untested_block_signal(tmp_path: Path) -> None:
    entries = [_entry("HB-TS01-009", signals=["untested_reference_subblock"])]
    task = _reference_task(tmp_path / "pkg")
    patch = tmp_path / "fix.patch"
    _write(patch, REFERENCE_PATCH)
    # no reference and no solution/: nothing to compare against
    assert "untested_reference_subblock" not in match_task(task, entries)["signals"]
    result = match_task(task, entries, reference=patch)
    refs = result["signals"]["untested_reference_subblock"]
    assert any(r.startswith("reference app/pricing.py:11 ") for r in refs)
    # `tests/test.sh` names compute_surcharge, but a runner script exercises no code
    (hit,) = result["matches"]
    assert hit["entry_id"] == "HB-TS01-009" and hit["score"] >= 1.5
    # The same patch shipped as solution/*.patch needs no flag.
    _write(task / "solution" / "fix.patch", REFERENCE_PATCH)
    assert match_task(task, entries)["signals"]["untested_reference_subblock"] == refs
    # Only runner scripts to read: no test code to judge a block against.
    _write(task / "tests" / "test_pricing.py", "")
    assert (
        "untested_reference_subblock" not in match_task(task, entries, reference=patch)["signals"]
    )
    # A reference belongs to one task package.
    view = tmp_path / "view"
    _write(view / "bundle.json", "{}")
    _reference_task(view / "tasks" / "alpha")
    with pytest.raises(ValueError, match="one task"):
        match_task(view, entries, reference=patch)
    with pytest.raises(ValueError, match="task package"):
        match_target(task / "tests" / "test_pricing.py", kind="trace", reference=patch)
    with pytest.raises(ValueError, match="not found"):
        match_task(task, entries, reference=tmp_path / "missing.patch")


def test_solution_code_without_a_patch_still_flags_functions_no_test_calls(tmp_path: Path) -> None:
    task = tmp_path / "pkg"
    _write(task / "instruction.md", "Compute totals.\n")
    _write(task / "tests" / "test_totals.py", "from sol import total_a\n\nassert total_a() == 1\n")
    _write(
        task / "solution" / "sol.py",
        "def total_a():\n    return 1\n\ndef total_b():\n    return 2\n",
    )
    entries = [_entry("HB-TS01-009", signals=["untested_reference_subblock"])]
    refs = match_task(task, entries)["signals"]["untested_reference_subblock"]
    assert len(refs) == 1 and "total_b" in refs[0]


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


# --------------------------------------------------------------------------- precision


def _harbor_boilerplate_task(task: Path) -> Path:
    """A clean Harbor-schema-1.1 package: only the harness's own scaffolding, no defect."""
    _write(task / "instruction.md", "Write the sum of the two inputs to `/app/out.txt`.\n")
    _write(
        task / "task.toml",
        'schema_version = "1.1"\n[task]\nname = "synthetic/clean"\n'
        "[environment]\nmemory_mb = 2048\nallow_internet = true\n",
    )
    _write(task / ".gitignore", "*.pyc\n")
    _write(task / "environment" / "Dockerfile", "FROM docker.io/example/base@sha256:abc\n")
    _write(
        task / "tests" / "test.sh",
        "#!/bin/bash\n"
        "curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh\n"
        "git add -A\ngit rm -f --cached junk\n"
        'echo "cache warm"\n'
        "uvx -p 3.13 pytest /tests/test_outputs.py\n"
        'if [ $? -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi\n',
    )
    _write(
        task / "tests" / "test_outputs.py",
        "def test_sum():\n    assert int(open('/app/out.txt').read()) == 7\n",
    )
    return task


def test_harness_boilerplate_alone_never_fires_a_task_side_entry(
    tmp_path: Path, catalog: list[HackbookEntry]
) -> None:
    # Regression: reward.txt / single-FROM Dockerfile / `git add -A` / `cache` / uv installer /
    # `allow_internet = true` once fired HB-TS03-00x, HB-TS04-00x and HB-TS05-004 on every task.
    task = _harbor_boilerplate_task(tmp_path / "clean")
    result = match_task(task, catalog)
    assert result["matches"] == [], [m["entry_id"] for m in result["matches"]]
    assert result["signals"] == {}


def test_explicit_internet_counts_only_when_the_instruction_names_an_upstream(
    tmp_path: Path, catalog: list[HackbookEntry]
) -> None:
    task = _harbor_boilerplate_task(tmp_path / "upstream")
    _write(
        task / "instruction.md",
        "Reproduce the result of https://github.com/example/project at the pinned tag.\n",
    )
    result = match_task(task, catalog)
    assert "git_history_or_remote_leak" in result["signals"]
    _write(task / "environment" / ".gitignore", "x\n")
    surface = load_task_surface(task)
    refs = extract_structural_signals(surface)["git_history_or_remote_leak"]
    assert not any("git metadata" in ref for ref in refs)  # .gitignore is not .git/


def test_diff_format_hidden_tests_only_count_added_assertions(tmp_path: Path) -> None:
    task = tmp_path / "patch-task"
    _write(task / "instruction.md", "Make `total()` correct.\n")
    _write(
        task / "tests" / "test.patch",
        "--- a/tests/test_total.py\n+++ b/tests/test_total.py\n@@ -1,3 +1,3 @@\n"
        "-    assert total(a) == 1\n+    assert total(a) == 2\n",
    )
    signals = extract_structural_signals(load_task_surface(task))
    assert "wrong_or_contradictory_gold_constant" not in signals


def test_coverage_counts_each_evidence_status_per_source() -> None:
    def example(status: str, source: str) -> HackbookExample:
        return HackbookExample.model_validate(
            {
                "source": source,
                "evidence_status": status,
                "item_or_task_id": f"{source}:{status}",
                "summary": "s",
            }
        )

    replayed = SYNTHETIC[0].model_copy(
        update={
            "examples": [example("executed", "reward_seeker"), example("claimed", "benchjack")],
            "sources": ["reward_seeker", "benchjack"],
        }
    )
    seen = SYNTHETIC[1].model_copy(
        update={"examples": [example("observed", "tb4_run_census")], "sources": ["tb4_run_census"]}
    )
    cov = coverage_summary([replayed, seen], root=REPO)
    assert cov["total_examples"] == 3
    assert (cov["executed_examples"], cov["observed_examples"], cov["claimed_examples"]) == (
        1,
        1,
        1,
    )
    # Observed is neither executed nor claimed: only the replayed entry counts as executed.
    assert cov["entries_with_executed_evidence"] == 1
    by_source = {row["source"]: row for row in cov["by_source"]}
    assert by_source["reward_seeker"]["executed_examples"] == 1
    assert by_source["tb4_run_census"]["observed_examples"] == 1
    assert by_source["tb4_run_census"]["executed_examples"] == 0
    assert by_source["benchjack"]["claimed_examples"] == 1
    by_class = {row["taxonomy_class_id"]: row for row in cov["by_class"]}
    assert by_class["TS01"]["executed_examples"] == 1 and by_class["TS02"]["observed_examples"] == 1


def test_direction_keeps_the_side_the_caller_hunts_plus_either(tmp_path: Path) -> None:
    task = _planted_task(tmp_path / "pkg")
    everything = match_task(task, top=100)["matches"]
    assert {"false_pass", "false_fail", "either"} <= {m["direction"] for m in everything}
    seeker = match_task(task, direction="false_pass", top=100)["matches"]
    assert {m["direction"] for m in seeker} == {"false_pass", "either"}
    assert {m["entry_id"] for m in seeker} == {
        m["entry_id"] for m in everything if m["direction"] != "false_fail"
    }
    repairer = match_task(task, direction="false_fail", top=100)["matches"]
    assert {m["direction"] for m in repairer} == {"false_fail", "either"}
    # The cut to `top` happens after the filter: a false-fail entry never takes a slot.
    top = match_task(task, direction="false_pass", top=3)["matches"]
    assert len(top) == 3 and all(m["direction"] != "false_fail" for m in top)
    with pytest.raises(ValueError, match="direction must be one of"):
        match_task(task, direction="pass")


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
