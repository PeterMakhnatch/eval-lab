"""Tests for the developer fast-loop command (evallab registry devloop)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.devloop import (
    affected_test_modules,
    is_doc_path,
    is_full_suite_trigger,
    is_src_path,
    is_test_path,
    plan_devloop,
    run_devloop,
    src_path_to_module,
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_affected_test_modules_src_change_maps_to_its_tests(repo_root: Path) -> None:
    """A changed src module maps to every test module that imports it."""
    changed = ["src/evallab/governance.py"]
    affected = affected_test_modules(changed, repo_root)
    assert "tests/test_governance.py" in affected


def test_affected_test_modules_test_change_maps_to_itself(repo_root: Path) -> None:
    """A changed test module maps to itself."""
    changed = ["tests/test_governance.py"]
    affected = affected_test_modules(changed, repo_root)
    assert affected == ["tests/test_governance.py"]


def test_affected_test_modules_docs_change_maps_to_empty(repo_root: Path) -> None:
    """A documentation-only change returns an empty list (indicating docs lane)."""
    changed = ["docs/NOW.md", "agents/CHECKS.md", "README.md"]
    affected = affected_test_modules(changed, repo_root)
    assert affected == []


def test_affected_test_modules_unknown_config_change_maps_to_all(repo_root: Path) -> None:
    """An unknown configuration change triggers the full test suite."""
    changed = ["unknown_config.json"]
    affected = affected_test_modules(changed, repo_root)
    assert affected == ["__all__"]


def test_affected_test_modules_makefile_maps_to_all(repo_root: Path) -> None:
    """Makefile or workflow changes trigger the full test suite."""
    assert affected_test_modules(["Makefile"], repo_root) == ["__all__"]
    assert affected_test_modules([".github/workflows/ci.yml"], repo_root) == ["__all__"]
    assert affected_test_modules(["pyproject.toml"], repo_root) == ["__all__"]


def test_src_path_to_module() -> None:
    assert src_path_to_module("src/evallab/devloop.py") == "evallab.devloop"
    assert src_path_to_module("src/evallab/__init__.py") == "evallab"
    assert (
        src_path_to_module("src/evallab/interpretation/trajectory_ir.py")
        == "evallab.interpretation.trajectory_ir"
    )


def test_path_classifiers() -> None:
    assert is_doc_path("docs/NOW.md")
    assert is_doc_path("agents/CHECKS.md")
    assert is_doc_path("AGENTS.md")
    assert is_doc_path(".omp/skills/delivery/SKILL.md")
    assert not is_doc_path("src/evallab/devloop.py")

    assert is_test_path("tests/test_devloop.py")
    assert not is_test_path("tests/conftest.py")
    assert not is_test_path("src/evallab/devloop.py")

    assert is_src_path("src/evallab/devloop.py")
    assert not is_src_path("tests/test_devloop.py")

    assert is_full_suite_trigger("Makefile")
    assert is_full_suite_trigger("pyproject.toml")
    assert is_full_suite_trigger(".github/workflows/ci.yml")
    assert is_full_suite_trigger("tests/conftest.py")
    assert not is_full_suite_trigger("docs/NOW.md")


def test_plan_devloop_docs_lane(repo_root: Path) -> None:
    plan = plan_devloop(["docs/NOW.md"], repo_root)
    assert plan.lane == "docs_consumer"
    assert plan.modules == []
    assert plan.command == "uv run --no-sync pytest -q -n0 -p no:cacheprovider -m docs_consumer"
    assert "docs_consumer" in plan.reasons


def test_plan_devloop_full_suite(repo_root: Path) -> None:
    plan = plan_devloop(["Makefile"], repo_root)
    assert plan.lane == "full"
    assert plan.modules == ["__all__"]
    assert plan.command == "uv run --no-sync pytest -q -n0 -p no:cacheprovider"
    assert "__all__" in plan.reasons


def test_plan_devloop_focused(repo_root: Path) -> None:
    plan = plan_devloop(["tests/test_devloop.py"], repo_root)
    assert plan.lane == "focused"
    assert plan.modules == ["tests/test_devloop.py"]
    assert "tests/test_devloop.py" in plan.command
    assert "-q -n0 -p no:cacheprovider" in plan.command
    assert "tests/test_devloop.py" in plan.reasons


def test_run_devloop_json(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = run_devloop(repo_root, since="HEAD", as_json=True)
    assert ret == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "changed" in payload
    assert "modules" in payload
    assert "command" in payload
    assert isinstance(payload["changed"], list)
    assert isinstance(payload["modules"], list)
    assert isinstance(payload["command"], str)


def test_run_devloop_human_readable(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = run_devloop(repo_root, since="HEAD", as_json=False)
    assert ret == 0
    captured = capsys.readouterr()
    assert "devloop:" in captured.out
