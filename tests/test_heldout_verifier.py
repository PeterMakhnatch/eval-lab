"""Fixture tests for the HAR-197 offline holdout verifier payload.

These tests are developer-authored isolated fixtures covering exact
base+patch+test overlay, genuine pass/fail result parsing, no false score
on errors/skips/zero tests/timeouts, and malicious paths/digest changes.

NOTE: authored for parent verification -- the assigning worker must not
execute this file. The end-to-end cases below build tiny throwaway git
repositories under ``tmp_path`` and run the payload's own git/test
drivers through :func:`evallab.heldout_verifier.run_verification`; they
are safe to run in CI (``git`` and ``pytest`` required) but were not run
at authoring time.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from evallab import heldout_verifier as hv

GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="git executable not available")

PASS_FINGERPRINT = "a" * 64


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _test_entry(
    path: str = "tests/test_holdout_x.py",
    qual: str = "test_alpha",
    change: str = "added",
) -> dict[str, object]:
    return {
        "node_id": path + "::" + qual.replace(".", "::"),
        "path": path,
        "qualname": qual,
        "fingerprint": PASS_FINGERPRINT,
        "change": change,
    }


def _file_entry(
    path: str, text: str, mode: str | None = "100644"
) -> dict[str, object]:
    if mode is None:
        return {"path": path, "mode": None, "content_base64": None, "sha256": None}
    data = text.encode("utf-8")
    return {"path": path, "mode": mode, "content_base64": _b64(data), "sha256": _sha(data)}


def _suite(
    workdir: str,
    tests: list[dict[str, object]] | None = None,
    files: list[dict[str, object]] | None = None,
    status: str = "ready",
) -> dict[str, object]:
    if tests is None:
        tests = [_test_entry()]
    if files is None:
        files = [_file_entry("tests/test_holdout_x.py", "def test_alpha():\n    assert True\n")]
    return {
        "schema_version": "heldout-suite/v1",
        "task_name": "fixtures/holdout",
        "image": "fixtures/img@sha256:" + "b" * 64,
        "workdir": workdir,
        "base_commit": "c" * 40,
        "fix_commit": "d" * 40,
        "fix_parent": "e" * 40,
        "hidden_patch_sha256": "f" * 64,
        "status": status,
        "tests": tests,
        "files": files,
        "exclusions": [],
        "refusals": [],
        "notes": [],
    }


def _write_run(
    root: Path,
    suite: dict[str, object],
    *,
    framework: str = "pytest",
    command: list[str] | None = None,
    agent_diff: bytes = b"",
    bootstrap_rel: str | None = None,
    timeout_sec: int = 120,
    env: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Write a config dir (agent.diff + heldout-run.json); return (config, logs)."""
    cfg_dir = root / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.diff").write_bytes(agent_diff)
    if command is None:
        command = [sys.executable, "-m", "pytest", "-q"]
    config = {
        "schema_version": "heldout-run/v1",
        "suite": suite,
        "agent_patch": {"path": "agent.diff", "sha256": _sha(agent_diff)},
        "framework": framework,
        "command": command,
        "env": env,
        "bootstrap": bootstrap_rel,
        "timeout_sec": timeout_sec,
    }
    config_path = cfg_dir / "heldout-run.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path, root / "logs"


def _read_result(logs: Path) -> dict[str, object]:
    return json.loads((logs / "heldout-result.json").read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _git_repo(root: Path, name: str = "repo") -> tuple[Path, str]:
    repo = root / name
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


# --------------------------------------------------------------------------- #
# Payload hygiene: stdlib-only, legacy-interpreter syntax
# --------------------------------------------------------------------------- #


def test_payload_is_stdlib_only_with_no_fstrings() -> None:
    """The in-image payload must not import evallab/host packages or f-strings."""
    source = Path(hv.__file__).read_text(encoding="utf-8")
    assert "from __future__" not in source
    tree = ast.parse(source)
    allowed = {
        "argparse",
        "base64",
        "errno",
        "hashlib",
        "json",
        "os",
        "re",
        "stat",
        "subprocess",
        "sys",
        "time",
        "xml",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None
            assert node.module.split(".")[0] in allowed, node.module
        elif isinstance(node, ast.JoinedStr):
            raise AssertionError("f-string found; Python 3.5 images cannot parse it")


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "good",
    [
        "tests/test_x.py",
        "tests/fixtures/data.json",
        "a/b/c.txt",
        "support/helper.py",
    ],
)
def test_safe_relpaths_accepted(good: str) -> None:
    assert hv.is_safe_relpath(good)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "/abs/path.py",
        "../escape.py",
        "a/../../escape.py",
        "a//b.py",
        "a/./b.py",
        ".git/hooks/post-checkout",
        "a/.git/config",
        ".git",
        "C:/evil.py",
        "a/\x00b.py",
    ],
)
def test_unsafe_relpaths_rejected(bad: str) -> None:
    assert not hv.is_safe_relpath(bad)
    assert hv.relpath_error(bad) is not None


def test_resolve_within_blocks_escape(tmp_path: Path) -> None:
    base = str(tmp_path)
    assert hv.resolve_within(base, "tests/a.py") is not None
    assert hv.resolve_within(base, "../evil.py") is None
    assert hv.resolve_within(base, "/abs.py") is None


# --------------------------------------------------------------------------- #
# Suite / config validation
# --------------------------------------------------------------------------- #


def test_valid_suite_passes(tmp_path: Path) -> None:
    assert hv.validate_suite(_suite(str(tmp_path))) == []


def test_suite_rejects_malicious_paths_and_digest_changes(tmp_path: Path) -> None:
    bad_tests = [_test_entry(path="../evil.py", qual="test_x")]
    errors = hv.validate_suite(_suite(str(tmp_path), tests=bad_tests))
    assert any(error.startswith("unsafe_path:") for error in errors)
    tampered = _file_entry("tests/test_holdout_x.py", "def test_alpha():\n    assert True\n")
    assert isinstance(tampered["content_base64"], str)
    tampered["content_base64"] = _b64(b"different bytes")
    errors = hv.validate_suite(_suite(str(tmp_path), files=[tampered]))
    assert any(error.startswith("digest_mismatch:") for error in errors)


def test_suite_rejects_node_id_qualname_mismatch(tmp_path: Path) -> None:
    entry = _test_entry()
    entry["node_id"] = "tests/test_holdout_x.py::wrong_name"
    errors = hv.validate_suite(_suite(str(tmp_path), tests=[entry]))
    assert any("node_id" in error for error in errors)


def test_suite_rejects_duplicates_and_ready_without_tests(tmp_path: Path) -> None:
    entry = _test_entry()
    errors = hv.validate_suite(_suite(str(tmp_path), tests=[entry, dict(entry)]))
    assert any("duplicate node_id" in error for error in errors)
    errors = hv.validate_suite(_suite(str(tmp_path), tests=[]))
    assert any("no selected tests" in error for error in errors)


def test_suite_deletion_entry_consistency(tmp_path: Path) -> None:
    deletion = {"path": "tests/old.py", "mode": None, "content_base64": None, "sha256": None}
    assert hv.validate_suite(_suite(str(tmp_path), files=[deletion])) == []
    broken = dict(deletion)
    broken["sha256"] = "a" * 64
    errors = hv.validate_suite(_suite(str(tmp_path), files=[broken]))
    assert errors != []


def test_config_rejects_pytest_bootstrap_and_bad_timeout(tmp_path: Path) -> None:
    suite = _suite(str(tmp_path))
    config_path, _ = _write_run(tmp_path, suite, bootstrap_rel="setup.py")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert any("bootstrap" in error for error in hv.validate_config(config))
    config["bootstrap"] = None
    config["timeout_sec"] = 0
    assert any("timeout_sec" in error for error in hv.validate_config(config))
    config["timeout_sec"] = hv.MAX_TIMEOUT_SEC + 1
    assert any("timeout_sec" in error for error in hv.validate_config(config))
    config["timeout_sec"] = 60
    config["framework"] = "nosuch"
    assert any("framework" in error for error in hv.validate_config(config))


# --------------------------------------------------------------------------- #
# Agent-diff scanning
# --------------------------------------------------------------------------- #


def _scan(tmp_path: Path, text: str) -> tuple[list[str], int]:
    diff = tmp_path / "agent.diff"
    diff.write_bytes(text.encode("utf-8"))
    return hv.scan_agent_diff(str(diff))


CLEAN_DIFF = """diff --git a/pkg/core.py b/pkg/core.py
index 1111111..2222222 100644
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1,2 +1,2 @@
 def answer():
-    return 1
+    return 2
"""


def test_scan_clean_diff(tmp_path: Path) -> None:
    problems, count = _scan(tmp_path, CLEAN_DIFF)
    assert problems == []
    assert count == 1


def test_scan_rejects_binary_placeholder(tmp_path: Path) -> None:
    problems, _ = _scan(
        tmp_path,
        "diff --git a/img/logo.png b/img/logo.png\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "Binary files /dev/null and b/img/logo.png differ\n",
    )
    assert any(problem.startswith("binary_placeholder:") for problem in problems)


@pytest.mark.parametrize(
    "header",
    [
        "diff --git a/x b/../../etc/evil\n",
        "diff --git a/.git/hooks/x b/.git/hooks/x\n",
        "diff --git a/x b/C:/evil\n",
    ],
)
def test_scan_rejects_unsafe_targets(tmp_path: Path, header: str) -> None:
    problems, _ = _scan(tmp_path, header)
    assert any(problem.startswith("unsafe_path:") for problem in problems)


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_scan_rejects_symlink_and_submodule_modes(tmp_path: Path, mode: str) -> None:
    problems, _ = _scan(
        tmp_path,
        "diff --git a/link b/link\nnew file mode {0}\nindex 0000000..1234567\n".format(mode),
    )
    assert any(problem.startswith("unsafe_path:") for problem in problems)


# --------------------------------------------------------------------------- #
# Overlay semantics (no git needed)
# --------------------------------------------------------------------------- #


def test_overlay_writes_exact_bytes_and_modes(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    files = [
        _file_entry("tests/new_test.py", "x = 1\n"),
        _file_entry("support/run.sh", "#!/bin/sh\n", mode="100755"),
    ]
    assert hv.overlay_suite_files(str(workdir), files) is None
    assert (workdir / "tests" / "new_test.py").read_bytes() == b"x = 1\n"
    assert stat.S_IMODE(os.stat(str(workdir / "support" / "run.sh")).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(str(workdir / "tests" / "new_test.py")).st_mode) == 0o644


def test_overlay_deletes_and_refuses_digest_mismatch(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    victim = workdir / "tests" / "old.py"
    victim.parent.mkdir(parents=True)
    victim.write_text("old", encoding="utf-8")
    deletion = {"path": "tests/old.py", "mode": None, "content_base64": None, "sha256": None}
    assert hv.overlay_suite_files(str(workdir), [deletion]) is None
    assert not victim.exists()
    tampered = _file_entry("tests/new.py", "hello")
    tampered["sha256"] = "0" * 64
    error = hv.overlay_suite_files(str(workdir), [tampered])
    assert error is not None and error.startswith("digest_mismatch:")
    assert not (workdir / "tests" / "new.py").exists()


def test_overlay_refuses_symlink_escape_and_traversal(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    outside = tmp_path / "outside"
    workdir.mkdir()
    outside.mkdir()
    os.symlink(str(outside), str(workdir / "slink"))
    entry = _file_entry("slink/evil.py", "evil")
    error = hv.overlay_suite_files(str(workdir), [entry])
    assert error is not None and error.startswith("unsafe_path:")
    assert list(outside.iterdir()) == []
    traversal = _file_entry("../evil.py", "evil")
    error = hv.overlay_suite_files(str(workdir), [traversal])
    assert error is not None and error.startswith("unsafe_path:")


# --------------------------------------------------------------------------- #
# Result parsing: genuine pass/fail, no false score
# --------------------------------------------------------------------------- #

PASS_XML = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<testsuite errors="0" failures="0" name="pytest" skipped="0" tests="2">'
    b'<testcase classname="tests.test_x" name="test_a"/>'
    b'<testcase classname="tests.test_x" name="test_b"/>'
    b"</testsuite>"
)

FAIL_XML = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<testsuite errors="0" failures="1" name="pytest" skipped="0" tests="2">'
    b'<testcase classname="tests.test_x" name="test_a"/>'
    b'<testcase classname="tests.test_x" name="test_b">'
    b'<failure message="assert 1 == 2">traceback</failure>'
    b"</testcase></testsuite>"
)

SETUP_ERROR_XML = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<testsuite errors="1" failures="0" name="pytest" skipped="0" tests="1">'
    b'<testcase classname="tests.test_x" name="test_a">'
    b'<error message="fixture &apos;db&apos; not found">traceback</error>'
    b"</testcase></testsuite>"
)

COLLECTION_XML = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<testsuite errors="1" failures="0" name="pytest" skipped="0" tests="1">'
    b"<testcase classname=\"\" name=\"collection\">"
    b"<error message=\"ERROR collecting tests/test_x.py\">ModuleNotFoundError</error>"
    b"</testcase></testsuite>"
)

SKIP_ONLY_XML = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<testsuite errors="0" failures="0" name="pytest" skipped="2" tests="2">'
    b'<testcase classname="tests.test_x" name="test_a"><skipped message="x"/></testcase>'
    b'<testcase classname="tests.test_x" name="test_b"><skipped message="y"/></testcase>'
    b"</testsuite>"
)

PARTIAL_SKIP_XML = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<testsuite errors="0" failures="0" name="pytest" skipped="1" tests="3">'
    b'<testcase classname="tests.test_x" name="test_a"/>'
    b'<testcase classname="tests.test_x" name="test_b"/>'
    b'<testcase classname="tests.test_x" name="test_c"><skipped message="z"/></testcase>'
    b"</testsuite>"
)


def test_pytest_pass_and_fail_score_numeric() -> None:
    assert hv.decide_pytest_outcome(hv.parse_junit_xml(PASS_XML)) == (
        "passed",
        1.0,
        "pass_observed",
    )
    assert hv.decide_pytest_outcome(hv.parse_junit_xml(FAIL_XML)) == (
        "failed",
        0.0,
        "fail_observed",
    )


def test_pytest_never_scores_errors_skips_or_empty() -> None:
    assert hv.decide_pytest_outcome(hv.parse_junit_xml(SETUP_ERROR_XML))[0] == "unscored"
    assert hv.decide_pytest_outcome(hv.parse_junit_xml(COLLECTION_XML))[1] is None
    assert hv.decide_pytest_outcome(hv.parse_junit_xml(COLLECTION_XML))[2] == (
        "collection_error"
    )
    outcome, holdout, reason = hv.decide_pytest_outcome(hv.parse_junit_xml(SKIP_ONLY_XML))
    assert (outcome, holdout, reason) == ("unscored", None, "skip_only")
    assert hv.decide_pytest_outcome(hv.parse_junit_xml(b"not xml")) == (
        "unscored",
        None,
        "missing_report",
    )
    empty = PASS_XML.replace(b'tests="2"', b'tests="0"')
    assert hv.decide_pytest_outcome(hv.parse_junit_xml(empty))[2] == "zero_tests"


def test_pytest_partial_skip_with_passes_scores() -> None:
    assert hv.decide_pytest_outcome(hv.parse_junit_xml(PARTIAL_SKIP_XML)) == (
        "passed",
        1.0,
        "pass_observed",
    )


OK_TEXT = (
    "test_a (tests.test_x.TestX) ... ok\n"
    "test_b (tests.test_x.TestX) ... ok\n"
    "----------------------------------------------------------------------\n"
    "Ran 2 tests in 0.010s\n"
    "\n"
    "OK\n"
)

FAIL_TEXT = (
    "test_a (tests.test_x.TestX) ... ok\n"
    "test_b (tests.test_x.TestX) ... FAIL\n"
    "======================================================================\n"
    "FAIL: test_b (tests.test_x.TestX)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "...", line 1, in test_b\n'
    "AssertionError: 1 != 2\n"
    "----------------------------------------------------------------------\n"
    "Ran 2 tests in 0.010s\n"
    "\n"
    "FAILED (failures=1)\n"
)

LOADER_TEXT = (
    "ERROR: test_x (unittest.loader._FailedTest)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    "ModuleNotFoundError: No module named 'tests.test_x'\n"
    "----------------------------------------------------------------------\n"
    "Ran 1 test in 0.001s\n"
    "\n"
    "FAILED (errors=1)\n"
)

BODY_ERROR_TEXT = (
    "test_b (tests.test_x.TestX) ... ERROR\n"
    "======================================================================\n"
    "ERROR: test_b (tests.test_x.TestX)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "...", line 1, in test_b\n'
    "TypeError: unsupported operand\n"
    "----------------------------------------------------------------------\n"
    "Ran 1 test in 0.001s\n"
    "\n"
    "FAILED (errors=1)\n"
)

SKIP_ONLY_TEXT = (
    "test_a (tests.test_x.TestX) ... skipped 'no db'\n"
    "test_b (tests.test_x.TestX) ... skipped 'no db'\n"
    "----------------------------------------------------------------------\n"
    "Ran 2 tests in 0.001s\n"
    "\n"
    "OK (skipped=2)\n"
)


def test_unittest_pass_fail_and_loader_errors() -> None:
    assert hv.decide_unittest_outcome(hv.parse_unittest_output(OK_TEXT)) == (
        "passed",
        1.0,
        "pass_observed",
    )
    assert hv.decide_unittest_outcome(hv.parse_unittest_output(FAIL_TEXT)) == (
        "failed",
        0.0,
        "fail_observed",
    )
    # Test-body exceptions are observed execution outcomes, not infra noise.
    assert hv.decide_unittest_outcome(hv.parse_unittest_output(BODY_ERROR_TEXT)) == (
        "failed",
        0.0,
        "fail_observed",
    )
    outcome, holdout, reason = hv.decide_unittest_outcome(
        hv.parse_unittest_output(LOADER_TEXT)
    )
    assert (outcome, holdout, reason) == ("unscored", None, "collection_error")


def test_unittest_never_scores_skips_zero_or_garbage() -> None:
    assert hv.decide_unittest_outcome(hv.parse_unittest_output(SKIP_ONLY_TEXT)) == (
        "unscored",
        None,
        "skip_only",
    )
    assert hv.decide_unittest_outcome(hv.parse_unittest_output("Ran 0 tests\n\nOK\n"))[2] == (
        "zero_tests"
    )
    assert hv.decide_unittest_outcome(hv.parse_unittest_output("hello")) == (
        "unscored",
        None,
        "missing_report",
    )


def test_node_id_to_unittest_label() -> None:
    assert (
        hv.node_id_to_unittest_label("tests/test_x.py::TestX::test_b")
        == "tests.test_x.TestX.test_b"
    )
    assert hv.node_id_to_unittest_label("tests/test_x.py::test_a") == "tests.test_x.test_a"
    with pytest.raises(ValueError):
        hv.node_id_to_unittest_label("tests/test_x.py")
    with pytest.raises(ValueError):
        hv.node_id_to_unittest_label("tests/test_x.txt::test_a")


def test_run_command_timeout_and_spawn_error(tmp_path: Path) -> None:
    sleepy = hv.run_command(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        str(tmp_path),
        {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        0.2,
    )
    assert sleepy["timed_out"] is True
    missing = hv.run_command(["/nonexistent-binary-xyz", "x"], str(tmp_path), {}, 10)
    assert missing["returncode"] is None
    assert missing["error"] is not None


# --------------------------------------------------------------------------- #
# Run-level: validation before workspace, stale rewards, report shape
# --------------------------------------------------------------------------- #


def test_invalid_config_writes_unscored_result_without_reward(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    bad = tmp_path / "heldout-run.json"
    bad.write_text("{oops", encoding="utf-8")
    assert hv.run_verification(str(bad), str(logs)) == 2
    result = _read_result(logs)
    assert result["schema_version"] == "heldout-result/v1"
    assert result["outcome"] == "unscored"
    assert result["holdout_pass"] is None
    assert set(result["counts"]) == {"tests", "failures", "errors", "skipped"}
    assert isinstance(result["reason"], str)
    assert not (logs / "reward.json").exists()


def test_suite_not_ready_is_unscored(tmp_path: Path) -> None:
    suite = _suite(str(tmp_path), status="no_extra_tests")
    config_path, logs = _write_run(tmp_path, suite)
    assert hv.run_verification(str(config_path), str(logs)) == 2
    assert _read_result(logs)["reason"] == "suite_not_ready"
    assert not (logs / "reward.json").exists()


def test_patch_digest_change_is_unscored_and_repo_untouched(tmp_path: Path) -> None:
    suite = _suite(str(tmp_path))
    config_path, logs = _write_run(tmp_path, suite, agent_diff=b"diff-bytes")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["agent_patch"]["sha256"] = "0" * 64
    config_path.write_text(json.dumps(config), encoding="utf-8")
    marker = tmp_path / "repo-marker.txt"
    marker.write_text("pristine", encoding="utf-8")
    assert hv.run_verification(str(config_path), str(logs)) == 2
    result = _read_result(logs)
    assert result["reason"] == "hash_mismatch"
    assert result["holdout_pass"] is None
    assert marker.read_text(encoding="utf-8") == "pristine"
    assert not (logs / "reward.json").exists()


def test_stale_rewards_cleared_on_unscored_attempt(tmp_path: Path) -> None:
    suite = _suite(str(tmp_path))
    config_path, logs = _write_run(tmp_path, suite, agent_diff=b"diff-bytes")
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "reward.json").write_text('{"holdout_pass": 1.0}', encoding="utf-8")
    (logs / "reward.txt").write_text("stale", encoding="utf-8")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["agent_patch"]["sha256"] = "0" * 64
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert hv.run_verification(str(config_path), str(logs)) == 2
    assert not (logs / "reward.json").exists()
    assert not (logs / "reward.txt").exists()


def test_binary_placeholder_diff_is_unscored_before_workspace(tmp_path: Path) -> None:
    suite = _suite(str(tmp_path))
    diff = (
        "diff --git a/img/logo.png b/img/logo.png\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "Binary files /dev/null and b/img/logo.png differ\n"
    ).encode("utf-8")
    config_path, logs = _write_run(tmp_path, suite, agent_diff=diff)
    assert hv.run_verification(str(config_path), str(logs)) == 2
    assert _read_result(logs)["reason"] == "binary_placeholder"


def test_malicious_suite_path_never_reaches_workspace(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "keep.txt").write_text("keep", encoding="utf-8")
    files = [_file_entry("../../evil.py", "evil")]
    suite = _suite(str(workdir), files=files)
    config_path, logs = _write_run(tmp_path, suite)
    assert hv.run_verification(str(config_path), str(logs)) == 2
    assert _read_result(logs)["reason"] == "unsafe_path"
    assert (workdir / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "evil.py").exists()


def test_unittest_requires_bare_interpreter_prefix(tmp_path: Path) -> None:
    suite = _suite(str(tmp_path))
    config_path, logs = _write_run(
        tmp_path, suite, framework="unittest", command=[sys.executable, "-m", "unittest"]
    )
    assert hv.run_verification(str(config_path), str(logs)) == 2
    assert _read_result(logs)["reason"] == "invalid_config"


def test_missing_bootstrap_is_unscored(tmp_path: Path) -> None:
    suite = _suite(str(tmp_path))
    config_path, logs = _write_run(
        tmp_path,
        suite,
        framework="unittest",
        command=[sys.executable],
        bootstrap_rel="test-bootstrap.py",
    )
    assert hv.run_verification(str(config_path), str(logs)) == 2
    assert _read_result(logs)["reason"] == "bootstrap_error"


# --------------------------------------------------------------------------- #
# End-to-end: exact base + patch + test overlay through real git and runners
# --------------------------------------------------------------------------- #

AGENT_TEST_WRONG = "from pkg.core import answer\ndef test_answer():\n    assert answer() == 999\n"
SUITE_TEST_PASS = "from pkg.core import answer\ndef test_answer():\n    assert answer() == 2\n"
SUITE_TEST_FAIL = "from pkg.core import answer\ndef test_answer():\n    assert answer() == 3\n"


def _agent_diff_for(repo: Path, base: str, *, agent_test: str | None) -> bytes:
    (repo / "pkg" / "core.py").write_text("def answer():\n    return 2\n", encoding="utf-8")
    if agent_test is not None:
        tests_dir = repo / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "test_holdout_answer.py").write_text(agent_test, encoding="utf-8")
    _git(repo, "add", "-A")
    return _git(repo, "diff", "--cached", base).encode("utf-8")


def _answer_suite(repo: Path, test_text: str) -> dict[str, object]:
    path = "tests/test_holdout_answer.py"
    return _suite(
        str(repo),
        tests=[_test_entry(path=path, qual="test_answer")],
        files=[_file_entry(path, test_text)],
    )


@requires_git
def test_e2e_pytest_pass_overlays_authoritative_tests(tmp_path: Path) -> None:
    """Agent fix + authoritative overlay: agent-edited test cannot survive."""
    repo, base = _git_repo(tmp_path)
    diff = _agent_diff_for(repo, base, agent_test=AGENT_TEST_WRONG)
    suite = _answer_suite(repo, SUITE_TEST_PASS)
    suite["base_commit"] = base
    config_path, logs = _write_run(
        tmp_path, suite, agent_diff=diff, env={"PYTHONPATH": str(repo)}
    )
    assert hv.run_verification(str(config_path), str(logs)) == 0
    assert json.loads((logs / "reward.json").read_text(encoding="utf-8")) == {
        "holdout_pass": 1.0
    }
    result = _read_result(logs)
    assert result["outcome"] == "passed"
    assert result["holdout_pass"] == 1.0
    assert result["counts"]["tests"] >= 1
    assert (repo / "pkg" / "core.py").read_text(encoding="utf-8") == (
        "def answer():\n    return 2\n"
    )
    assert (repo / "tests" / "test_holdout_answer.py").read_text(
        encoding="utf-8"
    ) == SUITE_TEST_PASS


@requires_git
def test_e2e_pytest_failure_scores_zero(tmp_path: Path) -> None:
    repo, base = _git_repo(tmp_path)
    diff = _agent_diff_for(repo, base, agent_test=None)
    suite = _answer_suite(repo, SUITE_TEST_FAIL)
    suite["base_commit"] = base
    config_path, logs = _write_run(
        tmp_path, suite, agent_diff=diff, env={"PYTHONPATH": str(repo)}
    )
    assert hv.run_verification(str(config_path), str(logs)) == 0
    assert json.loads((logs / "reward.json").read_text(encoding="utf-8")) == {
        "holdout_pass": 0.0
    }
    result = _read_result(logs)
    assert result["outcome"] == "failed"
    assert result["counts"]["failures"] >= 1


@requires_git
def test_e2e_empty_diff_is_valid_unchanged_workspace(tmp_path: Path) -> None:
    """A present but empty agent.diff skips git apply; base behavior still scores."""
    repo, base = _git_repo(tmp_path)
    text = "from pkg.core import answer\ndef test_answer():\n    assert answer() == 1\n"
    suite = _answer_suite(repo, text)
    suite["base_commit"] = base
    config_path, logs = _write_run(
        tmp_path, suite, agent_diff=b"", env={"PYTHONPATH": str(repo)}
    )
    assert hv.run_verification(str(config_path), str(logs)) == 0
    assert _read_result(logs)["outcome"] == "passed"


@requires_git
def test_e2e_timeout_is_unscored_without_reward(tmp_path: Path) -> None:
    repo, base = _git_repo(tmp_path)
    slow = "import time\ndef test_answer():\n    time.sleep(30)\n"
    suite = _answer_suite(repo, slow)
    suite["base_commit"] = base
    config_path, logs = _write_run(
        tmp_path, suite, agent_diff=b"", env={"PYTHONPATH": str(repo)}, timeout_sec=1
    )
    assert hv.run_verification(str(config_path), str(logs)) == 2
    result = _read_result(logs)
    assert result["outcome"] == "unscored"
    assert result["reason"] == "timeout"
    assert result["holdout_pass"] is None
    assert not (logs / "reward.json").exists()


@requires_git
def test_e2e_unittest_with_trusted_bootstrap(tmp_path: Path) -> None:
    repo, base = _git_repo(tmp_path)
    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add tests package")
    base = _git(repo, "rev-parse", "HEAD").strip()
    diff = _agent_diff_for(repo, base, agent_test=None)
    body = (
        "import unittest\n"
        "from pkg.core import answer\n"
        "class TestAnswer(unittest.TestCase):\n"
        "    def test_answer(self):\n"
        "        self.assertEqual(answer(), 2)\n"
    )
    path = "tests/test_holdout_u.py"
    suite = _suite(
        str(repo),
        tests=[_test_entry(path=path, qual="TestAnswer.test_answer")],
        files=[_file_entry(path, body)],
    )
    suite["base_commit"] = base
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.diff").write_bytes(diff)
    (cfg_dir / "test-bootstrap.py").write_text(
        "open('booted.marker', 'w').write('ok')\n", encoding="utf-8"
    )
    config = {
        "schema_version": "heldout-run/v1",
        "suite": suite,
        "agent_patch": {"path": "agent.diff", "sha256": _sha(diff)},
        "framework": "unittest",
        "command": [sys.executable],
        "env": {"PYTHONPATH": str(repo)},
        "bootstrap": "test-bootstrap.py",
        "timeout_sec": 120,
    }
    config_path = cfg_dir / "heldout-run.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    logs = tmp_path / "logs"
    assert hv.run_verification(str(config_path), str(logs)) == 0
    result = _read_result(logs)
    assert result["outcome"] == "passed"
    assert json.loads((logs / "reward.json").read_text(encoding="utf-8")) == {
        "holdout_pass": 1.0
    }
    assert (repo / "booted.marker").read_text(encoding="utf-8") == "ok"
