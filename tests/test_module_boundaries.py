"""Import-graph guards for named safety boundaries.

Each test protects a boundary that no runtime check enforces: which module may
launch Harbor, which modules must stay ignorant of the LLM verifier, and which
modules must not touch credentials, the network, or the policy queue.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src/evallab"


def _module(relative: str) -> ast.Module:
    return ast.parse((SRC / relative).read_text())


def _imported_modules(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.add(node.module.split(".")[0])
    return names


def _imported_names(tree: ast.Module) -> set[str]:
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


def _code_string_literals(tree: ast.Module) -> list[str]:
    """String constants excluding docstrings, so prose cannot fail the check."""
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node not in docstrings
    ]


def test_only_the_queue_executor_imports_the_harbor_runner() -> None:
    launchers = sorted(
        path.name
        for path in SRC.glob("*.py")
        if path.name != "runner.py"
        and "run_experiment" in _imported_names(ast.parse(path.read_text()))
    )
    assert launchers == ["queue.py"]


def test_task_workbench_and_registry_never_import_the_llm_verifier() -> None:
    for relative in ("task_workbench.py", "registry.py"):
        imported = _imported_modules(_module(relative))
        assert not {"evallab.llm_verifier", "evallab.calibrate"} & imported, relative


def test_task_workbench_has_no_queue_registry_write_or_gh_capability() -> None:
    tree = _module("task_workbench.py")
    assert "evallab.queue" not in _imported_modules(tree)
    assert "TaskRegistryRecord" not in _imported_names(tree)
    assert not any(literal.startswith("gh ") for literal in _code_string_literals(tree))


def test_quota_accounting_is_a_pure_read_of_injected_job_directories() -> None:
    tree = _module("quota.py")
    imported = _imported_modules(tree)
    assert imported.isdisjoint(
        {"urllib", "http", "socket", "requests", "httpx", "subprocess", "os", "shutil"}
    ), sorted(imported)
    assert "evallab.queue" not in imported
    assert "evallab.cli" not in imported


def test_quota_accounting_names_no_credential_store() -> None:
    literals = " ".join(_code_string_literals(_module("quota.py")))
    for forbidden in (
        "auth.json",
        "find-generic-password",
        "ANTHROPIC",
        "OPENAI",
        ".codex",
        "Keychain",
    ):
        assert forbidden not in literals, forbidden
