"""Tests for the developer fast-loop command (evallab registry devloop)."""

from __future__ import annotations

import json
import subprocess
import sys
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


def test_run_devloop_json_clean_inventory(tmp_path, monkeypatch, capsys) -> None:
    def clean_inventory(command, **kwargs):
        assert command[0] == "git"
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("evallab.devloop.subprocess.run", clean_inventory)
    assert run_devloop(tmp_path, as_json=True, run=True) == 0
    assert json.loads(capsys.readouterr().out) == {
        "changed": [],
        "modules": [],
        "command": "",
        "lane": "none",
        "reasons": {},
    }


@pytest.mark.parametrize("exit_code", [0, 5])
@pytest.mark.parametrize("as_json", [False, True])
def test_run_devloop_preserves_child_output_and_status(
    tmp_path, monkeypatch, capfd, exit_code, as_json
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_changed.py").write_text("def test_changed(): pass\n")
    monkeypatch.setattr(
        "evallab.devloop.get_changed_files",
        lambda root, *, since: ["tests/test_changed.py"],
    )
    child_run = subprocess.run

    def execute(command, **kwargs):
        assert command == [
            "uv",
            "run",
            "--no-sync",
            "pytest",
            "-q",
            "-n0",
            "-p",
            "no:cacheprovider",
            "tests/test_changed.py",
        ]
        assert kwargs["cwd"] == tmp_path
        return child_run(
            [
                sys.executable,
                "-c",
                "import sys; print('child stdout'); "
                "print('child stderr', file=sys.stderr); "
                f"sys.exit({exit_code})",
            ],
            **kwargs,
        )

    monkeypatch.setattr("evallab.devloop.subprocess.run", execute)
    assert run_devloop(tmp_path, as_json=as_json, run=True) == exit_code
    output = capfd.readouterr()
    assert "child stderr" in output.err
    if as_json:
        assert json.loads(output.out) == {
            "changed": ["tests/test_changed.py"],
            "modules": ["tests/test_changed.py"],
            "command": ("uv run --no-sync pytest -q -n0 -p no:cacheprovider tests/test_changed.py"),
            "lane": "focused",
            "reasons": {"tests/test_changed.py": ["Direct test file modification"]},
        }
        assert "child stdout" in output.err
    else:
        assert "1 affected test module(s)" in output.out
        assert "Direct test file modification" in output.out
        assert "Executing command..." in output.out
        assert "child stdout" in output.out
        assert "child stdout" not in output.err


@pytest.mark.parametrize(
    ("since", "failed_command"),
    [("missing-ref", "diff"), ("working", "diff"), ("working", "ls-files")],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_git_inventory_failure_never_becomes_successful_empty_plan(
    tmp_path, monkeypatch, capsys, since, failed_command, as_json
) -> None:
    def inventory(command, **kwargs):
        assert command[0] == "git", "pytest must not run after an inventory failure"
        if command[1] == failed_command:
            result = subprocess.CompletedProcess(
                command, 128, stdout="", stderr="inventory unavailable"
            )
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(
                    result.returncode, command, output=result.stdout, stderr=result.stderr
                )
            return result
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("evallab.devloop.subprocess.run", inventory)
    assert run_devloop(tmp_path, since=since, as_json=as_json, run=True) != 0
    output = capsys.readouterr()
    assert output.out == ""
    assert "inventory unavailable" in output.err


def test_all_declared_test_roots_are_selected_and_explained(tmp_path) -> None:
    roots = [
        "tests",
        "dashboard/tests",
        "research/analysis/tests",
        "research/calibration/tests",
        "research/experiments/tests",
    ]
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = " + json.dumps(roots) + "\n"
    )
    expected = []
    for index, test_root in enumerate(roots):
        directory = tmp_path / test_root
        directory.mkdir(parents=True)
        name = "test_consumer.py" if index % 2 == 0 else "consumer_test.py"
        consumer = directory / name
        consumer.write_text("from evallab.cohort import compare\n")
        (directory / "test_unrelated.py").write_text("from evallab.truth import compare\n")
        expected.append(consumer.relative_to(tmp_path).as_posix())

    plan = plan_devloop(["src/evallab/cohort.py"], tmp_path)
    assert plan.modules == sorted(expected)
    assert plan.reasons == {
        consumer: ["Imports evallab.cohort (src/evallab/cohort.py)"] for consumer in expected
    }
    for consumer in expected:
        assert affected_test_modules([consumer], tmp_path) == [consumer]


def test_overlapping_consumers_preserve_direct_selection_and_reasons(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests", "tests/nested", "dashboard/tests"]\n'
    )
    files = {
        "tests/test_shared.py": "from evallab import alpha, beta\n",
        "tests/nested/test_alpha.py": "import evallab.alpha.child\n",
        "dashboard/tests/beta_test.py": 'TARGET = "evallab." "beta"\n',
        "tests/support.py": "from evallab import alpha\n",
        # Raw references select consumers even if their Python cannot be parsed.
        "tests/test_text.py": "# evallab.alphabet\nnot valid Python !\n",
        "tests/test_transitive.py": "from evallab import wrapper\n",
        "src/evallab/wrapper.py": "from evallab import alpha, beta\n",
        "outside/test_ignored.py": "from evallab import alpha, beta\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    changed = [
        "src/evallab/alpha.py",
        "tests/support.py",
        "src/evallab/beta.py",
        "tests/test_shared.py",
    ]
    plan = plan_devloop(changed, tmp_path)
    alpha_reference = ("evallab.alpha", "src/evallab/alpha.py")
    beta_reference = ("evallab.beta", "src/evallab/beta.py")
    expected_references = {
        "dashboard/tests/beta_test.py": [beta_reference],
        "tests/nested/test_alpha.py": [alpha_reference],
        "tests/support.py": [alpha_reference],
        "tests/test_shared.py": [alpha_reference, beta_reference],
        "tests/test_text.py": [alpha_reference],
    }
    assert plan.changed == changed
    assert plan.lane == "focused"
    assert plan.modules == sorted(expected_references)
    assert set(plan.reasons) == set(expected_references)
    for consumer, references in expected_references.items():
        for module, source in references:
            assert any(module in reason and source in reason for reason in plan.reasons[consumer])
    assert plan.command == (
        "uv run --no-sync pytest -q -n0 -p no:cacheprovider "
        "dashboard/tests/beta_test.py tests/nested/test_alpha.py tests/support.py "
        "tests/test_shared.py tests/test_text.py"
    )
    assert affected_test_modules(changed, tmp_path) == plan.modules


def test_consumer_inventory_is_fresh_for_each_plan(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    consumer = tmp_path / "tests/test_consumer.py"
    changed = ["src/evallab/alpha.py", "src/evallab/beta.py"]
    consumer.write_text("from evallab import alpha\n")
    first = plan_devloop(changed, tmp_path)
    consumer.write_text("from evallab import beta\n")
    second = plan_devloop(changed, tmp_path)
    assert first.modules == second.modules == ["tests/test_consumer.py"]
    assert set(first.reasons) == set(second.reasons) == {"tests/test_consumer.py"}
    assert any(
        "evallab.alpha" in reason and "src/evallab/alpha.py" in reason
        for reason in first.reasons["tests/test_consumer.py"]
    )
    assert all("evallab.beta" not in reason for reason in first.reasons["tests/test_consumer.py"])
    assert any(
        "evallab.beta" in reason and "src/evallab/beta.py" in reason
        for reason in second.reasons["tests/test_consumer.py"]
    )
    assert all("evallab.alpha" not in reason for reason in second.reasons["tests/test_consumer.py"])


def test_deleted_or_renamed_test_uses_remaining_suite(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_renamed.py").write_text("def test_remaining(): assert True\n")
    plan = plan_devloop(["tests/test_removed.py", "tests/test_renamed.py"], tmp_path)
    assert plan.lane == "full"
    assert plan.modules == ["__all__"]
    assert "tests/test_removed.py" not in plan.command
