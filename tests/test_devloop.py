"""Tests for the developer fast-loop command (evallab registry devloop)."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from evallab.devloop import (
    _referenced_modules,
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
        consumer: ["References evallab.cohort (src/evallab/cohort.py)"] for consumer in expected
    }
    for consumer in expected:
        assert affected_test_modules([consumer], tmp_path) == [consumer]


def test_changed_module_selects_consumers_referencing_it(tmp_path) -> None:
    """A changed src module selects tests that reference it directly."""
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    source = tmp_path / "src/evallab"
    source.mkdir(parents=True)
    (source / "a.py").write_text("import evallab.b\n")
    (source / "b.py").write_text("value = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_consumer.py").write_text("import evallab.a\n")
    (tmp_path / "tests/test_direct.py").write_text("import evallab.b\n")

    plan = plan_devloop(["src/evallab/b.py"], tmp_path)

    # Only the test that references the changed module itself is selected;
    # importers of importers are out of scope for the focused lane.
    assert plan.modules == ["tests/test_direct.py"]
    assert plan.reasons == {"tests/test_direct.py": ["References evallab.b (src/evallab/b.py)"]}


def test_reference_matching_handles_unparseable_sources(tmp_path) -> None:
    files = {
        "src/evallab/b.py": "value = 1\n",
        "tests/test_direct.py": "import evallab.b\n",
        "tests/test_chain.py": "import evallab.c\n",
        "tests/test_unparseable.py": "# evallab.b\nnot valid Python !\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    plan = plan_devloop(["src/evallab/b.py"], tmp_path)

    assert plan.modules == ["tests/test_direct.py", "tests/test_unparseable.py"]
    assert plan.reasons == {
        "tests/test_direct.py": ["References evallab.b (src/evallab/b.py)"],
        "tests/test_unparseable.py": ["References evallab.b (src/evallab/b.py)"],
    }


def test_source_inventory_is_once_per_plan_and_fresh(tmp_path, monkeypatch) -> None:
    (tmp_path / "tests").mkdir()
    consumer = tmp_path / "tests/test_consumer.py"
    consumer.write_text("import evallab.b\n")
    read_text = Path.read_text
    reads: list[Path] = []

    def record_read(path, *args, **kwargs):
        reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", record_read)
    changed = ["src/evallab/b.py", "src/evallab/missing.py"]
    first = plan_devloop(changed, tmp_path)
    assert first.modules == ["tests/test_consumer.py"]
    assert reads.count(consumer) == 1

    consumer.write_text("import external.b\n")
    second = plan_devloop(changed, tmp_path)
    assert second.modules == []
    assert reads.count(consumer) == 2


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
        "tests/test_joined.py": ["References evallab.alpha (src/evallab/alpha.py)"]
    }


def test_consumer_feature_cache_reuses_content_across_roots(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setenv("EVALLAB_DEVLOOP_CACHE_DIR", str(cache))
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    for root in (first_root, second_root):
        (root / "src/evallab").mkdir(parents=True)
        (root / "src/evallab/b.py").write_text("value = 1\n")
        (root / "tests").mkdir()
        (root / "tests/test_consumer.py").write_text("import evallab.b\n")
    parse = ast.parse
    parsed_consumers: list[str] = []

    def record_parse(content, filename="<unknown>", *args, **kwargs):
        if "/tests/" in str(filename):
            parsed_consumers.append(str(filename))
        return parse(content, filename, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", record_parse)
    first = plan_devloop(["src/evallab/b.py"], first_root)
    assert first.modules == ["tests/test_consumer.py"]
    assert len(parsed_consumers) == 1
    assert (cache / "consumer-features.json").is_file()

    # Identical consumer content in a second root is served from the cache.
    second = plan_devloop(["src/evallab/b.py"], second_root)
    assert second.modules == ["tests/test_consumer.py"]
    assert len(parsed_consumers) == 1

    consumer = second_root / "tests/test_consumer.py"
    consumer.write_text("import external.b\n")
    third = plan_devloop(["src/evallab/b.py"], second_root)
    assert third.modules == []
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
    data = json.loads((cache / "consumer-features.json").read_text())
    assert data["version"] == 3
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
        "tests/test_wrapper.py": "from evallab import wrapper\n",
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
    # Transitive-only consumers (via src wrappers) stay out of the focused lane.
    assert "tests/test_wrapper.py" not in plan.modules
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


def _write_test(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_research_experiment_assets_select_declared_and_mentioning_tests(tmp_path) -> None:
    """Experiment JSON/markdown select the experiment's tests and path mentions."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["research/experiments/exp-a", "tests"]\n'
    )
    _write_test(
        tmp_path,
        "research/experiments/exp-a/test_exp_a.py",
        "def test_exp_a(): assert True\n",
    )
    _write_test(
        tmp_path,
        "tests/test_ladder.py",
        'DATA = "research/experiments/exp-a/cells.json"\ndef test_ladder(): assert True\n',
    )
    _write_test(tmp_path, "tests/test_other.py", "def test_other(): assert True\n")
    changed = [
        "research/experiments/exp-a/README.md",
        "research/experiments/exp-a/cells.json",
    ]
    plan = plan_devloop(changed, tmp_path)
    assert plan.lane == "focused"
    assert "research/experiments/exp-a/test_exp_a.py" in plan.modules
    assert "tests/test_ladder.py" in plan.modules
    assert "tests/test_other.py" not in plan.modules


def test_scripts_ops_python_selects_mentioning_tests(tmp_path) -> None:
    """An ops script change selects tests that mention the script path."""
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    _write_test(
        tmp_path,
        "tests/test_installer.py",
        'SCRIPT = "scripts/ops/install.sh"\ndef test_installer(): assert True\n',
    )
    _write_test(tmp_path, "tests/test_other.py", "def test_other(): assert True\n")
    (tmp_path / "scripts/ops").mkdir(parents=True)
    (tmp_path / "scripts/ops/tool.py").write_text("print('ok')\n")
    plan = plan_devloop(["scripts/ops/tool.py"], tmp_path)
    # The parent directory mention is a valid selection needle too.
    assert plan.lane == "focused"
    assert "tests/test_installer.py" in plan.modules
    assert "tests/test_other.py" not in plan.modules


def test_asset_without_mentioning_test_keeps_full_suite(tmp_path) -> None:
    """An asset no test mentions still requires the full suite (recall guard)."""
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    _write_test(tmp_path, "tests/test_other.py", "def test_other(): assert True\n")
    plan = plan_devloop(["scripts/ops/orphan.py"], tmp_path)
    assert plan.lane == "full"
    assert plan.modules == ["__all__"]


def test_nested_conftest_change_uses_full_suite(tmp_path) -> None:
    """Any conftest.py rewrites collection for its subtree, not only tests/."""
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    _write_test(tmp_path, "tests/test_other.py", "def test_other(): assert True\n")
    assert is_full_suite_trigger("tests/sub/conftest.py")
    assert is_full_suite_trigger("research/experiments/exp-a/conftest.py")
    plan = plan_devloop(["research/experiments/exp-a/conftest.py"], tmp_path)
    assert plan.lane == "full"
    assert plan.modules == ["__all__"]
