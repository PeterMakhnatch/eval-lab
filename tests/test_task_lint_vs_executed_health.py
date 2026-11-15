"""Offline fixture contracts for the task-lint/executed-health join (HAR-454)."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "research/experiments/task-lint-vs-executed-health/build.py"
HEADER = "task,executed_class,lint_codes,finding_count\n"


@pytest.fixture
def build():
    spec = importlib.util.spec_from_file_location("task_lint_vs_executed_health", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_task(corpus: Path, name: str, *, clean: bool, solution: bool = True) -> Path:
    task = corpus / name
    task.mkdir(parents=True)
    if solution:
        (task / "solution").mkdir()
        (task / "solution" / "solve.sh").write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
    manifest = (
        'artifacts = ["output.txt"]\n\n[verifier]\nenvironment_mode = "separate"\n'
        if clean
        else "[verifier]\n"
    )
    (task / "task.toml").write_text(manifest, encoding="utf-8")
    return task


def write_card(path: Path, rows: list[tuple[str, str]]) -> None:
    payload: dict[str, Any] = {"rows": [{"task": task, "class": kind} for task, kind in rows]}
    path.write_text(json.dumps(payload), encoding="utf-8")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_join_keeps_card_tasks_and_skips_extra_packages(build, tmp_path: Path) -> None:
    corpus, card, out = tmp_path / "corpus", tmp_path / "card.json", tmp_path / "out"
    write_task(corpus, "dirty-task", clean=False, solution=False)
    write_task(corpus, "clean-task", clean=True)
    write_task(corpus, "uncarded-task", clean=False, solution=False)
    write_card(card, [("clean-task", "clean"), ("dirty-task", "reference_below_one")])

    assert build.main(["--card", str(card), "--corpus", str(corpus), "--out-dir", str(out)]) == 0

    rows = read_rows(out / "task-lint-health.csv")
    assert [row["task"] for row in rows] == ["clean-task", "dirty-task"]
    assert rows[0] == {
        "task": "clean-task",
        "executed_class": "clean",
        "lint_codes": "",
        "finding_count": "0",
    }
    assert rows[1]["executed_class"] == "reference_below_one"
    assert rows[1]["lint_codes"] == "solution-present;verifier-isolation"
    assert rows[1]["finding_count"] == "2"


def test_card_task_absent_from_clone_exits_2_without_output(
    build, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus, card, out = tmp_path / "corpus", tmp_path / "card.json", tmp_path / "out"
    corpus.mkdir()
    write_card(card, [("missing-task", "clean")])

    assert build.main(["--card", str(card), "--corpus", str(corpus), "--out-dir", str(out)]) == 2

    assert "join key" in capsys.readouterr().err
    assert not out.exists()


def test_task_with_zero_findings_has_empty_codes(build, tmp_path: Path) -> None:
    corpus, card = tmp_path / "corpus", tmp_path / "card.json"
    write_task(corpus, "clean-task", clean=True)
    write_card(card, [("clean-task", "clean")])

    text = build.render(build.build_rows(card, corpus))

    assert text == HEADER + "clean-task,clean,,0\n"


def test_check_passes_then_rejects_an_edited_copy_without_writing(
    build, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus, card, out = tmp_path / "corpus", tmp_path / "card.json", tmp_path / "out"
    write_task(corpus, "clean-task", clean=True)
    write_card(card, [("clean-task", "clean")])
    argv = ["--card", str(card), "--corpus", str(corpus), "--out-dir", str(out)]
    assert build.main(argv) == 0
    csv_path = out / "task-lint-health.csv"
    built = csv_path.read_bytes()

    assert build.main([*argv, "--check"]) == 0
    assert csv_path.read_bytes() == built

    edited = "task,executed_class,lint_codes,finding_count\nedited,clean,,0\n"
    csv_path.write_text(edited, encoding="utf-8")
    assert build.main([*argv, "--check"]) == 1
    assert csv_path.read_text(encoding="utf-8") == edited
    assert "edited" in capsys.readouterr().err
