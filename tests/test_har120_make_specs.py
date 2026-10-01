"""Behavioral checks for the HAR-120/G2 spec generator's contamination drop."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

EXP = Path("research/experiments/har120-data-batch/make_specs.py")


def _load():
    spec = importlib.util.spec_from_file_location("har120_make_specs", EXP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MAKE = _load()


def _proposal(*projects: str) -> list[dict]:
    return [
        {
            "task_id": f"format-code-task-{i:06d}",
            "project": project,
            "run": "original",
            "run_digest": "sha256:" + "0" * 64,
        }
        for i, project in enumerate(projects, start=1)
    ]


def _eval(*repos: str) -> list[dict]:
    return [{"task": f"eval-{i}", "repo_key": MAKE.repo_key(repo)} for i, repo in enumerate(repos)]


def test_repo_key_normalises_last_segment_case_and_separators() -> None:
    assert MAKE.repo_key("github.com/psf/black") == MAKE.repo_key("black") == "black"
    assert MAKE.repo_key("Django_Filters") == "django_filters"
    assert MAKE.repo_key("github.com/jazzband/pip-tools") == "pip_tools"


def test_repo_key_unknown_project_is_none() -> None:
    assert MAKE.repo_key("format-code-task-000016") is None


def test_drop_on_shared_repo_reports_eval_task() -> None:
    kept, dropped = MAKE.select_tasks(_proposal("black", "environ"), _eval("github.com/psf/black"))
    assert [row["task_id"] for row in kept] == ["format-code-task-000002"]
    assert len(dropped) == 1
    assert dropped[0]["task_id"] == "format-code-task-000001"
    assert "eval-0" in dropped[0]["reason"]


def test_drop_is_case_and_separator_insensitive() -> None:
    kept, dropped = MAKE.select_tasks(_proposal("django_filters"), _eval("Django-Filters"))
    assert kept == []
    assert len(dropped) == 1


def test_no_drop_without_repo_overlap() -> None:
    kept, dropped = MAKE.select_tasks(_proposal("black", "environ"), _eval("responses"))
    assert len(kept) == 2
    assert dropped == []


def test_empty_eval_list_drops_nothing() -> None:
    kept, dropped = MAKE.select_tasks(_proposal("black"), [])
    assert len(kept) == 1
    assert dropped == []


def test_eval_rows_without_a_known_repo_drop_nothing() -> None:
    kept, dropped = MAKE.select_tasks(_proposal("black"), [{"task": "eval-0", "repo_key": None}])
    assert len(kept) == 1
    assert dropped == []


def test_spec_names_are_deterministic_attempt_pairs() -> None:
    assert MAKE.spec_name("format-code-task-001647", 1) == "har120-001647-a1"
    assert MAKE.spec_name("format-code-task-001647", 2) == "har120-001647-a2"
    assert MAKE.spec_name("format-code-task-001647", 1) == MAKE.spec_name(
        "format-code-task-001647", 1
    )


def test_eval_list_accepts_project_header(tmp_path: Path) -> None:
    # HAR-127 names the column `repo`; accept the ledger's `project` too.
    path = tmp_path / "eval.csv"
    path.write_text("task,project\nformat-code-task-009999,black\n")
    rows = MAKE.load_eval_list(path)
    assert rows == [{"task": "format-code-task-009999", "repo_key": "black"}]
    kept, dropped = MAKE.select_tasks(_proposal("github.com/psf/black"), rows)
    assert kept == []
    assert len(dropped) == 1


def test_eval_list_header_only_drops_nothing(tmp_path: Path) -> None:
    path = tmp_path / "eval.csv"
    path.write_text("task,digest,repo,image_mib\n")
    assert MAKE.load_eval_list(path) == []
