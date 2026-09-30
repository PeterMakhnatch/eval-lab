"""Task health census behavior: labeling, static detection, summary (HAR-108)."""

from __future__ import annotations

import json
from pathlib import Path

from evallab.hidden_patch import project_modules
from evallab.task_health import (
    blocklist_facts,
    build_health_rows,
    hosts_bypassable,
    label_task,
    leak_assessment,
    nop_evidence,
    project_key_for,
    read_task_health_parquet,
    static_checks,
    summarize_health,
    write_task_health_parquet,
)

_PATCH = """\
diff --git a/pkg/tests/test_widget.py b/pkg/tests/test_widget.py
new file mode 100644
--- /dev/null
+++ b/pkg/tests/test_widget.py
@@ -0,0 +1,4 @@
+from pkg.widget import Widget
+
+def test_widget():
+    assert Widget().value == 1
diff --git a/mimo_test_command.sh b/mimo_test_command.sh
new file mode 100755
--- /dev/null
+++ b/mimo_test_command.sh
@@ -0,0 +1,2 @@
+#!/bin/bash
+python -m pytest pkg/tests/test_widget.py -v
"""

_INSTRUCTION = "Add a Widget with a value attribute.\n"


def _task(root: Path, *, patch: str = _PATCH, instruction: str = _INSTRUCTION) -> Path:
    task = root / "task"
    (task / "tests").mkdir(parents=True)
    (task / "tests" / "test.patch").write_text(patch)
    (task / "instruction.md").write_text(instruction)
    return task


def _trial(
    root: Path,
    stdout: str,
    *,
    reward: float | None = 0.0,
    exception_type: str | None = None,
    reward_file: bool = True,
) -> Path:
    trial = root / "job" / "job__trial"
    (trial / "verifier").mkdir(parents=True)
    (trial / "verifier" / "test-stdout.txt").write_text(stdout)
    if reward_file and reward is not None:
        (trial / "verifier" / "reward.txt").write_text(str(reward))
    result: dict = {"verifier_result": {"rewards": {"reward": reward}}}
    if exception_type is not None:
        result["exception_info"] = {"exception_type": exception_type}
    (trial / "result.json").write_text(json.dumps(result))
    return trial


def _label(tmp_path: Path, stdout: str, **trial_kwargs: object) -> tuple[str, list[str], str]:
    task = _task(tmp_path)
    trial = _trial(tmp_path, stdout, **trial_kwargs)  # type: ignore[arg-type]
    return label_task(static_checks(task), nop_evidence(trial, _INSTRUCTION))


def test_setup_error_beats_a_passing_reward(tmp_path: Path) -> None:
    """An environment that cannot import beats whatever reward was written."""
    label, reasons, evidence = _label(
        tmp_path,
        "E   ModuleNotFoundError: No module named 'pyproj._context'\n1 passed\n",
        reward=1.0,
    )
    assert label == "broken_environment"
    assert reasons[0] == "setup_error"
    assert "pyproj._context" in evidence


def test_import_of_an_instruction_named_symbol_is_sound(tmp_path: Path) -> None:
    """A collection error naming the feature the instruction asks for is the agent's work."""
    task = _task(tmp_path, instruction="Provide a rename verb.\n")
    stdout = (
        "_______ ERROR collecting pkg/tests/test_widget.py ________\n"
        "E   ImportError: cannot import name 'rename' from 'pkg'\n"
        "Interrupted: 1 error during collection\n"
    )
    trial = _trial(tmp_path, stdout, reward=0.0)
    label, reasons, evidence = label_task(
        static_checks(task), nop_evidence(trial, "Provide a rename verb.\n")
    )
    assert label == "sound"
    assert reasons == []
    assert "excused" in evidence


def test_nop_reward_one_is_grader_suspect(tmp_path: Path) -> None:
    label, reasons, evidence = _label(tmp_path, "collected 2 items\n2 passed\n", reward=1.0)
    assert label == "grader_suspect"
    assert reasons == ["nop_passes"]
    assert "reward 1" in evidence


def test_patch_not_applied_is_broken(tmp_path: Path) -> None:
    label, reasons, evidence = _label(
        tmp_path,
        "the hidden tests could not be applied (testbed problem, not scored)\n",
        reward=None,
        reward_file=False,
    )
    assert label == "broken_environment"
    assert "tests_not_applied" in reasons
    assert "could not be applied" in evidence


def test_no_nop_is_unknown() -> None:
    label, reasons, evidence = label_task({"literal_source_asserts": []}, None)
    assert label == "unknown"
    assert reasons == ["no_nop"]
    assert evidence


def test_missing_project_code_is_excused_but_build_artifacts_are_not(tmp_path: Path) -> None:
    """A name or submodule missing from the project the hidden tests import is
    the agent's work; a missing version file, compiled extension, or name
    missing from an installed package is the environment."""
    trial = _trial(tmp_path, "")

    def excused(stdout: str) -> bool:
        (trial / "verifier" / "test-stdout.txt").write_text(stdout)
        return nop_evidence(trial, "Add the HyAB colour metric.\n", {"colour"})[
            "setup_error_excused"
        ]

    collecting = "ERROR collecting colour/difference/tests/test_delta_e.py\n"
    assert excused(
        collecting + "E   ImportError: cannot import name 'delta_E_HyAB' from "
        "'colour.difference' (/testbed/colour/difference/__init__.py)\n"
    )
    assert excused(collecting + "E   AttributeError: module 'colour' has no attribute 'hyab'\n")
    assert not excused(collecting + "E   ModuleNotFoundError: No module named 'colour._version'\n")
    assert not excused(collecting + "E   ModuleNotFoundError: No module named 'colour.version'\n")
    assert not excused(
        collecting + "E   ImportError: cannot import name 'x' from 'colour.difference' "
        "(/usr/lib/python3/dist-packages/colour/difference/__init__.py)\n"
    )
    assert not excused(collecting + "E   ModuleNotFoundError: No module named 'scipy.spatial'\n")


def test_project_modules_include_the_test_files_package(tmp_path: Path) -> None:
    """000700: the hidden test adds a name inside an existing parenthesised
    import, so only the test file's own directory names the project."""
    patch = (
        "diff --git a/colour/difference/tests/test_delta_e.py b/colour/difference/tests/test_delta_e.py\n"
        "--- a/colour/difference/tests/test_delta_e.py\n"
        "+++ b/colour/difference/tests/test_delta_e.py\n"
        "@@ -1,3 +1,4 @@\n"
        " from colour.difference import (\n"
        "+    delta_E_HyAB,\n"
        " )\n"
        "diff --git a/src/pkg/tests/test_y.py b/src/pkg/tests/test_y.py\n"
        "--- /dev/null\n"
        "+++ b/src/pkg/tests/test_y.py\n"
        "@@ -0,0 +1 @@\n"
        "+import numpy\n"
        "diff --git a/tests/test_z.py b/tests/test_z.py\n"
        "--- /dev/null\n"
        "+++ b/tests/test_z.py\n"
        "@@ -0,0 +1 @@\n"
        "+import json\n"
    )
    assert project_modules(_task(tmp_path, patch=patch)) == {"colour", "pkg"}


_DISTUTILS_NOISE = (
    "Error processing line 1 of /usr/lib/python3/dist-packages/distutils-precedence.pth:\n"
    "  Traceback (most recent call last):\n"
    "  ModuleNotFoundError: No module named '_distutils_hack'\n"
)


def test_colored_counts_and_startup_noise_still_grade_sound(tmp_path: Path) -> None:
    """Census false positives: colour codes hid pytest's counts (tests_did_not_run)
    and Debian's .pth start-up traceback matched ModuleNotFoundError."""
    stdout = _DISTUTILS_NOISE + "FAILED pkg/tests/test_widget.py::test_widget\n"
    stdout += "\x1b[31m\x1b[1m1 failed\x1b[0m\x1b[31m in 0.04s\x1b[0m\ntest command exited 1\n"
    label, reasons, _evidence = _label(tmp_path, stdout)
    assert (label, reasons) == ("sound", [])


def test_counts_win_over_a_collection_error(tmp_path: Path) -> None:
    """One module failing to import while the rest run is not 'did not run';
    the collection error itself is still judged as a setup error."""
    task = _task(tmp_path, instruction="Add a Widget.\n")
    stdout = "ERROR collecting pkg/tests/test_widget.py\nE   ImportError: cannot import name 'Widget' from 'pkg.widget'\n3 passed, 1 error\n"
    label, _reasons, _evidence = label_task(
        static_checks(task), nop_evidence(_trial(tmp_path, stdout), "Add a Widget.\n")
    )
    assert label == "sound"


def test_custom_runner_output_counts_as_ran_but_silence_does_not(tmp_path: Path) -> None:
    patch = _one_file_patch("def test_x():\n    pass", command="bash run_checks.sh")
    static = static_checks(_task(tmp_path, patch=patch))
    assert static["test_runner"] == "other"
    spoke = nop_evidence(
        _trial(tmp_path / "a", "FAIL: F1 missing --open help\ntest command exited 1\n"), ""
    )
    assert label_task(static, spoke)[0] == "sound"
    silent = nop_evidence(_trial(tmp_path / "b", "test command exited 1\n"), "")
    label, reasons, evidence = label_task(static, silent)
    assert (label, reasons, evidence) == ("grader_suspect", ["tests_did_not_run"], "no test output")


def test_new_environment_shapes_are_setup_errors(tmp_path: Path) -> None:
    cases = {
        "/usr/bin/python: No module named pytest\n": "No module named pytest",
        "pkg_resources.DistributionNotFound: The 'chainer' distribution was not found\n": "DistributionNotFound",
        "File config.vars not found: you must run ./configure before running make.\n": "configure",
        "ImportError: cannot import name 'dict2schema' from 'webargs' "
        "(/root/.venv/lib/python3.11/site-packages/webargs/__init__.py)\n": "dict2schema",
    }
    for index, (stdout, needle) in enumerate(cases.items()):
        label, reasons, evidence = _label(tmp_path / str(index), stdout)
        assert (label, reasons) == ("broken_environment", ["setup_error"]), stdout
        assert needle in evidence
    # Logged by a plugin loader while the tests still ran (001273): not setup.
    logged = "pkg_resources.DistributionNotFound: The 'uWSGI' distribution was not found\n"
    label, _reasons, _evidence = _label(tmp_path / "logged", logged + "1 failed, 3 passed\n")
    assert label == "sound"


def test_helper_key_error_for_the_named_function_is_excused(tmp_path: Path) -> None:
    """001601: ivy's test helper looks the new frontend function up by name."""
    stdout = (
        "ERROR collecting ivy_tests/test_general_functions.py\n"
        "ivy_tests/helpers/testing_helpers.py:179: in _import_fn\n"
        "E   KeyError: 'sequence_mask'\n"
    )
    trial = _trial(tmp_path, stdout)
    assert nop_evidence(trial, "Add `tf.sequence_mask` to the frontend.\n")["setup_error_excused"]
    assert not nop_evidence(trial, "Add a boolean mask helper.\n")["setup_error_excused"]


def _one_file_patch(
    body: str, *, command: str = "python -m pytest pkg/tests/test_widget.py -v"
) -> str:
    added = "".join(f"+{line}\n" for line in body.splitlines())
    command_added = "".join(f"+{line}\n" for line in command.splitlines())
    return (
        "diff --git a/pkg/tests/test_widget.py b/pkg/tests/test_widget.py\n"
        "new file mode 100644\n--- /dev/null\n+++ b/pkg/tests/test_widget.py\n"
        f"@@ -0,0 +1,{body.count(chr(10)) + 1} @@\n{added}"
        "diff --git a/mimo_test_command.sh b/mimo_test_command.sh\n"
        "new file mode 100755\n--- /dev/null\n+++ b/mimo_test_command.sh\n"
        f"@@ -0,0 +1,{command.count(chr(10)) + 1} @@\n{command_added}"
    )


def test_literal_source_assert_is_only_the_project_source_shape(tmp_path: Path) -> None:
    """1634: read project source and assert a substring. Generated text is not that."""
    task = _task(
        tmp_path,
        patch=_one_file_patch(
            "peewee_path = '/app/vendor/peewee/peewee.py'\n"
            "peewee_source = open(peewee_path).read()\n"
            "assert 'def atomic(self, transaction_type=None, **kwargs):' in peewee_source\n"
            "text = Path('pkg/widget.py').read_text()\n"
            "assert 'def render' in text\n"
            "src = inspect.getsource(Widget.atomic)\n"
            "assert 'def atomic' in src\n"
        ),
    )
    found = static_checks(task)["literal_source_asserts"]
    assert len(found) == 3
    assert any("def atomic(self, transaction_type=None, **kwargs):" in line for line in found)
    assert any("def render" in line for line in found)
    assert any("inspect.getsource" in line for line in found)

    generated = static_checks(
        _task(
            tmp_path / "generated",
            patch=_one_file_patch(
                "generated_output = render_models()\nassert 'class X' in generated_output\n"
            ),
        )
    )
    assert generated["literal_source_asserts"] == []

    written = static_checks(
        _task(
            tmp_path / "written",
            patch=_one_file_patch(
                "Path('pkg/widget.py').write_text('def render(): pass')\n"
                "source = Path('pkg/widget.py').read_text()\n"
                "assert 'def render' in source\n"
            ),
        )
    )
    assert written["literal_source_asserts"] == []

    generated_file = static_checks(
        _task(
            tmp_path / "joined",
            patch=_one_file_patch(
                "out = Path(d) / 'out.py'\noutput = out.read_text()\nassert 'class X' in output\n"
            ),
        )
    )
    assert generated_file["literal_source_asserts"] == []


def test_empty_new_file_parses_and_runners_name_bundled_and_django(tmp_path: Path) -> None:
    empty = _task(
        tmp_path,
        patch=(
            "diff --git a/tests/__init__.py b/tests/__init__.py\n"
            "new file mode 100644\n"
            "index 00000000..e69de29b\n"
            "diff --git a/pkg/tests/test_widget.py b/pkg/tests/test_widget.py\n"
            "new file mode 100644\n--- /dev/null\n+++ b/pkg/tests/test_widget.py\n"
            "@@ -0,0 +1,1 @@\n+assert True\n"
            "diff --git a/mimo_test_command.sh b/mimo_test_command.sh\n"
            "new file mode 100755\n--- /dev/null\n+++ b/mimo_test_command.sh\n"
            "@@ -0,0 +1,2 @@\n"
            "+base64 -d mimo_build_env.tar.gz.b64 | tar -xzf - -C /testbed\n"
            "+exec bash /testbed/.build_env/test_command.sh\n"
        ),
    )
    assert static_checks(empty)["patch_well_formed"] is True
    assert static_checks(empty)["test_runner"] == "bundled"

    django = _task(
        tmp_path / "django",
        patch=_one_file_patch(
            "assert True\n", command="python tests/manage.py test test_ajax_contract"
        ),
    )
    assert static_checks(django)["test_runner"] == "django"

    garbage = _task(
        tmp_path / "garbage",
        patch=("diff --git a/pkg/widget.py b/pkg/widget.py\nthis is not a diff\n"),
    )
    assert static_checks(garbage)["patch_well_formed"] is False


def test_project_key_falls_back_from_imports_then_path(tmp_path: Path) -> None:
    imported = static_checks(
        _task(
            tmp_path,
            patch=_one_file_patch("import os\nfrom siuba.verbs import rename\nimport pytest\n"),
        )
    )
    assert project_key_for(
        "code:format-code-task-000001",
        "format-code-task-000001",
        imported["imported_modules"],
        imported["patch_files"],
    ) == ("siuba", "test_import")

    bare = static_checks(_task(tmp_path / "bare", patch=_one_file_patch("assert True\n")))
    assert project_key_for(
        "code:format-code-task-000009",
        "format-code-task-000009",
        bare["imported_modules"],
        bare["patch_files"],
    ) == ("pkg", "test_path")


def test_summary_counts_come_only_from_the_rows(tmp_path: Path) -> None:
    rows = [
        {
            "label": "sound",
            "split": "train",
            "project_key": "repo-a",
            "reasons": [],
            "nop_cost_usd": 0.25,
        },
        {
            "label": "sound",
            "split": "heldout",
            "project_key": "repo-a",
            "reasons": [],
            "nop_cost_usd": 0.25,
        },
        {
            "label": "broken_environment",
            "split": "train",
            "project_key": "repo-b",
            "reasons": ["setup_error"],
            "nop_cost_usd": 0.5,
        },
        {
            "label": "broken_environment",
            "split": "train",
            "project_key": "repo-b",
            "reasons": ["setup_error"],
            "nop_cost_usd": 0.0,
        },
        {
            "label": "unknown",
            "split": "train",
            "project_key": "repo-c",
            "reasons": ["no_nop"],
            "nop_cost_usd": None,
        },
    ]
    summary = summarize_health(rows, table_path=tmp_path / "task_health.parquet")
    assert f"table: {tmp_path / 'task_health.parquet'}" in summary
    assert "rows: 5" in summary
    assert "| sound | 2 |" in summary
    assert "| broken_environment | 2 |" in summary
    assert "| unknown | 1 |" in summary
    assert "| grader_suspect | 0 |" in summary
    assert "| train | 1 | 2 | 0 | 1 |" in summary
    assert "| heldout | 1 | 0 | 0 | 0 |" in summary
    assert "| repo-a | 2 | 2 | 0 | 0 | 0 |" in summary
    assert "| repo-b | 2 | 0 | 2 | 0 | 0 |" in summary
    assert "singleton projects: 1" in summary
    assert "projects with >=2 broken tasks: 1" in summary
    assert "| repo-b | 2 |" in summary
    assert "| setup_error | 2 |" in summary
    assert "$1.0000" in summary

    out = write_task_health_parquet(rows, tmp_path / "task_health.parquet")
    reread = summarize_health(read_task_health_parquet(out), table_path=out)
    assert reread == summary


def test_build_uses_the_latest_nop_and_labels_the_rest_unknown(tmp_path: Path) -> None:
    task = _task(tmp_path)
    trial = _trial(tmp_path, "collected 1 item\n1 failed\n", reward=0.0)
    pool = [
        {
            "task_id": "t-1",
            "task_version_digest": "sha256:abc",
            "split": "train",
            "split_group": "code:pkg",
            "category": "Python",
            "task": "task",
            "image_mib": 12,
        },
        {
            "task_id": "t-2",
            "task_version_digest": "sha256:def",
            "split": "heldout",
            "split_group": "code:format-code-task-000002",
            "category": "Python",
            "task": "missing",
        },
    ]
    qualification = [
        {
            "task_version_digest": "sha256:abc",
            "agent_name": "nop",
            "job_name": "job",
            "trial_name": "job__trial",
            "finished_at": "2026-09-30T00:00:00Z",
            "est_cost_usd": 0.1,
            "reward": 1.0,
        },
        {
            "task_version_digest": "sha256:abc",
            "agent_name": "nop",
            "job_name": "older",
            "trial_name": "older__trial",
            "finished_at": "2026-09-01T00:00:00Z",
            "est_cost_usd": 9.0,
        },
        {
            "task_version_digest": "sha256:abc",
            "agent_name": "terminus",
            "job_name": "model",
            "trial_name": "model__trial",
            "finished_at": "2026-09-30T01:00:00Z",
        },
    ]
    rows = build_health_rows(pool, qualification, [tmp_path], tmp_path)
    by_id = {row["task_id"]: row for row in rows}
    assert by_id["t-1"]["label"] == "sound"
    assert by_id["t-1"]["nop_job_name"] == "job"
    assert by_id["t-1"]["nop_cost_usd"] == 0.1
    assert by_id["t-1"]["nop_reward"] == 0.0
    assert by_id["t-1"]["nop_tests_applied"] is True
    assert by_id["t-1"]["nop_tests_ran"] is True
    assert by_id["t-1"]["nop_setup_error"] is None
    assert by_id["t-1"]["nop_setup_error_excused"] is False
    assert by_id["t-1"]["nop_exception_type"] is None
    assert by_id["t-1"]["image_mib"] == 12
    assert by_id["t-1"]["project_key"] == "pkg"
    assert by_id["t-1"]["project_key_source"] == "split_group"
    assert by_id["t-2"]["label"] == "unknown"
    assert by_id["t-2"]["nop_tests_applied"] is None
    assert by_id["t-2"]["image_mib"] is None
    assert by_id["t-2"]["project_key"] == "t-2"
    assert by_id["t-2"]["project_key_source"] == "task_id"
    assert trial.is_dir() and task.is_dir()


_GIT_BLOCK = """\
# mimo answer-leak blocklist
0.0.0.0 github.com
0.0.0.0 codeload.github.com
0.0.0.0 raw.githubusercontent.com
0.0.0.0 gitlab.com
0.0.0.0 bitbucket.org
"""


def _blocklist(task: Path, text: str) -> None:
    path = task / "environment" / "setup" / "files" / "blocklist"
    path.parent.mkdir(parents=True)
    path.write_text(text)


def test_blocklist_parsing_and_leak_channel_precedence(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    _blocklist(blocked, _GIT_BLOCK)
    assert blocklist_facts(blocked) == {"leak_git_blocked": True, "leak_pypi_blocked": False}

    open_git = tmp_path / "open"
    _blocklist(open_git, "0.0.0.0 github.com\n0.0.0.0 gitlab.com\n")
    assert blocklist_facts(open_git)["leak_git_blocked"] is False
    assert blocklist_facts(open_git)["leak_pypi_blocked"] is False

    pypi_blocked = tmp_path / "pypi"
    _blocklist(pypi_blocked, _GIT_BLOCK + "0.0.0.0 files.pythonhosted.org\n")
    assert blocklist_facts(pypi_blocked)["leak_pypi_blocked"] is True
    assert blocklist_facts(tmp_path / "missing")["leak_git_blocked"] is None

    issue = {
        "number": 260,
        "is_pr": False,
        "state": "closed",
        "closed_at": "2019-08-27T00:00:00Z",
        "url": "https://github.com/Pylons/waitress/issues/260",
    }
    released = {
        "pypi_project": "waitress",
        "match": "repo_url",
        "latest_version": "2.1.2",
        "last_upload": "2023-01-01T00:00:00Z",
        "releases": 12,
        "repo_url": "https://github.com/Pylons/waitress",
        "repo_source": "split_group",
        "issue": issue,
        "released_after_close": True,
        "first_release_after_close": "1.4.0",
    }
    fix = leak_assessment(True, False, released, True)
    assert fix["leak_channel"] == "pypi_fix_released"
    assert fix["leak_issue_url"].endswith("/260")
    assert fix["leak_issue_closed_at"] == "2019-08-27T00:00:00Z"
    assert fix["leak_first_release_after_close"] == "1.4.0"
    # a dated release beats an open git host and a plain package match
    assert leak_assessment(False, False, released, True)["leak_channel"] == "pypi_fix_released"

    package = {
        **released,
        "issue": None,
        "released_after_close": None,
        "first_release_after_close": None,
    }
    assert leak_assessment(True, False, package, True)["leak_channel"] == "pypi_package"
    # PyPI blocked, so the dated release is not a download; the repo remains
    held = leak_assessment(True, True, released, True)
    assert held["leak_channel"] == "git_only"
    assert "rewrite /etc/hosts and bypass the answer-leak blocklist" in held["leak_note"]

    git_only = {
        "pypi_project": None,
        "match": None,
        "releases": 0,
        "repo_url": "https://github.com/Pylons/waitress",
        "repo_source": "split_group",
        "issue": issue,
        "released_after_close": False,
        "first_release_after_close": None,
    }
    noted = leak_assessment(True, False, git_only, True)
    assert noted["leak_channel"] == "git_only"
    assert "no release after issue close" in noted["leak_note"]
    sealed = leak_assessment(True, False, git_only, False)
    assert sealed["leak_channel"] == "git_only"
    assert "agent user is not root" in sealed["leak_note"]

    assert leak_assessment(True, False, None, True)["leak_channel"] == "unknown"
    assert leak_assessment(None, None, released, True)["leak_channel"] == "unknown"
    assert (
        leak_assessment(
            True, False, {"releases": 0, "issue": None, "released_after_close": None}, True
        )["leak_channel"]
        == "none_found"
    )

    for name, text, expected in (
        ("unset", "[agent]\ntimeout_sec = 1\n", True),
        ("root", '[agent]\nuser = "root"\n', True),
        ("other", '[agent]\nuser = "agent"\n', False),
    ):
        task = tmp_path / name
        task.mkdir()
        (task / "task.toml").write_text(text)
        assert hosts_bypassable(task) is expected

    summary = summarize_health(
        [
            {
                "label": "sound",
                "leak_channel": "pypi_fix_released",
                "leak_pypi_match": "repo_url",
                "reasons": [],
                "nop_cost_usd": 0,
                "split": "train",
                "project_key": "a",
            },
            {
                "label": "unknown",
                "leak_channel": "pypi_fix_released",
                "leak_pypi_match": "name",
                "reasons": [],
                "nop_cost_usd": 0,
                "split": "train",
                "project_key": "b",
            },
            {
                "label": "sound",
                "leak_channel": "git_only",
                "leak_pypi_match": None,
                "reasons": [],
                "nop_cost_usd": 0,
                "split": "heldout",
                "project_key": "c",
            },
        ]
    )
    assert "| pypi_fix_released | 2 |" in summary
    assert "| git_only | 1 |" in summary
    assert "| none_found | 0 |" in summary
    assert "| pypi_fix_released | 1 | 0 | 0 | 1 |" in summary
    assert "| git_only | 1 | 0 | 0 | 0 |" in summary
