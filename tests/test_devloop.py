"""Tests for the developer fast-loop command (evallab registry devloop)."""

from __future__ import annotations

import ast
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from evallab.devloop import (
    _referenced_modules,
    get_changed_files,
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
    [("missing-ref", "merge-base"), ("working", "diff"), ("working", "ls-files")],
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


@pytest.mark.parametrize(
    ("importer", "content"),
    [
        ("a.py", "import evallab.b\n"),
        ("a.py", "from evallab import b\n"),
        ("a.py", "from . import b\n"),
        ("a.py", "from .b import value\n"),
        ("nested/a.py", "from .. import b\n"),
        ("nested/a.py", "from ..b import value\n"),
        ("nested/__init__.py", "from .. import b\n"),
    ],
)
def test_transitive_import_consumer_is_selected(tmp_path, importer, content) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    source = tmp_path / "src/evallab"
    path = source / importer
    path.parent.mkdir(parents=True)
    path.write_text(content)
    (source / "b.py").write_text("value = 1\n")
    (tmp_path / "tests").mkdir()
    intermediate = src_path_to_module(f"src/evallab/{importer}")
    (tmp_path / "tests/test_consumer.py").write_text(f"import {intermediate}\n")

    plan = plan_devloop(["src/evallab/b.py"], tmp_path)

    assert plan.modules == ["tests/test_consumer.py"]
    assert plan.reasons == {
        "tests/test_consumer.py": [f"Imports evallab.b via {intermediate}"]
    }


def test_transitive_selection_handles_cycles_and_unparseable_sources(tmp_path) -> None:
    files = {
        "src/evallab/b.py": "import evallab.a\n",
        "src/evallab/a.py": "import evallab.b\n",
        "src/evallab/c.py": "from evallab.a import value\n",
        "src/evallab/unparseable.py": "import evallab.b\nnot valid Python !\n",
        "src/evallab/external.py": "import external.b\nfrom external import b\n",
        "src/evallab/invalid_relative.py": "from ... import b\n",
        "tests/test_chain.py": "import evallab.c\n",
        "tests/test_indirect.py": "from evallab import a, c\n",
        "tests/test_multiple.py": "from evallab import a, b, c\n",
        "tests/test_unparseable.py": "import evallab.unparseable\n",
        "tests/test_external.py": "import evallab.external\n",
        "tests/test_invalid_relative.py": "import evallab.invalid_relative\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    plan = plan_devloop(["src/evallab/b.py"], tmp_path)

    assert plan.modules == [
        "tests/test_chain.py",
        "tests/test_indirect.py",
        "tests/test_multiple.py",
    ]
    assert plan.reasons == {
        "tests/test_chain.py": ["Imports evallab.b via evallab.c"],
        "tests/test_indirect.py": ["Imports evallab.b via evallab.a"],
        "tests/test_multiple.py": ["Imports evallab.b (src/evallab/b.py)"],
    }


def test_source_inventory_is_once_per_plan_and_fresh(tmp_path, monkeypatch) -> None:
    source = tmp_path / "src/evallab"
    source.mkdir(parents=True)
    importer = source / "a.py"
    importer.write_text("import evallab.b\n")
    (source / "b.py").write_text("value = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_consumer.py").write_text("import evallab.a\n")
    read_text = Path.read_text
    reads: list[Path] = []

    def record_read(path, *args, **kwargs):
        reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", record_read)
    changed = ["src/evallab/b.py", "src/evallab/missing.py"]
    first = plan_devloop(changed, tmp_path)
    assert first.modules == ["tests/test_consumer.py"]
    assert reads.count(importer) == reads.count(source / "b.py") == 1
    assert reads.count(tmp_path / "tests/test_consumer.py") == 1

    importer.write_text("import external.b\n")
    second = plan_devloop(changed, tmp_path)
    assert second.modules == []
    assert reads.count(importer) == reads.count(source / "b.py") == 2


@pytest.mark.parametrize(
    ("content", "target", "expected"),
    [
        ("from evallab.a import b\n", "evallab.a.b", True),
        ('VALUE = "evallab.qax"\n', "evallab.qa", True),
        ('"""import evallab.fake"""\n', "evallab.fake", True),
        ('VALUE = "evallab.const"\n', "evallab.const", True),
        ('VALUE = "evallab.evallab.b"\n', "evallab.b", True),
        ('VALUE = "ev" "allab.joined"\n', "evallab.joined", True),
        ('VALUE = "ev\\x61llab.escaped"\n', "evallab.escaped", True),
        ('VALUE = "evallab.odd-name"\n', "evallab.odd-name", True),
        ('VALUES = ["evallab.", "separate"]\n', "evallab.separate", False),
    ],
)
def test_reference_matching_preserves_literal_and_ast_semantics(
    tmp_path, content, target, expected
) -> None:
    consumer = tmp_path / "test_consumer.py"
    consumer.write_text(content)

    assert _referenced_modules(consumer, {target}) == ({target} if expected else set())


def test_separate_string_constants_do_not_join_module_references(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_split.py").write_text('VALUES = ["evallab.", "alpha"]\n')
    (tmp_path / "tests/test_joined.py").write_text('VALUE = "evallab." "alpha"\n')

    plan = plan_devloop(["src/evallab/alpha.py"], tmp_path)

    assert plan.modules == ["tests/test_joined.py"]
    assert plan.reasons == {
        "tests/test_joined.py": ["Imports evallab.alpha (src/evallab/alpha.py)"]
    }


def test_import_cache_reuses_content_across_roots_and_resolves_relative_imports(
    tmp_path, monkeypatch
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setenv("EVALLAB_DEVLOOP_CACHE_DIR", str(cache))
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    for root, package in [(first_root, ""), (second_root, "nested/")]:
        source = root / "src/evallab" / package
        source.mkdir(parents=True)
        (source / "a.py").write_text("from . import b\n")
        (source / "b.py").write_text("value = 1\n")
        (root / "tests").mkdir()
        (root / "tests/test_consumer.py").write_text(
            "import evallab.a\nimport evallab.nested.a\n"
        )
    parse = ast.parse
    parsed_sources: list[str] = []
    parsed_consumers: list[str] = []

    def record_parse(content, filename="<unknown>", *args, **kwargs):
        if "/src/evallab/" in str(filename):
            parsed_sources.append(str(filename))
        elif "/tests/" in str(filename):
            parsed_consumers.append(str(filename))
        return parse(content, filename, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", record_parse)
    first = plan_devloop(["src/evallab/b.py"], first_root)
    assert first.modules == ["tests/test_consumer.py"]
    assert len(parsed_sources) == 2
    assert len(parsed_consumers) == 1
    assert (cache / "import-graph.json").is_file()

    second = plan_devloop(["src/evallab/nested/b.py"], second_root)
    assert second.reasons == {
        "tests/test_consumer.py": ["Imports evallab.nested.b via evallab.nested.a"]
    }
    assert len(parsed_sources) == 2
    assert len(parsed_consumers) == 1

    modified = second_root / "src/evallab/nested/a.py"
    modified.write_text("import external.b\n")
    third = plan_devloop(["src/evallab/nested/b.py"], second_root)
    assert third.modules == []
    assert parsed_sources[-1] == str(modified)
    assert len(parsed_sources) == 3
    assert len(parsed_consumers) == 1

    consumer = second_root / "tests/test_consumer.py"
    consumer.write_text("import external.b\n")
    fourth = plan_devloop(["src/evallab/nested/b.py"], second_root)
    assert fourth.modules == []
    assert parsed_consumers == [
        str(first_root / "tests/test_consumer.py"),
        str(consumer),
    ]


def test_oversized_consumer_constants_are_permanent_cache_misses(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setenv("EVALLAB_DEVLOOP_CACHE_DIR", str(cache))
    (tmp_path / "tests").mkdir()
    consumer = tmp_path / "tests/test_consumer.py"
    consumer.write_text("VALUE = " + repr("x" * (2 * 1024 * 1024) + "evallab.alpha") + "\n")
    parse = ast.parse
    parsed_consumers: list[str] = []

    def record_parse(content, filename="<unknown>", *args, **kwargs):
        parsed_consumers.append(str(filename))
        return parse(content, filename, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", record_parse)
    first = plan_devloop(["src/evallab/alpha.py"], tmp_path)
    second = plan_devloop(["src/evallab/alpha.py"], tmp_path)

    assert first.modules == second.modules == ["tests/test_consumer.py"]
    assert parsed_consumers == [str(consumer), str(consumer)]
    data = json.loads((cache / "import-graph.json").read_text())
    assert data["version"] == 2
    assert len(data["consumers"]) == 1
    assert next(iter(data["consumers"].values()))["constant_text"] is None


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
        "tests/test_transitive.py": [],
    }
    assert plan.changed == changed
    assert plan.lane == "focused"
    assert plan.modules == sorted(expected_references)
    assert set(plan.reasons) == set(expected_references)
    for consumer, references in expected_references.items():
        for module, source in references:
            assert any(module in reason and source in reason for reason in plan.reasons[consumer])
    assert plan.reasons["tests/test_transitive.py"] == [
        "Imports evallab.alpha via evallab.wrapper",
        "Imports evallab.beta via evallab.wrapper",
    ]
    assert plan.command == (
        "uv run --no-sync pytest -q -n0 -p no:cacheprovider "
        "dashboard/tests/beta_test.py tests/nested/test_alpha.py tests/support.py "
        "tests/test_shared.py tests/test_text.py tests/test_transitive.py"
    )


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


@pytest.mark.parametrize(
    "changed",
    [
        ["docs/removed-page.md"],
        ["fixtures/old.json", "fixtures/new.json"],
        ["research/evidence/sample.yaml"],
    ],
)
def test_checkpoint_selects_deleted_renamed_and_non_python_references(tmp_path, changed) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    for index, path in enumerate(changed):
        (tests / f"test_consumer_{index}.py").write_text(f"RESOURCE = {path!r}\n")
    (tests / "test_unrelated.py").write_text("def test_unrelated(): pass\n")

    plan = plan_devloop(changed, tmp_path, checkpoint=True)

    assert plan.modules == [f"tests/test_consumer_{i}.py" for i in range(len(changed))]
    assert plan.lane == "focused"
    assert all((tmp_path / module).is_file() for module in plan.modules)


def test_checkpoint_selects_asset_runtime_loader_consumers(tmp_path) -> None:
    source = tmp_path / "src/evallab"
    source.mkdir(parents=True)
    (source / "guide.py").write_text('GUIDE = "docs" / "guide.md"\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_guide.py").write_text("from evallab.guide import GUIDE\n")

    plan = plan_devloop(["docs/guide.md"], tmp_path, checkpoint=True)

    assert plan.modules == ["tests/test_guide.py"]


@pytest.mark.parametrize("changed", [[], ["src/evallab/unrelated.py"], ["Makefile"]])
def test_checkpoint_always_selects_repository_contracts(tmp_path, changed) -> None:
    contracts = {
        "tests/test_quality_gates.py",
        "tests/test_ci_coverage.py",
        "tests/test_governance.py",
        "tests/test_route_page_links.py",
        "tests/test_fixture_conformance.py",
        "tests/test_attach.py::TestFrontMatterParsing",
    }
    for selector in contracts:
        path = tmp_path / selector.partition("::")[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_contract(): pass\n")

    plan = plan_devloop(changed, tmp_path, checkpoint=True)

    assert set(plan.modules) == contracts
    assert "-n2" in plan.command
    assert "__all__" not in plan.modules


def test_checkpoint_keeps_renamed_test_and_consumers_of_deleted_name(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_new.py").write_text("def test_new(): pass\n")
    (tmp_path / "tests/test_manifest.py").write_text('FILES = ["tests/test_old.py"]\n')

    plan = plan_devloop(["tests/test_old.py", "tests/test_new.py"], tmp_path, checkpoint=True)

    assert plan.modules == ["tests/test_manifest.py", "tests/test_new.py"]
    assert "tests/test_old.py" not in shlex.split(plan.command)


def test_branch_inventory_includes_commits_both_rename_sides_and_local_edits(tmp_path) -> None:
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "old.json").write_text("{}\n")
    (tmp_path / "tracked.py").write_text("before = 1\n")
    git("add", "old.json", "tracked.py")
    git("commit", "-qm", "base")
    git("branch", "baseline")
    git("mv", "old.json", "new.json")
    git("commit", "-qm", "rename")
    (tmp_path / "tracked.py").write_text("after = 2\n")
    (tmp_path / "untracked file.json").write_text("{}\n")

    assert get_changed_files(tmp_path, since="baseline") == [
        "new.json",
        "old.json",
        "tracked.py",
        "untracked file.json",
    ]


def test_checkpoint_includes_individually_declared_test_file(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["research/test_single.py"]\n'
    )
    (tmp_path / "research").mkdir()
    (tmp_path / "research/test_single.py").write_text('ASSET = "fixtures/sample.json"\n')

    plan = plan_devloop(["fixtures/sample.json"], tmp_path, checkpoint=True)

    assert plan.modules == ["research/test_single.py"]


@pytest.mark.parametrize(
    "changed",
    [
        ["src/evallab/removed.py"],
        ["src/evallab/removed.py", "src/evallab/renamed.py"],
    ],
)
def test_checkpoint_preserves_consumers_of_removed_module_names(tmp_path, changed) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_consumer.py").write_text("from evallab.removed import load\n")

    plan = plan_devloop(changed, tmp_path, checkpoint=True)

    assert plan.modules == ["tests/test_consumer.py"]


def test_checkpoint_does_not_expand_prose_references_into_runtime_suites(tmp_path) -> None:
    source = tmp_path / "src/evallab"
    source.mkdir(parents=True)
    (source / "unrelated.py").write_text(
        '# See docs/topic.md.\nHELP = "Read docs/topic.md before invoking this command."\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_unrelated.py").write_text(
        "import evallab.unrelated\npytestmark = pytest.mark.docs_consumer\n"
    )

    plan = plan_devloop(["docs/topic.md"], tmp_path, checkpoint=True)

    assert plan.modules == []
